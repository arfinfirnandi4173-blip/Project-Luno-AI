"""
test_streaming_speech_integration.py
========================================

LLM Streaming -> Real-Time Speech Pipeline sprint - Phase 13 scenarios
20-41 ("DUAL OUTPUT", "QUEUE", "CANCELLATION", "DEPTH", "SAFETY").

Two harness styles, matching the SAME conventions already established by
this codebase's prior sprints' test files - no new test convention:

  * QUEUE (24-28) and most of CANCELLATION (29-34) use a direct
    `AdapterManager.standalone()` + `FishAudioAdapter`/`MockFishAudioClient`
    harness (mirrors `tests/test_tts_queue.py`) and/or a direct
    `StreamingSpeechCoordinator` harness with an injectable
    `publish_stream_chunk` capture callback - no real event bus round
    trip needed to prove backpressure/cancellation bookkeeping.

  * DUAL OUTPUT (20-23), DEPTH (35-37), and SAFETY (38-41) use the real
    `main_runtime_demo.RuntimeDemoConsole` (mirrors `tests/test_tts_cancellation.py`'s
    own `_load_demo()`/`_wake()` pattern) with `ENABLE_LLM_TTS_STREAMING`
    turned on for the duration of the test only - these scenarios are
    fundamentally about whole-turn behavior (`_generate_reply()`/`_speak()`/
    memory retrieval/persistent state), which only the real console
    exercises end to end.

No real network access, no real Fish Audio server, no real speaker -
`MockOpenRouterClient`/`MockFishAudioClient` only.

Run:
    python3 -m pytest tests/test_streaming_speech_integration.py -q
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import (  # noqa: E402
    LLMCancelled, LLMChunk, LLMError, LLMFinished, LLMStreaming,
    SpeakStreamChunk, SpeechChunkPlaybackFinished, StopPlayback,
)
from luno.adapters.fish_audio import FishAudioAdapter, FishAudioClient, MockFishAudioClient  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.incremental_speech import StreamingSpeechCoordinator  # noqa: E402
from luno.speech_chunk import SpeechChunk  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


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


@contextlib.contextmanager
def _streaming(demo, enabled: bool, max_pending: int = 4):
    """Flips `luno.config.ENABLE_LLM_TTS_STREAMING`/`LLM_TTS_STREAM_MAX_PENDING_CHUNKS`
    for the duration of the `with` block only, then restores the previous
    values. Must wrap BOTH `RuntimeDemoConsole(...)` construction AND
    `.start()` - `BehaviorTreeModule.bind_event_bus()` reads this flag once,
    at `.start()` time (see that method's own docstring)."""
    prev_enabled = demo.legacy_config.ENABLE_LLM_TTS_STREAMING
    prev_max = demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS
    demo.legacy_config.ENABLE_LLM_TTS_STREAMING = enabled
    demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = max_pending
    try:
        yield
    finally:
        demo.legacy_config.ENABLE_LLM_TTS_STREAMING = prev_enabled
        demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = prev_max


def _streaming_enabled(demo, max_pending: int = 4):
    return _streaming(demo, True, max_pending)


def _run_streaming_turn(demo, user_text: str, canned_reply: str, *, chunk_delay_s: float = 0.0, playback_delay_s: float = 0.02):
    """Constructs+starts a console, wakes it, speaks one turn, and waits
    for both the full chat response and speech completion. Caller is
    responsible for `console.stop()` (kept out of a context manager here
    so callers can inspect `console.speak_log`/etc. before stopping)."""
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=canned_reply, chunk_delay_s=chunk_delay_s),
        fish_audio_client=MockFishAudioClient(playback_delay_s=playback_delay_s),
    )
    console.start()
    _wake(console, demo)  # subscriptions below start AFTER the wake ack's own speak_request/speech_playback_finished
    assistant_responses: List[Dict[str, Any]] = []
    stream_chunks: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    sub1 = console.event_bus.subscribe("assistant_response", lambda e: assistant_responses.append(e.data))
    sub2 = console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(e.data))
    sub3 = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data))
    try:
        console.simulate_speech(user_text)
        assert _wait_until(lambda: len(assistant_responses) >= 1, 5.0), "no assistant_response received"
        assert _wait_until(lambda: len(finished) >= 1, 5.0), "speech never finished"
    finally:
        console.event_bus.unsubscribe(sub1)
        console.event_bus.unsubscribe(sub2)
        console.event_bus.unsubscribe(sub3)
    return console, assistant_responses, stream_chunks, finished


