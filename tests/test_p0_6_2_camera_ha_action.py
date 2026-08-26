"""
tests/test_p0_6_2_camera_ha_action.py
========================================

LUNO P0.6.2 (First Real Home Assistant Action - Safe Single-Device
Camera Automation) - dedicated regression suite.

Adds the FIRST real-device-affecting automation rule on top of the
already-live-verified camera pipeline (P0.5.4) and log-only rule
(P0.6/P0.6.1): `camera_human_detected_test_action` - `event.kind ==
"human_detected"` -> `home_assistant.turn_on` targeting `light.wled`
("RGB Strip"), the project's own pre-existing, real, low-risk default
light (`.env`'s `RGB_LIGHT_ENTITY`, also present in `config/
lights.config.json`) - never a fabricated entity id.

Architecture reused, not replaced: the existing `home_assistant.turn_on`
`AutomationAction` type (Sprint 72), the existing `RealHomeAssistant
Handler`'s own verified on/off dispatch (`homeassistant.turn_on`
generic service, idempotent, state-checked), and the existing
`event.<field>` condition mechanism (P0.6). The ONE genuine gap closed
this sprint - `luno/automation/models.py::validate_action()` previously
accepted any non-empty-after-`str()` target for an HA action (a list,
a dict, or the literal wildcard `"*"` all passed) - tightened to
require a real, single, non-wildcard entity-id string. This is the
ONLY production file this sprint modifies besides `config/
automation_rules.json` itself.

Every test in this file uses the MockHomeAssistantHandler (or a small,
explicitly-failing stand-in for the HA-failure-isolation test) - NEVER
a real device. Per Section 16's own instruction, only the external HA
boundary is mocked; rule matching, condition evaluation, and dispatch
all go through the real `AutomationEngine`.
"""

from __future__ import annotations

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

from luno.automation.models import (  # noqa: E402
    AutomationAction,
    AutomationRuleError,
    rule_from_dict,
    validate_action,
    validate_rule,
)
from luno.tool_manager.handler import ToolHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402
from luno.tool_manager.result import ToolResult  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)

_LOG_RULE_ID = "camera_human_detected_log"
_HA_RULE_ID = "camera_human_detected_test_action"
_TEST_ENTITY = "light.wled"
_REAL_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _camera_event_data(kind: str, camera_id: str = "tapo_c212") -> Dict[str, Any]:
    return {
        "camera_id": camera_id, "kind": kind, "entity_id": f"vision:{kind}",
        "old_state": None, "new_state": None, "confidence": None,
        "timestamp": time.time(), "source": "vision",
    }


def _ha_rule(enabled: bool = True, target: Any = _TEST_ENTITY, cooldown: float = 30.0) -> Dict[str, Any]:
    return {
        _HA_RULE_ID: {
            "name": "Camera human detected -> RGB Strip test light ON",
            "enabled": enabled,
            "trigger": "event:camera_automation.camera_event",
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_detected"}],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": target}}],
            "cooldown_seconds": cooldown,
        }
    }


def _log_rule(enabled: bool = True) -> Dict[str, Any]:
    return {
        _LOG_RULE_ID: {
            "name": "Camera human detected (log only)",
            "enabled": enabled,
            "trigger": "event:camera_automation.camera_event",
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_detected"}],
            "actions": [{"type": "automation.log", "parameters": {"message": "log-only"}}],
            "cooldown_seconds": 0.0,
        }
    }


def _write_rules(path: str, rules: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)


def _build_stack(rules: Optional[Dict[str, Any]] = None, rules_path: Optional[str] = None):
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is None:
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_6_2_automation_rules_test_")
        os.close(fd)
    engine._rules_path = rules_path
    if rules is not None:
        _write_rules(rules_path, rules)
    engine.reload_rules()

    return runtime, modules, adapter_manager, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None and rules_path != _REAL_RULES_PATH:
        try:
            os.remove(rules_path)
        except OSError:
            pass


class _AlwaysFailHAHandler(ToolHandler):
    """A minimal, explicitly-failing stand-in for the HA boundary -
    used ONLY by the HA-failure-isolation test (Section 15's own
    "HA failure isolated" case). Never touches a real device - this
    IS the mock boundary Section 16 requires; it simply always fails
    instead of always succeeding."""
    name = "home_assistant"
    default_timeout_s = 5.0
    max_timeout_s = 15.0

    def supported_actions(self) -> List[str]:
        return ["turn_on", "turn_off"]

    def execute(self, tool_call: ToolCall, context: Any = None) -> ToolResult:
        return ToolResult.fail(self.name, tool_call.action, "simulated Home Assistant unreachable")


# ============================================================================
# A. Configuration
# ============================================================================

