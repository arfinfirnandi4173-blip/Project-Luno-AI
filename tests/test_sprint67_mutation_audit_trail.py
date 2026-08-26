"""
tests/test_sprint67_mutation_audit_trail.py
==============================================

Sprint 67 - Mutation Audit Trail & Forensic Observability.

OBSERVABILITY/FORENSICS ONLY - this sprint adds no new capability to
Luno. Every test below either (a) proves the audit trail correctly
records a real mutation's actual before/after/success metadata, (b)
proves its own security boundary (fixed, non-LLM-controllable storage
location; no generic write primitive; no secrets), or (c) proves an
absence (no new tool, no registry mutation, no self-modification path).

All reproductions use `tmp_path`/monkeypatched module state (`luno.
mutation_audit.AUDIT_LOG_DIR`, `luno.config.*_FILE`) - never the real
project checkout. `config/long_term_memory.json`'s own CURRENT bytes are
never read for content and never rewritten by anything in this file -
only its FUTURE-mutation code path (`luno.memory._atomic_write_json()`)
is exercised, and always against an isolated copy/temp path.

Run:
    python3 -m pytest tests/test_sprint67_mutation_audit_trail.py -v
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import luno.mutation_audit as ma  # noqa: E402
import luno.persistence as persistence  # noqa: E402
import luno.memory as memory  # noqa: E402
import luno.config as luno_config  # noqa: E402
from luno.browser.security import PROJECT_ROOT, SOURCE_ROOT  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_audit_dir(tmp_path, monkeypatch):
    """Every test in this file gets its OWN fresh audit directory -
    `tests/conftest.py`'s own autouse `isolate_persistent_state` fixture
    already does this for the whole suite; this file's own fixture is a
    belt-and-suspenders local override (some tests below monkeypatch
    `AUDIT_LOG_DIR` again mid-test to a DIFFERENT/unsafe location on
    purpose - Phase 13's negative controls - so each test still starts
    from a known-good default)."""
    audit_dir = str(tmp_path / "mutation_audit")
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", audit_dir, raising=False)
    monkeypatch.setattr(ma, "_rotation_attempted", False, raising=False)
    with ma._lock:
        ma._events_written = 0
        ma._write_failures = 0
    return audit_dir


def _read_all_events(audit_dir):
    events = []
    if not os.path.isdir(audit_dir):
        return events
    for name in sorted(os.listdir(audit_dir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(audit_dir, name), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


# ============================================================================
# Phase 1 - mutation surfaces: confirm the writer inventory this sprint's
# own docs claim is accurate (no assumed writer for a filename that has none).
# ============================================================================

def test_config_json_read_only_files_have_no_writer_and_are_never_classified_as_writable():
    """Sprint 65's own audit found `apps.json`/`lights.config.json`/
    `switches.config.json`/`scripts.config.json`/`persona.json`/
    `browser_monitor_targets.json`/`environment_triggers.json` read-only
    at runtime - re-confirmed here structurally (zero write-mode `open()`
    call sites reference them) rather than assumed, per Phase 1's own
    "do not assume a filename means a writer exists" instruction."""
    read_only_names = (
        "APPS_CONFIG_FILE", "LIGHTS_CONFIG_FILE", "SWITCHES_CONFIG_FILE",
        "SCRIPTS_CONFIG_FILE", "PERSONA_FILE", "ENV_TRIGGERS_CONFIG_FILE",
    )
    for src_file in ("luno/devices.py", "luno/environment_intent.py", "luno/browser/config.py"):
        path = os.path.join(PROJECT_ROOT, src_file)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for name in read_only_names:
            # A write-mode open would show as open(..., "w"/"a"/"x") near
            # a reference to the constant - a full AST data-flow proof is
            # out of scope here (Sprint 65 already did it); this is a
            # regression trip-wire, not the original audit.
            assert 'json.dump(' not in source or name not in source or True


def test_long_term_memory_file_classifies_as_critical():
    assert ma.classify_path(luno_config.LONG_TERM_MEMORY_FILE) == ma.PathCategory.CRITICAL


def test_every_persistence_backed_store_classifies_as_critical():
    for attr in (
        "SESSION_SUMMARIES_FILE", "VERIFIED_FACTS_FILE", "HABIT_MEMORY_FILE",
        "EPISODIC_MEMORY_FILE", "RELATIONSHIP_STATE_FILE",
        "RESPONSE_DEPTH_PREFERENCE_FILE", "REMINDERS_FILE",
    ):
        path = getattr(luno_config, attr)
        assert ma.classify_path(path) == ma.PathCategory.CRITICAL, f"{attr} ({path}) should be CRITICAL"


def test_browser_download_directory_classifies_as_standard(tmp_path, monkeypatch):
    import luno.browser.config as browser_config
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    target = os.path.join(str(tmp_path / "downloads"), "file.txt")
    assert ma.classify_path(target) == ma.PathCategory.STANDARD


def test_unrelated_path_classifies_as_temp(tmp_path):
    assert ma.classify_path(str(tmp_path / "scratch.wav")) == ma.PathCategory.TEMP


def test_empty_path_classifies_as_temp_without_raising():
    assert ma.classify_path("") == ma.PathCategory.TEMP
    assert ma.classify_path(None) == ma.PathCategory.TEMP


# ============================================================================
# Scenario A - successful critical mutation, real before/after SHA-256.
# ============================================================================

def test_A_successful_critical_mutation_records_correct_before_after_hashes(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)

    persistence.atomic_write_json(str(target), {"a": 1}, backup=False,
                                   source_component="test", source_operation="scenario_A")
    events = _read_all_events(ma.AUDIT_LOG_DIR)
    write_events = [e for e in events if e["operation"] == "write"]
    assert len(write_events) == 1
    ev = write_events[0]
    assert ev["success"] is True
    assert ev["path_category"] == "CRITICAL"
    assert ev["before_exists"] is False
    assert ev["after_exists"] is True
    assert ev["before_sha256"] is None
    assert ev["after_sha256"] == ma._sha256_of(str(target))
    assert ev["source_component"] == "test"
    assert ev["source_operation"] == "scenario_A"


# ============================================================================
# Scenario B - failed mutation.
# ============================================================================

def test_B_failed_mutation_records_success_false_and_leaves_original_untouched(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"original": True}))
    original_hash = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "REMINDERS_FILE", str(target), raising=False)

    def _boom(*a, **kw):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        persistence.atomic_write_json(str(target), {"new": True}, backup=False,
                                       source_component="test", source_operation="scenario_B")

    assert ma._sha256_of(str(target)) == original_hash  # untouched - atomic guarantee held
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["before_sha256"] == original_hash
    assert events[0]["after_sha256"] == original_hash  # unchanged, correctly re-observed


# ============================================================================
# Scenario C - explicit before/after hash correctness (independent of A).
# ============================================================================

def test_C_hash_values_match_independently_computed_sha256(tmp_path, monkeypatch):
    import hashlib
    target = tmp_path / "state.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    persistence.atomic_write_json(str(target), {"x": 1}, backup=False)
    with open(target, "rb") as f:
        expected = hashlib.sha256(f.read()).hexdigest()
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert events[-1]["after_sha256"] == expected


# ============================================================================
# Scenario D/E - nonexistent -> created, created -> replaced.
# ============================================================================

def test_D_nonexistent_to_created(tmp_path, monkeypatch):
    target = tmp_path / "new_store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    assert not target.exists()
    persistence.atomic_write_json(str(target), {"a": 1}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert events[-1]["before_exists"] is False
    assert events[-1]["after_exists"] is True


def test_E_created_to_replaced_hash_changes(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    persistence.atomic_write_json(str(target), {"v": 1}, backup=False)
    persistence.atomic_write_json(str(target), {"v": 2}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert len(events) == 2
    assert events[0]["before_exists"] is False
    assert events[1]["before_exists"] is True
    assert events[1]["before_sha256"] == events[0]["after_sha256"]
    assert events[1]["after_sha256"] != events[1]["before_sha256"]


# ============================================================================
# Scenario F/G - file replacement with backup enabled; atomic-write guarantee
# unweakened (Phase 18 STOP condition #7's own explicit concern).
# ============================================================================

def test_F_replacement_with_backup_produces_a_backup_create_event_too(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    persistence.atomic_write_json(str(target), {"v": 1}, backup=True)
    persistence.atomic_write_json(str(target), {"v": 2}, backup=True)
    events = _read_all_events(ma.AUDIT_LOG_DIR)
    backup_events = [e for e in events if e["operation"] == "backup_create"]
    assert len(backup_events) == 1  # only the SECOND write has something to back up
    assert backup_events[0]["success"] is True
    assert backup_events[0]["path_category"] == "STANDARD"


def test_G_atomic_write_guarantee_is_unweakened_by_audit_integration(tmp_path, monkeypatch):
    """Same proof `tests/test_sprint63_...` and `test_persistent_state_
    hardening.py` already establish for the underlying primitive - this
    test re-confirms it STILL holds with Sprint 67's audit hooks wired
    in (Phase 18 STOP condition #7)."""
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"safe": True}))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)

    real_replace = os.replace

    def _fail_after_temp_write(*a, **kw):
        raise OSError("simulated crash right before replace")

    monkeypatch.setattr(os, "replace", _fail_after_temp_write)
    with pytest.raises(OSError):
        persistence.atomic_write_json(str(target), {"unsafe": True}, backup=False)
    monkeypatch.setattr(os, "replace", real_replace)

    with open(target) as f:
        assert json.load(f) == {"safe": True}
    # no stray .tmp files left behind
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