# ============================================================================
# DUAL OUTPUT (20-23)
# ============================================================================

def test_20_full_final_chat_response_reconstructed_correctly():
    demo = _load_demo()
    reply = ("Ini jawaban lengkap yang cukup panjang untuk diuji. Kalimat kedua "
              "menyusul di sini. Dan kalimat ketiga menutup jawabannya.")
    with _streaming_enabled(demo):
        console, assistant_responses, _chunks, _finished = _run_streaming_turn(demo, "ceritakan sesuatu", reply)
        try:
            assert assistant_responses[0]["text"] == reply
        finally:
            console.stop()


def test_21_voice_chunks_are_incremental_not_one_giant_block():
    demo = _load_demo()
    reply = " ".join(f"Kalimat nomor {n} yang cukup panjang untuk jadi chunk sendiri." for n in range(6))
    with _streaming_enabled(demo):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.03),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.02),
        )
        console.start()
        stream_chunks: List[Any] = []
        llm_finished_at: List[float] = []
        sub1 = console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(time.time()))
        sub2 = console.event_bus.subscribe("llm_finished", lambda e: llm_finished_at.append(time.time()))
        try:
            _wake(console, demo)
            console.simulate_speech("ceritakan panjang lebar tentang ESP32")
            assert _wait_until(lambda: len(llm_finished_at) >= 1, 5.0)
            assert _wait_until(lambda: len(stream_chunks) >= 2, 3.0)
        finally:
            console.event_bus.unsubscribe(sub1)
            console.event_bus.unsubscribe(sub2)
            console.stop()
        assert len(stream_chunks) >= 2, "expected multiple incremental voice chunks, not one block"
        assert any(ts < llm_finished_at[0] for ts in stream_chunks), (
            "no voice chunk was dispatched before the LLM stream finished generating - not real streaming"
        )


def test_22_chat_response_is_not_truncated_by_streaming():
    demo = _load_demo()
    reply = " ".join(f"Kalimat nomor {n} yang cukup panjang untuk diuji kelengkapannya." for n in range(10))
    with _streaming_enabled(demo):
        console, assistant_responses, _chunks, _finished = _run_streaming_turn(demo, "jelaskan panjang lebar", reply)
        try:
            assert assistant_responses[0]["text"] == reply
            assert len(assistant_responses[0]["text"]) == len(reply)
        finally:
            console.stop()


def test_23_voice_and_chat_do_not_duplicate_the_same_turn():
    demo = _load_demo()
    reply = "Jawaban singkat tapi lengkap untuk turn ini."
    with _streaming_enabled(demo):
        console, _ar, _chunks, finished = _run_streaming_turn(demo, "halo apa kabar", reply)
        try:
            assert len(finished) == 1, f"expected exactly one speech_playback_finished, got {len(finished)}"
            assert any("already spoken via LLM streaming" in s for s in console.behavior_tree_module.speak_log), (
                "legacy _speak() path did not skip publishing a duplicate whole-response SpeakRequest"
            )
        finally:
            console.stop()


# ============================================================================
# QUEUE (24-28)
# ============================================================================

def _mgr_with_fish_audio(client=None):
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client or MockFishAudioClient(playback_delay_s=0.02))
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


def _stream_chunk_event(request_id: str, seq: int, text: str, *, is_final: bool = False, conversation_id: Optional[str] = "conv-1"):
    chunk = SpeechChunk(
        chunk_id=f"{request_id}:chunk:{seq}", request_id=request_id, conversation_id=conversation_id,
        sequence=seq, total=(seq + 1) if is_final else -1, raw_text=text, text=text, is_final=is_final,
    )
    return SpeakStreamChunk(data={"request_id": request_id, "conversation_id": conversation_id, "chunk": chunk.to_dict()})


