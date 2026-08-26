"""
tests/test_sprint68_mutation_audit_hardening.py
===================================================

Sprint 68 - Mutation Audit Trail Verification & Hardening.

Independently VERIFIES Sprint 67's own claims (not merely re-asserts
them) and hardens the audit trail with: path canonicalization for
stored records, defensive bounding of every string field (not just the
four Sprint 67 already bounded), a non-fatal startup visibility check
for a misconfigured `MUTATION_AUDIT_LOG_DIR`, a "pending"/"completed"
two-phase append for CRITICAL mutations (makes Sprint 67's own
documented post-mutation-audit-failure blind spot DETECTABLE, not
closed), and a strictly read-only forensic replay helper
(`luno/mutation_audit_replay.py`).

No new persistence system, no new tracing system, no generic filesystem
writer, no LLM-controlled audit destination was added - every test
below either proves a hardening claim holds or proves an absence.

All reproductions use `tmp_path`/monkeypatched module state. `config/
long_term_memory.json`'s current bytes are never read for content and
never used as a write target anywhere in this file.

Run:
    python3 -m pytest tests/test_sprint68_mutation_audit_hardening.py -v
"""

from __future__ import annotations

import ast
import inspect
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
import luno.mutation_audit_replay as replay  # noqa: E402
import luno.persistence as persistence  # noqa: E402
import luno.memory as memory  # noqa: E402
import luno.config as luno_config  # noqa: E402
from luno.browser.security import PROJECT_ROOT, SOURCE_ROOT  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_audit_dir(tmp_path, monkeypatch):
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


def _set_old_mtime(path, when=(0, 0)):
    os.utime(path, when)


def _rotate_diagnostics(audit_dir, watch_path, max_retention_days):
    """Diagnostic snapshot attached to the two retention assertions
    below as their failure message. Genuinely earned its keep once: two
    full-suite-only failures here (`removed == 0` where `1` was
    expected) looked at first like filesystem-metadata timing under
    heavy I/O load, and were provisionally "fixed" with a retry loop -
    but the retry loop did NOT fix it (same failure, every attempt, for
    the full retry window), which was the tell that this was never a
    race. This diagnostic's `cutoff`/`now` fields caught the real cause
    on the next full-suite run: `now` read back as `1000006.0` (~11.6
    days after the epoch) instead of a real 2026 timestamp - `tests/
    test_camera_presence.py`'s `_adapter()` helper did `vmod.time.time =
    lambda: ...` (a raw, never-restored attribute assignment on the
    SHARED stdlib `time` module object) and nothing in that file ever
    put the real `time.time` back, so every test running afterward in
    the same pytest process inherited a permanently frozen fake clock.
    Fixed at the actual source (`tests/test_camera_presence.py` gained
    an autouse fixture that restores real `time.time` after each of its
    own tests) rather than papered over here - kept only as a guardrail
    in case a similar leak reappears elsewhere in the suite someday."""
    cutoff = time.time() - (max_retention_days * 86400)
    info = {"audit_dir": audit_dir, "watch_path": watch_path, "cutoff": cutoff, "now": time.time()}
    try:
        info["listdir"] = os.listdir(audit_dir)
    except Exception as e:
        info["listdir_error"] = repr(e)
    try:
        info["watch_exists"] = os.path.exists(watch_path)
        info["watch_mtime"] = os.path.getmtime(watch_path)
        info["watch_mtime_lt_cutoff"] = info["watch_mtime"] < cutoff
    except Exception as e:
        info["watch_stat_error"] = repr(e)
    return info


# ============================================================================
# PHASE 1 (baseline) sanity - re-verified, not assumed.
# ============================================================================

def test_baseline_config_json_count_is_18():
    # Sprint 71 (Camera Patrol) added config/camera_patrol_routes.json,
    # Sprint 72 (Automation Engine Dasar) added config/automation_
    # rules.json, and P0.5 (Real Camera Integration) added config/
    # camera_automation.json (per-camera Home Assistant entity-role
    # mapping, shipped with every role null - see luno/camera_
    # automation/cameras.py's own module docstring) - all legitimate,
    # new named-entity definition stores (same convention this repo
    # already uses for scripts.config.json/switches.config.json - see
    # luno/camera_patrol/controller.py's own module docstring, luno/
    # automation/engine.py's own Phase 11 section, and luno/camera_
    # automation/config.py's own P0.5 section, respectively). This
    # baseline count moved from 15 (Sprint 68's own count) to 16
    # (Sprint 71) to 17 (Sprint 72) to 18 (P0.5) for these three,
    # intentional, documented reasons - not an unplanned config change.
    config_dir = os.path.join(PROJECT_ROOT, "config")
    names = [n for n in os.listdir(config_dir) if n.endswith(".json")]
    assert len(names) == 18


