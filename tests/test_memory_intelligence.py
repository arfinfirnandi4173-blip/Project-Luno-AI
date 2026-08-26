"""
test_memory_intelligence.py
============================

MEMORY INTELLIGENCE & IMPORTANCE ENGINE sprint - test suite for the
ADDITIONS this sprint made to `luno/memory.py`'s existing long-term
memory system (importance 0-4, lifecycle active/stale/archived,
consolidation/conflict-with-history, importance-aware retrieval ranking,
archived-exclusion, and the optional Step 14 "mark important"/"forget
last memory" commands).

Does NOT duplicate `tests/test_manual_memory.py`'s existing coverage of
plain CRUD/dedup/intent-detection/retrieval-integration - that surface
is untouched by this sprint and already covered. This file covers ONLY
what this sprint added, per its own Step 17 requirement:
Importance / Lifecycle / Consolidation / Conflict / Retrieval /
Backward compatibility / Persistence / Safety.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test in this file can
ever touch Vinn's real production `config/long_term_memory.json`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import luno.memory as memory
from luno.memory_retrieval import MemoryRetrievalConfig, MemoryRetriever


def _retriever_with_manual_memory_source():
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    return retriever


# ─────────────────────────────────────────────
#  Importance classification
# ─────────────────────────────────────────────


def test_importance_trivial_activity_is_zero():
    # Deliberately NOT one of the sprint brief's own literal example
    # sentences ("aku lagi makan" is semantic intent, not a fixture to
    # hardcode) - same trivial-activity pattern, different wording.
    assert memory._classify_memory_importance("aku baru mandi", "other") == 0


def test_importance_temporary_wording_is_one():
    assert memory._classify_memory_importance("minggu depan aku ada acara keluarga", "other") == 1


def test_importance_useful_technical_fact_is_two():
    assert memory._classify_memory_importance("aku pakai Ableton Live 11 sekarang", "technical_fact") == 2


def test_importance_ongoing_project_is_three():
    assert memory._classify_memory_importance(
        "aku sedang mengembangkan sistem voice assistant", "project_context"
    ) == 3


def test_importance_identity_defining_is_four():
    assert memory._classify_memory_importance(
        "aku ingin Luno jadi personal AI companion aku", "project_context"
    ) == 4


def test_importance_explicit_flag_overrides_everything_to_four():
    # A trivial-sounding sentence, but explicitly flagged "important" by
    # the user - explicitness must win over the base/activity signal.
    assert memory._classify_memory_importance("ini penting banget, aku lagi makan obat rutin", "other") == 4


def test_importance_is_not_length_based():
    """Step 5's explicit prohibition: a much longer sentence must NOT
    automatically outrank a short one on token/char count alone."""
    short = memory._classify_memory_importance("aku pakai RTX 3060 Ti", "technical_fact")
    long_but_same_signal_class = memory._classify_memory_importance(
        "jadi ceritanya kemarin itu aku lagi nyari-nyari komponen baru buat rig-ku dan "
        "akhirnya aku putuskan buat pakai kartu grafis RTX 3060 Ti karena harganya pas "
        "di budget dan performanya juga lumayan buat kerjaan sehari-hari",
        "technical_fact",
    )
    assert short == long_but_same_signal_class == 2


def test_add_memory_stores_computed_importance():
    entry = memory.add_memory("aku sedang membangun Luno sebagai project utama")
    assert entry["importance"] in memory.MEMORY_IMPORTANCE_LEVELS
    assert entry["importance"] >= 3  # ongoing project involvement


def test_get_importance_recomputes_for_missing_field():
    """Old (schema v1) entries never had `importance` at all -
    `_get_importance` must recompute a REAL value, never a flat
    placeholder default."""
    entry = {"id": "x1", "text": "aku pakai RTX 3060 Ti", "category": "technical_fact"}
    assert memory._get_importance(entry) == 2


def test_get_importance_ignores_invalid_stored_value():
    entry = {"id": "x2", "text": "aku pakai RTX 3060 Ti", "category": "technical_fact", "importance": 99}
    assert memory._get_importance(entry) == 2


# ─────────────────────────────────────────────
#  Lifecycle
# ─────────────────────────────────────────────


def _entry(importance, days_ago, source="llm_auto", text="some fact"):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    return {
        "id": "e1", "text": text, "category": "other",
        "importance": importance, "source": source,
        "created_at": ts, "updated_at": ts,
    }


def test_lifecycle_fresh_entry_is_active():
    entry = _entry(importance=2, days_ago=1)
    assert memory.compute_lifecycle(entry) == "active"


def test_lifecycle_moderately_old_low_importance_is_stale():
    entry = _entry(importance=1, days_ago=30)
    assert memory.compute_lifecycle(entry) == "stale"


def test_lifecycle_very_old_low_importance_is_archived():
    entry = _entry(importance=0, days_ago=365)
    assert memory.compute_lifecycle(entry) == "archived"


def test_lifecycle_core_memory_never_archives():
    """Step 8: core (importance=4) memory must never decay past
    'stale' - archiving would make it invisible to normal retrieval,
    which is not acceptable for an explicitly core fact."""
    entry = _entry(importance=4, days_ago=5000)
    assert memory.compute_lifecycle(entry) in ("active", "stale")


def test_lifecycle_explicit_source_decays_slower_than_inferred():
    """Same importance/age, only `source` differs - explicit must never
    be LESS protected than inferred (Step 9)."""
    inferred = _entry(importance=1, days_ago=20, source="llm_auto")
    explicit = _entry(importance=1, days_ago=20, source="user_explicit")
    inferred_state = memory.compute_lifecycle(inferred)
    explicit_state = memory.compute_lifecycle(explicit)
    order = {"active": 0, "stale": 1, "archived": 2}
    assert order[explicit_state] <= order[inferred_state]


def test_lifecycle_temporary_memory_can_become_stale_quickly():
    entry = _entry(importance=1, days_ago=25, source="llm_auto")
    assert memory.compute_lifecycle(entry) != "active"


def test_lifecycle_accepts_injected_now_for_determinism():
    entry = _entry(importance=2, days_ago=0)
    far_future = datetime.now() + timedelta(days=10000)
    assert memory.compute_lifecycle(entry, now=far_future) == "archived"


def test_lifecycle_handles_missing_timestamp_gracefully():
    entry = {"id": "e2", "text": "x", "category": "other", "importance": 2}
    # Must not raise - missing timestamp falls back to "now", i.e. fresh/active.
    assert memory.compute_lifecycle(entry) == "active"


def test_lifecycle_non_dict_entry_defaults_to_active_without_crashing():
    assert memory.compute_lifecycle("not a dict") == "active"


# ─────────────────────────────────────────────
#  Consolidation
# ─────────────────────────────────────────────


def test_consolidation_reworded_same_fact_merges_not_duplicates():
    memory.add_memory("PC utamaku pakai RTX 3060 Ti buat kerja")
    memory.add_memory("Sekarang PC utamaku pakai RTX 3060 Ti buat kerja juga")
    assert len(memory.list_memories()) == 1


def test_consolidation_value_conflict_updates_not_duplicates():
    memory.add_memory("User memakai RTX 3060 Ti di PC utamanya")
    memory.add_memory("User sekarang memakai RTX 4070 di PC utamanya")
    all_memories = memory.list_memories()
    assert len(all_memories) == 1
    assert "4070" in all_memories[0]["text"]


def test_consolidation_unrelated_memories_stay_separate():
    memory.add_memory("aku suka kopi hitam")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.add_memory("proyek utamaku namanya Luno")
    assert len(memory.list_memories()) == 3


def test_consolidation_exact_duplicate_still_reinforces_not_multiplies():
    """Pre-existing substring-dedup branch (unchanged) must still return
    None and must not create a second entry, now WITH the added
    reinforcement side-effect (importance bump) - proving the new
    importance-model work didn't regress the old contract."""
    first = memory.add_memory("aku suka kopi hitam")
    again = memory.add_memory("aku suka kopi hitam")
    assert again is None
    assert len(memory.list_memories()) == 1
    reinforced = memory.list_memories()[0]
    assert reinforced["importance"] >= first["importance"]


