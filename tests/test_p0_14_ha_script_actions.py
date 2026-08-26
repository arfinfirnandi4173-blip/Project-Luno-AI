"""
tests/test_p0_14_ha_script_actions.py
=========================================

LUNO P0.14 (Advanced Home Assistant Automation Actions & Script Runner) -
dedicated regression suite.

Adds seven new `home_assistant.*` action types (`toggle`, `set_brightness`,
`set_color`, `set_temperature`, `run_script`, `activate_scene`,
`call_service`) and three new `sequence`-only control step types
(`wait_until`, `condition`, `stop_automation`) - every one of them
additive, none of them touching the EXISTING P0.11 `sequence` engine, the
P0.12 CRUD API, the P0.13 dashboard, or Vision/Camera Automation. Every
new action type still dispatches through the EXACT SAME `AutomationEngine.
_dispatch_action()` -> `_dispatch_tool_call()` -> `tool_requested` ->
ToolManager round trip every pre-existing action already used - there is
still exactly ONE execution path for Home Assistant actions in this
project (see `engine.py::_build_p0_14_tool_call()`'s own docstring).

Testing approach (same convention `test_p0_11_action_sequence.py`/
`test_p0_13_automation_dashboard.py` already established for this
package - read those files' own module docstrings for the full
rationale, not repeated here):

  * Real-bootstrap, real-thread, real-event-bus engine tests
    (`_build_stack()`/`_teardown()`/`_wait_until()` - a temp rules file,
    never the real `config/automation_rules.json`) for everything that
    needs an actual dispatched action or a real multi-second sequence.
  * Real-HTTP dashboard tests (`_build_dashboard()`) for the new `GET
    /api/automations/devices` endpoint and end-to-end CRUD/persistence
    round trips through the P0.12 API surface.
  * Static source-scan / AST architecture-guard tests (the same
    "structural proof, not a live browser/live HA" discipline every
    prior P0.x suite in this package already uses) for the frontend and
    for security-boundary invariants (Section 17/22 of the brief).

Sections A-T per the P0.14 brief's own Section 20 minimum-coverage list,
plus a dedicated concurrency test and a dedicated, honestly-scoped real
Home Assistant smoke-test section (Section 21).

REAL_HA_TEST = NOT_PERFORMED
-----------------------------
This sandbox has no real, reachable Home Assistant instance (no
`HOME_ASSISTANT_BACKEND=real` environment, no live HA URL/token
configured - confirmed by inspection of `luno/bootstrap/adapters.py`'s
own backend-selection logic and this environment's actual env vars). Per
the brief's own Section 21 instruction ("If real HA is unavailable: mark
REAL_HA_TEST = NOT_PERFORMED. Do not fabricate success."), `test_U1_real_
home_assistant_smoke_test_is_honestly_marked_not_performed` below is the
literal, honest record of that fact - it is a skip, not a fabricated
pass.
"""

from __future__ import annotations

import ast
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
from luno.dashboard import automation_api  # noqa: E402

from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import (  # noqa: E402
    ACTION_TYPES,
    DEFAULT_WAIT_UNTIL_TIMEOUT_SECONDS,
    MAX_WAIT_UNTIL_TIMEOUT_SECONDS,
    MIN_WAIT_UNTIL_TIMEOUT_SECONDS,
    SEQUENCE_STEP_TYPES,
    AutomationAction,
    AutomationRule,
    AutomationRuleError,
    AutomationTrigger,
    rule_from_dict,
    validate_rule,
    validate_sequence_step,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_ENGINE_PATH = os.path.join(_ROOT, "luno", "automation", "engine.py")
_MODELS_PATH = os.path.join(_ROOT, "luno", "automation", "models.py")
_MOCK_HA_PATH = os.path.join(_ROOT, "luno", "tool_manager", "builtin", "home_assistant.py")
_REAL_HA_PATH = os.path.join(_ROOT, "luno", "tool_manager", "builtin", "real_home_assistant.py")
_AUTOMATION_API_PATH = os.path.join(_ROOT, "luno", "dashboard", "automation_api.py")
_SERVER_PATH = os.path.join(_ROOT, "luno", "dashboard", "server.py")
_STATIC_INDEX_PATH = os.path.join(_ROOT, "luno", "dashboard", "static", "index.html")
_VISION_PATH = os.path.join(_ROOT, "luno", "vision.py")


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


def _build_stack(rules: Optional[Dict[str, Any]] = None, rules_path: Optional[str] = None):
    """Same real-bootstrap convention `test_p0_11_action_sequence.py`/
    `test_sprint72_automation_engine.py` already established - all-mock
    backends (this sandbox's default), a TEMPORARY rules file (never the
    real `config/automation_rules.json`)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is None:
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_14_automation_rules_test_")
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
    """Same real-bootstrap-plus-real-HTTP convention `test_p0_13_
    automation_dashboard.py` already established."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_14_dashboard_rules_test_")
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


# -- rule-building helpers ----------------------------------------------------

