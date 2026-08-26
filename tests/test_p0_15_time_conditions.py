"""
tests/test_p0_15_time_conditions.py
=======================================

LUNO P0.15 (Human-Friendly Dashboard UX & Time-Based Automation
Conditions) - dedicated regression suite.

Adds ONE new, additive condition type - `{"type": "time", "parameters":
{"after": "HH:MM", "before": "HH:MM"}}` - evaluated inside the EXISTING
`AutomationEngine` condition-evaluation pipeline (`_evaluate_conditions()`
-> `evaluate_condition()`, both in `luno/automation/{engine,conditions}.py`,
neither of which gained a second/parallel evaluation path). No scheduler,
timer, or polling loop was introduced anywhere - a time condition is a
pure, on-demand comparison against `datetime.datetime.now().time()`,
evaluated exactly once, at the moment the rest of a rule's conditions are
already evaluated (trigger-processing time), exactly like every other
condition type. This sprint also polishes the existing P0.13 Dashboard UI
(human-readable trigger/condition/action summaries, a dedicated "Time"
condition card, empty/loading states, inline validation messages) without
introducing a second persistence mechanism, a second execution path, or a
frontend framework.

Testing approach (same convention every P0.1x file in this package has
already established - read `test_p0_14_ha_script_actions.py`'s own module
docstring for the full rationale, not repeated here):

  * Pure unit tests directly against `conditions.py::evaluate_condition()`
    (passing a fixed `now=` - P0.15's own new, optional parameter - for
    full determinism, never monkeypatching the system clock) for every
    validation/normal-range/overnight-range case in the brief's Section 12.
  * Real-bootstrap, real-thread, real-event-bus engine tests (a temp rules
    file, never the real `config/automation_rules.json`) for end-to-end
    "condition true executes action / condition false executes nothing"
    behavior. These use REAL wall-clock time with a several-minute margin
    (see `_true_window()`/`_false_window()` below) rather than a fixed
    `now=`, because `engine.py` itself never passes `now=` to
    `evaluate_condition()` (by design - P0.15 does not thread a fake clock
    through the engine) - this is deliberately the one place this suite
    exercises the REAL clock, exactly as Section 3 requires ("evaluate
    using current local time at trigger-processing moment").
  * Real-HTTP dashboard tests for the create -> save -> reload persistence
    round trip through the EXISTING P0.12 CRUD API.
  * Static source-scan / AST architecture-guard tests (same "structural
    proof, not a live browser" discipline every prior P0.x suite in this
    package already uses) for the frontend and for the "no scheduler / no
    second execution path / no direct HA access from the frontend"
    invariants Section 12's own "Architecture guards" subsection requires.

REAL_HA_TEST = NOT_APPLICABLE
------------------------------
P0.15 adds no new Home Assistant action type and no new ToolManager
dispatch path - see `test_p0_14_ha_script_actions.py`'s own honestly-
scoped `test_U1_real_home_assistant_smoke_test_is_honestly_marked_not_
performed` for this project's most recent real-HA-availability record
(no real, reachable Home Assistant instance in this sandbox). Nothing in
this file claims otherwise.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import requests  # noqa: E402

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402

from luno.automation.conditions import CONDITION_INVALID, evaluate_condition  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import (  # noqa: E402
    CONDITION_TYPES,
    TIME_CONDITION_TYPE,
    AutomationCondition,
    AutomationRuleError,
    rule_from_dict,
    validate_condition,
    validate_rule,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_CONDITIONS_PATH = os.path.join(_ROOT, "luno", "automation", "conditions.py")
_ENGINE_PATH = os.path.join(_ROOT, "luno", "automation", "engine.py")
_MODELS_PATH = os.path.join(_ROOT, "luno", "automation", "models.py")
_STATIC_INDEX_PATH = os.path.join(_ROOT, "luno", "dashboard", "static", "index.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _write_rules(path: str, rules: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)


def _build_stack(rules: Optional[Dict[str, Any]] = None):
    """Same real-bootstrap convention every sibling P0.1x file already
    established - all-mock backends (this sandbox's default), a
    TEMPORARY rules file (never the real `config/automation_rules.json`)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_15_automation_rules_test_")
    os.close(fd)
    engine._rules_path = rules_path
    if rules is not None:
        _write_rules(rules_path, rules)
    return runtime, modules, adapter_manager, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


def _build_dashboard(rules: Optional[Dict[str, Any]] = None):
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_15_dashboard_rules_test_")
    os.close(fd)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules or {}, fh)
    modules["automation_engine"]._rules_path = rules_path

    runtime.start()
    dashboard = DashboardServer(
        runtime, adapter_manager, modules, cfg,
        audio_capture_store=adapters.get("audio_capture_store"), host="127.0.0.1", port=0,
    )
    dashboard.start()
    return runtime, modules, adapter_manager, dashboard, rules_path


