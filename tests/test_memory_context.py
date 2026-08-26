"""
test_memory_context.py
=======================

MEMORY CONTEXT ASSEMBLY & RETRIEVAL UNIFICATION sprint - test suite for
`luno/memory_context.py`, the new read-only selection layer that unifies
Luno's existing memory/context sources into one deterministic, bounded,
conflict-safe payload for the LLM.

Scope: this file tests ONLY the NEW `luno.memory_context` module's own
selection/dedup/budget/grouping logic. It does NOT re-test relevance
matching (`luno.memory_retrieval.query`), importance classification
(`luno.memory._classify_memory_importance`), lifecycle thresholds
(`luno.memory.compute_lifecycle`), or conflict classification
(`luno.memory._classify_conflict`) themselves - those are already covered
by `tests/test_memory_intelligence.py`/`tests/test_memory_conflict.py`/
`tests/test_memory_prompt_intelligence.py` and unchanged by this sprint
(this module only ever CALLS those existing functions, never reimplements
them).

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects every writer-capable persistent-state file (including
`LONG_TERM_MEMORY_FILE`/`VERIFIED_FACTS_FILE`) to an isolated temp path and
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import luno.memory as memory
import luno.memory_context as mc
from luno.episodic_memory import (
    EpisodicExperience,
    ExperienceCategory,
    make_episodic_experience_source,
)
from luno.memory_guard import VerifiedFactStore
from luno.memory_retrieval import MemoryRetriever, MemoryRetrievalConfig


# ─────────────────────────────────────────────
# Shared fixtures/helpers
# ─────────────────────────────────────────────

def _entry(text, importance=2, days_ago=0, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None):
    """Directly-constructed memory entry (bypassing `add_memory()`) so
    tests can pin exact importance/age/conflict/history fields - mirrors
    `tests/test_memory_prompt_intelligence.py`'s own `_entry()` helper."""
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    entry = {
        "id": id_ or f"e-{abs(hash(text)) % 100000}",
        "text": text,
        "category": category,
        "importance": importance,
        "source": source,
        "created_at": ts,
        "updated_at": ts,
        "history": history or [],
    }
    if conflict_status:
        entry["conflict_status"] = conflict_status
    if conflict_group:
        entry["conflict_group"] = conflict_group
    return entry


def _retriever_with_manual_memory():
    retriever = MemoryRetriever(MemoryRetrievalConfig.from_env())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    return retriever


def _assemble(text, retriever=None, verified_fact_store=None):
    retriever = retriever or _retriever_with_manual_memory()
    return mc.assemble_context(
        text,
        memory_retriever=retriever,
        get_manual_memories=memory.list_memories,
        verified_fact_store=verified_fact_store,
    )


def _fact_store(tmp_path, name="vf.json"):
    return VerifiedFactStore(path=str(tmp_path / name))


def _verified_ok(entity_id, actual_state):
    return {"success": True, "data": {"entity_id": entity_id, "actual_state": actual_state}}


# ─────────────────────────────────────────────
# BASIC
# ─────────────────────────────────────────────

def test_basic_no_relevant_memory_yields_empty_context():
    memory._memories.append(_entry("aku suka kopi hitam"))
    ctx = _assemble("cara masak nasi goreng enak")
    assert ctx.items == []
    assert ctx.render() == ""


def test_basic_one_relevant_memory_is_included():
    memory._memories.append(_entry("aku suka kopi hitam"))
    ctx = _assemble("kopi favoritku apa?")
    assert len(ctx.items) == 1
    assert "kopi hitam" in ctx.items[0].text
    assert "[Relevant Memories]" in ctx.render()


def test_basic_multiple_relevant_memories_are_ranked():
    memory._memories.append(_entry("aku suka main gitar", importance=1, id_="a"))
    memory._memories.append(_entry("aku suka main gitar listrik banget", importance=4, id_="b"))
    ctx = _assemble("cerita soal gitar dong")
    assert len(ctx.items) == 2
    # Higher-ranked item (importance=4, same relevance band) comes first.
    assert ctx.items[0]._rank_key() >= ctx.items[1]._rank_key()


