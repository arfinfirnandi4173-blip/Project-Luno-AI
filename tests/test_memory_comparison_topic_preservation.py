"""
test_memory_comparison_topic_preservation.py
================================================

CONTEXT-AWARE COMPARISON TOPIC PRESERVATION sprint.

Phase 0's own live reproduction through the real `RuntimeDemoConsole`
(see `docs/change_impact/memory_comparison_topic_preservation.md`) found
that a grammatically-complete comparison/sub-question incorrectly REPLACED
the active topic even when its own residual term was already part of the
active topic's own vocabulary: turn 2 ("Kalau mikrofonnya gimana?",
`classify_reference_type()` -> `"comparison"`) always REPLACED the
ESP32/INMP441 active topic with its own thin snapshot
(`{gimana, kalau, mikrofonnya, mock, reply}`), and turn 3 ("Yang tadi soal
mic gimana?") then retrieved that thin, wrong snapshot instead of the
original, richer one.

This is a targeted STATE-UPDATE fix, nothing else:

  - `luno.memory.is_pure_reference_followup(text, active_topic_terms=None)`
    (extended, additive, optional parameter - every existing caller/test
    that omits it is byte-for-byte unaffected) now ALSO returns `True`
    for a `"comparison"`-classified turn whose own meaningful residual
    term(s) (`_comparison_residual_terms()`, reusing
    `classify_reference_type()`'s own comparison-branch regex/filter
    verbatim, extended only with the brief's own named generic markers
    `"bagaimana"`/`"tadi"`/`"soal"`) overlap `active_topic_terms`
    (substring-based, `_residual_overlaps_active_topic()` - deterministic
    string containment, NOT embeddings, NOT a second classifier - the
    same class of primitive `luno.memory_context.
    _matches_keyword_category()` already uses elsewhere).
  - `main_runtime_demo.py::PlannerBridgeModule._on_assistant_response()`
    now fetches `existing_snapshot` BEFORE classifying (order swapped)
    so its own `.terms` can be threaded through.

Every other reference type (negation/cost_comparison/alternative_request/
continuation/direct_reference/unknown), `classify_query_intent()`,
`needs_topic_context()`, `select_topic_candidates()`,
`topic_history_to_relevant_memories()`,
`build_expanded_retrieval_text_from_history()`, `assemble_context()`,
`_rank_key()`, `_apply_budget()`, `render_context_block()`, the prompt-
injection trust boundary, TTS, streaming, and the persistence format are
ALL unchanged by this sprint - not re-tested here (already covered by
`tests/test_memory_continuity.py`, `tests/test_memory_topic_retention.py`,
`tests/test_memory_context.py`, `tests/
test_memory_retrieval_decision_quality_reaudit.py`).

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture protects
every test in this file - no test here can ever touch Vinn's real
production data.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
from typing import Callable

import luno.memory as memory
import luno.memory_context as memory_context

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================================
# SECTION 1 - unit tests: is_pure_reference_followup(active_topic_terms=...)
# ============================================================================

_ESP32_TOPIC = frozenset({"esp32", "inmp441", "mikrofon", "voice", "assistant"})
_ESP32_TOPIC_NO_MIC = frozenset({"esp32", "inmp441", "voice", "assistant"})


def test_01_omitting_active_topic_terms_is_byte_for_byte_backward_compatible():
    # Every existing caller/test before this sprint never passes
    # `active_topic_terms` - must behave EXACTLY as before this sprint.
    assert memory.is_pure_reference_followup("Kalau mikrofonnya gimana?") is False
    assert memory.is_pure_reference_followup("yang lain?") is True
    assert memory.is_pure_reference_followup("terus?") is True
    assert memory.is_pure_reference_followup("ESP32 gimana?") is False


def test_02_none_active_topic_terms_is_also_unaffected():
    assert memory.is_pure_reference_followup("Kalau mikrofonnya gimana?", active_topic_terms=None) is False
    assert memory.is_pure_reference_followup("Kalau mikrofonnya gimana?", active_topic_terms=frozenset()) is False


def test_03_same_term_comparison_preserves_scenario_1():
    # Phase 4 #1 - existing topic + same-term comparison -> preserve.
    assert memory.is_pure_reference_followup("Kalau mikrofonnya gimana?", active_topic_terms=_ESP32_TOPIC) is True


def test_04_suffix_normalized_same_term_preserves_scenario_2():
    # Phase 4 #2 - "INMP441-nya" (Indonesian possessive suffix) matches
    # the plain "inmp441" active-topic term via the existing substring
    # overlap primitive - no dedicated suffix-stripping table needed.
    assert memory.is_pure_reference_followup(
        "Kalau INMP441-nya gimana?", active_topic_terms=_ESP32_TOPIC_NO_MIC,
    ) is True


def test_05_genuinely_new_entity_still_replaces_scenario_3():
    # Phase 4 #3 - Example C from the brief: Bluetooth is genuinely absent
    # from the active topic -> existing replace behavior, unchanged.
    assert memory.is_pure_reference_followup(
        "Kalau Bluetooth-nya gimana?", active_topic_terms=_ESP32_TOPIC,
    ) is False


def test_06_generic_gimana_alone_does_not_false_preserve_scenario_4():
    # Phase 4 #4 - Example D from the brief: "caranya" is not part of the
    # active topic and is not one of the generic markers either, but it
    # simply doesn't overlap - correctly falls through to replace.
    assert memory.is_pure_reference_followup(
        "Kalau caranya gimana?", active_topic_terms=frozenset({"esp32", "inmp441"}),
    ) is False


def test_07_unrelated_word_replaces_scenario_5():
    # Phase 4 #5 - existing topic + unrelated word -> replace. Deliberately
    # NOT "harganya" ("the price") - `classify_reference_type()` already
    # classifies "harganya gimana?" as the pre-existing, unrelated
    # `"cost_comparison"` type (`_COST_COMPARISON_RE`'s own
    # `harganya\s+gimana` pattern, Sprint 4, untouched by this sprint),
    # which is ALREADY in `_PURE_REFERENCE_TYPES` regardless of any topic
    # overlap - a true "genuinely unrelated COMPARISON word" needs a word
    # that doesn't also happen to trip a different, higher-precedence,
    # pre-existing pure-reference pattern.
    assert memory.classify_reference_type("Kalau warnanya gimana?") == "comparison"
    assert memory.is_pure_reference_followup(
        "Kalau warnanya gimana?", active_topic_terms=frozenset({"esp32", "inmp441", "mikrofon"}),
    ) is False


def test_08_brief_named_generic_markers_never_count_as_residual_alone():
    # "gimana"/"bagaimana"/"kalau"/"yang"/"itu"/"tadi"/"soal" must never,
    # by themselves, register as a "meaningful residual" - even if one of
    # them happens to literally also be an active-topic term (a
    # pathological, defensive case: the active topic itself would have to
    # contain one of these function words, which normal topic extraction
    # would never produce, but the exclusion must hold regardless).
    for marker in ("gimana", "bagaimana", "kalau", "yang", "itu", "tadi", "soal"):
        assert marker not in memory._comparison_residual_terms(f"{marker}?")


def test_09_comparison_residual_terms_extracts_mic_correctly():
    assert memory._comparison_residual_terms("Yang tadi soal mic gimana?") == frozenset({"mic"})


def test_10_non_comparison_types_never_gain_the_new_behavior():
    # `active_topic_terms` must ONLY ever influence a "comparison"-typed
    # turn - every other type's is_pure_reference_followup() result is
    # completely independent of it.
    topic = frozenset({"esp32", "inmp441"})
    assert memory.classify_reference_type("yang lain?") == "alternative_request"
    assert memory.is_pure_reference_followup("yang lain?", active_topic_terms=topic) is True
    assert memory.is_pure_reference_followup("yang lain?", active_topic_terms=frozenset()) is True
    assert memory.classify_reference_type("ESP32 pakai apa lagi selain INMP441?") in ("unknown", "comparison")
    # A negation turn ("tanpa X") must still always replace, regardless
    # of any topic overlap - this sprint does not touch negation.
    assert memory.classify_reference_type("kalau tanpa MQTT?") == "negation_of_current_option"
    assert memory.is_pure_reference_followup("kalau tanpa MQTT?", active_topic_terms=topic) is False


# ============================================================================
# SECTION 2 - repeated-comparison robustness (Phase 4 #9)
# ============================================================================

def test_11_repeated_same_topic_comparisons_do_not_degrade_the_snapshot():
    """Simulates several consecutive comparison turns about the SAME
    already-active topic, each one calling `update_active_topic()` with
    the `is_followup` value `is_pure_reference_followup()` now correctly
    computes - the active topic must stay anchored to the ORIGINAL rich
    terms throughout, never degrading into a chain of thinner and
    thinner snapshots the way the pre-fix REPLACE-always behavior did."""
    snapshot = memory_context.update_active_topic(
        None, "ESP32 pakai INMP441 buat voice assistant.", "Oke, INMP441 itu mikrofon buat ESP32.", is_followup=False,
    )
    assert "esp32" in snapshot.terms and "inmp441" in snapshot.terms
    original_terms = snapshot.terms

    for user_text in (
        "Kalau mikrofonnya gimana?",
        "Kalau INMP441-nya gimana?",
        "Kalau esp32-nya gimana?",
    ):
        is_followup = memory.is_pure_reference_followup(user_text, active_topic_terms=snapshot.terms)
        assert is_followup is True, f"{user_text!r} should have been recognized as referring to the active topic"
        snapshot = memory_context.update_active_topic(snapshot, user_text, "Oke.", is_followup=is_followup)
        # PRESERVE means the snapshot's own terms are untouched (only its
        # age is reset to 0) - never replaced with the new turn's own
        # (thinner) vocabulary.
        assert snapshot.terms == original_terms, (
            f"active topic degraded after {user_text!r}: {sorted(snapshot.terms)}"
        )


# ============================================================================
# SECTION 3 - unchanged-behavior regression guards (Phase 4 #10, #11)
# ============================================================================

def test_12_sprint4_pure_reference_behavior_unchanged():
    # A representative sample of Sprint 4's own worked examples,
    # reconfirmed byte-for-byte unaffected by this sprint.
    assert memory.classify_reference_type("yang lain?") == "alternative_request"
    assert memory.classify_reference_type("terus?") == "continuation"
    assert memory.classify_reference_type("kalau itu gimana?") == "direct_reference"
    assert memory.classify_reference_type("tanpa MQTT?") == "negation_of_current_option"
    assert memory.classify_reference_type("yang lebih murah?") == "cost_comparison"
    assert memory.is_pure_reference_followup("yang lain?") is True
    assert memory.is_pure_reference_followup("kalau itu gimana?") is True


def test_13_sprint5_topic_history_selection_unchanged():
    # `select_topic_candidates()` itself (Sprint 5 / Memory Topic
    # Retention) is completely untouched by this sprint - reconfirmed
    # with a direct call, independent of `is_pure_reference_followup()`.
    history = [
        memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "inmp441", "mic"}), turns_since_active=0),
        memory_context.ActiveTopicSnapshot(terms=frozenset({"aquascape", "pompa"}), turns_since_active=1),
    ]
    candidates = memory_context.select_topic_candidates(history, "Yang soal mic gimana?", is_short_followup=False)
    assert len(candidates) == 1
    assert "mic" in candidates[0].terms


def test_14_concurrent_conversations_remain_isolated():
    # Two independent `ActiveTopicSnapshot` chains, keyed by conversation
    # (mirrors `PlannerBridgeModule._active_topic`'s own per-conversation
    # dict) - updating one must never affect the other.
    conv_a = memory_context.update_active_topic(
        None, "ESP32 pakai INMP441.", "Oke.", is_followup=False,
    )
    conv_b = memory_context.update_active_topic(
        None, "Aquascape pakai pompa CO2.", "Oke.", is_followup=False,
    )
    is_followup_a = memory.is_pure_reference_followup("Kalau inmp441-nya gimana?", active_topic_terms=conv_a.terms)
    conv_a = memory_context.update_active_topic(conv_a, "Kalau inmp441-nya gimana?", "Oke.", is_followup=is_followup_a)
    assert "esp32" in conv_a.terms
    assert "pompa" not in conv_a.terms
    assert "aquascape" not in conv_a.terms
    assert "aquascape" in conv_b.terms
    assert "esp32" not in conv_b.terms


# ============================================================================
# SECTION 4 - production-path E2E (real RuntimeDemoConsole, isolated state)
# ============================================================================

def _load_demo(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules[name] = demo
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


def _run_turn(console, demo, text, request_id, conversation_id=None, canned_reply=None):
    if canned_reply is not None:
        console.openrouter_adapter.client.canned_text = canned_reply
    captured = {}
    need_llm = threading.Event()

    def _capture(e):
        if e.get("request_id") != request_id:
            return
        captured["system_prompt"] = e.get("system_prompt")
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(need_llm.is_set, 5.0), "no need_llm_response within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "assistant_response / active-topic update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


def _memory_block(prompt):
    """Scopes an assertion to ONLY the rendered `[BEGIN STORED MEMORY
    CONTEXT]...[END STORED MEMORY CONTEXT]` block - Luno's own static
    persona text separately lists "ESP32/Arduino" under its own always-
    present "Knowledgeable about:" line, which would otherwise false-
    positive an unscoped substring check (same pitfall documented in
    `tests/test_memory_continuity.py`'s own `_word_in` helper)."""
    i = prompt.find("BEGIN STORED MEMORY CONTEXT")
    j = prompt.find("END STORED MEMORY CONTEXT")
    if i == -1 or j == -1:
        return ""
    return prompt[i:j]


def test_E2E_A_turn3_recovers_original_esp32_inmp441_topic_not_thin_snapshot():
    """Phase 5's own critical E2E test - the exact 3-turn scenario. A
    `canned_reply` for turn 1 mentioning "mikrofon" is used for turn 1 -
    this mirrors real production (a real LLM reply to "ESP32 pakai
    INMP441 buat voice assistant" would very plausibly describe the
    INMP441 as a microphone, and `extract_topic_terms_from_turn()`
    already documents merging BOTH user and reply text into the topic
    snapshot specifically so "entities the assistant introduced but the
    user never typed verbatim" are captured) - NOT a rigged test; the
    default echo-mock reply (used throughout `tests/
    test_memory_retrieval_decision_quality_reaudit.py`) never mentions
    "mic"/"mikrofon" at all since it only echoes the user's own ESP32/
    INMP441 text, so this exact literal word-pairing genuinely cannot be
    bridged without either informative reply content or embeddings (see
    this sprint's own Known Limitations for the transparent, un-rigged
    version of this same scenario)."""
    demo = _load_demo("main_runtime_demo_comp_preserve_e2e_a")
    console = _new_console(demo)
    console.start()
    try:
        p1 = _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-a-1", "conv-cp-a",
            canned_reply="Oke, INMP441 itu mikrofon I2S yang bagus buat ESP32 voice assistant kamu.",
        )
        p2 = _run_turn(
            console, demo, "Kalau mikrofonnya gimana?", "cp-a-2", "conv-cp-a",
            canned_reply="Mikrofonnya bagus, kualitas audionya jernih.",
        )
        p3 = _run_turn(console, demo, "Yang tadi soal mic gimana?", "cp-a-3", "conv-cp-a")

        block2 = _memory_block(p2)
        assert re.search(r'\besp32\b', block2, re.IGNORECASE), "turn 2 must still surface the ESP32 topic"

        block3 = _memory_block(p3)
        assert block3, "turn 3 must carry a memory block"
        assert re.search(r'\b(esp32|inmp441)\b', block3, re.IGNORECASE), (
            f"turn 3 must recover the ORIGINAL ESP32/INMP441 topic, not merely turn 2's thin "
            f"snapshot - got: {block3!r}"
        )
    finally:
        console.stop()


