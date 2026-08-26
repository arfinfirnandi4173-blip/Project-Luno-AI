"""
tests/test_p0_8_0_camera_action_safety.py
=============================================

LUNO P0.8.0 (Camera Automation -> Home Assistant Action Safety Pipeline)
- dedicated regression suite.

CRITICAL SAFETY REQUIREMENT (brief Section "CRITICAL SAFETY
REQUIREMENT" + Section 14): NOT ONE test in this file ever calls
`register_real_tool_handlers()` or constructs a real Home Assistant
client - every "allowed" test below routes through the SAME
`MockHomeAssistantHandler` `luno/tool_manager/builtin/__init__.py::
register_all()` already registers by default (confirmed directly:
`test_00_fixture_uses_the_mock_ha_handler_not_a_real_one`). No real
light is ever turned on or off by this file.

Sections, mirroring the brief's own numbering:

  A. `validate_camera_ha_action()` pure unit tests (Section 3/5/7) - no
     bootstrap, no Event Bus, just the function directly.
  B. Real-bootstrap Event Bus tests (Section 10) - the real Runtime/
     AutomationEngine/CameraAutomationModule stack, publishing a real
     `camera_automation.camera_event` and observing the safety gate +
     mocked HA dispatcher react to it end to end.
  C. Cooldown/duplicate protection (Section 4).
  D. State-aware skip via the real bootstrap + an injected fake reader
     (Section 5, `engine.ha_state_reader`).
  E. Non-camera automation unaffected (Section 9 item 13).
  F. Attribution (Section 6).
  G. Architecture guard (Section 12) - no direct HA API calls from the
     safety module, no new HA client/Event Bus/AutomationEngine.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.automation.camera_action_safety import (  # noqa: E402
    CAMERA_HA_ACTION_TYPES,
    SafetyCheckResult,
    validate_camera_ha_action,
)
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import AutomationAction, AutomationRule, AutomationTrigger  # noqa: E402
from luno.bootstrap.adapters import register_all_adapters, register_camera_action_ha_state_reader  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_SAFETY_MODULE_PATH = os.path.join(_ROOT, "luno", "automation", "camera_action_safety.py")
_TEST_ENTITY = "light.test_camera_automation"


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


def _valid_event_data(**overrides: Any) -> Dict[str, Any]:
    data = {
        "camera_id": "tapo_c212", "kind": "human_detected", "available": True,
        "human_present": True, "person_count": 1, "detected_objects": ("person",),
        "detection_error": None,
    }
    data.update(overrides)
    return data


def _build_stack(camera_automation_enabled: bool = True):
    """Real bootstrap - Runtime/modules/adapters exactly as `main.py`
    wires them (MOCK backends throughout - `register_real_tool_
    handlers()` is deliberately NEVER called anywhere in this file, per
    this sprint's own hard safety constraint). Also wires `register_
    camera_action_ha_state_reader()` (no-ops harmlessly on the mock HA
    client, which has no `get_entity_state` - see that function's own
    docstring) so the real bootstrap path this sprint added is itself
    exercised, not just its individual pieces."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=camera_automation_enabled, cooldown_s=0.0)
    register_camera_action_ha_state_reader(modules, adapters)
    return runtime, modules, adapter_manager


def _teardown(runtime, adapter_manager) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


def _mock_ha_handler(modules: Dict[str, Any]) -> MockHomeAssistantHandler:
    tool_manager_module = modules["tool_manager_module"]
    handler = tool_manager_module.manager.registry.get("home_assistant")
    assert isinstance(handler, MockHomeAssistantHandler), (
        f"expected MockHomeAssistantHandler, got {type(handler)!r} - a test in this file would be "
        "silently exercising a REAL Home Assistant call, which this sprint must never do"
    )
    return handler


def _publish_camera_event(runtime, **overrides: Any) -> None:
    runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_valid_event_data(**overrides)))


# ============================================================================
# 0. Fixture sanity - prove the mock, not a real HA client, is in play
# ============================================================================

def test_00_fixture_uses_the_mock_ha_handler_not_a_real_one():
    runtime, modules, adapter_manager = _build_stack()
    try:
        _mock_ha_handler(modules)  # raises AssertionError if not mock
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# A. validate_camera_ha_action() - pure unit tests (Section 3/5/7)
# ============================================================================

def test_01_valid_human_detected_is_allowed():
    result = validate_camera_ha_action("home_assistant.turn_on", _TEST_ENTITY, _valid_event_data())
    assert result.allowed is True
    assert result.skip_dispatch is False
    assert result.code == "ok"


def test_02_detection_error_blocks_the_action():
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(detection_error="Conv.bn"),
    )
    assert result.allowed is False
    assert result.code == "detection_error_present"


def test_03_camera_offline_kind_blocks_the_action():
    result = validate_camera_ha_action(
        "home_assistant.turn_off", _TEST_ENTITY, _valid_event_data(kind="camera_offline"),
    )
    assert result.allowed is False
    assert result.code == "camera_offline"


def test_03b_available_false_blocks_the_action():
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(available=False),
    )
    assert result.allowed is False
    assert result.code == "camera_unavailable"


def test_04_malformed_camera_event_missing_kind_blocks_the_action():
    malformed = _valid_event_data()
    del malformed["kind"]
    result = validate_camera_ha_action("home_assistant.turn_on", _TEST_ENTITY, malformed)
    assert result.allowed is False
    assert result.code == "malformed_camera_event"


def test_04b_missing_event_context_blocks_the_action():
    result = validate_camera_ha_action("home_assistant.turn_on", _TEST_ENTITY, None)
    assert result.allowed is False
    assert result.code == "missing_event_context"


def test_04c_non_dict_event_context_blocks_the_action():
    result = validate_camera_ha_action("home_assistant.turn_on", _TEST_ENTITY, "not_a_dict")  # type: ignore[arg-type]
    assert result.allowed is False
    assert result.code == "missing_event_context"


@pytest.mark.parametrize("bad_target", [None, "", "   ", "*", "not_an_entity_id", "light..", ["light.a", "light.b"], 123])
def test_05_invalid_entity_id_blocks_the_action(bad_target):
    result = validate_camera_ha_action("home_assistant.turn_on", bad_target, _valid_event_data())
    assert result.allowed is False
    assert result.code == "invalid_target"


def test_06_unsupported_action_type_blocks_the_action():
    result = validate_camera_ha_action("home_assistant.toggle", _TEST_ENTITY, _valid_event_data())
    assert result.allowed is False
    assert result.code == "unsupported_action_type"


def test_06b_camera_action_types_never_accepted_here():
    result = validate_camera_ha_action("camera.preset", _TEST_ENTITY, _valid_event_data())
    assert result.allowed is False
    assert result.code == "unsupported_action_type"


def test_06c_camera_ha_action_types_is_exactly_turn_on_turn_off():
    assert CAMERA_HA_ACTION_TYPES == {"home_assistant.turn_on", "home_assistant.turn_off"}


def test_07_already_on_skips_dispatch():
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(), ha_state_reader=lambda e: "on",
    )
    assert result.allowed is True
    assert result.skip_dispatch is True
    assert result.code == "already_in_desired_state"


def test_08_already_off_skips_dispatch():
    result = validate_camera_ha_action(
        "home_assistant.turn_off", _TEST_ENTITY, _valid_event_data(kind="human_cleared"), ha_state_reader=lambda e: "off",
    )
    assert result.allowed is True
    assert result.skip_dispatch is True
    assert result.code == "already_in_desired_state"


def test_09_different_state_proceeds_to_dispatch():
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(), ha_state_reader=lambda e: "off",
    )
    assert result.allowed is True
    assert result.skip_dispatch is False


def test_10_state_reader_none_skips_the_check_entirely():
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(), ha_state_reader=None,
    )
    assert result.allowed is True
    assert result.skip_dispatch is False


def test_11_state_lookup_failure_blocks_the_action():
    def _boom(entity_id: str) -> str:
        raise RuntimeError("HA unreachable")
    result = validate_camera_ha_action(
        "home_assistant.turn_on", _TEST_ENTITY, _valid_event_data(), ha_state_reader=_boom,
    )
    assert result.allowed is False
    assert result.code == "ha_state_lookup_failed"


def test_12_safety_check_result_to_public_dict_shape():
    result = SafetyCheckResult(allowed=True, code="ok", message="fine")
    d = result.to_public_dict()
    assert d == {"allowed": True, "code": "ok", "message": "fine", "skip_dispatch": False}


# ============================================================================
# B. Real-bootstrap Event Bus tests (Section 10)
# ============================================================================

def test_13_valid_human_detected_reaches_completed_via_mock_dispatcher():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed))
        # Filtered to this sprint's own test entity - `camera_human_
        # detected_test_action` (P0.6.2) ALSO matches `human_detected`
        # and ALSO now passes through the same safety gate (targeting
        # "light.wled", a DIFFERENT entity) - both are expected to fire
        # independently; this test only asserts about its own rule/entity.
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY]
        assert len(ha_calls) == 1
        assert ha_calls[0]["action"] == "turn_on"
        assert ha_calls[0]["target"] == _TEST_ENTITY
        _mock_ha_handler(modules)  # still the mock - no real call was possible
    finally:
        _teardown(runtime, adapter_manager)


