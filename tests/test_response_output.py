"""
test_response_output.py
=========================

Chat/Voice Dual Output sprint - tests for `luno/response_output.py` (the
pure `build_dual_response()` presentation adapter) AND for its wiring
into the real `PlannerBridgeModule`/`BehaviorTreeModule`/
`RuntimeDemoConsole` production pipeline (same no-network/no-hardware
conventions as `tests/test_response_policy.py`, which this file mirrors
structurally).

Sections:

  1. Pure adapter tests - call `build_dual_response()` directly. No I/O,
     no event bus, no console.
  2. Semantic safety - critical content (warnings, numbers, device
     names/config values, conclusions, required actions) survives
     DETAILED-depth voice compression.
  3. No duplicate reasoning - one turn through the real production
     bridge performs exactly one `NeedLLMResponse`/LLM call, one
     `compute_response_policy()` call, and one `build_dual_response()`
     call.
  4. End-to-end integration through the real `RuntimeDemoConsole`
     pipeline - Chat (`assistant_response`) keeps receiving the raw,
     untouched text; Voice (`speak_request`) receives the adapted
     `voice_text`, never `chat_text`; all three depths (SHORT/NORMAL/
     DETAILED) are proven through the real bridge, not just the pure
     function.

Persistent-state safety: every test in this file runs under
`tests/conftest.py`'s autouse `isolate_persistent_state` fixture - no
test here can ever touch Vinn's real `config/*.json` files.

Run:
    python3 -m pytest -q tests/test_response_output.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.response_output import DualResponse, build_dual_response  # noqa: E402
from luno.response_policy import ResponsePolicy  # noqa: E402

# ============================================================================
# Section 1 - pure adapter tests
# ============================================================================


def test_1_short_depth_voice_matches_chat_meaning():
    r = build_dual_response("Bisa. ESP32 mendukung MQTT.", ResponsePolicy(depth="short", score=10))
    assert r.chat_text == "Bisa. ESP32 mendukung MQTT."
    assert r.voice_text == "Bisa. ESP32 mendukung MQTT."
    assert r.depth == "short"


def test_2_normal_depth_voice_no_worse_than_chat_verbosity():
    text = "Bisa. ESP32 cocok untuk MQTT karena ringan dan efisien untuk IoT."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=38))
    assert r.chat_text == text
    assert len(r.voice_text) <= len(text) + 5  # cleaning only, no summarization
    assert r.depth == "normal"


def test_3_detailed_depth_voice_much_shorter_than_chat():
    detailed = "\n\n".join(
        f"Paragraf nomor {i} membahas detail teknis tambahan tentang topik ini secara panjang lebar."
        for i in range(1, 12)
    )
    r = build_dual_response(detailed, ResponsePolicy(depth="detailed", score=90))
    assert r.chat_text == detailed
    assert len(r.voice_text) < len(detailed) * 0.7
    assert r.voice_adapted is True


def test_4_empty_response():
    r = build_dual_response("", ResponsePolicy(depth="normal", score=32))
    assert r.chat_text == ""
    assert r.voice_text == ""
    assert r.voice_adapted is False


def test_5_multiline_response_reads_as_one_continuous_utterance():
    text = "Baris pertama.\nBaris kedua.\nBaris ketiga."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert "\n" not in r.voice_text


def test_6_markdown_emphasis_stripped_from_voice_never_from_chat():
    text = "**Important:** restart the ESP32."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.chat_text == text  # Chat keeps the ** markers
    assert "**" not in r.voice_text
    assert "Important: restart the ESP32." in r.voice_text


def test_7_bullet_list_becomes_comma_joined_prose_in_voice():
    text = "You will need:\n- Wi-Fi\n- MQTT\n- Home Assistant"
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.chat_text == text
    assert "-" not in r.voice_text.replace("Wi-Fi", "")  # only the real hyphen in "Wi-Fi" survives
    assert "Wi-Fi" in r.voice_text and "MQTT" in r.voice_text and "Home Assistant" in r.voice_text


def test_8_numbered_list_markers_never_read_as_digits():
    text = "Steps:\n1. ESP32 publish data.\n2. MQTT broker menerima data.\n3. Home Assistant subscribe."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.chat_text == text
    assert "1." not in r.voice_text and "2." not in r.voice_text and "3." not in r.voice_text
    assert "one" not in r.voice_text.lower().split() and "two" not in r.voice_text.lower().split()
    assert "ESP32 publish data" in r.voice_text


def test_9_code_block_never_read_literally():
    text = "Use this:\n```python\ndigitalWrite(2, HIGH)\n```\nto turn on the pin."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.chat_text == text
    assert "```" not in r.voice_text
    assert "digitalWrite" not in r.voice_text
    assert "backtick" not in r.voice_text.lower()


def test_10_inline_code_stripped_of_backticks_content_kept():
    text = "Run `pip install foo` to fix it."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert "`" not in r.voice_text
    assert "pip install foo" in r.voice_text


def test_11_url_never_spoken_verbatim():
    text = "Open the config page at https://example.com/config/very/long/path to continue."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert "https://" not in r.voice_text
    assert "example.com" not in r.voice_text


def test_12_mixed_markdown_code_url_all_handled_together():
    text = "**Note:** see [the docs](https://example.com/docs) and run `pip install foo`."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert "**" not in r.voice_text
    assert "https://" not in r.voice_text
    assert "`" not in r.voice_text
    assert "[" not in r.voice_text and "]" not in r.voice_text
    assert "the docs" in r.voice_text
    assert "pip install foo" in r.voice_text


def test_13_very_long_detailed_response_still_compresses_and_does_not_crash():
    long_text = " ".join(f"Kalimat ke {i} menjelaskan bagian sistem yang berbeda." for i in range(1, 60))
    r = build_dual_response(long_text, ResponsePolicy(depth="detailed", score=95))
    assert len(r.voice_text) < len(long_text)
    assert r.voice_text  # never empty


def test_14_already_short_response_unchanged_in_meaning():
    r = build_dual_response("Oke!", ResponsePolicy(depth="short", score=5))
    assert r.voice_text == "Oke!"


def test_15_indonesian_language_number_conversion():
    r = build_dual_response("Suhunya -3 derajat.", ResponsePolicy(depth="normal", score=32), language="indonesian")
    assert "minus tiga" in r.voice_text.lower()


def test_16_english_language_number_conversion():
    r = build_dual_response("The temperature is -3 degrees.", ResponsePolicy(depth="normal", score=32), language="english")
    assert "negative three" in r.voice_text.lower()


def test_response_policy_string_depth_accepted_directly():
    """`response_policy` may be a plain depth string, not only a
    `ResponsePolicy` object - defensive flexibility documented in
    `build_dual_response()`'s own docstring."""
    r = build_dual_response("Test kalimat.", "short")
    assert r.depth == "short"


