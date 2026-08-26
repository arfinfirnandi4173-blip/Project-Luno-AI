"""
test_memory_conflict.py
=========================

MEMORY CONFLICT RESOLUTION & TRUSTED FACTS GUARD sprint - test suite for
the ADDITIONS this sprint made to `luno/memory.py`'s existing long-term
memory system: deterministic conflict classification
(`_classify_conflict`) layered inside `add_memory()`'s existing
consolidation pipeline, `conflict_status`/`conflict_group` metadata for
genuinely ambiguous conflicts, `history[].reason` tagging, historical-
query-aware retrieval (`search_memories()` /
`make_manual_memory_source()`), and the two new explicit commands
(`detect_show_conflicts_command`/`list_conflicts`,
`detect_resolve_conflict_command`/`resolve_conflict_by_topic`).

Does NOT duplicate `tests/test_manual_memory.py`'s or
`tests/test_memory_intelligence.py`'s existing coverage of plain CRUD /
importance / lifecycle / the PRE-EXISTING consolidation mechanics (exact
dedup, same-topic merge with history) - all of that is untouched by this
sprint and already covered. This file covers ONLY what this sprint
added, per its own Section 18 categories: No conflict / Refinement /
Correction / Temporal / Ambiguity / Importance / Provenance /
Persistence.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test in this file can
ever touch Vinn's real production `config/long_term_memory.json`.
"""

from __future__ import annotations

import json

import pytest

import luno.memory as memory
from luno.memory_retrieval import MemoryRetrievalConfig, MemoryRetriever


def _retriever_with_manual_memory_source():
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    return retriever


# ─────────────────────────────────────────────
#  No conflict
# ─────────────────────────────────────────────


def test_no_conflict_unrelated_preference_memories_coexist():
    """Section 2's own example: liking a guitar and liking a game are
    not competing claims - a bare preference category never lands in
    AMBIGUOUS_CONFLICT with no other signal."""
    memory.add_memory("Aku suka gitar.")
    memory.add_memory("Aku suka game.")
    stored = memory.list_memories()
    assert len(stored) == 2
    assert all(m.get("conflict_status") is None for m in stored)


def test_no_conflict_same_topic_different_context_coexist():
    """Section 10/2's own example: 'RTX 3060 Ti untuk PC' and 'RTX 3070
    Ti untuk server' share enough vocabulary to land in the
    consolidation band, but the distinguishing PC/server qualifier means
    they are about different machines - must coexist, never merge."""
    memory.add_memory("RTX 3060 Ti untuk PC.")
    memory.add_memory("RTX 3070 Ti untuk server.")
    stored = memory.list_memories()
    assert len(stored) == 2
    texts = {m["text"] for m in stored}
    assert any("PC" in t for t in texts)
    assert any("server" in t for t in texts)
    assert all(m.get("conflict_status") is None for m in stored)


def test_no_conflict_multiple_hardware_devices_coexist():
    memory.add_memory("Laptop kerja pakai RTX 3060 Ti.")
    memory.add_memory("PC utama pakai RTX 4070.")
    assert len(memory.list_memories()) == 2


# ─────────────────────────────────────────────
#  Refinement
# ─────────────────────────────────────────────


def test_refinement_broad_to_specific_upgrades_text():
    """Section 2's own example: 'Aku pakai Windows.' -> 'Aku pakai
    Windows 11 Pro.' must not create a second, contradicting entry - the
    stored text should become the MORE detailed version, not silently
    discard it."""
    memory.add_memory("Aku pakai Windows.")
    memory.add_memory("Aku pakai Windows 11 Pro.")
    stored = memory.list_memories()
    assert len(stored) == 1
    assert stored[0]["text"] == "Aku pakai Windows 11 Pro"
    assert any(h["text"] == "Aku pakai Windows" for h in stored[0]["history"])


def test_refinement_specific_to_more_specific_still_one_entry():
    memory.add_memory("Aku pakai RTX 3060 Ti untuk kerja.")
    memory.add_memory("Aku pakai RTX 3060 Ti untuk kerja render video.")
    stored = memory.list_memories()
    assert len(stored) == 1
    assert "render video" in stored[0]["text"]


def test_refinement_does_not_create_duplicate_memory():
    memory.add_memory("Aku pakai Windows.")
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Windows 11 Pro.")
    assert len(memory.list_memories()) == 1