def _teardown_dashboard(runtime, adapter_manager, dashboard, rules_path=None) -> None:
    ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


def _time_cond(after: Any, before: Any) -> Dict[str, Any]:
    return {"type": "time", "target": "", "value": None, "parameters": {"after": after, "before": before}}


def _manual_rule(rule_id: str, conditions=None, actions=None, sequence=None, cooldown: float = 0.0) -> Dict[str, Any]:
    r: Dict[str, Any] = {"name": rule_id, "enabled": True, "trigger": "manual", "cooldown_seconds": cooldown}
    if conditions is not None:
        r["conditions"] = conditions
    if sequence is not None:
        r["sequence"] = sequence
    else:
        r["actions"] = actions if actions is not None else [{"type": "automation.log", "parameters": {"message": "ran"}}]
    return {rule_id: r}


def _t(h: int, m: int) -> datetime.time:
    return datetime.time(hour=h, minute=m)


# ============================================================================
# A. Time validation (Section 12 - "Time validation")
# ============================================================================

def test_A1_valid_normal_range_passes_validation():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    validate_condition(cond)  # must not raise


def test_A2_valid_overnight_range_passes_validation():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "22:00", "before": "02:00"})
    validate_condition(cond)  # must not raise


def test_A3_invalid_hour_rejected():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "24:00", "before": "02:00"})
    with pytest.raises(AutomationRuleError, match="HH:MM"):
        validate_condition(cond)


def test_A4_invalid_minute_rejected():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "12:60", "before": "18:00"})
    with pytest.raises(AutomationRuleError, match="HH:MM"):
        validate_condition(cond)


def test_A5_malformed_string_rejected():
    for bad in ("6pm", "1800", "18-00", "", "  ", "18:0"):
        cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": bad, "before": "20:00"})
        with pytest.raises(AutomationRuleError):
            validate_condition(cond)


def test_A6_missing_after_rejected():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"before": "20:00"})
    with pytest.raises(AutomationRuleError, match="'after'"):
        validate_condition(cond)


def test_A7_missing_before_rejected():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00"})
    with pytest.raises(AutomationRuleError, match="'before'"):
        validate_condition(cond)


def test_A8_time_type_deliberately_not_a_member_of_condition_types():
    """The design decision this sprint's own docstrings document: `"time"`
    is a distinct top-level constant, not a `CONDITION_TYPES` member (that
    frozenset stays pure comparison OPERATORS - `equals`/`greater_than`/
    etc. - used by `wait_until`'s and the generic condition row's own
    unrelated operator dropdowns). Re-verified here as a regression guard,
    since `validate_rule()`'s per-condition loop reaching the WRONG branch
    would silently break every test above."""
    assert TIME_CONDITION_TYPE == "time"
    assert TIME_CONDITION_TYPE not in CONDITION_TYPES


def test_A9_rule_from_dict_round_trips_parameters_field():
    rule = rule_from_dict("r1", {
        "name": "r1", "trigger": "manual",
        "conditions": [_time_cond("18:00", "23:30")],
        "actions": [{"type": "automation.log", "parameters": {"message": "x"}}],
    })
    assert len(rule.conditions) == 1
    assert rule.conditions[0].type == "time"
    assert rule.conditions[0].parameters == {"after": "18:00", "before": "23:30"}
    validate_rule(rule)  # must not raise


