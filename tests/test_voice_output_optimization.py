"""
test_voice_output_optimization.py
====================================

Voice Output Optimization sprint - "make Luno's voice response concise
and natural WITHOUT changing the full chat response." Tests for the
extension made to `luno/response_output.py`'s existing
`build_dual_response()` (generalizing its previously DETAILED-only
budget/priority-selection compression to SHORT and NORMAL too) and to
`luno/response_policy.py`'s explicit-instruction phrase lists (additive
only - see that module's own docstring comments at the edit sites).

NO NEW MODULE WAS CREATED FOR THIS SPRINT. There is no separate "voice
optimizer" function to import - `build_dual_response()` IS the voice
output optimizer now, exactly as `tests/test_response_output.py`'s own
existing test suite already exercises it. This file exists as its own
module (rather than appended to that one) purely for organizational
clarity per the sprint brief's own explicit "create
tests/test_voice_output_optimization.py" instruction; it imports the
exact same public API, reuses the exact same fixtures/conventions
(`ResponsePolicy`, `build_dual_response`), and runs under the same
autouse `isolate_persistent_state` fixture from `tests/conftest.py` (no
test here can ever touch Vinn's real `config/*.json` files).

Sections:
  1. Scenarios 1-30 (the sprint brief's own named list) - pure
     `build_dual_response()` calls.
  2. Structural/architectural guarantees - no second LLM call, no memory
     write, no extra TTS engine/normalizer/chunker/streaming pipeline, no
     additional playback thread per chunk.
  3. E2E scenarios A-I through the real production bridge
     (`RuntimeDemoConsole`), mirroring
     `tests/test_response_output.py`'s own Section 3/4 conventions.

Run:
    python3 -m pytest -q tests/test_voice_output_optimization.py
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
from luno.response_policy import ResponsePolicy, compute_response_policy  # noqa: E402

# ============================================================================
# Section 1 - the sprint brief's own 30 named scenarios
# ============================================================================


def _policy(depth, score, explicit=False):
    return ResponsePolicy(depth=depth, score=score, explicit=explicit)


# ---- 1. SHORT simple answer -------------------------------------------------

def test_01_short_simple_answer():
    r = build_dual_response("Bisa, ESP32 mendukung MQTT.", _policy("short", 10))
    assert r.chat_text == "Bisa, ESP32 mendukung MQTT."
    assert "Bisa" in r.voice_text
    assert r.depth == "short"


# ---- 2. SHORT long explanation - compresses down, keeps the direct answer --

def test_02_short_long_explanation_compresses():
    text = (
        "Bisa, ESP32 mendukung MQTT secara native. "
        "MQTT adalah protokol messaging ringan untuk IoT. "
        "Protokol ini dikembangkan sejak tahun 1999 oleh IBM. "
        "Banyak platform smart home memakai MQTT sebagai standar komunikasi. "
        "Ada juga alternatif seperti CoAP dan HTTP, tapi MQTT lebih hemat bandwidth. "
        "Kamu bisa mulai belajar MQTT lewat banyak tutorial online gratis."
    )
    full = build_dual_response(text, _policy("detailed", 90))
    short = build_dual_response(text, _policy("short", 10))
    assert short.chat_text == text  # chat untouched regardless of voice depth
    assert len(short.voice_text) < len(full.voice_text)
    assert short.voice_text.startswith("Bisa, ESP32 mendukung MQTT")
    assert short.voice_adapted is True


# ---- 3. NORMAL simple answer - short input, no compression needed ----------

def test_03_normal_simple_answer_unchanged():
    text = "Bisa. ESP32 cocok untuk MQTT karena ringan dan efisien untuk IoT."
    r = build_dual_response(text, _policy("normal", 38))
    assert r.chat_text == text
    assert len(r.voice_text) <= len(text) + 5  # cleaning only at this size


# ---- 4. NORMAL long explanation - compresses, keeps more than SHORT --------

def test_04_normal_long_explanation_keeps_more_than_short():
    # Distinct topics per sentence (mirrors test_c3's own convention in
    # test_response_output.py) so near-duplicate dedup - an EXISTING,
    # correct, shared behavior - never collapses these before budget-based
    # selection even runs; a "Fakta nomor {i}..." template differing only
    # by number is >82% word-identical and would be deduped away first,
    # which would test dedup instead of the thing this test is about.
    topics = [
        "Wi-Fi", "Bluetooth", "MQTT", "HTTP", "sensor suhu", "sensor cahaya",
        "relay", "motor servo", "layar OLED", "baterai", "charging", "GPIO",
    ]
    text = " ".join(f"Modul ini mendukung {topic}." for topic in topics)
    normal = build_dual_response(text, _policy("normal", 38))
    short = build_dual_response(text, _policy("short", 10))
    assert len(normal.voice_chunks) > len(short.voice_chunks)
    assert len(normal.voice_chunks) < len(topics)  # NORMAL still compresses a genuinely long list of facts


# ---- 5. DETAILED answer - unchanged pre-existing behavior ------------------

def test_05_detailed_answer_still_compresses_bounded():
    text = "\n\n".join(
        f"Paragraf nomor {i} membahas detail teknis tambahan tentang topik ini." for i in range(1, 12)
    )
    r = build_dual_response(text, _policy("detailed", 90))
    assert r.voice_adapted is True
    assert len(r.voice_text) < len(text)


# ---- 6. Warning preservation (soft/general "penting"/"harus") --------------

def test_06_warning_preservation_across_all_depths():
    text = (
        "ESP32 gampang dipakai untuk banyak proyek IoT rumahan. "
        "Penting: selalu gunakan resistor pull-up pada pin I2C. "
        "Ada banyak sensor I2C murah yang kompatibel. "
        "Kamu juga bisa memakai sensor analog biasa kalau perlu. "
        "Banyak tutorial membahas kalibrasi sensor lebih lanjut."
    )
    for depth, score in (("short", 10), ("normal", 38), ("detailed", 90)):
        r = build_dual_response(text, _policy(depth, score))
        assert "pull-up" in r.voice_text or "penting" in r.voice_text.lower(), f"lost warning at {depth}"


# ---- 7. Safety warning preservation (hard prohibition, the 220V case) ------

def test_07_safety_warning_survives_every_depth():
    text = (
        "Kamu bisa menyalakan relay lewat ESP32 dengan mudah. "
        "Jangan sambungkan pin GPIO langsung ke 220V, itu bisa merusak board dan berbahaya. "
        "Banyak tutorial online membahas cara wiring yang aman. "
        "Relay module biasanya sudah punya optocoupler untuk isolasi. "
        "Modul relay juga tersedia dalam berbagai varian jumlah channel."
    )
    for depth, score in (("short", 10), ("normal", 38), ("detailed", 90)):
        r = build_dual_response(text, _policy(depth, score))
        assert "220V" in r.voice_text and "jangan" in r.voice_text.lower(), f"lost safety warning at {depth}"


# ---- 8. Numeric-value preservation ------------------------------------------

def test_08_numeric_value_preservation():
    text = (
        "Motor ini bisa dikendalikan lewat PWM dengan mikrokontroler apa saja. "
        "Ada banyak driver motor yang bisa dipakai tergantung kebutuhan proyek. "
        "Tegangan kerja maksimum motor ini adalah 12 volt. "
        "Banyak forum online membahas cara memilih driver motor yang tepat. "
        "Pemilihan driver juga tergantung pada arus yang dibutuhkan motor."
    )
    # Pinned explicitly - Indonesian-specific spoken-number assertion, must
    # not depend on ambient LUNO_LANGUAGE env default (normalize_for_speech()
    # itself defaults to "english" when no language is passed).
    r = build_dual_response(text, _policy("short", 10), language="indonesian")
    assert "12" in r.voice_text or "dua belas" in r.voice_text.lower()


# ---- 9. Condition preservation (soft conditional, scored higher) ----------

def test_09_condition_scored_higher_than_plain_filler():
    # Note: the filler sentence deliberately avoids any `_WARNING_KEYWORDS`
    # substring (e.g. a phrase like "tidak terlalu penting" would itself
    # false-positive-match "penting" and become a hard must-keep via the
    # EXISTING, pre-existing `_has_warning()` mechanism - not something
    # this sprint changes - which would defeat the point of this test).
    text = (
        "Suhu CPU kamu sekarang normal. "
        "Kamu bisa memantau suhu lewat aplikasi monitoring kapan saja. "
        "Kalau suhu CPU melewati 85 derajat, segera periksa fan dan thermal paste."
    )
    r = build_dual_response(text, _policy("short", 10))
    assert "kalau" in r.voice_text.lower() or "85" in r.voice_text or "eighty" in r.voice_text.lower()


# ---- 10. Explicit "jelaskan detail" -> no aggressive shortening -----------

def test_10_explicit_full_detail_skips_compression():
    text = " ".join(f"Detail nomor {i} tentang cara kerja sistem ini." for i in range(1, 15))
    policy = compute_response_policy("jelaskan semuanya tentang cara kerja sistem ini")
    assert policy.depth == "detailed" and policy.explicit is True
    r = build_dual_response(text, policy)
    deduped_count = len(build_dual_response(text, _policy("detailed", 90, explicit=False)).voice_chunks)
    assert len(r.voice_chunks) >= deduped_count  # explicit full-detail never compresses MORE than implicit


# ---- 11. Explicit "singkat" -> strongly favors SHORT -----------------------

def test_11_explicit_short_instruction_resolves_to_short_depth():
    policy = compute_response_policy("jawab singkat aja ya")
    assert policy.depth == "short" and policy.explicit is True


# ---- 12. Explicit "semua" -> DETAILED, list items not dropped -------------

def test_12_explicit_semua_preserves_all_list_items():
    labels = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    text = "Penyebabnya ada beberapa:\n" + "\n".join(f"- Penyebab {label}" for label in labels)
    policy = compute_response_policy("sebutkan semua penyebabnya")
    assert policy.depth == "detailed" and policy.explicit is True
    r = build_dual_response(text, policy)
    for label in labels:
        assert f"Penyebab {label}" in r.voice_text


# ---- 13. Numbered list -----------------------------------------------------

def test_13_numbered_list_handled():
    text = "Steps:\n1. ESP32 publish data.\n2. MQTT broker menerima data.\n3. Home Assistant subscribe."
    r = build_dual_response(text, _policy("normal", 38))
    assert "1." not in r.voice_text and "2." not in r.voice_text
    assert "ESP32 publish data" in r.voice_text


# ---- 14. Bullet list --------------------------------------------------------

def test_14_bullet_list_handled():
    text = "You will need:\n- Wi-Fi\n- MQTT\n- Home Assistant"
    r = build_dual_response(text, _policy("normal", 38))
    assert "Wi-Fi" in r.voice_text and "MQTT" in r.voice_text and "Home Assistant" in r.voice_text


# ---- 15. Long list - not blindly read in full at SHORT/NORMAL, list items
#          exempted from budget-drop but a very long list still fits under
#          the module's existing chunk grouping (documented known
#          limitation: true list-run summarization is out of scope, see
#          response_output.py's `_select_by_priority` docstring) ----------

def test_15_long_list_short_depth_still_answers_directly():
    labels = [f"item{i}" for i in range(1, 21)]
    text = "Ada beberapa kemungkinan penyebabnya:\n" + "\n".join(f"- {label}" for label in labels)
    r = build_dual_response(text, _policy("short", 10))
    assert r.voice_text.startswith("Ada beberapa kemungkinan")
    # SHORT never crashes/empties on a long list, and the direct lead survives
    assert r.voice_text


# ---- 16. Code block ---------------------------------------------------------

def test_16_code_block_stripped_not_read_literally():
    text = "Use this:\n```python\ndigitalWrite(2, HIGH)\n```\nto turn on the pin."
    r = build_dual_response(text, _policy("short", 10))
    assert "```" not in r.voice_text and "digitalWrite" not in r.voice_text


# ---- 17. Markdown headings --------------------------------------------------

def test_17_markdown_headings_stripped():
    # Headings are stripped by the existing `normalize_for_speech()` when
    # they form their own paragraph (blank-line-separated), matching how
    # a real LLM response formats headings - mirrors this module's own
    # sentence-splitting convention (paragraphs split on blank lines).
    text = "# Setup\n\nFirst connect the ESP32.\n\n## Wiring\n\nThen wire the relay."
    r = build_dual_response(text, _policy("normal", 38))
    assert "#" not in r.voice_text
    assert "connect the ESP32" in r.voice_text


# ---- 18. Multiple paragraphs ------------------------------------------------

def test_18_multiple_paragraphs_no_linebreaks_in_voice():
    text = "Paragraf pertama membahas topik A.\n\nParagraf kedua membahas topik B."
    r = build_dual_response(text, _policy("normal", 38))
    assert "\n" not in r.voice_text


# ---- 19. Empty response -----------------------------------------------------

def test_19_empty_response_never_raises():
    r = build_dual_response("", _policy("short", 10))
    assert r.chat_text == "" and r.voice_text == "" and r.voice_chunks == []


# ---- 20. Very short response ------------------------------------------------

def test_20_very_short_response_unchanged():
    r = build_dual_response("Oke!", _policy("short", 5))
    assert r.voice_text == "Oke!"


# ---- 21. Indonesian response ------------------------------------------------

def test_21_indonesian_response():
    r = build_dual_response("Suhunya -3 derajat.", _policy("normal", 38), language="indonesian")
    assert "minus tiga" in r.voice_text.lower()


# ---- 22. Mixed Indonesian/English technical response -----------------------

def test_22_mixed_language_technical_response():
    text = (
        "ESP32 support both Wi-Fi dan Bluetooth secara bersamaan. "
        "Kamu bisa pakai library WiFi.h untuk koneksi dasar. "
        "Untuk fitur lebih advanced, coba pakai AsyncWebServer."
    )
    r = build_dual_response(text, _policy("short", 10))
    assert r.voice_text  # never empty, never crashes on mixed-language input


# ---- 23. Response with URLs -------------------------------------------------

def test_23_response_with_urls_stripped():
    text = "Lihat dokumentasinya di https://example.com/docs/config untuk detail lebih lanjut."
    r = build_dual_response(text, _policy("normal", 38))
    assert "https://" not in r.voice_text


# ---- 24. Response with emojis -----------------------------------------------

def test_24_response_with_emojis_stripped():
    text = "ESP32 kamu berhasil terhubung ke Wi-Fi! 🎉📶"
    r = build_dual_response(text, _policy("short", 10))
    assert "🎉" not in r.voice_text and "📶" not in r.voice_text
    assert "terhubung" in r.voice_text


# ---- 25. Response with numbers/units ----------------------------------------

def test_25_response_with_numbers_and_units():
    text = "Tegangan kerjanya adalah 12.5 volt, arusnya sekitar 2 ampere."
    # Pinned explicitly (see test_08 above) - Indonesian-specific assertion.
    r = build_dual_response(text, _policy("short", 10), language="indonesian")
    assert "12" in r.voice_text or "dua belas" in r.voice_text.lower()


# ---- 26. Response with punctuation ------------------------------------------

def test_26_response_with_heavy_punctuation():
    text = "Apakah ini aman?! Ya, ini aman... Bagus, terima kasih!!"
    r = build_dual_response(text, _policy("normal", 38))
    assert "?!" not in r.voice_text  # mixed repeated punctuation collapsed to one mark
    assert "!!" not in r.voice_text  # repeated "!" collapsed to one
    assert r.voice_text  # never empty, never crashes


# ---- 27. Response with existing normalized voice text (idempotency-ish) ---

def test_27_already_clean_text_passes_through_stably():
    text = "ESP32 mendukung MQTT."
    r1 = build_dual_response(text, _policy("normal", 38))
    r2 = build_dual_response(r1.voice_text, _policy("normal", 38))
    assert r2.voice_text == r1.voice_text  # feeding already-clean text back in is stable


# ---- 28. DETAILED does not unnecessarily compress a short response --------

def test_28_detailed_does_not_compress_a_short_reply():
    text = "Bisa. ESP32 mendukung fitur itu."
    r = build_dual_response(text, _policy("detailed", 90))
    assert r.voice_text == text
    assert r.voice_adapted is False


# ---- 29. SHORT does not remove a critical warning even under tight budget -

def test_29_short_never_drops_critical_warning_under_tight_budget():
    text = " ".join(f"Kalimat pengisi nomor {i} yang tidak terlalu penting." for i in range(1, 10))
    text += " Jangan pernah mematikan power supply saat sedang flashing firmware, itu bisa merusak chip."
    r = build_dual_response(text, _policy("short", 10))
    assert "jangan" in r.voice_text.lower() and "flashing" in r.voice_text.lower()


# ---- 30. Optimizer never mutates the original text --------------------------

def test_30_optimizer_never_mutates_original_input_string():
    text = "Bisa. ESP32 mendukung MQTT. Ini kalimat tambahan yang cukup panjang untuk diuji."
    original_copy = str(text)
    for depth, score in (("short", 10), ("normal", 38), ("detailed", 90)):
        r = build_dual_response(text, _policy(depth, score))
        assert text == original_copy  # input string itself never mutated
        assert r.chat_text == text  # chat is always the exact, unmodified original


# ============================================================================
# Section 2 - structural/architectural guarantees
# ============================================================================


def test_no_llm_module_imported_by_response_output():
    """No second LLM call - `response_output.py` imports nothing beyond
    the standard library plus `luno.response_policy`/`luno.text_normalizer`
    (verified by inspecting its own module source, not just trusting the
    docstring)."""
    import luno.response_output as ro
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    for forbidden in ("openrouter", "openai", "anthropic", "requests.post", "httpx", "llm_manager"):
        assert forbidden not in src.lower(), f"unexpected LLM/network dependency: {forbidden}"


def test_build_dual_response_is_deterministic_pure_function():
    """Same input always produces the same output - no randomness, no
    clock, no hidden state carried between calls."""
    text = "Bisa. ESP32 mendukung MQTT karena ringan. Ada banyak contoh proyek IoT yang memakainya."
    results = {build_dual_response(text, _policy("short", 10)).voice_text for _ in range(5)}
    assert len(results) == 1


def test_optimizer_never_writes_persistent_state_files():
    """Read-only with respect to memory/relationship/verified-fact state -
    calling `build_dual_response()` many times must never create or touch
    any file under config/."""
    config_dir = os.path.join(_ROOT, "config")
    before = {}
    if os.path.isdir(config_dir):
        before = {f: os.path.getmtime(os.path.join(config_dir, f)) for f in os.listdir(config_dir)}
    text = "Bisa. ESP32 mendukung MQTT. Penting: jangan lupa restart setelah update firmware."
    for depth, score in (("short", 10), ("normal", 38), ("detailed", 90)):
        build_dual_response(text, _policy(depth, score))
    after = {}
    if os.path.isdir(config_dir):
        after = {f: os.path.getmtime(os.path.join(config_dir, f)) for f in os.listdir(config_dir)}
    assert before == after


def test_response_policy_explicit_field_is_the_only_intent_signal_used():
    """`response_output.py` reads `ResponsePolicy.explicit` (already
    computed by the existing depth policy) rather than re-matching intent
    phrases itself - guards against a second, competing classifier ever
    being introduced here."""
    import luno.response_output as ro
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    # the module must never DEFINE its own phrase list resembling the
    # depth policy's explicit-instruction phrase tables (a docstring
    # reference to those names, explaining that they're read from the
    # OTHER module, is fine and expected - only a local assignment would
    # indicate a second, competing classifier).
    assert "_EXPLICIT_SHORT_PHRASES = (" not in src
    assert "_EXPLICIT_DETAILED_PHRASES = (" not in src


def test_normalize_for_speech_still_the_only_cleaning_pass():
    """No duplicate normalizer - `response_output.py` must still import
    and use the existing `normalize_for_speech`, never reimplement
    markdown/emoji/number cleaning itself."""
    import luno.response_output as ro
    assert ro.normalize_for_speech.__module__ == "luno.text_normalizer.normalizer"


# ============================================================================
# Section 3 - E2E scenarios A-I through the real production bridge
# ============================================================================


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_voice_opt", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_voice_opt"] = demo
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
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _ask_and_capture(console, demo, text):
    """Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) to be dispatch-mode-agnostic.
    `ENABLE_LLM_TTS_STREAMING` now defaults to `True` (see
    `luno/config.py`), so a turn's voice dispatch may arrive as either
    ONE legacy `speak_request` OR a SEQUENCE of `speak_stream_chunk`
    events (each a real `SpeechChunk.to_dict()`, terminated by
    `is_final`) plus a separate `response_depth_assigned` event carrying
    `depth`. This helper still returns a plain dict shaped exactly like
    the legacy `speak_request` payload - callers do not need to know
    which path fired. See `tests/test_response_output.py::_ask_and_capture`
    for the identical, more fully documented original of this fix."""
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


# ---- A. Full LLM response -> CHAT full, VOICE optimized -------------------

def test_e2e_a_chat_full_voice_optimized():
    demo = _load_demo()
    canned = (
        "Bisa, ESP32 mendukung MQTT. MQTT adalah protokol ringan untuk IoT. "
        "Ada banyak broker MQTT open-source yang gratis dipakai. "
        "Kamu juga bisa memakai layanan cloud kalau tidak mau hosting sendiri. "
        "Banyak tutorial membahas setup broker dari nol. "
        "MQTT juga mendukung QoS untuk memastikan keandalan pengiriman pesan."
    )
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jelaskan tentang MQTT di ESP32")
        assert result["assistant_response"].get("text") == canned  # CHAT full, untouched
        spoken = result["speak_request"].get("text", "")
        assert spoken != canned  # VOICE optimized/adapted (6 sentences exceeds NORMAL's budget floor)
        assert "Bisa, ESP32 mendukung MQTT" in spoken
    finally:
        console.stop()


# ---- B. SHORT -> concise ----------------------------------------------------

def test_e2e_b_short_depth_produces_concise_voice():
    demo = _load_demo()
    canned = (
        "Bisa. ESP32 mendukung fitur deep sleep. "
        "Deep sleep membantu menghemat daya baterai secara signifikan. "
        "Ada beberapa mode sleep lain seperti light sleep dan modem sleep. "
        "Konfigurasinya bisa disesuaikan lewat esp_sleep API."
    )
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jawab singkat, apa ESP32 support deep sleep?")
        assert result["speak_request"].get("depth") == "short"
        spoken = result["speak_request"].get("text", "")
        assert len(spoken) < len(canned)
    finally:
        console.stop()


# ---- C. NORMAL -> useful supporting context --------------------------------

def test_e2e_c_normal_depth_reaches_speak_request():
    demo = _load_demo()
    canned = "Relay adalah saklar elektronik yang dikendalikan oleh sinyal listrik dari mikrokontroler."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "cara pasang relay ke ESP32?")
        assert result["speak_request"].get("depth") == "normal"
    finally:
        console.stop()


# ---- D. DETAILED -> preserves most meaningful detail ------------------------

def test_e2e_d_detailed_depth_reaches_speak_request():
    demo = _load_demo()
    canned = "Penjelasan arsitektur lengkap ESP32 dari CPU sampai peripheral secara menyeluruh."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jelaskan arsitektur ESP32 dari CPU sampai peripheral")
        assert result["speak_request"].get("depth") == "detailed"
    finally:
        console.stop()


# ---- E. Explicit "jelaskan lebih detail" prevents aggressive compression --

def test_e2e_e_explicit_detail_request_reaches_detailed_depth():
    demo = _load_demo()
    canned = "Jawaban singkat."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jelaskan semuanya secara detail tentang cara kerja WiFi di ESP32")
        assert result["speak_request"].get("depth") == "detailed"
    finally:
        console.stop()


# ---- F. Critical warning survives SHORT optimization through the real bridge

def test_e2e_f_critical_warning_survives_short_through_real_bridge():
    demo = _load_demo()
    canned = (
        "ESP32 gampang dipakai untuk banyak proyek. "
        "Ada banyak library pendukung yang tersedia gratis. "
        "Jangan sambungkan pin GPIO langsung ke 220V, itu bisa merusak board. "
        "Banyak hobbyist memakainya untuk proyek rumahan."
    )
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "jawab singkat, apa saja kelebihan ESP32?")
        spoken = result["speak_request"].get("text", "")
        assert "220V" in spoken and "jangan" in spoken.lower()
    finally:
        console.stop()


# ---- G. Existing streaming TTS pipeline still receives valid chunks -------

def test_e2e_g_speak_request_still_carries_valid_chunks():
    demo = _load_demo()
    canned = "ESP32 mendukung Wi-Fi. ESP32 juga mendukung Bluetooth. Keduanya bisa dipakai bersamaan."
    console = _new_console(demo, canned_text=canned)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "apa saja fitur ESP32?")
        payload = result["speak_request"]
        chunks = payload.get("chunks")
        assert chunks, "speak_request must still carry a non-empty 'chunks' list"
        assert " ".join(c["text"] for c in chunks) == payload.get("text")
        assert chunks[-1]["is_final"] is True
    finally:
        console.stop()


# ---- H. Cancellation/barge-in still works (streaming coordinator untouched)

def test_e2e_h_streaming_coordinator_module_public_surface_intact():
    """UPDATED by a LATER sprint (Voice Pipeline Latency & Semantic
    Speech Segmentation Sprint 3 - "Production-Safe LLM -> TTS Streaming
    Activation"): this test originally asserted `luno/incremental_speech.py`
    was untouched by THIS (Voice Output Optimization) sprint, using the
    presence of a "never applies [budget-based compression]" docstring
    note as a proxy for "the old SHORT/NORMAL-bypasses-depth-policy
    limitation still exists." Sprint 3's own Phase 0 audit found that
    limitation was a genuine, confirmed bypass of response-depth policy
    for EVERY depth (not just SHORT/NORMAL) - a streamed turn was spoken
    in full, uncompressed, regardless of depth - and fixed it directly in
    `incremental_speech.py` (see that module's own "RESPONSE-DEPTH-
    POLICY-SAFE REDESIGN" docstring section and
    `docs/change_impact/llm_tts_streaming_activation.md`). That fix is
    intentional and correct, so the OLD assertion (which would now
    fail-by-design) is retired. What remains true, and still worth
    testing here: the module's PUBLIC SURFACE (the two classes this
    codebase's other tests construct directly) is unchanged, so nothing
    downstream that merely imports/constructs these classes breaks."""
    import luno.incremental_speech as inc
    assert hasattr(inc, "IncrementalSpeechBuffer")
    assert hasattr(inc, "StreamingSpeechCoordinator")


# ---- I. Existing response-depth policy remains the single depth authority -

def test_e2e_i_depth_computed_exactly_once_per_turn():
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
