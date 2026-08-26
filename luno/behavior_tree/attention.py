"""
attention.py
============

The spec's "Attention Model": tracks whether the user is nearby, looking
at Luno, speaking, or busy (gaming/coding/sleeping/watching a movie), and
turns that into a single answer the rest of the tree actually needs:
"is it okay to interrupt them right now?" - `AttentionState.
available_for_interruption`, which gates every Proactive Behavior node
(see `conditions.proactive_eligible`).

HONEST LIMITATION: like `emotion.py`, this reads whatever signals are
already on the Blackboard (Vision Memory's human tracking, YOLO presence,
Home Assistant presence sensors) - it does not itself talk to any sensor.
"gaming"/"coding"/"watching a movie" specifically have no dedicated
detector wired up anywhere in Luno yet (that would need e.g. desktop
window-focus detection); `AttentionEstimator` degrades gracefully to
`"unknown"`/`busy=False` when nothing indicates otherwise, rather than
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .blackboard import Blackboard

_BUSY_ACTIVITIES = frozenset({"gaming", "coding", "sleeping", "watching_movie", "typing", "in_meeting"})


@dataclass
class AttentionState:
    user_present: bool = False
    looking_at_luno: bool = False
    speaking: bool = False
    busy: bool = False
    activity: str = "unknown"
    available_for_interruption: bool = False
    confidence: float = 0.0


class AttentionEstimator:
    """Stateless - see `emotion.py`'s docstring for why estimators in this
    package are split into a pure `estimate()` plus a `from_blackboard()`
    convenience wrapper."""

    @staticmethod
    def estimate(*, present: bool, looking_at_luno: bool, speaking: bool, activity: str,
                 conversation_ongoing: bool) -> AttentionState:
        busy = activity in _BUSY_ACTIVITIES

        # Never "available for interruption" while: absent, actively
        # speaking (to someone else or mid-command), already talking to
        # Luno (conversation_ongoing - that's not an interruption, it's
        # already the highest-priority thing happening), or doing a
        # recognized focus-heavy activity.
        available = present and not speaking and not busy and not conversation_ongoing

        confidence = 0.7 if activity != "unknown" else 0.35
        return AttentionState(
            user_present=present,
            looking_at_luno=looking_at_luno,
            speaking=speaking,
            busy=busy,
            activity=activity,
            available_for_interruption=available,
            confidence=confidence,
        )

    @staticmethod
    def from_blackboard(bb: Blackboard) -> AttentionState:
        """Pulls signals off `bb.user`/`bb.conversation`, estimates, writes
        `bb.attention_available_for_interruption` for cheap reads from
        `conditions.py`, and returns the full `AttentionState` for callers
        that want the detail (e.g. `actions.run_proactive`)."""
        state = AttentionEstimator.estimate(
            present=bb.user.present,
            looking_at_luno=bb.user.looking_at_luno,
            speaking=bb.user.speaking,
            activity=bb.user.activity,
            conversation_ongoing=bb.conversation.ongoing,
        )
        bb.attention_available_for_interruption = state.available_for_interruption
        return state
