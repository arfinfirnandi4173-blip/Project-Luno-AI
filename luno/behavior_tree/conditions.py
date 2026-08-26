"""
conditions.py
=============

One small, pure predicate function per priority tier from the spec's
"Priority Order" (plus the Proactive/Sleep/Error-recovery gates that sit
alongside them) - every function takes the `Blackboard` (and occasionally
a `CooldownManager`) and returns a plain bool or a small result object,
NEVER mutates anything. `behavior_tree.py` is the only place these get
called from, in priority order, to pick which `BehaviorNode` runs.

Keeping these here (instead of inline lambdas on each node) makes the
priority logic readable top-to-bottom as its own file, and makes each
rule independently unit-testable against a hand-built Blackboard - exactly
how `test_behavior_tree.py` exercises them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .blackboard import Blackboard, HAEventSeverity
from .cooldowns import CooldownManager

# ---------------------------------------------------------------------------
# Priority 0 - Emergency
# ---------------------------------------------------------------------------

def has_emergency(bb: Blackboard) -> bool:
    """Life-safety-grade Home Assistant events (smoke, gas leak, fire) OR
    the system being in a genuinely broken state (3+ consecutive errors -
    see `Blackboard.record_error`). Nothing else in this file outranks
    this - see behavior_tree.py's node ordering."""
    if bb.unhandled_ha_events(HAEventSeverity.EMERGENCY):
        return True
    return not bb.system.healthy


# ---------------------------------------------------------------------------
# Priority 1 - Critical Home Assistant events
# ---------------------------------------------------------------------------

def has_critical_ha_event(bb: Blackboard) -> bool:
    """Property/security-grade events (water leak, power outage, door
    opened while away) - urgent, but not the "call 911" tier above."""
    return bool(bb.unhandled_ha_events(HAEventSeverity.CRITICAL))


# ---------------------------------------------------------------------------
# Priority 2 - Direct user speech
# ---------------------------------------------------------------------------

def direct_user_speech(bb: Blackboard) -> bool:
    """Wake word just fired, or the user is actively mid-utterance (VAD/
    STT-in-progress signal). Deliberately does NOT include
    `pending_user_text` (a transcript that's already finished) - once
    transcription is done this condition goes false and priority hands off
    to `conversation_continuation` below, which is what actually plans/
    replies. That handoff is what keeps "listening" and "conversing" as
    two distinct priority tiers, per the spec, without duplicating logic."""
    return bb.wake_word_detected or bb.user.speaking


# ---------------------------------------------------------------------------
# Priority 3 - Tool execution
# ---------------------------------------------------------------------------

def tool_execution_pending(bb: Blackboard) -> bool:
    """True while a tool was dispatched OUTSIDE a normal conversation turn
    (see `ToolStatus` docstring in blackboard.py) and hasn't finished yet.
    Tools called BY the LLM mid-conversation (the far more common case,
    e.g. `lihat_kamera`/`save_memory`) are already handled inside whatever
    `generate_reply` handler is wired in (real wiring: main.py's
    `Luno_Brain()`, which already does its own tool-calling loop) - this
    condition exists so a *separately* dispatched tool (e.g. Proactive
    Behavior directly flipping a light off) still blocks lower-priority
    behaviors from acting like nothing's happening while it's in flight."""
    return bb.tool.running


# ---------------------------------------------------------------------------
# Priority 4 - Conversation continuation
# ---------------------------------------------------------------------------

def conversation_continuation(bb: Blackboard) -> bool:
    """A transcript is waiting to be planned, or a turn is already
    in flight (thinking/talking)."""
    return bb.conversation.pending_user_text is not None or bb.conversation.ongoing


# ---------------------------------------------------------------------------
# Priority 5 - Visual events (Watching Behavior - silent)
# ---------------------------------------------------------------------------

def has_visual_event(bb: Blackboard) -> bool:
    return bool(bb.unhandled_visual_events())


# ---------------------------------------------------------------------------
# Proactive Behavior - gated by attention + conversation + cooldown
# ---------------------------------------------------------------------------

@dataclass
class ProactiveCandidate:
    key: str          # cooldown key, e.g. "proactive:lights_left_on"
    message_hint: str  # short factual hint for the action to phrase into speech


_TIRED_EMOTIONS = frozenset({"tired", "sleepy"})


