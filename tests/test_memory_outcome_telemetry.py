"""
test_memory_outcome_telemetry.py
==================================

MEMORY OUTCOME TELEMETRY & CLOSED-LOOP LEARNING sprint - test suite for
the ADDITIONS this sprint made to `luno/memory.py` (`record_outcome_evidence()`,
`get_conflict_group_member_ids()`, `get_memory_outcome_summary()`,
`get_memory_selection_explanation()`, `classify_context_outcome()`'s
corrected negative-before-positive priority), `luno/memory_turn_trace.py`
(new module - `MemoryTurnTrace`/`build_turn_trace()`),
`main_runtime_demo.py` (wires `classify_context_outcome()` into
`_handle_memory_feedback_command()`'s actual dispatch, builds/stores a
bounded per-conversation `MemoryTurnTrace`), and
`luno/dashboard/collectors.py` (Outcome/"why selected" panels).

Does NOT duplicate `tests/test_memory_learning.py`'s own feedback-loop
coverage or `tests/test_memory_evaluation.py`'s own `evaluate_memory()`/
`calibrate_memory()` coverage - both are reused, unchanged, by this
sprint's own logic. This file only covers what is NEW: turn-scoped
selection tracking (candidate/relevant/selected/rendered, no double-
counting), outcome-driven evidence mapping, outcome classification
priority, and the read-only outcome/explainability surface.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file.

End-to-end scenarios through the REAL production bridge (Step 18) live in
`tests/test_runtime_demo.py`, matching this repository's own established
precedent.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

import luno.memory as memory
from luno.memory_context import AssembledContext, ContextItem
from luno.memory_retrieval.models import RelevantMemory
from luno.memory_turn_trace import MemoryTurnTrace, build_turn_trace


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None):
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
    return entry


def _put(entry):
    memory._memories.append(entry)
    return entry


def _rm(entry, score=1.0, historical=False):
    text = entry["text"]
    if historical:
        text = f"[MANUAL MEMORY - {entry.get('category', 'other')}, historical] {text}"
    return RelevantMemory(text=text, source="manual_memory", score=score, raw=entry)


def _item(memory_id, source="manual_memory", relevance=0.7, importance=2, conflict_group=None):
    return ContextItem(source=source, memory_id=memory_id, text="x", relevance=relevance,
                        importance=importance, conflict_group=conflict_group)


# ============================================================================
# A - Selection tracking (candidate/relevant/selected/rendered, no double-
# counting)
# ============================================================================

def test_selected_memory_is_tracked_as_candidate_and_selected():
    a = _put(_entry("fact a", id_="a"))
    rel = [_rm(a, score=0.9)]
    ac = AssembledContext(items=[_item("a")])
    trace = build_turn_trace("t1", rel, ac)
    assert trace.candidate_memory_ids == {"a"}
    assert trace.relevant_memory_ids == {"a"}
    assert trace.selected_memory_ids == {"a"}
    assert trace.rendered_memory_ids == {"a"}
    assert trace.not_selected_memory_ids() == set()


def test_unselected_relevant_memory_is_candidate_but_not_selected():
    a = _put(_entry("fact a", id_="a"))
    rel = [_rm(a, score=0.9)]
    ac = AssembledContext(items=[])  # lost to ranking/budget
    trace = build_turn_trace("t2", rel, ac)
    assert trace.candidate_memory_ids == {"a"}
    assert trace.selected_memory_ids == set()
    assert trace.not_selected_memory_ids() == {"a"}


def test_irrelevant_memory_never_becomes_a_candidate():
    _put(_entry("fact a", id_="a"))
    _put(_entry("fact b", id_="b"))
    # "b" is simply never surfaced by the retriever this turn.
    rel = [_rm(memory.get_memory("a"), score=0.9)]
    ac = AssembledContext(items=[_item("a")])
    trace = build_turn_trace("t3", rel, ac)
    assert "b" not in trace.candidate_memory_ids
    assert "b" not in trace.selected_memory_ids


def test_conflict_group_selection_counts_each_real_member_exactly_once():
    a = _put(_entry("color is blue", id_="a", conflict_status="ambiguous_conflict", conflict_group="g1"))
    b = _put(_entry("color is red", id_="b", conflict_status="ambiguous_conflict", conflict_group="g1"))
    # The conflict note is rendered under a SYNTHETIC id, never a real one.
    ac = AssembledContext(items=[_item("conflict:g1", conflict_group="g1")])
    trace = build_turn_trace("t4", [], ac)
    assert trace.selected_memory_ids == {"a", "b"}
    assert "conflict:g1" not in trace.selected_memory_ids
    # Each real member appears in the id SET exactly once (sets cannot
    # hold a duplicate) - this is the "no double counting" guarantee.
    assert len(trace.selected_memory_ids) == 2


def test_historical_entry_and_current_entry_count_once_not_twice():
    a = _put(_entry("fact a", id_="a"))
    # The SAME underlying memory surfaced twice this turn - once as its
    # current wording, once as a historical-query hit (same `raw`, same
    # id, different rendered text/score) - a real, possible shape per
    # `make_manual_memory_source()`'s own historical-query branch.
    rel = [_rm(a, score=0.9), _rm(a, score=0.5, historical=True)]
    ac = AssembledContext(items=[_item("a")])
    trace = build_turn_trace("t5", rel, ac)
    assert trace.candidate_memory_ids == {"a"}
    assert len(trace.candidate_memory_ids) == 1


def test_duplicate_context_sections_do_not_double_count():
    """A memory id appearing more than once in `assembled_context.items`
    (e.g. once as a normal item, once - hypothetically - re-surfaced by
    another section) is still counted exactly once, because every
    tracking field here is a `set`, not a list/counter."""
    a = _put(_entry("fact a", id_="a"))
    ac = AssembledContext(items=[_item("a"), _item("a")])
    trace = build_turn_trace("t6", [_rm(a)], ac)
    assert trace.selected_memory_ids == {"a"}
    assert len(trace.selected_memory_ids) == 1


def test_verified_fact_and_experience_ids_tracked_separately_read_only():
    ac = AssembledContext(items=[
        _item("living_room_light", source="verified_facts"),
        _item("exp-1", source="episodic_memory"),
    ])
    trace = build_turn_trace("t7", [], ac)
    assert trace.selected_verified_fact_ids == {"living_room_light"}
    assert trace.selected_experience_ids == {"exp-1"}
    assert trace.selected_memory_ids == set()  # never mixed into the manual-memory pool


def test_turn_trace_never_carries_message_text():
    """Hard constraint #17 - a `MemoryTurnTrace` must never carry the
    user's message or the assistant's response, only ids/scores/short
    reason strings. Structural guard via dataclass field inspection."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(MemoryTurnTrace)}
    for forbidden in ("user_text", "response_text", "transcript", "message", "utterance"):
        assert forbidden not in field_names