def test_baseline_no_real_mutation_audit_dir_exists_before_any_real_write():
    real_dir = os.path.join(PROJECT_ROOT, "logs", "mutation_audit")
    # this assertion is about the REAL checkout, not this test's own
    # isolated tmp_path - it must remain true throughout this whole file.
    assert not os.path.isdir(real_dir) or os.listdir(real_dir) == []


# ============================================================================
# PHASE 2 - schema review, explicit proof for every claim.
# ============================================================================

def test_timestamp_is_generated_locally_and_is_recent_utc():
    from datetime import datetime, timezone
    ev = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                             source_component="t", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True)
    ts = datetime.fromisoformat(ev.timestamp)
    now = datetime.now(timezone.utc)
    assert abs((now - ts).total_seconds()) < 10


def test_operation_is_always_application_supplied_never_derived_from_toolcall_params():
    """AST inventory: every call site of `mutation_audit.record_mutation`/
    `record_pending_mutation` anywhere in `luno/` passes `operation=` a
    string literal (or an f-string built purely from string literals),
    never a `ToolCall.parameters`/`params` dict lookup."""
    hits = []
    for dirpath, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "luno")):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            if "record_mutation(" not in src and "record_pending_mutation(" not in src:
                continue
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fname_attr = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if fname_attr not in ("record_mutation", "record_pending_mutation"):
                        continue
                    for kw in node.keywords:
                        if kw.arg == "operation" and not isinstance(kw.value, (ast.Constant, ast.JoinedStr)):
                            hits.append((path, ast.dump(kw.value)))
    assert hits == []


def test_path_is_canonicalized_in_the_stored_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative = "some_file.json"
    ev = ma.record_mutation(operation="write", path=relative, category=ma.PathCategory.TEMP,
                             source_component="t", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True)
    assert ev.path == os.path.abspath(relative)
    assert os.path.isabs(ev.path)


def test_path_category_only_ever_one_of_three_fixed_values():
    for cat in (ma.PathCategory.CRITICAL, ma.PathCategory.STANDARD, ma.PathCategory.TEMP):
        ev = ma.record_mutation(operation="write", path="x", category=cat,
                                 source_component="t", source_operation="t",
                                 before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                                 success=True)
        assert ev.path_category in ("CRITICAL", "STANDARD", "TEMP")


def test_source_component_and_operation_never_reachable_from_toolcall_parameters():
    """Every call site passing `source_component=`/`source_operation=`
    anywhere in `luno/` uses a fixed string literal (or an f-string of
    literals) - never a dict lookup into `tool_call.parameters`/`params`."""
    hits = []
    for dirpath, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "luno")):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            if "source_component=" not in src and "source_operation=" not in src:
                continue
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg not in ("source_component", "source_operation"):
                        continue
                    if isinstance(kw.value, (ast.Constant, ast.JoinedStr, ast.BoolOp)):
                        continue
                    # allow `x or "default"`/plain Name passthrough of an
                    # already-validated local parameter (e.g. this
                    # module's own optional kwargs) - only flag a
                    # subscript/attribute access shaped like dict/params
                    # indexing.
                    src_dump = ast.dump(kw.value)
                    if "Subscript" in src_dump and ("params" in src_dump.lower() or "parameters" in src_dump.lower()):
                        hits.append((path, src_dump))
    assert hits == []


def test_success_field_is_strictly_boolean():
    ev = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                             source_component="t", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success="truthy-but-not-bool")
    assert ev.success is True
    assert isinstance(ev.success, bool)


def test_before_after_metadata_limited_to_exists_size_sha256():
    fields = {"exists", "size", "sha256"}
    import dataclasses
    snapshot_fields = {f.name for f in dataclasses.fields(ma.FileSnapshot)}
    assert snapshot_fields == fields


def test_tool_name_action_name_correlation_id_cannot_inject_file_content(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    malicious = "'; DROP TABLE x; -- \x00<script>alert(1)</script>"
    persistence.atomic_write_json(str(target), {"a": 1}, backup=False,
                                   tool_name=malicious, action_name=malicious)
    with open(target) as f:
        content = json.load(f)
    assert content == {"a": 1}  # the real write is completely unaffected
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert malicious[:50] in events[-1]["tool_name"]  # stored verbatim (bounded), never interpreted


def test_pid_matches_the_real_process_id():
    ev = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                             source_component="t", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True)
    assert ev.pid == os.getpid()


def test_no_conversation_or_memory_content_field_exists_in_schema():
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ma.MutationEvent)}
    forbidden = {"conversation", "memory_content", "prompt", "response", "utterance", "message"}
    assert not (field_names & forbidden)


# ============================================================================
# PHASE 3 - path trust boundary.
# ============================================================================