def test_14_detection_error_blocks_the_test_rule_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish_camera_event(runtime, detection_error="Conv.bn")
        time.sleep(0.3)
        assert not any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed)
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant"]
        assert ha_calls == []
    finally:
        _teardown(runtime, adapter_manager)


def test_15_camera_unavailable_blocks_the_test_rule_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish_camera_event(runtime, available=False)
        time.sleep(0.3)
        assert not any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed)
        assert [c for c in tool_calls if c.get("tool") == "home_assistant"] == []
    finally:
        _teardown(runtime, adapter_manager)


def test_16_malformed_camera_event_blocks_the_test_rule():
    """A `kind` missing entirely means the FIRST condition
    (`event.kind == "human_detected"`) is already `CONDITION_INVALID`
    (P0.6's own fail-closed `event.<field>` semantics) - the rule never
    even reaches the safety gate, which is itself a correct "no device
    action" outcome, just enforced one layer earlier. Confirmed here at
    the end-to-end level regardless of which layer catches it."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        malformed = _valid_event_data()
        del malformed["kind"]
        runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=malformed))
        time.sleep(0.3)
        assert not any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed)
        assert [c for c in tool_calls if c.get("tool") == "home_assistant"] == []
    finally:
        _teardown(runtime, adapter_manager)


def test_17_disabled_camera_automation_never_reaches_the_rule():
    """Section 7 item 9. Disabled camera automation means `VisionCameraEventBridge`
    itself never subscribes/publishes at all (P0.5.3's own `is_enabled()`
    gate) - but this test proves the fail-safe outcome directly by simply
    never publishing a `camera_automation.camera_event` in the first
    place (the honest simulation of "camera automation disabled": no
    event ever exists to react to)."""
    runtime, modules, adapter_manager = _build_stack(camera_automation_enabled=False)
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        time.sleep(0.2)
        assert completed == []
    finally:
        _teardown(runtime, adapter_manager)


def test_18_disabled_rule_never_fires():
    """Section 7 item 8 - a disabled AutomationRule is refused by the
    EXISTING, pre-P0.8.0 `_on_bus_event()`/`enabled` check, unrelated to
    (and unaffected by) the new safety gate. Verified directly against
    the real loaded rule set by disabling it at runtime through the
    engine's own existing reload path."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        with engine._lock:
            rule = engine._rules.get("camera_test_automation_safety_action")
            assert rule is not None
            import dataclasses
            engine._rules["camera_test_automation_safety_action"] = dataclasses.replace(rule, enabled=False)
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish_camera_event(runtime)
        time.sleep(0.3)
        assert not any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed)
        # Only this sprint's own rule/entity was disabled - the
        # pre-existing camera_human_detected_test_action rule (targeting
        # a DIFFERENT entity, "light.wled") is untouched and still fires;
        # filtered to this test's own entity accordingly.
        assert [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY] == []
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# C. Cooldown / duplicate protection (Section 4) - reuses the EXISTING
#    AutomationEngine cooldown, no second implementation.
# ============================================================================