def test_build_turn_trace_is_pure_read_only(monkeypatch):
    a = _put(_entry("fact a", id_="a"))
    saved = {"called": False}
    monkeypatch.setattr(memory, "_save", lambda: saved.__setitem__("called", True))
    ac = AssembledContext(items=[_item("a")])
    build_turn_trace("t8", [_rm(a)], ac)
    assert saved["called"] is False


# ============================================================================
# B - Outcome classification
# ============================================================================

@pytest.mark.parametrize("text,expected", [
    ("iya benar", "positive"),
    ("itu salah", "negative"),
    ("ok", "neutral"),
    ("yang tadi salah, sekarang RTX 4090", "correction"),
    ("bagaimana cuaca hari ini", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_classify_context_outcome_matrix(text, expected):
    assert memory.classify_context_outcome(text) == expected


def test_classify_context_outcome_silence_is_unknown_not_positive():
    assert memory.classify_context_outcome(None) == "unknown"
    assert memory.classify_context_outcome("") == "unknown"
    assert memory.classify_context_outcome("   ") == "unknown"


def test_classify_context_outcome_ambiguous_feedback_text_alone_is_negative_not_correction():
    """Step 9's own example: "itu salah" with NO replacement clause is
    `negative`, not `correction` - correction requires an actual captured
    replacement value."""
    assert memory.classify_context_outcome("itu salah") == "negative"
    assert memory.detect_memory_feedback_correction("itu salah") is None


# ============================================================================
# C - Priority
# ============================================================================

def test_correction_beats_negative():
    # A correction-shaped message always wins as "correction" even though
    # it also describes something as wrong (which the bare negative regex
    # would otherwise match in isolation).
    assert memory.classify_context_outcome("itu salah, yang benar adalah RTX 4090") == "correction"


def test_memory_was_updated_always_wins_as_correction_regardless_of_text():
    assert memory.classify_context_outcome("iya benar", memory_was_updated=True) == "correction"
    assert memory.classify_context_outcome("random unrelated text", memory_was_updated=True) == "correction"


def test_negative_checked_before_positive_in_source_order():
    """Step 6's explicit priority list ranks explicit negative feedback
    ABOVE explicit positive feedback. The two regex sets are fully
    anchored and mutually exclusive today (no real text matches both), so
    this is proven structurally: the negative check must appear before
    the positive check in the function's own source."""
    source = inspect.getsource(memory.classify_context_outcome)
    # Search for the actual CODE check (`if (detect_...`), not any mention
    # of the function name in the docstring's prose (which discusses both
    # in the priority-list explanation, in either order).
    negative_idx = source.index("if (detect_negative_memory_feedback")
    positive_idx = source.index("if (detect_positive_memory_feedback")
    assert negative_idx < positive_idx


def test_explicit_positive_beats_neutral():
    assert memory.classify_context_outcome("iya benar") == "positive"
    assert memory.classify_context_outcome("iya benar") != "neutral"


def test_unknown_remains_unknown_for_unrecognized_text():
    assert memory.classify_context_outcome("aku mau makan siang dulu ya") == "unknown"


# ============================================================================
# D - Evidence mapping (record_outcome_evidence)
# ============================================================================

def test_positive_outcome_bumps_retrieval_success_count():
    e = _put(_entry("fact"))
    result = memory.record_outcome_evidence(e["id"], "positive")
    assert result["retrieval_success_count"] == 1
    assert memory._get_retrieval_success_count(memory.get_memory(e["id"])) == 1


def test_negative_outcome_bumps_retrieval_miss_count():
    e = _put(_entry("fact"))
    result = memory.record_outcome_evidence(e["id"], "negative")
    assert result["retrieval_miss_count"] == 1


def test_neutral_and_unknown_outcomes_are_no_ops():
    e = _put(_entry("fact"))
    before = dict(memory.get_memory(e["id"]))
    assert memory.record_outcome_evidence(e["id"], "neutral") is None
    assert memory.record_outcome_evidence(e["id"], "unknown") is None
    assert memory.get_memory(e["id"]) == before


def test_correction_outcome_is_a_no_op_in_record_outcome_evidence():
    """Correction's evidence (`correction_count`) is exclusively
    `update_memory()`'s responsibility - `record_outcome_evidence()`
    itself does nothing for `"correction"`, avoiding a double mutation
    path."""
    e = _put(_entry("fact"))
    assert memory.record_outcome_evidence(e["id"], "correction") is None


def test_record_outcome_evidence_unknown_memory_id_returns_none():
    assert memory.record_outcome_evidence("does-not-exist", "positive") is None


def test_record_outcome_evidence_is_bounded_like_retrieval_count():
    e = _put(_entry("fact"))
    e["retrieval_success_count"] = memory._MAX_RETRIEVAL_COUNT
    memory.record_outcome_evidence(e["id"], "positive")
    assert memory._get_retrieval_success_count(memory.get_memory(e["id"])) == memory._MAX_RETRIEVAL_COUNT


def test_get_conflict_group_member_ids_resolves_real_members():
    _put(_entry("a", id_="a", conflict_status="ambiguous_conflict", conflict_group="g1"))
    _put(_entry("b", id_="b", conflict_status="ambiguous_conflict", conflict_group="g1"))
    _put(_entry("c", id_="c"))  # unrelated, not in the group
    members = set(memory.get_conflict_group_member_ids("g1"))
    assert members == {"a", "b"}


def test_get_conflict_group_member_ids_empty_for_unknown_group():
    assert memory.get_conflict_group_member_ids("no-such-group") == []
    assert memory.get_conflict_group_member_ids(None) == []
    assert memory.get_conflict_group_member_ids("") == []


# ============================================================================
# E - Safety
# ============================================================================

def test_ambiguous_feedback_never_mutates_a_random_memory():
    """`record_outcome_evidence()`/`classify_context_outcome()` never
    guess a target - a caller with no resolved id simply never calls
    either mutator with one. Simulated here by confirming the mutator
    itself does nothing when given no valid id (the actual ambiguous-
    target-resolution safety net lives in `main_runtime_demo.py`'s
    session feedback target, covered by Scenario D in
    `tests/test_runtime_demo.py`)."""
    a = _put(_entry("fact a", id_="a"))
    b = _put(_entry("fact b", id_="b"))
    before_a, before_b = dict(a), dict(b)
    assert memory.record_outcome_evidence(None, "positive") is None
    assert memory.record_outcome_evidence("", "negative") is None
    assert memory.get_memory("a") == before_a
    assert memory.get_memory("b") == before_b


def test_unknown_outcome_never_changes_evaluation_score():
    e = _put(_entry("fact"))
    memory.calibrate_memory(e["id"])
    before_score = memory.get_memory(e["id"])["evaluation_score"]
    memory.record_outcome_evidence(e["id"], "unknown")
    assert memory.get_memory(e["id"])["evaluation_score"] == before_score


def test_dashboard_get_does_not_mutate_state(monkeypatch):
    from luno.dashboard import collectors
    e = _put(_entry("fact", conflict_status=None))
    memory.record_outcome_evidence(e["id"], "positive")
    saved = {"called": False}
    monkeypatch.setattr(memory, "_save", lambda: saved.__setitem__("called", True))
    collectors.collect_memory_detail(e["id"])
    collectors.collect_memory_overview()
    collectors.collect_memory_list()
    assert saved["called"] is False


def test_telemetry_never_modifies_text_history_or_importance():
    e = _put(_entry("original text", importance=3, history=[{"text": "old", "changed_at": "2020-01-01T00:00:00"}]))
    memory.record_outcome_evidence(e["id"], "positive")
    memory.record_outcome_evidence(e["id"], "negative")
    live = memory.get_memory(e["id"])
    assert live["text"] == "original text"
    assert live["importance"] == 3
    assert live["history"] == [{"text": "old", "changed_at": "2020-01-01T00:00:00"}]


def test_get_memory_outcome_summary_shape_and_read_only(monkeypatch):
    e = _put(_entry("fact"))
    memory.record_outcome_evidence(e["id"], "positive")
    memory.calibrate_memory(e["id"])
    saved = {"called": False}
    monkeypatch.setattr(memory, "_save", lambda: saved.__setitem__("called", True))
    summary = memory.get_memory_outcome_summary(e["id"])
    assert saved["called"] is False
    assert set(summary.keys()) == {
        "memory_id", "retrieval_success_count", "retrieval_miss_count",
        "feedback_event_count", "correction_count", "evaluation_score",
        "evaluation_confidence", "last_evaluated_at",
    }
    assert "text" not in summary
    assert "history" not in summary


def test_get_memory_outcome_summary_unknown_id_returns_none():
    assert memory.get_memory_outcome_summary("nope") is None


def test_get_memory_selection_explanation_never_claims_truth():
    e = _put(_entry("fact"))
    text = memory.get_memory_selection_explanation(memory.get_memory(e["id"]))
    for forbidden in ("is true", "is definitely", "AI decided", "guaranteed"):
        assert forbidden not in text.lower()


# ============================================================================
# F - Verified Facts / Episodic Memory isolation (guard, not a new
# implementation)
# ============================================================================

def test_outcome_telemetry_functions_never_reference_verified_fact_store_or_episodic_memory():
    # `build_turn_trace()` legitimately reads the pre-existing
    # `ContextItem.source == "episodic_memory"` STRING TAG (the same
    # source-name convention `memory_context._SOURCE_PRIORITY` itself
    # already uses) as read-only awareness, per its own module docstring
    # - that is not an import of or reference to the `luno.episodic_memory`
    # MODULE/`EpisodicMemoryStore` CLASS, so it is checked against a
    # narrower forbidden list than the other, memory.py-only functions.
    functions_full_isolation = [
        memory.record_outcome_evidence, memory.classify_context_outcome,
        memory.get_conflict_group_member_ids, memory.get_memory_outcome_summary,
        memory.get_memory_selection_explanation,
    ]
    forbidden = ("memory_guard", "VerifiedFactStore", "episodic_memory", "EpisodicMemoryStore")
    for fn in functions_full_isolation:
        source = inspect.getsource(fn)
        for token in forbidden:
            assert token not in source, f"{fn.__name__} unexpectedly references {token}"

    trace_source = inspect.getsource(build_turn_trace)
    for token in ("memory_guard", "VerifiedFactStore", "EpisodicMemoryStore", "import episodic_memory"):
        assert token not in trace_source, f"build_turn_trace unexpectedly references {token}"


def test_no_evidence_ever_written_to_verified_fact_or_experience_ids():
    """`MemoryTurnTrace.selected_verified_fact_ids`/`selected_experience_ids`
    are read-only awareness fields - nothing in `luno/memory.py` ever
    accepts them as a mutation target (there is no
    `record_verified_fact_evidence()`/`record_experience_evidence()`
    function anywhere)."""
    assert not hasattr(memory, "record_verified_fact_evidence")
    assert not hasattr(memory, "record_experience_evidence")
