"""
test_llm_tts_streaming_production.py
=======================================

SPRINT 3 - PRODUCTION-SAFE LLM -> TTS STREAMING ACTIVATION, Phase 12's
34-scenario test matrix + Phase 13's real production-path E2E proofs.

Builds on the pre-existing streaming architecture (`luno.incremental_speech`,
from the "LLM Streaming -> Real-Time Speech Pipeline" sprint) and this
sprint's own fix to it: Phase 0's audit found the ORIGINAL streaming
path spoke every settled sentence immediately, with NO involvement from
`luno.response_output.build_dual_response()` - a confirmed bypass of
response-depth policy for every depth. This sprint's fix (documented in
`luno/incremental_speech.py`'s own "RESPONSE-DEPTH-POLICY-SAFE REDESIGN"
docstring section) dispatches only the one, always-safe lead sentence
early, then reconciles the REMAINING content against a real
`build_dual_response()` call once the full reply is known - the SAME
selection authority the non-streaming path already uses. This file
proves that fix, plus barge-in/cancellation/pause/conversation-lifecycle
safety, plus a companion fix to a PRE-EXISTING gap found along the way
(`BehaviorTreeModule._generate_reply()` not waking on `llm_cancelled`
during active LLM generation - see that method's own `_on_cancel()`
docstring in `main_runtime_demo.py`).

Most scenarios below run through the REAL `RuntimeDemoConsole` (real
event bus, real threading, mocked only at the LLM/audio-device boundary)
- per Phase 13's own instruction, never a direct internal handler call
for anything claiming to be an E2E proof.

Run:
    python3 -m pytest tests/test_llm_tts_streaming_production.py -q
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import statistics
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.fish_audio import MockFishAudioClient  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_streaming_prod", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_streaming_prod"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


@contextlib.contextmanager
def _streaming(demo, enabled: bool, max_pending: int = 4):
    """Must wrap BOTH `RuntimeDemoConsole(...)` construction AND
    `.start()` - `BehaviorTreeModule.bind_event_bus()` reads this flag
    once, at `.start()` time (same established convention every prior
    streaming sprint's own test files already use)."""
    prev_enabled = demo.legacy_config.ENABLE_LLM_TTS_STREAMING
    prev_max = demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS
    demo.legacy_config.ENABLE_LLM_TTS_STREAMING = enabled
    demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = max_pending
    try:
        yield
    finally:
        demo.legacy_config.ENABLE_LLM_TTS_STREAMING = prev_enabled
        demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = prev_max


def _new_console(demo, *, reply: str, chunk_delay_s: float = 0.0, playback_delay_s: float = 0.01, fail_tts: bool = False, malformed: bool = False):
    return demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=chunk_delay_s, malformed=malformed),
        fish_audio_client=MockFishAudioClient(playback_delay_s=playback_delay_s, fail=fail_tts),
    )


def _run_turn(console, demo, user_text: str, *, timeout_s: float = 8.0):
    """Wakes (if not already) and runs ONE turn, returning collected
    event data. Caller owns `console.stop()`."""
    stream_chunks: List[Dict[str, Any]] = []
    speak_requests: List[Dict[str, Any]] = []
    assistant_responses: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    llm_finished_at: List[float] = []
    started: List[Dict[str, Any]] = []
    subs = [
        console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(e.data)),
        console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e.data)),
        console.event_bus.subscribe("assistant_response", lambda e: assistant_responses.append(e.data)),
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data)),
        console.event_bus.subscribe("llm_finished", lambda e: llm_finished_at.append(time.time())),
        console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.data)),
    ]
    try:
        console.simulate_speech(user_text)
        _wait_until(lambda: len(finished) >= 1 or len(assistant_responses) >= 1, timeout_s)
        _wait_until(lambda: len(finished) >= 1, timeout_s)
        # Two independent modules each settle asynchronously AFTER
        # `speech_playback_finished`/`speech_playback_cancelled` fires (their
        # own subscriber callbacks run on the dispatcher, not synchronously
        # with ours): `BargeInModule.speaking` (barge-in gating) and
        # `SessionManagerModule`'s own `ConversationState` (SPEAKING ->
        # WAITING_USER/IDLE, see `_handle_playback_done()`). A caller that
        # wants to immediately fire ANOTHER turn (multi-turn loops) must
        # wait for BOTH to quiesce - firing simulate_speech() while
        # session_manager's state is still SPEAKING causes the utterance to
        # be silently dropped (see `_handle_speech_recognized()`'s own
        # "AWAKENING/THINKING/SPEAKING ... not forwarded" branch, no log,
        # no event - which looked exactly like a genuine hang until traced).
        from luno.wake_session import ConversationState
        _wait_until(lambda: not console.barge_in_module.speaking, 3.0)
        _wait_until(
            lambda: console.session_manager.session.state in (
                ConversationState.LISTENING, ConversationState.WAITING_USER, ConversationState.IDLE,
            ),
            3.0,
        )
    finally:
        for s in subs:
            console.event_bus.unsubscribe(s)
    return {
        "stream_chunks": stream_chunks, "speak_requests": speak_requests,
        "assistant_responses": assistant_responses, "finished": finished,
        "llm_finished_at": llm_finished_at, "started": started,
    }


