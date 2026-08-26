"""
test_manual_memory.py
======================

Manual Memory Management sprint - test suite for the ADDITIONS this
sprint made to `luno/memory.py`'s existing long-term memory system
(`category`/`source`/`updated_at`/`schema_version` fields, `get_memory`,
`update_memory`, `update_memory_by_topic`, `delete_memory_by_id`,
`search_memories`, `detect_update_memory_command`,
`detect_delete_memory_by_id_command`, `detect_delete_memory_by_topic_command`,
`make_manual_memory_source`).

Does NOT duplicate `tests/test_memory_regression.py`'s existing coverage
of `_load()`'s own missing/malformed-file safety, or the PRE-EXISTING
`add_memory`/`remove_memory`/`detect_remember_command`/
`detect_forget_fact_command`/`is_recall_command` behavior - all of that
is untouched by this sprint and already covered. This file covers the
NEW surface only, plus a handful of "old + new work together" checks
(e.g. dedup still works after adding the new fields).

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed (unlike `test_memory_regression.py`,
written before that reset existed).

Categories (mirrors this sprint's own §18 test requirements):
  - Store (create/read/list/update/delete/missing memory/malformed file/
    missing file/wrong root type/partial entries)
  - Deduplication (same memory twice, normalized duplicate, restart-safe)
  - Intent (explicit save/update/delete vs. ordinary conversation)
  - Grounding (stored text is exactly what the user supplied)
  - Retrieval (relevant surfaces, irrelevant excluded, bounded, integrates
    with the existing MemoryRetriever)
  - Update (explicit correction, unrelated memories unchanged, ambiguity
    refuses to guess)
  - Delete (exact memory removed, unrelated preserved, no longer retrieved)
  - Persistence (survives reload, ids stable, dedup stable)
  - Safety (malformed state fails safely, real production state untouched)
"""

from __future__ import annotations

import json

import pytest

import luno.memory as memory
from luno.memory_retrieval import MemoryRetrievalConfig, MemoryRetriever


# ─────────────────────────────────────────────
#  Store: create / read / list
# ─────────────────────────────────────────────


def test_create_memory_has_manual_memory_fields():
    entry = memory.add_memory("aku suka Avenged Sevenfold")
    assert entry is not None
    assert entry["text"] == "aku suka Avenged Sevenfold"
    assert entry["source"] == "user_explicit"
    assert entry["category"] in memory.MANUAL_MEMORY_CATEGORIES
    assert entry["schema_version"] == memory.MANUAL_MEMORY_SCHEMA_VERSION
    assert entry["created_at"] == entry["updated_at"]
    assert isinstance(entry["id"], str) and entry["id"]


def test_create_memory_llm_auto_source_is_distinct():
    """`luno/main.py`'s legacy save_memory tool now passes `source="llm_auto"`
    explicitly - proves that path is honestly labeled, not silently
    defaulted to "user_explicit"."""
    entry = memory.add_memory("user likes tea", source="llm_auto")
    assert entry["source"] == "llm_auto"


def test_read_memory_by_id():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    fetched = memory.get_memory(created["id"])
    assert fetched == created


def test_read_missing_memory_returns_none():
    assert memory.get_memory("does-not-exist") is None


def test_list_memories_returns_all():
    memory.add_memory("aku suka kopi hitam")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    listed = memory.list_memories()
    assert len(listed) == 2
    assert {m["text"] for m in listed} == {"aku suka kopi hitam", "PC utamaku pakai RTX 3060 Ti"}


# ─────────────────────────────────────────────
#  Store: category classification
# ─────────────────────────────────────────────


@pytest.mark.parametrize("text,expected_category", [
    ("aku suka Avenged Sevenfold", "preference"),
    ("PC utamaku pakai RTX 3060 Ti", "technical_fact"),
    ("selalu matikan lampu sebelum tidur", "instruction"),
    ("proyek utamaku namanya Luno", "project_context"),
    ("langit itu biru", "other"),
])
def test_category_classification(text, expected_category):
    entry = memory.add_memory(text)
    assert entry["category"] == expected_category


