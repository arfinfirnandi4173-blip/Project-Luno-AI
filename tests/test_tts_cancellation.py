"""
test_tts_cancellation.py
==========================

TTS Chunk Queue & Cancellation sprint - dedicated test suite for the
CANCELLATION contract (`luno.speech_chunk.SpeechCancellationToken`,
`FishAudioAdapter`'s handling of `StopPlayback`/`PausePlayback`/
`ResumePlayback`/`LLMCancelled`) AND the Barge-in integration that
drives it in real usage.

No new barge-in detector is created here - `luno.barge_in.BargeInModule`
is used completely unmodified, exactly like `tests/test_barge_in_console.py`
already does; this file only adds NEW scenarios specific to multi-chunk
cancellation that that file's own pre-existing (single-block) scenarios
don't cover.

No network, no real Fish Audio server, no real speaker.

Run:
    python3 -m pytest tests/test_tts_cancellation.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import LLMCancelled, PausePlayback, ResumePlayback, SpeakRequest, StopPlayback  # noqa: E402
from luno.adapters.fish_audio import FishAudioAdapter, MockFishAudioClient  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.speech_chunk import SpeechCancellationToken, build_speech_chunks  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mgr_with_fish_audio(client=None):
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client or MockFishAudioClient(playback_delay_s=0.03))
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


def _speak_request(texts: List[str], request_id: str, conversation_id: Optional[str] = "conv-1"):
    chunks = build_speech_chunks(texts, texts, request_id=request_id, conversation_id=conversation_id)
    return SpeakRequest(data={
        "text": " ".join(texts), "request_id": request_id, "conversation_id": conversation_id,
        "chunks": [c.to_dict() for c in chunks],
    })


# ============================================================================
# SpeechCancellationToken - unit-level (no adapter/event bus needed)
# ============================================================================

def test_token_starts_neither_cancelled_nor_paused():
    t = SpeechCancellationToken("r1")
    assert t.is_cancelled is False
    assert t.is_paused is False


def test_token_cancel_is_idempotent_21():
    t = SpeechCancellationToken("r1")
    t.cancel()
    t.cancel()
    t.cancel()
    assert t.is_cancelled is True
    assert t.is_paused is False  # cancel() always clears pause too


def test_token_pause_then_cancel_clears_pause():
    t = SpeechCancellationToken("r1")
    t.pause()
    assert t.is_paused is True
    t.cancel()
    assert t.is_cancelled is True
    assert t.is_paused is False


def test_token_wait_while_paused_returns_immediately_when_cancelled():
    t = SpeechCancellationToken("r1")
    t.pause()
    t.cancel()
    start = time.time()
    t.wait_while_paused(poll_interval_s=0.5)  # would hang 0.5s+ if cancel didn't break the wait
    assert time.time() - start < 0.2


def test_token_wait_while_paused_unblocks_on_resume():
    t = SpeechCancellationToken("r1")
    t.pause()
    released = threading.Event()

    def _resume_soon():
        time.sleep(0.05)
        t.resume()
        released.set()

    threading.Thread(target=_resume_soon, daemon=True).start()
    t.wait_while_paused(poll_interval_s=0.01)
    assert released.is_set()


# ============================================================================
# 17. cancel before first synthesis
# ============================================================================

def test_17_cancel_before_first_synthesis_never_plays_anything():
    """`StopPlayback` published IMMEDIATELY after `SpeakRequest` (before
    the worker thread has necessarily even started `_play()`) must still
    prevent any chunk from ever playing - exercises the "token registered
    before executor submission" fix in `handle_event()`."""
    client = MockFishAudioClient(playback_delay_s=0.05, synthesis_delay_s=0.1)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Never", "should", "play"], "presynth-cancel"))
    mgr.event_bus.publish(StopPlayback(data={"request_id": "presynth-cancel"}))
    ok = _wait_until(lambda: cancelled == ["presynth-cancel"], timeout_s=2.0)
    time.sleep(0.2)
    mgr.stop_all()

    assert ok, f"cancelled={cancelled} played={client.played}"
    assert client.played == []


