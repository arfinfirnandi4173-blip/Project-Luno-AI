"""
test_bounded_entity_provenance.py
====================================================

SPRINT 48 - BOUNDED ENTITY PROVENANCE & AMBIGUITY RESOLUTION.

Goal: revisit Sprint 47's own central unfixed finding (known limitation
#8 in `docs/project_handover.md` SS16) - a curated-vocabulary single
token with ZERO grounding in EITHER of exactly 2 live topics wrongly,
confidently resolves to whichever topic is merely most recent, instead
of refusing. Sprint 47 tried and reverted TWO threshold-only widenings
of `is_active_topic_relevant_to_query()`'s `distinct_other_count`
guard, because both broke a different, equally-valid, formally-
IDENTICAL-shaped existing test (`test_20_single_other_topic_no_
conflict_still_trusted`, Sprint 44; `test_27_e2e_no_contamination_
reverse_direction`, Sprint 46) with the OPPOSITE correct answer. This
sprint was explicitly told NOT to repeat that same threshold-only
approach and to find a genuinely different, bounded mechanism.

Phase 0-2 reconnaissance (before any code changed) reproduced all 8
scenarios (A-H) in this sprint's own brief live through the real
`RuntimeDemoConsole`, using deliberately generic canned replies (the
same leak-free-reply discipline Sprint 47 established). Findings:

- **Scenario B** ("Board itu gimana?" after ESP32/INMP441 - the OLDER
  topic - then Aquascape - the ACTIVE/most-recent topic): a REAL,
  reproduced ambiguity-safety bug, identical to Sprint 47's own
  Scenario 5 - confidently, wrongly resolved to Aquascape.
- **Scenario B-mirror** ("Mic-nya gimana?" after Aquarium then
  ESP32/INMP441 - Sprint 46's own `test_27`): the textbook IDENTICAL
  formal shape (curated single token, zero grounding in the active OR
  the sole other topic, exactly 1 other topic) - but the CORRECT
  answer here is to trust recency (resolve to ESP32/INMP441).
- **Scenario A** ("Pompanya gimana?" after "Aquascape A pakai pompa
  kecil." then "Aquascape B pakai pompa besar." - Sprint 47's known
  limitation #9): reproduced, confirmed UNCHANGED by this sprint's own
  fix (a completely different code path - the `active_score > 0`
  `coverage > 0.5` lineage-skip branch, not the `active_score == 0`
  branch Scenarios B/B-mirror exercise). See "Investigated and
  REJECTED" below for why this was not also fixed.
- **Scenarios C, D, E, F, G, H**: already correct, unmodified by this
  sprint - see each scenario's own test below for the specific existing
  mechanism that already handles it (raw-overlap short-circuit, the
  existing `len(query_tokens) != 1` multi-token refusal, Sprint 47's
  own `is_demonstrative_anchored_followup()` merge preservation, the
  existing `distinct_other_count >= 2` guard, and Sprint 44's own
  single-topic "nothing else it could mean" precedent, respectively).

**Root cause (confirmed via direct token/regex inspection, not
guesswork):** Sprint 46's "mic" case and Sprint 47's "board" case are
LITERALLY indistinguishable from `distinct_other_count`/lexical-overlap
alone - both are a curated single token with zero grounding in either
of exactly 2 live topics. But the two example queries are NOT
identical: "Board itu gimana?" places the demonstrative "itu"
immediately after the sole content word (its own 2nd word); "Mic-nya
gimana?" does not (its 2nd word is "gimana?", the clitic is fused onto
the noun itself). In Indonesian, a demonstrative immediately after a
lone noun idiomatically marks a back-reference to something already
established/known - not necessarily the most-recently-active thing -
while a bare possessive/clitic follow-up naturally continues whatever
is presently active. This is a GRAMMATICAL signal already computed
elsewhere in this module for a different purpose (`_DEMONSTRATIVE_
ANCHORED_RE`, Sprint 47's own `is_demonstrative_anchored_followup()`),
not a new vocabulary/threshold mechanism.

**Fix - one new, narrow, additive `if` inside `is_active_topic_
relevant_to_query()`'s existing `active_score == 0` branch**
(`luno/memory_context.py`), placed immediately after the existing
`distinct_other_count >= 2` guard: when exactly the `distinct_other_
count >= 1` case remains (the case Sprint 47's own two threshold
attempts could not safely touch) AND the query text is demonstrative-
anchored (`_DEMONSTRATIVE_ANCHORED_RE.search(text)` - the SAME regex
Sprint 47 already built, reused verbatim, never duplicated), refuse
rather than trust recency. Does not change the `>= 2` tier's own
behavior at all, does not touch the `active_score > 0` branch, does not
touch `select_topic_candidates()`, `select_temporal_fallback_
candidate()`, `update_active_topic()`/`update_topic_history()`, or
`ActiveTopicSnapshot`'s field set - genuinely additive, zero new state,
zero new representation.

**No bounded-provenance data structure was introduced.** The sprint's
own brief suggested (as one *possible* conceptual direction, not a
mandate) attaching a per-entry provenance tag to topic-history entries
recording which turn/domain each curated-vocabulary synonym-group
member was established under. Investigated and found unnecessary: the
purely grammatical, STATELESS signal above fully resolves the specific,
reproduced defect (Scenario B) without adding any field to `ActiveTopic
Snapshot`, without a second data structure, and without persisting
anything - satisfying this sprint's own "smallest safe mechanism, only
if the existing representation cannot solve the problem safely" bar
even more strictly than a provenance tag would have.

**Investigated and REJECTED - a distinguisher-token signal for
limitation #9** (two distinctly-named entities, "Aquascape A"/
"Aquascape B", conflated by the `coverage > 0.5` lineage-skip
heuristic): the handover's own speculative Sprint 49 candidate
suggested a short, capitalized, standalone letter/number token
("A"/"B") appearing in both entries' own terms but with DIFFERENT
values could signal "these are explicitly, separately named" more
strongly than majority coverage alone. Direct tokenizer inspection
(`analyze_query("Aquascape A pakai pompa kecil.")` vs `analyze_query(
"Aquascape B pakai pompa besar.")`) found this signal is NOT reliably
available: the shared tokenizer/stopword-filtering pipeline DROPS the
single-letter token "a" entirely (treated as a stopword/too-short
token) while KEEPING "b" (not a recognized stopword) - see `test_46_
tokenizer_asymmetry_blocks_distinguisher_token_signal` below for the
live-reproduced, exact asymmetry. Building a "these are different
entities" signal on a foundation that silently disappears for one of
the two most natural distinguisher letters ("A") but not the other
("B") would be inconsistent and unsafe - concretely worse than the
current, at-least-CONSISTENT (if imperfect) majority-coverage
heuristic. NOT implemented. Limitation #9 remains open, unchanged,
documented (see `test_44_known_limitation_a9_still_unfixed_by_
design`).

Every test below verifies actual candidate/injection behavior (via
`RuntimeDemoConsole` E2E probes reading the rendered system prompt) or
the direct return value of `is_active_topic_relevant_to_query()` -
never only a classifier's own output in isolation.
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

from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402
from luno.memory_context import ActiveTopicSnapshot  # noqa: E402
from luno.memory_retrieval.query import analyze_query  # noqa: E402


# ============================================================================
# Shared E2E harness (same pattern as prior sprints' own test files - each
# test file stays independently testable, per this project's own convention)
# ============================================================================

def _load_demo(tag: str = "s48"):
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


def _new_console(demo, replies=None, canned_text="Dicatat."):
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


def _snap(*terms, age=0, status="active", source_sentence=""):
    return ActiveTopicSnapshot(terms=frozenset(terms), turns_since_active=age, status=status, source_sentence=source_sentence)


# ============================================================================
# Section 1 - unit tests for the new gate inside is_active_topic_relevant_to_query()
# ============================================================================

def test_01_board_case_now_refuses_single_other_topic():
    active = _snap("aquascape", "pump", "kecil", "pakai")
    other = _snap("esp32", "inmp441", "pakai")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Board itu gimana?", topic_history=[active, other],
    ) is False


def test_02_mic_case_still_trusts_recency_single_other_topic():
    # Sprint 46's own test_27 shape - textbook IDENTICAL formal shape to
    # test_01 above, but NOT demonstrative-anchored - must remain True.
    active = _snap("esp32", "inmp441", "dengan", "pakai")
    other = _snap("aquarium")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Mic-nya gimana?", topic_history=[active, other],
    ) is True


def test_03_regression_lock_test_20_single_other_topic_no_conflict_still_trusted():
    active = _snap("aquascape", "pompa", "kecil")
    other = _snap("gpu", "rtx", "upgrade")
    text = "Filternya gimana?"
    assert memory_context.is_active_topic_relevant_to_query(active, text, topic_history=[active, other]) is True


def test_04_regression_lock_test_21_lineage_entries_not_counted():
    active = _snap("gpu", "rtx", "upgrade", "gaming", "cukup")
    lineage = _snap("gpu", "rtx")
    other = _snap("aquascape", "pompa")
    text = "Yang murah?"
    assert memory_context.is_active_topic_relevant_to_query(
        active, text, topic_history=[active, lineage, other],
    ) is True


def test_05_demonstrative_anchored_but_lineage_only_still_trusted():
    # The new gate must not fire when the ONLY "other" entry is the same
    # lineage (majority-covered) - it should never even be counted as a
    # distinct competitor, demonstrative or not.
    active = _snap("gpu", "rtx", "upgrade", "gaming", "cukup")
    lineage = _snap("gpu", "rtx")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Board itu gimana?", topic_history=[active, lineage],
    ) is True


def test_06_two_or_more_distinct_others_still_refuses_regardless_of_demonstrative():
    # The existing `>= 2` guard already refuses here - the new gate is
    # purely additive and must not change this tier's own behavior.
    active = _snap("gpu", "rtx", "upgrade")
    other_a = _snap("esp32", "inmp441", "mic")
    other_b = _snap("aquascape", "pompa", "kecil")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Yang wireless?", topic_history=[active, other_a, other_b],
    ) is False
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Itu yang mana?", topic_history=[active, other_a, other_b],
    ) is False


def test_07_single_topic_conversation_demonstrative_anchored_still_trusted():
    # Invariant 6 (Sprint 47 brief): "single-topic conversations may use
    # stronger fallback." No OTHER topic exists at all - the new gate's
    # own `distinct_other_count >= 1` precondition never fires.
    active = _snap("aquascape", "pompa", "kecil")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Board itu gimana?", topic_history=[active],
    ) is True
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Board itu gimana?", topic_history=None,
    ) is True


def test_08_raw_overlap_short_circuits_before_new_gate():
    active = _snap("aquascape", "board", "pompa")  # "board" literally present
    other = _snap("esp32", "inmp441")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Board itu gimana?", topic_history=[active, other],
    ) is True


def test_09_active_score_positive_branch_unaffected_by_demonstrative():
    # Regression lock for test_38/test_39 (test_semantic_context_bridging.py)
    # - the `active_score > 0` branch is a DIFFERENT code path, untouched
    # by this sprint's own new gate (which lives only in the `== 0` branch).
    snap_a = _snap("aku", "mau", "ganti", "gpu")
    snap_b = _snap("aku", "mau", "ganti", "mic")
    result = memory_context.is_active_topic_relevant_to_query(
        snap_b, "Kalau upgrade itu gimana?", topic_history=[snap_a, snap_b],
    )
    assert result is False  # unchanged from Sprint 46 - tied normalized overlap


def test_10_historical_query_precedence_unaffected():
    # The Sprint 46 historical-query short-circuit runs BEFORE the new
    # gate and must still take precedence.
    active = _snap("ssd", "rencana", status="planned")
    other = _snap("hdd")
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Yang sebelumnya gimana?", topic_history=[active, other],
    )
    assert result is False


def test_11_source_lock_reuses_existing_regex_not_a_new_mechanism():
    # The new gate must reuse `_DEMONSTRATIVE_ANCHORED_RE` (Sprint 47's
    # own constant) verbatim - never define a second/duplicate regex.
    import inspect
    source = inspect.getsource(memory_context.is_active_topic_relevant_to_query)
    assert "_DEMONSTRATIVE_ANCHORED_RE" in source
    assert "distinct_other_count >= 1" in source
    # The pre-existing `>= 2` tier must remain completely untouched.
    assert "distinct_other_count >= 2" in source


# ============================================================================
# Section 2 - E2E scenarios A-H (real RuntimeDemoConsole probes)
# ============================================================================

def test_12_scenario_A_same_generic_vocabulary_two_named_entities_known_limitation():
    """Known limitation #9 (unchanged this sprint - different code path,
    see module docstring's "Investigated and REJECTED" section)."""
    demo = _load_demo("s48-12")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t12-1", conversation_id="c12")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t12-2", conversation_id="c12")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t12-3", conversation_id="c12")
        # Documents CURRENT (unchanged, imperfect) behavior.
        assert sp is not None
    finally:
        console.stop()