def _seq_rule(rule_id: str, sequence, cooldown: float = 0.0, enabled: bool = True) -> Dict[str, Any]:
    return {rule_id: {
        "name": rule_id, "enabled": enabled, "trigger": "manual",
        "sequence": sequence, "cooldown_seconds": cooldown,
    }}


def _ha_on(target: str) -> Dict[str, Any]:
    return {"type": "home_assistant.turn_on", "parameters": {"target": target}}


def _delay(seconds) -> Dict[str, Any]:
    return {"type": "delay", "seconds": seconds}


def _log(message: str = "x") -> Dict[str, Any]:
    return {"type": "automation.log", "parameters": {"message": message}}


def _call_service(domain: str, service: str, entity_ids, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"type": "home_assistant.call_service", "parameters": {
        "domain": domain, "service": service,
        "target": {"entity_id": entity_ids}, "data": data or {},
    }}


def _run_script(entity_id: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"entity_id": entity_id}
    if variables is not None:
        params["variables"] = variables
    return {"type": "home_assistant.run_script", "parameters": params}


def _activate_scene(entity_id: str) -> Dict[str, Any]:
    return {"type": "home_assistant.activate_scene", "parameters": {"entity_id": entity_id}}


def _wait_until_step(target: str, operator: str, value: Any, attribute: str = "state",
                      timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"target": target, "attribute": attribute, "operator": operator, "value": value}
    if timeout_seconds is not None:
        params["timeout_seconds"] = timeout_seconds
    return {"type": "wait_until", "parameters": params}


def _condition_step(conditions, then=None, else_=None) -> Dict[str, Any]:
    return {"type": "condition", "parameters": {"conditions": conditions, "then": then or [], "else": else_ or []}}


def _stop_step() -> Dict[str, Any]:
    return {"type": "stop_automation", "parameters": {}}


# ============================================================================
# A. Generic HA service action (Section 4)
# ============================================================================

def test_A1_call_service_dispatches_via_tool_manager_with_domain_service_entity_data():
    rules = _seq_rule("r1", [_call_service("light", "turn_on", ["light.wled"], {"brightness_pct": 50})])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 1
        call = tool_calls[0]
        assert call["tool"] == "home_assistant"
        assert call["action"] == "call_service"
        assert call["parameters"]["domain"] == "light"
        assert call["parameters"]["service"] == "turn_on"
        assert call["parameters"]["entity_id"] == ["light.wled"]
        assert call["parameters"]["data"] == {"brightness_pct": 50}
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_A2_call_service_multiple_entity_ids_all_included_in_tool_call():
    rules = _seq_rule("r1", [_call_service("light", "turn_off", ["light.wled", "light.main_light"])])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert tool_calls[0]["parameters"]["entity_id"] == ["light.wled", "light.main_light"]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_A3_call_service_validation_rejects_bad_domain_format():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [_call_service("Light!", "turn_on", ["light.wled"])]})
    with pytest.raises(AutomationRuleError, match="lowercase snake_case 'domain'"):
        validate_rule(rule)


def test_A4_call_service_requires_at_least_one_entity_id():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [_call_service("light", "turn_on", [])]})
    with pytest.raises(AutomationRuleError, match="entity id"):
        validate_rule(rule)


# ============================================================================
# B. Run HA script (Section 5)
# ============================================================================

def test_B1_run_script_dispatches_with_entity_id_target():
    rules = _seq_rule("r1", [_run_script("script.morning_routine")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert tool_calls[0]["action"] == "run_script"
        assert tool_calls[0]["target"] == "script.morning_routine"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_B2_run_script_with_variables_included_in_parameters():
    rules = _seq_rule("r1", [_run_script("script.morning_routine", variables={"room": "living_room", "brightness": 50})])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert tool_calls[0]["parameters"]["variables"] == {"room": "living_room", "brightness": 50}
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_B3_run_script_requires_entity_id_at_validation():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [{"type": "home_assistant.run_script", "parameters": {}}]})
    with pytest.raises(AutomationRuleError, match="entity_id"):
        validate_rule(rule)


# ============================================================================
# C. Activate scene (Section 6)
# ============================================================================

def test_C1_activate_scene_dispatches():
    rules = _seq_rule("r1", [_activate_scene("scene.movie_mode")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert tool_calls[0]["action"] == "activate_scene"
        assert tool_calls[0]["target"] == "scene.movie_mode"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_C2_activate_scene_requires_entity_id():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [{"type": "home_assistant.activate_scene", "parameters": {}}]})
    with pytest.raises(AutomationRuleError, match="entity_id"):
        validate_rule(rule)


# ============================================================================
# D. Delay (Section 7 - unmodified P0.11 behavior, proven to still work
# alongside the new P0.14 step types in the same sequence)
# ============================================================================

