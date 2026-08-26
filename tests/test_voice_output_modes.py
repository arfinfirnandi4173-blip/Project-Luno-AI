"""
test_voice_output_modes.py
============================

Voice Output Mode sprint - "ALL / SHORT" voice output mode. Tests for
the new `luno/voice_output_mode.py` module (enum/validation/command
matching) and for the ONE change made to the existing
`luno.response_output.build_dual_response()` (an additive
`voice_output_mode` parameter that, for `"ALL"`, bypasses voice
compression entirely - dedup AND priority-selection - while `"SHORT"`
remains byte-identical to this function's pre-existing, unmodified
behavior), plus the runtime-toggle/command-detection wiring added to
`main_runtime_demo.py` (`PlannerBridgeModule.get_voice_output_mode()` /
`.set_voice_output_mode()`, the `response_depth_assigned` event's new
`voice_output_mode` field) and to `luno/incremental_speech.py`
(`StreamingSpeechCoordinator` threading the same field through for the
streaming path).

NO NEW SELECTOR, RANKER, SUMMARIZER, LLM JUDGE, OR EMBEDDING MODEL WAS
INTRODUCED. SHORT reuses the pre-existing `_select_by_priority()` /
`_repair_orphans()` / per-depth budget pipeline, completely untouched.

Sections:
  1. `luno.voice_output_mode` - enum/validation/command-phrase matching.
  2. `build_dual_response()` - ALL vs SHORT, pure function tests
     (brief's own Phase 7 B/C scenarios + Phase 8 edge cases).
  3. Chat integrity - `chat_text` always equals the raw `response_text`,
     in both modes.
  4. TTS integrity - chunk coverage/ordering in both modes.
  5. E2E through the real `RuntimeDemoConsole` - runtime toggle (direct
     call AND spoken command), "next turn" semantics, streaming still
     active in ALL mode, cancellation safety, chat integrity, memory/
     topic-state isolation.
  6. Latency - first-audio / total latency, SHORT vs ALL (Phase 9).

Persistent-state safety: every test in this file runs under
`tests/conftest.py`'s autouse `isolate_persistent_state` fixture - no
test here can ever touch Vinn's real `config/*.json` files.

Run:
    python3 -m pytest -q tests/test_voice_output_modes.py
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

from luno.response_output import build_dual_response  # noqa: E402
from luno.response_policy import ResponsePolicy  # noqa: E402
from luno.voice_output_mode import (  # noqa: E402
    DEFAULT_VOICE_OUTPUT_MODE,
    VOICE_MODE_ALL_PHRASES,
    VOICE_MODE_SHORT_PHRASES,
    VOICE_OUTPUT_MODE_ALL,
    VOICE_OUTPUT_MODE_SHORT,
    is_valid_voice_output_mode,
    match_voice_output_mode_command,
    resolve_voice_output_mode,
)


# ============================================================================
# Section 1 - luno.voice_output_mode: enum / validation / command matching
# ============================================================================

def test_default_mode_is_short():
    assert DEFAULT_VOICE_OUTPUT_MODE == "SHORT"


def test_resolve_valid_values_case_and_whitespace_insensitive():
    assert resolve_voice_output_mode("ALL") == VOICE_OUTPUT_MODE_ALL
    assert resolve_voice_output_mode("all") == VOICE_OUTPUT_MODE_ALL
    assert resolve_voice_output_mode("  All  ") == VOICE_OUTPUT_MODE_ALL
    assert resolve_voice_output_mode("SHORT") == VOICE_OUTPUT_MODE_SHORT
    assert resolve_voice_output_mode("short") == VOICE_OUTPUT_MODE_SHORT


def test_resolve_invalid_or_missing_falls_back_to_short_never_raises():
    for garbage in (None, "", "   ", "banana", "LONG", 42, 3.14, [], {}, object()):
        assert resolve_voice_output_mode(garbage) == DEFAULT_VOICE_OUTPUT_MODE


def test_is_valid_voice_output_mode():
    assert is_valid_voice_output_mode("ALL") is True
    assert is_valid_voice_output_mode("short") is True
    assert is_valid_voice_output_mode("banana") is False
    assert is_valid_voice_output_mode(None) is False
    assert is_valid_voice_output_mode(42) is False


def test_match_all_phrases():
    for phrase in VOICE_MODE_ALL_PHRASES:
        assert match_voice_output_mode_command(phrase) == VOICE_OUTPUT_MODE_ALL, phrase
        # case-insensitive / trailing punctuation tolerant, same as
        # `luno.barge_in.matcher`'s own normalize() contract.
        assert match_voice_output_mode_command(phrase.upper() + "!") == VOICE_OUTPUT_MODE_ALL, phrase


def test_match_short_phrases():
    for phrase in VOICE_MODE_SHORT_PHRASES:
        assert match_voice_output_mode_command(phrase) == VOICE_OUTPUT_MODE_SHORT, phrase
        assert match_voice_output_mode_command(phrase.upper() + "?") == VOICE_OUTPUT_MODE_SHORT, phrase


def test_match_ordinary_utterance_returns_none():
    for text in ("apa kabar", "matikan lampu kamar", "ESP32 pakai INMP441", "berapa harga sepatu"):
        assert match_voice_output_mode_command(text) is None


def test_match_empty_and_none_returns_none():
    assert match_voice_output_mode_command("") is None
    assert match_voice_output_mode_command(None) is None
    assert match_voice_output_mode_command("   ") is None


def test_match_phrase_embedded_in_longer_sentence_still_matches():
    # `barge_in.matcher`'s own " phrase " substring rule - a whole-phrase
    # match even when it is not the ENTIRE utterance.
    assert match_voice_output_mode_command("oke voice semua ya") == VOICE_OUTPUT_MODE_ALL
    assert match_voice_output_mode_command("baiklah mode short saja") == VOICE_OUTPUT_MODE_SHORT


# ============================================================================
# Section 2 - build_dual_response(): ALL vs SHORT
# ============================================================================

_SHORT_POLICY = ResponsePolicy(depth="short", score=10, reasons=[], explicit=False)
_NORMAL_POLICY = ResponsePolicy(depth="normal", score=32, reasons=[], explicit=False)
_DETAILED_POLICY = ResponsePolicy(depth="detailed", score=80, reasons=[], explicit=False)

_LONG_TEXT = (
    "ESP32 mendukung banyak protokol komunikasi. Salah satunya adalah MQTT untuk IoT. "
    "WiFi bawaan ESP32 mendukung koneksi 2.4GHz. Bluetooth juga tersedia di sebagian besar board. "
    "Kamu bisa memakai library PubSubClient untuk MQTT. Ada juga alternatif seperti AsyncMqttClient. "
    "Kalau koneksi WiFi terputus, sebaiknya pakai auto-reconnect. Jangan lupa menutup koneksi dengan benar. "
    "Penting untuk selalu memvalidasi payload sebelum diproses. Secara keseluruhan, ESP32 sangat fleksibel untuk proyek IoT."
)

_LIST_TEXT = (
    "Berikut langkahnya:\n"
    "1. Sambungkan ESP32 ke power.\n"
    "2. Buka Arduino IDE.\n"
    "3. Upload firmware ke board.\n"
    "4. Buka Serial Monitor untuk cek log."
)

_BULLET_TEXT = (
    "Beberapa pilihan mikrofon untuk ESP32:\n"
    "- INMP441 (I2S, digital, kualitas bagus)\n"
    "- MAX9814 (analog, ada AGC)\n"
    "- MAX4466 (analog, murah)"
)

_PARAGRAPH_TEXT = (
    "Paragraf pertama membahas soal daya. ESP32 butuh suplai stabil 3.3V.\n\n"
    "Paragraf kedua membahas soal koneksi. WiFi dan Bluetooth bisa dipakai bersamaan.\n\n"
    "Paragraf ketiga adalah penutup. Semoga membantu proyekmu."
)

_WARNING_TEXT = (
    "ESP32 bisa dipakai untuk kontrol relay. Jangan sambungkan langsung ke tegangan 220V. "
    "Gunakan modul relay dengan optocoupler untuk isolasi. Selalu periksa rating arus relay sebelum dipakai."
)

_CONDITION_TEXT = (
    "Kamu bisa memakai deep sleep untuk hemat baterai. "
    "Kalau hotspot mulai mendekati 90 derajat, sebaiknya periksa aliran udara di sekitar board. "
    "Konfigurasi wake-up bisa lewat timer atau GPIO eksternal."
)


def test_a_default_mode_omitted_equals_short_byte_identical():
    """Default mode is SHORT - a caller that never passes
    `voice_output_mode` at all gets byte-identical output to a caller
    that passes `voice_output_mode="SHORT"` explicitly, and to this
    function's own pre-existing (pre-sprint) behavior."""
    omitted = build_dual_response(_LONG_TEXT, _NORMAL_POLICY)
    explicit_short = build_dual_response(_LONG_TEXT, _NORMAL_POLICY, voice_output_mode="SHORT")
    assert omitted.voice_text == explicit_short.voice_text
    assert omitted.voice_chunks == explicit_short.voice_chunks
    assert omitted.voice_output_mode == "SHORT"
    assert explicit_short.voice_output_mode == "SHORT"


