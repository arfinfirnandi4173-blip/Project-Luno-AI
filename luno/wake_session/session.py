"""
session.py
==========

`ConversationSession` - a pure, dependency-free state machine (no Event
Bus, no threads, no I/O) enforcing the Sprint 2 transition rules. Mirrors
`luno.behavior_tree.state_machine.StateMachine`'s shape (a `state`
property, a bounded transition history, `transition_to()` returning
False for a no-op re-entry) on purpose - same discipline, new/separate
state set - but stays fully standalone so it's testable with zero
Event Bus wiring, exactly like every other package's core logic in this
project.

`SessionManagerModule` (see `manager.py`) is the ONLY thing that drives
this class from real Events; this file has no idea the Event Bus
exists.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Optional

from .models import TIMEOUT_ACTIVE_STATES, ConversationState, WakeSessionConfig


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionTransition:
    from_state: ConversationState
    to_state: ConversationState
    at: datetime
    reason: str


class ConversationSession:
    def __init__(self, config: Optional[WakeSessionConfig] = None) -> None:
        self.config = config or WakeSessionConfig()
        self.state: ConversationState = ConversationState.IDLE if not self.config.sleep_enabled else ConversationState.SLEEPING
        self.previous_state: Optional[ConversationState] = None
        self.entered_at: datetime = utcnow()
        self.history: Deque[SessionTransition] = deque(maxlen=50)
        self._deadline: Optional[datetime] = None
        self._lock = threading.Lock()

        #: how many times this session has been woken - handy for
        #: tests/inspection, no behavioral use.
        self.wake_count = 0

    # -- core transition primitive -------------------------------------------

    def transition_to(self, new_state: ConversationState, reason: str = "") -> bool:
        with self._lock:
            if new_state == self.state:
                return False
            now = utcnow()
            self.history.append(SessionTransition(self.state, new_state, now, reason))
            self.previous_state = self.state
            self.state = new_state
            self.entered_at = now
            self._deadline = self._compute_deadline(now) if new_state in TIMEOUT_ACTIVE_STATES else None
            return True

    def _compute_deadline(self, now: datetime) -> datetime:
        from datetime import timedelta
        return now + timedelta(seconds=self.config.session_timeout_s)

    # -- timeout ------------------------------------------------------------

    def touch(self) -> None:
        """Reset the inactivity deadline - called whenever we (re)enter a
        timeout-active state as the direct result of a real user
        interaction. A no-op if the current state doesn't track a
        timeout at all (IDLE/SLEEPING/AWAKENING/THINKING/SPEAKING)."""
        with self._lock:
            if self.state in TIMEOUT_ACTIVE_STATES:
                self._deadline = self._compute_deadline(utcnow())

    def seconds_remaining(self) -> Optional[float]:
        with self._lock:
            if self._deadline is None:
                return None
            return (self._deadline - utcnow()).total_seconds()

    def is_timed_out(self) -> bool:
        remaining = self.seconds_remaining()
        return remaining is not None and remaining <= 0

    # -- convenience predicates ------------------------------------------------

    @property
    def time_in_state(self) -> float:
        return (utcnow() - self.entered_at).total_seconds()

    def is_awake(self) -> bool:
        """True for every state except SLEEPING - i.e. "don't require a
        wake word right now"."""
        return self.state != ConversationState.SLEEPING

    # -- config reload --------------------------------------------------------

    def reconfigure(self, new_config: WakeSessionConfig) -> None:
        with self._lock:
            old_sleep_enabled = self.config.sleep_enabled
            self.config = new_config
            # Only force a state change on the enabled/disabled EDGE - a
            # bare timeout/wake-word/ack change mid-flight shouldn't
            # yank the current conversation out from under a user.
            if old_sleep_enabled and not new_config.sleep_enabled and self.state == ConversationState.SLEEPING:
                self.state = ConversationState.IDLE
                self._deadline = None
            elif not old_sleep_enabled and new_config.sleep_enabled and self.state == ConversationState.IDLE:
                self.state = ConversationState.SLEEPING
                self._deadline = None