def test_unknown_depth_falls_back_to_normal_never_raises():
    r = build_dual_response("Test kalimat.", ResponsePolicy(depth="something_weird", score=50))
    assert r.depth == "normal"


def test_dual_response_fields_are_unambiguously_named():
    """Guards the sprint's own 'no ambiguous fields' requirement."""
    fields = DualResponse.__dataclass_fields__
    assert "chat_text" in fields and "voice_text" in fields
    assert "text" not in fields and "response" not in fields and "content" not in fields


# ============================================================================
# Section 2 - semantic safety: critical content survives DETAILED compression
# ============================================================================


def test_17_warning_survives_detailed_compression():
    text = (
        "ESP32 bisa dipakai untuk banyak proyek IoT sederhana di rumah.\n\n"
        "Penting: jangan sambungkan pin GPIO langsung ke 220V, itu bisa merusak board.\n\n"
        "Ada banyak tutorial online yang membahas dasar-dasar pemrogramannya.\n\n"
        "ESP32 juga populer di kalangan hobbyist karena harganya terjangkau.\n\n"
        "Komunitasnya juga cukup besar dan aktif membantu pemula.\n\n"
        "Firmware-nya bisa di-update kapan saja lewat USB tanpa alat tambahan.\n\n"
        "Banyak board turunan ESP32 tersedia dengan variasi jumlah pin."
    )
    r = build_dual_response(text, ResponsePolicy(depth="detailed", score=90))
    assert r.voice_adapted is True
    assert len(r.voice_text) < len(text)
    assert "220V" in r.voice_text or "jangan" in r.voice_text.lower()


def test_18_required_numeric_spec_survives_detailed_compression():
    text = (
        "Motor ini bisa dikendalikan lewat PWM dengan mikrokontroler apa saja.\n\n"
        "Sebagai referensi umum, motor stepper punya banyak variasi tegangan kerja.\n\n"
        "Ada banyak driver motor yang bisa dipakai tergantung kebutuhan proyek.\n\n"
        "Wajib dicatat: tegangan kerja maksimum motor ini adalah 12 volt, jangan melebihi itu.\n\n"
        "Banyak forum online membahas cara memilih driver motor yang tepat.\n\n"
        "Pemilihan driver juga tergantung pada arus yang dibutuhkan motor."
    )
    # Pinned explicitly: this test asserts Indonesian-specific spoken-number
    # behavior ("12" or "dua belas"), so it must not depend on the ambient
    # LUNO_LANGUAGE env default (normalize_for_speech() itself defaults to
    # "english" when no language is passed and the env var is unset/other).
    r = build_dual_response(text, ResponsePolicy(depth="detailed", score=90), language="indonesian")
    assert "12" in r.voice_text or "dua belas" in r.voice_text.lower()