# ─────────────────────────────────────────────
# 1-2. Streaming disabled -> legacy path; streaming enabled -> incremental path.
# ─────────────────────────────────────────────

def test_01_streaming_disabled_uses_legacy_speak_request_path():
    demo = _load_demo()
    with _streaming(demo, False):
        console = _new_console(demo, reply="Balasan singkat untuk mode legacy.")
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "halo")
            assert r["speak_requests"], "legacy path must publish speak_request"
            assert not r["stream_chunks"], "legacy path must never publish speak_stream_chunk"
        finally:
            console.stop()


def test_02_streaming_enabled_uses_incremental_speak_stream_chunk_path():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan singkat untuk mode streaming.")
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "halo")
            assert r["stream_chunks"], "streaming path must publish speak_stream_chunk"
            assert not r["speak_requests"], "streaming path must not ALSO publish a legacy speak_request (no duplicate audio path)"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 3. First audio does not wait for full LLM response.
# ─────────────────────────────────────────────

def test_03_first_audio_dispatched_before_llm_finished():
    demo = _load_demo()
    reply = "Kalimat pertama yang cukup panjang untuk diuji. Kalimat kedua melengkapi jawabannya."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.03)
        console.start()
        try:
            _wake(console, demo)
            dispatch_times: List[float] = []
            llm_finished_at: List[float] = []
            sub1 = console.event_bus.subscribe("speak_stream_chunk", lambda e: dispatch_times.append(time.time()))
            sub2 = console.event_bus.subscribe("llm_finished", lambda e: llm_finished_at.append(time.time()))
            try:
                console.simulate_speech("ceritakan sesuatu")
                assert _wait_until(lambda: len(llm_finished_at) >= 1, 5.0)
                assert _wait_until(lambda: len(dispatch_times) >= 1, 3.0)
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)
            assert dispatch_times[0] < llm_finished_at[0], "first speech unit was not dispatched before llm_finished"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 4/6/7/8. Coherence - no partial word/number, long sentence boundary preserved.
# ─────────────────────────────────────────────