def _coord_harness(max_pending_chunks: int = 4, max_buffer_chars: int = 220):
    """A `StreamingSpeechCoordinator` wired to a real event bus (for
    `llm_*`/`speech_chunk_playback_finished` subscriptions) but with
    `publish_stream_chunk` INJECTED as a plain capture list, rather than
    going through the real `SpeakStreamChunk` -> `FishAudioAdapter` wire -
    isolates coordinator-level backpressure/cancellation bookkeeping from
    `FishAudioAdapter`'s own playback timing (that's what the
    `_mgr_with_fish_audio()` harness above is for)."""
    mgr = AdapterManager.standalone()
    mgr.start_all()
    published: List[Dict[str, Any]] = []
    coord = StreamingSpeechCoordinator(
        mgr.event_bus, max_pending_chunks=max_pending_chunks, max_buffer_chars=max_buffer_chars,
        publish_stream_chunk=lambda rid, cid, chunk: published.append(chunk),
    )
    return mgr, coord, published


def test_24_stream_chunks_play_strictly_in_order():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    texts = [f"Bagian nomor {n}." for n in range(5)]
    try:
        for i, t in enumerate(texts):
            mgr.event_bus.publish(_stream_chunk_event("stream-order", i, t, is_final=(i == len(texts) - 1)))
        ok = _wait_until(lambda: finished == ["stream-order"], 3.0)
        assert ok, f"played={client.played}"
        assert client.played == texts
    finally:
        mgr.stop_all()


def test_25_bounded_backpressure_never_exceeds_configured_max_pending():
    mgr, coord, published = _coord_harness(max_pending_chunks=2)
    try:
        rid = "q25"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        text = " ".join(f"Kalimat nomor {n} yang cukup panjang untuk jadi chunk sendiri." for n in range(10))
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": text, "text_so_far": text, "index": 1}))
        time.sleep(0.05)
        assert len(published) <= 2, f"backpressure violated: {len(published)} chunks dispatched at once"
    finally:
        mgr.stop_all()


def test_26_only_the_first_sentence_dispatches_before_llm_finished_regardless_of_playback_signals():
    """SUPERSEDED by Sprint 3 (Production-Safe LLM -> TTS Streaming
    Activation): this test used to prove that `held_chunks` drained
    incrementally, mid-stream, as `speech_chunk_playback_finished`
    signals arrived - deliberately WITHOUT ever publishing `LLMFinished`.
    That premise is exactly the response-depth-policy bypass Sprint 3
    fixed (see `luno/incremental_speech.py`'s own module docstring,
    "RESPONSE-DEPTH-POLICY-SAFE REDESIGN"): dispatching sentences 2+
    before the full reply is known would risk speaking MORE than
    SHORT/NORMAL's budget-based selection allows, since that selection
    can only be computed once the total sentence count is known.

    The new, intentional contract this test now proves instead: only
    ONE sentence (the always-safe lead sentence) is EVER dispatched
    before `llm_finished` fires - and, critically, further
    `speech_chunk_playback_finished` signals do NOT cause any additional
    mid-stream dispatch (there is no more "held" content to drain -
    everything else waits for the real, depth-policy-aware selection in
    `_on_finished()`, proven separately by `test_35`/`test_36`/`test_37`
    below and by `tests/test_llm_tts_streaming_production.py`)."""
    mgr, coord, published = _coord_harness(max_pending_chunks=2)
    try:
        rid = "q26"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        # 6 sentences fed as ONE delta, followed by a 7th still-open
        # sentence to confirm the 6th's own boundary too - settles all 6
        # in a single `feed()` call, well before any `LLMFinished`.
        text = " ".join(f"Kalimat nomor {n} yang cukup panjang untuk chunk sendiri." for n in range(6)) + " Kalimat penutup"
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": text, "text_so_far": text, "index": 1}))
        time.sleep(0.05)
        first_batch = len(published)
        assert first_batch == 1, f"expected exactly one early-dispatched sentence, got {first_batch}"
        for i in range(first_batch):
            mgr.event_bus.publish(SpeechChunkPlaybackFinished(data={
                "request_id": rid, "chunk_id": published[i]["chunk_id"], "sequence": published[i]["sequence"],
            }))
            time.sleep(0.02)
        assert len(published) == first_batch, (
            "a playback-finished signal triggered additional mid-stream dispatch before llm_finished - "
            "this would bypass response-depth-based selection (Sprint 3's own fix)"
        )
    finally:
        mgr.stop_all()


