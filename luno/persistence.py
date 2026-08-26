"""
persistence.py
===============

Generic, domain-agnostic JSON persistence hardening helper -
Persistent State Hardening V2 sprint. Extracted from the pattern
already proven on `config/long_term_memory.json`
(`luno/memory.py`, Memory Recovery & Persistence Hardening sprint),
generalized so every other writable JSON state store in this project
can reuse ONE tested implementation instead of six independent,
partial reimplementations (see
`docs/change_impact/persistent_state_hardening_v2.md` for the full
audit that found this gap).

This module knows NOTHING about domain semantics - not memory
importance, not relationship trust, not habit patterns, not reminders,
not verified facts, not episodic memories. It operates purely on
`(path, data)` where `data` is already a JSON-serializable Python
object (list or dict, whatever the caller's own schema is).
Dependency direction is:

    domain module -> this module -> filesystem

never the reverse - this module must never import any `luno.*` domain
module.

`luno/memory.py`'s own six persistence functions (`_atomic_write_json`,
`_backup_current_memory_file`, `_prune_memory_backups`,
`_load_latest_valid_backup`, `_refuse_if_pytest_targeting_unisolated_path`,
`_MEMORY_BACKUP_RETENTION`) are NOT replaced or rewritten by this
module - they remain the reference implementation, documented as a
CONTRACT in the change-impact doc's Phase 1. This module is a
parallel, reusable extraction of the same contract for the other six
stores.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

#: Backups live in `<same directory as the primary file>/backups/` -
#: co-located with the file they protect, same convention
#: `luno/memory.py`'s own backup directory already uses.
BACKUP_DIR_NAME = "backups"

#: Default retention - keep at most this many timestamped backups per
#: store, oldest pruned first. Never fewer than 1 regardless of this
#: value (see `prune_backups()`'s own `max(1, ...)` floor) - "never
#: delete the last valid backup" holds even if misconfigured to 0.
DEFAULT_RETENTION = 20


class BackupFailedError(RuntimeError):
    """Raised by `atomic_write_json()` when a pre-write backup of an
    EXISTING primary file could not be created - Phase 3's own rule:
    "backup failure HARUS mencegah destructive overwrite." Never raised
    when the primary file doesn't exist yet (nothing to back up, so a
    fresh first write is always allowed through)."""


def _backup_dir(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(path)), BACKUP_DIR_NAME)


def _backup_filename(path: str, now=None) -> str:
    now = now or datetime.now()
    basename = os.path.basename(path)
    stem, ext = os.path.splitext(basename)
    # Microsecond-resolution suffix so two backups within the same
    # second (e.g. a fast test loop, or two saves in one turn) never
    # collide and silently overwrite one another - same convention
    # luno/memory.py's own _memory_backup_filename() uses.
    return f"{stem}.{now.strftime('%Y%m%dT%H%M%S%f')}{ext}"


def backup_dir_for(path: str) -> str:
    """Public accessor - the `backups/` directory a given store's
    backups live in, for callers/tests that want to inspect it
    directly."""
    return _backup_dir(path)


def list_backups(path: str) -> List[str]:
    """Sorted (oldest-first) list of backup filenames for `path`'s
    store. Empty list if the backup directory doesn't exist yet or
    can't be listed - never raises."""
    backup_dir = _backup_dir(path)
    basename = os.path.basename(path)
    stem, ext = os.path.splitext(basename)
    try:
        return sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith(stem + ".") and f.endswith(ext)
        )
    except Exception:
        return []


def prune_backups(path: str, retention: int = DEFAULT_RETENTION,
                   source_component: Optional[str] = None,
                   source_operation: Optional[str] = None) -> None:
    """Keep at most `retention` backups (oldest deleted first), never
    deleting the last remaining one regardless of how `retention` is
    misconfigured. Best-effort - a prune failure never blocks or
    corrupts the primary write (pruning only ever runs AFTER a
    successful backup + write, never before).

    Sprint 67: each successful deletion gets a lightweight (TEMP-
    category, metadata-only) `luno.mutation_audit` record (Phase 1.J -
    "delete operations" - and the sprint's own explicit "backup
    operations" forensic-visibility ask) - a snapshot is taken
    immediately BEFORE each `os.remove()` since the file obviously can't
    be inspected afterward."""
    from . import mutation_audit
    backup_dir = _backup_dir(path)
    entries = list_backups(path)
    keep = max(1, retention)
    excess = len(entries) - keep
    for name in entries[:max(0, excess)]:
        backup_path = os.path.join(backup_dir, name)
        try:
            os.remove(backup_path)
            mutation_audit.record_backup_pruned(
                backup_path=backup_path,
                source_component=source_component or "persistence",
                source_operation=source_operation or "prune_backups",
            )
        except Exception as ex:
            print(f"[Persistence] ✗ Failed to prune old backup {name}: {ex}")


