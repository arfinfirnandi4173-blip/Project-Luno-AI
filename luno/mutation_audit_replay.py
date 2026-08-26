"""
mutation_audit_replay.py
==========================

Sprint 68 (Mutation Audit Trail Verification & Hardening), Phase 8 - a
small, STRICTLY READ-ONLY helper for reconstructing a mutation timeline
from `logs/mutation_audit/*.jsonl`. No UI, no write access anywhere in
this module - every function here only ever calls `open(path, "r", ...)`
(never `"w"`/`"a"`) and never `os.remove()`/`os.replace()`/`os.rename()`.
This is enforced structurally, not just by convention -
`tests/test_sprint68_mutation_audit_hardening.py`'s own Phase 9 tests
assert no write-mode file operation exists anywhere in this module's
source.

WHY THIS IS JUSTIFIED (Phase 8's own "if useful, add a small read-only
helper... if the existing schema is sufficient and no helper is
justified, document that decision instead"): Sprint 68's own Phase 6
addition (`luno.mutation_audit.record_pending_mutation()` - a "pending"
record appended before a CRITICAL mutation, paired by `correlation_id`
with the normal "completed" record after) is only forensically useful
if something can actually PAIR them up and flag an orphan - reading the
raw JSONL by hand to find a `"write:pending"` line with no matching
`"write"` line for the same `correlation_id` is exactly the kind of
mechanical task this module exists to do instead. Filtering by path/
correlation ID/component and chronological ordering are the other
obviously-useful primitives a human (or a future agent) doing forensic
review would otherwise reimplement ad hoc every time.

DOES NOT DUPLICATE `luno.mutation_audit.read_events_for_day()` - that
function already exists (Sprint 67) and stays the single-day primitive
this module's own `load_events()` is built on top of (loops it across a
day range) - not reimplemented here.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import mutation_audit as _ma


def _iter_day_strings(start_day: Optional[str], end_day: Optional[str]) -> List[str]:
    """Inclusive `[start_day, end_day]` range of `YYYY-MM-DD` strings, or
    just today if both are omitted. Pure date arithmetic - no filesystem
    access, never raises on a malformed date (falls back to `[today]`)."""
    if start_day is None and end_day is None:
        return [_ma._today_str()]
    try:
        start = datetime.strptime(start_day or end_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_day or start_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return [_ma._today_str()]
    if end < start:
        start, end = end, start
    days = []
    cursor = start
    # Bounded to 400 days so a badly-formed range can never turn into an
    # unbounded loop - generous enough for any realistic forensic query
    # against a 90-day-retention audit trail (Phase 12's own policy).
    for _ in range(400):
        days.append(cursor.strftime("%Y-%m-%d"))
        if cursor >= end:
            break
        cursor += timedelta(days=1)
    return days


def load_events(start_day: Optional[str] = None, end_day: Optional[str] = None
                 ) -> List[Dict[str, Any]]:
    """Loads every parseable event across `[start_day, end_day]` (both
    `YYYY-MM-DD`, inclusive; defaults to just today). Read-only - never
    creates, modifies, or deletes any file. Malformed lines are silently
    skipped (same policy as `mutation_audit.read_events_for_day()`,
    which this function calls once per day in range) - use
    `count_malformed_lines()` below if the malformed-line COUNT itself
    is what a caller wants to know."""
    events: List[Dict[str, Any]] = []
    for day in _iter_day_strings(start_day, end_day):
        events.extend(_ma.read_events_for_day(day))
    return events


def count_malformed_lines(day: Optional[str] = None) -> int:
    """Phase 8's own "detect malformed lines" requirement, made
    reportable rather than silently absorbed: re-reads the same file
    `read_events_for_day()` does, but counts non-blank lines that fail
    to parse as JSON instead of skipping them silently. Read-only.
    Returns 0 if the file doesn't exist or can't be opened - never
    raises."""
    directory = _ma._audit_dir()
    path = os.path.join(directory, f"{day or _ma._today_str()}.jsonl")
    malformed = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json
                    json.loads(line)
                except Exception:
                    malformed += 1
    except Exception:
        return 0
    return malformed


def filter_by_path(events: Iterable[Dict[str, Any]], path: str) -> List[Dict[str, Any]]:
    """Exact match against the CANONICALIZED form Sprint 68's own
    `mutation_audit._canonicalize_for_storage()` writes into every
    record's `path` field - callers should pass the same kind of path
    (absolute, or let this function canonicalize it the same way)."""
    canonical = _ma._canonicalize_for_storage(path)
    return [e for e in events if e.get("path") == canonical]


def filter_by_correlation_id(events: Iterable[Dict[str, Any]], correlation_id: str
                              ) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("correlation_id") == correlation_id]


def filter_by_source_component(events: Iterable[Dict[str, Any]], source_component: str
                                ) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("source_component") == source_component]


def order_chronologically(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sorts by `timestamp` (ISO-8601 strings sort correctly
    lexicographically when generated consistently, as `mutation_audit.
    _now_iso()` always does - UTC, fixed-width millisecond precision) -
    a plain string sort, not a datetime re-parse, so a single malformed/
    missing timestamp on one event degrades to "sorts as the empty
    string" rather than raising and losing the whole list."""
    return sorted(events, key=lambda e: e.get("timestamp") or "")


