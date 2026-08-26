"""
tests/test_p0_8_9_wled_off_debounce.py
=========================================

LUNO P0.8.9 (Implement the missing WLED OFF automation rule) - dedicated
regression suite. See docs/change_impact/camera_automation_p0_8_9.md for
the full design writeup.

Context: P0.8.8 fixed the permanent-suppression bug so `human_confirmed`
correctly turns `light.wled` ON every time a person is (re-)confirmed.
There was, however, no corresponding OFF rule at all - `config/
automation_rules.json`'s only OFF rule (`camera_test_automation_safety_
action_off`) targets the mock `light.test_camera_automation` entity, not
the real `light.wled`. This sprint adds `camera_wled_human_cleared_off`,
which turns `light.wled` OFF ten seconds after `human_cleared`, UNLESS a
fresh `human_confirmed` arrives before that delay elapses (in which case
the pending OFF is cancelled and the light simply stays on).

Mechanism (see `luno/automation/engine.py`'s own new docstrings for the
full design rationale): NO new scheduler/timer was invented. The rule
schema gained one optional action parameter - `delay_seconds` - honored
ONLY for `home_assistant.turn_on`/`turn_off` actions
(`models.py::validate_action()`). `AutomationEngine._dispatch_home_
assistant_action()` schedules a delayed dispatch via the EXISTING,
already-reused `runtime.scheduler` (`luno.core.scheduler.Scheduler.
schedule_once()`/`cancel()` - the same primitive this engine already used
for TIME triggers and cooldown cleanup before this sprint). Cancellation
is entity-target-keyed, not rule-id-keyed: ANY new Home Assistant
dispatch (immediate or delayed) for a given `target` entity first cancels
whatever was previously pending for that SAME target
(`_cancel_pending_delayed_action()`). This one small, generic rule is
what makes BOTH "a fresh human_confirmed cancels a pending OFF" and "a
repeated human_cleared resets its own pending OFF's debounce window
instead of double-scheduling" fall out of a single mechanism, with zero
new coupling between the ON and OFF rules (neither rule refers to the
other's id anywhere).

Honesty discipline preserved throughout (same as every P0.8.x sprint):
these tests prove Stages A-D of the brief's own A-F framework (person/
event -> camera event published -> AutomationEngine received it -> a
real, mocked `home_assistant.turn_off`/`turn_on` call was dispatched with
the correct entity id and at the correct time). None of this proves
Stage E (a fresh HA state read-back - already covered by P0.8.7's own
suite, unmodified by this sprint) or Stage F (physical WLED illumination
- outside this codebase's observability, as documented in every prior
change-impact doc). No test here claims otherwise.

Sections:
  A. Model/schema validation for the new `delay_seconds` action parameter
     (fast, no bootstrap - proves the schema change is additive and
     correctly bounded).
  B. Real config/automation_rules.json sanity - the ACTUAL new rule
     (`camera_wled_human_cleared_off`) targets `light.wled`, has a 10s
     delay, and the two PRE-EXISTING rules this sprint must not regress
     (`camera_human_detected_test_action`, `camera_test_automation_
     safety_action_off`) are byte-for-byte unchanged.
  C. End-to-end engine/scheduler behavior (real bootstrap, real
     `runtime.scheduler`, MOCK HA backend only) - required scenarios
     1-6, 9 from the brief, using a short test-only delay so the suite
     stays fast.
  D. Real production call path - `VisionCameraEventBridge._on_person_
     confirmed()`/`_on_person_left()` (not a mocked helper) driving the
     exact same ON -> pending OFF -> cancelled-by-re-confirm -> genuine
     OFF sequence end to end.
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

from luno.adapters.events import HumanPresenceConfirmed, CameraPersonLeft  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import (  # noqa: E402
    AutomationAction,
    AutomationRuleError,
    validate_action,
)
from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.camera_automation.cameras import CameraEvent  # noqa: E402
from luno.camera_automation.vision_bridge import VisionCameraEventBridge  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.1)
_REAL_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")
_TEST_ENTITY = "light.p0_8_9_test"
#: Short test-only delay - the REAL production rule uses 10.0s
#: (config/automation_rules.json); Section B proves that value directly
#: against the real file. Everywhere else uses this short value purely so
#: the suite runs quickly against the REAL scheduler tick loop, never a
#: fake/simulated clock.
_TEST_DELAY_S = 0.4


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mk_event(kind: str, camera_id: str = "tapo_c212") -> CameraEvent:
    return CameraEvent(
        camera_id=camera_id, kind=kind, entity_id=f"vision:{kind}",
        old_state=None, new_state=None, confidence=None,
        timestamp=time.time(), source="vision",
    )


# ============================================================================
# A. Model/schema validation - delay_seconds.
# ============================================================================

def test_A1_delay_seconds_valid_value_accepted():
    validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": 10.0}))


def test_A2_delay_seconds_zero_accepted_same_as_absent():
    validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": 0.0}))


def test_A3_delay_seconds_absent_still_valid_backward_compat():
    """Every rule this project had before P0.8.9 never set this
    parameter at all - must remain valid, unchanged."""
    validate_action(AutomationAction(type="home_assistant.turn_on", parameters={"target": "light.wled"}))


def test_A4_delay_seconds_negative_rejected():
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": -1.0}))


def test_A5_delay_seconds_too_large_rejected():
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": 100000.0}))


def test_A6_delay_seconds_non_numeric_rejected():
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": "soon"}))


def test_A7_delay_seconds_bool_rejected():
    """`isinstance(True, int)` is `True` in Python - explicitly excluded
    so a typo'd `"delay_seconds": true` is refused, not silently coerced
    to `1.0`."""
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="home_assistant.turn_off", parameters={"target": "light.wled", "delay_seconds": True}))


def test_A8_delay_seconds_rejected_for_non_ha_action_type():
    with pytest.raises(AutomationRuleError):
        validate_action(AutomationAction(type="automation.log", parameters={"message": "x", "delay_seconds": 5.0}))


# ============================================================================
# B. Real config/automation_rules.json sanity.
# ============================================================================

def _load_real_rules() -> Dict[str, Any]:
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_B1_new_off_rule_exists_and_targets_light_wled():
    rules = _load_real_rules()
    assert "camera_wled_human_cleared_off" in rules
    rule = rules["camera_wled_human_cleared_off"]
    assert rule["enabled"] is True
    actions = rule["actions"]
    assert len(actions) == 1
    assert actions[0]["type"] == "home_assistant.turn_off"
    assert actions[0]["parameters"]["target"] == "light.wled", "must target the REAL light, not light.test_camera_automation"


def test_B2_new_off_rule_uses_a_10_second_delay():
    rules = _load_real_rules()
    delay = rules["camera_wled_human_cleared_off"]["actions"][0]["parameters"]["delay_seconds"]
    assert delay == 10.0


def test_B3_new_off_rule_triggers_on_human_cleared():
    rules = _load_real_rules()
    conditions = rules["camera_wled_human_cleared_off"]["conditions"]
    kind_conditions = [c for c in conditions if c["target"] == "event.kind"]
    assert len(kind_conditions) == 1
    assert kind_conditions[0]["value"] == "human_cleared"


def test_B4_new_off_rule_validates_cleanly_via_real_engine_loader():
    """Loads the REAL file through the REAL `AutomationEngine._load_rules_
    from_disk()` path (not just raw json.load) - proves the rule is
    actually loadable, not merely well-formed JSON."""
    engine = AutomationEngine(rules_path=_REAL_RULES_PATH)
    loaded = engine._load_rules_from_disk()
    assert "camera_wled_human_cleared_off" in loaded
    rule = loaded["camera_wled_human_cleared_off"]
    assert rule.enabled is True
    assert rule.actions[0].parameters.get("target") == "light.wled"
    assert rule.actions[0].parameters.get("delay_seconds") == 10.0


def test_B5_existing_on_rule_unchanged():
    """The existing WLED ON rule (`camera_human_detected_test_action`)
    must be completely unaffected by this sprint - same trigger,
    conditions, action, target, cooldown as before P0.8.9."""
    rules = _load_real_rules()
    rule = rules["camera_human_detected_test_action"]
    assert rule["enabled"] is True
    assert rule["cooldown_seconds"] == 30.0
    actions = rule["actions"]
    assert len(actions) == 1
    assert actions[0]["type"] == "home_assistant.turn_on"
    assert actions[0]["parameters"] == {"target": "light.wled"}, "must have no delay_seconds - immediate dispatch, unchanged"
    kind_conditions = [c for c in rule["conditions"] if c["target"] == "event.kind"]
    assert kind_conditions[0]["value"] == "human_confirmed"


def test_B6_existing_test_entity_off_rule_unchanged():
    """The pre-existing mock-entity OFF rule (`camera_test_automation_
    safety_action_off`) must be untouched - still targets `light.test_
    camera_automation`, not `light.wled`, and has no delay_seconds."""
    rules = _load_real_rules()
    rule = rules["camera_test_automation_safety_action_off"]
    assert rule["enabled"] is True
    assert rule["cooldown_seconds"] == 30.0
    actions = rule["actions"]
    assert actions[0]["type"] == "home_assistant.turn_off"
    assert actions[0]["parameters"] == {"target": "light.test_camera_automation"}


def test_B7_real_rules_file_has_exactly_six_rules():
    """Guards against an accidental duplicate/renamed rule - five
    pre-existing rules (P0.6-P0.8.2) plus exactly one new one this
    sprint (P0.8.9) adds.

    P0.10 note: this test's own NAME still says "six" (left unchanged
    to preserve this file's own historical P0.8.9 framing/diff), but the
    real, live `config/automation_rules.json` now has EIGHT rules -
    P0.10 (Phase 5) additively shipped two new log-only diagnostic rules
    (`occupancy_test_log`, `occupancy_long_presence_test`) alongside the
    six that existed when this test was first written. The assertion
    below is intentionally updated to the current, correct set rather
    than left stale - see docs/change_impact/camera_automation_p0_10.md
    for the full P0.10 rationale."""
    rules = _load_real_rules()
    assert set(rules.keys()) == {
        "camera_human_detected_log",
        "camera_human_detected_test_action",
        "camera_multiple_people_log",
        "camera_test_automation_safety_action",
        "camera_test_automation_safety_action_off",
        "camera_wled_human_cleared_off",
        "occupancy_test_log",
        "occupancy_long_presence_test",
    }


# ============================================================================
# C. End-to-end engine/scheduler behavior - real bootstrap, real
#    runtime.scheduler, MOCK HA backend only.
# ============================================================================

def _build_stack(off_delay_s: float = _TEST_DELAY_S):
    """Real bootstrap (`register_all_modules`/`register_all_adapters`,
    all-mock backends), pointed at a TEMPORARY rules file containing an
    ON rule and a debounced OFF rule that both target `_TEST_ENTITY` -
    never the user's real `config/automation_rules.json` (Section B
    tests that file directly instead, read-only)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)

    rules = {
        "p0_8_9_wled_on_test_rule": {
            "name": "p0_8_9_wled_on_test_rule",
            "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_confirmed"}],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": _TEST_ENTITY}}],
            "cooldown_seconds": 0.0,
        },
        "p0_8_9_wled_off_test_rule": {
            "name": "p0_8_9_wled_off_test_rule",
            "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_cleared"}],
            "actions": [{"type": "home_assistant.turn_off", "parameters": {"target": _TEST_ENTITY, "delay_seconds": off_delay_s}}],
            "cooldown_seconds": 0.0,
        },
    }
    engine: AutomationEngine = modules["automation_engine"]
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_8_9_test_")
    os.close(fd)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    engine._rules_path = rules_path

    bridge: VisionCameraEventBridge = modules["vision_camera_event_bridge"]
    return runtime, modules, adapter_manager, cam_module, bridge, rules_path