def test_basic_irrelevant_memory_excluded():
    memory._memories.append(_entry("aku suka kopi hitam"))
    memory._memories.append(_entry("aku pakai RTX 3060 Ti"))
    ctx = _assemble("kopi favoritku apa?")
    assert len(ctx.items) == 1
    assert "kopi" in ctx.items[0].text.lower()


# ─────────────────────────────────────────────
# IMPORTANCE - a ranking signal, never a relevance override
# ─────────────────────────────────────────────

def test_importance_relevant_high_importance_ranks_above_relevant_low_importance():
    memory._memories.append(_entry("aku suka kucing", importance=0, id_="low"))
    memory._memories.append(_entry("aku sangat suka kucing peliharaan", importance=4, id_="high"))
    ctx = _assemble("cerita soal kucing")
    assert len(ctx.items) == 2
    assert ctx.items[0].memory_id == "high"


def test_importance_4_irrelevant_memory_never_enters_candidate_pool():
    """The sprint's hard guarantee: importance=4 can never rescue an
    irrelevant memory - it must never even become a candidate."""
    memory._memories.append(_entry("Luno adalah AI companion utama Vinn", importance=4))
    ctx = _assemble("cara masak nasi goreng enak")
    assert ctx.items == []


# ─────────────────────────────────────────────
# LIFECYCLE - reused from luno.memory.compute_lifecycle, not reimplemented
# ─────────────────────────────────────────────

def test_lifecycle_active_memory_included():
    memory._memories.append(_entry("aku suka kopi hitam", importance=2, days_ago=1))
    ctx = _assemble("kopi favoritku apa?")
    assert len(ctx.items) == 1
    assert ctx.items[0].lifecycle == "active"


def test_lifecycle_stale_memory_included_when_relevant():
    # importance=2 -> (active_days=60, stale_days=180); 100 days -> stale.
    entry = _entry("aku suka kopi hitam", importance=2, days_ago=100)
    memory._memories.append(entry)
    assert memory.compute_lifecycle(entry) == "stale"
    ctx = _assemble("kopi favoritku apa?")
    assert len(ctx.items) == 1
    assert ctx.items[0].lifecycle == "stale"


def test_lifecycle_archived_memory_excluded_even_if_relevant():
    # importance=0 -> (active_days=3, stale_days=14); 100 days -> archived.
    entry = _entry("aku suka kopi hitam", importance=0, days_ago=100)
    memory._memories.append(entry)
    assert memory.compute_lifecycle(entry) == "archived"
    ctx = _assemble("kopi favoritku apa?")
    assert ctx.items == []


# ─────────────────────────────────────────────
# SOURCES
# ─────────────────────────────────────────────

def test_sources_manual_memory_item_has_correct_provenance():
    memory._memories.append(_entry("aku suka kopi hitam"))
    ctx = _assemble("kopi favoritku apa?")
    assert ctx.items[0].source == "manual_memory"
    assert ctx.items[0].provenance == "manual_memory"


def test_sources_episodic_memory_item_included_in_own_section():
    exp = EpisodicExperience(
        experience_id="fp1", timestamp=None,
        category=ExperienceCategory.TECHNICAL_PROBLEM_SOLVED.value,
        summary="akhirnya masalah docker kelar juga", source="conversation",
    )
    retriever = _retriever_with_manual_memory()
    retriever.register_source("episodic_memory", make_episodic_experience_source(lambda: [exp]))
    ctx = _assemble("kemarin kita benerin docker apa ya?", retriever=retriever)
    assert len(ctx.items) == 1
    assert ctx.items[0].source == "episodic_memory"
    assert "[Relevant Experiences]" in ctx.render()


def test_sources_verified_fact_included_when_relevant(tmp_path):
    store = _fact_store(tmp_path)
    store.record(_verified_ok("living_room_light", "on"))
    ctx = _assemble("living room light gimana?", verified_fact_store=store)
    assert len(ctx.items) == 1
    assert ctx.items[0].source == "verified_facts"
    assert "[Verified Facts]" in ctx.render()


def test_sources_verified_fact_irrelevant_is_excluded(tmp_path):
    store = _fact_store(tmp_path)
    store.record(_verified_ok("living_room_light", "on"))
    ctx = _assemble("cara masak nasi goreng enak", verified_fact_store=store)
    assert ctx.items == []


# ─────────────────────────────────────────────
# DEDUP (Step 9) - transient-only, never mutates the underlying store
# ─────────────────────────────────────────────