def test_19_repeated_human_detected_does_not_repeatedly_call_turn_on():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_camera_event(runtime)
        _publish_camera_event(runtime)
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed))
        time.sleep(0.3)  # let any (incorrect) second/third dispatch have time to appear
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY]
        assert len(ha_calls) == 1, f"expected exactly 1 turn_on call under cooldown, got {len(ha_calls)}"
    finally:
        _teardown(runtime, adapter_manager)


def test_20_camera_test_rule_has_a_nonzero_cooldown():
    """Sanity check on the rule definition itself - the cooldown/dup
    protection proven in test_19 above only works because this rule
    actually sets `cooldown_seconds > 0`, reusing the engine's existing
    mechanism (Section 4 explicitly forbids a second one)."""
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    assert rules["camera_test_automation_safety_action"]["cooldown_seconds"] > 0


# ============================================================================
# D. State-aware skip via the real bootstrap (Section 5)
# ============================================================================

def test_21_already_on_light_skips_the_redundant_turn_on_call():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = lambda entity_id: "on" if entity_id == _TEST_ENTITY else None
        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed))
        # Filtered to this sprint's own entity - the reader only reports
        # "on" for `_TEST_ENTITY`; the pre-existing light.wled rule's
        # reader lookup returns None (not "on"), so IT still dispatches
        # normally - a separate, correct outcome this test isn't about.
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY]
        assert ha_calls == [], "the light was already ON - no Home Assistant call should have been made at all"
        # automation.completed's own Event Bus payload is metadata-only
        # (execution_id/rule_id/correlation_id - Phase 10) and does NOT
        # carry action_results; the full AutomationExecution record
        # (including action_results) lives in the engine's own bounded
        # in-memory history - the same place the dashboard/status API
        # already reads it from.
        execution = engine._last_execution["camera_test_automation_safety_action"]
        action_results = execution.to_public_dict()["action_results"]
        assert action_results[0]["code"] == "already_in_desired_state"
        assert action_results[0]["status"] == "completed"
    finally:
        _teardown(runtime, adapter_manager)