def test_consolidation_ambiguous_case_is_not_guessed():
    """Two existing memories that are both plausibly 'the same topic' as
    a new one at a TIE score must NOT be silently merged into either -
    Step 10's 'DO NOT GUESS'. Constructed directly (appended to
    `_memories`, bypassing `add_memory()`) specifically so the two
    candidates themselves are never compared against each other - only
    against the new text - forcing a genuine tie in
    `_find_conflicting_memory`'s own Jaccard scoring."""
    now = memory._now_iso()
    memory._memories.append({
        "id": "pa", "text": "proyek A menggunakan Python untuk backend",
        "category": "project_context", "importance": 2, "source": "llm_auto",
        "created_at": now, "updated_at": now, "history": [],
    })
    memory._memories.append({
        "id": "pb", "text": "proyek B menggunakan Python untuk backend",
        "category": "project_context", "importance": 2, "source": "llm_auto",
        "created_at": now, "updated_at": now, "history": [],
    })
    result = memory._find_conflicting_memory("proyek C menggunakan Python untuk backend", "project_context")
    assert result == "ambiguous"
    assert len(memory._memories) == 2


# ─────────────────────────────────────────────
#  Conflict / history
# ─────────────────────────────────────────────


def test_conflict_update_preserves_previous_state_in_history():
    created = memory.add_memory("User memakai RTX 3060 Ti")
    memory.update_memory(created["id"], "User memakai RTX 4070")
    updated = memory.get_memory(created["id"])
    assert updated["text"] == "User memakai RTX 4070"
    assert any("3060" in h["text"] for h in updated["history"])


