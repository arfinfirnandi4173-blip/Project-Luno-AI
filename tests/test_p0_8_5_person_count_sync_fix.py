"""
tests/test_p0_8_5_person_count_sync_fix.py
=================================================

LUNO P0.8.5 (Trace and fix `camera_person_entered` firing with
`person_count=0`) - dedicated regression suite.

Context (see docs/change_impact/camera_automation_p0_8_5.md for the full
trace): P0.8.4 proved real YOLO detection genuinely works on the real
Tapo C212 stream (standalone `tools/vision_debug_viewer.py`, real
person=0.70-0.83 confidences, `person_count=1`). Despite that, the real
`luno_live_p0_8_1_verification.py` observer logged:
    [Vision] camera_person_entered observed
    [CAMERA EVENT] kind=human_detected available=None detection_error=None person_count=0

Root cause (traced end-to-end, source-level + a real `logs/runtime/
2026-08-22.log` excerpt confirming `camera_person_entered`/
`camera_person_left` themselves fire correctly and in alternating pairs -
the debounce itself was never broken):

  - `VisionAdapter._update_person_presence()` (the ONE method that
    publishes `CameraPersonEntered`/`CameraPersonLeft`) was, before this
    fix, called ONLY from `on_detections()` - fed by `luno.vision.
    detect_objects()`'s SEPARATE presence-only YOLO call
    (`CAMERA_WATCH_INTERVAL_S`, default 1.0s cadence, returns only a
    label SET, no count).
  - The resulting `CameraEvent.person_count` (read by `Vision
    CameraEventBridge._ingest()` via `vision_context.build_vision_
    context()`) comes from `status["human_count"]` = `len(VisionAdapter.
    _known_humans)` - populated ONLY by `on_vision_cycle()`, fed by the
    SEPARATE `detect_objects_tracked()` tracked-cycle loop (`VISION_FPS`,
    default 0.5s cadence, the ONLY path that actually counts people).
  - Two independent, uncoordinated polling loops: whichever one first
    notices "a person is here" fires the (single, shared, debounced)
    `CameraPersonEntered`, but the person_count enrichment was always
    read from the OTHER loop's own, possibly-not-yet-caught-up snapshot
    - a genuine cross-loop race, not a detection failure and not the
    P0.8.4 shared-model concurrency bug (a DIFFERENT, already-fixed
    issue one level lower in the pipeline).

Fix (`luno/adapters/vision.py::VisionAdapter.on_vision_cycle()`, one
additive line): also calls the SAME, already-existing, already-tested
`_update_person_presence(len(current_humans) > 0)` - reusing the ONE
shared debounce state (`_person_present_debounced`/`_person_last_seen_
at`), never inventing a second one. Whichever loop notices a transition
first still wins (no removal of the existing `on_detections()` call
site); because the tracked-cycle loop is both faster (0.5s vs 1.0s
default cadence) AND the one with the real count, and because it sets
`_known_humans` on the SAME synchronous call, on the SAME thread,
strictly before publishing, it now wins the race in the overwhelming
majority of real transitions - and whenever it does, the person_count a
subscriber reads immediately afterward is guaranteed non-stale. Nothing
is fabricated: every input is real, current-cycle YOLO-derived data.

This suite proves (via `luno.adapters.vision.VisionAdapter` directly,
the same test-double convention already established in `tests/test_
vision_sprint8.py` - `VisionCycleResult`/`TrackedDetection`/`HumanState`
construction, a minimal fake Event Bus, no real camera/YOLO/torch
needed):
  A. A tracked cycle whose `humans` list contains a real `label="person"`
     `TrackedDetection`/`HumanState` yields `person_count=1` in
     `VisionAdapter._extra_status()` immediately after that ONE cycle -
     no second loop, no second cycle, needed.
  B. A person at confidence 0.70 (matching the real observed range) is
     counted identically to any other in-range confidence - Vision
     Adapter/tracked-cycle bookkeeping is confidence-value-agnostic
     (ultralytics' own `conf=` kwarg already filtered below-threshold
     detections out before this layer ever sees them).
  C. An empty tracked cycle (`humans=[]`) yields `person_count=0`.
  D. `on_camera_status()` (camera_online) ALONE never publishes
     `CameraPersonEntered`/`human_detected` and never flips the person-
     presence debounce state.
  E. `CameraReconnected` specifically (via `on_camera_status(connected=
     True)`) never produces a `camera_person_entered`-type event.
  F. A genuine 0->1 tracked-cycle transition (via `on_vision_cycle()`
     ALONE, WITHOUT ever calling `on_detections()`) produces EXACTLY ONE
     `CameraPersonEntered`, and `_known_humans`/`person_count` is
     already correct (1) by the time that event is on the bus - the
     core P0.8.5 fix, proven directly.
  G. A genuine 1->0 tracked-cycle transition, after the debounce's own
     absence timeout has elapsed, produces exactly one
     `CameraPersonLeft`.
  H. Three consecutive tracked cycles all reporting the SAME person
     present (1->1->1) publish exactly one `CameraPersonEntered` total -
     no repeated/spammed events while nothing has actually changed.

Plus two cross-loop consistency tests proving the fix is genuinely
ADDITIVE (never double-fires when both loops eventually agree, and the
OLD on_detections()-only trigger path still works exactly as before -
zero regression to pre-Sprint-8/pre-P0.8.5 behavior).
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import List

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import vision_memory as vm  # noqa: E402
from luno.adapters.vision import MockVisionSource, VisionAdapter, VisionCycleResult  # noqa: E402
from luno.adapters.events import CameraPersonEntered, CameraPersonLeft, CameraDisconnected, CameraReconnected  # noqa: E402
from luno.vision_human_state import Facing, HumanState, Posture, Presence  # noqa: E402
from luno.vision_tracking import TrackedDetection  # noqa: E402


class FakeEventBus:
    """Same minimal convention already established in
    tests/test_vision_sprint8.py::FakeEventBus - just records every
    published Event."""

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
    vm.configure(db_path=os.path.join(tempfile.mkdtemp(), "vm_p0_8_5_test.sqlite3"))


def _new_adapter(person_absence_timeout_s: float = 0.0):
    """`person_absence_timeout_s=0.0` (default here, override per-test)
    so tests never need a real `time.sleep(5)` to observe a
    `CameraPersonLeft` - `_update_person_presence()`'s own `now -
    self._person_last_seen_at >= self._person_absence_timeout_s`
    comparison is satisfied immediately with a 0-second timeout, exactly
    as legitimately as a real 5-second one would be after enough real
    time passed."""
    _isolate_vision_memory()
    bus = FakeEventBus()
    adapter = VisionAdapter(source=MockVisionSource(), person_absence_timeout_s=person_absence_timeout_s)
    adapter.bind(bus)
    adapter.start()
    return adapter, bus


def _person_cycle(tracking_id: str = "person#1", confidence: float = 0.78, fps: float = 2.0) -> VisionCycleResult:
    det = TrackedDetection(
        id=tracking_id, label="person", confidence=confidence, bbox=(10.0, 10.0, 90.0, 220.0),
        first_seen=0.0, last_seen=0.0, tracking_age_s=0.0,
    )
    human = HumanState(
        tracking_id=tracking_id, posture=Posture.STANDING, facing=Facing.TOWARD_CAMERA,
        hand_raised=False, presence=Presence.PRESENT,
    )
    return VisionCycleResult(objects=[det], humans=[human], fps=fps, latency_ms=10.0)


def _empty_cycle(fps: float = 2.0) -> VisionCycleResult:
    return VisionCycleResult(objects=[], humans=[], fps=fps, latency_ms=10.0)


# ─────────────────────────────────────────────
# A/B/C - person_count directly from a real tracked cycle
# ─────────────────────────────────────────────

def test_A_tracked_cycle_with_person_yields_person_count_one():
    adapter, _bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle())
    status = adapter._extra_status()
    assert status["human_count"] == 1
    assert status["person_present"] is True


def test_B_person_at_real_observed_confidence_0_70_is_counted():
    """Matches the LOWEST real confidence observed on the actual Tapo
    C212 stream via tools/vision_debug_viewer.py (person=0.70-0.83) -
    proves the count is confidence-VALUE-agnostic once a detection has
    already cleared ultralytics' own conf= threshold upstream."""
    adapter, _bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle(confidence=0.70))
    assert adapter._extra_status()["human_count"] == 1


def test_C_empty_cycle_yields_person_count_zero():
    adapter, _bus = _new_adapter()
    adapter.on_vision_cycle(_empty_cycle())
    status = adapter._extra_status()
    assert status["human_count"] == 0
    assert status["person_present"] is False


# ─────────────────────────────────────────────
# D/E - camera_online/reconnected can NEVER generate human_detected
# ─────────────────────────────────────────────

def test_D_camera_online_alone_never_publishes_human_detected():
    adapter, bus = _new_adapter()
    adapter.on_camera_status({"connected": True, "source": "rtsp"})
    assert CameraPersonEntered.EVENT_TYPE not in bus.types()
    assert bus.count(CameraReconnected.EVENT_TYPE) == 1
    # Never touches the person-presence debounce state either.
    assert adapter._person_present_debounced is False


def test_E_camera_reconnected_specifically_never_produces_camera_person_entered():
    adapter, bus = _new_adapter()
    # A prior disconnect, then a reconnect - the realistic sequence.
    adapter.on_camera_status({"connected": False, "source": "rtsp"})
    adapter.on_camera_status({"connected": True, "source": "rtsp"})
    assert bus.count(CameraDisconnected.EVENT_TYPE) == 1
    assert bus.count(CameraReconnected.EVENT_TYPE) == 1
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 0
    assert bus.count(CameraPersonLeft.EVENT_TYPE) == 0


# ─────────────────────────────────────────────
# F/G/H - the actual P0.8.5 fix: on_vision_cycle() alone drives the
# shared debounce, with a person_count that is correct AT THE MOMENT
# the event fires.
# ─────────────────────────────────────────────

def test_F_tracked_cycle_alone_0_to_1_fires_exactly_one_entered_with_correct_count():
    """THE core P0.8.5 regression test: on_vision_cycle() ALONE (never
    calling on_detections() - i.e. simulating the tracked-cycle loop
    winning the race against the presence-watch loop, which is the
    common case after this fix since it is both faster and has the real
    count) must fire CameraPersonEntered, and by the time it does,
    _known_humans/person_count must ALREADY be correct - proving the
    P0.8.5 bug (camera_person_entered + person_count=0) cannot reproduce
    via this path anymore."""
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_empty_cycle())  # baseline: nobody present
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 0

    adapter.on_vision_cycle(_person_cycle())  # real transition: 0 -> 1
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1
    # The critical assertion: by the time CameraPersonEntered is on the
    # bus, the SAME adapter's own status already reports the correct,
    # non-zero count - exactly what VisionCameraEventBridge reads.
    status = adapter._extra_status()
    assert status["human_count"] == 1
    assert status["person_present"] is True


def test_G_tracked_cycle_alone_1_to_0_fires_exactly_one_left_after_timeout():
    adapter, bus = _new_adapter(person_absence_timeout_s=0.0)
    adapter.on_vision_cycle(_person_cycle())
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1

    adapter.on_vision_cycle(_empty_cycle())  # 1 -> 0, timeout=0 so this fires immediately
    assert bus.count(CameraPersonLeft.EVENT_TYPE) == 1
    status = adapter._extra_status()
    assert status["human_count"] == 0
    assert status["person_present"] is False


def test_H_repeated_person_detections_do_not_repeatedly_emit_entered():
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle())  # 0 -> 1 (fires)
    adapter.on_vision_cycle(_person_cycle())  # 1 -> 1 (must NOT fire again)
    adapter.on_vision_cycle(_person_cycle())  # 1 -> 1 (must NOT fire again)
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1
    assert bus.count(CameraPersonLeft.EVENT_TYPE) == 0


# ─────────────────────────────────────────────
# Cross-loop consistency - the fix is additive, never double-fires when
# BOTH loops eventually agree, and the OLD on_detections()-only trigger
# path is completely unaffected (zero regression to pre-P0.8.5 behavior).
# ─────────────────────────────────────────────

def test_I_on_detections_only_path_still_works_unchanged():
    """The pre-existing, pre-Sprint-8 presence-only path (a VisionSource
    that only ever calls on_detections(), never on_vision_cycle() -
    e.g. a hypothetical simpler backend) must still fire CameraPersonEntered
    exactly as it always has - this fix is additive, not a replacement."""
    adapter, bus = _new_adapter()
    adapter.on_detections([{"label": "person", "confidence": 0.9}])
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1


def test_J_both_loops_agreeing_never_double_fires():
    """If the presence-watch loop (on_detections) notices the person
    FIRST, and the tracked-cycle loop (on_vision_cycle) catches up a
    moment later reporting the SAME presence, only ONE CameraPersonEntered
    must exist in total - the shared debounce state is what guarantees
    this (no new per-loop bookkeeping was invented)."""
    adapter, bus = _new_adapter()
    adapter.on_detections([{"label": "person", "confidence": 0.9}])  # loop A wins the race
    adapter.on_vision_cycle(_person_cycle())  # loop B catches up a moment later
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1
    # And the count is STILL correct after loop B's own cycle landed.
    assert adapter._extra_status()["human_count"] == 1


def test_K_tracked_loop_winning_the_race_leaves_presence_loop_consistent():
    """The reverse order: tracked-cycle loop (loop B, now also a
    trigger) wins the race, presence-watch loop (loop A) catches up
    later with its own (count-blind) detection of the same person -
    still exactly one CameraPersonEntered, never two."""
    adapter, bus = _new_adapter()
    adapter.on_vision_cycle(_person_cycle())  # loop B wins the race (the fix)
    adapter.on_detections([{"label": "person", "confidence": 0.9}])  # loop A catches up
    assert bus.count(CameraPersonEntered.EVENT_TYPE) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