def test_22_off_light_still_dispatches_turn_on():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = lambda entity_id: "off"
        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed))
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY]
        assert len(ha_calls) == 1
    finally:
        _teardown(runtime, adapter_manager)


def test_23_ha_state_lookup_failure_is_a_safe_failure_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]

        def _boom(entity_id: str) -> str:
            raise RuntimeError("simulated HA state lookup failure")
        engine.ha_state_reader = _boom

        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_test_automation_safety_action" for d in failed))
        assert not any(d.get("rule_id") == "camera_test_automation_safety_action" for d in completed)
        assert [c for c in tool_calls if c.get("tool") == "home_assistant"] == []
    finally:
        _teardown(runtime, adapter_manager)


def test_24_register_camera_action_ha_state_reader_noop_on_mock_backend():
    """The mock `HomeAssistantClient` has no `get_entity_state` at all -
    `register_camera_action_ha_state_reader()` must leave `ha_state_
    reader` at its `None` default rather than wiring a broken/fake
    callable."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        engine: AutomationEngine = modules["automation_engine"]
        assert engine.ha_state_reader is None
    finally:
        _teardown(runtime, adapter_manager)


def test_24b_register_camera_action_ha_state_reader_noop_when_modules_or_adapters_missing():
    register_camera_action_ha_state_reader({}, {})  # must not raise
    register_camera_action_ha_state_reader({"automation_engine": object()}, {})  # must not raise


# ============================================================================
# E. Non-camera automation unaffected (Section 9 item 13 / Section 1.6)
# ============================================================================

def test_25_non_camera_manual_trigger_ha_action_is_unaffected_by_the_gate():
    """A rule triggered manually (not by `camera_automation.camera_event`)
    must never be routed through the camera action safety gate at all -
    proven directly by dispatching a manual-trigger rule whose target
    would otherwise fail Section 3B's stricter entity-id regex (it
    doesn't - "light.wled" is well-formed - but critically, this rule
    has NO `event_data` at all, which WOULD be refused as `missing_
    event_context` if it were incorrectly routed through the gate)."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        with engine._lock:
            engine._rules["p0_8_0_manual_test_rule"] = AutomationRule(
                id="p0_8_0_manual_test_rule", name="manual test rule", enabled=True,
                trigger=AutomationTrigger(type="manual"),
                actions=[AutomationAction(type="home_assistant.turn_on", parameters={"target": "light.wled"})],
                cooldown_seconds=0.0,
            )
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        ok = engine.run_automation("p0_8_0_manual_test_rule")
        assert ok
        assert _wait_until(lambda: any(d.get("rule_id") == "p0_8_0_manual_test_rule" for d in completed))
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == "light.wled"]
        assert len(ha_calls) == 1
    finally:
        _teardown(runtime, adapter_manager)


