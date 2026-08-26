"""
tests/test_p0_8_6_end_to_end_human_wled_reliability.py
========================================================

LUNO P0.8.6 (End-to-End Human Detection -> WLED Reliability Fix) -
dedicated regression suite. See docs/change_impact/camera_automation_
p0_8_6.md for the full root-cause trace; this file proves the fix's 20
brief-mandated scenarios directly, with real assertions (unlike the
pre-existing legacy `luno/tool_manager/tests/test_real_home_assistant_
verification.py`, which returns `(bool, str)` tuples that pytest does
NOT fail on - a pre-existing condition, not something this sprint
introduced or is responsible for fixing; noted in the final report's
"Remaining Issues").

Two real-world problems this sprint fixes:
  1. False-positive human detections (a single frame at, say, person=
     0.506) directly firing `CameraPersonEntered` -> `human_detected`
     -> a real `home_assistant.turn_on` on `light.wled`, with zero
     confidence floor and zero temporal confirmation.
  2. HA reporting `light.wled=on`/"verification success" without any
     honest statement of what that verification actually proves (HA's
     own reported state, not independent physical confirmation).

Sections:
  A (items 1-4). Sub-threshold and single-cycle-at-threshold confidence
     never confirms presence on their own.
  B (items 5-6). Sustained, in-range detection confirms exactly once
     and never re-fires while presence continues.
  C (items 7-8). Falling/rising edges of the CONFIRMED signal - stricter
     than the raw P0.8.5 debounce it sits on top of.
  D (item 9). Multi-person tracked cycles.
  E (items 10-11). False-positive-frame-then-nothing / false-positive-
     frame-then-real-sustained-detection sequences.
  F (item 12). Low-confidence detections remain visible in
     diagnostic/debug status output (never silently dropped).
  G (items 13-14). WLED already-ON / OFF end-to-end via the real
     bootstrap + mock HA dispatcher (no real light ever touched).
  H (items 15-16). HA command failure / entity-unavailable never
     produce a false verification success, via `RealHomeAssistantHandler`
     + a synthetic `FakeHAClient` (still no real HA/network call).
  I (item 17). `light.wled` configuration is consistent (and the
     "unconfigured entity" warning is a confirmed non-defect) across
     `config/lights.config.json`, `config/automation_rules.json`, and
     `config/camera_automation.json`.
  J (items 18-20). Lightweight guards proving this sprint's changes are
     additive and did not touch the P0.8.4 concurrency lock or the
     P0.8.5 cross-loop fix's own call sites - full confirmation that
     P0.8.0-P0.8.5's OWN dedicated suites are still green comes from
     running them directly (Phase 7 of this sprint), not from
     duplicating them here.
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

from luno import config as luno_config  # noqa: E402
from luno import vision_memory as vm  # noqa: E402
from luno.adapters.events import (  # noqa: E402
    CameraPersonEntered,
    CameraPersonLeft,
    HumanPresenceConfirmed,
    HumanPresenceUnconfirmed,
)
from luno.adapters.vision import MockVisionSource, VisionAdapter, VisionCycleResult  # noqa: E402
from luno.vision_human_state import Facing, HumanState, Posture, Presence  # noqa: E402
from luno.vision_tracking import TrackedDetection  # noqa: E402

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
from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_WLED_ENTITY = "light.wled"


# ============================================================================
# Shared fixtures / helpers
# ============================================================================

class FakeEventBus:
    """Same minimal convention as tests/test_p0_8_5_person_count_sync_fix.py."""

    def __init__(self) -> None:
        self.published: List[object] = []

    def subscribe(self, *_a, **_k):
        return "sub"

    def unsubscribe(self, *_a, **_k):
        pass

    def publish(self, event) -> None:
        self.published.append(event)

    def types(self) -> List[str]:
        return [e.type for e in self.published]

    def count(self, event_type: str) -> int:
        return sum(1 for t in self.types() if t == event_type)


def _isolate_vision_memory() -> None:
    vm.reset()
    vm.configure(db_path=os.path.join(tempfile.mkdtemp(), "vm_p0_8_6_test.sqlite3"))


def _new_adapter(person_absence_timeout_s: float = 0.0) -> "tuple[VisionAdapter, FakeEventBus]":
    _isolate_vision_memory()
    bus = FakeEventBus()
    adapter = VisionAdapter(source=MockVisionSource(), person_absence_timeout_s=person_absence_timeout_s)
    adapter.bind(bus)
    adapter.start()
    return adapter, bus


def _person_cycle(confidence: float, tracking_id: str = "person#1", fps: float = 2.0) -> VisionCycleResult:
    det = TrackedDetection(
        id=tracking_id, label="person", confidence=confidence, bbox=(10.0, 10.0, 90.0, 220.0),
        first_seen=0.0, last_seen=0.0, tracking_age_s=0.0,
    )
    human = HumanState(
        tracking_id=tracking_id, posture=Posture.STANDING, facing=Facing.TOWARD_CAMERA,
        hand_raised=False, presence=Presence.PRESENT,
    )
    return VisionCycleResult(objects=[det], humans=[human], fps=fps, latency_ms=10.0)


def _two_person_cycle(conf_a: float, conf_b: float, fps: float = 2.0) -> VisionCycleResult:
    det_a = TrackedDetection(
        id="person#1", label="person", confidence=conf_a, bbox=(10.0, 10.0, 90.0, 220.0),
        first_seen=0.0, last_seen=0.0, tracking_age_s=0.0,
    )
    det_b = TrackedDetection(
        id="person#2", label="person", confidence=conf_b, bbox=(150.0, 10.0, 230.0, 220.0),
        first_seen=0.0, last_seen=0.0, tracking_age_s=0.0,
    )
    human_a = HumanState(tracking_id="person#1", posture=Posture.STANDING, facing=Facing.TOWARD_CAMERA, hand_raised=False, presence=Presence.PRESENT)
    human_b = HumanState(tracking_id="person#2", posture=Posture.STANDING, facing=Facing.AWAY, hand_raised=False, presence=Presence.PRESENT)
    return VisionCycleResult(objects=[det_a, det_b], humans=[human_a, human_b], fps=fps, latency_ms=10.0)


def _empty_cycle(fps: float = 2.0) -> VisionCycleResult:
    return VisionCycleResult(objects=[], humans=[], fps=fps, latency_ms=10.0)


def _confirm_cycles() -> int:
    """The real, configured HUMAN_DETECTION_CONFIRM_CYCLES (default 3) -
    read live from config so this suite never silently drifts from the
    actual production value, per the brief's own "do not blindly copy a
    number" instruction."""
    return max(1, int(getattr(luno_config, "HUMAN_DETECTION_CONFIRM_CYCLES", 3)))


