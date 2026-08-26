"""
test_fish_audio_barge_in.py
=============================

Sprint 3 additions to the Adapter Layer, isolated from the rest of
`test_adapters.py` (Task A/Sprint 1 coverage) so the two suites stay
independently runnable: `FishAudioAdapter`'s pause/resume/stop control
plane, the new `SpeakRequest` playback trigger (distinct from
`AssistantResponse`), and - the scenario that exposed a real bug during
development - a SECOND utterance (e.g. a barge-in CONFIRM prompt)
starting to play while a FIRST one is merely paused, not stopped, which
only works if `MockFishAudioClient` tracks cancel/pause state PER
CALL rather than sharing one pair of flags across the whole client
instance. No network, no real Fish Audio server.

Run:
    python3 -m luno.adapters.tests.test_fish_audio_barge_in
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.adapters.events import (  # noqa: E402
    AssistantResponse, PausePlayback, ResumePlayback, SpeakRequest, StopPlayback,
)
from luno.adapters.fish_audio import FishAudioAdapter, MockFishAudioClient, PlaybackCancelled  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402

Result = Tuple[bool, str]


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mgr_with_fish_audio(client=None) -> Tuple[AdapterManager, FishAudioAdapter]:
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client or MockFishAudioClient(playback_delay_s=0.3))
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


# ============================================================================
# SpeakRequest - the new, separate playback trigger
# ============================================================================

def test_speak_request_triggers_playback_same_as_assistant_response() -> Result:
    mgr, fa = _mgr_with_fish_audio(MockFishAudioClient(playback_delay_s=0.05))
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    mgr.event_bus.publish(SpeakRequest(data={"text": "hello from speak_request", "request_id": "sr-1"}))
    ok = _wait_until(lambda: finished == ["sr-1"])
    mgr.stop_all()
    return ok, f"finished={finished}"


def test_assistant_response_still_works_unmodified() -> Result:
    """Sprint 3's addition must be purely additive - AssistantResponse
    alone (no SpeakRequest at all) still triggers playback exactly like
    Task A's own suite expects."""
    mgr, fa = _mgr_with_fish_audio(MockFishAudioClient(playback_delay_s=0.05))
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    mgr.event_bus.publish(AssistantResponse(data={"text": "hello", "request_id": "ar-1"}))
    ok = _wait_until(lambda: finished == ["ar-1"])
    mgr.stop_all()
    return ok, f"finished={finished}"


# ============================================================================
# Pause / resume
# ============================================================================

def test_pause_then_resume_lets_playback_finish_without_restarting() -> Result:
    client = MockFishAudioClient(playback_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, paused, resumed, finished = [], [], [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_paused", lambda e: paused.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_resumed", lambda e: resumed.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "a slow sentence", "request_id": "p-1"}))
    ok1 = _wait_until(lambda: started == ["p-1"])
    mgr.event_bus.publish(PausePlayback(data={"request_id": "p-1"}))
    ok2 = _wait_until(lambda: paused == ["p-1"])
    # while genuinely paused, playback must not silently finish on its own
    time.sleep(0.4)
    ok3 = finished == []
    mgr.event_bus.publish(ResumePlayback(data={"request_id": "p-1"}))
    ok4 = _wait_until(lambda: resumed == ["p-1"])
    ok5 = _wait_until(lambda: finished == ["p-1"], timeout_s=2.0)
    mgr.stop_all()
    ok = ok1 and ok2 and ok3 and ok4 and ok5
    return ok, f"started={started} paused={paused} resumed={resumed} finished={finished}"


def test_stop_during_pause_still_cancels() -> Result:
    client = MockFishAudioClient(playback_delay_s=0.5)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: None)
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "slow", "request_id": "sp-1"}))
    time.sleep(0.05)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "sp-1"}))
    time.sleep(0.05)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "sp-1"}))
    ok = _wait_until(lambda: cancelled == ["sp-1"], timeout_s=2.0)
    mgr.stop_all()
    return ok, f"cancelled={cancelled}"


# ============================================================================
# The bug this file was written to catch: a second (interjecting) call
# to play() while the first is merely paused must not corrupt either.
# ============================================================================