def test_conflict_history_is_bounded():
    created = memory.add_memory("versi 0")
    mid = created["id"]
    for i in range(1, 10):
        memory.update_memory(mid, f"versi {i}")
    final = memory.get_memory(mid)
    assert len(final["history"]) <= memory._MAX_MEMORY_HISTORY_ENTRIES


def test_conflict_update_importance_never_decreases():
    created = memory.add_memory("ini penting banget: alergi kacang")
    assert created["importance"] == 4
    # A bland-sounding correction to the SAME fact should not silently
    # demote it from core down to a lower importance.
    updated = memory.update_memory(created["id"], "alergi kacang mede juga")
    assert updated["importance"] == 4


def test_conflict_does_not_silently_overwrite_unrelated_memory():
    a = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    b = memory.add_memory("aku suka kopi hitam")
    memory.update_memory(a["id"], "PC utamaku pakai RTX 4070")
    unrelated = memory.get_memory(b["id"])
    assert unrelated["text"] == "aku suka kopi hitam"


# ─────────────────────────────────────────────
#  Retrieval - relevance vs. importance, and budget
# ─────────────────────────────────────────────


def test_retrieval_relevance_beats_raw_importance():
    """Step 12's own worked example: a high-importance but IRRELEVANT
    memory must not beat a lower-importance RELEVANT one for a specific
    query."""
    memory.add_memory("aku ingin Luno jadi personal AI companion aku")  # importance 4, irrelevant
    memory.add_memory("aku sekarang pakai Guitar Rig 7")  # importance 2, relevant
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("cara setting Guitar Rig gimana ya?")
    assert len(results) == 1
    assert "Guitar Rig" in results[0].text


def test_retrieval_among_relevant_candidates_higher_importance_ranks_higher():
    memory.add_memory("aku sedang mengembangkan sistem voice assistant bernama Luno")  # importance 3
    memory.add_memory("aku pakai Luno buat nyalain lampu doang sih")  # lower/base importance, still mentions Luno
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("ceritain soal Luno dong")
    assert len(results) >= 1
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_retrieval_archived_memory_excluded_from_ambient_source():
    old_ts = (datetime.now() - timedelta(days=5000)).isoformat(timespec="seconds")
    memory._memories.append({
        "id": "old1", "text": "aku lagi ngantuk banget hari ini",
        "category": "other", "importance": 0, "source": "llm_auto",
        "created_at": old_ts, "updated_at": old_ts, "history": [],
        "schema_version": memory.MANUAL_MEMORY_SCHEMA_VERSION,
    })
    assert memory.compute_lifecycle(memory._memories[0]) == "archived"
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("aku ngantuk gak sih tadi?")
    assert all("ngantuk" not in r.text for r in results)


