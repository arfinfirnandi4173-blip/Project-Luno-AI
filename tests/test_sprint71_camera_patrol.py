"""
tests/test_sprint71_camera_patrol.py
=======================================

Sprint 71 (Camera Patrol) - dedicated regression suite.

Builds on the SAME real bootstrap (`register_all_modules`/
`register_all_adapters`, all-mock backends by default) `tests/
test_sprint71_dashboard_startup_recovery.py`/`tests/test_dashboard.py`
already establish - no physical camera is ever needed. Every actual PTZ
movement a patrol issues goes through the REAL `tool_requested` ->
`ToolManagerBridgeModule` -> `ToolManager` -> `camera_ptz` handler round
trip (see `luno/camera_patrol/controller.py`'s own module docstring) -
these are genuine E2E tests through the real runtime path, not tests of
a private helper in isolation.

Route-model/validation tests are pure (no bootstrap) and live at the top
of this file; lifecycle/safety/ownership/persistence/security tests
follow, using a temporary `config/camera_patrol_routes.json`-equivalent
file pointed to via `CameraPatrolModule._routes_path` (never the real
one - this suite never touches the user's own route configuration).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.camera_patrol.route import (  # noqa: E402
    MAX_DWELL_SECONDS, PatrolRoute, PatrolRouteError, validate_route,
)
from luno.camera_patrol.state import PatrolState  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import (  # noqa: E402
    RealCameraPTZHandler, classify_tapo_exception,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _write_routes(path: str, routes: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(routes, fh)


def _build_stack(routes: Optional[Dict[str, Any]] = None, routes_path: Optional[str] = None):
    """Real bootstrap - same helper convention `tests/test_dashboard.py`/
    `tests/test_sprint71_dashboard_startup_recovery.py` already
    established. Points the freshly-constructed `CameraPatrolModule` at
    a TEMPORARY routes file (never the real `config/camera_patrol_
    routes.json`) so this suite can never mutate the user's own patrol
    configuration."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    patrol = modules["camera_patrol_module"]
    if routes_path is None:
        fd, routes_path = tempfile.mkstemp(suffix=".json", prefix="camera_patrol_routes_test_")
        os.close(fd)
    patrol._routes_path = routes_path
    if routes is not None:
        _write_routes(routes_path, routes)

    return runtime, modules, adapter_manager, cfg, routes_path


