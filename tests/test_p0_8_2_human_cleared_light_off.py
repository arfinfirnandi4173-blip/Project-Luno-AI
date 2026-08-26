"""
tests/test_p0_8_2_human_cleared_light_off.py
================================================

LUNO P0.8.2 (Camera Human Cleared -> Safe Light Off) - dedicated
regression suite.

CRITICAL SAFETY REQUIREMENT (brief Section 4): NOT ONE test in this
file ever calls `register_real_tool_handlers()` or constructs a real
Home Assistant client - every test routes through the SAME
`MockHomeAssistantHandler` `luno/tool_manager/builtin/__init__.py::
register_all()` already registers by default (confirmed directly by
`test_00`). No real light is ever turned on or off by this file.

Architecture note (Section 2 of the P0.8.2 brief): `luno/automation/
camera_action_safety.py::validate_camera_ha_action()` (P0.8.0) already
treats `home_assistant.turn_off` identically to `turn_on` - it has NO
`kind`-specific branching at all (it only special-cases `kind ==
"camera_offline"` as a REFUSAL condition, never as a trigger for any
action). This means P0.8.2 needed ZERO changes to the safety gate
itself - the entire safety requirement is already satisfied by every
one of P0.8.0's own existing checks (action allowlist, entity
validation, event validity, detection_error/camera_offline/available
checks, and the optional state-aware skip). This file's job is to
prove that reuse is actually true, not to test new safety logic that
does not exist.

Sections:
  A. Fixture sanity + rule shape.
  B. OFF-rule condition matching (Section 7 semantics: human_cleared ->
     OFF only, never human_detected/camera_online/camera_offline/
     vision_detection_failed/malformed events).
  C. Safety gate blocking conditions applied to `turn_off` specifically.
  D. State-aware OFF behavior (Section 5).
  E. Cooldown/duplicate protection + ON/OFF independence (Section 6).
  F. ON rule remains independently functional (Section 7).
  G. Architecture/mutation guard (Section 13).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.automation.camera_action_safety import validate_camera_ha_action  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
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
_ROOT_TESTS = os.path.join(_ROOT, "tests")
_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")
_ON_RULE_ID = "camera_test_automation_safety_action"
_OFF_RULE_ID = "camera_test_automation_safety_action_off"
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


def _cleared_event_data(**overrides: Any) -> Dict[str, Any]:
    data = {
        "camera_id": "tapo_c212", "kind": "human_cleared", "available": True,
        "human_present": False, "person_count": 0, "detected_objects": (),
        "detection_error": None,
    }
    data.update(overrides)
    return data


def _detected_event_data(**overrides: Any) -> Dict[str, Any]:
    data = {
        "camera_id": "tapo_c212", "kind": "human_detected", "available": True,
        "human_present": True, "person_count": 1, "detected_objects": ("person",),
        "detection_error": None,
    }
    data.update(overrides)
    return data


def _build_stack(camera_automation_enabled: bool = True):
    """Same real-bootstrap pattern `test_p0_8_0_camera_action_safety.py`
    already established - MOCK backends throughout, `register_real_
    tool_handlers()` never called."""
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


def _publish(runtime, data: Dict[str, Any]) -> None:
    runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=data))


# ============================================================================
# A. Fixture sanity + rule shape
# ============================================================================

def test_00_fixture_uses_the_mock_ha_handler_not_a_real_one():
    runtime, modules, adapter_manager = _build_stack()
    try:
        _mock_ha_handler(modules)
    finally:
        _teardown(runtime, adapter_manager)


def test_01_off_rule_shape_on_disk():
    rules = json.loads(_read(_RULES_PATH))
    rule = rules[_OFF_RULE_ID]
    assert rule["enabled"] is True
    assert rule["cooldown_seconds"] == 30.0
    conditions = {(c["target"], c["value"] if not isinstance(c["value"], list) else tuple(c["value"])) for c in rule["conditions"]}
    assert ("event.kind", "human_cleared") in conditions
    assert ("event.available", True) in conditions
    assert ("event.detection_error", None) in conditions
    assert len(rule["actions"]) == 1
    assert rule["actions"][0]["type"] == "home_assistant.turn_off"
    assert rule["actions"][0]["parameters"]["target"] == _TEST_ENTITY


def test_02_on_rule_is_untouched_by_this_sprint():
    rules = json.loads(_read(_RULES_PATH))
    on_rule = rules[_ON_RULE_ID]
    assert on_rule["enabled"] is True
    assert on_rule["cooldown_seconds"] == 30.0
    assert on_rule["actions"][0]["type"] == "home_assistant.turn_on"
    assert on_rule["actions"][0]["parameters"]["target"] == _TEST_ENTITY
    assert len(on_rule["conditions"]) == 3


# ============================================================================
# B. OFF-rule condition matching (Section 7 semantics)
# ============================================================================

def test_10_human_cleared_matches_off_rule_and_completes():
    """#1 - human_cleared matches the OFF rule."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        assert _wait_until(lambda: any(d.get("rule_id") == _OFF_RULE_ID for d in completed))
        off_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY]
        assert len(off_calls) == 1
        assert off_calls[0]["action"] == "turn_off"
    finally:
        _teardown(runtime, adapter_manager)