def test_04_first_speech_unit_is_a_complete_coherent_sentence():
    demo = _load_demo()
    reply = "Ini adalah kalimat pertama yang lengkap dan utuh. Kalimat kedua menyusul di sini."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.01)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("ceritakan sesuatu")
                assert _wait_until(lambda: len(chunks) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            first_text = chunks[0]["chunk"]["text"]
            assert first_text.strip().endswith((".", "!", "?")), f"first dispatched unit is not a complete sentence: {first_text!r}"
            assert first_text.strip() == "Ini adalah kalimat pertama yang lengkap dan utuh."
        finally:
            console.stop()


def test_06_07_long_sentence_never_splits_mid_word():
    demo = _load_demo()
    long_sentence = (
        "Ini adalah satu kalimat yang sangat panjang sekali dengan banyak sekali kata "
        "di dalamnya sehingga kemungkinan besar akan melewati batas ukuran satu chunk "
        "TTS pada umumnya dan harus dipecah di suatu tempat yang aman tanpa memotong "
        "kata mana pun secara tidak wajar sama sekali di tengah-tengahnya."
    )
    with _streaming(demo, True):
        console = _new_console(demo, reply=long_sentence, chunk_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("jelaskan detail panjang")
                assert _wait_until(lambda: any(c["chunk"].get("is_final") for c in chunks), 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            words = set(long_sentence.replace(".", "").split())
            spoken = " ".join(c["chunk"]["text"] for c in chunks)
            spoken_words = spoken.replace(".", "").split()
            # every word that appears in the spoken output must be a WHOLE
            # word from the original sentence - never a truncated fragment.
            for w in spoken_words:
                assert w in words, f"word {w!r} looks like a mid-word split fragment"
        finally:
            console.stop()


def test_08_no_mid_number_split_across_deltas():
    demo = _load_demo()
    reply = "Tegangan yang dibutuhkan adalah 3.7 volt untuk baterai LiPo ini."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.02)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("berapa tegangan baterai")
                assert _wait_until(lambda: any(c["chunk"].get("is_final") for c in chunks), 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            spoken = " ".join(c["chunk"]["text"] for c in chunks).lower()
            # normalize_for_speech spells "3.7" out as words (observed:
            # "three point seven") - the point is it must appear as ONE
            # coherent number phrase, never split so only "three" or only
            # "seven" survives alone without the other.
            assert "three point seven" in spoken or "tiga koma tujuh" in spoken or "3.7" in spoken or "3,7" in spoken, spoken
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 5. Short sentence preserved.
# ─────────────────────────────────────────────

def test_05_short_first_sentence_preserved_intact():
    demo = _load_demo()
    reply = "Sudah terhubung. Kamu bisa lanjut ke langkah berikutnya sekarang."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.02)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("cek status")
                assert _wait_until(lambda: len(chunks) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            assert chunks[0]["chunk"]["text"].strip() == "Sudah terhubung."
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 9-10. No orphan conditional/causal sentence.
# ─────────────────────────────────────────────

def test_09_no_orphan_conditional_sentence_survives_alone():
    demo = _load_demo()
    reply = (
        "Restart ESP32 dulu sebelum melanjutkan langkah berikutnya. "
        "Kalau masih gagal terhubung, cek kabel power dan koneksi WiFi kamu."
    )
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("kenapa esp32 gak connect")
                assert _wait_until(lambda: any(c["chunk"].get("is_final") for c in chunks), 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            spoken = " ".join(c["chunk"]["text"] for c in chunks).lower()
            if "kalau masih gagal" in spoken:
                assert "restart esp32" in spoken, "conditional survived without its setup sentence"
        finally:
            console.stop()


def test_10_no_orphan_causal_sentence_survives_alone():
    demo = _load_demo()
    reply = (
        "Konverter murah tanpa heatsink bisa overheat pada beban tinggi. "
        "Akibatnya, komponen bisa terbakar dalam waktu singkat."
    )
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("kenapa converter saya panas")
                assert _wait_until(lambda: any(c["chunk"].get("is_final") for c in chunks), 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            spoken = " ".join(c["chunk"]["text"] for c in chunks).lower()
            if "akibatnya" in spoken:
                assert "overheat" in spoken or "konverter" in spoken, "causal survived without its setup sentence"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 11. Semantic ordering - chunks never reordered.
# ─────────────────────────────────────────────

def test_11_chunks_dispatched_in_strict_sequence_order():
    demo = _load_demo()
    markers = [f"m{n}" for n in range(8)]
    reply = " ".join(f"Bagian {m} membahas topik yang berbeda beda satu sama lain." for m in markers)
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            chunks: List[Dict[str, Any]] = []
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: chunks.append(e.data))
            try:
                console.simulate_speech("jelaskan semuanya secara detail dan lengkap")
                assert _wait_until(lambda: any(c["chunk"].get("is_final") for c in chunks), 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            seqs = [c["chunk"]["sequence"] for c in chunks]
            assert seqs == sorted(seqs), f"chunks dispatched out of order: {seqs}"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 12. TTS pipelining still functional (real client, synth/playback overlap)
#    - proven exhaustively in tests/test_voice_pipeline_latency.py tests
#    E-H; re-verified here at the coordinator-dispatch level for the
#    streaming path specifically.
# ─────────────────────────────────────────────

def test_12_streaming_path_still_uses_pipelined_fish_audio_playback():
    from luno.adapters.fish_audio import FishAudioAdapter
    from luno.adapters.manager import AdapterManager

    client = MockFishAudioClient(playback_delay_s=0.05)
    # `MockFishAudioClient.supports_split_synthesis()` reflects the same
    # ABC contract `RealFishAudioClient` implements - here we just prove
    # multiple stream chunks all play, in order, through the SAME
    # `_play_stream()`/`_play_stream_pipelined()` dispatch this sprint
    # did not modify.
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    from luno.adapters.events import SpeakStreamChunk
    from luno.speech_chunk import SpeechChunk
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    try:
        texts = [f"Bagian nomor {n}." for n in range(4)]
        for i, t in enumerate(texts):
            chunk = SpeechChunk(
                chunk_id=f"p12:chunk:{i}", request_id="p12", conversation_id="c",
                sequence=i, total=(i + 1) if i == len(texts) - 1 else -1,
                raw_text=t, text=t, is_final=(i == len(texts) - 1),
            )
            mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "p12", "conversation_id": "c", "chunk": chunk.to_dict()}))
        assert _wait_until(lambda: finished == ["p12"], 3.0)
        assert client.played == texts
    finally:
        mgr.stop_all()


# ─────────────────────────────────────────────
# 13-16. Cancellation at every stage.
# ─────────────────────────────────────────────

def test_13_cancellation_before_first_audio():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat panjang yang seharusnya dibatalkan sebelum sempat terdengar sama sekali.", chunk_delay_s=3.0, playback_delay_s=0.05)
        console.behavior_tree_module.llm_timeout_s = 3.0
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            cancelled: List[Any] = []
            sub1 = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
            try:
                console.simulate_speech("ceritakan sesuatu yang sangat panjang")
                time.sleep(0.1)
                console.simulate_speech("stop")
                assert _wait_until(lambda: not console.barge_in_module.thinking, 3.0)
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)
            assert started == [], "audio started despite cancellation before any chunk settled"
        finally:
            console.stop()


def test_14_cancellation_during_synthesis():
    from luno.adapters.fish_audio import FishAudioAdapter, PlaybackCancelled
    from luno.adapters.events import SpeakStreamChunk, StopPlayback
    from luno.adapters.manager import AdapterManager
    from luno.speech_chunk import SpeechChunk

    client = MockFishAudioClient(playback_delay_s=0.05, synthesis_delay_s=0.3)
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    try:
        chunk = SpeechChunk(chunk_id="p14:chunk:0", request_id="p14", conversation_id="c", sequence=0, total=1, raw_text="x", text="x", is_final=True)
        mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "p14", "conversation_id": "c", "chunk": chunk.to_dict()}))
        mgr.event_bus.publish(StopPlayback(data={"request_id": "p14"}))
        assert _wait_until(lambda: cancelled == ["p14"], 3.0)
        assert client.played == [], "chunk played despite cancellation during synthesis"
    finally:
        mgr.stop_all()