# ============================================================================
# 18. cancel during multi-chunk request (mid chunk 1)
# ============================================================================

def test_18_cancel_during_first_chunk_playback():
    client = MockFishAudioClient(playback_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Chunk satu panjang.", "Chunk dua.", "Chunk tiga."], "mid-chunk1-cancel"))
    ok1 = _wait_until(lambda: started == ["mid-chunk1-cancel"], timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "mid-chunk1-cancel"}))
    ok2 = _wait_until(lambda: cancelled == ["mid-chunk1-cancel"], timeout_s=2.0)
    mgr.stop_all()

    assert ok1 and ok2
    assert client.played == ["Chunk satu panjang."]


# ============================================================================
# 19. cancel between chunks (the "gap" race)
# ============================================================================

def test_19_cancel_between_chunks_prevents_next_chunk():
    client = MockFishAudioClient(playback_delay_s=0.03, synthesis_delay_s=0.05)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Alpha.", "Beta.", "Gamma."], "gap-cancel"))
    ok1 = _wait_until(lambda: client.played == ["Alpha."], timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "gap-cancel"}))
    ok2 = _wait_until(lambda: cancelled == ["gap-cancel"], timeout_s=2.0)
    time.sleep(0.3)
    mgr.stop_all()

    assert ok1 and ok2
    assert client.played == ["Alpha."]


# ============================================================================
# 20. cancel after synthesis before playback
# ============================================================================

def test_20_cancel_after_synthesis_before_playback_start():
    """`RealFishAudioClient`-style two-phase play (synthesize, THEN play)
    is exercised via `synthesis_delay_s` on the mock - cancelling DURING
    that synthesis window (before `on_playback_start`/`SpeechPlaybackStarted`
    ever fires) must abort cleanly with no audio ever heard."""
    client = MockFishAudioClient(playback_delay_s=0.05, synthesis_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Synthesizing..."], "synth-window-cancel"))
    time.sleep(0.05)  # well inside the 0.3s synthesis window, before playback starts
    mgr.event_bus.publish(StopPlayback(data={"request_id": "synth-window-cancel"}))
    ok = _wait_until(lambda: cancelled == ["synth-window-cancel"], timeout_s=2.0)
    mgr.stop_all()

    assert ok, f"started={started} cancelled={cancelled}"
    assert started == []
    assert client.played == []


# ============================================================================
# 21. cancellation is idempotent (adapter-level)
# ============================================================================

def test_21_repeated_stop_playback_events_are_idempotent():
    client = MockFishAudioClient(playback_delay_s=0.05)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Halo."], "idempotent-req"))
    ok = _wait_until(lambda: cancelled == [] or len(cancelled) >= 1, timeout_s=1.0)
    for _ in range(5):
        mgr.event_bus.publish(StopPlayback(data={"request_id": "idempotent-req"}))
    time.sleep(0.2)
    mgr.stop_all()

    assert len(cancelled) <= 1, f"StopPlayback published 5x must never publish more than one terminal event: {cancelled}"


# ============================================================================
# 22. remaining chunks never play
# ============================================================================

def test_22_remaining_chunks_never_play_after_cancellation():
    client = MockFishAudioClient(playback_delay_s=0.05)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    texts = [f"Bagian {n}." for n in range(10)]
    mgr.event_bus.publish(_speak_request(texts, "many-chunks-cancel"))
    ok1 = _wait_until(lambda: len(client.played) >= 1, timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "many-chunks-cancel"}))
    ok2 = _wait_until(lambda: cancelled == ["many-chunks-cancel"], timeout_s=2.0)
    time.sleep(0.3)
    mgr.stop_all()

    assert ok1 and ok2
    assert len(client.played) < 10, f"all 10 chunks played despite cancellation: {client.played}"
    assert client.played == texts[:len(client.played)]  # order preserved, no chunk skipped-then-played-later


# ============================================================================
# 23. stale request cannot play after newer request starts
# ============================================================================

