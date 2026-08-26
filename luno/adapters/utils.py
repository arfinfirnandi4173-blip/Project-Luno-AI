"""
utils.py
========

Shared helpers for the Adapter Layer - same shape as every other
package's `utils.py` this session (`vision_memory`, `behavior_tree`,
`planner`, `tool_manager`, `core`): id generation, UTC timestamps, a
one-line structured-ish logger, and an elapsed-time helper (adapters are
required to log execution time for every event they handle).
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime, timezone

_id_lock = threading.Lock()
_id_counter = itertools.count(1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_id(prefix: str = "id") -> str:
    with _id_lock:
        n = next(_id_counter)
    ms = int(utcnow().timestamp() * 1000)
    return f"{prefix}-{n}-{ms}"


def log(message: str, component: str = "adapters") -> None:
    ts = utcnow().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [Luno.adapters.{component}] {message}")


def elapsed_ms(start: float) -> float:
    return (time.time() - start) * 1000.0