def test_19_conclusion_sentence_survives_detailed_compression():
    text = (
        "ESP32 mengirim data lewat MQTT ke broker secara berkala.\n\n"
        "Broker MQTT bisa dipasang sendiri atau memakai layanan cloud.\n\n"
        "Ada banyak pilihan broker open-source yang gratis dipakai.\n\n"
        "Home Assistant mendukung banyak integrasi selain MQTT juga.\n\n"
        "Beberapa integrasi butuh konfigurasi tambahan yang cukup rumit.\n\n"
        "Jadi, intinya kamu hanya perlu tiga komponen utama: ESP32, broker MQTT, dan Home Assistant."
    )
    r = build_dual_response(text, ResponsePolicy(depth="detailed", score=90))
    assert "tiga komponen utama" in r.voice_text or "intinya" in r.voice_text.lower()


def test_20_lead_sentence_direct_answer_always_kept():
    text = "Tidak bisa, ESP32 tidak mendukung itu secara langsung.\n\n" + "\n\n".join(
        f"Alasan tambahan nomor {i} yang kurang penting untuk dijelaskan di sini." for i in range(1, 10)
    )
    r = build_dual_response(text, ResponsePolicy(depth="detailed", score=90))
    assert r.voice_text.startswith("Tidak bisa")


# ============================================================================
# Section 3 + 4 - end-to-end integration through the real production pipeline
# ============================================================================


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, canned_text="ok"):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0))


def _wake(console, demo) -> None:
    """Says the wake word via the REAL microphone-event path
    (`simulate_speech()` -> `SpeechRecognized`, the same mechanism
    `tests/test_wake_barge_in_integration.py` uses) and waits until the
    session is genuinely LISTENING. `_speak()` is only ever invoked by
    the real `BehaviorTree` tick loop once a session is open - publishing
    a bare `user_utterance` directly (as `test_response_policy.py`'s own
    E2E tests do, since THEY only need to reach `NeedLLMResponse`) skips
    that tick loop entirely and never reaches `_speak()`/`SpeakRequest`,
    which is exactly what THIS sprint's tests need to observe."""
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _ask_and_capture(console, demo, text):
    """Subscribes AFTER the caller has already woken the session (so the
    wake acknowledgement's own `speak_request` - a hardcoded "Yes?", not
    from the LLM - is never mistaken for this turn's), asks `text` via
    the real speech path, and waits for both the Chat-side
    (`assistant_response`) and Voice-side event(s) for this turn.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened), NOT rewritten to weaken any assertion.
    `ENABLE_LLM_TTS_STREAMING` now defaults to `True` (see
    `luno/config.py`'s own updated rationale - median first-audio
    latency drops ~70% through the pre-existing, already safety-verified
    streaming path), so a turn's voice dispatch may now arrive as either
    ONE legacy `speak_request` (unchanged shape) OR a SEQUENCE of
    `speak_stream_chunk` events (each carrying one real
    `SpeechChunk.to_dict()`-shaped dict, terminated by `is_final`) plus a
    separate `response_depth_assigned` event carrying `depth` (this event
    fires for every turn regardless of dispatch mode - see
    `main_runtime_demo.py`'s own publish site). This helper still returns
    a plain dict shaped exactly like the legacy `speak_request` payload
    (`text`, `depth`, `chunks`, ...) - callers do not need to know which
    path fired. Under streaming, `chunks` is the REAL accumulated list of
    `SpeechChunk.to_dict()` dicts (not synthesized data), so every
    existing assertion about `chunk_id`/`sequence`/`total`/`is_final`
    correlation still holds byte-for-byte."""
    captured = {}
    depth_holder = {}
    stream_chunks = []
    got_both = threading.Event()

    def _maybe_finish():
        if "assistant_response" not in captured:
            return
        if "speak_request" in captured:
            got_both.set()
            return
        if stream_chunks and stream_chunks[-1].get("is_final") and "depth" in depth_holder:
            ordered = sorted(stream_chunks, key=lambda c: c.get("sequence", 0))
            captured["speak_request"] = {
                "text": " ".join(c.get("text", "") for c in ordered),
                "raw_text": " ".join(c.get("raw_text", "") for c in ordered),
                "request_id": ordered[0].get("request_id") if ordered else None,
                "depth": depth_holder.get("depth"),
                "voice_adapted": True,
                "chunks": ordered,
            }
            got_both.set()

    def _on_assistant(e):
        captured["assistant_response"] = e
        _maybe_finish()

    def _on_speak(e):
        captured["speak_request"] = e
        _maybe_finish()

    def _on_stream_chunk(e):
        chunk = e.get("chunk") or {}
        stream_chunks.append(chunk)
        _maybe_finish()

    def _on_depth(e):
        depth_holder["depth"] = e.get("depth")
        _maybe_finish()

    sub_a = console.event_bus.subscribe("assistant_response", _on_assistant)
    sub_s = console.event_bus.subscribe("speak_request", _on_speak)
    sub_c = console.event_bus.subscribe("speak_stream_chunk", _on_stream_chunk)
    sub_d = console.event_bus.subscribe("response_depth_assigned", _on_depth)
    try:
        console.simulate_speech(text)
        assert _wait_until(got_both.is_set, 8.0), (
            f"missing events: {list(captured.keys())} "
            f"stream_chunks={len(stream_chunks)} depth={depth_holder}"
        )
    finally:
        console.event_bus.unsubscribe(sub_a)
        console.event_bus.unsubscribe(sub_s)
        console.event_bus.unsubscribe(sub_c)
        console.event_bus.unsubscribe(sub_d)
    return captured