def test_second_utterance_while_first_is_paused_does_not_corrupt_either() -> Result:
    client = MockFishAudioClient(playback_delay_s=0.6)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled, finished = [], [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    # first utterance starts, then gets paused (like a CONFIRM interrupt)...
    mgr.event_bus.publish(SpeakRequest(data={"text": "first, long utterance", "request_id": "first"}))
    ok1 = _wait_until(lambda: "first" in started)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "first"}))
    time.sleep(0.05)

    # ...while it's paused, a SECOND, short utterance interjects (the
    # confirm prompt) - this used to clear the FIRST call's shared
    # pause/cancel flags the instant its own play() call started.
    mgr.event_bus.publish(SpeakRequest(data={"text": "confirm prompt", "request_id": "second"}))
    ok2 = _wait_until(lambda: "second" in started)
    ok3 = _wait_until(lambda: "second" in finished, timeout_s=2.0)

    # the FIRST call must still be genuinely paused right now - it must
    # NOT have silently finished (which is what the un-fixed shared-state
    # bug caused: the second call's play() clearing the first's pause flag).
    ok4 = "first" not in finished and "first" not in cancelled

    # now actually cancel the first one - it must still be cancellable.
    mgr.event_bus.publish(StopPlayback(data={"request_id": "first"}))
    ok5 = _wait_until(lambda: "first" in cancelled, timeout_s=2.0)

    mgr.stop_all()
    ok = ok1 and ok2 and ok3 and ok4 and ok5
    return ok, f"started={started} cancelled={cancelled} finished={finished}"


def test_concurrent_pause_resume_stop_stress_no_crash() -> Result:
    client = MockFishAudioClient(playback_delay_s=0.2)
    mgr, fa = _mgr_with_fish_audio(client)
    errors = []

    def _hammer(i: int) -> None:
        try:
            rid = f"stress-{i}"
            mgr.event_bus.publish(SpeakRequest(data={"text": "x", "request_id": rid}))
            for ev_type in (PausePlayback, ResumePlayback, PausePlayback, StopPlayback):
                mgr.event_bus.publish(ev_type(data={"request_id": rid}))
        except BaseException as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)
    time.sleep(0.5)
    stats = mgr.event_bus.stats()
    mgr.stop_all()
    ok = not errors and stats["dropped"] == 0
    return ok, f"errors={errors} dropped={stats['dropped']}"


def test_playback_failure_still_publishes_cancelled_with_error() -> Result:
    client = MockFishAudioClient(playback_delay_s=0.01, fail=True)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))
    mgr.event_bus.publish(SpeakRequest(data={"text": "boom", "request_id": "fail-1"}))
    ok = _wait_until(lambda: len(cancelled) == 1)
    mgr.stop_all()
    has_error = bool(cancelled) and "error" in cancelled[0]
    return ok and has_error, f"cancelled={cancelled}"


def test_control_events_dropped_gracefully_before_start() -> Result:
    """PausePlayback/ResumePlayback/StopPlayback arriving for an adapter
    that was never started must not raise - `handle_event` short-circuits
    cleanly (mirrors the existing 'not started' guard for AssistantResponse/
    SpeakRequest)."""
    fa = FishAudioAdapter(client=MockFishAudioClient())
    try:
        fa.handle_event(SpeakRequest(data={"text": "x", "request_id": "never-started"}))
        fa.handle_event(PausePlayback(data={"request_id": "never-started"}))
        fa.handle_event(ResumePlayback(data={"request_id": "never-started"}))
        fa.handle_event(StopPlayback(data={"request_id": "never-started"}))
        ok = True
        detail = "no exception raised"
    except Exception as ex:  # pragma: no cover
        ok = False
        detail = f"raised {type(ex).__name__}: {ex}"
    return ok, detail


# ============================================================================
# Runner
# ============================================================================

SCENARIOS = [
    test_speak_request_triggers_playback_same_as_assistant_response,
    test_assistant_response_still_works_unmodified,
    test_pause_then_resume_lets_playback_finish_without_restarting,
    test_stop_during_pause_still_cancels,
    test_second_utterance_while_first_is_paused_does_not_corrupt_either,
    test_concurrent_pause_resume_stop_stress_no_crash,
    test_playback_failure_still_publishes_cancelled_with_error,
    test_control_events_dropped_gracefully_before_start,
]


def main() -> int:
    print("\n=== Fish Audio / Barge-In Adapter Suite ===")
    results = []
    for fn in SCENARIOS:
        name = fn.__name__
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"raised {type(ex).__name__}: {ex}"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} - {detail}")
        results.append((name, ok))

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{total} scenarios passed.")
    if passed != total:
        print("Failing scenarios:")
        for name, ok in results:
            if not ok:
                print(f"  - {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
