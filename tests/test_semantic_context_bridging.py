"""
test_semantic_context_bridging.py
===================================

SPRINT 43 - SEMANTIC CONTEXT BRIDGING & MEMORY PRECISION.

Covers `luno.memory_context`'s new bounded, deterministic, additive
lexical-normalization layer (`_strip_bounded_affixes()`,
`_normalize_terms_for_bridging()`, `_normalize_query_tokens_for_bridging()`,
`_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES`), its integration as a
STRICTLY WEAKER, fallback-only evidence tier inside `select_topic_
candidates()` and `select_temporal_fallback_candidate()`, and the new
`is_active_topic_relevant_to_query()` guard `main_runtime_demo.py`'s
pre-existing Sprint-4 single-slot recency branch now consults.

Root cause (Phase 0-2, live reproduction via RuntimeDemoConsole before any
code changed): `select_topic_candidates()`'s overlap check compared RAW
tokens only, so a follow-up using a morphological variant ("pembeliannya"
for "beli") or colloquial synonym ("mikrofon" for "mic") of the original
statement's wording shared no token with the stored entry and correctly,
safely matched nothing - but that emptiness then let an UNGUARDED
mechanism win by default: the pre-existing single-slot `_active_topic`
recency fallback, which fired whenever `is_short_followup` was true and
an active snapshot existed, with no check on whether the query's own
words related to it at all. Live reproduction found this produced a
WRONG topic (not just a missed one) whenever an unrelated "decoy" topic
happened to be the most recent one in history (Scenarios D/G), and could
also GUESS between two equally-plausible topics rather than reject
ambiguity (Scenario E).

Fix, two additive parts: (1) a bounded normalization layer, consulted
ONLY as a fallback tier after raw-token matching has already had first
refusal and found nothing - every existing exact-match behavior is
UNCHANGED; (2) a new relevance guard on the pre-existing single-slot
branch, ambiguity-aware across the full bounded topic history (not just
the one active snapshot), so a normalized-only match that ties with
another history entry is correctly rejected rather than guessed.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import threading
import time
from typing import Callable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402
from luno.memory_context import ActiveTopicSnapshot  # noqa: E402


# ============================================================================
# Shared E2E harness (same pattern as test_cross_system_conversation_
# consistency.py / test_temporal_memory_timeline_awareness.py)
# ============================================================================

def _load_demo(tag: str = "sxb"):
    unique = f"main_runtime_demo_{tag}_{id(object())}"
    demo_spec = importlib.util.spec_from_file_location(unique, os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[unique] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 6.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _build_client(demo, replies):
    from luno.adapters import MockOpenRouterClient

    class _Client(MockOpenRouterClient):
        def __init__(self):
            super().__init__(canned_text=None)

        def _resolve_text(self, messages):
            text = messages[0]["content"] if messages else ""
            for key, val in replies.items():
                if key.strip() in text or text.strip() == key.strip():
                    return val
            return "(no canned reply configured for this turn)"

    return _Client()


def _new_console(demo, replies=None, canned_text="Oke."):
    if replies:
        client = _build_client(demo, replies)
    else:
        from luno.adapters import MockOpenRouterClient
        client = MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0)
    return demo.RuntimeDemoConsole(openrouter_client=client)


def _run_turn(console, demo, text, request_id, conversation_id=None):
    done = threading.Event()

    def _capture(e):
        if e.get("request_id") == request_id:
            done.set()

    sub = console.event_bus.subscribe("assistant_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(done.is_set, 6.0), f"no assistant_response for {request_id!r} within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "active-topic/topic-history update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)


def _run_turn_capture_prompt(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture_prompt(e):
        if e.get("request_id") == request_id:
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture_prompt)
    try:
        _run_turn(console, demo, text, request_id, conversation_id=conversation_id)
        _wait_until(need_llm.is_set, 3.0)
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


def _lines_starting(sp: str, *prefixes: str) -> list:
    out = []
    for line in sp.splitlines():
        s = line.strip()
        if any(s.startswith(p) for p in prefixes):
            out.append(s)
    return out


_ANY_CANDIDATE_PREFIXES = (
    "- Active conversation topic", "- Referenced item", "- Previously stated",
    "- Planned", "- Completed", "- Cancelled",
)


# ============================================================================
# Section 1 - _strip_bounded_affixes() unit tests
# ============================================================================

def test_01_strip_suffix_an_nominalization():
    assert memory_context._strip_bounded_affixes("pembelian") == "beli"


def test_02_strip_suffix_and_prefix_penggantian():
    assert memory_context._strip_bounded_affixes("penggantian") == "ganti"


def test_03_strip_clitic_nya_then_derivational():
    assert memory_context._strip_bounded_affixes("pembeliannya") == "beli"


def test_04_strip_kan_suffix():
    assert memory_context._strip_bounded_affixes("naikkan") == "naik"


def test_05_strip_men_prefix():
    assert memory_context._strip_bounded_affixes("menggunakan") in ("guna", "gunakan")


def test_06_strip_di_prefix():
    assert memory_context._strip_bounded_affixes("diganti") == "ganti"


def test_07_no_affix_word_unchanged():
    assert memory_context._strip_bounded_affixes("gpu") == "gpu"


def test_08_short_root_guard_never_over_strips():
    # "di" + "a" would leave a 1-char root - must refuse and return
    # the token unchanged rather than fabricate a nonsense root.
    result = memory_context._strip_bounded_affixes("dia")
    assert len(result) >= memory_context._MIN_AFFIX_ROOT_LEN or result == "dia"


def test_09_english_ing_suffix():
    assert memory_context._strip_bounded_affixes("upgrading") == "upgrad"


def test_10_english_ed_suffix():
    assert memory_context._strip_bounded_affixes("upgraded") == "upgrad"


def test_11_never_returns_empty_string():
    for tok in ["a", "di", "ke", "se", "", "x", "ok"]:
        result = memory_context._strip_bounded_affixes(tok)
        assert result != "" or tok == ""


def test_12_short_entity_identifiers_unaffected():
    # Real product identifiers must never be mistaken for affixed words.
    for tok in ["esp32", "rtx", "gpu", "ram", "esp8266", "inmp441"]:
        assert memory_context._strip_bounded_affixes(tok) == tok


# ============================================================================
# Section 2 - synonym groups / phrase table unit tests
# ============================================================================

def test_13_mic_synonym_group_canonicalizes():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("mikrofon") == canon.get("mic") == canon.get("microphone")


def test_14_pump_synonym_group_canonicalizes():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("pump") == canon.get("pompa")


def test_15_board_synonym_group_canonicalizes():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("mikrokontroler") == canon.get("board") == canon.get("microcontroller")


def test_16_upgrade_ganti_synonym_group_canonicalizes():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("ganti") == canon.get("upgrade") == canon.get("naikkan")


def test_17_synonym_groups_never_contain_specific_entities():
    """Structural/architectural invariant: this sprint's own explicit
    prohibition against hardcoding Luno-specific product/entity names."""
    forbidden_entities = {
        "esp32", "esp8266", "rtx3060", "rtx", "inmp441", "max9814",
        "sph0645", "ws2812b", "sk6812", "synology", "qnap",
    }
    all_members = {m for group in memory_context._TOKEN_SYNONYM_GROUPS for m in group}
    assert not (all_members & forbidden_entities), (
        f"synonym groups must never contain specific entity names, found: {all_members & forbidden_entities}"
    )


def test_18_synonym_groups_bounded_small():
    """Anti-hardcoding structural check: the brief's own explicit
    "do not create a giant synonym dictionary" - a generous but bounded
    ceiling, not a growth target."""
    total_members = sum(len(g) for g in memory_context._TOKEN_SYNONYM_GROUPS)
    assert total_members <= 40, f"synonym layer has grown to {total_members} members - review for scope creep"


def test_19_phrase_table_bridges_kartu_grafis_to_gpu():
    expanded = memory_context._normalize_query_tokens_for_bridging("Kalau kartu grafis itu gimana?", {"kartu", "grafis"})
    assert "gpu" in expanded


def test_20_normalize_terms_is_purely_additive():
    """Normalization must never REMOVE a token - only add derived/alias
    forms alongside it, so raw-match behavior is always still possible."""
    original = frozenset({"pembelian", "gpu"})
    expanded = memory_context._normalize_terms_for_bridging(original)
    assert original <= expanded


# ============================================================================
# Section 3 - select_topic_candidates() with the new fallback tier
# ============================================================================

def _snap(terms, **kw):
    kw.setdefault("turns_since_active", 0)
    return ActiveTopicSnapshot(terms=frozenset(terms), **kw)


def test_21_raw_match_behavior_completely_unchanged():
    history = [_snap({"aku", "pakai", "gpu", "rtx", "3060"})]
    result = memory_context.select_topic_candidates(history, "Kalau GPU nya gimana?", False)
    assert len(result) == 1


def test_22_morphology_fallback_recovers_topic():
    history = [_snap({"aku", "mau", "beli", "mic", "inmp441"})]
    result = memory_context.select_topic_candidates(history, "Kalau pembeliannya jadi kapan?", False)
    assert len(result) == 1


def test_23_colloquial_fallback_recovers_topic():
    history = [_snap({"mic", "esp32", "inmp441"})]
    result = memory_context.select_topic_candidates(history, "Kalau mikrofon itu kualitasnya gimana?", False)
    assert len(result) == 1


def test_24_ambiguous_normalized_tie_returns_empty():
    history = [
        _snap({"aku", "mau", "ganti", "gpu", "rtx"}, turns_since_active=1),
        _snap({"aku", "mau", "ganti", "mic", "max9814"}, turns_since_active=0),
    ]
    result = memory_context.select_topic_candidates(history, "Kalau upgrade itu gimana?", False)
    assert result == []


def test_25_no_evidence_anywhere_returns_empty():
    history = [_snap({"aquascape", "pompa", "watt"})]
    result = memory_context.select_topic_candidates(history, "Kalau upgrade PC-ku gimana?", False)
    assert result == []


def test_26_raw_match_preferred_over_normalized_when_both_exist():
    # Entry A shares the literal word "upgrade"; Entry B only shares
    # normalized "ganti"~"upgrade" - raw must win outright, not tie.
    history = [
        _snap({"aku", "mau", "upgrade", "pc"}, turns_since_active=1),
        _snap({"aku", "mau", "ganti", "gpu"}, turns_since_active=0),
    ]
    result = memory_context.select_topic_candidates(history, "Kalau upgrade itu gimana?", False)
    assert len(result) == 1
    assert "pc" in result[0].terms


def test_27_empty_query_returns_empty():
    history = [_snap({"aku", "pakai", "gpu"})]
    assert memory_context.select_topic_candidates(history, "", False) == []


def test_28_empty_history_returns_empty():
    assert memory_context.select_topic_candidates([], "Kalau upgrade itu gimana?", False) == []


def test_29_stopword_only_query_returns_empty():
    history = [_snap({"aku", "pakai", "gpu"})]
    assert memory_context.select_topic_candidates(history, "kalau itu gimana ya", False) == []


def test_30_unrelated_entity_never_normalized_into_match():
    """A specific product identifier must never fuzzy-match another
    unrelated one via this mechanism (anti-hardcoding invariant)."""
    history = [_snap({"aku", "pakai", "esp8266"})]
    result = memory_context.select_topic_candidates(history, "Kalau ESP32 gimana?", False)
    assert result == []


# ============================================================================
# Section 4 - select_temporal_fallback_candidate() with the new tier
# ============================================================================

def test_31_temporal_fallback_normalized_bridge_planned():
    history = [_snap({"aku", "mau", "ganti", "gpu", "rtx"}, status="planned")]
    result = memory_context.select_temporal_fallback_candidate(history, "Yang mau aku upgrade itu apa?")
    assert result is not None
    assert "ganti" in result.terms


def test_32_temporal_fallback_ambiguous_tie_returns_none():
    history = [
        _snap({"aku", "mau", "ganti", "gpu"}, status="planned", turns_since_active=1),
        _snap({"aku", "mau", "ganti", "mic"}, status="planned", turns_since_active=0),
    ]
    result = memory_context.select_temporal_fallback_candidate(history, "Yang mau aku upgrade itu apa?")
    assert result is None


def test_33_temporal_fallback_no_eligible_status_returns_none():
    history = [_snap({"aku", "mau", "ganti", "gpu"}, status="active")]
    result = memory_context.select_temporal_fallback_candidate(history, "Yang mau aku upgrade itu apa?")
    assert result is None


def test_34_temporal_fallback_raw_match_still_wins_over_normalized():
    history = [
        _snap({"aku", "mau", "upgrade", "ram"}, status="planned", turns_since_active=1),
        _snap({"aku", "mau", "ganti", "gpu"}, status="planned", turns_since_active=0),
    ]
    result = memory_context.select_temporal_fallback_candidate(history, "Yang mau aku upgrade itu apa?")
    assert result is not None
    assert "ram" in result.terms


# ============================================================================
# Section 5 - is_active_topic_relevant_to_query() unit tests
# ============================================================================

def test_35_empty_query_is_always_relevant():
    snap = _snap({"aku", "pakai", "gpu"})
    assert memory_context.is_active_topic_relevant_to_query(snap, "kalau itu gimana") is True


def test_36_raw_overlap_is_relevant():
    snap = _snap({"aku", "pakai", "gpu", "rtx"})
    assert memory_context.is_active_topic_relevant_to_query(snap, "Kalau GPU-nya gimana?") is True


def test_37_zero_overlap_even_normalized_is_not_relevant():
    snap = _snap({"aku", "beli", "headset", "gaming"})
    assert memory_context.is_active_topic_relevant_to_query(snap, "Kalau upgrade PC-ku gimana?") is False


def test_38_unique_normalized_overlap_is_relevant():
    snap = _snap({"aku", "mau", "ganti", "gpu"})
    assert memory_context.is_active_topic_relevant_to_query(snap, "Kalau upgrade itu gimana?", topic_history=[snap]) is True


def test_39_tied_normalized_overlap_across_history_is_not_relevant():
    snap_a = _snap({"aku", "mau", "ganti", "gpu"})
    snap_b = _snap({"aku", "mau", "ganti", "mic"})
    result = memory_context.is_active_topic_relevant_to_query(
        snap_b, "Kalau upgrade itu gimana?", topic_history=[snap_a, snap_b],
    )
    assert result is False


def test_40_none_snapshot_is_always_relevant():
    assert memory_context.is_active_topic_relevant_to_query(None, "anything at all") is True


def test_41_no_topic_history_arg_still_works_conservatively():
    """Backward-compatible default: a caller that doesn't pass
    topic_history (the parameter defaults to None) still gets a correct,
    non-crashing answer based on the single snapshot alone."""
    snap = _snap({"aku", "mau", "ganti", "gpu"})
    assert memory_context.is_active_topic_relevant_to_query(snap, "Kalau upgrade itu gimana?") is True


# ============================================================================
# Section 6 - E2E Scenarios A-H via real RuntimeDemoConsole
# ============================================================================

def test_50_scenario_A_exact_lexical_match():
    demo = _load_demo("A")
    replies = {
        "Aku mau ganti GPU ke RTX 3060.": "Oke, dicatat rencananya.",
        "Kalau GPU nya gimana?": "GPU-nya RTX 3060.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU ke RTX 3060.", "sA-1", conversation_id="convA")
        sp = _run_turn_capture_prompt(console, demo, "Kalau GPU nya gimana?", "sA-2", conversation_id="convA")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "3060" in candidates[0]
    finally:
        console.stop()


def test_51_scenario_B_morphology_bridges_beli_pembelian():
    demo = _load_demo("B")
    replies = {
        "Aku mau beli mic INMP441.": "Oke, dicatat rencananya.",
        "Sementara itu aquascape-ku baru diservis.": "Oke, dicatat juga.",
        "Kalau pembeliannya jadi kapan?": "Belum tahu, masih dipikirkan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau beli mic INMP441.", "sB-1", conversation_id="convB")
        _run_turn(console, demo, "Sementara itu aquascape-ku baru diservis.", "sB-2", conversation_id="convB")
        sp = _run_turn_capture_prompt(console, demo, "Kalau pembeliannya jadi kapan?", "sB-3", conversation_id="convB")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "mic" in candidates[0].lower()
        assert "aquascape" not in candidates[0].lower()
    finally:
        console.stop()


def test_52_scenario_C_colloquial_bridges_mic_mikrofon():
    demo = _load_demo("C")
    replies = {
        "Mic ESP32-ku pakai INMP441.": "Oke, dicatat.",
        "Sementara itu PC-ku baru diupgrade.": "Oke, dicatat juga.",
        "Kalau mikrofon itu kualitasnya gimana?": "Kualitasnya bagus untuk voice.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Mic ESP32-ku pakai INMP441.", "sC-1", conversation_id="convC")
        _run_turn(console, demo, "Sementara itu PC-ku baru diupgrade.", "sC-2", conversation_id="convC")
        sp = _run_turn_capture_prompt(console, demo, "Kalau mikrofon itu kualitasnya gimana?", "sC-3", conversation_id="convC")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "mic" in candidates[0].lower()
        assert "diupgrade" not in candidates[0].lower()
    finally:
        console.stop()


def test_53_scenario_D_existing_topic_action_primary_example():
    """The brief's own PRIMARY worked example."""
    demo = _load_demo("D")
    replies = {
        "Aku mau ganti GPU ke RTX 3060.": "Oke, dicatat rencananya.",
        "Sementara itu aquascape-ku baru diservis.": "Oke, dicatat juga.",
        "Kalau upgrade itu jadi gimana?": "Masih proses, tunggu part datang.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU ke RTX 3060.", "sD-1", conversation_id="convD")
        _run_turn(console, demo, "Sementara itu aquascape-ku baru diservis.", "sD-2", conversation_id="convD")
        sp = _run_turn_capture_prompt(console, demo, "Kalau upgrade itu jadi gimana?", "sD-3", conversation_id="convD")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates, f"expected the GPU-upgrade topic to resolve, prompt had: {sp}"
        assert "gpu" in candidates[0].lower() or "rtx" in candidates[0].lower()
        assert "aquascape" not in candidates[0].lower()
    finally:
        console.stop()