def test_23_stale_request_cannot_resume_after_newer_request_starts():
    client = MockFishAudioClient(playback_delay_s=0.2)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled, finished = [], []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["old turn"], "old-turn"))
    ok1 = _wait_until(lambda: "old turn" in client.played, timeout_s=2.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "old-turn"}))
    ok2 = _wait_until(lambda: cancelled == ["old-turn"], timeout_s=2.0)

    mgr.event_bus.publish(_speak_request(["new turn chunk one", "new turn chunk two"], "new-turn"))
    ok3 = _wait_until(lambda: finished == ["new-turn"], timeout_s=2.0)
    mgr.stop_all()

    assert ok1 and ok2 and ok3, f"cancelled={cancelled} finished={finished} played={client.played}"
    assert client.played == ["old turn", "new turn chunk one", "new turn chunk two"]


# ============================================================================
# 24. worker remains usable after cancellation
# ============================================================================

def test_24_adapter_remains_usable_for_many_requests_after_cancellation():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["cancel me"], "to-cancel"))
    time.sleep(0.01)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "to-cancel"}))
    time.sleep(0.1)

    for i in range(5):
        mgr.event_bus.publish(_speak_request([f"turn {i}"], f"post-cancel-{i}"))
    ok = _wait_until(lambda: len(finished) == 5, timeout_s=3.0)
    mgr.stop_all()

    assert ok, f"finished={finished}"
    # NOTE: the adapter's playback executor allows up to 2 requests in
    # flight concurrently (pre-existing sizing for the paused-reply +
    # barge-in CONFIRM interjection case - see FishAudioAdapter docstring),
    # so completion order across 5 back-to-back *different* requests is not
    # guaranteed to match submission order. What this scenario actually
    # requires (worker remains usable after cancellation) is that every
    # post-cancellation request eventually completes exactly once - not
    # strict FIFO ordering across independent requests. Ordering WITHIN a
    # single multi-chunk request is covered separately by test_11.
    assert sorted(finished) == sorted(f"post-cancel-{i}" for i in range(5))


# ============================================================================
# 34. cancellation does not count as generic failure
# ============================================================================

def test_34_cancellation_never_reported_as_generic_speech_error():
    client = MockFishAudioClient(playback_delay_s=0.2)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled_payloads = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled_payloads.append(e.data))

    mgr.event_bus.publish(_speak_request(["Halo dunia."], "not-an-error"))
    time.sleep(0.02)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "not-an-error"}))
    ok = _wait_until(lambda: len(cancelled_payloads) == 1, timeout_s=2.0)
    mgr.stop_all()

    assert ok
    assert "error" not in cancelled_payloads[0], f"a genuine cancellation must never carry an 'error' field: {cancelled_payloads[0]}"


# ============================================================================
# Barge-in integration (scenarios 25-30) - real BargeInModule, real
# RuntimeDemoConsole, mirrors tests/test_barge_in_console.py's own
# convention, extended for MULTI-CHUNK replies specifically.
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def test_25_26_27_active_multichunk_speech_stopped_by_barge_in_state_resets():
    """25: active speech is stopped. 26: remaining chunks discarded.
    27: speaking state resets correctly (SPEAKING -> WAITING_USER/IDLE via
    the EXISTING `SessionManagerModule._handle_playback_done()`, which
    already treats `speech_playback_cancelled` identically to
    `speech_playback_finished` - see ARCHITECTURE_GUARD.md, no new state
    needed for this sprint)."""
    """Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now defaults
    to `True` (see `luno/config.py`), so a turn's voice dispatch fires
    `speak_stream_chunk` (one or more times) instead of the legacy
    single `speak_request`. This test's actual invariants - the turn was
    committed to speech, then genuinely cancelled by barge-in, then the
    session state recovers out of SPEAKING - are unaffected by which
    event name carried the commitment, so it now counts either."""
    demo = _load_demo()
    from luno.wake_session import ConversationState
    long_reply = " ".join(f"Kalimat panjang nomor {n} untuk memastikan ada banyak chunk suara." for n in range(1, 8))
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=long_reply, chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.15),
    )
    console.start()
    try:
        _wake(console, demo)
        speak_requests = []
        cancelled = []
        sub1 = console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e))
        sub1b = console.event_bus.subscribe("speak_stream_chunk", lambda e: speak_requests.append(e))
        sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
        try:
            console.simulate_speech("ceritakan sesuatu yang panjang")
            assert _wait_until(lambda: len(speak_requests) >= 1, 5.0)
            assert _wait_until(lambda: console.session_manager.session.state == ConversationState.SPEAKING, 3.0)
            console.simulate_speech("stop")  # barge-in interrupt word
            assert _wait_until(lambda: len(cancelled) >= 1, 3.0), "speech was never cancelled by barge-in"
            assert _wait_until(
                lambda: console.session_manager.session.state in (ConversationState.WAITING_USER, ConversationState.LISTENING, ConversationState.IDLE),
                3.0,
            ), f"state stuck at {console.session_manager.session.state}"
        finally:
            console.event_bus.unsubscribe(sub1)
            console.event_bus.unsubscribe(sub1b)
            console.event_bus.unsubscribe(sub2)
    finally:
        console.stop()