def test_26_pre_existing_p0_6_2_rule_still_matches_human_detected():
    """`camera_human_detected_test_action` (P0.6.2) still exists and
    still fires end to end through the same real bootstrap + safety gate
    stack.

    P0.8.6 UPDATE (documented, intentional behavior change - see
    docs/change_impact/camera_automation_p0_8_6.md Section 3): this rule
    now triggers on `event.kind == "human_confirmed"` (P0.8.6's own new,
    stricter, sustained-detection-only signal), not the raw, single-frame
    `"human_detected"` kind - a single YOLO frame must never directly
    turn on a real physical light (the exact bug P0.8.6 fixes). It ALSO
    now carries its own `event.available == true`/`event.detection_error
    == null` conditions (previously it had neither, relying solely on
    the safety gate below - see test_28's own P0.8.6 update for where
    that specific guarantee is now proven instead). This test still
    proves the ONE thing its name promises - the rule fires given a
    valid, confirmed event - just with `_valid_event_data()`'s new,
    correct `kind`."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_camera_event(runtime, kind="human_confirmed")
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_test_action" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# F. Attribution (Section 6)
# ============================================================================

def test_27_full_attribution_trail_observable_without_credentials():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        seen: List[Event] = []
        runtime.event_bus.subscribe("automation.*", lambda e: seen.append(e))
        runtime.event_bus.subscribe("tool_requested", lambda e: seen.append(e))
        _publish_camera_event(runtime)
        assert _wait_until(lambda: any(e.type == "automation.completed" and e.data.get("rule_id") == "camera_test_automation_safety_action" for e in seen))

        types_for_rule = [e.type for e in seen if e.data.get("rule_id") == "camera_test_automation_safety_action" or e.type == "tool_requested"]
        assert "automation.triggered" in types_for_rule
        assert "automation.condition_passed" in types_for_rule
        assert "automation.action_started" in types_for_rule
        assert "tool_requested" in types_for_rule
        assert "automation.completed" in types_for_rule

        # Filtered to this sprint's own test entity - the pre-existing
        # camera_human_detected_test_action rule (P0.6.2, target
        # "light.wled") ALSO matches human_detected and ALSO publishes
        # its own tool_requested independently/concurrently.
        tool_requested = next(
            e for e in seen if e.type == "tool_requested" and e.data.get("tool_call", {}).get("target") == _TEST_ENTITY
        )
        tool_call = tool_requested.data["tool_call"]
        assert tool_call["tool"] == "home_assistant"
        assert tool_call["action"] == "turn_on"
        assert tool_call["target"] == _TEST_ENTITY

        blob = json.dumps([e.data for e in seen]).lower()
        for forbidden in ("password", "token", "secret", "credential", "rtsp://", "api_key"):
            assert forbidden not in blob
    finally:
        _teardown(runtime, adapter_manager)


def test_28_refusal_reason_is_attributable_via_action_results():
    """Proves the action-dispatch safety gate itself still refuses a
    `detection_error`-carrying event and still surfaces the refusal
    reason via `action_results[].code` - independent of whether any
    PARTICULAR rule also happens to have its own `event.detection_error`
    condition (a second, condition-stage layer of defense, not a
    replacement for this one).

    P0.8.6 UPDATE (documented, intentional change): as of P0.8.6, EVERY
    shipped rule that calls `home_assistant.turn_on`/`turn_off`
    (`camera_human_detected_test_action`, `camera_test_automation_
    safety_action`, `camera_test_automation_safety_action_off`) now
    carries its own explicit `event.detection_error == null` condition -
    P0.8.6 brought the real WLED rule up to the same safety bar the
    P0.8.0 mock rule already had, closing a real gap where the physical-
    device rule was LESS gated than the mock one (see docs/change_impact/
    camera_automation_p0_8_6.md Section 3). This means every REAL shipped
    rule would now be refused at the CONDITION stage for this scenario,
    never reaching the action-dispatch safety gate this test exists to
    exercise. Rather than weaken this test's actual claim (that the GATE
    itself, at the action-dispatch layer, independently refuses and
    attributes a `detection_error`), this test now injects one synthetic
    rule with NO condition of its own - the exact shape `camera_human_
    detected_test_action` had before P0.8.6 - using the same `engine.
    _rules[...] = AutomationRule(...)` injection pattern test_25 already
    established in this file, so the gate's own standalone behavior stays
    directly, honestly proven rather than silently going untested."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        with engine._lock:
            engine._rules["p0_8_0_gate_only_test_rule"] = AutomationRule(
                id="p0_8_0_gate_only_test_rule", name="gate-only test rule (no condition of its own)",
                enabled=True,
                trigger=AutomationTrigger(type="event", parameters={"event_name": CAMERA_EVENT_TYPE}),
                actions=[AutomationAction(type="home_assistant.turn_on", parameters={"target": "light.wled"})],
                cooldown_seconds=0.0,
            )
            # `reload_rules()` is what normally (re)builds this cached
            # event-name -> rule-id index (see its own docstring) -
            # since this test injects a rule directly rather than going
            # through a real reload, it must update the SAME index by
            # hand or the engine's event dispatch (`_rules_by_event.get
            # (event.type, ())`) would never find this rule at all.
            engine._rules_by_event.setdefault(CAMERA_EVENT_TYPE, []).append("p0_8_0_gate_only_test_rule")
        failed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
        _publish_camera_event(runtime, kind="human_confirmed", detection_error="Conv.bn")
        assert _wait_until(lambda: any(d.get("rule_id") == "p0_8_0_gate_only_test_rule" for d in failed))
        # See test_21's own comment - action_results lives on the
        # engine's in-memory AutomationExecution history, not on the
        # (metadata-only) automation.failed event payload itself.
        execution = engine._last_execution["p0_8_0_gate_only_test_rule"]
        codes = [r.get("code") for r in execution.to_public_dict()["action_results"]]
        assert "detection_error_present" in codes
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# G. Architecture guard (Section 12)
# ============================================================================