def _teardown(runtime, adapter_manager, rules_path) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    try:
        os.remove(rules_path)
    except OSError:
        pass


def _ha_calls_for(tool_calls: List[Dict[str, Any]], action: str) -> List[Dict[str, Any]]:
    return [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _TEST_ENTITY and c.get("action") == action]


class _Harness:
    """Small shared setup for Section C/D - real bootstrap + tool_call/
    automation.completed observability taps, same idiom P0.8.8's own
    test_H already established."""

    def __init__(self, off_delay_s: float = _TEST_DELAY_S):
        self.runtime, self.modules, self.adapter_manager, self.cam_module, self.bridge, self.rules_path = _build_stack(off_delay_s)
        self.tool_calls: List[Dict[str, Any]] = []
        self.completed: List[Dict[str, Any]] = []
        self.skipped: List[Dict[str, Any]] = []

    def start(self) -> None:
        self.runtime.start()
        self.runtime.event_bus.subscribe("tool_requested", lambda e: self.tool_calls.append(e.data.get("tool_call", {})))
        self.runtime.event_bus.subscribe("automation.completed", lambda e: self.completed.append(e.data))
        self.runtime.event_bus.subscribe("automation.skipped", lambda e: self.skipped.append(e.data))
        handler = self.modules["tool_manager_module"].manager.registry.get("home_assistant")
        assert isinstance(handler, MockHomeAssistantHandler), "this suite must never exercise a real HA call"

    def teardown(self) -> None:
        _teardown(self.runtime, self.adapter_manager, self.rules_path)

    def on_calls(self) -> List[Dict[str, Any]]:
        return _ha_calls_for(self.tool_calls, "turn_on")

    def off_calls(self) -> List[Dict[str, Any]]:
        return _ha_calls_for(self.tool_calls, "turn_off")