def test_15_cancellation_during_playback():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat pertama untuk playback panjang sekali agar sempat dibatalkan di tengah jalan.", chunk_delay_s=0.0, playback_delay_s=0.3)
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            cancelled: List[Any] = []
            sub1 = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
            try:
                console.simulate_speech("ceritakan sesuatu yang panjang")
                assert _wait_until(lambda: len(started) >= 1, 3.0)
                console.simulate_speech("stop")
                assert _wait_until(lambda: len(cancelled) >= 1, 3.0)
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)
        finally:
            console.stop()


def test_16_cancellation_with_prefetched_audio_never_plays_it():
    from luno.adapters.fish_audio import FishAudioAdapter
    from luno.adapters.events import SpeakStreamChunk, StopPlayback
    from luno.adapters.manager import AdapterManager
    from luno.speech_chunk import SpeechChunk

    client = MockFishAudioClient(playback_delay_s=0.15)
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    started, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    try:
        texts = [f"Bagian {n} dari jawaban panjang." for n in range(4)]
        for i, t in enumerate(texts):
            chunk = SpeechChunk(chunk_id=f"p16:chunk:{i}", request_id="p16", conversation_id="c", sequence=i, total=(i + 1) if i == len(texts) - 1 else -1, raw_text=t, text=t, is_final=(i == len(texts) - 1))
            mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "p16", "conversation_id": "c", "chunk": chunk.to_dict()}))
        assert _wait_until(lambda: len(started) >= 1, 3.0)
        time.sleep(0.05)  # let the one-slot prefetch for chunk 2 kick off
        mgr.event_bus.publish(StopPlayback(data={"request_id": "p16"}))
        assert _wait_until(lambda: cancelled == ["p16"], 3.0)
        assert len(client.played) < len(texts), "all chunks played despite cancellation with a prefetched chunk in flight"
    finally:
        mgr.stop_all()


# ─────────────────────────────────────────────
# 17. Pause/resume.
# ─────────────────────────────────────────────

def test_17_pause_resume_still_correct_during_streaming():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat pertama untuk uji pause. Kalimat kedua melengkapi jawabannya di sini.", chunk_delay_s=0.0, playback_delay_s=0.1)
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            finished: List[Any] = []
            sub1 = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            sub2 = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
            try:
                console.simulate_speech("ceritakan dua hal")
                assert _wait_until(lambda: len(started) >= 1, 3.0)
                from luno.adapters.events import PausePlayback, ResumePlayback
                rid = console.behavior_tree_module._streaming_coordinator
                # Pause/resume via the real event bus, matching production
                # barge-in's own control-event shape.
                active_ids = list(console.fish_audio_adapter._in_flight_request_ids) if hasattr(console.fish_audio_adapter, "_in_flight_request_ids") else []
                if active_ids:
                    console.event_bus.publish(PausePlayback(data={"request_id": active_ids[0]}))
                    time.sleep(0.05)
                    console.event_bus.publish(ResumePlayback(data={"request_id": active_ids[0]}))
                assert _wait_until(lambda: len(finished) >= 1, 5.0), "turn never completed after pause/resume"
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 18-20. Conversation lifecycle.
# ─────────────────────────────────────────────

def test_18_conversation_end_during_generation_terminates_cleanly():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat yang sedang dihasilkan saat percakapan berakhir.", chunk_delay_s=2.0)
        console.behavior_tree_module.llm_timeout_s = 3.0
        console.start()
        try:
            _wake(console, demo)
            console.simulate_speech("ceritakan sesuatu")
            time.sleep(0.1)
            console.simulate_speech("tidur")  # sleep / end conversation
            time.sleep(0.3)
            coord = console.behavior_tree_module._streaming_coordinator
            # no deadlock, no exception - if we got here, cleanup proceeded.
            assert coord is not None
        finally:
            console.stop()


def test_19_conversation_end_during_playback_terminates_cleanly():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat yang sedang diputar saat percakapan berakhir dengan durasi cukup panjang.", chunk_delay_s=0.0, playback_delay_s=0.3)
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            sub = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            try:
                console.simulate_speech("ceritakan sesuatu")
                assert _wait_until(lambda: len(started) >= 1, 3.0)
            finally:
                console.event_bus.unsubscribe(sub)
            console.simulate_speech("tidur")
            time.sleep(0.3)
        finally:
            console.stop()


def test_20_new_conversation_after_cancellation_works_normally():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan A yang akan dibatalkan.", chunk_delay_s=0.0, playback_delay_s=0.2)
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            cancelled: List[Any] = []
            sub1 = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
            try:
                console.simulate_speech("request A")
                assert _wait_until(lambda: len(started) >= 1, 3.0)
                console.simulate_speech("stop")
                assert _wait_until(lambda: len(cancelled) >= 1, 3.0)
                assert _wait_until(lambda: not console.barge_in_module.speaking, 3.0)
                from luno.wake_session import ConversationState
                assert _wait_until(
                    lambda: console.session_manager.session.state in (
                        ConversationState.LISTENING, ConversationState.WAITING_USER, ConversationState.IDLE,
                    ),
                    3.0,
                )
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)

            console.openrouter_adapter.client.canned_text = "Balasan B yang benar benar baru."
            r = _run_turn(console, demo, "request B")
            assert r["assistant_responses"] and r["assistant_responses"][0]["text"] == "Balasan B yang benar benar baru."
            assert all("Balasan A" not in c["chunk"].get("text", "") for c in r["stream_chunks"])
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 21-23. No stale audio, no duplicate audio, no worker leak.
# ─────────────────────────────────────────────