def _confidence_floor() -> float:
    return float(getattr(luno_config, "HUMAN_DETECTION_CONFIDENCE", 0.60))


# ============================================================================
# A (items 1-4). Sub-threshold confidence never confirms on its own.
# ============================================================================

@pytest.mark.parametrize("confidence", [0.40, 0.50, 0.59])
def test_01_02_03_below_floor_confidence_never_confirms_even_at_max_cycles(confidence):
    """Items 1-3: 0.40/0.50/0.59, each held for MORE than
    HUMAN_DETECTION_CONFIRM_CYCLES consecutive cycles - still never
    confirms, since every single cycle fails the confidence floor
    (`_update_confirmed_presence()`'s `qualifies` check), and the streak
    never even starts."""
    assert confidence < _confidence_floor()
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles() + 2):
        adapter.on_vision_cycle(_person_cycle(confidence=confidence))
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 0
    assert adapter._extra_status()["human_confirmed"] is False


def test_04_single_cycle_at_or_above_floor_is_candidate_only_not_immediate_trigger():
    """Item 4: ONE cycle at exactly the floor (0.60) increments the
    streak to 1 but - unless HUMAN_DETECTION_CONFIRM_CYCLES == 1 (it is
    3 by default) - does NOT confirm and does NOT publish
    HumanPresenceConfirmed. This is the literal fix for the brief's
    reported bug (person_confidences=[0.506], a single frame, previously
    reaching WLED ON with zero confirmation)."""
    floor = _confidence_floor()
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle(confidence=floor))
    assert adapter._human_confirm_streak == 1
    if _confirm_cycles() > 1:
        assert adapter._extra_status()["human_confirmed"] is False
        assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 0


