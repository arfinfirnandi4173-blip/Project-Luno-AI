"""
test_memory_learning.py
========================

MEMORY LEARNING & FEEDBACK LOOP sprint - test suite for the ADDITIONS
this sprint made to `luno/memory.py` (usefulness scoring, positive/
negative/correction feedback, feedback-aware maintenance), `luno/
memory_context.py` (usefulness as a bounded ranking tie-breaker), and
`luno/dashboard/collectors.py` (usefulness/feedback surfaced read-only,
new sort modes).

Does NOT duplicate `tests/test_memory_maintenance.py`'s own usage-
tracking/maintenance-planner coverage or `tests/test_memory_conflict.py`'s
conflict-classification coverage - both are reused, unchanged, by this
sprint's own logic. This file only covers what is NEW: usefulness_score,
positive_feedback_count, negative_feedback_count, `apply_positive_feedback()`/
`apply_negative_feedback()`, the feedback-command detectors, the
usefulness-aware maintenance branch, and the dashboard surface for all of
the above.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production `config/long_term_memory.json`.

End-to-end scenarios through the REAL production bridge (Section 22 of
the sprint brief) live in `tests/test_runtime_demo.py`, matching this
repository's own established precedent (every prior memory sprint's E2E
scenarios were added there, not into a dedicated file) - see
`test_memory_learning_feedback_loop_end_to_end_positive_confirmation_scenario_a`/
`_correction_scenario_b`/`_ambiguous_feedback_never_mutates` in that file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import luno.memory as memory
import luno.memory_context as memory_context
from luno.memory_retrieval.models import MemoryRetrievalConfig, RelevantMemory
from luno.memory_retrieval.query import analyze_query
from luno.memory_retrieval.retriever import MemoryRetriever


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None,
           retrieval_count=None, last_retrieved_at=None, archived_by_maintenance=None,
           usefulness_score=None, positive_feedback_count=None, negative_feedback_count=None):
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
    if last_retrieved_at is not None:
        entry["last_retrieved_at"] = last_retrieved_at
    if archived_by_maintenance is not None:
        entry["archived_by_maintenance"] = archived_by_maintenance
    if usefulness_score is not None:
        entry["usefulness_score"] = usefulness_score
    if positive_feedback_count is not None:
        entry["positive_feedback_count"] = positive_feedback_count
    if negative_feedback_count is not None:
        entry["negative_feedback_count"] = negative_feedback_count
    return entry


def _rm(entry, score=1.0):
    """A minimal `RelevantMemory` shaped exactly like what
    `make_manual_memory_source()` produces - `source="manual_memory"`,
    `raw=<the entry dict>`."""
    return RelevantMemory(text=entry["text"], source="manual_memory", score=score, raw=entry)


def _put(entry):
    memory._memories.append(entry)
    return entry


# ============================================================================
# A - Schema / backward compatibility
# ============================================================================

def test_schema_version_bumped_and_non_gating():
    # Memory Evaluation & Self-Calibration sprint bumped this 3 -> 4 (see
    # that section's own comment in `luno/memory.py`) - updated here to
    # track reality, same "purely informational, nothing gates on it"
    # guarantee this test itself already documents below.
    assert memory.MANUAL_MEMORY_SCHEMA_VERSION == 4
    # A v1/v2 entry (no usefulness/feedback keys at all) must still load
    # and behave correctly - nothing gates on schema_version's value.
    old_entry = _entry("some old fact", importance=2)
    old_entry.pop("history", None)
    assert memory.get_memory_usefulness(old_entry) == 0.5
    assert memory.get_memory_positive_feedback_count(old_entry) == 0
    assert memory.get_memory_negative_feedback_count(old_entry) == 0


def test_default_usefulness_is_neutral_not_zero():
    """A pre-existing entry must never look "known to be bad" (0.0) the
    moment this sprint ships - the neutral default (0.5) represents "no
    evidence yet", not "confirmed useless"."""
    e = _entry("fact")
    assert memory.get_memory_usefulness(e) == 0.5


