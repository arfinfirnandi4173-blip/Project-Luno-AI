"""
test_memory_evaluation.py
==========================

MEMORY EVALUATION & SELF-CALIBRATION sprint - test suite for the
ADDITIONS this sprint made to `luno/memory.py` (evaluation-evidence
schema, `evaluate_memory()`, `calibrate_memory()`, `record_context_selection()`,
`classify_context_outcome()`, the evaluation-aware maintenance branch),
`luno/main_runtime_demo.py` (context-selection tracking + synchronous
recalibration on every feedback event), and `luno/dashboard/collectors.py`
/`controls.py` (evaluation score/confidence/evidence/recommendation
surfaced read-only, new sort modes, a `recalibrate` control).

MOST IMPORTANT RULE, restated (see `luno/memory.py`'s own section banner
near `evaluate_memory()`): an evaluation score is NOT truth. This file
tests that distinction structurally wherever it can, not just by
assertion - e.g. `evaluate_memory()` never reads `importance`/
`usefulness_score` to compute `score` (Section C), `calibrate_memory()`
never writes anything but `evaluation_score`/`last_evaluated_at`
(Section D). UPDATED (Memory Decision Quality & Adaptive Retrieval
sprint): Section F used to assert evaluation never appears in
`ContextItem`/`_rank_key()` at all; that sprint's own approved
specification later added evaluation to `_rank_key()` as an intentional,
low-priority ranking tiebreaker (see Section F's own updated banner and
docstrings). "Evaluation is not truth" still holds - it now participates
in ranking, but strictly subordinate to relevance/importance/context
evidence/usefulness, and it still never feeds into Verified Facts,
`importance`, or `usefulness_score` themselves (Section C's guarantee is
untouched).

Does NOT duplicate `tests/test_memory_learning.py`'s own usefulness/
feedback coverage, `tests/test_memory_maintenance.py`'s own usage-
tracking/planner coverage, or `tests/test_memory_conflict.py`'s
conflict-classification coverage - all three are reused, unchanged, by
this sprint's own logic.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production `config/long_term_memory.json`.

End-to-end scenarios through the REAL production bridge (Step 14) live in
`tests/test_runtime_demo.py`, matching this repository's own established
precedent (every prior memory sprint's E2E scenarios were added there).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

import luno.memory as memory
import luno.memory_context as memory_context
from luno.memory_retrieval.models import MemoryRetrievalConfig, RelevantMemory


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None,
           retrieval_count=None, positive_feedback_count=None, negative_feedback_count=None,
           correction_count=None, conflict_event_count=None,
           retrieval_success_count=None, retrieval_miss_count=None,
           feedback_event_count=None, evaluation_score=None, last_evaluated_at=None):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    entry = {
        "id": id_ or f"e-{abs(hash(text)) % 100000}",
        "text": text, "category": category, "importance": importance, "source": source,
        "created_at": ts, "updated_at": ts, "history": history or [],
    }
    if conflict_status:
        entry["conflict_status"] = conflict_status
    if conflict_group:
        entry["conflict_group"] = conflict_group
    if retrieval_count is not None:
        entry["retrieval_count"] = retrieval_count
    if positive_feedback_count is not None:
        entry["positive_feedback_count"] = positive_feedback_count
    if negative_feedback_count is not None:
        entry["negative_feedback_count"] = negative_feedback_count
    if correction_count is not None:
        entry["correction_count"] = correction_count
    if conflict_event_count is not None:
        entry["conflict_event_count"] = conflict_event_count
    if retrieval_success_count is not None:
        entry["retrieval_success_count"] = retrieval_success_count
    if retrieval_miss_count is not None:
        entry["retrieval_miss_count"] = retrieval_miss_count
    if feedback_event_count is not None:
        entry["feedback_event_count"] = feedback_event_count
    if evaluation_score is not None:
        entry["evaluation_score"] = evaluation_score
    if last_evaluated_at is not None:
        entry["last_evaluated_at"] = last_evaluated_at
    return entry


def _put(entry):
    memory._memories.append(entry)
    return entry


def _rm(entry, score=1.0):
    return RelevantMemory(text=entry["text"], source="manual_memory", score=score, raw=entry)


# ============================================================================
# A - Schema / backward compatibility
# ============================================================================

def test_schema_version_bumped_to_4():
    assert memory.MANUAL_MEMORY_SCHEMA_VERSION == 4


@pytest.mark.parametrize("accessor,field", [
    (memory._get_retrieval_success_count, "retrieval_success_count"),
    (memory._get_retrieval_miss_count, "retrieval_miss_count"),
    (memory._get_feedback_event_count, "feedback_event_count"),
    (memory._get_correction_count, "correction_count"),
    (memory._get_conflict_event_count, "conflict_event_count"),
])
def test_evidence_counters_default_to_zero_on_old_entries(accessor, field):
    old_entry = _entry("some old fact")
    assert field not in old_entry
    assert accessor(old_entry) == 0


@pytest.mark.parametrize("bad_value", [-1, "3", None, True, 2.5])
def test_malformed_evidence_counters_fall_back_to_zero(bad_value):
    e = _entry("fact")
    e["retrieval_success_count"] = bad_value
    assert memory._get_retrieval_success_count(e) == 0


def test_evaluation_score_defaults_to_neutral_not_zero():
    e = _entry("fact")
    assert memory.get_memory_evaluation_score(e) == memory._DEFAULT_EVALUATION_SCORE == 0.5


@pytest.mark.parametrize("bad_value", [-0.1, 1.1, "high", None, float("nan"), True])
def test_malformed_evaluation_score_falls_back_to_default(bad_value):
    e = _entry("fact")
    if bad_value is not None:
        e["evaluation_score"] = bad_value
    assert memory.get_memory_evaluation_score(e) == 0.5


def test_last_evaluated_at_defaults_to_none():
    e = _entry("fact")
    assert memory.get_memory_last_evaluated_at(e) is None


def test_evaluation_never_persists_a_lifecycle_field():
    """Step 3's explicit "do NOT persist `lifecycle` as a field" - proven
    by scanning every write this sprint's mutators perform."""
    e = _put(_entry("fact"))
    memory.calibrate_memory(e["id"])
    live = memory.get_memory(e["id"])
    assert "lifecycle" not in live


