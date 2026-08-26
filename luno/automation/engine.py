"""
engine.py
=========

`AutomationEngine` - Sprint 72 (Automation Engine Dasar). Implements the
deterministic pipeline the sprint brief specifies end to end:

    TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN

--------------------------------------------------------------------
Architecture - reuses the existing Event Bus / ToolManager, never a
second one (Phase 0's own instruction)
--------------------------------------------------------------------
This module never constructs its own Event Bus, Scheduler, or
`ToolManager`. It is a `Module` (same interface `CameraPatrolModule`/
`ToolManagerBridgeModule` already implement - see
`luno/core/module_manager.py::Module`), bound to the SAME `event_bus`
every other module already uses (`bind_event_bus()`) and, optionally, to
the SAME `runtime.scheduler` (`luno.core.scheduler.Scheduler`, already
built, already running one background tick thread - `bind_scheduler()`)
for TIME triggers - never a second scheduler/thread/timer (Phase 8/
Hard-Stop #12).

Trigger delivery reuses the exact "observability tap" subscription
idiom already established by `luno/dashboard/event_log_writer.py`/
`events_buffer.py`/`voice_latency.py` (`event_bus.subscribe("*",
self._on_bus_event, ...)`) - not a new mechanism, not polling (Phase 10:
"Jangan polling event bus").

Every actual device action this engine issues (camera or Home
Assistant) goes out through the EXACT SAME `tool_requested` ->
`ToolManagerBridgeModule` -> `ToolManager` round trip a manual voice
command, and Sprint 71's own `CameraPatrolModule`, already use (see
`_dispatch_tool_call()` below - a FOURTH caller of this established
pattern, not a new one). This is what lets the engine reuse Sprint 69-
71's error classification and single-worker FIFO serialization for
free, with zero duplicated action-execution logic.

--------------------------------------------------------------------
Ownership (Phase 5) - Manual PTZ > Automation > Patrol
--------------------------------------------------------------------
Every outgoing camera action this engine issues is tagged
`parameters={"_automation_origin": True, ...}`. Sprint 71's own
`CameraPatrolModule.on_manual_ptz_dispatch()` pre-dispatch hook already
stops an active patrol for ANY `camera_ptz` call that is not tagged
`_patrol_origin` - an automation-issued call satisfies that condition
unmodified, so "Automation > Patrol" falls out of the EXISTING Sprint 71
mechanism with zero changes to `camera_patrol/controller.py`.

"Manual > Automation" is the one genuinely new piece: this engine
registers its OWN pre-dispatch hook (`on_camera_dispatch()`) on the same
`ToolManagerBridgeModule` (multi-hook list, additive, no-op for every
other tool - see that class's own docstring). Watching every
`camera_ptz`/`camera_patrol` call that is untagged by BOTH
`_automation_origin` and `_patrol_origin` (i.e. genuinely manual) sets a
short "manual priority window" (`_MANUAL_PRIORITY_WINDOW_S`); any
automation camera action attempted while that window is open is refused
(`action_refused_busy`) rather than dispatched, so a human's manual
command is never contended for the camera by an automation rule. This is
a pragmatic, bounded, HONEST implementation - not a full "manual PTZ
session" tracker (this project has no such concept anywhere) - see the
Known Limitations section of `docs/change_impact/automation_engine.md`.

Concurrent PTZ ownership is additionally impossible at a lower layer
regardless of this engine's own logic: every `camera_ptz`/`camera_patrol`
call, from any of the three callers, is serialized through
`ToolManagerBridgeModule`'s single-worker FIFO executor (Sprint 71's own
architecture note) - this engine's hook is about PRIORITY/ordering, not
about preventing literal concurrent execution, which was already
structurally impossible before this sprint.

--------------------------------------------------------------------
Cooldown & loop protection (Phase 8/9)
--------------------------------------------------------------------
Cooldown needs no new thread or timer at all: `_cooldown_until` is a
small, bounded (at most one entry per loaded rule) in-memory dict,
checked with a plain `time.monotonic()` comparison at the moment a NEW
trigger arrives - "has enough time passed since this rule's last
execution?" A periodic cleanup job (`_cleanup_cooldowns`) is registered
on the REUSED `runtime.scheduler` (if bound), not a new thread, purely
for hygiene (Phase 8: "Cooldown state harus bounded dan cleanup-able").

Loop protection (Phase 9) is a bounded, frequency-based detector, not a
full causal-graph tracer (this sprint's action allowlist has no
"automation triggers another automation" primitive, so a precise causal
chain cannot cross module boundaries without every other module
carrying correlation metadata it was never asked to carry - see the
change-impact doc's own Known Limitations for the honest scope
statement). `_recent_firings` is a small bounded deque of
`(rule_id, monotonic_time)`; a rule whose OWN id has fired
`_MAX_FIRINGS_IN_WINDOW` or more times within `_CYCLE_WINDOW_S` is
refused with `automation_cycle_detected` - this catches both a literal
self-loop (A -> A) and an indirect one (A -> B -> A) the moment it
starts repeating rapidly, which is the only externally observable
symptom of either shape. A rule additionally can never have two
executions running at once (`_running_rule_ids` reentrancy guard).
`correlation_id`/`depth` are real, tested fields on every
`AutomationExecution` (root executions get `depth=0` and
`correlation_id == execution_id`) - infrastructure Sprint 72 wires up
for a FUTURE sprint that might add automation-to-automation chaining,
not something this sprint's own action allowlist can actually trigger
today (there is no "run another automation" action type in
`ACTION_TYPES`).

--------------------------------------------------------------------
Persistence (Phase 11)
--------------------------------------------------------------------
Rule DEFINITIONS are loaded from `config/automation_rules.json` (same
named-entity JSON convention as `config/camera_patrol_routes.json`),
read once at `start()`/`reload_rules()`. RUNTIME state (running/last
execution/cooldown/execution counts) lives ONLY in this object's own
Python attributes - never written to that file, and never written
per-event (Phase 11: "Jangan membuat write setiap kali event masuk").
The ONLY writes this module ever performs are `enable_automation()`/
`disable_automation()` - rare, explicit, user-initiated - and they go
through `luno.persistence.atomic_write_json()` (the SAME generic,
already-hardened, already-backup-and-mutation-audit-integrated
persistence primitive every other JSON-backed store in this project
uses), never a bespoke write.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import generate_id, log, utcnow
from .camera_action_safety import validate_camera_ha_action
from .conditions import CONDITION_INVALID, StateReaders, evaluate_condition
from .models import (
    ACTION_TYPES,
    DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS,
    MAX_WAIT_UNTIL_TIMEOUT_SECONDS,
    MIN_WAIT_UNTIL_TIMEOUT_SECONDS,
    _CAMERA_ACTION_TYPES,
    _INTERNAL_ACTION_TYPES,
    _SEQUENCE_CONDITION_STEP_TYPE,
    _SEQUENCE_DELAY_STEP_TYPE,
    _SEQUENCE_STOP_STEP_TYPE,
    _SEQUENCE_WAIT_UNTIL_STEP_TYPE,
    _extract_call_service_entity_ids,
    _sequence_step_from_raw,
    ActionResult,
    AutomationAction,
    AutomationCondition,
    AutomationExecution,
    AutomationRule,
    AutomationRuleError,
    ExecutionStatus,
    rule_from_dict,
    validate_rule,
)

DEFAULT_RULES_PATH = os.path.join("config", "automation_rules.json")

#: Ceiling for a single dispatched action's wait for `tool_finished`/
#: `tool_failed` - a little above every real handler's own worst-case
#: timeout in this project (camera_ptz's own 20s max_timeout_s) so a
#: genuine handler timeout is always what ends the wait, same reasoning
#: `camera_patrol/controller.py::_PTZ_CALL_TIMEOUT_S` already documents.
_ACTION_DISPATCH_TIMEOUT_S = 25.0
#: Same polling granularity as `camera_patrol/controller.py` - cheap
#: enough to never register as "busy looping" (Phase 16).
_POLL_INTERVAL_S = 0.1

#: Phase 5 - how long a genuinely manual camera dispatch keeps automation
#: camera actions refused for. Short enough that a legitimate automation
#: isn't starved for long, long enough to cover "the user is actively
#: driving the camera right now."
_MANUAL_PRIORITY_WINDOW_S = 2.0

#: Phase 9 - frequency-based cycle/loop detector (see module docstring).
_MAX_FIRINGS_IN_WINDOW = 3
_CYCLE_WINDOW_S = 5.0
_RECENT_FIRINGS_MAXLEN = 200

#: Phase 9 - defensive ceiling; unreachable via this sprint's own action
#: allowlist (no automation-to-automation chaining action exists yet),
#: exercised directly by tests via the `_depth` seam on `_trigger()`.
MAX_EXECUTION_DEPTH = 3

#: Bounded, in-memory-only execution history (Phase 11 - never
#: persisted). Per-rule and global caps keep memory bounded regardless
#: of how many times a rule has fired over a long-running process.
_HISTORY_PER_RULE = 20

#: How often the (optional, reused-scheduler-backed) cooldown cleanup
#: job runs - hygiene only, not load-bearing (see module docstring).
_COOLDOWN_CLEANUP_INTERVAL_S = 300.0

#: P0.8.0 (Camera Automation -> Home Assistant Action Safety Pipeline) -
#: the event type string `VisionCameraEventBridge`/`CameraAutomationModule`
#: publish (`luno.camera_automation.module.CAMERA_EVENT_TYPE`), kept here
#: as this package's OWN small string constant rather than an import.
#: `luno/automation` is a generic engine with no existing import-time
#: dependency on `luno/camera_automation` (the two packages are only ever
#: connected via the Event Bus, by design - see `vision_bridge.py`'s own
#: "never imports Vision/YOLO/RTSP code" precedent for the same
#: decoupling principle applied in the other direction here). Used ONLY
#: to decide whether a rule's HA action should pass through the camera
#: action safety gate below - never anything else.
_CAMERA_AUTOMATION_EVENT_TYPE = "camera_automation.camera_event"


def _coerce_delay_seconds(raw: Any) -> float:
    """P0.8.9 - defense in depth at DISPATCH time, mirroring the existing
    `ACTION_TYPES` re-check precedent in `_dispatch_action()`: rule LOAD
    time (`models.py::validate_action()`) already guarantees a well-formed
    value, but this never trusts that blindly. Anything missing or
    malformed degrades to `0.0` (immediate dispatch, the pre-P0.8.9
    default), never raises."""
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0.0 else 0.0


def _coerce_sequence_delay_seconds(raw: Any) -> float:
    """P0.11 - the sequence-step analog of `_coerce_delay_seconds()`
    above, same defense-in-depth philosophy: `models.py::validate_
    sequence_step()` already guarantees a finite, non-negative, in-range
    value at rule-LOAD time, but dispatch time never trusts that
    blindly. Anything missing/malformed/negative degrades to `0.0` (a
    genuine no-op delay), never raises and never blocks indefinitely."""
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value if value > 0.0 else 0.0


def _coerce_wait_until_timeout(raw: Any) -> float:
    """P0.14 - `wait_until`'s own dispatch-time defense in depth, same
    philosophy as `_coerce_delay_seconds()`/`_coerce_sequence_delay_
    seconds()` above: `models.py::_validate_wait_until_step()` already
    guarantees a finite, in-range value at rule-LOAD time when
    `timeout_seconds` is present at all, but dispatch time never trusts
    that blindly. Missing/malformed/out-of-range degrades to the
    documented default (`DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS`, 10s) -
    never `0.0` (a `wait_until` step, unlike a `delay`, must never
    resolve to "wait for no time at all" by accident) and never raises."""
    if raw is None:
        return DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS
    if not math.isfinite(value):
        return DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS
    if not (MIN_WAIT_UNTIL_TIMEOUT_SECONDS <= value <= MAX_WAIT_UNTIL_TIMEOUT_SECONDS):
        return DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS
    return value


def _metadata_payload(execution: AutomationExecution, **extra: Any) -> Dict[str, Any]:
    """Phase 10's own "payload harus metadata-only" rule - execution_id/
    rule_id/status plus whatever small extra fields a specific event
    needs, and NOTHING else (never a credential, frame, or raw exception
    object - errors are always pre-stringified by the caller)."""
    payload: Dict[str, Any] = {
        "execution_id": execution.execution_id,
        "rule_id": execution.rule_id,
        "correlation_id": execution.correlation_id,
    }
    payload.update(extra)
    return payload


class AutomationEngine(Module):
    name = "automation_engine"
    dependencies: List[str] = []

    def __init__(self, rules_path: str = DEFAULT_RULES_PATH, state_readers: Optional[StateReaders] = None) -> None:
        self._rules_path = rules_path
        self._state_readers: StateReaders = dict(state_readers or {})
        self._event_bus: Any = None
        self._scheduler: Any = None
        self._lock = threading.RLock()

        self._rules: Dict[str, AutomationRule] = {}
        self._rules_by_event: Dict[str, List[str]] = {}
        self._cooldown_until: Dict[str, float] = {}
        #: P0.8.9 (WLED OFF debounce) - at most one pending delayed Home
        #: Assistant action per TARGET ENTITY ID (never per-rule - this is
        #: what lets an unrelated rule targeting the SAME entity, e.g. the
        #: existing ON rule, transparently supersede a pending OFF without
        #: either rule knowing about the other by id). Value is
        #: `(job_id, execution, action_type)` - `job_id` is the REUSED
        #: `runtime.scheduler`'s own handle (`Scheduler.schedule_once()`'s
        #: return value, cancellable via `Scheduler.cancel()`), `execution`
        #: is the still-PENDING `AutomationExecution` this delayed action
        #: belongs to (so a supersede can honestly finalize it as
        #: SKIPPED/`action_superseded` rather than leaving it silently
        #: PENDING forever - see `_cancel_pending_delayed_action()`).
        self._pending_delayed_actions: Dict[str, Tuple[str, AutomationExecution, str]] = {}
        self._running_rule_ids: set = set()
        self._recent_firings: Deque[Tuple[str, float]] = deque(maxlen=_RECENT_FIRINGS_MAXLEN)
        self._history: Dict[str, List[AutomationExecution]] = {}
        self._last_execution: Dict[str, AutomationExecution] = {}

        self._bus_sub_id: Optional[str] = None
        self._time_job_ids: List[str] = []
        self._cooldown_cleanup_job_id: Optional[str] = None

        #: Phase 5 - set by `on_camera_dispatch()` whenever a genuinely
        #: manual camera call is observed; read by `_dispatch_action()`
        #: before any automation-issued camera action.
        self._manual_priority_until: float = 0.0

        #: P0.8.0 - OPTIONAL, plain public attribute (wired post-
        #: construction, same convention as `VisionCameraEventBridge.
        #: vision_status_reader` from P0.7 - see `luno/bootstrap/
        #: adapters.py::register_camera_action_ha_state_reader()`). A
        #: zero-argument-per-call callable `(entity_id) -> Optional[str]`
        #: reading the SAME real `RealHomeAssistantClient.get_entity_
        #: state()` the existing `RealHomeAssistantHandler._safe_get_
        #: state()` already calls - used ONLY by `camera_action_safety.
        #: validate_camera_ha_action()`'s "already in the desired state"
        #: shortcut (Section 5). Left `None` by default - that ONE
        #: sub-check is simply skipped (never blocks, never raises) when
        #: unwired (mock backend, tests, or bootstrap hasn't reached the
        #: wiring call yet).
        self.ha_state_reader: Optional[Callable[[str], Optional[str]]] = None

    # -- Module lifecycle ---------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def bind_scheduler(self, scheduler: Any) -> None:
        """Optional. Without a bound scheduler, TIME triggers simply
        never fire (EVENT/MANUAL triggers are unaffected) - documented
        as a known limitation for any caller (e.g. a unit test) that
        constructs this engine standalone."""
        self._scheduler = scheduler

    def start(self) -> None:
        self.reload_rules()
        if self._event_bus is not None:
            self._bus_sub_id = self._event_bus.subscribe("*", self._on_bus_event)
        self._register_time_triggers()
        if self._scheduler is not None:
            self._cooldown_cleanup_job_id = self._scheduler.schedule_periodic(
                "automation_cooldown_cleanup", self._cleanup_cooldowns, interval_s=_COOLDOWN_CLEANUP_INTERVAL_S,
            )

    def stop(self) -> None:
        """Never leaves a subscription or scheduled job orphaned (Phase
        16: no lifecycle-cleanup-free thread/job). Exceptions here are
        swallowed - a broken `stop()` must never block shutdown of the
        rest of the system (same contract every other `Module.stop()` in
        this project follows)."""
        try:
            if self._event_bus is not None and self._bus_sub_id is not None:
                self._event_bus.unsubscribe(self._bus_sub_id)
        except Exception as ex:  # pragma: no cover - defensive
            log(f"stop() failed to unsubscribe from event bus (ignored): {ex}", self.name)
        self._bus_sub_id = None

        if self._scheduler is not None:
            for job_id in self._time_job_ids:
                try:
                    self._scheduler.cancel(job_id)
                except Exception as ex:  # pragma: no cover - defensive
                    log(f"stop() failed to cancel time job {job_id} (ignored): {ex}", self.name)
            if self._cooldown_cleanup_job_id is not None:
                try:
                    self._scheduler.cancel(self._cooldown_cleanup_job_id)
                except Exception as ex:  # pragma: no cover - defensive
                    log(f"stop() failed to cancel cooldown cleanup job (ignored): {ex}", self.name)
        self._time_job_ids = []
        self._cooldown_cleanup_job_id = None

        # P0.8.9 - same "never leave a scheduled job orphaned" discipline
        # applied to pending delayed actions. Their executions are left
        # PENDING (not force-finalized) - `stop()` is a shutdown, not a
        # "this action was superseded" event, so claiming `action_
        # superseded` here would be dishonest.
        with self._lock:
            pending = dict(self._pending_delayed_actions)
            self._pending_delayed_actions = {}
        if self._scheduler is not None:
            for target, (job_id, _execution, _action_type) in pending.items():
                try:
                    self._scheduler.cancel(job_id)
                except Exception as ex:  # pragma: no cover - defensive
                    log(f"stop() failed to cancel pending delayed action for target={target!r} (ignored): {ex}", self.name)

    def health(self) -> ModuleHealthStatus:
        with self._lock:
            n_rules = len(self._rules)
            n_running = len(self._running_rule_ids)
        return ModuleHealthStatus(healthy=True, message=f"{n_rules} rule(s) loaded, {n_running} running")

    # -- rule loading ---------------------------------------------------------

    def reload_rules(self) -> None:
        """Reads `config/automation_rules.json` FRESH (same "reloadable
        without a restart" precedent `CameraPatrolModule._load_routes()`
        already established). A malformed individual rule is skipped and
        logged, never crashes the whole load (same defensive convention).
        Re-registers TIME triggers against the newly loaded rule set."""
        rules = self._load_rules_from_disk()
        with self._lock:
            self._rules = rules
            self._rules_by_event = {}
            for rule in rules.values():
                if rule.trigger is not None and rule.trigger.type == "event":
                    event_name = rule.trigger.parameters.get("event_name", "")
                    self._rules_by_event.setdefault(event_name, []).append(rule.id)
        self._register_time_triggers()

    def _load_rules_from_disk(self) -> Dict[str, AutomationRule]:
        try:
            with open(self._rules_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            log(f"failed to read {self._rules_path} (treating as no rules configured): {ex}", self.name)
            return {}
        if not isinstance(raw, dict):
            return {}
        rules: Dict[str, AutomationRule] = {}
        for rule_id, data in raw.items():
            if not isinstance(data, dict):
                continue
            try:
                rule = rule_from_dict(rule_id, data)
                validate_rule(rule)
            except AutomationRuleError as ex:
                log(f"skipping invalid automation rule '{rule_id}': {ex}", self.name)
                continue
            except Exception as ex:  # pragma: no cover - defensive
                log(f"skipping malformed automation rule '{rule_id}': {ex}", self.name)
                continue
            rules[rule_id] = rule
        return rules

    def _register_time_triggers(self) -> None:
        if self._scheduler is None:
            return
        for job_id in self._time_job_ids:
            try:
                self._scheduler.cancel(job_id)
            except Exception:  # pragma: no cover - defensive
                pass
        self._time_job_ids = []
        with self._lock:
            rules = list(self._rules.values())
        for rule in rules:
            if rule.trigger is None or rule.trigger.type != "time":
                continue
            hhmm = str(rule.trigger.parameters.get("time", ""))
            try:
                hour, minute = (int(p) for p in hhmm.split(":"))
            except ValueError:  # pragma: no cover - already validated at load time
                continue
            job_id = self._scheduler.schedule_predicate(
                f"automation_time:{rule.id}",
                (lambda rid=rule.id: self._on_time_trigger(rid)),
                (lambda now, h=hour, m=minute: now.hour == h and now.minute == m),
            )
            self._time_job_ids.append(job_id)

    # -- public per-rule API ---------------------------------------------------

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            rules = list(self._rules.values())
        return [r.to_public_dict() for r in rules]

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """P0.12 - the single-resource GET half of `list_rules()`
        (O(1) dict lookup, not a linear filter of `list_rules()`'s own
        output). Returns `None` for an unknown id - the caller (the new
        `automation_api.py` dashboard layer) turns that into a proper
        HTTP 404, matching this project's own existing "unmatched route
        -> 404" precedent (`server.py::_dispatch_get`'s own catch-all)."""
        with self._lock:
            rule = self._rules.get(rule_id)
        return rule.to_public_dict() if rule is not None else None

    # -- P0.12 (Automation API & CRUD) ------------------------------------------
    #
    # The three mutating rule-definition operations the new `/api/
    # automations` HTTP surface needs and `enable_automation()`/
    # `disable_automation()` above did not already provide. Each one
    # reuses the EXACT SAME model-layer parsing/validation
    # (`rule_from_dict()`/`validate_rule()` - the same functions
    # `_load_rules_from_disk()` already uses for every rule on every
    # `start()`/`reload_rules()`, never a second, parallel parser) and
    # the EXACT SAME persistence primitive `_persist_rules()`/
    # `luno.persistence.atomic_write_json()` `_set_enabled()` already
    # uses - never a second persistence mechanism.
    #
    # Unlike `_set_enabled()` (which mutates `self._rules` in place,
    # snapshots a copy, releases `self._lock`, and only THEN calls
    # `_persist_rules()`), these three methods hold `self._lock` across
    # the ENTIRE check -> validate -> persist -> reload sequence. This
    # is a deliberate, narrower correctness choice for CRUD specifically
    # (Phase 10's own "a GET must not corrupt state while a CREATE/
    # UPDATE/DELETE is occurring" + "concurrent CRUD must not corrupt
    # persistence" requirements): two concurrent CRUD calls for
    # DIFFERENT rule ids, if allowed to race between "mutate self._rules"
    # and "write to disk" (as `_set_enabled()`'s own release-before-
    # persist pattern permits), could persist out of mutation order and
    # silently lose whichever call's disk write lands first - see
    # `docs/change_impact/automation_api_p0_12.md`'s own Concurrency
    # section for the full race analysis. `self._lock` is an `RLock`,
    # and `reload_rules()`'s own body also acquires it - safely
    # reentrant from the same thread, not a deadlock. `_set_enabled()`
    # itself is completely UNTOUCHED by this sprint (still release-
    # before-persist) - preserving its exact existing, already-tested
    # behavior was a hard requirement, not merely a preference.

    def create_rule(self, data: Dict[str, Any], rule_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns `{"ok": bool, "code": str, "message": str, "rule":
        dict|None}`. `rule_id` is generated via the project's EXISTING
        `generate_id()` utility (already used for `execution_id`
        elsewhere in this file - never a new, second id scheme) when the
        caller doesn't supply one. `created_at`/`updated_at` are always
        set HERE, server-side, to the current time - never trusted from
        the caller's `data` dict (see `AutomationRule.description`'s own
        docstring in `models.py` for why)."""
        if not rule_id:
            rule_id = generate_id("automation")
        with self._lock:
            if rule_id in self._rules:
                return {"ok": False, "code": "duplicate_id",
                        "message": f"An automation called '{rule_id}' already exists.", "rule": None}
            try:
                parsed = rule_from_dict(rule_id, data)
            except AutomationRuleError as ex:
                return {"ok": False, "code": "invalid_rule", "message": str(ex), "rule": None}
            now = utcnow().isoformat()
            rule = AutomationRule(
                id=rule_id, name=parsed.name, enabled=parsed.enabled, trigger=parsed.trigger,
                conditions=parsed.conditions, actions=parsed.actions, cooldown_seconds=parsed.cooldown_seconds,
                execution_policy=parsed.execution_policy, sequence=parsed.sequence,
                description=parsed.description, created_at=now, updated_at=now,
            )
            try:
                validate_rule(rule)
            except AutomationRuleError as ex:
                return {"ok": False, "code": "invalid_rule", "message": str(ex), "rule": None}
            all_rules = dict(self._rules)
            all_rules[rule_id] = rule
            self._persist_rules(all_rules)
            self.reload_rules()
        return {"ok": True, "code": "automation_created", "message": f"Automation '{rule_id}' created.",
                "rule": rule.to_public_dict()}

    def update_rule(self, rule_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Returns the same `{"ok", "code", "message", "rule"}` shape as
        `create_rule()`. `id` and `created_at` are immutable - always
        carried through from the EXISTING stored rule regardless of
        whatever `data` contains for those keys; only `updated_at` is
        refreshed, to the current time, on every successful update."""
        with self._lock:
            existing = self._rules.get(rule_id)
            if existing is None:
                return {"ok": False, "code": "unknown_automation",
                        "message": f"No automation called '{rule_id}'.", "rule": None}
            try:
                parsed = rule_from_dict(rule_id, data)
            except AutomationRuleError as ex:
                return {"ok": False, "code": "invalid_rule", "message": str(ex), "rule": None}
            updated = AutomationRule(
                id=rule_id, name=parsed.name, enabled=parsed.enabled, trigger=parsed.trigger,
                conditions=parsed.conditions, actions=parsed.actions, cooldown_seconds=parsed.cooldown_seconds,
                execution_policy=parsed.execution_policy, sequence=parsed.sequence,
                description=parsed.description, created_at=existing.created_at, updated_at=utcnow().isoformat(),
            )
            try:
                validate_rule(updated)
            except AutomationRuleError as ex:
                return {"ok": False, "code": "invalid_rule", "message": str(ex), "rule": None}
            all_rules = dict(self._rules)
            all_rules[rule_id] = updated
            self._persist_rules(all_rules)
            self.reload_rules()
        return {"ok": True, "code": "automation_updated", "message": f"Automation '{rule_id}' updated.",
                "rule": updated.to_public_dict()}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        """Returns `{"ok": bool, "code": str, "message": str}`. Never
        silently no-ops for an unknown id (Phase 5's own explicit "do
        not silently ignore nonexistent IDs" requirement) - refuses with
        `unknown_automation`, the SAME code `_set_enabled()`/
        `run_automation()` already use for the identical condition, not
        a new/different code for the same concept. Also clears this
        rule's bounded, in-memory-only runtime bookkeeping (cooldown/
        last execution/history) - hygiene, not required for correctness
        (an already-running execution, if any, keeps the `AutomationRule`
        object it was given by reference and finishes normally; it is
        simply no longer reachable via `self._rules` for any FUTURE
        trigger, the same property `reload_rules()` already has for any
        rule removed from the JSON file directly today)."""
        with self._lock:
            if rule_id not in self._rules:
                return {"ok": False, "code": "unknown_automation", "message": f"No automation called '{rule_id}'."}
            all_rules = dict(self._rules)
            del all_rules[rule_id]
            self._persist_rules(all_rules)
            self.reload_rules()
            self._cooldown_until.pop(rule_id, None)
            self._last_execution.pop(rule_id, None)
            self._history.pop(rule_id, None)
        return {"ok": True, "code": "automation_deleted", "message": f"Automation '{rule_id}' deleted."}

    def get_status(self) -> List[Dict[str, Any]]:
        """Dashboard/voice-status-facing snapshot - read-only, in-memory
        only, metadata-only (Phase 13)."""
        with self._lock:
            rules = list(self._rules.values())
            running = set(self._running_rule_ids)
            cooldowns = dict(self._cooldown_until)
            last = dict(self._last_execution)
        now = time.monotonic()
        out = []
        for rule in rules:
            last_exec = last.get(rule.id)
            cooldown_until = cooldowns.get(rule.id)
            out.append({
                "id": rule.id,
                "name": rule.name,
                "enabled": rule.enabled,
                "trigger": rule.trigger.to_public_dict() if rule.trigger else None,
                "running": rule.id in running,
                "cooldown_remaining_s": max(0.0, cooldown_until - now) if cooldown_until else 0.0,
                "last_execution": last_exec.to_public_dict() if last_exec is not None else None,
            })
        return out

    def get_automation_status(self, rule_id: str) -> Optional[Dict[str, Any]]:
        for status in self.get_status():
            if status["id"] == rule_id:
                return status
        return None

    def run_automation(self, rule_id: str) -> Dict[str, Any]:
        """The MANUAL trigger (Phase 2/4): `run_automation("night_mode")`.
        Never blocks for the duration of the automation - starts the
        pipeline and returns almost immediately, same "return a small,
        honest result dict rather than raise for an expected refusal"
        convention `CameraPatrolModule.start_patrol()` already
        established.

        P0.12 - the returned dict gained one additive key, `execution_id`
        (via `_trigger()`'s new `_execution_out` seam - see that
        method's own docstring), so the new `POST /api/automations/{id}/
        run` endpoint can hand the caller a real, traceable execution id
        instead of just a bare accept/refuse boolean. Every pre-existing
        key (`ok`/`code`/`message`) is completely unchanged - this is a
        pure dict-key addition, not a reshape."""
        with self._lock:
            rule = self._rules.get(rule_id)
        if rule is None:
            return {"ok": False, "code": "unknown_automation", "message": f"No automation called '{rule_id}'.", "execution_id": None}
        if not rule.enabled:
            return {"ok": False, "code": "automation_disabled", "message": f"Automation '{rule_id}' is disabled.", "execution_id": None}
        execution_holder: List[AutomationExecution] = []
        accepted, code = self._trigger(rule, {"type": "manual", "parameters": {}}, _execution_out=execution_holder)
        execution_id = execution_holder[0].execution_id if execution_holder else None
        if not accepted:
            return {"ok": False, "code": code, "message": f"Automation '{rule_id}' was not started ({code}).", "execution_id": execution_id}
        return {"ok": True, "code": "automation_started", "message": f"Automation '{rule_id}' started.", "execution_id": execution_id}

    def enable_automation(self, rule_id: str) -> Dict[str, Any]:
        return self._set_enabled(rule_id, True)

    def disable_automation(self, rule_id: str) -> Dict[str, Any]:
        return self._set_enabled(rule_id, False)

    def _set_enabled(self, rule_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return {"ok": False, "code": "unknown_automation", "message": f"No automation called '{rule_id}'."}
            updated = AutomationRule(
                id=rule.id, name=rule.name, enabled=enabled, trigger=rule.trigger,
                conditions=rule.conditions, actions=rule.actions,
                cooldown_seconds=rule.cooldown_seconds, execution_policy=rule.execution_policy,
                # P0.11 - must be carried through here too, or toggling a
                # sequence-based rule's enabled state via enable_automation()/
                # disable_automation() would silently drop its sequence on
                # the next persisted write (the same bug class this line
                # would already have for `actions` if it were omitted).
                sequence=rule.sequence,
                # P0.12 - same reasoning as `sequence` above: description/
                # created_at/updated_at must survive an enable/disable
                # toggle's persisted write, or every automation created
                # through the new API would silently lose its metadata
                # the first time someone flipped its enabled switch.
                # `updated_at` is intentionally NOT refreshed here -
                # enabling/disabling is a distinct, existing operation
                # from `update_rule()`, and conflating the two would make
                # `updated_at` a less reliable signal for "the rule's
                # DEFINITION changed" specifically.
                description=rule.description, created_at=rule.created_at, updated_at=rule.updated_at,
            )
            self._rules[rule_id] = updated
            all_rules = dict(self._rules)
        self._persist_rules(all_rules)
        return {"ok": True, "code": "automation_enabled" if enabled else "automation_disabled",
                "message": f"Automation '{rule_id}' {'enabled' if enabled else 'disabled'}."}

    def _persist_rules(self, rules: Dict[str, AutomationRule]) -> None:
        """Phase 11 - the ONLY write path this module has. Rare
        (enable/disable only), never per-event. Reuses
        `luno.persistence.atomic_write_json()` (backup + Sprint 67
        mutation audit both come for free from that shared primitive -
        see module docstring)."""
        try:
            from .. import persistence
            payload = {rid: _rule_to_storage_dict(r) for rid, r in rules.items()}
            persistence.atomic_write_json(
                self._rules_path, payload,
                source_component="automation_engine", source_operation="set_enabled",
            )
        except Exception as ex:  # pragma: no cover - defensive
            log(f"failed to persist automation rules (in-memory state still updated): {ex}", self.name)

    # -- Phase 5 - camera ownership hook ---------------------------------------

    def on_camera_dispatch(self, tool_call: Dict[str, Any]) -> None:
        """Registered as a `ToolManagerBridgeModule` pre-dispatch hook -
        see module docstring's "Manual > Automation" section. Never
        raises - a hook that crashes must never block a manual command
        the user is actively waiting on."""
        try:
            if tool_call.get("tool") not in ("camera_ptz", "camera_patrol"):
                return
            parameters = tool_call.get("parameters") or {}
            if parameters.get("_automation_origin") or parameters.get("_patrol_origin"):
                return
            with self._lock:
                self._manual_priority_until = time.monotonic() + _MANUAL_PRIORITY_WINDOW_S
        except Exception as ex:  # pragma: no cover - defensive
            log(f"on_camera_dispatch raised (ignored): {ex}", self.name)

    def _manual_priority_active(self) -> bool:
        with self._lock:
            return time.monotonic() < self._manual_priority_until

    # -- trigger entry points ---------------------------------------------------

    def _on_bus_event(self, event: Event) -> None:
        """The Event Bus subscriber (Phase 10 - `event_bus.subscribe("*",
        ...)`, the SAME established observability-tap idiom
        `event_log_writer.py` already uses, not a new mechanism, not
        polling). Cheap: a dict lookup, nothing else, on the pump thread -
        see Phase 16's own <5ms budget for this exact code path. Actual
        rule execution always happens on a dedicated per-execution
        thread (`_trigger()` below), never inline here, for the same
        reason `ToolManagerBridgeModule`'s own C1 audit fix already
        established: a slow handler must never block delivery of every
        other event system-wide."""
        with self._lock:
            rule_ids = list(self._rules_by_event.get(event.type, ()))
            rules = [self._rules[rid] for rid in rule_ids if rid in self._rules]
        for rule in rules:
            if not rule.enabled:
                continue
            # P0.6 - thread the triggering event's own `.data` through so
            # an `event.<field>` condition (see conditions.py) can match
            # against THIS specific event, not just externally-registered
            # state_readers. `event.data` is already a plain dict (Phase
            # 10's own metadata-only payload convention every publisher
            # in this project already follows) - passed by reference,
            # never mutated here or anywhere downstream.
            self._trigger(rule, {"type": "event", "parameters": {"event_name": event.type}}, event_data=event.data)

    def _on_time_trigger(self, rule_id: str) -> None:
        """Called by the REUSED `runtime.scheduler` (Phase 8) when a
        `time:HH:MM` predicate job becomes due - never a new thread/timer
        of this module's own."""
        with self._lock:
            rule = self._rules.get(rule_id)
        if rule is None or not rule.enabled:
            return
        self._trigger(rule, {"type": "time", "parameters": {"time": rule.trigger.parameters.get("time")}})

    # -- the pipeline itself -----------------------------------------------------

    def _trigger(
        self,
        rule: AutomationRule,
        trigger_info: Dict[str, Any],
        _depth: int = 0,
        event_data: Optional[Dict[str, Any]] = None,
        _execution_out: Optional[List[AutomationExecution]] = None,
    ) -> Tuple[bool, str]:
        """Phase 6's own "Find matching rules -> Check enabled -> Check
        cooldown" steps, plus Phase 9's reentrancy/cycle checks - all
        cheap, synchronous, in-memory (Phase 16 budget). Returns
        `(accepted, code)`; `code` is `""` on acceptance or a `refused_*`/
        `automation_cycle_detected` reason otherwise. On acceptance,
        spawns the dedicated per-execution worker thread that actually
        runs conditions/actions.

        `event_data` (P0.6, optional): the `.data` dict of the event
        that caused this trigger, if any (only `_on_bus_event` ever
        passes one - `_on_time_trigger`/`run_automation()` have no
        originating event and leave this `None`, unchanged from before
        P0.6). Threaded through to `_evaluate_conditions()` so an
        `event.<field>` condition can match against it - see
        `conditions.py` module docstring.

        `_execution_out` (P0.12, optional): a purely OUT-parameter seam
        for a caller that needs to observe the `AutomationExecution`
        object this call creates (currently only `run_automation()`,
        so the new `POST /api/automations/{id}/run` endpoint can report
        a real `execution_id` back to the caller) WITHOUT changing this
        method's own return type/arity - the two existing direct
        `engine._trigger(...)` call sites in `tests/test_sprint72_
        automation_engine.py` already unpack a strict 2-tuple
        (`accepted, code = engine._trigger(...)`) and must keep working
        unmodified. If provided, the created `AutomationExecution` is
        appended to it on every path that actually constructs one
        (the accepted path, and the cycle-detected refusal path, which
        also builds one purely to publish a real `automation.failed`
        event) - left untouched (never appended to) for the two
        refusal paths above that return before any execution object
        exists (`refused_already_running`/`skipped_cooldown`), so an
        empty list after the call honestly means "no execution was
        created for this attempt"."""
        if _depth > MAX_EXECUTION_DEPTH:
            log(f"automation '{rule.id}' refused: max execution depth exceeded", self.name)
            return False, "automation_cycle_detected"

        now = time.monotonic()
        with self._lock:
            if rule.id in self._running_rule_ids:
                return False, "refused_already_running"
            cooldown_until = self._cooldown_until.get(rule.id)
            if cooldown_until is not None and now < cooldown_until:
                self._publish_skipped(rule, trigger_info, "cooldown_active")
                return False, "skipped_cooldown"
            recent = [t for rid, t in self._recent_firings if rid == rule.id and (now - t) < _CYCLE_WINDOW_S]
            if len(recent) >= _MAX_FIRINGS_IN_WINDOW:
                log(f"automation '{rule.id}' refused: cycle/loop protection triggered "
                    f"({len(recent)} firings within {_CYCLE_WINDOW_S}s)", self.name)
                cycle_execution = self._new_execution(rule, trigger_info, _depth)
                if _execution_out is not None:
                    _execution_out.append(cycle_execution)
                self._publish_failed(rule, cycle_execution, "automation_cycle_detected")
                return False, "automation_cycle_detected"
            self._recent_firings.append((rule.id, now))
            self._running_rule_ids.add(rule.id)

        execution = self._new_execution(rule, trigger_info, _depth)
        if _execution_out is not None:
            _execution_out.append(execution)
        thread = threading.Thread(
            target=self._run_execution, args=(rule, execution, event_data),
            daemon=True, name=f"luno-automation-{rule.id}",
        )
        thread.start()
        return True, ""

    def _new_execution(self, rule: AutomationRule, trigger_info: Dict[str, Any], depth: int) -> AutomationExecution:
        execution_id = generate_id("exec")
        return AutomationExecution(
            execution_id=execution_id, rule_id=rule.id, correlation_id=execution_id,
            depth=depth, trigger=trigger_info, final_status=ExecutionStatus.PENDING.value,
        )

    def _run_execution(
        self, rule: AutomationRule, execution: AutomationExecution, event_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            execution.started_at = time.monotonic()
            self._publish(execution, "automation.triggered")

            passed, reason = self._evaluate_conditions(rule, event_data)
            execution.condition_result = passed
            if not passed:
                execution.final_status = ExecutionStatus.SKIPPED.value
                execution.reason = reason or "condition_failed"
                self._publish(execution, "automation.condition_failed", reason=execution.reason)
                self._publish(execution, "automation.skipped", reason=execution.reason)
                return
            self._publish(execution, "automation.condition_passed")

            # P0.11 (Action Sequence Engine) - a rule using the new
            # `sequence` form is routed to a dedicated, self-contained
            # execution path that already finalizes (COMPLETED/FAILED)
            # and publishes its own terminal event before returning - see
            # `_run_sequence()`'s own docstring for why this mirrors the
            # SAME "the callback finalizes itself" shape P0.8.9's own
            # delayed-action mechanism below already established, rather
            # than reusing `_verify_and_finalize()`'s partial-failure
            # classification (Section 7 of this sprint's brief requires
            # a strict, binary FAILED-on-first-failing-step outcome, not
            # a three-way COMPLETED/PARTIAL_FAILURE/FAILED count). A rule
            # with an empty `rule.sequence` (i.e. every rule that existed
            # before this sprint) never enters this branch at all.
            if rule.sequence:
                self._run_sequence(rule, execution, event_data)
                return

            # P0.8.0 - `event_data` is threaded one step further than
            # before (it previously stopped at `_evaluate_conditions()`)
            # so the camera action safety gate can inspect the SAME
            # triggering CameraEvent's fields (kind/available/
            # detection_error) at ACTION-dispatch time, not just at
            # condition-evaluation time. `None` for time/manual triggers,
            # unchanged.
            self._run_actions(rule, execution, event_data)
            # P0.8.9 - if this run scheduled a DELAYED Home Assistant
            # action (see `_dispatch_home_assistant_action()`/`_schedule_
            # delayed_ha_action()`), genuine VERIFY/finalize must wait
            # for that action to actually fire (or be superseded) - never
            # claim COMPLETED for an action that has only been scheduled,
            # not dispatched (the same "do not claim the device changed
            # state merely because a call was queued" honesty discipline
            # this project already applies to `_verify_and_finalize()`'s
            # own docstring). The deferred callback (`_fire()` inside
            # `_schedule_delayed_ha_action()`) calls `_verify_and_
            # finalize(execution)` itself once it actually knows the
            # outcome; `_cancel_pending_delayed_action()` finalizes it as
            # SKIPPED/`action_superseded` if cancelled first instead.
            if any(r.code == "action_scheduled_delayed" for r in execution.action_results):
                return
            self._verify_and_finalize(execution)
        except Exception as ex:  # pragma: no cover - defensive, thread must never die silently
            log(f"automation '{rule.id}' execution raised an unexpected exception: {ex}", self.name)
            execution.final_status = ExecutionStatus.FAILED.value
            execution.reason = f"unexpected error: {ex}"
            self._publish(execution, "automation.failed", reason=execution.reason)
        finally:
            execution.completed_at = time.monotonic()
            with self._lock:
                self._running_rule_ids.discard(rule.id)
                # P0.8.2 fix (found via this sprint's own Section 6
                # verification, not invented speculatively): a cooldown
                # must only start once this rule's CONDITIONS actually
                # passed (i.e. it genuinely attempted its actions,
                # whether they then succeeded or failed) - never for a
                # SKIPPED execution whose conditions never matched in
                # the first place. Before this fix, `_cooldown_until`
                # was set unconditionally on every attempted trigger,
                # including one whose conditions failed - harmless for
                # every single-purpose rule this project had before
                # P0.8.2 (each one either matches or doesn't, with no
                # opposite-action sibling rule sharing its trigger), but
                # a real defect for a mutually-exclusive ON/OFF rule
                # PAIR sharing one trigger (`camera_test_automation_
                # safety_action`/`_off`): every `human_detected` event
                # was silently starting the OFF rule's own 30s cooldown
                # too (its conditions correctly failed, but the OLD code
                # still burned its cooldown window), so a person leaving
                # camera view within that window would find the OFF
                # rule already "in cooldown" and skip the genuine
                # `human_cleared` trigger, leaving the light stuck ON.
                # `execution.condition_result` is `None` (falsy) both
                # for a condition_failed SKIP and for the defensive
                # unexpected-exception path above, so both are correctly
                # excluded here - only a rule whose conditions were
                # actually evaluated as `True` ever starts its own
                # cooldown, exactly matching what "cooldown" is supposed
                # to mean ("don't repeat this rule's OWN actions too
                # soon"). No new cooldown mechanism was added - this is
                # the SAME `_cooldown_until` dict (Sprint 72, Phase 8),
                # simply gated one condition more precisely.
                if rule.cooldown_seconds > 0 and execution.condition_result:
                    self._cooldown_until[rule.id] = time.monotonic() + rule.cooldown_seconds
                self._last_execution[rule.id] = execution
                history = self._history.setdefault(rule.id, [])
                history.append(execution)
                if len(history) > _HISTORY_PER_RULE:
                    del history[: len(history) - _HISTORY_PER_RULE]

    def _evaluate_conditions(
        self, rule: AutomationRule, event_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Phase 3 - pure, read-only, never mutates anything. ALL
        conditions must pass (AND semantics) - the first failing/invalid
        one short-circuits, same "no partial execution" discipline the
        action loop below also follows.

        `event_data` (P0.6, optional): passed straight through to
        `evaluate_condition()` for any `event.<field>` condition - see
        `conditions.py` module docstring. `None` for time/manual
        triggers, unchanged pre-P0.6 behavior."""
        for condition in rule.conditions:
            ok, reason = evaluate_condition(condition, self._state_readers, event_data=event_data)
            if not ok:
                return False, reason or "condition_failed"
        return True, ""

    def _run_actions(
        self, rule: AutomationRule, execution: AutomationExecution, event_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        for action in rule.actions:
            self._publish(execution, "automation.action_started", action_type=action.type)
            result = self._dispatch_action(rule, action, execution, event_data)
            execution.action_results.append(result)
            event_type = "automation.action_completed" if result.status == "completed" else "automation.action_failed"
            self._publish(execution, event_type, action_type=action.type, status=result.status, code=result.code)

    # -- P0.11 (Action Sequence Engine) -----------------------------------------
    #
    # A `sequence`-based rule's steps run STRICTLY one at a time, on the
    # SAME dedicated per-execution thread `_trigger()` already spawns for
    # every execution (legacy or sequence) - see that method's own
    # docstring. This is what makes the sequential guarantee (Section 6
    # of the P0.11 brief: "A completes before B starts") and the
    # concurrency guarantee (Section 15: "a sequence's delay must not
    # block an unrelated automation") BOTH fall out of architecture that
    # already existed before this sprint, with zero new threading
    # primitives: `_wait_delay()` below blocks only the CALLING thread
    # (this execution's own), never the Event Bus pump thread, never the
    # AutomationEngine's own bookkeeping, and never another execution's
    # thread. Every device-action step is dispatched through the EXACT
    # SAME `_dispatch_action()` -> `_dispatch_tool_call()` -> the shared
    # `tool_requested` round trip every other action in this engine
    # (legacy or sequence) already uses - there is no second, parallel
    # device-control path anywhere in this section.

    def _run_sequence(
        self, rule: AutomationRule, execution: AutomationExecution, event_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Runs `rule.sequence` steps in strict order, stopping at the
        first failing step (P0.11 Section 7: "B fails -> C must NOT
        execute" - deliberately different from `_run_actions()`'s own
        "run every action regardless, classify the aggregate afterward"
        policy, which stays completely unmodified for `actions`-based
        rules). Self-finalizes (calls `_verify_and_finalize_sequence()`
        and returns) rather than falling through to the shared
        `_verify_and_finalize()` used by the legacy path, since that
        function's three-way COMPLETED/PARTIAL_FAILURE/FAILED
        classification does not match Section 7's required binary
        outcome for a sequence that stopped early."""
        total = len(rule.sequence)
        execution.total_steps = total
        execution.final_status = ExecutionStatus.RUNNING.value
        # Makes this IN-PROGRESS execution (not just its final outcome)
        # observable via get_status()/get_automation_status() while a
        # multi-second sequence is still running - `execution` is a
        # mutable object, so subsequent mutations below (current_step_
        # index, action_results, final_status) are visible through this
        # SAME stored reference without any further bookkeeping. Legacy
        # `actions`-based executions are unaffected - `_run_execution()`'s
        # own `finally` block is what sets `_last_execution` for them,
        # unchanged.
        with self._lock:
            self._last_execution[rule.id] = execution
        log(f"[Automation] execution={execution.execution_id} started automation={rule.id} steps={total}", self.name)

        for index, step in enumerate(rule.sequence):
            execution.current_step_index = index
            step_num = index + 1
            result = self._run_sequence_step(rule, execution, step, event_data, index, step_num, total)
            execution.action_results.append(result)

            # P0.14 - a `stop_automation` step (at this level OR reached
            # through a nested `condition` branch - `_run_condition_step()`
            # propagates its own inner result verbatim, so this check is
            # keyed on the RESULT's type/code, never `step.type`, and
            # therefore catches both cases identically) is a deliberate,
            # intentional early exit - CANCELLED, never FAILED.
            if result.type == _SEQUENCE_STOP_STEP_TYPE and result.status == "completed" and result.code == "stop_requested":
                execution.reason = execution.reason or "stopped_by_stop_automation_step"
                execution.final_status = ExecutionStatus.CANCELLED.value
                self._publish(execution, "automation.cancelled", reason=execution.reason)
                log(
                    f"[Automation] execution={execution.execution_id} CANCELLED at step={step_num}/{total} "
                    f"(stop_automation)", self.name,
                )
                return

            # P0.14 - a `wait_until` step (at this level or nested inside a
            # `condition` branch) that never saw its condition become true
            # within its own bounded timeout is a DISTINCT terminal outcome
            # from a genuine failure (Section 13/15) - the step behaved
            # exactly as designed, it simply ran out of its own honestly
            # bounded budget.
            if result.status == "timeout":
                execution.reason = f"step {index} ({step.type}) timed out: {result.message or result.code}"
                execution.final_status = ExecutionStatus.TIMEOUT.value
                self._publish(execution, "automation.timeout", reason=execution.reason)
                log(
                    f"[Automation] execution={execution.execution_id} TIMEOUT at step={step_num}/{total} "
                    f"type={step.type}", self.name,
                )
                return

            if result.status != "completed":
                execution.reason = f"step {index} ({step.type}) failed: {result.message or result.code}"
                log(
                    f"[Automation] execution={execution.execution_id} FAILED step={step_num}/{total} "
                    f"type={step.type} error={result.message!r}", self.name,
                )
                break

        self._verify_and_finalize_sequence(execution)

    def _run_sequence_step(
        self, rule: AutomationRule, execution: AutomationExecution, step: AutomationAction,
        event_data: Optional[Dict[str, Any]], index: int, step_num: int, total: int,
    ) -> ActionResult:
        """P0.11/P0.14 - the single dispatch point every sequence step (top
        level OR nested inside a `condition` branch - `_run_condition_step()`
        calls this SAME method for its own `then`/`else` steps, never a
        second copy of this if/elif chain) goes through. Keeping this as
        one small router (rather than inlining the branches at each of the
        two call sites) is what guarantees a step behaves identically
        whether it appears at the top of `rule.sequence` or inside a
        branch - the exact same guarantee Section 9 of the P0.14 brief
        asks for ("a home_assistant.* action, a delay, or another
        condition step all work identically inside a branch as they do at
        the top level")."""
        if step.type == _SEQUENCE_DELAY_STEP_TYPE:
            return self._run_delay_step(rule, execution, step, index, step_num, total)
        if step.type == _SEQUENCE_STOP_STEP_TYPE:
            return self._run_stop_step(execution, index, step_num, total)
        if step.type == _SEQUENCE_WAIT_UNTIL_STEP_TYPE:
            return self._run_wait_until_step(execution, step, index, step_num, total)
        if step.type == _SEQUENCE_CONDITION_STEP_TYPE:
            return self._run_condition_step(rule, execution, step, event_data, index, step_num, total)
        return self._run_action_step(rule, execution, step, event_data, index, step_num, total)

    def _run_stop_step(
        self, execution: AutomationExecution, index: int, step_num: int, total: int,
    ) -> ActionResult:
        """P0.14 Section 3 (CONTROL: "Stop Automation") - an explicit,
        intentional early exit. Never dispatches a tool call, never reads
        or mutates any device state - the ONLY thing this step does is
        produce a result `_run_sequence()`'s own stop-detection (see that
        method's own comment) recognizes and turns into
        `ExecutionStatus.CANCELLED`."""
        log(f"[Automation] execution={execution.execution_id} step={step_num}/{total} type=stop_automation - stopping sequence intentionally", self.name)
        self._publish(execution, "automation.step_started", step_index=index, total_steps=total, step_type=_SEQUENCE_STOP_STEP_TYPE)
        result = ActionResult(
            type=_SEQUENCE_STOP_STEP_TYPE, status="completed", code="stop_requested",
            message="automation stopped intentionally by a stop_automation step",
        )
        self._publish(
            execution, "automation.step_completed", step_index=index, total_steps=total,
            step_type=_SEQUENCE_STOP_STEP_TYPE, status=result.status, code=result.code,
        )
        return result

    def _run_wait_until_step(
        self, execution: AutomationExecution, step: AutomationAction, index: int, step_num: int, total: int,
    ) -> ActionResult:
        """P0.14 Section 8 - polls the SAME `ha_state_reader` hook the
        Camera Action Safety Gate already uses (`AutomationEngine.
        ha_state_reader`, wired by `luno/bootstrap/adapters.py::
        register_camera_action_ha_state_reader()` only when a real HA
        backend is connected - see that function's own docstring) - no
        second HA read path, no new client. Blocks ONLY this execution's
        own dedicated thread (`threading.Event().wait()`, the exact same
        non-busy-loop primitive `_wait_delay()` already uses), never the
        Event Bus pump thread and never another execution's thread - the
        same concurrency guarantee P0.11's `_wait_delay()` already
        established (Section 19: "a sequence's delay must not block an
        unrelated automation"), extended to a bounded polling wait rather
        than a fixed sleep.

        Only `attribute == "state"` is actually checkable today - there is
        no attribute-level (brightness/rgb_color/etc.) state reader
        anywhere in this engine, and this method never fabricates a match
        for what it cannot genuinely read (same honesty discipline
        `RealHomeAssistantHandler`'s own verify-loop already applies to
        device actions)."""
        p = step.parameters
        target = str(p.get("target", ""))
        attribute = str(p.get("attribute", "state") or "state")
        operator = str(p.get("operator", ""))
        expected_value = p.get("value")
        timeout_seconds = _coerce_wait_until_timeout(p.get("timeout_seconds"))

        log(
            f"[Automation] execution={execution.execution_id} step={step_num}/{total} type=wait_until started "
            f"target={target!r} attribute={attribute!r} operator={operator!r} timeout={timeout_seconds}s", self.name,
        )
        self._publish(execution, "automation.step_started", step_index=index, total_steps=total, step_type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE)

        if attribute != "state" or self.ha_state_reader is None:
            code = "ha_state_reader_unavailable" if self.ha_state_reader is None else "unsupported_attribute"
            message = (
                "no Home Assistant state reader is bound (a real, connected Home Assistant backend is required "
                "for wait_until - the mock backend has no live entity state to poll)"
                if self.ha_state_reader is None
                else f"wait_until currently only supports attribute='state', got {attribute!r}"
            )
            result = ActionResult(type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status="timeout", code=code, message=message)
            self._publish(
                execution, "automation.step_failed", step_index=index, total_steps=total,
                step_type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status=result.status, code=result.code,
            )
            return result

        condition = AutomationCondition(type=operator, target="_wait_until_target", value=expected_value)
        deadline = time.monotonic() + timeout_seconds
        poll_interval_s = max(0.05, min(0.5, timeout_seconds))
        last_seen: Any = None
        while True:
            last_seen = self.ha_state_reader(target)
            ok, _reason = evaluate_condition(condition, {"_wait_until_target": lambda: last_seen}, event_data=None)
            if ok:
                result = ActionResult(
                    type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status="completed", code="condition_met",
                    message=f"{target}.{attribute} {operator} {expected_value!r} (actual={last_seen!r})",
                )
                log(f"[Automation] execution={execution.execution_id} step={step_num}/{total} completed (wait_until condition met)", self.name)
                self._publish(
                    execution, "automation.step_completed", step_index=index, total_steps=total,
                    step_type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status=result.status, code=result.code,
                )
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            threading.Event().wait(min(poll_interval_s, remaining))

        result = ActionResult(
            type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status="timeout", code="wait_timeout",
            message=f"timed out after {timeout_seconds}s waiting for {target}.{attribute} {operator} "
                    f"{expected_value!r} (last seen={last_seen!r})",
        )
        log(f"[Automation] execution={execution.execution_id} step={step_num}/{total} TIMEOUT (wait_until never satisfied)", self.name)
        self._publish(
            execution, "automation.step_failed", step_index=index, total_steps=total,
            step_type=_SEQUENCE_WAIT_UNTIL_STEP_TYPE, status=result.status, code=result.code,
        )
        return result

    def _run_condition_step(
        self, rule: AutomationRule, execution: AutomationExecution, step: AutomationAction,
        event_data: Optional[Dict[str, Any]], index: int, step_num: int, total: int,
    ) -> ActionResult:
        """P0.14 Section 9 - a constrained if/then/else branch. Reuses
        `evaluate_condition()` verbatim for its own `conditions` list
        (AND semantics, identical to `_evaluate_conditions()` above - no
        second condition engine) and `_run_sequence_step()` verbatim for
        every step inside whichever branch is chosen (so a nested action/
        delay/wait_until/condition step behaves identically to a top-level
        one - see `_run_sequence_step()`'s own docstring). The FIRST
        non-completed nested result (failure, timeout, or an intentional
        stop) is returned AS-IS (its own `type`/`status`/`code` preserved)
        rather than wrapped - `_run_sequence()`'s own result-based
        stop/timeout/failure detection (keyed on the RESULT, never
        `step.type`) then treats it identically to a top-level occurrence
        of the same outcome, with zero special-casing needed there for
        nesting."""
        p = step.parameters
        raw_conditions = p.get("conditions") or []
        conditions = [
            AutomationCondition(type=str(c.get("type", "")), target=str(c.get("target", "")), value=c.get("value"))
            for c in raw_conditions if isinstance(c, dict)
        ]
        self._publish(execution, "automation.step_started", step_index=index, total_steps=total, step_type=_SEQUENCE_CONDITION_STEP_TYPE)

        passed = True
        for cond in conditions:
            ok, why = evaluate_condition(cond, self._state_readers, event_data=event_data)
            if not ok:
                passed = False
                break

        branch_name = "then" if passed else "else"
        branch_raw = p.get(branch_name) or []
        log(
            f"[Automation] execution={execution.execution_id} step={step_num}/{total} type=condition "
            f"evaluated={passed} branch={branch_name} steps={len(branch_raw)}", self.name,
        )

        for raw_sub in branch_raw:
            if not isinstance(raw_sub, dict):
                continue
            sub_step = _sequence_step_from_raw(raw_sub)
            sub_result = self._run_sequence_step(rule, execution, sub_step, event_data, index, step_num, total)
            if sub_result.status != "completed" or (sub_result.type == _SEQUENCE_STOP_STEP_TYPE and sub_result.code == "stop_requested"):
                return sub_result

        result = ActionResult(
            type=_SEQUENCE_CONDITION_STEP_TYPE, status="completed", code="condition_branch_completed",
            message=f"condition evaluated to {passed}, {branch_name!r} branch completed ({len(branch_raw)} step(s))",
        )
        self._publish(
            execution, "automation.step_completed", step_index=index, total_steps=total,
            step_type=_SEQUENCE_CONDITION_STEP_TYPE, status=result.status, code=result.code,
        )
        return result

    def _run_delay_step(
        self, rule: AutomationRule, execution: AutomationExecution, step: AutomationAction,
        index: int, step_num: int, total: int,
    ) -> ActionResult:
        """Section 4.2/9 - a delay step has its OWN execution semantics,
        distinct from a device action: it never dispatches a tool call,
        and is never represented as a fake device action (its
        `ActionResult.type` is always the literal `"delay"`, never one of
        `ACTION_TYPES`, so a log/dashboard consumer can always tell the
        two apart)."""
        seconds = _coerce_sequence_delay_seconds(step.parameters.get("seconds"))
        log(
            f"[Automation] execution={execution.execution_id} step={step_num}/{total} "
            f"type=delay started duration={seconds}s", self.name,
        )
        self._publish(
            execution, "automation.step_started", step_index=index, total_steps=total,
            step_type=_SEQUENCE_DELAY_STEP_TYPE,
        )
        self._wait_delay(seconds)
        result = ActionResult(
            type=_SEQUENCE_DELAY_STEP_TYPE, status="completed", code="delay_completed",
            message=f"waited {seconds}s",
        )
        log(f"[Automation] execution={execution.execution_id} step={step_num}/{total} completed", self.name)
        self._publish(
            execution, "automation.step_completed", step_index=index, total_steps=total,
            step_type=_SEQUENCE_DELAY_STEP_TYPE, status=result.status, code=result.code,
        )
        return result

    def _run_action_step(
        self, rule: AutomationRule, execution: AutomationExecution, step: AutomationAction,
        event_data: Optional[Dict[str, Any]], index: int, step_num: int, total: int,
    ) -> ActionResult:
        """A device/internal/camera sequence step - reuses `_dispatch_
        action()` VERBATIM (Section 8 - "every device action must
        continue through the existing ToolManager"; this is the same
        dispatch call `_run_actions()` above makes for a legacy `actions`
        entry, so every existing safety gate, ownership rule, and P0.8.9
        per-action `delay_seconds` mechanic that already applies to a
        legacy action applies identically here - no second device-
        control code path was written for P0.11)."""
        log(
            f"[Automation] execution={execution.execution_id} step={step_num}/{total} "
            f"type={step.type} started", self.name,
        )
        self._publish(execution, "automation.step_started", step_index=index, total_steps=total, step_type=step.type)
        result = self._dispatch_action(rule, step, execution, event_data)
        ok = result.status == "completed"
        log(
            f"[Automation] execution={execution.execution_id} step={step_num}/{total} "
            f"{'completed' if ok else 'FAILED'}" + ("" if ok else f" error={result.message!r}"), self.name,
        )
        self._publish(
            execution, "automation.step_completed" if ok else "automation.step_failed",
            step_index=index, total_steps=total, step_type=step.type, status=result.status, code=result.code,
        )
        return result

    def _wait_delay(self, seconds: float) -> None:
        """P0.11 Section 4.2/19 - blocks ONLY the calling execution's own
        dedicated thread (see this section's own header comment) for
        `seconds`, never the AutomationEngine itself and never any other
        execution. Uses `threading.Event().wait(seconds)` rather than a
        bare `time.sleep(seconds)` - functionally equivalent today (no
        execution-cancellation mechanism exists anywhere in this engine
        yet, see the P0.11 change-impact doc's own Known Limitations for
        why one was NOT built this sprint), but this is the identical
        non-busy-loop blocking-wait primitive `_dispatch_tool_call()`
        already established elsewhere in this file, and leaves a ready
        `.set()` seam a future cancellation token could use to wake this
        wait early without changing its call sites. Never busy-loops:
        `Event.wait()` blocks on a real OS-level condition variable, the
        same primitive `threading.Event` uses throughout this project."""
        if seconds <= 0:
            return
        threading.Event().wait(seconds)

    def _verify_and_finalize_sequence(self, execution: AutomationExecution) -> None:
        """P0.11's own VERIFY step - Section 7's required BINARY outcome
        (COMPLETED only if every step that ran completed; FAILED the
        moment any step didn't) rather than `_verify_and_finalize()`'s
        three-way legacy classification (see `_run_sequence()`'s own
        docstring for why the two must differ)."""
        results = execution.action_results
        if not results:
            execution.final_status = ExecutionStatus.SKIPPED.value
            self._publish(execution, "automation.skipped", reason="no_actions")
            return
        if all(r.status == "completed" for r in results):
            execution.final_status = ExecutionStatus.COMPLETED.value
            self._publish(execution, "automation.completed")
            log(
                f"[Automation] execution={execution.execution_id} completed "
                f"duration={self._execution_duration_s(execution):.2f}s", self.name,
            )
        else:
            execution.final_status = ExecutionStatus.FAILED.value
            execution.reason = execution.reason or "sequence_step_failed"
            self._publish(execution, "automation.failed", reason=execution.reason)

    @staticmethod
    def _execution_duration_s(execution: AutomationExecution) -> float:
        if execution.started_at is None:
            return 0.0
        end = execution.completed_at if execution.completed_at is not None else time.monotonic()
        return max(0.0, end - execution.started_at)

    def _dispatch_action(
        self, rule: AutomationRule, action: AutomationAction, execution: AutomationExecution,
        event_data: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        if action.type not in ACTION_TYPES:
            # Defense in depth - validate_rule() should already have
            # refused this at load time (Phase 12: unknown action refused).
            return ActionResult(type=action.type, status="refused", code="action_type_not_allowlisted",
                                 message=f"Action type '{action.type}' is not allowlisted.")

        if action.type in _INTERNAL_ACTION_TYPES:
            return self._dispatch_internal_action(action, execution)

        if action.type in _CAMERA_ACTION_TYPES:
            if self._manual_priority_active():
                return ActionResult(type=action.type, status="refused", code="action_refused_busy",
                                     message="A manual camera command has priority right now.")
            return self._dispatch_camera_action(action, execution)

        return self._dispatch_home_assistant_action(rule, action, execution, event_data)

    def _dispatch_internal_action(self, action: AutomationAction, execution: AutomationExecution) -> ActionResult:
        message = str(action.parameters.get("message", ""))
        log(f"automation.log [{execution.rule_id}/{execution.execution_id}]: {message}", self.name)
        return ActionResult(type=action.type, status="completed", message=message)

    def _dispatch_camera_action(self, action: AutomationAction, execution: AutomationExecution) -> ActionResult:
        origin = {"_automation_origin": True, "_automation_execution_id": execution.execution_id}
        if action.type == "camera.preset":
            tool_call = {"tool": "camera_ptz", "action": "goto_preset", "target": action.parameters.get("preset"), "parameters": origin}
        elif action.type == "camera.home":
            tool_call = {"tool": "camera_ptz", "action": "center", "target": None, "parameters": origin}
        else:  # camera.stop_patrol
            tool_call = {"tool": "camera_patrol", "action": "stop", "target": None, "parameters": origin}
        ok, message = self._dispatch_tool_call(tool_call)
        return ActionResult(type=action.type, status="completed" if ok else "failed", message=message or "")

    def _dispatch_home_assistant_action(
        self, rule: AutomationRule, action: AutomationAction, execution: AutomationExecution,
        event_data: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        target = action.parameters.get("target")

        # P0.8.0 - the camera action safety gate runs ONLY for a rule
        # whose trigger is the camera automation event - every other HA
        # action (time/manual triggers, or an event trigger on some
        # unrelated event type) is completely unaffected, byte-for-byte
        # the same dispatch path as before this sprint.
        if self._is_camera_triggered_rule(rule):
            check = validate_camera_ha_action(
                action_type=action.type, target=target, event_data=event_data,
                ha_state_reader=self.ha_state_reader,
            )
            if not check.allowed:
                log(
                    f"camera action safety gate refused rule '{rule.id}' action '{action.type}' "
                    f"target={target!r}: {check.code} ({check.message})", self.name,
                )
                return ActionResult(type=action.type, status="refused", code=check.code, message=check.message)
            if check.skip_dispatch:
                log(
                    f"camera action safety gate: rule '{rule.id}' action '{action.type}' "
                    f"target={target!r} skipped ({check.code}): {check.message}", self.name,
                )
                return ActionResult(type=action.type, status="completed", code=check.code, message=check.message)

        # P0.8.9 - a dispatch (immediate OR delayed) for a given target
        # entity always supersedes whatever was previously pending for
        # that SAME target, unconditionally. This one small, generic rule
        # is what implements BOTH required cancellation semantics without
        # either rule needing to know the other's id: a fresh
        # `human_confirmed` -> immediate `turn_on` for `light.wled`
        # cancels a pending debounced `turn_off` for `light.wled`, and a
        # REPEATED `human_cleared` while an OFF is already pending simply
        # cancels-and-reschedules its own pending job (resets the
        # debounce window, never double-fires). For every rule that never
        # uses `delay_seconds` (i.e. every rule this project had before
        # P0.8.9), `_pending_delayed_actions` never has an entry for that
        # rule's target, so this is a guaranteed no-op - byte-for-byte
        # unchanged behavior.
        # P0.14 - only turn_on/turn_off/toggle ever accept `delay_seconds`
        # (`models.py::validate_action()` refuses it up front for every
        # other action type at rule-LOAD time - see that function's own
        # `_delay_seconds_action_types` check) - so the P0.8.9 debounce/
        # supersede mechanism below is correctly scoped to exactly these
        # three, never a gap for the new P0.14 action types.
        if action.type in ("home_assistant.turn_on", "home_assistant.turn_off", "home_assistant.toggle"):
            self._cancel_pending_delayed_action(target)

            ha_action = {
                "home_assistant.turn_on": "turn_on", "home_assistant.turn_off": "turn_off",
                "home_assistant.toggle": "toggle",
            }[action.type]
            tool_call = {
                "tool": "home_assistant", "action": ha_action, "target": target,
                "parameters": {"_automation_origin": True, "_automation_execution_id": execution.execution_id},
            }

            delay_seconds = _coerce_delay_seconds(action.parameters.get("delay_seconds"))
            if delay_seconds > 0.0:
                if self._scheduler is not None:
                    return self._schedule_delayed_ha_action(target, tool_call, action, execution, delay_seconds)
                # No scheduler bound (e.g. a standalone/unit-test construction
                # of this engine - see `bind_scheduler()`'s own docstring for
                # the same "optional, documented degraded mode" precedent for
                # TIME triggers). Fail OPEN, not silently-never: dispatch
                # immediately rather than leaving a device stuck in its
                # current state forever because nothing was ever bound to
                # honor the delay. Logged so this is never a silent surprise.
                log(
                    f"automation '{rule.id}': delay_seconds={delay_seconds} requested for target={target!r} but no "
                    f"scheduler is bound - dispatching immediately instead (delay ignored, known limitation).", self.name,
                )

            ok, message = self._dispatch_tool_call(tool_call)
            return ActionResult(type=action.type, status="completed" if ok else "failed", message=message or "")

        # P0.14 (Advanced Home Assistant Automation Actions & Script
        # Runner) - every action type below is new this sprint. Each one
        # still dispatches through the EXACT SAME `_dispatch_tool_call()`
        # -> `tool_requested` -> ToolManager round trip every action in
        # this engine (legacy `actions`, P0.11 `sequence` device steps, or
        # these new types) already uses - see `_build_p0_14_tool_call()`'s
        # own docstring. None of them accept `delay_seconds` (refused at
        # rule-LOAD time - see the comment above), so there is no
        # schedule/supersede branch here, only an immediate, synchronous
        # dispatch.
        tool_call = self._build_p0_14_tool_call(action, execution)
        if tool_call is None:
            # Defense in depth - `_dispatch_action()`'s own ACTION_TYPES
            # membership check, and `validate_action()` at rule-LOAD time
            # before that, should already make this unreachable for any
            # rule that was ever actually saved.
            return ActionResult(type=action.type, status="refused", code="action_type_not_allowlisted",
                                 message=f"Action type '{action.type}' is not allowlisted.")
        ok, message = self._dispatch_tool_call(tool_call)
        return ActionResult(type=action.type, status="completed" if ok else "failed", message=message or "")

    def _build_p0_14_tool_call(self, action: AutomationAction, execution: AutomationExecution) -> Optional[Dict[str, Any]]:
        """P0.14 - translates one of the new `home_assistant.*` action
        types into the SAME `{tool: "home_assistant", action, target,
        parameters}` shape `_dispatch_home_assistant_action()` has always
        built for `turn_on`/`turn_off` - the ToolManager "home_assistant"
        handler (mock AND real) is the single place that actually knows
        how to execute each of these (see that module's own P0.14
        docstring section). Returns `None` only for an action type this
        function does not recognize (unreachable in practice - see this
        method's only caller's own comment)."""
        origin = {"_automation_origin": True, "_automation_execution_id": execution.execution_id}
        p = action.parameters
        if action.type == "home_assistant.set_brightness":
            return {"tool": "home_assistant", "action": "set_brightness", "target": p.get("target"),
                    "parameters": dict(origin, level=p.get("level"))}
        if action.type == "home_assistant.set_color":
            params = dict(origin)
            if "rgb" in p:
                params["rgb"] = p.get("rgb")
            else:
                params["color"] = p.get("color")
            return {"tool": "home_assistant", "action": "set_color", "target": p.get("target"), "parameters": params}
        if action.type == "home_assistant.set_temperature":
            return {"tool": "home_assistant", "action": "set_temperature", "target": p.get("target"),
                    "parameters": dict(origin, value=p.get("value"))}
        if action.type == "home_assistant.run_script":
            params = dict(origin)
            variables = p.get("variables")
            if isinstance(variables, dict) and variables:
                params["variables"] = variables
            return {"tool": "home_assistant", "action": "run_script", "target": p.get("entity_id"), "parameters": params}
        if action.type == "home_assistant.activate_scene":
            return {"tool": "home_assistant", "action": "activate_scene", "target": p.get("entity_id"), "parameters": dict(origin)}
        if action.type == "home_assistant.call_service":
            params = dict(origin)
            params["domain"] = p.get("domain")
            params["service"] = p.get("service")
            params["entity_id"] = _extract_call_service_entity_ids(p.get("target"))
            params["data"] = p.get("data") or {}
            return {"tool": "home_assistant", "action": "call_service", "target": None, "parameters": params}
        return None

    def _cancel_pending_delayed_action(self, target: Optional[str]) -> None:
        """P0.8.9 - see `_pending_delayed_actions`'s own docstring. Pops
        and cancels whatever delayed job (if any) is currently pending for
        `target`, and honestly finalizes the SUPERSEDED execution as
        SKIPPED/`action_superseded` (never leaves it silently PENDING
        forever, and never claims it completed)."""
        if not target:
            return
        with self._lock:
            entry = self._pending_delayed_actions.pop(target, None)
        if entry is None:
            return
        job_id, pending_execution, action_type = entry
        if self._scheduler is not None:
            try:
                self._scheduler.cancel(job_id)
            except Exception as ex:  # pragma: no cover - defensive
                log(f"failed to cancel superseded delayed action for target={target!r} (ignored): {ex}", self.name)
        log(f"cancelled pending delayed automation action for target={target!r} (superseded by a new dispatch)", self.name)
        pending_execution.action_results = [
            r for r in pending_execution.action_results if r.code != "action_scheduled_delayed"
        ] + [ActionResult(
            type=action_type, status="completed", code="action_superseded",
            message=f"superseded before its delay elapsed - a newer action for target={target!r} took priority",
        )]
        pending_execution.final_status = ExecutionStatus.SKIPPED.value
        pending_execution.reason = "action_superseded"
        self._publish(pending_execution, "automation.skipped", reason="action_superseded")

    def _schedule_delayed_ha_action(
        self, target: str, tool_call: Dict[str, Any], action: AutomationAction,
        execution: AutomationExecution, delay_seconds: float,
    ) -> ActionResult:
        """P0.8.9 - uses the project's EXISTING `runtime.scheduler`
        (`Scheduler.schedule_once()`/`cancel()`, already reused by this
        same engine for TIME triggers and cooldown cleanup - see module
        docstring) rather than a new thread/timer of this module's own.
        Returns an honest, immediate `ActionResult` meaning "successfully
        SCHEDULED" (`code="action_scheduled_delayed"`) - genuine VERIFY
        (did the real `home_assistant.turn_on/off` call actually
        complete?) only happens once `_fire()` below actually runs, or
        `_cancel_pending_delayed_action()` supersedes it first."""
        job_id_holder: Dict[str, str] = {}

        def _fire() -> None:
            with self._lock:
                current = self._pending_delayed_actions.get(target)
                if current is None or current[0] != job_id_holder.get("id"):
                    # Superseded/cancelled since being scheduled - the
                    # entry has already been finalized (SKIPPED/
                    # `action_superseded`) by whatever dispatch replaced
                    # it. Doing nothing here is correct, not a bug.
                    return
                del self._pending_delayed_actions[target]
            ok, message = self._dispatch_tool_call(tool_call)
            result = ActionResult(type=action.type, status="completed" if ok else "failed", message=message or "")
            execution.action_results = [
                r for r in execution.action_results if r.code != "action_scheduled_delayed"
            ] + [result]
            event_type = "automation.action_completed" if result.status == "completed" else "automation.action_failed"
            self._publish(execution, event_type, action_type=action.type, status=result.status, code=result.code)
            self._verify_and_finalize(execution)

        job_id = self._scheduler.schedule_once(
            f"automation_delayed_action:{execution.execution_id}", _fire, delay_s=delay_seconds,
        )
        job_id_holder["id"] = job_id
        with self._lock:
            self._pending_delayed_actions[target] = (job_id, execution, action.type)
        return ActionResult(
            type=action.type, status="completed", code="action_scheduled_delayed",
            message=f"delayed {delay_seconds}s before dispatch (target={target!r})",
        )

    def _is_camera_triggered_rule(self, rule: AutomationRule) -> bool:
        """P0.8.0 - True only for a rule whose trigger is `event:
        camera_automation.camera_event` (the exact string
        `VisionCameraEventBridge`/`CameraAutomationModule` publish under -
        see `_CAMERA_AUTOMATION_EVENT_TYPE`'s own comment for why this is
        a literal, not an import). A rule with no trigger at all (should
        never happen - `validate_rule()` refuses that at load time) is
        treated as NOT camera-triggered, the safe default."""
        trigger = rule.trigger
        return (
            trigger is not None and trigger.type == "event"
            and trigger.parameters.get("event_name") == _CAMERA_AUTOMATION_EVENT_TYPE
        )

    def _dispatch_tool_call(self, tool_call: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Reuses the exact `tool_requested`/`tool_finished`/`tool_failed`
        round trip `CameraPatrolModule._dispatch_tool_call()` established
        in Sprint 71 - subscribing to BOTH outcomes BEFORE publishing,
        for the same reason (a fast handler may publish `tool_failed`
        almost immediately)."""
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

        sub_ok = self._event_bus.subscribe("tool_finished", _on_finished)
        sub_err = self._event_bus.subscribe("tool_failed", _on_failed)
        try:
            self._event_bus.publish(Event(type="tool_requested", data={"execution_id": execution_id, "tool_call": tool_call}))
            deadline = time.monotonic() + _ACTION_DISPATCH_TIMEOUT_S
            while time.monotonic() < deadline:
                if done.wait(_POLL_INTERVAL_S):
                    break
        finally:
            self._event_bus.unsubscribe(sub_ok)
            self._event_bus.unsubscribe(sub_err)

        if "finished" in box:
            return True, None
        if "failed" in box:
            failed = box["failed"]
            return False, str(failed.get("error") or failed.get("message") or "action failed")
        return False, "timed out waiting for action"

    def _verify_and_finalize(self, execution: AutomationExecution) -> None:
        """Phase 6's own VERIFY step. Honest scope (documented, not a
        false promise): "verified" here means the dispatched tool call
        itself reported success (the same `tool_finished` vs
        `tool_failed` signal Sprint 69/70 already established as this
        project's own honest completion signal for PTZ - "success once
        ACCEPTED, never a confirmed physical state read-back"). This
        function does not invent a second, independent state re-check
        that no underlying API in this project actually supports."""
        results = execution.action_results
        if not results:
            execution.final_status = ExecutionStatus.SKIPPED.value
            self._publish(execution, "automation.skipped", reason="no_actions")
            return
        n_ok = sum(1 for r in results if r.status == "completed")
        if n_ok == len(results):
            execution.final_status = ExecutionStatus.COMPLETED.value
            self._publish(execution, "automation.completed")
        elif n_ok == 0:
            execution.final_status = ExecutionStatus.FAILED.value
            execution.reason = "all_actions_failed"
            self._publish(execution, "automation.failed", reason=execution.reason)
        else:
            execution.final_status = ExecutionStatus.PARTIAL_FAILURE.value
            execution.reason = "some_actions_failed"
            self._publish(execution, "automation.failed", reason=execution.reason)

    # -- cooldown cleanup (Phase 8) ---------------------------------------------

    def _cleanup_cooldowns(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [rid for rid, until in self._cooldown_until.items() if until <= now]
            for rid in expired:
                del self._cooldown_until[rid]

    # -- Event Bus publishing (Phase 10 - metadata-only) -------------------------

    def _publish(self, execution: AutomationExecution, event_type: str, **extra: Any) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(Event(type=event_type, data=_metadata_payload(execution, **extra)))
        except Exception as ex:  # pragma: no cover - defensive, must never kill the execution thread
            log(f"failed to publish {event_type} (ignored): {ex}", self.name)

    def _publish_skipped(self, rule: AutomationRule, trigger_info: Dict[str, Any], reason: str) -> None:
        execution = self._new_execution(rule, trigger_info, 0)
        execution.final_status = ExecutionStatus.SKIPPED.value
        execution.reason = reason
        self._publish(execution, "automation.skipped", reason=reason)

    def _publish_failed(self, rule: AutomationRule, execution: AutomationExecution, reason: str) -> None:
        execution.final_status = ExecutionStatus.FAILED.value
        execution.reason = reason
        self._publish(execution, "automation.failed", reason=reason)
        with self._lock:
            self._running_rule_ids.discard(rule.id)


def _rule_to_storage_dict(rule: AutomationRule) -> Dict[str, Any]:
    """The on-disk shape (compact trigger string, matching Phase 2's own
    worked examples) - deliberately NOT the same as `to_public_dict()`
    (which uses the expanded `{"type":..., "parameters":...}` trigger
    form for API/dashboard consumers)."""
    trigger = rule.trigger
    if trigger is None:
        trigger_str = None
    elif trigger.type == "event":
        trigger_str = f"event:{trigger.parameters.get('event_name', '')}"
    elif trigger.type == "time":
        trigger_str = f"time:{trigger.parameters.get('time', '')}"
    else:
        trigger_str = "manual"
    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "trigger": trigger_str,
        "conditions": [c.to_public_dict() for c in rule.conditions],
        "actions": [a.to_public_dict() for a in rule.actions],
        "cooldown_seconds": rule.cooldown_seconds,
        "execution_policy": rule.execution_policy,
        # P0.11 - additive; empty list for every pre-P0.11 rule (identical
        # on-disk shape to before this sprint for those rules, since
        # `[a.to_public_dict() for a in []] == []`).
        "sequence": [s.to_public_dict() for s in rule.sequence],
        # P0.12 - additive; `""`/`None`/`None` for every pre-P0.12 rule,
        # identical on-disk shape to before this sprint for those rules.
        "description": rule.description,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }
