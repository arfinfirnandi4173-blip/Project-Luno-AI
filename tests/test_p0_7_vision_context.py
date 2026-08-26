"""
tests/test_p0_7_vision_context.py
====================================

LUNO P0.7 (Vision Context -> Automation Context) - dedicated regression
suite.

Covers (brief Section 14/15/16):
  A. `VisionContext`/`build_vision_context()`/`normalize_object_label()`
     as pure units (no bridge, no runtime).
  B. State preservation across a detector failure (Section 5/12's core
     safety requirement: `detection_error` must never zero `human_present`/
     `person_count`).
  C. `VisionCameraEventBridge` wiring - `vision_status_reader`,
     `_on_system_error`, and the additive `CameraEvent` fields it now
     attaches to every camera_automation.camera_event.
  D. The new `greater_equal` condition operator (models.py/conditions.py).
  E. The new `camera_multiple_people_log` example rule, end to end
     through the real bootstrap stack (Section 15's "real bootstrap
     integration test").
  F. Regression - the three pre-existing rules/behaviors from P0.6/
     P0.6.2/P0.6.3 remain unaffected by this sprint's additive changes.
  G. Architecture guard - this sprint never introduced a second Vision/
     YOLO/RTSP pipeline and never persists Vision Context to disk.

Zero files under `luno/adapters/vision.py` (VisionAdapter itself) were
touched by this sprint - every test below exercises the NEW, additive
surface only (`vision_context.py`, the extended `CameraEvent` fields,
`vision_bridge.py`'s new reader/system_error wiring, `greater_equal`,
and the one new rule) - see docs/change_impact/vision_context_p0_7.md
for the full writeup.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import CameraDisconnected, CameraPersonEntered, CameraPersonLeft, CameraReconnected, HumanPresenceConfirmed  # noqa: E402
from luno.automation.conditions import CONDITION_INVALID, evaluate_condition  # noqa: E402
from luno.automation.models import AutomationCondition, CONDITION_TYPES  # noqa: E402
from luno.bootstrap.adapters import register_all_adapters, register_vision_context_reader  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.camera_automation.cameras import CameraEvent  # noqa: E402
from luno.camera_automation.vision_bridge import VisionCameraEventBridge  # noqa: E402
from luno.camera_automation.vision_context import VisionContext, build_vision_context, normalize_object_label  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_VISION_CONTEXT_PATH = os.path.join(_ROOT, "luno", "camera_automation", "vision_context.py")
_VISION_BRIDGE_PATH = os.path.join(_ROOT, "luno", "camera_automation", "vision_bridge.py")


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


def _build_stack(camera_automation_enabled: bool = True):
    """Same helper convention as test_p0_6_3_unified_vision_camera_
    automation.py's own `_build_stack()` - real Runtime, real modules,
    real adapters, wired the exact way `main.py` wires them (including
    the new P0.7 `register_vision_context_reader()` post-hoc call, so
    these tests exercise the SAME path production actually runs)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=camera_automation_enabled, cooldown_s=0.0)
    register_vision_context_reader(modules, adapters)
    return runtime, modules, adapter_manager


def _teardown(runtime, adapter_manager) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


# ============================================================================
# A. VisionContext / build_vision_context() / normalize_object_label() -
#    pure unit tests, no bridge, no runtime.
# ============================================================================

def test_01_normalize_object_label_case_and_whitespace_only():
    assert normalize_object_label("Person") == "person"
    assert normalize_object_label(" PERSON ") == "person"
    assert normalize_object_label("television") == "television"
    assert normalize_object_label("") == ""
    assert normalize_object_label(None) == ""  # type: ignore[arg-type]


def test_02_build_vision_context_none_status_degrades_safely():
    ctx = build_vision_context(camera_id="tapo_c212", status=None)
    assert ctx.camera_id == "tapo_c212"
    assert ctx.available is None
    assert ctx.human_present is False
    assert ctx.person_count == 0
    assert ctx.detected_objects == ()
    assert ctx.detection_error is None


def test_03_build_vision_context_empty_dict_status_degrades_safely():
    ctx = build_vision_context(camera_id="tapo_c212", status={})
    assert ctx.available is None
    assert ctx.human_present is False
    assert ctx.person_count == 0