# ============================================================================
# B - evaluate_memory(): evidence-based scoring
# ============================================================================

def test_evaluate_memory_empty_evidence_is_neutral_keep():
    e = _entry("brand new fact, no evidence yet")
    result = memory.evaluate_memory(e)
    assert result["score"] == pytest.approx(0.5)
    assert result["confidence"] == 0.0
    assert result["strengths"] == []
    assert result["weaknesses"] == []
    assert result["recommendation"] == "keep"


def test_evaluate_memory_positive_feedback_raises_score():
    e = _entry("fact", positive_feedback_count=2)
    result = memory.evaluate_memory(e)
    assert result["score"] > 0.5
    assert any("positive confirmation" in s for s in result["strengths"])


def test_evaluate_memory_negative_feedback_lowers_score():
    e = _entry("fact", negative_feedback_count=2)
    result = memory.evaluate_memory(e)
    assert result["score"] < 0.5
    assert any("negative feedback" in w for w in result["weaknesses"])


def test_evaluate_memory_correction_lowers_score_more_than_negative_feedback():
    """Step 4: a correction is stronger negative evidence than a bare
    'no' - `_EVAL_CORRECTION_DELTA` > `_EVAL_NEGATIVE_FEEDBACK_DELTA`."""
    e_negative = _entry("fact", negative_feedback_count=1)
    e_correction = _entry("fact", correction_count=1)
    score_negative = memory.evaluate_memory(e_negative)["score"]
    score_correction = memory.evaluate_memory(e_correction)["score"]
    assert score_correction < score_negative


def test_evaluate_memory_usage_alone_is_bounded_below_ceiling():
    """Step 4: 'usage alone does not prove truth' - many successful
    context selections with ZERO explicit feedback can never cross
    `_EVAL_USAGE_ONLY_CEILING`, no matter how large."""
    e = _entry("fact", retrieval_success_count=10_000)
    result = memory.evaluate_memory(e)
    assert result["score"] <= memory._EVAL_USAGE_ONLY_CEILING


def test_evaluate_memory_unconfirmed_repeated_retrieval_is_a_bounded_one_time_penalty():
    """Step 4: 'retrieved repeatedly but never got positive feedback' is
    negative evidence - but bounded, not compounding per extra miss."""
    e_five = _entry("fact", retrieval_miss_count=5)
    e_fifty = _entry("fact", retrieval_miss_count=50)
    score_five = memory.evaluate_memory(e_five)["score"]
    score_fifty = memory.evaluate_memory(e_fifty)["score"]
    assert score_five < 0.5
    assert score_five == score_fifty  # one-time penalty, not compounded


def test_evaluate_memory_does_not_assume_frequent_use_means_correct():
    """Step 4's explicit prohibition: high retrieval_count alone (the
    OLD usage concept, not retrieval_success_count) must not, by itself,
    read as strong positive evidence the way explicit feedback does."""
    e_used_only = _entry("fact", retrieval_count=50)
    e_confirmed = _entry("fact", positive_feedback_count=2)
    assert memory.evaluate_memory(e_used_only)["score"] < memory.evaluate_memory(e_confirmed)["score"]


def test_evaluate_memory_obsolete_wording_is_negative_evidence():
    e = _entry("untuk sementara pakai laptop lama")
    result = memory.evaluate_memory(e)
    assert result["score"] < 0.5
    assert any("obsolete" in w for w in result["weaknesses"])


def test_evaluate_memory_stale_with_no_confirming_evidence_is_negative():
    e = _entry("fact", importance=2, source="user_explicit", days_ago=100)
    assert memory.compute_lifecycle(e) == "stale"
    result = memory.evaluate_memory(e)
    assert result["score"] < 0.5
    assert any("stale" in w for w in result["weaknesses"])