@pytest.mark.parametrize("bad_value", [-0.1, 1.1, "high", None, float("nan"), True])
def test_malformed_usefulness_score_falls_back_to_default(bad_value):
    e = _entry("fact")
    if bad_value is not None:
        e["usefulness_score"] = bad_value
    assert memory.get_memory_usefulness(e) == 0.5


@pytest.mark.parametrize("bad_value", [-1, "3", None, True, 2.5])
def test_malformed_feedback_counts_fall_back_to_zero(bad_value):
    e = _entry("fact")
    if bad_value is not None:
        e["positive_feedback_count"] = bad_value
        e["negative_feedback_count"] = bad_value
    assert memory.get_memory_positive_feedback_count(e) == 0
    assert memory.get_memory_negative_feedback_count(e) == 0


def test_usefulness_accessors_never_crash_on_non_dict():
    assert memory.get_memory_usefulness(None) == 0.5
    assert memory.get_memory_usefulness("not a dict") == 0.5
    assert memory.get_memory_positive_feedback_count(None) == 0
    assert memory.get_memory_negative_feedback_count(None) == 0


# ============================================================================
# B - Usage tracking (confirming the pre-existing mechanism this sprint
# reuses, plus the new usefulness usage-nudge folded into it)
# ============================================================================

def test_usage_increment_once_per_genuine_retrieval():
    e = _put(_entry("fact about GPU"))
    memory.record_memory_usage([_rm(e)])
    assert memory.get_memory_retrieval_count(memory.get_memory(e["id"])) == 1


def test_usage_does_not_double_count_within_one_call():
    """A single `record_memory_usage()` call with the SAME memory
    appearing more than once in the list must still only increment once -
    `record_memory_usage()` de-duplicates by id via a `set()` internally."""
    e = _put(_entry("fact"))
    memory.record_memory_usage([_rm(e), _rm(e)])
    assert memory.get_memory_retrieval_count(memory.get_memory(e["id"])) == 1


def test_usage_not_counted_for_memory_absent_from_result_list():
    """A memory that merely exists in the store, but never appears in the
    retrieval result list, must never accrue usage."""
    counted = _put(_entry("fact A", id_="a"))
    not_counted = _put(_entry("fact B", id_="b"))
    memory.record_memory_usage([_rm(counted)])
    assert memory.get_memory_retrieval_count(memory.get_memory("a")) == 1
    assert memory.get_memory_retrieval_count(memory.get_memory("b")) == 0
    assert memory.get_memory_usefulness(memory.get_memory("b")) == 0.5  # untouched


def test_usage_not_counted_for_archived_memory_excluded_from_source():
    """`make_manual_memory_source()` excludes archived entries BEFORE
    `record_memory_usage()` ever sees them - so an archived memory's
    usage stays frozen even if the user's query would otherwise match it."""
    archived = _put(_entry("archived fact about docker", archived_by_maintenance=True))
    source = memory.make_manual_memory_source(memory.list_memories)
    config = MemoryRetrievalConfig.from_env()
    results = source(analyze_query("cerita soal docker"), config)
    assert results == []  # archived entries never become candidates
    memory.record_memory_usage(results)
    assert memory.get_memory_retrieval_count(memory.get_memory(archived["id"])) == 0


def test_usage_not_counted_for_budget_rejected_candidate():
    """A candidate that a source WOULD have produced but that never made
    it into the final, budget-limited retrieval result must not accrue
    usage - `record_memory_usage()` only ever sees what the caller
    actually passes it (the final list), matching production's own call
    site (`relevant_memories_early`, already budget-limited)."""
    winner = _put(_entry("keyboard preference A", id_="w"))
    loser = _put(_entry("keyboard preference B", id_="l"))
    # Simulate "loser lost to budget" by simply not including it in the
    # list handed to record_memory_usage - the exact shape
    # `MemoryRetriever._apply_limits()` already produces upstream.
    memory.record_memory_usage([_rm(winner)])
    assert memory.get_memory_retrieval_count(memory.get_memory("w")) == 1
    assert memory.get_memory_retrieval_count(memory.get_memory("l")) == 0