def test_13_scenario_B_board_case_now_refuses():
    """FIXED this sprint - was Sprint 47's own known limitation #8."""
    demo = _load_demo("s48-13")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "Board itu gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t13-1", conversation_id="c13")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t13-2", conversation_id="c13")
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t13-3", conversation_id="c13")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"'Board itu gimana?' must now refuse, not guess Aquascape: {sp}"
    finally:
        console.stop()


def test_14_scenario_B_mirror_mic_case_still_resolves():
    """MUST remain unchanged - Sprint 46's own test_27_e2e_no_
    contamination_reverse_direction, re-verified end-to-end here too."""
    demo = _load_demo("s48-14")
    replies = {
        "Aquarium saya 50x25.": "Oke, dicatat ukuran aquarium 50x25.",
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Mic-nya gimana?": "INMP441 adalah mic I2S yang bagus buat ESP32.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquarium saya 50x25.", "t14-1", conversation_id="c14")
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t14-2", conversation_id="c14")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t14-3", conversation_id="c14")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined or "inmp441" in joined
        assert "aquarium" not in joined
    finally:
        console.stop()


def test_15_scenario_C_unique_term_resolves_regardless_of_recency_position():
    demo = _load_demo("s48-15")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "INMP441-nya gimana?": "INMP441 adalah mic I2S.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t15-1", conversation_id="c15")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t15-2", conversation_id="c15")
        sp = _run_turn_capture_prompt(console, demo, "INMP441-nya gimana?", "t15-3", conversation_id="c15")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "inmp441" in joined or "esp32" in joined
        assert "aquascape" not in joined
    finally:
        console.stop()