def test_evaluate_memory_historical_memory_with_no_negative_evidence_since_gets_bonus():
    """Step 5: historical truth must not be sacrificed - a memory that
    has survived an update/correction, with no NEW negative evidence
    since, is not penalized for merely having a past."""
    e_plain = _entry("fact")
    e_with_history = _entry("fact", history=[{"text": "old wording", "changed_at": "2020-01-01T00:00:00"}])
    assert memory.evaluate_memory(e_with_history)["score"] > memory.evaluate_memory(e_plain)["score"]


def test_evaluate_memory_ambiguous_conflict_always_recommends_review():
    """Step 10: evaluation must never make an unresolved conflict
    disappear - regardless of any other evidence on the entry."""
    e = _entry("fact", conflict_status="ambiguous_conflict", conflict_group="g1",
               positive_feedback_count=5)  # even strongly positive otherwise
    result = memory.evaluate_memory(e)
    assert result["recommendation"] == "review"


def test_evaluate_memory_never_reads_importance():
    """Step 5's explicit prohibition: importance cannot directly become
    the evaluation score. Two entries, identical evidence, different
    importance -> identical score."""
    e_low = _entry("fact", importance=0, positive_feedback_count=1)
    e_high = _entry("fact", importance=4, positive_feedback_count=1)
    assert memory.evaluate_memory(e_low)["score"] == memory.evaluate_memory(e_high)["score"]


def test_evaluate_memory_never_reads_usefulness_score():
    """Step 5's explicit prohibition: usefulness alone cannot make a
    memory 'considered true' through this path - two entries, identical
    OTHER evidence, wildly different usefulness_score -> identical
    evaluation score."""
    e_low_useful = _entry("fact", positive_feedback_count=1)
    e_low_useful["usefulness_score"] = 0.05
    e_high_useful = _entry("fact", positive_feedback_count=1)
    e_high_useful["usefulness_score"] = 0.95
    assert memory.evaluate_memory(e_low_useful)["score"] == memory.evaluate_memory(e_high_useful)["score"]


def test_evaluate_memory_source_never_reads_importance_or_usefulness_attribute_names():
    """Structural guard, not just a behavioral one - the function body
    itself must never even reference `_get_importance`/`_get_usefulness`/
    `usefulness_score`/`entry.get("importance")` when computing `score`."""
    source = inspect.getsource(memory.evaluate_memory)
    assert "_get_importance(entry)" not in source
    assert "_get_usefulness(entry)" not in source
    assert 'entry.get("usefulness_score")' not in source
    assert 'entry.get("importance")' not in source


def test_evaluate_memory_score_and_confidence_are_bounded():
    e = _entry("fact", positive_feedback_count=999, negative_feedback_count=999,
               correction_count=999, retrieval_success_count=999999)
    result = memory.evaluate_memory(e)
    assert 0.0 <= result["score"] <= 1.0
    assert 0.0 <= result["confidence"] <= 1.0


def test_evaluate_memory_recommendation_is_always_one_of_the_closed_vocabulary():
    cases = [
        _entry("fact"),
        _entry("fact", positive_feedback_count=5),
        _entry("fact", negative_feedback_count=5, correction_count=3),
        _entry("fact", conflict_status="ambiguous_conflict", conflict_group="g"),
        _entry("untuk sementara begini", importance=0),
    ]
    for e in cases:
        result = memory.evaluate_memory(e)
        assert result["recommendation"] in memory.MEMORY_EVALUATION_RECOMMENDATIONS


def test_evaluate_memory_is_pure_never_mutates_and_never_saves(monkeypatch):
    e = _put(_entry("fact", positive_feedback_count=1))
    before = dict(e)
    saved = {"called": False}
    monkeypatch.setattr(memory, "_save", lambda: saved.__setitem__("called", True))
    memory.evaluate_memory(e)
    assert e == before
    assert saved["called"] is False