def test_04_build_vision_context_normal_status():
    status = {
        "camera_connected": True,
        "human_count": 2,
        "objects": [
            {"label": "Person", "confidence": 0.9},
            {"label": " television ", "confidence": 0.8},
            {"label": "person", "confidence": 0.7},
        ],
    }
    ctx = build_vision_context(camera_id="tapo_c212", status=status)
    assert ctx.available is True
    assert ctx.human_present is True
    assert ctx.person_count == 2
    # normalized + deduplicated + sorted
    assert ctx.detected_objects == ("person", "television")


def test_05_build_vision_context_ignores_malformed_object_entries():
    status = {"camera_connected": True, "human_count": 0, "objects": ["not_a_dict", {"no_label": True}, {"label": 123}]}
    ctx = build_vision_context(camera_id="tapo_c212", status=status)
    assert ctx.detected_objects == ()


def test_06_build_vision_context_negative_or_bad_human_count_clamped():
    ctx = build_vision_context(camera_id="tapo_c212", status={"human_count": -5})
    assert ctx.person_count == 0
    assert ctx.human_present is False

    ctx2 = build_vision_context(camera_id="tapo_c212", status={"human_count": "not_a_number"})
    assert ctx2.person_count == 0


def test_07_vision_context_to_dict_shape():
    ctx = VisionContext(camera_id="tapo_c212", human_present=True, person_count=1, detected_objects=("person",))
    d = ctx.to_dict()
    assert d["camera_id"] == "tapo_c212"
    assert d["human_present"] is True
    assert d["person_count"] == 1
    assert d["detected_objects"] == ("person",)
    assert "detection_error" in d
    assert "timestamp" in d


def test_08_vision_context_is_frozen():
    ctx = VisionContext(camera_id="tapo_c212")
    with pytest.raises(Exception):
        ctx.camera_id = "other"  # type: ignore[misc]


# ============================================================================
# B. State preservation on detector failure (Section 5/12 - the core
#    safety requirement of this sprint).
# ============================================================================

def test_09_detection_error_does_not_zero_human_present_or_person_count():
    """The critical contract: a detector failure passed alongside an
    otherwise-normal status must NOT report human_present=False/
    person_count=0 - it must pass through whatever the (separately
    honest) status snapshot already says."""
    status = {"camera_connected": True, "human_count": 1, "objects": [{"label": "person"}]}
    ctx = build_vision_context(camera_id="tapo_c212", status=status, detection_error="boom: Conv.bn")
    assert ctx.human_present is True
    assert ctx.person_count == 1
    assert ctx.detected_objects == ("person",)
    assert ctx.detection_error == "boom: Conv.bn"


def test_10_detection_error_with_no_status_still_reports_error_honestly():
    ctx = build_vision_context(camera_id="tapo_c212", status=None, detection_error="boom")
    assert ctx.detection_error == "boom"
    assert ctx.human_present is False  # honestly "nothing known", not a false positive either


def test_11_no_detection_error_is_none_by_default():
    ctx = build_vision_context(camera_id="tapo_c212", status={"human_count": 0})
    assert ctx.detection_error is None


# ============================================================================
# C. VisionCameraEventBridge wiring - vision_status_reader, system_error
#    tracking, and the additive CameraEvent fields.
# ============================================================================

class _FakeEventBus:
    def __init__(self) -> None:
        self.published: List[Event] = []
        self.subs: Dict[str, List[Any]] = {}

    def subscribe(self, event_type, handler):
        self.subs.setdefault(event_type, []).append(handler)
        return f"sub-{event_type}-{len(self.subs[event_type])}"

    def unsubscribe(self, *_a, **_k):
        pass

    def publish(self, event: Event) -> None:
        self.published.append(event)

    def dispatch(self, event: Event) -> None:
        for handler in self.subs.get(event.type, []):
            handler(event)

    def types(self) -> List[str]:
        return [e.type for e in self.published]


def _new_bridge_with_fake_bus(camera_automation_enabled: bool = True):
    cam_module = CameraAutomationModule()
    cam_module._config = CameraAutomationConfig(enabled=camera_automation_enabled, cooldown_s=0.0)
    bus = _FakeEventBus()
    cam_module.bind_event_bus(bus)
    cam_module.start()
    bridge = VisionCameraEventBridge(camera_automation=cam_module, camera_id="tapo_c212")
    bridge.bind_event_bus(bus)
    bridge.start()
    return bridge, cam_module, bus


def test_12_vision_status_reader_defaults_to_none():
    bridge, _cam, _bus = _new_bridge_with_fake_bus()
    assert bridge.vision_status_reader is None


