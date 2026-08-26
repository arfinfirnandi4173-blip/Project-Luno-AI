"""
event_log_writer.py
=====================

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture). `EventLogWriter` - a passive Event Bus observer, built with the
EXACT SAME technique `events_buffer.py::EventRingBuffer`/`StatsAggregator`
and `voice_latency.py::VoiceLatencyRecorder` already established
(`event_bus.subscribe("*", self._on_event, priority=-1000)`, every
handler wrapped in `try/except: pass`, unsubscribe in `stop()`) - this is
that same pattern, pointed at two new destinations: a bounded,
date-rotated JSONL file per day (`logs/events/YYYY-MM-DD.jsonl`) and a
bounded, date-rotated human-readable text file per day
(`logs/runtime/YYYY-MM-DD.log`).

Why a NEW file, not an extension of `EventRingBuffer`/`LogCapture`: both
of those are deliberately IN-MEMORY ONLY (bounded `deque`s) - built for
the dashboard's own live "last N events/lines" pages, not for durable,
across-restart storage. Nothing before this sprint writes the Event Bus's
own event stream to disk at all (`logging_setup.py::log_lifecycle()`
mirrors the LAUNCHER's own lifecycle lines to a file, a different,
narrower, opt-in mechanism this module does not touch). This module adds
exactly the missing piece - durable, structured, on-disk event history a
future Claude Pro account (or any other tool) can read WITHOUT a live
process - reusing every existing convention (the same `Event` shape, the
same subscribe-priority pattern, the same "a bug here must never be able
to take down real event delivery" discipline) rather than inventing a
second bus or a second event shape.

OBSERVABILITY ONLY: this module never publishes an event on its own
initiative (except a bounded, best-effort `observability_error` note for
its OWN internal failures - see `_report_internal_error()`), never
mutates an `Event`, never blocks the bus (subscriber priority/async
unaffected), and its own failure (disk full, permissions, unwritable
path, malformed payload) can never propagate back to break a turn -
every single file operation in this module is wrapped in its own
`try/except: pass`.

Privacy (Sprint 50 Phase 1's own "do not log secrets" requirement):
`_redact()` recursively walks the event's `data` dict and replaces the
VALUE of any key matching `_SENSITIVE_KEY_RE` (api_key/password/token/
secret/authorization/credential, case-insensitive, substring match) with
a fixed `"***REDACTED***"` marker - applied before EITHER file format is
written, so neither ever receives a raw secret even if some future
publisher accidentally included one. `_bound_value()` separately caps any
individual string value at `MAX_FIELD_CHARS` (500) so a pathologically
large payload can never turn one log line into an unbounded file (Phase
13's own "oversized input must be bounded" requirement) - this project's
own existing event publishers never include raw prompt/conversation text
in `data` in the first place (see `MemoryTurnTrace`'s own long-standing
privacy boundary), so this is a defense-in-depth bound, not a workaround
for a real oversized payload observed in this codebase today.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from luno.core.event_bus import EventBus
    from luno.core.events import Event

DEFAULT_LOG_DIR = "logs"
DEFAULT_MAX_RETENTION_DAYS = 14
MAX_FIELD_CHARS = 500

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|password|passwd|token|secret|authorization|auth[_-]?header|credential|bearer)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"

#: The 6 Sprint 50 event types that get the Phase 3 multi-line
#: human-readable rendering; every other event type (the pre-existing
#: ~50 event types this project already publishes) still gets ONE
#: compact line - see `_format_text_line()`. This is a presentation
#: choice only - ALL event types are written to the JSONL file
#: identically, uniformly, via `_redact()` + `event.to_dict()`.
_PRETTY_EVENT_TYPES = frozenset({
    "memory_reference_classified", "memory_topic_decision",
    "memory_selection_summary", "test_case_captured",
    "test_case_replayed", "observability_error",
})


def _redact(value: Any) -> Any:
    """Recursively replaces any dict VALUE whose KEY looks secret-shaped
    with a fixed redaction marker - see module docstring. Never raises:
    an unexpected value shape (e.g. a custom object that isn't JSON-safe)
    is stringified rather than allowed to break logging."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return _bound_value(value)


def _bound_value(value: Any) -> Any:
    """Caps any individual string at `MAX_FIELD_CHARS` - see module
    docstring's privacy/bounding section. Non-string, non-collection
    values (int/float/bool/None) pass through unchanged; anything else
    not already JSON-safe is stringified (and then bounded) so
    `json.dumps()` in `_write_jsonl_line()` can never raise on a type it
    doesn't recognize."""
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CHARS:
            return value[:MAX_FIELD_CHARS] + "...[truncated]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict) or isinstance(value, (list, tuple)):
        return _redact(value)
    try:
        text = str(value)
    except Exception:
        return "<unrepresentable>"
    return _bound_value(text)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _format_text_line(record: Dict[str, Any]) -> str:
    """One human-readable rendering per event - Phase 3's own example
    format for the 6 Sprint 50 event types (a multi-line block, blank
    line between events, mirroring the sprint brief's own worked
    example almost verbatim); a single compact line (matching this
    project's own `[HH:MM:SS.mmm] [Luno.<component>] <message>`
    convention from `core/utils.py::log()`) for everything else, so an
    operator scanning the file sees the SAME visual language it already
    uses elsewhere in this project, not a second logging dialect."""
    ts = record.get("timestamp", "")
    etype = record.get("type", "")
    data = record.get("data", {}) or {}
    hhmmss = ts[11:23] if len(ts) >= 23 else ts
    if etype not in _PRETTY_EVENT_TYPES:
        preview = ", ".join(f"{k}={v}" for k, v in list(data.items())[:6])
        return f"[{hhmmss}] [Luno.event_bus] {etype} ({preview})" if preview else f"[{hhmmss}] [Luno.event_bus] {etype}"

    lines = [f"[{ts}]"]
    session = data.get("conversation_id") or "-"
    turn = data.get("request_id") or "-"
    lines.append(f"SESSION: {session}")
    lines.append(f"TURN: {turn}")
    lines.append(f"EVENT: {etype}")
    for key, value in data.items():
        if key in ("conversation_id", "request_id"):
            continue
        lines.append(f"{key.upper()}: {value}")
    lines.append("")  # blank separator line between events
    return "\n".join(lines)


class EventLogWriter:
    """Lifecycle mirrors `EventRingBuffer`/`StatsAggregator`/
    `VoiceLatencyRecorder` exactly: constructed, `start()`ed to begin
    observing, `stop()`ped to unsubscribe. Disabled-by-default in test/
    demo contexts (see `RuntimeDemoConsole`'s own `enable_observability_log`
    constructor flag) so the ~2900 pre-existing tests that construct a
    console never gain a new on-disk side effect unless they opt in -
    only `DashboardServer.start()` wires this unconditionally (matching
    every other dashboard telemetry component's own "only runs when the
    dashboard is explicitly started" convention)."""

    def __init__(
        self, event_bus: "EventBus", log_dir: str = DEFAULT_LOG_DIR,
        max_retention_days: int = DEFAULT_MAX_RETENTION_DAYS,
    ) -> None:
        self._event_bus = event_bus
        self._log_dir = log_dir
        self._max_retention_days = max_retention_days
        self._lock = threading.RLock()
        self._sub_id: Optional[str] = None
        self._events_written = 0
        self._write_failures = 0
        self._rotate_old_files()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._sub_id is not None:
            return
        self._sub_id = self._event_bus.subscribe("*", self._on_event, priority=-1000)

    def stop(self) -> None:
        if self._sub_id is not None:
            self._event_bus.unsubscribe(self._sub_id)
            self._sub_id = None

    # -- event handling -------------------------------------------------------

    def _on_event(self, event: "Event") -> None:
        try:
            record = event.to_dict()
            record["data"] = _redact(record.get("data") or {})
            self._write_jsonl_line(record)
            self._write_text_line(record)
        except Exception:
            pass  # a logging bug must never be able to disrupt real event delivery

    def _events_dir(self) -> str:
        return os.path.join(self._log_dir, "events")

    def _runtime_dir(self) -> str:
        return os.path.join(self._log_dir, "runtime")

    def _write_jsonl_line(self, record: Dict[str, Any]) -> None:
        try:
            directory = self._events_dir()
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{_today_str()}.jsonl")
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self._events_written += 1
        except Exception:
            with self._lock:
                self._write_failures += 1

    def _write_text_line(self, record: Dict[str, Any]) -> None:
        try:
            directory = self._runtime_dir()
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{_today_str()}.log")
            text = _format_text_line(record)
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
        except Exception:
            with self._lock:
                self._write_failures += 1

    # -- rotation -------------------------------------------------------------

    def _rotate_old_files(self) -> None:
        """Best-effort deletion of any `YYYY-MM-DD.{jsonl,log}` file
        older than `max_retention_days` in either directory - runs once
        at construction time (a cheap, one-shot pass; this module is
        local-first, deliberately not a background cron). Never raises:
        an unreadable/undeletable directory just means rotation silently
        does nothing this run, exactly like every other best-effort
        operation in this module."""
        if self._max_retention_days <= 0:
            return
        cutoff = time.time() - (self._max_retention_days * 86400)
        for directory in (self._events_dir(), self._runtime_dir()):
            try:
                if not os.path.isdir(directory):
                    continue
                for name in os.listdir(directory):
                    if not (name.endswith(".jsonl") or name.endswith(".log")):
                        continue
                    path = os.path.join(directory, name)
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                    except Exception:
                        continue
            except Exception:
                continue

    # -- introspection (for tests/health checks only, never on the critical path)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"events_written": self._events_written, "write_failures": self._write_failures}