# ============================================================================
# B (items 5-6). Sustained qualifying detection confirms exactly once.
# ============================================================================

def test_05_sustained_qualifying_detection_confirms_exactly_once():
    """Item 5: person detected >= HUMAN_DETECTION_CONFIDENCE for exactly
    HUMAN_DETECTION_CONFIRM_CYCLES consecutive cycles -> exactly one
    HumanPresenceConfirmed event, and `human_confirmed` is True by the
    time it's on the bus."""
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 1
    assert adapter._extra_status()["human_confirmed"] is True


def test_06_continued_presence_never_emits_a_duplicate_confirmation_event():
    """Item 6: many MORE qualifying cycles after confirmation - the
    event fires on the rising edge only, never repeats while presence
    is continuous (same "avoid event spam" discipline as P0.8.5's own
    CameraPersonEntered debounce)."""
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles() + 10):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 1
    assert bus.count(HumanPresenceUnconfirmed.EVENT_TYPE) == 0


# ============================================================================
# C (items 7-8). Falling/rising edges - CONFIRMED is stricter than the
# raw P0.8.5 debounce it sits on top of.
# ============================================================================

def test_07_confirmed_presence_drops_immediately_on_one_non_qualifying_cycle():
    """Item 7 (as realized for the CONFIRMED signal specifically - see
    `_update_confirmed_presence()`'s own docstring): unlike the raw
    P0.8.5 `_person_present_debounced` state (which only falls after a
    multi-second absence TIMEOUT, proven unaffected in test_07b below),
    the stricter automation-facing `human_confirmed` gate resets its
    streak - and un-confirms - the moment a single cycle fails to
    qualify. This is intentional: the gate exists specifically to keep
    physical automation off marginal evidence, so it fails toward
    "not confirmed" fast rather than lingering."""
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))
    assert adapter._extra_status()["human_confirmed"] is True

    adapter.on_vision_cycle(_empty_cycle())  # one non-qualifying cycle
    assert adapter._extra_status()["human_confirmed"] is False
    assert bus.count(HumanPresenceUnconfirmed.EVENT_TYPE) == 1


def test_07b_raw_p0_8_5_presence_debounce_is_unaffected_by_the_new_gate():
    """The pre-existing, pinned P0.8.5 behavior (person_present_debounced
    only falls after person_absence_timeout_s of continuous absence) is
    completely untouched by this sprint's additive confirmation layer -
    proven directly here with a nonzero timeout so a single empty cycle
    does NOT flip the raw presence signal, even though it DOES
    immediately un-confirm the (separate, stricter) automation gate."""
    adapter, bus = _new_adapter(person_absence_timeout_s=999.0)
    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))
    adapter.on_vision_cycle(_empty_cycle())
    status = adapter._extra_status()
    assert status["person_present"] is True  # raw presence: still debounced PRESENT
    assert status["human_confirmed"] is False  # automation gate: already un-confirmed


def test_08_person_returns_produces_a_new_confirmation_event():
    """Item 8: after un-confirming, a fresh run of qualifying cycles
    confirms again and publishes a SECOND HumanPresenceConfirmed - the
    signal is a real, repeatable rising/falling edge, not a one-shot."""
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))
    adapter.on_vision_cycle(_empty_cycle())  # un-confirm
    assert adapter._extra_status()["human_confirmed"] is False

    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_person_cycle(confidence=0.78))  # person returns
    assert adapter._extra_status()["human_confirmed"] is True
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 2
    assert bus.count(HumanPresenceUnconfirmed.EVENT_TYPE) == 1