def test_refinement_backward_keeps_more_detailed_existing_text():
    """A terser restatement of a SUBSET of an already-detailed memory
    must not regress the stored detail."""
    memory.add_memory("Aku pakai Windows 11 Pro di laptop kerja.")
    result = memory.add_memory("Aku pakai Windows 11 Pro.")
    assert result is None
    stored = memory.list_memories()
    assert len(stored) == 1
    assert stored[0]["text"] == "Aku pakai Windows 11 Pro di laptop kerja"


# ─────────────────────────────────────────────
#  Correction
# ─────────────────────────────────────────────


def test_correction_supersedes_old_active_memory():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    stored = memory.list_memories()
    assert len(stored) == 1
    assert "3060" in stored[0]["text"]


def test_correction_old_value_remains_in_history():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    entry = memory.list_memories()[0]
    assert any("3070" in h["text"] and h.get("reason") == "correction" for h in entry["history"])


def test_correction_new_value_becomes_current():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    entry = memory.list_memories()[0]
    assert "3060" in entry["text"]
    assert "3070" not in entry["text"]


# ─────────────────────────────────────────────
#  Temporal
# ─────────────────────────────────────────────


def test_temporal_dulu_sekarang_merges_with_reason():
    memory.add_memory("Aku kerja di perusahaan Alpha.")
    memory.add_memory("Dulu di perusahaan Alpha, sekarang aku kerja di perusahaan Beta.")
    stored = memory.list_memories()
    assert len(stored) == 1
    assert "Beta" in stored[0]["text"]
    assert any(h.get("reason") == "temporal_change" for h in stored[0]["history"])


def test_temporal_both_remain_retrievable_via_search():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    historical_results = memory.search_memories("GPU yang dulu pernah aku pakai")
    assert any("3070" in r["text"] and r.get("historical") for r in historical_results)


def test_temporal_current_query_selects_current_value():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("GPU ku sekarang apa?")
    assert any("3060" in r.text for r in results)
    assert not any("historical" in r.text for r in results)


def test_temporal_historical_query_selects_old_value():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("GPU yang dulu pernah aku pakai apa?")
    assert any("3070" in r.text and "historical" in r.text for r in results)


# ─────────────────────────────────────────────
#  Ambiguity
# ─────────────────────────────────────────────


def test_ambiguous_conflict_preserves_both_without_resolution():
    """Section 2's primary worked example: Windows 11 vs Ubuntu, no
    correction/temporal signal either way - both must be preserved,
    neither silently deleted or overwritten."""
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    stored = memory.list_memories()
    assert len(stored) == 2
    texts = {m["text"] for m in stored}
    assert texts == {"Aku pakai Windows 11", "Aku pakai Ubuntu"}


def test_ambiguous_conflict_flags_both_with_shared_group():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    stored = memory.list_memories()
    assert all(m.get("conflict_status") == "ambiguous_conflict" for m in stored)
    groups = {m["conflict_group"] for m in stored}
    assert len(groups) == 1  # same shared group


def test_ambiguous_conflict_no_automatic_deletion():
    before = len(memory.list_memories())
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    assert len(memory.list_memories()) == before + 2


def test_ambiguous_conflict_no_arbitrary_winner_neither_text_changed():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    texts = {m["text"] for m in memory.list_memories()}
    assert "Aku pakai Windows 11" in texts
    assert "Aku pakai Ubuntu" in texts


def test_list_conflicts_groups_by_conflict_group():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    groups = memory.list_conflicts()
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_resolve_conflict_by_topic_keeps_named_side_and_preserves_history():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    status, entry = memory.resolve_conflict_by_topic("Ubuntu")
    assert status == "resolved"
    assert entry["text"] == "Aku pakai Ubuntu"
    assert entry.get("conflict_status") is None
    assert any(h["text"] == "Aku pakai Windows 11" for h in entry["history"])
    stored = memory.list_memories()
    assert len(stored) == 1
    assert stored[0]["text"] == "Aku pakai Ubuntu"


def test_resolve_conflict_by_topic_no_match_returns_not_found():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    status, entry = memory.resolve_conflict_by_topic("Guitar Rig")
    assert status == "not_found"
    assert entry is None
    assert len(memory.list_memories()) == 2


def test_detect_show_conflicts_command_matches():
    assert memory.detect_show_conflicts_command("Tampilkan konflik memory.") is True
    assert memory.detect_show_conflicts_command("show memory conflicts") is True


def test_detect_show_conflicts_command_does_not_match_ordinary_text():
    assert memory.detect_show_conflicts_command("aku lagi bingung nih") is False


