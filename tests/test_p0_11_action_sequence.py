"""
tests/test_p0_11_action_sequence.py
======================================

LUNO P0.11 (Action Sequence Engine) - dedicated regression suite.

`AutomationRule` gained a new, additive `sequence` field (mutually
exclusive with the pre-existing `actions` field) - an ORDERED list of
steps, each reusing `AutomationAction`'s own `{type, parameters}` shape
verbatim: either an existing `ACTION_TYPES` member (dispatched through
the EXACT SAME `AutomationEngine._dispatch_action()` -> `_dispatch_tool_
call()` -> `tool_requested` -> ToolManager round trip every pre-existing
action already uses) or the new `"delay"` pseudo-type (`{"type": "delay",
"seconds": N}`), which pauses ONLY the calling execution's own dedicated
thread (`_wait_delay()`, a plain non-busy-loop `threading.Event().wait()`
- never the AutomationEngine itself, never another execution).

A sequence stops at the FIRST failing step (Section 7 of the brief -
deliberately different from the legacy `actions` list's own "run every
action regardless, then classify COMPLETED/PARTIAL_FAILURE/FAILED"
policy, which remains completely unmodified for every existing rule).

Nothing about `_run_actions()`/`_verify_and_finalize()`/`_dispatch_
action()`/`_dispatch_tool_call()`/cooldown/loop-protection/camera
ownership/the P0.8.9 delayed-HA-action mechanism was modified - every
legacy `actions`-based rule (there is no existing rule anywhere with a
`sequence` field, since it did not exist before this sprint) is
byte-for-byte unaffected.

Sections (see the P0.11 brief's own Section 17 minimum-coverage list):
  A. Schema
  B. Device actions
  C. Ordering
  D. Delay
  E. Failure
  F. Execution state
  G. Logging
  H. ToolManager
  I. Backward compatibility
  J. Concurrent execution
  K. Cancellation (documents the honest absence - see Known Limitations)
  L. Regression (verified by this sprint's own regression sweep, not
     duplicated here - same convention every prior P0.x suite in this
     project already established)
  M. Architecture guards (Section 18 of the brief)
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
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

from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import (  # noqa: E402
    MAX_DELAY_SECONDS,
    MAX_SEQUENCE_STEPS,
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
_VISION_PATH = os.path.join(_ROOT, "luno", "vision.py")
_VISION_OCC_PATH = os.path.join(_ROOT, "luno", "vision_occupancy.py")
_ADAPTERS_VISION_PATH = os.path.join(_ROOT, "luno", "adapters", "vision.py")


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
    """Same real-bootstrap convention `tests/test_sprint72_automation_
    engine.py::_build_stack` already established - all-mock backends, a
    TEMPORARY rules file (never the real `config/automation_rules.json`)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is None:
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_11_automation_rules_test_")
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


def _seq_rule(rule_id: str, sequence, cooldown: float = 0.0, enabled: bool = True) -> Dict[str, Any]:
    return {rule_id: {
        "name": rule_id, "enabled": enabled, "trigger": "manual",
        "sequence": sequence, "cooldown_seconds": cooldown,
    }}


def _ha_on(target: str) -> Dict[str, Any]:
    return {"type": "home_assistant.turn_on", "parameters": {"target": target}}


def _ha_off(target: str) -> Dict[str, Any]:
    return {"type": "home_assistant.turn_off", "parameters": {"target": target}}


def _delay(seconds) -> Dict[str, Any]:
    return {"type": "delay", "seconds": seconds}


def _log(message: str = "x") -> Dict[str, Any]:
    return {"type": "automation.log", "parameters": {"message": message}}


# ============================================================================
# A. Schema
# ============================================================================

def test_A1_valid_sequence_rule_loads_and_validates():
    rule = rule_from_dict("r", {
        "name": "r", "trigger": "manual",
        "sequence": [_ha_on("light.wled"), _delay(1), _ha_off("light.wled")],
    })
    validate_rule(rule)  # must not raise
    assert [s.type for s in rule.sequence] == ["home_assistant.turn_on", "delay", "home_assistant.turn_off"]
    assert rule.sequence[1].parameters == {"seconds": 1}


def test_A2_empty_sequence_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": []})
    with pytest.raises(AutomationRuleError, match="at least one action"):
        validate_rule(rule)


def test_A3_malformed_sequence_step_missing_type_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [{"parameters": {}}]})
    with pytest.raises(AutomationRuleError, match="unknown step type"):
        validate_rule(rule)