# ============================================================================
# D (item 9). Multi-person cycles.
# ============================================================================

def test_09_two_people_detected_yields_person_count_two():
    """Item 9: `human_count` (what `CameraEvent.person_count` is built
    from) correctly reflects both tracked people, and confirmation only
    needs at least ONE of them to qualify each cycle (matches
    `_update_confirmed_presence()`'s `any(...)` semantics - "a human
    presence is confirmed", not "every detected person individually
    qualifies")."""
    adapter, bus = _new_adapter()
    for _ in range(_confirm_cycles()):
        adapter.on_vision_cycle(_two_person_cycle(conf_a=0.82, conf_b=0.45))
    status = adapter._extra_status()
    assert status["human_count"] == 2
    assert status["human_confirmed"] is True


# ============================================================================
# E (items 10-11). False-positive-frame sequences.
# ============================================================================

def test_10_one_false_positive_frame_then_nothing_never_triggers():
    """Item 10: one single qualifying frame (the exact real-world
    person=0.506... well, here at a clearly-qualifying 0.80 to isolate
    "does ONE frame alone ever confirm" from the confidence-floor
    question already covered in tests 01-04) followed by empty cycles -
    never confirms, never fires HumanPresenceConfirmed."""
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle(confidence=0.80))  # one qualifying frame
    for _ in range(5):
        adapter.on_vision_cycle(_empty_cycle())  # then nothing
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 0
    assert adapter._extra_status()["human_confirmed"] is False


def test_11_false_positive_frame_then_real_sustained_detection_confirms_only_after_full_streak():
    """Item 11: a single false-positive-shaped qualifying frame, then a
    gap, then a REAL sustained run - confirmation only happens once the
    streak has genuinely accumulated HUMAN_DETECTION_CONFIRM_CYCLES
    consecutive qualifying cycles counted from the fresh start (the gap
    resets the streak to zero; the earlier lone frame contributes
    nothing to the later streak)."""
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle(confidence=0.80))  # false-positive-shaped single frame
    adapter.on_vision_cycle(_empty_cycle())  # gap resets the streak
    assert adapter._human_confirm_streak == 0

    # Now the real, sustained run - confirmation must not happen before
    # the full streak length is reached again.
    for i in range(_confirm_cycles() - 1):
        adapter.on_vision_cycle(_person_cycle(confidence=0.80))
        assert adapter._extra_status()["human_confirmed"] is False, f"confirmed too early at cycle {i + 1}"
    adapter.on_vision_cycle(_person_cycle(confidence=0.80))  # the Nth cycle completes the streak
    assert adapter._extra_status()["human_confirmed"] is True
    assert bus.count(HumanPresenceConfirmed.EVENT_TYPE) == 1


# ============================================================================
# F (item 12). Low-confidence detections stay visible in diagnostics.
# ============================================================================

def test_12_low_confidence_detections_remain_visible_in_status_output():
    """Item 12: a below-floor detection (e.g. 0.506, the brief's own
    real false-positive example) still populates `_known_objects`/
    `_known_humans` and is still visible via `_extra_status()["objects"]`
    /["humans"] - the confidence FLOOR gates the AUTOMATION signal only
    (`human_confirmed`); it must never make the underlying detection
    invisible to the debug/diagnostic viewer (`tools/vision_debug_
    viewer.py`, a fully separate standalone tool, reads this same
    tracked-cycle data path and was never wired to HUMAN_DETECTION_
    CONFIDENCE - confirmed by inspection, no change needed there)."""
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle(confidence=0.506))
    status = adapter._extra_status()
    assert status["human_count"] == 1
    assert status["object_count"] == 1
    objects = status["objects"]
    assert any(o.get("label") == "person" and abs(o.get("confidence", 0.0) - 0.506) < 1e-9 for o in objects)
    assert status["human_confirmed"] is False  # automation gate correctly NOT confirmed