def test_13_ingest_without_reader_wired_produces_honest_defaults():
    """Section 12's own "no reader wired yet" degrade-gracefully path -
    the CameraEvent must still be produced (kind is unaffected), just
    with unavailable-context defaults."""
    bridge, _cam, bus = _new_bridge_with_fake_bus()
    bus.dispatch(Event(type=CameraPersonEntered.EVENT_TYPE))
    published = [e for e in bus.published if e.type == CAMERA_EVENT_TYPE]
    assert len(published) == 1
    data = published[0].data
    assert data["kind"] == "human_detected"
    assert data["human_present"] is False
    assert data["person_count"] == 0
    assert data["available"] is None


def test_14_ingest_with_reader_wired_attaches_vision_context_fields():
    bridge, _cam, bus = _new_bridge_with_fake_bus()
    bridge.vision_status_reader = lambda: {
        "camera_connected": True,
        "human_count": 3,
        "objects": [{"label": "person"}, {"label": "chair"}],
    }
    bus.dispatch(Event(type=CameraPersonEntered.EVENT_TYPE))
    published = [e for e in bus.published if e.type == CAMERA_EVENT_TYPE]
    assert len(published) == 1
    data = published[0].data
    assert data["human_present"] is True
    assert data["person_count"] == 3
    assert set(data["detected_objects"]) == {"person", "chair"}
    assert data["available"] is True
    assert data["detection_error"] is None


def test_15_system_error_tracked_then_threaded_into_next_ingest_then_cleared():
    bridge, _cam, bus = _new_bridge_with_fake_bus()
    bridge.vision_status_reader = lambda: {"camera_connected": True, "human_count": 1, "objects": [{"label": "person"}]}

    # Unrelated system_error (different adapter) - must NOT be tracked.
    bus.dispatch(Event(type="system_error", data={"adapter": "other", "error_type": "vision_detection_failed"}))
    bus.dispatch(Event(type=CameraPersonEntered.EVENT_TYPE))
    first = [e for e in bus.published if e.type == CAMERA_EVENT_TYPE][-1]
    assert first.data["detection_error"] is None

    # Genuine Vision detector failure - must be tracked and threaded
    # into the NEXT ingest, without zeroing human_present/person_count.
    bus.dispatch(Event(type="system_error", data={"adapter": "vision", "error_type": "vision_detection_failed", "error": "Conv.bn"}))
    bus.dispatch(Event(type=CameraPersonLeft.EVENT_TYPE))
    second = [e for e in bus.published if e.type == CAMERA_EVENT_TYPE][-1]
    assert second.data["detection_error"] == "Conv.bn"
    assert second.data["human_present"] is True  # last-known-good status, unaffected
    assert second.data["person_count"] == 1

    # Cleared after being read once - the NEXT ingest must not repeat it.
    # Uses a THIRD, distinct kind (camera_offline) rather than re-firing
    # human_detected - CameraAutomationModule's own shared dedupe/cooldown
    # is keyed on (camera_id, kind) (see module.py::_publish_if_not_
    # suppressed), so a repeat of the SAME kind with unchanged state is a
    # legitimate no-op re-fire, not a bridge/VisionContext bug - re-using
    # human_detected here would test the dedupe layer, not detection_error
    # clearing.
    bus.dispatch(Event(type=CameraDisconnected.EVENT_TYPE))
    third = [e for e in bus.published if e.type == CAMERA_EVENT_TYPE][-1]
    assert third.data["kind"] == "camera_offline"
    assert third.data["detection_error"] is None


def test_16_unrelated_system_error_type_never_tracked():
    bridge, _cam, bus = _new_bridge_with_fake_bus()
    bus.dispatch(Event(type="system_error", data={"adapter": "vision", "error_type": "something_else"}))
    assert bridge._last_detection_error is None


def test_17_camera_event_to_dict_includes_p0_7_fields():
    ev = CameraEvent(
        camera_id="tapo_c212", kind="human_detected", entity_id="vision:x",
        old_state=None, new_state=None, human_present=True, person_count=2,
        detected_objects=("person",), available=True, detection_error=None,
    )
    d = ev.to_dict()
    for key in ("human_present", "person_count", "detected_objects", "available", "detection_error"):
        assert key in d


def test_18_camera_event_p0_7_fields_default_to_none_or_empty_for_ha_sourced_path():
    """HA-sourced CameraEvent construction sites (pre-P0.7) never pass
    these new fields - confirms they're truly optional/additive and
    don't break that existing path."""
    ev = CameraEvent(camera_id="tapo_c212", kind="human_detected", entity_id="binary_sensor.x", old_state="off", new_state="on")
    assert ev.human_present is None
    assert ev.person_count is None
    assert ev.detected_objects == ()
    assert ev.available is None
    assert ev.detection_error is None