def test_dedup_exact_normalized_text_collapses_to_one():
    a = mc.ContextItem(source="manual_memory", memory_id="a", text="Cup is on the desk.", relevance=0.6)
    b = mc.ContextItem(source="episodic_memory", memory_id="b", text="cup is on the desk", relevance=0.5)
    out = mc.deduplicate_context_items([a, b])
    assert len(out) == 1
    assert out[0].relevance == 0.6  # the higher-ranked survivor


def test_dedup_same_memory_id_same_source_collapses_to_one():
    a = mc.ContextItem(source="manual_memory", memory_id="x1", text="User likes tea.", relevance=0.6)
    b = mc.ContextItem(source="manual_memory", memory_id="x1", text="User likes tea a lot.", relevance=0.7)
    out = mc.deduplicate_context_items([a, b])
    assert len(out) == 1
    assert out[0].relevance == 0.7


def test_dedup_current_and_historical_same_memory_id_are_not_collapsed():
    """Regression guard: a current rendering and a historical (superseded)
    rendering of the SAME underlying manual-memory record must both
    survive dedup - collapsing them would silently present an old value
    as current, or lose the current value entirely (Step 12's hard rule).
    This is exactly the class of bug caught during this sprint's own
    end-to-end testing (see docs/change_impact/memory_context_assembly.md
    section 3.1's own account)."""
    current = mc.ContextItem(source="manual_memory", memory_id="g1", text="Current: RTX 3060 Ti.",
                              relevance=1.0, historical=False)
    historical = mc.ContextItem(source="manual_memory", memory_id="g1", text="Historical: RTX 3070 Ti.",
                                 relevance=0.7, historical=True)
    out = mc.deduplicate_context_items([current, historical])
    assert len(out) == 2


def test_dedup_strong_token_similarity_collapses_near_duplicates():
    a = mc.ContextItem(source="manual_memory", memory_id="a", text="User likes drinking black coffee every morning.", relevance=0.6)
    b = mc.ContextItem(source="episodic_memory", memory_id="b", text="User likes drinking black coffee every single morning.", relevance=0.5)
    out = mc.deduplicate_context_items([a, b])
    assert len(out) == 1


def test_dedup_genuinely_distinct_items_both_survive():
    a = mc.ContextItem(source="manual_memory", memory_id="a", text="User likes guitar.", relevance=0.6)
    b = mc.ContextItem(source="manual_memory", memory_id="b", text="User likes video games.", relevance=0.6)
    out = mc.deduplicate_context_items([a, b])
    assert len(out) == 2


# ─────────────────────────────────────────────
# CONFLICT (Step 11) - both sides represented together, never arbitrated
# ─────────────────────────────────────────────

def test_conflict_non_conflicting_entries_appear_normally():
    memory._memories.append(_entry("aku suka gitar", category="preference"))
    memory._memories.append(_entry("aku suka game", category="preference"))
    ctx = _assemble("apa yang aku suka?")
    assert len(ctx.items) == 2
    assert not any(i.conflict_group for i in ctx.items)


def test_conflict_ambiguous_relevant_group_produces_one_hedged_note():
    memory._memories.append(_entry(
        "OS ku Windows", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os1",
    ))
    memory._memories.append(_entry(
        "OS ku Ubuntu", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os2",
    ))
    ctx = _assemble("OS ku sekarang apa?")
    conflict_items = [i for i in ctx.items if i.conflict_group]
    assert len(conflict_items) == 1
    assert "Windows" in conflict_items[0].text
    assert "Ubuntu" in conflict_items[0].text
    assert "conflicting" in conflict_items[0].text.lower()


def test_conflict_irrelevant_group_omitted():
    memory._memories.append(_entry(
        "OS ku Windows", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os1",
    ))
    memory._memories.append(_entry(
        "OS ku Ubuntu", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os2",
    ))
    ctx = _assemble("cara masak nasi goreng enak")
    assert ctx.items == []


# ─────────────────────────────────────────────
# HISTORICAL (Step 12)
# ─────────────────────────────────────────────

