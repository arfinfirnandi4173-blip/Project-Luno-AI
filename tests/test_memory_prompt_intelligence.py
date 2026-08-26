"""
test_memory_prompt_intelligence.py
====================================

MEMORY PROMPT INTELLIGENCE sprint - test suite for the ADDITIONS this
sprint made to `luno/memory.py`'s `build_memory_prompt()`.

Scope: this file covers ONLY the new `query_text=` kwarg path
(`_select_memories_for_prompt()`/`_score_memory_for_prompt()`) - the
importance/lifecycle/relevance/conflict-aware selection layered on top
of the EXISTING, UNCHANGED `_memories` store, `_get_importance()`,
`compute_lifecycle()`, `_is_historical_query()`, and
`luno.memory_retrieval.query` tokenizer this sprint reuses rather than
duplicates. Does NOT re-test importance classification, lifecycle
thresholds, conflict classification, or consolidation themselves -
those are `tests/test_memory_intelligence.py`'s and
`tests/test_memory_conflict.py`'s job, already covered and unchanged by
this sprint.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production `config/long_term_memory.json`.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

import luno.memory as memory
from luno.memory_retrieval import MemoryRetrievalConfig, MemoryRetriever


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None):
    """Directly-constructed memory entry (bypassing `add_memory()`) so
    tests can pin exact importance/age/conflict/history fields -
    mirrors `tests/test_memory_intelligence.py`'s own `_entry()` helper,
    extended with the conflict/history fields this sprint's selection
    logic reads."""
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


# ─────────────────────────────────────────────
# A/E - importance=4 survives normal limits / core memory survives age
# ─────────────────────────────────────────────


def test_importance_4_relevant_memory_is_selected():
    memory._memories.append(_entry("Luno adalah AI companion utama Vinn", importance=4, days_ago=1))
    prompt = memory.build_memory_prompt(query_text="Luno itu apa buat kamu?")
    assert "AI companion" in prompt


def test_core_memory_remains_eligible_despite_age():
    # importance=4 at 5000 days old computes to "stale", never "archived"
    # (compute_lifecycle's own core-memory protection) - must still be
    # selectable when relevant.
    entry = _entry("Luno adalah AI companion utama Vinn", importance=4, days_ago=5000)
    memory._memories.append(entry)
    assert memory.compute_lifecycle(entry) in ("active", "stale")
    prompt = memory.build_memory_prompt(query_text="Luno itu apa buat kamu?")
    assert "AI companion" in prompt


# ─────────────────────────────────────────────
# B - importance=0 excluded under a tight budget
# ─────────────────────────────────────────────


def test_low_importance_excluded_under_tight_budget(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "1")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "10000")
    memory._memories.append(_entry("game favorit satu penting sekali", importance=4, days_ago=1))
    memory._memories.append(_entry("game favorit dua kurang penting", importance=0, days_ago=1))
    prompt = memory.build_memory_prompt(query_text="game favoritku apa?")
    assert "satu" in prompt
    assert "dua" not in prompt


# ─────────────────────────────────────────────
# C - stale low-importance memories are deprioritized
# ─────────────────────────────────────────────


def test_stale_memory_deprioritized_under_tight_budget(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "1")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "10000")
    memory._memories.append(_entry("proyek robotik fresh terbaru", importance=2, days_ago=1))
    memory._memories.append(_entry("proyek robotik lama sekali", importance=2, days_ago=100))
    prompt = memory.build_memory_prompt(query_text="proyek robotik gimana?")
    assert "fresh" in prompt
    assert "lama sekali" not in prompt


# ─────────────────────────────────────────────
# D - archived ordinary memories excluded
# ─────────────────────────────────────────────


def test_archived_memory_excluded_even_if_relevant():
    entry = _entry("kucing peliharaan lama sudah tidak dibahas lagi", importance=0, days_ago=5000)
    memory._memories.append(entry)
    assert memory.compute_lifecycle(entry) == "archived"
    prompt = memory.build_memory_prompt(query_text="kucing peliharaan gimana kabarnya?")
    assert prompt == ""
    # Not deleted - still fully intact and recoverable directly.
    assert memory.get_memory(entry["id"]) is not None
    assert memory.list_memories() == [entry]


# ─────────────────────────────────────────────
# F/G - relevance beats importance in both directions
# ─────────────────────────────────────────────


def test_irrelevant_high_importance_memory_does_not_pollute_specific_query():
    memory._memories.append(_entry("Vinn suka Avenged Sevenfold", importance=4, days_ago=1, category="preference"))
    prompt = memory.build_memory_prompt(query_text="cara konfigurasi Docker networking")
    assert prompt == ""


def test_relevant_low_importance_beats_irrelevant_high_importance():
    memory._memories.append(_entry("Vinn sedang mengembangkan Luno", importance=4, days_ago=1, category="project_context"))
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1, category="technical_fact"))
    prompt = memory.build_memory_prompt(query_text="RTX 3060 di PC gimana?")
    assert "RTX 3060" in prompt
    assert "Avenged" not in prompt or "Sevenfold" not in prompt  # sanity - wrong fixture guard
    assert "mengembangkan Luno" not in prompt


# ─────────────────────────────────────────────
# H/I/K - current vs. historical
# ─────────────────────────────────────────────


def _make_corrected_gpu_pair():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop")


def test_current_query_prefers_current_value_over_history():
    _make_corrected_gpu_pair()
    prompt = memory.build_memory_prompt(query_text="RTX di laptop sekarang apa?")
    assert "3060" in prompt
    assert "3070" not in prompt


def test_historical_query_surfaces_relevant_history():
    _make_corrected_gpu_pair()
    prompt = memory.build_memory_prompt(query_text="dulu RTX di laptop apa?")
    assert "3070" in prompt
    assert "historical" in prompt.lower() or "previously" in prompt.lower() or "superseded" in prompt.lower()


def test_history_not_dumped_for_non_historical_relevant_query():
    """A current-shaped query that's still relevant to an entry carrying
    history must NOT surface that history - Section 10's "don't dump
    history unnecessarily"."""
    _make_corrected_gpu_pair()
    prompt = memory.build_memory_prompt(query_text="RTX di laptop gimana?")
    assert "3070" not in prompt
    assert "superseded" not in prompt.lower()