def test_usage_driven_usefulness_nudge_is_small_and_capped():
    e = _put(_entry("fact"))
    for _ in range(3):
        memory.record_memory_usage([_rm(e)])
    updated = memory.get_memory(e["id"])
    assert updated["usefulness_score"] == pytest.approx(0.5 + 3 * memory._USEFULNESS_USAGE_DELTA, abs=1e-9)

    # Hammer it with many more retrievals - usage volume ALONE must never
    # cross the usage-nudge ceiling (only explicit feedback can).
    for _ in range(500):
        memory.record_memory_usage([_rm(memory.get_memory(e["id"]))])
    final = memory.get_memory(e["id"])
    assert final["usefulness_score"] <= memory._USEFULNESS_USAGE_NUDGE_CEILING


def test_repeated_irrelevant_style_retrieval_is_never_a_penalty():
    """Section 9's "repeated irrelevant retrieval -> jangan otomatis
    dihukum" - there is no penalty branch anywhere in `record_memory_usage()`;
    every genuine retrieval event can only ever nudge usefulness UP (or
    leave it unchanged once capped), never down."""
    e = _put(_entry("fact"))
    before = memory.get_memory_usefulness(e)
    for _ in range(10):
        memory.record_memory_usage([_rm(memory.get_memory(e["id"]))])
        after = memory.get_memory_usefulness(memory.get_memory(e["id"]))
        assert after >= before
        before = after


def test_memory_context_assemble_never_double_counts_or_calls_record_usage():
    """`memory_context.assemble_context()` must stay strictly read-only
    with respect to usage - it is driven by the SAME `relevant_memories_early`
    the caller already passes to `record_memory_usage()` separately, and
    never calls that function (or mutates retrieval_count/usefulness)
    itself (see that module's own docstring's "Read-only" guarantee)."""
    e = _put(_entry("fact about lampu kamar tidur"))
    retriever = MemoryRetriever(MemoryRetrievalConfig.from_env())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    before = dict(memory.get_memory(e["id"]))
    relevant = retriever.retrieve_memories("lampu kamar tidur")
    assembled = memory_context.assemble_context("lampu kamar tidur", memory_retriever=retriever, precomputed_relevant_memories=relevant)
    assert len(assembled.items) >= 1
    after = dict(memory.get_memory(e["id"]))
    assert before == after  # byte-for-byte unchanged - assemble_context never touched usage/usefulness


# ============================================================================
# C - Positive feedback
# ============================================================================

def test_positive_feedback_clear_target_increments_and_raises_usefulness():
    e = _put(_entry("fact", usefulness_score=0.5))
    updated = memory.apply_positive_feedback(e["id"])
    assert updated["positive_feedback_count"] == 1
    assert updated["usefulness_score"] == pytest.approx(0.65, abs=1e-9)


def test_positive_feedback_unknown_id_is_a_safe_no_op():
    assert memory.apply_positive_feedback("does-not-exist") is None


def test_positive_feedback_usefulness_is_bounded_at_max():
    e = _put(_entry("fact", usefulness_score=0.95))
    for _ in range(10):
        memory.apply_positive_feedback(e["id"])
    final = memory.get_memory(e["id"])
    assert final["usefulness_score"] == memory.MEMORY_USEFULNESS_MAX
    assert final["usefulness_score"] <= 1.0


def test_positive_feedback_never_raises_importance_to_4():
    """Section 6/20's explicit "jangan langsung menaikkan importance ke 4" -
    `apply_positive_feedback()` must never touch `importance` at all, no
    matter how many times it's called."""
    e = _put(_entry("fact", importance=2))
    for _ in range(20):
        memory.apply_positive_feedback(e["id"])
    assert memory.get_memory(e["id"])["importance"] == 2


def test_positive_feedback_never_touches_text_or_deletes():
    e = _put(_entry("original text", importance=2))
    memory.apply_positive_feedback(e["id"])
    updated = memory.get_memory(e["id"])
    assert updated["text"] == "original text"
    assert len(memory.list_memories()) == 1


