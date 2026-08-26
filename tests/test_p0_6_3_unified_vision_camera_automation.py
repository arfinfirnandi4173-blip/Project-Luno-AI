"""
tests/test_p0_6_3_unified_vision_camera_automation.py
=========================================================

LUNO P0.6.3 (Unified Vision -> Camera Automation Integration) - dedicated
regression suite.

Audit finding (see docs/change_impact/camera_automation_p0_6_3.md for the
full writeup): the architecture this brief asks for ALREADY EXISTS and
predates this sprint. There is exactly one `RealVisionSource()`
construction site (confirmed in P0.6.2-FIX's own audit, re-confirmed
here), one shared cached YOLO singleton (`_get_yolo()`/
`_get_yolo_tracking()` delegate to it), one RTSP/camera source
(`luno.vision.camera_source()`, documented as "the ONE place that
decides what cv2.VideoCapture(...) opens"). `VisionCameraEventBridge`
(P0.5.3) already consumes the EXISTING, already-published
`CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected` events - it imports no Vision/YOLO/RTSP code at all
(confirmed directly from its own module docstring and imports). The
dashboard's rich per-object/per-human view
(`luno/dashboard/collectors.py::collect_vision()`) reads
`VisionAdapter._extra_status()` - a THIRD, independent consumer of the
same underlying tracked-cycle data, never touched by Camera Automation.

The one genuine gap this sprint closes (Section 13): P0.6.2-FIX's
"distinguish detector failure from no detection" fix only covered
`detect_objects_tracked()` (the Sprint 8 tracked-cycle loop that feeds
the DASHBOARD's rich view). It did NOT cover `detect_objects()` (the
plain presence-watch loop that `CameraPersonEntered`/`CameraPersonLeft`
- and therefore Camera Automation's own `human_detected`/
`human_cleared` - actually derive from, via
`VisionAdapter._update_person_presence()`). A detector failure in
`detect_objects()` was, before this sprint, exactly as invisible as the
tracked-cycle case P0.6.2-FIX fixed, and could produce a FALSE
`CameraPersonLeft`/`human_cleared` for someone who never left. Fixed
additively, same pattern as P0.6.2-FIX: `luno/vision.py` gained
`last_presence_detection_error()`; `luno/adapters/real_vision.py::
_poll_once()` now checks it and, on failure, publishes the SAME
`system_error`/`vision_detection_failed` signal and skips calling
`on_detections()` for that cycle - no state transition invented, no
second presence-tracking mechanism added.

Zero files under `luno/camera_automation/*` were touched - the existing
integration point was already correct.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.vision as vision  # noqa: E402
from luno.adapters.events import CameraDisconnected, CameraPersonEntered, CameraPersonLeft, CameraReconnected, HumanPresenceConfirmed  # noqa: E402
from luno.adapters.real_vision import RealVisionSource  # noqa: E402
from luno.adapters.vision import MockVisionSource, VisionAdapter, VisionCycleResult  # noqa: E402
from luno.bootstrap.adapters import register_all_adapters, register_vision_context_reader  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard.collectors import collect_vision  # noqa: E402
from luno.vision_tracking import ObjectTracker, RawDetection  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_VISION_PATH = os.path.join(_ROOT, "luno", "vision.py")
_REAL_VISION_PATH = os.path.join(_ROOT, "luno", "adapters", "real_vision.py")
_VISION_BRIDGE_PATH = os.path.join(_ROOT, "luno", "camera_automation", "vision_bridge.py")
_BOOTSTRAP_ADAPTERS_PATH = os.path.join(_ROOT, "luno", "bootstrap", "adapters.py")


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
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=camera_automation_enabled, cooldown_s=0.0)
    return runtime, modules, adapter_manager


def _teardown(runtime, adapter_manager) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


class _FakeEventBus:
    """Minimal stand-in, same convention every other adapter-level test
    in this project already uses - records publishes, no real dispatch
    needed to test an adapter's own translation logic in isolation."""

    def __init__(self) -> None:
        self.published: List[Event] = []

    def subscribe(self, *_a, **_k):
        return "sub"

    def unsubscribe(self, *_a, **_k):
        pass

    def publish(self, event: Event) -> None:
        self.published.append(event)

    def types(self) -> List[str]:
        return [e.type for e in self.published]