def test_01_valid_test_entity_loads():
    rule = rule_from_dict(_HA_RULE_ID, _ha_rule()[_HA_RULE_ID])
    validate_rule(rule)  # must not raise
    assert rule.actions[0].type == "home_assistant.turn_on"
    assert rule.actions[0].parameters["target"] == _TEST_ENTITY


def test_02_missing_entity_rejected():
    data = _ha_rule()[_HA_RULE_ID]
    del data["actions"][0]["parameters"]["target"]
    rule = rule_from_dict(_HA_RULE_ID, data)
    with pytest.raises(AutomationRuleError):
        validate_rule(rule)


def test_03_empty_entity_rejected():
    rule = rule_from_dict(_HA_RULE_ID, _ha_rule(target="")[_HA_RULE_ID])
    with pytest.raises(AutomationRuleError):
        validate_rule(rule)


@pytest.mark.parametrize("bad_target", ["*", [], None, {}, ["light.wled"]])
def test_04_wildcard_or_non_string_target_rejected(bad_target):
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="home_assistant.turn_on", parameters={"target": bad_target}))


def test_05_valid_single_entity_string_still_accepted():
    validate_action(AutomationAction(type="home_assistant.turn_on", parameters={"target": _TEST_ENTITY}))  # must not raise


def test_06_shipped_entity_comes_from_real_project_configuration():
    """Section 4 - never fabricated. light.wled is the project's own
    pre-existing default light (.env's RGB_LIGHT_ENTITY, also present in
    config/lights.config.json) - confirmed here directly against those
    real files, not merely asserted."""
    import re
    env_path = os.path.join(_ROOT, ".env")
    with open(env_path, "r", encoding="utf-8") as fh:
        env_text = fh.read()
    assert re.search(r"^RGB_LIGHT_ENTITY\s*=\s*light\.wled\s*$", env_text, re.MULTILINE)

    lights_path = os.path.join(_ROOT, "config", "lights.config.json")
    with open(lights_path, "r", encoding="utf-8") as fh:
        lights = json.load(fh)
    entity_ids = [v.get("entity_id") for v in lights.values() if isinstance(v, dict)]
    assert _TEST_ENTITY in entity_ids


# ============================================================================
# B. Trigger (event matching - reuses P0.6's own event.<field> mechanism
#    unchanged; re-verified here for THIS rule specifically)
# ============================================================================

def test_07_human_detected_matches():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
        assert completed[0]["rule_id"] == _HA_RULE_ID
    finally:
        _teardown(runtime, adapter_manager, rules_path)


@pytest.mark.parametrize("kind", ["human_cleared", "camera_online", "camera_offline"])
def test_08_other_kinds_do_not_match(kind):
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    completed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data(kind)))
        assert _wait_until(lambda: len(skipped) == 1)
        assert completed == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# C. Action (correct HA service requested, single entity, no extra action)
# ============================================================================