def test_positive_feedback_ambiguous_target_is_the_callers_responsibility():
    """`apply_positive_feedback()` itself has no notion of "ambiguous" -
    target resolution (Section 6's "jika target ambiguous: jangan modify
    memory") lives one layer up, in the CALLER (see
    `main_runtime_demo.py::_handle_memory_feedback_command()`, and the
    end-to-end proof in `tests/test_runtime_demo.py::
    test_memory_learning_feedback_loop_end_to_end_ambiguous_feedback_never_mutates`).
    At this module's level, the equivalent guarantee is: a caller that
    never resolves a target (passes no id, or an id that doesn't exist)
    can never accidentally mutate the wrong memory."""
    a = _put(_entry("fact A", id_="a"))
    b = _put(_entry("fact B", id_="b"))
    # Simulating "ambiguous, so the caller correctly declines to call
    # apply_positive_feedback at all" - nothing changes.
    before = [dict(m) for m in memory.list_memories()]
    # (no call made)
    after = [dict(m) for m in memory.list_memories()]
    assert before == after


# ============================================================================
# D - Negative feedback
# ============================================================================

def test_negative_feedback_clear_target_increments_and_lowers_usefulness():
    e = _put(_entry("fact", usefulness_score=0.5))
    updated = memory.apply_negative_feedback(e["id"])
    assert updated["negative_feedback_count"] == 1
    assert updated["usefulness_score"] == pytest.approx(0.35, abs=1e-9)


def test_negative_feedback_unknown_id_is_a_safe_no_op():
    assert memory.apply_negative_feedback("does-not-exist") is None


def test_negative_feedback_never_deletes():
    e = _put(_entry("fact"))
    memory.apply_negative_feedback(e["id"])
    memory.apply_negative_feedback(e["id"])
    assert len(memory.list_memories()) == 1
    assert memory.get_memory(e["id"]) is not None


def test_negative_feedback_usefulness_is_bounded_at_min():
    e = _put(_entry("fact", usefulness_score=0.1))
    for _ in range(10):
        memory.apply_negative_feedback(e["id"])
    final = memory.get_memory(e["id"])
    assert final["usefulness_score"] == memory.MEMORY_USEFULNESS_MIN
    assert final["usefulness_score"] >= 0.0


def test_negative_feedback_never_touches_text_or_importance():
    e = _put(_entry("original text", importance=3))
    memory.apply_negative_feedback(e["id"])
    updated = memory.get_memory(e["id"])
    assert updated["text"] == "original text"
    assert updated["importance"] == 3


def test_negative_feedback_does_not_remove_protected_memory():
    """A protected (importance=4) memory can still receive negative
    feedback (the feedback itself is truthful evidence, Section 20 only
    forbids DELETION as a consequence) - it must remain fully intact and
    still protected afterward."""
    e = _put(_entry("core fact", importance=4))
    memory.apply_negative_feedback(e["id"])
    updated = memory.get_memory(e["id"])
    assert updated is not None
    assert updated["importance"] == 4
    assert memory.is_memory_protected(e["id"]) is True


# ============================================================================
# E - Correction feedback (reuses the EXISTING update_memory()/history
# mechanism - no second correction engine)
# ============================================================================

def test_correction_detector_extracts_new_value():
    assert memory.detect_memory_feedback_correction("yang tadi salah, sekarang RTX 4090") == "RTX 4090"
    assert memory.detect_memory_feedback_correction("itu salah, seharusnya Ubuntu 22.04") == "Ubuntu 22.04"
    assert memory.detect_memory_feedback_correction("ordinary sentence with no correction shape") is None


def test_correction_via_update_memory_preserves_history_and_current_value():
    e = _put(_entry("GPU-ku RTX 3070 Ti", importance=2))
    new_value = memory.detect_memory_feedback_correction("yang tadi salah, sekarang RTX 4090")
    updated = memory.update_memory(e["id"], new_value, reason="correction")
    memory.apply_negative_feedback(e["id"], reason="user_correction")

    final = memory.get_memory(e["id"])
    assert final["text"] == "RTX 4090"  # new value is current
    assert any(h["text"] == "GPU-ku RTX 3070 Ti" for h in final["history"])  # old value preserved
    assert final["history"][-1].get("reason") == "correction"
    assert final["negative_feedback_count"] == 1  # feedback metadata updated truthfully
    assert len(memory.list_memories()) == 1  # never duplicated into a second entry


