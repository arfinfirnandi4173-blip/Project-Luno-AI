"""
test_memory_persistence_hardening.py
======================================

Memory Recovery & Persistence Hardening sprint - Phase 8's self-test
list, covering the backup/atomic-write mechanism and pytest-mutation
guard added to `luno/memory.py`'s `_save()`/`_load()` this sprint (see
`docs/change_impact/memory_recovery.md`), plus a regression guard over
the recovery artifacts themselves (`recovery/migrated_candidate.json`).

Every test here follows the SAME "monkeypatch `config.LONG_TERM_MEMORY_FILE`
to a `tmp_path`-derived location" convention already established by
`tests/test_memory_regression.py`/`tests/test_manual_memory.py` - on top
of `tests/conftest.py`'s own autouse `isolate_persistent_state` fixture,
which already redirects `LONG_TERM_MEMORY_FILE` (and resets
`memory._memories`) before every test in this file even starts. No test
in this file ever touches Vinn's real `config/long_term_memory.json`.
"""

from __future__ import annotations

import json
import os

import luno.memory as memory_module


def _save_state():
    return list(memory_module._memories)


def _restore_state(saved):
    memory_module._memories = saved


# ============================================================================
# 1/2. Backup is created before mutation; mutation succeeds and the
#      backup remains valid.
# ============================================================================