# ─────────────────────────────────────────────
# J - ambiguous conflicts are never silently resolved
# ─────────────────────────────────────────────


def test_ambiguous_conflict_surfaces_both_sides_never_one_winner():
    memory.add_memory("Aku pakai Windows 11")
    memory.add_memory("Aku pakai Ubuntu")
    groups = memory.list_conflicts()
    assert len(groups) == 1
    prompt = memory.build_memory_prompt(query_text="OS ku sekarang apa, Windows atau Ubuntu?")
    assert "Windows 11" in prompt
    assert "Ubuntu" in prompt
    assert "conflicting" in prompt.lower() or "unresolved" in prompt.lower()


def test_ambiguous_conflict_omitted_when_irrelevant():
    memory.add_memory("Aku pakai Windows 11")
    memory.add_memory("Aku pakai Ubuntu")
    prompt = memory.build_memory_prompt(query_text="cara masak nasi goreng enak")
    assert prompt == ""


# ─────────────────────────────────────────────
# L - Verified Facts remain separate
# ─────────────────────────────────────────────


def test_build_memory_prompt_never_touches_verified_facts_module():
    """Structural guard - `build_memory_prompt()`/`_select_memories_for_prompt()`
    must have zero source-level reference to `memory_guard`/`VerifiedFactStore`
    (the sprint's own Section 11 requirement: no code change there unless
    an actual gap is found - none was)."""
    src = inspect.getsource(memory.build_memory_prompt) + inspect.getsource(memory._select_memories_for_prompt)
    assert "memory_guard" not in src
    assert "VerifiedFactStore" not in src