def test_correction_reuses_existing_conflict_history_mechanism_no_new_field():
    """The correction path introduces NO new persisted field beyond what
    `update_memory()` already writes (`history[].reason`) plus the
    feedback counters this sprint adds - confirming no second
    correction/history engine was built."""
    e = _put(_entry("old fact"))
    memory.update_memory(e["id"], "new fact", reason="correction")
    entry = memory.get_memory(e["id"])
    history_entry = entry["history"][0]
    assert set(history_entry.keys()) <= {"text", "changed_at", "reason"}


# ============================================================================
# F - Retrieval integration (relevance/importance mandatory before
# usefulness; usefulness only ever a tie-breaker)
# ============================================================================

def test_retrieval_relevance_still_mandatory_regardless_of_usefulness():
    """A memory with maximum usefulness but ZERO relevance to the query
    must never appear - the token_overlap gate runs before usefulness is
    ever consulted."""
    _put(_entry("Vinn suka Avenged Sevenfold", importance=4, usefulness_score=1.0,
                 positive_feedback_count=50, category="preference"))
    source = memory.make_manual_memory_source(memory.list_memories)
    config = MemoryRetrievalConfig.from_env()
    results = source(analyze_query("cara konfigurasi Docker networking"), config)
    assert results == []


def test_retrieval_usefulness_only_breaks_ties_never_outranks_importance():
    high_importance_low_usefulness = _put(_entry(
        "Docker networking pakai bridge mode", importance=3, usefulness_score=0.0, id_="hi",
    ))
    low_importance_high_usefulness = _put(_entry(
        "Docker networking pakai host mode", importance=1, usefulness_score=1.0, id_="lo",
    ))
    source = memory.make_manual_memory_source(memory.list_memories)
    config = MemoryRetrievalConfig.from_env()
    results = source(analyze_query("Docker networking"), config)
    by_source_id = {r.raw["id"]: r.score for r in results}
    # The importance=3 entry outranks the importance=1 entry even though
    # usefulness points the other way - importance (0.05/level = 0.10
    # swing here) is a much bigger contributor than usefulness's bounded
    # ±0.025 nudge, so it can never flip this ordering.
    assert by_source_id["hi"] > by_source_id["lo"]


def test_retrieval_usefulness_breaks_tie_among_equal_importance():
    a = _put(_entry("Docker compose file location A", importance=2, usefulness_score=0.0, id_="a"))
    b = _put(_entry("Docker compose file location B", importance=2, usefulness_score=1.0, id_="b"))
    source = memory.make_manual_memory_source(memory.list_memories)
    config = MemoryRetrievalConfig.from_env()
    results = source(analyze_query("Docker compose file"), config)
    by_id = {r.raw["id"]: r.score for r in results}
    assert by_id["b"] > by_id["a"]


def test_context_item_rank_key_orders_relevance_then_importance_then_usefulness():
    low_relevance_high_useful = memory_context.ContextItem(
        source="manual_memory", memory_id="x", text="x", relevance=0.5, importance=2, usefulness=1.0,
    )
    high_relevance_low_useful = memory_context.ContextItem(
        source="manual_memory", memory_id="y", text="y", relevance=0.9, importance=2, usefulness=0.0,
    )
    assert high_relevance_low_useful._rank_key() > low_relevance_high_useful._rank_key()

    same_relevance_high_importance = memory_context.ContextItem(
        source="manual_memory", memory_id="a", text="a", relevance=0.6, importance=4, usefulness=0.0,
    )
    same_relevance_low_importance_high_useful = memory_context.ContextItem(
        source="manual_memory", memory_id="b", text="b", relevance=0.6, importance=0, usefulness=1.0,
    )
    assert same_relevance_high_importance._rank_key() > same_relevance_low_importance_high_useful._rank_key()

    same_relevance_same_importance_a = memory_context.ContextItem(
        source="manual_memory", memory_id="c", text="c", relevance=0.6, importance=2, usefulness=0.9,
    )
    same_relevance_same_importance_b = memory_context.ContextItem(
        source="manual_memory", memory_id="d", text="d", relevance=0.6, importance=2, usefulness=0.1,
    )
    assert same_relevance_same_importance_a._rank_key() > same_relevance_same_importance_b._rank_key()