def test_54_scenario_E_multi_topic_ambiguity_must_not_guess():
    demo = _load_demo("E")
    replies = {
        "Aku mau ganti GPU ke RTX 3060.": "Oke, dicatat rencananya.",
        "Aku juga mau ganti mic ke MAX9814.": "Oke, dicatat juga.",
        "Kalau upgrade itu gimana?": "Upgrade yang mana ya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU ke RTX 3060.", "sE-1", conversation_id="convE")
        _run_turn(console, demo, "Aku juga mau ganti mic ke MAX9814.", "sE-2", conversation_id="convE")
        sp = _run_turn_capture_prompt(console, demo, "Kalau upgrade itu gimana?", "sE-3", conversation_id="convE")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, (
            f"genuine ambiguity between two equally-plausible topics must retrieve nothing, prompt had: {sp}"
        )
    finally:
        console.stop()


def test_55_scenario_F_semantic_recovery_across_unrelated_topic():
    demo = _load_demo("F")
    replies = {
        "Aku rencana beli mic INMP441.": "Oke, dicatat rencananya.",
        "Btw cuaca hari ini panas banget.": "Iya lumayan panas.",
        "Kalau jadi beli yang itu?": "Belum jadi beli.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku rencana beli mic INMP441.", "sF-1", conversation_id="convF")
        _run_turn(console, demo, "Btw cuaca hari ini panas banget.", "sF-2", conversation_id="convF")
        sp = _run_turn_capture_prompt(console, demo, "Kalau jadi beli yang itu?", "sF-3", conversation_id="convF")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "mic" in candidates[0].lower()
    finally:
        console.stop()