def _teardown(runtime, adapter_manager, routes_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if routes_path is not None:
        try:
            os.remove(routes_path)
        except OSError:
            pass


def _save_presets(modules: Dict[str, Any], names) -> None:
    """Pre-seeds the (default, mock) camera_ptz handler with saved
    preset positions, same shape `luno/tool_manager/tests/
    test_camera_ptz.py`'s own `_FakeTapoClient` uses - lets
    `goto_preset` succeed without a physical camera."""
    mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
    for name in names:
        mock._presets[name.lower()] = (0.0, 0.0)


# ============================================================================
# Route model / validation (pure, no bootstrap)
# ============================================================================

def test_01_valid_route_passes_validation():
    route = PatrolRoute(name="rumah", presets=["pintu", "meja", "jendela"], dwell_seconds=10.0, loop=False)
    validate_route(route)  # must not raise


def test_02_empty_route_rejected():
    route = PatrolRoute(name="empty", presets=[], dwell_seconds=10.0)
    with pytest.raises(PatrolRouteError, match="no presets"):
        validate_route(route)


def test_03_unknown_preset_is_a_runtime_failure_not_a_static_rejection():
    """Route-model validation is pure (no camera access) - see
    `route.py::validate_route`'s own docstring for why "unknown preset"
    is deliberately NOT checkable here. A syntactically fine route with
    a made-up preset name passes static validation ..."""
    route = PatrolRoute(name="r", presets=["definitely_not_a_real_preset"], dwell_seconds=0.1)
    validate_route(route)  # passes - see test_20 for the actual runtime failure this produces


def test_04_duplicate_preset_rejected():
    route = PatrolRoute(name="dup", presets=["pintu", "meja", "PINTU"], dwell_seconds=5.0)
    with pytest.raises(PatrolRouteError, match="more than once"):
        validate_route(route)


def test_05_invalid_dwell_rejected():
    for bad_dwell in (-1.0, float("nan"), float("inf"), MAX_DWELL_SECONDS + 1):
        route = PatrolRoute(name="r", presets=["a"], dwell_seconds=bad_dwell)
        with pytest.raises(PatrolRouteError):
            validate_route(route)


def test_06_invalid_cycle_rejected():
    for bad_cycles in (0, -1, 1.5, "3"):
        route = PatrolRoute(name="r", presets=["a"], dwell_seconds=1.0, loop=True, max_cycles=bad_cycles)
        with pytest.raises(PatrolRouteError):
            validate_route(route)


def test_07_missing_safety_bound_rejected_when_looping():
    route = PatrolRoute(name="unbounded", presets=["a", "b"], dwell_seconds=1.0, loop=True)
    with pytest.raises(PatrolRouteError, match="no bound"):
        validate_route(route)


def test_08_non_looping_route_needs_no_bound():
    route = PatrolRoute(name="single_pass", presets=["a", "b"], dwell_seconds=1.0, loop=False)
    validate_route(route)  # must not raise - a single pass is inherently bounded


def test_09_looping_route_with_max_cycles_or_max_duration_is_valid():
    validate_route(PatrolRoute(name="r1", presets=["a"], dwell_seconds=1.0, loop=True, max_cycles=3))
    validate_route(PatrolRoute(name="r2", presets=["a"], dwell_seconds=1.0, loop=True, max_duration_seconds=60.0))


# ============================================================================
# Lifecycle (real bootstrap, mock camera_ptz)
# ============================================================================

def test_10_full_lifecycle_started_moving_dwelling_completed():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"quick": {"presets": ["a", "b"], "dwell_seconds": 0.05, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    events: List[Any] = []
    for evt in ("camera_patrol_started", "camera_patrol_moving", "camera_patrol_dwell", "camera_patrol_completed"):
        runtime.event_bus.subscribe(evt, lambda e: events.append(e.type))
    try:
        runtime.start()
        result = patrol.start_patrol("quick")
        assert result["ok"] is True
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.COMPLETED
        assert "camera_patrol_started" in events
        assert "camera_patrol_moving" in events
        assert "camera_patrol_dwell" in events
        assert "camera_patrol_completed" in events
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_11_stop_mid_run_reaches_stopped():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"slow": {"presets": ["a", "b"], "dwell_seconds": 5.0, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("slow")
        assert _wait_until(lambda: patrol.get_status()["state"] == PatrolState.DWELLING, 3.0)
        t0 = time.monotonic()
        result = patrol.stop_patrol()
        elapsed = time.monotonic() - t0
        assert result["ok"] is True
        assert result["state"] == PatrolState.STOPPED
        assert elapsed < 3.0, f"stop took {elapsed:.2f}s during a 5s dwell - not deterministic"
        assert patrol.get_status()["state"] == PatrolState.STOPPED
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_12_failure_stops_patrol_and_does_not_continue():
    """The second preset ('missing') doesn't exist on the (mock) camera
    - goto_preset fails honestly, and the patrol must stop there, never
    reaching a hypothetical third preset."""
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "missing", "c"], "dwell_seconds": 0.05, "loop": False}}
    )
    _save_presets(modules, ["a", "c"])  # 'missing' deliberately not saved
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.FAILED
        assert status["preset"] == "missing"
        assert status["reason"]
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Safety
# ============================================================================