def test_evaluate_memory_is_deterministic():
    """Same `(entry, now)` -> same output, every time."""
    e = _entry("fact", positive_feedback_count=2, negative_feedback_count=1,
               retrieval_success_count=3, retrieval_miss_count=1)
    now = datetime(2026, 1, 1, 12, 0, 0)
    results = [memory.evaluate_memory(dict(e), now=now) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_evaluate_memory_handles_non_dict_gracefully():
    result = memory.evaluate_memory(None)
    assert result["score"] == 0.5
    assert result["recommendation"] == "keep"


# ============================================================================
# C - Explainability (Step 12)
# ============================================================================

def test_explain_evaluation_format_matches_brief_and_avoids_truth_language():
    e = _entry("fact", positive_feedback_count=1, negative_feedback_count=0)
    text = memory.get_memory_evaluation_explanation(e)
    assert text.startswith("Evaluation: ")
    assert "Confidence: " in text
    assert "Positive evidence:" in text
    assert "Negative evidence:" in text
    assert "Recommendation:" in text
    for forbidden_word in ("truth", "verified", "guaranteed", "fact-checked"):
        assert forbidden_word not in text.lower()


def test_explain_evaluation_shows_none_recorded_when_no_evidence():
    e = _entry("fact")
    text = memory.get_memory_evaluation_explanation(e)
    assert "(none recorded yet)" in text


# ============================================================================
# D - calibrate_memory(): the ONLY writer in this section
# ============================================================================

def test_calibrate_memory_persists_score_and_timestamp():
    e = _put(_entry("fact", positive_feedback_count=2))
    result = memory.calibrate_memory(e["id"])
    assert result is not None
    assert result["evaluation_score"] == memory.evaluate_memory(e)["score"]
    assert result["last_evaluated_at"] is not None
    live = memory.get_memory(e["id"])
    assert live["evaluation_score"] == result["evaluation_score"]


def test_calibrate_memory_is_deterministic():
    e = _put(_entry("fact", positive_feedback_count=2, negative_feedback_count=1))
    r1 = memory.calibrate_memory(e["id"])
    r2 = memory.calibrate_memory(e["id"])
    assert r1["evaluation_score"] == r2["evaluation_score"]


def test_calibrate_memory_backward_compatible_on_pre_sprint_entry():
    e = _put(_entry("old fact"))
    e.pop("history", None)
    result = memory.calibrate_memory(e["id"])
    assert result is not None
    assert 0.0 <= result["evaluation_score"] <= 1.0


def test_calibrate_memory_score_is_bounded():
    e = _put(_entry("fact", positive_feedback_count=999))
    result = memory.calibrate_memory(e["id"])
    assert memory.MEMORY_EVALUATION_MIN <= result["evaluation_score"] <= memory.MEMORY_EVALUATION_MAX


def test_calibrate_memory_never_mutates_text():
    e = _put(_entry("original wording"))
    memory.calibrate_memory(e["id"])
    assert memory.get_memory(e["id"])["text"] == "original wording"


def test_calibrate_memory_never_mutates_importance():
    e = _put(_entry("fact", importance=2, negative_feedback_count=3, correction_count=2))
    memory.calibrate_memory(e["id"])
    assert memory.get_memory(e["id"])["importance"] == 2


def test_calibrate_memory_never_mutates_history():
    e = _put(_entry("fact", history=[{"text": "old", "changed_at": "2020-01-01T00:00:00"}]))
    before_history = list(e["history"])
    memory.calibrate_memory(e["id"])
    assert memory.get_memory(e["id"])["history"] == before_history


def test_calibrate_memory_never_mutates_source_or_conflict_group():
    e = _put(_entry("fact", source="user_explicit", conflict_status="ambiguous_conflict", conflict_group="g1"))
    memory.calibrate_memory(e["id"])
    live = memory.get_memory(e["id"])
    assert live["source"] == "user_explicit"
    assert live["conflict_group"] == "g1"
    assert live["conflict_status"] == "ambiguous_conflict"


def test_calibrate_memory_writes_only_evaluation_score_and_last_evaluated_at():
    e = _put(_entry("fact", positive_feedback_count=1))
    before_keys = set(e.keys())
    memory.calibrate_memory(e["id"])
    after = memory.get_memory(e["id"])
    new_keys = set(after.keys()) - before_keys
    assert new_keys <= {"evaluation_score", "last_evaluated_at"}


def test_calibrate_memory_returns_none_for_unknown_id():
    assert memory.calibrate_memory("does-not-exist") is None


def test_calibrated_memory_is_never_treated_as_a_verified_fact():
    """Step 8's own explicit distinction - calibration never writes
    anything resembling a Verified Fact marker, and `memory_guard`'s
    `VerifiedFact` dataclass has no awareness of this field at all
    (see Section G below for the full structural isolation proof)."""
    e = _put(_entry("fact", positive_feedback_count=5))
    result = memory.calibrate_memory(e["id"])
    assert "verified" not in result
    assert "is_verified_fact" not in result


# ============================================================================
# E - Retrieval outcome tracking (Step 6) + context outcome (Step 7)
# ============================================================================

def test_record_context_selection_increments_success_for_selected_and_miss_for_dropped():
    a = _put(_entry("fact a", id_="a"))
    b = _put(_entry("fact b", id_="b"))
    memory.record_context_selection({"a", "b"}, {"a"})
    assert memory._get_retrieval_success_count(memory.get_memory("a")) == 1
    assert memory._get_retrieval_miss_count(memory.get_memory("a")) == 0
    assert memory._get_retrieval_success_count(memory.get_memory("b")) == 0
    assert memory._get_retrieval_miss_count(memory.get_memory("b")) == 1


def test_record_context_selection_ignores_unknown_ids_silently():
    result = memory.record_context_selection({"ghost"}, {"ghost"})
    assert result == []


def test_record_context_selection_empty_candidates_is_a_no_op():
    assert memory.record_context_selection(set(), set()) == []
    assert memory.record_context_selection(None, None) == []


@pytest.mark.parametrize("text,expected", [
    ("iya benar", "positive"),
    ("itu salah", "negative"),
    ("ok", "neutral"),
    ("oke", "neutral"),
    ("", "unknown"),
    (None, "unknown"),
    ("what's the weather like today", "unknown"),
])
def test_classify_context_outcome_deterministic_signals(text, expected):
    assert memory.classify_context_outcome(text) == expected


def test_classify_context_outcome_memory_updated_wins_as_correction():
    assert memory.classify_context_outcome("iya benar", memory_was_updated=True) == "correction"


def test_classify_context_outcome_silence_is_never_positive():
    assert memory.classify_context_outcome(None) != "positive"
    assert memory.classify_context_outcome("") != "positive"


def test_classify_context_outcome_always_returns_closed_vocabulary():
    for text in ("iya benar", "itu salah", "ok", "", None, "random unrelated sentence",
                 "yang tadi salah, sekarang begini"):
        assert memory.classify_context_outcome(text) in (
            "positive", "negative", "neutral", "correction", "unknown"
        )


# ============================================================================
# F - Retrieval ranking (UPDATED by the Memory Decision Quality & Adaptive
# Retrieval sprint): evaluation is now an intentional, low-priority
# ranking tiebreaker (subordinate to relevance/importance/context
# evidence/usefulness), superseding this section's original Memory
# Evaluation & Self-Calibration sprint invariant that evaluation was
# "transient metadata only", never a ranking signal. See each test's own
# docstring below for the specific old-vs-new contract and reasoning.
# ============================================================================

def test_context_item_evaluation_field_holds_the_shared_evaluate_memory_score():
    """CONTRACT CHANGE (Memory Decision Quality & Adaptive Retrieval
    sprint): replaces `test_context_item_has_no_evaluation_field_at_all`
    from this file's own original Memory Evaluation & Self-Calibration
    sprint, which asserted the OPPOSITE - that `ContextItem` must never
    carry an `evaluation`-shaped field at all, on the grounds that
    "evaluation stays purely observational metadata." That was this
    project's correct, deliberate contract AT THE TIME (evaluation had
    no ranking role yet). The Adaptive Retrieval sprint's own,
    subsequently approved specification explicitly supersedes it - see
    `docs/change_impact/memory_adaptive_retrieval.md` for the full
    architecture audit and decision record. The new authoritative
    ranking order is: relevance -> lifecycle -> conflict handling ->
    importance -> context-specific evidence -> usefulness -> evaluation
    -> usage -> source priority -> budget - i.e. `evaluate_memory()`'s
    existing `score` is now an intentional, LOW-PRIORITY ranking
    tie-breaker among items that already passed every stronger gate
    above it, reusing the EXISTING `evaluate_memory()` output (never a
    second, duplicate evaluation computation) - never a way to
    manufacture relevance for an otherwise-irrelevant item (see
    `test_irrelevant_memory_cannot_be_rescued_by_high_evaluation` below,
    which still holds and is the invariant that actually matters).

    What the OLD test's narrower, still-valid half continues to protect
    (kept here, not discarded): `ContextItem` must never grow an
    `evaluation_confidence` field - CONFIDENCE (evidence volume) has no
    ranking role at all, only the raw SCORE does, so this sprint adds
    exactly ONE new field, not a second, parallel evaluation system
    living on `ContextItem`."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(memory_context.ContextItem)}
    assert "evaluation" in field_names, (
        "ContextItem.evaluation must exist - the approved Adaptive Retrieval contract "
        "requires evaluate_memory()'s score to participate in ranking as a low-priority tiebreaker."
    )
    assert "evaluation_confidence" not in field_names, (
        "only the raw evaluation SCORE participates in ranking - confidence must never leak "
        "in as a second, parallel evaluation-shaped ranking field"
    )
    assert "evaluation_score" not in field_names, (
        "the field is named `evaluation` (matching `usefulness`'s own naming convention on "
        "ContextItem), never a second, differently-named duplicate of the same concept"
    )


def test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker():
    """CONTRACT CHANGE - replaces `test_rank_key_source_never_reads_evaluation`
    (see the sibling test above for the full reasoning/citation).
    `_rank_key()` DOES reference `self.evaluation` now, by design - this
    test asserts WHERE it sits in the tuple: strictly AFTER relevance/
    importance/context_evidence/usefulness and strictly BEFORE
    usage_count/priority, via a direct structural check of the tuple
    itself (not just a textual source-code search), so it can only ever
    break a tie among items that already passed every stronger-priority
    gate - never outrank a relevance, importance, context-evidence, or
    usefulness difference.

    CONTRACT CHANGE #2 (Memory Retrieval & Decision Quality sprint, per
    the SAME Strict Rule #15 precedent as the change above - documented,
    not silently weakened): `_rank_key()`'s tuple grew by one more
    element, `intent_bonus`, inserted strictly AFTER `usage_count` and
    strictly BEFORE `priority` - the sprint's own approved "bounded
    intent/continuity preference -> existing source-priority tie-break"
    ordering. `intent_bonus` defaults to `None` (contributes `0.0`) for
    every item unless `assemble_context()` was given an `intent`/
    `previous_topic_terms` this turn, so this item (constructed without
    either) contributes exactly `0.0` here - proving the new field slots
    in without disturbing any of the six pre-existing positions this
    test already covers.

    CONTRACT CHANGE #3 (Sprint 40, Memory Confidence & Conflict
    Resolution, per the SAME Strict Rule #15 precedent as the two changes
    above - documented, not silently weakened): `_rank_key()`'s tuple
    grew by one FINAL trailing element, `confidence`, inserted strictly
    AFTER `priority` (i.e. last position, after every pre-existing
    field). `confidence` defaults to `None` (contributes `0.0`) for
    every item unless the retrieval layer's `relevant_memory_to_context_item()`
    populated it (currently only for `active_conversation`-sourced
    items), so this item (constructed directly, bypassing that adapter)
    contributes exactly `0.0` here - proving the new field slots in
    without disturbing any of the eight pre-existing positions this test
    already covers. Deliberately placed LAST rather than earlier in the
    tuple (contrast with the brief's own abstract "relevance > confidence
    > importance" framing) - the only reproduced defect confidence fixes
    is an arbitrary tie between two ALREADY-tied `active_conversation`
    items (current vs. superseded topic), so it only needs to break ties
    after every other existing signal, never ahead of them."""
    source = inspect.getsource(memory_context.ContextItem._rank_key)
    assert "self.evaluation" in source, "evaluation must be read by _rank_key() per the approved contract"
    assert "self.intent_bonus" in source, "intent_bonus must be read by _rank_key() per the approved contract"
    assert "self.confidence" in source, "confidence must be read by _rank_key() per the approved contract"

    item = memory_context.ContextItem(
        source="manual_memory", memory_id="x", text="x",
        relevance=0.11, importance=2, context_evidence=0.33,
        usefulness=0.55, evaluation=0.77, usage_count=9, priority=3,
    )
    assert item._rank_key() == (0.11, 2, 0.33, 0.55, 0.77, 9, 0.0, 3, 0.0), (
        "expected the exact tuple order (relevance, importance, context_evidence, "
        "usefulness, evaluation, usage_count, intent_bonus, priority, confidence) - "
        f"got {item._rank_key()}"
    )


def test_irrelevant_memory_cannot_be_rescued_by_high_evaluation():
    """UPDATED (Memory Decision Quality & Adaptive Retrieval sprint):
    renamed from `test_irrelevant_memory_never_rescued_by_high_evaluation`.
    Since that original sprint, `evaluation` now DOES participate in
    `_rank_key()` (see `test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker`
    above) - so this test is strengthened rather than left to hold
    "trivially": it now sets evaluation to its maximum (1.0) on the
    low-relevance item and to its minimum (0.0) on the high-relevance
    item, i.e. actively tries to let evaluation rescue the irrelevant
    one, and confirms it still cannot - relevance occupies tuple
    position 0 and dominates every comparison regardless of what any
    later-tier signal (importance/context_evidence/usefulness/
    evaluation) contains. This is the actual safety property Phase 3 of
    the Adaptive Retrieval sprint requires ("an irrelevant memory must
    never become relevant merely because ... evaluation ... is high")."""
    low_relevance_high_eval = memory_context.ContextItem(
        source="manual_memory", memory_id="a", text="a",
        relevance=0.1, importance=4, context_evidence=1.0, usefulness=1.0, evaluation=1.0,
    )
    high_relevance_low_eval = memory_context.ContextItem(
        source="manual_memory", memory_id="b", text="b",
        relevance=0.9, importance=0, context_evidence=0.0, usefulness=0.0, evaluation=0.0,
    )
    assert high_relevance_low_eval._rank_key() > low_relevance_high_eval._rank_key()


def test_importance_still_outranks_usefulness_and_evaluation():
    """UPDATED (Memory Decision Quality & Adaptive Retrieval sprint):
    renamed from
    `test_importance_still_outranks_usefulness_with_evaluation_evidence_present`.
    The old docstring's claim that "`_rank_key()`'s tuple order
    (relevance, importance, usefulness, priority) is completely
    unchanged by this sprint" is no longer accurate - the tuple grew to
    (relevance, importance, context_evidence, usefulness, evaluation,
    usage_count, priority) to add context-specific evidence and
    evaluation as approved by the Adaptive Retrieval sprint. What DOES
    still hold, and is what this test actually protects: importance
    (tier 2) still outranks usefulness AND evaluation (tiers 4 and 5)
    even when the lower-importance item is maxed out on both of those
    later signals."""
    high_importance = memory_context.ContextItem(
        source="manual_memory", memory_id="a", text="a",
        relevance=0.8, importance=4, context_evidence=0.0, usefulness=0.1, evaluation=0.0,
    )
    high_usefulness_low_importance = memory_context.ContextItem(
        source="manual_memory", memory_id="b", text="b",
        relevance=0.8, importance=1, context_evidence=1.0, usefulness=1.0, evaluation=1.0,
    )
    assert high_importance._rank_key() > high_usefulness_low_importance._rank_key()


def test_assemble_context_still_gates_on_relevance_before_anything_else(monkeypatch):
    """End-to-end-ish (still in-process, no real bridge): two manual
    memories with rich evaluation evidence recorded on one of them, fed
    through the real `assemble_context()` - relevance/budget behavior is
    completely unaffected by this sprint's additions."""
    a = _put(_entry("user suka kopi hitam setiap pagi", id_="coffee"))
    memory.record_context_selection({"coffee"}, {"coffee"})
    memory.calibrate_memory("coffee")

    class _StubRetriever:
        def retrieve_memories(self, text):
            return [_rm(a, score=0.9)]

    result = memory_context.assemble_context(
        "apa kebiasaan pagi user?",
        memory_retriever=_StubRetriever(),
        get_manual_memories=lambda: memory.list_memories(),
        config=MemoryRetrievalConfig.from_env(),
    )
    assert any(item.memory_id == "coffee" for item in result.items)


def test_verified_facts_remain_separate_from_evaluation():
    from luno.memory_guard import VerifiedFact
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(VerifiedFact)}
    assert "evaluation_score" not in field_names
    assert "retrieval_success_count" not in field_names
    assert "correction_count" not in field_names


# ============================================================================
# G - Maintenance integration (Step 10) - advisory only, never auto-delete
# ============================================================================

def test_stale_memory_with_strong_evaluation_evidence_upgrades_to_reinforce():
    e = _put(_entry("fact", importance=2, source="user_explicit", days_ago=200))
    assert memory.compute_lifecycle(e) == "stale"
    for _ in range(4):
        memory.apply_positive_feedback(e["id"])
    plan = memory._plan_action_for_entry(memory.get_memory(e["id"]))
    assert plan["action"] == "reinforce"


def test_stale_memory_with_ambiguous_low_confidence_evaluation_becomes_review():
    e = _put(_entry("fact", importance=2, source="user_explicit", days_ago=200,
                     positive_feedback_count=1, negative_feedback_count=1))
    plan = memory._plan_action_for_entry(memory.get_memory(e["id"]))
    assert plan["action"] == "review"


def test_stale_memory_with_zero_real_evidence_still_defaults_to_archive():
    """Evaluation must never manufacture a NEW way to protect a memory
    out of thin air - a plain, unconfirmed stale memory is archived
    exactly as it was before this sprint."""
    e = _put(_entry("fact", importance=2, source="user_explicit", days_ago=200))
    plan = memory._plan_action_for_entry(memory.get_memory(e["id"]))
    assert plan["action"] == "archive"


def test_evaluation_cannot_auto_delete_or_archive_via_analyze_memory_maintenance():
    """`analyze_memory_maintenance()`/`evaluate_memory()` are both
    analysis-only - even a very low evaluation score never itself
    removes an entry from `_memories`; only an explicit
    `apply_maintenance_plan()` call can, and only via the SAME
    pre-existing archive path."""
    e = _put(_entry("fact", negative_feedback_count=5, correction_count=5, days_ago=200,
                     importance=2, source="user_explicit"))
    before_count = len(memory._memories)
    memory.analyze_memory_maintenance()
    memory.evaluate_memory(memory.get_memory(e["id"]))
    assert len(memory._memories) == before_count
    assert memory.get_memory(e["id"]) is not None


def test_unresolved_conflict_remains_protected_regardless_of_evaluation():
    e = _put(_entry("fact", conflict_status="ambiguous_conflict", conflict_group="g1",
                     negative_feedback_count=5))
    plan = memory._plan_action_for_entry(memory.get_memory(e["id"]))
    assert plan["action"] == "review"
    assert memory._is_protected_from_archival(memory.get_memory(e["id"]))


def test_analyze_memory_maintenance_is_still_pure_with_evaluation_integrated():
    e = _put(_entry("fact", importance=2, source="user_explicit", days_ago=200, positive_feedback_count=3))
    before = dict(memory.get_memory(e["id"]))
    memory.analyze_memory_maintenance()
    after = memory.get_memory(e["id"])
    assert before == after


# ============================================================================
# H - main_runtime_demo.py wiring (feedback -> record_feedback_event + calibrate)
# ============================================================================

def test_feedback_event_count_increments_via_record_feedback_event():
    e = _put(_entry("fact"))
    assert memory._get_feedback_event_count(memory.get_memory(e["id"])) == 0
    memory.record_feedback_event(e["id"])
    assert memory._get_feedback_event_count(memory.get_memory(e["id"])) == 1


def test_update_memory_with_correction_reason_increments_correction_count():
    e = _put(_entry("old wording"))
    assert memory._get_correction_count(memory.get_memory(e["id"])) == 0
    memory.update_memory(e["id"], "new wording", reason="correction")
    assert memory._get_correction_count(memory.get_memory(e["id"])) == 1


def test_update_memory_with_non_correction_reason_does_not_increment_correction_count():
    e = _put(_entry("old wording"))
    memory.update_memory(e["id"], "refined wording", reason="refinement")
    assert memory._get_correction_count(memory.get_memory(e["id"])) == 0


def test_tag_ambiguous_conflict_increments_conflict_event_count_on_both_sides():
    a = _put(_entry("fact a", id_="a"))
    b = _put(_entry("fact b", id_="b"))
    memory._tag_ambiguous_conflict(b, a)
    assert memory._get_conflict_event_count(memory.get_memory("a")) == 1
    assert memory._get_conflict_event_count(memory.get_memory("b")) == 1


# ============================================================================
# I - Dashboard surface (Step 11/12/13) - read-only, no mutation from GET
# ============================================================================

def test_dashboard_list_renders_evaluation_score_confidence_and_recommendation():
    from luno.dashboard import collectors
    e = _put(_entry("fact", positive_feedback_count=1))
    data = collectors.collect_memory_list()
    row = next(r for r in data["items"] if r["id"] == e["id"])
    assert "evaluation_score" in row
    assert "evaluation_confidence" in row
    assert "evaluation_recommendation" in row
    assert row["evaluation_recommendation"] in memory.MEMORY_EVALUATION_RECOMMENDATIONS


def test_dashboard_detail_renders_evidence_and_explanation():
    from luno.dashboard import collectors
    e = _put(_entry("fact", positive_feedback_count=2, negative_feedback_count=1))
    detail = collectors.collect_memory_detail(e["id"])
    assert detail["evaluation"]["score"] is not None
    assert detail["evidence_counts"]["positive_feedback_count"] == 2
    assert detail["evidence_counts"]["negative_feedback_count"] == 1
    assert "Recommendation:" in detail["evaluation_explanation"]


@pytest.mark.parametrize("sort_mode", ["highest_evaluation", "lowest_evaluation", "low_confidence", "recently_evaluated"])
def test_dashboard_new_sort_modes_do_not_error_and_return_all_matches(sort_mode):
    from luno.dashboard import collectors
    _put(_entry("fact one", id_="s1", positive_feedback_count=2))
    _put(_entry("fact two", id_="s2", negative_feedback_count=1))
    data = collectors.collect_memory_list(sort=sort_mode)
    assert data["total_matched"] == 2


def test_dashboard_highest_evaluation_sort_orders_correctly():
    from luno.dashboard import collectors
    _put(_entry("weak fact", id_="weak", negative_feedback_count=3, correction_count=2))
    _put(_entry("strong fact", id_="strong", positive_feedback_count=3))
    data = collectors.collect_memory_list(sort="highest_evaluation")
    ids_in_order = [row["id"] for row in data["items"]]
    assert ids_in_order.index("strong") < ids_in_order.index("weak")


def test_dashboard_list_and_detail_never_mutate_or_save(monkeypatch):
    from luno.dashboard import collectors
    e = _put(_entry("fact", positive_feedback_count=1))
    saved = {"called": False}
    monkeypatch.setattr(memory, "_save", lambda: saved.__setitem__("called", True))
    collectors.collect_memory_overview()
    collectors.collect_memory_list(sort="highest_evaluation")
    collectors.collect_memory_detail(e["id"])
    assert saved["called"] is False


def test_dashboard_overview_evaluation_recommendation_tally():
    from luno.dashboard import collectors
    _put(_entry("fact", positive_feedback_count=1))
    overview = collectors.collect_memory_overview()
    tally = overview["evaluation_recommendations"]
    assert set(tally.keys()) == set(memory.MEMORY_EVALUATION_RECOMMENDATIONS)
    assert sum(tally.values()) == 1


def test_dashboard_recalibrate_control_persists_and_requires_valid_id():
    from luno.dashboard import controls
    e = _put(_entry("fact", positive_feedback_count=1))
    r = controls.memory_recalibrate(e["id"])
    assert r["ok"] is True
    assert memory.get_memory(e["id"])["last_evaluated_at"] is not None

    bad = controls.memory_recalibrate("")
    assert bad["ok"] is False
    bad2 = controls.memory_recalibrate("does-not-exist")
    assert bad2["ok"] is False


def test_dashboard_feedback_controls_also_calibrate():
    from luno.dashboard import controls
    e = _put(_entry("fact"))
    r = controls.memory_feedback_positive(e["id"])
    assert r["ok"] is True
    live = memory.get_memory(e["id"])
    assert live.get("feedback_event_count", 0) == 1
    assert live.get("last_evaluated_at") is not None


# ============================================================================
# J - Verified Facts / Episodic Memory isolation (guard, not a new
# implementation - confirming this sprint's own additions never touch
# either boundary)
# ============================================================================

def test_evaluation_functions_never_reference_verified_fact_store_or_episodic_memory():
    functions = [
        memory.evaluate_memory, memory._explain_evaluation, memory.calibrate_memory,
        memory.record_context_selection, memory.classify_context_outcome,
        memory.record_feedback_event, memory._get_evaluation_score,
        memory._get_retrieval_success_count, memory._get_retrieval_miss_count,
        memory._get_correction_count, memory._get_conflict_event_count,
        memory._get_feedback_event_count, memory._plan_action_for_entry,
    ]
    forbidden = ("memory_guard", "VerifiedFactStore", "episodic_memory", "EpisodicMemoryStore")
    for fn in functions:
        source = inspect.getsource(fn)
        for token in forbidden:
            assert token not in source, f"{fn.__name__} unexpectedly references {token}"


def test_no_truth_score_field_exists_anywhere():
    """Step 9's explicit instruction: do NOT add a `truth_score` field
    unless truly required - it wasn't, so it must not exist."""
    e = _put(_entry("fact", positive_feedback_count=3))
    calibrated = memory.calibrate_memory(e["id"])
    assert "truth_score" not in calibrated
    assert "truth_score" not in memory.evaluate_memory(e)
    assert not hasattr(memory, "MEMORY_TRUTH_MIN")