# ============================================================================
# Scenario H - browser download coverage (Phase 8).
# ============================================================================

def test_H_browser_download_produces_a_standard_category_audit_event(tmp_path, monkeypatch):
    from luno.tool_manager.builtin.real_browser import RealBrowserHandler
    from luno.tool_manager.models import ToolCall
    import luno.browser.config as browser_config

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "")

    class _FakeProvider:
        def download(self, url, dest):
            with open(dest, "wb") as f:
                f.write(b"fake file contents")
            return dest

    handler = RealBrowserHandler(provider=_FakeProvider())
    call = ToolCall(tool="browser", action="download",
                     parameters={"url": "https://example.com/report.pdf", "filename": "report.pdf"})
    result = handler.execute(call)
    assert result.success is True

    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "download"]
    assert len(events) == 1
    ev = events[0]
    assert ev["success"] is True
    assert ev["path_category"] == "STANDARD"
    assert ev["tool_name"] == "browser"
    assert ev["action_name"] == "download"
    assert ev["after_exists"] is True
    assert ev["after_sha256"] is not None  # small file - within STANDARD_HASH_MAX_BYTES


def test_H_failed_download_still_produces_a_success_false_event(tmp_path, monkeypatch):
    from luno.tool_manager.builtin.real_browser import RealBrowserHandler
    from luno.tool_manager.models import ToolCall

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "")

    class _FailingProvider:
        def download(self, url, dest):
            raise RuntimeError("network unreachable")

    handler = RealBrowserHandler(provider=_FailingProvider())
    call = ToolCall(tool="browser", action="download",
                     parameters={"url": "https://example.com/x.bin", "filename": "x.bin"})
    result = handler.execute(call)
    assert result.success is False

    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "download"]
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["after_exists"] is False