def test_b1_all_mode_short_response_edge_case():
    dual = build_dual_response("Sudah.", _SHORT_POLICY, voice_output_mode="ALL")
    assert dual.voice_text == "Sudah."
    short_dual = build_dual_response("Sudah.", _SHORT_POLICY, voice_output_mode="SHORT")
    assert short_dual.voice_text == "Sudah."


def test_b2_all_mode_long_response_every_sentence_reaches_voice():
    all_dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    short_dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="SHORT")
    # SHORT depth's own budget must have actually compressed something,
    # otherwise this scenario doesn't exercise the bypass at all.
    assert len(short_dual.voice_chunks) < len(all_dual.voice_chunks)
    # Every original sentence's own leading words must appear somewhere
    # in ALL's voice_text, in original order - nothing dropped.
    assert "MQTT" in all_dual.voice_text and "PubSubClient" in all_dual.voice_text
    assert "auto-reconnect" in all_dual.voice_text and "fleksibel" in all_dual.voice_text
    assert all_dual.voice_adapted is False


def test_b3_all_mode_numbered_list_setup_and_all_items():
    dual = build_dual_response(_LIST_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    assert "Berikut langkahnya" in dual.voice_text
    for item in ("Sambungkan ESP32", "Arduino IDE", "Upload firmware", "Serial Monitor"):
        assert item in dual.voice_text, dual.voice_text


def test_c3_short_mode_list_coherence_unchanged():
    """SHORT must be byte-identical to calling build_dual_response()
    with no voice_output_mode at all (the pre-existing, already-tested
    list-coherence behavior is completely untouched)."""
    with_mode = build_dual_response(_LIST_TEXT, _SHORT_POLICY, voice_output_mode="SHORT")
    without_mode = build_dual_response(_LIST_TEXT, _SHORT_POLICY)
    assert with_mode.voice_text == without_mode.voice_text
    assert with_mode.voice_chunks == without_mode.voice_chunks


def test_b4_all_mode_bullet_list_all_items():
    dual = build_dual_response(_BULLET_TEXT, _NORMAL_POLICY, voice_output_mode="ALL")
    for item in ("INMP441", "MAX9814", "MAX4466"):
        assert item in dual.voice_text


def test_b5_all_mode_multiple_paragraphs():
    dual = build_dual_response(_PARAGRAPH_TEXT, _NORMAL_POLICY, voice_output_mode="ALL")
    assert "suplai stabil" in dual.voice_text
    assert "Bluetooth" in dual.voice_text
    assert "proyekmu" in dual.voice_text


def test_b6_all_mode_explanatory_response_and_warnings_intact():
    dual = build_dual_response(_WARNING_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    assert "Jangan sambungkan langsung ke tegangan 220V" in dual.voice_text
    assert "optocoupler" in dual.voice_text
    assert "rating arus relay" in dual.voice_text


def test_b7_all_mode_conditions_intact():
    dual = build_dual_response(_CONDITION_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    assert "derajat" in dual.voice_text and "hotspot" in dual.voice_text
    assert "GPIO eksternal" in dual.voice_text


def test_c_short_mode_condition_dependency_protection_unchanged():
    """SHORT's own soft-conditional / dependency-protection behavior
    (`_repair_orphans()`) is completely untouched - same output with or
    without the new parameter."""
    with_mode = build_dual_response(_CONDITION_TEXT, _SHORT_POLICY, voice_output_mode="SHORT")
    without_mode = build_dual_response(_CONDITION_TEXT, _SHORT_POLICY)
    assert with_mode.voice_text == without_mode.voice_text


def test_d_invalid_mode_falls_back_to_short_never_crashes():
    dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="banana")
    short_dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="SHORT")
    assert dual.voice_output_mode == "SHORT"
    assert dual.voice_text == short_dual.voice_text


def test_d_none_mode_falls_back_to_short():
    dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode=None)
    assert dual.voice_output_mode == "SHORT"