def find_proactive_candidate(bb: Blackboard) -> Optional[ProactiveCandidate]:
    """Looks for one of the spec's example Proactive triggers ("user
    forgot lights on", "user appears tired", "package detected", "unknown
    person enters room", "door left open", "room too dark") in the current
    Blackboard state. Returns the FIRST match (checked in roughly
    "most actionable first" order) or None. Does NOT check attention/
    cooldown/conversation-state itself - see `proactive_eligible()` below,
    which wraps this with those gates so the two concerns (what happened /
    is it okay to mention it right now) stay separately testable."""

    for event in bb.unhandled_visual_events():
        desc_lower = event.description.lower()
        if "unknown" in desc_lower and ("person" in desc_lower or "human" in desc_lower):
            return ProactiveCandidate("proactive:unknown_person", event.description)
        if "package" in desc_lower or "parcel" in desc_lower or "delivery" in desc_lower:
            return ProactiveCandidate("proactive:package_detected", event.description)

    # NOTE: these two deliberately do NOT require `not bb.user.present`.
    # "Forgot the lights on / left the door open" is a fact about the ROOM,
    # independent of whether anyone's there right now - and it can only
    # ever be SPOKEN once someone IS there to hear it (that's what
    # `proactive_eligible()`'s attention gate below already enforces, via
    # `AttentionEstimator` requiring `present=True` for `available_for_
    # interruption`). Requiring absence here too would make these two
    # candidates permanently unreachable (contradicts the attention gate)
    # - a real bug caught by `test_behavior_tree.py`.
    if bb.room.light_on:
        return ProactiveCandidate("proactive:lights_left_on", "The room light is on but no one's there.")

    if bb.room.door_closed is False:
        return ProactiveCandidate("proactive:door_left_open", "The door has been left open.")

    if bb.room.dark and bb.user.present:
        return ProactiveCandidate("proactive:room_too_dark", "The room looks dark while the user is present.")

    if bb.user.present and bb.user.emotion in _TIRED_EMOTIONS:
        return ProactiveCandidate("proactive:user_tired", "The user looks tired.")

    return None


def proactive_eligible(
    bb: Blackboard, cooldowns: CooldownManager, min_interval_s: float = 1800.0
) -> Optional[ProactiveCandidate]:
    """Returns the candidate to act on if ALL of the spec's Proactive
    Behavior gates pass ("User is present. No ongoing conversation. No
    recent interruption."), else None. `min_interval_s` (default 30 min)
    is the "don't repeat the same nudge too often" cooldown - each
    candidate's own `.key` is what gets checked/marked, so different kinds
    of nudges don't block each other."""
    if bb.conversation.ongoing:
        return None
    if not bb.attention_available_for_interruption:
        return None
    candidate = find_proactive_candidate(bb)
    if candidate is None:
        return None
    if not cooldowns.is_ready(candidate.key, min_interval_s):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Priority 6 - Idle (fallback - always eligible)
# ---------------------------------------------------------------------------

def idle_default(bb: Blackboard) -> bool:
    return True


# ---------------------------------------------------------------------------
# Background maintenance
# ---------------------------------------------------------------------------

def background_maintenance_due(bb: Blackboard, cooldowns: CooldownManager, interval_s: float = 300.0) -> bool:
    """Low-priority periodic upkeep hook (see actions.run_background_
    maintenance) - only "due" every `interval_s` (default 5 min) AND only
    considered at all while nothing else wants to run (it's the
    second-to-lowest priority node)."""
    return cooldowns.is_ready("background_maintenance", interval_s)


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def should_sleep(bb: Blackboard, absence_threshold_s: float = 900.0) -> bool:
    """User has been away for a while AND it's late/early. Both conditions
    required so Luno doesn't "fall asleep" just because the user stepped
    out mid-afternoon - only when it's ALSO plausible they're done for the
    night. Trivially preempted the moment presence/wake-word/an HA event
    reappears, since Sleep is the lowest-priority node (see
    behavior_tree.py)."""
    if bb.user.present:
        return False
    if bb.user.last_seen_at is None:
        return False
    absent_for = (bb.now - bb.user.last_seen_at).total_seconds()
    if absent_for < absence_threshold_s:
        return False
    return bb.now.hour >= 23 or bb.now.hour < 6
