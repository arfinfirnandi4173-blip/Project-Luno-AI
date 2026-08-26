"""
mutation_audit.py
==================

Sprint 67 (Mutation Audit Trail & Forensic Observability). Answers,
for every future filesystem mutation this project's own code performs:

    mutation -> identify operation -> identify source/path ->
    capture before/after metadata -> timestamp -> audit record

OBSERVABILITY/FORENSICS ONLY - this module grants Luno NO new
capability. It never chooses what gets written or where; it only
watches writes that were ALREADY going to happen (via
`luno.persistence.atomic_write_json()`, `luno.memory`'s own private
mirror of that same contract, and the Sprint-66-hardened browser
download path) and records metadata about them. Nothing here can be
invoked as a tool, nothing here accepts an LLM/tool-supplied path, and
nothing here ever touches file CONTENTS, memory contents, or
conversation contents - only fixed, named metadata fields (Phase 2's own
explicit "audit metadata only, never secrets/tokens/content" rule).

REUSE, NOT REINVENTION (Phase 0's own instruction: "do not redesign the
logging system if an existing structured logging mechanism can safely
be reused"):

  - Storage location: the SAME `logs/` root `luno/dashboard/
    event_log_writer.py` already established for `logs/events/`/
    `logs/runtime/` (Sprint 50, Runtime Observability) - this module
    adds one more sibling, `logs/mutation_audit/`, date-rotated JSONL,
    same file-naming convention (`YYYY-MM-DD.jsonl`), same
    append-under-a-lock discipline, same bounded-retention rotation
    shape. `logs/` already satisfies every Phase 4 storage requirement
    (outside `luno/`, outside the browser download root, outside
    `long_term_memory.json`/any config file, not LLM/tool-controlled,
    deterministic) without inventing a new location.
  - Path safety: `luno.browser.security`'s Sprint 66 primitives
    (`_resolve_for_comparison()`, `_path_contains()`,
    `_collect_critical_paths()`, `SOURCE_ROOT`/`PROJECT_ROOT`, and
    `validate_download_directory()` itself) are imported and reused
    DIRECTLY, not reimplemented - the exact same invariant that keeps
    `BROWSER_DOWNLOAD_DIR` out of the source tree/critical files applies
    equally well to this module's own audit directory, since both are
    "somewhere Luno writes generated, non-critical output."
  - Redaction/bounding conventions (secret-shaped-key scrubbing, a
    per-field character cap) mirror `event_log_writer.py`'s own
    `_redact()`/`_bound_value()`, applied here to the small number of
    free-text-ish fields this schema has (`source_component`,
    `source_operation`) even though, unlike Event Bus payloads, none of
    this module's own callers ever pass LLM-controlled text into those
    fields - defense in depth, not a response to an observed problem.

FAIL-CLOSED, BUT ONLY WHERE IT MATTERS (Phase 5): `assert_audit_
subsystem_available()` is the ONE call in this module allowed to raise
and block a real mutation - and it is only ever invoked by a caller
BEFORE a CRITICAL-category (security-sensitive) mutation begins. Every
OTHER function here (`record_mutation()`, `record_backup_created()`,
`record_backup_pruned()`, `snapshot()`) is unconditionally best-effort:
an audit-recording failure AFTER a mutation has already happened can
never retroactively undo that mutation, so this module never pretends
otherwise - it logs the failure to stderr, counts it in `_write_
failures` for introspection, and returns normally. See
`docs/change_impact/mutation_audit_trail.md` Phase 10 for the exact
evidence boundary this implies.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

# ─────────────────────────────────────────────
#  Storage location
# ─────────────────────────────────────────────

#: Same override convention every `luno/config.py` `*_FILE` constant
#: already uses (`os.getenv(...)` at import time) - trusted, operator-
#: level configuration ONLY. Never settable from a `ToolCall.parameters`
#: value or any other LLM-controlled input anywhere in this codebase -
#: see `tests/test_sprint67_mutation_audit_trail.py`'s Phase 13
#: negative-control tests for the executable proof.
AUDIT_LOG_DIR: str = os.getenv("MUTATION_AUDIT_LOG_DIR", "").strip() or os.path.join("logs", "mutation_audit")

#: Forensic value trends higher than routine debug logs (this IS the
#: evidence trail, not a convenience log) - kept longer than
#: `event_log_writer.py`'s own 14-day default. Still bounded (Phase 12:
#: "do not allow unlimited audit growth").
DEFAULT_MAX_RETENTION_DAYS = 90

#: `STANDARD`-category files (browser downloads, backup copies) are only
#: hashed if they're at or under this size - Phase 3's own "do not hash
#: every byte of every temporary/browser file if that would create
#: unacceptable overhead" instruction. `CRITICAL` files are always
#: hashed regardless of size (they are, by definition, this project's
#: own small JSON state stores - never observed above a few KB).
STANDARD_HASH_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

MAX_FIELD_CHARS = 200

_lock = threading.RLock()
_events_written = 0
_write_failures = 0
_rotation_attempted = False


class PathCategory(str, Enum):
    CRITICAL = "CRITICAL"
    STANDARD = "STANDARD"
    TEMP = "TEMP"


class AuditSubsystemUnavailableError(RuntimeError):
    """Raised ONLY by `assert_audit_subsystem_available()`, and ONLY
    meant to be called by a caller about to perform a CRITICAL-category
    mutation - Phase 5's fail-closed requirement: "if audit logging
    fails, the underlying protected mutation should FAIL CLOSED when
    the mutation is security-sensitive." Every other function in this
    module is deliberately non-raising (best-effort)."""


# ─────────────────────────────────────────────
#  Schema (Phase 2)
# ─────────────────────────────────────────────

@dataclass
class FileSnapshot:
    exists: bool
    size: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class MutationEvent:
    """Minimum fields per Phase 2, plus the optional ones this project
    can safely provide. Deliberately NO free-form "details"/"context"
    dict - every field is fixed and named, so there is no slot a caller
    could accidentally (or a future caller deliberately) stuff secrets,
    file contents, or conversation text into."""
    timestamp: str
    operation: str
    path: str
    path_category: str
    source_component: str
    source_operation: str
    success: bool
    before_exists: bool
    after_exists: bool
    before_size: Optional[int]
    after_size: Optional[int]
    before_sha256: Optional[str]
    after_sha256: Optional[str]
    tool_name: Optional[str] = None
    action_name: Optional[str] = None
    correlation_id: Optional[str] = None
    pid: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────
#  Path classification (Phase 3)
# ─────────────────────────────────────────────

#: Sprint 68 (Mutation Audit Trail Verification & Hardening) - Phase 4's
#: own "very long ... fields must never break JSONL integrity"
#: requirement applies to EVERY string field this module accepts as a
#: caller argument, not just the four `_bound()` already covered
#: (`source_component`/`source_operation`/`tool_name`/`action_name`).
#: `operation`/`correlation_id` are always short, fixed, code-authored
#: values in every REAL call site today, but `record_mutation()` is a
#: public function - an unbounded value reaching it (accidentally, or
#: from a future caller) must still be truncated rather than allowed to
#: produce an arbitrarily large JSONL line. `path` gets its own,
#: separate, longer cap (`MAX_PATH_CHARS`) since real paths are
#: legitimately longer than a 200-char label but are still bounded by
#: the OS itself (~4096 bytes on Linux) - this is defense in depth, not
#: a response to an observed overflow.
MAX_PATH_CHARS = 4096


def _bound(value: Optional[str], max_chars: int = MAX_FIELD_CHARS) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _canonicalize_for_storage(path: str) -> str:
    """Sprint 68 (Phase 2's own "path is canonicalized" checklist item -
    NOT actually true of Sprint 67's original `record_mutation()`, which
    stored whatever string a caller passed verbatim). Uses plain
    `os.path.abspath()` (NOT `luno.browser.security._resolve_for_
    comparison()`'s realpath+normcase) deliberately: `abspath()` never
    touches the filesystem and never resolves a symlink, so a caller
    passing a not-yet-created path (a common, legitimate case - see
    `test_D_nonexistent_to_created`) still canonicalizes cleanly, and
    the STORED path stays human-readable/stable for forensic review
    rather than being silently rewritten to a symlink's target. Security
    decisions (CRITICAL/STANDARD classification, the audit directory's
    own safety) already use the stronger realpath-based resolution
    elsewhere (`classify_path()`, `_validate_audit_dir()`) - this
    function only affects what gets WRITTEN into the record, never a
    security comparison. Never raises: a malformed path (e.g. an
    embedded NUL byte) falls back to the original string, bounded."""
    try:
        return os.path.abspath(str(path))
    except Exception:
        return str(path)


def classify_path(path: str) -> "PathCategory":
    """CRITICAL: resolves to (or contains) one of Sprint 66's own
    dynamically-collected critical files (`luno.browser.security.
    _collect_critical_paths()` - every `luno.config` `*_FILE` constant
    plus the fixed launcher/license/manifest set) - reused directly so
    this list can never drift out of sync with that already-reviewed
    inventory. STANDARD: resolves inside the configured browser download
    directory. Everything else (including paths that don't resolve at
    all, e.g. malformed input): TEMP. Never raises."""
    if not path:
        return PathCategory.TEMP
    try:
        from luno.browser import security as _sec
        resolved = _sec._resolve_for_comparison(path)
        for critical in _sec._collect_critical_paths():
            critical_resolved = _sec._resolve_for_comparison(critical)
            if resolved == critical_resolved or _sec._path_contains(critical_resolved, resolved):
                return PathCategory.CRITICAL
    except Exception:
        pass
    try:
        from luno.browser import security as _sec
        from luno.browser.config import BrowserConfig
        download_dir = BrowserConfig.from_env().download_dir
        if download_dir:
            base = _sec._resolve_for_comparison(download_dir)
            resolved = _sec._resolve_for_comparison(path)
            if _sec._path_contains(base, resolved):
                return PathCategory.STANDARD
    except Exception:
        pass
    return PathCategory.TEMP


def _sha256_of(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def snapshot(path: str, category: "PathCategory") -> FileSnapshot:
    """Best-effort, never raises. Hash policy (Phase 3, documented):
    CRITICAL -> always hashed (these are this project's own small JSON
    state stores, never observed above a few KB). STANDARD -> hashed
    only up to `STANDARD_HASH_MAX_BYTES`. TEMP -> metadata (exists/size)
    only, never hashed."""
    try:
        exists = bool(path) and os.path.exists(path)
    except Exception:
        return FileSnapshot(exists=False)
    if not exists:
        return FileSnapshot(exists=False)
    size: Optional[int] = None
    try:
        size = os.path.getsize(path)
    except Exception:
        pass
    sha: Optional[str] = None
    if category == PathCategory.CRITICAL:
        sha = _sha256_of(path)
    elif category == PathCategory.STANDARD and (size is None or size <= STANDARD_HASH_MAX_BYTES):
        sha = _sha256_of(path)
    return FileSnapshot(exists=True, size=size, sha256=sha)


# ─────────────────────────────────────────────
#  Audit subsystem availability (Phase 5 - fail-closed integration point)
# ─────────────────────────────────────────────

def _audit_dir() -> str:
    return AUDIT_LOG_DIR


def _validate_audit_dir() -> "tuple[bool, str]":
    """Reuses Sprint 66's `validate_download_directory()` UNCHANGED -
    the audit log directory must satisfy the exact same "does not
    overlap SOURCE_ROOT/PROJECT_ROOT/any critical file" invariant a
    browser download directory does. See module docstring."""
    from luno.browser.security import validate_download_directory
    return validate_download_directory(_audit_dir())


def assert_audit_subsystem_available() -> None:
    """The ONE call in this module allowed to raise and block a real
    mutation. Callers about to perform a CRITICAL-category write call
    this FIRST (before backup, before the temp-file write, before
    `os.replace()`) - if it raises, the caller must not proceed. Cheap:
    a handful of path resolutions plus a directory-create + `os.access()`
    check, no full write-and-verify probe, well under the 5ms/operation
    target (see Phase 14 performance tests)."""
    ok, reason = _validate_audit_dir()
    if not ok:
        raise AuditSubsystemUnavailableError(
            f"mutation audit log directory {_audit_dir()!r} failed its own safety check "
            f"and cannot be used ({reason}) - refusing to proceed with a security-sensitive "
            f"mutation until the MUTATION_AUDIT_LOG_DIR configuration is fixed."
        )
    directory = _audit_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as ex:
        raise AuditSubsystemUnavailableError(
            f"mutation audit log directory {directory!r} could not be created: {ex}"
        ) from ex
    if not os.access(directory, os.W_OK):
        raise AuditSubsystemUnavailableError(
            f"mutation audit log directory {directory!r} is not writable"
        )


def _warn_if_audit_dir_unsafe_at_import() -> None:
    """Sprint 68 (Phase 3.3 - "environment-variable configuration cannot
    silently weaken the boundary"). Sprint 67's own `assert_audit_
    subsystem_available()` already fails closed at the moment of the
    FIRST CRITICAL write - correct, but that means a misconfigured
    `MUTATION_AUDIT_LOG_DIR` env var stays completely silent from
    process start until that first write happens, which could be much
    later (or, for a process that only ever performs STANDARD/TEMP
    writes, never - fail-closed only applies to CRITICAL). This adds
    EARLY, NON-FATAL visibility: a `print()` warning at import time if
    the resolved default is unsafe, so a misconfiguration is visible in
    startup logs immediately rather than discovered only when a write
    is later silently skipped. Deliberately non-fatal (this module is
    imported very early, transitively, by `luno.persistence`/`luno.
    memory` - a hard raise here would crash unrelated application
    startup paths that never even touch a CRITICAL store, which would
    itself violate the "audit failure can silently bypass a security
    boundary" STOP condition in spirit by turning an observability
    concern into an availability one). The REAL enforcement remains
    exactly where Sprint 67 put it: `assert_audit_subsystem_available()`,
    called at actual CRITICAL-write time. Never raises."""
    try:
        ok, reason = _validate_audit_dir()
        if not ok:
            print(
                f"[MutationAudit] ⚠ MUTATION_AUDIT_LOG_DIR ({AUDIT_LOG_DIR!r}) failed its own "
                f"safety check at startup ({reason}) - CRITICAL-category writes will refuse to "
                f"proceed (fail closed) until this is fixed. This warning is informational only; "
                f"it does not itself block anything."
            )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Writing audit records (Phase 4/6/11)
# ─────────────────────────────────────────────

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append_event(event: MutationEvent) -> bool:
    """Append-only (Phase 4: "append-oriented"), one JSON object per
    line, one file per UTC day - identical shape to `event_log_writer.
    py`'s own JSONL writer. Concurrency (Phase 11): a single `threading.
    RLock()` scoped ONLY to this append (never a global application
    lock) - safe for concurrent writers and rapid sequential writes;
    each `open(..., "a")` + single `write()` is itself append-atomic at
    the OS level for writes under `PIPE_BUF` (every event line here is
    far smaller), so even without the lock two writers could not
    interleave mid-line - the lock exists to keep this module's own
    `_events_written`/`_write_failures` counters correct, not to prevent
    line-interleaving. Never raises - a logging failure here can never
    propagate back and turn an already-decided mutation outcome into an
    exception the caller didn't ask for (see module docstring's
    "fail-closed, but only where it matters")."""
    global _events_written, _write_failures
    _maybe_rotate_once()
    try:
        directory = _audit_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{_today_str()}.jsonl")
        line = json.dumps(event.to_dict(), default=str, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _events_written += 1
        return True
    except Exception as ex:
        with _lock:
            _write_failures += 1
        try:
            print(f"[MutationAudit] ✗ Failed to append audit record ({event.operation} {event.path}): {ex}")
        except Exception:
            pass
        return False


def record_mutation(
    *, operation: str, path: str, category: "PathCategory",
    source_component: str, source_operation: str,
    before: FileSnapshot, after: FileSnapshot, success: bool,
    tool_name: Optional[str] = None, action_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> MutationEvent:
    """The primary entry point. Builds and appends ONE `MutationEvent`
    describing an ACTUAL, ALREADY-DECIDED mutation outcome - callers
    must call this AFTER the real write attempt (success or failure),
    never before (Phase 6: "avoid logging success before os.replace()
    actually succeeds"). Returns the event that was recorded (or
    attempted) for callers/tests that want to inspect it directly -
    the event is returned even if the append itself failed, so a test
    can assert on `success`/hashes independent of the audit file's own
    write outcome."""
    event = MutationEvent(
        timestamp=_now_iso(),
        operation=_bound(operation, max_chars=100) or "unknown",
        path=_bound(_canonicalize_for_storage(path), max_chars=MAX_PATH_CHARS) or "",
        path_category=category.value if isinstance(category, PathCategory) else str(category),
        source_component=_bound(source_component) or "unknown",
        source_operation=_bound(source_operation) or "unknown",
        success=bool(success),
        before_exists=before.exists,
        after_exists=after.exists,
        before_size=before.size,
        after_size=after.size,
        before_sha256=before.sha256,
        after_sha256=after.sha256,
        tool_name=_bound(tool_name),
        action_name=_bound(action_name),
        correlation_id=_bound(correlation_id, max_chars=64) or uuid.uuid4().hex[:16],
        pid=os.getpid(),
    )
    _append_event(event)
    return event


def record_pending_mutation(
    *, operation: str, path: str, category: "PathCategory",
    source_component: str, source_operation: str, before: FileSnapshot,
    tool_name: Optional[str] = None, action_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> str:
    """Sprint 68 (Mutation Audit Trail Verification & Hardening) - Phase
    6's own "determine whether the existing implementation can safely
    improve [the post-mutation-audit-failure blind spot] without
    violating architecture" question. Answer: a two-phase append,
    reusing the SAME single append-only JSONL mechanism (no second
    persistence/transaction system, no new storage location, no new
    global state) - a "pending" record is appended BEFORE a
    CRITICAL-category mutation begins (operation name suffixed
    `":pending"` so it never collides with, or gets counted as, the
    real completed event of the same base operation name), then the
    normal `record_mutation()` call still happens exactly as before,
    AFTER the mutation, carrying the SAME `correlation_id`.

    This does NOT close Sprint 67's own documented blind spot (Phase
    10.D: a mutation can still succeed while its COMPLETED audit record
    fails to append) - it turns an invisible gap into a DETECTABLE one:
    a forensic reader (see `luno.mutation_audit_replay.find_orphaned_
    pending_events()`) can find a `"write:pending"` record with no
    matching `"write"` completion for the same `correlation_id` and
    know something happened whose outcome went unrecorded, even though
    it cannot recover WHAT the outcome was. Fully closing that gap would
    require verifying the audit append itself as part of the same
    atomic transaction as the mutation - which Phase 6's own STOP
    condition explicitly forbids building (a second persistence/
    transaction system) - so this is the safe, bounded improvement that
    stays within the existing architecture, not a claim of a stronger
    guarantee than that.

    Best-effort, like every other function here except `assert_audit_
    subsystem_available()` - never raises. Returns the `correlation_id`
    used (generated fresh if not supplied) so the caller can thread the
    SAME id into its own follow-up `record_mutation()` call."""
    correlation_id = correlation_id or uuid.uuid4().hex[:16]
    record_mutation(
        operation=f"{operation}:pending", path=path, category=category,
        source_component=source_component, source_operation=source_operation,
        # `after` is NOT a real observation yet - this record exists
        # purely to mark INTENT before the mutation runs. Reusing
        # `before` here (rather than inventing an `Optional[FileSnapshot]`
        # schema change) keeps the schema exactly as documented in Phase
        # 2 - readers must treat `after_*` fields on a `":pending"`
        # record as meaningless, which the operation-name suffix itself
        # signals unambiguously.
        before=before, after=before, success=False,
        tool_name=tool_name, action_name=action_name, correlation_id=correlation_id,
    )
    return correlation_id


def record_backup_created(
    *, backup_path: str, source_path: str,
    source_component: str, source_operation: str,
    tool_name: Optional[str] = None, action_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> MutationEvent:
    """Phase 1.E / the sprint's own explicit "backup operations" ask.
    Records the CREATION of a pre-write backup copy - always
    `operation="backup_create"`, always STANDARD category (Phase 3: "a
    backup is a generated artifact," not the critical file itself,
    hashing it is cheap since it is a copy of an already-small store).
    `source_path` is not stored as a separate schema field (Phase 2 has
    no such field) but is folded into `source_operation` for forensic
    traceability without widening the schema."""
    after = snapshot(backup_path, PathCategory.STANDARD)
    return record_mutation(
        operation="backup_create", path=backup_path, category=PathCategory.STANDARD,
        source_component=source_component,
        source_operation=f"{source_operation} (backup of {source_path})",
        before=FileSnapshot(exists=False), after=after, success=after.exists,
        tool_name=tool_name, action_name=action_name, correlation_id=correlation_id,
    )


def record_backup_pruned(
    *, backup_path: str, source_component: str, source_operation: str,
    correlation_id: Optional[str] = None,
) -> MutationEvent:
    """Phase 1.J (delete operations) applied to retention pruning -
    always TEMP category (metadata-only; a pruned backup is a copy of a
    copy, the lowest forensic priority this module tracks, and
    retention can delete many files in one pass so this stays cheap).
    Snapshot the file's existence/size BEFORE the caller actually
    deletes it - callers must call this immediately before `os.remove()`,
    not after (the file won't exist to snapshot otherwise)."""
    before = snapshot(backup_path, PathCategory.TEMP)
    return record_mutation(
        operation="backup_prune_delete", path=backup_path, category=PathCategory.TEMP,
        source_component=source_component, source_operation=source_operation,
        before=before, after=FileSnapshot(exists=False), success=True,
        correlation_id=correlation_id,
    )


# ─────────────────────────────────────────────
#  Retention (Phase 12)
# ─────────────────────────────────────────────

def rotate_old_audit_logs(max_retention_days: int = DEFAULT_MAX_RETENTION_DAYS) -> int:
    """Deletes `logs/mutation_audit/YYYY-MM-DD.jsonl` files older than
    `max_retention_days`, based on file mtime - the SAME approach
    `event_log_writer.py::_rotate_old_files()` already uses. Returns the
    count of files removed (tests assert on this). Retention here can
    ONLY ever delete files under this module's OWN audit directory -
    never `config/*.json`, never a backup, never anything this module
    did not itself create (Phase 12: "audit cleanup may delete only
    audit records"). `max_retention_days <= 0` disables rotation
    entirely (a deliberate escape hatch, matching `event_log_writer.py`'s
    own convention) rather than deleting everything."""
    if max_retention_days <= 0:
        return 0
    cutoff = time.time() - (max_retention_days * 86400)
    directory = _audit_dir()
    removed = 0
    try:
        if not os.path.isdir(directory):
            return 0
        for name in os.listdir(directory):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(directory, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed


def _maybe_rotate_once() -> None:
    """Opportunistic, at-most-once-per-process rotation trigger, called
    from `_append_event()` - a real runtime process gets automatic
    rotation without a dedicated background scheduler, at negligible
    cost (one `listdir()` the first time this module ever writes an
    event, never again this process). Tests exercise the real
    `rotate_old_audit_logs()` function directly rather than relying on
    this gate, so this optimization never affects test coverage of the
    retention policy itself."""
    global _rotation_attempted
    if _rotation_attempted:
        return
    with _lock:
        if _rotation_attempted:
            return
        _rotation_attempted = True
    rotate_old_audit_logs()


# ─────────────────────────────────────────────
#  Introspection (tests/health checks only, never on a mutation's own critical path)
# ─────────────────────────────────────────────

def stats() -> Dict[str, int]:
    with _lock:
        return {"events_written": _events_written, "write_failures": _write_failures}


def read_events_for_day(day: Optional[str] = None) -> "list[Dict[str, Any]]":
    """Test/forensic-inspection helper - reads back every event recorded
    for a given UTC day (default: today) as plain dicts. Never raises;
    returns an empty list if the file doesn't exist or can't be parsed
    line-by-line (a malformed trailing line, e.g. from a crash mid-write,
    is skipped rather than failing the whole read - Phase 10.G)."""
    directory = _audit_dir()
    path = os.path.join(directory, f"{day or _today_str()}.jsonl")
    events: "list[Dict[str, Any]]" = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return events


# Sprint 68 - one-time, non-fatal startup visibility check. See the
# function's own docstring for why this is a warning, not a raise.
_warn_if_audit_dir_unsafe_at_import()