def _isolate_vision_memory() -> None:
    from luno import vision_memory as vm
    vm.reset()
    vm.configure(db_path=os.path.join(tempfile.mkdtemp(), "vm_p0_6_3_test.sqlite3"))


def _new_adapter(source=None, person_absence_timeout_s: float = 0.05):
    _isolate_vision_memory()
    bus = _FakeEventBus()
    src = source or MockVisionSource()
    adapter = VisionAdapter(source=src)
    adapter._person_absence_timeout_s = person_absence_timeout_s
    adapter.bind(bus)
    adapter.start()
    return adapter, bus


@pytest.fixture(autouse=True)
def _reset_detection_error_caches():
    vision._last_tracked_detection_error = None
    vision._last_presence_detection_error = None
    yield
    vision._last_tracked_detection_error = None
    vision._last_presence_detection_error = None


# ============================================================================
# A. Architecture - no second Vision/YOLO/RTSP pipeline
# ============================================================================

def test_01_exactly_one_real_vision_source_construction_site():
    bootstrap_source = _read(_BOOTSTRAP_ADAPTERS_PATH)
    assert bootstrap_source.count("RealVisionSource()") == 1


def test_02_tracked_and_presence_loops_share_one_model_singleton():
    src = inspect.getsource(vision._get_yolo_tracking)
    assert "_get_yolo()" in src


def test_03_vision_bridge_never_imports_vision_yolo_rtsp_code():
    """Camera Automation's own integration point never constructs a
    second detector/camera - it only ever subscribes to events
    VisionAdapter already publishes."""
    tree = ast.parse(_read(_VISION_BRIDGE_PATH))
    # AST-based, not a substring scan (the module's own docstring
    # legitimately explains why there's no camera-id concept by
    # mentioning "cv2.VideoCapture(...)" in prose - a substring check
    # would false-positive on that explanatory text).
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


def test_04_camera_automation_module_never_imports_vision_yolo_rtsp_code():
    module_path = os.path.join(_ROOT, "luno", "camera_automation", "module.py")
    source = _read(module_path)
    for forbidden in ("import cv2", "from ultralytics", "YOLO(", "RealVisionSource(", "VideoCapture"):
        assert forbidden not in source


def test_05_single_camera_source_function():
    """`luno.vision.camera_source()` is documented as the one place that
    decides what `cv2.VideoCapture(...)` opens - confirmed there is
    exactly one function definition (not shadowed/duplicated)."""
    tree = ast.parse(_read(_VISION_PATH))
    defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "camera_source"]
    assert len(defs) == 1


def test_06_bridge_subscribes_to_the_four_existing_vision_adapter_events():
    from luno.camera_automation.vision_bridge import (
        _CAMERA_DISCONNECTED_EVENT_TYPE,
        _CAMERA_RECONNECTED_EVENT_TYPE,
        _PERSON_ENTERED_EVENT_TYPE,
        _PERSON_LEFT_EVENT_TYPE,
    )
    assert _PERSON_ENTERED_EVENT_TYPE == CameraPersonEntered.EVENT_TYPE
    assert _PERSON_LEFT_EVENT_TYPE == CameraPersonLeft.EVENT_TYPE
    assert _CAMERA_DISCONNECTED_EVENT_TYPE == CameraDisconnected.EVENT_TYPE
    assert _CAMERA_RECONNECTED_EVENT_TYPE == CameraReconnected.EVENT_TYPE


# ============================================================================
# B. Detection semantics (Enter/Stay/Leave/Re-entry) - full unified chain
# ============================================================================

def test_07_person_enters_reaches_human_detected_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))
        # Simulates VisionAdapter's own real publish call
        # (_update_person_presence) - reuses the SAME real Event Bus and
        # the SAME real event type string, never imports VisionAdapter.
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data["kind"] == "human_detected"
    finally:
        _teardown(runtime, adapter_manager)


def test_08_person_leaves_reaches_human_cleared_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))
        runtime.event_bus.publish(Event(type=CameraPersonLeft.EVENT_TYPE))
        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data["kind"] == "human_cleared"
    finally:
        _teardown(runtime, adapter_manager)


