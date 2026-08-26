"""
utils.py
========

Tiny standalone helpers shared across the package - kept deliberately
small (same role as `vision_memory/utils.py`'s time helpers).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_id(prefix: str = "id") -> str:
    """Short, collision-safe id for plans/tasks - e.g. `plan_a1b2c3d4e5f6`.
    Not a full UUID (too long to be pleasant in logs/tests) but the 12 hex
    chars from `uuid4` are more than enough entropy for this use case."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