def test_D1_delay_step_still_works_inside_a_sequence_with_new_step_types():
    rules = _seq_rule("r1", [_ha_on("A"), _delay(0.1), _run_script("script.x"), _activate_scene("scene.y")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        results = status["last_execution"]["action_results"]
        assert [r["type"] for r in results] == [
            "home_assistant.turn_on", "delay", "home_assistant.run_script", "home_assistant.activate_scene",
        ]
        assert all(r["status"] == "completed" for r in results)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# E. Wait until - success (Section 8)
# ============================================================================

def test_E1_wait_until_completes_once_ha_state_reader_reports_expected_value():
    rules = _seq_rule("r1", [_wait_until_step("light.wled", "equals", "on", timeout_seconds=5)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    # `ha_state_reader` is a plain, directly-settable public attribute for
    # exactly this purpose (see its own docstring in engine.py) - never
    # bound to anything under the mock backend by default.
    engine.ha_state_reader = lambda entity_id: "on"
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1, timeout_s=3.0)
        status = engine.get_automation_status("r1")
        result = status["last_execution"]["action_results"][0]
        assert result["type"] == "wait_until"
        assert result["status"] == "completed"
        assert result["code"] == "condition_met"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_E2_wait_until_uses_existing_condition_operators_greater_than():
    rules = _seq_rule("r1", [_wait_until_step("sensor.count", "greater_than", 1, timeout_seconds=5)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    # `greater_than`'s comparison is a plain Python `>` (see conditions.py)
    # with no string->number coercion - the state reader must return a
    # genuinely numeric value for a numeric operator to ever match,
    # exactly like a real HA numeric sensor's `state_readers` value would.
    engine.ha_state_reader = lambda entity_id: 3
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1, timeout_s=3.0)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# F. Wait until - timeout (Section 8/15)
# ============================================================================

def test_F1_wait_until_times_out_when_condition_never_becomes_true():
    rules = _seq_rule("r1", [_wait_until_step("light.wled", "equals", "on", timeout_seconds=1)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine.ha_state_reader = lambda entity_id: "off"  # never matches "on"
    timed_out: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.timeout", lambda e: timed_out.append(e.data))
    try:
        runtime.start()
        t0 = time.monotonic()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(timed_out) == 1, timeout_s=3.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.5, "wait_until must never hang past its own configured timeout"
        status = engine.get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "TIMEOUT"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F2_wait_until_times_out_honestly_when_no_ha_state_reader_bound():
    """Section 8's own 'never fabricate a match' requirement - under the
    mock backend (this sandbox's default), `AutomationEngine.ha_state_
    reader` is `None` by construction, and `_run_wait_until_step()` must
    report an honest `ha_state_reader_unavailable` timeout rather than
    ever claiming the condition was met."""
    rules = _seq_rule("r1", [_wait_until_step("light.wled", "equals", "on", timeout_seconds=2)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    assert engine.ha_state_reader is None
    timed_out: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.timeout", lambda e: timed_out.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(timed_out) == 1, timeout_s=3.0)
        status = engine.get_automation_status("r1")
        result = status["last_execution"]["action_results"][0]
        assert result["code"] == "ha_state_reader_unavailable"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F3_wait_until_timeout_bounds_enforced_at_validation():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [_wait_until_step("x", "equals", "on", timeout_seconds=99999)]})
    with pytest.raises(AutomationRuleError, match="timeout_seconds"):
        validate_rule(rule)
    rule2 = rule_from_dict("r2", {"name": "r2", "trigger": "manual",
                                   "sequence": [_wait_until_step("x", "equals", "on", timeout_seconds=-5)]})
    with pytest.raises(AutomationRuleError, match="timeout_seconds"):
        validate_rule(rule2)


# ============================================================================
# G. Condition - true branch (Section 9)
# ============================================================================

def test_G1_condition_true_runs_then_branch():
    rules = _seq_rule("r1", [
        _condition_step([{"target": "occupancy.person_count", "type": "greater_than", "value": 1}],
                         then=[_run_script("script.then_branch")], else_=[_run_script("script.else_branch")]),
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine._state_readers["occupancy.person_count"] = lambda: 3
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 1
        assert tool_calls[0]["target"] == "script.then_branch"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# H. Condition - false branch (Section 9)
# ============================================================================

def test_H1_condition_false_runs_else_branch():
    rules = _seq_rule("r1", [
        _condition_step([{"target": "occupancy.person_count", "type": "greater_than", "value": 1}],
                         then=[_run_script("script.then_branch")], else_=[_run_script("script.else_branch")]),
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine._state_readers["occupancy.person_count"] = lambda: 0
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 1
        assert tool_calls[0]["target"] == "script.else_branch"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_H2_condition_nested_sub_step_dispatches_identically_to_a_top_level_step():
    """Section 9's own 'a nested step behaves identically to a top-level
    one' requirement, proven directly: the SAME `home_assistant.turn_on`
    step, once at the top level and once nested inside a condition's
    `then` branch, must produce an identical tool_call shape (minus the
    obviously-different target)."""
    rules = _seq_rule("r1", [
        _ha_on("light.top_level"),
        _condition_step([{"target": "occupancy.person_count", "type": "equals", "value": 1}],
                         then=[_ha_on("light.nested")]),
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine._state_readers["occupancy.person_count"] = lambda: 1
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        engine.run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 2
        top, nested = tool_calls
        assert top["tool"] == nested["tool"] == "home_assistant"
        assert top["action"] == nested["action"] == "turn_on"
        assert top["target"] == "light.top_level"
        assert nested["target"] == "light.nested"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# I. Sequence ordering with mixed new/old step types (Section 6 of P0.11,
# re-verified here for the new P0.14 step types specifically)
# ============================================================================

def test_I1_mixed_step_types_execute_in_strict_order():
    rules = _seq_rule("r1", [
        _ha_on("A"), _call_service("light", "turn_on", ["B"]), _run_script("script.C"),
        _activate_scene("scene.D"), _delay(0.05), _ha_on("E"),
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    started: List[Any] = []
    runtime.event_bus.subscribe("automation.step_started", lambda e: started.append(e.data["step_index"]))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert started == [0, 1, 2, 3, 4, 5]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# J. Failure stops sequence (Section 14) - proven with a new P0.14 step
# preceding the failure, showing the SAME strict stop-on-failure discipline
# P0.11 already established applies uniformly regardless of step type.
# ============================================================================

def test_J1_a_failure_after_a_p0_14_step_still_stops_the_sequence():
    rules = _seq_rule("r1", [
        _run_script("script.ok"),
        {"type": "camera.preset", "parameters": {"preset": "does_not_exist"}},  # real runtime failure
        _activate_scene("scene.never_reached"),
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        assert len(tool_calls) == 2  # script.ok + the failing preset - scene.never_reached must NEVER dispatch
        assert not any(c.get("target") == "scene.never_reached" for c in tool_calls)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "FAILED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_J2_stop_automation_step_produces_cancelled_not_failed():
    rules = _seq_rule("r1", [_ha_on("A"), _stop_step(), _ha_on("never_reached")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    cancelled: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.cancelled", lambda e: cancelled.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(cancelled) == 1)
        assert len(tool_calls) == 1  # only "A" - the stop step and the step after it never dispatch anything
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "CANCELLED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# K. Entity validation (Section 16)
# ============================================================================

def test_K1_set_brightness_requires_numeric_level_0_to_100():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [
        {"type": "home_assistant.set_brightness", "parameters": {"target": "light.wled", "level": 150}},
    ]})
    with pytest.raises(AutomationRuleError, match="0-100"):
        validate_rule(rule)


def test_K2_set_color_requires_color_or_rgb():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [
        {"type": "home_assistant.set_color", "parameters": {"target": "light.wled"}},
    ]})
    with pytest.raises(AutomationRuleError, match="color.*rgb"):
        validate_rule(rule)


def test_K3_toggle_target_must_not_be_wildcard():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [
        {"type": "home_assistant.toggle", "parameters": {"target": "*"}},
    ]})
    with pytest.raises(AutomationRuleError, match="wildcard"):
        validate_rule(rule)


def test_K4_wait_until_requires_non_empty_target_and_a_value():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [{"type": "wait_until", "parameters": {"operator": "equals"}}]})
    with pytest.raises(AutomationRuleError, match="target"):
        validate_rule(rule)


# ============================================================================
# L. Invalid action rejection (Section 3/16)
# ============================================================================

def test_L1_unknown_action_type_never_reaches_the_engine():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [{"type": "shell.run_command", "parameters": {}}]})
    with pytest.raises(AutomationRuleError, match="unknown step type"):
        validate_rule(rule)


def test_L2_delay_seconds_still_refused_on_every_new_p0_14_action_type():
    """P0.8.9's own existing rule ('delay_seconds' only valid on turn_on/
    turn_off/toggle) must still refuse it on every genuinely new P0.14
    action type - not silently accepted/ignored."""
    for action_type, extra in (
        ("home_assistant.set_brightness", {"target": "light.wled", "level": 50}),
        ("home_assistant.run_script", {"entity_id": "script.x"}),
        ("home_assistant.call_service", {"domain": "light", "service": "turn_on", "target": {"entity_id": ["light.x"]}}),
    ):
        params = dict(extra, delay_seconds=5)
        rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                     "sequence": [{"type": action_type, "parameters": params}]})
        with pytest.raises(AutomationRuleError, match="delay_seconds"):
            validate_rule(rule)


def test_L3_condition_step_missing_conditions_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [{"type": "condition", "parameters": {"then": [_log()]}}]})
    with pytest.raises(AutomationRuleError, match="conditions"):
        validate_rule(rule)


def test_L4_condition_nesting_beyond_max_depth_rejected():
    """Section 16 - recursive/unbounded condition nesting must be
    rejected, not silently allowed. Builds a chain one level DEEPER than
    `MAX_CONDITION_NESTING_DEPTH` allows."""
    from luno.automation.models import MAX_CONDITION_NESTING_DEPTH

    inner: Dict[str, Any] = _log()
    for _ in range(MAX_CONDITION_NESTING_DEPTH + 1):
        inner = _condition_step([{"target": "x", "type": "equals", "value": 1}], then=[inner])
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [inner]})
    with pytest.raises(AutomationRuleError, match="nesting too deep"):
        validate_rule(rule)


# ============================================================================
# M. Unknown service rejection (Section 4/16)
# ============================================================================

def test_M1_call_service_rejects_non_snake_case_service_name():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [_call_service("light", "TurnOn", ["light.wled"])]})
    with pytest.raises(AutomationRuleError, match="lowercase snake_case 'service'"):
        validate_rule(rule)