def test_13_infinite_patrol_rejected_at_start():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"unbounded": {"presets": ["a"], "dwell_seconds": 0.05, "loop": True}}
    )
    _save_presets(modules, ["a"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        result = patrol.start_patrol("unbounded")
        assert result["ok"] is False
        assert result["code"] == "refused_no_patrol_route"
        assert patrol.get_status()["state"] == PatrolState.IDLE
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_14_max_cycles_enforced():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"loop3": {"presets": ["a"], "dwell_seconds": 0.02, "loop": True, "max_cycles": 3}}
    )
    _save_presets(modules, ["a"])
    patrol = modules["camera_patrol_module"]
    moving_events: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("camera_patrol_moving", lambda e: moving_events.append(dict(e.data)))
    try:
        runtime.start()
        patrol.start_patrol("loop3")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        assert patrol.get_status()["state"] == PatrolState.COMPLETED
        cycles_seen = {e["cycle"] for e in moving_events}
        assert cycles_seen == {1, 2, 3}, f"expected exactly cycles 1-3, saw {cycles_seen}"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_15_max_duration_enforced():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"timed": {"presets": ["a"], "dwell_seconds": 0.1, "loop": True, "max_duration_seconds": 0.35}}
    )
    _save_presets(modules, ["a"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        t0 = time.monotonic()
        patrol.start_patrol("timed")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        elapsed = time.monotonic() - t0
        assert patrol.get_status()["state"] == PatrolState.COMPLETED
        assert elapsed < 3.0, f"max_duration_seconds=0.35 was not enforced (ran {elapsed:.2f}s)"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_16_stop_prevents_next_preset_from_executing():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b", "c"], "dwell_seconds": 3.0, "loop": False}}
    )
    _save_presets(modules, ["a", "b", "c"])
    mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: patrol.get_status()["state"] == PatrolState.DWELLING, 3.0)
        preset_before = patrol.get_status()["preset"]
        assert preset_before == "a"
        patrol.stop_patrol()
        time.sleep(0.5)  # give any (incorrect) continuation a chance to happen
        assert patrol.get_status()["preset"] == "a", "patrol advanced to another preset after stop() was called"
        assert patrol.get_status()["state"] == PatrolState.STOPPED
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_17_timeout_stops_patrol():
    """A camera_ptz handler that never returns within ITS OWN timeout -
    ToolManager's own timeout enforcement (already relied upon, not
    reinvented - see controller.py's module docstring) must surface as a
    patrol FAILED, not a hang."""
    import threading as _threading

    from luno.tool_manager.context import ExecutionContext
    from luno.tool_manager.handler import ToolHandler
    from luno.tool_manager.result import ToolResult

    class _HangingPTZHandler(ToolHandler):
        name = "camera_ptz"
        default_timeout_s = 0.3
        max_timeout_s = 1.0

        def supported_actions(self):
            return ["goto_preset", "center", "pan_left", "pan_right", "tilt_up", "tilt_down", "save_preset"]

        def execute(self, tool_call, context: Optional[ExecutionContext] = None) -> ToolResult:
            _threading.Event().wait(5.0)  # never returns within default_timeout_s
            return ToolResult.ok(self.name, tool_call.action, "unreachable")

    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a"], "dwell_seconds": 0.05, "loop": False}}
    )
    modules["tool_manager_module"].manager.registry.register("camera_ptz", _HangingPTZHandler())
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.FAILED
        assert "time" in (status["reason"] or "").lower()
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_18_camera_disconnect_failure_stops_patrol():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b"], "dwell_seconds": 0.05, "loop": False}}
    )
    mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
    mock._presets["a"] = (0.0, 0.0)

    original_execute = mock.execute

    def _flaky_execute(tool_call, context=None):
        if tool_call.action == "goto_preset" and tool_call.target == "b":
            raise ConnectionError("simulated camera offline")
        return original_execute(tool_call, context)

    mock.execute = _flaky_execute
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.FAILED
        assert status["preset"] == "b"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_19_auth_failure_stops_patrol_using_sprint69_70_classification():
    """Reuses `classify_tapo_exception` (Sprint 69/70) - not a new
    classifier. Registers the REAL `RealCameraPTZHandler` against a fake
    Tapo client (same fake-client convention `luno/tool_manager/tests/
    test_camera_ptz.py` already established) whose `setPreset()` raises
    an auth-failure-shaped exception."""

    class _FakeAuthFailClient:
        def getPresets(self):
            return {"1": "a"}

        def setPreset(self, preset_id):
            raise RuntimeError("Invalid login credentials")

    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a"], "dwell_seconds": 0.05, "loop": False}}
    )
    modules["tool_manager_module"].manager.registry.register("camera_ptz", RealCameraPTZHandler(_FakeAuthFailClient()))
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.FAILED
        classified = classify_tapo_exception(RuntimeError("Invalid login credentials"))
        assert classified.category == "AUTH_FAILED"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_20_unknown_preset_fails_patrol_at_runtime_honestly():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["definitely_not_a_real_preset"], "dwell_seconds": 0.05, "loop": False}}
    )
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        status = patrol.get_status()
        assert status["state"] == PatrolState.FAILED
        assert "no saved position" in (status["reason"] or "").lower() or "definitely_not_a_real_preset" in (status["reason"] or "")
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_21_no_concurrent_ptz_ownership():
    """`ToolManagerBridgeModule`'s own single-worker pool already
    serializes EVERY tool execution (not just camera_ptz) - this test
    proves that guarantee actually holds for a patrol-issued call
    racing a manual one, by recording enter/exit timestamps on the
    handler itself and asserting no two calls ever overlap."""
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b", "c", "d"], "dwell_seconds": 0.01, "loop": False}}
    )
    mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
    for name in ("a", "b", "c", "d"):
        mock._presets[name] = (0.0, 0.0)

    intervals: List[Any] = []
    lock = threading.Lock()
    original_execute = mock.execute

    def _timed_execute(tool_call, context=None):
        start = time.monotonic()
        result = original_execute(tool_call, context)
        with lock:
            intervals.append((start, time.monotonic()))
        return result

    mock.execute = _timed_execute
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        # Fire a real manual command while patrol is (probably) still running.
        runtime.event_bus.publish(Event(type="tool_requested", data={
            "execution_id": "manual_concurrency_check",
            "tool_call": {"tool": "camera_ptz", "action": "pan_left", "target": None, "parameters": {}},
        }))
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        time.sleep(0.2)
        with lock:
            snapshot = sorted(intervals)
        for i in range(1, len(snapshot)):
            assert snapshot[i][0] >= snapshot[i - 1][1], f"overlapping PTZ calls detected: {snapshot[i-1]} and {snapshot[i]}"
        assert len(snapshot) >= 2
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_22_manual_ptz_command_stops_active_patrol():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b"], "dwell_seconds": 5.0, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: patrol.is_running(), 3.0)

        done = threading.Event()

        def _on_finished(e):
            if e.get("execution_id") == "manual_override_check":
                done.set()

        runtime.event_bus.subscribe("tool_finished", _on_finished)
        runtime.event_bus.publish(Event(type="tool_requested", data={
            "execution_id": "manual_override_check",
            "tool_call": {"tool": "camera_ptz", "action": "pan_left", "target": None, "parameters": {}},
        }))
        assert done.wait(5.0), "manual camera_ptz command never completed"
        assert not patrol.is_running(), "patrol was still active after a manual PTZ command executed"
        assert patrol.get_status()["state"] == PatrolState.STOPPED
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_23_repeated_start_is_refused_not_an_automatic_restart():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b"], "dwell_seconds": 5.0, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        first = patrol.start_patrol("r")
        assert first["ok"] is True
        assert _wait_until(lambda: patrol.is_running(), 3.0)
        second = patrol.start_patrol("r")
        assert second["ok"] is False
        assert second["code"] == "refused_patrol_already_running"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_23b_stop_when_idle_reports_already_idle():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack()
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        result = patrol.stop_patrol()
        assert result["ok"] is True
        assert result["code"] == "patrol_already_idle"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_23c_no_routes_configured_refuses_clearly():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(routes={})
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        result = patrol.start_patrol()
        assert result["ok"] is False
        assert result["code"] == "refused_no_patrol_route"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Persistence