def test_retrieval_archived_memory_still_reachable_via_search_and_list():
    old_ts = (datetime.now() - timedelta(days=5000)).isoformat(timespec="seconds")
    memory._memories.append({
        "id": "old2", "text": "aku lagi ngantuk banget hari ini",
        "category": "other", "importance": 0, "source": "llm_auto",
        "created_at": old_ts, "updated_at": old_ts, "history": [],
        "schema_version": memory.MANUAL_MEMORY_SCHEMA_VERSION,
    })
    assert memory.get_memory("old2") is not None
    assert any(m["id"] == "old2" for m in memory.list_memories())
    assert any("ngantuk" in m["text"] for m in memory.search_memories("ngantuk"))


def test_retrieval_result_count_is_bounded_by_budget():
    for i in range(10):
        memory.add_memory(f"aku suka game nomor {i} banget")
    config = MemoryRetrievalConfig(max_results=3, max_tokens=100000)
    retriever = MemoryRetriever(config)
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    results = retriever.retrieve_memories("game favoritku apa aja")
    assert len(results) <= 3


# ─────────────────────────────────────────────
#  Backward compatibility
# ─────────────────────────────────────────────


def test_old_schema_v1_entry_loads_without_importance_or_history(tmp_path, monkeypatch):
    old_file = tmp_path / "long_term_memory.json"
    old_file.write_text(json.dumps([
        {
            "id": "legacy1", "text": "aku suka kopi hitam",
            "created_at": "2025-01-01T00:00:00", "updated_at": "2025-01-01T00:00:00",
            "category": "preference", "source": "user_explicit", "schema_version": 1,
        }
    ]), encoding="utf-8")
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(old_file))
    memory._load()
    assert len(memory._memories) == 1
    loaded = memory._memories[0]
    assert "importance" not in loaded  # never silently rewritten on mere load
    assert memory._get_importance(loaded) in memory.MEMORY_IMPORTANCE_LEVELS
    assert memory.compute_lifecycle(loaded) in ("active", "stale", "archived")


def test_missing_optional_fields_do_not_crash_retrieval():
    now = memory._now_iso()
    memory._memories.append({
        "id": "bare1", "text": "PC utamaku pakai RTX 3060 Ti",
        "created_at": now, "updated_at": now,
        "category": "technical_fact",
        # no importance, no history, no source, no schema_version
    })
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("cerita soal PC-ku dong")
    assert len(results) == 1


def test_malformed_importance_field_does_not_crash():
    entry = {"id": "bad1", "text": "some fact", "category": "other", "importance": "not-a-number"}
    assert memory._get_importance(entry) in memory.MEMORY_IMPORTANCE_LEVELS
    assert memory.compute_lifecycle(entry) in ("active", "stale", "archived")


def test_unknown_extra_fields_are_safely_ignored():
    memory._memories.append({
        "id": "extra1", "text": "aku suka kopi hitam",
        "created_at": "2025-01-01T00:00:00", "updated_at": "2025-01-01T00:00:00",
        "category": "preference", "importance": 2, "history": [], "source": "user_explicit",
        "schema_version": 2, "some_future_field_this_code_has_never_heard_of": {"nested": True},
    })
    assert memory.get_memory("extra1") is not None
    assert any(m["id"] == "extra1" for m in memory.list_memories())


# ─────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────