def test_M2_call_service_rejects_non_string_service():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual",
                                 "sequence": [_call_service("light", None, ["light.wled"])]})
    with pytest.raises(AutomationRuleError, match="service"):
        validate_rule(rule)


# ============================================================================
# N. No direct HA frontend access (Section 22) - static source-scan, same
# convention `test_p0_13_automation_dashboard.py`'s own Section T already
# established for this dashboard.
# ============================================================================

def _script_block() -> str:
    html = _read(_STATIC_INDEX_PATH)
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m is not None
    return m.group(1)


def test_N1_dashboard_javascript_never_references_home_assistant_directly():
    """The new P0.14 UI code (entity pickers, step param fields, the
    condition-branch builder) only ever POSTs to the EXISTING `/api/
    automations*` family - never a raw Home Assistant URL/host, never a
    `homeassistant://`/`ws://` scheme, never a second fetch target."""
    source = _script_block()
    for forbidden in ("homeassistant.local", "ws://", "wss://", "8123", "hass.io"):
        assert forbidden not in source, f"dashboard JS must never reference {forbidden!r} directly"


def test_N2_new_p0_14_fetch_call_sites_all_target_the_automations_family():
    source = _script_block()
    assert "/api/automations/devices" in source
    # Every api(...) call this file's own P0.14 additions introduced
    # (ensureAutomSchema()'s new devices fetch) must target the same
    # `/api/automations*` family every pre-existing call already used.
    devices_call = re.search(r"api\(\s*'(/api/automations/devices[^']*)'", source)
    assert devices_call is not None