def test_A4_unknown_step_type_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [{"type": "shell.run"}]})
    with pytest.raises(AutomationRuleError, match="unknown step type"):
        validate_rule(rule)


def test_A5_both_actions_and_sequence_rejected():
    rule = AutomationRule(
        id="r", name="r", trigger=AutomationTrigger(type="manual"),
        actions=[AutomationAction(type="automation.log")],
        sequence=[AutomationAction(type="automation.log")],
    )
    with pytest.raises(AutomationRuleError, match="not define both"):
        validate_rule(rule)


def test_A6_malformed_delay_missing_seconds_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [{"type": "delay"}]})
    with pytest.raises(AutomationRuleError, match="must be a number"):
        validate_rule(rule)


def test_A7_negative_delay_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [_delay(-4)]})
    with pytest.raises(AutomationRuleError, match="negative delay not allowed"):
        validate_rule(rule)


def test_A8_nan_and_infinite_delay_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [_delay(bad)]})
        with pytest.raises(AutomationRuleError, match="NaN/Infinity not allowed"):
            validate_rule(rule)


def test_A9_delay_beyond_max_rejected():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [_delay(MAX_DELAY_SECONDS + 1)]})
    with pytest.raises(AutomationRuleError, match="must be"):
        validate_rule(rule)


def test_A10_delay_seconds_param_on_device_step_rejected():
    rule = rule_from_dict("r", {
        "name": "r", "trigger": "manual",
        "sequence": [{"type": "home_assistant.turn_on", "parameters": {"target": "x", "delay_seconds": 5}}],
    })
    with pytest.raises(AutomationRuleError, match="not supported on a sequence step"):
        validate_rule(rule)


def test_A11_malformed_device_action_step_reuses_validate_action_message():
    """`camera.preset` with no `preset` parameter - the SAME error
    `validate_action()` already raises for the `actions` list, proving
    step validation is not a parallel, differently-behaved check."""
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [{"type": "camera.preset"}]})
    with pytest.raises(AutomationRuleError, match="camera.preset action requires"):
        validate_rule(rule)


def test_A12_too_many_sequence_steps_rejected():
    rule = rule_from_dict("r", {
        "name": "r", "trigger": "manual",
        "sequence": [_log() for _ in range(MAX_SEQUENCE_STEPS + 1)],
    })
    with pytest.raises(AutomationRuleError, match="sequence too long"):
        validate_rule(rule)


def test_A13_error_identifies_automation_and_step_index():
    rule = rule_from_dict("night_mode", {"name": "night_mode", "trigger": "manual", "sequence": [_log(), _delay(-4)]})
    with pytest.raises(AutomationRuleError) as excinfo:
        validate_rule(rule)
    msg = str(excinfo.value)
    assert "night_mode" in msg
    assert "step 1" in msg


def test_A14_sequence_step_types_is_action_types_plus_delay():
    from luno.automation.models import ACTION_TYPES
    # P0.14 - three new sequence-only control pseudo-types (`wait_until`,
    # `condition`, `stop_automation`) join `"delay"` - legitimate,
    # in-scope literal update (see test_15's own P0.14 comment in
    # test_sprint72_automation_engine.py for the sibling ACTION_TYPES
    # update this mirrors).
    assert SEQUENCE_STEP_TYPES == ACTION_TYPES | {"delay", "wait_until", "condition", "stop_automation"}


# ============================================================================
# B. Device actions
# ============================================================================