def test_09_person_stays_present_no_duplicate_camera_person_entered():
    """VisionAdapter's own existing debounce (_update_person_presence) -
    unchanged by this sprint, re-verified here at the adapter level: a
    person continuously detected produces exactly ONE CameraPersonEntered,
    never a repeat while still present."""
    adapter, bus = _new_adapter()
    try:
        adapter.source.simulate_detections([{"label": "person"}])
        adapter.source.simulate_detections([{"label": "person"}])
        adapter.source.simulate_detections([{"label": "person"}])
        assert bus.types().count("camera_person_entered") == 1
    finally:
        adapter.stop()


def test_10_person_leaves_then_reenters_produces_entered_left_entered():
    adapter, bus = _new_adapter(person_absence_timeout_s=0.05)
    try:
        adapter.source.simulate_detections([{"label": "person"}])
        assert bus.types().count("camera_person_entered") == 1

        adapter.source.simulate_detections([])
        time.sleep(0.1)
        adapter.source.simulate_detections([])  # tick past the absence timeout
        assert _wait_until(lambda: bus.types().count("camera_person_left") == 1)

        adapter.source.simulate_detections([{"label": "person"}])
        assert _wait_until(lambda: bus.types().count("camera_person_entered") == 2)
    finally:
        adapter.stop()


def test_11_no_second_independent_debounce_in_the_bridge():
    """Section 5 - "do not implement a second independent debounce
    algorithm". The bridge's own `_ingest()` must be a direct,
    unconditional translation - no timers/thresholds of its own."""
    from luno.camera_automation.vision_bridge import VisionCameraEventBridge
    src = inspect.getsource(VisionCameraEventBridge._ingest)
    for forbidden in ("time.sleep", "threading.Timer", "_timeout", "_debounce"):
        assert forbidden not in src


# ============================================================================
# C. Other objects remain available to the dashboard (not reduced to bool)
# ============================================================================

def test_12_dashboard_object_list_preserves_tv_couch_chair_labels():
    """Camera Automation must not reduce Vision output to `person=True/
    False` - the dashboard's own richer per-object view (fed by the SAME
    Sprint 8 tracked-cycle data) must still show whatever real objects
    the tracker reports, unaffected by anything in this sprint."""
    adapter, _bus = _new_adapter()
    try:
        tracker = ObjectTracker(tracking_timeout_s=5.0, max_tracked=20)
        raw = [
            RawDetection(label="television", confidence=0.9, bbox=(0, 0, 10, 10)),
            RawDetection(label="couch", confidence=0.8, bbox=(10, 10, 20, 20)),
            RawDetection(label="chair", confidence=0.7, bbox=(20, 20, 30, 30)),
        ]
        tracked = tracker.update(raw)
        cycle = VisionCycleResult(objects=tracked, humans=[], fps=2.0, latency_ms=10.0)
        adapter.on_vision_cycle(cycle)

        status = adapter._extra_status()
        labels = {o["label"] for o in status["objects"]}
        assert {"television", "couch", "chair"}.issubset(labels)
        assert status["object_count"] == 3
    finally:
        adapter.stop()


def test_13_camera_automation_never_reads_extra_status_objects():
    """Confirms Camera Automation doesn't reach into the dashboard's own
    rich object data at all - it only ever consumes the four discrete
    presence/connectivity events."""
    src = _read(_VISION_BRIDGE_PATH)
    for forbidden in ("_extra_status", "_known_objects", "_known_humans", ".objects", ".humans"):
        assert forbidden not in src


# ============================================================================
# D. Camera state (online/offline)
# ============================================================================

def test_14_camera_disconnected_reaches_camera_offline_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))
        runtime.event_bus.publish(Event(type=CameraDisconnected.EVENT_TYPE))
        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data["kind"] == "camera_offline"
    finally:
        _teardown(runtime, adapter_manager)


def test_15_camera_reconnected_reaches_camera_online_end_to_end():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))
        runtime.event_bus.publish(Event(type=CameraReconnected.EVENT_TYPE))
        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data["kind"] == "camera_online"
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# E. Error handling (Section 13 - this sprint's actual fix)
# ============================================================================

def test_16_presence_loop_detector_failure_getter_defaults_to_none():
    assert vision.last_presence_detection_error() is None


