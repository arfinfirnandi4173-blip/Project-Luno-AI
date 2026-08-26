"""
utils.py
========

Small shared helpers used across the Core Integration Layer - identical
in spirit to `vision_memory/utils.py`, `behavior_tree`'s helpers,
`planner/utils.py` and `tool_manager/utils.py` from earlier packages:
a UTC timestamp helper, an id generator, and a one-line logger. Nothing
here has any opinion about events, modules, or scheduling.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone

_id_lock = threading.Lock()
_id_counter = itertools.count(1)


def utcnow() -> datetime:
    """Timezone-aware UTC `datetime` - never naive. Every timestamp field
    in this package uses this, avoiding the naive/aware comparison bugs
    that showed up earlier in `vision_memory`."""
    return datetime.now(timezone.utc)


def generate_id(prefix: str = "id") -> str:
    """Process-unique, sortable-ish id: `<prefix>-<counter>-<ms>`. Not a
    UUID - this process is the only writer for any of Core's internal
    state, so a monotonic counter is simpler, cheaper, and just as
    collision-free."""
    with _id_lock:
        n = next(_id_counter)
    ms = int(utcnow().timestamp() * 1000)
    return f"{prefix}-{n}-{ms}"


def log(message: str, component: str = "core") -> None:
    """Structured-ish one-line logger. Deliberately dependency-free
    (no `logging` config assumptions) so this package works identically
    whether or not the host application has configured Python's
    `logging` module - matches the plain-`print` convention used by
    every prior package this session (`vision_memory`, `behavior_tree`,
    `planner`, `tool_manager`)."""
    ts = utcnow().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [Luno.{component}] {message}")