def test_27_cancellation_flushes_pending_held_chunks():
    mgr, coord, published = _coord_harness(max_pending_chunks=1)
    try:
        rid = "q27"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        text = " ".join(f"Kalimat nomor {n} yang panjang sekali untuk diuji." for n in range(6))
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": text, "text_so_far": text, "index": 1}))
        time.sleep(0.05)
        dispatched_before = len(published)
        assert dispatched_before >= 1
        coord.cancel_turn(rid)
        if published:
            mgr.event_bus.publish(SpeechChunkPlaybackFinished(data={
                "request_id": rid, "chunk_id": published[-1]["chunk_id"], "sequence": published[-1]["sequence"],
            }))
        time.sleep(0.05)
        assert len(published) == dispatched_before, "cancellation did not stop further chunk dispatch"
    finally:
        mgr.stop_all()


class _FailOnceStreamClient(FishAudioClient):
    """Fails the very FIRST `play()` call only, then succeeds forever
    after - proves `_play_stream()`'s bounded retry-then-continue policy
    (same as `_play()`'s) keeps the STREAM worker alive across one
    genuine per-chunk failure, distinct from cancellation."""

    def __init__(self, playback_delay_s: float = 0.01) -> None:
        self.calls = 0
        self.played: List[str] = []
        self.playback_delay_s = playback_delay_s

    def play(self, text: str, on_playback_start=None) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthesis failure")
        if on_playback_start is not None:
            on_playback_start()
        self.played.append(text)
        time.sleep(self.playback_delay_s)

    def stop(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass


def test_28_stream_worker_survives_a_chunk_failure_and_keeps_going():
    client = _FailOnceStreamClient()
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    texts = ["Chunk pertama sempat gagal.", "Chunk kedua berhasil.", "Chunk ketiga penutup."]
    try:
        for i, t in enumerate(texts):
            mgr.event_bus.publish(_stream_chunk_event("q28", i, t, is_final=(i == len(texts) - 1)))
        ok = _wait_until(lambda: finished == ["q28"], 3.0)
        assert ok, f"stream worker never recovered/finished - played={client.played}"
        assert len(client.played) >= 2, "worker did not keep processing chunks after the first failure"
    finally:
        mgr.stop_all()


# ============================================================================
# CANCELLATION (29-34)
# ============================================================================

def test_29_cancel_during_llm_generation_stops_the_turn():
    mgr, coord, published = _coord_harness(max_pending_chunks=10)
    try:
        rid = "c29"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": "Sedang mikir dulu ", "text_so_far": "Sedang mikir dulu ", "index": 1}))
        mgr.event_bus.publish(LLMCancelled(data={"request_id": rid}))
        time.sleep(0.02)
        assert coord.is_turn_streamed_and_completed(rid) is False
        dispatched_at_cancel = len(published)
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": "lanjutan yang seharusnya diabaikan", "text_so_far": "x", "index": 2}))
        mgr.event_bus.publish(LLMFinished(data={"request_id": rid}))
        time.sleep(0.02)
        assert len(published) == dispatched_at_cancel, "chunks were dispatched for a turn after llm_cancelled"
    finally:
        mgr.stop_all()


def test_30_cancel_after_partial_text_already_dispatched_stops_further_output():
    mgr, coord, published = _coord_harness(max_pending_chunks=10)
    try:
        rid = "c30"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        # A sentence only "settles" (flushes) once something ELSE arrives
        # after its terminal punctuation - see `IncrementalSpeechBuffer`'s
        # own docstring / `test_10_sentence_boundary_flushes_as_soon_as_confirmed`
        # in test_incremental_speech_buffer.py - so a continuation word is
        # fed here too, to actually confirm the first sentence's boundary.
        text = "Kalimat pertama sudah selesai dan lengkap. Lanjutan"
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": text, "text_so_far": text, "index": 1}))
        time.sleep(0.02)
        assert len(published) >= 1, "expected the first sentence to already have been dispatched"
        already_dispatched = len(published)
        mgr.event_bus.publish(LLMCancelled(data={"request_id": rid}))
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid, "delta": " Kalimat kedua yang harus diabaikan.", "text_so_far": "x", "index": 2}))
        time.sleep(0.02)
        assert len(published) == already_dispatched
    finally:
        mgr.stop_all()


