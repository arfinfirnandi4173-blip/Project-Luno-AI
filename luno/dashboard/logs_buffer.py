"""
logs_buffer.py
================

`LogCapture` - a `sys.stdout` "tee" that captures every line this
process already prints (EVERY package in this project logs through one
of exactly two dependency-free conventions that both `print()`:
`luno.core.utils.log(message, component)` and `luno.bootstrap.
logging_setup.log_lifecycle(...)` - see both modules' own docstrings)
into a bounded, thread-safe, structured ring buffer for the dashboard's
"Logs" page.

This is purely additive/observational: real stdout still receives
every single line, in the same order, completely unchanged - a tee, not
a redirect. Nothing about how any of the 19 existing subsystems (or the
bootstrap layer itself) logs is modified by installing this; a
process run WITHOUT the dashboard enabled behaves byte-for-byte as
before, since `install()` is only ever called by `DashboardServer.
start()`.

Structure extraction is regex-based, best-effort, over the ALREADY
-established `[HH:MM:SS.mmm] [Luno.<component>] <message>` line shape
(see `core/utils.py::log()`) - a line that doesn't match is still
captured verbatim (raw text, `module=None`, `level` guessed from
keywords), never dropped.

`level` is a heuristic (no log line printed anywhere in this project
today carries an explicit level field) - inferred from keywords in the
message (`error`/`failed`/`raised` -> ERROR, `warn` -> WARNING, else
INFO) and documented as such; this is an honest approximation, not a
claim that the underlying code has real leveled logging.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, TextIO

DEFAULT_LOG_BUFFER_SIZE = 10000

_LINE_RE = re.compile(r"^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\]\s+\[Luno\.(?P<module>[^\]]+)\]\s+(?P<message>.*)$")
_REQUEST_ID_RE = re.compile(r"request_id[=\s]+([A-Za-z0-9_\-]+)")
_ERROR_WORDS = ("error", "failed", "raised", "exception", "traceback")
_WARN_WORDS = ("warn", "degraded", "not reachable", "unhealthy", "dropped")


def _guess_level(message: str) -> str:
    lowered = message.lower()
    if any(w in lowered for w in _ERROR_WORDS):
        return "ERROR"
    if any(w in lowered for w in _WARN_WORDS):
        return "WARNING"
    return "INFO"


class LogCapture:
    def __init__(self, maxlen: int = DEFAULT_LOG_BUFFER_SIZE) -> None:
        self._lock = threading.RLock()
        self._lines: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._live_subscribers: List[Any] = []
        self._original_stdout: Optional[TextIO] = None
        self._installed = False

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            self._original_stdout = sys.stdout
            sys.stdout = _TeeWriter(self._original_stdout, self._capture_line)
            self._installed = True

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            if self._original_stdout is not None:
                sys.stdout = self._original_stdout
            self._installed = False

    def _capture_line(self, line: str) -> None:
        stripped = line.rstrip("\n")
        if not stripped:
            return
        match = _LINE_RE.match(stripped)
        module = match.group("module") if match else None
        message = match.group("message") if match else stripped
        req_match = _REQUEST_ID_RE.search(stripped)
        record = {
            "seq": None,
            "wall_time": time.time(),
            "module": module,
            "level": _guess_level(stripped),
            "request_id": req_match.group(1) if req_match else None,
            "message": message,
            "raw": stripped,
        }
        with self._lock:
            self._seq += 1
            record["seq"] = self._seq
            self._lines.append(record)
            subscribers = list(self._live_subscribers)
        for callback in subscribers:
            try:
                callback(record)
            except Exception:
                pass

    def snapshot(self, limit: int = 200, module: str = "", level: str = "", search: str = "", request_id: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._lines)
        if module:
            items = [r for r in items if (r["module"] or "").lower() == module.lower()]
        if level:
            items = [r for r in items if r["level"].lower() == level.lower()]
        if request_id:
            items = [r for r in items if r["request_id"] == request_id]
        needle = (search or "").strip().lower()
        if needle:
            items = [r for r in items if needle in r["raw"].lower()]
        return items[-limit:]

    def full_text(self) -> str:
        """Everything currently buffered, newline-joined - what the
        "Downloadable" logs requirement serves."""
        with self._lock:
            return "\n".join(r["raw"] for r in self._lines)

    def add_live_subscriber(self, callback: Any) -> None:
        with self._lock:
            self._live_subscribers.append(callback)

    def remove_live_subscriber(self, callback: Any) -> None:
        with self._lock:
            try:
                self._live_subscribers.remove(callback)
            except ValueError:
                pass


class _TeeWriter:
    """Minimal `TextIO`-shaped wrapper: every `write()` goes to the real
    stdout FIRST (so real console/log-file behavior is byte-for-byte
    unchanged - see `logging_setup.py`'s own file mirroring, which reads
    from the real stdout stream this wraps, not from this class), then
    the line is handed to `on_line` for capture. Buffers partial writes
    (many `print()` calls issue more than one `write()`) and only
    captures once a full line is complete."""

    def __init__(self, real_stdout: TextIO, on_line: Any) -> None:
        self._real = real_stdout
        self._on_line = on_line
        self._partial = ""

    def write(self, text: str) -> int:
        n = self._real.write(text)
        self._real.flush()
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._on_line(line + "\n")
        return n

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return getattr(self._real, "isatty", lambda: False)()

    def __getattr__(self, item: str) -> Any:
        # Forwards anything else (encoding, fileno, ...) to the real
        # stream - keeps this a drop-in `sys.stdout` replacement even
        # for code that pokes at less-common `TextIO` attributes.
        return getattr(self._real, item)