def test_e_empty_response_no_crash_all_mode():
    dual = build_dual_response("", _SHORT_POLICY, voice_output_mode="ALL")
    assert dual.voice_text == ""
    assert dual.chat_text == ""
    assert dual.voice_chunks == []
    assert dual.voice_output_mode == "ALL"


def test_e_empty_response_no_crash_short_mode():
    dual = build_dual_response(None, _SHORT_POLICY, voice_output_mode="SHORT")
    assert dual.voice_text == ""
    assert dual.voice_chunks == []


def test_all_mode_bypasses_dedup_of_near_duplicate_sentences():
    text = (
        "ESP32 mendukung WiFi bawaan yang stabil. "
        "ESP32 mendukung WiFi bawaan yang stabil sekali. "
        "Itu sangat berguna untuk IoT."
    )
    all_dual = build_dual_response(text, _SHORT_POLICY, voice_output_mode="ALL")
    short_dual = build_dual_response(text, _SHORT_POLICY, voice_output_mode="SHORT")
    # SHORT's pre-existing dedup collapses the two near-identical
    # sentences into one; ALL must keep both, unmodified.
    assert all_dual.voice_text.count("WiFi bawaan") == 2
    assert short_dual.voice_text.count("WiFi bawaan") <= 1


def test_all_mode_voice_adapted_is_false():
    dual = build_dual_response(_LONG_TEXT, _DETAILED_POLICY, voice_output_mode="ALL")
    assert dual.voice_adapted is False