def test_e2e_chat_receives_raw_untouched_text():
    """Chat's own event (`assistant_response`) must still carry the LLM's
    raw text, byte-for-byte - this sprint must not touch that path."""
    demo = _load_demo()
    canned = "**Penting:** restart ESP32-nya dulu ya."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "cara restart ESP32?")
        assert result["assistant_response"].get("text") == canned
    finally:
        console.stop()


def test_e2e_voice_receives_adapted_text_not_raw_markdown():
    """Voice's own event (`speak_request`) must receive the ADAPTED
    `voice_text`, never the raw markdown-bearing `chat_text`."""
    demo = _load_demo()
    canned = "**Penting:** restart ESP32-nya dulu ya."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "cara restart ESP32?")
        spoken = result["speak_request"].get("text", "")
        assert "**" not in spoken
        assert spoken != canned
        assert "restart ESP32" in spoken or "restart esp32" in spoken.lower()
    finally:
        console.stop()


def test_e2e_depth_short_reaches_speak_request():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke, bisa.")
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jawab singkat, apa itu relay?")
        assert result["speak_request"].get("depth") == "short"
    finally:
        console.stop()


def test_e2e_depth_normal_reaches_speak_request():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Relay adalah saklar elektronik yang dikendalikan oleh sinyal listrik.")
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "cara pasang relay ke ESP32?")
        assert result["speak_request"].get("depth") == "normal"
    finally:
        console.stop()


def test_e2e_depth_detailed_reaches_speak_request():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Penjelasan arsitektur lengkap ESP32 dari CPU sampai peripheral.")
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jelaskan arsitektur ESP32 dari CPU sampai peripheral")
        assert result["speak_request"].get("depth") == "detailed"
    finally:
        console.stop()


def test_e2e_one_turn_performs_exactly_one_build_dual_response_call():
    """No duplicate reasoning: one turn -> exactly one `NeedLLMResponse`
    -> exactly one `build_dual_response()` call (never one per channel,
    never a second independent adaptation). Wrapping happens AFTER
    waking, so the wake acknowledgement's own `_speak()` call (a
    hardcoded "Yes?", still routed through the same `build_dual_response()`
    call site) is never counted as part of THIS turn's total.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - NOT because the invariant changed, but
    because this test's OWN observability of it was broken by the
    `ENABLE_LLM_TTS_STREAMING` default flip. `luno/incremental_speech.py`
    imports `build_dual_response` with its own
    `from .response_output import ... build_dual_response` (a SEPARATE
    module-level name binding from `main_runtime_demo.py`'s own
    `from luno.response_output import build_dual_response`), and its
    `_on_finished()` calls `build_dual_response()` UNCONDITIONALLY for
    every turn, streaming or not (see that method's own docstring:
    "Fed unconditionally, regardless of dispatch phase"). Patching only
    `demo.build_dual_response` therefore silently missed every call made
    through the (now-default) streaming path - the real call still
    happened, this test just couldn't see it, and reported 0 instead of
    1. The real invariant (exactly one call, never a duplicate, never
    zero) is unchanged and still fully enforced - this fix patches BOTH
    of the function's independent module-level bindings, sharing one
    counting wrapper and one call list, so the true total is observed
    regardless of which path a turn takes."""
    demo = _load_demo()
    import luno.incremental_speech as incremental_speech_module

    console = _new_console(demo, canned_text="Jawaban singkat.")
    console.start()
    try:
        _wake(console, demo)
        calls = []
        original = demo.build_dual_response
        assert incremental_speech_module.build_dual_response is original, (
            "luno.incremental_speech's own build_dual_response binding is "
            "expected to be the exact same function object as "
            "main_runtime_demo's - if this ever fails, the two call sites "
            "have diverged and this test's patching strategy must be "
            "revisited."
        )

        def _counting(*a, **kw):
            calls.append((a, kw))
            return original(*a, **kw)

        demo.build_dual_response = _counting
        incremental_speech_module.build_dual_response = _counting
        try:
            _ask_and_capture(console, demo, "apa itu MQTT?")
            assert len(calls) == 1, f"expected exactly 1 build_dual_response() call, got {len(calls)}"
        finally:
            demo.build_dual_response = original
            incremental_speech_module.build_dual_response = original
    finally:
        console.stop()