def test_verified_fact_store_unaffected_by_prompt_generation(tmp_path):
    from luno.memory_guard import VerifiedFactStore
    from luno.tool_manager.result import ToolResult

    store = VerifiedFactStore(path=str(tmp_path / "verified_facts.json"))
    result = ToolResult(success=True, tool="home_assistant", action="turn_on",
                         message="ok", data={"entity_id": "light.kamar", "target": "lampu kamar"})
    store.record(result, tool_name="home_assistant", request_id="r1")
    before = store.all_facts()

    memory._memories.append(_entry("lampu kamar gampang dinyalain", importance=2, days_ago=1))
    memory.build_memory_prompt(query_text="lampu kamar gimana?")

    assert store.all_facts() == before


# ─────────────────────────────────────────────
# M - Episodic Memory is not duplicated
# ─────────────────────────────────────────────


def test_build_memory_prompt_never_touches_episodic_memory_module():
    src = inspect.getsource(memory.build_memory_prompt) + inspect.getsource(memory._select_memories_for_prompt)
    assert "episodic_memory" not in src
    assert "EpisodicMemoryStore" not in src


def test_episodic_experience_never_appears_in_manual_memory_prompt(monkeypatch, tmp_path):
    import luno.episodic_memory as episodic

    monkeypatch.setattr(episodic.config, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic.json"))
    is_new, exp = episodic.observe_turn(
        "akhirnya home assistant integration jalan lancar, masalahnya udah kelar semua",
        had_successful_tool_call=True, explicit_memory_shared=False,
    )
    assert is_new is True

    memory._memories.append(_entry("home assistant integration emang keren", importance=2, days_ago=1))
    prompt = memory.build_memory_prompt(query_text="home assistant integration gimana?")
    assert "keren" in prompt
    # The episodic store's own summary text never leaks in - it lives in
    # a completely separate store this function never reads.
    assert exp.summary not in prompt


# ─────────────────────────────────────────────
# N - duplicate memories do not appear twice
# ─────────────────────────────────────────────


def test_duplicate_text_entries_collapse_to_one_in_selection():
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1, id_="dup1"))
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1, id_="dup2"))
    selected = memory._select_memories_for_prompt("RTX 3060 di PC gimana?")
    assert selected.count("PC utamaku pakai RTX 3060 Ti") == 1


# ─────────────────────────────────────────────
# O - deterministic output
# ─────────────────────────────────────────────


def test_prompt_output_is_deterministic():
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1))
    memory._memories.append(_entry("GPU laptop RTX 3060 Ti juga", importance=3, days_ago=1))
    first = memory.build_memory_prompt(query_text="RTX 3060 gimana?")
    second = memory.build_memory_prompt(query_text="RTX 3060 gimana?")
    assert first == second


# ─────────────────────────────────────────────
# P - old schema-v1 memory still works
# ─────────────────────────────────────────────


def test_schema_v1_entry_without_importance_or_category_still_selectable():
    # No "importance", "category", "source", "schema_version", or
    # "history" key at all - exactly what a pre-Memory-Intelligence-sprint
    # entry looks like on disk. Recent timestamp deliberately - this test
    # is about missing FIELDS being tolerated, not about age/lifecycle
    # (already covered by the archived-exclusion test above).
    recent = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    memory._memories.append({
        "id": "v1", "text": "user likes tea", "created_at": recent,
    })
    prompt = memory.build_memory_prompt(query_text="does the user like tea?")
    assert "user likes tea" in prompt


# ─────────────────────────────────────────────
# Q - malformed entries never crash prompt generation
# ─────────────────────────────────────────────


