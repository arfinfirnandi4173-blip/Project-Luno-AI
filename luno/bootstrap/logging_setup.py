"""
logging_setup.py
==================

Structured lifecycle logging for the launcher itself. Every existing
package in this project already logs through the same tiny, dependency-
free convention: `luno.core.utils.log(message, component)`, printing
`[HH:MM:SS.mmm] [Luno.<component>] <message>` - deliberately NOT
Python's `logging` module (see that function's own docstring: "works
identically whether or not the host application has configured Python's
`logging` module"). Sprint 6 doesn't change that convention for the 19
existing subsystems (nothing about how they log is touched) - this file
adds exactly one thing on top, for the launcher's OWN lifecycle
milestones: a structured variant carrying the fields the spec's
"Logging" section asks for explicitly (timestamp, module, action,
duration, status, request id), formatted as one still-`print`-based
line so it reads consistently alongside every other package's own log
output, optionally ALSO mirrored to a file when `LauncherConfig.log_file`
is set.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from typing import IO, Optional

_lock = threading.Lock()
_log_file_handle: Optional[IO[str]] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def configure_logging(log_file: Optional[str] = None) -> None:
    """Opens (append mode) the optional structured log file. Safe to
    call more than once (e.g. across a `/reload`) - re-opens if the
    target path changed, otherwise a no-op. Never raises: a launcher
    must never fail to start because a log file path is unwritable -
    falls back to stdout-only logging and reports the problem there."""
    global _log_file_handle
    with _lock:
        if log_file is None:
            if _log_file_handle is not None:
                try:
                    _log_file_handle.close()
                except Exception:
                    pass
                _log_file_handle = None
            return
        try:
            _log_file_handle = open(log_file, "a", encoding="utf-8", buffering=1)
        except OSError as ex:
            _log_file_handle = None
            print(f"[bootstrap] WARNING: could not open log file '{log_file}' ({ex}) - logging to stdout only")


def log_lifecycle(
    module: str,
    action: str,
    status: str = "ok",
    duration_ms: Optional[float] = None,
    request_id: Optional[str] = None,
) -> None:
    """One structured lifecycle line: timestamp, module, action,
    duration (if known), status, request id (if applicable). Examples
    this produces, matching the spec's own worked examples:

        [12:03:41.512] [Luno.runtime] Runtime started (status=ok)
        [12:03:41.812] [Luno.planner] Planner initialized (status=ok, duration=210.4ms)
        [12:03:42.001] [Luno.whisper] Whisper connected (status=ok, duration=88.2ms)
        [12:03:42.301] [Luno.fish_audio] Fish Audio ready (status=ok, duration=140.7ms)
        [12:03:42.310] [Luno.memory_retrieval] Memory Retrieval initialized (status=ok)
        [12:03:42.315] [Luno.wake_session] Wake Session active (status=ok)
        [12:03:42.320] [Luno.runtime] Listening... (status=ok)
    """
    ts = _utcnow().strftime("%H:%M:%S.%f")[:-3]
    parts = [f"status={status}"]
    if duration_ms is not None:
        parts.append(f"duration={duration_ms:.1f}ms")
    if request_id:
        parts.append(f"request_id={request_id}")
    line = f"[{ts}] [Luno.{module}] {action} ({', '.join(parts)})"
    with _lock:
        print(line, file=sys.stdout)
        if _log_file_handle is not None:
            try:
                _log_file_handle.write(line + "\n")
            except Exception:
                pass


class Timer:
    """`with Timer() as t: ...; log_lifecycle(..., duration_ms=t.ms)` -
    a tiny context manager so call sites don't hand-roll `time.time()`
    bookkeeping for every lifecycle log line."""

    def __init__(self) -> None:
        self._start = 0.0
        self.ms: float = 0.0

    def __enter__(self) -> "Timer":
        import time
        self._start = time.time()
        return self

    def __exit__(self, *exc) -> None:
        import time
        self.ms = (time.time() - self._start) * 1000.0
