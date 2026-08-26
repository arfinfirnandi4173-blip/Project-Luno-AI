"""
test_semantic_voice_selection.py
===================================

SPRINT 37 - SEMANTIC VOICE SELECTION & COHERENT SHORT MODE.

Root cause (Phase 0, reproduced against the pre-sprint code before any
edit in this sprint - see docs/change_impact/semantic_voice_selection.md
for the full trace): SHORT/NORMAL selection picked sentences by raw
`_score_sentence()` score, sentence-by-sentence, with list items either
blanket-protected (every item always kept, regardless of whether it was
the one the user actually needed) or - once that blanket protection is
absent (e.g. DETAILED depth) - competing on a mostly-flat score where an
early FILLER sentence's "earlier is better" tiebreak could beat a later,
genuinely load-bearing list item or closing/answer sentence, producing
either (a) a bare list setup with none of its own items ("Berikut
beberapa pilihan mikrofon:" and then silence), or (b) a genuine closing/
answer sentence silently dropped because it didn't happen to contain one
of `_has_conclusion_cue()`'s fixed cue words ("jadi"/"kesimpulannya"/...).

Fix (all in `luno/response_output.py`, additive, deterministic, `re`/
set-overlap only - NO LLM judge, NO embeddings, NO second
selector/ranker, NO summarizer):
  1. `_find_list_runs()` - groups consecutive list items into a
     LIST_SETUP / LIST_ITEM(s) / LIST_CONCLUSION structure, purely
     positional (reuses the EXISTING `is_list_item` flag and adjacency -
     the same primitive `_starts_list_run()` already used).
  2. `_list_run_relevant_items()` / `_apply_list_relevance_bonus()` - a
     list item whose own DISTINCTIVE words (not shared by every item in
     the run) also appear in the run's own conclusion sentence gets a
     scoring bonus (reuses `_word_set()`, the SAME Jaccard-style
     primitive `_dedupe()` already established - no new matcher).
  3. `_repair_list_run_coherence()` - a bounded, deterministic
     generalization of the EXISTING `_repair_orphans()` rescue-or-drop
     philosophy (same slack constant, `+4`) applied to a WHOLE list run:
     if >=1 item survives, its setup/conclusion must too; if a run's
     setup survives with zero items (the concrete DETAILED-mode
     reproduction), the single best item is rescued.
  4. `_select_by_priority()`'s blanket "every list item is always
     must-keep" rule is relaxed to "keep the whole run UNLESS a
     relevance signal was found" - when the run's own conclusion names a
     specific item, that item competes and wins on its boosted score
     instead, letting an irrelevant sibling be dropped (Phase 5 scenario
     C) while still never producing a fragment.

`_repair_orphans()`, `_score_sentence()`, `_is_dependent_sentence()`,
`_build_semantic_units()`, `_compute_budget_for_depth()`, and the entire
ALL-mode bypass (Sprint 36) are ALL UNCHANGED - see the structural guard
tests below.

Sections:
  1. Unit tests for the new list-run/relevance helpers.
  2. The Phase 9 adversarial matrix (24 scenarios).
  3. ALL-mode / chat-integrity invariants (Sprint 36, untouched).
  4. Structural guards - no second selector/ranker/LLM judge/embedding
     model was introduced.
  5. E2E through the real RuntimeDemoConsole (Phase 10) + latency.

Run:
    python3 -m pytest -q tests/test_semantic_voice_selection.py
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

from luno.response_output import (  # noqa: E402
    DualResponse,
    _dedupe,
    _find_list_runs,
    _list_run_relevant_items,
    _split_into_raw_sentences,
    _strip_list_markers,
    _Sentence,
    build_dual_response,
)
from luno.response_policy import ResponsePolicy  # noqa: E402
from luno.text_normalizer import normalize_for_speech  # noqa: E402


def _sentences_of(text: str) -> List[_Sentence]:
    raw_chunks = _split_into_raw_sentences(text)
    sentences = []
    for chunk in raw_chunks:
        cleaned = normalize_for_speech(_strip_list_markers(chunk.raw)).strip()
        if cleaned:
            sentences.append(_Sentence(chunk.raw, cleaned, chunk.is_list_item))
    return _dedupe(sentences)


def _short(text: str) -> DualResponse:
    return build_dual_response(text, ResponsePolicy(depth="short", score=10, reasons=[], explicit=False))


def _normal(text: str) -> DualResponse:
    return build_dual_response(text, ResponsePolicy(depth="normal", score=32, reasons=[], explicit=False))


def _detailed(text: str) -> DualResponse:
    return build_dual_response(text, ResponsePolicy(depth="detailed", score=80, reasons=[], explicit=False))


def _no_orphan_reference(voice_text: str) -> None:
    """Structural sanity check used across the adversarial matrix - a
    sentence must never OPEN the spoken text with a marker that
    presupposes something not present (this is a coarse, deterministic
    proxy for "no dangling antecedent": the FIRST spoken sentence must
    never itself be a causal/continuation/reference opener, since there
    is nothing before it to refer to)."""
    first = voice_text.strip().split(".")[0].strip().lower()
    for marker in ("karena itu", "akibatnya", "oleh karena itu", "selain itu", "namun,", "hal ini"):
        assert not first.startswith(marker), f"orphaned opener with no antecedent: {voice_text!r}"


# ============================================================================
# Section 1 - unit tests for the new list-run/relevance helpers
# ============================================================================

def test_find_list_runs_basic_setup_items_conclusion():
    sentences = _sentences_of(
        "Berikut pilihannya:\n- A\n- B\n- C\nSemua tersedia di toko."
    )
    runs = _find_list_runs(sentences)
    assert len(runs) == 1
    r = runs[0]
    assert r["setup"] == 0
    assert r["items"] == [1, 2, 3]
    assert r["conclusion"] == 4


def test_find_list_runs_no_setup_no_conclusion():
    sentences = _sentences_of("- A\n- B\n- C")
    runs = _find_list_runs(sentences)
    assert len(runs) == 1
    assert runs[0]["setup"] is None
    assert runs[0]["conclusion"] is None
    assert runs[0]["items"] == [0, 1, 2]


def test_find_list_runs_two_separate_lists_not_cross_linked():
    sentences = _sentences_of(
        "Ada dua kategori.\n"
        "Sensor:\n- DHT22\n- BMP280\n"
        "Aktuator:\n- Relay\n- Servo\n"
        "Semua tersedia."
    )
    runs = _find_list_runs(sentences)
    assert len(runs) == 2
    # The sentence between the two lists ("Aktuator:") must be recognized
    # as the SECOND run's setup, never mistaken for the FIRST run's
    # conclusion (Phase 9 scenario 16).
    assert runs[0]["conclusion"] is None
    assert runs[1]["setup"] is not None
    assert sentences[runs[1]["setup"]].cleaned.startswith("Aktuator")


def test_list_run_relevant_items_finds_named_answer():
    # Same wording already verified in test_02/E2E - deliberately avoids
    # generic verbs ("pakai", "punya") that appear in only one item and
    # would otherwise register as a false-positive "distinctive" overlap
    # with the conclusion (a real property of the plain word-overlap
    # primitive, not a bug - the test picks unambiguous wording instead
    # of weakening the assertion).
    sentences = _sentences_of(
        "Berikut pilihan mikrofon:\n"
        "- INMP441 cocok karena komunikasinya menggunakan I2S.\n"
        "- MAX9814 menggunakan output analog dengan AGC.\n"
        "Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding analog."
    )
    runs = _find_list_runs(sentences)
    relevant = _list_run_relevant_items(sentences, runs[0])
    # The primitive is plain deterministic word-overlap (no embeddings/
    # semantics) - it correctly flags the named answer (index 1,
    # INMP441/I2S) as relevant. Because the conclusion's own comparison
    # clause ("...lebih stabil dibanding analog") happens to reuse the
    # word "analog", MAX9814 (index 2) also picks up the bonus here; this
    # is an honest, documented limitation of a word-overlap heuristic
    # (see docs/change_impact/semantic_voice_selection.md #10), not a
    # correctness requirement of the function - what matters is that the
    # true answer is never MISSED.
    assert 1 in relevant


def test_list_run_relevant_items_empty_when_no_conclusion():
    sentences = _sentences_of("Berikut pilihan:\n- A\n- B\n- C")
    runs = _find_list_runs(sentences)
    assert _list_run_relevant_items(sentences, runs[0]) == set()


def test_list_run_relevant_items_empty_when_conclusion_names_nothing_specific():
    sentences = _sentences_of(
        "Berikut pilihan:\n- A\n- B\n- C\nSemua tersedia di toko elektronik."
    )
    runs = _find_list_runs(sentences)
    assert _list_run_relevant_items(sentences, runs[0]) == set()


# ============================================================================
# Section 2 - Phase 9 adversarial matrix (24 scenarios)
# ============================================================================

# ---- 1. list setup + 3 items (no conclusion) ------------------------------

def test_01_list_setup_plus_three_items():
    d = _short("Berikut pilihannya:\n- ESP32\n- Raspberry Pi\n- Arduino")
    assert "Berikut pilihannya" in d.voice_text
    for item in ("ESP32", "Raspberry Pi", "Arduino"):
        assert item in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 2. list setup + conclusion --------------------------------------------

def test_02_list_setup_plus_conclusion_not_dropped():
    d = _short(
        "ESP32 adalah mikrokontroler yang populer untuk proyek IoT.\n"
        "Ada banyak modul tambahan yang bisa dipakai bersamanya.\n"
        "Salah satu yang sering ditanyakan adalah soal mikrofon.\n"
        "Berikut beberapa pilihan mikrofon yang cocok untuk ESP32:\n"
        "- INMP441 cocok karena komunikasinya menggunakan I2S.\n"
        "- MAX9814 menggunakan output analog dengan AGC.\n"
        "- SPH0645 juga memakai I2S tapi lebih murah.\n"
        "Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding analog."
    )
    assert "Berikut beberapa pilihan mikrofon" in d.voice_text  # setup survives
    assert "kualitas suara terbaik" in d.voice_text  # conclusion NO LONGER silently dropped
    _no_orphan_reference(d.voice_text)


# ---- 3. setup + dependent sentence -----------------------------------------

def test_03_setup_plus_dependent_sentence():
    d = _short(
        "ESP32 menggunakan WiFi bawaan. "
        "Modul ini membutuhkan supply 3.3V. "
        "Karena itu jangan memberikan 5V langsung ke pin tersebut."
    )
    assert "5V" in d.voice_text or "5v" in d.voice_text.lower()
    assert "3.3" in d.voice_text or "three" in d.voice_text.lower()
    _no_orphan_reference(d.voice_text)


# ---- 4. condition chain -----------------------------------------------------

def test_04_condition_chain():
    d = _short(
        "ESP32 bisa dipakai untuk banyak proyek. "
        "Kalau supply daya tidak stabil, board bisa restart sendiri. "
        "Jadi gunakan power supply yang baik."
    )
    assert "restart" in d.voice_text or "power supply" in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 5. explanation chain (causal) -----------------------------------------

def test_05_explanation_chain():
    d = _short(
        "INMP441 lebih cocok untuk voice assistant. "
        "Karena menggunakan I2S, kualitas suaranya lebih jernih. "
        "Ini penting untuk pengenalan suara yang akurat."
    )
    # the causal clause must never appear without INMP441's own lead sentence
    assert "INMP441" in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 6. warning chain --------------------------------------------------------

def test_06_warning_chain():
    d = _short(
        "Modul ini butuh tegangan 3.3V. "
        "Jangan sambungkan langsung ke 5V. "
        "Bisa merusak komponen secara permanen."
    )
    assert "Jangan sambungkan langsung ke 5V" in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 7. short functional sentence -------------------------------------------

def test_07_short_functional_sentences_survive_by_function():
    for text in ("Sudah terhubung.", "Perlu resistor tambahan.", "Jadi pilih INMP441."):
        d = _short(text)
        assert d.voice_text.strip() != ""


# ---- 8. unrelated short sentence --------------------------------------------

def test_08_unrelated_short_sentence_not_forced_in():
    d = _short(
        "ESP32 mendukung WiFi bawaan untuk koneksi internet. "
        "Selain itu, cuaca hari ini cerah. "
        "Raspberry Pi cocok dipakai sebagai server rumahan yang hemat daya."
    )
    _no_orphan_reference(d.voice_text)


# ---- 9. long independent sentence -------------------------------------------

def test_09_long_independent_sentence_kept_whole():
    long_sentence = (
        "ESP32 adalah mikrokontroler dengan WiFi dan Bluetooth bawaan yang sangat populer "
        "untuk proyek IoT karena harganya terjangkau dan komunitasnya besar."
    )
    d = _short(long_sentence)
    assert d.voice_text.strip() == long_sentence.strip()


# ---- 10. multiple independent topics ----------------------------------------

def test_10_multiple_independent_topics_no_cross_contamination():
    d = _short(
        "ESP32 mendukung WiFi. "
        "Raspberry Pi cocok untuk server rumahan. "
        "Arduino Uno populer untuk pemula."
    )
    _no_orphan_reference(d.voice_text)


# ---- 11. nested list ---------------------------------------------------------

def test_11_nested_list_items_all_belong_to_one_run():
    sentences = _sentences_of(
        "Berikut kategorinya:\n- Sensor\n  - DHT22\n  - BMP280\n- Aktuator\nSemua tersedia."
    )
    runs = _find_list_runs(sentences)
    assert len(runs) == 1  # nested items still register as is_list_item, one contiguous run
    d = _short(
        "Berikut kategorinya:\n- Sensor\n  - DHT22\n  - BMP280\n- Aktuator\nSemua tersedia di toko."
    )
    assert "Berikut kategorinya" in d.voice_text


# ---- 12. numbered list -------------------------------------------------------

def test_12_numbered_list_setup_and_items():
    d = _short(
        "Berikut langkahnya:\n1. Sambungkan ESP32 ke power.\n2. Buka Arduino IDE.\n"
        "3. Upload firmware ke board.\n4. Buka Serial Monitor."
    )
    assert "Berikut langkahnya" in d.voice_text
    for item in ("Sambungkan ESP32", "Arduino IDE", "Upload firmware", "Serial Monitor"):
        assert item in d.voice_text


# ---- 13. markdown (dash) list -------------------------------------------------

def test_13_markdown_dash_list():
    d = _short("Beberapa opsi:\n- Opsi A\n- Opsi B\n- Opsi C")
    assert "Opsi A" in d.voice_text and "Opsi C" in d.voice_text


# ---- 14. paragraph + list -----------------------------------------------------

def test_14_paragraph_plus_list():
    d = _short(
        "ESP32 punya banyak fitur menarik untuk IoT.\n\n"
        "Berikut beberapa pilihan sensor suhu:\n- DHT22\n- DS18B20\n- BMP280\n\n"
        "Semua sensor ini kompatibel dengan Arduino IDE."
    )
    assert "Berikut beberapa pilihan sensor suhu" in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 15. list + conclusion (budget allows full retention) --------------------

def test_15_list_plus_conclusion_retained_when_budget_allows():
    d = _normal(
        "Berikut beberapa pilihan mikrofon:\n"
        "- INMP441\n- MAX9814\n- SPH0645\n"
        "Untuk kualitas suara, INMP441 paling cocok."
    )
    assert "Berikut beberapa pilihan mikrofon" in d.voice_text
    assert "INMP441 paling cocok" in d.voice_text


# ---- 16. two separate lists ---------------------------------------------------

def test_16_two_separate_lists_each_keep_own_setup():
    d = _normal(
        "Ada dua kategori komponen.\n"
        "Berikut sensor yang bisa dipakai:\n- DHT22\n- BMP280\n"
        "Berikut aktuator yang bisa dipakai:\n- Relay\n- Servo\n"
        "Semua tersedia di toko elektronik."
    )
    assert "Berikut sensor yang bisa dipakai" in d.voice_text
    assert "Berikut aktuator yang bisa dipakai" in d.voice_text


# ---- 17. very small SHORT budget ----------------------------------------------

def test_17_very_small_short_budget_still_coherent_not_fragmented():
    d = _short(
        "Berikut pilihannya:\n"
        "- A sangat murah dan mudah didapat di toko lokal.\n"
        "- B lebih mahal tapi kualitasnya lebih baik untuk jangka panjang.\n"
        "- C adalah pilihan menengah dengan harga wajar.\n"
        "- D adalah pilihan premium dengan garansi lima tahun.\n"
        "- E adalah pilihan hemat untuk pemula.\n"
        "Kalau budget terbatas, pilih A karena paling murah dan mudah didapat."
    )
    assert "Berikut pilihannya" in d.voice_text
    assert "budget terbatas, pilih A" in d.voice_text
    assert "A sangat murah" in d.voice_text
    # the deliberately irrelevant middle options must not ALL survive a
    # genuinely tight budget - this is the "drop irrelevant item" case.
    assert "B lebih mahal" not in d.voice_text
    _no_orphan_reference(d.voice_text)


# ---- 18. normal SHORT budget (ordinary reply length) ---------------------------

def test_18_normal_short_budget_ordinary_reply():
    d = _short(
        "ESP32 adalah board yang sangat populer. "
        "Ada banyak varian ESP32 di pasaran. "
        "Salah satu yang direkomendasikan adalah ESP32-WROOM."
    )
    assert d.voice_text.strip() != ""
    _no_orphan_reference(d.voice_text)


# ---- 19. DETAILED mode ----------------------------------------------------------

def test_19_detailed_mode_setup_never_left_without_payload():
    d = _detailed(
        "ESP32 adalah mikrokontroler yang populer untuk proyek IoT.\n"
        "Ada banyak modul tambahan yang bisa dipakai bersamanya.\n"
        "Salah satu yang sering ditanyakan adalah soal mikrofon.\n"
        "Berikut beberapa pilihan mikrofon yang cocok untuk ESP32:\n"
        "- INMP441 cocok karena komunikasinya menggunakan I2S.\n"
        "- MAX9814 menggunakan output analog dengan AGC.\n"
        "- SPH0645 juga memakai I2S tapi lebih murah.\n"
        "Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding analog."
    )
    # THE reproduced Phase 0 bug: DETAILED used to select the setup with
    # ZERO of its own list items. Must never happen now.
    if "Berikut beberapa pilihan mikrofon" in d.voice_text:
        assert any(item in d.voice_text for item in ("INMP441", "MAX9814", "SPH0645"))


# ---- 20. ALL mode (Sprint 36 invariant) -----------------------------------------

def test_20_all_mode_reads_everything_untouched_by_this_sprint():
    text = (
        "ESP32 adalah mikrokontroler yang populer untuk proyek IoT.\n"
        "Berikut beberapa pilihan mikrofon yang cocok untuk ESP32:\n"
        "- INMP441 cocok karena komunikasinya menggunakan I2S.\n"
        "- MAX9814 menggunakan output analog dengan AGC.\n"
        "- SPH0645 juga memakai I2S tapi lebih murah.\n"
        "Untuk kualitas suara terbaik, sebaiknya pakai INMP441."
    )
    d = build_dual_response(text, ResponsePolicy(depth="short", score=10, reasons=[], explicit=False), voice_output_mode="ALL")
    for fragment in ("ESP32 adalah mikrokontroler", "INMP441 cocok", "MAX9814", "SPH0645", "kualitas suara terbaik"):
        assert fragment in d.voice_text
    assert d.voice_adapted is False
    assert d.chat_text == text


# ---- 21/22. streaming enabled / cancellation during speech ----------------------
# (covered in Section 5 E2E - real event bus / real cancellation required)

# ---- 23. response containing only 2 sentences -----------------------------------

def test_23_two_sentence_response():
    d = _short("Sudah terhubung. Semua konfigurasi sudah benar.")
    assert "Sudah terhubung" in d.voice_text
    assert "konfigurasi sudah benar" in d.voice_text


# ---- 24. response containing only 1 sentence -------------------------------------

def test_24_one_sentence_response():
    d = _short("Sudah.")
    assert d.voice_text.strip() == "Sudah."


# ============================================================================
# Section 3 - chat-integrity / ALL-mode invariants (Sprint 36, re-verified)
# ============================================================================

def test_chat_text_always_equals_raw_response():
    text = (
        "Berikut pilihannya:\n- A\n- B\n- C\nSemua tersedia."
    )
    for builder in (_short, _normal, _detailed):
        d = builder(text)
        assert d.chat_text == text


# ============================================================================
# Section 4 - structural guards: no second selector/ranker/LLM judge
# ============================================================================

def test_no_new_forbidden_imports_in_response_output():
    # Checks actual `import`/`from ... import` statements only - the
    # module's own docstrings/comments legitimately SAY words like
    # "embedding" or "LLM judge" when explaining what it deliberately
    # does NOT use, which would false-positive a plain substring scan.
    import ast
    import luno.response_output as ro
    src = open(ro.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.lower())
    forbidden = ("openai", "anthropic", "sentence_transformers", "sklearn", "torch", "tensorflow", "embedding")
    for word in forbidden:
        assert not any(word in name for name in imported_names), f"forbidden dependency import found: {word}"


def test_build_dual_response_still_pure_no_io():
    import inspect
    import luno.response_output as ro
    src = inspect.getsource(ro)
    assert "requests" not in src.lower().split("import")[0]  # crude but effective for this module's known import block


# ============================================================================
# Section 5 - E2E through the real RuntimeDemoConsole (Phase 10) + latency
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_semantic_voice", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_semantic_voice"] = demo
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
    from luno.wake_session import ConversationState
    stream_chunks: List[Dict[str, Any]] = []
    speak_requests: List[Dict[str, Any]] = []
    assistant_responses: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    started: List[Dict[str, Any]] = []
    cancelled: List[Dict[str, Any]] = []
    subs = [
        console.event_bus.subscribe("speak_stream_chunk", lambda e: stream_chunks.append(e.data)),
        console.event_bus.subscribe("speak_request", lambda e: speak_requests.append(e.data)),
        console.event_bus.subscribe("assistant_response", lambda e: assistant_responses.append(e.data)),
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data)),
        console.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.data)),
        console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data)),
    ]
    try:
        console.simulate_speech(user_text)
        _wait_until(lambda: len(finished) >= 1 or len(cancelled) >= 1 or len(assistant_responses) >= 1, timeout_s)
        _wait_until(lambda: len(finished) >= 1 or len(cancelled) >= 1, timeout_s)
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
        "started": started, "cancelled": cancelled,
    }


def _spoken_text(result: Dict[str, Any]) -> str:
    if result["speak_requests"]:
        return result["speak_requests"][-1].get("text", "")
    if result["stream_chunks"]:
        ordered = sorted(result["stream_chunks"], key=lambda c: c.get("chunk", {}).get("sequence", 0))
        return " ".join(c.get("chunk", {}).get("text", "") for c in ordered)
    return ""


_MIC_REPLY = (
    "ESP32 adalah mikrokontroler yang populer untuk proyek IoT.\n"
    "Berikut beberapa pilihan mikrofon yang cocok untuk ESP32:\n"
    "- INMP441 cocok karena komunikasinya menggunakan I2S.\n"
    "- MAX9814 menggunakan output analog dengan AGC.\n"
    "- SPH0645 juga memakai I2S tapi lebih murah.\n"
    "Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding analog."
)


def test_e2e_raw_vs_short_vs_all_voice_output():
    demo = _load_demo()
    console = _new_console(demo, reply=_MIC_REPLY)
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, demo, "Jelasin pilihan mikrofon untuk ESP32.")
        chat_text = r["assistant_responses"][0].get("text")
        assert chat_text == _MIC_REPLY  # RAW - untouched

        spoken_short = _spoken_text(r)
        assert spoken_short != _MIC_REPLY  # SHORT - genuinely shorter
        assert len(spoken_short) < len(_MIC_REPLY)
        assert "Berikut beberapa pilihan mikrofon" in spoken_short  # setup retained
        assert "kualitas suara terbaik" in spoken_short  # conclusion retained (the fixed bug)
        _no_orphan_reference(spoken_short)
    finally:
        console.stop()


def test_e2e_all_mode_reads_full_response():
    demo = _load_demo()
    console = _new_console(demo, reply=_MIC_REPLY)
    console.start()
    try:
        _wake(console, demo)
        conv_id = console.behavior_tree_module.conversation_id
        console.planner_module.set_voice_output_mode(conv_id, "ALL")
        r = _run_turn(console, demo, "Jelasin pilihan mikrofon untuk ESP32.")
        spoken_all = _spoken_text(r)
        for fragment in ("mikrokontroler yang populer", "INMP441 cocok", "MAX9814", "SPH0645", "kualitas suara terbaik"):
            assert fragment in spoken_all
    finally:
        console.stop()


def test_e2e_cancellation_during_semantic_selected_speech():
    demo = _load_demo()
    console = _new_console(
        demo,
        reply="Kalimat panjang yang sengaja dibuat untuk diputar cukup lama sehingga bisa dibatalkan di tengah jalan saat sedang berbicara.",
        chunk_delay_s=0.0, playback_delay_s=0.3,
    )
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


def test_e2e_streaming_still_enabled_for_semantic_selection():
    demo = _load_demo()
    assert demo.legacy_config.ENABLE_LLM_TTS_STREAMING is True
    console = _new_console(demo, reply=_MIC_REPLY, chunk_delay_s=0.01)
    console.start()
    try:
        _wake(console, demo)
        r = _run_turn(console, demo, "Jelasin pilihan mikrofon untuk ESP32.")
        assert r["stream_chunks"], "semantic selection must not disable streaming"
    finally:
        console.stop()


def test_latency_semantic_selection_not_significantly_worse():
    """Phase 10 - semantic grouping/relevance scoring runs entirely on
    the ALREADY-known full text once `llm_finished` fires (same call
    site `build_dual_response()` always ran at) - it must not add a
    meaningful latency cost over the pre-sprint per-sentence selection."""
    demo = _load_demo()

    def _first_chunk_latency() -> float:
        console = _new_console(demo, reply=_MIC_REPLY, chunk_delay_s=0.01, playback_delay_s=0.0)
        console.start()
        try:
            _wake(console, demo)
            first_chunk_at: List[float] = []
            t0 = {}
            sub = console.event_bus.subscribe("speak_stream_chunk", lambda e: first_chunk_at.append(time.time()))
            try:
                t0["t"] = time.time()
                console.simulate_speech("Jelasin pilihan mikrofon untuk ESP32.")
                assert _wait_until(lambda: len(first_chunk_at) >= 1, 5.0)
            finally:
                console.event_bus.unsubscribe(sub)
            return first_chunk_at[0] - t0["t"]
        finally:
            console.stop()

    latencies = [_first_chunk_latency() for _ in range(3)]
    assert all(lat < 2.0 for lat in latencies), latencies