def test_short_mode_voice_adapted_matches_pre_existing_behavior():
    with_mode = build_dual_response(_LONG_TEXT, _DETAILED_POLICY, voice_output_mode="SHORT")
    without_mode = build_dual_response(_LONG_TEXT, _DETAILED_POLICY)
    assert with_mode.voice_adapted == without_mode.voice_adapted


# ============================================================================
# Section 3 - Chat integrity: chat_text always equals raw response_text
# ============================================================================

def test_chat_text_equals_raw_in_all_mode():
    for text in (_LONG_TEXT, _LIST_TEXT, _BULLET_TEXT, _PARAGRAPH_TEXT, "Sudah."):
        dual = build_dual_response(text, _SHORT_POLICY, voice_output_mode="ALL")
        assert dual.chat_text == text


def test_chat_text_equals_raw_in_short_mode():
    for text in (_LONG_TEXT, _LIST_TEXT, _BULLET_TEXT, _PARAGRAPH_TEXT, "Sudah."):
        dual = build_dual_response(text, _SHORT_POLICY, voice_output_mode="SHORT")
        assert dual.chat_text == text


# ============================================================================
# Section 4 - TTS integrity
# ============================================================================

def test_all_mode_voice_chunks_cover_every_sentence():
    dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    # 10 sentences in _LONG_TEXT - every one must appear somewhere across
    # the chunk sequence (list items would be grouped, but this text has
    # none, so 1 chunk == 1 sentence here).
    joined = " ".join(dual.voice_chunks)
    for fragment in ("MQTT", "WiFi", "Bluetooth", "PubSubClient", "AsyncMqttClient",
                      "auto-reconnect", "menutup koneksi", "memvalidasi payload", "fleksibel"):
        assert fragment in joined, fragment


