"""
cooldowns.py
============

Backs the spec's Safety Rules: "Never spam. Never repeat the same
reminder. Respect cooldown timers." A `CooldownManager` is just a
thread-safe `key -> last_fired_at` map with an `is_ready(key, min_interval)`
check - deliberately dumb/small, since every OTHER module (actions.py,
conditions.py) is responsible for choosing good keys (e.g.
`"proactive:lights_left_on"`, `f"ha:{event.kind}:{event.entity_id}"`,
`"idle:blink"`) and sensible intervals for its own behaviors.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, Optional

from .blackboard import utcnow


class CooldownManager:
    """Thread-safe - `actions.py` may check/mark cooldowns from a
    background dispatch thread (see `actions._dispatch`) while the main
    tick thread is evaluating `conditions.py` at the same time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_fired: Dict[str, datetime] = {}

    def is_ready(self, key: str, min_interval_s: float) -> bool:
        """True if `key` has never fired, or fired more than
        `min_interval_s` seconds ago."""
        with self._lock:
            last = self._last_fired.get(key)
        if last is None:
            return True
        return (utcnow() - last).total_seconds() >= min_interval_s

    def mark_fired(self, key: str, when: Optional[datetime] = None) -> None:
        with self._lock:
            self._last_fired[key] = when or utcnow()

    def remaining(self, key: str, min_interval_s: float) -> float:
        """Seconds left until `key` is ready again (0.0 if already ready)."""
        with self._lock:
            last = self._last_fired.get(key)
        if last is None:
            return 0.0
        elapsed = (utcnow() - last).total_seconds()
        return max(0.0, min_interval_s - elapsed)

    def reset(self, key: str) -> None:
        with self._lock:
            self._last_fired.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._last_fired.clear()