def test_malformed_entries_do_not_crash_prompt_generation():
    memory._memories.append("not a dict")
    memory._memories.append({"id": "no-text"})
    memory._memories.append({"id": "bad-conflict", "text": "GPU cocok banget",
                              "conflict_status": "ambiguous_conflict",
                              "conflict_group": {"not": "hashable"}})
    memory._memories.append(_entry("GPU utama RTX 3060 Ti", importance=2, days_ago=1))
    # Must not raise.
    prompt = memory.build_memory_prompt(query_text="GPU gimana?")
    assert "RTX 3060" in prompt


def test_malformed_top_level_state_does_not_crash_bare_call():
    memory._memories.append("not a dict")
    memory._memories.append({"id": "no-text"})
    # Bare call (legacy full-dump path) must also stay crash-safe.
    memory.build_memory_prompt()


# ─────────────────────────────────────────────
# R - prompt stays within the configured budget
# ─────────────────────────────────────────────


def test_selection_bounded_by_max_results(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "2")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "10000")
    for i in range(6):
        memory._memories.append(_entry(f"aku suka main game favorit nomor {i} banget sekali", importance=2, days_ago=1))
    selected = memory._select_memories_for_prompt("game favoritku apa aja?")
    assert len(selected) <= 2


def test_selection_bounded_by_max_tokens(monkeypatch):
    monkeypatch.setenv("MAX_MEMORY_RESULTS", "100")
    monkeypatch.setenv("MAX_MEMORY_TOKENS", "5")
    for i in range(6):
        memory._memories.append(_entry(f"aku suka main game favorit nomor {i} banget sekali sekali lagi", importance=2, days_ago=1))
    selected = memory._select_memories_for_prompt("game favoritku apa aja?")
    assert len(selected) <= 1


# ─────────────────────────────────────────────
# S - prompt selection is read-only
# ─────────────────────────────────────────────


def test_prompt_generation_never_calls_save(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "_save", lambda: calls.append(1))
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1))
    memory.build_memory_prompt(query_text="RTX 3060 gimana?")
    memory.build_memory_prompt()
    assert calls == []


def test_prompt_generation_does_not_mutate_entries():
    entry = _entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1)
    memory._memories.append(entry)
    before = dict(entry)
    memory.build_memory_prompt(query_text="RTX 3060 gimana?")
    assert entry == before


# ─────────────────────────────────────────────
# T - restart preserves exact stored data
# ─────────────────────────────────────────────


def test_restart_preserves_exact_stored_data_after_prompt_generation():
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.add_memory("Vinn suka kopi hitam")
    memory.build_memory_prompt(query_text="RTX 3060 gimana?")
    memory.build_memory_prompt()

    before = [dict(m) for m in memory.list_memories()]
    memory._memories = []
    memory._load()
    after = [dict(m) for m in memory.list_memories()]
    assert after == before


# ─────────────────────────────────────────────
# Backward compatibility - the bare, zero-arg call path is untouched
# ─────────────────────────────────────────────


def test_bare_call_still_dumps_everything_unconditionally():
    """The zero-arg path must remain byte-for-byte the legacy behavior -
    this is what keeps every pre-sprint caller/test passing untouched."""
    memory._memories.append(_entry("kucing peliharaan lama sudah tidak dibahas lagi", importance=0, days_ago=5000))
    memory._memories.append(_entry("Vinn suka Avenged Sevenfold", importance=4, days_ago=1, category="preference"))
    prompt = memory.build_memory_prompt()
    assert "kucing peliharaan" in prompt
    assert "Avenged Sevenfold" in prompt


def test_no_signal_query_selects_nothing():
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=2, days_ago=1))
    prompt = memory.build_memory_prompt(query_text="apa itu 5 + 5?")
    assert prompt == ""


def test_empty_query_text_falls_back_to_legacy_bare_behavior():
    memory._memories.append(_entry("PC utamaku pakai RTX 3060 Ti", importance=0, days_ago=5000))
    # An explicitly-empty string is falsy - same code path as omitting
    # the argument entirely (see build_memory_prompt's own docstring).
    prompt = memory.build_memory_prompt(query_text="")
    assert "RTX 3060" in prompt