def backup_current_file(path: str) -> Optional[str]:
    """Copies the CURRENT on-disk file at `path` (if any) into
    `backups/` BEFORE any write touches it. A COPY, never a
    move/rename - the original stays exactly where it is until
    `atomic_write_json()` swaps it afterward. Returns the backup's full
    path, or `None` if there was nothing to back up (primary doesn't
    exist yet - not a failure). Raises on a genuine backup failure
    (permissions, disk full, etc.) - the caller (`atomic_write_json`)
    treats that as fatal to the write, per Phase 3."""
    if not os.path.exists(path):
        return None
    backup_dir = _backup_dir(path)
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, _backup_filename(path))
    shutil.copyfile(path, dest)
    return dest


def refuse_if_pytest_targeting_unisolated_path(path: str) -> None:
    """Defense-in-depth guard (Phase 7) - refuses (raises loudly) a
    write made while a pytest test is running (`PYTEST_CURRENT_TEST` is
    set - pytest itself always sets this, nothing else does) to a path
    that is NOT under the system temp directory. In a correctly
    isolated test run (`tests/conftest.py`'s autouse
    `isolate_persistent_state` fixture already redirects every
    writable store's path to a fresh `tmp_path`-derived location before
    every test), this check never trips - it exists purely as a second
    line of defense against the exact failure class the Memory Recovery
    incident was caused by (a bare, non-isolated script/test writing
    straight to a real path). Deliberately inert outside pytest, so
    normal production runtime (`main.py`/`main_runtime_demo.py`, the
    dashboard server) is completely unaffected."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        resolved = os.path.abspath(path)
        tmp_root = os.path.abspath(tempfile.gettempdir())
    except Exception:
        return
    if resolved.startswith(tmp_root):
        return
    raise RuntimeError(
        f"Refusing to write {resolved!r} during a pytest run - this path is not "
        f"under the system temp directory ({tmp_root!r}), so it looks like a real/"
        f"production path rather than an isolated test fixture path. Isolate it via "
        f"tests/conftest.py's isolate_persistent_state fixture (or monkeypatch the "
        f"relevant config.*_FILE constant to a tmp_path) instead of writing here "
        f"directly."
    )


def atomic_write_json(path: str, data: Any, *, backup: bool = True,
                       retention: int = DEFAULT_RETENTION,
                       source_component: Optional[str] = None,
                       source_operation: Optional[str] = None,
                       tool_name: Optional[str] = None,
                       action_name: Optional[str] = None) -> None:
    """Write-temp-then-replace, with an optional pre-write backup -
    Persistent State Hardening V2's core primitive, generalizing
    `luno/memory.py`'s own `_atomic_write_json()`/
    `_backup_current_memory_file()` pattern for use by any JSON-backed
    store.

    Sequence: refuse-if-unisolated-pytest -> Sprint 67 fail-closed audit
    pre-check (CRITICAL-category paths only - see below) -> backup
    current primary (if `backup` and a primary exists) -> write temp
    file in the SAME directory as `path` -> flush -> fsync ->
    `os.replace()` -> prune old backups -> Sprint 67 audit record of
    the ACTUAL outcome. If ANYTHING fails before the final
    `os.replace()`, the original file at `path` is left completely
    untouched - only the throwaway `.tmp` file (and, if backup failed,
    nothing new at all) is affected.

    Raises `BackupFailedError` if `backup=True`, a primary file
    exists, and backing it up fails - Phase 3's "backup failure must
    prevent a destructive overwrite," stricter than `luno/memory.py`'s
    own best-effort backup (a deliberate, documented difference - see
    `docs/change_impact/persistent_state_hardening_v2.md` §4).

    Sprint 67 (Mutation Audit Trail): every call to this function now
    produces exactly one `luno.mutation_audit.MutationEvent` describing
    what actually happened (never logged as a success before
    `os.replace()` genuinely succeeds - see `mutation_audit.py`'s own
    Phase 6 discussion). For every store this function backs
    (`SESSION_SUMMARIES_FILE`, `VERIFIED_FACTS_FILE`, `HABIT_MEMORY_FILE`,
    `EPISODIC_MEMORY_FILE`, `RELATIONSHIP_STATE_FILE`,
    `RESPONSE_DEPTH_PREFERENCE_FILE`, `REMINDERS_FILE`), that path
    classifies as CRITICAL (Sprint 66's own dynamically-collected
    critical-file inventory, reused unchanged), which additionally means
    `mutation_audit.assert_audit_subsystem_available()` is checked
    BEFORE any of the above sequence begins - if the audit subsystem
    itself is unavailable (its own directory unwritable/misconfigured),
    this function refuses to write at all rather than silently
    proceeding unaudited (Phase 5's own fail-closed requirement for
    security-sensitive mutations). `source_component`/`source_operation`/
    `tool_name`/`action_name` are optional forensic context a caller may
    supply - every existing caller of this function keeps working
    unchanged if it omits them (they default to this function's own
    name)."""
    refuse_if_pytest_targeting_unisolated_path(path)

    from . import mutation_audit
    category = mutation_audit.classify_path(path)
    before = mutation_audit.snapshot(path, category)
    correlation_id: Optional[str] = None
    if category == mutation_audit.PathCategory.CRITICAL:
        mutation_audit.assert_audit_subsystem_available()
        # Sprint 68 - a "pending" marker BEFORE the mutation begins, so
        # the crash window between a successful os.replace() and this
        # function's own completed audit record (Phase 10.D's
        # documented blind spot) becomes DETECTABLE by a forensic reader
        # even when it can't be prevented - see `mutation_audit.record_
        # pending_mutation()`'s own docstring for why this doesn't
        # require a second persistence system.
        correlation_id = mutation_audit.record_pending_mutation(
            operation="write", path=path, category=category,
            source_component=source_component or "persistence",
            source_operation=source_operation or "atomic_write_json",
            before=before, tool_name=tool_name, action_name=action_name,
        )

    success = False
    try:
        if backup:
            try:
                backup_path = backup_current_file(path)
            except Exception as ex:
                raise BackupFailedError(
                    f"Refusing to write {path!r} - pre-write backup of the existing "
                    f"primary file failed: {ex}"
                ) from ex
            else:
                if backup_path:
                    mutation_audit.record_backup_created(
                        backup_path=backup_path, source_path=path,
                        source_component=source_component or "persistence",
                        source_operation=source_operation or "atomic_write_json",
                        tool_name=tool_name, action_name=action_name,
                        correlation_id=correlation_id,
                    )

        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

        success = True
        if backup:
            prune_backups(path, retention=retention, source_component=source_component,
                           source_operation=source_operation)
    finally:
        after = mutation_audit.snapshot(path, category)
        mutation_audit.record_mutation(
            operation="write", path=path, category=category,
            source_component=source_component or "persistence",
            source_operation=source_operation or "atomic_write_json",
            tool_name=tool_name, action_name=action_name,
            before=before, after=after, success=success,
            correlation_id=correlation_id,
        )


def load_latest_valid_backup(path: str, *, validate: Optional[Callable[[Any], bool]] = None
                              ) -> Tuple[Any, Optional[str]]:
    """Tries each backup for `path`'s store, newest-first, and returns
    the first one that parses as valid JSON AND (if `validate` is
    given) passes the caller's own shape check - e.g.
    `validate=lambda d: isinstance(d, list)`. Returns
    `(data, backup_name)`, or `(None, None)` if no backup exists or
    none are valid. Callers decide what "valid" means for their own
    schema - this helper never assumes list vs. dict."""
    backup_dir = _backup_dir(path)
    for name in reversed(list_backups(path)):
        try:
            with open(os.path.join(backup_dir, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            if validate is None or validate(data):
                return data, name
        except Exception:
            continue
    return None, None


def safe_load_json(path: str, default: Any, *, validate: Optional[Callable[[Any], bool]] = None,
                    recover_from_backup: bool = False, log_prefix: Optional[str] = None
                    ) -> Tuple[Any, str]:
    """Generic safe-load: missing file / parse failure / (if
    `validate` given) invalid shape all fall back to `default` (a fresh
    copy is NOT made here - callers pass an already-appropriate
    default, e.g. `[]`/`{}`/a fresh dataclass instance, exactly
    matching each store's own pre-existing fallback behavior; this
    function never invents a shape a caller didn't ask for).

    If `recover_from_backup=True` and the primary is missing/invalid,
    tries `load_latest_valid_backup()` before falling back to
    `default` - only meaningful for stores that opt in (Phase 5:
    "implement generic recovery only if it does not change expected
    domain behavior" - most of this sprint's six stores do NOT opt in,
    since they never had a recovery contract before; see the
    change-impact doc for exactly which stores enable this).

    Returns `(data, source)` where `source` is one of `"primary"`,
    `"backup:<name>"`, or `"default"` - always observable, never
    silent about which branch was taken, if `log_prefix` is given
    (prints one line describing the outcome; `log_prefix=None` stays
    fully silent, matching stores whose pre-existing behavior never
    logged on fallback)."""
    def _log(msg: str) -> None:
        if log_prefix:
            print(f"[{log_prefix}] {msg}")

    if not path or not os.path.exists(path):
        return default, "default"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if validate is None or validate(data):
            return data, "primary"
        _log(f"✗ {path} did not match the expected shape - falling back")
    except Exception as ex:
        _log(f"✗ Failed to load {path}: {ex}")

    if recover_from_backup:
        data, name = load_latest_valid_backup(path, validate=validate)
        if data is not None:
            _log(f"✓ Recovered from backup {name}")
            return data, f"backup:{name}"

    return default, "default"