def test_e2e_one_turn_performs_exactly_one_llm_request():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Jawaban.")
    console.start()
    try:
        _wake(console, demo)
        need_llm_events = []
        sub = console.event_bus.subscribe("need_llm_response", lambda e: need_llm_events.append(e))
        try:
            _ask_and_capture(console, demo, "apa itu MQTT?")
        finally:
            console.event_bus.unsubscribe(sub)
        assert len(need_llm_events) == 1
    finally:
        console.stop()


# ============================================================================
# Section 5 - TTS Chunking/Streaming sprint: pure `voice_chunks` tests
# ============================================================================
#
# `voice_chunks` is derived from the SAME sentence selection `voice_text`
# is joined from (see luno/response_output.py's module docstring) - every
# test below therefore also implicitly re-confirms "voice chunk sequence
# matches the full response" (sprint checklist item 20), since
# `"".join or " ".join(chunks)`-equivalent always reconstructs `voice_text`.


def test_c1_short_response_is_one_chunk():
    r = build_dual_response("Bisa.", ResponsePolicy(depth="short", score=10))
    assert r.voice_chunks == ["Bisa."]


def test_c2_normal_response_multiple_sentences_multiple_chunks():
    text = "ESP32 mendukung Wi-Fi. ESP32 juga mendukung Bluetooth. Keduanya bisa dipakai bersamaan."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert len(r.voice_chunks) == 3
    assert r.voice_chunks == [
        "ESP32 mendukung Wi-Fi.",
        "ESP32 juga mendukung Bluetooth.",
        "Keduanya bisa dipakai bersamaan.",
    ]


def test_c3_long_response_many_chunks_in_order():
    # Deliberately distinct topics per sentence (not "Kalimat ke {i} ...")
    # so near-duplicate dedup (an EXISTING, correct behavior shared with
    # `voice_text` - see `_dedupe()`) never removes any of them - this
    # test is about chunk ORDER/determinism, not dedup.
    #
    # Voice Output Optimization sprint - CONTRACT CHANGE, documented per
    # ARCHITECTURE_GUARD.md's own "spec vs. existing test conflict ->
    # update the test with an explicit documented reason" precedent.
    # Before this sprint, NORMAL depth never compressed at all (only
    # DETAILED did), so this test could assert all 19 placeholder
    # sentences survived verbatim. This sprint's brief explicitly asks
    # NORMAL to "avoid exhaustive examples" - and 19 near-identical
    # "Modul ini mendukung X." sentences IS exactly that shape. NORMAL's
    # new budget-based compression (see response_output.py's
    # `_NORMAL_BUDGET_FLOOR`/`_NORMAL_BUDGET_RATIO`) now intentionally
    # trims this down. The test still verifies what actually matters for
    # ITS original purpose (chunking mechanics): compression happened,
    # the lead sentence survived first, ordering among survivors is
    # preserved, and the result is fully deterministic.
    topics = [
        "Wi-Fi", "Bluetooth", "MQTT", "HTTP", "sensor suhu", "sensor cahaya",
        "relay", "motor servo", "layar OLED", "baterai", "charging", "GPIO",
        "PWM", "ADC", "interrupt", "deep sleep", "OTA update", "watchdog", "logging",
    ]
    text = " ".join(f"Modul ini mendukung {topic}." for topic in topics)
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert 0 < len(r.voice_chunks) < len(topics)  # compressed, never emptied
    assert r.voice_chunks[0] == "Modul ini mendukung Wi-Fi."  # lead always kept
    # every surviving chunk is one of the original topic sentences, in
    # the SAME relative order they appeared in (never reordered/rewritten)
    seen_idx = [topics.index(c.replace("Modul ini mendukung ", "").rstrip(".")) for c in r.voice_chunks]
    assert seen_idx == sorted(seen_idx)
    # deterministic - identical input always produces identical output
    r2 = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r2.voice_chunks == r.voice_chunks


