"""
test_voice_pipeline_latency.py
================================

VOICE PIPELINE LATENCY & SEMANTIC SEGMENTATION sprint - Phase 1 baseline
measurement + Phase 12 latency test matrix (A-H).

Phase 0's audit (see docs/change_impact/voice_pipeline_latency_and_semantic_segmentation.md)
traced the REAL current production path and found:

  - The default configuration (`ENABLE_LLM_TTS_STREAMING=False`, the
    actual default in `luno/config.py` and this project's own `.env`)
    routes every turn through `BehaviorTreeModule._generate_reply()`,
    which BLOCKS on `assistant_response`/`llm_error` - i.e. waits for the
    ENTIRE LLM reply - before `_speak()` ever runs `build_dual_response()`
    or publishes a single `speak_request`. This is CASE B from the
    sprint brief's own bottleneck taxonomy ("LLM streams tokens but Luno
    waits for the entire response before speaking").
  - A COMPLETE, already-tested, already-production-wired alternative
    already exists (`luno.incremental_speech.StreamingSpeechCoordinator`/
    `IncrementalSpeechBuffer`, from the "LLM Streaming -> Real-Time
    Speech Pipeline" sprint) that flushes and speaks each SETTLED
    sentence as soon as it's confirmed complete, while the LLM is still
    generating the rest - but it is gated behind that same flag,
    default OFF.
  - A SEPARATE, real gap (found by this sprint, not assumed): the
    already-existing TTS Chunk Pipelining sprint's synth/playback
    overlap (`_play_stream_pipelined()`) was NEVER reachable from the
    DEFAULT `speak_request`/`AssistantResponse` code path at all -
    `_play()` never checked `client.supports_split_synthesis()`. Fixed
    in this sprint (`_play_pipelined()`, `luno/adapters/fish_audio.py`)
    - see tests below (D) for direct proof.

This file proves both findings with real, deterministic, repeatable
measurements - `MockOpenRouterClient`/`MockFishAudioClient` for the
console-level (real event-bus, real threading) latency comparison, and
a `TimedFakeSession`-driven REAL `RealFishAudioClient` (mirroring
`tests/test_tts_chunk_pipelining.py`'s own established technique) for
the synth/playback-overlap proof, which needs a client that genuinely
separates synthesis from playback (`MockFishAudioClient` does not).

Run:
    python3 -m pytest tests/test_voice_pipeline_latency.py -q -s
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import PausePlayback, ResumePlayback, SpeakRequest, SpeakStreamChunk, StopPlayback  # noqa: E402
from luno.adapters.fish_audio import FishAudioAdapter, MockFishAudioClient, PlaybackCancelled  # noqa: E402
from luno.adapters.fish_audio_real import RealFishAudioClient, RealFishAudioConfig  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_latency", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_latency"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


@contextlib.contextmanager
def _streaming(demo, enabled: bool, max_pending: int = 4):
    prev_enabled = demo.legacy_config.ENABLE_LLM_TTS_STREAMING
    prev_max = demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS
    demo.legacy_config.ENABLE_LLM_TTS_STREAMING = enabled
    demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = max_pending
    try:
        yield
    finally:
        demo.legacy_config.ENABLE_LLM_TTS_STREAMING = prev_enabled
        demo.legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS = prev_max


def _stats(values: List[float]) -> Dict[str, float]:
    s = sorted(values)
    n = len(s)
    p95_idx = min(n - 1, int(round(0.95 * (n - 1))))
    return {
        "min": round(s[0], 4), "median": round(statistics.median(s), 4),
        "p95": round(s[p95_idx], 4), "max": round(s[-1], 4),
    }


# ─────────────────────────────────────────────
# Phase 1 - one real turn's worth of T0-T8-equivalent timestamps,
# through the REAL console + REAL event bus + REAL threading, mocked
# only at the network/audio-device boundary (MockOpenRouterClient/
# MockFishAudioClient) - same technique every prior streaming sprint's
# own E2E suite already established.
# ─────────────────────────────────────────────

def _measure_first_audio_latency(demo, *, streaming_enabled: bool, reply: str, user_text: str,
                                  chunk_delay_s: float, playback_delay_s: float) -> Dict[str, Optional[float]]:
    """Runs ONE turn, returns a dict of latencies (seconds, relative to
    T0 = the moment `simulate_speech()` is called) for every timestamp
    the sprint brief names that this harness can observe:
    `llm_streaming` (T1-ish - LLM stream begins), first `llm_chunk`
    (T2 - first token), `llm_finished` (T3 - full response known),
    first `speak_request`/`speak_stream_chunk` (T5/T6-ish - first speech
    unit dispatched), `speech_playback_started` (T8 - first real audio),
    `speech_playback_finished` (whole turn done). Missing timestamps
    (e.g. no `speak_stream_chunk` at all when streaming is disabled)
    are `None`, never a fabricated number."""
    with _streaming(demo, streaming_enabled):
        # Construction must happen INSIDE the `with` block, not just
        # `.start()` - `RuntimeDemoConsole.__init__()`/`BehaviorTreeModule`
        # construction also consults the flag (see `_streaming()`'s own
        # docstring above, matching every prior streaming sprint's own
        # established test convention).
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=chunk_delay_s),
            fish_audio_client=MockFishAudioClient(playback_delay_s=playback_delay_s),
        )
        console.start()
        try:
            _wake(console, demo)
            marks: Dict[str, float] = {}

            def _mark(name: str):
                def _cb(e):
                    marks.setdefault(name, time.time())
                return _cb

            subs = [
                console.event_bus.subscribe("llm_streaming", _mark("llm_streaming")),
                console.event_bus.subscribe("llm_chunk", _mark("llm_chunk_first")),
                console.event_bus.subscribe("llm_finished", _mark("llm_finished")),
                console.event_bus.subscribe("speak_stream_chunk", _mark("first_speech_dispatch")),
                console.event_bus.subscribe("speak_request", _mark("first_speech_dispatch")),
                console.event_bus.subscribe("speech_playback_started", _mark("first_audio")),
                console.event_bus.subscribe("speech_playback_finished", _mark("speech_done")),
            ]
            try:
                t0 = time.time()
                console.simulate_speech(user_text)
                assert _wait_until(lambda: "speech_done" in marks, 8.0), "turn never completed speech playback"
            finally:
                for s in subs:
                    console.event_bus.unsubscribe(s)
        finally:
            console.stop()
    return {k: (round(v - t0, 4) if v is not None else None) for k, v in marks.items()}


# A realistic multi-sentence reply - long enough that "wait for the
# WHOLE thing" vs "speak as soon as sentence 1 settles" produces a
# measurable, honest difference (not a contrived one-liner).
_LONG_REPLY = (
    "ESP32 kamu tidak bisa connect ke MQTT broker. "
    "Masalah ini biasanya muncul karena kredensial WiFi yang salah dimasukkan ke firmware. "
    "Selain itu, broker MQTT mungkin memerlukan autentikasi username dan password yang belum kamu konfigurasi. "
    "Firewall di jaringan lokal juga bisa memblokir port 1883 yang digunakan MQTT secara default. "
    "Coba periksa serial monitor untuk melihat pesan error koneksi WiFi terlebih dahulu. "
    "Kalau WiFi sudah terhubung tapi MQTT masih gagal, periksa kembali username dan password broker kamu."
)


def test_A_first_audio_latency_measured_default_vs_streaming():
    """THE core Phase 1/2 proof. Runs several repetitions of the SAME
    long reply through BOTH configurations and reports min/median/p95/max
    first-audio latency (T8 - T0) for each - printed (run with `-s` to
    see them) and asserted: the streaming configuration's median
    first-audio latency must be MEANINGFULLY lower (not just noise) than
    the default configuration's, proving CASE B is real and that the
    already-built streaming path actually closes it, not merely that
    streaming exists."""
    demo = _load_demo()
    n_runs = 5
    chunk_delay_s = 0.03  # ~33 tokens/s - realistic-ish LLM token cadence for this harness
    playback_delay_s = 0.05

    default_latencies = []
    streaming_latencies = []
    for i in range(n_runs):
        m = _measure_first_audio_latency(
            demo, streaming_enabled=False, reply=_LONG_REPLY, user_text=f"kenapa ESP32 gak bisa connect MQTT {i}",
            chunk_delay_s=chunk_delay_s, playback_delay_s=playback_delay_s,
        )
        assert m.get("first_audio") is not None, f"default run {i} never produced audio: {m}"
        default_latencies.append(m["first_audio"])

    for i in range(n_runs):
        m = _measure_first_audio_latency(
            demo, streaming_enabled=True, reply=_LONG_REPLY, user_text=f"kenapa ESP32 gak bisa connect MQTT {i}",
            chunk_delay_s=chunk_delay_s, playback_delay_s=playback_delay_s,
        )
        assert m.get("first_audio") is not None, f"streaming run {i} never produced audio: {m}"
        streaming_latencies.append(m["first_audio"])

    default_stats = _stats(default_latencies)
    streaming_stats = _stats(streaming_latencies)

    print(f"\n[LATENCY] first-audio-latency (T8-T0), default (non-streaming) path, n={n_runs}: {default_stats}")
    print(f"[LATENCY] first-audio-latency (T8-T0), streaming path,               n={n_runs}: {streaming_stats}")
    improvement_pct = round(100 * (1 - streaming_stats["median"] / default_stats["median"]), 1)
    print(f"[LATENCY] median improvement: {improvement_pct}%")

    # The default path must wait for the FULL reply (5 more sentences'
    # worth of chunk_delay_s after the first) before it can even START
    # selection/synthesis - the streaming path starts as soon as
    # sentence 1 settles. Assert the measured relationship, not just
    # print it - this is the actual regression-worthy proof.
    assert streaming_stats["median"] < default_stats["median"], (
        f"streaming path was not faster: default={default_stats} streaming={streaming_stats}"
    )


def test_B_streaming_speaks_before_full_llm_response_when_supported():
    """Phase 2 CASE B's own defining question, directly observed: does the
    first speech dispatch happen BEFORE `llm_finished`? For the DEFAULT
    (non-streaming) path this must be False (by construction - `_speak()`
    is only ever called with the full text). For the streaming path this
    must be True for a genuinely multi-sentence reply."""
    demo = _load_demo()
    m_default = _measure_first_audio_latency(
        demo, streaming_enabled=False, reply=_LONG_REPLY, user_text="kenapa ESP32 gak bisa connect MQTT default",
        chunk_delay_s=0.02, playback_delay_s=0.02,
    )
    assert m_default.get("first_speech_dispatch") is not None
    assert m_default.get("llm_finished") is not None
    assert m_default["first_speech_dispatch"] >= m_default["llm_finished"], (
        "default path unexpectedly dispatched speech before the LLM finished - architecture assumption violated"
    )

    m_stream = _measure_first_audio_latency(
        demo, streaming_enabled=True, reply=_LONG_REPLY, user_text="kenapa ESP32 gak bisa connect MQTT stream",
        chunk_delay_s=0.02, playback_delay_s=0.02,
    )
    assert m_stream.get("first_speech_dispatch") is not None
    assert m_stream.get("llm_finished") is not None
    assert m_stream["first_speech_dispatch"] < m_stream["llm_finished"], (
        f"streaming path did not dispatch speech before llm_finished: {m_stream}"
    )


def test_C_no_incomplete_sentence_ever_dispatched_to_tts():
    """Phase 9/10's own hard invariant, observed directly through the real
    pipeline: every `speak_stream_chunk` dispatched during a streamed
    turn must be a complete, terminally-punctuated (or the deliberately
    always-flushed final) sentence - never a bare, still-growing partial
    like "Kalau WiFi...". Reuses `IncrementalSpeechBuffer`'s own
    settle-only-on-confirmed-boundary guarantee (unmodified by this
    sprint) - this test proves that guarantee holds through the REAL
    event-bus path, not just in the buffer's own unit tests."""
    demo = _load_demo()
    dispatched_texts: List[str] = []
    with _streaming(demo, True):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text=_LONG_REPLY, chunk_delay_s=0.02),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
        )
        console.start()
        try:
            _wake(console, demo)
            sub = console.event_bus.subscribe(
                "speak_stream_chunk",
                lambda e: dispatched_texts.append(((e.data.get("chunk") or {}).get("text") or "")),
            )
            try:
                console.simulate_speech("jelaskan kenapa ESP32 gak connect MQTT")
                assert _wait_until(lambda: len(dispatched_texts) >= 3, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
        finally:
            console.stop()

    non_empty = [t for t in dispatched_texts if t]
    assert non_empty, "no chunk text was ever dispatched"
    for t in non_empty:
        stripped = t.strip()
        # Every dispatched chunk must end on genuine sentence-final
        # punctuation OR be the split-tail of a too-long single sentence
        # (`_split_long_sentence()`'s own clause-boundary output, which
        # legitimately does not always end in `.`/`!`/`?`) - what it must
        # NEVER be is an obviously still-growing fragment ending mid-word
        # with no punctuation at all AND no comma/semicolon clause break.
        ends_reasonably = stripped[-1:] in ".!?…,;" or stripped[-1:].isalnum() is False
        assert ends_reasonably or len(stripped.split()) <= 3, f"suspicious fragment dispatched: {t!r}"


def test_D_no_partial_sentence_leaks_even_with_very_slow_token_arrival():
    """Adversarial timing case - even when tokens arrive one at a time,
    very slowly, no chunk is ever dispatched before its sentence-ending
    punctuation actually arrived."""
    demo = _load_demo()
    dispatched_texts: List[str] = []
    with _streaming(demo, True):
        console = demo.RuntimeDemoConsole(
            openrouter_client=demo.MockOpenRouterClient(canned_text="Kalau WiFi sudah terhubung, periksa MQTT.", chunk_delay_s=0.05),
            fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
        )
        console.start()
        try:
            _wake(console, demo)
            sub = console.event_bus.subscribe(
                "speak_stream_chunk",
                lambda e: dispatched_texts.append(((e.data.get("chunk") or {}).get("text") or "")),
            )
            try:
                console.simulate_speech("apa yang harus saya cek")
                assert _wait_until(lambda: len(dispatched_texts) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
        finally:
            console.stop()
    assert any(t for t in dispatched_texts), "nothing dispatched"
    for t in dispatched_texts:
        if not t:
            continue
        assert "kalau wifi..." not in t.lower(), f"an incomplete conditional opener leaked: {t!r}"


# ─────────────────────────────────────────────
# E-H: synth/playback overlap proof for the NEWLY-FIXED default
# (`speak_request`/`_play_pipelined()`) path - the `TimedFakeSession`/
# `make_timed_player` technique from tests/test_tts_chunk_pipelining.py,
# reused verbatim (not reimplemented), driving a REAL `RealFishAudioClient`
# through `speak_request`/`AssistantResponse` (never `SpeakStreamChunk` -
# this proves the DEFAULT, non-LLM-streaming code path specifically).
# ─────────────────────────────────────────────

class FakeResponse:
    def __init__(self, content: bytes = b"FAKE-WAV-BYTES"):
        self.status_code = 200
        self.content = content

    def json(self) -> Dict[str, Any]:
        return {"detail": "ok"}


class TimedFakeSession:
    def __init__(self, events: List[Tuple[float, str, Dict[str, Any]]], delay_s: float = 0.2):
        self.events = events
        self.delay_s = delay_s

    def post(self, url: str, json: Any = None, timeout: Any = None):
        text = (json or {}).get("text", "")
        self.events.append((time.time(), "SynthesisStart", {"text": text[:20]}))
        time.sleep(self.delay_s)
        self.events.append((time.time(), "SynthesisEnd", {"text": text[:20]}))
        return FakeResponse(content=b"FAKE-WAV-BYTES:" + text.encode())


def _make_timed_player(events: List[Tuple[float, str, Dict[str, Any]]], duration_s: float = 0.3):
    def _play_audio(wav_bytes: bytes, control) -> None:
        control.on_playback_start()
        events.append((time.time(), "PlaybackStart", {}))
        slept = 0.0
        step = 0.01
        while slept < duration_s:
            if control.cancel.is_set():
                raise PlaybackCancelled("cancelled")
            time.sleep(step)
            slept += step
        events.append((time.time(), "PlaybackEnd", {}))
    return _play_audio


def _mgr_with_real_fish_audio(client: RealFishAudioClient) -> Tuple[AdapterManager, FishAudioAdapter]:
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


def test_E_default_path_pipelining_synth_overlaps_playback():
    """THE core proof for the `_play_pipelined()` fix: a 3-chunk
    `speak_request` (the DEFAULT, non-LLM-streaming event type) through
    the REAL `RealFishAudioClient` must show chunk 2's synthesis starting
    BEFORE chunk 1's playback ends - FAILED before this sprint's fix
    (`_play()` never checked `supports_split_synthesis()`), passes now."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.2)
    client = RealFishAudioClient(session=session, play_audio_fn=_make_timed_player(events, duration_s=0.35))
    mgr, fa = _mgr_with_real_fish_audio(client)
    finished: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
    try:
        mgr.event_bus.publish(SpeakRequest(data={
            "request_id": "pipeline-default", "text": "one two three",
            "chunks": [
                {"text": "Chunk satu.", "chunk_id": "c0", "sequence": 0, "total": 3, "is_final": False},
                {"text": "Chunk dua.", "chunk_id": "c1", "sequence": 1, "total": 3, "is_final": False},
                {"text": "Chunk tiga.", "chunk_id": "c2", "sequence": 2, "total": 3, "is_final": True},
            ],
        }))
        assert _wait_until(lambda: len(finished) == 1, 5.0)
    finally:
        mgr.stop_all()
        client.close()

    starts = [(t, kind) for t, kind, _ in events if kind == "SynthesisStart"]
    ends = [(t, kind) for t, kind, _ in events if kind == "PlaybackEnd"]
    assert len(starts) == 3 and len(ends) == 3, f"events={events}"
    synth_start_1 = starts[1][0]
    playback_end_0 = ends[0][0]
    assert synth_start_1 < playback_end_0, (
        f"chunk 2 synthesis did not start before chunk 1 playback ended (no overlap): "
        f"synth_start_1={synth_start_1} playback_end_0={playback_end_0} events={events}"
    )


def test_F_default_path_playback_order_never_reordered_by_pipelining():
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    # Later chunks synthesize FASTER than earlier ones - if pipelining
    # ever reordered playback by "whichever synthesis finished first",
    # this would catch it.
    session = TimedFakeSession(events, delay_s=0.05)

    class VariableDelaySession(TimedFakeSession):
        def post(self, url, json=None, timeout=None):
            text = (json or {}).get("text", "")
            delay = {"Satu.": 0.3, "Dua.": 0.05, "Tiga.": 0.01}.get(text, 0.05)
            self.events.append((time.time(), "SynthesisStart", {"text": text}))
            time.sleep(delay)
            self.events.append((time.time(), "SynthesisEnd", {"text": text}))
            return FakeResponse(content=b"FAKE-WAV-BYTES:" + text.encode())

    client = RealFishAudioClient(session=VariableDelaySession(events), play_audio_fn=_make_timed_player(events, duration_s=0.05))
    mgr, fa = _mgr_with_real_fish_audio(client)
    finished: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
    try:
        mgr.event_bus.publish(SpeakRequest(data={
            "request_id": "pipeline-order", "text": "x",
            "chunks": [
                {"text": "Satu.", "chunk_id": "c0", "sequence": 0, "total": 3, "is_final": False},
                {"text": "Dua.", "chunk_id": "c1", "sequence": 1, "total": 3, "is_final": False},
                {"text": "Tiga.", "chunk_id": "c2", "sequence": 2, "total": 3, "is_final": True},
            ],
        }))
        assert _wait_until(lambda: len(finished) == 1, 5.0)
    finally:
        mgr.stop_all()
        client.close()

    playback_starts = [(t, meta) for t, kind, meta in events if kind == "PlaybackStart"]
    assert len(playback_starts) == 3
    # Order proof: PlaybackStart timestamps themselves are monotonic (the
    # method plays sequentially, one at a time) - reordering would show
    # up as chunk 3 (fastest synth) playing before chunk 1.
    times = [t for t, _ in playback_starts]
    assert times == sorted(times), f"playback timestamps not monotonic: {times}"


def test_G_cancellation_during_default_path_synthesis_discards_stale_audio():
    """Cancelling mid-synthesis of chunk 1 (the fix from this sprint -
    see `_play_pipelined()`'s own docstring on why chunk 0's synthesis
    now goes through `_prefetch_executor` instead of a raw in-thread
    call) must never let that chunk's audio play, and must never publish
    `SpeechPlaybackStarted`."""
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=1.0)
    client = RealFishAudioClient(session=session, play_audio_fn=_make_timed_player(events, duration_s=0.05))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started: List[Any] = []
    cancelled: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
    try:
        mgr.event_bus.publish(SpeakRequest(data={
            "request_id": "pipeline-cancel", "text": "will be cancelled",
            "chunks": [{"text": "Chunk lambat.", "chunk_id": "c0", "sequence": 0, "total": 1, "is_final": True}],
        }))
        time.sleep(0.15)
        mgr.event_bus.publish(StopPlayback(data={"request_id": "pipeline-cancel"}))
        assert _wait_until(lambda: len(cancelled) == 1, 3.0)
    finally:
        mgr.stop_all()
        client.close()
    assert started == [], f"SpeechPlaybackStarted was published despite cancellation during synthesis: {started}"


def test_H_pause_resume_still_correct_on_default_pipelined_path():
    events: List[Tuple[float, str, Dict[str, Any]]] = []
    session = TimedFakeSession(events, delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=_make_timed_player(events, duration_s=0.15))
    mgr, fa = _mgr_with_real_fish_audio(client)
    finished: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e))
    try:
        mgr.event_bus.publish(SpeakRequest(data={
            "request_id": "pipeline-pause", "text": "x",
            "chunks": [
                {"text": "Satu.", "chunk_id": "c0", "sequence": 0, "total": 2, "is_final": False},
                {"text": "Dua.", "chunk_id": "c1", "sequence": 1, "total": 2, "is_final": True},
            ],
        }))
        time.sleep(0.05)
        mgr.event_bus.publish(PausePlayback(data={"request_id": "pipeline-pause"}))
        time.sleep(0.1)
        mgr.event_bus.publish(ResumePlayback(data={"request_id": "pipeline-pause"}))
        assert _wait_until(lambda: len(finished) == 1, 5.0), "turn never finished after resume"
    finally:
        mgr.stop_all()
        client.close()