def test_backup_created_before_mutation_and_remains_valid(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps([{"id": "old1", "text": "old fact", "created_at": "2026-01-01T00:00:00"}]), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._memories = [{"id": "new1", "text": "new fact", "created_at": "2026-01-02T00:00:00", "schema_version": 4}]
        memory_module._save()

        backup_dir = tmp_path / "backups"
        assert backup_dir.is_dir()
        backups = sorted(backup_dir.glob("long_term_memory.*.json"))
        assert len(backups) == 1, f"expected exactly 1 backup, got {backups}"

        backup_content = json.loads(backups[0].read_text(encoding="utf-8"))
        assert backup_content == [{"id": "old1", "text": "old fact", "created_at": "2026-01-01T00:00:00"}], \
            "backup must hold the PRE-write content, not the new content"

        main_content = json.loads(target.read_text(encoding="utf-8"))
        assert main_content == memory_module._memories, "main file must hold the NEW content after a successful save"
    finally:
        _restore_state(saved)


# ============================================================================
# 3/5. Failed write leaves original production state intact; corrupted
#      new JSON does not replace valid JSON.
# ============================================================================

def test_failed_write_leaves_original_state_intact(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    original = [{"id": "keep1", "text": "must survive a failed save", "created_at": "2026-01-01T00:00:00"}]
    target.write_text(json.dumps(original), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._memories = [{"id": "new1", "text": "this write will fail", "created_at": "2026-01-02T00:00:00"}]

        def _boom(*args, **kwargs):
            raise OSError("simulated disk failure mid-write")

        monkeypatch.setattr(memory_module.json, "dump", _boom)
        memory_module._save()  # must not raise (caught and logged, per _save()'s own contract)

        # The ORIGINAL file content must be completely untouched - the
        # write-temp-then-replace design means a failure before the final
        # os.replace() can never leave the main file half-written.
        assert json.loads(target.read_text(encoding="utf-8")) == original

        # No leftover .tmp file either (best-effort cleanup on failure).
        leftovers = list(tmp_path.glob("long_term_memory.json.*.tmp"))
        assert leftovers == [], f"a temp file was left behind: {leftovers}"
    finally:
        _restore_state(saved)


# ============================================================================
# 4. Atomic replacement works (no partial/half-written file ever
#    observable on disk after a successful save).
# ============================================================================

def test_atomic_replace_leaves_no_temp_file_and_full_content_on_success(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._memories = [{"id": f"m{i}", "text": f"fact {i}", "created_at": "2026-01-01T00:00:00"} for i in range(50)]
        memory_module._save()

        assert target.exists()
        content = json.loads(target.read_text(encoding="utf-8"))
        assert len(content) == 50, "the full, complete new content must be present - never a partial write"
        leftovers = list(tmp_path.glob("long_term_memory.json.*.tmp"))
        assert leftovers == [], f"a temp file was left behind after a successful save: {leftovers}"
    finally:
        _restore_state(saved)


# ============================================================================
# 6. Restart/reload loads the latest valid state (recovers from backup
#    when the primary file is corrupted).
# ============================================================================

def test_reload_recovers_from_latest_backup_when_primary_is_corrupted(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))

        # Save #1: no prior file exists yet, so nothing gets backed up -
        # this just creates the initial main file.
        memory_module._memories = [{"id": "good1", "text": "the last known-good state", "created_at": "2026-01-01T00:00:00"}]
        memory_module._save()

        # Save #2: THIS is the save that backs up save #1's content
        # (the pre-write state) before overwriting the main file.
        memory_module._memories = [{"id": "good2", "text": "a second, later state", "created_at": "2026-01-02T00:00:00"}]
        memory_module._save()

        # Now corrupt the PRIMARY file directly (simulating hand-editing
        # gone wrong, or any other out-of-band corruption) - the backup
        # taken by save #2 (holding save #1's content) must still be
        # sitting in backups/.
        target.write_text("{not valid json at all,,,", encoding="utf-8")

        memory_module._load()  # must not raise
        assert memory_module._memories == [{"id": "good1", "text": "the last known-good state", "created_at": "2026-01-01T00:00:00"}], \
            "must recover the latest backed-up state, not fall back to an empty store"
    finally:
        _restore_state(saved)


def test_reload_falls_back_to_empty_when_no_backup_exists_either(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("{not valid json,,,", encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()  # must not raise
        assert memory_module._memories == [], "no backup exists - the only safe fallback is an empty store, exactly as before this sprint"
    finally:
        _restore_state(saved)


# ============================================================================
# 7. Backup rotation never removes all valid backups.
# ============================================================================

def test_backup_rotation_respects_retention_and_never_empties(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    saved = _save_state()
    saved_retention = memory_module._MEMORY_BACKUP_RETENTION
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._MEMORY_BACKUP_RETENTION = 3

        for i in range(10):
            memory_module._memories = [{"id": f"m{i}", "text": f"fact {i}", "created_at": "2026-01-01T00:00:00"}]
            memory_module._save()

        backups = sorted((tmp_path / "backups").glob("long_term_memory.*.json"))
        assert len(backups) == 3, f"expected retention to cap backups at 3, got {len(backups)}"

        # The retained backups must be the MOST RECENT ones (oldest
        # pruned first) - the newest backup's content should be one of
        # the later saves, not fact 0.
        contents = [json.loads(b.read_text(encoding="utf-8")) for b in backups]
        texts = {c[0]["text"] for c in contents}
        assert "fact 0" not in texts, "the oldest backup should have been pruned"

        # Never zero, even with a pathologically low/misconfigured retention.
        memory_module._MEMORY_BACKUP_RETENTION = 0
        memory_module._memories = [{"id": "final", "text": "final save", "created_at": "2026-01-01T00:00:00"}]
        memory_module._save()
        backups_after = sorted((tmp_path / "backups").glob("long_term_memory.*.json"))
        assert len(backups_after) >= 1, "retention must never prune away the last remaining backup"
    finally:
        _restore_state(saved)
        memory_module._MEMORY_BACKUP_RETENTION = saved_retention


# ============================================================================
# 8. Test fixtures never modify the production file (meta-test, same
#    pattern several existing memory test files already assert).
# ============================================================================

def test_this_test_files_isolated_path_is_never_the_real_production_file():
    import tempfile

    real_path = os.path.join("config", "long_term_memory.json")
    isolated_path = memory_module.config.LONG_TERM_MEMORY_FILE
    assert os.path.abspath(isolated_path) != os.path.abspath(real_path)
    # conftest.py's autouse isolate_persistent_state fixture redirects
    # every test in this file to a path under pytest's own tmp_path,
    # which is itself always under the system temp directory.
    assert os.path.abspath(isolated_path).startswith(os.path.abspath(tempfile.gettempdir()))


def test_pytest_guard_refuses_write_to_a_non_isolated_looking_path(monkeypatch, tmp_path):
    """The Phase 7 defense-in-depth guard - even though `PYTEST_CURRENT_TEST`
    is genuinely set right now (we ARE inside pytest), pointing
    `LONG_TERM_MEMORY_FILE` at a path that is NOT under the system temp
    directory must make `_save()` refuse loudly rather than silently
    write there."""
    import pytest

    not_a_tmp_path = os.path.join(os.path.abspath(os.sep), "definitely_not_a_pytest_tmp_dir", "long_term_memory.json")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", not_a_tmp_path)
        memory_module._memories = [{"id": "x", "text": "should never be written", "created_at": "2026-01-01T00:00:00"}]
        with pytest.raises(RuntimeError):
            memory_module._save()
        assert not os.path.exists(not_a_tmp_path), "the guard must fire BEFORE anything is written"
    finally:
        _restore_state(saved)


# ============================================================================
# 9/10. Recovery migration preserves all five historical memories and
#       does not fabricate metadata (regression guard over the actual
#       recovery artifacts produced by recovery/migrate_snapshot.py).
# ============================================================================

_RECOVERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recovery")

_FORBIDDEN_FABRICATED_FIELDS = {
    "importance", "usefulness_score", "positive_feedback_count", "negative_feedback_count",
    "retrieval_count", "last_retrieved_at", "retrieval_success_count", "retrieval_miss_count",
    "feedback_event_count", "correction_count", "conflict_event_count", "evaluation_score",
    "last_evaluated_at", "context_evidence", "conflict_status", "conflict_group", "updated_at",
}


def _load_recovery_json(name):
    path = os.path.join(_RECOVERY_DIR, name)
    if not os.path.exists(path):
        import pytest
        pytest.skip(f"{path} not present in this checkout")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_migrated_candidate_preserves_all_five_snapshot_memories_exactly():
    snapshot = _load_recovery_json("snapshot_2026-07-23.json")
    candidate = _load_recovery_json("migrated_candidate.json")

    assert len(snapshot) == 5
    assert len(candidate) == 5

    snap_by_id = {e["id"]: e["text"] for e in snapshot}
    cand_by_id = {e["id"]: e["text"] for e in candidate}
    assert snap_by_id.keys() == cand_by_id.keys(), "every snapshot id must be preserved"
    for mem_id, text in snap_by_id.items():
        assert cand_by_id[mem_id] == text, f"text for {mem_id!r} must match the snapshot exactly"

    snap_created = {e["id"]: e["created_at"] for e in snapshot}
    for e in candidate:
        assert e["created_at"] == snap_created[e["id"]], f"created_at for {e['id']!r} must be preserved from the snapshot"


def test_migrated_candidate_does_not_fabricate_metadata():
    candidate = _load_recovery_json("migrated_candidate.json")
    for e in candidate:
        present_forbidden = _FORBIDDEN_FABRICATED_FIELDS & set(e.keys())
        assert not present_forbidden, f"entry {e.get('id')!r} has fabricated field(s): {present_forbidden}"
        # schema_version/history/source are the only added fields, and
        # they must be the documented schema-default values, never a
        # historical claim.
        assert e["schema_version"] == 4
        assert e["history"] == []
        assert e["source"] == "user_explicit"


def test_migrated_candidate_loads_cleanly_and_computes_neutral_defaults_via_accessors(monkeypatch, tmp_path):
    """The omitted fields (importance/usefulness/evaluation/...) must
    behave EXACTLY like any other legacy/absent-field entry already
    does in this codebase - computed/defaulted on demand by the
    existing accessors, never crashing, never silently `None`."""
    candidate = _load_recovery_json("migrated_candidate.json")
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps(candidate), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert len(memory_module._memories) == 5
        for entry in memory_module._memories:
            importance = memory_module.get_memory_importance(entry)
            assert importance in memory_module.MEMORY_IMPORTANCE_LEVELS
            usefulness = memory_module.get_memory_usefulness(entry)
            assert usefulness == 0.5
            evaluation = memory_module.evaluate_memory(entry)
            # No interaction evidence exists for any recovered entry (no
            # feedback/retrieval/correction/conflict counters were ever
            # set - Phase 4's whole point), so `evaluate_memory()` must
            # report zero confidence either way. The score itself may be
            # exactly neutral (0.5) OR nudged down by the SAME small,
            # deterministic, evidence-free age-based staleness term
            # `evaluate_memory()` already applies to any sufficiently old
            # entry with zero evidence (a real, known fact - these
            # entries' `created_at` really is ~2-3 weeks old) - never
            # nudged by anything resembling fabricated feedback/
            # correction/conflict evidence.
            assert 0.4 <= evaluation["score"] <= 0.5
            # Confidence is 0.0 for a truly evidence-free entry, or the
            # single "stale by age" evidence unit's worth (0.08) - never
            # more, since no OTHER evidence source (feedback/retrieval/
            # correction/conflict) was ever recorded.
            assert evaluation["confidence"] in (0.0, 0.08)
            assert evaluation["strengths"] == []
            assert "positive confirmation" not in " ".join(evaluation["weaknesses"])
            assert "correction" not in " ".join(evaluation["weaknesses"])
            assert "negative feedback" not in " ".join(evaluation["weaknesses"])
            lifecycle = memory_module.compute_lifecycle(entry)
            assert lifecycle in ("active", "stale", "archived")
    finally:
        _restore_state(saved)


# ============================================================================
# Long-Term Memory Self-Healing / Recovery Hardening sprint
# ============================================================================
#
# Extends this SAME file (per the brief's own "extend the existing
# hardening test suite" instruction) with the deterministic recovery
# sequence added to `_load()`/`_save()`: primary valid -> load as-is;
# primary invalid -> newest-valid-backup wins; primary AND every backup
# invalid -> quarantine the corrupted primary (deferred to the next
# `_save()` - `_load()` itself stays 100% read-only, see `luno/memory.py`'s
# own "IMPORTANT ARCHITECTURAL CONSTRAINT" note) and continue with a
# fresh, empty store. Same isolation convention as every test above -
# `tmp_path`-derived paths only, never Vinn's real `config/long_term_
# memory.json`.

import shutil as _shutil


def _backups_dir(tmp_path):
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    return d


def _write_backup(tmp_path, content, ts):
    """Writes a synthetic backup file using the EXACT existing naming
    convention (`_memory_backup_filename()`'s own format), so
    `_list_memory_backups()`'s glob picks it up exactly like a real one
    `_backup_current_memory_file()` would have produced."""
    d = _backups_dir(tmp_path)
    path = d / f"long_term_memory.{ts}.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _reset_recovery_globals():
    """Test hygiene only - not a production reset mechanism. Ensures
    each test in this section starts from a clean, known baseline for
    the new module-level bookkeeping this sprint added, regardless of
    what an EARLIER test in this same process may have left behind
    (mirrors the same defensive spirit as `_save_state()`/`_restore_
    state()` above, extended to the two new globals)."""
    memory_module._persistence_status = {"status": "healthy", "detail": None}
    memory_module._pending_quarantine_path = None


# ----------------------------------------------------------------------
# 1. Valid primary loads normally
# ----------------------------------------------------------------------

def test_r1_valid_primary_loads_normally(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    good = [{"id": "m1", "text": "valid primary content", "created_at": "2026-01-01T00:00:00", "schema_version": 4}]
    target.write_text(json.dumps(good), encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == good
        assert memory_module.get_persistence_status()["status"] == "healthy"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 2. Missing primary creates a valid, fresh (empty) in-memory store
# ----------------------------------------------------------------------

def test_r2_missing_primary_creates_valid_fresh_memory(monkeypatch, tmp_path):
    target = tmp_path / "does_not_exist.json"
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        # Missing-file is a NORMAL empty start, not a corruption event -
        # status is "healthy", never "fresh_after_unrecoverable_corruption",
        # and _load() must never create the file as a side effect
        # (matches the pre-existing test_F in test_sprint63_long_term_
        # memory_recovery.py - not weakened here).
        assert memory_module.get_persistence_status()["status"] == "healthy"
        assert not target.exists()
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 3. Malformed primary JSON triggers recovery (no backup -> fresh)
# ----------------------------------------------------------------------

def test_r3_malformed_primary_json_triggers_recovery(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("{not valid json at all,,,", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()  # must not raise
        assert memory_module._memories == []
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 4. Valid newest backup is restored
# ----------------------------------------------------------------------

def test_r4_valid_newest_backup_is_restored(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted primary {{{", encoding="utf-8")
    older = [{"id": "old", "text": "older backup", "created_at": "2026-01-01T00:00:00"}]
    newer = [{"id": "new", "text": "newest backup - must win", "created_at": "2026-01-02T00:00:00"}]
    _write_backup(tmp_path, older, "20260101T000000000000")
    _write_backup(tmp_path, newer, "20260102T000000000000")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == newer
        status = memory_module.get_persistence_status()
        assert status["status"] == "recovered_from_backup"
        assert status["detail"] == "long_term_memory.20260102T000000000000.json"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 5. Newest backup corrupt + older backup valid -> older backup wins
# ----------------------------------------------------------------------

def test_r5_newest_backup_corrupt_older_backup_valid_is_restored(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted primary {{{", encoding="utf-8")
    older_valid = [{"id": "old", "text": "the only usable backup", "created_at": "2026-01-01T00:00:00"}]
    _write_backup(tmp_path, older_valid, "20260101T000000000000")
    _write_backup(tmp_path, "not valid json at all", "20260102T000000000000")  # newest, but corrupt
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == older_valid, "must skip the corrupt newest backup and fall through to the older valid one"
        status = memory_module.get_persistence_status()
        assert status["status"] == "recovered_from_backup"
        assert status["detail"] == "long_term_memory.20260101T000000000000.json"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 6. All backups corrupt -> fresh memory
# ----------------------------------------------------------------------

def test_r6_all_backups_corrupt_falls_back_to_fresh_memory(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted primary {{{", encoding="utf-8")
    _write_backup(tmp_path, "also not valid json", "20260101T000000000000")
    _write_backup(tmp_path, {"oops": "wrong root type too"}, "20260102T000000000000")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 7. No backups at all -> fresh memory
# ----------------------------------------------------------------------

def test_r7_no_backups_at_all_falls_back_to_fresh_memory(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted primary {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        assert not (tmp_path / "backups").exists()
        memory_module._load()
        assert memory_module._memories == []
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 8. Wrong root type (primary is valid JSON but not a list) -> recovery
# ----------------------------------------------------------------------

def test_r8_wrong_root_type_triggers_recovery(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps({"oops": "should have been a list"}), encoding="utf-8")
    good_backup = [{"id": "b1", "text": "from backup", "created_at": "2026-01-01T00:00:00"}]
    _write_backup(tmp_path, good_backup, "20260101T000000000000")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()  # must not raise - matches the pre-existing
        # test_wrong_root_type_new_operations_fail_safely in
        # tests/test_manual_memory.py, now additionally proven to
        # actually recover real content when a backup is available
        # rather than merely "not crashing."
        assert memory_module._memories == good_backup
        assert memory_module.get_persistence_status()["status"] == "recovered_from_backup"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


def test_r8b_wrong_root_type_no_backup_falls_back_to_fresh_empty_list(monkeypatch, tmp_path):
    """Same wrong-root-type primary as above, but with NO backup at all -
    must fall back to a clean empty list (never the invalid dict itself),
    unlike `_load()`'s OWN pre-hardening behavior which would have left
    `_memories` as the raw, wrong-shaped dict."""
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps({"oops": "should have been a list"}), encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 9. Root-level schema mismatch (an entirely different envelope shape,
#    not the flat list this format actually uses) -> recovery
# ----------------------------------------------------------------------

def test_r9_root_level_schema_mismatch_triggers_recovery(monkeypatch, tmp_path):
    """`config/long_term_memory.json`'s real, documented, production
    format IS a flat JSON list (`MANUAL_MEMORY_SCHEMA_VERSION` lives
    PER-ENTRY, not as a root-level envelope field) - a versioned-envelope
    shape like `{"schema_version": 99, "entries": [...]}` is a genuine
    schema mismatch at the ROOT level, not merely "an old/new per-entry
    schema_version," and must be treated exactly like any other wrong
    root type."""
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps({"schema_version": 99, "entries": [{"id": "x", "text": "y"}]}), encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 10. Invalid individual memory entry - a DELIBERATE, documented
#     boundary (NOT full-file recovery), plus the corresponding
#     backup-side guarantee.
# ----------------------------------------------------------------------

def test_r10_individual_malformed_entry_in_primary_does_not_evict_good_sibling(monkeypatch, tmp_path):
    """This is the SAME scenario `tests/test_manual_memory.py::test_
    partial_malformed_entries_are_skipped_not_crashed` already covers
    (and this sprint MUST NOT regress): a hand-edited PRIMARY file with
    one well-formed entry and one malformed one (missing "text", or not
    even a dict). `_validate_memory_data()` deliberately validates ROOT
    SHAPE ONLY (see its own docstring in `luno/memory.py`) - it does
    NOT reject the whole file merely because one entry is individually
    malformed, because doing so would silently discard the still-good
    sibling entry, which is a WORSE outcome than the existing, already-
    tested behavior of loading the list as-is and letting the
    established per-entry defensive filters (`search_memories()`, etc.)
    skip the bad one at the point of use."""
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps([
        {"id": "good1", "text": "a perfectly good memory", "created_at": "2026-01-01T00:00:00"},
        {"id": "bad1", "created_at": "2026-01-01T00:00:00"},  # no "text" at all
        "not even a dict",
    ]), encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module.get_persistence_status()["status"] == "healthy", (
            "a list with SOME malformed entries is still a structurally valid "
            "primary - only a non-list root should ever trigger recovery"
        )
        ids = [m["id"] for m in memory_module._memories if isinstance(m, dict)]
        assert "good1" in ids, "the well-formed sibling entry must never be silently discarded"
        results = memory_module.search_memories("perfectly good memory")
        assert len(results) == 1 and results[0]["id"] == "good1"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


def test_r10b_backup_candidate_with_wrong_root_type_is_rejected_in_favor_of_an_older_valid_one(monkeypatch, tmp_path):
    """The genuinely enforceable half of "invalid recovery source is
    rejected": a BACKUP candidate (never hand-edited in normal
    operation, unlike the primary) that fails the SAME root-shape
    contract (`_validate_memory_data()`) is skipped during the newest-
    first scan, exactly like a syntactically-invalid one already is."""
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted primary {{{", encoding="utf-8")
    older_valid = [{"id": "old", "text": "the only usable backup", "created_at": "2026-01-01T00:00:00"}]
    _write_backup(tmp_path, older_valid, "20260101T000000000000")
    _write_backup(tmp_path, {"wrong": "root type"}, "20260102T000000000000")  # newest, wrong shape
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == older_valid
        assert memory_module.get_persistence_status()["detail"] == "long_term_memory.20260101T000000000000.json"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 11. Corrupted primary is preserved/quarantined (never simply
#     overwritten and silently destroyed)
# ----------------------------------------------------------------------

def test_r11_corrupted_primary_is_quarantined_on_the_next_save(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    corrupted_bytes = b"\xff\xfe\x00corrupted binary garbage, not json at all"
    target.write_bytes(corrupted_bytes)
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"

        # _load() itself must NEVER touch the primary file - proven the
        # same way tests/test_sprint63_long_term_memory_recovery.py::
        # test_K_failed_load_never_writes_to_primary_path already proves
        # for the pre-existing behavior.
        assert target.read_bytes() == corrupted_bytes
        quarantine_dir = tmp_path / "quarantine"
        assert not quarantine_dir.exists(), "_load() must never write a quarantine artifact itself"

        # The quarantine only happens on the NEXT _save() - simulate the
        # user adding a new memory (the realistic trigger).
        memory_module._memories.append(
            {"id": "new1", "text": "first fact after recovery", "created_at": "2026-01-01T00:00:00", "schema_version": 4}
        )
        memory_module._save()

        assert quarantine_dir.is_dir()
        quarantined = sorted(quarantine_dir.glob("long_term_memory.corrupt.*.json"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == corrupted_bytes, "the quarantine artifact must preserve the corrupted bytes exactly"

        # The primary path now holds the NEW, valid content - never the
        # corrupted bytes, never silently left in place.
        assert json.loads(target.read_text(encoding="utf-8")) == memory_module._memories
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 12. Quarantine never overwrites a previous quarantine artifact
# ----------------------------------------------------------------------

def test_r12_quarantine_does_not_overwrite_a_previous_quarantine_artifact(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        quarantine_dir = tmp_path / "quarantine"
        quarantine_dir.mkdir()
        # Pre-seed a quarantine artifact whose name will collide with the
        # one `_finalize_pending_quarantine_if_any()` is about to compute,
        # by monkeypatching `datetime.now()` deterministically.
        import datetime as _dt

        class _FixedDatetime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime(2026, 1, 1, 0, 0, 0, 0)

        monkeypatch.setattr(memory_module, "datetime", _FixedDatetime)
        expected_name = "long_term_memory.corrupt.20260101T000000000000.json"
        (quarantine_dir / expected_name).write_text("PRE-EXISTING - must survive untouched", encoding="utf-8")

        memory_module._load()
        memory_module._save()

        # The pre-existing artifact must be completely untouched...
        assert (quarantine_dir / expected_name).read_text(encoding="utf-8") == "PRE-EXISTING - must survive untouched"
        # ...and the NEW quarantine copy must have landed under a
        # disambiguated name instead of silently overwriting it.
        disambiguated = quarantine_dir / f"{expected_name}.1"
        assert disambiguated.exists()
        assert disambiguated.read_text(encoding="utf-8") == "corrupted {{{"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 13/14. Recovered memory preserves original IDs and metadata
# ----------------------------------------------------------------------

def test_r13_recovered_memory_preserves_original_ids_and_metadata(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    rich_backup = [
        {
            "id": "rich1", "text": "a fact with full metadata", "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00", "category": "technical_fact", "importance": 3,
            "history": [{"text": "an older wording", "reason": "correction", "at": "2026-01-01T12:00:00"}],
            "source": "user_explicit", "schema_version": 4, "usefulness_score": 0.75,
            "positive_feedback_count": 2, "negative_feedback_count": 0,
        },
    ]
    _write_backup(tmp_path, rich_backup, "20260101T000000000000")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        # Recovered AS-IS - never re-ranked, never re-stamped, never
        # missing a single field the backup actually had.
        assert memory_module._memories == rich_backup
        recovered = memory_module._memories[0]
        assert recovered["id"] == "rich1"
        assert recovered["importance"] == 3
        assert recovered["history"] == rich_backup[0]["history"]
        assert recovered["usefulness_score"] == 0.75
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 15. Fresh memory uses the exact existing production schema
# ----------------------------------------------------------------------

def test_r15_fresh_memory_uses_the_exact_existing_production_schema(monkeypatch, tmp_path):
    """The fresh store IS the existing schema's own empty-state
    representation - a plain JSON list, `[]` - never a new envelope, a
    version marker, or any wrapper structure the existing loader
    (`_load()`) wouldn't already understand natively."""
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        memory_module._save()
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert on_disk == [], "the fresh store must be a bare empty list - the existing production schema's own empty state"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 16. Fresh memory can immediately accept a new memory
# ----------------------------------------------------------------------

def test_r16_fresh_memory_can_immediately_accept_a_new_memory(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module._memories == []
        entry = memory_module.add_memory("aku suka teh hijau")  # calls _save() internally
        assert entry is not None
        assert entry["text"] == "aku suka teh hijau"
        assert len(memory_module._memories) == 1
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert len(on_disk) == 1 and on_disk[0]["text"] == "aku suka teh hijau"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 17. Fresh memory survives restart
# ----------------------------------------------------------------------

def test_r17_fresh_memory_survives_restart(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        memory_module.add_memory("a fact added right after unrecoverable corruption")  # triggers the quarantine+persist
        # Simulate a full restart: forget in-memory state, reload from disk.
        memory_module._memories = ["sentinel-must-be-replaced"]
        memory_module._load()
        assert len(memory_module._memories) == 1
        assert memory_module._memories[0]["text"] == "a fact added right after unrecoverable corruption"
        assert memory_module.get_persistence_status()["status"] == "healthy", (
            "after the fresh store has actually been persisted once, a subsequent "
            "restart must load it as a completely normal, healthy primary file"
        )
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 18/19. Recovery status is correctly reported / normal startup reports
#        healthy status
# ----------------------------------------------------------------------

def test_r18_recovery_status_is_correctly_reported_for_every_branch(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))

        # healthy (valid primary)
        target.write_text(json.dumps([{"id": "a", "text": "x", "created_at": "2026-01-01T00:00:00"}]), encoding="utf-8")
        memory_module._load()
        assert memory_module.get_persistence_status() == {"status": "healthy", "detail": None}

        # recovered_from_backup
        target.write_text("corrupt {{{", encoding="utf-8")
        _write_backup(tmp_path, [{"id": "b", "text": "y", "created_at": "2026-01-01T00:00:00"}], "20260101T000000000000")
        memory_module._load()
        status = memory_module.get_persistence_status()
        assert status["status"] == "recovered_from_backup"
        assert status["detail"] == "long_term_memory.20260101T000000000000.json"

        # fresh_after_unrecoverable_corruption
        _shutil.rmtree(tmp_path / "backups")
        target.write_text("corrupt {{{", encoding="utf-8")
        memory_module._load()
        status = memory_module.get_persistence_status()
        assert status["status"] == "fresh_after_unrecoverable_corruption"
        assert status["detail"]
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


def test_r19_normal_startup_reports_healthy_status(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"  # does not exist yet
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))
        memory_module._load()
        assert memory_module.get_persistence_status()["status"] == "healthy"

        # get_persistence_status() must return a COPY - mutating the
        # returned dict must never affect this module's own bookkeeping.
        snap = memory_module.get_persistence_status()
        snap["status"] = "tampered"
        assert memory_module.get_persistence_status()["status"] == "healthy"

        # Also surfaced through the existing memory_health_report()
        # passthrough - no second status model, no new dashboard page.
        report = memory_module.memory_health_report()
        assert report["persistence_status"]["status"] == "healthy"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 20/21/22. Recovery does not modify Verified Facts / Episodic Memory /
#           Relationship State
# ----------------------------------------------------------------------

def test_r20_r21_r22_recovery_does_not_touch_other_persistent_stores(monkeypatch, tmp_path):
    from luno import config as luno_config

    verified_facts_path = tmp_path / "verified_facts.json"
    episodic_memory_path = tmp_path / "episodic_memory.json"
    relationship_state_path = tmp_path / "relationship_state.json"
    verified_facts_path.write_text(json.dumps([{"fact": "untouched"}]), encoding="utf-8")
    episodic_memory_path.write_text(json.dumps([{"event": "untouched"}]), encoding="utf-8")
    relationship_state_path.write_text(json.dumps({"trust": "untouched"}), encoding="utf-8")

    before = {
        "verified_facts": verified_facts_path.read_bytes(),
        "episodic_memory": episodic_memory_path.read_bytes(),
        "relationship_state": relationship_state_path.read_bytes(),
    }

    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
        monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(verified_facts_path), raising=False)
        monkeypatch.setattr(luno_config, "EPISODIC_MEMORY_FILE", str(episodic_memory_path), raising=False)
        monkeypatch.setattr(luno_config, "RELATIONSHIP_STATE_FILE", str(relationship_state_path), raising=False)

        # Full recovery cycle: unrecoverable corruption, then a save
        # that finalizes the quarantine + fresh persist.
        memory_module._load()
        memory_module.add_memory("post-recovery fact")

        assert verified_facts_path.read_bytes() == before["verified_facts"]
        assert episodic_memory_path.read_bytes() == before["episodic_memory"]
        assert relationship_state_path.read_bytes() == before["relationship_state"]
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# 26. Concurrent/repeated recovery does not corrupt the resulting file
# ----------------------------------------------------------------------

def test_r26_repeated_recovery_and_save_does_not_corrupt_the_resulting_file(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text("corrupted {{{", encoding="utf-8")
    saved = _save_state()
    _reset_recovery_globals()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(target))

        # Repeated _load() calls (simulating overlapping/repeated
        # startup attempts) - each is idempotent and never itself writes.
        for _ in range(3):
            memory_module._load()
            assert memory_module._memories == []
            assert memory_module.get_persistence_status()["status"] == "fresh_after_unrecoverable_corruption"
        assert target.read_text(encoding="utf-8") == "corrupted {{{", "repeated _load() must never touch the primary file"

        # Repeated _save() calls afterward - only the FIRST one has a
        # pending quarantine to finalize; none of them may ever produce
        # a half-written or invalid primary file.
        for i in range(3):
            memory_module._memories.append(
                {"id": f"m{i}", "text": f"fact {i}", "created_at": "2026-01-01T00:00:00", "schema_version": 4}
            )
            memory_module._save()
            on_disk = json.loads(target.read_text(encoding="utf-8"))
            assert on_disk == memory_module._memories, f"iteration {i}: primary file must always be fully valid JSON matching in-memory state"

        quarantine_dir = tmp_path / "quarantine"
        quarantined = list(quarantine_dir.glob("long_term_memory.corrupt.*.json"))
        assert len(quarantined) == 1, "only the FIRST save should have quarantined anything - later saves have no pending quarantine"
    finally:
        _restore_state(saved)
        _reset_recovery_globals()


# ----------------------------------------------------------------------
# Meta - this sprint's own function inventory still present (mirrors
# tests/test_sprint63_long_term_memory_recovery.py::test_O's own
# convention for the ORIGINAL hardening functions, extended to the new
# ones this sprint added).
# ----------------------------------------------------------------------

def test_r_new_recovery_functions_are_present_and_named_as_documented():
    for fn_name in (
        "_validate_memory_data", "get_persistence_status",
        "_recover_from_backup_or_go_fresh", "_finalize_pending_quarantine_if_any",
        "_memory_quarantine_dir", "_memory_quarantine_filename",
        # Pre-existing functions this sprint reuses/extends - must still
        # be present, unrenamed (mirrors test_sprint63's own test_O).
        "_backup_current_memory_file", "_list_memory_backups", "_prune_memory_backups",
        "_atomic_write_json", "_load_latest_valid_backup", "_load", "_save",
    ):
        assert hasattr(memory_module, fn_name), f"expected luno.memory.{fn_name} to still exist"