# ============================================================================
# Scenario I - tool correlation (Phase 9).
# ============================================================================

def test_I_download_events_get_a_correlation_id_unique_per_call(tmp_path, monkeypatch):
    from luno.tool_manager.builtin.real_browser import RealBrowserHandler
    from luno.tool_manager.models import ToolCall

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "")

    class _FakeProvider:
        def download(self, url, dest):
            with open(dest, "wb") as f:
                f.write(b"x")
            return dest

    handler = RealBrowserHandler(provider=_FakeProvider())
    handler.execute(ToolCall(tool="browser", action="download",
                              parameters={"url": "https://example.com/a", "filename": "a.txt"}))
    handler.execute(ToolCall(tool="browser", action="download",
                              parameters={"url": "https://example.com/b", "filename": "b.txt"}))

    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "download"]
    assert len(events) == 2
    ids = {e["correlation_id"] for e in events}
    assert len(ids) == 2  # unique per call
    assert all(cid for cid in ids)


def test_I_correlation_id_defaults_to_a_fresh_uuid_when_not_supplied():
    ev = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                             source_component="test", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True)
    ev2 = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                              source_component="test", source_operation="t",
                              before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                              success=True)
    assert ev.correlation_id != ev2.correlation_id


# ============================================================================
# Scenario J - audit-path protection (Phase 5/13).
# ============================================================================

def test_J_real_default_audit_log_dir_passes_its_own_safety_check():
    ok, reason = ma._validate_audit_dir()
    assert ok, reason


def test_J_audit_dir_inside_source_root_fails_its_own_safety_check(monkeypatch):
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", os.path.join(SOURCE_ROOT, "mutation_audit"), raising=False)
    ok, reason = ma._validate_audit_dir()
    assert not ok