def find_orphaned_pending_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Phase 8's own "report missing mutation pairs" requirement,
    specifically for the Phase 6/Sprint 68 pending/completed pairing:
    returns every `"<op>:pending"` event whose `correlation_id` has NO
    matching completed event (`operation == "<op>"`, the same base name
    with the `":pending"` suffix stripped) anywhere in `events`. This is
    the concrete, detectable signature of Sprint 67's own documented
    Phase 10.D blind spot (mutation succeeded, its completed audit
    record failed to append) actually occurring - an empty return value
    does NOT prove it never happened (a process that crashed between the
    pending write and ANY further audit activity, including this read,
    would simply show a pending record with no completion, which correct
    behavior of this function to still surface) - it means no orphan is
    currently visible in the range queried."""
    by_correlation: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        cid = e.get("correlation_id")
        if cid:
            by_correlation.setdefault(cid, []).append(e)

    orphans: List[Dict[str, Any]] = []
    for cid, group in by_correlation.items():
        pending = [e for e in group if str(e.get("operation", "")).endswith(":pending")]
        for p in pending:
            base_op = str(p["operation"])[: -len(":pending")]
            has_completion = any(e.get("operation") == base_op for e in group)
            if not has_completion:
                orphans.append(p)
    return orphans


def summarize(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A small, read-only rollup - counts by operation/success/category,
    plus the orphaned-pending count - useful as a one-call sanity check
    without a caller having to compose the primitives above by hand."""
    events = list(events)
    by_operation: Dict[str, int] = {}
    success_count = 0
    failure_count = 0
    by_category: Dict[str, int] = {}
    for e in events:
        op = str(e.get("operation", "unknown"))
        by_operation[op] = by_operation.get(op, 0) + 1
        cat = str(e.get("path_category", "unknown"))
        by_category[cat] = by_category.get(cat, 0) + 1
        # `success` on a "*:pending" record is a placeholder (always
        # `False` - see `mutation_audit.record_pending_mutation()`'s own
        # docstring), never a real outcome - excluded from success/
        # failure tallies so a normal pending-then-completed mutation
        # doesn't get double-counted as one success and one failure.
        if op.endswith(":pending"):
            continue
        if e.get("success"):
            success_count += 1
        else:
            failure_count += 1
    return {
        "total_events": len(events),
        "by_operation": by_operation,
        "by_path_category": by_category,
        "success_count": success_count,
        "failure_count": failure_count,
        "orphaned_pending_count": len(find_orphaned_pending_events(events)),
    }