def test_31_cancel_while_tts_has_pending_stream_chunks():
    client = MockFishAudioClient(playback_delay_s=0.2)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    rid = "c31"
    texts = [f"Bagian {n} dari jawaban panjang." for n in range(6)]
    try:
        for i, t in enumerate(texts):
            mgr.event_bus.publish(_stream_chunk_event(rid, i, t, is_final=(i == len(texts) - 1)))
        time.sleep(0.05)  # let the first chunk start playing
        mgr.event_bus.publish(StopPlayback(data={"request_id": rid}))
        ok = _wait_until(lambda: cancelled == [rid], 3.0)
        assert ok
        assert len(client.played) < len(texts), "all chunks played despite cancellation - queue was not stopped"
    finally:
        mgr.stop_all()


def test_32_cancel_while_a_chunk_is_actively_playing_interrupts_it():
    client = MockFishAudioClient(playback_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    rid = "c32"
    texts = [f"Bagian {n}." for n in range(4)]
    try:
        for i, t in enumerate(texts):
            mgr.event_bus.publish(_stream_chunk_event(rid, i, t, is_final=(i == len(texts) - 1)))
        assert _wait_until(lambda: started == [rid], 2.0)
        t0 = time.time()
        mgr.event_bus.publish(StopPlayback(data={"request_id": rid}))
        ok = _wait_until(lambda: cancelled == [rid], 2.0)
        elapsed = time.time() - t0
        assert ok
        assert elapsed < 0.3 * len(texts), "cancellation did not interrupt active playback promptly"
    finally:
        mgr.stop_all()


def test_33_stale_llm_chunk_after_cancellation_never_reaches_speak_stream_chunk_event():
    mgr = AdapterManager.standalone()
    mgr.start_all()
    coord = StreamingSpeechCoordinator(mgr.event_bus, max_pending_chunks=10)  # real event publish, no injection
    seen: List[Dict[str, Any]] = []
    mgr.event_bus.subscribe("speak_stream_chunk", lambda e: seen.append(e.data))
    try:
        rid = "c33"
        coord.start_turn(rid, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid}))
        mgr.event_bus.publish(LLMCancelled(data={"request_id": rid}))
        time.sleep(0.02)
        mgr.event_bus.publish(LLMChunk(data={
            "request_id": rid, "delta": "teks basi yang seharusnya tak pernah sampai ke TTS.",
            "text_so_far": "x", "index": 1,
        }))
        mgr.event_bus.publish(LLMFinished(data={"request_id": rid}))
        time.sleep(0.05)
        assert seen == [], f"stale chunk(s) reached speak_stream_chunk after cancellation: {seen}"
    finally:
        mgr.stop_all()


