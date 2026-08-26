"""
utils.py
========

Tiny, dependency-free helpers - same shape as every other package's own
`utils.py` in this project (`vision_memory/utils.py`, `core/utils.py`),
duplicated here rather than imported so this package has ZERO dependency
on `luno.core` and stays usable completely standalone (matches
`wake_session/session.py` defining its own `utcnow()` instead of
importing `core.utils` for the same reason - only the Event-Bus-facing
`manager.py` files in this project import `core` directly).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def log(message: str, debug: bool, component: str = "memory_retrieval") -> None:
    """Debug-gated logger - unlike `core.utils.log` (always on), every
    call site here passes the caller's own `MemoryRetrievalConfig.debug`
    flag, since these logs must only appear in debug mode per the spec
    ("These logs should only appear in debug mode")."""
    if not debug:
        return
    ts = utcnow().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [Luno.{component}] {message}")
