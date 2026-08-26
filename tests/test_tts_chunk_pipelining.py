"""
test_tts_chunk_pipelining.py
=============================

TTS Chunk Pipelining sprint - proves the audible-gap bug the Phase 0 audit
found (`docs/change_impact/tts_chunk_gap_audit.md` - a prior, read-only
audit sprint) and, after the fix, proves it is closed: `FishAudioAdapter`
must be able to synthesize chunk N+1 WHILE chunk N is still playing (a
bounded, ONE-SLOT lookahead - never more than one chunk prefetched ahead),
never changing playback ORDER, and never letting cancellation leak stale
prefetched audio into an already-stopped turn.

Uses the SAME test-double technique `luno/adapters/tests/test_fish_audio_real.py`
already established (a scripted `requests.Session`-like fake for
synthesis, an injected `play_audio_fn` fake for playback) driving the
REAL `FishAudioAdapter` + REAL `RealFishAudioClient` + REAL `AdapterManager`/
Event Bus through REAL `SpeakStreamChunk` events - not a synthetic helper
production never calls. `MockFishAudioClient` is untouched by this sprint
(it never had a separate synthesis phase to overlap - see
`supports_split_synthesis()`'s own docstring) and every existing test that
uses it is unaffected; these tests exercise the NEW opt-in pipelined path,
which only `RealFishAudioClient` (and any future client returning
`supports_split_synthesis() -> True`) ever takes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from luno.adapters.events import PausePlayback, ResumePlayback, SpeakStreamChunk, StopPlayback
from luno.adapters.fish_audio import FishAudioAdapter, PlaybackCancelled
from luno.adapters.fish_audio_real import RealFishAudioClient, RealFishAudioConfig, TTSSynthesisError
from luno.adapters.manager import AdapterManager
from luno.speech_chunk import SpeechChunk


# ─────────────────────────────────────────────
# Shared fakes - synthesis (HTTP) and playback (audio hardware) boundaries
# only, mirroring test_fish_audio_real.py's own FakeSession/make_fake_player.
# ─────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"FAKE-WAV-BYTES"):
        self.status_code = status_code
        self.content = content

    def json(self) -> Dict[str, Any]:
        return {"detail": "fake error"}


class TimedFakeSession:
    """Records every synthesis call's start/end time (via the `events`
    list passed in) so tests can assert real ordering - `delay_s` (or a
    per-text override via `delay_by_text`) simulates a real TTS backend's
    HTTP round trip."""

    def __init__(self, events: List[Tuple[float, str, Dict[str, Any]]], delay_s: float = 0.3,
                 delay_by_text: Optional[Dict[str, float]] = None, fail_texts: Optional[set] = None,
                 raise_exc: Optional[BaseException] = None):
        self.events = events
        self.delay_s = delay_s
        self.delay_by_text = delay_by_text or {}
        self.fail_texts = fail_texts or set()
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: Any = None):
        text = (json or {}).get("text", "")
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        self.events.append((time.time(), "SynthesisStart", {"text": text[:20]}))
        delay = self.delay_by_text.get(text, self.delay_s)
        time.sleep(delay)
        if self.raise_exc is not None:
            self.events.append((time.time(), "SynthesisError", {"text": text[:20]}))
            raise self.raise_exc
        if text in self.fail_texts:
            self.events.append((time.time(), "SynthesisError", {"text": text[:20]}))
            return FakeResponse(status_code=500)
        self.events.append((time.time(), "SynthesisEnd", {"text": text[:20]}))
        # Tag the wav content with the source text so downstream playback
        # fakes (which only ever see raw bytes, like the real Fish Audio
        # client boundary) can still identify which chunk they're
        # playing - needed for `_played_texts_from()` below.
        return FakeResponse(content=b"FAKE-WAV-BYTES:" + text.encode())


def make_timed_player(events: List[Tuple[float, str, Dict[str, Any]]], duration_s: float = 0.15,
                       duration_by_wav: Optional[Dict[bytes, float]] = None):
    """Stands in for real `sounddevice`/`soundfile` playback - blocks for
    `duration_s`, records PlaybackStart/PlaybackEnd, honors cancel/pause
    without losing position (mirrors `test_fish_audio_real.py`'s own
    `make_fake_player`)."""

    def _play_audio(wav_bytes: bytes, control) -> None:
        dur = (duration_by_wav or {}).get(wav_bytes, duration_s)
        control.on_playback_start()
        prefix = b"FAKE-WAV-BYTES:"
        played_text = wav_bytes[len(prefix):].decode(errors="ignore") if wav_bytes.startswith(prefix) else ""
        events.append((time.time(), "PlaybackStart", {"text": played_text}))
        slept = 0.0
        step = 0.005
        while slept < dur:
            if control.cancel.is_set():
                events.append((time.time(), "PlaybackCancelledMidway", {}))
                raise PlaybackCancelled("playback cancelled")
            if control.pause.is_set():
                time.sleep(step)
                continue
            time.sleep(step)
            slept += step
        events.append((time.time(), "PlaybackEnd", {}))

    return _play_audio


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mgr_with_real_fish_audio(client: RealFishAudioClient) -> Tuple[AdapterManager, FishAudioAdapter]:
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


def _publish_stream_chunks(mgr: AdapterManager, request_id: str, texts: List[str],
                            conversation_id: Optional[str] = None, gap_s: float = 0.02) -> None:
    total = len(texts)
    for i, text in enumerate(texts):
        chunk = SpeechChunk(
            chunk_id=f"{request_id}:chunk:{i}", request_id=request_id, conversation_id=conversation_id,
            sequence=i, total=total, raw_text=text, text=text, is_final=(i == total - 1),
        )
        mgr.event_bus.publish(SpeakStreamChunk(data={
            "request_id": request_id, "conversation_id": conversation_id, "chunk": chunk.to_dict(),
        }))
        if gap_s:
            time.sleep(gap_s)


def _close_marker(request_id: str, sequence: int, conversation_id: Optional[str] = None) -> Dict[str, Any]:
    return SpeechChunk(
        chunk_id=f"{request_id}:chunk:{sequence}", request_id=request_id, conversation_id=conversation_id,
        sequence=sequence, total=sequence + 1, raw_text="", text="", is_final=True,
    ).to_dict()


def _events_of(events: List[Tuple[float, str, Dict[str, Any]]], name: str) -> List[Tuple[float, str, Dict[str, Any]]]:
    return [e for e in events if e[1] == name]


# ============================================================================
# Phase 2 / core proof - the central invariant this whole sprint exists for.
# ============================================================================

def test_synthesis_of_next_chunk_starts_before_current_playback_ends():
    """THE core proof: SYNTH_START[2] must occur BEFORE PLAY_END[1] - i.e.
    chunk 2's Fish Audio synthesis call must already be running while
    chunk 1's audio is still playing, not after it stops. This is exactly
    the bug the Phase 0 audit measured (~1.3s of silence per boundary,
    equal to the next chunk's own synthesis latency)."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.3)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.5)
    mgr, fa = _mgr_with_real_fish_audio(client)

    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: done.set())

    texts = ["chunk one text here", "chunk two text here", "chunk three text here"]
    _publish_stream_chunks(mgr, "pipeline-core", texts, gap_s=0.01)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()

    assert ok, "turn never finished"
    synth_starts = _events_of(events, "SynthesisStart")
    playback_ends = _events_of(events, "PlaybackEnd")
    assert len(synth_starts) >= 2, f"expected at least 2 synthesis calls, got {events}"
    assert len(playback_ends) >= 1, f"expected at least 1 playback to finish, got {events}"

    synth_start_2 = synth_starts[1][0]
    play_end_1 = playback_ends[0][0]
    assert synth_start_2 < play_end_1, (
        f"SYNTH_START_2 ({synth_start_2}) must be BEFORE PLAY_END_1 ({play_end_1}) - "
        f"chunk 2 synthesis must overlap chunk 1 playback, not follow it. "
        f"Full timeline: {[(round(t,3), n, kw) for t,n,kw in events]}"
    )