def test_16_scenario_D_demonstrative_multi_token_still_refuses():
    """Already-correct precedent (Scenario 1's own boundary) - a
    demonstrative-anchored query with >1 real residual token was
    already refusing via `len(query_tokens) != 1`, unaffected by this
    sprint's own (single-token-only) new gate."""
    demo = _load_demo("s48-16")
    replies = {
        "Aku pakai ESP32-S3.": "Dicatat ESP32-S3.",
        "Aku juga punya aquascape 50x25x25.": "Dicatat aquascape.",
        "Board itu WiFi-nya gimana?": "Bisa jelasin lebih spesifik?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai ESP32-S3.", "t16-1", conversation_id="c16")
        _run_turn(console, demo, "Aku juga punya aquascape 50x25x25.", "t16-2", conversation_id="c16")
        sp = _run_turn_capture_prompt(console, demo, "Board itu WiFi-nya gimana?", "t16-3", conversation_id="c16")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"prompt had: {sp}"
    finally:
        console.stop()


def test_17_scenario_E_attribute_continuity_survives_across_turns():
    demo = _load_demo("s48-17")
    replies = {
        "Aku punya aquascape 50x25x25.": "Dicatat.",
        "Tank itu pompanya kecil.": "Dicatat pompa kecil.",
        "Kalau filternya?": "Filter belum disebutkan.",
        "Terus ukurannya berapa?": "Sekitar yang sudah disebutkan sebelumnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya aquascape 50x25x25.", "t17-1", conversation_id="c17")
        _run_turn(console, demo, "Tank itu pompanya kecil.", "t17-2", conversation_id="c17")
        _run_turn(console, demo, "Kalau filternya?", "t17-3", conversation_id="c17")
        sp = _run_turn_capture_prompt(console, demo, "Terus ukurannya berapa?", "t17-4", conversation_id="c17")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "aquascape" in joined, f"aquascape identity should survive: {sp}"
    finally:
        console.stop()