def test_09_correct_ha_service_requested_single_entity():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    tool_calls: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 1
        call = tool_calls[0]["tool_call"]
        assert call["tool"] == "home_assistant"
        assert call["action"] == "turn_on"
        assert call["target"] == _TEST_ENTITY
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_10_no_wildcard_or_extra_targeting_no_extra_device_action():
    """Exactly one tool_requested for exactly one entity - no PTZ, no
    second HA call, for a single human_detected event."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    tool_calls: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_call"]["target"] == _TEST_ENTITY
        assert all(tc["tool_call"]["tool"] not in ("camera_ptz", "camera_patrol") for tc in tool_calls)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# D. Safety
# ============================================================================

def test_11_disabled_rule_does_nothing():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule(enabled=False))
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        time.sleep(0.3)
        assert tool_calls == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_12_invalid_target_rule_fails_to_load_and_never_fires():
    """A malformed target (wildcard) makes the WHOLE rule fail to load
    (defense at config-load time, Section 7's own "must not execute if
    validation fails") - proven here by publishing the triggering event
    and confirming absolutely nothing happens (not even 'skipped' - the
    rule was never registered at all)."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule(target="*"))
    engine = modules["automation_engine"]
    assert _HA_RULE_ID not in engine._rules
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        time.sleep(0.3)
        assert tool_calls == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_13_unsupported_service_rejected_at_load_time():
    """P0.6.2's own allowlist: only home_assistant.turn_on/turn_off are
    expressible at all - light.turn_on/switch.turn_on/lock.unlock/etc.
    are not valid AutomationAction types and are refused at load."""
    for bad_type in ("light.turn_on", "switch.turn_on", "lock.unlock", "script.run"):
        with pytest.raises(AutomationRuleError):
            validate_action(AutomationAction(type=bad_type, parameters={"target": _TEST_ENTITY}))


def test_14_ha_failure_isolated_engine_and_bus_survive(monkeypatch):
    """If Home Assistant is unreachable, the action must report FAILED,
    never crash the Event Bus/engine, and never expose credentials -
    exercised via the explicitly-failing `_AlwaysFailHAHandler` stand-in
    (Section 16's own mock-the-HA-boundary instruction)."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    tool_manager_module = modules["tool_manager_module"]
    tool_manager_module.manager.registry.register("home_assistant", _AlwaysFailHAHandler())

    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(failed) == 1, timeout_s=20.0)
        assert failed[0]["rule_id"] == _HA_RULE_ID
        assert "reason" in failed[0]
        # Never a credential/token/URL in the metadata-only failure payload.
        blob = json.dumps(failed[0]).lower()
        for forbidden in ("token", "password", "authorization", "ws://", "http://"):
            assert forbidden not in blob

        # Engine/bus still alive after the failure.
        marker: List[bool] = []
        runtime.event_bus.subscribe("marker_event", lambda e: marker.append(True))
        runtime.event_bus.publish(Event(type="marker_event", data={}))
        assert _wait_until(lambda: marker == [True])
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_15_action_exception_isolated(monkeypatch):
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_ha_rule())
    engine = modules["automation_engine"]

    def _boom(action, execution):
        raise RuntimeError("simulated dispatch failure")

    monkeypatch.setattr(engine, "_dispatch_home_assistant_action", _boom)

    outcomes: List[Any] = []
    runtime.event_bus.subscribe("automation.failed", lambda e: outcomes.append(("failed", e.data)))
    runtime.event_bus.subscribe("automation.completed", lambda e: outcomes.append(("completed", e.data)))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(outcomes) >= 1)
        assert outcomes[0][0] == "failed"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# E. Regression - the log-only rule must keep working unchanged, alongside
#    the new real-device rule (Section 14 - both rules evaluated).
# ============================================================================

def test_16_log_only_rule_unaffected_both_rules_fire_independently():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules={**_log_rule(), **_ha_rule()})
    log_completed: List[Dict[str, Any]] = []
    ha_completed: List[Dict[str, Any]] = []

    def _on_completed(e):
        rid = e.data.get("rule_id")
        if rid == _LOG_RULE_ID:
            log_completed.append(e.data)
        elif rid == _HA_RULE_ID:
            ha_completed.append(e.data)

    runtime.event_bus.subscribe("automation.completed", _on_completed)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(log_completed) == 1 and len(ha_completed) == 1)
        # log-only rule never produced a tool_requested; the HA rule
        # produced exactly one.
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_call"]["tool"] == "home_assistant"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_17_real_shipped_rules_file_has_both_rules_and_log_rule_unchanged():
    """Confirms the ACTUAL config/automation_rules.json this sprint
    ships - not a copy - still has the P0.6.1 log-only rule byte-for-
    byte as it was (Section 5: 'must remain unchanged'), plus the new
    rule."""
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw[_LOG_RULE_ID] == {
        "name": "Camera human detected (log only)",
        "enabled": True,
        "trigger": "event:camera_automation.camera_event",
        "conditions": [{"type": "equals", "target": "event.kind", "value": "human_detected"}],
        "actions": [{"type": "automation.log", "parameters": {"message": "camera_automation.camera_event kind=human_detected matched (log-only, no device action)"}}],
        "cooldown_seconds": 0.0,
    }
    assert _HA_RULE_ID in raw
    assert raw[_HA_RULE_ID]["actions"] == [{"type": "home_assistant.turn_on", "parameters": {"target": _TEST_ENTITY}}]


def test_18_real_bootstrap_both_shipped_rules_load_and_fire(monkeypatch):
    """Full real-bootstrap integration test using the ACTUAL shipped
    config/automation_rules.json (not a copy).

    P0.8.6 UPDATE (documented, intentional behavior change - see
    docs/change_impact/camera_automation_p0_8_6.md Section 3):
    `_LOG_RULE_ID` (`camera_human_detected_log`) still fires on the raw
    `kind="human_detected"` event, exactly as before - full observability
    of every raw detection is preserved. `_HA_RULE_ID` (`camera_human_
    detected_test_action`, the rule that actually calls `home_assistant.
    turn_on`) now requires `kind="human_confirmed"` (P0.8.6's new,
    sustained-detection-only signal - a single raw frame must never
    directly flip a real physical light) PLUS `available=true`/
    `detection_error=null` (brought up to the same safety bar
    `camera_test_automation_safety_action`'s mock rule already had).
    This test now publishes both a raw `human_detected` event (for the
    log rule) and a `human_confirmed` one with a healthy `available`/
    `detection_error` (for the HA rule) - still proving, honestly, that
    BOTH shipped rules load and fire from the real file."""
    monkeypatch.setenv("CAMERA_AUTOMATION_ENABLED", "true")
    monkeypatch.setenv("CAMERA_AUTOMATION_COOLDOWN_S", "0")
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=None, rules_path=_REAL_RULES_PATH)
    completed_rule_ids: List[str] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed_rule_ids.append(e.data.get("rule_id")))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        confirmed_data = _camera_event_data("human_confirmed")
        confirmed_data["available"] = True
        confirmed_data["detection_error"] = None
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=confirmed_data))
        assert _wait_until(lambda: _LOG_RULE_ID in completed_rule_ids and _HA_RULE_ID in completed_rule_ids)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# F. Security / diff-relevant static checks
# ============================================================================

def test_19_no_eval_exec_or_dynamic_import_in_models_py():
    import ast
    path = os.path.join(_ROOT, "luno", "automation", "models.py")
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    forbidden_calls = {"eval", "exec", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            assert name not in forbidden_calls


def test_20_shipped_rules_file_never_contains_a_credential_shaped_value():
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        text = fh.read().lower()
    for forbidden in ("password", "token", "secret", "ws://", "http://", "authorization"):
        assert forbidden not in text


# ============================================================================
# G. Observer wiring - the P0.6.2 additions to the SAME reused
#    `luno_live_camera_event_observer.py` script (P0.5.4-LIVE/P0.6.1),
#    not a new observer implementation. Wiring/static proofs only - no
#    hardware claims (matches every prior sprint's own convention).
# ============================================================================

import luno_live_camera_event_observer as obs  # noqa: E402


def test_21_observer_tracks_the_real_shipped_ha_rule_id():
    assert obs._TRACKED_HA_RULE_ID == _HA_RULE_ID
    assert obs._HA_TEST_ENTITY == _TEST_ENTITY


def test_22_observer_on_ha_action_event_counts_only_the_tracked_ha_rule():
    observer = obs._LiveObserver()
    handler = observer.on_ha_action_event("completed")
    handler(Event(type="automation.completed", data={"rule_id": _HA_RULE_ID}))
    handler(Event(type="automation.completed", data={"rule_id": _LOG_RULE_ID}))
    assert observer.ha_action_event_counts["completed"] == 1


def test_23_observer_on_ha_action_event_prints_section_13_log_format(capsys):
    observer = obs._LiveObserver()
    handler = observer.on_ha_action_event("completed")
    handler(Event(type="automation.completed", data={"rule_id": _HA_RULE_ID}))
    out = capsys.readouterr().out
    assert f"rule={_HA_RULE_ID}" in out
    assert "event=camera_automation.camera_event" in out
    assert "kind=human_detected" in out
    assert "action=home_assistant.turn_on" in out
    assert f"target={_TEST_ENTITY}" in out
    assert "result=success" in out


def test_24_observer_on_ha_action_event_failed_reports_result_failed(capsys):
    observer = obs._LiveObserver()
    handler = observer.on_ha_action_event("failed")
    handler(Event(type="automation.failed", data={"rule_id": _HA_RULE_ID, "reason": "all_actions_failed"}))
    out = capsys.readouterr().out
    assert "result=failed" in out
    assert "reason=all_actions_failed" in out


def test_25_observer_on_tool_requested_classifies_by_tool(capsys):
    observer = obs._LiveObserver()
    observer.on_tool_requested(Event(type="tool_requested", data={"tool_call": {"tool": "home_assistant", "action": "turn_on", "target": _TEST_ENTITY}}))
    observer.on_tool_requested(Event(type="tool_requested", data={"tool_call": {"tool": "camera_ptz", "action": "goto_preset"}}))
    assert observer.tool_requested_by_tool == {"home_assistant": 1, "camera_ptz": 1}
    assert observer.tool_requested_count == 2
    out = capsys.readouterr().out
    assert _TEST_ENTITY not in out  # never echoes the full tool_call


def test_26_observer_never_calls_a_write_or_control_method_itself():
    import inspect
    from tests.test_luno_live_camera_event_observer import _code_only  # reuse the established helper
    source = _code_only(inspect.getsource(obs))
    forbidden = ("moveMotor", "calibrateMotor", "savePreset", "setPreset", "call_service")
    for name in forbidden:
        assert name not in source, f"forbidden write/control reference found: {name}"


def test_27_observer_never_writes_automation_rules_json():
    import inspect
    import re
    source = inspect.getsource(obs)
    write_mode_opens = re.findall(r"""open\([^)]*['"]\s*[wa]\+?['"]""", source)
    assert not write_mode_opens, f"found file-write call(s): {write_mode_opens}"
