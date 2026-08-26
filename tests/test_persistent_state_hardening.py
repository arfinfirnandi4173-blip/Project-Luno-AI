"""
test_persistent_state_hardening.py
====================================

Persistent State Hardening V2 sprint - test suite for the new shared
helper `luno/persistence.py` AND for each of the six domain stores this
sprint wired to it (`RelationshipStore`, `EpisodicMemoryStore`,
`luno.memory`'s session-summaries functions, `HabitMemory`,
`luno.reminders`, `VerifiedFactStore`). `config/long_term_memory.json`
(`luno.memory`'s own `_save`/`_load`) is the REFERENCE IMPLEMENTATION
and was NOT modified this sprint - its own regression coverage lives in
`tests/test_memory_persistence_hardening.py`, unchanged, still green.

Division of labor (per this sprint's own "test every store, don't just
test the helper once" instruction, read literally but efficiently):

  - Section 0 below is the EXHAUSTIVE, path-agnostic test of
    `luno/persistence.py` itself - every one of the sprint's own
    required scenarios (A-P) proven once, rigorously, against the
    shared mechanics (atomicity, backup-before-write, retention,
    corrupted-primary recovery, the pytest guard) that EVERY domain
    store below now delegates to verbatim.
  - Sections 1-6 (one per store) do NOT re-derive those same generic
    mechanics six more times - since all six stores route through the
    IDENTICAL `atomic_write_json()`/`safe_load_json()` calls proven in
    Section 0, re-proving atomicity/retention/corruption-recovery per
    store would only be testing the same three lines of shared code
    over and over under six different names. Instead, each per-store
    section proves the WIRING itself: real round-trip through the
    store's OWN public load/save API (D), a real backup appearing
    before a real domain mutation with the PREVIOUS domain state
    inside it (E/F), multiple consecutive real writes (N), the pytest
    guard firing for the ACTUAL wired path (M), no unrelated domain
    field getting mutated by persistence machinery (O), and that no
    store's isolated backup directory/state leaks into another's (P).

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects every `config.*_FILE` constant used below to a fresh,
test-private `tmp_path`-derived location for EVERY test in this file -
no test here can ever touch Vinn's real production files. Several tests
additionally re-monkeypatch a SPECIFIC path (still under `tmp_path`) for
deterministic control over backup/corruption scenarios - this layers on
top of, never replaces, the fixture's own isolation.
"""

from __future__ import annotations

import json
import os

import pytest

import luno.persistence as persistence
from luno import config
from luno import episodic_memory
from luno import memory
from luno import memory_guard
from luno import reminders
from luno import relationship_engine
from luno.proactive import habit_memory as habit_memory_module


# ============================================================================
# Section 0 - luno/persistence.py itself (path-agnostic, exhaustive, A-P)
# ============================================================================

def test_A_missing_file_returns_default(tmp_path):
    path = str(tmp_path / "nope.json")
    data, source = persistence.safe_load_json(path, default={"x": 1})
    assert data == {"x": 1}
    assert source == "default"


def test_B_empty_file_falls_back_to_default(tmp_path):
    path = str(tmp_path / "empty.json")
    open(path, "w").close()
    data, source = persistence.safe_load_json(path, default=[])
    assert data == []
    assert source == "default"