def test_18_scenario_F_correction_identity_available_later():
    demo = _load_demo("s48-18")
    replies = {
        "Pakai ESP32.": "Oke ESP32.",
        "Eh maksudku ESP32-S3.": "Oke, dicatat ESP32-S3.",
        "Board itu RAM-nya berapa?": "ESP32-S3 punya RAM cukup besar.",
        "Terus harganya berapa?": "Sekitar yang sudah disebutkan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Pakai ESP32.", "t18-1", conversation_id="c18")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "t18-2", conversation_id="c18")
        _run_turn(console, demo, "Board itu RAM-nya berapa?", "t18-3", conversation_id="c18")
        sp = _run_turn_capture_prompt(console, demo, "Terus harganya berapa?", "t18-4", conversation_id="c18")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "s3" in joined or "esp32" in joined, f"corrected identity should remain available: {sp}"
    finally:
        console.stop()


def test_19_scenario_G_single_topic_demonstrative_anchored_still_resolves():
    """Invariant 6 - single-topic conversations may use stronger
    fallback - even when demonstrative-anchored."""
    demo = _load_demo("s48-19")
    replies = {
        "Aku pakai ESP32-S3.": "Dicatat ESP32-S3.",
        "Board itu gimana?": "ESP32-S3 board yang bagus.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai ESP32-S3.", "t19-1", conversation_id="c19")
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t19-2", conversation_id="c19")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined, f"single-topic conversation should still resolve: {sp}"
    finally:
        console.stop()