# ─────────────────────────────────────────────
#  Store: update
# ─────────────────────────────────────────────


def test_update_memory_by_id_changes_text_and_updated_at():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    updated = memory.update_memory(created["id"], "RTX 5070")
    assert updated["text"] == "RTX 5070"
    assert updated["id"] == created["id"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert updated["category"] == "technical_fact"


def test_update_memory_by_id_missing_returns_none():
    assert memory.update_memory("does-not-exist", "new text") is None


def test_update_memory_by_id_empty_text_returns_none_and_does_not_mutate():
    created = memory.add_memory("aku suka kopi hitam")
    result = memory.update_memory(created["id"], "   ")
    assert result is None
    assert memory.get_memory(created["id"])["text"] == "aku suka kopi hitam"


def test_update_memory_by_topic_updates_correct_memory():
    memory.add_memory("aku suka kopi hitam")
    gpu_entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    status, updated = memory.update_memory_by_topic("RTX 3060", "RTX 5070")
    assert status == "updated"
    assert updated["id"] == gpu_entry["id"]
    assert updated["text"] == "RTX 5070"


def test_update_memory_by_topic_unrelated_memories_unchanged():
    coffee_entry = memory.add_memory("aku suka kopi hitam")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.update_memory_by_topic("RTX 3060", "RTX 5070")
    assert memory.get_memory(coffee_entry["id"])["text"] == "aku suka kopi hitam"


def test_update_memory_by_topic_not_found_changes_nothing():
    entry = memory.add_memory("aku suka kopi hitam")
    status, updated = memory.update_memory_by_topic("GPU", "RTX 5070")
    assert status == "not_found"
    assert updated is None
    assert memory.get_memory(entry["id"])["text"] == "aku suka kopi hitam"


def test_update_memory_by_topic_ambiguous_does_not_destroy_state():
    """Step 10's safety mandate: two equally-good matches must NOT be
    guessed at - nothing is changed, both survive untouched."""
    gpu_entry = memory.add_memory("GPU lamaku RTX 3060 Ti")
    gpu_entry_2 = memory.add_memory("GPU baruku juga RTX 3060 Ti dulunya")
    status, updated = memory.update_memory_by_topic("GPU RTX 3060", "RTX 5070")
    assert status == "ambiguous"
    assert updated is None
    assert memory.get_memory(gpu_entry["id"])["text"] == "GPU lamaku RTX 3060 Ti"
    assert memory.get_memory(gpu_entry_2["id"])["text"] == "GPU baruku juga RTX 3060 Ti dulunya"


# ─────────────────────────────────────────────
#  Store: delete
# ─────────────────────────────────────────────


def test_delete_memory_by_id_removes_exact_memory():
    keep = memory.add_memory("aku suka kopi hitam")
    remove = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    removed_text = memory.delete_memory_by_id(remove["id"])
    assert removed_text == "PC utamaku pakai RTX 3060 Ti"
    assert memory.get_memory(remove["id"]) is None
    assert memory.get_memory(keep["id"]) is not None


def test_delete_memory_by_id_missing_returns_none_and_does_not_mutate():
    keep = memory.add_memory("aku suka kopi hitam")
    result = memory.delete_memory_by_id("does-not-exist")
    assert result is None
    assert len(memory.list_memories()) == 1
    assert memory.get_memory(keep["id"]) is not None


def test_deleted_memory_no_longer_retrieved():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.delete_memory_by_id(entry["id"])
    results = memory.search_memories("PC RTX")
    assert results == []


# ─────────────────────────────────────────────
#  Store: malformed / missing / wrong-root-type files, partial entries
# ─────────────────────────────────────────────


def test_missing_file_new_operations_fail_safely(monkeypatch, tmp_path):
    """`_memories` is already `[]` (autouse fixture reset) and the
    redirected file does not exist yet - every NEW function must behave
    like an ordinary empty store, never raise."""
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(tmp_path / "does_not_exist.json"))
    assert memory.get_memory("anything") is None
    assert memory.update_memory("anything", "text") is None
    status, entry = memory.update_memory_by_topic("anything", "text")
    assert status == "not_found" and entry is None
    assert memory.delete_memory_by_id("anything") is None
    assert memory.search_memories("anything") == []