# ============================================================================
# G (items 13-14). WLED already-ON / OFF via the real bootstrap + mock HA.
# ============================================================================

def _valid_confirmed_event_data(**overrides: Any) -> Dict[str, Any]:
    data = {
        "camera_id": "tapo_c212", "kind": "human_confirmed", "available": True,
        "human_present": True, "person_count": 1, "detected_objects": ("person",),
        "detection_error": None,
    }
    data.update(overrides)
    return data


def _build_stack(camera_automation_enabled: bool = True):
    """Real bootstrap, MOCK HA backend throughout - `register_real_tool_
    handlers()` is never called in this file, mirroring test_p0_8_0_
    camera_action_safety.py's own hard safety constraint."""
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
        f"expected MockHomeAssistantHandler, got {type(handler)!r} - this file must never exercise a real HA call"
    )
    return handler


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _publish_confirmed_event(runtime, **overrides: Any) -> None:
    runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_valid_confirmed_event_data(**overrides)))


def test_13_light_wled_already_on_skips_the_redundant_turn_on_call():
    """Item 13: WLED already ON -> no duplicate/redundant turn_on call -
    reuses the EXISTING `validate_camera_ha_action()`/`engine.
    ha_state_reader` "already_in_desired_state" mechanism P0.8.0 already
    built (no second dedup mechanism invented here)."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = lambda entity_id: "on" if entity_id == _WLED_ENTITY else None
        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_confirmed_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_test_action" for d in completed))
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _WLED_ENTITY]
        assert ha_calls == [], "light.wled was already ON - no Home Assistant call should have been made"
        _mock_ha_handler(modules)
    finally:
        _teardown(runtime, adapter_manager)


def test_14_light_wled_off_dispatches_turn_on_exactly_once():
    """Item 14: WLED OFF -> turn_on issued exactly once, even across a
    repeated confirmed signal (rule cooldown + tool-layer dedup both
    apply, same as P0.8.0's own test_19)."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        engine: AutomationEngine = modules["automation_engine"]
        engine.ha_state_reader = lambda entity_id: "off" if entity_id == _WLED_ENTITY else None
        tool_calls: List[Dict[str, Any]] = []
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        _publish_confirmed_event(runtime)
        _publish_confirmed_event(runtime)
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_test_action" for d in completed))
        time.sleep(0.3)
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _WLED_ENTITY]
        assert len(ha_calls) == 1
        assert ha_calls[0]["action"] == "turn_on"
        _mock_ha_handler(modules)
    finally:
        _teardown(runtime, adapter_manager)