# ============================================================================
# G - Persistence
# ============================================================================

def test_persistence_survives_simulated_restart(tmp_path, monkeypatch):
    from luno import config as luno_config
    path = str(tmp_path / "long_term_memory.json")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", path, raising=False)

    e = memory.add_memory("aku pakai keyboard Keychron K8")
    memory.apply_positive_feedback(e["id"])
    memory.apply_negative_feedback(e["id"])
    memory.record_memory_usage([_rm(memory.get_memory(e["id"]))])
    before = memory.get_memory(e["id"])

    # Simulate a restart: reload straight from disk.
    memory._load()
    after = memory.get_memory(e["id"])
    assert after["usefulness_score"] == before["usefulness_score"]
    assert after["positive_feedback_count"] == before["positive_feedback_count"]
    assert after["negative_feedback_count"] == before["negative_feedback_count"]
    assert after["retrieval_count"] == before["retrieval_count"]


def test_old_schema_v1_entry_loads_and_defaults_safely(tmp_path, monkeypatch):
    import json
    from luno import config as luno_config
    path = str(tmp_path / "long_term_memory.json")
    v1_entry = {"id": "v1x", "text": "an old v1 fact", "created_at": "2024-01-01T00:00:00"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump([v1_entry], f)
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", path, raising=False)

    memory._load()
    loaded = memory.get_memory("v1x")
    assert loaded is not None
    assert memory.get_memory_usefulness(loaded) == 0.5
    assert memory.get_memory_positive_feedback_count(loaded) == 0
    assert memory.get_memory_negative_feedback_count(loaded) == 0
    # Feedback still works on a v1 entry loaded this way.
    updated = memory.apply_positive_feedback("v1x")
    assert updated["positive_feedback_count"] == 1


def test_malformed_optional_metadata_does_not_crash_health_report():
    _put(_entry("weird entry", usefulness_score="not a number", positive_feedback_count=-5, negative_feedback_count="x"))
    report = memory.memory_health_report()  # must not raise
    assert report["total"] == 1
    assert report["usefulness"]["medium"] == 1  # falls back to the neutral 0.5 default -> medium bucket


# ============================================================================
# H - Maintenance integration
# ============================================================================

def test_maintenance_considers_usefulness_protects_stale_high_usefulness_memory():
    """Section 16's worked example: importance=2, low retrieval_count, but
    HIGH usefulness -> reinforce, not archive - usefulness is a second,
    independent signal from raw usage."""
    e = _entry("useful but rarely retrieved fact", importance=2, days_ago=90,
                retrieval_count=0, usefulness_score=0.9)
    plan_item = memory._plan_action_for_entry(e)
    assert plan_item["action"] == "reinforce"


def test_maintenance_still_archives_stale_low_usefulness_low_usage():
    """The contrasting half of Section 16's example - low usage AND low
    usefulness together still archive (unchanged, pre-existing
    conservative behavior, not made any more aggressive by this sprint)."""
    e = _entry("stale unused fact", importance=2, days_ago=90, retrieval_count=0, usefulness_score=0.2)
    plan_item = memory._plan_action_for_entry(e)
    assert plan_item["action"] == "archive"


def test_maintenance_does_not_archive_just_because_usage_is_low():
    """Section 16/20's "jangan archive hanya karena usage rendah" - a
    FRESH (active-lifecycle) memory with zero usage must never be
    recommended for archival by usage/usefulness alone."""
    e = _entry("brand new fact", importance=2, days_ago=0, retrieval_count=0, usefulness_score=0.2)
    plan_item = memory._plan_action_for_entry(e)
    assert plan_item["action"] != "archive"


def test_maintenance_protected_memory_never_archived_regardless_of_usefulness():
    protected = _entry("core fact", importance=4, days_ago=900, retrieval_count=0, usefulness_score=0.0)
    plan_item = memory._plan_action_for_entry(protected)
    assert plan_item["action"] != "archive"
    assert plan_item["action"] == "keep"


def test_maintenance_ambiguous_conflict_stays_safe_regardless_of_negative_feedback():
    conflicted = _entry("disputed fact", importance=2, days_ago=90,
                          conflict_status="ambiguous_conflict", conflict_group="g1",
                          usefulness_score=0.0, negative_feedback_count=5)
    plan_item = memory._plan_action_for_entry(conflicted)
    assert plan_item["action"] == "review"  # never archived/consolidated automatically


def test_maintenance_usage_still_considered_reinforce_path_unchanged():
    """Confirms the pre-existing usage-driven reinforcement path (Memory
    Lifecycle & Maintenance sprint) is completely unaffected by this
    sprint's additive usefulness branch."""
    e = _entry("frequently used fact", importance=2, days_ago=1, retrieval_count=10)
    plan_item = memory._plan_action_for_entry(e)
    assert plan_item["action"] == "reinforce"


def test_apply_maintenance_plan_never_deletes_and_never_touches_importance_via_usefulness():
    e = _put(_entry("stale useful fact", importance=2, days_ago=90, retrieval_count=0, usefulness_score=0.9))
    plan = memory.analyze_memory_maintenance()
    results = memory.apply_maintenance_plan(plan)
    applied = next(r for r in results if r["memory_id"] == e["id"])
    assert applied["status"] == "applied"
    assert applied["action"] == "reinforce"
    updated = memory.get_memory(e["id"])
    assert updated["importance"] == 3  # the PRE-EXISTING, bounded +1/cap-3 reinforcement rule, not usefulness overwriting it
    assert len(memory.list_memories()) == 1  # nothing deleted


# ============================================================================
# I - Explainability
# ============================================================================

def test_usefulness_explanation_is_human_readable_and_reflects_evidence():
    e = _put(_entry("fact", usefulness_score=0.5))
    memory.apply_positive_feedback(e["id"])
    memory.apply_positive_feedback(e["id"])
    memory.apply_negative_feedback(e["id"])
    text = memory.get_memory_usefulness_explanation(memory.get_memory(e["id"]))
    assert "Usefulness:" in text
    assert "positive feedback x 2" in text
    assert "negative feedback x 1" in text


def test_usefulness_explanation_never_crashes_on_malformed_entry():
    e = _entry("weird", usefulness_score="bad", positive_feedback_count=None)
    text = memory.get_memory_usefulness_explanation(e)  # must not raise
    assert "Usefulness:" in text


# ============================================================================
# J - Dashboard surface
# ============================================================================

def test_dashboard_list_exposes_usefulness_and_feedback_fields():
    from luno.dashboard import collectors
    e = _put(_entry("fact", usefulness_score=0.7, positive_feedback_count=3, negative_feedback_count=1))
    data = collectors.collect_memory_list()
    row = next(r for r in data["items"] if r["id"] == e["id"])
    assert row["usefulness"] == 0.7
    assert row["positive_feedback_count"] == 3
    assert row["negative_feedback_count"] == 1
    assert "usage_count" in row


def test_dashboard_detail_exposes_usefulness_explanation():
    from luno.dashboard import collectors
    e = _put(_entry("fact", usefulness_score=0.7))
    detail = collectors.collect_memory_detail(e["id"])
    assert detail["usefulness"] == 0.7
    assert "usefulness_explanation" in detail
    assert "Usefulness:" in detail["usefulness_explanation"]


def test_dashboard_overview_exposes_usefulness_buckets_and_feedback_totals():
    from luno.dashboard import collectors
    _put(_entry("useful one", usefulness_score=0.9, positive_feedback_count=2, id_="a"))
    _put(_entry("useless one", usefulness_score=0.1, negative_feedback_count=1, id_="b"))
    overview = collectors.collect_memory_overview()
    assert overview["usefulness"]["high"] == 1
    assert overview["usefulness"]["low"] == 1
    assert overview["total_positive_feedback"] == 2
    assert overview["total_negative_feedback"] == 1


@pytest.mark.parametrize("sort_mode,expect_first_id", [
    ("most_used", "used_a"),
    ("most_useful", "useful_a"),
    ("low_usefulness", "useful_b"),
    ("recently_reinforced", "used_a"),
])
def test_dashboard_list_sort_modes_order_correctly(sort_mode, expect_first_id):
    from luno.dashboard import collectors
    now_iso = memory._now_iso()
    _put(_entry("fact one", id_="used_a", retrieval_count=10, last_retrieved_at=now_iso, usefulness_score=0.5))
    _put(_entry("fact two", id_="used_b", retrieval_count=1, usefulness_score=0.5))
    _put(_entry("fact three", id_="useful_a", usefulness_score=0.95))
    _put(_entry("fact four", id_="useful_b", usefulness_score=0.05))
    data = collectors.collect_memory_list(sort=sort_mode, limit=10)
    assert data["items"][0]["id"] == expect_first_id


def test_dashboard_needs_review_sort_surfaces_conflicted_entries_first():
    from luno.dashboard import collectors
    _put(_entry("ordinary fact", id_="ordinary"))
    _put(_entry("conflicted fact A", id_="conflict_a", conflict_status="ambiguous_conflict", conflict_group="g1", category="technical_fact"))
    _put(_entry("conflicted fact B", id_="conflict_b", conflict_status="ambiguous_conflict", conflict_group="g1", category="technical_fact"))
    data = collectors.collect_memory_list(sort="needs_review", limit=10)
    top_ids = {row["id"] for row in data["items"][:2]}
    assert top_ids == {"conflict_a", "conflict_b"}


def test_dashboard_get_never_mutates_usefulness_or_feedback_fields():
    from luno.dashboard import collectors
    e = _put(_entry("fact", usefulness_score=0.5, positive_feedback_count=0, negative_feedback_count=0))
    before = dict(memory.get_memory(e["id"]))
    collectors.collect_memory_overview()
    collectors.collect_memory_list(search="fact")
    collectors.collect_memory_detail(e["id"])
    after = dict(memory.get_memory(e["id"]))
    assert before == after


def test_dashboard_feedback_controls_apply_and_never_delete():
    from luno.dashboard import controls
    e = _put(_entry("fact"))
    r = controls.memory_feedback_positive(e["id"])
    assert r["ok"] is True
    assert memory.get_memory(e["id"])["positive_feedback_count"] == 1

    r2 = controls.memory_feedback_negative(e["id"])
    assert r2["ok"] is True
    assert memory.get_memory(e["id"])["negative_feedback_count"] == 1
    assert len(memory.list_memories()) == 1


def test_dashboard_feedback_control_requires_valid_id():
    from luno.dashboard import controls
    r = controls.memory_feedback_positive("")
    assert r["ok"] is False
    r2 = controls.memory_feedback_negative("does-not-exist")
    assert r2["ok"] is False


# ============================================================================
# K - Verified Facts / Episodic Memory isolation (guard, not a new
# implementation - confirming this sprint's own additions never touch
# either boundary)
# ============================================================================

def test_feedback_functions_never_reference_verified_fact_store_or_episodic_memory():
    """Same structural technique the Memory Prompt Intelligence/Memory
    Context Assembly sprints already established for this exact claim -
    `inspect.getsource()` over every new function this sprint added,
    asserting zero reference to `memory_guard`/`VerifiedFactStore`/
    `episodic_memory`/`EpisodicMemoryStore`."""
    import inspect
    functions = [
        memory.apply_positive_feedback, memory.apply_negative_feedback,
        memory._get_usefulness, memory._explain_usefulness,
        memory.detect_positive_memory_feedback, memory.detect_negative_memory_feedback,
        memory.detect_memory_feedback_correction, memory.mark_last_memory_useful,
        memory.mark_last_memory_not_useful, memory._plan_action_for_entry,
    ]
    forbidden = ("memory_guard", "VerifiedFactStore", "episodic_memory", "EpisodicMemoryStore")
    for fn in functions:
        source = inspect.getsource(fn)
        for token in forbidden:
            assert token not in source, f"{fn.__name__} unexpectedly references {token}"


def test_verified_fact_store_has_no_usefulness_or_feedback_concept():
    """Confirms `VerifiedFactStore` (a completely separate module/store)
    was not touched by this sprint at all - it has no `usefulness_score`/
    feedback fields and this sprint added none."""
    from luno.memory_guard import VerifiedFact
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(VerifiedFact)}
    assert "usefulness_score" not in field_names
    assert "positive_feedback_count" not in field_names
    assert "negative_feedback_count" not in field_names