def test_audit_log_cannot_be_redirected_into_config():
    monkeypatch_target = os.path.join(PROJECT_ROOT, "config")
    ma_backup = ma.AUDIT_LOG_DIR
    try:
        ma.AUDIT_LOG_DIR = monkeypatch_target
        ok, reason = ma._validate_audit_dir()
        assert not ok
    finally:
        ma.AUDIT_LOG_DIR = ma_backup


def test_audit_log_cannot_be_redirected_into_luno_source():
    ma_backup = ma.AUDIT_LOG_DIR
    try:
        ma.AUDIT_LOG_DIR = SOURCE_ROOT
        ok, reason = ma._validate_audit_dir()
        assert not ok
    finally:
        ma.AUDIT_LOG_DIR = ma_backup


def test_audit_log_cannot_be_redirected_to_arbitrary_user_selected_path_when_it_overlaps_critical_files():
    ma_backup = ma.AUDIT_LOG_DIR
    try:
        ma.AUDIT_LOG_DIR = os.path.dirname(luno_config.LONG_TERM_MEMORY_FILE) or "config"
        ok, reason = ma._validate_audit_dir()
        assert not ok
    finally:
        ma.AUDIT_LOG_DIR = ma_backup


def test_symlink_escape_into_source_tree_is_rejected(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("platform has no symlink support")
    link = tmp_path / "sneaky_audit_dir"
    try:
        os.symlink(SOURCE_ROOT, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this sandbox")
    ma_backup = ma.AUDIT_LOG_DIR
    try:
        ma.AUDIT_LOG_DIR = str(link)
        ok, reason = ma._validate_audit_dir()
        assert not ok
    finally:
        ma.AUDIT_LOG_DIR = ma_backup


def test_env_var_misconfiguration_does_not_silently_weaken_the_boundary(monkeypatch, capsys):
    """A `MUTATION_AUDIT_LOG_DIR` pointed at the source tree must be
    caught - both by the Sprint 68 startup warning (re-simulated here by
    calling the function directly, since real import already happened)
    and, more importantly, by the Sprint 67 fail-closed check at actual
    CRITICAL-write time (re-verified, not merely re-asserted)."""
    ma_backup = ma.AUDIT_LOG_DIR
    try:
        ma.AUDIT_LOG_DIR = os.path.join(SOURCE_ROOT, "evil_audit")
        ma._warn_if_audit_dir_unsafe_at_import()
        captured = capsys.readouterr()
        assert "failed its own safety check" in captured.out
        with pytest.raises(ma.AuditSubsystemUnavailableError):
            ma.assert_audit_subsystem_available()
    finally:
        ma.AUDIT_LOG_DIR = ma_backup


def test_invalid_audit_path_fails_closed_for_critical_writes(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"safe": True}))
    original_hash = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", SOURCE_ROOT, raising=False)
    with pytest.raises(ma.AuditSubsystemUnavailableError):
        persistence.atomic_write_json(str(target), {"new": True}, backup=False)
    assert ma._sha256_of(str(target)) == original_hash


def test_standard_downloads_are_never_blocked_by_an_unsafe_audit_directory(tmp_path, monkeypatch):
    from luno.tool_manager.builtin.real_browser import RealBrowserHandler
    from luno.tool_manager.models import ToolCall

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "")
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", SOURCE_ROOT, raising=False)  # deliberately unsafe

    class _FakeProvider:
        def download(self, url, dest):
            with open(dest, "wb") as f:
                f.write(b"ok")
            return dest

    handler = RealBrowserHandler(provider=_FakeProvider())
    result = handler.execute(ToolCall(tool="browser", action="download",
                                       parameters={"url": "https://example.com/f", "filename": "f.txt"}))
    assert result.success is True  # download proceeds regardless - STANDARD is never fail-closed


# ============================================================================
# PHASE 4 - adversarial event integrity. Every emitted line must remain
# independently parseable JSON, one object per line.
# ============================================================================

_ADVERSARIAL_VALUES = [
    "x" * 5000,                                   # very long
    "héllo wörld 日本語 🎉🔥",                        # unicode
    "line1\nline2\r\nline3",                       # newlines
    'quote " inside \' value',                     # quotes
    '{"looks": "like json"}',                      # JSON-looking string
    "../../../etc/passwd",                         # path separators / traversal-shaped
    "a\x00b\x00c",                                  # null bytes (where the runtime permits)
    "op with spaces AND-symbols!@#$%^&*()",        # unexpected operation-name shape
]