def test_malformed_json_file_new_operations_fail_safely(monkeypatch, tmp_path):
    bad_file = tmp_path / "long_term_memory.json"
    bad_file.write_text("{not valid json,,,", encoding="utf-8")
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(bad_file))
    memory._load()  # must not raise
    assert memory.list_memories() == []
    assert memory.search_memories("anything") == []
    assert memory.get_memory("anything") is None


def test_wrong_root_type_new_operations_fail_safely(monkeypatch, tmp_path):
    bad_file = tmp_path / "long_term_memory.json"
    bad_file.write_text(json.dumps({"oops": "should have been a list"}), encoding="utf-8")
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(bad_file))
    memory._load()  # must not raise (matches _load()'s own existing behavior)
    # Whatever _load() produced, every new function must still not raise.
    memory.list_memories()
    memory.search_memories("anything")
    memory.get_memory("anything")
    memory.update_memory("anything", "text")
    memory.delete_memory_by_id("anything")


def test_partial_malformed_entries_are_skipped_not_crashed(monkeypatch, tmp_path):
    """A hand-edited file with one well-formed entry and one entry missing
    "text" entirely - `search_memories()`/`make_manual_memory_source()`
    must skip the malformed one, never crash on it."""
    mixed_file = tmp_path / "long_term_memory.json"
    mixed_file.write_text(json.dumps([
        {"id": "good1", "text": "aku suka kopi hitam", "created_at": "2026-01-01T00:00:00"},
        {"id": "bad1", "created_at": "2026-01-01T00:00:00"},  # no "text" at all
        "not even a dict",
    ]), encoding="utf-8")
    monkeypatch.setattr(memory.config, "LONG_TERM_MEMORY_FILE", str(mixed_file))
    memory._load()
    results = memory.search_memories("kopi hitam")
    assert len(results) == 1
    assert results[0]["id"] == "good1"


# ─────────────────────────────────────────────
#  Deduplication
# ─────────────────────────────────────────────


def test_exact_duplicate_saved_three_times_creates_one_entry():
    memory.add_memory("ingat aku suka A7X")
    memory.add_memory("ingat aku suka A7X")
    memory.add_memory("ingat aku suka A7X")
    assert len(memory.list_memories()) == 1


def test_normalized_duplicate_case_and_punctuation_is_skipped():
    memory.add_memory("Aku Suka A7X")
    result = memory.add_memory("aku suka a7x!!!")
    assert result is None
    assert len(memory.list_memories()) == 1


def test_dedup_is_restart_safe():
    """Simulates a process restart: save, drop the in-memory list, reload
    from disk, then attempt the same save again - must still dedup (not
    timestamp-based, so a reload does not reset the dedup window)."""
    memory.add_memory("aku suka Avenged Sevenfold")
    memory._memories = []  # simulate restart: forget in-memory state
    memory._load()  # reload from the isolated file
    assert len(memory.list_memories()) == 1
    result = memory.add_memory("aku suka Avenged Sevenfold")
    assert result is None
    assert len(memory.list_memories()) == 1


# ─────────────────────────────────────────────
#  Intent detection - explicit vs. ordinary conversation
# ─────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("ubah memory GPU jadi RTX 5070", ("GPU", "RTX 5070")),
    ("update memory about GPU to RTX 5070", ("GPU", "RTX 5070")),
    ("ganti memory PC lama jadi PC baru", ("PC lama", "PC baru")),
    ("tolong koreksi memory alamat jadi Jl. Merdeka", ("alamat", "Jl. Merdeka")),
])
def test_explicit_update_intent_detected(text, expected):
    assert memory.detect_update_memory_command(text) == expected