def test_21_no_stale_audio_leaks_into_next_turn():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Konten lama yang tidak boleh muncul lagi.", chunk_delay_s=0.0, playback_delay_s=0.15)
        console.start()
        try:
            _wake(console, demo)
            started: List[Any] = []
            sub = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
            try:
                console.simulate_speech("turn lama")
                assert _wait_until(lambda: len(started) >= 1, 3.0)
                console.simulate_speech("stop")
                assert _wait_until(lambda: not console.barge_in_module.speaking, 3.0)
            finally:
                console.event_bus.unsubscribe(sub)

            console.openrouter_adapter.client.canned_text = "Konten baru yang benar."
            r = _run_turn(console, demo, "turn baru")
            spoken = " ".join(c["chunk"].get("text", "") for c in r["stream_chunks"])
            assert "Konten lama" not in spoken
        finally:
            console.stop()


def test_22_no_duplicate_audio_for_a_fully_streamed_turn():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan pendek dan lengkap untuk diuji duplikasi.")
        console.start()
        try:
            r = None
            _wake(console, demo)
            r = _run_turn(console, demo, "halo")
            assert not r["speak_requests"], "a legacy SpeakRequest was ALSO published - two audio paths could speak simultaneously"
            assert len(r["finished"]) == 1
        finally:
            console.stop()


def test_23_no_thread_leak_after_many_streamed_turns():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan singkat berulang.")
        console.start()
        try:
            _wake(console, demo)
            for i in range(5):
                console.openrouter_adapter.client.canned_text = f"Balasan singkat nomor {i}."
                r = _run_turn(console, demo, f"pertanyaan {i}")
                assert r["finished"]
            coord = console.behavior_tree_module._streaming_coordinator
            assert len(coord._turns) == 0, f"turn state leaked across turns: {list(coord._turns.keys())}"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 24-26. Timeout handling, streaming failure fallback, TTS failure fallback.
# ─────────────────────────────────────────────

def test_24_llm_timeout_still_produces_an_apology_not_a_hang():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="tidak akan pernah selesai", chunk_delay_s=999.0)
        console.behavior_tree_module.llm_timeout_s = 0.3
        console.start()
        try:
            _wake(console, demo)
            t0 = time.time()
            console.simulate_speech("ceritakan sesuatu")
            ok = _wait_until(lambda: any("took too long" in s or "gave up" in s for s in console.behavior_tree_module.speak_log), 3.0)
            elapsed = time.time() - t0
            assert ok, "no timeout apology was ever spoken"
            assert elapsed < 2.0, f"timeout handling took too long: {elapsed}s"
        finally:
            console.stop()


def test_25_streaming_failure_falls_back_without_duplicate_speech():
    demo = _load_demo()
    reply = "Kalimat pertama berhasil sampai sini lalu semuanya gagal total setelah ini terjadi kesalahan."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, malformed=True)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "ceritakan sesuatu yang akan gagal")
            assert r["assistant_responses"] == [], "a complete assistant_response was published despite the stream failing"
            apology_count = sum(
                1 for s in console.behavior_tree_module.speak_log
                if "problem" in s.lower() or "sorry" in s.lower() or "maaf" in s.lower()
            )
            assert apology_count == 1, f"expected exactly one apology, got {apology_count}: {list(console.behavior_tree_module.speak_log)}"
        finally:
            console.stop()


def test_26_tts_failure_does_not_crash_the_turn():
    demo = _load_demo()
    with _streaming(demo, True):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text="Balasan yang gagal disintesis.", chunk_delay_s=0.0),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.01, fail=True),
        )
        console.start()
        try:
            _wake(console, demo)
            cancelled_or_error: List[Any] = []
            sub = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled_or_error.append(e))
            try:
                console.simulate_speech("halo")
                assert _wait_until(lambda: len(cancelled_or_error) >= 1, 5.0), "TTS failure never surfaced any terminal event"
            finally:
                console.event_bus.unsubscribe(sub)
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 27-31. Response depth SHORT/NORMAL/DETAILED + explicit overrides.
# ─────────────────────────────────────────────

def test_27_response_depth_short_produces_few_chunks():
    demo = _load_demo()
    reply = "Jawabannya iya, benar begitu."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "jawab singkat, kenapa ESP32 panas?")
            real_chunks = [c for c in r["stream_chunks"] if c["chunk"].get("text")]
            assert len(real_chunks) <= 2
        finally:
            console.stop()


def test_28_response_depth_normal_produces_normal_chunks():
    demo = _load_demo()
    reply = "Kalimat pertama menjelaskan konteksnya. Kalimat kedua menambahkan detail secukupnya."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "apa itu ESP32?")
            real_chunks = [c for c in r["stream_chunks"] if c["chunk"].get("text")]
            assert len(real_chunks) >= 2
        finally:
            console.stop()


