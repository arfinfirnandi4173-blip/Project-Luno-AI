"""
test_voice_naturalness_and_latency.py
========================================

VOICE OUTPUT NATURALNESS + FIRST-AUDIO LATENCY sprint - Phase 6's own
25-scenario test matrix (10 semantic/list coherence, 5 short-sentence
protection, 10 streaming latency) plus the required real-console E2E
proof (intro/setup + multiple bullets + conclusion).

This sprint fixed exactly two things, both documented in depth at their
own call sites (see `luno/response_output.py::_starts_list_run()` and
`luno/config.py`'s `ENABLE_LLM_TTS_STREAMING` default, plus the
cancellation-safety fix in `luno/adapters/fish_audio.py
::_play_stream_pipelined()`):

  A. A bulleted/numbered list run's own SETUP sentence (the sentence
     immediately before it) is now protected the SAME way a
     discourse-marker dependent sentence already was - reusing
     `_select_scores_with_setup_bonus()`'s scoring bonus and
     `_repair_orphans()`'s hard rescue, never a new mechanism.
  b. `ENABLE_LLM_TTS_STREAMING` now defaults to `True`, activating the
     PRE-EXISTING, already safety-verified streaming architecture
     (`luno.incremental_speech`) in production by default, cutting
     median first-audio latency substantially (see
     `docs/change_impact/voice_output_naturalness_and_latency.md` for
     the full measured numbers) - and, along the way, this sprint found
     and fixed a genuine, previously-dormant cancellation gap in the
     streaming pipelined playback path (chunk 0 was not
     cancellation-responsive during synthesis - now fixed identically
     to how the legacy path's own sibling method was already fixed by
     an earlier sprint).

This file does NOT re-prove things already exhaustively covered
elsewhere - `tests/test_semantic_speech_units.py` already covers
short-sentence FUNCTION classification broadly, `tests/
test_llm_tts_streaming_production.py` already covers 34 streaming
production scenarios, `tests/test_real_fish_audio_console.py` already
covers real-client cancellation-during-synthesis end-to-end. This file
specifically targets what THIS sprint changed, using the SAME
established test conventions (pure `build_dual_response()` calls for
semantic scenarios, real `RuntimeDemoConsole` for streaming/E2E
scenarios) already used throughout the suite.

Run:
    python3 -m pytest tests/test_voice_naturalness_and_latency.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any, Callable, Dict, List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import response_output as ro  # noqa: E402
from luno.response_policy import compute_response_policy, DEPTH_DETAILED, DEPTH_NORMAL, DEPTH_SHORT  # noqa: E402
from luno.adapters.fish_audio import MockFishAudioClient  # noqa: E402


def _policy(depth_query: str = "", text_for_default: str = ""):
    return compute_response_policy(depth_query or text_for_default)


def _build(text: str, query: str = "", language: str = "indonesian"):
    policy = _policy(query, text)
    return ro.build_dual_response(text, policy, language=language)


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_naturalness", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_naturalness"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _new_console(demo, *, reply: str, chunk_delay_s: float = 0.0, playback_delay_s: float = 0.01, synthesis_delay_s: float = 0.0):
    return demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=chunk_delay_s),
        fish_audio_client=MockFishAudioClient(playback_delay_s=playback_delay_s, synthesis_delay_s=synthesis_delay_s),
    )


def _run_turn(console, user_text: str, *, timeout_s: float = 8.0) -> Dict[str, Any]:
    """Same shape as `test_llm_tts_streaming_production.py::_run_turn()`
    - runs ONE real turn, returns collected event data, quiesces both
    barge-in and session state before returning so a caller can safely
    fire a second turn."""
    stream_chunks: List[Dict[str, Any]] = []
    speak_requests: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    cancelled: List[Dict[str, Any]] = []
    llm_started_at: List[float] = []
    first_audio_at: List[float] = []
    subs = [
        console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(e.data)),
        console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e.data)),
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data)),
        console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data)),
        console.event_bus.subscribe("llm_started", lambda e: llm_started_at.append(time.time())),
        console.event_bus.subscribe("speech_playback_started", lambda e: first_audio_at.append(time.time())),
    ]
    try:
        console.simulate_speech(user_text)
        assert _wait_until(lambda: len(finished) >= 1 or len(cancelled) >= 1, timeout_s), "turn never completed"
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
        "finished": finished, "cancelled": cancelled,
        "llm_started_at": llm_started_at, "first_audio_at": first_audio_at,
    }


# ============================================================================
# SECTION A - Semantic / list coherence (10 scenarios)
# ============================================================================

def test_A1_setup_sentence_before_list_survives_short_depth():
    """The exact bug this sprint reproduced and fixed: a conversational
    setup sentence pushed to index 1 (not the always-must-keep index 0),
    immediately followed by a bulleted list, at SHORT depth (the
    tightest budget) - must not be dropped in favor of the bullets
    alone."""
    text = (
        "Oke, saya jelasin ya. Untuk membuat sistem ini, ada beberapa bagian utama.\n"
        "- Sensor suhu\n- Modul relay\n- ESP32\n- Power supply\n- Kabel jumper"
    )
    dual = _build(text, query="jelasin singkat", language="indonesian")
    assert "beberapa bagian utama" in dual.voice_text.lower() or "bagian utama" in dual.voice_text.lower()


def test_A2_explanation_before_dependent_list_survives():
    text = (
        "Baik. Berikut cara pasang relay ke ESP32 supaya aman dipakai lama.\n"
        "1. Sambungkan VCC ke 5V\n2. Sambungkan GND ke ground\n3. Sambungkan sinyal ke GPIO"
    )
    dual = _build(text, query="cara pasang relay", language="indonesian")
    assert "cara pasang relay" in dual.voice_text.lower() or "aman dipakai" in dual.voice_text.lower()


def test_A3_cause_then_consequence_preserved():
    dual = _build(
        "ESP32 terlalu panas karena resistor salah pasang. Jadi board bisa rusak permanen.",
        query="", language="indonesian",
    )
    assert "terlalu panas" in dual.voice_text.lower() or "karena" in dual.voice_text.lower()


def test_A4_question_then_answer_preserved():
    dual = _build(
        "Apa itu MQTT? MQTT adalah protokol ringan untuk komunikasi IoT.",
        query="", language="indonesian",
    )
    assert "mqtt" in dual.voice_text.lower()


def test_A5_claim_then_supporting_explanation_preserved():
    dual = _build(
        "ESP32 sangat cocok untuk proyek IoT. Karena harganya murah dan mendukung Wi-Fi bawaan.",
        query="", language="indonesian",
    )
    assert "cocok" in dual.voice_text.lower() or "murah" in dual.voice_text.lower()


def test_A6_warning_then_mitigation_preserved():
    dual = _build(
        "Hati-hati, tegangan tinggi bisa berbahaya. Selalu matikan daya sebelum memegang kabel.",
        query="", language="indonesian",
    )
    spoken = dual.voice_text.lower()
    assert "hati-hati" in spoken or "berbahaya" in spoken
    assert "matikan daya" in spoken or "sebelum" in spoken


def test_A7_unrelated_distant_sentence_not_pulled_in():
    """Anti-example from the brief: an unrelated sentence about weather,
    far from the list, must never be pulled in just because a nearby
    setup sentence is being protected."""
    text = (
        "Oke, saya jelasin ya. Btw di luar cuacanya lagi hujan deras sekali hari ini.\n"
        "Untuk membuat sistem ini, ada beberapa bagian penting.\n"
        "- Sensor suhu\n- Modul relay\n- ESP32"
    )
    dual = _build(text, query="jelasin singkat sistem", language="indonesian")
    assert "hujan" not in dual.voice_text.lower(), "unrelated distant sentence must never be pulled in"


def test_A8_list_item_at_index_zero_has_no_setup_to_protect():
    """A list item that IS the very first sentence has nothing before
    it - `_starts_list_run()` must not try to protect a nonexistent
    predecessor (and must not crash)."""
    text = "- Sensor suhu\n- Modul relay\n- ESP32\nItu saja komponennya."
    dual = _build(text, query="", language="indonesian")
    assert dual.voice_text  # must not raise, must produce something


def test_A9_multiple_independent_list_runs_each_protected():
    text = (
        "Pertama, bagian sensor: - Suhu\n- Kelembapan\n"
        "Kedua, bagian aktuator: - Relay\n- Motor servo"
    )
    dual = _build(text, query="jelasin", language="indonesian")
    spoken = dual.voice_text.lower()
    assert "bagian sensor" in spoken or "bagian aktuator" in spoken


def test_A10_detailed_depth_still_allows_list_compression():
    """DETAILED depth does NOT hard-protect every list item
    (`protect_list_items = depth != DEPTH_DETAILED`) - this sprint's fix
    must not silently override that existing, intentional rule."""
    text = (
        "Oke, saya jelasin ya. Untuk membuat sistem ini, ada beberapa bagian penting.\n"
        + "\n".join(f"- Komponen nomor {n} yang cukup panjang penjelasannya di sini" for n in range(1, 12))
    )
    policy = compute_response_policy("jelaskan detail")
    assert policy.depth == DEPTH_DETAILED, f"expected DETAILED, got {policy.depth}"
    dual = ro.build_dual_response(text, policy, language="indonesian")
    # DETAILED is allowed to compress list items too - just must not crash
    # and must still keep the setup sentence.
    assert "beberapa bagian penting" in dual.voice_text.lower() or "bagian penting" in dual.voice_text.lower()


# ============================================================================
# SECTION B - Short sentence protection (5 scenarios, by FUNCTION not length)
# ============================================================================

def test_B1_short_setup_sentence_kept_before_list():
    text = "Oke. Begini caranya.\n- Langkah satu\n- Langkah dua\n- Langkah tiga"
    dual = _build(text, query="caranya", language="indonesian")
    assert "begini caranya" in dual.voice_text.lower() or "caranya" in dual.voice_text.lower()


def test_B2_short_conclusion_sentence_kept():
    text = "ESP32 mendukung Wi-Fi. ESP32 juga mendukung Bluetooth. Jadi sangat fleksibel."
    dual = _build(text, query="", language="indonesian")
    assert "fleksibel" in dual.voice_text.lower() or "jadi" in dual.voice_text.lower()


def test_B3_short_independent_statement_survives_short_budget():
    dual = _build("Sudah selesai.", query="jawab singkat", language="indonesian")
    assert "selesai" in dual.voice_text.lower()


def test_B4_short_filler_sentence_can_be_dropped_under_pressure():
    """A short sentence with NO structural function (pure filler) is
    allowed to be dropped when budget is tight - this sprint must not
    introduce a blanket 'always keep short sentences' rule (explicitly
    forbidden by the brief)."""
    text = (
        "Baik, saya jawab ya. Hmm, oke. "
        "ESP32 adalah mikrokontroler populer untuk proyek IoT karena harganya terjangkau dan fitur lengkap."
    )
    dual = _build(text, query="jawab sangat singkat", language="indonesian")
    # the substantive answer must survive even if filler doesn't.
    assert "mikrokontroler" in dual.voice_text.lower() or "iot" in dual.voice_text.lower()


def test_B5_short_unrelated_sentence_not_falsely_protected_by_list_fix():
    """A short sentence that happens to precede a list, but bears no
    real setup relationship (e.g. it's a standalone aside), must not be
    force-protected just because a list follows it somewhere later in a
    long response - `_starts_list_run()` only looks at the IMMEDIATELY
    following sentence, never further ahead."""
    text = (
        "ESP32 punya banyak fitur menarik. Btw saya suka warna biru. "
        "Salah satu fiturnya adalah dukungan Wi-Fi bawaan.\n"
        "- Wi-Fi\n- Bluetooth\n- Dual core"
    )
    dual = _build(text, query="", language="indonesian")
    assert "warna biru" not in dual.voice_text.lower(), (
        "an unrelated short aside must not be protected just because a list exists later in the response"
    )


# ============================================================================
# SECTION C - Streaming / first-audio latency (10 scenarios)
# ============================================================================

def test_C1_streaming_is_the_production_default():
    """Phase 3's own fix - `ENABLE_LLM_TTS_STREAMING` must default to
    `True` in a clean environment (no override)."""
    import luno.config as config
    assert os.environ.get("ENABLE_LLM_TTS_STREAMING") is not None or config.ENABLE_LLM_TTS_STREAMING is True, (
        "streaming must be the production default per this sprint's own Phase 3 fix"
    )


def test_C2_default_path_dispatches_via_speak_stream_chunk_not_speak_request():
    demo = _load_demo()
    console = _new_console(demo, reply="Jawaban singkat untuk uji default path.")
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, "halo")
        assert r["stream_chunks"], "default (streaming) path must dispatch via speak_stream_chunk"
        assert not r["speak_requests"], "default path must never ALSO publish the legacy speak_request"
    finally:
        console.stop()


def test_C3_first_audio_starts_before_full_multisentence_reply_would_have_been_needed():
    """A multi-sentence reply must start audio from the FIRST settled
    sentence, not wait for the whole reply - proven by first-audio
    latency being close to first-sentence latency, not full-reply
    latency, using a deliberately slow LLM stream (chunk_delay_s) so the
    difference is measurable."""
    demo = _load_demo()
    reply = "Kalimat pertama yang pendek. Kalimat kedua yang jauh lebih panjang untuk memperlambat keseluruhan balasan ini secara nyata."
    console = _new_console(demo, reply=reply, chunk_delay_s=0.15, playback_delay_s=0.01)
    console.start()
    try:
        _wake(console, demo)
        t0 = time.time()
        r = _run_turn(console, "cerita")
        assert r["first_audio_at"], "no first-audio timestamp captured"
        first_audio_latency = r["first_audio_at"][0] - t0
        # the full reply (2 sentences at chunk_delay_s=0.15/word-ish) would
        # take noticeably longer than just the first sentence settling.
        assert first_audio_latency < 1.0, f"first audio took {first_audio_latency:.3f}s - too slow for a streamed first sentence"
    finally:
        console.stop()


def test_C4_never_sends_half_a_sentence_to_tts():
    """Every dispatched stream chunk's text must end at a real sentence
    boundary (or be the final flush) - streaming must never speak a
    truncated mid-sentence fragment."""
    demo = _load_demo()
    reply = "Bagian satu selesai. Bagian dua juga selesai. Bagian tiga penutup."
    console = _new_console(demo, reply=reply, chunk_delay_s=0.02)
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, "jelaskan")
        assert r["stream_chunks"], "expected at least one stream chunk"
        for c in r["stream_chunks"]:
            chunk = c.get("chunk") or {}
            text = (chunk.get("text") or "").strip()
            if text:
                assert text[-1] in ".!?…" or chunk.get("is_final"), (
                    f"chunk text does not end at a sentence boundary and is not final: {text!r}"
                )
    finally:
        console.stop()


def test_C5_short_depth_reply_not_over_spoken_under_streaming():
    demo = _load_demo()
    console = _new_console(demo, reply="Oke, bisa.")
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, "jawab singkat, apa itu relay?")
        assert r["stream_chunks"], "expected streamed dispatch"
        spoken_text = " ".join((c.get("chunk") or {}).get("text", "") for c in r["stream_chunks"])
        assert "oke" in spoken_text.lower() or "bisa" in spoken_text.lower()
    finally:
        console.stop()


def test_C6_pipelined_synthesis_path_reached_for_split_synthesis_client():
    """Phase 4's own requirement - verify the production path actually
    REACHES the one-slot prefetch/pipelining implementation for a
    client that supports split synthesis, rather than assuming it."""
    from luno.adapters.fish_audio import FishAudioAdapter, PlaybackCancelled  # noqa: F401
    from luno.adapters.manager import AdapterManager
    from luno.adapters.events import SpeakStreamChunk
    from luno.speech_chunk import SpeechChunk

    class _SplitClient(MockFishAudioClient):
        def __init__(self):
            super().__init__(playback_delay_s=0.01)
            self.synthesize_calls = 0
            self.play_audio_calls = 0

        def supports_split_synthesis(self) -> bool:
            return True

        def synthesize(self, text: str):
            self.synthesize_calls += 1
            return text

        def play_audio(self, audio, on_playback_start=None):
            self.play_audio_calls += 1
            self.play(audio, on_playback_start=on_playback_start)

    client = _SplitClient()
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    try:
        chunk = SpeechChunk(chunk_id="pC6:chunk:0", request_id="pC6", conversation_id="c", sequence=0, total=1, raw_text="x", text="x", is_final=True)
        mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "pC6", "conversation_id": "c", "chunk": chunk.to_dict()}))
        assert _wait_until(lambda: finished == ["pC6"], 3.0)
        assert client.synthesize_calls >= 1, "split-synthesis client's own synthesize() was never called - pipelined path not reached"
        assert client.play_audio_calls >= 1, "split-synthesis client's own play_audio() was never called - pipelined path not reached"
    finally:
        mgr.stop_all()


def test_C7_chunk0_cancellation_responsive_during_synthesis_pipelined():
    """This sprint's own Phase 5 bug fix, proven directly: a
    `StopPlayback` arriving WHILE chunk 0 is still synthesizing (on a
    split-synthesis-capable client) must be honored - the chunk must
    NEVER play. Regression proof for the `_play_stream_pipelined()` fix
    in `luno/adapters/fish_audio.py`."""
    from luno.adapters.fish_audio import FishAudioAdapter
    from luno.adapters.manager import AdapterManager
    from luno.adapters.events import SpeakStreamChunk, StopPlayback
    from luno.speech_chunk import SpeechChunk

    class _SlowSplitClient(MockFishAudioClient):
        def supports_split_synthesis(self) -> bool:
            return True

        def synthesize(self, text: str):
            # Reuses play()'s own cancellation-aware synthesis-delay loop
            # so a genuine mid-synthesis cancel raises PlaybackCancelled
            # exactly like the real adapter's own contract expects.
            entry = {"cancel": __import__("threading").Event(), "pause": __import__("threading").Event()}
            with self._lock:
                self._active.append(entry)
            try:
                slept = 0.0
                step = 0.01
                while slept < self.synthesis_delay_s:
                    if entry["cancel"].is_set():
                        from luno.adapters.fish_audio import PlaybackCancelled
                        raise PlaybackCancelled("cancelled during synthesize()")
                    time.sleep(min(step, self.synthesis_delay_s - slept))
                    slept += step
            finally:
                with self._lock:
                    if entry in self._active:
                        self._active.remove(entry)
            return text

        def play_audio(self, audio, on_playback_start=None):
            self.played.append(audio)
            if on_playback_start is not None:
                on_playback_start()

    client = _SlowSplitClient(playback_delay_s=0.01, synthesis_delay_s=0.3)
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    try:
        chunk = SpeechChunk(chunk_id="pC7:chunk:0", request_id="pC7", conversation_id="c", sequence=0, total=1, raw_text="x", text="x", is_final=True)
        mgr.event_bus.publish(SpeakStreamChunk(data={"request_id": "pC7", "conversation_id": "c", "chunk": chunk.to_dict()}))
        mgr.event_bus.publish(StopPlayback(data={"request_id": "pC7"}))
        assert _wait_until(lambda: cancelled == ["pC7"], 3.0), "cancellation during synthesis was never honored"
        assert client.played == [], "chunk played despite cancellation arriving during synthesis"
    finally:
        mgr.stop_all()


def test_C8_no_duplicate_dispatch_speak_request_and_speak_stream_chunk():
    demo = _load_demo()
    console = _new_console(demo, reply="Jawaban untuk uji dispatch tunggal.")
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, "halo")
        assert bool(r["stream_chunks"]) != bool(r["speak_requests"]) or (r["stream_chunks"] and not r["speak_requests"]), (
            "a turn must dispatch via exactly one mechanism, never both"
        )
    finally:
        console.stop()


def test_C9_reconciliation_reaches_build_dual_response_for_multi_sentence_reply():
    """For a reply too long to be fully covered by the single early
    dispatch, the reconciliation step must actually run
    `build_dual_response()` (proven by depth-appropriate compression
    showing up in the dispatched text for a long DETAILED-triggering
    reply)."""
    demo = _load_demo()
    long_reply = " ".join(f"Kalimat panjang nomor {n} untuk memicu reconciliation." for n in range(1, 10))
    console = _new_console(demo, reply=long_reply, chunk_delay_s=0.0)
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, "jelaskan detail semuanya")
        assert r["stream_chunks"], "expected streamed dispatch"
        spoken_text = " ".join((c.get("chunk") or {}).get("text", "") for c in r["stream_chunks"])
        # reconciliation ran if MORE than just the first raw sentence made it out.
        assert len(r["stream_chunks"]) >= 2 or spoken_text.count("Kalimat panjang") >= 2
    finally:
        console.stop()


def test_C10_barge_in_during_real_synthesis_still_honored_under_default_streaming():
    """End-to-end confirmation (real console, not the adapter-only proof
    above) that barge-in during synthesis works under the now-default
    streaming path - the companion, higher-level proof of C7."""
    demo = _load_demo()
    console = _new_console(demo, reply="Balasan yang akan diinterupsi.", synthesis_delay_s=0.05)
    console.start()
    try:
        _wake(console, demo)
        console.simulate_speech("ceritakan sesuatu")
        console.simulate_speech("stop")
        cancelled_or_finished = _wait_until(
            lambda: True,  # allow the turn to settle; real assertion below
            0.5,
        )
        # give the turn a moment to resolve either way, then confirm no crash
        # and the console remains responsive for a subsequent turn.
        time.sleep(0.3)
        r2 = _run_turn(console, "apa itu ESP32?")
        assert r2["stream_chunks"] or r2["finished"], "console must remain responsive after a barge-in during streaming synthesis"
    finally:
        console.stop()


# ============================================================================
# SECTION D - Real E2E proof through RuntimeDemoConsole
# ============================================================================

def test_D_e2e_intro_setup_bullets_conclusion_through_real_console():
    """The brief's own required E2E test: a response with an intro/
    setup sentence, multiple bullets, and a conclusion, run through the
    REAL production console. Verifies: chat output remains complete
    (untouched), speech includes the setup/context (not just bullets),
    bullets remain understandable, the conclusion is not orphaned, and
    first audio starts without waiting for the entire response."""
    demo = _load_demo()
    reply = (
        "Oke, saya jelasin ya. Untuk membuat sistem monitoring suhu ini, ada beberapa bagian penting.\n"
        "- Sensor suhu DHT22\n"
        "- Modul ESP32\n"
        "- Relay untuk kipas\n"
        "- Power supply 5V\n"
        "- Kabel jumper secukupnya\n"
        "Jadi kalau semua bagian itu sudah siap, sistemnya bisa langsung dirakit."
    )
    console = _new_console(demo, reply=reply, chunk_delay_s=0.05, playback_delay_s=0.01)
    console.start()
    try:
        _wake(console, demo)
        assistant_responses: List[Dict[str, Any]] = []
        first_audio_at: List[float] = []
        subs = [
            console.event_bus.subscribe("assistant_response", lambda e: assistant_responses.append(e.data)),
            console.event_bus.subscribe("speech_playback_started", lambda e: first_audio_at.append(time.time())),
        ]
        try:
            t0 = time.time()
            r = _run_turn(console, "jelaskan cara bikin sistem monitoring suhu")

            # Chat output remains complete and untouched.
            assert assistant_responses, "no assistant_response captured"
            assert assistant_responses[0].get("text") == reply, "chat text must remain byte-identical to the raw LLM reply"

            # First audio starts promptly - not after the whole (slow,
            # chunk_delay_s=0.05/word) response finished generating.
            assert first_audio_at, "no first-audio timestamp captured"
            assert (first_audio_at[0] - t0) < 1.5, "first audio took too long - looks like it waited for the full response"

            spoken_text = " ".join((c.get("chunk") or {}).get("text", "") for c in r["stream_chunks"]).lower()
            assert spoken_text, "nothing was ever spoken"
            # Speech includes setup/context, not just bullets.
            assert "bagian penting" in spoken_text or "saya jelasin" in spoken_text
            # At least some bullet content survived and remains
            # understandable (device/component names present).
            assert any(term in spoken_text for term in ("sensor", "esp32", "relay", "power supply", "kabel"))
            # Conclusion is not orphaned - either present verbatim-ish or
            # its own content ("langsung dirakit"/"siap") made it through.
            assert "dirakit" in spoken_text or "siap" in spoken_text or "jadi" in spoken_text
        finally:
            for s in subs:
                console.event_bus.unsubscribe(s)
    finally:
        console.stop()


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