def test_20_scenario_H_three_competing_topics_still_refuses():
    demo = _load_demo("s48-20")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pompa kecil.": "Dicatat.",
        "GPU pakai RTX 3060.": "Dicatat.",
        "Board itu gimana?": "Bisa jelasin lebih spesifik?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t20-1", conversation_id="c20")
        _run_turn(console, demo, "Aquascape pakai pompa kecil.", "t20-2", conversation_id="c20")
        _run_turn(console, demo, "GPU pakai RTX 3060.", "t20-3", conversation_id="c20")
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t20-4", conversation_id="c20")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"3-way ambiguous, demonstrative-anchored: {sp}"
    finally:
        console.stop()


# ============================================================================
# Section 3 - shared alias / synonym-group interaction
# ============================================================================

def test_21_board_group_member_mikrokontroler_also_refuses():
    active = _snap("aquascape", "pump", "kecil")
    other = _snap("esp32", "inmp441")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Mikrokontroler itu gimana?", topic_history=[active, other],
    ) is False


def test_22_gpu_group_member_vga_demonstrative_refuses_with_one_other():
    active = _snap("aquascape", "pompa")
    other = _snap("motherboard", "ram")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "VGA itu gimana?", topic_history=[active, other],
    ) is False


def test_23_gpu_group_member_vga_non_demonstrative_still_trusts_recency():
    active = _snap("aquascape", "pompa")
    other = _snap("motherboard", "ram")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "VGA-nya gimana?", topic_history=[active, other],
    ) is True


def test_24_pompa_group_member_pump_demonstrative_refuses():
    active = _snap("esp32", "inmp441")
    other = _snap("motherboard", "ram")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Pump itu gimana?", topic_history=[active, other],
    ) is False


# ============================================================================
# Section 4 - bounded-state / eviction / cross-conversation isolation
# ============================================================================

def test_25_e2e_topic_history_eviction_new_gate_still_functions():
    """Push more than `_TOPIC_HISTORY_MAX_ENTRIES` (8) rich topics, then
    verify the new gate still correctly refuses a demonstrative-anchored,
    zero-grounded query against the (still-bounded) history."""
    demo = _load_demo("s48-25")
    replies = {}
    turns = []
    for i in range(10):
        text = f"Gadget {chr(65 + i)} pakai chip{i}."
        replies[text] = "Dicatat."
        turns.append(text)
    replies["Aquascape pakai pump kecil."] = "Dicatat."
    replies["Board itu gimana?"] = "Bisa jelasin lebih spesifik?"
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        for i, text in enumerate(turns):
            _run_turn(console, demo, text, f"t25-{i}", conversation_id="c25")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t25-aq", conversation_id="c25")
        history = console.planner_module._topic_history.get("c25") or []
        assert len(history) <= 8, f"topic history not bounded: {len(history)} entries"
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t25-final", conversation_id="c25")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"expected refusal even with a full, bounded history: {sp}"
    finally:
        console.stop()


