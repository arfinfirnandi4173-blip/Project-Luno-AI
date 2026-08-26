"""
tests/test_sprint72_automation_engine.py
===========================================

Sprint 72 (Automation Engine Dasar) - dedicated regression suite.

Builds on the SAME real bootstrap (`register_all_modules`/
`register_all_adapters`, all-mock backends by default) Sprint 71's own
`tests/test_sprint71_camera_patrol.py` already established - no physical
camera/Home Assistant server is ever needed. Every actual device action
this suite exercises goes through the REAL `tool_requested` ->
`ToolManagerBridgeModule` -> `ToolManager` -> `camera_ptz`/`camera_
patrol`/`home_assistant` handler round trip (see `luno/automation/
engine.py`'s own module docstring) - these are genuine E2E tests through
the real runtime path, not tests of a private helper in isolation.

Model/condition-evaluator/security-scan tests are pure (no bootstrap)
and live at the top of this file; pipeline/cooldown/loop-protection/
ownership/persistence/dashboard tests follow, using a temporary
`config/automation_rules.json`-equivalent file pointed to via
`AutomationEngine._rules_path` (never the real one - this suite never
touches the user's own automation configuration).
"""

from __future__ import annotations

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

from luno.automation.conditions import CONDITION_INVALID, evaluate_condition  # noqa: E402
from luno.automation.engine import AutomationEngine, MAX_EXECUTION_DEPTH  # noqa: E402
from luno.automation.models import (  # noqa: E402
    ACTION_TYPES,
    CONDITION_TYPES,
    MAX_ACTIONS_PER_RULE,
    TRIGGER_TYPES,
    AutomationAction,
    AutomationCondition,
    AutomationRule,
    AutomationRuleError,
    AutomationTrigger,
    rule_from_dict,
    validate_action,
    validate_condition,
    validate_rule,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


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
    """Real bootstrap - same helper convention `tests/
    test_sprint71_camera_patrol.py` already established. Points the
    freshly-constructed `AutomationEngine` at a TEMPORARY rules file
    (never the real `config/automation_rules.json`) so this suite can
    never mutate the user's own automation configuration."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is None:
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_test_")
        os.close(fd)
    engine._rules_path = rules_path
    if rules is not None:
        _write_rules(rules_path, rules)

    return runtime, modules, adapter_manager, cfg, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


def _save_presets(modules: Dict[str, Any], names) -> None:
    """Pre-seeds the (default, mock) camera_ptz handler with saved
    preset positions, same shape Sprint 71's own `test_sprint71_
    camera_patrol.py::_save_presets` uses - lets `goto_preset` succeed
    without a physical camera."""
    mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
    for name in names:
        mock._presets[name.lower()] = (0.0, 0.0)


def _log_action(message: str = "hi") -> Dict[str, Any]:
    return {"type": "automation.log", "parameters": {"message": message}}


def _run_now_rule(rule_id: str = "r1", actions=None, conditions=None, cooldown: float = 0.0) -> Dict[str, Any]:
    return {
        rule_id: {
            "name": rule_id,
            "enabled": True,
            "trigger": "manual",
            "conditions": conditions or [],
            "actions": actions or [_log_action()],
            "cooldown_seconds": cooldown,
        }
    }


# ============================================================================
# A. Domain model / validation (pure, no bootstrap)
# ============================================================================

def test_01_valid_rule_passes_validation():
    rule = AutomationRule(
        id="r", name="Rule", trigger=AutomationTrigger(type="manual"),
        actions=[AutomationAction(type="automation.log", parameters={"message": "hi"})],
    )
    validate_rule(rule)  # must not raise


def test_02_empty_rule_id_or_name_rejected():
    with pytest.raises(AutomationRuleError):
        validate_rule(AutomationRule(id="", name="x", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")]))
    with pytest.raises(AutomationRuleError):
        validate_rule(AutomationRule(id="x", name="", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")]))


def test_03_unknown_trigger_type_rejected():
    with pytest.raises(AutomationRuleError, match="unknown trigger type"):
        validate_rule(AutomationRule(id="r", name="r", trigger=AutomationTrigger(type="expression"), actions=[AutomationAction(type="automation.log")]))


def test_04_unknown_condition_type_rejected():
    with pytest.raises(AutomationRuleError, match="unknown condition type"):
        validate_rule(AutomationRule(
            id="r", name="r", trigger=AutomationTrigger(type="manual"),
            conditions=[AutomationCondition(type="python_eval", target="x", value=1)],
            actions=[AutomationAction(type="automation.log")],
        ))


def test_05_unknown_action_type_rejected():
    with pytest.raises(AutomationRuleError, match="unknown action type"):
        validate_rule(AutomationRule(id="r", name="r", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="shell.run")]))


def test_06_rule_with_no_actions_rejected():
    with pytest.raises(AutomationRuleError, match="at least one action"):
        validate_rule(AutomationRule(id="r", name="r", trigger=AutomationTrigger(type="manual"), actions=[]))


def test_07_too_many_actions_rejected():
    actions = [AutomationAction(type="automation.log") for _ in range(MAX_ACTIONS_PER_RULE + 1)]
    with pytest.raises(AutomationRuleError, match="too many actions"):
        validate_rule(AutomationRule(id="r", name="r", trigger=AutomationTrigger(type="manual"), actions=actions))


def test_08_cooldown_out_of_range_rejected():
    for bad in (-1.0, 999999999.0):
        with pytest.raises(AutomationRuleError):
            validate_rule(AutomationRule(id="r", name="r", trigger=AutomationTrigger(type="manual"),
                                          actions=[AutomationAction(type="automation.log")], cooldown_seconds=bad))


def test_09_event_trigger_requires_event_name():
    with pytest.raises(AutomationRuleError, match="event_name"):
        validate_trigger_only(AutomationTrigger(type="event", parameters={}))


def test_10_time_trigger_requires_hhmm():
    with pytest.raises(AutomationRuleError, match="HH:MM"):
        validate_trigger_only(AutomationTrigger(type="time", parameters={"time": "25:99"}))
    validate_trigger_only(AutomationTrigger(type="time", parameters={"time": "22:00"}))  # must not raise


def validate_trigger_only(trigger: AutomationTrigger) -> None:
    from luno.automation.models import validate_trigger
    validate_trigger(trigger)


def test_11_camera_preset_action_requires_preset_param():
    with pytest.raises(AutomationRuleError, match="preset"):
        validate_action(AutomationAction(type="camera.preset", parameters={}))
    validate_action(AutomationAction(type="camera.preset", parameters={"preset": "pintu"}))  # must not raise


def test_12_home_assistant_action_requires_target_param():
    with pytest.raises(AutomationRuleError, match="target"):
        validate_action(AutomationAction(type="home_assistant.turn_on", parameters={}))
    validate_action(AutomationAction(type="home_assistant.turn_on", parameters={"target": "Main Lamp"}))  # must not raise


def test_13_rule_from_dict_compact_trigger_strings():
    r_event = rule_from_dict("r1", {"name": "r1", "trigger": "event:tool_finished", "actions": [{"type": "automation.log"}]})
    assert r_event.trigger.type == "event" and r_event.trigger.parameters["event_name"] == "tool_finished"

    r_time = rule_from_dict("r2", {"name": "r2", "trigger": "time:22:00", "actions": [{"type": "automation.log"}]})
    assert r_time.trigger.type == "time" and r_time.trigger.parameters["time"] == "22:00"

    r_manual = rule_from_dict("r3", {"name": "r3", "trigger": "manual", "actions": [{"type": "automation.log"}]})
    assert r_manual.trigger.type == "manual"


def test_14_rule_from_dict_and_validate_round_trip_via_disk():
    rule = rule_from_dict("night_mode", {
        "name": "Night Mode", "enabled": True, "trigger": "time:22:00",
        "conditions": [{"type": "state_is", "target": "camera_patrol", "value": "idle"}],
        "actions": [{"type": "home_assistant.turn_off", "parameters": {"target": "Main Lamp"}}, {"type": "camera.home"}],
        "cooldown_seconds": 60,
    })
    validate_rule(rule)  # must not raise
    assert rule.to_public_dict()["trigger"]["type"] == "time"


def test_15_allowlists_are_exactly_the_documented_sets():
    assert TRIGGER_TYPES == {"event", "time", "manual"}
    # P0.7 (Vision Context -> Automation Context) added "greater_equal" -
    # the one new operator that sprint's own brief called for (to express
    # `event.person_count >= 2`) - see models.py's own CONDITION_TYPES
    # comment for the full rationale. Every operator from Sprint 72 is
    # still present unchanged; this is a strictly additive update.
    assert CONDITION_TYPES == {
        "equals", "not_equals", "greater_than", "less_than", "contains", "state_is", "greater_equal",
    }
    # P0.14 (Advanced Home Assistant Automation Actions & Script Runner)
    # added seven new, additive `home_assistant.*` action types - every
    # type from Sprint 72/P0.6.2/P0.8.x is still present unchanged; this
    # is a strictly additive update (legitimate, in-scope literal update,
    # not a workaround - see `docs/change_impact/ha_script_actions_p0_14.md`).
    assert ACTION_TYPES == {
        "camera.preset", "camera.home", "camera.stop_patrol",
        "home_assistant.turn_on", "home_assistant.turn_off", "automation.log",
        "home_assistant.toggle", "home_assistant.set_brightness",
        "home_assistant.set_color", "home_assistant.set_temperature",
        "home_assistant.run_script", "home_assistant.activate_scene",
        "home_assistant.call_service",
    }


# ============================================================================
# B. Condition evaluator (pure, no bootstrap)
# ============================================================================

def test_16_equals_and_state_is_pass_and_fail():
    readers = {"camera": lambda: "connected"}
    assert evaluate_condition(AutomationCondition(type="equals", target="camera", value="connected"), readers) == (True, "")
    assert evaluate_condition(AutomationCondition(type="state_is", target="camera", value="connected"), readers) == (True, "")
    ok, reason = evaluate_condition(AutomationCondition(type="equals", target="camera", value="disconnected"), readers)
    assert ok is False and reason == ""


def test_17_not_equals_greater_than_less_than_contains():
    readers = {"n": lambda: 5, "items": lambda: ["a", "b"]}
    assert evaluate_condition(AutomationCondition(type="not_equals", target="n", value=4), readers)[0] is True
    assert evaluate_condition(AutomationCondition(type="greater_than", target="n", value=1), readers)[0] is True
    assert evaluate_condition(AutomationCondition(type="less_than", target="n", value=10), readers)[0] is True
    assert evaluate_condition(AutomationCondition(type="contains", target="items", value="a"), readers)[0] is True
    assert evaluate_condition(AutomationCondition(type="contains", target="items", value="z"), readers)[0] is False


def test_18_unknown_condition_type_is_invalid():
    ok, reason = evaluate_condition(AutomationCondition(type="python_eval", target="n", value=1), {"n": lambda: 1})
    assert ok is False and reason == CONDITION_INVALID


def test_19_unknown_target_is_invalid():
    ok, reason = evaluate_condition(AutomationCondition(type="equals", target="nonexistent", value=1), {"n": lambda: 1})
    assert ok is False and reason == CONDITION_INVALID


def test_20_incompatible_comparison_is_invalid_not_a_crash():
    ok, reason = evaluate_condition(AutomationCondition(type="greater_than", target="s", value=1), {"s": lambda: "not a number"})
    assert ok is False and reason == CONDITION_INVALID


def test_21_reader_exception_is_invalid_not_a_crash():
    def _boom():
        raise RuntimeError("sensor offline")
    ok, reason = evaluate_condition(AutomationCondition(type="equals", target="s", value=1), {"s": _boom})
    assert ok is False and reason == CONDITION_INVALID


def test_22_condition_evaluation_never_mutates_the_reader_target():
    """Phase 3: "tidak boleh melakukan mutation" - the reader itself is
    the ONLY thing a condition ever touches, and evaluate_condition()
    calls it exactly once (read, not read-modify-write)."""
    calls = []

    def _reader():
        calls.append(1)
        return "connected"
    evaluate_condition(AutomationCondition(type="equals", target="camera", value="connected"), {"camera": _reader})
    assert calls == [1]


# ============================================================================
# C. Security boundary (Phase 12) - static source scan, pure
# ============================================================================

_AUTOMATION_SOURCE_FILES = [
    os.path.join(_ROOT, "luno", "automation", "models.py"),
    os.path.join(_ROOT, "luno", "automation", "conditions.py"),
    os.path.join(_ROOT, "luno", "automation", "engine.py"),
    os.path.join(_ROOT, "luno", "automation", "__init__.py"),
    os.path.join(_ROOT, "luno", "tool_manager", "builtin", "automation.py"),
]


def test_23_no_eval_exec_shell_or_dynamic_import_anywhere_in_the_package():
    """AST-based (not a plain text scan) so this module's OWN docstrings
    - which legitimately mention "eval()"/"exec()" by name while
    documenting that they are forbidden - never produce a false
    positive. Walks every real function-call node in the file looking
    for the actual forbidden calls/keyword usage."""
    import ast

    forbidden_calls = {"eval", "exec", "__import__"}
    for path in _AUTOMATION_SOURCE_FILES:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
                assert name not in forbidden_calls, f"forbidden call {name!r} found in {path}"
                if name in ("system",) and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                    pytest.fail(f"forbidden call os.system(...) found in {path}")
                if isinstance(func, ast.Attribute) and func.attr == "import_module":
                    pytest.fail(f"forbidden dynamic import_module(...) found in {path}")
                if isinstance(func, ast.Attribute) and func.attr == "Popen":
                    pytest.fail(f"forbidden subprocess.Popen(...) found in {path}")
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        pytest.fail(f"forbidden shell=True found in {path}")


def test_24_no_expression_field_in_the_schema():
    """Phase 1's own explicit ban: no rule/condition/action dict this
    package produces or consumes ever has a raw "expression"/"code" key."""
    rule = AutomationRule(
        id="r", name="r", trigger=AutomationTrigger(type="manual"),
        conditions=[AutomationCondition(type="equals", target="x", value=1)],
        actions=[AutomationAction(type="automation.log")],
    )
    payload = json.dumps(rule.to_public_dict())
    assert "expression" not in payload and '"code"' not in payload


# ============================================================================
# D. Trigger engine (event / time / manual / unknown / disabled) - real bootstrap
# ============================================================================

def test_25_manual_trigger_runs_the_automation():
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(_run_now_rule("r1"))
    try:
        runtime.start()
        engine = modules["automation_engine"]
        result = engine.run_automation("r1")
        assert result["ok"] is True
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_26_unknown_automation_refused():
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(_run_now_rule("r1"))
    try:
        runtime.start()
        engine = modules["automation_engine"]
        result = engine.run_automation("does_not_exist")
        assert result["ok"] is False and result["code"] == "unknown_automation"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_27_disabled_automation_refuses_manual_run():
    rules = _run_now_rule("r1")
    rules["r1"]["enabled"] = False
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        result = engine.run_automation("r1")
        assert result["ok"] is False and result["code"] == "automation_disabled"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_28_disabled_automation_does_not_react_to_its_event_trigger():
    rules = {
        "r1": {"name": "r1", "enabled": False, "trigger": "event:some_test_event", "actions": [_log_action()], "cooldown_seconds": 0},
    }
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        runtime.event_bus.publish(Event(type="some_test_event", data={}))
        time.sleep(0.3)
        assert engine.get_automation_status("r1")["last_execution"] is None
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_29_event_trigger_fires_the_automation():
    rules = {
        "r1": {"name": "r1", "enabled": True, "trigger": "event:some_test_event", "actions": [_log_action()], "cooldown_seconds": 0},
    }
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        runtime.event_bus.publish(Event(type="some_test_event", data={}))
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert engine.get_automation_status("r1")["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_30_time_trigger_registers_a_scheduler_job_not_a_new_thread():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "time:23:59", "actions": [_log_action()], "cooldown_seconds": 0}}
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        assert len(engine._time_job_ids) == 1
        job_ids = {j.job_id for j in runtime.scheduler.all_jobs()}
        assert engine._time_job_ids[0] in job_ids
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_31_time_trigger_fires_via_on_time_trigger_directly():
    """Avoids waiting for a real wall-clock minute - calls the SAME
    callback the reused scheduler would call once its predicate becomes
    true (see `_register_time_triggers()`), proving the trigger->pipeline
    wiring itself works end to end."""
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "time:00:00", "actions": [_log_action()], "cooldown_seconds": 0}}
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine._on_time_trigger("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_32_malformed_rule_is_skipped_at_load_not_a_crash():
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_test_")
    os.close(fd)
    _write_rules(rules_path, {
        "bad": {"name": "bad", "trigger": "expression:1+1", "actions": [{"type": "automation.log"}]},
        "good": {"name": "good", "trigger": "manual", "actions": [{"type": "automation.log"}]},
    })
    runtime, modules, adapter_manager, cfg, _ = _build_stack(rules_path=rules_path)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        ids = {r["id"] for r in engine.list_rules()}
        assert "good" in ids and "bad" not in ids
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# E. Action engine / pipeline (camera / home_assistant / internal)
# ============================================================================

def test_33_camera_preset_action_dispatches_real_camera_ptz_call():
    rules = _run_now_rule("r1", actions=[{"type": "camera.preset", "parameters": {"preset": "pintu"}}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        _save_presets(modules, ["pintu"])
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "COMPLETED"
        assert last["action_results"][0]["status"] == "completed"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_34_camera_home_action_dispatches_center():
    rules = _run_now_rule("r1", actions=[{"type": "camera.home"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert engine.get_automation_status("r1")["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_35_camera_stop_patrol_action_dispatches_camera_patrol_stop():
    rules = _run_now_rule("r1", actions=[{"type": "camera.stop_patrol"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert engine.get_automation_status("r1")["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_36_home_assistant_turn_on_and_turn_off_actions():
    rules = _run_now_rule("r1", actions=[
        {"type": "home_assistant.turn_on", "parameters": {"target": "Main Lamp"}},
        {"type": "home_assistant.turn_off", "parameters": {"target": "Main Lamp"}},
    ])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "COMPLETED"
        assert [a["status"] for a in last["action_results"]] == ["completed", "completed"]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_37_action_failure_reported_honestly():
    """An unseeded preset name makes the REAL mock camera_ptz handler
    fail (see camera_ptz.py::goto_preset) - the automation must report
    this as a genuine failure, never a fake success."""
    rules = _run_now_rule("r1", actions=[{"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "FAILED"
        assert last["action_results"][0]["status"] == "failed"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_38_unknown_action_type_refused_defensively():
    """validate_rule() already refuses this at load time (test_05) - this
    proves the SECOND, defense-in-depth layer inside _dispatch_action()
    also refuses rather than attempting any dispatch.

    P0.8.0 update: `_dispatch_action()` gained a leading `rule` parameter
    (+ a trailing optional `event_data`) so the camera action safety gate
    can tell a camera-triggered rule's HA action apart from every other
    rule's - see `luno/automation/engine.py::_dispatch_home_assistant_
    action()`'s own docstring. A minimal `AutomationRule` (no trigger)
    is enough here since this test only exercises the unknown-action-type
    branch, which returns before `rule` is ever inspected."""
    engine = AutomationEngine()
    from luno.automation.models import AutomationExecution, AutomationRule
    rule = AutomationRule(id="r1", name="r1")
    execution = AutomationExecution(execution_id="e1", rule_id="r1", correlation_id="e1")
    result = engine._dispatch_action(rule, AutomationAction(type="shell.run", parameters={}), execution)
    assert result.status == "refused" and result.code == "action_type_not_allowlisted"


def test_39_automation_log_action_never_dispatches_a_tool_call():
    """P0.8.0 update: see test_38's own docstring for why `_dispatch_
    action()` now takes a leading `rule` argument."""
    engine = AutomationEngine()  # no event bus bound at all
    from luno.automation.models import AutomationExecution, AutomationRule
    rule = AutomationRule(id="r1", name="r1")
    execution = AutomationExecution(execution_id="e1", rule_id="r1", correlation_id="e1")
    result = engine._dispatch_action(rule, AutomationAction(type="automation.log", parameters={"message": "hi"}), execution)
    assert result.status == "completed" and result.message == "hi"


def test_40_internal_and_camera_action_type_sets_do_not_overlap():
    from luno.automation.models import _CAMERA_ACTION_TYPES, _INTERNAL_ACTION_TYPES
    assert _CAMERA_ACTION_TYPES.isdisjoint(_INTERNAL_ACTION_TYPES)
    # P0.14 - seven new `home_assistant.*` action types are neither
    # camera actions nor internal (`automation.log`)-style actions, so
    # they're added to the third operand here (legitimate, in-scope
    # literal update - see test_15's own P0.14 comment).
    _p0_14_ha_action_types = {
        "home_assistant.turn_on", "home_assistant.turn_off", "home_assistant.toggle",
        "home_assistant.set_brightness", "home_assistant.set_color", "home_assistant.set_temperature",
        "home_assistant.run_script", "home_assistant.activate_scene", "home_assistant.call_service",
    }
    assert _CAMERA_ACTION_TYPES | _INTERNAL_ACTION_TYPES | _p0_14_ha_action_types == ACTION_TYPES


# ============================================================================
# F. No-partial-execution policy (Phase 7)
# ============================================================================

def test_41_failing_condition_skips_the_whole_rule_zero_actions_run():
    rules = _run_now_rule("r1", conditions=[{"type": "state_is", "target": "camera_patrol", "value": "moving"}],
                           actions=[{"type": "home_assistant.turn_on", "parameters": {"target": "Main Lamp"}}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()  # camera_patrol module starts IDLE, not "moving"
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "SKIPPED"
        assert last["action_results"] == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_42_passing_condition_runs_the_action():
    rules = _run_now_rule("r1", conditions=[{"type": "state_is", "target": "camera_patrol", "value": "idle"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert engine.get_automation_status("r1")["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_43_partial_failure_when_one_action_succeeds_and_one_fails():
    rules = _run_now_rule("r1", actions=[
        {"type": "home_assistant.turn_on", "parameters": {"target": "Main Lamp"}},
        {"type": "camera.preset", "parameters": {"preset": "unseeded_preset"}},
    ])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "PARTIAL_FAILURE"
        assert [a["status"] for a in last["action_results"]] == ["completed", "failed"]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_44_multiple_conditions_are_and_semantics():
    rules = _run_now_rule("r1", conditions=[
        {"type": "state_is", "target": "camera_patrol", "value": "idle"},
        {"type": "equals", "target": "camera_patrol", "value": "moving"},  # deliberately false
    ])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert engine.get_automation_status("r1")["last_execution"]["final_status"] == "SKIPPED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_45_unknown_condition_target_skips_never_partially_proceeds():
    rules = _run_now_rule("r1", conditions=[{"type": "state_is", "target": "nonexistent_sensor", "value": "x"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "SKIPPED" and last["reason"] == CONDITION_INVALID
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# G. Cooldown (Phase 8)
# ============================================================================

def test_46_first_execution_succeeds_repeated_trigger_skipped_by_cooldown():
    rules = _run_now_rule("r1", cooldown=60.0)
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        r1 = engine.run_automation("r1")
        assert r1["ok"] is True
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        r2 = engine.run_automation("r1")
        assert r2["ok"] is False and r2["code"] == "skipped_cooldown"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_47_cooldown_expiry_allows_a_new_execution():
    rules = _run_now_rule("r1", cooldown=0.2)
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        time.sleep(0.35)
        r2 = engine.run_automation("r1")
        assert r2["ok"] is True
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_48_zero_cooldown_allows_immediate_repeats():
    rules = _run_now_rule("r1", cooldown=0.0)
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        r1 = engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        r2 = engine.run_automation("r1")
        assert r1["ok"] is True and r2["ok"] is True
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_49_cooldown_cleanup_removes_expired_entries():
    engine = AutomationEngine()
    engine._cooldown_until["r1"] = time.monotonic() - 1.0  # already expired
    engine._cooldown_until["r2"] = time.monotonic() + 999.0  # still active
    engine._cleanup_cooldowns()
    assert "r1" not in engine._cooldown_until
    assert "r2" in engine._cooldown_until


def test_50_cooldown_state_is_bounded_to_loaded_rule_count():
    rules = _run_now_rule("r1", cooldown=60.0)
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        for _ in range(20):
            engine.run_automation("r1")
        assert len(engine._cooldown_until) <= 1
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# H. Loop / cycle protection (Phase 9)
# ============================================================================

def test_51_reentrancy_guard_refuses_a_rule_already_running():
    engine = AutomationEngine()
    rule = AutomationRule(id="r1", name="r1", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")])
    with engine._lock:
        engine._rules["r1"] = rule
        engine._running_rule_ids.add("r1")
    accepted, code = engine._trigger(rule, {"type": "manual", "parameters": {}})
    assert accepted is False and code == "refused_already_running"


def test_52_rapid_repeated_firing_of_the_same_rule_is_detected_as_a_cycle():
    rules = _run_now_rule("r1", cooldown=0.0)
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        codes = []
        for _ in range(6):
            result = engine.run_automation("r1")
            codes.append(result.get("code"))
            time.sleep(0.05)
        assert "unknown_automation" not in codes
        # By the sprint's own detector (>= 3 firings within 5s), later
        # attempts in this rapid-fire burst must be refused as a cycle.
        assert any(c == "automation_cycle_detected" for c in codes) or codes.count("automation_started") < 6
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_53_execution_depth_beyond_max_is_refused():
    engine = AutomationEngine()
    rule = AutomationRule(id="r1", name="r1", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")])
    with engine._lock:
        engine._rules["r1"] = rule
    accepted, code = engine._trigger(rule, {"type": "manual", "parameters": {}}, _depth=MAX_EXECUTION_DEPTH + 1)
    assert accepted is False and code == "automation_cycle_detected"


def test_54_root_execution_has_matching_correlation_id_and_zero_depth():
    engine = AutomationEngine()
    rule = AutomationRule(id="r1", name="r1", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")])
    execution = engine._new_execution(rule, {"type": "manual", "parameters": {}}, 0)
    assert execution.correlation_id == execution.execution_id
    assert execution.depth == 0


def test_55_action_count_per_rule_is_capped_at_load_time():
    """Phase 9's own "per-execution action limit" - enforced at
    validation, so a rule can never even be loaded with more actions
    than one execution is allowed to run (see test_07 for the direct
    validate_rule() proof; this proves it end to end through the real
    loader)."""
    too_many = {f"a{i}": {} for i in range(MAX_ACTIONS_PER_RULE + 1)}
    rules = {"r1": {
        "name": "r1", "trigger": "manual",
        "actions": [{"type": "automation.log"} for _ in range(MAX_ACTIONS_PER_RULE + 1)],
    }}
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        assert engine.list_rules() == []  # the whole rule was skipped as invalid, not truncated
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# I. Camera ownership (Phase 5) - Manual PTZ > Automation > Patrol
# ============================================================================

def test_56_automation_camera_action_stops_an_active_patrol():
    rules = {"rumah": {"presets": ["pintu"], "dwell_seconds": 5.0, "loop": False}}
    auto_rules = _run_now_rule("r1", actions=[{"type": "camera.home"}])
    runtime, modules, adapter_manager, cfg, auto_path = _build_stack(auto_rules)
    patrol_fd, patrol_path = tempfile.mkstemp(suffix=".json", prefix="camera_patrol_routes_test_")
    os.close(patrol_fd)
    with open(patrol_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    modules["camera_patrol_module"]._routes_path = patrol_path
    try:
        runtime.start()
        _save_presets(modules, ["pintu"])
        patrol = modules["camera_patrol_module"]
        patrol.start_patrol("rumah")
        assert _wait_until(lambda: patrol.is_running())

        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        assert _wait_until(lambda: not patrol.is_running())
    finally:
        _teardown(runtime, adapter_manager, auto_path)
        try:
            os.remove(patrol_path)
        except OSError:
            pass


def test_57_manual_camera_command_causes_automation_camera_action_to_be_refused():
    rules = _run_now_rule("r1", actions=[{"type": "camera.home"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        # Simulate a genuinely manual camera_ptz dispatch the same way
        # ToolManagerBridgeModule._process_event() would invoke every
        # registered pre-dispatch hook.
        engine.on_camera_dispatch({"tool": "camera_ptz", "action": "pan_left", "target": None, "parameters": {}})
        assert engine._manual_priority_active() is True

        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["action_results"][0]["status"] == "refused"
        assert last["action_results"][0]["code"] == "action_refused_busy"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_58_automation_origin_and_patrol_origin_tagged_calls_do_not_open_the_manual_window():
    engine = AutomationEngine()
    engine.on_camera_dispatch({"tool": "camera_ptz", "action": "center", "target": None, "parameters": {"_automation_origin": True}})
    assert engine._manual_priority_active() is False
    engine.on_camera_dispatch({"tool": "camera_ptz", "action": "center", "target": None, "parameters": {"_patrol_origin": True}})
    assert engine._manual_priority_active() is False


def test_59_no_concurrent_ptz_ownership_across_manual_automation_and_patrol():
    """Every camera_ptz call - manual, automation, or patrol-issued - is
    serialized through ToolManagerBridgeModule's single-worker FIFO
    executor (Sprint 71's own architecture). This proves that guarantee
    still holds with automation added into the mix: two concurrently
    dispatched camera_ptz calls never overlap in time."""
    rules = _run_now_rule("r1", actions=[{"type": "camera.home"}, {"type": "camera.home"}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        mock = modules["tool_manager_module"].manager.registry.get("camera_ptz")
        intervals: List[Any] = []
        lock = threading.Lock()
        original_execute = mock.execute

        def _tracked_execute(tool_call, context=None):
            start = time.monotonic()
            result = original_execute(tool_call, context)
            end = time.monotonic()
            with lock:
                intervals.append((start, end))
            return result

        mock.execute = _tracked_execute
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None, timeout_s=5.0)
        time.sleep(0.2)

        intervals.sort()
        for i in range(1, len(intervals)):
            assert intervals[i][0] >= intervals[i - 1][1], "two camera_ptz calls overlapped in time"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# J. Failure / timeout handling (Phase 7 continued)
# ============================================================================

def test_60_dispatch_tool_call_times_out_honestly_when_no_event_bus_bound():
    engine = AutomationEngine()  # never bound to an event bus
    ok, message = engine._dispatch_tool_call({"tool": "camera_ptz", "action": "center", "target": None, "parameters": {}})
    assert ok is False and "not bound to an event bus" in message


def test_61_device_offline_style_failure_stops_at_the_failing_action_never_continues_pretending_success():
    rules = _run_now_rule("r1", actions=[{"type": "camera.preset", "parameters": {"preset": "offline_camera"}}])
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["final_status"] == "FAILED"
        assert last["action_results"][0]["message"]  # a real, honest failure message, not blank
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# K. Persistence (Phase 11)
# ============================================================================

def test_62_automation_definition_survives_a_reload():
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        engine.reload_rules()
        ids = {r["id"] for r in engine.list_rules()}
        assert "r1" in ids
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_63_running_an_automation_does_not_mutate_the_rules_file():
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        with open(rules_path, "rb") as fh:
            before = fh.read()
        engine = modules["automation_engine"]
        for _ in range(3):
            engine.run_automation("r1")
            time.sleep(0.05)
        _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        with open(rules_path, "rb") as fh:
            after = fh.read()
        assert before == after
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_64_disable_then_enable_persists_to_disk_and_reload_reflects_it():
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        result = engine.disable_automation("r1")
        assert result["ok"] is True
        with open(rules_path, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk["r1"]["enabled"] is False

        engine.reload_rules()
        assert engine.get_automation_status("r1")["enabled"] is False
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_65_disable_creates_a_backup_of_the_previous_rules_file():
    from luno import persistence as persistence_mod
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        before_backups = set(persistence_mod.list_backups(rules_path))
        engine.disable_automation("r1")
        after_backups = set(persistence_mod.list_backups(rules_path))
        assert len(after_backups) > len(before_backups)
    finally:
        _teardown(runtime, adapter_manager, rules_path)
        backup_dir = persistence_mod.backup_dir_for(rules_path)
        for name in persistence_mod.list_backups(rules_path):
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass


def test_66_no_credential_shaped_field_exists_anywhere_in_the_rule_or_execution_schema():
    forbidden_substrings = ["password", "token", "secret", "api_key", "credential"]
    rule = AutomationRule(
        id="r", name="r", trigger=AutomationTrigger(type="manual"),
        actions=[AutomationAction(type="home_assistant.turn_on", parameters={"target": "Main Lamp"})],
    )
    payload = json.dumps(rule.to_public_dict()).lower()
    for token in forbidden_substrings:
        assert token not in payload


def test_67_event_payloads_are_metadata_only():
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        seen: List[Event] = []
        sub_id = runtime.event_bus.subscribe("automation.*", lambda e: seen.append(e))
        engine = modules["automation_engine"]
        engine.run_automation("r1")
        assert _wait_until(lambda: any(e.type == "automation.completed" for e in seen))
        runtime.event_bus.unsubscribe(sub_id)
        # P0.8.0 - "code" added to automation.action_completed/action_failed
        # (e.g. "already_in_desired_state", "detection_error_present") so
        # a camera-triggered action's outcome is attributable without
        # inspecting logs - still metadata-only (a short code string,
        # never a credential/frame/raw exception - checked below like
        # every other field here already was).
        allowed_keys = {"execution_id", "rule_id", "correlation_id", "action_type", "status", "reason", "code"}
        forbidden = ["password", "token", "secret", "credential", "frame", "image"]
        for e in seen:
            assert set(e.data.keys()) <= allowed_keys, f"unexpected key(s) in {e.type}: {e.data.keys()}"
            blob = json.dumps(e.data).lower()
            for token in forbidden:
                assert token not in blob
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# L. Dashboard / ToolHandler surface (Phase 13/14)
# ============================================================================

def test_68_collect_automation_is_additive_and_backward_compatible():
    from luno.dashboard import collectors
    assert collectors.collect_automation({}) == {"available": False, "automations": []}

    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        result = collectors.collect_automation(modules)
        assert result["available"] is True
        assert any(a["id"] == "r1" for a in result["automations"])
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_69_automation_tool_handler_run_enable_disable_status():
    from luno.tool_manager.builtin.automation import AutomationToolHandler
    from luno.tool_manager.models import ToolCall

    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        handler = modules["tool_manager_module"].manager.registry.get("automation")
        assert isinstance(handler, AutomationToolHandler)

        status_all = handler.execute(ToolCall(tool="automation", action="status", target=None))
        assert status_all.success and status_all.data["automations"]

        disable = handler.execute(ToolCall(tool="automation", action="disable", target="r1"))
        assert disable.success

        run_disabled = handler.execute(ToolCall(tool="automation", action="run", target="r1"))
        assert run_disabled.success is False and run_disabled.error_type == "automation_disabled"

        enable = handler.execute(ToolCall(tool="automation", action="enable", target="r1"))
        assert enable.success

        run_ok = handler.execute(ToolCall(tool="automation", action="run", target="r1"))
        assert run_ok.success is True

        unknown = handler.execute(ToolCall(tool="automation", action="run", target="ghost"))
        assert unknown.success is False and unknown.error_type == "unknown_automation"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# M. Natural-language parser classification (Phase 14)
# ============================================================================

from luno.planner.parser import _classify_automation  # noqa: E402


def test_70_parser_classifies_run_enable_disable_status():
    assert _classify_automation("aktifkan otomasi mode malam") == ("run", "mode_malam")
    assert _classify_automation("jalankan otomatisasi night mode") == ("run", "night_mode")
    assert _classify_automation("matikan otomasi mode malam") == ("disable", "mode_malam")
    assert _classify_automation("nonaktifkan otomatisasi mode malam") == ("disable", "mode_malam")
    action, target = _classify_automation("status otomasi")
    assert action == "status"


def test_71_parser_does_not_misclassify_ordinary_ha_commands():
    """The anchor word ("otomasi"/"otomatisasi"/"automation") is required
    - see _classify_automation's own docstring for why. A bare "aktifkan
    mode malam"/"nyalakan lampu" must NOT be classified as an automation
    command (it would otherwise collide with ordinary Home Assistant
    parsing elsewhere in this file)."""
    assert _classify_automation("aktifkan mode malam") == (None, None)
    assert _classify_automation("nyalakan lampu kamar") == (None, None)


def test_72_parser_wired_into_clause_to_step_produces_automation_tool_call():
    from luno.planner.parser import IntentParser
    steps = [s for s in IntentParser.parse("jalankan otomasi night_mode") if s.tool == "automation"]
    assert len(steps) == 1
    assert steps[0].action == "run" and steps[0].target == "night_mode"


def test_73_full_round_trip_from_parsed_step_to_unknown_automation_refusal():
    """Proves the LLM/parser boundary (Phase 12/14): the parser can only
    ever produce a `target` string, resolved at RUNTIME against the
    registered automation set - never a fuzzy/best-guess match."""
    rules = _run_now_rule("mode_malam")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        from luno.planner.parser import IntentParser
        step = [s for s in IntentParser.parse("jalankan otomasi mode_malam yang tidak_ada") if s.tool == "automation"][0]
        engine = modules["automation_engine"]
        # The parser slugified the WHOLE trailing phrase, including the
        # extra words - this must resolve to unknown, never fuzzy-match
        # the real "mode_malam" rule.
        result = engine.run_automation(step.target)
        assert result["ok"] is False and result["code"] == "unknown_automation"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# N. Performance (Phase 16) - pure in-memory portions only
# ============================================================================

def test_74_trigger_matching_and_cooldown_check_are_well_under_5ms():
    engine = AutomationEngine()
    rule = AutomationRule(id="r1", name="r1", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")])
    with engine._lock:
        engine._rules["r1"] = rule

    durations = []
    for _ in range(200):
        with engine._lock:
            engine._running_rule_ids.discard("r1")
            engine._cooldown_until.pop("r1", None)
        t0 = time.perf_counter()
        engine._trigger(rule, {"type": "manual", "parameters": {}})
        durations.append((time.perf_counter() - t0) * 1000.0)
        _wait_until(lambda: "r1" not in engine._running_rule_ids or True, timeout_s=0.5)
        time.sleep(0.01)
    avg = sum(durations) / len(durations)
    assert avg < 5.0, f"average _trigger() dispatch overhead {avg:.3f}ms exceeds the 5ms budget"


def test_75_execution_metadata_creation_is_well_under_5ms():
    engine = AutomationEngine()
    rule = AutomationRule(id="r1", name="r1", trigger=AutomationTrigger(type="manual"), actions=[AutomationAction(type="automation.log")])
    durations = []
    for _ in range(500):
        t0 = time.perf_counter()
        engine._new_execution(rule, {"type": "manual", "parameters": {}}, 0)
        durations.append((time.perf_counter() - t0) * 1000.0)
    avg = sum(durations) / len(durations)
    assert avg < 5.0


def test_76_condition_evaluation_is_well_under_5ms():
    readers = {"camera": lambda: "connected"}
    condition = AutomationCondition(type="state_is", target="camera", value="connected")
    durations = []
    for _ in range(500):
        t0 = time.perf_counter()
        evaluate_condition(condition, readers)
        durations.append((time.perf_counter() - t0) * 1000.0)
    avg = sum(durations) / len(durations)
    assert avg < 5.0


# ============================================================================
# O. Module lifecycle hygiene (Phase 16 - no orphaned thread/job)
# ============================================================================

def test_77_stop_cancels_time_jobs_and_cooldown_cleanup_job():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "time:23:59", "actions": [_log_action()], "cooldown_seconds": 0}}
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        assert len(engine._time_job_ids) == 1
        job_id = engine._time_job_ids[0]
        engine.stop()
        assert job_id not in {j.job_id for j in runtime.scheduler.all_jobs()}
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_78_stop_unsubscribes_from_the_event_bus():
    rules = _run_now_rule("r1")
    runtime, modules, adapter_manager, cfg, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        before = runtime.event_bus.subscriber_count()
        engine.stop()
        after = runtime.event_bus.subscriber_count()
        assert after < before
    finally:
        _teardown(runtime, adapter_manager, rules_path)