def test_E2E_B_genuinely_new_branch_bluetooth_still_replaces():
    """Phase 5's second required check: a comparison naming something
    genuinely absent from the active topic must still create a real new
    branch, not incorrectly preserve the old one as the only active
    topic."""
    demo = _load_demo("main_runtime_demo_comp_preserve_e2e_b")
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-b-1", "conv-cp-b",
            canned_reply="Oke, INMP441 itu mikrofon buat ESP32 kamu.",
        )
        p2 = _run_turn(console, demo, "Kalau Bluetooth-nya gimana?", "cp-b-2", "conv-cp-b")
        block2 = _memory_block(p2)
        # Turn 2 itself still legitimately surfaces the ESP32 topic via
        # the (unmodified) single-slot retrieval fallback - the question
        # here is what happens to the STATE afterward.
        p3 = _run_turn(console, demo, "Terus gimana caranya?", "cp-b-3", "conv-cp-b")
        block3 = _memory_block(p3)
        assert re.search(r'\bbluetooth\b', block3, re.IGNORECASE), (
            "the Bluetooth branch must have become the new active topic"
        )
    finally:
        console.stop()


def test_E2E_C_multi_topic_history_mic_recovers_esp32_not_aquascape():
    """Phase 4 #6 / Phase 6 - ESP32/INMP441 + aquascape topics both
    established; asking about "mic" must recover the ESP32 branch, never
    the aquascape one, regardless of which was discussed more recently."""
    demo = _load_demo("main_runtime_demo_comp_preserve_e2e_c")
    console = _new_console(demo)
    console.start()
    try:
        # "mikrofon" used EXACT (no "-nya" suffix) in both the topic-
        # establishing reply and the recovery query - recovering topic A
        # here (topic B, aquascape, is already the CURRENT single-slot
        # active topic at turn 3) can ONLY happen via `select_topic_
        # candidates()`'s own EXACT-token-overlap match against the
        # bounded `_topic_history` (Sprint 5, unmodified by this sprint -
        # it does `query_tokens & entry.terms`, not a substring check),
        # so the query word must exactly match a stored topic term - a
        # DIFFERENT mechanism than this sprint's own substring-based
        # comparison-residual overlap (which only ever affects what the
        # NEXT active topic becomes, not this turn's own retrieval).
        _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-c-1", "conv-cp-c",
            canned_reply="Oke, INMP441 itu mikrofon yang bagus buat ESP32 kamu.",
        )
        _run_turn(
            console, demo, "Aku juga punya aquascape dengan pompa CO2.", "cp-c-2", "conv-cp-c",
            canned_reply="Oke, aquascape kamu pakai pompa CO2 ya.",
        )
        p3 = _run_turn(console, demo, "Yang soal mikrofon gimana?", "cp-c-3", "conv-cp-c")
        block3 = _memory_block(p3)
        assert re.search(r'\b(esp32|inmp441|mikrofon)\b', block3, re.IGNORECASE)
        assert not re.search(r'\b(aquascape|pompa)\b', block3, re.IGNORECASE)
    finally:
        console.stop()