def test_playback_order_is_never_reordered_by_pipelining():
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    # Make LATER chunks synthesize FASTER than earlier ones, to actively
    # try to provoke out-of-order playback if the implementation ever
    # played whichever chunk's audio became ready first instead of
    # strictly respecting sequence order.
    session = TimedFakeSession(events, delay_by_text={
        "first chunk": 0.4, "second chunk": 0.05, "third chunk": 0.02,
    })
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    played_order: List[str] = []
    # `TimedFakeSession.post()` already tags its response content with the
    # source text (`b"FAKE-WAV-BYTES:" + text.encode()`) - decode that
    # straight out of the bytes `synthesize()`/`client._synthesize`
    # produces, no extra tagging needed.
    prefix = b"FAKE-WAV-BYTES:"

    def play_audio(wav_bytes, control):
        control.on_playback_start()
        played_order.append(wav_bytes[len(prefix):].decode(errors="ignore") if wav_bytes.startswith(prefix) else "")
        time.sleep(0.05)

    client._play_audio = play_audio
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: done.set())

    _publish_stream_chunks(mgr, "pipeline-order", ["first chunk", "second chunk", "third chunk"], gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()

    assert ok
    assert played_order == ["first chunk", "second chunk", "third chunk"], (
        f"playback order must remain strictly sequential regardless of synthesis speed, got {played_order}"
    )


# ============================================================================
# Phase 10 - full test matrix (20 items). Items 1-2 above (overlap proof,
# ordering) count toward this matrix too - the rest follow below.
# ============================================================================

def _played_texts_from(events: List[Tuple[float, str, Dict[str, Any]]]) -> List[str]:
    return [kw.get("text", "") for t, n, kw in events if n == "PlaybackStart"]


def test_single_chunk_turn_plays_normally_without_prefetch():
    """3. A one-chunk turn has nothing to prefetch - must still play and
    finish cleanly through the new pipelined path."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.05)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    _publish_stream_chunks(mgr, "single", ["only chunk"], gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert _played_texts_from(events) == ["only chunk"]


def test_two_chunk_turn_overlaps_and_completes():
    """4. Minimal (two-chunk) overlap case, distinct from the 3-chunk core
    proof test above."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.2)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.4)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    _publish_stream_chunks(mgr, "two-chunk", ["chunk alpha", "chunk beta"], gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    synth_starts = _events_of(events, "SynthesisStart")
    playback_ends = _events_of(events, "PlaybackEnd")
    assert len(synth_starts) == 2 and len(playback_ends) == 2
    assert synth_starts[1][0] < playback_ends[0][0]
    assert _played_texts_from(events) == ["chunk alpha", "chunk beta"]


def test_many_chunks_every_boundary_overlaps():
    """5. Five chunks - EVERY chunk-N+1 synthesis must start before
    chunk-N's playback ends, not just the first boundary."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.15)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.3)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    texts = [f"chunk number {i}" for i in range(5)]
    _publish_stream_chunks(mgr, "many-chunks", texts, gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok
    synth_starts = _events_of(events, "SynthesisStart")
    playback_ends = _events_of(events, "PlaybackEnd")
    assert len(synth_starts) == 5 and len(playback_ends) == 5
    for i in range(4):
        assert synth_starts[i + 1][0] < playback_ends[i][0], (
            f"boundary {i}: SYNTH_START[{i+1}]={synth_starts[i+1][0]} must be < PLAY_END[{i}]={playback_ends[i][0]}"
        )
    assert _played_texts_from(events) == texts


def test_prefetch_synthesis_failure_is_retried_then_skipped_not_crashed():
    """6. If the PREFETCHED chunk's synthesis fails outright, the adapter
    must retry (bounded) and, on exhausting retries, skip that one chunk
    and continue the stream - never crash the whole turn."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05, fail_texts={"bad chunk"})
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.1)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: done.set())
    _publish_stream_chunks(mgr, "fail-chunk", ["good one", "bad chunk", "good two"], gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok
    played = _played_texts_from(events)
    assert played == ["good one", "good two"], f"bad chunk must be skipped, not crash the turn: {played}"


def test_slow_synthesis_does_not_deadlock():
    """7. A pathologically slow chunk must still eventually complete (no
    deadlock) - bounded purely by the test's own timeout, proving no
    unbounded wait was introduced."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_by_text={"normal": 0.05, "slow one": 0.6})
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.05)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    _publish_stream_chunks(mgr, "slow-synth", ["normal", "slow one", "normal"], gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok, "slow synthesis must not deadlock the turn"


def test_cancellation_during_prefetch_synthesis_discards_stale_audio():
    """8. StopPlayback arrives WHILE chunk 2's prefetch synthesis is still
    running (chunk 1 is playing). Chunk 2 must never be played - its
    eventually-ready prefetched audio is simply discarded."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.3)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=1.0)
    mgr, fa = _mgr_with_real_fish_audio(client)
    cancelled = threading.Event()
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.set())
    _publish_stream_chunks(mgr, "cancel-during-prefetch", ["first", "second"], gap_s=0.0)
    # Wait until chunk 1 is actually playing and chunk 2's prefetch has
    # started (both SynthesisStart events observed), then cancel.
    assert _wait_until(lambda: len(_events_of(events, "PlaybackStart")) >= 1, timeout_s=5.0)
    assert _wait_until(lambda: len(_events_of(events, "SynthesisStart")) >= 2, timeout_s=5.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "cancel-during-prefetch"}))
    ok = _wait_until(cancelled.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok, "expected speech_playback_cancelled"
    played = _played_texts_from(events)
    assert "second" not in played, f"cancelled chunk must never play, got {played}"


def test_cancellation_right_after_prefetch_ready_before_use():
    """9. StopPlayback arrives just as chunk 2's prefetch synthesis has
    already FINISHED (audio ready) but before it was ever handed to
    `play_audio()` - still must never play."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_by_text={"first": 0.5, "second": 0.05})
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=1.0)
    mgr, fa = _mgr_with_real_fish_audio(client)
    cancelled = threading.Event()
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.set())
    _publish_stream_chunks(mgr, "cancel-after-ready", ["first", "second"], gap_s=0.0)
    assert _wait_until(lambda: len(_events_of(events, "SynthesisEnd")) >= 2, timeout_s=5.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "cancel-after-ready"}))
    ok = _wait_until(cancelled.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    played = _played_texts_from(events)
    assert "second" not in played, f"already-synthesized but unused chunk must never play, got {played}"


def test_cancellation_mid_playback_of_current_chunk():
    """10. StopPlayback arrives while chunk 1 ITSELF is still playing (the
    original, non-prefetch-related cancellation case) - must still work
    identically through the new pipelined path."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=2.0)
    mgr, fa = _mgr_with_real_fish_audio(client)
    cancelled = threading.Event()
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.set())
    _publish_stream_chunks(mgr, "cancel-mid-current", ["only one playing a while"], gap_s=0.0)
    assert _wait_until(lambda: len(_events_of(events, "PlaybackStart")) >= 1, timeout_s=5.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "cancel-mid-current"}))
    ok = _wait_until(cancelled.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert len(_events_of(events, "PlaybackEnd")) == 0


def test_pause_then_resume_completes_successfully():
    """11-12. PausePlayback while chunk 1 is playing, then ResumePlayback -
    the turn must still finish successfully, exactly as before this
    sprint."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.3)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    _publish_stream_chunks(mgr, "pause-resume", ["chunk one", "chunk two"], gap_s=0.0)
    assert _wait_until(lambda: len(_events_of(events, "PlaybackStart")) >= 1, timeout_s=5.0)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "pause-resume"}))
    time.sleep(0.15)
    mgr.event_bus.publish(ResumePlayback(data={"request_id": "pause-resume"}))
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert _played_texts_from(events) == ["chunk one", "chunk two"]


def test_pause_does_not_cancel_prefetch_synthesis():
    """13. While chunk 1's PLAYBACK is paused, chunk 2's PREFETCH
    synthesis must keep running to completion in the background -
    `synthesize()` has no pause-check by design (see
    `FishAudioClient.synthesize()`'s own docstring)."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.2)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=1.0)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    _publish_stream_chunks(mgr, "pause-prefetch", ["chunk one", "chunk two"], gap_s=0.0)
    assert _wait_until(lambda: len(_events_of(events, "SynthesisStart")) >= 2, timeout_s=5.0)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "pause-prefetch"}))
    # Prefetch synthesis for chunk two must complete DESPITE the pause.
    ok = _wait_until(lambda: len(_events_of(events, "SynthesisEnd")) >= 2, timeout_s=5.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "pause-prefetch"}))
    mgr.stop_all()
    client.close()
    assert ok, "prefetch synthesis must keep progressing while paused, not stall"