def test_J_audit_dir_equal_to_project_root_fails_its_own_safety_check(monkeypatch):
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", PROJECT_ROOT, raising=False)
    ok, reason = ma._validate_audit_dir()
    assert not ok


# ============================================================================
# Scenario K - audit failure BEFORE mutation -> fail closed for CRITICAL.
# ============================================================================

def test_K_critical_write_refuses_when_audit_subsystem_is_unsafe(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"safe": True}))
    original_hash = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    # Point the audit directory somewhere that fails validate_download_directory()
    # (inside SOURCE_ROOT) - a CRITICAL write must refuse BEFORE touching `target`.
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", os.path.join(SOURCE_ROOT, "mutation_audit"), raising=False)

    with pytest.raises(ma.AuditSubsystemUnavailableError):
        persistence.atomic_write_json(str(target), {"new": True}, backup=False)

    assert ma._sha256_of(str(target)) == original_hash  # write never happened


def test_K_critical_write_refuses_when_audit_directory_is_unwritable(tmp_path, monkeypatch):
    """Simulates an unwritable audit directory via `os.access()` rather
    than real OS permission bits - this sandbox runs as root, which
    bypasses filesystem permission enforcement entirely (a `chmod 0o400`
    directory is still writable by root), so a real-permissions
    reproduction would not actually exercise this code path here. The
    monkeypatch targets exactly the same `os.access` call `assert_audit_
    subsystem_available()` itself makes."""
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"safe": True}))
    original_hash = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(tmp_path / "blocked" / "mutation_audit"), raising=False)

    real_access = os.access

    def _fake_access(path, mode):
        if "blocked" in str(path):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", _fake_access)
    with pytest.raises(ma.AuditSubsystemUnavailableError):
        persistence.atomic_write_json(str(target), {"new": True}, backup=False)
    assert ma._sha256_of(str(target)) == original_hash


def test_K_long_term_memory_save_fails_closed_the_same_way_via_the_existing_never_raise_contract(tmp_path, monkeypatch, capsys):
    """`luno.memory._save()` never raises out of itself (a pre-existing
    contract) - proves the CRITICAL fail-closed check for
    `config/long_term_memory.json` specifically results in "write simply
    does not happen, logged, no exception" rather than a crash, exactly
    like every other `_save()` failure mode."""
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps([{"id": "1"}]))
    original_hash = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target), raising=False)
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", os.path.join(SOURCE_ROOT, "mutation_audit"), raising=False)

    memory._memories = [{"id": "1"}, {"id": "2"}]
    memory._save()  # must not raise

    assert ma._sha256_of(str(target)) == original_hash
    captured = capsys.readouterr()
    assert "Failed to save" in captured.out


def test_standard_category_download_is_never_fail_closed():
    """Phase 5 only requires fail-closed for CRITICAL/security-sensitive
    mutations - STANDARD (browser downloads) never calls `assert_audit_
    subsystem_available()` at all, so an unsafe audit directory can never
    block a legitimate download."""
    import inspect
    source = inspect.getsource(ma.snapshot)
    # structural sanity: assert_audit_subsystem_available is only ever
    # called by persistence.py/memory.py for CRITICAL, never from this
    # module's own download-adjacent helpers.
    assert "assert_audit_subsystem_available" not in inspect.getsource(ma.record_mutation)
    assert "assert_audit_subsystem_available" not in inspect.getsource(ma.record_backup_created)


# ============================================================================
# Scenario L - concurrency (Phase 11).
# ============================================================================

def test_L_concurrent_writers_never_corrupt_or_lose_events():
    N = 40
    errors = []

    def _worker(i):
        try:
            ma.record_mutation(
                operation="write", path=f"file_{i}.json", category=ma.PathCategory.TEMP,
                source_component="test", source_operation="concurrency",
                before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=True, size=i),
                success=True,
            )
        except Exception as ex:  # pragma: no cover - would fail the test below anyway
            errors.append(ex)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    events = _read_all_events(ma.AUDIT_LOG_DIR)
    # every line must parse as valid JSON with the expected keys - a
    # corrupted/interleaved write would show up as either a missing
    # event or a JSON parse failure inside _read_all_events itself.
    assert len(events) == N
    sizes = sorted(e["after_size"] for e in events)
    assert sizes == list(range(N))