# ============================================================================

def test_24_no_persistent_config_mutation_from_running_a_patrol():
    config_dir = os.path.join(_ROOT, "config")
    json_files = sorted(f for f in os.listdir(config_dir) if f.endswith(".json"))

    def _hash_all():
        digests = {}
        for name in json_files:
            with open(os.path.join(config_dir, name), "rb") as fh:
                digests[name] = hashlib.sha256(fh.read()).hexdigest()
        return digests

    before = _hash_all()
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b"], "dwell_seconds": 0.05, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
    finally:
        _teardown(runtime, adapter_manager, routes_path)
    after = _hash_all()
    changed = {name: (before[name], after[name]) for name in json_files if before[name] != after[name]}
    assert not changed, f"config/*.json mutated by running a patrol: {changed}"


def test_25_runtime_state_is_ephemeral_not_written_to_the_routes_file():
    fd, routes_path = tempfile.mkstemp(suffix=".json", prefix="camera_patrol_routes_test_")
    os.close(fd)
    _write_routes(routes_path, {"r": {"presets": ["a"], "dwell_seconds": 0.05, "loop": False}})
    before_bytes = open(routes_path, "rb").read()

    runtime, modules, adapter_manager, cfg, _ = _build_stack(routes_path=routes_path)
    _save_presets(modules, ["a"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
        # Compare BEFORE teardown - _teardown() itself deletes routes_path.
        after_bytes = open(routes_path, "rb").read()
        assert before_bytes == after_bytes, "current_preset/index/cycle/state runtime fields leaked into the routes file"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Security
# ============================================================================

def test_26_credentials_never_appear_in_event_payloads():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a"], "dwell_seconds": 0.05, "loop": False}}
    )
    _save_presets(modules, ["a"])
    patrol = modules["camera_patrol_module"]
    all_payloads: List[Dict[str, Any]] = []
    for evt in ("camera_patrol_started", "camera_patrol_moving", "camera_patrol_dwell", "camera_patrol_completed", "camera_patrol_failed", "camera_patrol_stopped"):
        runtime.event_bus.subscribe(evt, lambda e: all_payloads.append(dict(e.data)))
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: not patrol.is_running(), 5.0)
    finally:
        _teardown(runtime, adapter_manager, routes_path)

    forbidden_keys = {"password", "username", "credential", "token", "session", "frame", "image"}
    for payload in all_payloads:
        keys = {k.lower() for k in payload.keys()}
        leaked = keys & forbidden_keys
        assert not leaked, f"event payload contains a forbidden key: {leaked} in {payload}"
        allowed_keys = {"route", "preset", "index", "cycle", "reason"}
        assert keys <= allowed_keys, f"event payload has unexpected keys (not metadata-only): {keys - allowed_keys}"