def test_C1_human_confirmed_turns_wled_on():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        assert h.on_calls()[0]["action"] == "turn_on"
    finally:
        h.teardown()


def test_C2_human_cleared_does_not_immediately_turn_off():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        time.sleep(0.05)  # well before _TEST_DELAY_S (0.4s)
        assert len(h.off_calls()) == 0, "turn_off must NOT be dispatched immediately on human_cleared"
    finally:
        h.teardown()


def test_C3_wled_remains_on_through_most_of_the_delay_window():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        time.sleep(_TEST_DELAY_S * 0.5)
        assert len(h.off_calls()) == 0, "still within the debounce window - WLED must remain on"
    finally:
        h.teardown()


def test_C4_after_delay_elapses_wled_turns_off():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        assert _wait_until(lambda: len(h.off_calls()) >= 1, timeout_s=3.0), "turn_off must dispatch once the debounce delay elapses"
        assert _wait_until(lambda: any(d.get("rule_id") == "p0_8_9_wled_off_test_rule" for d in h.completed), timeout_s=2.0), (
            "automation.completed for the OFF rule must fire once the delayed action actually runs"
        )
    finally:
        h.teardown()


def test_C5_human_confirmed_during_pending_off_cancels_it():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        time.sleep(_TEST_DELAY_S * 0.3)  # well within the pending window
        # Person re-confirmed before the OFF delay elapses.
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        # Wait past when the (now-cancelled) OFF would otherwise have fired.
        time.sleep(_TEST_DELAY_S * 1.5)
        assert len(h.off_calls()) == 0, "the pending OFF must have been cancelled - WLED must never have turned off"
        assert len(h.on_calls()) >= 2, "the re-confirmation's own turn_on must still have dispatched"
        assert any(d.get("reason") == "action_superseded" for d in h.skipped), (
            "the superseded OFF execution must be honestly finalized as SKIPPED/action_superseded, not left silently pending"
        )
    finally:
        h.teardown()