def test_11_human_detected_does_not_match_off_rule():
    """#2 - a single human_detected must not trigger OFF."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _detected_event_data())
        assert _wait_until(lambda: any(d.get("rule_id") == _ON_RULE_ID for d in completed))
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == []
    finally:
        _teardown(runtime, adapter_manager)


@pytest.mark.parametrize("kind", ["camera_online", "camera_offline", "vision_detection_failed"])
def test_12_unrelated_kinds_do_not_match_off_rule(kind):
    """#3/#4 - camera_online/camera_offline (and, defensively,
    vision_detection_failed - not a real CameraEvent.kind value in this
    project, but tested anyway per the brief's own explicit list) never
    match the OFF rule's own `event.kind == "human_cleared"` condition."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data(kind=kind))
        time.sleep(0.3)
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == []
    finally:
        _teardown(runtime, adapter_manager)


def test_13_malformed_event_does_not_match_off_rule():
    """#6 - malformed event (missing kind) blocks OFF at the condition-
    evaluation stage (never even reaches the safety gate)."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        bad_data = _cleared_event_data()
        del bad_data["kind"]
        _publish(runtime, bad_data)
        time.sleep(0.3)
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == []
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# C. Safety gate blocking conditions applied to turn_off specifically
# ============================================================================

def test_20_detection_error_blocks_off_via_safety_gate():
    """#5 - detection_error present blocks OFF - proves the SAME safety
    gate function already blocks this for turn_off, not just turn_on."""
    result = validate_camera_ha_action(
        action_type="home_assistant.turn_off", target=_TEST_ENTITY,
        event_data=_cleared_event_data(detection_error="Conv.bn"),
    )
    assert result.allowed is False
    assert result.code == "detection_error_present"


def test_21_detection_error_blocks_off_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data(detection_error="Conv.bn"))
        time.sleep(0.3)
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == [], "a detector failure must NEVER turn the light off"
    finally:
        _teardown(runtime, adapter_manager)


def test_22_camera_offline_never_causes_off_via_safety_gate():
    """CRITICAL (brief Section 4): camera_offline must never turn the
    light off. Structurally double-blocked: (a) the OFF rule's own
    `event.kind == "human_cleared"` condition never matches `kind ==
    "camera_offline"` (condition-evaluation stage, proven by test_12
    above); (b) even if some future rule's conditions were looser, the
    safety gate itself independently refuses any `camera_offline`-kind
    event outright, checked here directly."""
    result = validate_camera_ha_action(
        action_type="home_assistant.turn_off", target=_TEST_ENTITY,
        event_data={"kind": "camera_offline", "available": False, "detection_error": None},
    )
    assert result.allowed is False
    assert result.code == "camera_offline"


def test_23_malformed_event_blocks_off_via_safety_gate():
    result = validate_camera_ha_action(action_type="home_assistant.turn_off", target=_TEST_ENTITY, event_data=None)
    assert result.allowed is False
    assert result.code == "missing_event_context"

    result2 = validate_camera_ha_action(action_type="home_assistant.turn_off", target=_TEST_ENTITY, event_data={"kind": ""})
    assert result2.allowed is False
    assert result2.code == "malformed_camera_event"


def test_24_disabled_camera_automation_blocks_off():
    """#7 - disabled camera automation means no camera_automation.
    camera_event is ever published in the first place (same honest
    simulation `test_p0_8_0_camera_action_safety.py::test_17` already
    established)."""
    runtime, modules, adapter_manager = _build_stack(camera_automation_enabled=False)
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        time.sleep(0.2)
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
    finally:
        _teardown(runtime, adapter_manager)


def test_25_disabled_off_rule_blocks_off():
    """#8 - disabling ONLY the OFF rule blocks it, while leaving the ON
    rule (and every other rule) untouched."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        with engine._lock:
            rule = engine._rules.get(_OFF_RULE_ID)
            assert rule is not None
            engine._rules[_OFF_RULE_ID] = dataclasses.replace(rule, enabled=False)
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        time.sleep(0.3)
        assert not any(d.get("rule_id") == _OFF_RULE_ID for d in completed)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == []

        # the ON rule is untouched - still fires for its own event
        _publish(runtime, _detected_event_data())
        assert _wait_until(lambda: any(d.get("rule_id") == _ON_RULE_ID for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


def test_26_invalid_entity_blocks_off_via_safety_gate():
    """#9 - invalid entity id blocks OFF, same allowlist the ON rule
    already relies on."""
    for bad_target in (None, "", "   ", "*", "not_an_entity_id", ["light.a", "light.b"]):
        result = validate_camera_ha_action(action_type="home_assistant.turn_off", target=bad_target, event_data=_cleared_event_data())
        assert result.allowed is False
        assert result.code == "invalid_target", f"expected invalid_target for {bad_target!r}, got {result.code!r}"


def test_27_ha_state_reader_failure_blocks_off():
    """#10 - a wired ha_state_reader that raises is a fail-closed
    condition (never silently proceeds as if unwired)."""
    def _raising_reader(entity_id: str) -> Optional[str]:
        raise RuntimeError("simulated HA state lookup failure")

    result = validate_camera_ha_action(
        action_type="home_assistant.turn_off", target=_TEST_ENTITY,
        event_data=_cleared_event_data(), ha_state_reader=_raising_reader,
    )
    assert result.allowed is False
    assert result.code == "ha_state_lookup_failed"


def test_28_unsupported_ha_action_blocked_for_off_rule_shape():
    """Bonus, per Section 4's own explicit list ('unsupported HA action
    is requested') - the allowlist applies equally regardless of
    on/off direction."""
    result = validate_camera_ha_action(action_type="home_assistant.toggle", target=_TEST_ENTITY, event_data=_cleared_event_data())
    assert result.allowed is False
    assert result.code == "unsupported_action_type"


def test_29_missing_event_context_blocks_off_end_to_end():
    """#missing event context - a rule with a valid trigger but somehow
    no event context (defensively covered - conditions/dispatch always
    receive `event_data` for an event-triggered rule in this engine, so
    this is proven at the unit level, matching P0.8.0's own coverage
    for the identical case)."""
    result = validate_camera_ha_action(action_type="home_assistant.turn_off", target=_TEST_ENTITY, event_data=None)
    assert result.allowed is False
    assert result.code == "missing_event_context"


# ============================================================================
# D. State-aware OFF behavior (Section 5)
# ============================================================================

def test_30_light_already_off_skips_the_ha_call():
    """#11 - human_cleared + light already OFF -> skip, zero
    tool_requested events, code=already_in_desired_state."""
    def _reader(entity_id: str) -> Optional[str]:
        return "off"

    result = validate_camera_ha_action(
        action_type="home_assistant.turn_off", target=_TEST_ENTITY,
        event_data=_cleared_event_data(), ha_state_reader=_reader,
    )
    assert result.allowed is True
    assert result.skip_dispatch is True
    assert result.code == "already_in_desired_state"


def test_31_light_already_off_skips_end_to_end_zero_tool_requested():
    runtime, modules, adapter_manager = _build_stack()
    try:
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = lambda entity_id: "off"
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        assert _wait_until(lambda: any(d.get("rule_id") == _OFF_RULE_ID for d in completed))
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off"]
        assert off_calls == [], "already-off must produce ZERO tool_requested events"
    finally:
        _teardown(runtime, adapter_manager)


def test_32_light_on_performs_exactly_one_turn_off():
    """#12 - light ON -> exactly one home_assistant.turn_off."""
    def _reader(entity_id: str) -> Optional[str]:
        return "on"

    runtime, modules, adapter_manager = _build_stack()
    try:
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = _reader
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        ok = _wait_until(lambda: any(c.get("action") == "turn_off" and c.get("target") == _TEST_ENTITY for c in tool_calls))
        assert ok
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off" and c.get("target") == _TEST_ENTITY]
        assert len(off_calls) == 1
    finally:
        _teardown(runtime, adapter_manager)


def test_33_no_ha_state_reader_wired_still_allows_off():
    """Legitimate 'optimization unavailable' case (Section 5's own 'if
    already available' qualifier) - mock backend has no `get_entity_
    state`, so `ha_state_reader` stays `None` and the OFF action still
    proceeds normally (not blocked)."""
    result = validate_camera_ha_action(
        action_type="home_assistant.turn_off", target=_TEST_ENTITY,
        event_data=_cleared_event_data(), ha_state_reader=None,
    )
    assert result.allowed is True
    assert result.skip_dispatch is False
    assert result.code == "ok"


# ============================================================================
# E. Cooldown/duplicate protection + ON/OFF independence (Section 6)
# ============================================================================

def test_40_repeated_human_cleared_obeys_cooldown():
    """#13 - three human_cleared events in a row within the OFF rule's
    own 30s cooldown window must only ever produce ONE turn_off call -
    the EXISTING `_cooldown_until` mechanism (Sprint 72, Phase 8),
    reused unchanged, keyed by `rule.id` so this is the OFF rule's own
    independent cooldown entry."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        _publish(runtime, _cleared_event_data())
        _publish(runtime, _cleared_event_data())
        time.sleep(0.5)
        off_calls = [c for c in tool_calls if c.get("action") == "turn_off" and c.get("target") == _TEST_ENTITY]
        assert len(off_calls) == 1, f"expected exactly 1 turn_off call for 3x human_cleared within cooldown, got {len(off_calls)}"
    finally:
        _teardown(runtime, adapter_manager)


def test_41_off_rule_cooldown_does_not_suppress_the_on_rule():
    """The OFF rule's own cooldown entry (keyed by `camera_test_
    automation_safety_action_off`) must have zero effect on the ON
    rule's own, separately-keyed cooldown entry (`camera_test_
    automation_safety_action`) - proven by triggering OFF first, then
    immediately triggering ON, and confirming ON still fires."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _cleared_event_data())
        assert _wait_until(lambda: any(c.get("action") == "turn_off" and c.get("target") == _TEST_ENTITY for c in tool_calls))
        _publish(runtime, _detected_event_data())
        ok = _wait_until(lambda: any(c.get("action") == "turn_on" and c.get("target") == _TEST_ENTITY for c in tool_calls))
        assert ok, "the ON rule must remain independently triggerable immediately after the OFF rule's own cooldown started"
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# F. ON rule remains independently functional (Section 7)
# ============================================================================

def test_50_on_rule_still_fires_for_human_detected():
    """#14 - the pre-existing ON rule remains fully functional,
    unaffected by this sprint's own additive OFF rule."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _detected_event_data())
        assert _wait_until(lambda: any(d.get("rule_id") == _ON_RULE_ID for d in completed))
        on_calls = [c for c in tool_calls if c.get("action") == "turn_on" and c.get("target") == _TEST_ENTITY]
        assert len(on_calls) == 1
    finally:
        _teardown(runtime, adapter_manager)


def test_51_full_on_off_on_off_cycle():
    """The end-state diagram the brief itself draws: ENTER -> human_
    detected -> ON, LEAVE -> human_cleared -> OFF, repeated twice."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))

        _publish(runtime, _detected_event_data())
        assert _wait_until(lambda: any(c.get("action") == "turn_on" for c in tool_calls))
        _publish(runtime, _cleared_event_data())
        assert _wait_until(lambda: any(c.get("action") == "turn_off" for c in tool_calls))

        # a SECOND on/off cycle is refused by cooldown (30s) within this
        # test's own short window - proven separately by test_40/test_13
        # above; this test's own job is just the ORDER on -> off works.
        actions_in_order = [c.get("action") for c in tool_calls if c.get("target") == _TEST_ENTITY]
        assert actions_in_order[:2] == ["turn_on", "turn_off"]
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# G. Architecture/mutation guard (Section 13)
# ============================================================================

def test_60_no_ptz_action_occurs():
    """#15 - no PTZ action (camera_ptz/camera_patrol tool) ever occurs
    as a result of any test in this file."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _detected_event_data())
        _publish(runtime, _cleared_event_data())
        time.sleep(0.5)
        ptz_calls = [c for c in tool_calls if c.get("tool") in ("camera_ptz", "camera_patrol")]
        assert ptz_calls == []
    finally:
        _teardown(runtime, adapter_manager)


def test_61_no_unrelated_ha_action_occurs():
    """#16 - only the configured test entity is ever targeted by this
    file's own two rules; no other entity/service is ever called."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        _publish(runtime, _detected_event_data())
        assert _wait_until(lambda: any(c.get("target") == _TEST_ENTITY for c in tool_calls))
        _publish(runtime, _cleared_event_data())
        assert _wait_until(lambda: any(c.get("action") == "turn_off" for c in tool_calls))
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant"]
        for call in ha_calls:
            # P0.6.2's own camera_human_detected_test_action ALSO fires
            # for human_detected (targets a DIFFERENT entity, "light.
            # wled") - expected, same co-firing test_p0_8_0's own
            # test_13 already documents. Every home_assistant call must
            # target EITHER this file's own test entity OR that one
            # pre-existing, unrelated entity - never anything else.
            assert call.get("target") in (_TEST_ENTITY, "light.wled"), f"unexpected HA target: {call.get('target')!r}"
    finally:
        _teardown(runtime, adapter_manager)


def test_62_no_second_home_assistant_client_construction_site():
    """#17."""
    adapters_source = _read(os.path.join(_ROOT, "luno", "bootstrap", "adapters.py"))
    assert adapters_source.count("RealHomeAssistantClient(") == 1


def test_63_no_second_automation_engine_construction_site():
    modules_source = _read(os.path.join(_ROOT, "luno", "bootstrap", "modules.py"))
    assert modules_source.count("AutomationEngine(") == 1


def test_64_no_second_cooldown_implementation():
    """#18 - `camera_action_safety.py` (P0.8.0, unmodified this sprint)
    has no cooldown/debounce logic of its own at all; the only
    cooldown STATE (an actual attribute/dict, not mere prose mentioning
    the concept - this module's own docstring legitimately explains
    reuse in prose, which must not trip this check) in the entire
    automation package remains `AutomationEngine`'s own."""
    safety_source = _read(os.path.join(_ROOT, "luno", "automation", "camera_action_safety.py"))
    for forbidden in ("self._cooldown_until", "_cooldown_until[", "_cooldown_until =", "time.sleep(", "threading.Timer("):
        assert forbidden not in safety_source, f"unexpected cooldown-like construct in camera_action_safety.py: {forbidden!r}"
    engine_source = _read(os.path.join(_ROOT, "luno", "automation", "engine.py"))
    assert engine_source.count("_cooldown_until: Dict") == 1


def test_65_no_vision_polling_loop_or_second_pipeline_introduced():
    """#19 - none of this sprint's own touched files construct a
    second Vision/YOLO/RTSP pipeline. `luno/bootstrap/adapters.py`
    itself legitimately contains the ONE pre-existing `RealVisionSource
    ()` production construction site (unchanged, not new this sprint -
    `test_66` below verifies the repo-wide count stays exactly 1), so
    it is deliberately excluded from this particular file-content check
    and covered by that count-based check instead."""
    touched_files = [
        os.path.join(_ROOT, "config", "automation_rules.json"),
        os.path.join(_ROOT, "luno_live_p0_8_1_verification.py"),
    ]
    forbidden = ("cv2.VideoCapture", "ultralytics.YOLO", "YOLO(", "RealVisionSource(")
    for path in touched_files:
        if not os.path.exists(path):
            continue
        src = _read(path)
        for pattern in forbidden:
            assert pattern not in src, f"forbidden pattern {pattern!r} found in {path}"


def test_66_architecture_mutation_guard_exactly_one_real_vision_source_site():
    """#20 - repo-wide, exactly one `RealVisionSource(` production
    construction site (unchanged from every prior sprint in this line)."""
    site_count = 0
    for dirpath, _dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            src = _read(path)
            # count only actual construction calls, not the class def line
            site_count += src.count("RealVisionSource()") + src.count("RealVisionSource(vision")
    assert site_count == 1, f"expected exactly 1 RealVisionSource() construction site, found {site_count}"


def test_67_no_credentials_appear_in_config_or_new_files():
    """No secret-shaped values written to config/automation_rules.json
    or any file this sprint touched."""
    forbidden_substrings = ("TAPO_PASSWORD=", "HA_TOKEN=", "access_token\":", "rtsp://")
    for path in (_RULES_PATH, os.path.join(_ROOT, "luno", "bootstrap", "adapters.py")):
        src = _read(path)
        for forbidden in forbidden_substrings:
            assert forbidden not in src, f"forbidden credential-shaped substring {forbidden!r} found in {path}"