def test_27_patrol_route_public_dict_never_contains_credential_fields():
    route = PatrolRoute(name="r", presets=["a", "b"], dwell_seconds=1.0)
    public = route.to_public_dict()
    forbidden = {"password", "username", "credential", "token", "session", "rtsp", "url"}
    assert not (set(k.lower() for k in public.keys()) & forbidden)


def test_28_dashboard_status_has_no_credential_fields():
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack()
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        status = patrol.get_status()
        forbidden = {"password", "username", "credential", "token", "session"}
        assert not (set(k.lower() for k in status.keys()) & forbidden)
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Dashboard integration (additive only)
# ============================================================================

def test_29_dashboard_vision_endpoint_includes_patrol_fields():
    from luno.dashboard import collectors

    runtime, modules, adapter_manager, cfg, routes_path = _build_stack()
    try:
        runtime.start()
        result = collectors.collect_vision(adapter_manager, modules)
        assert "patrol_state" in result
        assert result["patrol_state"] == PatrolState.IDLE
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_30_dashboard_vision_endpoint_unaffected_without_modules_arg():
    """Backward compatibility - any caller that doesn't pass `modules`
    (i.e. every caller that existed before this sprint) gets exactly the
    same response shape as before, no patrol_* keys at all."""
    from luno.dashboard import collectors

    runtime, modules, adapter_manager, cfg, routes_path = _build_stack()
    try:
        runtime.start()
        result = collectors.collect_vision(adapter_manager)
        assert "patrol_state" not in result
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Voice/text command parsing (Planner integration)
# ============================================================================