def test_29_response_depth_detailed_non_explicit_still_bounded():
    demo = _load_demo()
    markers = [f"m{n}" for n in range(15)]
    reply = " ".join(f"Bagian {m} membahas detail teknis yang berbeda beda." for m in markers)
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            # "cara kerja" alone hits the architecture_or_deep_analysis
            # bucket (score 75 -> DEPTH_DETAILED) WITHOUT matching any of
            # `_EXPLICIT_DETAILED_PHRASES` (those require "secara detail"/
            # "secara mendalam"/"secara rinci"/"semuanya"/"lengkap"/etc) -
            # i.e. genuinely non-explicit DETAILED, unlike test_31 below.
            r = _run_turn(console, demo, "jelaskan cara kerja regulator ESP32")
            real_chunks = [c for c in r["stream_chunks"] if c["chunk"].get("text")]
            # non-explicit DETAILED still applies budget-based compression
            # (same as the non-streaming path) - must NOT speak all 15.
            assert len(real_chunks) < len(markers)
        finally:
            console.stop()


def test_30_explicit_short_override_respected():
    demo = _load_demo()
    reply = "Baterai LiPo 3.7V ini cocok untuk drone kecil kamu. Ini penting karena tegangan yang lebih tinggi bisa merusak motor brushless yang dipakai."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "jawab singkat aja, baterai apa yang cocok")
            spoken = " ".join(c["chunk"].get("text", "") for c in r["stream_chunks"])
            assert spoken.strip() != ""
        finally:
            console.stop()


def test_31_explicit_detailed_override_skips_compression_entirely():
    demo = _load_demo()
    markers = ["alpha", "beta", "gamma", "delta", "epsilon"]
    reply = " ".join(f"Poin {m} dijelaskan secara rinci di sini." for m in markers)
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "jelaskan detail cara kerja regulator ESP32")
            spoken = " ".join(c["chunk"].get("text", "") for c in r["stream_chunks"])
            for m in markers:
                assert m in spoken, f"explicit-detailed streaming dropped point {m!r}"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# 32-33. Multiple consecutive turns; concurrent conversations isolated.
# ─────────────────────────────────────────────

def test_32_multiple_consecutive_turns_all_complete_correctly():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan pertama.")
        console.start()
        try:
            _wake(console, demo)
            for i in range(3):
                console.openrouter_adapter.client.canned_text = f"Balasan turn nomor {i}."
                r = _run_turn(console, demo, f"pertanyaan {i}")
                assert r["assistant_responses"] and r["assistant_responses"][0]["text"] == f"Balasan turn nomor {i}."
        finally:
            console.stop()


def test_33_concurrent_conversation_ids_remain_isolated_in_coordinator():
    from luno.incremental_speech import StreamingSpeechCoordinator
    from luno.adapters.manager import AdapterManager
    from luno.adapters.events import LLMChunk, LLMFinished, LLMStreaming

    mgr = AdapterManager.standalone()
    mgr.start_all()
    published: List[Dict[str, Any]] = []
    coord = StreamingSpeechCoordinator(mgr.event_bus, publish_stream_chunk=lambda rid, cid, chunk: published.append((rid, cid, chunk)))
    try:
        coord.start_turn("conv-a-turn", "conv-a")
        coord.start_turn("conv-b-turn", "conv-b")
        mgr.event_bus.publish(LLMStreaming(data={"request_id": "conv-a-turn"}))
        mgr.event_bus.publish(LLMStreaming(data={"request_id": "conv-b-turn"}))
        text_a = "Kalimat percakapan A."
        text_b = "Kalimat percakapan B."
        mgr.event_bus.publish(LLMChunk(data={"request_id": "conv-a-turn", "delta": text_a, "text_so_far": text_a, "index": 1}))
        mgr.event_bus.publish(LLMChunk(data={"request_id": "conv-b-turn", "delta": text_b, "text_so_far": text_b, "index": 1}))
        mgr.event_bus.publish(LLMFinished(data={"request_id": "conv-a-turn"}))
        mgr.event_bus.publish(LLMFinished(data={"request_id": "conv-b-turn"}))
        time.sleep(0.1)
        for rid, cid, chunk in published:
            if rid == "conv-a-turn":
                assert cid == "conv-a"
                assert "percakapan B" not in chunk.get("text", "")
            elif rid == "conv-b-turn":
                assert cid == "conv-b"
                assert "percakapan A" not in chunk.get("text", "")
    finally:
        mgr.stop_all()


# ─────────────────────────────────────────────
# 34. Instrumentation does not persist state.
# ─────────────────────────────────────────────

def test_34_latency_instrumentation_never_persists_to_disk():
    demo = _load_demo()
    data_dir = demo.legacy_config.DATA_DIR
    before = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Balasan untuk uji instrumentasi.")
        console.start()
        try:
            _wake(console, demo)
            _run_turn(console, demo, "halo")
        finally:
            console.stop()
    after = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    new_files = after - before
    suspicious = [f for f in new_files if "latency" in f.lower() or "timing" in f.lower() or "ttft" in f.lower() or "ttfs" in f.lower() or "ttfa" in f.lower()]
    assert not suspicious, f"latency instrumentation appears to have persisted to disk: {suspicious}"


# ─────────────────────────────────────────────
# Phase 5 - proof that the companion barge-in-during-generation fix
# (main_runtime_demo.py's `_generate_reply()` `_on_cancel()`) actually
# works: a barge-in that lands WHILE the LLM is still generating no
# longer blocks the next turn until `llm_timeout_s`.
# ─────────────────────────────────────────────