def test_56_scenario_G_false_positive_protection():
    demo = _load_demo("G")
    replies = {
        "Aku baru beli headset baru buat gaming.": "Oke, dicatat.",
        "Kalau upgrade PC-ku gimana ya, mumpung ada budget?": "Bisa mulai dari GPU dulu.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku baru beli headset baru buat gaming.", "sG-1", conversation_id="convG")
        sp = _run_turn_capture_prompt(
            console, demo, "Kalau upgrade PC-ku gimana ya, mumpung ada budget?", "sG-2", conversation_id="convG",
        )
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"unrelated headset topic must not be injected, prompt had: {sp}"
    finally:
        console.stop()


def test_57_scenario_H_temporal_plus_semantic_combination():
    demo = _load_demo("H")
    replies = {
        "Minggu depan aku mau ganti GPU ke RTX 3060.": "Oke, dicatat rencananya.",
        "Sementara itu aquascape-ku baru diservis.": "Oke, dicatat juga.",
        "Yang mau aku upgrade itu apa?": "GPU ke RTX 3060.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Minggu depan aku mau ganti GPU ke RTX 3060.", "sH-1", conversation_id="convH")
        _run_turn(console, demo, "Sementara itu aquascape-ku baru diservis.", "sH-2", conversation_id="convH")
        sp = _run_turn_capture_prompt(console, demo, "Yang mau aku upgrade itu apa?", "sH-3", conversation_id="convH")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates, f"expected the planned GPU topic to resolve, prompt had: {sp}"
        assert "gpu" in candidates[0].lower() or "rtx" in candidates[0].lower()
    finally:
        console.stop()


