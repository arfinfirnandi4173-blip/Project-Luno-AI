"""
conditions.py
=============

Sprint 72 Phase 3 - a pure, deterministic condition evaluator. A
condition NEVER mutates a device, NEVER calls an LLM, and NEVER executes
arbitrary code - `evaluate_condition()` only reads a value through a
caller-supplied, already-vetted `state_readers` mapping (name -> zero-
argument callable) and compares it against `condition.value` using one
of a fixed set of pure Python operators. There is no `eval`/`exec`
anywhere in this file - see `tests/test_sprint72_automation_engine.py`'s
dedicated source-scan test for the executable proof.

Unknown condition type, unknown target, or any exception raised while
reading/comparing state is treated IDENTICALLY: the condition is
INVALID. `engine.py` treats an invalid condition the same as a FAILED
one (Phase 7: no partial execution - the whole rule is SKIPPED, no
action runs), never a partial/best-guess pass.

--------------------------------------------------------------------
P0.6 addition - `event.<field>` targets (additive, backward compatible)
--------------------------------------------------------------------
Sprint 72's original design only let a condition read externally
registered, caller-supplied `state_readers` (e.g. "what is
camera_patrol's current state right now?") - there was no way for a
condition to inspect a specific FIELD of the event that actually
triggered this execution (e.g. `camera_automation.camera_event`'s own
`kind` field, "human_detected" vs "human_cleared"). P0.6 needs exactly
that (match only `kind == "human_detected"`), and no existing
mechanism in this package could express it - state_readers has no
concept of "the event that just fired."

Rather than inventing a second condition engine, this is the smallest
possible additive extension to the existing one: a condition whose
`target` starts with the literal prefix `"event."` is resolved from the
triggering event's OWN `data` dict (the field name is whatever follows
the prefix, e.g. `target="event.kind"` reads `event_data["kind"]`)
instead of `state_readers`. Every target that does NOT start with
`"event."` is completely unaffected - resolved via `state_readers`
exactly as before, byte-for-byte identical behavior to pre-P0.6. A
missing/no-`event_data` (e.g. a `time`/`manual` trigger, which has no
originating event at all) resolves an `event.*` target to
`CONDITION_INVALID`, the same fail-closed semantics an unknown
state_reader target already had - no new failure mode, no relaxation of
the existing "no partial execution" guarantee.

--------------------------------------------------------------------
P0.15 addition - the `"time"` condition type (additive, backward compatible)
--------------------------------------------------------------------
A condition whose `type` is `TIME_CONDITION_TYPE` ("time") is NOT one of
the `CONDITION_TYPES` comparison operators above and has no `target` at
all - it is checked and returned in its own dedicated branch at the very
top of `evaluate_condition()`, before the `target` resolution block runs.
It reads `condition.parameters["after"]`/`["before"]` (both `HH:MM`,
validated at rule-save-time by `models.py`'s `_validate_time_condition()`,
but re-parsed and fail-closed here too) and compares the current local
time against that window, supporting both normal (`after <= before`) and
overnight/crosses-midnight (`after > before`) ranges, inclusive on both
boundaries. No new execution path, scheduler, or polling loop is
introduced - this is a pure, on-demand comparison evaluated exactly once,
at the moment `engine.py` evaluates the rest of the rule's conditions,
exactly like every other condition type in this file.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Any, Callable, Dict, Optional, Tuple

from .models import TIME_CONDITION_TYPE, AutomationCondition

StateReaders = Dict[str, Callable[[], Any]]

CONDITION_INVALID = "condition_invalid"

#: P0.6 - prefix marking a condition target as "read from the
#: triggering event's own data", not from `state_readers`. See module
#: docstring's "P0.6 addition" section.
_EVENT_TARGET_PREFIX = "event."


def _parse_hhmm(raw: Any) -> Optional[_datetime.time]:
    """P0.15 - parses an `HH:MM` (24h) string into a `datetime.time`,
    returning `None` on anything malformed. `models.py`'s own
    `_validate_time_condition()` already rejects a rule at save-time if
    `after`/`before` aren't valid `HH:MM` strings, but `evaluate_condition()`
    never trusts that upstream check either (same defense-in-depth
    convention as every other branch in this file) - a bad value here
    fails closed to `CONDITION_INVALID` below, never raises."""
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return _datetime.time(hour=hour, minute=minute)


def evaluate_condition(
    condition: AutomationCondition,
    state_readers: StateReaders,
    event_data: Optional[Dict[str, Any]] = None,
    now: Optional[_datetime.time] = None,
) -> Tuple[bool, str]:
    """Returns `(passed, reason)`. `reason` is `""` whenever the
    condition was evaluated at all (whether it passed or genuinely
    failed) and `CONDITION_INVALID` when it could not be evaluated in
    the first place (unknown type, unknown target, or an incompatible
    value for the comparison, e.g. `greater_than` against a non-numeric
    current value) - callers must not distinguish "genuinely failed" from
    "invalid" for execution purposes (both SKIP the rule), but the
    distinct reason string is preserved for the dashboard/logs/tests.

    `event_data` (P0.6, optional, defaults to `None` - existing callers
    that never pass it get identical pre-P0.6 behavior) is the `.data`
    dict of the event that triggered this execution, used only for
    `target` strings starting with `"event."` - see module docstring.

    `now` (P0.15, optional, defaults to `None`) is a `datetime.time`
    used only for `condition.type == "time"` (see `TIME_CONDITION_TYPE`)
    - when `None`, the current local wall-clock time is read via
    `datetime.datetime.now().time()`. Tests pass a fixed `now` directly
    for full determinism instead of monkeypatching the system clock;
    every existing caller (which never passes `now`) is unaffected."""
    if condition.type == TIME_CONDITION_TYPE:
        # P0.15 - a time condition has no `target`/state-reader concept
        # at all, so it is checked and returned FIRST, entirely before
        # the target-resolution block below (which would otherwise try
        # to resolve an empty/irrelevant `condition.target`).
        after = _parse_hhmm(condition.parameters.get("after"))
        before = _parse_hhmm(condition.parameters.get("before"))
        if after is None or before is None:
            return False, CONDITION_INVALID
        current_time = now if now is not None else _datetime.datetime.now().time()
        if after <= before:
            # Normal (same-day) range, both boundaries inclusive.
            # 18:00-23:30: 18:00 pass, 20:00 pass, 23:30 pass, 23:31 fail.
            passed = after <= current_time <= before
        else:
            # Overnight (crosses-midnight) range, both boundaries
            # inclusive. 22:00-02:00: 21:59 fail, 22:00 pass, 23:59
            # pass, 00:00 pass, 01:59 pass, 02:00 pass, 02:01 fail.
            passed = current_time >= after or current_time <= before
        return bool(passed), ""

    if condition.target.startswith(_EVENT_TARGET_PREFIX):
        field = condition.target[len(_EVENT_TARGET_PREFIX):]
        if not event_data or field not in event_data:
            return False, CONDITION_INVALID
        current = event_data[field]
    else:
        reader = state_readers.get(condition.target)
        if reader is None:
            return False, CONDITION_INVALID
        try:
            current = reader()
        except Exception:
            return False, CONDITION_INVALID

    try:
        if condition.type in ("equals", "state_is"):
            return bool(current == condition.value), ""
        if condition.type == "not_equals":
            return bool(current != condition.value), ""
        if condition.type == "greater_than":
            return bool(current > condition.value), ""
        if condition.type == "less_than":
            return bool(current < condition.value), ""
        if condition.type == "greater_equal":
            # P0.7 - the one new operator this sprint adds (models.py's
            # own CONDITION_TYPES comment has the full rationale). Same
            # TypeError-fails-closed contract every other comparison
            # operator here already has - a non-numeric `current` (e.g.
            # comparing a string) falls through to CONDITION_INVALID
            # below, never raises past this function.
            return bool(current >= condition.value), ""
        if condition.type == "contains":
            return bool(condition.value in current), ""
    except TypeError:
        return False, CONDITION_INVALID
    # Not one of CONDITION_TYPES - validate_rule() should have already
    # refused this at load time, but evaluate_condition() itself never
    # trusts that and fails closed regardless (defense in depth).
    return False, CONDITION_INVALID
