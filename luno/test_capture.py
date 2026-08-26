"""
test_capture.py
=================

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture) - Phase 7/8/11: turns a REAL interaction that already happened
into a reviewable JSON test-case file, without inventing any new raw-
conversation store.

Two already-existing, already-bounded pieces of state are read (never
extended, never duplicated):

  - `console.conversation_log` (`Deque[Tuple[str, str]]`, `(channel,
    text)`) - already maintained by BOTH `RuntimeDemoConsole` and
    `luno.bootstrap.console.ProductionConsole` for their own `/history`
    command, well before this sprint. This is the ONLY place in this
    project that already holds raw utterance text in memory - reusing it
    here (read-only) is why this module does not need, and does not
    create, a second conversation-text store.
  - `console.planner_module._last_turn_trace` (`MemoryTurnTrace`,
    Sprint-32/49/50) - the bounded, per-conversation, non-text
    classification/decision record every turn already produces.

`mark_test_case()` is the "/mark_test" mechanism the sprint brief asks
for - deliberately a plain, directly-callable function (not a new REPL
parser) so both consoles' own command dispatch can call it with one line
(see `luno/bootstrap/console.py`'s own `/mark_test` handler), and so
`RuntimeDemoConsole`-driven test/replay scripts can call it directly
without going through any text-command layer at all.

Status lifecycle (Phase 11's own data-quality gate): every case is born
`"candidate"` - `candidate -> reviewed -> approved -> rejected` is a
STRICT, enforced enum (`_VALID_STATUSES`); nothing in this module ever
auto-promotes a case past `"candidate"` on its own. Only a human (or a
future tool acting under a human's explicit instruction) calling
`set_case_status(..., "approved")` moves a case into
`tests/real_world/approved/` - the ONLY directory
`replay.py::replay_all()`'s own default regression sweep ever reads (see
that module's own docstring) - so one weird utterance can never become
permanent regression law on its own.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

DEFAULT_BASE_DIR = os.path.join("tests", "real_world")
_VALID_STATUSES = ("candidate", "reviewed", "approved", "rejected")
_MAX_CONVERSATION_TURNS = 12
_MAX_LINE_CHARS = 500
_CASE_ID_RE = re.compile(r"^real_(\d{6})\.json$")


def _bound_text(text: str) -> str:
    if not isinstance(text, str):
        return str(text)
    if len(text) > _MAX_LINE_CHARS:
        return text[:_MAX_LINE_CHARS] + "...[truncated]"
    return text


def _case_dir(base_dir: str, status: str) -> str:
    return os.path.join(base_dir, status)


def _next_case_id(base_dir: str) -> str:
    """Scans every status subdirectory (a case can be MOVED between
    them by `set_case_status()`, so the id space must be checked across
    all four, not just `candidates/`) for the highest existing
    `real_NNNNNN` id and returns the next one, zero-padded to 6 digits.
    Starts at `real_000001` when nothing exists yet. Never raises: an
    unreadable directory is simply treated as contributing no ids."""
    highest = 0
    for status in _VALID_STATUSES:
        directory = _case_dir(base_dir, status)
        try:
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                match = _CASE_ID_RE.match(name)
                if match:
                    highest = max(highest, int(match.group(1)))
        except Exception:
            continue
    return f"real_{highest + 1:06d}"


def _extract_conversation(conversation_log: Optional[Deque[Tuple[str, str]]], turns_back: int) -> List[str]:
    """User-channel utterances only (matches the sprint brief's own
    Phase 8 worked example, which lists only user turns in
    `"conversation"`), most recent `turns_back`, oldest-first, each
    length-bounded via `_bound_text()`."""
    if not conversation_log:
        return []
    user_lines = [text for channel, text in conversation_log if channel == "USER"]
    return [_bound_text(t) for t in user_lines[-turns_back:]]


def mark_test_case(
    console: Any, conversation_id: Optional[str] = None, note: str = "", scenario: str = "",
    base_dir: str = DEFAULT_BASE_DIR, turns_back: int = _MAX_CONVERSATION_TURNS,
) -> Optional[Dict[str, Any]]:
    """Captures THIS conversation's most recent turn as a `"candidate"`
    test case. `console` is anything exposing `.conversation_log`
    (`Deque[Tuple[str, str]]`) and `.planner_module` (with
    `._last_turn_trace`) - both `RuntimeDemoConsole` and
    `ProductionConsole` already qualify with zero changes.

    `conversation_id=None` (the `/mark_test` console command's own
    default, since `ProductionConsole` tracks no per-session id concept
    today - see `collectors.py::collect_conversation()`'s own
    `"session_id": None` comment) falls back to whichever conversation
    produced the MOST RECENT entry in `_turn_trace_history` - the exact
    same fallback `collectors.py::_find_trace()` already uses for the
    identical reason, applied here for consistency rather than inventing
    a second convention.

    Returns the written case dict, or `None` (never raises) if there is
    no recorded turn for this conversation yet - matches every other
    best-effort/non-fatal convention in this project ("nothing to mark"
    is not an error)."""
    try:
        planner_module = getattr(console, "planner_module", None)
        trace = None
        if planner_module is not None:
            if conversation_id:
                trace = getattr(planner_module, "_last_turn_trace", {}).get(conversation_id)
            else:
                history = getattr(planner_module, "_turn_trace_history", None)
                if history:
                    conversation_id, trace = history[-1]
        conversation_log = getattr(console, "conversation_log", None)
        conversation = _extract_conversation(conversation_log, turns_back)
        if trace is None and not conversation:
            return None

        case_id = _next_case_id(base_dir)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        actual: Dict[str, Any] = {}
        if trace is not None:
            actual = {
                "turn_id": getattr(trace, "turn_id", None),
                "reference_type": getattr(trace, "reference_type", ""),
                "is_short_followup": getattr(trace, "is_short_followup", None),
                "query_intent": getattr(trace, "query_intent", ""),
                "topic_decision": getattr(trace, "topic_decision", ""),
                "ambiguity_check_result": getattr(trace, "ambiguity_check_result", None),
                "ambiguity_refusal": getattr(trace, "is_ambiguity_refusal", False),
                "active_topic_terms": list(getattr(trace, "active_topic_terms", [])),
                "candidate_count": len(getattr(trace, "candidate_memory_ids", set())),
                "selected_count": len(getattr(trace, "selected_memory_ids", set())),
                "funnel": dict(getattr(trace, "funnel", {}) or {}),
            }

        case = {
            "id": case_id,
            "source": "runtime",
            "scenario": scenario or "",
            "status": "candidate",
            "captured_at": now,
            "conversation_id": conversation_id,
            "conversation": conversation,
            "actual": actual,
            "expected": None,
            "note": _bound_text(note) if note else "",
            "metadata": {
                "request_id": actual.get("turn_id"),
            },
        }

        directory = _case_dir(base_dir, "candidate")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{case_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(case, fh, indent=2, ensure_ascii=False, default=str)

        try:
            event_bus = getattr(console, "event_bus", None)
            if event_bus is not None:
                from luno.core.events import Event
                event_bus.publish(Event(type="test_case_captured", data={
                    "case_id": case_id, "status": "candidate",
                    "scenario": scenario or "", "conversation_turn_count": len(conversation),
                }))
        except Exception:
            pass  # telemetry must never be able to break capture

        return case
    except Exception:
        return None  # capture must never be able to break a turn/console


def load_case(case_id: str, base_dir: str = DEFAULT_BASE_DIR) -> Optional[Dict[str, Any]]:
    """Finds `case_id` across all four status directories (a case may
    have been moved) and returns its parsed dict, or `None` if not
    found/unreadable."""
    for status in _VALID_STATUSES:
        path = os.path.join(_case_dir(base_dir, status), f"{case_id}.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None
    return None


def list_cases(status: str = "", base_dir: str = DEFAULT_BASE_DIR) -> List[str]:
    """Case ids present under `status` (or every status if `""`),
    sorted. Never raises - an unreadable/missing directory contributes
    no ids."""
    statuses = (status,) if status else _VALID_STATUSES
    ids: List[str] = []
    for st in statuses:
        directory = _case_dir(base_dir, st)
        try:
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                match = _CASE_ID_RE.match(name)
                if match:
                    ids.append(f"real_{match.group(1)}")
        except Exception:
            continue
    return sorted(set(ids))


def set_case_status(case_id: str, new_status: str, base_dir: str = DEFAULT_BASE_DIR, annotated_expected: Optional[Dict[str, Any]] = None) -> bool:
    """Phase 11's own data-quality gate: moves a case file from its
    CURRENT status directory to `new_status`'s directory, updating the
    `status` field inside the file itself (and, optionally, filling in
    `expected` - the human-annotated "what SHOULD this turn have done"
    block the case is otherwise born without). `new_status` MUST be one
    of `_VALID_STATUSES`; anything else is rejected (returns `False`,
    never raises, never silently accepts an invalid status). Only a
    case actually moved into `"approved"` this way is ever picked up by
    `replay.py::replay_all()`'s own default sweep."""
    if new_status not in _VALID_STATUSES:
        return False
    case = load_case(case_id, base_dir=base_dir)
    if case is None:
        return False
    old_status = case.get("status", "candidate")
    if old_status not in _VALID_STATUSES:
        old_status = "candidate"
    case["status"] = new_status
    if annotated_expected is not None:
        case["expected"] = annotated_expected
    try:
        new_dir = _case_dir(base_dir, new_status)
        os.makedirs(new_dir, exist_ok=True)
        new_path = os.path.join(new_dir, f"{case_id}.json")
        with open(new_path, "w", encoding="utf-8") as fh:
            json.dump(case, fh, indent=2, ensure_ascii=False, default=str)
        old_path = os.path.join(_case_dir(base_dir, old_status), f"{case_id}.json")
        if old_status != new_status and os.path.isfile(old_path):
            os.remove(old_path)
        return True
    except Exception:
        return False