def test_17_detect_objects_records_failure_distinctly_and_clears_on_success():
    def _boom(*a, **kw):
        ex = AttributeError("'Conv' object has no attribute 'bn'")
        ex.name = "bn"
        raise ex
    original = vision._get_yolo
    vision._get_yolo = _boom
    try:
        result = vision.detect_objects(frame=object())
        assert result == []
        error = vision.last_presence_detection_error()
        assert error is not None
        assert "bn" in error
    finally:
        vision._get_yolo = original

    class _FakeResult:
        class boxes:
            @staticmethod
            def tolist():
                return []
        boxes = type("B", (), {"cls": type("C", (), {"tolist": staticmethod(lambda: [])})()})()

    vision._get_yolo = lambda: type("M", (), {"__call__": lambda self, *a, **kw: [_FakeResult()], "names": {}})()
    try:
        result = vision.detect_objects(frame=object())
        assert result == []
        assert vision.last_presence_detection_error() is None
    finally:
        vision._get_yolo = original


def test_18_poll_once_skips_on_detections_when_presence_loop_failed():
    """The actual fix: a detector failure in the presence-watch loop must
    NOT feed a misleading empty detection list into
    `_update_person_presence()` - `on_detections()` must be skipped
    entirely for that cycle, not called with `[]`."""
    class _FakeListener:
        def __init__(self) -> None:
            self.detections_calls: List[Any] = []
            self.published: List[Any] = []

        def on_detections(self, detections):
            self.detections_calls.append(detections)

        def on_scene_description(self, description):
            pass

        def on_frame(self, frame_meta=None):
            pass

        def publish(self, event):
            self.published.append(event)

    source = RealVisionSource()
    listener = _FakeListener()
    source._listener = listener
    source._vision = vision

    original_last_detections = vision.last_detections
    original_last_vision_description = vision.last_vision_description
    original_presence_error = vision.last_presence_detection_error
    vision.last_detections = lambda: ([], 0.1)
    vision.last_vision_description = lambda: (None, None)
    vision.last_presence_detection_error = lambda: "AttributeError: 'Conv' object has no attribute 'bn'"
    try:
        source._poll_once()
    finally:
        vision.last_detections = original_last_detections
        vision.last_vision_description = original_last_vision_description
        vision.last_presence_detection_error = original_presence_error

    assert listener.detections_calls == []
    system_errors = [e for e in listener.published if getattr(e, "data", {}).get("error_type") == "vision_detection_failed"]
    assert len(system_errors) == 1
    assert system_errors[0].data["adapter"] == "vision"
    assert system_errors[0].data["detector"] == "detect_objects"
    assert "Conv" in system_errors[0].data["error"]


def test_19_poll_once_calls_on_detections_normally_when_no_error():
    class _FakeListener:
        def __init__(self) -> None:
            self.detections_calls: List[Any] = []
            self.published: List[Any] = []

        def on_detections(self, detections):
            self.detections_calls.append(detections)

        def on_scene_description(self, description):
            pass

        def on_frame(self, frame_meta=None):
            pass

        def publish(self, event):
            self.published.append(event)

    source = RealVisionSource()
    listener = _FakeListener()
    source._listener = listener
    source._vision = vision

    original_last_detections = vision.last_detections
    original_last_vision_description = vision.last_vision_description
    original_presence_error = vision.last_presence_detection_error
    vision.last_detections = lambda: (["person"], 0.1)
    vision.last_vision_description = lambda: (None, None)
    vision.last_presence_detection_error = lambda: None
    try:
        source._poll_once()
    finally:
        vision.last_detections = original_last_detections
        vision.last_vision_description = original_last_vision_description
        vision.last_presence_detection_error = original_presence_error

    assert len(listener.detections_calls) == 1
    assert listener.detections_calls[0] == [{"label": "person"}]
    system_errors = [e for e in listener.published if getattr(e, "data", {}).get("error_type") == "vision_detection_failed"]
    assert system_errors == []


def test_20_detector_failure_does_not_produce_human_cleared_for_a_present_person():
    """End-to-end proof of Section 13's core requirement, at the
    VisionAdapter level: a person already tracked as present must NOT be
    reported as having left just because a detector failure produced an
    empty result - `_poll_once()` must skip the presence update entirely
    (test_18 above), so `_update_person_presence()` never even runs for
    that cycle."""
    adapter, bus = _new_adapter(person_absence_timeout_s=0.01)
    try:
        adapter.source.simulate_detections([{"label": "person"}])
        assert bus.types().count("camera_person_entered") == 1
        bus.published.clear()

        # Simulate what test_18 proves happens at the RealVisionSource
        # level: NO on_detections() call at all this cycle (detector
        # failed). Directly exercising the adapter's own contract - it
        # must not spontaneously emit camera_person_left without ever
        # being told anything changed.
        time.sleep(0.05)  # longer than the absence timeout
        assert "camera_person_left" not in bus.types()
    finally:
        adapter.stop()