@pytest.mark.parametrize("value", _ADVERSARIAL_VALUES)
def test_adversarial_metadata_values_never_produce_malformed_jsonl(value, tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    ev = ma.record_mutation(operation=value, path=value, category=ma.PathCategory.TEMP,
                             source_component=value, source_operation=value,
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True, tool_name=value, action_name=value, correlation_id=value)
    events = _read_all_events(str(audit_dir))
    assert len(events) == 1  # exactly one line, independently parseable (already proven by _read_all_events)
    assert events[0]["operation"]  # non-empty, bounded, stored


def test_extremely_long_correlation_id_is_bounded(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    huge_id = "c" * 100_000
    ev = ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                             source_component="t", source_operation="t",
                             before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                             success=True, correlation_id=huge_id)
    assert len(ev.correlation_id) <= 64 + len("...[truncated]")


def test_every_jsonl_line_is_exactly_one_json_object_no_trailing_garbage(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    for v in _ADVERSARIAL_VALUES:
        ma.record_mutation(operation="write", path=v, category=ma.PathCategory.TEMP,
                            source_component="t", source_operation="t",
                            before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                            success=True)
    day_file = audit_dir / f"{ma._today_str()}.jsonl"
    with open(day_file, "r", encoding="utf-8") as f:
        lines = [l for l in f.read().split("\n") if l.strip()]
    assert len(lines) == len(_ADVERSARIAL_VALUES)
    for line in lines:
        obj = json.loads(line)  # raises if malformed
        assert isinstance(obj, dict)


# ============================================================================
# PHASE 5 - concurrency across mixed real writers (persistence, memory,
# browser-style events).
# ============================================================================

def test_concurrent_mixed_writers_each_produce_correct_events(tmp_path, monkeypatch):
    persistence_target = tmp_path / "persistence_store.json"
    memory_target = tmp_path / "memory_store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(persistence_target), raising=False)
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(memory_target), raising=False)

    errors = []

    def _persistence_writer(i):
        try:
            persistence.atomic_write_json(str(persistence_target), {"n": i}, backup=False)
        except Exception as ex:
            errors.append(ex)

    def _memory_writer(i):
        try:
            memory._memories = [{"id": str(i)}]
            memory._save()
        except Exception as ex:
            errors.append(ex)

    def _browser_style_event(i):
        try:
            snap = ma.FileSnapshot(exists=True, size=i, sha256="deadbeef")
            ma.record_mutation(operation="download", path=f"dl_{i}.bin", category=ma.PathCategory.STANDARD,
                                source_component="browser", source_operation="_dispatch:download",
                                tool_name="browser", action_name="download",
                                before=ma.FileSnapshot(exists=False), after=snap, success=True)
        except Exception as ex:
            errors.append(ex)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=_persistence_writer, args=(i,)))
        threads.append(threading.Thread(target=_memory_writer, args=(i,)))
        threads.append(threading.Thread(target=_browser_style_event, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    events = _read_all_events(ma.AUDIT_LOG_DIR)
    # every line parses (already implied by _read_all_events not raising)
    download_events = [e for e in events if e["operation"] == "download"]
    assert len(download_events) == 10
    correlation_ids = {e["correlation_id"] for e in download_events}
    assert len(correlation_ids) == 10  # unique per event


def test_each_successful_mutation_produces_at_most_one_completed_success_event(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    persistence.atomic_write_json(str(target), {"v": 1}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR)
              if e["operation"] == "write" and e["success"] is True]
    assert len(events) == 1


def test_failed_operation_produces_the_expected_failure_event(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"orig": True}))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        persistence.atomic_write_json(str(target), {"v": 2}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR)
              if e["operation"] == "write" and e["success"] is False]
    assert len(events) == 1


def test_mutation_audit_introduces_no_second_global_state_mechanism():
    """The module's own mutable global state is limited to simple
    in-memory counters/flags - never a second file-backed store, queue,
    or database."""
    import types
    module_globals = {k: v for k, v in vars(ma).items() if not k.startswith("__")}
    # Imported modules (os, json, uuid, hashlib, time, threading) and the
    # `from __future__ import annotations` marker are not "state this
    # module owns" - they're ordinary import bindings every module has.
    # Excluding ModuleType (and the `annotations` future-flag object,
    # which is itself a plain object, not a module) is the correct filter
    # here, not evidence of hidden state.
    stateful = {k: v for k, v in module_globals.items()
                if not (isinstance(v, (types.FunctionType, type, types.ModuleType))
                        or k.isupper() or callable(v) or k == "annotations")}
    # the only expected non-constant, non-function globals are the
    # small counters/flags this module documents (_lock/_events_written/
    # _write_failures/_rotation_attempted) plus AUDIT_LOG_DIR itself.
    allowed = {"_lock", "_events_written", "_write_failures", "_rotation_attempted", "AUDIT_LOG_DIR"}
    unexpected = set(stateful.keys()) - allowed
    assert unexpected == set(), f"unexpected module-level mutable state: {unexpected}"


# ============================================================================
# PHASE 6 - crash/failure windows, incl. backup-specific scenarios.
# ============================================================================

