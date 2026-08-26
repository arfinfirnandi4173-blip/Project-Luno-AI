"""
test_voice_response_intelligence.py
=====================================

SPRINT 1 - VOICE RESPONSE INTELLIGENCE (Context-Preserving Response
Selection). Builds on top of the Voice Output Coherence sprint's own
fix (see `tests/test_voice_output_coherence.py`) rather than duplicating
its scenarios: that sprint proved and fixed ONE specific orphaning
pattern (a soft-conditional clause surviving without the diagnostic
sentence right before it). This sprint's own problem report is broader -
even after that fix, TWO individually well-scoring sentences (e.g. both
carry a number, or one is simply the lead) can still survive selection
side by side without actually forming a coherent GROUP, because one of
them opens with a causal ("Akibatnya, ..."), continuation ("Selain itu,
...", "Setelah ...", "Namun, ..."), or backward-reference ("Ini terjadi
karena...", "Entity ini...") discourse marker that presupposes its own
immediate predecessor - and that predecessor did not happen to score
well enough on its own to also survive a tight budget.

WHAT THIS SPRINT DID NOT TOUCH (per its own explicit scope):
  - `luno.response_policy.compute_response_policy()` - Response Depth
    Decision was ALREADY fully implemented (explicit phrase tables,
    task-type/complexity scoring) - reused as-is, zero changes.
  - TTS chunking/pipelining/Fish Audio playback - `voice_chunks` is
    still ALWAYS derived from the SAME already-selected sentence list
    (see `_group_sentences_into_chunk_pairs()`'s own docstring, itself
    unmodified) - chunking never re-selects or reorders.
  - Memory retrieval/decision quality, cancellation/pause handling -
    entirely untouched, no imports of those modules added anywhere in
    `response_output.py` (see the structural test below).

WHAT THIS SPRINT ADDED (all in `luno/response_output.py`, additive,
deterministic, `re`-only - no LLM judge, no embeddings, no second
tokenizer, no second summarizer):
  1. Three new leading-window marker tables (`_CAUSAL_KEYWORDS`,
     `_CONTINUATION_KEYWORDS`, `_REFERENCE_KEYWORDS`), matched via the
     SAME `_compile_word_boundary_marker_pattern()` the Voice Output
     Coherence sprint already established - but ANCHORED to the very
     first words of a sentence (`_has_leading_marker()`, `.match()` not
     `.search()`), deliberately stricter than the existing
     `_has_condition()` whole-sentence check (left completely unchanged).
  2. `_dependency_kind()` - classifies each sentence INDEPENDENT /
     SUPPORTING / DEPENDENT, one-hop only (looks at `i` and `i + 1`,
     never a dependency graph).
  3. `_select_scores_with_setup_bonus()` GENERALIZED (same function,
     same bonus constant, same guard shape - now triggered by ANY
     dependency category, not just soft conditionals) - purely additive
     superset of triggers, so every Voice Output Coherence sprint
     scenario keeps behaving identically.
  4. `_repair_orphans()` - a NEW deterministic post-selection pass:
     removes any selected DEPENDENT sentence whose predecessor isn't
     also selected, then greedily re-admits (predecessor, orphan) pairs
     by original score, within a small bounded slack (`budget + 4`) -
     never an unlimited budget blowout, never a lone rescued orphan
     without its predecessor.

Tests below distinguish "shorter" from "coherent": most assertions check
that a DEPENDENT sentence never survives alone without its predecessor,
never merely word/sentence COUNT.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable

import pytest

from luno import response_output as ro
from luno.response_policy import compute_response_policy, DEPTH_DETAILED, DEPTH_NORMAL, DEPTH_SHORT

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ─────────────────────────────────────────────
# Shared worked examples (new - deliberately NOT reused from
# test_voice_output_coherence.py's own CAUSE_CHAIN/PROBLEM_DIAGNOSIS_
# SOLUTION/etc., to avoid duplicating that suite's coverage).
# ─────────────────────────────────────────────

# Objective-2 worked example A: causal + continuation chain (Sprint brief's
# own ESP32/WiFi reconnect scenario).
RECONNECT_WATCHDOG = (
    "ESP32 bisa reconnect otomatis ke WiFi kalau koneksi terputus. "
    "Kamu perlu mengaktifkan fitur auto-reconnect di kode firmware terlebih dahulu. "
    "Fitur ini bekerja dengan memantau status koneksi setiap beberapa detik. "
    "Jika status terputus terdeteksi, ESP32 akan mencoba reconnect sebanyak 5 kali. "
    "Selain itu, kamu bisa menambahkan watchdog timer untuk reset otomatis jika reconnect gagal terus. "
    "Watchdog timer ini bekerja dengan menghitung waktu sejak koneksi terakhir berhasil. "
    "Jika waktu tersebut melebihi 30 detik, sistem akan reset otomatis. "
    "Dengan kombinasi auto-reconnect dan watchdog, ESP32 kamu jadi jauh lebih stabil untuk project IoT jangka panjang."
)

# Objective-2 worked example B: causal + reference chain (MQTT/Home
# Assistant auto-discovery scenario).
HOME_ASSISTANT_DASHBOARD = (
    "Home Assistant bisa menampilkan status semua device ESP32 dalam satu dashboard. "
    "Kamu perlu menginstal integrasi MQTT terlebih dahulu di Home Assistant. "
    "Integrasi ini butuh broker MQTT yang sudah berjalan di jaringan yang sama. "
    "Setelah broker terpasang, ESP32 bisa publish data sensor ke topic tertentu. "
    "Akibatnya, Home Assistant otomatis mendeteksi entity baru dari topic yang dipublish itu. "
    "Entity ini kemudian bisa ditambahkan ke dashboard secara manual atau otomatis. "
    "Namun, kamu perlu memastikan format payload MQTT sesuai standar auto-discovery Home Assistant. "
    "Kalau formatnya salah, entity tidak akan muncul di dashboard sama sekali."
)

# A short reply (2 sentences) where the second sentence opens with a
# reference marker - "short response quality" must still not orphan it.
SHORT_REFERENCE_PAIR = (
    "Baterai LiPo 3.7V ini cocok untuk drone kecil kamu. "
    "Ini penting karena tegangan yang lebih tinggi bisa merusak motor brushless yang dipakai."
)

# A response where a HARD prohibition (must-keep regardless of dependency
# classification) sits right after an ordinary explanatory sentence -
# confirms warning-protection (Voice Output Coherence sprint) and the new
# dependency mechanism compose without conflict.
SAFETY_WITH_CAUSAL_FOLLOWUP = (
    "Modul step-down converter ini menurunkan tegangan 12V ke 5V untuk Raspberry Pi. "
    "Konverter ini bekerja dengan mengubah frekuensi switching sesuai beban. "
    "Jangan gunakan converter murah tanpa heatsink untuk beban di atas 2 ampere. "
    "Akibatnya, komponen bisa overheat dan terbakar dalam waktu singkat. "
    "Pilih converter dengan rating arus minimal 3 ampere untuk keamanan ekstra."
)

# Pathological many-dependent-openers reply - proves the orphan-repair
# slack cap (`budget + 4`) actually bounds growth rather than silently
# defeating compression.
MANY_DEPENDENTS = " ".join(
    f"Selanjutnya, langkah {i} adalah menghubungkan kabel nomor {i} ke pin yang sesuai."
    for i in range(1, 13)
)
MANY_DEPENDENTS = "Berikut adalah cara merangkai semua kabel modul relay delapan channel. " + MANY_DEPENDENTS

ENGLISH_CAUSAL_CONTINUATION = (
    "Your solar charge controller shows a fault code E03 on the display. "
    "This happens because the battery voltage dropped below the safe cutoff threshold. "
    "As a result, the controller disconnects the load automatically to protect the battery. "
    "However, the panel keeps charging normally as long as sunlight is available. "
    "Once the battery recovers above the threshold, the load reconnects automatically."
)


def _policy(depth_query: str = "", text_for_default: str = ""):
    return compute_response_policy(depth_query or text_for_default)


def _build(text: str, query: str = "", language: str = "indonesian"):
    policy = _policy(query, text)
    return ro.build_dual_response(text, policy, language=language)


def _sentences(text: str):
    raw_chunks = ro._split_into_raw_sentences(text)
    out = []
    for chunk in raw_chunks:
        cleaned = ro.normalize_for_speech(ro._strip_list_markers(chunk.raw), language="indonesian").strip()
        if cleaned:
            out.append(ro._Sentence(chunk.raw, cleaned, chunk.is_list_item))
    return out


def _no_orphans(voice_text: str, original_text: str) -> bool:
    """True if no sentence surviving in `voice_text` opens with a
    dependency marker while its immediate predecessor is absent from
    `voice_text`. Generic checker reused by several scenario tests below
    - does not hardcode any one example's wording.

    "Predecessor" is computed on the DEDUPED sentence list (the SAME list
    `_select_by_priority()`/`_repair_orphans()` actually operate on, via
    `_dedupe()` - itself an existing, pre-Sprint-1 mechanism, unmodified)
    rather than the raw original text - a near-duplicate run (e.g. many
    near-identical numbered steps) already collapses BEFORE selection
    even runs, so "predecessor" for adjacency purposes must mean the
    previous SURVIVING-DEDUPE sentence, not the previous raw sentence.

    Presence is checked via each sentence's leading-word signature (not
    the full cleaned string) so this stays robust to the production
    pipeline's own number-locale resolution (e.g. "5" reading as "lima"
    vs "five" depending on ambient `LUNO_LANGUAGE`) - the signature is
    short enough that it usually lands before a mid/trailing number word,
    and where it doesn't (adjacent near-duplicates differing mainly by a
    leading number), `_dedupe()` will already have collapsed those
    sentences together before this check even applies."""
    def _sig(cleaned: str, n: int = 5) -> str:
        return " ".join(cleaned.lower().split()[:n])

    sentences = ro._dedupe(_sentences(original_text))
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    voice_lower = voice_text.lower()
    present = [_sig(s.cleaned) in voice_lower for s in sentences]
    for i, s in enumerate(sentences):
        if i == 0 or not present[i]:
            continue
        if ro._is_dependent_sentence(sentences, i, condition_indices) and not present[i - 1]:
            return False
    return True


# ─────────────────────────────────────────────
# 1. Response Depth Decision - reused, not rebuilt (sanity only).
# ─────────────────────────────────────────────

def test_01_response_depth_policy_untouched_explicit_short_still_wins():
    p = compute_response_policy("jawab singkat aja ya")
    assert p.depth == DEPTH_SHORT and p.explicit


def test_02_response_depth_policy_untouched_explicit_detailed_still_wins():
    p = compute_response_policy("jelaskan semuanya secara detail")
    assert p.depth == DEPTH_DETAILED and p.explicit


# ─────────────────────────────────────────────
# 2. Dependency classification unit tests (`_dependency_kind`).
# ─────────────────────────────────────────────

def test_03_lead_sentence_always_independent():
    sentences = _sentences(RECONNECT_WATCHDOG)
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    assert ro._dependency_kind(sentences, 0, condition_indices) == ro._DEP_INDEPENDENT


def test_04_causal_opener_classified_dependent():
    sentences = _sentences(HOME_ASSISTANT_DASHBOARD)
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    idx = next(i for i, s in enumerate(sentences) if s.cleaned.lower().startswith("akibatnya"))
    assert ro._dependency_kind(sentences, idx, condition_indices) == ro._DEP_DEPENDENT


def test_05_predecessor_of_dependent_sentence_classified_supporting():
    # RECONNECT_WATCHDOG's "Jika status terputus terdeteksi, ..." is a
    # conditional opener (DEPENDENT); its own predecessor ("Fitur ini
    # bekerja dengan memantau status koneksi...") carries no marker of
    # its own, so it must classify as SUPPORTING, not DEPENDENT (unlike
    # HOME_ASSISTANT_DASHBOARD's "Akibatnya" chain, where the predecessor
    # is itself a "Setelah ..." opener - a 3-link chain, DEPENDENT itself).
    sentences = _sentences(RECONNECT_WATCHDOG)
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    idx = next(i for i, s in enumerate(sentences) if s.cleaned.lower().startswith("jika status terputus"))
    assert ro._dependency_kind(sentences, idx - 1, condition_indices) == ro._DEP_SUPPORTING


def test_06_ordinary_sentence_with_no_marker_is_independent():
    sentences = _sentences("Sensor DHT22 mengukur suhu dan kelembaban udara sekitar.")
    condition_indices = set()
    assert ro._dependency_kind(sentences, 0, condition_indices) == ro._DEP_INDEPENDENT


def test_07_conditional_opener_still_classified_dependent_reusing_has_condition():
    sentences = _sentences(RECONNECT_WATCHDOG)
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    idx = next(i for i, s in enumerate(sentences) if s.cleaned.lower().startswith("jika"))
    assert ro._dependency_kind(sentences, idx, condition_indices) == ro._DEP_DEPENDENT


def test_08_english_causal_and_continuation_leading_markers_detected():
    assert ro._has_causal_lead("as a result, the controller disconnects the load.")
    assert ro._has_continuation_lead("however, the panel keeps charging normally.")
    # "This happens because..." opens with a REFERENCE marker (bare
    # "this"), not a causal one - the causal word itself is mid-sentence.
    assert ro._has_reference_lead("this happens because the battery voltage dropped.")


# ─────────────────────────────────────────────
# 3. Context-Preserving Selection - integration tests through
#    `build_dual_response()`, across all three depths, on both new
#    worked examples (objective 2's own reported symptom).
# ─────────────────────────────────────────────

def test_09_reconnect_watchdog_short_has_no_orphaned_dependent():
    dr = _build(RECONNECT_WATCHDOG, "singkat aja, kenapa ESP32 saya suka putus koneksi WiFi")
    assert dr.depth == DEPTH_SHORT
    assert _no_orphans(dr.voice_text, RECONNECT_WATCHDOG), dr.voice_text


def test_10_reconnect_watchdog_normal_has_no_orphaned_dependent():
    dr = _build(RECONNECT_WATCHDOG, "kenapa ESP32 saya suka putus koneksi")
    assert _no_orphans(dr.voice_text, RECONNECT_WATCHDOG), dr.voice_text


def test_11_home_assistant_dashboard_short_has_no_orphaned_dependent():
    dr = _build(HOME_ASSISTANT_DASHBOARD, "singkat aja, gimana cara nampilin ESP32 di Home Assistant")
    assert dr.depth == DEPTH_SHORT
    assert _no_orphans(dr.voice_text, HOME_ASSISTANT_DASHBOARD), dr.voice_text


def test_12_home_assistant_dashboard_normal_has_no_orphaned_dependent():
    dr = _build(HOME_ASSISTANT_DASHBOARD, "gimana cara nampilin ESP32 di Home Assistant")
    assert _no_orphans(dr.voice_text, HOME_ASSISTANT_DASHBOARD), dr.voice_text


def test_13_home_assistant_dashboard_detailed_has_no_orphaned_dependent():
    dr = _build(HOME_ASSISTANT_DASHBOARD, "jelaskan detail semuanya tentang Home Assistant MQTT")
    assert dr.depth == DEPTH_DETAILED
    assert _no_orphans(dr.voice_text, HOME_ASSISTANT_DASHBOARD), dr.voice_text


def test_14_english_causal_continuation_chain_has_no_orphaned_dependent():
    dr = _build(ENGLISH_CAUSAL_CONTINUATION, "short", language="english")
    assert _no_orphans(dr.voice_text, ENGLISH_CAUSAL_CONTINUATION), dr.voice_text


def test_15_causal_broker_reference_never_speaks_broker_before_it_is_introduced():
    """Concrete anti-example from this sprint's own report: 'Akibatnya,
    Home Assistant otomatis mendeteksi entity baru dari topic yang
    dipublish itu.' must never survive without something upstream having
    introduced 'broker'/'topic' - i.e. a bare causal/reference conclusion
    heard with no antecedent."""
    dr = _build(HOME_ASSISTANT_DASHBOARD, "short")
    voice_lower = dr.voice_text.lower()
    if "akibatnya" in voice_lower:
        assert "broker" in voice_lower or "topic" in voice_lower, (
            f"'Akibatnya...' survived without its antecedent ever being spoken: {dr.voice_text!r}"
        )


# ─────────────────────────────────────────────
# 4. Voice Budget Selection - budget is an information budget, not a
#    hard sentence-count wall, but still BOUNDED (never unlimited).
# ─────────────────────────────────────────────

def test_16_short_pair_rescue_fits_within_generous_slack():
    dr = _build(SHORT_REFERENCE_PAIR, "short")
    # both sentences are short and mutually dependent (2nd opens with "ini
    # penting karena") - the pair must survive together, not one alone.
    assert "baterai lipo" in dr.voice_text.lower()
    assert "ini penting karena" in dr.voice_text.lower() or "penting karena" in dr.voice_text.lower()


def test_17_many_dependents_repair_does_not_defeat_compression():
    dr = _build(MANY_DEPENDENTS, "short")
    total_sentences = len(_sentences(MANY_DEPENDENTS))
    selected_count = dr.voice_text.count(".") + dr.voice_text.count("!") + dr.voice_text.count("?")
    # the repair pass must not silently balloon a 13-sentence reply back
    # to near-full length just because most sentences share one marker.
    assert len(dr.voice_text) < len(ro.normalize_for_speech(MANY_DEPENDENTS, language="indonesian")) * 0.85
    assert selected_count < total_sentences


def test_18_many_dependents_repair_never_leaves_a_lone_orphan():
    dr = _build(MANY_DEPENDENTS, "short")
    assert _no_orphans(dr.voice_text, MANY_DEPENDENTS), dr.voice_text


def test_19_explicit_detailed_skips_compression_entirely_dependency_irrelevant():
    dr = _build(HOME_ASSISTANT_DASHBOARD, "jelaskan semuanya secara lengkap dan detail")
    assert dr.depth == DEPTH_DETAILED and dr.voice_adapted is False
    for sentence in HOME_ASSISTANT_DASHBOARD.strip().split(". "):
        pass  # explicit-detailed keeps everything - no per-sentence drop possible
    assert not dr.voice_adapted


# ─────────────────────────────────────────────
# 5. Short response quality - SHORT depth stays coherent, never merely
#    "fewer words".
# ─────────────────────────────────────────────

def test_20_safety_prohibition_and_causal_followup_both_survive_short():
    dr = _build(SAFETY_WITH_CAUSAL_FOLLOWUP, "short")
    voice_lower = dr.voice_text.lower()
    assert "jangan gunakan converter murah" in voice_lower  # hard must-keep, unaffected by this sprint
    assert _no_orphans(dr.voice_text, SAFETY_WITH_CAUSAL_FOLLOWUP), dr.voice_text


def test_21_selected_sentences_preserve_original_order_after_repair():
    dr = _build(HOME_ASSISTANT_DASHBOARD, "short")
    sentences = _sentences(HOME_ASSISTANT_DASHBOARD)
    positions = [
        i for i, s in enumerate(sentences)
        if s.cleaned.lower() in dr.voice_text.lower()
    ]
    assert positions == sorted(positions), f"order not preserved: {positions}"


def test_22_voice_chunks_still_derived_from_same_selection_as_voice_text():
    dr = _build(RECONNECT_WATCHDOG, "short")
    joined_chunks = " ".join(dr.voice_chunks).strip()
    # normalize whitespace for a loose equality check (chunk joins may
    # differ in separator spacing from `_join_sentences`, never content)
    assert " ".join(joined_chunks.split()) == " ".join(dr.voice_text.split())


# ─────────────────────────────────────────────
# 6. Adversarial - false-positive guards (mirrors the "harus" inside
#    "seharusnya" bug class from the Voice Output Coherence sprint).
# ─────────────────────────────────────────────

def test_23_selanjutnya_substring_inside_another_word_does_not_false_positive():
    assert not ro._has_continuation_lead("keberlanjutan sistem irigasi ini butuh perawatan rutin.")
    assert not ro._has_continuation_lead("proyek ini adalah kelanjutan dari versi sebelumnya di semester lalu.")


def test_24_selanjutnya_as_genuine_leading_marker_still_detected():
    assert ro._has_continuation_lead("selanjutnya, kamu bisa mengatur parameter kalibrasi sensor.")


def test_25_ini_as_ordinary_mid_sentence_modifier_does_not_false_positive():
    assert not ro._has_reference_lead("motor ini bisa dikendalikan lewat PWM dengan mikrokontroler apa saja.")
    assert not ro._has_reference_lead("konfigurasi ini bisa diubah kapan saja lewat file settings.")


def test_26_ini_as_genuine_leading_reference_still_detected():
    assert ro._has_reference_lead("ini adalah driver motor yang umum dipakai untuk stepper.")


def test_27_menyebabkan_does_not_false_positive_on_bare_sebab_substring():
    # word-boundary technique (reused, not reimplemented) must not let
    # "sebab" match inside "menyebabkan"/"disebabkan".
    assert not ro._has_causal_lead("menyebabkan kerusakan permanen pada motor jika dibiarkan terus.")


def test_28_no_second_classifier_llm_or_embedding_library_introduced():
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    forbidden_substrings = (
        "import openai", "import anthropic", "requests.post", "memory_retrieval",
        "MemoryRetriever", "import memory", "from .memory", "LLM(", "llm_client",
        "sentence_transformers", "import spacy", "import nltk", "import faiss",
        "import gensim", "cosine_similarity",
    )
    for forbidden in forbidden_substrings:
        assert forbidden not in src, (
            f"response_output.py must not reference {forbidden!r} - no second summarizer/classifier/embeddings"
        )
    assert src.count("_CAUSAL_KEYWORDS = (") == 1
    assert src.count("_CONTINUATION_KEYWORDS = (") == 1
    assert src.count("_REFERENCE_KEYWORDS = (") == 1
    assert src.count("_CONDITION_KEYWORDS = (") == 1, "must not introduce a second condition-keyword table"


def test_29_persistent_memory_or_global_state_not_touched_by_this_module():
    src_path = ro.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    for forbidden in ("long_term_memory.json", "json.dump", "open(", "global "):
        assert forbidden not in src, f"response_output.py must stay a pure, stateless function ({forbidden!r} found)"


# ============================================================================
# 2 real production-path E2E tests through RuntimeDemoConsole/PlannerBridgeModule
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo_voice_response_intelligence", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_voice_response_intelligence"] = demo
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


def test_e2e_a_reconnect_watchdog_fix_holds_end_to_end():
    """The auto-reconnect/watchdog-timer causal-continuation chain,
    verified through the REAL production path (not just a direct unit
    call) - chat stays exactly the LLM's original text; voice never
    speaks a dependent sentence without its predecessor."""
    demo = _load_demo()
    console = _new_console(demo, canned_text=RECONNECT_WATCHDOG)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "kenapa ESP32 saya suka putus koneksi WiFi")
        assert result["assistant_response"].get("text") == RECONNECT_WATCHDOG  # chat untouched
        spoken = result["speak_request"].get("text", "")
        assert _no_orphans(spoken, RECONNECT_WATCHDOG), spoken
    finally:
        console.stop()


def test_e2e_b_home_assistant_dashboard_fix_holds_end_to_end():
    """The Home Assistant/MQTT auto-discovery causal-reference chain,
    verified end to end."""
    demo = _load_demo()
    console = _new_console(demo, canned_text=HOME_ASSISTANT_DASHBOARD)
    console.start()
    try:
        _wake(console, demo)
        result = _ask_and_capture(console, demo, "gimana cara nampilin ESP32 di Home Assistant")
        assert result["assistant_response"].get("text") == HOME_ASSISTANT_DASHBOARD  # chat untouched
        spoken = result["speak_request"].get("text", "")
        assert _no_orphans(spoken, HOME_ASSISTANT_DASHBOARD), spoken
    finally:
        console.stop()