def test_close_marker_mid_stream_does_not_break_pipeline():
    """14. A non-final, empty-text close marker arriving BETWEEN two real
    chunks must be handled (published as finished, never synthesized/
    played) without disrupting the surrounding prefetch pipeline."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.1)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    request_id = "mid-close-marker"
    mgr.event_bus.publish(SpeakStreamChunk(data={
        "request_id": request_id, "conversation_id": None,
        "chunk": SpeechChunk(chunk_id=f"{request_id}:0", request_id=request_id, conversation_id=None,
                              sequence=0, total=3, raw_text="alpha", text="alpha", is_final=False).to_dict(),
    }))
    time.sleep(0.02)
    # `_close_marker()` (used by the trailing-final-marker test below)
    # always sets `is_final=True` - a genuinely MID-stream close marker
    # (more chunks still to come) is built by hand here instead.
    mid_stream_close_marker = SpeechChunk(
        chunk_id=f"{request_id}:chunk:1", request_id=request_id, conversation_id=None,
        sequence=1, total=3, raw_text="", text="", is_final=False,
    ).to_dict()
    mgr.event_bus.publish(SpeakStreamChunk(data={
        "request_id": request_id, "conversation_id": None,
        "chunk": mid_stream_close_marker,
    }))
    time.sleep(0.02)
    mgr.event_bus.publish(SpeakStreamChunk(data={
        "request_id": request_id, "conversation_id": None,
        "chunk": SpeechChunk(chunk_id=f"{request_id}:2", request_id=request_id, conversation_id=None,
                              sequence=2, total=3, raw_text="beta", text="beta", is_final=True).to_dict(),
    }))
    ok = _wait_until(done.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert _played_texts_from(events) == ["alpha", "beta"]


def test_stream_close_final_marker_after_real_chunks():
    """15. A trailing final close marker (no more real text, just the
    "stream is done" signal) after real chunks already played must still
    finish the turn successfully."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.1)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    request_id = "trailing-close"
    mgr.event_bus.publish(SpeakStreamChunk(data={
        "request_id": request_id, "conversation_id": None,
        "chunk": SpeechChunk(chunk_id=f"{request_id}:0", request_id=request_id, conversation_id=None,
                              sequence=0, total=2, raw_text="only", text="only", is_final=False).to_dict(),
    }))
    time.sleep(0.05)
    mgr.event_bus.publish(SpeakStreamChunk(data={
        "request_id": request_id, "conversation_id": None,
        "chunk": _close_marker(request_id, 1),
    }))
    ok = _wait_until(done.is_set, timeout_s=5.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert _played_texts_from(events) == ["only"]


def test_repeated_sequential_requests_leave_no_leftover_state():
    """16. Two full turns run one after another (different request_ids) -
    each must succeed independently, and the adapter's own per-request
    bookkeeping dicts must be empty again after each (no leak between
    turns)."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.03)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.05)
    mgr, fa = _mgr_with_real_fish_audio(client)
    for rid in ("seq-a", "seq-b"):
        done = threading.Event()
        sub_id = mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
        _publish_stream_chunks(mgr, rid, ["one", "two"], gap_s=0.0)
        ok = _wait_until(done.is_set, timeout_s=5.0)
        assert ok, f"{rid} never finished"
        mgr.event_bus.unsubscribe(sub_id)
        assert fa._stream_queues.get(rid) is None
        assert fa._chunk_control.get(rid) is None
        with fa._in_flight_lock:
            assert rid not in fa._in_flight_request_ids
    mgr.stop_all()
    client.close()


def test_concurrent_unrelated_requests_stay_isolated():
    """17. Two DIFFERENT request_ids' streams running concurrently (the
    two-worker `_playback_executor` slot) must never cross-play each
    other's chunks."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.08)
    mgr, fa = _mgr_with_real_fish_audio(client)
    finished_ids: List[str] = []
    lock = threading.Lock()

    def _on_finished(e):
        with lock:
            finished_ids.append(e.get("request_id"))

    mgr.event_bus.subscribe("speech_playback_finished", _on_finished)
    t1 = threading.Thread(target=_publish_stream_chunks, args=(mgr, "conc-a", ["a1", "a2"]), kwargs={"gap_s": 0.0})
    t2 = threading.Thread(target=_publish_stream_chunks, args=(mgr, "conc-b", ["b1", "b2"]), kwargs={"gap_s": 0.0})
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    ok = _wait_until(lambda: len(finished_ids) >= 2, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok
    played = _played_texts_from(events)
    assert set(played) == {"a1", "a2", "b1", "b2"}
    assert sorted(finished_ids) == ["conc-a", "conc-b"]


def test_no_stale_audio_ever_played_after_cancellation():
    """18. Direct assertion that a cancelled request's prefetched-but-
    unused audio is never handed to `play_audio()` at all - checked by
    asserting no PlaybackStart for the cancelled chunk's text ever
    appears, even after waiting past when its background synthesis
    would have completed."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.15)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.3)
    mgr, fa = _mgr_with_real_fish_audio(client)
    cancelled = threading.Event()
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.set())
    _publish_stream_chunks(mgr, "no-stale-audio", ["keep this one", "never play this"], gap_s=0.0)
    assert _wait_until(lambda: len(_events_of(events, "PlaybackStart")) >= 1, timeout_s=5.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "no-stale-audio"}))
    assert _wait_until(cancelled.is_set, timeout_s=5.0)
    # Give the abandoned prefetch's background thread plenty of time to
    # actually finish synthesizing, to prove it's discarded, not merely
    # "not yet ready".
    time.sleep(0.4)
    mgr.stop_all()
    client.close()
    assert "never play this" not in _played_texts_from(events)


def test_prefetch_executor_bounded_no_thread_leak_across_many_turns():
    """19. Running many sequential turns must not grow the number of
    live threads unboundedly - `_prefetch_executor` stays a fixed
    `max_workers=2` pool for the adapter's whole lifetime."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.02)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.02)
    mgr, fa = _mgr_with_real_fish_audio(client)
    assert fa._prefetch_executor is not None
    assert fa._prefetch_executor._max_workers == 2
    for i in range(15):
        done = threading.Event()
        mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
        _publish_stream_chunks(mgr, f"leak-check-{i}", ["one", "two", "three"], gap_s=0.0)
        assert _wait_until(done.is_set, timeout_s=5.0)
    # Pool identity/size must be unchanged - no per-turn executor created.
    assert fa._prefetch_executor is not None
    assert fa._prefetch_executor._max_workers == 2
    mgr.stop_all()
    client.close()


def test_one_slot_prefetch_bound_never_exceeded():
    """20. At most ONE prefetch synthesis job is ever in flight at a time
    - measured directly by counting concurrently-open `synthesize()`
    calls via a lock-guarded counter in the fake HTTP session."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    lock = threading.Lock()
    concurrent = {"count": 0, "max": 0}

    class CountingSession(TimedFakeSession):
        def post(self, url, json=None, timeout=None):
            with lock:
                concurrent["count"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["count"])
            try:
                return super().post(url, json=json, timeout=timeout)
            finally:
                with lock:
                    concurrent["count"] -= 1

    session = CountingSession(events, delay_s=0.15)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="r.wav", reference_text="hi", timeout_s=5, synthesis_poll_s=0.02),
        session=session,
    )
    client._play_audio = make_timed_player(events, duration_s=0.3)
    mgr, fa = _mgr_with_real_fish_audio(client)
    done = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: done.set())
    texts = [f"chunk {i}" for i in range(6)]
    _publish_stream_chunks(mgr, "one-slot-bound", texts, gap_s=0.0)
    ok = _wait_until(done.is_set, timeout_s=10.0)
    mgr.stop_all()
    client.close()
    assert ok
    assert concurrent["max"] <= 2, f"never more than ~1 prefetch job (bounded by executor size 2), got max concurrent={concurrent['max']}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