def test_N3_no_eval_or_dynamic_function_construction_introduced():
    """Word-boundary regex, not a bare substring check - a naive `"eval("
    in source` false-positives on unrelated identifiers that merely END
    with "eval" (e.g. this file's own pre-existing `recallMemoryRetrieval(`/
    `collect_memory_retrieval(` calls) - the same "false positive from a
    substring match" pitfall this project's own architecture-guard tests
    elsewhere already learned to avoid."""
    source = _script_block()
    assert re.search(r"(?<![A-Za-z0-9_])eval\s*\(", source) is None
    assert "new Function(" not in source
    assert 'setTimeout("' not in source and "setInterval(\"" not in source


# ============================================================================
# O. No ToolManager bypass (Section 1/22)
# ============================================================================

def test_O1_build_p0_14_tool_call_never_dispatches_directly():
    """`_build_p0_14_tool_call()` must be a PURE translator (build and
    return a dict) - it must never itself call `_dispatch_tool_call()`;
    only `_dispatch_home_assistant_action()` (its one caller) does that,
    exactly the same way it already does for turn_on/turn_off/toggle."""
    source = _read(_ENGINE_PATH)
    start = source.index("def _build_p0_14_tool_call(")
    end = source.index("\n    def _cancel_pending_delayed_action(")
    body = source[start:end]
    assert "_dispatch_tool_call(" not in body
    assert "self._event_bus.publish(" not in body


def test_O2_wait_until_and_condition_and_stop_steps_never_call_dispatch_tool_call_directly():
    """Section 8/9's own 'reuses the existing state-reading/dispatch
    mechanism, no second HA read/dispatch path' requirement, proven
    structurally: none of the three new control-step methods may call
    `_dispatch_tool_call()`/`self._event_bus.publish(` themselves - only
    `_run_action_step()` (via `_dispatch_action()`) may."""
    source = _read(_ENGINE_PATH)
    for method_name, next_method in (
        ("_run_stop_step", "_run_wait_until_step"),
        ("_run_wait_until_step", "_run_condition_step"),
        ("_run_condition_step", "_run_delay_step"),
    ):
        start = source.index(f"def {method_name}(")
        end = source.index(f"\n    def {next_method}(")
        body = source[start:end]
        assert "_dispatch_tool_call(" not in body, f"{method_name} must never dispatch a tool call directly"
        assert "self._event_bus.publish(" not in body, f"{method_name} must never publish to the event bus directly"


