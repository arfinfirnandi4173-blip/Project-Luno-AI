"""
test_memory_adaptive_retrieval.py
==================================

MEMORY DECISION QUALITY & ADAPTIVE RETRIEVAL sprint - dedicated test
suite for this sprint's own additions: `classify_query_context_category()`,
the context-evidence schema (`context_evidence` counters,
`get_context_evidence_score()`, `get_memory_context_evidence()`,
`get_memory_context_specialization_summary()`, `list_context_specialized_memories()`),
`record_outcome_evidence(..., context_category=...)`, `ContextItem`'s
three new fields (`evaluation`/`context_evidence`/`usage_count`) and
`_rank_key()`'s extended tuple, and the `query_category`-threading added
to `relevant_memory_to_context_item()`/`_manual_memory_conflict_items()`/
`assemble_context()`.

Does NOT re-test `tests/test_memory_evaluation.py`'s own `evaluate_memory()`/
`calibrate_memory()` math (Sections B/C/D there), `tests/test_memory_learning.py`'s
own usefulness/feedback-loop coverage, `tests/test_memory_conflict.py`'s
own conflict-classification coverage, or `tests/test_memory_maintenance.py`'s
own lifecycle/usage-tracking coverage - all reused, unchanged, by this
sprint. `tests/test_memory_evaluation.py`'s own Section F now also
carries the two test conflicts resolved when this sprint resumed
(`test_context_item_evaluation_field_holds_the_shared_evaluate_memory_score`
/ `test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker`) -
this file focuses on the NEW mechanisms those tests don't cover:
context-specific evidence, query-category threading, and full
`assemble_context()` E2E scenarios (A-Q below, per the sprint spec).

MOST IMPORTANT RULE, restated: RELEVANCE FIRST. An irrelevant memory must
never become relevant merely because importance/usefulness/evaluation/
retrieval frequency/feedback is high. Adaptive signals may rank relevant
candidates; they must never manufacture relevance. Section A below proves
this under real budget pressure (not just via the tuple-position proof in
test_memory_evaluation.py), through the real `assemble_context()` path.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production `config/long_term_memory.json`.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest

import luno.memory as memory
import luno.memory_context as memory_context
from luno.memory_retrieval.models import MemoryRetrievalConfig, RelevantMemory


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None,
           usefulness_score=None, evaluation_score=None, context_evidence=None,
           retrieval_count=None):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    entry = {
        "id": id_ or f"e-{abs(hash((text, id_))) % 100000}",
        "text": text, "category": category, "importance": importance, "source": source,
        "created_at": ts, "updated_at": ts, "history": [],
    }
    if conflict_status:
        entry["conflict_status"] = conflict_status
    if conflict_group:
        entry["conflict_group"] = conflict_group
    if usefulness_score is not None:
        entry["usefulness_score"] = usefulness_score
    if evaluation_score is not None:
        entry["evaluation_score"] = evaluation_score
    if context_evidence is not None:
        entry["context_evidence"] = context_evidence
    if retrieval_count is not None:
        entry["retrieval_count"] = retrieval_count
    return entry


def _put(entry):
    memory._memories.append(entry)
    return entry


def _rm(entry, score, source="manual_memory", text=None, stale=False):
    return RelevantMemory(text=text or entry["text"], source=source, score=score, raw=entry, stale=stale)


def _tight_config(max_results=1, max_tokens=4000):
    cfg = MemoryRetrievalConfig.from_env()
    cfg.max_results = max_results
    cfg.max_tokens = max_tokens
    return cfg


class _StubRetriever:
    """Feeds a fixed candidate list straight in, bypassing the real
    `MemoryRetriever`/vision/episodic sources entirely - the same pattern
    `tests/test_memory_evaluation.py`'s own E2E-ish test already
    established."""

    def __init__(self, candidates):
        self._candidates = candidates

    def retrieve_memories(self, text):
        return list(self._candidates)


# ============================================================================
# A/B/C - relevance-first guarantee under real budget pressure: an
# irrelevant-but-high-signal candidate must lose to a relevant-but-
# low-signal one, through the REAL assemble_context() path (not just the
# tuple-position proof already in test_memory_evaluation.py).
# ============================================================================

def test_A_irrelevant_high_importance_memory_is_excluded_under_budget():
    high_importance_low_relevance = _put(_entry("resep nasi goreng ibu", importance=5, id_="cooking"))
    low_importance_high_relevance = _put(_entry("jadwal minum obat setiap jam 8 pagi", importance=1, id_="meds"))

    candidates = [
        _rm(high_importance_low_relevance, score=0.15),
        _rm(low_importance_high_relevance, score=0.95),
    ]
    result = memory_context.assemble_context(
        "kapan saya harus minum obat?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=1),
    )
    ids = [i.memory_id for i in result.items]
    assert ids == ["meds"], f"expected only the relevant item to survive budget, got {ids}"


def test_B_irrelevant_high_usefulness_memory_is_excluded_under_budget():
    high_usefulness_low_relevance = _put(_entry(
        "resep nasi goreng ibu", importance=2, usefulness_score=1.0, id_="cooking2",
    ))
    low_usefulness_high_relevance = _put(_entry(
        "jadwal minum obat setiap jam 8 pagi", importance=2, usefulness_score=0.0, id_="meds2",
    ))
    candidates = [
        _rm(high_usefulness_low_relevance, score=0.15),
        _rm(low_usefulness_high_relevance, score=0.95),
    ]
    result = memory_context.assemble_context(
        "kapan saya harus minum obat?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=1),
    )
    ids = [i.memory_id for i in result.items]
    assert ids == ["meds2"], f"expected only the relevant item to survive budget, got {ids}"


def test_C_irrelevant_high_evaluation_memory_is_excluded_under_budget():
    # Strong positive retrieval-success evidence -> high evaluate_memory() score.
    high_eval_low_relevance = _put(_entry(
        "resep nasi goreng ibu", importance=2, id_="cooking3",
    ))
    for _ in range(5):
        memory.record_outcome_evidence("cooking3", "positive")

    low_eval_high_relevance = _put(_entry(
        "jadwal minum obat setiap jam 8 pagi", importance=2, id_="meds3",
    ))

    candidates = [
        _rm(memory.get_memory("cooking3"), score=0.15),
        _rm(low_eval_high_relevance, score=0.95),
    ]
    result = memory_context.assemble_context(
        "kapan saya harus minum obat?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=1),
    )
    ids = [i.memory_id for i in result.items]
    assert ids == ["meds3"], f"expected only the relevant item to survive budget, got {ids}"


# ============================================================================
# D - relevant candidates CAN be ranked adaptively (context evidence breaks
# ties among items that already passed relevance/importance).
# ============================================================================

def test_D_relevant_candidates_ranked_by_context_evidence_when_otherwise_tied():
    reliable_in_medical = _put(_entry("minum air putih 2 liter sehari", importance=2, id_="water"))
    unproven_in_medical = _put(_entry("matikan lampu kalau keluar rumah", importance=2, id_="lights"))

    # Build up positive evidence for "water" specifically in the medical
    # ("kesehatan") category, and leave "lights" with no evidence at all.
    category = memory.classify_query_context_category("kapan saya harus minum obat dan cek kesehatan?")
    for _ in range(3):
        memory.record_outcome_evidence("water", "positive", context_category=category)

    candidates = [
        _rm(memory.get_memory("lights"), score=0.8),
        _rm(memory.get_memory("water"), score=0.8),
    ]
    result = memory_context.assemble_context(
        "kapan saya harus minum obat dan cek kesehatan?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    ids = [i.memory_id for i in result.items]
    assert ids.index("water") < ids.index("lights"), (
        f"expected the item with positive context-specific evidence to outrank the "
        f"equally-relevant, equally-important item with none, got order {ids}"
    )


# ============================================================================
# E - importance / evaluation / usefulness interaction follows the
# approved tier order (relevance > importance > context_evidence >
# usefulness > evaluation), exercised through real ContextItem instances.
# ============================================================================

def test_E_full_tier_order_importance_beats_context_evidence_beats_usefulness_beats_evaluation():
    base = dict(source="manual_memory", text="x", relevance=0.8)
    # Each item wins on exactly one tier and loses on every tier below it -
    # proves the tiers are consulted strictly in order, not blended/averaged.
    winner_by_importance = memory_context.ContextItem(
        memory_id="a", importance=5, context_evidence=0.0, usefulness=0.0, evaluation=0.0, **base,
    )
    loser_but_wins_lower_tiers = memory_context.ContextItem(
        memory_id="b", importance=1, context_evidence=1.0, usefulness=1.0, evaluation=1.0, **base,
    )
    assert winner_by_importance._rank_key() > loser_but_wins_lower_tiers._rank_key()

    winner_by_context_evidence = memory_context.ContextItem(
        memory_id="c", importance=2, context_evidence=1.0, usefulness=0.0, evaluation=0.0, **base,
    )
    loser_but_wins_usefulness_and_evaluation = memory_context.ContextItem(
        memory_id="d", importance=2, context_evidence=0.0, usefulness=1.0, evaluation=1.0, **base,
    )
    assert winner_by_context_evidence._rank_key() > loser_but_wins_usefulness_and_evaluation._rank_key()

    winner_by_usefulness = memory_context.ContextItem(
        memory_id="e", importance=2, context_evidence=0.5, usefulness=1.0, evaluation=0.0, **base,
    )
    loser_but_wins_evaluation = memory_context.ContextItem(
        memory_id="f", importance=2, context_evidence=0.5, usefulness=0.0, evaluation=1.0, **base,
    )
    assert winner_by_usefulness._rank_key() > loser_but_wins_evaluation._rank_key()


# ============================================================================
# F - lifecycle filtering remains intact (archived entries never surface,
# including via the conflict-group path this sprint threads query_category
# through).
# ============================================================================

def test_F_archived_conflict_entries_are_still_filtered_out():
    old_ts = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    a = _put({
        "id": "old-a", "text": "kucing peliharaan bernama Milo", "category": "other",
        "importance": 1, "source": "llm_auto", "created_at": old_ts, "updated_at": old_ts,
        "history": [], "conflict_status": "ambiguous_conflict", "conflict_group": "pet-name",
    })
    b = _put({
        "id": "old-b", "text": "kucing peliharaan bernama Coco", "category": "other",
        "importance": 1, "source": "llm_auto", "created_at": old_ts, "updated_at": old_ts,
        "history": [], "conflict_status": "ambiguous_conflict", "conflict_group": "pet-name",
    })
    assert memory.compute_lifecycle(a) == "archived"
    assert memory.compute_lifecycle(b) == "archived"

    result = memory_context.assemble_context(
        "siapa nama kucing saya?",
        memory_retriever=_StubRetriever([]),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    assert result.items == [], "archived ambiguous-conflict entries must never surface, even via the conflict path"


# ============================================================================
# G - ambiguous conflicts remain grouped into one hedged note (never
# arbitrated), with query_category threaded through without changing that
# guarantee.
# ============================================================================

def test_G_ambiguous_conflict_still_merged_into_one_hedged_note():
    _put(_entry("kucing saya bernama Milo", id_="cat-a", conflict_status="ambiguous_conflict",
                conflict_group="cat-name", days_ago=2))
    _put(_entry("kucing saya bernama Coco", id_="cat-b", conflict_status="ambiguous_conflict",
                conflict_group="cat-name", days_ago=1))

    result = memory_context.assemble_context(
        "siapa nama kucing saya?",
        memory_retriever=_StubRetriever([]),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    conflict_items = [i for i in result.items if i.memory_id and i.memory_id.startswith("conflict:")]
    assert len(conflict_items) == 1, f"expected exactly one merged conflict note, got {conflict_items}"
    assert "Milo" in conflict_items[0].text and "Coco" in conflict_items[0].text


# ============================================================================
# H - historical retrieval remains historical-query aware (query_category
# threading does not disturb the existing ", historical]" detection/
# section routing).
# ============================================================================

def test_H_historical_item_still_routed_to_historical_context_section():
    entry = _put(_entry("alamat lama sebelum pindah", id_="old-address"))
    candidates = [_rm(entry, score=0.9, text="alamat lama sebelum pindah [manual_memory, historical]")]
    result = memory_context.assemble_context(
        "dimana alamat lama saya sebelum pindah?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    assert any(i.historical for i in result.items)
    assert any(i.memory_id == "old-address" for i in result.sections["Historical Context"])


# ============================================================================
# I - Verified Facts remain isolated: never touched by importance/
# usefulness/evaluation/context_evidence/usage_count, even when threaded
# through query_category-aware assemble_context().
# ============================================================================

def test_I_verified_facts_carry_no_adaptive_signal_fields():
    class _StubFactStore:
        def all_facts(self):
            return [{"entity_id": "living_room_light", "value": "on"}]

    result = memory_context.assemble_context(
        "is the living room light on right now?",
        memory_retriever=_StubRetriever([]),
        verified_fact_store=_StubFactStore(),
        config=_tight_config(max_results=5),
    )
    fact_items = [i for i in result.items if i.source == "verified_facts"]
    assert len(fact_items) == 1
    item = fact_items[0]
    assert item.importance is None
    assert item.usefulness is None
    assert item.evaluation is None
    assert item.context_evidence is None
    assert item.usage_count is None


# ============================================================================
# J - Episodic Memory remains isolated: same guarantee as Verified Facts,
# proven via the generic relevant_memory_to_context_item() adapter path
# (episodic raw records have no "importance" key).
# ============================================================================

def test_J_episodic_memory_items_carry_no_adaptive_signal_fields():
    episodic_raw = {"id": "ep-1", "text": "user tersenyum saat membahas liburan", "timestamp": datetime.now()}
    candidates = [_rm(episodic_raw, score=0.9, source="episodic_memory",
                       text="user tersenyum saat membahas liburan")]
    result = memory_context.assemble_context(
        "bagaimana perasaan user soal liburan?",
        memory_retriever=_StubRetriever(candidates),
        config=_tight_config(max_results=5),
    )
    episodic_items = [i for i in result.items if i.source == "episodic_memory"]
    assert len(episodic_items) == 1
    item = episodic_items[0]
    assert item.importance is None
    assert item.usefulness is None
    assert item.evaluation is None
    assert item.context_evidence is None
    assert item.usage_count is None


# ============================================================================
# K - budget remains enforced (max_results AND max_tokens), unchanged by
# this sprint's additional ranking signals.
# ============================================================================

def test_K_budget_max_results_still_caps_item_count():
    entries = [_put(_entry(f"fakta nomor {i} tentang rumah", id_=f"fact-{i}")) for i in range(5)]
    candidates = [_rm(e, score=0.9 - i * 0.01) for i, e in enumerate(entries)]
    result = memory_context.assemble_context(
        "ceritakan tentang rumah saya",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=2, max_tokens=10_000),
    )
    assert len(result.items) == 2


def test_K_budget_max_tokens_still_caps_total_size():
    long_text = "x " * 2000  # ~2000+ estimated tokens on its own
    e1 = _put(_entry(long_text, id_="long"))
    e2 = _put(_entry("fakta pendek tentang rumah", id_="short"))
    candidates = [_rm(e1, score=0.95), _rm(e2, score=0.9)]
    result = memory_context.assemble_context(
        "ceritakan tentang rumah saya",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=10, max_tokens=50),
    )
    total_tokens = sum(memory_context._estimate_tokens(i.text) for i in result.items)
    assert total_tokens <= 50


# ============================================================================
# L/M - deterministic ranking: identical inputs, identical (repeated)
# outputs, no hidden randomness/unordered-collection dependence.
# ============================================================================

def test_L_M_repeated_identical_inputs_produce_identical_ranking():
    entries = [_put(_entry(f"catatan {i} tentang rumah", id_=f"note-{i}")) for i in range(6)]
    for idx, eid in enumerate(("note-1", "note-3")):
        memory.record_outcome_evidence(eid, "positive")

    def make_candidates():
        return [_rm(e, score=0.5 + (i % 3) * 0.1) for i, e in enumerate(entries)]

    results = []
    for _ in range(3):
        result = memory_context.assemble_context(
            "ceritakan tentang rumah saya",
            memory_retriever=_StubRetriever(make_candidates()),
            get_manual_memories=lambda: memory.list_memories(),
            config=_tight_config(max_results=10, max_tokens=10_000),
        )
        results.append([i.memory_id for i in result.items])

    assert results[0] == results[1] == results[2], f"ranking must be reproducible across identical calls: {results}"


# ============================================================================
# N - no retrieval-time mutation: assemble_context() never writes to any
# manual-memory entry (no evidence bump, no evaluation persisted, no text
# change), even though it reads evaluate_memory()/get_context_evidence_score().
# ============================================================================

def test_N_assemble_context_never_mutates_manual_memory_entries():
    entry = _put(_entry("jadwal olahraga setiap sore", id_="exercise"))
    before = copy.deepcopy(memory.list_memories())

    candidates = [_rm(entry, score=0.9)]
    memory_context.assemble_context(
        "kapan saya olahraga?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )

    after = memory.list_memories()
    assert after == before, "assemble_context() must be read-only - no mutation of any manual memory entry"


# ============================================================================
# O - no persistence drift from retrieval: assemble_context() never calls
# luno.memory._save() (or any other persistence write) at all.
# ============================================================================

def test_O_assemble_context_never_triggers_a_save(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("assemble_context() must never call memory._save() - it is read-only")

    monkeypatch.setattr(memory, "_save", _fail_if_called)

    entry = _put(_entry("nomor telepon dokter keluarga", id_="doctor-phone"))
    candidates = [_rm(entry, score=0.9)]
    memory_context.assemble_context(
        "berapa nomor telepon dokter?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    # If _save() had been called, the monkeypatched stub above would have
    # raised and failed this test already - reaching here is the proof.


# ============================================================================
# P - backward compatibility with existing callers: every new parameter is
# additive/optional; callers that don't know about query_category (or
# don't pass get_manual_memories/verified_fact_store at all) still work
# exactly as before this sprint.
# ============================================================================

def test_P_relevant_memory_to_context_item_without_query_category_still_works():
    entry = _put(_entry("makanan favorit adalah rendang", id_="food"))
    rm = _rm(entry, score=0.8)
    # Old call signature - no query_category argument at all.
    item = memory_context.relevant_memory_to_context_item(rm)
    assert item.memory_id == "food"
    assert item.context_evidence is None, "omitting query_category must behave exactly as pre-sprint (None)"
    assert item.evaluation is not None, "evaluation itself is unconditional - only context_evidence needs a category"


def test_P_assemble_context_without_manual_memories_or_verified_facts_still_works():
    result = memory_context.assemble_context(
        "apa kabar hari ini?",
        memory_retriever=_StubRetriever([]),
    )
    assert result.items == []
    assert result.sections["Relevant Memories"] == []


# ============================================================================
# Q - legacy behavior when adaptive signals are unavailable/defaulted: a
# pre-sprint entry with no context_evidence/evaluation_score keys at all
# ranks and renders exactly as it would have before this sprint (neutral
# defaults, no crash).
# ============================================================================

def test_Q_legacy_entry_with_no_adaptive_fields_gets_neutral_defaults():
    legacy_entry = {
        "id": "legacy-1", "text": "warna favorit adalah biru", "category": "other",
        "importance": 2, "source": "llm_auto",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"), "history": [],
        # deliberately no context_evidence / evaluation_score / usefulness_score keys
    }
    _put(legacy_entry)

    assert memory.get_context_evidence_score(legacy_entry, "other") == 0.5
    evaluation = memory.evaluate_memory(legacy_entry)
    assert 0.0 <= evaluation["score"] <= 1.0

    candidates = [_rm(legacy_entry, score=0.9)]
    result = memory_context.assemble_context(
        "apa warna favorit saya?",
        memory_retriever=_StubRetriever(candidates),
        get_manual_memories=lambda: memory.list_memories(),
        config=_tight_config(max_results=5),
    )
    assert any(i.memory_id == "legacy-1" for i in result.items), "a legacy entry must still surface and rank normally"