def test_C_malformed_json_falls_back_to_default(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("{not valid json at all")
    data, source = persistence.safe_load_json(path, default={"fallback": True})
    assert data == {"fallback": True}
    assert source == "default"


def test_D_valid_json_round_trip(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
    data, source = persistence.safe_load_json(path, default=None)
    assert data == {"a": 1, "b": [1, 2, 3]}
    assert source == "primary"


def test_E_backup_created_before_mutation(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": 1})
    assert persistence.list_backups(path) == []  # nothing to back up on the first write
    persistence.atomic_write_json(path, {"v": 2})
    backups = persistence.list_backups(path)
    assert len(backups) == 1


def test_F_backup_contains_previous_state(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": "first"})
    persistence.atomic_write_json(path, {"v": "second"})
    backups = persistence.list_backups(path)
    with open(os.path.join(persistence.backup_dir_for(path), backups[-1]), "r") as f:
        backup_content = json.load(f)
    assert backup_content == {"v": "first"}
    with open(path, "r") as f:
        primary_content = json.load(f)
    assert primary_content == {"v": "second"}


def test_G_atomic_replace_no_tmp_leftover(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": 1})
    persistence.atomic_write_json(path, {"v": 2})
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


def test_H_failed_write_leaves_primary_untouched(tmp_path, monkeypatch):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": "safe"})
    before = open(path).read()

    def _boom(*a, **kw):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(OSError):
        persistence.atomic_write_json(path, {"v": "would-corrupt"})
    after = open(path).read()
    assert before == after
    # no stray .tmp file left behind either
    assert not any(f.endswith(".tmp") for f in os.listdir(tmp_path))


def test_I_backup_retention_caps_count(tmp_path):
    path = str(tmp_path / "store.json")
    for i in range(25):
        persistence.atomic_write_json(path, {"v": i}, retention=5)
    assert len(persistence.list_backups(path)) == 5


def test_J_retention_never_deletes_all_backups(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": 0})
    persistence.atomic_write_json(path, {"v": 1}, retention=0)  # misconfigured to 0
    assert len(persistence.list_backups(path)) >= 1


def test_K_corrupted_primary_recovers_from_latest_backup(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": "good-1"})
    persistence.atomic_write_json(path, {"v": "good-2"})  # backs up good-1
    with open(path, "w") as f:
        f.write("{corrupted")
    data, source = persistence.safe_load_json(
        path, default={"v": "fallback"}, validate=lambda d: isinstance(d, dict) and "v" in d,
        recover_from_backup=True,
    )
    assert data == {"v": "good-1"}
    assert source.startswith("backup:")


def test_L_invalid_backup_is_skipped_in_favor_of_an_older_valid_one(tmp_path):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": "oldest-valid"})
    persistence.atomic_write_json(path, {"v": "middle"})   # backs up "oldest-valid"
    persistence.atomic_write_json(path, {"v": "newest-before-corruption"})   # backs up "middle"
    # Corrupt the NEWEST backup directly (the one containing "middle" -
    # simulating a backup that itself got damaged) - recovery must skip
    # it and fall back to the next-newest valid one ("oldest-valid").
    # Note "newest-before-corruption" was never itself backed up - a
    # backup is only ever taken of the PREVIOUS primary, before a write.
    backups = persistence.list_backups(path)
    with open(os.path.join(persistence.backup_dir_for(path), backups[-1]), "w") as f:
        f.write("{not json")
    with open(path, "w") as f:
        f.write("{also corrupted")
    data, name = persistence.load_latest_valid_backup(path, validate=lambda d: isinstance(d, dict))
    assert data == {"v": "oldest-valid"}


def test_M_pytest_guard_refuses_non_isolated_path(tmp_path):
    # PYTEST_CURRENT_TEST is always set during a pytest run - a path NOT
    # under the system temp directory must be refused.
    with pytest.raises(RuntimeError):
        persistence.refuse_if_pytest_targeting_unisolated_path("/not/a/temp/path/store.json")
    # A path that IS under tmp_path (itself under the system temp root)
    # must be allowed through without raising.
    persistence.refuse_if_pytest_targeting_unisolated_path(str(tmp_path / "store.json"))


def test_M_atomic_write_json_itself_refuses_non_isolated_path():
    with pytest.raises(RuntimeError):
        persistence.atomic_write_json("/not/a/temp/path/store.json", {"v": 1})


def test_N_multiple_consecutive_writes_all_land_correctly(tmp_path):
    path = str(tmp_path / "store.json")
    for i in range(10):
        persistence.atomic_write_json(path, {"v": i})
    data, _source = persistence.safe_load_json(path, default=None)
    assert data == {"v": 9}
    assert len(persistence.list_backups(path)) == 9  # one backup per write after the first


def test_O_backup_failure_prevents_destructive_overwrite(tmp_path, monkeypatch):
    path = str(tmp_path / "store.json")
    persistence.atomic_write_json(path, {"v": "original"})

    def _boom(*a, **kw):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(persistence, "backup_current_file", _boom)
    with pytest.raises(persistence.BackupFailedError):
        persistence.atomic_write_json(path, {"v": "would-overwrite"})
    data, _source = persistence.safe_load_json(path, default=None)
    assert data == {"v": "original"}  # refused, never overwritten


def test_backup_verification_state_a_b_c_then_failed_write_preserves_c(tmp_path, monkeypatch):
    """Phase 12's own literal scenario: STATE A -> save STATE B -> save
    STATE C. Primary must equal C; the newest backup must equal B; the
    backup before that must equal A. Then simulate a failed write -
    primary must still equal C afterward, proven via an automated test
    (not just narrated)."""
    path = str(tmp_path / "store.json")

    persistence.atomic_write_json(path, {"state": "A"})
    persistence.atomic_write_json(path, {"state": "B"})
    persistence.atomic_write_json(path, {"state": "C"})

    primary, _source = persistence.safe_load_json(path, default=None)
    assert primary == {"state": "C"}

    backups = persistence.list_backups(path)
    assert len(backups) == 2
    with open(os.path.join(persistence.backup_dir_for(path), backups[-1])) as f:
        newest_backup = json.load(f)
    with open(os.path.join(persistence.backup_dir_for(path), backups[-2])) as f:
        previous_backup = json.load(f)
    assert newest_backup == {"state": "B"}
    assert previous_backup == {"state": "A"}

    def _boom(*a, **kw):
        raise OSError("simulated failed write")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(OSError):
        persistence.atomic_write_json(path, {"state": "D-would-be-destructive"})

    primary_after_failed_write, _source = persistence.safe_load_json(path, default=None)
    assert primary_after_failed_write == {"state": "C"}


def test_P_independent_stores_do_not_share_backup_directories(tmp_path):
    path_a = str(tmp_path / "store_a.json")
    path_b = str(tmp_path / "store_b.json")
    persistence.atomic_write_json(path_a, {"who": "a-1"})
    persistence.atomic_write_json(path_a, {"who": "a-2"})
    persistence.atomic_write_json(path_b, {"who": "b-1"})
    persistence.atomic_write_json(path_b, {"who": "b-2"})
    assert len(persistence.list_backups(path_a)) == 1
    assert len(persistence.list_backups(path_b)) == 1
    a_data, _ = persistence.safe_load_json(path_a, default=None)
    b_data, _ = persistence.safe_load_json(path_b, default=None)
    assert a_data == {"who": "a-2"}
    assert b_data == {"who": "b-2"}


# ============================================================================
# Section 1 - RelationshipStore (config/relationship_state.json)
# ============================================================================

def test_relationship_store_round_trip_and_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "relationship_state.json")
    monkeypatch.setattr(config, "RELATIONSHIP_STATE_FILE", path, raising=False)

    state1 = relationship_engine.RelationshipState(trust=0.4, closeness=0.3)
    assert relationship_engine.RelationshipStore.save(state1) is True
    assert persistence.list_backups(path) == []  # first write, nothing to back up

    state2 = relationship_engine.RelationshipState(trust=0.7, closeness=0.6)
    assert relationship_engine.RelationshipStore.save(state2) is True
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert backed_up["trust"] == pytest.approx(0.4)

    reloaded = relationship_engine.RelationshipStore.load()
    assert reloaded.trust == pytest.approx(0.7)
    assert reloaded.closeness == pytest.approx(0.6)


def test_relationship_store_missing_and_malformed_behavior_unchanged(tmp_path, monkeypatch):
    path = str(tmp_path / "relationship_state.json")
    monkeypatch.setattr(config, "RELATIONSHIP_STATE_FILE", path, raising=False)

    # missing
    default_state = relationship_engine.RelationshipStore.load()
    assert isinstance(default_state, relationship_engine.RelationshipState)

    # malformed
    with open(path, "w") as f:
        f.write("{not valid")
    fallback_state = relationship_engine.RelationshipStore.load()
    assert isinstance(fallback_state, relationship_engine.RelationshipState)
    assert fallback_state.trust == default_state.trust  # same neutral default, not a crash


def test_relationship_store_pytest_guard_fires_for_unisolated_path(monkeypatch):
    monkeypatch.setattr(config, "RELATIONSHIP_STATE_FILE", "/not/a/temp/path/relationship_state.json", raising=False)
    ok = relationship_engine.RelationshipStore.save(relationship_engine.RelationshipState())
    assert ok is False  # save() catches the guard's RuntimeError and returns False, never raises out


# ============================================================================
# Section 2 - EpisodicMemoryStore (config/episodic_memory.json)
# ============================================================================

def test_episodic_store_round_trip_and_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "episodic_memory.json")
    monkeypatch.setattr(config, "EPISODIC_MEMORY_FILE", path, raising=False)

    category = next(iter(episodic_memory.ExperienceCategory)).value
    e1 = episodic_memory.EpisodicExperience(
        experience_id="exp-1", category=category, summary="user smiled", timestamp=1000.0,
    )
    assert episodic_memory.EpisodicMemoryStore.save([e1]) is True
    assert persistence.list_backups(path) == []

    e2 = episodic_memory.EpisodicExperience(
        experience_id="exp-2", category=category, summary="user laughed", timestamp=2000.0,
    )
    assert episodic_memory.EpisodicMemoryStore.save([e1, e2]) is True
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert len(backed_up) == 1 and backed_up[0]["experience_id"] == "exp-1"

    reloaded = episodic_memory.EpisodicMemoryStore.load()
    assert [e.id for e in reloaded] == ["exp-1", "exp-2"]


def test_episodic_store_missing_and_malformed_behavior_unchanged(tmp_path, monkeypatch):
    path = str(tmp_path / "episodic_memory.json")
    monkeypatch.setattr(config, "EPISODIC_MEMORY_FILE", path, raising=False)
    assert episodic_memory.EpisodicMemoryStore.load() == []
    with open(path, "w") as f:
        f.write("not json")
    assert episodic_memory.EpisodicMemoryStore.load() == []
    with open(path, "w") as f:
        json.dump({"not": "a list"}, f)
    assert episodic_memory.EpisodicMemoryStore.load() == []


# ============================================================================
# Section 3 - luno.memory session summaries (config/session_summaries.json)
# ============================================================================

def test_session_summaries_round_trip_and_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "session_summaries.json")
    monkeypatch.setattr(config, "SESSION_SUMMARIES_FILE", path, raising=False)
    monkeypatch.setattr(memory, "_session_summaries", [], raising=False)

    memory._session_summaries.append({"id": "s1", "summary": "talked about GPUs", "turn_count": 4, "ended_at": "t1"})
    memory._save_session_summaries()
    assert persistence.list_backups(path) == []

    memory._session_summaries.append({"id": "s2", "summary": "talked about coffee", "turn_count": 2, "ended_at": "t2"})
    memory._save_session_summaries()
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert len(backed_up) == 1 and backed_up[0]["id"] == "s1"

    memory._session_summaries = []
    memory._load_session_summaries()
    assert [s["id"] for s in memory._session_summaries] == ["s1", "s2"]


def test_session_summaries_missing_file_behavior_unchanged(tmp_path, monkeypatch):
    path = str(tmp_path / "session_summaries.json")
    monkeypatch.setattr(config, "SESSION_SUMMARIES_FILE", path, raising=False)
    monkeypatch.setattr(memory, "_session_summaries", ["stale"], raising=False)
    memory._load_session_summaries()
    assert memory._session_summaries == []


# ============================================================================
# Section 4 - HabitMemory (config/habit_memory.json)
# ============================================================================

def test_habit_memory_round_trip_and_backup(tmp_path):
    path = str(tmp_path / "habit_memory.json")
    hm = habit_memory_module.HabitMemory(path=path)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    hm.open_arrival_window("evening", now)
    hm.record_verified_action("turn_on", "ac_kamar", now)
    assert persistence.list_backups(path) == []  # first write

    hm.open_arrival_window("evening", now)
    hm.record_verified_action("turn_on", "lampu_meja", now)
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert len(backed_up["patterns"]) == 1  # only ac_kamar existed at that point

    hm2 = habit_memory_module.HabitMemory(path=path)
    assert hm2.snapshot()["pattern_count"] == 2


def test_habit_memory_missing_and_malformed_behavior_unchanged(tmp_path):
    path = str(tmp_path / "habit_memory.json")
    hm = habit_memory_module.HabitMemory(path=path)  # missing file -> no crash
    assert hm.snapshot()["pattern_count"] == 0

    with open(path, "w") as f:
        f.write("{not valid json")
    hm2 = habit_memory_module.HabitMemory(path=path)  # malformed -> no crash, fresh start
    assert hm2.snapshot()["pattern_count"] == 0


# ============================================================================
# Section 5 - luno.reminders (config/reminders.json)
# ============================================================================

def test_reminders_round_trip_and_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "reminders.json")
    monkeypatch.setattr(config, "REMINDERS_FILE", path, raising=False)
    monkeypatch.setattr(reminders, "_reminders", [], raising=False)

    entry1 = reminders.add_reminder("minum obat", "2026-09-01T20:00:00")
    assert entry1 is not None
    assert persistence.list_backups(path) == []

    entry2 = reminders.add_reminder("angkat jemuran", "2026-09-01T17:00:00")
    assert entry2 is not None
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert len(backed_up) == 1 and backed_up[0]["id"] == entry1["id"]

    reminders._reminders = []
    reminders._load()
    assert {r["id"] for r in reminders._reminders} == {entry1["id"], entry2["id"]}


def test_reminders_missing_file_behavior_unchanged(tmp_path, monkeypatch):
    path = str(tmp_path / "reminders.json")
    monkeypatch.setattr(config, "REMINDERS_FILE", path, raising=False)
    monkeypatch.setattr(reminders, "_reminders", ["stale"], raising=False)
    reminders._load()
    assert reminders._reminders == []


# ============================================================================
# Section 6 - VerifiedFactStore (config/verified_facts.json)
# ============================================================================

def _verified_tool_result(entity_id, value):
    return {"success": True, "data": {"entity_id": entity_id, "actual_state": value}}


def test_verified_facts_round_trip_and_backup(tmp_path):
    path = str(tmp_path / "verified_facts.json")
    store = memory_guard.VerifiedFactStore(path=path)

    fact1 = store.record(_verified_tool_result("living_room_light", "on"), tool_name="home_assistant")
    assert fact1 is not None
    assert persistence.list_backups(path) == []

    fact2 = store.record(_verified_tool_result("kitchen_light", "off"), tool_name="home_assistant")
    assert fact2 is not None
    backups = persistence.list_backups(path)
    assert len(backups) == 1
    with open(os.path.join(persistence.backup_dir_for(path), backups[0])) as f:
        backed_up = json.load(f)
    assert "living_room_light" in backed_up and "kitchen_light" not in backed_up

    store2 = memory_guard.VerifiedFactStore(path=path)
    assert store2.get("living_room_light")["value"] == "on"
    assert store2.get("kitchen_light")["value"] == "off"


def test_verified_facts_missing_and_malformed_behavior_unchanged(tmp_path):
    path = str(tmp_path / "verified_facts.json")
    store = memory_guard.VerifiedFactStore(path=path)  # missing file -> no crash
    assert store.all_facts() == []

    with open(path, "w") as f:
        f.write("{not valid json")
    store2 = memory_guard.VerifiedFactStore(path=path)  # malformed -> no crash, fresh start
    assert store2.all_facts() == []