def test_B1_single_device_action_step_dispatches_via_tool_manager():
    rules = _seq_rule("r1", [_ha_on("Main Lamp")])
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
        assert tool_calls[0]["tool"] == "home_assistant"
        assert tool_calls[0]["action"] == "turn_on"
        assert tool_calls[0]["target"] == "Main Lamp"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_B2_multiple_device_action_steps_all_dispatch():
    rules = _seq_rule("r1", [_ha_on("Main Lamp"), _ha_off("Main Lamp")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(completed) == 1)
        assert [c["action"] for c in tool_calls] == ["turn_on", "turn_off"]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# C. Ordering (Section 6 - the sequential guarantee)
# ============================================================================

def test_C1_strict_ordering_A_then_B_then_C_proven_by_timestamps():
    rules = _seq_rule("r1", [_ha_on("A"), _ha_on("B"), _ha_on("C")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    started: List[Any] = []
    completed_steps: List[Any] = []
    runtime.event_bus.subscribe("automation.step_started", lambda e: started.append((time.monotonic(), e.data)))
    runtime.event_bus.subscribe("automation.step_completed", lambda e: completed_steps.append((time.monotonic(), e.data)))
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        assert len(started) == 3 and len(completed_steps) == 3
        # Never: step N+1 starts before step N completes.
        for i in range(3):
            assert started[i][1]["step_index"] == i
            assert completed_steps[i][1]["step_index"] == i
            assert started[i][0] <= completed_steps[i][0]
            if i > 0:
                assert completed_steps[i - 1][0] <= started[i][0], (
                    "a later step must never START before the previous step COMPLETED"
                )
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_C2_ordering_holds_with_a_delay_between_two_actions():
    rules = _seq_rule("r1", [_ha_on("A"), _delay(0.3), _ha_on("B")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Any] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append((time.monotonic(), e.data.get("tool_call", {}))))
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        t0 = time.monotonic()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        assert len(tool_calls) == 2
        gap = tool_calls[1][0] - tool_calls[0][0]
        assert gap >= 0.28, f"the second action must not dispatch until the 0.3s delay elapsed (gap={gap:.3f}s)"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# D. Delay
# ============================================================================

def test_D1_delay_step_executes_and_produces_a_delay_result():
    rules = _seq_rule("r1", [_delay(0.1)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        results = status["last_execution"]["action_results"]
        assert len(results) == 1
        assert results[0]["type"] == "delay"
        assert results[0]["status"] == "completed"
        assert results[0]["code"] == "delay_completed"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D2_delay_duration_is_honored_approximately():
    rules = _seq_rule("r1", [_delay(0.4)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        t0 = time.monotonic()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1, timeout_s=3.0)
        elapsed = time.monotonic() - t0
        assert 0.35 <= elapsed <= 2.0, f"expected ~0.4s delay, took {elapsed:.3f}s"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D3_fractional_delay_supported():
    rules = _seq_rule("r1", [_delay(0.15)])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_D4_zero_delay_is_valid_and_a_no_op_pause():
    rule = rule_from_dict("r", {"name": "r", "trigger": "manual", "sequence": [_delay(0)]})
    validate_rule(rule)  # must not raise - MIN_DELAY_SECONDS == 0.0


def test_D5_negative_delay_rejected_at_validation():
    with pytest.raises(AutomationRuleError, match="negative delay"):
        validate_sequence_step(AutomationAction(type="delay", parameters={"seconds": -1}), 0, "r")


def test_D6_invalid_delay_type_rejected():
    with pytest.raises(AutomationRuleError, match="must be a number"):
        validate_sequence_step(AutomationAction(type="delay", parameters={"seconds": "two"}), 0, "r")


# ============================================================================
# E. Failure semantics (Section 7)
# ============================================================================

def test_E1_A_succeeds_B_fails_C_never_runs():
    rules = _seq_rule("r1", [
        _ha_on("A"),
        {"type": "camera.preset", "parameters": {"preset": "does_not_exist"}},
        _ha_on("C"),
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
        # Step 0 (A) and step 1 (the failing preset) dispatch; step 2 (C)
        # must NEVER reach ToolManager.
        assert len(tool_calls) == 2
        assert tool_calls[0]["target"] == "A"
        assert tool_calls[1]["tool"] == "camera_ptz"
        assert not any(c.get("target") == "C" for c in tool_calls)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_E2_failed_sequence_final_status_is_failed_not_partial():
    rules = _seq_rule("r1", [_ha_on("A"), {"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "FAILED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_E3_failure_reason_identifies_step_index_and_action():
    rules = _seq_rule("r1", [_ha_on("A"), {"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        reason = status["last_execution"]["reason"]
        assert "step 1" in reason
        assert "camera.preset" in reason
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_E4_exception_is_never_swallowed_silently():
    """A device-offline-style failure (P0.8.x's own convention) must
    show up as a real, visible failed ActionResult - never silently
    absorbed into a fake success."""
    rules = _seq_rule("r1", [{"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        result = status["last_execution"]["action_results"][0]
        assert result["status"] == "failed"
        assert result["message"]
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# F. Execution state (Section 5)
# ============================================================================

def test_F1_running_status_observable_during_a_mid_sequence_delay():
    rules = _seq_rule("r1", [_ha_on("A"), _delay(1.0), _ha_on("B")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        # Poll shortly after start - should be RUNNING, mid-delay.
        time.sleep(0.3)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["running"] is True
        last = status["last_execution"]
        assert last is not None
        assert last["final_status"] == "RUNNING"
        assert last["current_step_index"] == 1  # the delay step
        assert last["total_steps"] == 3
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F2_completed_status_after_full_success():
    rules = _seq_rule("r1", [_ha_on("A"), _ha_off("A")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "COMPLETED"
        assert status["running"] is False
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F3_failed_status_after_failure():
    rules = _seq_rule("r1", [{"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "FAILED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F4_current_step_and_total_steps_track_progress():
    rules = _seq_rule("r1", [_ha_on("A"), _ha_on("B"), _ha_on("C")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        last = status["last_execution"]
        assert last["total_steps"] == 3
        assert last["current_step_index"] == 2  # the final, last-run step
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_F5_legacy_actions_execution_never_sets_step_fields():
    """Backward compatibility at the execution-observability level too -
    an `actions`-based rule's execution must keep `current_step_index`/
    `total_steps` at their default `None`, never accidentally populated
    by shared code."""
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [_log()], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        status = modules["automation_engine"].get_automation_status("r1")
        last = status["last_execution"]
        assert last["current_step_index"] is None
        assert last["total_steps"] is None
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# G. Logging (Section 10)
# ============================================================================

def test_G1_sequence_start_step_and_completion_log_lines(capsys):
    rules = _seq_rule("r1", [_ha_on("A"), _delay(0.05), _ha_off("A")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        capsys.readouterr()  # drain bootstrap noise
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        time.sleep(0.05)
        out = capsys.readouterr().out
        assert "started automation=r1 steps=3" in out
        assert "step=1/3" in out and "type=home_assistant.turn_on started" in out
        assert "step=2/3" in out and "type=delay started duration=0.05s" in out
        assert "step=3/3" in out and "type=home_assistant.turn_off started" in out
        assert "completed duration=" in out
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_G2_failure_logging_includes_failed_step(capsys):
    rules = _seq_rule("r1", [{"type": "camera.preset", "parameters": {"preset": "does_not_exist"}}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        capsys.readouterr()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        time.sleep(0.05)
        out = capsys.readouterr().out
        assert "FAILED step=1/1" in out
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_G3_delay_step_never_logged_as_a_device_action():
    """Section 9 - a delay step's own log line always says `type=delay`,
    never one of `ACTION_TYPES` - proven structurally via the source
    (the delay branch's log format string), not just behaviorally."""
    source = _read(_ENGINE_PATH)
    assert 'type=delay started duration=' in source


# ============================================================================
# H. ToolManager integration (Section 8/18 - architecture guard)
# ============================================================================

def test_H1_every_sequence_device_action_goes_through_tool_requested():
    rules = _seq_rule("r1", [_ha_on("A"), _delay(0.05), {"type": "camera.home"}])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        # Exactly the two non-delay steps dispatched a tool_requested -
        # the delay step never does.
        assert len(tool_calls) == 2
        assert {c["tool"] for c in tool_calls} == {"home_assistant", "camera_ptz"}
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_H2_sequence_dispatch_reuses_dispatch_action_not_a_parallel_path():
    """Structural proof - `_run_action_step()` calls `self._dispatch_
    action(...)`, the EXACT SAME method `_run_actions()` (legacy) calls -
    there is no second `_dispatch_*_step`/`_sequence_dispatch` device-
    control method anywhere in this file.

    Baseline includes every dispatch-*like*-named method that already
    existed prior to P0.11 (confirmed by reading the full pre-P0.11
    engine.py source), including `on_camera_dispatch` - a pre-existing
    EventBus SUBSCRIBER callback (not a device-control dispatch path),
    named this way for unrelated historical reasons. P0.11 must not add
    a NEW member to this set."""
    source = _read(_ENGINE_PATH)
    tree = ast.parse(source)
    dispatch_like_methods = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "dispatch" in node.name.lower()
    }
    baseline = {
        "_dispatch_action", "_dispatch_internal_action", "_dispatch_camera_action",
        "_dispatch_home_assistant_action", "_dispatch_tool_call", "on_camera_dispatch",
    }
    assert dispatch_like_methods == baseline, (
        f"unexpected new/removed dispatch-like method(s): {dispatch_like_methods ^ baseline}"
    )


# ============================================================================
# I. Backward compatibility (Section 3/13)
# ============================================================================

def test_I1_existing_single_action_automation_still_works_exactly_once():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [_ha_on("Main Lamp")], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    done: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: done.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(done) == 1)
        assert len(tool_calls) == 1
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_I2_existing_multi_action_partial_failure_semantics_unchanged():
    """The pre-existing `actions` list's OWN "run everything, then
    classify" policy (Sprint 72's own PARTIAL_FAILURE) must remain
    completely untouched by P0.11 - unlike a `sequence`, which stops on
    the first failure."""
    rules = {"r1": {
        "name": "r1", "enabled": True, "trigger": "manual",
        "actions": [_ha_on("Main Lamp"), {"type": "camera.preset", "parameters": {"preset": "unseeded"}}],
        "cooldown_seconds": 0.0,
    }}
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    try:
        runtime.start()
        modules["automation_engine"].run_automation("r1")
        assert _wait_until(lambda: len(failed) == 1)
        # BOTH actions dispatch (legacy policy never stops early).
        assert len(tool_calls) == 2
        status = modules["automation_engine"].get_automation_status("r1")
        assert status["last_execution"]["final_status"] == "PARTIAL_FAILURE"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_I3_real_shipped_wled_rules_file_still_loads_and_validates():
    """The REAL, shipped `config/automation_rules.json` must still load
    without error under the new, stricter `validate_rule()`.

    P0.14 update: the original assertion here (`rule.sequence == []` for
    every shipped rule) was a P0.11-era snapshot of reality, true only
    because no real rule used `sequence` YET at the time P0.11 was
    written. It is no longer true - as of this sprint, the real, live
    `config/automation_rules.json` genuinely contains a user-authored
    `sequence`-based rule ("Back From Work" /
    `automation-4051-1787679811273`, created through the real P0.13
    Dashboard during this project's own live usage) - a welcome sign the
    P0.13 dashboard actually works end-to-end, not a defect. This test's
    real purpose (the shipped file loads and validates without raising,
    whatever its current real-world content is) is unaffected."""
    real_rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(real_rules_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    for rule_id, data in raw.items():
        rule = rule_from_dict(rule_id, data)
        validate_rule(rule)  # must not raise for any shipped rule


def test_I4_enable_disable_persists_sequence_field_too():
    """`enable_automation()`/`disable_automation()` must not silently
    drop a rule's `sequence` on the one write path this module has."""
    rules = _seq_rule("r1", [_log("a"), _delay(0.01), _log("b")])
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    try:
        runtime.start()
        engine = modules["automation_engine"]
        result = engine.disable_automation("r1")
        assert result["ok"] is True
        with open(rules_path, "r", encoding="utf-8") as fh:
            persisted = json.load(fh)
        assert persisted["r1"]["enabled"] is False
        assert len(persisted["r1"]["sequence"]) == 3
        assert persisted["r1"]["sequence"][1]["type"] == "delay"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# J. Concurrent execution (Section 15)
# ============================================================================

def test_J1_sequence_delay_in_one_automation_does_not_block_an_unrelated_one():
    rules = {}
    rules.update(_seq_rule("slow", [_ha_on("A"), _delay(1.5), _ha_on("B")]))
    rules.update({"fast": {"name": "fast", "enabled": True, "trigger": "manual", "actions": [_log("hi")], "cooldown_seconds": 0.0}})
    runtime, modules, adapter_manager, rules_path = _build_stack(rules)
    completed_ids: List[str] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed_ids.append(e.data.get("rule_id")))
    try:
        runtime.start()
        engine = modules["automation_engine"]
        t0 = time.monotonic()
        engine.run_automation("slow")
        time.sleep(0.1)  # let 'slow' enter its delay
        engine.run_automation("fast")
        assert _wait_until(lambda: "fast" in completed_ids, timeout_s=2.0)
        fast_elapsed = time.monotonic() - t0
        assert fast_elapsed < 1.0, (
            f"'fast' must complete well before 'slow's own 1.5s delay elapses (took {fast_elapsed:.2f}s)"
        )
        assert _wait_until(lambda: "slow" in completed_ids, timeout_s=3.0)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# K. Cancellation - honestly documents the absence (Section 14)
# ============================================================================

def test_K1_no_execution_cancellation_mechanism_exists_yet():
    """Section 14: 'If cancellation does NOT exist, do not build a full
    cancellation framework in P0.11... document this limitation.' This
    project's `AutomationEngine` has no method to cancel an IN-PROGRESS
    execution (only `_cancel_pending_delayed_action()`, P0.8.9's own
    narrower mechanism for a not-yet-fired SCHEDULED single HA action,
    a completely different concept - not a general execution-cancel).
    This test is a structural, honest proof of that absence, not a
    fabricated capability."""
    engine = AutomationEngine()
    public_methods = {name for name in dir(engine) if not name.startswith("_")}
    assert not any("cancel" in name.lower() for name in public_methods), (
        "no public cancellation API exists on AutomationEngine yet - if this ever fires, "
        "P0.11's own 'no cancellation was built' claim is now stale and must be revisited"
    )


def test_K2_wait_delay_uses_an_interruptible_primitive_ready_for_future_cancellation():
    """Even though no cancellation exists YET, `_wait_delay()` was
    deliberately built on `threading.Event.wait()` (interruptible via
    `.set()`) rather than `time.sleep()` (not interruptible) - Section
    14's own 'keep the implementation structured so a future sprint can
    add cancellation later' instruction, verified structurally via AST
    (not a raw substring search, which would also match the method's
    OWN docstring prose contrasting the two approaches)."""
    tree = ast.parse(_read(_ENGINE_PATH))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_wait_delay"
    )
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]

    def _dotted(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return ""

    called_names = {_dotted(c) for c in calls}
    assert "wait" in called_names, f"expected an Event().wait(...) call, got calls={called_names}"
    assert "time.sleep" not in called_names, "_wait_delay() must never call time.sleep() as real code"


# ============================================================================
# M. Architecture guards (Section 18)
# ============================================================================

def test_M1_no_direct_http_or_ha_calls_from_the_sequence_code():
    source = _read(_ENGINE_PATH)
    for forbidden in ("requests.", "urllib.request", "http.client", "aiohttp"):
        assert forbidden not in source, f"engine.py must never call {forbidden!r} directly - only via ToolManager"


def test_M2_no_new_thread_spawned_per_sequence_step():
    """Structural proof - `threading.Thread(` must appear EXACTLY once
    in the whole file (the pre-existing one-thread-per-EXECUTION spawn
    in `_trigger()`) - a sequence's steps must never spawn their own
    threads (that would be parallel execution, explicitly forbidden in
    P0.11 - reserved for a future P0.12)."""
    source = _read(_ENGINE_PATH)
    assert source.count("threading.Thread(") == 1


def test_M3_no_busy_loop_or_global_blocking_sleep_for_delay():
    """`time.sleep(` must never appear as executable code anywhere in
    engine.py (only `_wait_delay()`'s own docstring MENTIONS it, as
    prose contrasting it with the real `Event().wait()` implementation)."""
    tree = ast.parse(_read(_ENGINE_PATH))
    sleep_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "sleep" and isinstance(n.func.value, ast.Name) and n.func.value.id == "time"
    ]
    assert sleep_calls == [], "engine.py must never call time.sleep() as real code - only Event().wait()"


def test_M4_no_second_automation_engine_or_tool_manager_class_defined():
    tree = ast.parse(_read(_ENGINE_PATH))
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert class_names == {"AutomationEngine"}


def test_M5_vision_module_does_not_import_automation():
    for path in (_VISION_PATH, _ADAPTERS_VISION_PATH, _VISION_OCC_PATH):
        source = _read(path)
        assert "luno.automation" not in source and "from ..automation" not in source and "from .automation" not in source, (
            f"{path} must not import the automation package - P0.11 must not touch Vision/Occupancy"
        )


def test_M6_no_parallel_execution_primitives_introduced():
    """No `ThreadPoolExecutor`/`asyncio.gather`/`concurrent.futures` -
    style parallel-step execution anywhere in engine.py (P0.11 is
    sequential WITHIN one execution by explicit requirement; parallel
    execution is reserved for a future P0.12)."""
    source = _read(_ENGINE_PATH)
    for forbidden in ("ThreadPoolExecutor", "asyncio.gather", "concurrent.futures", "multiprocessing"):
        assert forbidden not in source


def test_M7_sequence_execution_never_bypasses_dispatch_action_for_non_delay_steps():
    """Structural proof that `_run_action_step()` (the only caller of
    device-action dispatch for a sequence step) calls `_dispatch_action`
    and nothing lower-level (no direct `_dispatch_tool_call`/`_dispatch_
    home_assistant_action` skip-ahead that would bypass the camera safety
    gate / ownership checks / allowlist re-check `_dispatch_action`
    itself performs)."""
    source = _read(_ENGINE_PATH)
    start = source.index("def _run_action_step(")
    end = source.index("\n    def _wait_delay(")
    body = source[start:end]
    assert "self._dispatch_action(" in body
    assert "_dispatch_tool_call(" not in body
    assert "_dispatch_home_assistant_action(" not in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