def test_E2E_D_multi_topic_history_pompa_recovers_aquascape_not_esp32():
    """Phase 4 #7 - the symmetric case: asking about "pompa" must select
    the aquascape branch."""
    demo = _load_demo("main_runtime_demo_comp_preserve_e2e_d")
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-d-1", "conv-cp-d",
            canned_reply="Oke, INMP441 mikrofonnya bagus buat ESP32.",
        )
        _run_turn(
            console, demo, "Aku juga punya aquascape dengan pompa CO2.", "cp-d-2", "conv-cp-d",
            canned_reply="Oke, aquascape kamu pakai pompa CO2 ya.",
        )
        p3 = _run_turn(console, demo, "Kalau pompanya gimana?", "cp-d-3", "conv-cp-d")
        block3 = _memory_block(p3)
        assert re.search(r'\b(aquascape|pompa)\b', block3, re.IGNORECASE)
        assert not re.search(r'\b(esp32|inmp441)\b', block3, re.IGNORECASE)
    finally:
        console.stop()


def test_E2E_E_new_subject_does_not_inherit_previous_topic():
    """Phase 4 #8 - a completely new, unrelated subject must not inherit
    any previous topic."""
    demo = _load_demo("main_runtime_demo_comp_preserve_e2e_e")
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-e-1", "conv-cp-e",
            canned_reply="Oke, INMP441 itu mikrofon buat ESP32 kamu.",
        )
        # Deliberately no shared vocabulary with turn 1's own text/reply
        # (no "bagus"/"buat"/"kamu"/etc.) - an accidental shared common
        # word would trigger `select_topic_candidates()`'s own, separate,
        # unmodified content-overlap mechanism and produce a false
        # positive unrelated to anything this sprint's fix touches.
        p2 = _run_turn(console, demo, "Berapa harga tiket bioskop weekend ini?", "cp-e-2", "conv-cp-e")
        block2 = _memory_block(p2)
        assert not re.search(r'\b(esp32|inmp441|mikrofon)\b', block2, re.IGNORECASE)
    finally:
        console.stop()


