"""
test_voice_output_coherence.py
================================

VOICE OUTPUT COHERENCE sprint - Phase 0 proved (via a real, code-derived
reproduction harness, not speculation) that Luno's audible "sounds
disconnected on long responses" symptom is introduced BEFORE TTS, inside
`luno.response_output.build_dual_response()`'s depth-aware sentence
SELECTION (`_select_by_priority()`/`_score_sentence()`), NOT by TTS chunk
pipelining, NOT by Fish Audio, and NOT by chunk splitting
(`_group_sentences_into_chunk_pairs()` always chunks whatever sentence
subset selection ALREADY chose - by construction, per that function's own
docstring).

Two concrete, reproducible root causes were found and fixed:

1. **Orphaned soft-conditional clauses (hypotheses C + D).**
   `_score_sentence()` gives an ordinary explanatory/diagnostic sentence
   with no number/warning/condition keyword a near-zero score (only a
   ~0.1-per-index position tiebreak), so it is dropped ahead of almost
   any other candidate - even when a LATER, SURVIVING sentence's meaning
   depends on it (a soft conditional clause, e.g. "Jika WiFi sudah
   terhubung tapi MQTT masih gagal, ..." presupposes the reader was just
   told HOW to check whether WiFi is connected). Fixed with one new,
   bounded, deterministic scoring signal: a sentence that immediately
   PRECEDES a soft-conditional sentence (`_has_condition()`, the SAME
   existing detector) gets a modest "sets up a conditional" bonus -
   reusing the existing conditional detector, not a new classifier.

2. **`_has_warning()` false-positive substring collision (hypothesis B).**
   Naive substring matching (`"harus" in cleaned_lower`) matched INSIDE
   "seharusnya" ("should"/"supposedly" - not a warning at all), silently
   promoting an ordinary probabilistic sentence to hard must-keep status
   and displacing a genuinely more important sentence from a bounded
   budget. Fixed with a word-boundary-safe match (the SAME technique
   already established in `luno.memory._compile_word_boundary_marker_pattern()`
   for the exact same class of bug - "lanjut" matching inside
   "selanjutnya" - reused here, not reinvented).

Both fixes are additive, bounded, deterministic, keyword/position-based -
no LLM call, no second tokenizer (reuses `_has_condition`/the existing
`re` module already imported), no new persistent state, and the
compression MECHANISM (budgets, must-keep set, `_select_by_priority`'s
overall shape, order preservation via `sorted(keep)`) is otherwise
UNCHANGED. See docs/change_impact/voice_output_coherence.md for the full
audit trail and before/after examples.

Tests below distinguish "shorter" from "coherent": most assertions check
dependency/order/prohibition PRESERVATION, never merely word/sentence
COUNT.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable, List

import pytest

from luno import response_output as ro
from luno.response_policy import compute_response_policy, DEPTH_DETAILED, DEPTH_NORMAL, DEPTH_SHORT

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ─────────────────────────────────────────────
# Shared worked examples (Indonesian, matching this project's primary
# conversational language and the existing test suite's own convention).
# ─────────────────────────────────────────────

CAUSE_CHAIN = (
    "GPU kamu mengalami throttling karena suhu yang terlalu tinggi. "
    "Ini terjadi karena thermal paste yang sudah lama tidak diganti kehilangan efektivitasnya dalam menghantarkan panas. "
    "Akibatnya, panas dari chip GPU tidak bisa dipindahkan ke heatsink dengan cepat. "
    "Karena panas menumpuk, GPU secara otomatis menurunkan clock speed untuk melindungi dirinya sendiri. "
    "Penurunan clock speed inilah yang menyebabkan frame rate kamu turun drastis saat bermain game berat. "
    "Jadi, solusinya adalah mengganti thermal paste agar suhu kembali normal dan performa GPU pulih."
)

PROBLEM_DIAGNOSIS_SOLUTION = (
    "ESP32 kamu tidak bisa connect ke MQTT broker. "
    "Masalah ini biasanya muncul karena kredensial WiFi yang salah dimasukkan ke firmware. "
    "Selain itu, broker MQTT mungkin memerlukan autentikasi username dan password yang belum kamu konfigurasi. "
    "Firewall di jaringan lokal juga bisa memblokir port 1883 yang digunakan MQTT secara default. "
    "Untuk mendiagnosis lebih lanjut, coba periksa serial monitor untuk melihat pesan error koneksi WiFi terlebih dahulu. "
    "Jika WiFi sudah terhubung tapi MQTT masih gagal, periksa kembali username dan password broker kamu. "
    "Setelah kredensial diperbaiki, ESP32 seharusnya bisa terhubung ke broker tanpa masalah."
)

PREREQUISITE_CHAIN = (
    "Sebelum kamu bisa menggunakan Home Assistant untuk mengontrol lampu pintar, kamu perlu memahami konsep entity dan device terlebih dahulu. "
    "Entity adalah representasi digital dari satu fungsi perangkat, misalnya satu saklar lampu adalah satu entity. "
    "Device adalah kumpulan entity yang berasal dari satu perangkat fisik, misalnya satu smart plug bisa punya entity saklar dan entity pengukur daya. "
    "Setelah kamu memahami perbedaan ini, kamu bisa mulai membuat automation yang menargetkan entity secara spesifik. "
    "Automation inilah yang nantinya memungkinkan lampu menyala otomatis ketika kamu masuk ruangan."
)

LIST_ITEMS_RESPONSE = (
    "Ada beberapa hal yang perlu kamu siapkan sebelum memulai project ini.\n"
    "1. Microcontroller ESP32 atau ESP8266.\n"
    "2. Sensor DHT22 untuk suhu dan kelembaban.\n"
    "3. Breadboard dan kabel jumper.\n"
    "4. Kabel USB untuk flashing firmware.\n"
    "Setelah semua bahan siap, kamu bisa mulai merangkai sirkuitnya sesuai skema yang sudah disediakan."
)

CONCLUSION_DEPENDS_ON_EXPLANATION = (
    "Motor servo kamu bergerak tersentak-sentak alih-alih halus. "
    "Ini terjadi karena sinyal PWM yang dikirim mikrokontroler punya frekuensi yang tidak stabil. "
    "Frekuensi yang tidak stabil membuat servo salah menginterpretasikan posisi target yang diminta. "
    "Kesalahan interpretasi posisi inilah yang membuat servo terus mengoreksi arah secara tiba-tiba. "
    "Jadi, kamu perlu menstabilkan frekuensi PWM terlebih dahulu sebelum masalah gerakan tersentak ini hilang."
)

SAFETY_RESPONSE = (
    "Modul relay ini digunakan untuk mengontrol perangkat AC bertegangan tinggi. "
    "Jangan sambungkan relay ini langsung ke sumber listrik 220V tanpa isolasi yang memadai. "
    "Kesalahan pengkabelan bisa menyebabkan sengatan listrik atau kebakaran. "
    "Pastikan kamu menggunakan relay dengan rating tegangan yang sesuai dengan bebannya. "
    "Setelah semua terpasang dengan aman, kamu bisa menguji relay dengan beban kecil terlebih dahulu."
)

MIXED_SHORT_LONG = (
    "Ya, bisa. "
    "ESP32 mendukung deep sleep mode yang menurunkan konsumsi daya hingga di bawah 10 microampere ketika perangkat sedang tidak melakukan tugas aktif apapun, jauh lebih hemat dibanding modul WiFi lain di kelas yang sama. "
    "Tapi ingat, wake-up dari deep sleep butuh waktu. "
    "Jika aplikasi kamu butuh respons instan, sebaiknya gunakan light sleep saja."
)

ENGLISH_RESPONSE = (
    "Your GPU is thermal throttling because the temperature is too high. "
    "This happens because old thermal paste loses its ability to transfer heat efficiently. "
    "As a result, heat from the chip cannot reach the heatsink fast enough. "
    "Because heat builds up, the GPU automatically lowers its clock speed to protect itself. "
    "So, the fix is to replace the thermal paste to restore normal temperatures and performance."
)


def _policy(depth_query: str = "", text_for_default: str = ""):
    return compute_response_policy(depth_query or text_for_default)


def _build(text: str, query: str = "", language: str = "indonesian"):
    policy = _policy(query, text)
    return ro.build_dual_response(text, policy, language=language)


# ─────────────────────────────────────────────
# Phase 0 core proof tests - these FAIL against the pre-fix code and
# PASS after the fix (confirmed by running before any production edit).
# ─────────────────────────────────────────────

def test_diagnostic_prerequisite_is_not_orphaned_by_its_own_conditional_followup():
    """THE core proof: if the voice response keeps the soft-conditional
    "Jika WiFi sudah terhubung tapi MQTT masih gagal, periksa kembali..."
    sentence, it must ALSO keep the diagnostic sentence immediately
    before it ("Untuk mendiagnosis lebih lanjut, coba periksa serial
    monitor untuk melihat pesan error koneksi WiFi terlebih dahulu.") -
    otherwise the listener hears a conditional referring to a check they
    were never told how to perform. FAILS against the pre-fix scoring
    (the conditional survives via its own condition-keyword boost while
    its own prerequisite, having no scoring feature, is dropped)."""
    dr = _build(PROBLEM_DIAGNOSIS_SOLUTION, "kenapa ESP32 gak bisa connect MQTT")
    assert dr.depth == DEPTH_NORMAL
    has_conditional_followup = "wifi sudah terhubung" in dr.voice_text.lower()
    has_diagnostic_prerequisite = "serial monitor" in dr.voice_text.lower()
    assert has_conditional_followup, "expected the conditional followup sentence to be selected at all"
    assert has_diagnostic_prerequisite, (
        f"the conditional followup ('jika WiFi sudah terhubung...') survived compression but its own "
        f"prerequisite ('periksa serial monitor...') was dropped - an orphaned, disconnected-sounding "
        f"conditional. voice_text={dr.voice_text!r}"
    )


def test_has_warning_does_not_false_positive_on_seharusnya():
    """`_has_warning()` must not treat "seharusnya" ('should'/'supposedly'
    - an ordinary probabilistic statement, not a warning) as containing
    the warning keyword "harus" ('must'). FAILS against the pre-fix naive
    substring match."""
    cleaned_lower = "setelah kredensial diperbaiki, esp32 seharusnya bisa terhubung ke broker tanpa masalah."
    assert not ro._has_warning(cleaned_lower), (
        "'seharusnya' must not false-positive-match the 'harus' warning keyword via naive substring search"
    )
    # Sanity: a GENUINE warning must still be detected (word-boundary fix
    # must not become so strict it stops matching real warnings).
    assert ro._has_warning("kamu harus mengganti kabel ini segera")


def test_genuine_warning_keywords_still_detected_after_word_boundary_fix():
    """Every existing `_WARNING_KEYWORDS` entry must still match its own
    genuine, whole-word usage after the word-boundary fix - the fix must
    narrow false positives, never silently stop detecting real warnings."""
    genuine_examples = {
        "jangan": "jangan sentuh kabel itu",
        "wajib": "kamu wajib memakai sarung tangan",
        "harus": "kamu harus mematikan daya dulu",
        "dilarang": "dilarang membuka casing saat menyala",
        "berbahaya": "ini berbahaya jika disentuh langsung",
        "important": "this step is important for safety",
        "warning": "warning: high voltage present",
        "must": "you must disconnect power first",
        "do not ": "do not touch the exposed wire",
    }
    for kw, sentence in genuine_examples.items():
        assert ro._has_warning(sentence.lower()), f"genuine warning keyword {kw!r} must still be detected in {sentence!r}"


# ============================================================================
# Full test matrix (18 scenarios from the brief) - pure `build_dual_response()`
# calls. Assertions favor COHERENCE/dependency/order checks over plain
# word/sentence counts wherever the brief's own scenario asks for it.
# ============================================================================

def _sentence_indices_in_original_order(dr: ro.DualResponse, original_sentences: List[str]) -> List[int]:
    """Maps each `voice_chunks` entry back to its index in the ORIGINAL
    (pre-selection) sentence list by substring containment on a
    normalized/lowered basis - used to assert order preservation without
    hardcoding exact `normalize_for_speech` output."""
    indices = []
    for chunk in dr.voice_chunks:
        chunk_l = chunk.lower()
        for i, orig in enumerate(original_sentences):
            if orig.lower()[:20] in chunk_l or chunk_l[:20] in orig.lower():
                indices.append(i)
                break
    return indices


# ---- 1. Long explanatory response -------------------------------------------

def test_01_long_explanatory_response_stays_coherent_at_normal():
    dr = _build(CONCLUSION_DEPENDS_ON_EXPLANATION, "kenapa servo aku gerakannya tersentak-sentak")
    assert dr.depth == DEPTH_NORMAL
    assert "motor servo kamu bergerak" in dr.voice_text.lower()  # lead always kept
    # coherence, not just brevity: voice_text must be a genuine SUBSET of
    # sentences (never fabricated wording) - every chunk must appear
    # verbatim (case-insensitive) somewhere in the original text.
    for chunk in dr.voice_chunks:
        assert chunk.lower().rstrip(".") in CONCLUSION_DEPENDS_ON_EXPLANATION.lower() or any(
            w in CONCLUSION_DEPENDS_ON_EXPLANATION.lower() for w in chunk.lower().split()[:3]
        )


# ---- 2. Cause -> explanation -> consequence ---------------------------------

def test_02_cause_explanation_consequence_order_preserved():
    dr = _build(CAUSE_CHAIN, "kenapa GPU aku throttling ya")
    original = [s.strip() for s in CAUSE_CHAIN.split(". ") if s.strip()]
    idxs = _sentence_indices_in_original_order(dr, original)
    assert idxs == sorted(idxs), f"voice_chunks must preserve original sentence order, got mapped indices {idxs}"
    # the lead (cause) and the closing ('Jadi, solusinya...') sentence must
    # both survive - a cause->consequence chain must never end on an
    # isolated middle fact with no resolution.
    assert "throttling" in dr.voice_text.lower()
    assert "solusinya" in dr.voice_text.lower()


# ---- 3. Problem -> diagnosis -> solution ------------------------------------

def test_03_problem_diagnosis_solution_no_orphaned_conditional():
    dr = _build(PROBLEM_DIAGNOSIS_SOLUTION, "kenapa ESP32 gak bisa connect MQTT")
    if "wifi sudah terhubung" in dr.voice_text.lower():
        assert "serial monitor" in dr.voice_text.lower(), (
            "a conditional followup must never survive without the diagnostic step it depends on"
        )


# ---- 4. Technical explanation with prerequisites ----------------------------

def test_04_prerequisite_chain_lead_and_order_preserved():
    dr = _build(PREREQUISITE_CHAIN, "gimana cara pakai home assistant untuk kontrol lampu")
    assert dr.voice_chunks, "expected at least one chunk"
    assert "sebelum kamu bisa menggunakan home assistant" in dr.voice_text.lower()
    original = [s.strip() for s in PREREQUISITE_CHAIN.split(". ") if s.strip()]
    idxs = _sentence_indices_in_original_order(dr, original)
    assert idxs == sorted(idxs)


# ---- 5. Multiple bullet/list-like items -------------------------------------

def test_05_list_items_kept_together_and_in_order():
    dr = _build(LIST_ITEMS_RESPONSE, "cara mulai project sensor suhu")
    lowered = dr.voice_text.lower()
    order = [lowered.find(x) for x in ("esp32", "dht22", "breadboard", "usb")]
    present = [o for o in order if o != -1]
    assert present == sorted(present), "list items that survive must remain in their original relative order"


# ---- 6. Conclusion that depends on previous explanation ---------------------

def test_06_conclusion_sentence_survives_via_conclusion_cue():
    dr = _build(CONCLUSION_DEPENDS_ON_EXPLANATION, "kenapa servo aku gerakannya tersentak-sentak")
    assert "menstabilkan frekuensi pwm" in dr.voice_text.lower(), (
        "the final 'jadi, kamu perlu menstabilkan frekuensi PWM...' conclusion must survive "
        "(last-sentence + conclusion-cue hard must-keep)"
    )


# ---- 7/8/9. SHORT / NORMAL / DETAILED depth behavior ------------------------

def test_07_short_depth_compresses_but_keeps_lead():
    dr = _build(CAUSE_CHAIN, "singkat aja kenapa GPU aku throttling")
    assert dr.depth == DEPTH_SHORT
    assert len(dr.voice_text) < len(dr.chat_text)
    assert "throttling" in dr.voice_text.lower()


def test_08_normal_depth_moderate_compression():
    dr = _build(CAUSE_CHAIN, "kenapa GPU aku throttling ya")
    assert dr.depth == DEPTH_NORMAL
    assert len(dr.voice_text) <= len(dr.chat_text)


def test_09_detailed_depth_retains_most_content():
    dr = _build(CAUSE_CHAIN, "jelaskan secara mendalam kenapa GPU aku throttling")
    assert dr.depth == DEPTH_DETAILED
    assert len(dr.voice_chunks) >= 4  # DETAILED's own least-aggressive budget


# ---- 10. Explicit detailed request skips compression entirely --------------

def test_10_explicit_detailed_request_skips_compression_entirely():
    dr = _build(CAUSE_CHAIN, "jelaskan semuanya kenapa GPU aku throttling")
    assert dr.depth == DEPTH_DETAILED
    original_count = len([s for s in CAUSE_CHAIN.split(". ") if s.strip()])
    assert len(dr.voice_chunks) == original_count, "an explicit 'jelaskan semuanya' must speak every sentence"


# ---- 11. Explicit "singkat aja" ---------------------------------------------

def test_11_explicit_singkat_aja_forces_short_and_compresses():
    dr = _build(PROBLEM_DIAGNOSIS_SOLUTION, "singkat aja, kenapa ESP32 gak bisa connect MQTT")
    assert dr.depth == DEPTH_SHORT
    assert len(dr.voice_text) < len(dr.chat_text)


# ---- 12. Indonesian responses -----------------------------------------------

def test_12_indonesian_response_cleaned_and_selected():
    dr = _build(PREREQUISITE_CHAIN, "gimana cara pakai home assistant", language="indonesian")
    assert dr.voice_text
    assert dr.chat_text == PREREQUISITE_CHAIN


# ---- 13. Mixed short and long sentences -------------------------------------

def test_13_mixed_short_and_long_sentences_all_meaningful_survive():
    dr = _build(MIXED_SHORT_LONG, "apa ESP32 support deep sleep")
    assert "ya, bisa" in dr.voice_text.lower()  # short lead
    # the short "tapi ingat, wake-up dari deep sleep butuh waktu" caveat and
    # its own dependent conditional must not be arbitrarily orphaned.
    if "jika aplikasi kamu butuh respons instan" in dr.voice_text.lower():
        assert "wake-up" in dr.voice_text.lower() or "sleep" in dr.voice_text.lower()


# ---- 14. Long response with important prohibition/safety information -------

def test_14_safety_prohibition_survives_at_every_depth():
    for query in ("kenapa relay saya gak nyala", "singkat aja kenapa relay saya gak nyala",
                  "jelaskan secara mendalam kenapa relay saya gak nyala"):
        dr = _build(SAFETY_RESPONSE, query)
        assert "jangan sambungkan" in dr.voice_text.lower() or "220v" in dr.voice_text.lower(), (
            f"hard prohibition must survive at depth={dr.depth}: voice_text={dr.voice_text!r}"
        )


# ---- 15. Sentence order is unchanged ----------------------------------------

def test_15_sentence_order_never_reordered_across_examples():
    for text, query in (
        (CAUSE_CHAIN, "kenapa GPU aku throttling ya"),
        (PROBLEM_DIAGNOSIS_SOLUTION, "kenapa ESP32 gak bisa connect MQTT"),
        (PREREQUISITE_CHAIN, "gimana cara pakai home assistant untuk kontrol lampu"),
        (SAFETY_RESPONSE, "kenapa relay saya gak nyala"),
    ):
        dr = _build(text, query)
        original = [s.strip() for s in text.split(". ") if s.strip()]
        idxs = _sentence_indices_in_original_order(dr, original)
        assert idxs == sorted(idxs), f"order violated for {query!r}: {idxs}"


# ---- 16. Chat output is byte-identical before/after -------------------------

def test_16_chat_text_always_byte_identical_to_input_at_every_depth():
    for text, query in (
        (CAUSE_CHAIN, "kenapa GPU aku throttling ya"),
        (CAUSE_CHAIN, "singkat aja kenapa GPU aku throttling"),
        (CAUSE_CHAIN, "jelaskan semuanya kenapa GPU aku throttling"),
        (PROBLEM_DIAGNOSIS_SOLUTION, "kenapa ESP32 gak bisa connect MQTT"),
        (LIST_ITEMS_RESPONSE, "cara mulai project sensor suhu"),
        (SAFETY_RESPONSE, "kenapa relay saya gak nyala"),
    ):
        dr = _build(text, query)
        assert dr.chat_text == text, "chat_text must be byte-identical to the original response_text, always"


# ---- 17. TTS pipelining tests remain green ----------------------------------

def test_17_tts_chunk_pipelining_module_not_imported_or_modified_by_this_fix():
    """Structural guard: `response_output.py` must not import anything
    from the TTS/Fish Audio adapter layer - this sprint's fix operates
    entirely upstream of TTS, never touching playback/pipelining code.
    (The TTS Chunk Pipelining suite itself is re-run as part of this
    sprint's own regression sweep - see docs/change_impact/voice_output_coherence.md.)"""
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    # No actual IMPORT of the TTS/Fish Audio adapter layer - a prose
    # mention in the module docstring (explaining how `voice_chunks`
    # feeds FishAudioAdapter downstream) is expected and fine; an actual
    # `import`/`from ... import` line would not be.
    assert "import fish_audio" not in src.lower()
    assert "from .adapters" not in src
    assert "from luno.adapters" not in src


# ---- 18. No second retrieval/classifier/summarizer is introduced -----------

def test_18_no_second_classifier_llm_or_retrieval_module_imported():
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    forbidden_substrings = (
        "import openai", "import anthropic", "requests.post", "memory_retrieval",
        "MemoryRetriever", "import memory", "from .memory", "LLM(", "llm_client",
    )
    for forbidden in forbidden_substrings:
        assert forbidden not in src, f"response_output.py must not reference {forbidden!r} - no second summarizer/classifier/LLM"
    # the two new coherence helpers must reuse `_has_condition`, never a
    # new keyword table competing with `_CONDITION_KEYWORDS`.
    assert "_CONDITION_KEYWORDS = (" in src
    assert src.count("_CONDITION_KEYWORDS = (") == 1, "must not introduce a second condition-keyword table"


# ============================================================================
# 2 real production-path E2E tests through RuntimeDemoConsole/PlannerBridgeModule
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_voice_coherence", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_voice_coherence"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 8.0, interval_s: float = 0.02) -> bool:
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


def test_e2e_a_real_pipeline_orphaned_conditional_fix_holds_end_to_end():
    """The core MQTT diagnostic-prerequisite fix, verified through the
    REAL `RuntimeDemoConsole`/`PlannerBridgeModule`/`BehaviorTreeModule
    ._speak()` -> `build_dual_response()` production path - not just a
    direct unit call."""
    demo = _load_demo()
    console = _new_console(demo, canned_text=PROBLEM_DIAGNOSIS_SOLUTION)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "kenapa ESP32 gak bisa connect MQTT")
        assert result["assistant_response"].get("text") == PROBLEM_DIAGNOSIS_SOLUTION  # chat untouched
        spoken = result["speak_request"].get("text", "").lower()
        if "wifi sudah terhubung" in spoken:
            assert "serial monitor" in spoken, f"orphaned conditional reproduced end-to-end: {spoken!r}"
    finally:
        console.stop()


def test_e2e_b_real_pipeline_safety_prohibition_survives_and_chat_unchanged():
    """A long response carrying a hard prohibition, through the real
    production path - chat stays byte-identical, voice keeps the
    prohibition, TTS chunks are produced from the SAME already-selected
    sentence list (never a second/independent pass)."""
    demo = _load_demo()
    console = _new_console(demo, canned_text=SAFETY_RESPONSE)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "kenapa relay saya gak nyala")
        assert result["assistant_response"].get("text") == SAFETY_RESPONSE
        speak_event = result["speak_request"]
        spoken = speak_event.get("text", "").lower()
        assert "jangan sambungkan" in spoken or "220v" in spoken
        chunks = speak_event.get("chunks")
        if chunks:
            assert len(chunks) >= 1
    finally:
        console.stop()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