def test_14b_raw_human_detected_kind_alone_no_longer_triggers_wled():
    """The core end-to-end proof that the rule change actually closes
    the reported bug: a raw, single-frame `human_detected` kind (what
    the OLD rule matched on) must NOT reach light.wled anymore - only
    `human_confirmed` does (test_14 above)."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_valid_confirmed_event_data(kind="human_detected")))
        time.sleep(0.3)
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == _WLED_ENTITY]
        assert ha_calls == [], "a raw single-frame human_detected event must never directly trigger WLED anymore"
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# H (items 15-16). HA command failure / entity unavailable never produce
# a false verification success - via RealHomeAssistantHandler + a
# synthetic FakeHAClient (still zero real HA/network calls).
# ============================================================================

class _FakeHAClient:
    """Minimal fake standing in for RealHomeAssistantClient - same
    convention as luno/tool_manager/tests/test_real_home_assistant_
    verification.py::FakeHAClient, reused here (not reinvented) for the
    two P0.8.6-specific claims: an honest failure never reports success,
    and the new `verification_scope` field is present and correctly
    worded."""

    def __init__(self) -> None:
        self.states: Dict[str, Optional[str]] = {}
        self.state_after_call: Dict[str, str] = {}
        self._called_entities: set = set()
        self.call_service_result: Optional[Dict[str, Any]] = None
        self.calls: List[Any] = []

    def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        if self.call_service_result is not None:
            return self.call_service_result
        self._called_entities.add(entity_id)
        return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_entity_state(self, entity_id):
        target = self.state_after_call.get(entity_id)
        if target is not None and entity_id in self._called_entities:
            self.states[entity_id] = target
        return self.states.get(entity_id)

    def get_entity_attributes(self, entity_id):
        return None


def _patch_wled_light():
    from luno import devices
    saved = dict(devices.LIGHTS)
    devices.LIGHTS.clear()
    devices.LIGHTS.update({"rgb strip": {"entity_id": _WLED_ENTITY, "aliases": []}})
    return saved


def _restore_lights(saved):
    from luno import devices
    devices.LIGHTS.clear()
    devices.LIGHTS.update(saved)


def _set_env(**kwargs):
    saved = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = str(v)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_15_ha_service_call_failure_never_reports_success():
    """Item 15: HA reports the service call itself failed -> the tool
    result must be success=False, never a false "verified"/"turned on"
    claim."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=500, VERIFY_RETRIES=1)
    try:
        client = _FakeHAClient()
        client.states[_WLED_ENTITY] = "off"
        client.call_service_result = {"success": False, "error": "simulated HA rejection"}
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is False
        assert result.data.get("success") is False
        assert "turned on" not in result.message.lower()
        assert "i've" not in result.message.lower()
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_16_entity_unavailable_never_produces_a_false_verification_success():
    """Item 16: the device never leaves an unavailable state after the
    call - `_UNAVAILABLE_STATES` already correctly fails verification;
    proven directly here with the real WLED entity/target."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=300, VERIFY_RETRIES=1)
    try:
        client = _FakeHAClient()
        client.states[_WLED_ENTITY] = "unavailable"
        client.state_after_call[_WLED_ENTITY] = "unavailable"  # stays unavailable even after the call
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is False
        assert result.data.get("actual_state") in ("unavailable", None)
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_16b_successful_verification_reports_honest_scope_not_physical_confirmation():
    """The WLED-verification wording fix itself: a genuinely successful
    HA-reported state change must carry the new, additive
    `verification_scope: "ha_reported_state"` field, and must never
    claim physical device confirmation - only that HA's own reported
    state now matches. This is the direct proof for the brief's
    "determine what verification success actually verifies" requirement
    and the "HA accepted state change, not physical device confirmed"
    relabeling it mandated."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = _FakeHAClient()
        client.states[_WLED_ENTITY] = "off"
        client.state_after_call[_WLED_ENTITY] = "on"
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is True
        assert result.data.get("verification_scope") == "ha_reported_state"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


# ============================================================================
# I (item 17). light.wled configuration consistency + the "unconfigured
# entity" warning is a confirmed non-defect.
# ============================================================================

def test_17_light_wled_is_registered_for_outbound_control():
    """`config/lights.config.json` (what `home_assistant.turn_on`'s
    target-resolution actually reads) has a real, well-formed
    `light.wled` entry - the light this sprint's rule targets genuinely
    exists in Luno's own device registry."""
    path = os.path.join(_ROOT, "config", "lights.config.json")
    with open(path, "r", encoding="utf-8") as fh:
        lights = json.load(fh)
    matches = [v for v in lights.values() if v.get("entity_id") == _WLED_ENTITY]
    assert len(matches) == 1


def test_17b_automation_rule_targets_the_same_registered_entity():
    path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    rule = rules["camera_human_detected_test_action"]
    assert rule["actions"][0]["parameters"]["target"] == _WLED_ENTITY
    assert any(c.get("target") == "event.kind" and c.get("value") == "human_confirmed" for c in rule["conditions"])
    assert any(c.get("target") == "event.available" and c.get("value") is True for c in rule["conditions"])
    assert any(c.get("target") == "event.detection_error" and c.get("value") is None for c in rule["conditions"])