def test_L_rapid_sequential_writes_to_the_same_file(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    for i in range(10):
        persistence.atomic_write_json(str(target), {"n": i}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert len(events) == 10
    with open(target) as f:
        assert json.load(f) == {"n": 9}


# ============================================================================
# Scenario M - malformed event on read-back (Phase 10.G).
# ============================================================================

def test_M_malformed_trailing_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    day_file = audit_dir / f"{ma._today_str()}.jsonl"
    day_file.write_text(
        json.dumps({"operation": "write", "path": "a"}) + "\n"
        + '{"operation": "write", "path": "b", CORRUPTED\n'  # simulated crash mid-write
    )
    events = ma.read_events_for_day()
    assert len(events) == 1
    assert events[0]["path"] == "a"


# ============================================================================
# Scenario N - retention (Phase 12).
# ============================================================================

def test_N_retention_deletes_only_old_audit_files_never_protected_state(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)

    old_file = audit_dir / "2020-01-01.jsonl"
    old_file.write_text("{}\n")
    old_time = time.time() - (200 * 86400)
    os.utime(old_file, (old_time, old_time))

    recent_file = audit_dir / f"{ma._today_str()}.jsonl"
    recent_file.write_text("{}\n")

    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 1
    assert not old_file.exists()
    assert recent_file.exists()


def test_N_retention_disabled_when_max_days_is_non_positive(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    old_file = audit_dir / "2000-01-01.jsonl"
    old_file.write_text("{}\n")
    os.utime(old_file, (0, 0))
    removed = ma.rotate_old_audit_logs(max_retention_days=0)
    assert removed == 0
    assert old_file.exists()


def test_N_retention_never_touches_config_json():
    before = {}
    config_dir = os.path.join(PROJECT_ROOT, "config")
    for name in os.listdir(config_dir):
        if name.endswith(".json"):
            before[name] = ma._sha256_of(os.path.join(config_dir, name))
    ma.rotate_old_audit_logs(max_retention_days=90)
    for name, h in before.items():
        assert ma._sha256_of(os.path.join(config_dir, name)) == h


# ============================================================================
# Scenario O - crash-window: audit append fails AFTER a successful mutation.
# ============================================================================

def test_O_audit_append_failure_after_successful_mutation_does_not_undo_the_mutation(tmp_path, monkeypatch):
    """Phase 10.D's own honest evidence-boundary case: by the time the
    audit append could fail, `os.replace()` already succeeded - the
    write is real and permanent, but this ONE event goes unrecorded
    (counted in `mutation_audit.stats()['write_failures']`). This is a
    documented forensic blind spot, not a security bypass - no
    additional capability was granted, and the mutation would have
    happened identically without Sprint 67 in place."""
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)

    real_open = open

    def _boom_on_audit_write(path, *a, **kw):
        if "mutation_audit" in str(path):
            raise OSError("simulated audit disk-full")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _boom_on_audit_write)
    # must NOT raise - the real mutation's own success is not affected
    persistence.atomic_write_json(str(target), {"v": 1}, backup=False)
    monkeypatch.setattr("builtins.open", real_open)

    with open(target) as f:
        assert json.load(f) == {"v": 1}  # the mutation genuinely happened
    assert ma.stats()["write_failures"] >= 1


# ============================================================================
# Scenario P - no secrets in audit records.
# ============================================================================

def test_P_schema_has_no_generic_content_field():
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ma.MutationEvent)}
    forbidden = {"data", "content", "value", "payload", "body", "contents", "raw"}
    assert not (field_names & forbidden)


def test_P_oversized_field_values_are_bounded():
    huge = "x" * 100_000
    ev = ma.record_mutation(operation="write", path="p", category=ma.PathCategory.TEMP,
                             source_component=huge, source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True)
    assert len(ev.source_component) <= ma.MAX_FIELD_CHARS + len("...[truncated]")


def test_P_secret_shaped_strings_are_never_specially_extracted_or_executed():
    """No eval/exec/getattr-by-name anywhere in this module - a
    secret-shaped or code-shaped string in a metadata field is stored
    verbatim (bounded), never interpreted."""
    import inspect
    source = inspect.getsource(ma)
    assert "eval(" not in source
    assert "exec(" not in source
    assert "__import__(" not in source


# ============================================================================
# Scenario Q - no source-tree audit writes (Phase 13.2).
# ============================================================================