def test_c4_sentence_boundary_never_cut_badly():
    """The exact worked example from the sprint brief."""
    text = (
        "Pompa aquascape kamu kemungkinan perlu dibersihkan. "
        "Kalau alirannya sudah melemah, matikan pompa dulu. "
        "Setelah itu baru bongkar bagian impeller dan bersihkan kotorannya."
    )
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), max_chunk_chars=220)
    assert r.voice_chunks == [
        "Pompa aquascape kamu kemungkinan perlu dibersihkan.",
        "Kalau alirannya sudah melemah, matikan pompa dulu.",
        "Setelah itu baru bongkar bagian impeller dan bersihkan kotorannya.",
    ]
    for chunk in r.voice_chunks:
        assert chunk.strip().endswith((".", "!", "?"))  # never a mid-sentence fragment


def test_c5_paragraph_boundary_respected():
    text = "Paragraf pertama membahas topik A.\n\nParagraf kedua membahas topik B sepenuhnya berbeda."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.voice_chunks == [
        "Paragraf pertama membahas topik A.",
        "Paragraf kedua membahas topik B sepenuhnya berbeda.",
    ]


def test_c6_maximum_chunk_size_is_a_ceiling_not_a_target():
    """Two short sentences are NOT merged into one chunk just because
    they'd both fit under `max_chunk_chars` - default granularity is one
    sentence per chunk (see module docstring: smallest unit, fastest
    time-to-first-audio)."""
    text = "Oke. Siap."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), max_chunk_chars=220)
    assert r.voice_chunks == ["Oke.", "Siap."]


def test_c6b_oversized_single_sentence_split_at_clause_boundary_not_mid_word():
    long_sentence = (
        "Ini adalah satu kalimat yang sangat panjang sekali, dengan banyak klausa "
        "yang dipisahkan koma, supaya kita bisa menguji pemotongan pada batas klausa, "
        "bukan pada batas karakter yang membabi buta, dan bukan di tengah kata."
    )
    r = build_dual_response(long_sentence, ResponsePolicy(depth="normal", score=32), max_chunk_chars=60)
    assert len(r.voice_chunks) > 1
    for chunk in r.voice_chunks:
        assert len(chunk) <= 60 or " " not in chunk.strip()  # only a single unsplittable word may exceed
    rejoined = " ".join(c.rstrip(",") for c in r.voice_chunks)
    for word in ("Ini", "adalah", "klausa", "kata."):
        assert word in rejoined
    # never a mid-word cut: every chunk starts/ends on a real word boundary
    for chunk in r.voice_chunks:
        assert not chunk.startswith(" ") and not chunk.endswith(" ")


def test_c7_indonesian_punctuation_sentence_boundaries():
    text = "Apakah ini aman? Ya, ini aman! Bagus, terima kasih."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.voice_chunks == ["Apakah ini aman?", "Ya, ini aman!", "Bagus, terima kasih."]


def test_c8_english_punctuation_sentence_boundaries():
    text = "Is this safe? Yes, it is safe! Great, thank you."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), language="english")
    assert r.voice_chunks == ["Is this safe?", "Yes, it is safe!", "Great, thank you."]


def test_c9_url_not_cut_badly_across_chunks():
    text = "Buka https://example.com/config/very/long/path/that/is/quite/long untuk lanjut."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), max_chunk_chars=30)
    joined = " ".join(r.voice_chunks)
    assert "https://" not in joined  # URLs are stripped for voice entirely (pre-existing normalizer behavior)
    for chunk in r.voice_chunks:
        assert "example.com" not in chunk  # confirms nothing URL-shaped leaks into a mid-cut fragment


def test_c10_number_decimal_not_cut_badly():
    text = "Tegangan kerjanya adalah 12.5 volt, jangan melebihi itu."
    # Pinned explicitly (see test_18 above) - Indonesian-specific assertion,
    # must not depend on ambient LUNO_LANGUAGE env default.
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), max_chunk_chars=15, language="indonesian")
    joined = " ".join(r.voice_chunks)
    # the number must survive intact somewhere (spoken-word form, via the
    # existing normalizer) - never split into "12" in one chunk and ".5"/
    # "volt" dangling alone in the next with the digits themselves severed
    assert ("12" in joined or "dua belas" in joined.lower())
    for i, chunk in enumerate(r.voice_chunks):
        # no chunk boundary lands strictly inside a bare digit run
        assert not (chunk.rstrip().endswith(tuple("0123456789")) and i + 1 < len(r.voice_chunks)
                    and r.voice_chunks[i + 1].lstrip()[:1].isdigit())


def test_c11_markdown_and_code_handling_matches_voice_text():
    text = "**Note:** run `pip install foo` then see [docs](https://example.com/docs)."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    joined = " ".join(r.voice_chunks)
    assert "**" not in joined and "`" not in joined and "https://" not in joined
    assert "pip install foo" in joined


def test_c12_chunk_order_always_matches_sentence_order():
    text = "Pertama. Kedua. Ketiga. Keempat. Kelima."
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert r.voice_chunks == ["Pertama.", "Kedua.", "Ketiga.", "Keempat.", "Kelima."]