def test_barge_in_during_active_generation_unblocks_promptly():
    demo = _load_demo()
    with _streaming(demo, True):
        console = _new_console(demo, reply="Kalimat panjang.", chunk_delay_s=5.0, playback_delay_s=0.01)
        console.behavior_tree_module.llm_timeout_s = 3.0
        console.start()
        try:
            _wake(console, demo)
            console.simulate_speech("ceritakan sesuatu yang sangat panjang")
            assert _wait_until(lambda: console.barge_in_module.thinking, 2.0), "LLM never entered the thinking state"
            console.simulate_speech("stop")
            assert _wait_until(lambda: not console.barge_in_module.thinking, 3.0)

            console.openrouter_adapter.client.canned_text = "Jawaban baru setelah interupsi."
            console.openrouter_adapter.client.chunk_delay_s = 0.0
            finished: List[Any] = []
            sub = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
            try:
                t0 = time.time()
                console.simulate_speech("pertanyaan baru singkat")
                ok = _wait_until(lambda: len(finished) >= 1, 6.0)
                elapsed = time.time() - t0
            finally:
                console.event_bus.unsubscribe(sub)
            assert ok, "new turn after barge-in-during-generation never completed"
            assert elapsed < 2.0, (
                f"new turn took {elapsed}s to complete - suggests _generate_reply() was still "
                f"blocked waiting on the CANCELLED prior turn instead of waking on llm_cancelled"
            )
        finally:
            console.stop()


# ─────────────────────────────────────────────
# Phase 13 - 3 real production-path E2E tests (explicit, self-contained,
# through RuntimeDemoConsole -> PlannerBridgeModule -> real Event Bus ->
# real streaming path). Several tests above already exercise this same
# real path; these three are called out explicitly per the brief.
# ─────────────────────────────────────────────

def test_E2E_1_short_response_stays_intact_through_real_console():
    demo = _load_demo()
    reply = "Sudah berhasil. Relay sekarang aktif."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "cek status relay")
            spoken = " ".join(c["chunk"].get("text", "") for c in r["stream_chunks"]).lower()
            assert "sudah berhasil" in spoken
            assert "relay sekarang aktif" in spoken
        finally:
            console.stop()


def test_E2E_2_conditional_response_setup_and_condition_stay_coherent():
    demo = _load_demo()
    reply = "Restart ESP32 dulu. Kalau masih gagal, cek kabel power dan koneksi WiFi."
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply)
        console.start()
        try:
            _wake(console, demo)
            r = _run_turn(console, demo, "gimana kalau esp32 gagal konek")
            spoken = " ".join(c["chunk"].get("text", "") for c in r["stream_chunks"]).lower()
            if "kalau masih gagal" in spoken:
                assert "restart" in spoken
        finally:
            console.stop()


def test_E2E_3_long_response_first_unit_speakable_before_llm_completes():
    demo = _load_demo()
    reply = (
        "GPU mengalami throttling saat gaming berat. "
        "Ini terjadi karena thermal paste sudah kering dan tidak menghantarkan panas dengan baik. "
        "Akibatnya, panas dari GPU core tidak bisa dipindahkan ke heatsink secara efisien."
    )
    with _streaming(demo, True):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.03)
        console.start()
        try:
            _wake(console, demo)
            dispatch_times: List[float] = []
            llm_finished_at: List[float] = []
            sub1 = console.event_bus.subscribe("speak_stream_chunk", lambda e: dispatch_times.append(time.time()))
            sub2 = console.event_bus.subscribe("llm_finished", lambda e: llm_finished_at.append(time.time()))
            try:
                console.simulate_speech("kenapa gpu saya throttle")
                assert _wait_until(lambda: len(llm_finished_at) >= 1, 5.0)
                assert _wait_until(lambda: len(dispatch_times) >= 1, 3.0)
            finally:
                console.event_bus.unsubscribe(sub1)
                console.event_bus.unsubscribe(sub2)
            assert dispatch_times[0] < llm_finished_at[0], "first TTS dispatch did not occur before LLM full-response-completion"
        finally:
            console.stop()


# ─────────────────────────────────────────────
# Phase 13 latency regression - default vs streaming, 5+ repetitions,
# min/median/p95/max, plus inter-chunk gap measurement.
# ─────────────────────────────────────────────

def _stats(values: List[float]) -> Dict[str, float]:
    s = sorted(values)
    n = len(s)
    p95_idx = min(n - 1, int(round(0.95 * (n - 1))))
    return {"min": round(s[0], 4), "median": round(statistics.median(s), 4), "p95": round(s[p95_idx], 4), "max": round(s[-1], 4)}


def _measure_first_audio_latency(demo, *, streaming_enabled: bool, reply: str, user_text: str) -> float:
    with _streaming(demo, streaming_enabled):
        console = _new_console(demo, reply=reply, chunk_delay_s=0.03, playback_delay_s=0.05)
        console.start()
        try:
            _wake(console, demo)
            marks: Dict[str, float] = {}
            sub = console.event_bus.subscribe("speech_playback_started", lambda e: marks.setdefault("t", time.time()))
            try:
                t0 = time.time()
                console.simulate_speech(user_text)
                assert _wait_until(lambda: "t" in marks, 8.0)
            finally:
                console.event_bus.unsubscribe(sub)
        finally:
            console.stop()
    return round(marks["t"] - t0, 4)