def test_34_new_request_works_normally_after_a_prior_cancellation():
    mgr, coord, published = _coord_harness(max_pending_chunks=10)
    try:
        rid_a, rid_b = "c34-a", "c34-b"
        coord.start_turn(rid_a, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid_a}))
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid_a, "delta": "Ini akan dibatalkan.", "text_so_far": "x", "index": 1}))
        mgr.event_bus.publish(LLMCancelled(data={"request_id": rid_a}))
        time.sleep(0.02)
        before_b = len(published)

        coord.start_turn(rid_b, "conv")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": rid_b}))
        text_b = "Request baru ini harus berjalan normal."
        mgr.event_bus.publish(LLMChunk(data={"request_id": rid_b, "delta": text_b, "text_so_far": text_b, "index": 1}))
        mgr.event_bus.publish(LLMFinished(data={"request_id": rid_b}))
        time.sleep(0.05)

        new_chunks = published[before_b:]
        assert len(new_chunks) >= 1, "no new request_id B chunks were ever dispatched"
        assert all(c["request_id"] == rid_b for c in new_chunks)
        assert coord.is_turn_streamed_and_completed(rid_b) is True
        assert coord.is_turn_streamed_and_completed(rid_a) is False
    finally:
        mgr.stop_all()


# ============================================================================
# DEPTH (35-37)
# ============================================================================

def test_35_short_depth_speaks_few_chunks():
    demo = _load_demo()
    reply = "Jawabannya iya, benar begitu."
    with _streaming_enabled(demo):
        console, _ar, chunks, _finished = _run_streaming_turn(demo, "jawab singkat, kenapa ESP32 panas?", reply)
        try:
            real_chunks = [c for c in chunks if c["chunk"].get("text")]
            assert len(real_chunks) <= 2, f"SHORT depth produced too many voice chunks: {real_chunks}"
        finally:
            console.stop()


def test_36_normal_depth_produces_normal_sentence_chunks():
    demo = _load_demo()
    reply = "Kalimat pertama menjelaskan konteksnya. Kalimat kedua menambahkan detail secukupnya."
    with _streaming_enabled(demo):
        console, _ar, chunks, _finished = _run_streaming_turn(demo, "apa itu ESP32?", reply)
        try:
            real_chunks = [c for c in chunks if c["chunk"].get("text")]
            assert len(real_chunks) >= 2, f"expected at least 2 normal sentence-based chunks, got {real_chunks}"
        finally:
            console.stop()


def test_37_detailed_depth_is_spoken_in_full_not_compressed():
    demo = _load_demo()
    # Distinct marker WORDS (not digits) for each point - `normalize_for_speech`
    # (the EXISTING, reused normalizer) spells digits out for TTS (e.g.
    # "1" -> "satu"), so this asserts on content that survives normalization
    # unchanged, exactly like `test_17_mixed_indonesian_english_streamed`/
    # `test_19_url_and_code_block_use_existing_normalizer` in
    # test_incremental_speech_buffer.py already do.
    markers = ["alpha", "beta", "gamma", "delta", "epsilon"]
    reply = " ".join(f"Poin {m} dijelaskan secara rinci di sini." for m in markers)
    with _streaming_enabled(demo):
        console, _ar, chunks, _finished = _run_streaming_turn(demo, "jelaskan detail cara kerja regulator ESP32", reply)
        try:
            spoken_text = " ".join(c["chunk"].get("text", "") for c in chunks)
            for m in markers:
                assert m in spoken_text, f"DETAILED streaming dropped/compressed point {m!r}: {spoken_text!r}"
        finally:
            console.stop()


# ============================================================================
# SAFETY (38-41)
# ============================================================================