# ============================================================================
# Section 7 - attribute / ordinal references combined with bridging
# ============================================================================

def test_58_attribute_reference_merge_unaffected_by_normalization():
    demo = _load_demo("attr")
    replies = {
        "Sekarang aku pakai INMP441.": "Oke, INMP441 dicatat sebagai mic sekarang.",
        "Kalau yang wireless?": "Bisa pakai modul I2S over WiFi custom.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Sekarang aku pakai INMP441.", "sAttr-1", conversation_id="convAttr")
        sp = _run_turn_capture_prompt(console, demo, "Kalau yang wireless?", "sAttr-2", conversation_id="convAttr")
        candidates = _lines_starting(sp, "- Active conversation topic")
        assert candidates and "inmp441" in candidates[0].lower()
    finally:
        console.stop()


def test_59_ordinal_reference_unaffected_by_normalization():
    demo = _load_demo("ord")
    list_reply = "1. INMP441 - baseline stabil.\n2. MAX9814 - sensitif tapi lebih noisy."
    replies = {
        "Ada 2 pilihan mic, jelasin.": list_reply,
        "Yang kedua gimana?": "MAX9814 lebih sensitif.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Ada 2 pilihan mic, jelasin.", "sOrd-1", conversation_id="convOrd")
        sp = _run_turn_capture_prompt(console, demo, "Yang kedua gimana?", "sOrd-2", conversation_id="convOrd")
        ref = _lines_starting(sp, "- Referenced item")
        assert ref and "MAX9814" in ref[0]
    finally:
        console.stop()