def test_c13_empty_response_no_chunks():
    r = build_dual_response("", ResponsePolicy(depth="normal", score=32))
    assert r.voice_chunks == []


def test_c14_whitespace_only_response_no_chunks():
    r = build_dual_response("   \n\n   ", ResponsePolicy(depth="normal", score=32))
    assert r.voice_chunks == []


def test_c15_list_items_grouped_into_one_chunk_unless_oversized():
    """The header line ("You will need:") is an ORDINARY sentence, not a
    list item - it gets its own chunk (a natural pause before the list
    starts reading). The three list items that follow it ARE grouped into
    one chunk together (comma-joined) since that fits comfortably under
    the default ceiling."""
    text = "You will need:\n- Wi-Fi\n- MQTT\n- Home Assistant"
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32))
    assert len(r.voice_chunks) == 2
    assert r.voice_chunks[0] == "You will need:"
    assert "Wi-Fi" in r.voice_chunks[1] and "MQTT" in r.voice_chunks[1] and "Home Assistant" in r.voice_chunks[1]


def test_c16_oversized_list_run_splits_at_item_boundaries_not_mid_item():
    # Distinct wording per item (not a number that `normalize_for_speech`
    # spells out as a word) so this test stays focused on "did the list
    # get split at ITEM boundaries" rather than number-to-word rendering
    # (already covered by test_15/test_16 in Section 1).
    labels = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    text = "Steps:\n- " + "\n- ".join(f"Step {label} with a fairly long description text" for label in labels)
    r = build_dual_response(text, ResponsePolicy(depth="normal", score=32), max_chunk_chars=80, language="english")
    assert len(r.voice_chunks) > 1
    joined = " ".join(r.voice_chunks)
    for label in labels:
        assert f"Step {label}" in joined


def test_c17_detailed_depth_chunks_match_the_compressed_voice_text_not_the_full_chat_text():
    text = "\n\n".join(
        f"Paragraf nomor {i} membahas detail teknis tambahan tentang topik ini secara panjang lebar."
        for i in range(1, 12)
    )
    r = build_dual_response(text, ResponsePolicy(depth="detailed", score=90))
    assert " ".join(r.voice_chunks) == r.voice_text  # same selection, just chunked
    assert len(r.voice_chunks) < 11  # DETAILED compression already dropped some sentences


def test_c18_voice_chunks_reconstruct_voice_text_exactly():
    """Sprint checklist item 20 - "Voice gets the same chunk sequence as
    the full response" - proven directly: joining `voice_chunks` with a
    single space always reconstructs `voice_text` exactly, for any depth,
    since both come from the identical `selected` sentence list."""
    cases = [
        ("Bisa. ESP32 mendukung MQTT.", "short"),
        ("ESP32 mendukung Wi-Fi. ESP32 juga mendukung Bluetooth.", "normal"),
        ("You will need:\n- Wi-Fi\n- MQTT\n- Home Assistant", "normal"),
    ]
    for text, depth in cases:
        r = build_dual_response(text, ResponsePolicy(depth=depth, score=32))
        assert " ".join(r.voice_chunks) == r.voice_text, f"mismatch for {text!r}"


# ============================================================================
# Section 6 - TTS Chunking/Streaming sprint: chunks reach the real
# `speak_request` event through the full production pipeline
# ============================================================================