def test_C6_after_genuine_clear_later_confirmed_turns_on_again():
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        assert _wait_until(lambda: len(h.off_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 2, timeout_s=3.0), "a fresh confirmation after a genuine OFF must turn WLED on again"
    finally:
        h.teardown()


def test_C7_repeated_human_cleared_does_not_duplicate_off_command():
    """No duplicate OFF command from repeated human_cleared events -
    a repeat that arrives before the first one's delay elapses simply
    resets the SAME pending job's debounce window. Only two repeats are
    used here (rather than many rapid-fire ones) so the test stays below
    this engine's own PRE-EXISTING, unrelated loop/cycle protection
    (`_MAX_FIRINGS_IN_WINDOW = 3` firings within `_CYCLE_WINDOW_S = 5.0s`
    - see `engine.py`'s own Phase 9 docstring) - that protection is a
    correct, deliberate safeguard this sprint must not defeat, not
    something this test is trying to probe."""
    h = _Harness()
    try:
        h.start()
        h.cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))
        time.sleep(_TEST_DELAY_S * 0.5)  # well before the first one would fire
        h.cam_module.ingest_external_camera_event(_mk_event("human_cleared"))  # resets the same pending job
        assert len(h.off_calls()) == 0, "still resetting - must not have fired yet"
        assert _wait_until(lambda: len(h.off_calls()) >= 1, timeout_s=3.0), "must eventually fire exactly once after the reset settles"
        time.sleep(_TEST_DELAY_S * 2)  # give any erroneous second firing a chance to appear
        assert len(h.off_calls()) == 1, f"expected exactly one turn_off dispatch, got {len(h.off_calls())}"
    finally:
        h.teardown()