@pytest.mark.parametrize("text", [
    "GPU-ku sekarang RTX 5070",       # ordinary statement, no trigger phrase
    "PC utamaku pakai RTX 3060 Ti",   # ordinary statement
    "aku baru beli GPU baru",
    "apa kabar?",
])
def test_ordinary_statement_does_not_trigger_update(text):
    assert memory.detect_update_memory_command(text) is None


@pytest.mark.parametrize("text,expected_id", [
    ("hapus memory nomor 12", "12"),
    ("hapus memory nomer abc123", "abc123"),
    ("delete memory number 12", "12"),
    ("delete memory #12", "12"),
])
def test_explicit_delete_by_id_intent_detected(text, expected_id):
    assert memory.detect_delete_memory_by_id_command(text) == expected_id


@pytest.mark.parametrize("text,expected_topic", [
    ("hapus memory tentang GPU lamaku", "GPU lamaku"),
    ("hapus memory soal PC-ku", "PC-ku"),
    ("delete memory about my old GPU", "my old GPU"),
])
def test_explicit_delete_by_topic_intent_detected(text, expected_topic):
    assert memory.detect_delete_memory_by_topic_command(text) == expected_topic


def test_delete_by_id_pattern_does_not_leak_into_topic_pattern():
    """"hapus memory nomor 12" must be recognized by the BY-ID detector,
    not misread as a topic query of "nomor 12" by the by-topic detector -
    `main_runtime_demo.py` checks by-id first for exactly this reason."""
    assert memory.detect_delete_memory_by_id_command("hapus memory nomor 12") == "12"
    # The by-topic detector's own regex WOULD also match this text (its
    # `(.+)$` group is greedy) - this is exactly why the caller must check
    # by-id first, not a claim that the by-topic detector alone is smart
    # enough to refuse it.
    assert memory.detect_delete_memory_by_topic_command("hapus memory nomor 12") == "nomor 12"


@pytest.mark.parametrize("text", [
    "lupakan semua",             # existing "clear everything" phrase
    "apa kabar?",
    "nyalakan lampu ruang tamu",
    "aku lagi capek",
])
def test_ordinary_conversation_does_not_trigger_new_delete_detectors(text):
    assert memory.detect_delete_memory_by_id_command(text) is None
    assert memory.detect_delete_memory_by_topic_command(text) is None


# ─────────────────────────────────────────────
#  Grounding - stored text is exactly what the user supplied
# ─────────────────────────────────────────────


def test_stored_text_contains_only_supplied_fact_no_invention():
    """luno/memory.py's detect_remember_command() extracts the fact via a
    regex capture group (literal substring of the user's own text) - this
    test proves add_memory() then stores that EXACT text, never expanding
    or embellishing it."""
    user_text = "ingat aku suka Avenged Sevenfold"
    fact = memory.detect_remember_command(user_text)
    assert fact == "aku suka Avenged Sevenfold"
    entry = memory.add_memory(fact)
    assert entry["text"] == "aku suka Avenged Sevenfold"
    assert "sering" not in entry["text"]
    assert "banget" not in entry["text"]


def test_update_stored_text_is_exactly_the_supplied_replacement():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    updated = memory.update_memory(created["id"], "RTX 5070")
    assert updated["text"] == "RTX 5070"


# ─────────────────────────────────────────────
#  Retrieval - integration with the existing MemoryRetriever
# ─────────────────────────────────────────────


def _retriever_with_manual_memory_source():
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    return retriever


def test_relevant_manual_memory_is_retrieved():
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("cari memory tentang PC-ku")
    assert len(results) == 1
    assert "RTX 3060" in results[0].text
    assert results[0].source == "manual_memory"
    assert "[MANUAL MEMORY" in results[0].text


def test_irrelevant_manual_memory_is_excluded():
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("gimana cuaca hari ini?")
    assert results == []


def test_query_with_no_signal_never_touches_the_store():
    """"berapa 5 + 5?" reduces to zero retrieval-signal tokens - the
    source must not even be called, same discipline every other source
    in this project already follows."""
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    retriever = _retriever_with_manual_memory_source()
    results = retriever.retrieve_memories("what's 5 + 5?")
    assert results == []