def test_short_mode_voice_chunks_are_selected_subset():
    all_dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    short_dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="SHORT")
    assert len(short_dual.voice_chunks) < len(all_dual.voice_chunks)


def test_chunk_ordering_unchanged_in_all_mode():
    dual = build_dual_response(_LONG_TEXT, _SHORT_POLICY, voice_output_mode="ALL")
    joined = " ".join(dual.voice_chunks)
    assert joined.index("MQTT") < joined.index("PubSubClient") < joined.index("fleksibel")


# ============================================================================
# Section 5 - E2E through the real RuntimeDemoConsole
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_voice_modes", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_voice_modes"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _new_console(demo, *, reply: str, chunk_delay_s: float = 0.0, playback_delay_s: float = 0.01):
    from luno.adapters.fish_audio import MockFishAudioClient
    return demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=chunk_delay_s),
        fish_audio_client=MockFishAudioClient(playback_delay_s=playback_delay_s),
    )


def _run_turn(console, demo, user_text: str, *, timeout_s: float = 8.0) -> Dict[str, Any]:
    """Mirrors `tests/test_llm_tts_streaming_production.py::_run_turn` -
    wakes (if needed), runs ONE full turn, waits for BOTH barge-in and
    session-manager to settle so a caller can immediately fire another
    turn (multi-turn toggle tests need this)."""
    from luno.wake_session import ConversationState
    stream_chunks: List[Dict[str, Any]] = []
    speak_requests: List[Dict[str, Any]] = []
    assistant_responses: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    started: List[Dict[str, Any]] = []
    depth_events: List[Dict[str, Any]] = []
    subs = [
        console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(e.data)),
        console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e.data)),
        console.event_bus.subscribe("assistant_response", lambda e: assistant_responses.append(e.data)),
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data)),
        console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.data)),
        console.event_bus.subscribe("response_depth_assigned", lambda e: depth_events.append(e.data)),
    ]
    try:
        console.simulate_speech(user_text)
        _wait_until(lambda: len(finished) >= 1 or len(assistant_responses) >= 1, timeout_s)
        _wait_until(lambda: len(finished) >= 1, timeout_s)
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
        "started": started, "depth_events": depth_events,
    }


def _spoken_text(result: Dict[str, Any]) -> str:
    if result["speak_requests"]:
        return result["speak_requests"][-1].get("text", "")
    if result["stream_chunks"]:
        ordered = sorted(result["stream_chunks"], key=lambda c: c.get("chunk", {}).get("sequence", 0))
        return " ".join(c.get("chunk", {}).get("text", "") for c in ordered)
    return ""


_E2E_LONG_REPLY = (
    "ESP32 mendukung WiFi dan Bluetooth sekaligus. MQTT adalah pilihan populer untuk IoT. "
    "Kamu bisa pakai broker gratis seperti Mosquitto. Banyak tutorial membahas setup dari nol. "
    "QoS penting untuk memastikan pesan sampai. Selalu tutup koneksi dengan benar setelah selesai."
)


def test_e2e_default_mode_is_short_on_fresh_console():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        assert r["assistant_responses"][0].get("text") == _E2E_LONG_REPLY  # chat: full, untouched
        spoken = _spoken_text(r)
        assert spoken != _E2E_LONG_REPLY  # voice: compressed (default SHORT)
        conv_id = console.behavior_tree_module.conversation_id
        assert console.planner_module.get_voice_output_mode(conv_id) == "SHORT"
    finally:
        console.stop()