def test_A10_existing_condition_construction_without_parameters_still_defaults_to_empty_dict():
    """Backward compatibility (Section 11) - every P0.6-P0.10 call site
    constructs `AutomationCondition(type=..., target=..., value=...)`
    with no `parameters` kwarg at all; the new field must default
    harmlessly to `{}`, never `None`, never a required positional arg."""
    cond = AutomationCondition(type="equals", target="occupancy.state", value=True)
    assert cond.parameters == {}
    validate_condition(cond)  # must not raise - ordinary comparison condition, unaffected


# ============================================================================
# B. Normal range evaluation (Section 12 - "Normal range")
# ============================================================================

def test_B1_exactly_at_start_passes():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    passed, reason = evaluate_condition(cond, {}, now=_t(18, 0))
    assert passed is True and reason == ""


def test_B2_inside_range_passes():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    passed, reason = evaluate_condition(cond, {}, now=_t(20, 0))
    assert passed is True and reason == ""


def test_B3_exactly_at_end_passes():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    passed, reason = evaluate_condition(cond, {}, now=_t(23, 30))
    assert passed is True and reason == ""


def test_B4_immediately_outside_start_fails():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    passed, reason = evaluate_condition(cond, {}, now=_t(17, 59))
    assert passed is False and reason == ""  # genuinely evaluated and failed, not invalid


def test_B5_immediately_outside_end_fails():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "18:00", "before": "23:30"})
    passed, reason = evaluate_condition(cond, {}, now=_t(23, 31))
    assert passed is False and reason == ""


# ============================================================================
# C. Overnight range evaluation (Section 12 - "Overnight range", 22:00-02:00)
# ============================================================================

@pytest.mark.parametrize("hh,mm,expected", [
    (21, 59, False), (22, 0, True), (23, 59, True),
    (0, 0, True), (1, 59, True), (2, 0, True), (2, 1, False),
])
def test_C1_overnight_window_every_worked_example_from_the_brief(hh, mm, expected):
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "22:00", "before": "02:00"})
    passed, reason = evaluate_condition(cond, {}, now=_t(hh, mm))
    assert passed is expected
    assert reason == ""  # always a genuine evaluation, never CONDITION_INVALID


def test_C2_invalid_after_or_before_fails_closed_as_condition_invalid():
    cond = AutomationCondition(type=TIME_CONDITION_TYPE, parameters={"after": "not-a-time", "before": "02:00"})
    passed, reason = evaluate_condition(cond, {}, now=_t(1, 0))
    assert passed is False
    assert reason == CONDITION_INVALID


def test_C3_non_time_condition_types_completely_unaffected():
    """The new branch is checked FIRST in `evaluate_condition()`, but only
    ever triggers for `condition.type == "time"` - every pre-existing
    operator must still behave byte-for-byte as before."""
    cond = AutomationCondition(type="equals", target="foo", value=1)
    passed, reason = evaluate_condition(cond, {"foo": lambda: 1})
    assert passed is True and reason == ""
    cond2 = AutomationCondition(type="greater_than", target="bar", value=5)
    passed2, reason2 = evaluate_condition(cond2, {"bar": lambda: 3})
    assert passed2 is False and reason2 == ""


# ============================================================================
# D. Automation behavior (Section 12 - "Automation behavior") - real engine,
# real clock, several-minute margin so a slow test run can never flip a
# result (see module docstring's own rationale for using the real clock
# here specifically).
# ============================================================================

def _true_window() -> Dict[str, Any]:
    """A time condition guaranteed to be TRUE right now (an ~6-minute
    window centered on the current real time). Correctly degrades to an
    overnight-style window (handled the same as any other overnight
    window) if `now` happens to be within 3 minutes of midnight."""
    now = datetime.datetime.now()
    after = (now - datetime.timedelta(minutes=3)).strftime("%H:%M")
    before = (now + datetime.timedelta(minutes=3)).strftime("%H:%M")
    return _time_cond(after, before)


def _false_window() -> Dict[str, Any]:
    """A time condition guaranteed to be FALSE right now (a 3-minute
    window entirely 10-13 minutes in the future)."""
    now = datetime.datetime.now()
    after = (now + datetime.timedelta(minutes=10)).strftime("%H:%M")
    before = (now + datetime.timedelta(minutes=13)).strftime("%H:%M")
    return _time_cond(after, before)