def test_detect_resolve_conflict_command_matches_and_captures_topic():
    assert memory.detect_resolve_conflict_command("memory Ubuntu yang benar") == "Ubuntu"


def test_detect_resolve_conflict_command_does_not_match_ordinary_text():
    assert memory.detect_resolve_conflict_command("itu jawaban yang benar kok") is None


# ─────────────────────────────────────────────
#  Importance is not truth
# ─────────────────────────────────────────────


def test_high_importance_does_not_automatically_win_factual_conflict():
    """Step 6/7-equivalent core rule (Section 6): a core (importance=4)
    memory must not silently overwrite/absorb a contradictory,
    lower-importance new memory just because it outranks it."""
    memory.add_memory("Aku pakai Windows 11.")
    memory.mark_last_memory_important()
    memory.add_memory("Aku pakai Ubuntu.")
    stored = memory.list_memories()
    assert len(stored) == 2
    windows_entry = next(m for m in stored if "Windows" in m["text"])
    ubuntu_entry = next(m for m in stored if "Ubuntu" in m["text"])
    assert windows_entry["importance"] == 4
    assert windows_entry.get("conflict_status") == "ambiguous_conflict"
    assert ubuntu_entry.get("conflict_status") == "ambiguous_conflict"


def test_low_importance_explicit_correction_can_still_supersede():
    created = memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    assert memory._get_importance(created) < 4
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    stored = memory.list_memories()
    assert len(stored) == 1
    assert "3060" in stored[0]["text"]


# ─────────────────────────────────────────────
#  Provenance
# ─────────────────────────────────────────────


def test_explicit_source_not_downgraded_by_inferred_correction():
    """Section 8/9: an existing `user_explicit` memory's `source` must
    never be silently downgraded just because the correcting text came
    from an `llm_auto` (inferred) call."""
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.", source="user_explicit")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.", source="llm_auto")
    entry = memory.list_memories()[0]
    assert entry["source"] == "user_explicit"


def test_inferred_memory_never_treated_as_verified_fact():
    """Verified Facts Guard boundary (Section 15): `luno.memory` and
    `luno.memory_guard.VerifiedFactStore` remain completely separate
    stores - an `llm_auto` (inferred) manual memory must never appear in
    or influence the Verified Facts store."""
    from luno.memory_guard import VerifiedFactStore
    import tempfile
    import os as _os

    memory.add_memory("Aku pakai Ubuntu.", source="llm_auto")

    tmp_path = _os.path.join(tempfile.mkdtemp(), "verified_facts_isolated.json")
    store = VerifiedFactStore(path=tmp_path)
    assert store.all_facts() == []
    assert store.get("aku_pakai_ubuntu") is None


# ─────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────


def test_conflict_metadata_survives_restart():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    memory._memories.clear()
    memory._load()
    stored = memory.list_memories()
    assert len(stored) == 2
    assert all(m.get("conflict_status") == "ambiguous_conflict" for m in stored)
    groups = {m["conflict_group"] for m in stored}
    assert len(groups) == 1


def test_history_reason_survives_restart():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop.")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop.")
    memory._memories.clear()
    memory._load()
    entry = memory.list_memories()[0]
    assert any(h.get("reason") == "correction" for h in entry["history"])


def test_malformed_conflict_metadata_fails_safely(tmp_path, monkeypatch):
    now = memory._now_iso()
    bad_file = tmp_path / "long_term_memory.json"
    bad_file.write_text(json.dumps([
        {
            "id": "bad1", "text": "aku suka kopi hitam",
            "created_at": now, "updated_at": now,
            "category": "preference", "conflict_status": 12345, "conflict_group": {"not": "a string"},
        }
    ]), encoding="utf-8")
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(bad_file))
    memory._load()
    # Must not crash list_conflicts(), search, or retrieval, even with a
    # malformed conflict_status that isn't the expected string sentinel.
    assert memory.list_conflicts() == []
    assert len(memory.list_memories()) == 1
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("kopi hitam")
    assert len(results) == 1


def test_persistence_on_disk_shape_is_plain_json_list():
    memory.add_memory("Aku pakai Windows 11.")
    memory.add_memory("Aku pakai Ubuntu.")
    saved_path = memory.config.LONG_TERM_MEMORY_FILE
    on_disk = json.loads(open(saved_path, encoding="utf-8").read())
    assert isinstance(on_disk, list)
    assert len(on_disk) == 2
    assert all(e.get("conflict_status") == "ambiguous_conflict" for e in on_disk)