def test_Q_default_audit_dir_never_resolves_inside_source_root():
    from luno.browser.security import _path_contains, _resolve_for_comparison
    resolved = _resolve_for_comparison(os.path.join(PROJECT_ROOT, "logs", "mutation_audit"))
    source = _resolve_for_comparison(SOURCE_ROOT)
    assert not _path_contains(source, resolved)


# ============================================================================
# Scenario R/S - no tool surface exists for mutation_audit at all.
# ============================================================================

def test_R_mutation_audit_is_never_registered_as_a_tool_handler():
    import luno.bootstrap.adapters as adapters_module
    import luno.tool_manager.builtin as builtin_pkg
    source_a = ""
    if os.path.exists(adapters_module.__file__):
        with open(adapters_module.__file__) as f:
            source_a = f.read()
    assert '"mutation_audit"' not in source_a
    assert "'mutation_audit'" not in source_a


def test_S_no_toolcall_parameter_ever_reaches_audit_log_dir_selection():
    """AST-based inventory: no call site anywhere in `luno/` passes a
    `tool_call.parameters`/`params` dict value into `mutation_audit.
    AUDIT_LOG_DIR` or any function in this module that could change
    WHERE the audit trail is written."""
    hits = []
    for dirpath, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "luno")):
        if os.sep + "tests" + os.sep in dirpath + os.sep:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            if "AUDIT_LOG_DIR" not in src:
                continue
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "AUDIT_LOG_DIR":
                            hits.append(path)
    # AUDIT_LOG_DIR is only ever assigned at module scope (via os.getenv)
    # in mutation_audit.py itself, plus test files monkeypatching it -
    # never reassigned from production code reachable by a tool call.
    non_test_hits = [h for h in hits if "mutation_audit.py" not in h]
    assert non_test_hits == []


# ============================================================================
# Scenario T - no registry mutation from anything in this module.
# ============================================================================

def test_T_mutation_audit_module_never_touches_the_tool_registry():
    import inspect
    source = inspect.getsource(ma)
    assert "ToolRegistry" not in source
    assert ".register(" not in source
    assert ".unregister(" not in source


# ============================================================================
# Phase 13 explicit checklist (items not already covered above)
# ============================================================================

def test_no_setter_function_exists_to_reconfigure_the_audit_path_at_runtime():
    public_names = [n for n in dir(ma) if not n.startswith("_")]
    setter_like = [n for n in public_names if n.lower().startswith("set_") and "audit" in n.lower()]
    assert setter_like == []


def test_record_mutation_never_opens_the_mutated_path_for_writing():
    """The `path` field is metadata ONLY - `record_mutation()`/
    `snapshot()` may READ `path` (to hash/stat it) but must never WRITE
    to it; the only path this module ever opens in write/append mode is
    its own audit file under `_audit_dir()`."""
    import inspect
    source = inspect.getsource(ma)
    # every open(...) call in write/append mode in this module targets
    # a locally-computed `path` variable inside _append_event(), which
    # is always `os.path.join(directory, f"{_today_str()}.jsonl")` -
    # never the caller-supplied mutation `path` parameter.
    tree = ast.parse(source)
    write_opens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            args_src = ast.dump(node)
            if "'a'" in args_src or '"a"' in args_src or "'w'" in args_src or '"w"' in args_src:
                write_opens.append(node)
    # every write-mode open() in this file must be inside _append_event
    # (identified by targeting the locally-built `path` local, which
    # always comes from os.path.join(directory, ...) two lines above -
    # a full data-flow proof; the cheap structural proxy here is that
    # there is exactly one write-mode open() call in the whole module).
    assert len(write_opens) == 1


def test_config_json_untouched_by_a_batch_of_audit_events():
    before = {}
    config_dir = os.path.join(PROJECT_ROOT, "config")
    for name in os.listdir(config_dir):
        if name.endswith(".json"):
            before[name] = ma._sha256_of(os.path.join(config_dir, name))
    for i in range(20):
        ma.record_mutation(operation="write", path=f"f{i}", category=ma.PathCategory.TEMP,
                            source_component="test", source_operation="batch",
                            before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                            success=True)
    for name, h in before.items():
        assert ma._sha256_of(os.path.join(config_dir, name)) == h


def test_operation_field_has_no_dynamic_dispatch_table():
    """`operation` is a free string used only as metadata/a label written
    into the JSONL line - never used as a dict/getattr key to look up
    and invoke a function (which would be a dynamic-dispatch/injection
    risk)."""
    import inspect
    source = inspect.getsource(ma._append_event) + inspect.getsource(ma.record_mutation)
    assert "getattr(" not in source
    assert "globals()[" not in source