# ============================================================================
# SECTION 5 - Phase 6 safety / contamination
# ============================================================================

def test_15_phase6_topic_a_b_recovery_and_unrelated_query_safety():
    """Full Phase 6 scenario: Topic A (ESP32/INMP441), Topic B
    (aquascape/pompa), then a mic question (must recover A), a pompa
    question (must recover B), then a genuinely unrelated question (must
    inject NEITHER topic) - no recency-only fallback may override
    explicit token overlap."""
    demo = _load_demo("main_runtime_demo_comp_preserve_phase6")
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(
            console, demo, "ESP32 pakai INMP441 buat voice assistant.", "cp-p6-1", "conv-cp-p6",
            canned_reply="Oke, INMP441 itu mikrofon yang bagus buat ESP32 kamu.",
        )
        _run_turn(
            console, demo, "Aku juga punya aquascape dengan pompa CO2.", "cp-p6-2", "conv-cp-p6",
            canned_reply="Oke, aquascape kamu pakai pompa CO2 ya.",
        )
        # Topic B (aquascape) is the CURRENT single-slot active topic at
        # this point (turn 2 was a genuinely fresh/rich statement) -
        # recovering Topic A here exercises `select_topic_candidates()`'s
        # own EXACT-token-overlap match against `_topic_history` (Sprint
        # 5, unmodified), so "mikrofon" is used verbatim (no "-nya"
        # suffix) to guarantee an exact match against the stored topic
        # term - a different mechanism than this sprint's own substring-
        # based fix, which only affects the state-update decision.
        p3 = _run_turn(console, demo, "Yang soal mikrofon gimana?", "cp-p6-3", "conv-cp-p6")
        block3 = _memory_block(p3)
        assert re.search(r'\b(esp32|inmp441|mikrofon)\b', block3, re.IGNORECASE), "must recover Topic A"
        assert not re.search(r'\b(aquascape|pompa)\b', block3, re.IGNORECASE)

        p4 = _run_turn(console, demo, "Kalau pompanya gimana?", "cp-p6-4", "conv-cp-p6")
        block4 = _memory_block(p4)
        assert re.search(r'\b(aquascape|pompa)\b', block4, re.IGNORECASE), "must recover Topic B"
        assert not re.search(r'\b(esp32|inmp441)\b', block4, re.IGNORECASE)

        p5 = _run_turn(console, demo, "Berapa harga sepatu?", "cp-p6-5", "conv-cp-p6")
        block5 = _memory_block(p5)
        assert not re.search(r'\b(esp32|inmp441|mikrofon|aquascape|pompa)\b', block5, re.IGNORECASE), (
            "unrelated query must not inject either topic merely because one is recent"
        )
    finally:
        console.stop()
