"""
test_fish_audio_chunking.py
=============================

TTS Chunking/Streaming sprint - `FishAudioAdapter._play()`'s new
sequential multi-chunk playback (see that module's own updated
docstring). Isolated from `test_fish_audio_barge_in.py` (pre-existing
single-block pause/resume/stop contract, unchanged - re-run there to
prove this sprint didn't regress it) and `test_fish_audio_real.py`
(RealFishAudioClient-specific regressions, also unchanged). This file
covers only what's NEW: chunk ordering, the "gap between chunks" race
`StopPlayback`/`PausePlayback` must close, per-chunk retry/skip on
failure, and that a chunk-less `SpeakRequest` still behaves exactly like
before this sprint (backward compatibility).

No network, no audio hardware - `MockFishAudioClient` (unmodified,
pre-existing) covers ordering/gap/stop/pause scenarios; a small local
`ScriptedFishAudioClient` (mirrors `MockFishAudioClient`'s own per-call
`_active`-entry pattern) covers scripted per-chunk failures, since
`MockFishAudioClient` has no per-call failure scripting.

Modern pytest-`assert` style (matches `test_fish_audio_api.py`'s own
precedent in this same directory - NOT the older Result-tuple runner
`test_fish_audio_barge_in.py`/`test_fish_audio_real.py` use, which
pytest collects but never actually asserts on the returned `(ok, detail)`
tuple - a pre-existing gap, out of this sprint's scope, documented in
docs/change_impact/tts_chunking_streaming.md rather than fixed here).

Run:
    python3 -m pytest luno/adapters/tests/test_fish_audio_chunking.py
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from luno.adapters.events import PausePlayback, ResumePlayback, SpeakRequest, StopPlayback
from luno.adapters.fish_audio import FishAudioAdapter, FishAudioClient, MockFishAudioClient, PlaybackCancelled
from luno.adapters.manager import AdapterManager


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mgr_with_fish_audio(client=None) -> Tuple[AdapterManager, FishAudioAdapter]:
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client or MockFishAudioClient(playback_delay_s=0.05))
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


class ScriptedFishAudioClient(FishAudioClient):
    """Like `MockFishAudioClient`, but a given chunk TEXT can be scripted
    to fail N times before succeeding (or fail forever) - lets a test
    exercise `FishAudioAdapter._play()`'s bounded per-chunk retry/skip
    behavior deterministically. Per-call `_active` entry tracking mirrors
    `MockFishAudioClient`'s own documented reasoning exactly (the
    2-worker concurrent-interjection case)."""

    def __init__(
        self,
        fail_times: Optional[Dict[str, int]] = None,
        playback_delay_s: float = 0.02,
        synthesis_delay_s: float = 0.0,
    ) -> None:
        self.played: List[str] = []
        self._remaining_failures: Dict[str, int] = dict(fail_times or {})
        self.playback_delay_s = playback_delay_s
        self.synthesis_delay_s = synthesis_delay_s
        self._active: List[Dict[str, threading.Event]] = []
        self._lock = threading.Lock()

    def play(self, text: str, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        entry = {"cancel": threading.Event(), "pause": threading.Event()}
        with self._lock:
            self._active.append(entry)
        try:
            if self.synthesis_delay_s > 0:
                slept = 0.0
                step = 0.01
                while slept < self.synthesis_delay_s:
                    if entry["cancel"].is_set():
                        raise PlaybackCancelled("cancelled during synthesis")
                    time.sleep(min(step, self.synthesis_delay_s - slept))
                    slept += step
            remaining = self._remaining_failures.get(text, 0)
            if remaining != 0:
                if remaining > 0:
                    self._remaining_failures[text] = remaining - 1
                raise RuntimeError(f"scripted failure for chunk: {text!r}")
            self.played.append(text)
            if on_playback_start is not None:
                on_playback_start()
            slept = 0.0
            step = 0.01
            while slept < self.playback_delay_s:
                if entry["cancel"].is_set():
                    raise PlaybackCancelled("cancelled during playback")
                if entry["pause"].is_set():
                    time.sleep(step)
                    continue
                time.sleep(min(step, self.playback_delay_s - slept))
                slept += step
        finally:
            with self._lock:
                if entry in self._active:
                    self._active.remove(entry)

    def stop(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["cancel"].set()
                entry["pause"].clear()

    def pause(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["pause"].set()

    def resume(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["pause"].clear()


# ============================================================================
# Ordering / backward compatibility
# ============================================================================

def test_multi_chunk_plays_all_chunks_in_order_no_duplicates():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    started, finished = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    chunks = ["Chunk satu.", "Chunk dua.", "Chunk tiga."]
    mgr.event_bus.publish(SpeakRequest(data={
        "text": " ".join(chunks), "request_id": "order-1", "chunks": chunks,
    }))
    ok = _wait_until(lambda: finished == ["order-1"], timeout_s=3.0)
    mgr.stop_all()

    assert ok, f"started={started} finished={finished} played={client.played}"
    assert started == ["order-1"], "SpeechPlaybackStarted must fire exactly once, on the FIRST chunk only"
    assert client.played == chunks, f"chunks must play in exact order, no duplicates: {client.played}"


def test_chunks_absent_falls_back_to_single_legacy_call():
    """No `chunks` field at all (every pre-chunking-sprint caller) -
    byte-identical to the old single-block behavior: exactly one
    `client.play()` call with the full `text`."""
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "legacy single block", "request_id": "legacy-1"}))
    ok = _wait_until(lambda: finished == ["legacy-1"])
    mgr.stop_all()

    assert ok
    assert client.played == ["legacy single block"]


def test_single_item_chunks_list_behaves_like_legacy_single_call():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={
        "text": "one chunk only", "request_id": "single-chunk-1", "chunks": ["one chunk only"],
    }))
    ok = _wait_until(lambda: finished == ["single-chunk-1"])
    mgr.stop_all()

    assert ok
    assert client.played == ["one chunk only"]


def test_empty_chunks_list_falls_back_to_text():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "fallback text", "request_id": "empty-chunks-1", "chunks": []}))
    ok = _wait_until(lambda: finished == ["empty-chunks-1"])
    mgr.stop_all()

    assert ok
    assert client.played == ["fallback text"]


# ============================================================================
# Stop / interrupt across chunk boundaries (Phase 6)
# ============================================================================

def test_stop_while_first_chunk_playing_never_plays_later_chunks():
    client = MockFishAudioClient(playback_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled, finished = [], [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    chunks = ["First chunk, long enough to interrupt.", "Second chunk.", "Third chunk."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "stop-1", "chunks": chunks}))
    ok1 = _wait_until(lambda: started == ["stop-1"])
    mgr.event_bus.publish(StopPlayback(data={"request_id": "stop-1"}))
    ok2 = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    time.sleep(0.2)  # give a wrongly-not-stopped loop time to (incorrectly) keep going
    mgr.stop_all()

    assert ok1 and ok2, f"started={started} cancelled={cancelled}"
    assert finished == [], "must never publish SpeechPlaybackFinished for a stopped turn"
    assert client.played == [chunks[0]], f"only the chunk that was actually playing may have started: {client.played}"
    assert cancelled[0].get("chunk_index") == 0


def test_stop_in_gap_between_chunks_prevents_next_chunk_from_starting():
    """Closes the exact race this sprint's own Phase 6 exists for: a
    `StopPlayback` that lands AFTER one chunk finishes but BEFORE the
    next chunk's `client.play()` call begins must still prevent that next
    chunk from ever playing - not just a stop that lands while a chunk is
    actively mid-playback (covered above)."""
    client = MockFishAudioClient(playback_delay_s=0.03, synthesis_delay_s=0.05)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    chunks = ["Alpha.", "Beta.", "Gamma."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "gap-stop-1", "chunks": chunks}))
    ok1 = _wait_until(lambda: client.played == ["Alpha."], timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "gap-stop-1"}))
    ok2 = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    time.sleep(0.3)  # well past chunk 2/3's own synthesis+playback budget
    mgr.stop_all()

    assert ok1 and ok2, f"played={client.played} cancelled={cancelled}"
    assert client.played == ["Alpha."], f"Beta/Gamma must never play after a gap-stop: {client.played}"


def test_pause_in_gap_between_chunks_holds_until_resumed():
    client = MockFishAudioClient(playback_delay_s=0.03, synthesis_delay_s=0.05)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    chunks = ["Satu.", "Dua."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "gap-pause-1", "chunks": chunks}))
    ok1 = _wait_until(lambda: client.played == ["Satu."], timeout_s=2.0)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "gap-pause-1"}))
    time.sleep(0.3)  # well past what chunk 2 would need if NOT held
    held = client.played == ["Satu."]
    mgr.event_bus.publish(ResumePlayback(data={"request_id": "gap-pause-1"}))
    ok2 = _wait_until(lambda: finished == ["gap-pause-1"], timeout_s=2.0)
    mgr.stop_all()

    assert ok1, f"played={client.played}"
    assert held, f"chunk 2 must not start while paused in the gap: played={client.played}"
    assert ok2, f"played={client.played} finished={finished}"
    assert client.played == chunks


def test_new_turn_after_stop_is_never_affected_by_previous_turns_stop_flag():
    """Each `_play()` call gets its OWN control entry (see module
    docstring) - a brand-new, unrelated request_id must play normally
    even though a DIFFERENT, previous turn was just stopped. Guards
    against exactly the "stale global stop flag silences the next turn"
    bug class a naive implementation could introduce."""
    client = MockFishAudioClient(playback_delay_s=0.2)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled, finished = [], []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={
        "text": "old turn", "request_id": "old-turn", "chunks": ["old turn"],
    }))
    ok1 = _wait_until(lambda: "old turn" in client.played, timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "old-turn"}))
    ok2 = _wait_until(lambda: cancelled == ["old-turn"], timeout_s=2.0)

    new_chunks = ["New turn chunk one.", "New turn chunk two."]
    mgr.event_bus.publish(SpeakRequest(data={
        "text": " ".join(new_chunks), "request_id": "new-turn", "chunks": new_chunks,
    }))
    ok3 = _wait_until(lambda: finished == ["new-turn"], timeout_s=2.0)
    mgr.stop_all()

    assert ok1 and ok2 and ok3, f"cancelled={cancelled} finished={finished} played={client.played}"
    assert client.played == ["old turn"] + new_chunks


# ============================================================================
# Per-chunk failure / retry / skip (Phase 7)
# ============================================================================

def test_middle_chunk_fails_once_then_succeeds_on_retry_no_duplicate_no_reorder():
    client = ScriptedFishAudioClient(fail_times={"Beta.": 1}, playback_delay_s=0.01)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    chunks = ["Alpha.", "Beta.", "Gamma."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "retry-1", "chunks": chunks}))
    ok = _wait_until(lambda: finished == ["retry-1"], timeout_s=2.0)
    mgr.stop_all()

    assert ok, f"played={client.played}"
    assert client.played == chunks, f"retry must recover the SAME chunk, in order, exactly once each: {client.played}"


def test_middle_chunk_fails_permanently_is_skipped_not_fatal():
    client = ScriptedFishAudioClient(fail_times={"Beta.": -1}, playback_delay_s=0.01)  # -1 = always fails
    mgr, fa = _mgr_with_fish_audio(client)
    finished, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    chunks = ["Alpha.", "Beta.", "Gamma."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "skip-1", "chunks": chunks}))
    ok = _wait_until(lambda: finished == ["skip-1"], timeout_s=2.0)
    mgr.stop_all()

    assert ok, f"played={client.played} cancelled={cancelled}"
    assert cancelled == [], "a single permanently-failing chunk must not abort the whole turn"
    assert client.played == ["Alpha.", "Gamma."], f"the failed chunk is skipped, order preserved, no duplicate: {client.played}"


def test_all_chunks_fail_publishes_cancelled_with_aggregate_error():
    client = ScriptedFishAudioClient(fail_times={"Alpha.": -1, "Beta.": -1}, playback_delay_s=0.01)
    mgr, fa = _mgr_with_fish_audio(client)
    finished, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    chunks = ["Alpha.", "Beta."]
    mgr.event_bus.publish(SpeakRequest(data={"text": " ".join(chunks), "request_id": "all-fail-1", "chunks": chunks}))
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    mgr.stop_all()

    assert ok, f"played={client.played} cancelled={cancelled}"
    assert finished == []
    assert client.played == []
    assert "error" in cancelled[0]


def test_single_block_failure_has_no_retry_and_preserves_original_error_message():
    """Backward-compat guard (see fish_audio.py's own comment at this
    exact branch): the legacy `total == 1` path must fail IMMEDIATELY -
    no retry - and publish the ORIGINAL exception's own message, exactly
    like every pre-chunking-sprint test already expects
    (`test_fish_audio_real.py::test_synthesis_http_error_publishes_error_without_started`
    and friends)."""
    client = ScriptedFishAudioClient(fail_times={"will fail": -1}, playback_delay_s=0.01)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    mgr.event_bus.publish(SpeakRequest(data={"text": "will fail", "request_id": "legacy-fail-1"}))
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    mgr.stop_all()

    assert ok
    assert cancelled[0].get("error") == "scripted failure for chunk: 'will fail'"
    assert client.played == []
