"""
utils.py
========

Tiny standalone helpers - same minimal role as `vision_memory/utils.py`'s
time helpers and `planner/utils.py`'s id generator. Deliberately no
imports from anywhere else in this package, so every other module can
depend on this one freely.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000.0


def generate_id(prefix: str = "exec") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def log(message: str) -> None:
    """Single choke point for this package's console logging - covers the
    spec's Logging requirement (registration, execution, completion,
    failures, timeouts, cancellation, retries) without every module
    reimplementing its own prefix."""
    print(f"[ToolManager] {message}")