# ============================================================================
# Section 8 - cross-conversation isolation
# ============================================================================

def test_60_normalized_bridging_does_not_leak_across_conversations():
    demo = _load_demo("iso")
    replies = {
        "Aku mau ganti GPU ke RTX 3060.": "Oke, dicatat rencananya.",
        "Aku mau beli pompa aquascape.": "Oke, dicatat rencananya juga.",
        "Kalau upgrade itu gimana?": "(reply A)",
        "Kalau pembeliannya gimana?": "(reply B)",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU ke RTX 3060.", "sIso-A1", conversation_id="convIsoA")
        _run_turn(console, demo, "Aku mau beli pompa aquascape.", "sIso-B1", conversation_id="convIsoB")
        spA = _run_turn_capture_prompt(console, demo, "Kalau upgrade itu gimana?", "sIso-A2", conversation_id="convIsoA")
        spB = _run_turn_capture_prompt(console, demo, "Kalau pembeliannya gimana?", "sIso-B2", conversation_id="convIsoB")
        candA = _lines_starting(spA, *_ANY_CANDIDATE_PREFIXES)
        candB = _lines_starting(spB, *_ANY_CANDIDATE_PREFIXES)
        assert candA and "gpu" in candA[0].lower()
        assert "pompa" not in candA[0].lower()
        assert candB and "pompa" in candB[0].lower()
        assert "gpu" not in candB[0].lower()
    finally:
        console.stop()


# ============================================================================
# Section 9 - topic-history eviction behavior
# ============================================================================

def test_61_normalized_bridging_respects_bounded_history_eviction():
    """A morphological/colloquial match must only ever be found within
    the ALREADY-bounded `_TOPIC_HISTORY_MAX_ENTRIES` window - this
    sprint adds no new storage, no new eviction rule."""
    demo = _load_demo("evict")
    replies = {}
    fillers = []
    for i in range(memory_context._TOPIC_HISTORY_MAX_ENTRIES + 2):
        stmt = f"Topik pengisi nomor {i} soal cuaca hari ini."
        replies[stmt] = "Oke."
        fillers.append(stmt)
    replies["Aku mau beli mic INMP441."] = "Oke, dicatat rencananya."
    replies["Kalau pembeliannya gimana?"] = "(reply)"

    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau beli mic INMP441.", "sEvict-0", conversation_id="convEvict")
        for i, stmt in enumerate(fillers):
            _run_turn(console, demo, stmt, f"sEvict-{i+1}", conversation_id="convEvict")
        sp = _run_turn_capture_prompt(
            console, demo, "Kalau pembeliannya gimana?", "sEvict-last", conversation_id="convEvict",
        )
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, (
            f"the mic-purchase entry should have aged out of the bounded history by now, prompt had: {sp}"
        )
    finally:
        console.stop()


# ============================================================================
# Section 10 - empty / unknown query behavior
# ============================================================================

def test_62_empty_text_select_topic_candidates_returns_empty():
    history = [_snap({"aku", "pakai", "gpu"})]
    assert memory_context.select_topic_candidates(history, "", False) == []


def test_63_none_text_select_topic_candidates_does_not_raise():
    history = [_snap({"aku", "pakai", "gpu"})]
    assert memory_context.select_topic_candidates(history, None, False) == []


def test_64_gibberish_query_returns_empty_not_a_crash():
    history = [_snap({"aku", "pakai", "gpu"})]
    result = memory_context.select_topic_candidates(history, "asdkjaslkdj qwoeiqwoe", False)
    assert result == []


def test_65_unknown_query_temporal_fallback_returns_none():
    history = [_snap({"aku", "pakai", "gpu"}, status="active")]
    result = memory_context.select_temporal_fallback_candidate(history, "asdkjaslkdj qwoeiqwoe")
    assert result is None


# ============================================================================
# Section 11 - architectural / structural invariants
# ============================================================================

def test_66_invariant_no_embedding_or_llm_judge_in_bridging_code():
    src = inspect.getsource(memory_context)
    forbidden_imports = [
        "import openai", "from openai", "import sentence_transformers",
        "from sentence_transformers", "import faiss", "from faiss",
        "import tensorflow", "import torch",
    ]
    forbidden_calls = [".embed(", "get_embedding(", "cosine_similarity(", ".encode("]
    for term in forbidden_imports + forbidden_calls:
        assert term not in src, f"forbidden mechanism {term!r} found in luno/memory_context.py"


def test_67_invariant_normalization_functions_are_pure_no_io():
    """Structural check: the bridging layer must be pure/deterministic -
    no file, network, or persistence calls anywhere in its own source."""
    src_strip = inspect.getsource(memory_context._strip_bounded_affixes)
    src_norm = inspect.getsource(memory_context._normalize_terms_for_bridging)
    for src in (src_strip, src_norm):
        for forbidden in ("open(", "requests.", "socket.", "urllib", "subprocess"):
            assert forbidden not in src


def test_68_invariant_rank_key_unchanged_signature():
    """`_rank_key()` must remain untouched by this sprint - the fix lives
    entirely at candidate GENERATION, before ranking is ever reached."""
    from luno.memory_context import ContextItem
    sig = inspect.signature(ContextItem._rank_key)
    assert list(sig.parameters.keys()) == ["self"]


def test_69_invariant_assemble_context_signature_unchanged():
    """Structural smoke check - Sprint 43 must not have added ANY new
    parameter (required or optional) to `assemble_context()`'s public
    contract at all - the fix lives entirely inside `memory_context`'s
    topic-candidate helper functions, never touching this call site's
    own signature. Baseline captured directly from this sprint's Phase 0
    reconnaissance read of the pre-existing function."""
    sig = inspect.signature(memory_context.assemble_context)
    baseline_params = [
        "text", "memory_retriever", "get_manual_memories", "verified_fact_store",
        "relationship_state", "config", "precomputed_relevant_memories", "intent",
        "previous_topic_terms", "retrieval_query_override", "funnel",
    ]
    assert list(sig.parameters.keys()) == baseline_params, (
        f"assemble_context()'s signature changed - expected {baseline_params}, got {list(sig.parameters.keys())}"
    )


def test_70_invariant_topic_history_max_entries_unchanged():
    assert memory_context._TOPIC_HISTORY_MAX_ENTRIES == 8


def test_71_min_affix_root_len_is_conservative():
    """A structural safety-margin check - too small a minimum would make
    the affix stripper prone to false positives on short real words."""
    assert memory_context._MIN_AFFIX_ROOT_LEN >= 3


def test_72_normalize_query_tokens_handles_none_text():
    result = memory_context._normalize_query_tokens_for_bridging(None, {"gpu"})
    assert "gpu" in result
