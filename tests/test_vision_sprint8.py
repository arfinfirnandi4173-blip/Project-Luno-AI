"""
test_vision_sprint8.py
========================

Sprint 8 - Real Vision Integration regression suite.

Covers the spec's own required test list: USB camera, mock backend, real
backend, reconnect, frame skipping, object tracking, human tracking, lost
objects, duplicate suppression, memory integration, dashboard updates,
stress test, long-running stability, thread leak.

Every test here runs with ZERO real camera/GPU/ultralytics install
required (this sandbox has neither) - `luno.vision`'s camera/model
functions are monkeypatched with plain fakes that mimic their real
return shapes (see each test's own setup), exactly the same technique
`tests/test_real_adapters.py`/`tests/test_production_launcher.py` already
use for their own hardware-free adapters. `luno.vision_tracking`/
`luno.vision_human_state`'s own pure logic needs no faking at all - it
has zero I/O dependency by design.

Run:
    python3 tests/test_vision_sprint8.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.config as legacy_config  # noqa: E402
import luno.vision as vision_module  # noqa: E402
from luno import vision_memory as vm  # noqa: E402
from luno.adapters.real_vision import MAX_CONSECUTIVE_CYCLE_FAILURES, RealVisionSource  # noqa: E402
from luno.adapters.vision import (  # noqa: E402
    MockVisionSource,
    VisionAdapter,
    VisionCycleResult,
)
from luno.vision_human_state import Facing, HumanState, HumanStateEstimator, Posture, Presence  # noqa: E402
from luno.vision_tracking import ObjectTracker, RawDetection, TrackedDetection  # noqa: E402

# Saved BEFORE any scenario below gets a chance to monkeypatch
# `vision_module.detect_objects_tracked`/`attach_pose_keypoints` (several
# do, via `_install_fake_real_vision()`, to exercise `RealVisionSource`'s
# orchestration without a real camera) - tests that need the REAL
# implementation (test_21, test_25) call these saved references directly
# rather than `vision_module.detect_objects_tracked` itself, so they stay
# correct no matter what order scenarios run in.
_REAL_DETECT_OBJECTS_TRACKED = vision_module.detect_objects_tracked
_REAL_ATTACH_POSE_KEYPOINTS = vision_module.attach_pose_keypoints

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


class FakeEventBus:
    """Minimal stand-in - just records every published Event's `.type`,
    exactly like the fakes already used throughout this project's other
    adapter-level tests (no real Event Bus/Dispatcher machinery needed to
    test an adapter's own translation logic in isolation)."""

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


def _isolate_vision_memory() -> None:
    """Points the module-level Vision Memory singleton at a throwaway
    SQLite file for this process, so these tests never touch the real
    project's `config/vision_memory.sqlite3` (same isolation technique
    used earlier in this project's own memory/session-summary tests)."""
    vm.reset()
    vm.configure(db_path=os.path.join(tempfile.mkdtemp(), "vm_sprint8_test.sqlite3"))


def _new_adapter(source=None) -> Tuple[VisionAdapter, FakeEventBus]:
    _isolate_vision_memory()
    bus = FakeEventBus()
    adapter = VisionAdapter(source=source or MockVisionSource())
    adapter.bind(bus)
    adapter.start()
    return adapter, bus


# ============================================================================
# 1) USB camera capture + reconnect (luno.vision._capture_frame/camera_status)
# ============================================================================

class _FakeCamera:
    def __init__(self, ok_sequence: List[bool]) -> None:
        self._ok_sequence = list(ok_sequence)
        self._opened = True

    def isOpened(self) -> bool:
        return self._opened

    def grab(self) -> None:
        pass

    def read(self):
        ok = self._ok_sequence.pop(0) if self._ok_sequence else False
        return ok, ("FRAME" if ok else None)

    def release(self) -> None:
        self._opened = False


@scenario
def test_01_usb_camera_capture_success_and_status():
    import cv2

    vision_module.release_camera()
    original = cv2.VideoCapture
    # Sprint 69: local-camera opens now pass an explicit backend candidate
    # (e.g. `cv2.CAP_V4L2` on Linux) as a second positional arg - accept
    # (and ignore) it here, same as this project's other camera fakes
    # updated for the same reason (see `tests/test_camera_health_check_
    # timeout.py`'s own module docstring for why this signature matters).
    cv2.VideoCapture = lambda src, backend=None: _FakeCamera([True, True, True])
    try:
        frame = vision_module.capture_frame()
        assert frame == "FRAME"
        status = vision_module.camera_status()
        assert status["connected"] is True
        assert status["error"] is None
    finally:
        cv2.VideoCapture = original
        vision_module.release_camera()


@scenario
def test_02_camera_disconnect_then_automatic_reconnect(monkeypatch=None):
    import cv2

    # `SCENARIOS` below also runs this function with zero args (see
    # `__main__`) - `monkeypatch` isn't available there, so this test
    # manages its own fake-time revert in the `finally` block instead of
    # relying on the pytest fixture's auto-revert when run that way.
    import luno.vision as _vmod

    vision_module.release_camera()
    original_cv2 = cv2.VideoCapture
    original_time = _vmod.time.time
    # connected, connected, DROPS (failed read), reconnects - Sprint 69
    # added a reopen cooldown (`config.CAMERA_REOPEN_COOLDOWN_S`) so a
    # failed camera is no longer retried on the very next call (that
    # immediate-retry behavior is exactly the "hammer a known-broken
    # camera" bug Sprint 69 fixed) - reconnect now only happens once the
    # cooldown has actually elapsed, proven here with a controllable fake
    # clock rather than a real `time.sleep()`.
    cv2.VideoCapture = lambda src, backend=None: _FakeCamera([True, True, False, True])
    clock = {"now": 1_000_000.0}
    _vmod.time.time = lambda: clock["now"]
    try:
        r1 = vision_module.capture_frame()
        r2 = vision_module.capture_frame()
        r3 = vision_module.capture_frame()  # read fails -> BUSY, cooldown starts
        assert [r1, r2, r3] == ["FRAME", "FRAME", None]

        # retrying immediately, still within the cooldown window, must NOT
        # reopen the camera at all
        r4 = vision_module.capture_frame()
        assert r4 is None

        # advance the fake clock past the cooldown - NOW a retry succeeds
        clock["now"] += legacy_config.CAMERA_REOPEN_COOLDOWN_S + 1.0
        r5 = vision_module.capture_frame()
        assert r5 == "FRAME"
        assert vision_module.camera_status()["connected"] is True
    finally:
        cv2.VideoCapture = original_cv2
        _vmod.time.time = original_time
        vision_module.release_camera()


@scenario
def test_03_ip_camera_url_takes_priority_over_index():
    original_url, original_index = legacy_config.CAMERA_URL, legacy_config.CAMERA_INDEX
    try:
        legacy_config.CAMERA_URL = ""
        legacy_config.CAMERA_INDEX = 3
        assert vision_module.camera_source() == 3
        legacy_config.CAMERA_URL = "rtsp://192.168.1.50:554/stream"
        assert vision_module.camera_source() == "rtsp://192.168.1.50:554/stream"
    finally:
        legacy_config.CAMERA_URL = original_url
        legacy_config.CAMERA_INDEX = original_index


# ============================================================================
# 2) Mock backend - existing (pre-Sprint-8) behavior stays unchanged
# ============================================================================

@scenario
def test_04_mock_backend_scene_description_and_detections_unchanged():
    adapter, bus = _new_adapter()
    try:
        adapter.source.simulate_scene("A person is sitting at the desk.")
        assert "vision_updated" in bus.types()

        adapter.source.simulate_detections([{"label": "person"}, {"label": "cup"}])
        assert "person_appeared" in bus.types()
        assert "object_appeared" in bus.types()

        bus.published.clear()
        adapter.source.simulate_detections([{"label": "cup"}])
        assert "person_disappeared" in bus.types()
    finally:
        adapter.stop()


# ============================================================================
# 3) Real backend - tracked-object + human-pose cycle, end to end
# ============================================================================

def _install_fake_real_vision(detections_by_call, camera_ok: bool = True):
    """Monkeypatches the handful of `luno.vision` functions
    `RealVisionSource` calls, so its orchestration logic can be exercised
    without a real camera/ultralytics install. `detections_by_call` is
    either a fixed list (same detections every cycle) or a callable
    `() -> List[RawDetection]` for cycle-varying scenarios."""
    vision_module.start_watch = lambda: None
    vision_module.stop_watch = lambda: None
    vision_module.start_vision_watch = lambda: None
    vision_module.stop_vision_watch = lambda: None
    vision_module.release_camera = lambda: None
    vision_module.last_detections = lambda: ([], None)
    vision_module.last_vision_description = lambda: (None, None)
    vision_module.capture_frame = lambda: ("FRAME" if camera_ok else None)
    vision_module.camera_status = lambda: {"connected": camera_ok, "source": 0, "error": None if camera_ok else "no frame"}

    def _detect(frame=None):
        return detections_by_call() if callable(detections_by_call) else list(detections_by_call)

    vision_module.detect_objects_tracked = _detect
    vision_module.attach_pose_keypoints = lambda frame, dets: dets


@scenario
def test_05_real_backend_tracked_cycle_publishes_structured_events():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 25.0
    _install_fake_real_vision([
        RawDetection(label="cup", confidence=0.9, bbox=(0, 0, 10, 10)),
        RawDetection(label="person", confidence=0.85, bbox=(20, 20, 60, 160)),
    ])

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, bus = _new_adapter(source=source)
    try:
        time.sleep(0.3)
    finally:
        adapter.stop()
        time.sleep(0.1)

    types = bus.types()
    assert "object_detected" in types
    assert "human_entered" in types
    assert "scene_changed" in types
    assert "vision_frame_processed" in types
    status = adapter._extra_status()
    assert status["backend"] == "real"
    assert status["object_count"] == 2
    assert status["human_count"] == 1


# ============================================================================
# 4) Object tracking - stable ids, no flicker
# ============================================================================

@scenario
def test_06_object_tracker_keeps_stable_id_across_frames():
    tracker = ObjectTracker(iou_threshold=0.3, tracking_timeout_s=5.0, max_tracked=10)
    r1 = tracker.update([RawDetection("cup", 0.9, (0, 0, 10, 10))], now=0.0)
    r2 = tracker.update([RawDetection("cup", 0.91, (0.5, 0.5, 10.5, 10.5))], now=0.1)
    assert r1[0].id == r2[0].id == "cup#1"
    assert r2[0].tracking_age_s > 0


@scenario
def test_07_object_tracker_no_flicker_on_tiny_jitter():
    """A near-identical bounding box across frames must keep the SAME id
    - the spec's own "avoid flickering detections" requirement."""
    tracker = ObjectTracker(iou_threshold=0.3, tracking_timeout_s=5.0, max_tracked=10)
    ids = set()
    for i in range(10):
        jitter = i * 0.05
        result = tracker.update([RawDetection("book", 0.9, (jitter, jitter, 10 + jitter, 10 + jitter))], now=i * 0.1)
        ids.add(result[0].id)
    assert ids == {"book#1"}


@scenario
def test_08_object_tracker_timeout_evicts_and_reassigns_new_id():
    tracker = ObjectTracker(iou_threshold=0.3, tracking_timeout_s=1.0, max_tracked=10)
    tracker.update([RawDetection("cup", 0.9, (0, 0, 10, 10))], now=0.0)
    r2 = tracker.update([RawDetection("cup", 0.9, (0, 0, 10, 10))], now=2.0)  # 2s gap > 1s timeout
    assert r2[0].id == "cup#2"
    assert "cup#1" in tracker.lost_ids


@scenario
def test_09_object_tracker_max_objects_cap_protects_fresh_matches():
    tracker = ObjectTracker(max_tracked=2)
    tracker.update([RawDetection("book", 0.9, (0, 0, 5, 5))], now=0)
    tracker.update([RawDetection("book", 0.9, (0, 0, 5, 5)), RawDetection("phone", 0.9, (20, 20, 25, 25))], now=0.01)
    result = tracker.update([
        RawDetection("book", 0.9, (0, 0, 5, 5)),
        RawDetection("phone", 0.9, (20, 20, 25, 25)),
        RawDetection("bottle", 0.9, (40, 40, 45, 45)),
    ], now=0.02)
    ids = {d.id for d in result}
    # Both already-confirmed tracks (book/phone) must survive; the
    # brand-new "bottle" this same frame is the one dropped, NOT an
    # eviction of something just matched a moment ago.
    assert ids == {"book#1", "phone#1"}
    assert not tracker.lost_ids


# ============================================================================
# 5) Human tracking - pose/orientation ESTIMATES, no biometric identification
# ============================================================================

def _make_keypoints(nose=True, l_eye=True, r_eye=True, hip_y=100, knee_y=150, ankle_y=200, wrist_up=False):
    kps = [(0.0, 0.0, 0.0)] * 17
    if nose:
        kps[0] = (50, 30, 0.9)
    if l_eye:
        kps[1] = (48, 25, 0.9)
    if r_eye:
        kps[2] = (52, 25, 0.9)
    kps[5] = (40, 60, 0.9)  # left shoulder
    kps[6] = (60, 60, 0.9)  # right shoulder
    kps[9] = (40, 20 if wrist_up else 90, 0.9)  # left wrist
    kps[10] = (60, 90, 0.9)  # right wrist
    kps[11] = (40, hip_y, 0.9)
    kps[13] = (40, knee_y, 0.9)
    kps[15] = (40, ankle_y, 0.9)
    return kps


@scenario
def test_10_human_state_standing_facing_camera():
    est = HumanStateEstimator()
    det = TrackedDetection(id="person#1", label="person", confidence=0.9, bbox=(0, 0, 100, 220),
                            first_seen=0, last_seen=0, tracking_age_s=0, keypoints=_make_keypoints())
    state = est.estimate(det)
    assert state.posture == Posture.STANDING
    assert state.facing == Facing.TOWARD_CAMERA
    assert state.hand_raised is False
    assert state.presence == Presence.PRESENT


@scenario
def test_11_human_state_sitting():
    est = HumanStateEstimator()
    det = TrackedDetection(id="person#2", label="person", confidence=0.9, bbox=(0, 0, 100, 120),
                            first_seen=0, last_seen=0, tracking_age_s=0,
                            keypoints=_make_keypoints(hip_y=100, knee_y=110, ankle_y=200))
    assert est.estimate(det).posture == Posture.SITTING


@scenario
def test_12_human_state_hand_raised():
    est = HumanStateEstimator()
    det = TrackedDetection(id="person#3", label="person", confidence=0.9, bbox=(0, 0, 100, 220),
                            first_seen=0, last_seen=0, tracking_age_s=0, keypoints=_make_keypoints(wrist_up=True))
    assert est.estimate(det).hand_raised is True


@scenario
def test_13_human_state_looking_away():
    est = HumanStateEstimator()
    det = TrackedDetection(id="person#4", label="person", confidence=0.9, bbox=(0, 0, 100, 220),
                            first_seen=0, last_seen=0, tracking_age_s=0, keypoints=_make_keypoints(r_eye=False))
    assert est.estimate(det).facing == Facing.AWAY


@scenario
def test_14_human_state_walking_detected_via_movement():
    est = HumanStateEstimator(walking_speed_threshold=0.05)
    a = TrackedDetection(id="person#5", label="person", confidence=0.9, bbox=(0, 0, 50, 150),
                          first_seen=0, last_seen=0, tracking_age_s=0, keypoints=None)
    b = TrackedDetection(id="person#5", label="person", confidence=0.9, bbox=(30, 0, 80, 150),
                          first_seen=0, last_seen=0.1, tracking_age_s=0.1, keypoints=None)
    assert est.estimate(a).posture in (Posture.STANDING, Posture.UNKNOWN)
    assert est.estimate(b).posture == Posture.WALKING


@scenario
def test_15_human_state_never_assigns_identity():
    """The spec's explicit "no biometric identification / no identity
    storage" requirement - `HumanState` has no identity/name field at
    all, and the adapter's `HumanObservation` it feeds Vision Memory
    always leaves `identity=None` (see test_20 below)."""
    assert not hasattr(HumanState, "identity")
    assert not hasattr(HumanState, "name")


@scenario
def test_16_human_state_forget_cleans_up_bookkeeping():
    est = HumanStateEstimator()
    det = TrackedDetection(id="person#6", label="person", confidence=0.9, bbox=(0, 0, 50, 150),
                            first_seen=0, last_seen=0, tracking_age_s=0, keypoints=None)
    est.estimate(det)
    assert "person#6" in est.active_ids()
    est.forget("person#6")
    assert "person#6" not in est.active_ids()


# ============================================================================
# 6) Lost objects/humans + duplicate suppression (adapter-level diffing)
# ============================================================================

@scenario
def test_17_lost_objects_and_humans_publish_events():
    adapter, bus = _new_adapter()
    try:
        c1 = VisionCycleResult(
            objects=[TrackedDetection(id="cup#1", label="cup", confidence=0.9, bbox=(0, 0, 10, 10), first_seen=0, last_seen=0, tracking_age_s=0)],
            humans=[HumanState(tracking_id="person#1", posture=Posture.STANDING, facing=Facing.TOWARD_CAMERA, hand_raised=False, presence=Presence.PRESENT)],
            fps=2.0, latency_ms=10.0,
        )
        adapter.source.simulate_vision_cycle(c1)
        bus.published.clear()

        c2 = VisionCycleResult(objects=[], humans=[], lost_object_ids=["cup#1"], lost_human_ids=["person#1"], fps=2.0, latency_ms=10.0)
        adapter.source.simulate_vision_cycle(c2)
        assert "object_lost" in bus.types()
        assert "human_left" in bus.types()
    finally:
        adapter.stop()


@scenario
def test_18_duplicate_detections_do_not_flood_event_bus():
    adapter, bus = _new_adapter()
    try:
        cup = TrackedDetection(id="cup#1", label="cup", confidence=0.9, bbox=(0, 0, 10, 10), first_seen=0, last_seen=0, tracking_age_s=0)
        adapter.source.simulate_vision_cycle(VisionCycleResult(objects=[cup], fps=2.0, latency_ms=10.0))
        bus.published.clear()

        # 5 more cycles, near-identical bbox/confidence (sensor jitter) -
        # must NOT re-fire object_detected/object_updated each time.
        for i in range(5):
            jittered = TrackedDetection(id="cup#1", label="cup", confidence=0.9 + i * 0.001,
                                         bbox=(0.01 * i, 0.01 * i, 10 + 0.01 * i, 10 + 0.01 * i),
                                         first_seen=0, last_seen=i * 0.1, tracking_age_s=i * 0.1)
            adapter.source.simulate_vision_cycle(VisionCycleResult(objects=[jittered], fps=2.0, latency_ms=10.0))

        assert "object_detected" not in bus.types()
        assert "object_updated" not in bus.types()
        # vision_frame_processed is the one event allowed every cycle
        # regardless (it's a heartbeat, not a change signal).
        assert bus.types().count("vision_frame_processed") == 5
    finally:
        adapter.stop()


@scenario
def test_19_camera_status_transitions_deduplicated():
    adapter, bus = _new_adapter()
    try:
        adapter.source.simulate_camera_status({"connected": False, "source": 0, "error": "gone"})
        adapter.source.simulate_camera_status({"connected": False, "source": 0, "error": "gone"})  # dup
        adapter.source.simulate_camera_status({"connected": False, "source": 0, "error": "still gone"})  # dup (same connected value)
        adapter.source.simulate_camera_status({"connected": True, "source": 0, "error": None})
        assert bus.types().count("camera_disconnected") == 1
        assert bus.types().count("camera_reconnected") == 1
    finally:
        adapter.stop()


# ============================================================================
# 7) Vision Memory integration (structured SceneObservation, no direct DB writes)
# ============================================================================

@scenario
def test_20_structured_scene_observation_reaches_vision_memory():
    adapter, bus = _new_adapter()
    try:
        cup = TrackedDetection(id="cup#1", label="cup", confidence=0.9, bbox=(0, 0, 10, 10), first_seen=0, last_seen=0, tracking_age_s=0)
        person = HumanState(tracking_id="person#1", posture=Posture.SITTING, facing=Facing.TOWARD_CAMERA, hand_raised=True, presence=Presence.PRESENT)
        adapter.source.simulate_vision_cycle(VisionCycleResult(objects=[cup], humans=[person], fps=2.0, latency_ms=10.0))

        state = vm.get_world_state()
        assert any(o.label == "cup" for o in state.objects.values())
        assert any(h.identity is None for h in state.humans.values())  # never a name/identity
    finally:
        adapter.stop()


@scenario
def test_21_detect_objects_tracked_label_normalization_and_confidence_filter():
    class _FakeTensor:
        def __init__(self, data):
            self._data = data

        def tolist(self):
            return self._data

    class _FakeBoxes:
        def __init__(self, xyxy, conf, cls):
            self.xyxy = _FakeTensor(xyxy)
            self.conf = _FakeTensor(conf)
            self.cls = _FakeTensor(cls)

    class _FakeResult:
        def __init__(self, boxes):
            self.boxes = boxes

    class _FakeModel:
        names = {0: "person", 1: "cell phone", 3: "dining table"}

        def __call__(self, frame, verbose=False, conf=0.0, device="cpu"):
            # Real ultralytics applies its own `conf` threshold internally
            # and never returns boxes below it - this fake mirrors that so
            # the test exercises the same "caller passes conf, model does
            # the filtering" contract `detect_objects_tracked()` relies on.
            all_boxes = [[0, 0, 10, 10], [20, 20, 30, 30], [40, 40, 50, 50]]
            all_conf = [0.9, 0.1, 0.95]
            all_cls = [1, 0, 3]
            kept = [(b, c, k) for b, c, k in zip(all_boxes, all_conf, all_cls) if c >= conf]
            return [_FakeResult(boxes=_FakeBoxes(
                xyxy=[b for b, _, _ in kept], conf=[c for _, c, _ in kept], cls=[k for _, _, k in kept],
            ))]

    original_get = vision_module._get_yolo_tracking
    original_conf = legacy_config.CONFIDENCE_THRESHOLD
    original_max = legacy_config.MAX_OBJECTS
    vision_module._get_yolo_tracking = lambda: _FakeModel()
    legacy_config.CONFIDENCE_THRESHOLD = 0.2  # excludes the 0.1-confidence detection
    legacy_config.MAX_OBJECTS = 20
    try:
        dets = _REAL_DETECT_OBJECTS_TRACKED(frame=object())
        labels = {d.label for d in dets}
        assert labels == {"phone", "table"}  # COCO names normalized, low-confidence one dropped
    finally:
        vision_module._get_yolo_tracking = original_get
        legacy_config.CONFIDENCE_THRESHOLD = original_conf
        legacy_config.MAX_OBJECTS = original_max


@scenario
def test_21b_get_yolo_tracking_is_a_singleton_delegating_to_get_yolo():
    """RAM regression guard (the exact bug fixed this session): there
    must be only ONE resident YOLO model instance shared by the
    presence-watch path (`_get_yolo()`, used by `start_watch()`/
    `detect_objects()`) and the Sprint 8 tracked-detection cycle
    (`_get_yolo_tracking()`, used by `detect_objects_tracked()`) - NOT
    two independent `YOLO(...)` loads. `_get_yolo_tracking()` must
    delegate to `_get_yolo()` rather than maintaining its own separate
    lazy-loaded model cache.

    Verified structurally (monkeypatching `_get_yolo()` itself with a
    call-counting fake) rather than by actually loading a real YOLO
    model - `ultralytics` isn't installed in this environment (see this
    file's own docstring: "ZERO real camera/GPU/ultralytics install
    required"), and this test only needs to prove DELEGATION, not
    ultralytics' own loading behavior."""
    calls = []
    fake_model = object()
    original_get_yolo = vision_module._get_yolo
    vision_module._get_yolo = lambda: (calls.append(1), fake_model)[1]
    try:
        result_from_tracking_1 = vision_module._get_yolo_tracking()
        result_from_tracking_2 = vision_module._get_yolo_tracking()
        result_from_general = vision_module._get_yolo()
    finally:
        vision_module._get_yolo = original_get_yolo

    assert result_from_tracking_1 is fake_model
    assert result_from_tracking_2 is fake_model
    assert result_from_general is fake_model
    # every call - from EITHER entry point - went through the same
    # underlying `_get_yolo()` singleton logic, never a separate cache
    assert len(calls) == 3

    # Also guard against the specific historical bug reappearing: no
    # module-level `_yolo_tracking_model` cache should exist anymore.
    assert not hasattr(vision_module, "_yolo_tracking_model")


def test_21c_yolo_checkpoint_hint_fires_only_for_the_stale_bn_attribute_error():
    """Reported gap: "[Vision] YOLO detect gagal ... 'Conv' object has
    no attribute 'bn'" gave no actionable next step - confirmed against
    ultralytics' own `BaseModel.fuse()` (nn/tasks.py), this exact
    signature means a LOCAL `.pt` checkpoint file predates the
    installed `ultralytics` version, not a webcam/config problem.
    `_yolo_checkpoint_hint()` must fire ONLY for that precise signature
    (an `AttributeError` whose missing attribute name is exactly "bn")
    and stay silent for every other kind of failure, so it never
    misleads on an unrelated error."""
    try:
        object().bn
    except AttributeError as ex:
        bn_error = ex
    hint = vision_module._yolo_checkpoint_hint(bn_error)
    assert "stale/mismatched" in hint
    assert "pip install -U ultralytics" in hint

    try:
        object().some_other_attribute
    except AttributeError as ex:
        other_attribute_error = ex
    assert vision_module._yolo_checkpoint_hint(other_attribute_error) == ""

    assert vision_module._yolo_checkpoint_hint(ValueError("unrelated failure")) == ""
    assert vision_module._yolo_checkpoint_hint(RuntimeError("camera not found")) == ""


# ============================================================================
# 8) Dashboard updates
# ============================================================================

@scenario
def test_22_dashboard_collector_reflects_live_vision_state():
    from luno.adapters.manager import AdapterManager
    from luno.adapters.models import AdapterConfig
    from luno.core.runtime import Runtime
    from luno.dashboard import collectors

    _isolate_vision_memory()
    runtime = Runtime()
    runtime.start()
    try:
        mgr = AdapterManager(runtime.module_manager, runtime.coordinator, runtime.event_bus)
        adapter = VisionAdapter(source=MockVisionSource())
        mgr.register(adapter, AdapterConfig(name="vision"))
        adapter.bind(runtime.event_bus)
        adapter.start()

        cup = TrackedDetection(id="cup#1", label="cup", confidence=0.9, bbox=(0, 0, 10, 10), first_seen=0, last_seen=0, tracking_age_s=0)
        adapter.source.simulate_vision_cycle(VisionCycleResult(objects=[cup], fps=3.0, latency_ms=12.0))

        result = collectors.collect_vision(mgr)
        assert result["available"] is True
        assert result["backend"] == "mock"
        assert result["object_count"] == 1
        assert result["fps"] == 3.0
        assert any(o["label"] == "cup" for o in result["objects"])
    finally:
        runtime.stop()


@scenario
def test_23_dashboard_collector_handles_missing_vision_adapter():
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from luno.dashboard import collectors

    runtime = Runtime()
    runtime.start()
    try:
        mgr = AdapterManager(runtime.module_manager, runtime.coordinator, runtime.event_bus)
        result = collectors.collect_vision(mgr)
        assert result["available"] is False
    finally:
        runtime.stop()


# ============================================================================
# 9) Frame skipping / configurable FPS
# ============================================================================

@scenario
def test_24_tracked_cycle_respects_configured_fps():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 10.0  # ~0.1s per cycle
    _install_fake_real_vision([RawDetection(label="cup", confidence=0.9, bbox=(0, 0, 10, 10))])

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, bus = _new_adapter(source=source)
    try:
        time.sleep(0.5)  # should yield roughly 5 cycles at 10fps
    finally:
        adapter.stop()

    frame_events = bus.types().count("vision_frame_processed")
    # Generous bounds (sandboxed CI timing) - proves the loop is rate-
    # limited by VISION_FPS rather than spinning as fast as possible
    # (which would produce vastly more than ~5) or stalling entirely.
    assert 2 <= frame_events <= 12, frame_events


@scenario
def test_25_pose_model_only_invoked_when_person_present():
    calls = {"n": 0}

    def _pose_model():
        calls["n"] += 1
        raise AssertionError("pose model must not be invoked with no person in frame")

    original = vision_module._get_yolo_pose
    vision_module._get_yolo_pose = _pose_model
    try:
        only_objects = [RawDetection(label="cup", confidence=0.9, bbox=(0, 0, 10, 10))]
        result = _REAL_ATTACH_POSE_KEYPOINTS(object(), only_objects)
        assert calls["n"] == 0
        assert result[0].keypoints is None
    finally:
        vision_module._get_yolo_pose = original


# ============================================================================
# 10) Failure handling: SystemError + restart-only-the-vision-adapter
# ============================================================================

@scenario
def test_26_inference_failure_publishes_system_error_and_self_restarts():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 40.0

    def _boom():
        raise RuntimeError("camera exploded")

    vision_module.start_watch = lambda: None
    vision_module.stop_watch = lambda: None
    vision_module.start_vision_watch = lambda: None
    vision_module.stop_vision_watch = lambda: None
    vision_module.release_camera = lambda: None
    vision_module.last_detections = lambda: ([], None)
    vision_module.last_vision_description = lambda: (None, None)
    vision_module.capture_frame = _boom
    vision_module.camera_status = lambda: {"connected": False, "source": 0, "error": "camera exploded"}

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, bus = _new_adapter(source=source)
    try:
        time.sleep(0.8)
    finally:
        adapter.stop()
        time.sleep(0.2)

    assert bus.types().count("system_error") >= MAX_CONSECUTIVE_CYCLE_FAILURES
    assert adapter._restart_count >= 1
    # Runtime-level guarantee: the adapter itself is what restarted, not
    # anything torn down or crashed - the adapter object is still usable.
    assert adapter._restart_count < 1000  # sanity: not a runaway restart storm


# ============================================================================
# 11) Thread leak
# ============================================================================

def _vision_thread_names() -> List[str]:
    return [t.name for t in threading.enumerate() if "luno-vision" in t.name]


@scenario
def test_27_no_thread_leak_across_start_stop_cycles():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 20.0
    _install_fake_real_vision([RawDetection(label="cup", confidence=0.9, bbox=(0, 0, 10, 10))])

    for _ in range(3):
        source = RealVisionSource(poll_interval_s=100.0)
        adapter, _bus = _new_adapter(source=source)
        time.sleep(0.15)
        adapter.stop()
        time.sleep(0.1)

    assert _vision_thread_names() == []


@scenario
def test_28_no_thread_leak_across_restarts_triggered_by_failures():
    """The exact bug caught during development: `listener.restart()`
    called from WITHIN the tracked-cycle thread itself must never leave
    the old generation's thread running alongside the new one - see
    `RealVisionSource.start()`'s own "fresh Event every generation"
    comment for the fix."""
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 30.0

    def _boom():
        raise RuntimeError("boom")

    vision_module.start_watch = lambda: None
    vision_module.stop_watch = lambda: None
    vision_module.start_vision_watch = lambda: None
    vision_module.stop_vision_watch = lambda: None
    vision_module.release_camera = lambda: None
    vision_module.last_detections = lambda: ([], None)
    vision_module.last_vision_description = lambda: (None, None)
    vision_module.capture_frame = _boom
    vision_module.camera_status = lambda: {"connected": False, "source": 0, "error": "boom"}

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, _bus = _new_adapter(source=source)
    try:
        time.sleep(1.0)  # long enough to trigger several restarts
    finally:
        adapter.stop()
        time.sleep(0.2)

    assert adapter._restart_count >= 1
    assert _vision_thread_names() == []


# ============================================================================
# 12) Stress test / long-running stability (compressed)
# ============================================================================

@scenario
def test_29_stress_many_cycles_varying_scene_no_crash_no_leak():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 50.0
    legacy_config.MAX_OBJECTS = 5
    legacy_config.TRACKING_TIMEOUT = 0.2

    counter = {"n": 0}

    def _varying_detections():
        counter["n"] += 1
        n = counter["n"]
        labels = ["cup", "phone", "book", "bottle", "laptop", "person"]
        dets = []
        for i, label in enumerate(labels):
            if (n + i) % 3 != 0:  # objects flicker in/out across cycles
                dets.append(RawDetection(label=label, confidence=0.5 + (i * 0.05), bbox=(i * 10.0, i * 10.0, i * 10.0 + 20, i * 10.0 + 20)))
        return dets

    _install_fake_real_vision(_varying_detections)

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, bus = _new_adapter(source=source)
    try:
        time.sleep(1.5)
    finally:
        adapter.stop()
        time.sleep(0.2)

    assert counter["n"] > 20  # a meaningful number of cycles actually ran
    assert "system_error" not in bus.types()  # varying-but-valid input is never a failure
    assert _vision_thread_names() == []
    status = adapter._extra_status()
    assert status["object_count"] <= 5  # MAX_OBJECTS respected throughout


# ============================================================================
# 13) Config reload picks up changed tracking knobs
# ============================================================================

@scenario
def test_30_tracker_config_rebuilt_on_restart_reload():
    legacy_config.CAMERA_VISION_ENABLED = False
    legacy_config.CAMERA_VISION_WATCH_ENABLED = False
    legacy_config.VISION_FPS = 20.0
    legacy_config.MAX_OBJECTS = 20
    _install_fake_real_vision([RawDetection(label="cup", confidence=0.9, bbox=(0, 0, 10, 10))])

    source = RealVisionSource(poll_interval_s=100.0)
    adapter, _bus = _new_adapter(source=source)
    try:
        time.sleep(0.15)
        # Simulate a user editing .env + running /reload: the value
        # changes, then the SAME restart_all()-style call re-applies it.
        legacy_config.MAX_OBJECTS = 1
        adapter.restart()
        assert source._tracker.max_tracked == 1
    finally:
        adapter.stop()


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