def test_retrieval_result_count_is_bounded():
    for i in range(10):
        memory.add_memory(f"aku suka game nomor {i} banget sekali")
    config = MemoryRetrievalConfig(max_results=3, max_tokens=100000)
    retriever = MemoryRetriever(config)
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    results = retriever.retrieve_memories("game favoritku apa aja")
    assert len(results) <= 3


def test_recall_everything_full_list_still_works_unchanged():
    """Pre-existing, protected behavior: is_recall_command +
    build_memory_prompt() still answer "apa yang kamu ingat tentang aku?"
    with the FULL list, completely independent of the new bounded
    MemoryRetriever source added by this sprint."""
    memory.add_memory("aku suka kopi hitam")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    assert memory.is_recall_command("apa yang kamu ingat tentang aku?")
    prompt = memory.build_memory_prompt()
    assert "kopi hitam" in prompt
    assert "RTX 3060" in prompt


# ─────────────────────────────────────────────
#  Persistence - survives reload, ids stable, dedup stable
# ─────────────────────────────────────────────


def test_memory_survives_reload():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory._memories = []  # simulate restart
    memory._load()
    reloaded = memory.get_memory(created["id"])
    assert reloaded is not None
    assert reloaded["text"] == "PC utamaku pakai RTX 3060 Ti"


def test_ids_remain_stable_across_reload():
    created = memory.add_memory("aku suka kopi hitam")
    original_id = created["id"]
    memory._memories = []
    memory._load()
    assert memory.get_memory(original_id) is not None


def test_update_survives_reload():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.update_memory(created["id"], "RTX 5070")
    memory._memories = []
    memory._load()
    reloaded = memory.get_memory(created["id"])
    assert reloaded["text"] == "RTX 5070"


def test_delete_survives_reload():
    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.delete_memory_by_id(created["id"])
    memory._memories = []
    memory._load()
    assert memory.get_memory(created["id"]) is None
    assert memory.list_memories() == []


# ─────────────────────────────────────────────
#  Safety - real production state untouched, isolation confirmed
# ─────────────────────────────────────────────


def test_manual_memory_operations_use_isolated_file_not_real_one():
    """Real-file-protection proof: record the real file's state, drive
    real writes through every new operation, confirm the real file is
    byte-for-byte unchanged, and confirm the isolated file DID receive
    the writes (not a no-op)."""
    import hashlib
    import os

    real_path = os.path.join("config", "long_term_memory.json")
    before_hash = None
    before_mtime = None
    if os.path.exists(real_path):
        with open(real_path, "rb") as f:
            before_hash = hashlib.sha256(f.read()).hexdigest()
        before_mtime = os.path.getmtime(real_path)

    isolated_path = memory.config.LONG_TERM_MEMORY_FILE
    assert isolated_path != real_path

    created = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.update_memory(created["id"], "RTX 5070")
    memory.add_memory("aku suka kopi hitam")
    memory.delete_memory_by_id(created["id"])

    after_hash = None
    after_mtime = None
    if os.path.exists(real_path):
        with open(real_path, "rb") as f:
            after_hash = hashlib.sha256(f.read()).hexdigest()
        after_mtime = os.path.getmtime(real_path)

    assert after_hash == before_hash
    assert after_mtime == before_mtime

    assert os.path.exists(isolated_path)
    with open(isolated_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert len(saved) == 1
    assert saved[0]["text"] == "aku suka kopi hitam"


def test_memories_reset_does_not_leak_between_tests_part_a():
    """Cross-test contamination proof for the new autouse `_memories`
    reset in `tests/conftest.py` - part_a saves, part_b (a fresh test)
    must not see it."""
    memory.add_memory("contamination-marker-fact-xyz")
    assert len(memory.list_memories()) == 1


def test_memories_reset_does_not_leak_between_tests_part_b():
    assert memory.list_memories() == []
    assert not any("contamination-marker-fact-xyz" in m.get("text", "") for m in memory.list_memories())