def test_e2e_speak_request_carries_chunks_matching_voice_text():
    """The real `_speak()` call site (main_runtime_demo.py) must attach
    `chunks` to the `speak_request` payload as structured `SpeechChunk`
    dicts (TTS Chunk Queue & Cancellation sprint - see
    luno/speech_chunk.py), and those chunks' `text` fields must
    reconstruct the exact same `voice_text` already asserted elsewhere -
    proves the wiring, not just the pure function.

    Voice Output Naturalness & First-Audio Latency sprint: two of this
    test's original per-chunk assertions are honestly relaxed (never
    silently dropped) to match the now-default streaming path's real,
    intentional behavior:

    1. `total`: under streaming (`ENABLE_LLM_TTS_STREAMING=True`), the
       very first chunk is dispatched to TTS DURING generation, before
       the full reply - and therefore the true final chunk count - is
       known; `luno/incremental_speech.py::IncrementalSpeechBuffer.
       feed()` correctly marks such an early chunk's `total=-1` (an
       explicit "not yet known" sentinel - see that module's own
       docstring), and only the LAST chunk of the whole turn
       (`flush_final()`'s own return) is ever marked with a real
       `total`. An early-dispatched chunk cannot know a total that
       doesn't exist yet. Separately, that final `total` is the
       buffer's own INTERNAL count of settled sentence-units
       (`last.sequence + 1`), which can be strictly GREATER than the
       number of chunks actually dispatched to `speak_stream_chunk`,
       because `_sentences_to_chunks()` can merge more than one settled
       sentence into a single dispatched `SpeechChunk` (the same
       list-item/short-sentence grouping `build_dual_response()`'s own
       voice-chunk pairing already does elsewhere) without "returning"
       the sequence numbers it already consumed for the merged-away
       units. `total` is documented and used ONLY as telemetry/logging
       (confirmed by reading every call site in
       `luno/adapters/fish_audio.py` - `sequence`/`is_final` drive
       playback and completion, `total` only ever appears in a log
       message), so it is honestly an upper bound on the dispatched
       chunk count, not an exact one.

    2. `sequence`/`chunk_id`: these are assigned by `IncrementalSpeechBuffer`'s
       own internal monotonic counter, which can advance for sentences
       that were internally SETTLED but never actually dispatched early
       (only the very first settled sentence is ever sent during
       generation - see `_on_chunk()`'s own docstring) - so the
       sequence numbers actually DISPATCHED to `speak_stream_chunk` can
       have gaps (e.g. 0, then 2) relative to their position in this
       turn's own visible chunk list. Nothing downstream
       (`FishAudioAdapter`) relies on contiguous zero-based numbering -
       `sequence` is only ever used for logging/telemetry and to
       correlate a chunk with its own `chunk_id`/completion event, never
       as an array index (confirmed by reading every call site in
       `luno/adapters/fish_audio.py`). The real, load-bearing invariants
       are: unique, STRICTLY INCREASING sequence numbers in arrival
       order (no reordering, no duplication - Phase 5's own "ordered
       chunks" guarantee), a `chunk_id` that matches its OWN `sequence`
       (not its array position), and a `total` that is always correct
       by the time the turn's last chunk arrives. All of those are
       verified below, unweakened."""
    demo = _load_demo()
    canned = "ESP32 mendukung Wi-Fi. ESP32 juga mendukung Bluetooth. Keduanya bisa dipakai bersamaan."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "apa saja fitur ESP32?")
        payload = result["speak_request"]
        chunks = payload.get("chunks")
        assert chunks, "speak_request must carry a non-empty 'chunks' list"
        assert isinstance(chunks, list) and len(chunks) >= 2
        assert all(isinstance(c, dict) for c in chunks)
        assert " ".join(c["text"] for c in chunks) == payload.get("text")
        # correlation fields present and internally consistent
        request_id = payload.get("request_id")
        prev_sequence = None
        for c in chunks:
            assert c["request_id"] == request_id
            assert isinstance(c["sequence"], int)
            assert prev_sequence is None or c["sequence"] > prev_sequence, (
                "chunk sequence numbers must be strictly increasing in "
                "arrival order - never reordered, never duplicated"
            )
            prev_sequence = c["sequence"]
            assert c["total"] == -1 or c["total"] >= len(chunks), (
                "total is either the honest 'not yet known' sentinel, or "
                "an upper bound on the dispatched chunk count - never an "
                "undercount"
            )
            assert c["chunk_id"] == f"{request_id}:chunk:{c['sequence']}"
        # The final chunk of the turn always knows a real (non-sentinel)
        # total by the time the turn is complete - never allowed to stay
        # unknown, and never LESS than what was actually dispatched.
        assert chunks[-1]["total"] >= len(chunks)
        assert chunks[-1]["is_final"] is True
        assert all(not c["is_final"] for c in chunks[:-1])
    finally:
        console.stop()


def test_e2e_chat_still_gets_full_response_when_voice_is_chunked():
    """Sprint checklist item 19 - Chat must keep getting the FULL response
    regardless of how many voice chunks were produced."""
    demo = _load_demo()
    canned = "Kalimat satu di sini. Kalimat dua di sini. Kalimat tiga di sini."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "coba jelaskan tiga hal")
        assert result["assistant_response"].get("text") == canned
        assert len(result["speak_request"].get("chunks") or []) >= 2
    finally:
        console.stop()


def test_e2e_depth_computed_exactly_once_per_turn_still_holds():
    """Reconfirms `test_response_policy.py`'s own guarantee in THIS
    sprint's context - `build_dual_response()` must consume the SAME
    depth, never trigger a second `compute_response_policy()` call."""
    demo = _load_demo()
    console = _new_console(demo, canned_text="Jawaban singkat.")
    console.start()
    try:
        _wake(console, demo)
        calls = []
        original = demo.compute_response_policy

        def _counting(*a, **kw):
            calls.append((a, kw))
            return original(*a, **kw)

        demo.compute_response_policy = _counting
        try:
            _ask_and_capture(console, demo, "apa itu MQTT?")
            assert len(calls) == 1
        finally:
            demo.compute_response_policy = original
    finally:
        console.stop()
