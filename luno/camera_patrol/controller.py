"""
controller.py
==============

`CameraPatrolModule` - a `Module` (Event-Bus-shaped, same interface
`ToolManagerBridgeModule`/`ProactiveModule` already implement - see
`luno/core/module_manager.py::Module`) that runs a bounded, deterministic,
stoppable-at-any-time sequence of Tapo PTZ presets.

--------------------------------------------------------------------
Architecture - reuses the existing PTZ foundation, never a second one
--------------------------------------------------------------------
This module NEVER imports `pytapo`, never talks to `RealCameraPTZHandler`/
`MockCameraPTZHandler` directly, and never constructs a `ToolManager` of
its own. Every actual camera movement goes out through the EXACT SAME
`tool_requested` -> `ToolManagerBridgeModule` -> `ToolManager` ->
`camera_ptz` handler round trip that a manual voice command already uses
(see `main_runtime_demo.py::PlannerBridgeModule._tool_bridge_handler` /
`RuntimeDemoConsole._execute_tool` for the two existing callers of this
exact pattern - `_goto_preset()`/`_return_home()` below are a THIRD
caller, not a new mechanism). This is what lets patrol reuse Sprint 69/
70's error classification, timeout handling, and single-worker FIFO
serialization for free, with zero duplicated PTZ logic.

Each outgoing patrol-driven `tool_requested` is tagged
`parameters={"_patrol_origin": True}` - the ONLY marker this package
adds to the shared tool-call shape - so `ToolManagerBridgeModule`'s new
(Sprint 71) pre-dispatch hook (see that class's own docstring in
`main_runtime_demo.py`) can tell a patrol-issued PTZ command apart from
a genuine MANUAL one and enforce Phase 5's ownership rule: a manual
`camera_ptz` command arriving while a patrol is active stops the patrol
FIRST (synchronously, bounded), then lets the manual command proceed -
never two controllers issuing PTZ commands at once.

--------------------------------------------------------------------
Threading model
--------------------------------------------------------------------
`start()`/`stop()` (the `Module` lifecycle - called once each by
`ModuleManager` at process start/shutdown) do NOT themselves start a
background thread; a `CameraPatrolModule` with no patrol running costs
nothing beyond holding its own (tiny, in-memory) state. `start_patrol()`
spawns exactly one dedicated daemon thread (`luno-camera-patrol`) that
runs `_run_patrol()`; `stop_patrol()` sets a per-run `threading.Event`
and (bounded) joins that thread so callers can rely on "stopped" meaning
the camera has genuinely stopped receiving new patrol-issued commands,
not just "asked nicely" - see `stop_patrol()`'s own docstring for the
exact bound. Every voluntary wait inside the loop uses `Event.wait(...)`
(same idiom `luno/proactive/manager.py::_tick_loop` already established
for this codebase - see that module's own docstring for why), never
`time.sleep()` - so a stop request takes effect within one polling
tick, never "after N seconds" (Sprint 71 Phase 6's explicit `sleep(10)`
anti-pattern warning).

--------------------------------------------------------------------
Persistence
--------------------------------------------------------------------
Route DEFINITIONS (name/presets/dwell/loop/bounds) are loaded, read
fresh on every `start_patrol()` call (same "reloadable without a
restart" precedent `real_camera_ptz.py::_PTZConfig.from_env()` already
established), from `config/camera_patrol_routes.json` - the SAME kind of
named-entity config file this repo already uses for `scripts.config.
json`/`switches.config.json`, not a new database. RUNTIME state
(current_preset/index/cycle/state/started_at) is held ONLY in this
object's own Python attributes - never written to that file or any
other - see Sprint 71 Phase 8. Nothing in `PatrolRoute` (see `route.py`)
can hold a credential, RTSP URL, or session token in the first place.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import generate_id, log
from .route import PatrolRoute, PatrolRouteError, route_from_dict, validate_route
from .state import PatrolState, is_terminal

#: Active means "this run is still doing something" - COMPLETED/STOPPED/
#: FAILED deliberately excluded (a finished run's status stays visible
#: via get_status() until the NEXT start_patrol() call, rather than
#: snapping back to idle the instant it ends - useful for "kenapa patrol
#: berhenti?" without a separate history store).
_ACTIVE_STATES = frozenset({PatrolState.STARTING, PatrolState.MOVING, PatrolState.DWELLING})

#: How long stop_patrol() waits for the patrol thread to actually exit
#: before giving up and reporting anyway. Generous enough to cover the
#: real camera_ptz handler's own worst-case timeout (max_timeout_s=20.0 -
#: see real_camera_ptz.py::RealCameraPTZHandler) plus a small buffer -
#: NOT unbounded, so a caller (e.g. the Phase 5 manual-override hook,
#: itself running on ToolManagerBridgeModule's own single worker thread)
#: is never blocked forever by a wedged patrol thread.
_STOP_JOIN_TIMEOUT_S = 25.0

#: Per-goto_preset-call wait ceiling - deliberately a little above the
#: real handler's own max_timeout_s (20.0s) so a genuine handler timeout
#: is always what actually ends the wait, never this ceiling racing it.
_PTZ_CALL_TIMEOUT_S = 25.0

#: Polling granularity for both the cancellable "wait for tool_finished/
#: tool_failed" helper and dwell interruption checks - same order of
#: magnitude as ToolManager's own `_interruptible_sleep` step (0.05s),
#: cheap enough to never register as "busy looping" (Phase 12).
_POLL_INTERVAL_S = 0.1

DEFAULT_ROUTES_PATH = os.path.join("config", "camera_patrol_routes.json")
DEFAULT_ROUTE_NAME = "default"


def _metadata_payload(route: Optional[PatrolRoute], preset: Optional[str], index: Optional[int],
                       cycle: Optional[int], **extra: Any) -> Dict[str, Any]:
    """Sprint 71 Phase 9's own "payload harus metadata-only" rule -
    route/preset/cycle/index plus whatever small extra fields a specific
    event needs (e.g. `reason`), and NOTHING else - never a credential,
    frame, or raw exception object. `extra` values are always plain
    str/int/float/bool/None (checked at call sites, not here, since this
    helper has no way to inspect an arbitrary caller's intent - every
    call site in this file passes only such values)."""
    payload = {
        "route": route.name if route is not None else None,
        "preset": preset,
        "index": index,
        "cycle": cycle,
    }
    payload.update(extra)
    return payload


class CameraPatrolModule(Module):
    name = "camera_patrol"
    dependencies: List[str] = []

    def __init__(self, routes_path: str = DEFAULT_ROUTES_PATH) -> None:
        self._routes_path = routes_path
        self._event_bus: Any = None
        self._lock = threading.RLock()

        self._state: str = PatrolState.IDLE
        self._route: Optional[PatrolRoute] = None
        self._current_preset: Optional[str] = None
        self._current_index: Optional[int] = None
        self._current_cycle: Optional[int] = None
        self._started_at: Optional[float] = None
        self._reason: Optional[str] = None

        self._cancel_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    # -- Module lifecycle ---------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        """Cheap by design - no background thread until an actual patrol
        is requested (see class docstring)."""
        return None

    def stop(self) -> None:
        """Process shutdown - never leaves a patrol thread orphaned
        (Phase 12: "membuat thread/process tanpa lifecycle cleanup").
        Exceptions here are swallowed (per `Module.stop()`'s own
        contract: "a broken stop() must never block shutdown of the rest
        of the system")."""
        try:
            self.stop_patrol(reason="shutdown")
        except Exception as ex:  # pragma: no cover - defensive
            log(f"stop() raised while stopping an active patrol (ignored): {ex}", self.name)

    def health(self) -> ModuleHealthStatus:
        with self._lock:
            state = self._state
        if state == PatrolState.FAILED:
            return ModuleHealthStatus(healthy=True, message=f"last patrol FAILED: {self._reason}")
        return ModuleHealthStatus(healthy=True, message=f"patrol state: {state}")

    # -- public per-run API ---------------------------------------------------

    def start_patrol(self, route_name: Optional[str] = None) -> Dict[str, Any]:
        """Never blocks for the duration of the patrol - validates the
        route, spawns the background thread, and returns immediately.
        Matches every OTHER "refused_*"/"already_*" outcome in this
        project (e.g. `luno/tool_manager/result.py::ToolResult.fail`) by
        returning a small, honest result dict rather than raising for an
        expected refusal."""
        with self._lock:
            if self._state in _ACTIVE_STATES:
                return {"ok": False, "code": "refused_patrol_already_running", "message": "A patrol is already running - stop it first."}

            route, error = self._resolve_route(route_name)
            if error is not None:
                return error

            cancel_event = threading.Event()
            self._cancel_event = cancel_event
            self._route = route
            self._current_preset = None
            self._current_index = None
            self._current_cycle = 0
            self._started_at = time.monotonic()
            self._reason = None
            self._state = PatrolState.STARTING
            thread = threading.Thread(target=self._run_patrol, args=(route, cancel_event), daemon=True, name="luno-camera-patrol")
            self._thread = thread

        thread.start()
        return {"ok": True, "code": "patrol_started", "message": f"Patrol '{route.name}' started.", "route": route.name}

    def stop_patrol(self, reason: str = "user_requested") -> Dict[str, Any]:
        """Deterministic stop (Phase 6): sets the cancellation flag for
        the CURRENT run, then joins that run's thread with a bounded
        timeout so the caller can trust "stopped" actually means the
        patrol thread has exited (or, in the rare case it hasn't within
        `_STOP_JOIN_TIMEOUT_S`, says so honestly rather than lying)."""
        with self._lock:
            if self._state not in _ACTIVE_STATES:
                return {"ok": True, "code": "patrol_already_idle", "message": "No patrol is currently running."}
            cancel_event = self._cancel_event
            thread = self._thread

        if cancel_event is not None:
            cancel_event.set()
        if thread is not None:
            thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():  # pragma: no cover - defensive, should not happen
                log(f"stop_patrol({reason=}): patrol thread did not exit within {_STOP_JOIN_TIMEOUT_S}s", self.name)
                return {"ok": False, "code": "stop_timed_out", "message": "Stop requested, but the patrol thread has not exited yet."}

        with self._lock:
            state = self._state
        return {"ok": True, "code": "patrol_stopped", "message": "Patrol stopped.", "state": state}

    def get_status(self) -> Dict[str, Any]:
        """Read-only, in-memory only, metadata-only - safe to expose to
        the dashboard/voice status query directly (Phase 9/10's own
        "metadata-only" rule applies here too, not just to Event Bus
        payloads)."""
        with self._lock:
            route = self._route
            return {
                "state": self._state,
                "route": route.name if route is not None else None,
                "preset": self._current_preset,
                "index": self._current_index,
                "cycle": self._current_cycle,
                "max_cycles": route.max_cycles if route is not None else None,
                "reason": self._reason,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._state in _ACTIVE_STATES

    # -- Phase 5 - manual PTZ override hook ------------------------------------

    def on_manual_ptz_dispatch(self, tool_call: Dict[str, Any]) -> None:
        """Registered as a `ToolManagerBridgeModule` pre-dispatch hook
        (Sprint 71) - called for EVERY tool call about to execute,
        BEFORE it executes. A no-op for anything except a genuinely
        MANUAL `camera_ptz` call (patrol's own calls are tagged
        `_patrol_origin` - see module docstring) while a patrol is
        active: stops the patrol FIRST (synchronously, bounded - see
        `stop_patrol()`), so by the time this returns and the manual
        command actually executes, patrol has already relinquished the
        camera. Never raises - a hook that crashes must never block a
        manual command the user is actively waiting on."""
        try:
            if tool_call.get("tool") != "camera_ptz":
                return
            parameters = tool_call.get("parameters") or {}
            if parameters.get("_patrol_origin"):
                return
            if not self.is_running():
                return
            log("manual camera_ptz command received while a patrol is active - stopping patrol first", self.name)
            self.stop_patrol(reason="manual_override")
        except Exception as ex:  # pragma: no cover - defensive
            log(f"on_manual_ptz_dispatch raised (ignored, manual command proceeds): {ex}", self.name)

    # -- route resolution -------------------------------------------------------

    def _resolve_route(self, route_name: Optional[str]) -> tuple:
        """Returns `(route, None)` on success or `(None, error_dict)` on
        a resolvable-without-a-crash failure (unknown/missing route
        name, or a route that fails `validate_route()`). Reads the
        routes file FRESH every call - see class docstring."""
        routes = self._load_routes()
        wanted = (route_name or DEFAULT_ROUTE_NAME).strip()
        if not routes:
            return None, {
                "ok": False, "code": "refused_no_patrol_route",
                "message": "No patrol routes are configured. Add one to config/camera_patrol_routes.json first.",
            }
        route = routes.get(wanted.lower())
        if route is None:
            known = ", ".join(sorted(routes)) or "none"
            return None, {
                "ok": False, "code": "refused_no_patrol_route",
                "message": f"No patrol route called '{wanted}' - known routes: {known}",
            }
        try:
            validate_route(route)
        except PatrolRouteError as ex:
            return None, {"ok": False, "code": "refused_no_patrol_route", "message": f"Patrol route '{route.name}' is invalid: {ex}"}
        return route, None

    def _load_routes(self) -> Dict[str, PatrolRoute]:
        try:
            with open(self._routes_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            log(f"failed to read {self._routes_path} (treating as no routes configured): {ex}", self.name)
            return {}
        if not isinstance(raw, dict):
            return {}
        routes: Dict[str, PatrolRoute] = {}
        for name, data in raw.items():
            if not isinstance(data, dict):
                continue
            try:
                routes[name.strip().lower()] = route_from_dict(name, data)
            except Exception as ex:  # pragma: no cover - defensive
                log(f"skipping malformed route '{name}' in {self._routes_path}: {ex}", self.name)
        return routes

    # -- the patrol loop itself -------------------------------------------------

    def _run_patrol(self, route: PatrolRoute, cancel_event: threading.Event) -> None:
        try:
            self._set_state(PatrolState.STARTING)
            self._publish("camera_patrol_started", preset=None, index=None, cycle=0)

            cycle = 0
            while True:
                cycle += 1
                with self._lock:
                    self._current_cycle = cycle

                for index, preset in enumerate(route.presets):
                    if cancel_event.is_set():
                        self._finish(PatrolState.STOPPED, "user_requested")
                        return
                    if self._duration_exceeded(route):
                        self._finish_after_bound(route, cancel_event)
                        return

                    with self._lock:
                        self._current_index = index
                        self._current_preset = preset
                    self._set_state(PatrolState.MOVING)
                    self._publish("camera_patrol_moving", preset=preset, index=index, cycle=cycle)

                    ok, failure_reason = self._goto_preset(preset, cancel_event)
                    if cancel_event.is_set():
                        self._finish(PatrolState.STOPPED, "user_requested")
                        return
                    if not ok:
                        self._finish(PatrolState.FAILED, failure_reason, preset=preset, index=index, cycle=cycle)
                        return

                    self._set_state(PatrolState.DWELLING)
                    self._publish("camera_patrol_dwell", preset=preset, index=index, cycle=cycle)
                    stopped_during_dwell = cancel_event.wait(route.dwell_seconds)
                    if stopped_during_dwell:
                        self._finish(PatrolState.STOPPED, "user_requested")
                        return
                    if self._duration_exceeded(route):
                        self._finish_after_bound(route, cancel_event)
                        return

                if not route.loop:
                    break
                if route.max_cycles is not None and cycle >= route.max_cycles:
                    break

            self._finish_after_bound(route, cancel_event)
        except Exception as ex:  # pragma: no cover - defensive, thread must never die silently
            log(f"patrol thread raised an unexpected exception: {ex}", self.name)
            self._finish(PatrolState.FAILED, f"unexpected error: {ex}")

    def _duration_exceeded(self, route: PatrolRoute) -> bool:
        if route.max_duration_seconds is None:
            return False
        with self._lock:
            started_at = self._started_at
        return started_at is not None and (time.monotonic() - started_at) >= route.max_duration_seconds

    def _finish_after_bound(self, route: PatrolRoute, cancel_event: threading.Event) -> None:
        """Reached the end of the route naturally (single pass done, or
        a loop/duration bound was reached) - not a stop, not a failure.
        Attempts the Phase 1 "Kembali ke Home" step best-effort, then
        reports COMPLETED - UNLESS a stop was requested at the exact
        moment this ran, in which case stop always wins (Phase 6: a stop
        request must never be silently overridden by an in-flight
        completion)."""
        if route.return_home and not cancel_event.is_set():
            self._attempt_return_home(cancel_event)
        if cancel_event.is_set():
            self._finish(PatrolState.STOPPED, "user_requested")
        else:
            self._finish(PatrolState.COMPLETED, None)

    def _finish(self, state: str, reason: Optional[str], preset: Optional[str] = None,
                index: Optional[int] = None, cycle: Optional[int] = None) -> None:
        with self._lock:
            self._state = state
            self._reason = reason
            route = self._route
            preset = preset if preset is not None else self._current_preset
            index = index if index is not None else self._current_index
            cycle = cycle if cycle is not None else self._current_cycle
        event_type = {
            PatrolState.STOPPED: "camera_patrol_stopped",
            PatrolState.COMPLETED: "camera_patrol_completed",
            PatrolState.FAILED: "camera_patrol_failed",
        }[state]
        extra: Dict[str, Any] = {}
        if reason is not None:
            extra["reason"] = str(reason)
        self._publish(event_type, preset=preset, index=index, cycle=cycle, **extra)

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def _publish(self, event_type: str, preset: Optional[str], index: Optional[int],
                 cycle: Optional[int], **extra: Any) -> None:
        if self._event_bus is None:
            return
        with self._lock:
            route = self._route
        try:
            self._event_bus.publish(Event(type=event_type, data=_metadata_payload(route, preset, index, cycle, **extra)))
        except Exception as ex:  # pragma: no cover - defensive, must never kill the patrol thread
            log(f"failed to publish {event_type} (ignored): {ex}", self.name)

    # -- PTZ dispatch (reuses the existing tool_requested/tool_finished round trip) --

    def _goto_preset(self, preset: str, cancel_event: threading.Event) -> tuple:
        """Returns `(True, None)` on success or `(False, reason)` on
        failure/timeout. Cancellable: polls in `_POLL_INTERVAL_S` slices
        so a `stop_patrol()` call takes effect promptly even while this
        is waiting - see module docstring's "Threading model" section
        for why an in-flight call itself cannot be interrupted (the
        same, already-accepted "cooperative, not preemptive" limit
        `ToolManager`'s own docstring documents)."""
        tool_call = {"tool": "camera_ptz", "action": "goto_preset", "target": preset, "parameters": {"_patrol_origin": True}}
        return self._dispatch_tool_call(tool_call, cancel_event)

    def _attempt_return_home(self, cancel_event: threading.Event) -> None:
        """Best-effort - a failure here is logged but never turns an
        otherwise-successful patrol into a FAILED one (every actual
        patrol movement already succeeded by the time this runs)."""
        tool_call = {"tool": "camera_ptz", "action": "center", "target": None, "parameters": {"_patrol_origin": True}}
        ok, reason = self._dispatch_tool_call(tool_call, cancel_event)
        if not ok:
            log(f"return-to-home (center) failed at end of patrol (ignored): {reason}", self.name)

    def _dispatch_tool_call(self, tool_call: Dict[str, Any], cancel_event: threading.Event) -> tuple:
        if self._event_bus is None:
            return False, "not bound to an event bus"

        execution_id = generate_id("exec")
        done = threading.Event()
        box: Dict[str, Event] = {}

        def _on_finished(e: Event) -> None:
            if e.get("execution_id") == execution_id:
                box["finished"] = e
                done.set()

        def _on_failed(e: Event) -> None:
            if e.get("execution_id") == execution_id:
                box["failed"] = e
                done.set()

        # Subscribe to BOTH outcomes BEFORE publishing - same ordering
        # `PlannerBridgeModule._tool_bridge_handler` already established,
        # for the same reason (ToolManagerBridgeModule may publish
        # tool_failed almost immediately; subscribing after publishing
        # would risk missing it).
        sub_ok = self._event_bus.subscribe("tool_finished", _on_finished)
        sub_err = self._event_bus.subscribe("tool_failed", _on_failed)
        try:
            self._event_bus.publish(Event(type="tool_requested", data={"execution_id": execution_id, "tool_call": tool_call}))
            deadline = time.monotonic() + _PTZ_CALL_TIMEOUT_S
            while time.monotonic() < deadline:
                if done.wait(_POLL_INTERVAL_S):
                    break
                if cancel_event.is_set():
                    return False, "cancelled"
        finally:
            self._event_bus.unsubscribe(sub_ok)
            self._event_bus.unsubscribe(sub_err)

        if "finished" in box:
            return True, None
        if "failed" in box:
            failed = box["failed"]
            return False, failed.get("error") or failed.get("message") or "camera_ptz call failed"
        return False, "timed out waiting for camera_ptz"