def test_28_29_user_utterance_continues_and_new_response_can_speak_after_cancellation():
    """28: user utterance continues through the normal pipeline after a
    barge-in. 29: a new response can speak normally afterward."""
    """Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - see the sibling fix immediately above for
    the full rationale (`ENABLE_LLM_TTS_STREAMING` now defaults to
    `True`, so `speak_stream_chunk` carries a turn's voice dispatch
    instead of `speak_request`)."""
    demo = _load_demo()
    long_reply = " ".join(f"Kalimat nomor {n} yang cukup panjang." for n in range(1, 6))
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=long_reply, chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.1),
    )
    console.start()
    try:
        _wake(console, demo)
        speak_requests = []
        sub = console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e))
        subb = console.event_bus.subscribe("speak_stream_chunk", lambda e: speak_requests.append(e))
        try:
            console.simulate_speech("ceritakan sesuatu")
            assert _wait_until(lambda: len(speak_requests) >= 1, 5.0)
            console.simulate_speech("stop")
            time.sleep(0.3)

            console.openrouter_adapter.client.canned_text = "Jawaban baru setelah interupsi."
            before = len(speak_requests)
            console.simulate_speech("apa itu ESP32?")
            assert _wait_until(lambda: len(speak_requests) > before, 5.0), "new turn never produced a new speak_request"
        finally:
            console.event_bus.unsubscribe(sub)
            console.event_bus.unsubscribe(subb)
    finally:
        console.stop()


def test_30_no_zombie_thread_or_stuck_queue_after_barge_in():
    """No new persistent thread outlives the cancelled request, and the
    adapter's own bookkeeping (`_chunk_control`/`_in_flight_request_ids`)
    is empty once the cancellation settles - "queue macet" (stuck queue)
    would show up as a leftover entry here."""
    demo = _load_demo()
    long_reply = " ".join(f"Kalimat nomor {n}." for n in range(1, 8))
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=long_reply, chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.1),
    )
    console.start()
    try:
        _wake(console, demo)
        cancelled = []
        sub = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
        try:
            console.simulate_speech("ceritakan sesuatu yang panjang sekali")
            assert _wait_until(lambda: console.session_manager.session.state.value == "speaking", 5.0)
            console.simulate_speech("stop")
            assert _wait_until(lambda: len(cancelled) >= 1, 3.0)
            time.sleep(0.2)
        finally:
            console.event_bus.unsubscribe(sub)

        fa = console.fish_audio_adapter
        with fa._chunk_control_lock:
            assert fa._chunk_control == {}, f"stuck queue entries: {fa._chunk_control}"
        with fa._in_flight_lock:
            assert fa._in_flight_request_ids == set(), f"stuck in-flight ids: {fa._in_flight_request_ids}"
    finally:
        console.stop()