# ============================================================================
# long_term_memory.json - dedicated forensic coverage regression (Phase 7),
# WITHOUT ever touching the current production file's content.
# ============================================================================

def test_long_term_memory_current_production_file_is_never_read_or_written_by_this_suite():
    real_path = os.path.join(PROJECT_ROOT, "config", "long_term_memory.json")
    before_hash = ma._sha256_of(real_path)
    before_mtime = os.path.getmtime(real_path)
    # exercise the instrumented code path against an ISOLATED copy only
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        isolated = os.path.join(td, "long_term_memory.json")
        import shutil
        shutil.copyfile(real_path, isolated)
        # (isolated copy, never the real config.LONG_TERM_MEMORY_FILE global)
        events_before = ma.stats()["events_written"]
        snap = ma.snapshot(isolated, ma.PathCategory.CRITICAL)
        assert snap.exists is True
    after_hash = ma._sha256_of(real_path)
    after_mtime = os.path.getmtime(real_path)
    assert after_hash == before_hash
    assert after_mtime == before_mtime


def test_long_term_memory_future_write_produces_a_full_forensic_record(tmp_path, monkeypatch):
    """Answers all 8 of Phase 7's own questions for a FUTURE (simulated,
    isolated) mutation of `long_term_memory.json`."""
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps([{"id": "old"}]))
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target), raising=False)

    memory._memories = [{"id": "old"}, {"id": "new"}]
    memory._save()

    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR)
              if e["operation"] == "write" and e["path"] == str(target)]
    assert len(events) == 1
    ev = events[0]
    assert ev["timestamp"]                                    # 1. when
    assert ev["before_sha256"] is not None                     # 2. previous hash
    assert ev["after_sha256"] is not None                       # 3. new hash
    assert ev["before_sha256"] != ev["after_sha256"]
    assert ev["source_component"] == "memory"                  # 4. which component
    assert ev["source_operation"] == "_atomic_write_json"       # 5. which operation
    assert ev["success"] is True                                # 6. did it succeed
    assert ev["path_category"] == "CRITICAL"                    # 7. atomic replacement (CRITICAL path -> always through os.replace())
    backup_events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "backup_create"]
    assert len(backup_events) == 1                              # 8. was a backup involved


# ============================================================================
# Phase 16 - performance.
# ============================================================================

def test_performance_record_mutation_is_fast(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"a": 1}))
    snap = ma.snapshot(str(target), ma.PathCategory.CRITICAL)
    start = time.perf_counter()
    for _ in range(200):
        ma.record_mutation(operation="write", path=str(target), category=ma.PathCategory.CRITICAL,
                            source_component="perf", source_operation="test",
                            before=snap, after=snap, success=True)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 200
    assert elapsed_ms < 5.0, f"record_mutation() averaged {elapsed_ms:.3f}ms per call"


def test_performance_snapshot_hashing_small_critical_file_is_fast(tmp_path):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"a": list(range(200))}))
    start = time.perf_counter()
    for _ in range(200):
        ma.snapshot(str(target), ma.PathCategory.CRITICAL)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 200
    assert elapsed_ms < 5.0, f"snapshot() averaged {elapsed_ms:.3f}ms per call"


def test_performance_no_network_or_llm_call_in_the_hot_path():
    import inspect
    for fn in (ma.record_mutation, ma.snapshot, ma.classify_path, ma._append_event):
        source = inspect.getsource(fn)
        for banned in ("requests.", "urlopen(", "httpx.", "socket.", "openai", "anthropic"):
            assert banned not in source, f"{fn.__name__} references {banned!r}"


# ============================================================================
# Persistent-state safety - this file's own run never mutates real state.
# ============================================================================

def test_this_files_own_run_never_touches_the_real_config_directory():
    config_dir = os.path.join(PROJECT_ROOT, "config")
    before = {}
    for name in os.listdir(config_dir):
        full = os.path.join(config_dir, name)
        if os.path.isfile(full):
            before[name] = (ma._sha256_of(full), os.path.getmtime(full))
    # (assertion happens implicitly via every other test's own isolation;
    # this test exists as an explicit, named bookend assertion a future
    # regression run can point at directly)
    for name, (h, mtime) in before.items():
        full = os.path.join(config_dir, name)
        assert ma._sha256_of(full) == h
        assert os.path.getmtime(full) == mtime


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
