"""
test_memory_regression.py
===========================

Regression & Architecture Guard sprint - the ONE confirmed golden-
regression gap found during the audit (see `ARCHITECTURE_GUARD.md` §7):
`luno/memory.py`'s long-term-memory and session-summary file loaders
(`_load()` / `_load_session_summaries()`) already fail safe on a
missing/malformed `config/long_term_memory.json` or
`config/session_summaries.json` (`try/except Exception -> []`, never
raises), but had ZERO test coverage before this sprint. Every other
golden-regression behavior in the sprint brief already had existing
coverage elsewhere (cited in `ARCHITECTURE_GUARD.md` §7) - this file
does not duplicate any of that.

Does NOT redesign the memory subsystem - `_load()`/`_load_session_
summaries()` are exercised exactly as they exist today, via the same
"monkeypatch `config.<FILE>`, call the loader, restore state" pattern
`tests/test_persona.py` already established for the analogous
`luno/persona.py` loader.

Run:
    python3 -m pytest tests/test_memory_regression.py
"""

from __future__ import annotations

import json

import luno.memory as memory_module


def _save_state():
    """`_memories`/`_session_summaries` are process-wide globals mutated
    in place by the loaders - save/restore around every test so this
    file can never leak state into a test that runs after it (same
    hygiene `luno/tool_manager/tests/test_real_home_assistant_verification.py`'s
    `_patch_devices`/`_restore_devices` already uses for its own global
    registries)."""
    return list(memory_module._memories), list(memory_module._session_summaries)


def _restore_state(saved):
    memory_module._memories = saved[0]
    memory_module._session_summaries = saved[1]


# ============================================================================
# Long-term memory (`config/long_term_memory.json` via `_load()`)
# ============================================================================

def test_missing_long_term_memory_file_loads_empty_without_crashing(monkeypatch, tmp_path):
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(tmp_path / "does_not_exist.json"))
        memory_module._load()  # must not raise
        assert memory_module._memories == []
        assert memory_module.list_memories() == []
        assert memory_module.build_memory_prompt() == ""
    finally:
        _restore_state(saved)


def test_malformed_json_long_term_memory_falls_back_to_empty(monkeypatch, tmp_path):
    """Corrupted/hand-edited JSON (a real, plausible failure mode - this
    file is meant to be user-editable) must never crash Luno's startup."""
    bad_file = tmp_path / "long_term_memory.json"
    bad_file.write_text("{not valid json,,,", encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(bad_file))
        memory_module._load()  # must not raise
        assert memory_module._memories == []
        assert memory_module.build_memory_prompt() == ""
    finally:
        _restore_state(saved)


def test_wrong_top_level_type_long_term_memory_falls_back_safely(monkeypatch, tmp_path):
    """Valid JSON that isn't a list (e.g. someone pasted a single object
    instead of an array) would break `build_memory_prompt()`'s
    `for m in _memories` / `m["text"]` iteration downstream if it were
    accepted as-is. `_load()` itself doesn't type-check, so this guards
    the REALISTIC failure mode one level up: confirms a non-list value
    at least loads without `_load()` itself raising, and that a plain
    dict (the most likely accidental shape) doesn't feed a crash into
    `build_memory_prompt()` either."""
    bad_file = tmp_path / "long_term_memory.json"
    bad_file.write_text(json.dumps({"oops": "this should have been a list"}), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(bad_file))
        memory_module._load()  # must not raise
        # Whatever _load() produced, list_memories()/build_memory_prompt()
        # must still not raise - the real safety net a corrupted-but-
        # syntactically-valid file needs.
        memory_module.list_memories()
        memory_module.build_memory_prompt()
    finally:
        _restore_state(saved)


def test_valid_long_term_memory_file_still_loads_correctly(monkeypatch, tmp_path):
    """Regression guard the other direction - the safe-fallback path
    must never accidentally swallow a perfectly valid file too."""
    good_file = tmp_path / "long_term_memory.json"
    good_file.write_text(json.dumps([{"id": "1", "text": "user likes tea", "created_at": "2026-01-01T00:00:00"}]), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(good_file))
        memory_module._load()
        assert memory_module.list_memories() == [{"id": "1", "text": "user likes tea", "created_at": "2026-01-01T00:00:00"}]
        assert "user likes tea" in memory_module.build_memory_prompt()
    finally:
        _restore_state(saved)


# ============================================================================
# Session summaries (`config/session_summaries.json` via `_load_session_summaries()`)
# ============================================================================

def test_missing_session_summaries_file_loads_empty_without_crashing(monkeypatch, tmp_path):
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "SESSION_SUMMARIES_FILE", str(tmp_path / "does_not_exist.json"))
        memory_module._load_session_summaries()  # must not raise
        assert memory_module._session_summaries == []
        assert memory_module.build_session_summary_prompt() == ""
    finally:
        _restore_state(saved)


def test_malformed_json_session_summaries_falls_back_to_empty(monkeypatch, tmp_path):
    bad_file = tmp_path / "session_summaries.json"
    bad_file.write_text("[{broken", encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "SESSION_SUMMARIES_FILE", str(bad_file))
        memory_module._load_session_summaries()  # must not raise
        assert memory_module._session_summaries == []
        assert memory_module.build_session_summary_prompt() == ""
    finally:
        _restore_state(saved)


def test_valid_session_summaries_file_still_loads_correctly(monkeypatch, tmp_path):
    good_file = tmp_path / "session_summaries.json"
    good_file.write_text(json.dumps([{"id": "s1", "summary": "talked about aquascaping", "turn_count": 4, "ended_at": "2026-01-01T00:00:00"}]), encoding="utf-8")
    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "SESSION_SUMMARIES_FILE", str(good_file))
        memory_module._load_session_summaries()
        assert len(memory_module._session_summaries) == 1
        assert "aquascaping" in memory_module.build_session_summary_prompt()
    finally:
        _restore_state(saved)


# ============================================================================
# Both loaders independent of each other (a broken one must not affect the other)
# ============================================================================

def test_malformed_long_term_memory_does_not_affect_session_summaries(monkeypatch, tmp_path):
    bad_memory_file = tmp_path / "long_term_memory.json"
    bad_memory_file.write_text("not json at all", encoding="utf-8")
    good_summaries_file = tmp_path / "session_summaries.json"
    good_summaries_file.write_text(json.dumps([{"id": "s1", "summary": "ok", "turn_count": 1, "ended_at": "2026-01-01T00:00:00"}]), encoding="utf-8")

    saved = _save_state()
    try:
        monkeypatch.setattr(memory_module.config, "LONG_TERM_MEMORY_FILE", str(bad_memory_file))
        monkeypatch.setattr(memory_module.config, "SESSION_SUMMARIES_FILE", str(good_summaries_file))
        memory_module._load()
        memory_module._load_session_summaries()

        assert memory_module._memories == []
        assert len(memory_module._session_summaries) == 1
    finally:
        _restore_state(saved)