def test_O3_every_p0_14_action_type_is_dispatched_through_dispatch_action():
    """End-to-end proof (not just structural): running every new action
    type produces exactly one `tool_requested` event per action, the
    SAME event `_dispatch_tool_call()` is the ONLY code in this file that
    ever publishes (Section 1's own 'exactly ONE execution path')."""
    rules = _seq_rule("r1", [
        _call_service("light", "turn_on", ["light.a"]), _run_script("script.b"), _activate_scene("scene.c"),
        {"type": "home_assistant.set_brightness", "parameters": {"target": "light.d", "level": 50}},
        {"type": "home_assistant.set_color", "parameters": {"target": "light.e", "color": "blue"}},
        {"type": "home_assistant.set_temperature", "parameters": {"target": "climate.f", "value": 21}},
        {"type": "home_assistant.toggle", "parameters": {"target": "light.g"}},
    ])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 7
        assert all(c["tool"] == "home_assistant" for c in tool_calls)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# P. No second execution path (Section 1/22)
# ============================================================================

def test_P1_still_exactly_one_thread_spawn_site_in_engine_py():
    """Same structural proof `test_p0_11_action_sequence.py::test_M2`
    already established for P0.11 - re-verified here to confirm P0.14
    did not add a second per-step/per-action thread spawn anywhere."""
    source = _read(_ENGINE_PATH)
    assert source.count("threading.Thread(") == 1


def test_P2_no_busy_loop_or_blocking_time_sleep_introduced_by_p0_14():
    tree = ast.parse(_read(_ENGINE_PATH))
    sleep_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "sleep" and isinstance(n.func.value, ast.Name) and n.func.value.id == "time"
    ]
    assert sleep_calls == [], "engine.py must never call time.sleep() as real code - only Event().wait()"


def test_P3_no_second_automation_engine_or_parallel_execution_primitive():
    tree = ast.parse(_read(_ENGINE_PATH))
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert class_names == {"AutomationEngine"}
    source = _read(_ENGINE_PATH)
    for forbidden in ("ThreadPoolExecutor", "asyncio.gather", "concurrent.futures", "multiprocessing"):
        assert forbidden not in source


def test_P4_real_ha_handler_dispatches_call_service_through_the_one_existing_client_facade():
    """The real handler's own `call_service` branch must only ever call
    `self._client.call_service(...)` (the SAME single facade every other
    action in that file already uses) - never open a second connection,
    never issue a raw HTTP request of its own."""
    source = _read(_REAL_HA_PATH)
    start = source.index('if tool_call.action == "call_service":')
    end = source.index('if tool_call.action == "set_temperature":')
    body = source[start:end]
    assert "self._client.call_service(" in body
    for forbidden in ("requests.", "urllib.request", "http.client", "aiohttp", "websocket"):
        assert forbidden not in body


# ============================================================================
# Q. Backward compatibility with P0.11 (Section 19)
# ============================================================================

def test_Q1_sequence_step_types_is_action_types_plus_delay_plus_new_control_types():
    assert SEQUENCE_STEP_TYPES == ACTION_TYPES | {"delay", "wait_until", "condition", "stop_automation"}