def test_19_vision_bridge_never_imports_vision_yolo_rtsp_code():
    """Re-confirms P0.6.3's own architecture invariant still holds after
    this sprint's additive changes to vision_bridge.py."""
    tree = ast.parse(_read(_VISION_BRIDGE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            module = getattr(node, "module", None) or ""
            assert "cv2" not in names
            assert "ultralytics" not in module
            assert module not in ("luno.vision", "..vision")
        if isinstance(node, ast.Call):
            func = node.func
            called_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            assert called_name not in ("YOLO", "RealVisionSource", "VideoCapture")


def test_20_vision_context_module_never_imports_vision_yolo_rtsp_code():
    tree = ast.parse(_read(_VISION_CONTEXT_PATH))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            module = getattr(node, "module", None) or ""
            assert "cv2" not in names
            assert "ultralytics" not in module
            assert module not in ("luno.vision", "..vision", ".vision")


def test_21_vision_context_module_has_no_event_bus_subscription():
    """Section 9's own "not a polling loop" requirement - vision_context.py
    itself must have no subscribe()/Event Bus concept at all, only the
    bridge (an existing, already-tested integration point) does."""
    source = _read(_VISION_CONTEXT_PATH)
    for forbidden in ("subscribe(", "EventBus", "event_bus"):
        assert forbidden not in source


# ============================================================================
# D. greater_equal condition operator
# ============================================================================

def test_22_greater_equal_in_condition_types():
    assert "greater_equal" in CONDITION_TYPES


def test_23_greater_equal_true_when_equal():
    cond = AutomationCondition(type="greater_equal", target="event.person_count", value=2)
    ok, reason = evaluate_condition(cond, {}, event_data={"person_count": 2})
    assert ok is True
    assert reason == ""


def test_24_greater_equal_true_when_greater():
    cond = AutomationCondition(type="greater_equal", target="event.person_count", value=2)
    ok, _ = evaluate_condition(cond, {}, event_data={"person_count": 5})
    assert ok is True


def test_25_greater_equal_false_when_less():
    cond = AutomationCondition(type="greater_equal", target="event.person_count", value=2)
    ok, _ = evaluate_condition(cond, {}, event_data={"person_count": 1})
    assert ok is False


def test_26_greater_equal_invalid_on_type_mismatch():
    cond = AutomationCondition(type="greater_equal", target="event.person_count", value=2)
    ok, reason = evaluate_condition(cond, {}, event_data={"person_count": "not_a_number"})
    assert ok is False
    assert reason == CONDITION_INVALID


def test_27_greater_equal_invalid_on_missing_event_field():
    cond = AutomationCondition(type="greater_equal", target="event.person_count", value=2)
    ok, reason = evaluate_condition(cond, {}, event_data={})
    assert ok is False
    assert reason == CONDITION_INVALID


# ============================================================================
# E. camera_multiple_people_log rule - real bootstrap stack, end to end
#    (Section 15's "real bootstrap integration test").
# ============================================================================

def test_28_multiple_people_rule_fires_when_person_count_ge_2():
    runtime, modules, adapter_manager = _build_stack()
    bridge: VisionCameraEventBridge = modules["vision_camera_event_bridge"]
    bridge.vision_status_reader = lambda: {"camera_connected": True, "human_count": 2, "objects": [{"label": "person"}]}
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_multiple_people_log" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


def test_29_multiple_people_rule_does_not_fire_when_person_count_below_2():
    runtime, modules, adapter_manager = _build_stack()
    bridge: VisionCameraEventBridge = modules["vision_camera_event_bridge"]
    bridge.vision_status_reader = lambda: {"camera_connected": True, "human_count": 1, "objects": [{"label": "person"}]}
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))
        time.sleep(0.2)
        rule_ids = {d.get("rule_id") for d in completed}
        assert "camera_multiple_people_log" not in rule_ids
    finally:
        _teardown(runtime, adapter_manager)


def test_30_multiple_people_rule_does_not_fire_on_human_cleared_even_if_stale_count_high():
    """The rule also requires kind == human_detected (AND semantics,
    Section 8's own worked example paired with the existing kind gate) -
    a human_cleared event must never fire it even if a stale
    person_count happened to be >= 2."""
    runtime, modules, adapter_manager = _build_stack()
    bridge: VisionCameraEventBridge = modules["vision_camera_event_bridge"]
    bridge.vision_status_reader = lambda: {"camera_connected": True, "human_count": 2, "objects": [{"label": "person"}]}
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=CameraPersonLeft.EVENT_TYPE))
        time.sleep(0.2)
        rule_ids = {d.get("rule_id") for d in completed}
        assert "camera_multiple_people_log" not in rule_ids
    finally:
        _teardown(runtime, adapter_manager)