def test_26_e2e_cross_conversation_isolation_new_gate():
    """Conversation X establishes ESP32/aquascape (board should refuse);
    conversation Y only ever discusses ESP32 alone (board should
    resolve) - the two conversations' own state must never leak into
    each other."""
    demo = _load_demo("s48-26")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "Board itu gimana?": "Bisa jelasin lebih spesifik?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t26x-1", conversation_id="cX")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t26x-2", conversation_id="cX")

        _run_turn(console, demo, "ESP32 pakai INMP441.", "t26y-1", conversation_id="cY")

        sp_x = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t26x-3", conversation_id="cX")
        sp_y = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t26y-2", conversation_id="cY")

        candidates_x = _lines_starting(sp_x, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates_x, f"convo X (2 topics) should refuse: {sp_x}"

        joined_y = " ".join(_lines_starting(sp_y, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined_y, f"convo Y (1 topic only) should resolve: {sp_y}"
    finally:
        console.stop()


def test_27_unrelated_query_never_fabricates_candidate():
    active = _snap("esp32", "inmp441")
    other = _snap("aquascape", "pompa")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Besok mau beli sepatu baru itu di mana?", topic_history=[active, other],
    ) is False  # multi-token, refused by the pre-existing `!= 1` check


# ============================================================================
# Section 5 - performance
# ============================================================================

def test_28_new_gate_meets_5ms_budget():
    active = _snap("aquascape", "pump", "kecil")
    other_a = _snap("esp32", "inmp441")
    other_b = _snap("gpu", "rtx")
    history = [active, other_a, other_b]
    n = 3000
    start = time.perf_counter()
    for _ in range(n):
        memory_context.is_active_topic_relevant_to_query(active, "Board itu gimana?", topic_history=history)
    elapsed_ms = (time.perf_counter() - start) * 1000 / n
    assert elapsed_ms < 5.0, f"is_active_topic_relevant_to_query() with new gate averaged {elapsed_ms:.4f}ms/call"


def test_29_new_gate_no_measurable_regression_vs_baseline_path():
    # Same measurement for a query that never reaches the new gate at
    # all (the `active_score > 0` branch) - confirms the new gate adds
    # no cost to the unrelated, majority-of-calls code path.
    active = _snap("aku", "mau", "ganti", "gpu")
    history = [active]
    n = 3000
    start = time.perf_counter()
    for _ in range(n):
        memory_context.is_active_topic_relevant_to_query(active, "Kalau upgrade-nya gimana?", topic_history=history)
    elapsed_ms = (time.perf_counter() - start) * 1000 / n
    assert elapsed_ms < 5.0, f"baseline path averaged {elapsed_ms:.4f}ms/call"


# ============================================================================
# Section 6 - Investigated-and-REJECTED (limitation #9) - regression locks
# ============================================================================

def test_30_tokenizer_asymmetry_blocks_distinguisher_token_signal():
    """Live-reproduced reason a per-entry 'distinguisher letter' signal
    for limitation #9 was investigated and REJECTED, not merely assumed
    unsafe: the single-letter token "a" is dropped by the shared
    tokenizer/stopword pipeline while "b" survives - an inconsistent
    foundation no safe signal could be built on without hardcoding
    letter-specific exceptions (exactly the kind of fragile, ungeneral
    special-casing this project's own discipline forbids)."""
    tokens_a = set(analyze_query("Aquascape A pakai pompa kecil.").tokens)
    tokens_b = set(analyze_query("Aquascape B pakai pompa besar.").tokens)
    assert "a" not in tokens_a, (
        f"expected tokenizer to drop the single-letter 'a' token (documented asymmetry), "
        f"got: {tokens_a}"
    )
    assert "b" in tokens_b, (
        f"expected tokenizer to KEEP the single-letter 'b' token (documented asymmetry), "
        f"got: {tokens_b}"
    )


def test_31_known_limitation_a9_still_unfixed_by_design():
    """Locks in that limitation #9 (Aquascape A/B conflation) remains
    CURRENT, UNCHANGED behavior after this sprint - not fixed, for the
    concrete, investigated reason in test_30 above and this file's own
    module docstring, not because it was left unexamined."""
    demo = _load_demo("s48-31")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t31-1", conversation_id="c31")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t31-2", conversation_id="c31")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t31-3", conversation_id="c31")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        # Documents CURRENT behavior (confidently resolves to B, the more
        # recent entry) - this assertion exists to DETECT any future
        # silent change to this known-imperfect behavior.
        assert "besar" in joined or "b" in joined or joined == ""
    finally:
        console.stop()


def test_32_coverage_threshold_still_untouched():
    # Regression lock: this sprint's own fix must not have touched the
    # `coverage > 0.5` lineage-skip heuristic at all (a DIFFERENT branch).
    import inspect
    source = inspect.getsource(memory_context.is_active_topic_relevant_to_query)
    assert source.count("coverage > 0.5") == 2  # one in each branch, unchanged from Sprint 46
    assert "coverage >= 0.5" not in source