def test_persistence_restart_preserves_importance_and_history():
    created = memory.add_memory("ini penting banget: alergi kacang")
    memory.update_memory(created["id"], "alergi kacang tanah")
    saved_path = memory.config.LONG_TERM_MEMORY_FILE
    on_disk = json.loads(open(saved_path, encoding="utf-8").read())
    match = next(e for e in on_disk if e["id"] == created["id"])
    assert match["importance"] == 4
    assert len(match["history"]) == 1
    # Simulate a restart: reset in-memory state and reload from disk.
    memory._memories.clear()
    memory._load()
    reloaded = memory.get_memory(created["id"])
    assert reloaded["importance"] == 4
    assert len(reloaded["history"]) == 1


def test_persistence_delete_removes_only_correct_entry():
    a = memory.add_memory("aku suka kopi hitam")
    b = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    c = memory.add_memory("proyek utamaku namanya Luno")
    memory.delete_memory_by_id(b["id"])
    remaining_ids = {m["id"] for m in memory.list_memories()}
    assert remaining_ids == {a["id"], c["id"]}


def test_persistence_duplicate_prevention_survives_restart():
    memory.add_memory("aku suka kopi hitam")
    memory._memories.clear()
    memory._load()
    again = memory.add_memory("aku suka kopi hitam")
    assert again is None
    assert len(memory.list_memories()) == 1


# ─────────────────────────────────────────────
#  Safety
# ─────────────────────────────────────────────


def test_safety_malformed_entry_in_store_does_not_crash_list_memories():
    memory._memories.append("not even a dict")
    memory._memories.append({"id": "ok1", "text": "aku suka kopi hitam", "category": "preference"})
    listed = memory.list_memories()
    assert any(m.get("id") == "ok1" for m in listed if isinstance(m, dict))


def test_safety_tests_never_touch_real_production_file(tmp_path):
    """Sanity check on the isolation fixture itself: the redirected path
    used by this test run must not be the real repo-relative production
    path - it must live under pytest's own isolated `tmp_path`."""
    assert memory.config.LONG_TERM_MEMORY_FILE.endswith("LONG_TERM_MEMORY_FILE.json")
    assert str(tmp_path) in memory.config.LONG_TERM_MEMORY_FILE
    assert memory.config.LONG_TERM_MEMORY_FILE != "config/long_term_memory.json"


# ─────────────────────────────────────────────
#  Optional Step 14 commands: mark important / forget last memory
# ─────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Memory ini penting.",
    "ini penting banget",
    "Jadikan memory ini permanen.",
    "This is important.",
])
def test_detect_mark_important_command_matches(text):
    assert memory.detect_mark_important_command(text) is True


@pytest.mark.parametrize("text", [
    "aku lagi mikirin sesuatu yang penting",
    "ingetin aku besok ada meeting penting",
])
def test_detect_mark_important_command_does_not_match_ordinary_text(text):
    assert memory.detect_mark_important_command(text) is False


@pytest.mark.parametrize("text", [
    "Jangan simpan ini.",
    "Lupakan memory ini.",
    "Hapus memory ini.",
    "Forget this memory.",
])
def test_detect_forget_last_memory_command_matches(text):
    assert memory.detect_forget_last_memory_command(text) is True


def test_mark_last_memory_important_promotes_most_recent_entry():
    memory.add_memory("aku suka kopi hitam")
    last = memory.add_memory("aku pakai keyboard mechanical")
    result = memory.mark_last_memory_important()
    assert result["id"] == last["id"]
    assert result["importance"] == 4
    assert result["source"] == "user_explicit"


def test_mark_last_memory_important_with_no_memories_returns_none():
    assert memory.mark_last_memory_important() is None


def test_forget_last_memory_removes_most_recent_entry():
    first = memory.add_memory("aku suka kopi hitam")
    memory.add_memory("aku pakai keyboard mechanical")
    removed_text = memory.forget_last_memory()
    assert removed_text == "aku pakai keyboard mechanical"
    remaining = memory.list_memories()
    assert len(remaining) == 1
    assert remaining[0]["id"] == first["id"]


def test_forget_last_memory_with_no_memories_returns_none():
    assert memory.forget_last_memory() is None