def test_historical_current_query_excludes_history():
    entry = _entry(
        "aku sekarang pakai RTX 3060 Ti", category="technical_fact", id_="gpu1",
        history=[{"text": "aku pakai RTX 3070 Ti", "changed_at": datetime.now().isoformat(timespec="seconds")}],
    )
    memory._memories.append(entry)
    ctx = _assemble("RTX ku sekarang apa?")
    assert not any(i.historical for i in ctx.items)
    assert "[Historical Context]" not in ctx.render()


def test_historical_query_includes_labeled_history():
    entry = _entry(
        "aku sekarang pakai RTX 3060 Ti", category="technical_fact", id_="gpu1",
        history=[{"text": "aku pakai RTX 3070 Ti", "changed_at": datetime.now().isoformat(timespec="seconds")}],
    )
    memory._memories.append(entry)
    ctx = _assemble("dulu RTX ku pernah apa?")
    historical_items = [i for i in ctx.items if i.historical]
    assert len(historical_items) == 1
    assert "3070" in historical_items[0].text
    assert "previously said" in historical_items[0].text or "superseded" in historical_items[0].text
    assert "[Historical Context]" in ctx.render()


# ─────────────────────────────────────────────
# BUDGET (Step 16) - reuses MemoryRetrievalConfig, no second budget system
# ─────────────────────────────────────────────

def test_budget_max_item_count_enforced(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "2")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "10000")
    for i in range(5):
        memory._memories.append(_entry(f"aku suka warna nomor {i} banget", id_=f"c{i}"))
    ctx = _assemble("warna favoritku apa?")
    assert len(ctx.items) <= 2


def test_budget_max_token_count_enforced(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "50")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "20")
    for i in range(5):
        memory._memories.append(_entry(f"aku suka warna nomor {i} banget sekali gemas", id_=f"d{i}"))
    ctx = _assemble("warna favoritku apa?")
    total_tokens = sum(mc._estimate_tokens(i.text) for i in ctx.items)
    assert total_tokens <= 20


def test_budget_conflict_note_survives_whole_or_not_at_all(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "50")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "10000")
    memory._memories.append(_entry(
        "OS ku Windows", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os1",
    ))
    memory._memories.append(_entry(
        "OS ku Ubuntu", category="technical_fact", conflict_status="ambiguous_conflict",
        conflict_group="os-conflict", id_="os2",
    ))
    ctx = _assemble("OS ku sekarang apa?")
    conflict_items = [i for i in ctx.items if i.conflict_group]
    assert len(conflict_items) == 1
    assert "Windows" in conflict_items[0].text and "Ubuntu" in conflict_items[0].text


# ─────────────────────────────────────────────
# SAFETY - read-only, no mutation, no double-counted usage
# ─────────────────────────────────────────────

def test_safety_assemble_context_never_mutates_manual_memory_store():
    memory._memories.append(_entry("aku suka kopi hitam", id_="s1"))
    before = [dict(m) for m in memory._memories]
    _assemble("kopi favoritku apa?")
    after = memory._memories
    assert after == before


def test_safety_assemble_context_never_bumps_usage_tracking():
    entry = _entry("aku suka kopi hitam", id_="s2")
    memory._memories.append(entry)
    _assemble("kopi favoritku apa?")
    stored = memory.get_memory("s2")
    assert stored.get("retrieval_count") in (None, 0)
    assert stored.get("last_retrieved_at") is None


def test_safety_assemble_context_never_writes_verified_facts(tmp_path):
    store = _fact_store(tmp_path)
    store.record(_verified_ok("living_room_light", "on"))
    before = store.all_facts()
    _assemble("living room light gimana?", verified_fact_store=store)
    after = store.all_facts()
    assert after == before


def test_safety_assemble_context_creates_no_new_memory():
    memory._memories.append(_entry("aku suka kopi hitam"))
    before_count = len(memory.list_memories())
    _assemble("kopi favoritku apa?")
    assert len(memory.list_memories()) == before_count


# ─────────────────────────────────────────────
# DETERMINISM
# ─────────────────────────────────────────────

def test_determinism_same_state_and_query_yields_identical_result():
    memory._memories.append(_entry("aku suka kopi hitam"))
    ctx1 = _assemble("kopi favoritku apa?")
    ctx2 = _assemble("kopi favoritku apa?")
    assert ctx1.render() == ctx2.render()
    assert [i.text for i in ctx1.items] == [i.text for i in ctx2.items]