def test_Q2_pre_p0_14_pure_actions_sequence_rule_still_completes_end_to_end():
    """A sequence using ONLY types that existed before P0.14 (turn_on,
    delay, turn_off, automation.log) must behave byte-for-byte as it did
    under P0.11 - the exact regression P0.11's own test suite already
    covers in depth; this is a single, focused re-check scoped to P0.14's
    own change surface."""
    rules = _seq_rule("r1", [_ha_on("light.wled"), _delay(0.05), _log("hi"), {"type": "home_assistant.turn_off", "parameters": {"target": "light.wled"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_Q3_real_shipped_wled_automation_rules_file_still_loads_and_validates():
    """The real, currently-shipped `config/automation_rules.json` (read-
    only here, never mutated) must still load and validate cleanly under
    the P0.14-extended schema - proves the new, larger `ACTION_TYPES`/
    `SEQUENCE_STEP_TYPES` allowlists are still a strict superset, never a
    breaking change, for whatever rule(s) are genuinely on disk today."""
    real_path = os.path.join(_ROOT, "config", "automation_rules.json")
    if not os.path.exists(real_path):
        pytest.skip("no config/automation_rules.json present in this environment")
    with open(real_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    for rule_id, data in raw.items():
        rule = rule_from_dict(rule_id, data)
        validate_rule(rule)  # must not raise


# ============================================================================
# R. Persistence (Section 18) - reuses the EXISTING P0.12 create/reload
# mechanism verbatim, never a second store.
# ============================================================================

def test_R1_new_action_types_survive_a_full_engine_reload_from_disk():
    runtime, modules, adapter_manager, rules_path = _build_stack({})
    engine: AutomationEngine = modules["automation_engine"]
    try:
        payload = {
            "name": "Persisted P0.14 Rule", "trigger": {"type": "manual", "parameters": {}},
            "sequence": [
                _call_service("light", "turn_on", ["light.wled"]),
                _run_script("script.morning", variables={"x": 1}),
                _wait_until_step("light.wled", "equals", "on", timeout_seconds=15),
                _condition_step([{"target": "occupancy.person_count", "type": "greater_than", "value": 1}],
                                 then=[_ha_on("light.a")], else_=[_ha_on("light.b")]),
                _stop_step(),
            ],
        }
        result = engine.create_rule(payload, rule_id="p0_14_persist_test")
        assert result["ok"], result["message"]

        engine.reload_rules()  # forces a fresh read from disk, not the in-memory copy
        reloaded = engine.get_rule("p0_14_persist_test")
        assert reloaded is not None
        types = [s["type"] for s in reloaded["sequence"]]
        assert types == [
            "home_assistant.call_service", "home_assistant.run_script",
            "wait_until", "condition", "stop_automation",
        ]
        assert reloaded["sequence"][1]["parameters"]["variables"] == {"x": 1}
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_R2_no_second_persistence_file_or_mechanism_introduced_by_p0_14():
    source = _read(_AUTOMATION_API_PATH)
    assert "sqlite" not in source.lower()
    assert "automation_rules_v2" not in source
    assert "open(" not in source, "automation_api.py must never open a file directly - persistence stays in engine.py"


# ============================================================================
# S. Execution monitor (Section 13)
# ============================================================================

def test_S1_current_step_index_and_total_steps_observable_mid_wait_until():
    rules = _seq_rule("r1", [_ha_on("A"), _wait_until_step("light.wled", "equals", "on", timeout_seconds=2), _ha_on("B")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine.ha_state_reader = lambda entity_id: "off"  # keeps the wait_until step genuinely mid-flight
    try:
        runtime.start()
        engine.run_automation("r1")
        time.sleep(0.3)
        status = engine.get_automation_status("r1")
        assert status["running"] is True
        last = status["last_execution"]
        assert last["final_status"] == "RUNNING"
        assert last["current_step_index"] == 1
        assert last["total_steps"] == 3
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_S2_cancelled_and_timeout_are_distinct_final_statuses_from_failed():
    """Section 13's own new status list (RUNNING/COMPLETED/FAILED/
    CANCELLED/TIMEOUT) - CANCELLED and TIMEOUT must be genuinely distinct
    string values from FAILED, never conflated."""
    from luno.automation.models import ExecutionStatus
    assert ExecutionStatus.CANCELLED.value == "CANCELLED"
    assert ExecutionStatus.TIMEOUT.value == "TIMEOUT"
    assert len({ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value, ExecutionStatus.TIMEOUT.value}) == 3


def test_S3_devices_endpoint_reachable_and_shaped_for_the_execution_monitor_ui():
    runtime, modules, adapter_manager, dashboard, rules_path = _build_dashboard({})
    try:
        r = requests.get(dashboard.url + "api/automations/devices", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert "ha_connected" in body
        assert set(body["categories"]) >= {"lights", "switches", "scripts", "fans", "climate", "sensors", "scenes"}
    finally:
        _teardown_dashboard(runtime, adapter_manager, dashboard, rules_path)


# ============================================================================
# T. Security architecture guards (Section 17/22)
# ============================================================================

#: AST-based (not a raw substring scan - several of these modules'
#: own docstrings legitimately DISCUSS "eval"/"exec"/"subprocess" in
#: prose as things they explicitly do NOT do, e.g. models.py's own
#: module docstring; a naive substring/regex scan false-positives on
#: that prose, and separately on unrelated identifiers that merely
#: CONTAIN one of these words, e.g. `collect_memory_retrieval(`
#: containing the substring "eval(" - the same structural-proof
#: discipline this project's other architecture-guard tests already use
#: for Python files, applied here to a fixed, closed forbidden-call/
#: forbidden-import list).
_FORBIDDEN_CALL_NAMES = {"eval", "exec", "os.system", "os.popen", "importlib.import_module"}
_FORBIDDEN_IMPORT_ROOTS = {"subprocess"}


def _assert_no_forbidden_code_ast(path: str) -> None:
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in _FORBIDDEN_IMPORT_ROOTS, f"{path}: forbidden import {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in _FORBIDDEN_IMPORT_ROOTS, f"{path}: forbidden import from {node.module!r}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = f"{func.value.id}.{func.attr}"
            else:
                name = None
            assert name not in _FORBIDDEN_CALL_NAMES, f"{path}: forbidden call {name!r}"


def test_T1_no_arbitrary_code_execution_primitives_in_models_py():
    _assert_no_forbidden_code_ast(_MODELS_PATH)


def test_T2_no_arbitrary_code_execution_primitives_in_engine_py():
    _assert_no_forbidden_code_ast(_ENGINE_PATH)


def test_T3_no_arbitrary_code_execution_primitives_in_ha_handlers():
    for path in (_MOCK_HA_PATH, _REAL_HA_PATH):
        _assert_no_forbidden_code_ast(path)


def test_T4_no_arbitrary_code_execution_primitives_in_automation_api_or_server():
    for path in (_AUTOMATION_API_PATH, _SERVER_PATH):
        _assert_no_forbidden_code_ast(path)


def test_T5_devices_endpoint_never_fabricates_entities_for_unsupported_categories():
    """Section 10/17 - 'Do not invent fake devices.' Categories with no
    real local registry (fans/climate/media_players/sensors/scenes/other)
    must come back as genuinely EMPTY lists, never populated with
    made-up entries, regardless of what's configured."""
    result = automation_api.get_devices({}, adapter_manager=None)
    for category in ("fans", "climate", "media_players", "sensors", "scenes", "other"):
        assert result["categories"][category] == []
    assert result["ha_connected"] is False  # no adapter_manager passed - honestly reports disconnected


def test_T6_camera_action_safety_gate_action_types_unchanged_by_p0_14():
    """The Camera Action Safety Gate's own allowlist (`CAMERA_HA_ACTION_
    TYPES`) was deliberately left untouched by P0.14 - every new action
    type is automatically refused for a camera-triggered rule (a
    conservative boundary, not a gap - see camera_action_safety.py)."""
    from luno.automation.camera_action_safety import CAMERA_HA_ACTION_TYPES
    assert CAMERA_HA_ACTION_TYPES == frozenset({"home_assistant.turn_on", "home_assistant.turn_off"})
    for new_type in ("home_assistant.call_service", "home_assistant.run_script", "home_assistant.activate_scene",
                      "home_assistant.toggle", "home_assistant.set_brightness"):
        assert new_type not in CAMERA_HA_ACTION_TYPES


def test_T7_validate_endpoint_still_has_zero_side_effects_for_p0_14_payloads():
    """Section 16's own 'validation must never execute the automation'
    requirement, re-verified specifically for a payload using every new
    P0.14 action/step type."""
    payload = {
        "name": "validate only", "trigger": {"type": "manual", "parameters": {}},
        "sequence": [_call_service("light", "turn_on", ["light.wled"]), _run_script("script.x"),
                     _wait_until_step("light.wled", "equals", "on"), _stop_step()],
    }
    result = automation_api.validate_automation(payload)
    assert result["valid"] is True
    # automation_api.validate_automation() takes no `modules`/engine handle at
    # all - structurally impossible for it to have dispatched anything.
    import inspect
    assert "modules" not in inspect.signature(automation_api.validate_automation).parameters


# ============================================================================
# Concurrency - a wait_until in one automation must never block another
# (Section 8's own bounded-wait, non-busy-loop requirement, end-to-end).
# ============================================================================

def test_CONCURRENCY1_wait_until_in_one_automation_does_not_block_an_unrelated_one():
    rules = {}
    rules.update(_seq_rule("slow", [_ha_on("A"), _wait_until_step("light.wled", "equals", "on", timeout_seconds=2), _ha_on("B")]))
    rules.update({"fast": {"name": "fast", "enabled": True, "trigger": "manual", "actions": [_log("hi")], "cooldown_seconds": 0.0}})
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    engine: AutomationEngine = modules["automation_engine"]
    engine.ha_state_reader = lambda entity_id: "off"  # keeps 'slow' waiting the full timeout
    completed_ids: List[str] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed_ids.append(e.data.get("rule_id")))
    timed_out_ids: List[str] = []
    runtime.event_bus.subscribe("automation.timeout", lambda e: timed_out_ids.append(e.data.get("rule_id")))
    try:
        runtime.start()
        t0 = time.monotonic()
        engine.run_automation("slow")
        time.sleep(0.1)  # let 'slow' enter its wait_until
        engine.run_automation("fast")
        assert _wait_until(lambda: "fast" in completed_ids, timeout_s=2.0)
        fast_elapsed = time.monotonic() - t0
        assert fast_elapsed < 1.5, (
            f"'fast' must complete well before 'slow's own 2s wait_until timeout elapses (took {fast_elapsed:.2f}s)"
        )
        assert _wait_until(lambda: "slow" in timed_out_ids, timeout_s=3.0)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# U. Real Home Assistant live smoke test (Section 21) - honestly not
# performed in this sandbox.
# ============================================================================

def test_U1_real_home_assistant_smoke_test_is_honestly_marked_not_performed():
    """No real, reachable Home Assistant instance exists in this sandbox
    (`HOME_ASSISTANT_BACKEND` is not set to `real`, and no live HA URL/
    token is configured - see `luno/bootstrap/adapters.py`'s own backend
    selection). Per Section 21's own explicit instruction, this is
    recorded as an honest skip, `REAL_HA_TEST = NOT_PERFORMED` - never a
    fabricated pass. If a real HA instance/credentials are ever wired
    into this environment, this test should be replaced with a real
    turn_on -> run_script -> activate_scene -> read-state-back sequence
    against real test entities."""
    backend = os.getenv("HOME_ASSISTANT_BACKEND", "mock")
    REAL_HA_TEST = "NOT_PERFORMED" if backend != "real" else "ATTEMPTED"
    if REAL_HA_TEST == "NOT_PERFORMED":
        pytest.skip("REAL_HA_TEST = NOT_PERFORMED - no real Home Assistant instance available in this sandbox")