def test_e2e_direct_api_switch_to_all_full_response_reaches_voice():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        resolved = console.planner_module.set_voice_output_mode(conv_id, "ALL")
        assert resolved == "ALL"
        assert console.planner_module.get_voice_output_mode(conv_id) == "ALL"
        r = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        assert r["assistant_responses"][0].get("text") == _E2E_LONG_REPLY
        spoken = _spoken_text(r)
        assert "Mosquitto" in spoken and "QoS" in spoken and "tutup koneksi" in spoken
    finally:
        console.stop()


def test_e2e_spoken_command_switches_mode_for_next_turn_only():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        assert console.planner_module.get_voice_output_mode(conv_id) == "SHORT"

        # Turn 1: the command utterance itself. Mode must NOT yet be ALL
        # for THIS turn's own (short, canned-echo) reply, but must become
        # ALL for the NEXT turn.
        r1 = _run_turn(console, demo, "voice semua")
        assert console.planner_module.get_voice_output_mode(conv_id) == "ALL"
        # Phase 5 - the command turn itself is forced to SHORT depth so
        # its own confirmation is never read as a long response.
        assert r1["depth_events"] and r1["depth_events"][-1].get("depth") == "short"

        # Turn 2: mode is now ALL - full reply must reach voice.
        r2 = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        spoken2 = _spoken_text(r2)
        assert "Mosquitto" in spoken2 and "QoS" in spoken2

        # Turn 3: switch back via spoken command.
        r3 = _run_turn(console, demo, "mode short")
        assert console.planner_module.get_voice_output_mode(conv_id) == "SHORT"

        # Turn 4: back to compressed.
        r4 = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        spoken4 = _spoken_text(r4)
        assert spoken4 != _E2E_LONG_REPLY
    finally:
        console.stop()


def test_e2e_multiple_consecutive_toggles_stay_consistent():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        sequence = ["ALL", "SHORT", "ALL", "ALL", "SHORT"]
        for mode in sequence:
            console.planner_module.set_voice_output_mode(conv_id, mode)
            assert console.planner_module.get_voice_output_mode(conv_id) == mode
        r = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        spoken = _spoken_text(r)
        assert spoken != _E2E_LONG_REPLY  # final state was SHORT
    finally:
        console.stop()


def test_e2e_chat_integrity_across_both_modes():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        for mode in ("SHORT", "ALL"):
            console.planner_module.set_voice_output_mode(conv_id, mode)
            r = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
            assert r["assistant_responses"][-1].get("text") == _E2E_LONG_REPLY, mode
    finally:
        console.stop()


def test_e2e_streaming_stays_active_in_all_mode():
    """Phase 2's explicit "jangan bypass streaming" - ALL must still
    dispatch via `speak_stream_chunk` (streaming defaults ON - see
    `luno.config.ENABLE_LLM_TTS_STREAMING`), never fall back to a single
    legacy `speak_request` merely because compression was skipped."""
    demo = _load_demo()
    assert demo.legacy_config.ENABLE_LLM_TTS_STREAMING is True
    console = _new_console(demo, reply=_E2E_LONG_REPLY, chunk_delay_s=0.01)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        r = _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        assert r["stream_chunks"], "ALL mode must not disable streaming"
        assert not r["speak_requests"], "streaming enabled - legacy path must not also fire"
    finally:
        console.stop()


def test_e2e_cancellation_during_all_mode_playback():
    demo = _load_demo()
    console = _new_console(demo, reply="Kalimat panjang sekali yang seharusnya bisa dibatalkan di tengah jalan saat sedang diputar penuh.", chunk_delay_s=0.0, playback_delay_s=0.3)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        started: List[Any] = []
        cancelled: List[Any] = []
        sub1 = console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e))
        sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
        try:
            console.simulate_speech("ceritakan sesuatu yang sangat panjang")
            assert _wait_until(lambda: len(started) >= 1, 3.0)
            console.simulate_speech("stop")
            assert _wait_until(lambda: len(cancelled) >= 1, 3.0)
        finally:
            console.event_bus.unsubscribe(sub1)
            console.event_bus.unsubscribe(sub2)
    finally:
        console.stop()