def test_31_multiple_people_rule_is_log_only_no_device_action():
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    rule = rules["camera_multiple_people_log"]
    assert rule["enabled"] is True
    for action in rule["actions"]:
        assert action["type"] == "automation.log"


def test_32_register_vision_context_reader_noop_when_bridge_or_adapter_manager_missing():
    """Section 12's own explicit "no-op harmlessly, never crash startup"
    requirement, tested directly."""
    register_vision_context_reader({}, {})  # must not raise
    register_vision_context_reader({"vision_camera_event_bridge": object()}, {})  # must not raise


# ============================================================================
# F. Regression - existing P0.6/P0.6.2/P0.6.3 rules unaffected
# ============================================================================

def test_33_camera_human_detected_log_rule_still_matches_human_detected():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_log" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


def test_34_camera_human_detected_test_action_rule_still_matches_human_detected():
    """P0.8.6 UPDATE (documented, intentional behavior change - see
    docs/change_impact/camera_automation_p0_8_6.md Section 3):
    `camera_human_detected_test_action` now requires `event.kind ==
    "human_confirmed"` (P0.8.6's new, sustained-detection-only signal,
    not the raw single-frame `CameraPersonEntered`/`human_detected`) PLUS
    `event.available == true` - a single raw frame must never directly
    turn on a real physical light. `available` is legitimately made
    `true` here the SAME way production does: calling the real
    `VisionAdapter.on_camera_status({"connected": True})` (fetched via
    `adapter_manager.registry.get("vision")`, matching this file's own
    real-bootstrap-stack convention) before publishing the new
    confirmation event - not a synthetic/injected status reader."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        vision_adapter = adapter_manager.registry.get("vision")
        assert vision_adapter is not None
        vision_adapter.on_camera_status({"connected": True, "source": "test"})
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_test_action" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


@pytest.mark.parametrize("event_type", [CameraDisconnected.EVENT_TYPE, CameraReconnected.EVENT_TYPE])
def test_35_camera_online_offline_events_still_reach_camera_event(event_type):
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))
        runtime.event_bus.publish(Event(type=event_type))
        assert _wait_until(lambda: len(received) == 1)
    finally:
        _teardown(runtime, adapter_manager)


def test_36_automation_rules_file_now_has_exactly_three_rules():
    """"Three rules" was accurate as of P0.7 (this file's own sprint) -
    P0.8.0, a LATER sprint, intentionally added a fourth
    (`camera_test_automation_safety_action`, see tests/test_p0_8_0_
    camera_action_safety.py). Updated to assert P0.7's own three rules
    remain a subset, present and byte-for-byte unchanged, same
    "issubset" convention test_26 in test_p0_6_3_unified_vision_camera_
    automation.py already established for the identical situation."""
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    assert {
        "camera_human_detected_log", "camera_human_detected_test_action", "camera_multiple_people_log",
    }.issubset(rules.keys())
    # All three P0.7-era rules must be byte-for-byte unchanged in shape.
    assert rules["camera_human_detected_test_action"]["actions"][0]["parameters"]["target"] == "light.wled"
    assert rules["camera_human_detected_log"]["actions"][0]["type"] == "automation.log"


# ============================================================================
# G. Architecture guard - no persistence, no second pipeline
# ============================================================================

def test_37_vision_context_never_persists_to_disk():
    for forbidden in ("open(", "json.dump", "sqlite3", ".write(", "pickle"):
        assert forbidden not in _read(_VISION_CONTEXT_PATH)


def test_38_vision_context_has_no_credentials_or_frame_fields():
    source = _read(_VISION_CONTEXT_PATH)
    for forbidden in ("rtsp://", "password", "frame_bytes", "np.ndarray", "cv2"):
        assert forbidden not in source.lower() or forbidden == "cv2" and "cv2" not in source


def test_39_no_new_polling_loop_introduced_by_this_sprint():
    for path in (_VISION_CONTEXT_PATH, _VISION_BRIDGE_PATH):
        source = _read(path)
        for forbidden in ("while True", "threading.Thread", "schedule.every"):
            assert forbidden not in source