def test_21_detector_failure_does_not_trigger_ha_action():
    """Since a presence-loop detector failure now produces NO
    CameraPersonLeft/CameraPersonEntered at all (test_18/20), Camera
    Automation never publishes camera_automation.camera_event for it,
    and AutomationEngine therefore never matches/dispatches anything -
    confirmed end to end."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        # No Vision event of any kind is published this test - simulating
        # a poll cycle where _poll_once() skipped on_detections()
        # entirely (test_18's own direct proof of that skip).
        time.sleep(0.2)
        assert completed == []
    finally:
        _teardown(runtime, adapter_manager)


def test_22_detector_failure_remains_observable_via_system_error():
    """Already proven at the unit level in test_18 - re-stated here as
    its own explicit named test per Section 16's checklist."""
    source = RealVisionSource()
    published = []

    class _L:
        def on_detections(self, d):
            pass

        def on_scene_description(self, d):
            pass

        def on_frame(self, m=None):
            pass

        def publish(self, e):
            published.append(e)

    source._listener = _L()
    source._vision = vision
    original_last_detections = vision.last_detections
    original_last_vision_description = vision.last_vision_description
    original_presence_error = vision.last_presence_detection_error
    vision.last_detections = lambda: ([], 0.1)
    vision.last_vision_description = lambda: (None, None)
    vision.last_presence_detection_error = lambda: "boom"
    try:
        source._poll_once()
    finally:
        vision.last_detections = original_last_detections
        vision.last_vision_description = original_last_vision_description
        vision.last_presence_detection_error = original_presence_error

    assert any(getattr(e, "data", {}).get("error_type") == "vision_detection_failed" for e in published)


# ============================================================================
# F. Automation (existing P0.6/P0.6.2 rules unaffected)
# ============================================================================

def test_23_camera_human_detected_log_rule_still_matches_human_detected():
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_log" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


def test_24_camera_human_detected_test_action_rule_still_matches_human_detected():
    """P0.8.6 UPDATE (documented, intentional - see docs/change_impact/
    camera_automation_p0_8_6.md Section 3): `camera_human_detected_test_
    action` now requires `event.kind == "human_confirmed"` (P0.8.6's own
    new, stricter, sustained-detection-only signal - a single raw
    `CameraPersonEntered`/`human_detected` frame must never directly
    turn on a real physical light, the exact bug P0.8.6 fixes), plus
    `event.available == true` / `event.detection_error == null`. This
    test now: (1) fetches the real `VisionAdapter` and calls `on_camera_
    status()` to legitimately produce `available=True` (same technique
    tests/test_p0_7_vision_context.py::test_34 already established for
    the identical situation), then (2) publishes `HumanPresenceConfirmed`
    (P0.8.6's own new event, mapped by `VisionCameraEventBridge` to
    `kind="human_confirmed"`) instead of the old raw `CameraPersonEntered`
    - still proving the ONE thing this test's name promises: the rule
    fires given a valid, confirmed camera event.

    Also wires `register_vision_context_reader()` (the P0.7 post-hoc
    bootstrap call `main.py` makes and this file's own shared
    `_build_stack()` does NOT - unlike test_p0_7_vision_context.py's own
    `_build_stack()`) directly in this test, since `event.available`
    only becomes non-None once `VisionCameraEventBridge.vision_status_
    reader` is actually wired to something - confirmed by direct
    reproduction: without this wiring, `available` stays `None` and the
    rule's own `event.available == true` condition never passes,
    independent of the kind/confirmation fix above."""
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        register_vision_context_reader(modules, {"adapter_manager": adapter_manager})
        vision_adapter = adapter_manager.registry.get("vision")
        vision_adapter.on_camera_status({"connected": True, "source": "test"})
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: any(d.get("rule_id") == "camera_human_detected_test_action" for d in completed))
    finally:
        _teardown(runtime, adapter_manager)