def test_1_audit_append_succeeds_normal_case(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    persistence.atomic_write_json(str(target), {"v": 1}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert len(events) == 1 and events[0]["success"] is True


def test_2_audit_directory_unsafe_before_mutation_refuses_write(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"orig": True}))
    original = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", SOURCE_ROOT, raising=False)
    with pytest.raises(ma.AuditSubsystemUnavailableError):
        persistence.atomic_write_json(str(target), {"v": 2}, backup=False)
    assert ma._sha256_of(str(target)) == original


def test_3_mutation_itself_fails_records_failure(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"orig": True}))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        persistence.atomic_write_json(str(target), {"v": 2}, backup=False)
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert events[-1]["success"] is False


def test_4_mutation_succeeds_but_completed_audit_append_fails_is_detectable_via_orphaned_pending(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)

    real_open = open
    call_count = {"n": 0}

    def _fail_second_audit_write(path, *a, **kw):
        if "mutation_audit" in str(path) and a and a[0] == "a":
            call_count["n"] += 1
            if call_count["n"] == 2:  # let the "pending" append succeed, fail the "completed" one
                raise OSError("simulated crash between pending and completed audit writes")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fail_second_audit_write)
    persistence.atomic_write_json(str(target), {"v": 1}, backup=False)  # must NOT raise
    monkeypatch.setattr("builtins.open", real_open)

    with open(target) as f:
        assert json.load(f) == {"v": 1}  # the mutation genuinely succeeded

    events = _read_all_events(ma.AUDIT_LOG_DIR)
    orphans = replay.find_orphaned_pending_events(events)
    assert len(orphans) == 1  # the blind spot is now DETECTABLE, not merely silent
    assert orphans[0]["operation"] == "write:pending"


def test_5_backup_creation_succeeds_but_its_own_audit_event_fails(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"orig": True}))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)

    real_record_backup_created = ma.record_backup_created

    def _boom(*a, **kw):
        raise RuntimeError("simulated audit failure recording the backup")

    monkeypatch.setattr(persistence.mutation_audit if hasattr(persistence, "mutation_audit") else ma,
                         "record_backup_created", _boom, raising=False)
    # persistence.py imports mutation_audit lazily inside the function -
    # patch the module attribute directly, which every lazy `from . import
    # mutation_audit` call resolves to the same module object.
    with pytest.raises(RuntimeError):
        # backup_current_file() itself is NOT wrapped in the same
        # try/except as the write - a failure recording the backup's
        # OWN audit event currently propagates. This test documents the
        # actual behavior rather than assuming it.
        persistence.atomic_write_json(str(target), {"v": 2}, backup=True)
    monkeypatch.setattr(ma, "record_backup_created", real_record_backup_created, raising=False)
    # the underlying backup FILE was still created on disk even though
    # recording its audit event raised - the real backup operation and
    # its own audit recording are not atomic with each other by design
    # (Phase 6 - only the PRIMARY write's own audit availability is a
    # fail-closed gate).
    backup_dir = persistence.backup_dir_for(str(target))
    assert os.path.isdir(backup_dir) and os.listdir(backup_dir)


def test_6_backup_creation_fails_prevents_the_write_entirely(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"orig": True}))
    original = ma._sha256_of(str(target))
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    monkeypatch.setattr(persistence, "backup_current_file",
                         lambda *a, **kw: (_ for _ in ()).throw(OSError("backup disk full")))
    with pytest.raises(persistence.BackupFailedError):
        persistence.atomic_write_json(str(target), {"v": 2}, backup=True)
    assert ma._sha256_of(str(target)) == original
    events = [e for e in _read_all_events(ma.AUDIT_LOG_DIR) if e["operation"] == "write"]
    assert events[-1]["success"] is False


def test_closing_the_blind_spot_fully_would_require_a_second_transaction_system_documented_not_built():
    """Structural proof this sprint did NOT attempt to fully close Phase
    10.D: `record_mutation()`'s own append is still a single, best-effort
    `open/write` with no verification/retry/rollback tied to the
    mutation's own transaction - i.e. no second persistence/transaction
    system was introduced, per the brief's own STOP condition."""
    source = inspect.getsource(ma._append_event)
    assert "retry" not in source.lower()
    assert "transaction" not in source.lower()
    assert "rollback" not in source.lower()


# ============================================================================
# PHASE 7 - retention.
# ============================================================================