def test_D1_condition_true_executes_action():
    rules = _manual_rule("r1", conditions=[_true_window()])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.log", lambda e: tool_calls.append(e.data))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: completed or skipped)
        assert len(completed) == 1, "a currently-true time condition must let the action run"
        assert not skipped
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D2_condition_false_executes_no_action():
    rules = _manual_rule("r1", conditions=[_false_window()])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: completed or skipped)
        assert len(skipped) == 1
        assert skipped[0].get("reason") == "condition_failed"
        assert not completed
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D3_condition_false_does_not_execute_sequence():
    rules = _manual_rule("r1", conditions=[_false_window()], sequence=[
        {"type": "automation.log", "parameters": {"message": "should never run"}},
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    logs: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.log", lambda e: logs.append(e.data))
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: skipped)
        time.sleep(0.2)  # a false-negative here (a step sneaking through) needs a moment to surface
        assert not logs, "no sequence step may execute when the time condition is false"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D4_condition_false_does_not_call_toolmanager():
    rules = _manual_rule("r1", conditions=[_false_window()], actions=[
        {"type": "home_assistant.turn_on", "parameters": {"target": "light.wled"}},
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: skipped)
        time.sleep(0.2)
        assert not tool_calls, "ToolManager must never be dispatched to when the time condition is false"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D5_existing_automations_without_time_conditions_remain_functional():
    """Backward compatibility (Section 11) - a rule with NO conditions at
    all (every rule that existed before this sprint) must complete exactly
    as it always did."""
    rules = _manual_rule("r1", conditions=None)
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D6_time_condition_combined_with_an_ordinary_condition_both_must_pass():
    rules = _manual_rule("r1", conditions=[_true_window(), {"type": "equals", "target": "always_true", "value": True}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine._state_readers["always_true"] = lambda: True
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# E. Persistence (Section 12 - "Persistence") - create -> save -> reload
# through the EXISTING P0.12 CRUD API, no new persistence mechanism.
# ============================================================================

def _ui_time_condition_payload(name: str = "P0.15 Persistence Test") -> Dict[str, Any]:
    """Mirrors `automBuildPayload()`/`automSaveDraft()` in `index.html`
    for a fresh draft carrying one time condition - the exact shape the
    browser's "+ Add Time Condition" button now produces."""
    return {
        "name": name, "description": "", "enabled": True,
        "trigger": {"type": "manual", "parameters": {}},
        "conditions": [_time_cond("18:00", "23:30")],
        "cooldown_seconds": 0, "execution_policy": "no_partial",
        "actions": [{"type": "automation.log", "parameters": {"message": "hi"}}],
    }


def test_E1_create_save_reload_round_trip_preserves_time_condition():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json=_ui_time_condition_payload(), timeout=5)
        assert r.status_code == 200 and r.json()["success"], r.text
        created = r.json()["automation"]
        assert created["conditions"] == [{"type": "time", "target": "", "value": None,
                                           "parameters": {"after": "18:00", "before": "23:30"}}]

        # Reload: force the SAME engine to re-read the rules file from
        # disk (`reload_rules()` - same convention `test_p0_14_ha_script_
        # actions.py::test_R1` already established) rather than trusting
        # the in-memory copy `create_rule()` just returned - the actual
        # persistence proof (Section 10/12's own "Create -> Save ->
        # Reload -> Load" requirement).
        engine = modules["automation_engine"]
        engine.reload_rules()
        rule = engine.get_rule(created["id"])
        assert rule is not None
        assert rule["conditions"] == [{"type": "time", "target": "", "value": None,
                                        "parameters": {"after": "18:00", "before": "23:30"}}]
    finally:
        _teardown_dashboard(runtime, adapter_manager, dashboard, rp)


def test_E2_overnight_time_condition_also_survives_persistence():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_time_condition_payload("Overnight Persistence Test")
        payload["conditions"] = [_time_cond("22:00", "02:00")]
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        assert r.status_code == 200 and r.json()["success"], r.text
        created = r.json()["automation"]

        engine = modules["automation_engine"]
        engine.reload_rules()

        r2 = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5)
        fetched = r2.json()
        assert fetched["conditions"] == [{"type": "time", "target": "", "value": None,
                                           "parameters": {"after": "22:00", "before": "02:00"}}]
    finally:
        _teardown_dashboard(runtime, adapter_manager, dashboard, rp)


def test_E3_invalid_time_condition_rejected_by_server_side_validation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_time_condition_payload("Bad Time")
        payload["conditions"] = [_time_cond("99:99", "02:00")]
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        body = r.json()
        assert not body.get("success", True), "server must refuse an invalid time condition, never silently persist it"
    finally:
        _teardown_dashboard(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# F. Dashboard (Section 12 - "Dashboard") - static source-scan, same
# convention `test_p0_13_automation_dashboard.py`/`test_p0_14_ha_script_
# actions.py` already established for this file (no headless browser in
# this project).
# ============================================================================

def _script_block() -> str:
    html = _read(_STATIC_INDEX_PATH)
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m is not None
    return m.group(1)


def test_F1_time_condition_card_markup_present():
    script = _script_block()
    assert "🕐 Time" in script
    assert "autom-time-card" in script
    assert "data-time-cond-field=\"after\"" in script
    assert "data-time-cond-field=\"before\"" in script
    assert "Active during this period" in script


def test_F2_add_time_condition_button_and_binding_present():
    script = _script_block()
    assert "autom-add-time-cond-btn" in script
    assert re.search(r"push\(\{type:\s*'time'", script)


def test_F3_time_condition_editor_reads_and_writes_the_parameters_field():
    script = _script_block()
    # The From/To inputs must write into `condition.parameters.{after,
    # before}` (P0.15's additive schema field), never `.target`/`.value`.
    assert "cond.parameters[inp.dataset.timeCondField]" in script


def test_F4_invalid_time_validation_messages_match_the_brief_wording():
    script = _script_block()
    assert "Invalid time" in script
    assert "Time range is incomplete" in script


def test_F5_overnight_range_not_rejected_client_side():
    """The client-side HH:MM regex must accept ANY valid HH:MM pair for
    `after`/`before` - it must never additionally require `after <=
    before` (which would incorrectly reject every overnight window)."""
    script = _script_block()
    m = re.search(r"const HHMM_RE = (/\^.*?\$/);", script)
    assert m is not None
    # No "after <= before"/"before < after"-style client-side comparison
    # anywhere near the time-condition validation block.
    validate_fn_start = script.index("function automClientValidate")
    validate_fn_end = script.index("\nfunction automRenderErrors")
    body = script[validate_fn_start:validate_fn_end]
    assert "after >" not in body and "after <" not in body and "before <" not in body and "before >" not in body


def test_F6_condition_appears_in_automation_list_card_summary():
    script = _script_block()
    assert "automConditionsHuman" in script
    assert "Only between" in script


def test_F7_empty_conditions_state_message_present():
    script = _script_block()
    assert "this automation runs every time its trigger fires" in script


def test_F8_loading_state_present_for_automations_list():
    html = _read(_STATIC_INDEX_PATH)
    assert "Loading automations" in html


# ============================================================================
# G. Architecture guards (Section 12 - "Architecture guards")
# ============================================================================

def test_G1_no_direct_ha_call_from_frontend():
    script = _script_block()
    for forbidden in ("homeassistant.local", "ws://", "wss://", "8123", "hass.io"):
        assert forbidden not in script


def test_G2_no_direct_config_mutation_from_frontend():
    """The frontend must never itself write `config/automation_rules.json`
    - it must only ever go through the existing `/api/automations*` CRUD
    endpoints, which do their own server-side persistence. This does NOT
    mean the literal filename can never appear in the script block at all
    (the P0.13 module docstring's own architecture comment - "this panel
    never reads/writes config/automation_rules.json..." - legitimately
    names the file it promises never to touch); what actually matters is
    that no filesystem-write API is ever called anywhere in the file."""
    script = _script_block()
    for forbidden in ("fs.writeFile", "fs.write(", "fs.writeFileSync", "require('fs')", 'require("fs")'):
        assert forbidden not in script


def test_G3_every_time_condition_write_goes_through_the_existing_automations_api():
    """The time card's own bindings only ever mutate `automState.draft` in
    memory; persistence still happens exclusively through `automSaveDraft
    ()`'s existing `POST /api/automations*` calls - no second save path
    was introduced for the new card."""
    script = _script_block()
    time_binding_start = script.index("$$('[data-time-cond-field]')")
    time_binding_end = script.index("$$('input[name=autom-mode]')")
    body = script[time_binding_start:time_binding_end]
    assert "fetch(" not in body
    assert "api(" not in body  # the time-card bindings themselves never call the API directly - only automSaveDraft() does


def test_G4_conditions_py_never_references_toolmanager_or_dispatch():
    """A time condition has no action of its own - `conditions.py` must
    remain a pure evaluator (Sprint 72's own original invariant,
    unchanged by P0.15): it may never reference ToolManager, publish to
    the event bus, or otherwise dispatch a device action."""
    source = _read(_CONDITIONS_PATH)
    for forbidden in ("ToolManager", "tool_requested", ".publish(", "event_bus"):
        assert forbidden not in source


def test_G5_no_scheduler_or_polling_loop_introduced_in_conditions_py():
    source = _read(_CONDITIONS_PATH)
    for forbidden in ("threading.Timer", "sched.", "asyncio", "while True", "setInterval", "cron"):
        assert forbidden not in source
    tree = ast.parse(source)
    thread_spawns = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and n.func.attr in ("Thread", "Timer")]
    assert thread_spawns == []


def test_G6_no_scheduler_or_polling_loop_introduced_in_models_py():
    source = _read(_MODELS_PATH)
    for forbidden in ("threading.Timer", "sched.", " cron", "APScheduler"):
        assert forbidden not in source


def test_G7_engine_py_still_has_exactly_one_thread_spawn_site():
    """Same structural proof `test_p0_14_ha_script_actions.py::test_P1`
    already established for P0.14, re-verified for P0.15 - confirms this
    sprint added zero new per-condition/per-time-check thread spawns."""
    source = _read(_ENGINE_PATH)
    assert source.count("threading.Thread(") == 1


def test_G8_engine_py_evaluate_condition_call_sites_unchanged():
    """P0.15 requires ZERO changes to `engine.py` for the time-condition
    feature itself (the whole point of routing it through the EXISTING
    generic `evaluate_condition()` loop) - the number of ACTUAL CALLS (an
    AST count, not a raw substring count - `engine.py` also mentions
    `evaluate_condition()` twice in prose/docstrings, which must not be
    miscounted as call sites) must be exactly what it already was before
    this sprint: three (the rule's own top-level conditions, `wait_until`'s
    target-state check, and the P0.14 nested `condition` step's own
    conditions list)."""
    tree = ast.parse(_read(_ENGINE_PATH))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "evaluate_condition"]
    assert len(calls) == 3


def test_G9_automation_engine_is_still_the_only_automationengine_class():
    tree = ast.parse(_read(_ENGINE_PATH))
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert class_names == {"AutomationEngine"}


def test_G10_no_new_execution_primitives_introduced_anywhere_in_the_backend():
    for path in (_CONDITIONS_PATH, _MODELS_PATH, _ENGINE_PATH):
        source = _read(path)
        for forbidden in ("ThreadPoolExecutor", "asyncio.gather", "concurrent.futures", "multiprocessing", "APScheduler"):
            assert forbidden not in source, f"{path}: forbidden execution primitive {forbidden!r}"


def test_G11_no_forbidden_code_execution_primitives_in_conditions_py():
    """Same `eval`/`exec`/`os.system`/`subprocess` AST guard every prior
    P0.1x file already runs over every file it touches - re-run here for
    `conditions.py` specifically, since this is the one file in this
    package no prior suite's own guard list has covered before."""
    forbidden_calls = {"eval", "exec", "os.system", "os.popen", "importlib.import_module"}
    forbidden_import_roots = {"subprocess"}
    tree = ast.parse(_read(_CONDITIONS_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_import_roots
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_import_roots
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = f"{func.value.id}.{func.attr}"
            else:
                name = None
            assert name not in forbidden_calls