def test_C8_different_target_entities_do_not_interfere():
    """A pending OFF for one entity must not be affected by an unrelated
    ON/OFF dispatch for a DIFFERENT entity - the cancellation key is the
    target entity id, not a global flag."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)
    rules = {
        "off_entity_a": {
            "name": "off_entity_a", "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_cleared"}],
            "actions": [{"type": "home_assistant.turn_off", "parameters": {"target": "light.entity_a", "delay_seconds": _TEST_DELAY_S}}],
            "cooldown_seconds": 0.0,
        },
        "on_entity_b": {
            "name": "on_entity_b", "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_confirmed"}],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.entity_b"}}],
            "cooldown_seconds": 0.0,
        },
    }
    engine: AutomationEngine = modules["automation_engine"]
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_8_9_entity_test_")
    os.close(fd)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    engine._rules_path = rules_path
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        cam_module.ingest_external_camera_event(_mk_event("human_cleared"))  # schedules pending OFF for entity_a
        time.sleep(_TEST_DELAY_S * 0.3)
        cam_module.ingest_external_camera_event(_mk_event("human_confirmed"))  # immediate ON for entity_b - unrelated target
        assert _wait_until(lambda: any(c.get("target") == "light.entity_b" for c in tool_calls), timeout_s=2.0)
        assert _wait_until(lambda: any(c.get("target") == "light.entity_a" and c.get("action") == "turn_off" for c in tool_calls), timeout_s=3.0), (
            "entity_a's pending OFF must still fire on schedule - an unrelated entity's dispatch must never cancel it"
        )
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# D. Real production call path - VisionCameraEventBridge, not a mocked
#    helper.
# ============================================================================

def test_D1_real_bridge_confirmed_then_cleared_then_delayed_off():
    """`bridge._on_person_confirmed()`/`bridge._on_person_left()` are
    EXACTLY what a real `HumanPresenceConfirmed`/`CameraPersonLeft` event
    from `VisionAdapter` would trigger via the Event Bus - the same
    "production call path, not a mocked helper" proof P0.8.8's own test_H
    established, extended here to cover the full ON -> pending OFF ->
    genuine OFF sequence."""
    h = _Harness()
    try:
        h.start()
        h.bridge._on_person_confirmed(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0), "stage C/D failed: real bridge -> engine -> turn_on never happened"

        h.bridge._on_person_left(Event(type=CameraPersonLeft.EVENT_TYPE))
        time.sleep(0.05)
        assert len(h.off_calls()) == 0, "turn_off must not be immediate even via the real bridge path"

        assert _wait_until(lambda: len(h.off_calls()) >= 1, timeout_s=3.0), "the real bridge path must still produce a delayed turn_off"
        off_call = h.off_calls()[0]
        assert off_call["target"] == _TEST_ENTITY
        assert off_call["action"] == "turn_off"
    finally:
        h.teardown()


def test_D2_real_bridge_reconfirmation_cancels_pending_off():
    h = _Harness()
    try:
        h.start()
        h.bridge._on_person_confirmed(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: len(h.on_calls()) >= 1, timeout_s=3.0)

        h.bridge._on_person_left(Event(type=CameraPersonLeft.EVENT_TYPE))
        time.sleep(_TEST_DELAY_S * 0.3)
        h.bridge._on_person_confirmed(Event(type=HumanPresenceConfirmed.EVENT_TYPE))  # person came back before the delay elapsed

        time.sleep(_TEST_DELAY_S * 1.5)
        assert len(h.off_calls()) == 0, "real bridge path: re-confirmation before the delay elapsed must cancel the pending OFF"
        assert len(h.on_calls()) >= 2
    finally:
        h.teardown()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