def test_38_context_assembled_exactly_once_per_streamed_turn():
    demo = _load_demo()
    reply = "Balasan singkat untuk memastikan context cuma dirakit sekali."
    with _streaming_enabled(demo):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.0),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
        )
        console.start()
        calls: List[int] = []
        original = console.planner_module.memory_retriever.retrieve_memories

        def _counting(*a, **kw):
            calls.append(1)
            return original(*a, **kw)

        console.planner_module.memory_retriever.retrieve_memories = _counting
        try:
            _wake(console, demo)
            finished = []
            sub = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
            try:
                console.simulate_speech("halo apa kabar")
                assert _wait_until(lambda: len(finished) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
        finally:
            console.stop()
        assert len(calls) == 1, f"expected context/memory retrieval exactly once, got {len(calls)}"


def test_39_memory_retrieval_count_unchanged_by_streaming():
    reply = "Jawaban yang sama untuk dua mode."
    user_text = "ceritakan sesuatu yang biasa saja"

    def _one_turn(streaming: bool) -> int:
        demo = _load_demo()
        with _streaming(demo, streaming):
            console = demo.RuntimeDemoConsole(
                openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.0),
                fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
            )
            console.start()
            calls: List[int] = []
            original = console.planner_module.memory_retriever.retrieve_memories

            def _counting(*a, **kw):
                calls.append(1)
                return original(*a, **kw)

            console.planner_module.memory_retriever.retrieve_memories = _counting
            try:
                _wake(console, demo)
                finished = []
                sub = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
                try:
                    console.simulate_speech(user_text)
                    assert _wait_until(lambda: len(finished) >= 1, 5.0)
                finally:
                    console.event_bus.unsubscribe(sub)
            finally:
                console.stop()
            return len(calls)

    n_off = _one_turn(False)
    n_on = _one_turn(True)
    assert n_off == 1
    assert n_on == 1
    assert n_off == n_on, "streaming changed how many times memory retrieval ran for an equivalent turn"


def test_40_no_duplicate_speech_event_for_a_fully_streamed_turn():
    demo = _load_demo()
    reply = "Balasan pendek dan lengkap."
    with _streaming_enabled(demo):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.0),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
        )
        console.start()
        _wake(console, demo)
        speak_requests, finished = [], []
        sub1 = console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e))
        sub2 = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
        try:
            console.simulate_speech("halo")
            assert _wait_until(lambda: len(finished) >= 1, 5.0)
        finally:
            console.event_bus.unsubscribe(sub1)
            console.event_bus.unsubscribe(sub2)
            console.stop()
        assert speak_requests == [], (
            "a legacy whole-response SpeakRequest was ALSO published for a turn already spoken via streaming - "
            "two audio paths could have spoken simultaneously"
        )
        assert len(finished) == 1


def _hash_state_files(demo) -> Dict[str, str]:
    data_dir = demo.legacy_config.DATA_DIR
    hashes: Dict[str, str] = {}
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if fname.endswith(".json"):
                fpath = os.path.join(data_dir, fname)
                try:
                    with open(fpath, "rb") as f:
                        hashes[fname] = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    pass
    return hashes


def test_41_streaming_does_not_change_persistent_state_outcome_vs_non_streaming():
    """Not a claim of "zero mutation" (an ordinary turn may legitimately
    write conversation/episodic history - unrelated to this sprint). The
    claim under test: streaming must not cause a DIFFERENT set of
    persistent files to change, or leave stray state behind, compared to
    the exact same turn run WITHOUT streaming - i.e. streaming only
    changes single-final-completion into incremental-completion (Phase 8),
    never memory/persistence behavior itself."""
    reply = "Balasan tetap untuk membandingkan hasil persistent state."
    user_text = "test perbandingan persistent state streaming"

    def _one_turn(streaming: bool) -> None:
        demo = _load_demo()
        with _streaming(demo, streaming):
            console = demo.RuntimeDemoConsole(
                openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.0),
                fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
            )
            console.start()
            try:
                _wake(console, demo)
                finished = []
                sub = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
                try:
                    console.simulate_speech(user_text)
                    _wait_until(lambda: len(finished) >= 1, 5.0)
                finally:
                    console.event_bus.unsubscribe(sub)
                time.sleep(0.1)
            finally:
                console.stop()

    demo_probe = _load_demo()
    before_off = _hash_state_files(demo_probe)
    _one_turn(False)
    after_off = _hash_state_files(_load_demo())

    before_on = _hash_state_files(_load_demo())
    _one_turn(True)
    after_on = _hash_state_files(_load_demo())

    changed_off = {k for k in set(before_off) | set(after_off) if before_off.get(k) != after_off.get(k)}
    changed_on = {k for k in set(before_on) | set(after_on) if before_on.get(k) != after_on.get(k)}
    assert changed_on == changed_off, (
        f"streaming changed a DIFFERENT set of persistent files than non-streaming: "
        f"streaming={changed_on} non_streaming={changed_off}"
    )