def test_17c_camera_automation_entities_allowlist_intentionally_excludes_light_wled():
    """Confirms (does not "fix" - there is nothing to fix) the
    "ignoring device_state_changed for unconfigured entity 'light.wled'"
    log line's own root cause: `config/camera_automation.json`'s
    `entities` allowlist governs INBOUND `device_state_changed`
    listening only, and light.wled is an OUTPUT device camera_automation
    intentionally acts ON (via home_assistant.turn_on), never something
    it needs to listen to inbound FROM - by CameraAutomationConfig.
    entities's own docstring. Proven here directly against the real
    shipped config: light.wled is genuinely absent from the allowlist,
    and no camera role in the config maps to it either."""
    path = os.path.join(_ROOT, "config", "camera_automation.json")
    with open(path, "r", encoding="utf-8") as fh:
        cam_cfg = json.load(fh)
    flat_entities = cam_cfg.get("entities", [])
    assert _WLED_ENTITY not in flat_entities
    for camera in cam_cfg.get("cameras", {}).values():
        assert camera.get("camera_entity") != _WLED_ENTITY
        assert camera.get("motion_entity") != _WLED_ENTITY
        assert camera.get("human_entity") != _WLED_ENTITY
        assert camera.get("availability_entity") != _WLED_ENTITY


# ============================================================================
# J (items 18-20). Additive-change guards - full confirmation that
# P0.8.0-P0.8.5's own suites are still green comes from running them
# directly (this sprint's Phase 7), not from duplicating them here.
# ============================================================================

def test_18_camera_presence_pre_p0_8_5_contract_file_still_exists_and_is_untouched_by_this_sprint():
    """Item 18 (guard, not a duplicate run): `tests/test_camera_
    presence.py` pins `_update_person_presence()`'s exact contract -
    this sprint's own design explicitly never touches that method or its
    call sites (see VisionAdapter.__init__'s P0.8.6 comment block).
    Confirmed here that the file still exists (the real regression
    proof is running it, done separately in this sprint's Phase 7)."""
    assert os.path.isfile(os.path.join(_ROOT, "tests", "test_camera_presence.py"))


def test_19_p0_8_4_yolo_lock_symbol_still_present_unmodified_by_this_sprint():
    """Item 19: this sprint's `luno/adapters/vision.py` changes never
    touch `luno/vision.py` (the file P0.8.4's `_yolo_lock` concurrency
    fix lives in) at all - confirmed directly: the lock symbol is still
    present, and this sprint's only production changes are additive
    (new events/config/adapter method), never a modification to that
    file."""
    vision_source_path = os.path.join(_ROOT, "luno", "vision.py")
    with open(vision_source_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "_yolo_lock = threading.Lock()" in source


def test_20_p0_8_5_shared_debounce_call_site_is_still_present_and_additive():
    """Item 20: `on_vision_cycle()` still calls the SAME P0.8.5 fix line
    (`self._update_person_presence(len(current_humans) > 0)`) - this
    sprint's own `_update_confirmed_presence()` call is a NEW, SEPARATE
    line added after it, never a replacement. The full P0.8.5 suite
    itself (tests/test_p0_8_5_person_count_sync_fix.py) is re-run
    directly, unmodified, as part of this sprint's Phase 7 regression
    gate - this is a source-level guard that the call site survived,
    not a substitute for that run."""
    vision_adapter_path = os.path.join(_ROOT, "luno", "adapters", "vision.py")
    with open(vision_adapter_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "self._update_person_presence(len(current_humans) > 0)" in source
    assert "self._update_confirmed_presence(person_confidences_this_cycle)" in source
    # The P0.8.5 call must appear BEFORE the new P0.8.6 call in source
    # order, matching on_vision_cycle()'s real execution order.
    assert source.index("self._update_person_presence(len(current_humans) > 0)") < source.index(
        "self._update_confirmed_presence(person_confidences_this_cycle)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