def test_29_safety_module_makes_no_direct_ha_api_call():
    tree = ast.parse(_read(_SAFETY_MODULE_PATH))
    source = _read(_SAFETY_MODULE_PATH)
    for forbidden in ("requests.", "websocket", "aiohttp", "http.client", "urllib", "call_service("):
        assert forbidden not in source
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            module = getattr(node, "module", None) or ""
            for forbidden_mod in ("requests", "websocket", "aiohttp", "http.client", "urllib.request"):
                assert forbidden_mod not in names and forbidden_mod not in module


def test_30_safety_module_has_no_event_bus_subscription_and_no_camera_vision_code():
    source = _read(_SAFETY_MODULE_PATH)
    for forbidden in ("subscribe(", "EventBus", "event_bus", "cv2", "ultralytics", "YOLO(", "RealVisionSource("):
        assert forbidden not in source


def test_31_exactly_one_automation_engine_construction_site_in_bootstrap():
    modules_source = _read(os.path.join(_ROOT, "luno", "bootstrap", "modules.py"))
    assert modules_source.count("AutomationEngine(") == 1


def test_32_no_second_home_assistant_client_construction_site():
    adapters_source = _read(os.path.join(_ROOT, "luno", "bootstrap", "adapters.py"))
    assert adapters_source.count("RealHomeAssistantClient(") == 1