def test_31_parser_classifies_patrol_commands():
    from luno.planner.parser import IntentParser

    cases = {
        "mulai patroli kamera": ("camera_patrol", "start", None),
        "mulai patroli rumah": ("camera_patrol", "start", "rumah"),
        "stop patroli kamera": ("camera_patrol", "stop", None),
        "status patroli kamera": ("camera_patrol", "status", None),
    }
    for text, expected in cases.items():
        steps = IntentParser.parse(text)
        assert len(steps) == 1, f"{text!r} did not parse to exactly one step: {steps}"
        got = (steps[0].tool, steps[0].action, steps[0].target)
        assert got == expected, f"{text!r} -> {got}, expected {expected}"


def test_32_existing_camera_ptz_commands_unaffected_by_patrol_parsing():
    from luno.planner.parser import IntentParser

    steps = IntentParser.parse("geser kamera ke kiri")
    assert len(steps) == 1
    assert (steps[0].tool, steps[0].action) == ("camera_ptz", "pan_left")


def test_33_start_command_end_to_end_through_the_planner_tool_call_shape():
    """The parser's own ParsedStep, once turned into a ToolCall by the
    Planner (see luno/planner/planner.py::_steps_to_tasks), must route
    correctly through the real camera_patrol handler."""
    from luno.planner.parser import IntentParser
    from luno.tool_manager.models import ToolCall as TMToolCall

    steps = IntentParser.parse("mulai patroli rumah")
    step = steps[0]
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"rumah": {"presets": ["a"], "dwell_seconds": 5.0, "loop": False}}
    )
    _save_presets(modules, ["a"])
    try:
        runtime.start()
        tool_manager = modules["tool_manager_module"].manager
        result = tool_manager.execute(TMToolCall(tool=step.tool, action=step.action, target=step.target))
        assert result.success is True
        patrol = modules["camera_patrol_module"]
        assert _wait_until(lambda: patrol.is_running(), 2.0)
    finally:
        _teardown(runtime, adapter_manager, routes_path)


# ============================================================================
# Performance (Phase 12 - in-memory overhead only)
# ============================================================================

def test_34_controller_in_memory_overhead_is_well_under_5ms():
    """`get_status()`/`stop_patrol()`-when-idle are pure in-memory
    operations (no network/movement timing involved) - measured
    separately from any actual PTZ call, per Phase 12's own "movement/
    dwell network timing tentu tidak dihitung" carve-out."""
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack()
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        iterations = 200
        t0 = time.perf_counter()
        for _ in range(iterations):
            patrol.get_status()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0 / iterations
        assert elapsed_ms < 5.0, f"get_status() averaged {elapsed_ms:.3f}ms/call, over the 5ms target"

        t0 = time.perf_counter()
        for _ in range(iterations):
            patrol.stop_patrol()  # idle every time - patrol_already_idle path
        elapsed_ms = (time.perf_counter() - t0) * 1000.0 / iterations
        assert elapsed_ms < 5.0, f"stop_patrol() (idle) averaged {elapsed_ms:.3f}ms/call, over the 5ms target"
    finally:
        _teardown(runtime, adapter_manager, routes_path)


def test_35_no_orphaned_thread_after_module_stop_mid_patrol():
    """Phase 12: "membuat thread/process tanpa lifecycle cleanup" -
    `CameraPatrolModule.stop()` (the Module-level lifecycle hook, called
    at process shutdown) must not leave a patrol thread running."""
    runtime, modules, adapter_manager, cfg, routes_path = _build_stack(
        routes={"r": {"presets": ["a", "b"], "dwell_seconds": 5.0, "loop": False}}
    )
    _save_presets(modules, ["a", "b"])
    patrol = modules["camera_patrol_module"]
    try:
        runtime.start()
        patrol.start_patrol("r")
        assert _wait_until(lambda: patrol.is_running(), 3.0)
        thread_before = patrol._thread
        assert thread_before is not None and thread_before.is_alive()
        patrol.stop()  # Module-level lifecycle stop, not stop_patrol() directly
        assert _wait_until(lambda: not thread_before.is_alive(), 5.0), "patrol thread survived CameraPatrolModule.stop()"
    finally:
        _teardown(runtime, adapter_manager, routes_path)