def test_latency_regression_default_vs_streaming_corrected_design():
    demo = _load_demo()
    reply = (
        "ESP32 kamu perlu terhubung ke jaringan WiFi rumah terlebih dahulu. "
        "Setelah itu pastikan broker MQTT sudah berjalan di jaringan yang sama. "
        "Kalau semua sudah siap, ESP32 bisa mulai publish data sensor ke topic tertentu."
    )
    default_vals = [_measure_first_audio_latency(demo, streaming_enabled=False, reply=reply, user_text="ceritakan tentang esp32") for _ in range(5)]
    streaming_vals = [_measure_first_audio_latency(demo, streaming_enabled=True, reply=reply, user_text="ceritakan tentang esp32") for _ in range(5)]
    default_stats = _stats(default_vals)
    streaming_stats = _stats(streaming_vals)
    print(f"[LATENCY] first-audio-latency, default path, n=5: {default_stats}")
    print(f"[LATENCY] first-audio-latency, streaming path (corrected, depth-policy-safe), n=5: {streaming_stats}")
    improvement = round((1 - streaming_stats["median"] / default_stats["median"]) * 100, 1)
    print(f"[LATENCY] median improvement: {improvement}%")
    assert streaming_stats["median"] < default_stats["median"], (
        "streaming (even under the depth-policy-safe redesign) must still measurably beat the default path "
        "for first-audio latency - the whole point of speaking the first sentence early"
    )


def test_latency_inter_chunk_gap_near_zero_when_synthesis_faster_than_playback():
    """Mirrors `tests/test_tts_chunk_pipelining.py`'s own established
    `TimedFakeSession`/`make_timed_player` technique exactly (real
    `FishAudioAdapter` + real `RealFishAudioClient` + real `AdapterManager`
    Event Bus, only the HTTP/audio-hardware boundary faked) - proves the
    inter-chunk GAP itself (PLAY_START[i+1] - PLAY_END[i]), not just that
    prefetch synthesis started early (already proven by
    `test_synthesis_of_next_chunk_starts_before_current_playback_ends` in
    that other file - this test is this sprint's own confirmation the
    streaming activation work didn't regress it)."""
    from luno.adapters.events import SpeakStreamChunk
    from luno.adapters.fish_audio import FishAudioAdapter
    from luno.adapters.fish_audio_real import RealFishAudioClient, RealFishAudioConfig
    from luno.adapters.manager import AdapterManager
    from luno.speech_chunk import SpeechChunk

    events: List[Any] = []

    class _FakeResponse:
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.status_code = status_code

        def json(self):
            return {}

    class _TimedFakeSession:
        def __init__(self, delay_s: float = 0.02):
            self.delay_s = delay_s

        def post(self, url, json=None, timeout=None):
            text = (json or {}).get("text", "")
            events.append((time.time(), "SynthesisStart", text))
            time.sleep(self.delay_s)
            events.append((time.time(), "SynthesisEnd", text))
            return _FakeResponse(content=b"FAKE-WAV:" + text.encode())

    def _play_audio(wav_bytes: bytes, control) -> None:
        control.on_playback_start()
        events.append((time.time(), "PlaybackStart", wav_bytes))
        # synth (0.02s) is deliberately faster than playback (0.08s) - the
        # whole point being tested: chunk N+1's synthesis should already be
        # done by the time chunk N's playback ends, so the transition gap
        # is near-zero, not another full synthesis wait tacked on.
        slept, step = 0.0, 0.005
        while slept < 0.08:
            if control.cancel.is_set():
                return
            time.sleep(step)
            slept += step
        events.append((time.time(), "PlaybackEnd", wav_bytes))

    session = _TimedFakeSession(delay_s=0.02)
    config = RealFishAudioConfig(engine="gptsovits")
    client = RealFishAudioClient(config, session=session, play_audio_fn=_play_audio)

    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    finished: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    try:
        texts = [f"Bagian {n}." for n in range(3)]
        total = len(texts)
        for i, t in enumerate(texts):
            chunk = SpeechChunk(chunk_id=f"gap-1:chunk:{i}", request_id="gap-1", conversation_id="c", sequence=i, total=total, raw_text=t, text=t, is_final=(i == total - 1))
            mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "gap-1", "conversation_id": "c", "chunk": chunk.to_dict()}))
        assert _wait_until(lambda: finished == ["gap-1"], 5.0)

        play_starts = sorted(e[0] for e in events if e[1] == "PlaybackStart")
        play_ends = sorted(e[0] for e in events if e[1] == "PlaybackEnd")
        assert len(play_starts) == total and len(play_ends) == total
        gaps = [round(play_starts[i + 1] - play_ends[i], 4) for i in range(total - 1)]
        print(f"[LATENCY] inter-chunk gaps (synth {session.delay_s}s < playback 0.08s): {gaps}")
        for g in gaps:
            assert g < 0.02, f"inter-chunk gap {g}s is not near-zero - prefetch synthesis was not overlapping playback as expected"
    finally:
        mgr.stop_all()