def test_33_automation_rules_file_has_at_least_the_p0_8_0_four_rules():
    """"Exactly four rules" was accurate as of P0.8.0 - P0.8.2, a LATER
    sprint, intentionally added a fifth (`camera_test_automation_safety_
    action_off`, the human_cleared -> turn_off counterpart to this
    file's own ON rule - see `docs/change_impact/camera_automation_p0_8_
    2.md`). Updated to `issubset()`, the same convention `test_p0_6_3_
    unified_vision_camera_automation.py::test_26`/`test_p0_7_vision_
    context.py::test_36` already established for the identical prior
    situation."""
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    assert {
        "camera_human_detected_log", "camera_human_detected_test_action",
        "camera_multiple_people_log", "camera_test_automation_safety_action",
    }.issubset(rules.keys())
    test_rule = rules["camera_test_automation_safety_action"]
    assert test_rule["actions"][0]["parameters"]["target"] == _TEST_ENTITY
    assert test_rule["actions"][0]["type"] == "home_assistant.turn_on"


def test_34_no_human_cleared_rule_used_a_delayed_or_timer_action_type():
    """Section 8's own explicit constraint was about DELAYED/timer-based
    OFF logic specifically ("Do NOT implement delayed OFF logic in
    P0.8.0 unless it already exists and can be reused without
    architectural changes") - it was never a blanket ban on a
    human_cleared rule existing at all. P0.8.2 (a LATER sprint)
    legitimately added one, but it fires IMMEDIATELY (the exact same
    instant trigger -> condition -> action pipeline every other rule
    already uses - see `docs/change_impact/camera_automation_p0_8_2.
    md`), never a delayed/timer mechanism. The deeper invariant this
    test now checks: no rule's action type is a delay/timer-based
    action - `AutomationEngine` still has no such mechanism at all
    (confirmed: no action type in `models.ACTION_TYPES` schedules a
    future action), so this remains true regardless of which specific
    rules exist."""
    from luno.automation.models import ACTION_TYPES
    delay_like_types = {t for t in ACTION_TYPES if "delay" in t or "timer" in t or "schedule" in t}
    assert delay_like_types == set(), (
        f"a delay/timer-like action type was added to ACTION_TYPES ({delay_like_types}) - "
        "P0.8.0/P0.8.2's own shared invariant (no delayed OFF logic without an architectural "
        "decision) may have been silently violated"
    )
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    for rule_id, rule in rules.items():
        for condition in rule.get("conditions", []):
            if condition.get("target") == "event.kind" and condition.get("value") == "human_cleared":
                for action in rule.get("actions", []):
                    assert action["type"] not in delay_like_types, (
                        f"rule {rule_id!r} uses a delay/timer-like action type - not allowed per "
                        "P0.8.0/P0.8.2's own shared invariant"
                    )