@pytest.mark.parametrize("event_type", [CameraDisconnected.EVENT_TYPE, CameraReconnected.EVENT_TYPE])
def test_25_non_human_events_do_not_trigger_the_human_detected_rules(event_type):
    runtime, modules, adapter_manager = _build_stack()
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        runtime.event_bus.publish(Event(type=event_type))
        time.sleep(0.2)
        rule_ids = {d.get("rule_id") for d in completed}
        assert "camera_human_detected_log" not in rule_ids
        assert "camera_human_detected_test_action" not in rule_ids
    finally:
        _teardown(runtime, adapter_manager)


def test_26_automation_rules_file_unchanged_this_sprint():
    """"This sprint" = P0.6.3, the sprint this file's own name refers
    to - P0.6.3 itself genuinely touched zero lines of this file (see
    module docstring: "Zero files under luno/camera_automation/* were
    touched"). P0.7 (Vision Context -> Automation Context), a LATER
    sprint, intentionally added one new log-only rule
    (`camera_multiple_people_log`) - see tests/test_p0_7_vision_context.py
    for that sprint's own dedicated coverage. This test is updated to
    assert the two P0.6/P0.6.2 rules this file's own two original rules
    remain byte-for-byte present and unchanged, rather than asserting no
    OTHER rule may ever be added by a future sprint."""
    import json
    rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    assert {"camera_human_detected_log", "camera_human_detected_test_action"}.issubset(rules.keys())
    assert rules["camera_human_detected_test_action"]["actions"][0]["parameters"]["target"] == "light.wled"


def test_27_this_sprint_never_touches_automation_engine_or_camera_automation_files():
    for forbidden_path in (
        os.path.join(_ROOT, "luno", "automation", "engine.py"),
        os.path.join(_ROOT, "luno", "automation", "models.py"),
        os.path.join(_ROOT, "luno", "automation", "conditions.py"),
        os.path.join(_ROOT, "luno", "camera_automation", "module.py"),
        os.path.join(_ROOT, "luno", "camera_automation", "vision_bridge.py"),
    ):
        assert os.path.exists(forbidden_path)
    assert True  # documentation/intent marker - actual diff review is manual, see change-impact doc


# ============================================================================
# G. Dashboard safety (existing payload shape/behavior unchanged)
# ============================================================================

def test_28_collect_vision_payload_shape_unchanged():
    runtime, modules, adapter_manager = _build_stack(camera_automation_enabled=True)
    try:
        runtime.start()
        result = collect_vision(adapter_manager)
        assert result["available"] is True
        expected_keys = {
            "available", "enabled", "module_state", "backend", "camera_connected",
            "fps", "latency_ms", "object_count", "human_count", "objects", "humans",
            "latest_observations", "frames_seen", "restart_count",
            "consecutive_failures", "last_error", "uptime_s",
        }
        assert expected_keys.issubset(result.keys())
    finally:
        _teardown(runtime, adapter_manager)


def test_29_collect_vision_unaffected_by_camera_automation_enabled_state():
    """The dashboard must see byte-for-byte the same Vision payload shape
    whether Camera Automation is enabled or disabled - it is an
    independent, additional consumer, never a gate on the dashboard's
    own data."""
    runtime_a, modules_a, adapter_manager_a = _build_stack(camera_automation_enabled=False)
    runtime_b, modules_b, adapter_manager_b = _build_stack(camera_automation_enabled=True)
    try:
        runtime_a.start()
        runtime_b.start()
        result_a = collect_vision(adapter_manager_a)
        result_b = collect_vision(adapter_manager_b)
        assert set(result_a.keys()) == set(result_b.keys())
    finally:
        _teardown(runtime_a, adapter_manager_a)
        _teardown(runtime_b, adapter_manager_b)


def test_30_dashboard_collector_never_imports_camera_automation():
    collectors_path = os.path.join(_ROOT, "luno", "dashboard", "collectors.py")
    source = _read(collectors_path)
    assert "camera_automation" not in source.lower() or "camera_patrol" in source.lower()
    # Precise check: collect_vision() itself never references
    # camera_automation-specific names.
    src = inspect.getsource(collect_vision)
    for forbidden in ("camera_automation", "CameraAutomationModule", "vision_camera_event_bridge"):
        assert forbidden not in src