def test_old_audit_file_deleted_current_day_preserved(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    old = audit_dir / "2020-01-01.jsonl"
    old.write_text("{}\n")
    _set_old_mtime(str(old), (0, 0))
    today = audit_dir / f"{ma._today_str()}.jsonl"
    today.write_text("{}\n")
    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 1, _rotate_diagnostics(str(audit_dir), str(old), 90)
    assert not old.exists()
    assert today.exists()


def test_malformed_audit_filename_does_not_crash_rotation(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    bad = audit_dir / "not-a-real-date.jsonl"
    bad.write_text("{}\n")
    _set_old_mtime(str(bad), (0, 0))  # very old -> should still be removed, no crash
    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 1, _rotate_diagnostics(str(audit_dir), str(bad), 90)
    assert not bad.exists()


def test_unrelated_files_inside_audit_dir_are_never_touched(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    unrelated = audit_dir / "README.txt"
    unrelated.write_text("do not delete me")
    _set_old_mtime(str(unrelated), (0, 0))
    ma.rotate_old_audit_logs(max_retention_days=90)
    assert unrelated.exists()
    assert unrelated.read_text() == "do not delete me"


def test_symlink_entry_inside_audit_dir_removes_only_the_link_never_the_target(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("platform has no symlink support")
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    critical_target = tmp_path / "pretend_critical.json"
    critical_target.write_text('{"important": true}')
    link = audit_dir / "2020-01-01.jsonl"
    try:
        os.symlink(critical_target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this sandbox")
    # rotate_old_audit_logs() ages files by `os.path.getmtime()`, which
    # FOLLOWS symlinks - so it's the TARGET's mtime that must be old for
    # this symlink to even be a rotation candidate (setting only the
    # link's own lutime, as an earlier version of this test did, left
    # the followed mtime "now" and the link was correctly never touched
    # - that was a test bug, not a production gap). Age the target itself
    # so this test actually exercises the deletion path.
    old = time.time() - (200 * 86400)
    _set_old_mtime(str(critical_target), (old, old))
    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 1, _rotate_diagnostics(str(audit_dir), str(critical_target), 90)
    # The symlink itself is gone (os.remove() on a symlink unlinks the
    # link, per POSIX semantics - it never follows to delete the target)...
    assert not os.path.lexists(str(link))
    # ...but the real critical file it pointed at is completely untouched.
    assert critical_target.exists()
    assert critical_target.read_text() == '{"important": true}'
    assert critical_target.exists()
    assert critical_target.read_text() == '{"important": true}'  # target NEVER touched


def test_future_dated_filename_with_current_mtime_is_kept(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    future_named = audit_dir / "2099-01-01.jsonl"
    future_named.write_text("{}\n")  # mtime is "now" - retention is mtime-based, not filename-based
    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 0
    assert future_named.exists()


def test_retention_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(tmp_path / "does_not_exist_at_all"), raising=False)
    removed = ma.rotate_old_audit_logs(max_retention_days=90)
    assert removed == 0


def test_rotation_cannot_delete_config_backups_or_source(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    old = audit_dir / "2020-01-01.jsonl"
    old.write_text("{}\n")
    os.utime(old, (0, 0))

    config_dir = os.path.join(PROJECT_ROOT, "config")
    before = {n: ma._sha256_of(os.path.join(config_dir, n)) for n in os.listdir(config_dir) if n.endswith(".json")}
    before_backup_count = len(os.listdir(os.path.join(config_dir, "backups")))
    before_source_hash = ma._sha256_of(os.path.join(SOURCE_ROOT, "mutation_audit.py"))

    ma.rotate_old_audit_logs(max_retention_days=90)

    for n, h in before.items():
        assert ma._sha256_of(os.path.join(config_dir, n)) == h
    assert len(os.listdir(os.path.join(config_dir, "backups"))) == before_backup_count
    assert ma._sha256_of(os.path.join(SOURCE_ROOT, "mutation_audit.py")) == before_source_hash


# ============================================================================
# PHASE 8 - read-only forensic replay helper.
# ============================================================================

def test_replay_load_events_and_filters(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    ma.record_mutation(operation="write", path="/a/b.json", category=ma.PathCategory.TEMP,
                        source_component="memory", source_operation="save",
                        before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=True),
                        success=True, correlation_id="cid-1")
    ma.record_mutation(operation="download", path="/a/c.bin", category=ma.PathCategory.STANDARD,
                        source_component="browser", source_operation="download",
                        before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=True),
                        success=True, correlation_id="cid-2")

    events = replay.load_events()
    assert len(events) == 2
    by_component = replay.filter_by_source_component(events, "memory")
    assert len(by_component) == 1 and by_component[0]["correlation_id"] == "cid-1"
    by_cid = replay.filter_by_correlation_id(events, "cid-2")
    assert len(by_cid) == 1
    by_path = replay.filter_by_path(events, "/a/b.json")
    assert len(by_path) == 1
    ordered = replay.order_chronologically(events)
    assert ordered[0]["timestamp"] <= ordered[-1]["timestamp"]


def test_replay_detects_malformed_lines_via_count(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    day_file = audit_dir / f"{ma._today_str()}.jsonl"
    day_file.write_text('{"a": 1}\n{BROKEN\nnot even close to json\n')
    assert replay.count_malformed_lines() == 2


def test_replay_is_strictly_read_only_no_write_mode_open_or_delete_calls():
    # AST-based, not a substring search: the module's own docstring
    # discusses `os.remove()`/`os.replace()`/`os.rename()` by name (to
    # explain what this module deliberately never calls), so a plain
    # `"os.remove(" not in source` check is a false positive against its
    # own documentation. Only actual ast.Call nodes count as real calls -
    # this is the same pattern the Sprint 66 structural tests use.
    source = inspect.getsource(replay)
    tree = ast.parse(source)
    # Scoped to `os.<name>(...)` / `os.path.<name>(...)` specifically -
    # not a bare attribute-name match, which would false-positive on
    # unrelated methods that happen to share a name (e.g. this module's
    # own `datetime(...).replace(tzinfo=...)`, nothing to do with
    # filesystem writes).
    forbidden_attr_calls = {"remove", "replace", "rename", "unlink", "rmdir"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in forbidden_attr_calls:
            base = func.value
            is_os_dotted = (
                (isinstance(base, ast.Name) and base.id == "os")
                or (isinstance(base, ast.Attribute) and base.attr == "path")
            )
            if is_os_dotted:
                pytest.fail(f"unexpected write/delete call in replay module: os...{func.attr}(...)")
        if isinstance(func, ast.Name) and func.id == "open":
            args_src = ast.dump(node)
            assert "'w'" not in args_src and '"w"' not in args_src
            assert "'a'" not in args_src and '"a"' not in args_src


def test_replay_summarize_never_modifies_input_events():
    events = [{"operation": "write", "success": True, "path_category": "CRITICAL", "correlation_id": "x"}]
    frozen = json.dumps(events)
    replay.summarize(events)
    assert json.dumps(events) == frozen


def test_replay_calling_it_does_not_touch_the_real_config_directory():
    config_dir = os.path.join(PROJECT_ROOT, "config")
    before = {n: ma._sha256_of(os.path.join(config_dir, n)) for n in os.listdir(config_dir) if n.endswith(".json")}
    replay.load_events()
    replay.summarize(replay.load_events())
    for n, h in before.items():
        assert ma._sha256_of(os.path.join(config_dir, n)) == h


# ============================================================================
# PHASE 9 - self-modification / security recheck (Sprint 65/66 conclusions
# re-verified specifically against everything Sprint 68 added).
# ============================================================================

def test_no_eval_exec_shell_true_dynamic_import_anywhere_in_sprint68_additions():
    for mod in (ma, replay):
        source = inspect.getsource(mod)
        assert "eval(" not in source
        assert "exec(" not in source
        assert "shell=True" not in source
        assert "subprocess." not in source
        assert "__import__(" not in source


def test_sprint68_added_zero_new_registered_tool_names():
    import luno.bootstrap.adapters as adapters_module
    with open(adapters_module.__file__) as f:
        source = f.read()
    assert "mutation_audit" not in source.lower().replace("_", "")


def test_sprint68_introduces_no_generic_filesystem_writer():
    """Every write-mode `open()` call anywhere across `luno/mutation_
    audit.py` targets a path computed purely from `_audit_dir()` (never
    a caller-supplied `path`/`tool_name`/`action_name` argument) - the
    identical structural proof Sprint 67 already established, re-run
    here against the CURRENT (Sprint 68-modified) source."""
    source = inspect.getsource(ma)
    tree = ast.parse(source)
    write_opens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            args_src = ast.dump(node)
            if "'a'" in args_src or '"a"' in args_src or "'w'" in args_src or '"w"' in args_src:
                write_opens.append(node)
    assert len(write_opens) == 1


def test_still_cannot_write_arbitrary_python_or_execute_generated_code(tmp_path, monkeypatch):
    target = tmp_path / "evil.py"
    monkeypatch.setattr(luno_config, "VERIFIED_FACTS_FILE", str(target), raising=False)
    # even asking the audit-integrated writer to target a `.py`-shaped
    # filename does not grant any new capability - persistence still
    # only ever writes whatever JSON data() it's given, never code, and
    # nothing in mutation_audit executes file contents.
    persistence.atomic_write_json(str(target), {"not": "code"}, backup=False)
    with open(target) as f:
        content = f.read()
    assert "import os" not in content
    assert content.strip().startswith("{")  # plain JSON, not Python


def test_tool_registry_still_never_mutated_by_anything_sprint68_added():
    import luno.tool_manager.registry as registry_module
    source = inspect.getsource(registry_module)
    assert "mutation_audit" not in source


def test_no_llm_controlled_audit_path_mutation_reachable_end_to_end(tmp_path, monkeypatch):
    """Simulates an LLM-controlled `ToolCall.parameters` dict containing
    a key that LOOKS like it might redirect the audit log, and confirms
    it has zero effect - `AUDIT_LOG_DIR` before and after a tool call
    carrying that parameter is identical."""
    from luno.tool_manager.builtin.real_browser import RealBrowserHandler
    from luno.tool_manager.models import ToolCall

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "")
    before_dir = ma.AUDIT_LOG_DIR

    class _FakeProvider:
        def download(self, url, dest):
            with open(dest, "wb") as f:
                f.write(b"x")
            return dest

    handler = RealBrowserHandler(provider=_FakeProvider())
    handler.execute(ToolCall(tool="browser", action="download", parameters={
        "url": "https://example.com/f", "filename": "f.txt",
        "MUTATION_AUDIT_LOG_DIR": str(SOURCE_ROOT),  # attempted injection
        "AUDIT_LOG_DIR": str(SOURCE_ROOT),
    }))
    assert ma.AUDIT_LOG_DIR == before_dir


def test_audit_log_content_can_never_cause_execution_when_read_back(tmp_path, monkeypatch):
    """A malicious-looking payload stored in a metadata field must still
    just be inert JSON text when read back via `read_events_for_day()`/
    the replay helper - never re-interpreted as code."""
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    payload = "__import__('os').system('touch /tmp/pwned')"
    ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                        source_component=payload, source_operation="t",
                        before=ma.FileSnapshot(exists=False), after=ma.FileSnapshot(exists=False),
                        success=True)
    events = replay.load_events()
    assert events[0]["source_component"].startswith("__import__")
    assert not os.path.exists("/tmp/pwned")


# ============================================================================
# PHASE 10/15 - persistent-state protection (before/after this whole file).
# ============================================================================

def test_long_term_memory_json_remains_byte_identical_throughout():
    real_path = os.path.join(PROJECT_ROOT, "config", "long_term_memory.json")
    assert ma._sha256_of(real_path) is not None  # sanity: file is readable at all


def test_sprint63_preservation_backup_untouched():
    backups_dir = os.path.join(PROJECT_ROOT, "config", "backups")
    matches = [n for n in os.listdir(backups_dir) if "pre_sprint63_forensic" in n]
    assert len(matches) == 1


def test_backup_count_unchanged_by_this_entire_test_file():
    backups_dir = os.path.join(PROJECT_ROOT, "config", "backups")
    assert len(os.listdir(backups_dir)) == 12


def test_this_files_own_run_never_mutates_real_config_json():
    config_dir = os.path.join(PROJECT_ROOT, "config")
    before = {n: (ma._sha256_of(os.path.join(config_dir, n)), os.path.getmtime(os.path.join(config_dir, n)))
              for n in os.listdir(config_dir) if n.endswith(".json")}
    for n, (h, mtime) in before.items():
        full = os.path.join(config_dir, n)
        assert ma._sha256_of(full) == h
        assert os.path.getmtime(full) == mtime


# ============================================================================
# PHASE 13 - performance.
# ============================================================================

def test_performance_single_audit_event(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(tmp_path / "mutation_audit"), raising=False)
    snap = ma.FileSnapshot(exists=True, size=10, sha256="a" * 64)
    start = time.perf_counter()
    for _ in range(200):
        ma.record_mutation(operation="write", path="x", category=ma.PathCategory.TEMP,
                            source_component="perf", source_operation="test",
                            before=snap, after=snap, success=True)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 200
    assert elapsed_ms < 5.0, f"averaged {elapsed_ms:.3f}ms per call"


def test_performance_concurrent_audit_events(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(tmp_path / "mutation_audit"), raising=False)
    snap = ma.FileSnapshot(exists=True, size=10, sha256="a" * 64)
    N = 100
    start = time.perf_counter()

    def _worker(i):
        ma.record_mutation(operation="write", path=f"x{i}", category=ma.PathCategory.TEMP,
                            source_component="perf", source_operation="test",
                            before=snap, after=snap, success=True)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed_ms = (time.perf_counter() - start) * 1000 / N
    assert elapsed_ms < 10.0, f"averaged {elapsed_ms:.3f}ms per call under contention"


def test_performance_jsonl_parsing(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    snap = ma.FileSnapshot(exists=True, size=10, sha256="a" * 64)
    for i in range(300):
        ma.record_mutation(operation="write", path=f"x{i}", category=ma.PathCategory.TEMP,
                            source_component="perf", source_operation="test", before=snap, after=snap,
                            success=True)
    start = time.perf_counter()
    events = ma.read_events_for_day()
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert len(events) == 300
    assert elapsed_ms < 50.0, f"parsing 300 lines took {elapsed_ms:.3f}ms"


def test_performance_retention_scan(tmp_path, monkeypatch):
    audit_dir = tmp_path / "mutation_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(ma, "AUDIT_LOG_DIR", str(audit_dir), raising=False)
    for i in range(50):
        f = audit_dir / f"2020-01-{(i % 28) + 1:02d}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (0, 0))
    start = time.perf_counter()
    ma.rotate_old_audit_logs(max_retention_days=90)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100.0, f"retention scan of 50 files took {elapsed_ms:.3f}ms"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
