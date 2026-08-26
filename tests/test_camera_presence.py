"""
test_camera_presence.py
===========================

Debounced room-level presence signal (`CameraPersonEntered`/
`CameraPersonLeft` - see `luno/adapters/events.py`) added to
`VisionAdapter._update_person_presence()` as part of the Gemini vision
migration. YOLO (local, always-on) is the ONLY input to this state
machine - Gemini is never called here (see the module's own "no vision
API call from the presence path" guard below), matching the migration
task's explicit requirement that presence detection stays fully local
and never triggers a remote vision call.

Uses a controllable fake clock (`vmod.time.time` monkeypatched to a
mutable counter) so the `CAMERA_PERSON_ABSENCE_TIMEOUT_S` debounce
window can be tested deterministically, with zero real `time.sleep()`.

Run:
    python3 -m pytest tests/test_camera_presence.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import luno.adapters.vision as vmod  # noqa: E402
from luno.adapters.events import CameraPersonEntered, CameraPersonLeft  # noqa: E402
from luno.adapters.vision import VisionAdapter  # noqa: E402

# `_adapter()` below patches the SHARED `time` module object's own
# `.time` attribute (`vmod.time` IS the real stdlib `time` module, since
# `luno/adapters/vision.py` does a plain `import time` - every other
# module that does `import time` gets the exact same object out of
# `sys.modules['time']`). Captured here, at collection time, BEFORE any
# test in this file has had a chance to patch it - this is the one
# real value `_restore_real_time_time` below ever restores to.
_REAL_TIME_TIME = vmod.time.time


@pytest.fixture(autouse=True)
def _restore_real_time_time():
    """`_adapter()`'s `vmod.time.time = lambda: ...` was a raw attribute
    assignment with no corresponding restore anywhere in this file - a
    genuine pre-existing bug (found during Sprint 68's own full-suite
    regression verification, unrelated to that sprint's own mutation-
    audit work), not merely theoretical: it permanently replaces the
    real `time.time()` for every test that runs afterward in the SAME
    pytest process, for the rest of that process's life, since `time` is
    a single shared module object process-wide - not scoped to this file
    or even to the `luno.adapters.vision` import path. Two of `tests/
    test_sprint68_mutation_audit_hardening.py`'s own retention tests
    (which call real, unmocked `time.time()`) were observed failing
    ONLY in full-suite runs, always with the exact same frozen value
    (`1_000_000.0`-based) that this file's own `_Clock` uses - proving
    this exact leak, not a timing race, was the cause. Autouse + no
    dependency on any individual test remembering to request
    `monkeypatch` - restores real time after EVERY test in this file,
    regardless of whether that test calls `_adapter()`."""
    yield
    vmod.time.time = _REAL_TIME_TIME


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds


def _adapter(monkeypatch_clock, timeout_s=5.0):
    """Builds a `VisionAdapter` with `_update_person_presence()`'s
    `time.time()` calls redirected to `monkeypatch_clock`, and
    `publish()` recording every event instead of touching a real Event
    Bus - same lightweight pattern `test_vision_sprint8.py` already uses
    for adapter-only tests. The patch this applies is reverted by this
    module's own `_restore_real_time_time` autouse fixture above, after
    every test - not by this function itself."""
    vmod.time.time = lambda: monkeypatch_clock.now
    adapter = VisionAdapter(person_absence_timeout_s=timeout_s)
    events = []
    adapter.publish = lambda e: events.append(e)
    return adapter, events


def _types(events):
    return [type(e).__name__ for e in events]


# ============================================================================
# ABSENT -> PRESENT
# ============================================================================

def test_first_detection_generates_exactly_one_camera_person_entered():
    clock = _Clock()
    adapter, events = _adapter(clock)

    adapter.on_detections([{"label": "person"}])

    entered = [e for e in events if isinstance(e, CameraPersonEntered)]
    assert len(entered) == 1


def test_repeated_detection_while_present_never_generates_repeated_entry_events():
    """The exact 'avoid event spam' requirement: the same person staying
    in frame across many polls must not flood the Event Bus."""
    clock = _Clock()
    adapter, events = _adapter(clock)

    for _ in range(10):
        adapter.on_detections([{"label": "person"}])
        clock.advance(1.0)

    entered = [e for e in events if isinstance(e, CameraPersonEntered)]
    assert len(entered) == 1, f"expected exactly 1 CameraPersonEntered, got {len(entered)}"
    assert [e for e in events if isinstance(e, CameraPersonLeft)] == []


def test_no_detection_ever_generates_no_presence_events_at_all():
    clock = _Clock()
    adapter, events = _adapter(clock)

    for _ in range(5):
        adapter.on_detections([])
        clock.advance(2.0)

    assert [e for e in events if isinstance(e, (CameraPersonEntered, CameraPersonLeft))] == []


# ============================================================================
# PRESENT -> ABSENT (debounced - the actual hysteresis requirement)
# ============================================================================

def test_present_to_absent_generates_exactly_one_camera_person_left_after_timeout():
    clock = _Clock()
    adapter, events = _adapter(clock, timeout_s=5.0)

    adapter.on_detections([{"label": "person"}])  # PRESENT
    clock.advance(6.0)
    adapter.on_detections([])  # missed for 6s > 5s timeout

    left = [e for e in events if isinstance(e, CameraPersonLeft)]
    assert len(left) == 1


def test_a_single_missed_poll_within_the_timeout_does_not_generate_camera_person_left():
    """The actual hysteresis guarantee: one momentary miss (occlusion,
    bad angle, a single dropped YOLO frame) must NOT flip the room to
    ABSENT - only sustained absence for the full timeout window does."""
    clock = _Clock()
    adapter, events = _adapter(clock, timeout_s=5.0)

    adapter.on_detections([{"label": "person"}])  # PRESENT
    clock.advance(1.0)
    adapter.on_detections([])  # missed ONE poll, well under the 5s timeout

    assert [e for e in events if isinstance(e, CameraPersonLeft)] == []

    # person reappears before the timeout would have elapsed - state
    # stays PRESENT the whole time, no entered/left pair generated at all
    clock.advance(1.0)
    adapter.on_detections([{"label": "person"}])
    entered = [e for e in events if isinstance(e, CameraPersonEntered)]
    assert len(entered) == 1  # still just the original one, not a second


def test_repeated_absence_polls_after_the_timeout_only_fire_left_once():
    clock = _Clock()
    adapter, events = _adapter(clock, timeout_s=5.0)

    adapter.on_detections([{"label": "person"}])
    clock.advance(6.0)
    for _ in range(5):
        adapter.on_detections([])
        clock.advance(1.0)

    left = [e for e in events if isinstance(e, CameraPersonLeft)]
    assert len(left) == 1, f"expected exactly 1 CameraPersonLeft, got {len(left)}"


def test_full_enter_leave_enter_cycle_generates_exactly_one_of_each():
    clock = _Clock()
    adapter, events = _adapter(clock, timeout_s=5.0)

    adapter.on_detections([{"label": "person"}])  # enter
    clock.advance(6.0)
    adapter.on_detections([])  # leave (debounced)
    clock.advance(1.0)
    adapter.on_detections([{"label": "person"}])  # enter again

    assert len(v := [e for e in events if isinstance(e, CameraPersonEntered)]) == 2, v
    assert len(v := [e for e in events if isinstance(e, CameraPersonLeft)]) == 1, v


# ============================================================================
# presence never calls the vision API - purely a local YOLO signal
# ============================================================================

def test_presence_updates_never_touch_the_vision_provider():
    """Hard requirement from the migration task: 'No Gemini call should
    happen merely because someone entered.' `_update_person_presence()`
    only ever calls `self.publish(...)` - never `luno.vision.ask_vision`/
    `_get_vision_provider` - verified here by asserting the vision
    provider singleton is never touched across a full enter/leave cycle."""
    import luno.vision as vision_module

    calls = []

    class _TripwireProvider:
        def analyze_image(self, image, prompt):
            calls.append((image, prompt))
            return "should never be called from presence detection"

    original = vision_module._vision_provider
    vision_module.set_vision_provider_for_testing(_TripwireProvider())
    try:
        clock = _Clock()
        adapter, _events = _adapter(clock, timeout_s=5.0)
        adapter.on_detections([{"label": "person"}])
        clock.advance(6.0)
        adapter.on_detections([])
        assert calls == []
    finally:
        vision_module.set_vision_provider_for_testing(original)


# ============================================================================
# status reporting
# ============================================================================

def test_extra_status_reports_debounced_person_present_flag():
    clock = _Clock()
    adapter, _events = _adapter(clock, timeout_s=5.0)
    assert adapter._extra_status()["person_present"] is False

    adapter.on_detections([{"label": "person"}])
    assert adapter._extra_status()["person_present"] is True

    clock.advance(6.0)
    adapter.on_detections([])
    assert adapter._extra_status()["person_present"] is False
