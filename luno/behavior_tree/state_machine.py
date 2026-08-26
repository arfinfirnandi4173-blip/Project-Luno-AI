"""
state_machine.py
=================

Enforces the spec's "Only one major state may be active at a time" rule.
`LunoState` is the closed set of major states from the spec's "Main
States" list; `StateMachine` tracks which one is currently active, since
when, and whether it can be preempted right now.

This module does NOT decide which state to go to next - that is
`behavior_tree.py`'s job (it picks a `BehaviorNode` based on
`conditions.py`, then calls `StateMachine.transition_to()`). This module
only enforces the invariant and keeps a short history for debugging.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Deque, Optional

from .blackboard import utcnow


class LunoState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    WATCHING = "watching"
    THINKING = "thinking"
    TALKING = "talking"
    EXECUTING_TOOL = "executing_tool"
    WAITING = "waiting"
    SLEEPING = "sleeping"
    ERROR_RECOVERY = "error_recovery"


# States that represent "free/available" - the tree can freely switch away
# from these without needing a strictly-higher-priority reason. Everything
# else is a "busy" state that only a higher-priority node may preempt (see
# behavior_tree.py's tick() for how that's enforced).
_FREE_STATES = frozenset({LunoState.IDLE, LunoState.WATCHING, LunoState.WAITING, LunoState.SLEEPING})


@dataclass
class Transition:
    from_state: LunoState
    to_state: LunoState
    at: datetime
    reason: str


class StateMachine:
    """Tracks Luno's single active major state."""

    def __init__(self, initial: LunoState = LunoState.IDLE) -> None:
        self.state: LunoState = initial
        self.previous_state: Optional[LunoState] = None
        self.entered_at: datetime = utcnow()
        self.history: Deque[Transition] = deque(maxlen=50)
        # Background action-completion callbacks (see actions._dispatch)
        # transition state from a DIFFERENT thread than the tick loop -
        # guard the compound read-modify-write with a lock, same
        # discipline as Blackboard.lock. Plain `.state` reads elsewhere
        # stay lock-free (single attribute read is atomic under the GIL).
        self._lock = threading.Lock()

    def transition_to(self, new_state: LunoState, reason: str = "") -> bool:
        """Move to `new_state`. Returns False (no-op) if already in that
        state - re-entering the same state doesn't reset `entered_at` or
        add a history entry, so `time_in_state` stays meaningful across
        repeated ticks where a long-running action just hasn't finished yet."""
        with self._lock:
            if new_state == self.state:
                return False
            now = utcnow()
            self.history.append(Transition(self.state, new_state, now, reason))
            self.previous_state = self.state
            self.state = new_state
            self.entered_at = now
            return True

    @property
    def time_in_state(self) -> float:
        return (utcnow() - self.entered_at).total_seconds()

    def is_interruptible(self) -> bool:
        """True if the CURRENT state is one the tree can freely switch away
        from this tick without needing a strictly-higher-priority reason
        (see `_FREE_STATES`). "Busy" states (LISTENING/THINKING/TALKING/
        EXECUTING_TOOL/ERROR_RECOVERY) still yield to a higher-priority
        node - see behavior_tree.py - this flag only controls whether a
        SAME-OR-LOWER priority node is also allowed to take over."""
        return self.state in _FREE_STATES
