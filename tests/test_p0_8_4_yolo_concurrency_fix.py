"""
tests/test_p0_8_4_yolo_concurrency_fix.py
=================================================

LUNO P0.8.4 (Resolve the Actual YOLO Model / Ultralytics Compatibility
Failure) - dedicated regression suite.

Context (see docs/change_impact/camera_automation_p0_8_4.md for the full
investigation): P0.8.3 fixed `_yolo_checkpoint_hint()`'s exception-matching
logic but honestly reported the underlying `AttributeError: 'Conv' object
has no attribute 'bn'` failure itself as UNRESOLVED - the real model/
ultralytics compatibility question had not been proven either way.

P0.8.4 could not execute real `torch`/`ultralytics` inference in this
sandbox (PyPI's Linux `torch==2.13.0` wheel dlopen's real CUDA runtime
libraries at import time via non-lazy/data-relocation-bound symbol
references - not fixable by stubbing `.so` files without genuine CUDA
binaries; see the P0.8.4 change-impact doc Section 1 for the full
evidence). Root cause was instead established via direct source
inspection of the real, exact-version-matching `torch==2.13.0`/
`ultralytics==8.4.123` packages (downloaded from PyPI to match the real
machine's installed versions) combined with the pickle-level checkpoint
forensics already completed in P0.8.3:

  - `_get_yolo()` (used by `detect_objects()`, `_monitor_loop()`, and -
    via `_get_yolo_tracking()`, a pure alias - `detect_objects_tracked()`)
    returns ONE shared `ultralytics.YOLO` singleton (`_yolo_model`).
  - `start_watch()` runs `detect_objects()` on its own background thread
    while `RealVisionSource`'s tracked-cycle loop runs
    `detect_objects_tracked()` concurrently on a SEPARATE thread - both
    against that same shared model object.
  - Before this fix, `detect_objects()` never passed `device=`, while
    `detect_objects_tracked()` always passed `device=_device_arg()`.
    `ultralytics.engine.model.Model.predict()` rebuilds its cached
    `self.predictor` (and re-runs `.fuse()` on the shared, already-fused
    underlying `nn.Module`) whenever `self.predictor.args.device !=
    args.get("device", self.predictor.args.device)` - a check this
    device-kwarg mismatch caused to flip-flop as the two threads
    interleaved, and which is not thread-safe to begin with.
  - `_yolo_lock` (already existed) only ever guarded the LAZY
    CONSTRUCTION of `_yolo_model`, never the actual inference call -
    so a `fuse()`-triggered `delattr(m, "bn")` on one thread could race
    a concurrently-running `Conv.forward()` reading `self.bn` on the
    other thread. This fully explains the real machine's "every cycle"
    `AttributeError: 'Conv' object has no attribute 'bn'` failure without
    requiring any model/dependency incompatibility.

Fix (`luno/vision.py`, additive/surgical only - no ultralytics/torch/`.pt`
file touched, no safety-gate/architecture change):
  - `detect_objects()`, `detect_objects_tracked()`, and `_monitor_loop()`
    now all pass the SAME explicit `device=_device_arg()` on every call.
  - All three now wrap the actual `model(frame, ...)` call itself (not
    just construction) in the pre-existing `_yolo_lock`.

This suite proves those two properties hold, using the same
monkeypatch-a-fake-model convention already established in
`test_p0_6_2_fix_vision_runtime_parity.py` - no real torch/ultralytics
needed, consistent with every other Vision unit test in this project.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.vision as vision  # noqa: E402


class _FakeBoxes:
    cls = type("T", (), {"tolist": lambda self: []})()


class _FakeResult:
    boxes = _FakeBoxes()


def _restore(attr_name, original):
    setattr(vision, attr_name, original)


# ────────────────────────────────────────────────────────────────
# Device-kwarg consistency
# ────────────────────────────────────────────────────────────────

def test_01_detect_objects_now_passes_device_kwarg():
    """Before P0.8.4, `detect_objects()` called `model(frame, ...)` with
    NO `device=` kwarg at all - the root cause of the predictor/device
    mismatch race documented above. It must now always pass one."""
    seen = {}

    class _FakeModel:
        names = {}

        def __call__(self, *a, **kw):
            seen.update(kw)
            return [_FakeResult()]

    original = vision._get_yolo
    vision._get_yolo = lambda: _FakeModel()
    original_capture = vision._capture_frame
    vision._capture_frame = lambda: object()
    try:
        vision.detect_objects()
        assert "device" in seen, "detect_objects() must pass an explicit device= kwarg (P0.8.4)"
        assert seen["device"] == vision._device_arg()
    finally:
        _restore("_get_yolo", original)
        _restore("_capture_frame", original_capture)


def test_02_detect_objects_tracked_still_passes_device_kwarg():
    """Must remain unchanged in intent (already passed device=), still
    consistent with detect_objects() now matching it."""
    seen = {}

    class _FakeModel:
        names = {}

        def __call__(self, *a, **kw):
            seen.update(kw)
            return [_FakeResult()]

    original = vision._get_yolo_tracking
    vision._get_yolo_tracking = lambda: _FakeModel()
    try:
        vision.detect_objects_tracked(frame=object())
        assert "device" in seen
        assert seen["device"] == vision._device_arg()
    finally:
        _restore("_get_yolo_tracking", original)


def test_03_detect_objects_and_detect_objects_tracked_pass_the_identical_device_value():
    """The actual defect: two call sites disagreeing on whether/what
    device= to pass, against the SAME shared singleton. Must now agree."""
    seen_plain = {}
    seen_tracked = {}

    def _plain_model(*a, **kw):
        seen_plain.update(kw)
        return [_FakeResult()]

    def _tracked_model(*a, **kw):
        seen_tracked.update(kw)
        return [_FakeResult()]

    class _PlainFake:
        names = {}
        __call__ = staticmethod(_plain_model)

    class _TrackedFake:
        names = {}
        __call__ = staticmethod(_tracked_model)

    orig_plain = vision._get_yolo
    orig_tracked = vision._get_yolo_tracking
    orig_capture = vision._capture_frame
    vision._get_yolo = lambda: _PlainFake()
    vision._get_yolo_tracking = lambda: _TrackedFake()
    vision._capture_frame = lambda: object()
    try:
        vision.detect_objects()
        vision.detect_objects_tracked(frame=object())
        assert seen_plain.get("device") == seen_tracked.get("device"), (
            "detect_objects() and detect_objects_tracked() must pass the "
            "identical device= value against the shared _yolo_model "
            "singleton - a mismatch here is exactly the P0.8.4 root cause."
        )
    finally:
        _restore("_get_yolo", orig_plain)
        _restore("_get_yolo_tracking", orig_tracked)
        _restore("_capture_frame", orig_capture)


def test_04_monitor_loop_source_passes_device_kwarg():
    """`_monitor_loop()` opens a real cv2 GUI window, so it is not
    practical to unit-test by invocation - verified via source inspection
    instead, matching this project's established architecture-guard
    convention (see test_p0_6_2_fix_vision_runtime_parity.py test_09)."""
    src = inspect.getsource(vision._monitor_loop)
    assert "device=_device_arg()" in src


# ────────────────────────────────────────────────────────────────
# Lock now wraps the actual inference call, not just construction
# ────────────────────────────────────────────────────────────────

def test_05_detect_objects_holds_yolo_lock_during_model_call():
    holds_lock_during_call = {}

    class _FakeModel:
        names = {}

        def __call__(self, *a, **kw):
            holds_lock_during_call["locked"] = vision._yolo_lock.locked()
            return [_FakeResult()]

    original = vision._get_yolo
    vision._get_yolo = lambda: _FakeModel()
    original_capture = vision._capture_frame
    vision._capture_frame = lambda: object()
    try:
        vision.detect_objects()
        assert holds_lock_during_call.get("locked") is True, (
            "detect_objects() must hold _yolo_lock while the model is "
            "actually running inference, not just while _get_yolo() "
            "constructs/returns the singleton (P0.8.4)."
        )
    finally:
        _restore("_get_yolo", original)
        _restore("_capture_frame", original_capture)
    # Lock must be released again afterwards (no permanent deadlock risk).
    assert not vision._yolo_lock.locked()


def test_06_detect_objects_tracked_holds_yolo_lock_during_model_call():
    holds_lock_during_call = {}

    class _FakeModel:
        names = {}

        def __call__(self, *a, **kw):
            holds_lock_during_call["locked"] = vision._yolo_lock.locked()
            return [_FakeResult()]

    original = vision._get_yolo_tracking
    vision._get_yolo_tracking = lambda: _FakeModel()
    try:
        vision.detect_objects_tracked(frame=object())
        assert holds_lock_during_call.get("locked") is True
    finally:
        _restore("_get_yolo_tracking", original)
    assert not vision._yolo_lock.locked()


def test_07_lock_released_even_when_model_call_raises():
    """A model() call that raises must not leave _yolo_lock held forever
    - both detect_objects()/detect_objects_tracked() already wrap the
    whole body in try/except, so the `with _yolo_lock:` block's own
    exception-safety must hold end to end."""

    class _BoomModel:
        names = {}

        def __call__(self, *a, **kw):
            raise RuntimeError("simulated detector failure")

    original = vision._get_yolo
    vision._get_yolo = lambda: _BoomModel()
    original_capture = vision._capture_frame
    vision._capture_frame = lambda: object()
    try:
        result = vision.detect_objects()
        assert result == []  # never-raises contract preserved
    finally:
        _restore("_get_yolo", original)
        _restore("_capture_frame", original_capture)
    assert not vision._yolo_lock.locked()


# ────────────────────────────────────────────────────────────────
# Real concurrency proof - two genuine OS threads never overlap inside
# the shared model's __call__
# ────────────────────────────────────────────────────────────────

def test_08_two_real_threads_never_overlap_inside_shared_model_call():
    """The actual bug was two real background threads (`_watch_thread`
    calling detect_objects(), `_cycle_thread` calling
    detect_objects_tracked()) concurrently mutating/reading the SAME
    shared model object. This test spawns two genuine `threading.Thread`s
    hammering detect_objects()/detect_objects_tracked() against a shared
    fake model and asserts the model's own __call__ body is NEVER
    entered by both threads at once - proving _yolo_lock now genuinely
    serializes them."""
    entered = threading.Event()
    overlap_detected = {"value": False}
    lock_state = threading.Lock()  # protects the two bookkeeping vars above

    class _SharedFakeModel:
        names = {}

        def __call__(self, *a, **kw):
            with lock_state:
                if entered.is_set():
                    overlap_detected["value"] = True
                entered.set()
            time.sleep(0.01)  # widen the race window on purpose
            with lock_state:
                entered.clear()
            return [_FakeResult()]

    shared = _SharedFakeModel()
    orig_plain = vision._get_yolo
    orig_tracked = vision._get_yolo_tracking
    orig_capture = vision._capture_frame
    vision._get_yolo = lambda: shared
    vision._get_yolo_tracking = lambda: shared
    vision._capture_frame = lambda: object()

    errors = []

    def _worker_plain():
        try:
            for _ in range(15):
                vision.detect_objects()
        except Exception as ex:  # pragma: no cover - diagnostic only
            errors.append(ex)

    def _worker_tracked():
        try:
            for _ in range(15):
                vision.detect_objects_tracked(frame=object())
        except Exception as ex:  # pragma: no cover - diagnostic only
            errors.append(ex)

    try:
        t1 = threading.Thread(target=_worker_plain)
        t2 = threading.Thread(target=_worker_tracked)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
    finally:
        _restore("_get_yolo", orig_plain)
        _restore("_get_yolo_tracking", orig_tracked)
        _restore("_capture_frame", orig_capture)

    assert not errors
    assert overlap_detected["value"] is False, (
        "Two threads entered the shared model's __call__ at the same "
        "time - _yolo_lock is not actually serializing inference calls."
    )


# ────────────────────────────────────────────────────────────────
# Architecture guards - fix confined to vision.py, no unrelated changes
# ────────────────────────────────────────────────────────────────

def test_09_yolo_pose_model_untouched_no_lock_added():
    """`_get_yolo_pose()`/`attach_pose_keypoints()` use a SEPARATE
    singleton (`_yolo_pose_model`) only ever called from inside the same
    tracked-cycle thread that also calls detect_objects_tracked() -
    never from `_watch_thread` - so it was never exposed to this race and
    intentionally was NOT changed this sprint."""
    src = inspect.getsource(vision.attach_pose_keypoints)
    assert "_yolo_lock" not in src


def test_10_yolo_lock_is_still_a_plain_threading_lock():
    assert isinstance(vision._yolo_lock, type(threading.Lock()))


def test_11_no_new_yolo_pipeline_introduced():
    """Still exactly two `from ultralytics import YOLO` construction
    sites in this file (unchanged since P0.8.3) - the fix only changed
    how the existing shared model is CALLED, never added a new model."""
    src = inspect.getsource(vision)
    assert src.count("from ultralytics import YOLO") == 2


def test_12_safety_gate_and_automation_untouched():
    """This sprint touched luno/vision.py only - no automation engine,
    HA action, or camera safety gate module was imported/referenced by
    the new code."""
    src = inspect.getsource(vision.detect_objects) + inspect.getsource(vision.detect_objects_tracked)
    for forbidden in ("camera_action_safety", "AutomationEngine", "home_assistant", "light.wled"):
        assert forbidden not in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