def test_e2e_memory_and_topic_state_untouched_by_mode_switch():
    """Phase 4/Phase 10's explicit "tidak mengubah memory state / topic
    history" - a direct `set_voice_output_mode()` call must never touch
    any OTHER per-conversation dict."""
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        _run_turn(console, demo, "ESP32 pakai INMP441 buat voice assistant")
        active_topic_before = dict(console.planner_module._active_topic)
        topic_history_before = dict(console.planner_module._topic_history)
        depth_pref_before = dict(console.planner_module._depth_preference)

        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        console.planner_module.set_voice_output_mode(conv_id, "SHORT")

        assert dict(console.planner_module._active_topic) == active_topic_before
        assert dict(console.planner_module._topic_history) == topic_history_before
        assert dict(console.planner_module._depth_preference) == depth_pref_before
    finally:
        console.stop()


def test_e2e_new_conversation_does_not_inherit_prior_mode():
    """Phase 4's "tidak bocor antar user/session" - `_on_conversation_ended()`
    must pop this conversation's mode entry."""
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        assert console.planner_module.get_voice_output_mode(conv_id) == "ALL"

        from luno.core.events import Event
        console.event_bus.publish(Event(type="conversation_ended", data={"session_id": conv_id, "reason": "test"}))
        assert _wait_until(lambda: conv_id not in console.planner_module._voice_output_mode, 3.0)
        assert console.planner_module.get_voice_output_mode(conv_id) == "SHORT"
    finally:
        console.stop()


def test_e2e_debug_status_snapshot_exposes_last_spoken_mode():
    demo = _load_demo()
    console = _new_console(demo, reply=_E2E_LONG_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        assert console.behavior_tree_module.status_snapshot()["last_voice_output_mode"] is None
        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        _run_turn(console, demo, "jelaskan tentang MQTT di ESP32")
        assert console.behavior_tree_module.status_snapshot()["last_voice_output_mode"] == "ALL"
    finally:
        console.stop()


# ============================================================================
# Section 6 - Latency (Phase 9)
# ============================================================================

def test_all_mode_first_audio_latency_not_significantly_worse_than_short():
    """Streaming dispatches the SAME one, always-safe lead sentence
    immediately regardless of mode (see `luno/incremental_speech.py`'s
    own "RESPONSE-DEPTH-POLICY-SAFE REDESIGN" - untouched by this
    sprint) - so first-audio latency must be comparable between modes.
    Never compares total speech DURATION (ALL naturally takes longer to
    finish speaking more content) - only time-to-first-chunk."""
    demo = _load_demo()

    def _first_chunk_latency(mode: str) -> float:
        console = _new_console(demo, reply=_E2E_LONG_REPLY, chunk_delay_s=0.01, playback_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            conv_id = console.behavior_tree_module.conversation_id
            console.planner_module.set_voice_output_mode(conv_id, mode)
            first_chunk_at: List[float] = []
            t0 = {}
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: first_chunk_at.append(time.time()))
            try:
                t0["t"] = time.time()
                console.simulate_speech("jelaskan tentang MQTT di ESP32")
                assert _wait_until(lambda: len(first_chunk_at) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            return first_chunk_at[0] - t0["t"]
        finally:
            console.stop()

    short_latency = _first_chunk_latency("SHORT")
    all_latency = _first_chunk_latency("ALL")
    # Generous bound (this is a mocked, near-instant backend - real
    # hardware would use a proportionally larger tolerance) - the point
    # is "not a multi-second regression", not "identical to the
    # millisecond".
    assert all_latency < short_latency + 1.0, (short_latency, all_latency)
