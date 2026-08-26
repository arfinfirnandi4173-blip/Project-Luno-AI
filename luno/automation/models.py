"""
models.py
=========

Sprint 72 (Automation Engine Dasar). Typed, allowlisted domain model:

    AutomationRule
     +- id / name / enabled
     +- trigger:    AutomationTrigger   (type + parameters)
     +- conditions: List[AutomationCondition]
     +- actions:    List[AutomationAction]
     +- cooldown_seconds
     +- execution_policy (reserved - see class docstring)

Every trigger/condition/action TYPE comes from a fixed, closed allowlist
(`TRIGGER_TYPES`/`CONDITION_TYPES`/`ACTION_TYPES` below). There is no
"expression" field anywhere in this schema, and nothing in this package
ever invokes a Python `eval`/`exec` builtin, a shell-interpreter
subprocess mode, or dynamic `importlib` on a rule-supplied string -
Phase 12's own hard security boundary. `tests/test_sprint72_automation_engine.py` has a dedicated
source-scan test proving this statically, the same convention Sprint 71
used for its own scope-guard test.

`AutomationExecution` is the per-run record the Phase 6 pipeline
produces - one instance per triggered attempt, kept in a small bounded
in-memory history by `engine.py` (never persisted to disk - see that
module's own Phase 11 docstring section).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AutomationRuleError(ValueError):
    """Raised by `validate_rule()`/`validate_trigger()`/
    `validate_condition()`/`validate_action()` for anything that fails
    validation - same role `PatrolRouteError` plays for
    `luno/camera_patrol/route.py` (Sprint 71's own precedent)."""


# -- allowlists (Phase 1/12 - no arbitrary/expression-based types) ----------

TRIGGER_TYPES = frozenset({"event", "time", "manual"})
CONDITION_TYPES = frozenset({
    "equals", "not_equals", "greater_than", "less_than", "contains", "state_is",
    # P0.7 (Vision Context -> Automation Context) - the ONE new operator
    # this sprint adds, so a rule can express `event.person_count >= 2`
    # (Section 8's own worked example). `greater_than`/`less_than` are
    # both strict, and there is no "not"/combinator mechanism to express
    # "not less_than" instead - this is the smallest safe addition that
    # covers the brief's own example without a second condition engine
    # or a generic expression language. No "less_equal" was added -
    # nothing in this sprint's brief or tests needs it (Section 8:
    # "implement only the smallest extension necessary").
    "greater_equal",
})
#: P0.15 (Human-Friendly Dashboard UX & Time-Based Automation Conditions) -
#: a condition `type` for a bounded local-time-of-day window
#: (`{"type": "time", "parameters": {"after": "18:00", "before": "23:30"}}`).
#: Deliberately kept OUT of `CONDITION_TYPES` above (a set of pure
#: comparison OPERATORS applied against a `target`'s read value - see
#: `wait_until`'s own operator dropdown and the generic condition row,
#: both of which are schema-driven from `CONDITION_TYPES` and would be
#: semantically wrong to also offer "time" as a comparison operator).
#: `"time"` is a self-contained condition KIND with its own two-field
#: `parameters` shape (no `target`, no `value` - "now" needs no external
#: reader) - `validate_condition()`/`evaluate_condition()` special-case
#: it explicitly rather than folding it into the operator set, so
#: `CONDITION_TYPES`'s own exact frozenset contents (asserted verbatim by
#: `tests/test_sprint72_automation_engine.py`) are completely unchanged.
TIME_CONDITION_TYPE = "time"
ACTION_TYPES = frozenset({
    "camera.preset", "camera.home", "camera.stop_patrol",
    "home_assistant.turn_on", "home_assistant.turn_off",
    "automation.log",
    # P0.14 (Advanced Home Assistant Automation Actions & Script Runner) -
    # additive only. Every one of these maps directly onto an action the
    # ToolManager's "home_assistant" handler (mock AND real) ALREADY
    # supports (`_SUPPORTED_ACTIONS` in `luno/tool_manager/builtin/
    # home_assistant.py` already lists "toggle"/"set_temperature"/
    # "set_color"/"set_brightness"/"run_script" - only "call_service" and
    # "activate_scene" needed a small, additive ToolManager extension, see
    # that module's own P0.14 docstring section) - there is still exactly
    # ONE execution path (AutomationEngine -> _dispatch_action() ->
    # ToolManager), never a second HA client or a second dispatch
    # mechanism for these new types.
    "home_assistant.toggle", "home_assistant.set_brightness",
    "home_assistant.set_color", "home_assistant.set_temperature",
    "home_assistant.run_script", "home_assistant.activate_scene",
    "home_assistant.call_service",
})
#: Action types that dispatch a REAL `camera_ptz`/`camera_patrol` tool
#: call (Phase 5 ownership rules only apply to these).
_CAMERA_ACTION_TYPES = frozenset({"camera.preset", "camera.home", "camera.stop_patrol"})
#: Action types that are purely internal - never publish a
#: `tool_requested` at all (Phase 4's own "automation.log ... hanya
#: mencatat execution metadata").
_INTERNAL_ACTION_TYPES = frozenset({"automation.log"})

MAX_CONDITIONS_PER_RULE = 20
#: Phase 9's own "per-execution action limit" - enforced here, at
#: validation time, so a rule can never even be LOADED with more
#: actions than one execution is allowed to run.
MAX_ACTIONS_PER_RULE = 20
MAX_COOLDOWN_SECONDS = 86400.0
MIN_COOLDOWN_SECONDS = 0.0

#: P0.12 (Automation API & CRUD) - a human-readable, optional, free-text
#: field the API's Create/Update surface persists (see `AutomationRule.
#: description`'s own docstring below). Bounded for the same "no
#: unreasonable/effectively-unbounded field" reason every other MAX_*
#: constant in this module exists.
MAX_DESCRIPTION_LENGTH = 500

#: P0.8.9 (WLED OFF debounce) - optional `delay_seconds` parameter on a
#: `home_assistant.turn_on`/`turn_off` action (see `engine.py::
#: _dispatch_home_assistant_action()`'s own docstring for how this is
#: actually executed - via the project's EXISTING `Scheduler.schedule_
#: once()`/`cancel()` primitives, never a new timer). Same reasoning as
#: `MAX_COOLDOWN_SECONDS`: generous enough for a real debounce use case,
#: bounded so a misconfigured rule can never "delay" an action for an
#: unreasonable/effectively-indefinite span.
MAX_DELAY_SECONDS = 300.0
MIN_DELAY_SECONDS = 0.0

#: P0.11 (Action Sequence Engine) - a `sequence` step whose `type` is
#: this sentinel pauses the CALLING execution (never the whole engine -
#: see `engine.py::_wait_delay()`'s own docstring) before the next step
#: runs. Deliberately a SEPARATE mechanism from the `delay_seconds`
#: parameter above (P0.8.9's own single-action debounce/supersede
#: mechanism, which defers dispatch of ONE action asynchronously via the
#: scheduler and does not pause a caller) - a sequence step's `delay` is
#: a simple, easy-to-reason-about blocking pause between two fully
#: synchronous steps, reusing the SAME `MAX_DELAY_SECONDS`/`MIN_DELAY_
#: SECONDS` bounds already established for the exact same reason (no
#: second, differently-bounded delay concept).
_SEQUENCE_DELAY_STEP_TYPE = "delay"
#: P0.14 - three new pseudo-types, same "sequence-only control step,
#: never a device action, never valid inside flat `actions`" treatment
#: `"delay"` already established above. `"wait_until"` polls an existing
#: state-reading mechanism (`AutomationEngine.ha_state_reader`, already
#: wired for the Camera Action Safety Gate - reused verbatim, no second
#: HA read path) with a bounded timeout; `"condition"` is a constrained,
#: declarative if/then/else branch (reuses `evaluate_condition()`
#: verbatim for its own `conditions` list - no new condition engine);
#: `"stop_automation"` is an explicit, intentional early exit (distinct
#: outcome - `ExecutionStatus.CANCELLED` - from a failing step).
_SEQUENCE_WAIT_UNTIL_STEP_TYPE = "wait_until"
_SEQUENCE_CONDITION_STEP_TYPE = "condition"
_SEQUENCE_STOP_STEP_TYPE = "stop_automation"
_SEQUENCE_CONTROL_STEP_TYPES = frozenset({
    _SEQUENCE_WAIT_UNTIL_STEP_TYPE, _SEQUENCE_CONDITION_STEP_TYPE, _SEQUENCE_STOP_STEP_TYPE,
})
#: P0.11/P0.14 - every valid `sequence` step `type` - the existing device/
#: internal `ACTION_TYPES` allowlist (reused verbatim, never duplicated)
#: PLUS `"delay"` PLUS the three new P0.14 control pseudo-types above.
#: None of the four pseudo-types are added to `ACTION_TYPES` itself, so
#: the pre-existing `actions` list (and `validate_action()`) continue to
#: reject all of them exactly as before - only a `sequence` list
#: understands them.
SEQUENCE_STEP_TYPES = ACTION_TYPES | {_SEQUENCE_DELAY_STEP_TYPE} | _SEQUENCE_CONTROL_STEP_TYPES
#: P0.11 - same reasoning/bound as `MAX_ACTIONS_PER_RULE` (Phase 9's own
#: "per-execution action limit," reused verbatim for the new list rather
#: than inventing a second limit).
MAX_SEQUENCE_STEPS = MAX_ACTIONS_PER_RULE

#: P0.14 - `wait_until`'s own timeout bound (Section 15 of the brief:
#: "Reject negative values, non-numeric values, unreasonably large
#: values" - same bounded-not-unbounded convention as every other MAX_*
#: constant in this module). Default matches the brief's own worked
#: example ("Recommended default: 10 seconds").
MIN_WAIT_UNTIL_TIMEOUT_SECONDS = 1.0
MAX_WAIT_UNTIL_TIMEOUT_SECONDS = 300.0
DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS = 10.0

#: P0.14 - `condition` step nesting bound (Section 16: "recursive
#: condition structures ... unsupported nesting" must be REJECTED, not
#: silently allowed to recurse without limit). A `condition` step's own
#: `then`/`else` branches may themselves contain `condition` steps up to
#: this many levels deep (1 = a condition step whose then/else branches
#: may not themselves contain another condition step) before
#: `validate_sequence_step()` refuses to load the rule at all.
MAX_CONDITION_NESTING_DEPTH = 3

#: P0.14 - `home_assistant.call_service`'s own domain/service allowlist
#: SHAPE check (Section 4/17: a controlled, declarative HA service call -
#: never an arbitrary string passed straight into a shell or `eval`).
#: This does not (and cannot, without hard-coding the entirety of every
#: HA integration's service catalog) allowlist specific domain/service
#: NAMES - `ToolManager`'s "home_assistant" handler is still the single
#: place that actually dispatches to Home Assistant, and Home Assistant
#: itself is the final authority on whether a given domain.service pair
#: really exists. What this regex DOES guarantee: `domain`/`service` can
#: only ever be a lowercase snake_case identifier - never a shell
#: fragment, a dotted Python path, or anything `eval`/`exec`/
#: `subprocess`-shaped.
_HA_DOMAIN_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: P0.14 - `call_service`'s optional `data`/`variables` dict is bounded
#: (same "no unreasonable/effectively-unbounded field" reasoning as
#: `MAX_DESCRIPTION_LENGTH` etc.) rather than accepting an arbitrarily
#: large payload.
MAX_CALL_SERVICE_DATA_KEYS = 20

_TIME_TRIGGER_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_EVENT_TRIGGER_PREFIX = "event:"
_TIME_TRIGGER_PREFIX = "time:"


class ExecutionStatus(str, Enum):
    """Phase 6's own minimal status set, plus `PARTIAL_FAILURE` (Phase 7:
    "action #1 sukses tetapi action #2 gagal ... Result: PARTIAL_FAILURE
    atau status equivalent")."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    REFUSED = "REFUSED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    #: P0.14 - two new, additive terminal statuses for `sequence`-based
    #: executions only (legacy `actions`-based rules can never produce
    #: either - neither a `stop_automation` step nor a `wait_until` step
    #: exists outside `sequence`). `CANCELLED` - an explicit, intentional
    #: `stop_automation` step was reached (never a failure - Section 3's
    #: own "Stop Automation" CONTROL action is a deliberate early exit).
    #: `TIMEOUT` - a `wait_until` step's condition never became true
    #: within its own bounded `timeout_seconds` window (Section 13/15 -
    #: distinct from `FAILED` so a dashboard/log reader can tell "the
    #: automation tried and a step genuinely errored" apart from "a step
    #: was still honestly waiting when its own budget ran out").
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class AutomationTrigger:
    type: str  # "event" | "time" | "manual"
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class AutomationCondition:
    type: str
    target: str = ""
    value: Any = None
    #: P0.15 - additive, defaults to `{}` for every condition that never
    #: sets it (i.e. every condition that existed before this sprint) -
    #: byte-for-byte the same `AutomationCondition(type=..., target=...,
    #: value=...)` shape those callers already use, unchanged. Only the
    #: new `"time"` condition type (see `TIME_CONDITION_TYPE`) actually
    #: reads this - `{"after": "HH:MM", "before": "HH:MM"}` - mirroring
    #: `AutomationAction.parameters`'s own established `{type,
    #: parameters}` shape rather than inventing a third representation.
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "target": self.target, "value": self.value, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class AutomationAction:
    type: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class AutomationRule:
    id: str
    name: str
    enabled: bool = True
    trigger: Optional[AutomationTrigger] = None
    conditions: List[AutomationCondition] = field(default_factory=list)
    actions: List[AutomationAction] = field(default_factory=list)
    cooldown_seconds: float = 0.0
    #: Reserved for a future sprint (e.g. "strict" vs "best_effort").
    #: Sprint 72 always applies the Phase 7 "no partial execution"
    #: policy regardless of this field's value - it is carried through
    #: to `to_public_dict()`/persistence purely so the schema already has
    #: the slot a later sprint would need, without inventing behavior
    #: this sprint was never asked to build.
    execution_policy: str = "no_partial"
    #: P0.11 (Action Sequence Engine) - an ADDITIVE, mutually exclusive
    #: alternative to `actions` above. Reuses `AutomationAction`'s own
    #: `{type, parameters}` shape verbatim (never a parallel step class) -
    #: a step's `type` is either an existing `ACTION_TYPES` member
    #: (dispatched through the EXACT SAME `AutomationEngine._dispatch_
    #: action()` path `actions` already uses) or the new `"delay"`
    #: pseudo-type (see `_SEQUENCE_DELAY_STEP_TYPE`'s own comment).
    #: `validate_rule()` requires exactly one of `actions`/`sequence` to
    #: be non-empty - never both, never neither. A rule that never sets
    #: this (i.e. every rule that existed before P0.11) has `sequence ==
    #: []`, byte-for-byte the same default `list.__eq__` result as before
    #: this field existed - zero behavioral change for any existing rule.
    sequence: List[AutomationAction] = field(default_factory=list)
    #: P0.12 (Automation API & CRUD) - additive, optional metadata the
    #: new `/api/automations` CRUD surface persists (see `docs/change_
    #: impact/automation_api_p0_12.md`'s own "why these three fields"
    #: reasoning). A rule loaded from a JSON file that predates this
    #: sprint (i.e. every rule that existed before P0.12) simply has no
    #: `"description"`/`"created_at"`/`"updated_at"` key on disk -
    #: `rule_from_dict()` defaults them to `""`/`None`/`None`
    #: respectively, byte-for-byte the same as before this field
    #: existed for those rules. `created_at`/`updated_at` are NEVER
    #: taken from a caller-supplied request body by the API layer (see
    #: `engine.py::create_rule()`/`update_rule()`) - only the engine
    #: itself sets them, at the moment of a genuine create/update, so
    #: they are always trustworthy server-side timestamps, never
    #: client-fabricated ones.
    description: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "trigger": self.trigger.to_public_dict() if self.trigger else None,
            "conditions": [c.to_public_dict() for c in self.conditions],
            "actions": [a.to_public_dict() for a in self.actions],
            "cooldown_seconds": self.cooldown_seconds,
            "execution_policy": self.execution_policy,
            "sequence": [s.to_public_dict() for s in self.sequence],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ActionResult:
    type: str
    status: str  # "completed" | "failed" | "refused"
    message: str = ""
    code: Optional[str] = None

    def to_public_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "status": self.status, "message": self.message, "code": self.code}


@dataclass
class AutomationExecution:
    """One record per triggered attempt (Phase 6). Metadata-only by
    construction - `trigger`/`reason`/action messages are always plain
    str/int/float/bool/None (never a raw exception object, a credential,
    or a frame - Phase 10's own payload rule, applied here too since
    this record is what `_publish()`/dashboard/tests all read from)."""
    execution_id: str
    rule_id: str
    correlation_id: str
    depth: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    trigger: Optional[Dict[str, Any]] = None
    condition_result: Optional[bool] = None
    action_results: List[ActionResult] = field(default_factory=list)
    final_status: str = ExecutionStatus.PENDING.value
    reason: Optional[str] = None
    #: P0.11 (Action Sequence Engine) - additive, `None` for every
    #: execution of an `actions`-based rule (unchanged pre-P0.11
    #: behavior). Only a `sequence`-based execution ever sets these -
    #: `current_step_index` is the 0-based index of the step currently
    #: running (or most recently run), `total_steps` is `len(rule.
    #: sequence)`. Both are updated on the SAME mutable `execution`
    #: object the engine keeps a live reference to in `_last_execution`
    #: (see `engine.py::_run_sequence()`), so `get_status()`/
    #: `get_automation_status()` can observe live progress through a
    #: multi-step, multi-second sequence - not just its final outcome.
    current_step_index: Optional[int] = None
    total_steps: Optional[int] = None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "rule_id": self.rule_id,
            "correlation_id": self.correlation_id,
            "depth": self.depth,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "trigger": self.trigger,
            "condition_result": self.condition_result,
            "action_results": [a.to_public_dict() for a in self.action_results],
            "final_status": self.final_status,
            "reason": self.reason,
            "current_step_index": self.current_step_index,
            "total_steps": self.total_steps,
        }


# -- validation --------------------------------------------------------------

def validate_trigger(trigger: Optional[AutomationTrigger]) -> None:
    if trigger is None:
        raise AutomationRuleError("rule has no trigger")
    if trigger.type not in TRIGGER_TYPES:
        raise AutomationRuleError(f"unknown trigger type {trigger.type!r} (allowed: {sorted(TRIGGER_TYPES)})")
    if trigger.type == "event":
        name = str(trigger.parameters.get("event_name", "")).strip()
        if not name:
            raise AutomationRuleError("event trigger requires a non-empty 'event_name' parameter")
    elif trigger.type == "time":
        hhmm = str(trigger.parameters.get("time", "")).strip()
        if not _TIME_TRIGGER_RE.match(hhmm):
            raise AutomationRuleError(f"time trigger requires 'time' formatted HH:MM, got {hhmm!r}")


def validate_condition(condition: AutomationCondition) -> None:
    # P0.15 - `"time"` is a self-contained condition KIND (see
    # `TIME_CONDITION_TYPE`'s own comment for why it is deliberately NOT
    # a `CONDITION_TYPES` member) - validated on its own `parameters.
    # after`/`parameters.before`, never on `target` (a time-of-day window
    # needs no external reader - "now" is not read through `target`/
    # `state_readers` at all, see `conditions.py::evaluate_condition()`).
    if condition.type == TIME_CONDITION_TYPE:
        _validate_time_condition(condition)
        return
    if condition.type not in CONDITION_TYPES:
        raise AutomationRuleError(f"unknown condition type {condition.type!r} (allowed: {sorted(CONDITION_TYPES)})")
    if not condition.target or not str(condition.target).strip():
        raise AutomationRuleError("condition requires a non-empty 'target'")


def _validate_time_condition(condition: AutomationCondition) -> None:
    """P0.15 Section 2 - reuses the EXACT SAME `_TIME_TRIGGER_RE` (HH:MM,
    24h, no seconds) the pre-existing `time` TRIGGER type already
    validates with above - no second time-format parser was invented.
    Both `after` and `before` are required and validated independently,
    so a rule author gets a specific, actionable error for whichever one
    is missing/malformed rather than one generic message."""
    params = condition.parameters
    for key in ("after", "before"):
        raw = params.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise AutomationRuleError(f"time condition requires a non-empty {key!r} (HH:MM, 24h)")
        if not isinstance(raw, str) or not _TIME_TRIGGER_RE.match(raw.strip()):
            raise AutomationRuleError(f"time condition's {key!r} must be formatted HH:MM (24h), got {raw!r}")


def validate_action(action: AutomationAction) -> None:
    if action.type not in ACTION_TYPES:
        raise AutomationRuleError(f"unknown action type {action.type!r} (allowed: {sorted(ACTION_TYPES)})")
    if action.type == "camera.preset" and not str(action.parameters.get("preset", "")).strip():
        raise AutomationRuleError("camera.preset action requires a non-empty 'preset' parameter")
    # P0.8.9 - `delay_seconds` is only meaningful (and only implemented)
    # for the two Home Assistant action types below - refused up front
    # for every other action type rather than silently ignored, so a
    # rule author gets an explicit load-time error instead of a
    # quietly-no-op parameter.
    _delay_seconds_action_types = ("home_assistant.turn_on", "home_assistant.turn_off", "home_assistant.toggle")
    if "delay_seconds" in action.parameters and action.type not in _delay_seconds_action_types:
        raise AutomationRuleError(
            f"'delay_seconds' is only supported on home_assistant.turn_on/turn_off/toggle actions, not {action.type!r}"
        )
    if action.type in ("home_assistant.turn_on", "home_assistant.turn_off", "home_assistant.toggle"):
        # P0.6.2 - the target must be a single, explicit entity id
        # string. Previously this only checked "truthy after str()",
        # which a list/dict/None would all pass (e.g. str([]) == "[]",
        # non-empty). Tightened to reject exactly what P0.6.2's own
        # brief requires be impossible for a real device-affecting
        # action: a non-string target (no "entity_id: []"/dynamic
        # expansion), a missing/None target (no "entity_id: null"), and
        # the literal wildcard string "*" - while remaining
        # byte-for-byte backward compatible for every rule that already
        # passes a normal, single, non-empty entity-id string (the only
        # shape any existing rule, including P0.6's own log-only rule,
        # ever used - that rule has no HA action at all, so this change
        # affects zero currently-loaded rules).
        target = action.parameters.get("target")
        if not isinstance(target, str) or not target.strip():
            raise AutomationRuleError(f"{action.type} action requires a non-empty string 'target' (a single entity id)")
        if target.strip() == "*":
            raise AutomationRuleError(f"{action.type} action target must not be a wildcard ('*')")
        # P0.8.9 - optional debounce delay (see MAX_DELAY_SECONDS comment
        # above). Absent entirely is the default, unchanged, immediate-
        # dispatch behavior every existing rule already relies on.
        if "delay_seconds" in action.parameters:
            delay = action.parameters.get("delay_seconds")
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise AutomationRuleError(f"{action.type} action's 'delay_seconds' must be a number, got {delay!r}")
            if not (MIN_DELAY_SECONDS <= float(delay) <= MAX_DELAY_SECONDS):
                raise AutomationRuleError(
                    f"{action.type} action's 'delay_seconds' out of range: {delay} "
                    f"(must be {MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS})"
                )
    # -- P0.14 (Advanced Home Assistant Automation Actions & Script
    # Runner) - every new action type below dispatches through the EXACT
    # SAME `_dispatch_home_assistant_action()` -> ToolManager path the
    # `turn_on`/`turn_off`/`toggle` block above already uses (see
    # `engine.py`'s own P0.14 section) - these are additive validation
    # rules only, never a second dispatch mechanism.
    if action.type == "home_assistant.set_brightness":
        _require_string_target(action, "target")
        _require_percent(action, "level")
    elif action.type == "home_assistant.set_color":
        _require_string_target(action, "target")
        has_color = isinstance(action.parameters.get("color"), str) and action.parameters.get("color", "").strip()
        rgb = action.parameters.get("rgb")
        has_rgb = isinstance(rgb, (list, tuple)) and len(rgb) == 3 and all(
            isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 255 for c in rgb
        )
        if not has_color and not has_rgb:
            raise AutomationRuleError(
                f"{action.type} action requires either a non-empty string 'color' or an ['r','g','b'] 0-255 'rgb' list"
            )
    elif action.type == "home_assistant.set_temperature":
        _require_string_target(action, "target")
        value = action.parameters.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AutomationRuleError(f"{action.type} action requires a numeric 'value'")
    elif action.type == "home_assistant.run_script":
        _require_entity_id(action, "entity_id")
        _validate_optional_data_dict(action, "variables")
    elif action.type == "home_assistant.activate_scene":
        _require_entity_id(action, "entity_id")
    elif action.type == "home_assistant.call_service":
        domain = action.parameters.get("domain")
        service = action.parameters.get("service")
        if not isinstance(domain, str) or not _HA_DOMAIN_SERVICE_RE.match(domain):
            raise AutomationRuleError(
                f"{action.type} action requires a lowercase snake_case 'domain' (e.g. 'light'), got {domain!r}"
            )
        if not isinstance(service, str) or not _HA_DOMAIN_SERVICE_RE.match(service):
            raise AutomationRuleError(
                f"{action.type} action requires a lowercase snake_case 'service' (e.g. 'turn_on'), got {service!r}"
            )
        target = action.parameters.get("target")
        entity_ids = _extract_call_service_entity_ids(target)
        if not entity_ids:
            raise AutomationRuleError(
                f"{action.type} action requires 'target': {{\"entity_id\": \"...\"}} "
                "or {\"entity_id\": [\"...\", ...]} with at least one non-empty entity id"
            )
        _validate_optional_data_dict(action, "data")


def _require_string_target(action: AutomationAction, key: str) -> None:
    target = action.parameters.get(key)
    if not isinstance(target, str) or not target.strip():
        raise AutomationRuleError(f"{action.type} action requires a non-empty string {key!r}")
    if target.strip() == "*":
        raise AutomationRuleError(f"{action.type} action {key!r} must not be a wildcard ('*')")


def _require_entity_id(action: AutomationAction, key: str) -> None:
    """P0.14 - same non-empty-string/no-wildcard shape as `_require_
    string_target()`, named separately because `run_script`/
    `activate_scene` use `entity_id` (matching the brief's own worked
    JSON examples) rather than `target` (the pre-existing turn_on/
    turn_off/toggle/set_* key) - two names for the identical
    "one explicit entity id string" requirement, not two behaviors."""
    _require_string_target(action, key)


def _require_percent(action: AutomationAction, key: str) -> None:
    level = action.parameters.get(key)
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        raise AutomationRuleError(f"{action.type} action requires a numeric {key!r} (0-100)")
    if not (0 <= level <= 100):
        raise AutomationRuleError(f"{action.type} action's {key!r} out of range: {level} (must be 0-100)")


def _validate_optional_data_dict(action: AutomationAction, key: str) -> None:
    if key not in action.parameters:
        return
    data = action.parameters.get(key)
    if not isinstance(data, dict):
        raise AutomationRuleError(f"{action.type} action's {key!r} must be an object, got {type(data).__name__}")
    if len(data) > MAX_CALL_SERVICE_DATA_KEYS:
        raise AutomationRuleError(
            f"{action.type} action's {key!r} has too many keys ({len(data)} > {MAX_CALL_SERVICE_DATA_KEYS})"
        )


def _extract_call_service_entity_ids(target: Any) -> List[str]:
    """P0.14 - accepts the brief's own worked `target: {"entity_id": [...]}`
    shape, plus the single-string convenience `target: {"entity_id":
    "light.wled"}` - never a bare string/list at the top level (Section
    4's own example is always an object), so a caller can't accidentally
    pass a raw domain/service string here instead of a target."""
    if not isinstance(target, dict):
        return []
    raw = target.get("entity_id")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [e.strip() for e in raw if isinstance(e, str) and e.strip()]


def validate_sequence_step(step: AutomationAction, index: int, rule_id: str, depth: int = 0) -> None:
    """P0.11 - the `sequence` analog of `validate_action()` above, reused
    directly for every non-`"delay"` step (Section 4.1's own "must match
    the project's existing ToolManager/action interface" requirement -
    there is no second, parallel validation path for a device-action
    step, whether it lives in `actions` or `sequence`). Every error
    message identifies the offending automation id AND step index
    (Section 12), matching the worked example in the brief.

    `depth` (P0.14, additive, default 0 - every pre-existing caller is
    unaffected) - how many `condition` steps this step is nested inside
    (0 for every top-level `rule.sequence` entry). Only ever incremented
    by `_validate_condition_step()`'s own recursive call into its `then`/
    `else` branches - see `MAX_CONDITION_NESTING_DEPTH`'s own docstring
    for why this exists (Section 16: reject unbounded/"recursive
    condition structures", never silently allow them)."""
    prefix = f"automation {rule_id!r}: sequence step {index}"
    if step.type == _SEQUENCE_DELAY_STEP_TYPE:
        seconds = step.parameters.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise AutomationRuleError(f"{prefix}: delay 'seconds' must be a number, got {seconds!r}")
        seconds_f = float(seconds)
        if not math.isfinite(seconds_f):
            raise AutomationRuleError(f"{prefix}: invalid delay seconds={seconds!r} (NaN/Infinity not allowed)")
        if seconds_f < 0:
            raise AutomationRuleError(f"{prefix}: invalid delay seconds={seconds!r} (negative delay not allowed)")
        if not (MIN_DELAY_SECONDS <= seconds_f <= MAX_DELAY_SECONDS):
            raise AutomationRuleError(
                f"{prefix}: invalid delay seconds={seconds!r} (must be {MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS})"
            )
        extra_keys = set(step.parameters) - {"seconds"}
        if extra_keys:
            raise AutomationRuleError(f"{prefix}: delay step has unexpected parameter(s) {sorted(extra_keys)!r}")
        return
    if step.type == _SEQUENCE_STOP_STEP_TYPE:
        # P0.14 (Section 3, CONTROL: "Stop Automation") - an explicit,
        # intentional early exit. No parameters at all (nothing to
        # configure) - any parameter present is refused up front rather
        # than silently ignored, same discipline the delay step's own
        # `extra_keys` check already established above.
        if step.parameters:
            raise AutomationRuleError(
                f"{prefix}: stop_automation step accepts no parameters, got {sorted(step.parameters)!r}"
            )
        return
    if step.type == _SEQUENCE_WAIT_UNTIL_STEP_TYPE:
        _validate_wait_until_step(step, prefix)
        return
    if step.type == _SEQUENCE_CONDITION_STEP_TYPE:
        _validate_condition_step(step, index, rule_id, depth)
        return
    if step.type not in SEQUENCE_STEP_TYPES:
        raise AutomationRuleError(
            f"{prefix}: unknown step type {step.type!r} (allowed: {sorted(SEQUENCE_STEP_TYPES)})"
        )
    # P0.11 - `delay_seconds` (P0.8.9's own single-action async-defer
    # mechanism) is deliberately not supported inside a sequence step -
    # see `_SEQUENCE_DELAY_STEP_TYPE`'s own comment for why these are two
    # separate mechanisms. A rule author who wants a pause between two
    # sequence steps must use an explicit `{"type": "delay", ...}` step
    # instead, which keeps the sequential-completion guarantee (Section
    # 6) simple and unambiguous rather than silently racing a deferred,
    # not-yet-verified action against the next step starting.
    if "delay_seconds" in step.parameters:
        raise AutomationRuleError(
            f"{prefix}: 'delay_seconds' is not supported on a sequence step - "
            f"use a separate {{'type': 'delay', 'seconds': N}} step instead"
        )
    try:
        validate_action(step)
    except AutomationRuleError as ex:
        raise AutomationRuleError(f"{prefix}: {ex}") from ex


def _validate_wait_until_step(step: AutomationAction, prefix: str) -> None:
    """P0.14 Section 8 - a controlled, bounded wait, reusing the SAME
    `CONDITION_TYPES` operator allowlist `AutomationCondition` already
    uses (no second comparison-operator vocabulary)."""
    p = step.parameters
    target = p.get("target")
    if not isinstance(target, str) or not target.strip():
        raise AutomationRuleError(f"{prefix}: wait_until requires a non-empty string 'target'")
    attribute = p.get("attribute", "state")
    if not isinstance(attribute, str) or not attribute.strip():
        raise AutomationRuleError(f"{prefix}: wait_until 'attribute' must be a non-empty string")
    operator = p.get("operator")
    if operator not in CONDITION_TYPES:
        raise AutomationRuleError(
            f"{prefix}: wait_until 'operator' must be one of {sorted(CONDITION_TYPES)}, got {operator!r}"
        )
    if "value" not in p:
        raise AutomationRuleError(f"{prefix}: wait_until requires a 'value' to compare against")
    if "timeout_seconds" in p:
        timeout = p.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise AutomationRuleError(f"{prefix}: wait_until 'timeout_seconds' must be a number, got {timeout!r}")
        timeout_f = float(timeout)
        if not math.isfinite(timeout_f):
            raise AutomationRuleError(f"{prefix}: invalid wait_until timeout_seconds={timeout!r} (NaN/Infinity not allowed)")
        if not (MIN_WAIT_UNTIL_TIMEOUT_SECONDS <= timeout_f <= MAX_WAIT_UNTIL_TIMEOUT_SECONDS):
            raise AutomationRuleError(
                f"{prefix}: wait_until 'timeout_seconds' out of range: {timeout} "
                f"(must be {MIN_WAIT_UNTIL_TIMEOUT_SECONDS}-{MAX_WAIT_UNTIL_TIMEOUT_SECONDS})"
            )


def _validate_condition_step(step: AutomationAction, index: int, rule_id: str, depth: int) -> None:
    """P0.14 Section 9 - a constrained, declarative if/then/else branch.
    Reuses `evaluate_condition()`/`validate_condition()` verbatim for its
    own `conditions` list (AND semantics, identical to a rule's top-level
    `conditions` - no second condition engine). `then`/`else` sub-steps
    are validated by recursing into `validate_sequence_step()` itself
    (so a `home_assistant.*` action, a `delay`, or another `condition`
    step all work identically inside a branch as they do at the top
    level) - bounded by `MAX_CONDITION_NESTING_DEPTH` so this recursion
    can never be unbounded (Section 16: "recursive condition structures
    ... unsupported nesting" must be rejected)."""
    prefix = f"automation {rule_id!r}: sequence step {index}"
    if depth >= MAX_CONDITION_NESTING_DEPTH:
        raise AutomationRuleError(
            f"{prefix}: condition step nesting too deep (max {MAX_CONDITION_NESTING_DEPTH} levels)"
        )
    p = step.parameters
    raw_conditions = p.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise AutomationRuleError(f"{prefix}: condition step requires a non-empty 'conditions' list")
    if len(raw_conditions) > MAX_CONDITIONS_PER_RULE:
        raise AutomationRuleError(
            f"{prefix}: condition step has too many conditions ({len(raw_conditions)} > {MAX_CONDITIONS_PER_RULE})"
        )
    for c in raw_conditions:
        if not isinstance(c, dict):
            raise AutomationRuleError(f"{prefix}: condition step's 'conditions' entries must be objects")
        cond = AutomationCondition(type=str(c.get("type", "")), target=str(c.get("target", "")), value=c.get("value"))
        try:
            validate_condition(cond)
        except AutomationRuleError as ex:
            raise AutomationRuleError(f"{prefix}: {ex}") from ex

    then_raw = p.get("then", [])
    else_raw = p.get("else", [])
    if not isinstance(then_raw, list) or not isinstance(else_raw, list):
        raise AutomationRuleError(f"{prefix}: condition step's 'then'/'else' must be lists of steps")
    if not then_raw and not else_raw:
        raise AutomationRuleError(f"{prefix}: condition step requires at least one step in 'then' or 'else'")
    for branch_name, branch in (("then", then_raw), ("else", else_raw)):
        if len(branch) > MAX_SEQUENCE_STEPS:
            raise AutomationRuleError(
                f"{prefix}: condition step's {branch_name!r} branch too long ({len(branch)} > {MAX_SEQUENCE_STEPS})"
            )
        for sub_index, raw_sub in enumerate(branch):
            if not isinstance(raw_sub, dict):
                raise AutomationRuleError(f"{prefix}: condition step's {branch_name!r}[{sub_index}] must be an object")
            sub_step = _sequence_step_from_raw(raw_sub)
            try:
                validate_sequence_step(sub_step, index, rule_id, depth=depth + 1)
            except AutomationRuleError as ex:
                raise AutomationRuleError(f"{prefix}: {branch_name}[{sub_index}]: {ex}") from ex


def validate_rule(rule: AutomationRule) -> None:
    if not rule.id or not str(rule.id).strip():
        raise AutomationRuleError("rule id must not be empty")
    if not rule.name or not str(rule.name).strip():
        raise AutomationRuleError("rule name must not be empty")
    validate_trigger(rule.trigger)
    if len(rule.conditions) > MAX_CONDITIONS_PER_RULE:
        raise AutomationRuleError(f"too many conditions ({len(rule.conditions)} > {MAX_CONDITIONS_PER_RULE})")
    for c in rule.conditions:
        validate_condition(c)

    # P0.11 - a rule now has exactly one of `actions` (Sprint 72's
    # original, unmodified list-of-independent-actions form) or
    # `sequence` (the new, additive, ordered-with-delays form). Requiring
    # exactly one (never both, never neither) keeps "which list actually
    # runs" unambiguous - there is no existing rule anywhere that sets
    # `sequence` (the field did not exist before this sprint), so this
    # stricter check changes behavior for zero currently-loaded rules.
    has_actions = bool(rule.actions)
    has_sequence = bool(rule.sequence)
    if has_actions and has_sequence:
        raise AutomationRuleError(
            f"automation {rule.id!r}: a rule must not define both 'actions' and 'sequence' - use exactly one"
        )
    if not has_actions and not has_sequence:
        raise AutomationRuleError("rule must have at least one action (either 'actions' or 'sequence')")

    if has_actions:
        if len(rule.actions) > MAX_ACTIONS_PER_RULE:
            raise AutomationRuleError(f"too many actions ({len(rule.actions)} > {MAX_ACTIONS_PER_RULE})")
        for a in rule.actions:
            validate_action(a)
    else:
        if len(rule.sequence) > MAX_SEQUENCE_STEPS:
            raise AutomationRuleError(
                f"automation {rule.id!r}: sequence too long ({len(rule.sequence)} > {MAX_SEQUENCE_STEPS})"
            )
        for index, step in enumerate(rule.sequence):
            validate_sequence_step(step, index, rule.id)

    if not (MIN_COOLDOWN_SECONDS <= rule.cooldown_seconds <= MAX_COOLDOWN_SECONDS):
        raise AutomationRuleError(f"cooldown_seconds out of range: {rule.cooldown_seconds}")

    # P0.12 - `description` is optional (default `""`) but, like every
    # other free-text-ish field this module already bounds, not
    # unbounded - a rule author (or a future dashboard editor) pasting
    # in an entire paragraph should get a clear validation error, not a
    # silently-truncated-somewhere-downstream surprise.
    if not isinstance(rule.description, str):
        raise AutomationRuleError(f"automation {rule.id!r}: 'description' must be a string, got {type(rule.description).__name__}")
    if len(rule.description) > MAX_DESCRIPTION_LENGTH:
        raise AutomationRuleError(
            f"automation {rule.id!r}: description too long ({len(rule.description)} > {MAX_DESCRIPTION_LENGTH})"
        )


# -- (de)serialization ---------------------------------------------------

def _trigger_from_raw(raw: Any) -> AutomationTrigger:
    """Accepts EITHER the compact string form (`"event:<name>"` /
    `"time:HH:MM"` / `"manual"` - Phase 2's own worked examples) OR the
    explicit object form (`{"type": ..., "parameters": {...}}`)."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith(_EVENT_TRIGGER_PREFIX):
            return AutomationTrigger(type="event", parameters={"event_name": text[len(_EVENT_TRIGGER_PREFIX):].strip()})
        if text.startswith(_TIME_TRIGGER_PREFIX):
            return AutomationTrigger(type="time", parameters={"time": text[len(_TIME_TRIGGER_PREFIX):].strip()})
        if text == "manual":
            return AutomationTrigger(type="manual", parameters={})
        raise AutomationRuleError(f"unrecognized compact trigger string: {raw!r}")
    if isinstance(raw, dict):
        return AutomationTrigger(type=str(raw.get("type", "")), parameters=dict(raw.get("parameters") or {}))
    raise AutomationRuleError("rule 'trigger' must be a string or an object")


def _sequence_step_from_raw(raw: Dict[str, Any]) -> AutomationAction:
    """P0.11 - reuses `AutomationAction`'s own `{type, parameters}` shape
    for every sequence step (see `AutomationRule.sequence`'s own
    docstring). A `"delay"` step accepts `"seconds"` at the TOP LEVEL
    (`{"type": "delay", "seconds": 2}`, matching the brief's own worked
    example and this project's general preference for the least-nested
    authoring shape) as well as the fully-explicit `{"type": "delay",
    "parameters": {"seconds": 2}}` form - both normalize to the same
    internal `AutomationAction(type="delay", parameters={"seconds": 2})`
    representation `validate_sequence_step()`/`engine.py::_run_sequence()`
    both consume."""
    step_type = str(raw.get("type", ""))
    parameters = dict(raw.get("parameters") or {})
    if step_type == _SEQUENCE_DELAY_STEP_TYPE and "seconds" not in parameters and "seconds" in raw:
        parameters["seconds"] = raw.get("seconds")
    return AutomationAction(type=step_type, parameters=parameters)


def rule_from_dict(rule_id: str, data: Dict[str, Any]) -> AutomationRule:
    """Mirrors `luno/camera_patrol/route.py::route_from_dict()`'s own
    loader shape - the `id` comes from the JSON key (same "named-entity
    config file" convention `config/scripts.config.json`/
    `config/camera_patrol_routes.json` already established), everything
    else comes from the value object. Raises `AutomationRuleError` for
    anything malformed - callers (the engine's own `_load_rules()`) skip
    and log a malformed entry rather than crash, same as camera_patrol."""
    conditions = [
        AutomationCondition(
            type=str(c.get("type", "")), target=str(c.get("target", "")), value=c.get("value"),
            # P0.15 - additive: absent entirely (every condition
            # persisted before this sprint) degrades to `{}`, byte-for-
            # byte the same default `AutomationCondition.parameters`
            # already has - zero behavioral change for any existing rule.
            parameters=dict(c.get("parameters") or {}),
        )
        for c in (data.get("conditions") or [])
        if isinstance(c, dict)
    ]
    actions = [
        AutomationAction(type=str(a.get("type", "")), parameters=dict(a.get("parameters") or {}))
        for a in (data.get("actions") or [])
        if isinstance(a, dict)
    ]
    # P0.11 - additive, mutually exclusive with `actions` (validate_rule()
    # enforces the mutual exclusion; loading both here is harmless since
    # an empty list is the correct default for whichever one a given rule
    # doesn't use).
    sequence = [
        _sequence_step_from_raw(s)
        for s in (data.get("sequence") or [])
        if isinstance(s, dict)
    ]
    # P0.12 - additive metadata (see `AutomationRule.description`'s own
    # docstring). Absent entirely (every rule persisted before this
    # sprint) degrades to the exact pre-P0.12 defaults - `""`/`None`/
    # `None` - never raises here even if the on-disk value is a
    # surprising type; `validate_rule()` is the single place that
    # actually enforces `description`'s type/length, matching this
    # loader's existing "malformed values are the validator's job, not
    # the loader's" convention (e.g. `cooldown_seconds`'s own float()
    # coercion above already follows the same split).
    description = data.get("description", "")
    if not isinstance(description, str):
        description = str(description)
    created_at = data.get("created_at")
    updated_at = data.get("updated_at")

    return AutomationRule(
        id=rule_id,
        name=str(data.get("name", rule_id)),
        enabled=bool(data.get("enabled", True)),
        trigger=_trigger_from_raw(data.get("trigger")),
        conditions=conditions,
        actions=actions,
        cooldown_seconds=float(data.get("cooldown_seconds", 0.0)),
        execution_policy=str(data.get("execution_policy", "no_partial")),
        sequence=sequence,
        description=description,
        created_at=created_at if isinstance(created_at, str) else None,
        updated_at=updated_at if isinstance(updated_at, str) else None,
    )
