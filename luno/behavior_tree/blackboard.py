"""
blackboard.py
=============

The Blackboard is the single source of shared state every Behavior Tree
node reads from and writes to. Nothing in this module makes decisions -
that is `conditions.py` (reads the Blackboard) and `actions.py` (writes to
it). Keeping ALL shared state in one place, instead of scattered across
node-local variables, is what lets `conditions.py` stay a set of small pure
functions and lets `scheduler.py` run heavy actions on background threads
safely (see `Blackboard.lock` below).

Sections, matching the spec's "Blackboard" list 1:1:
    - user state          -> `UserState`
    - room state           -> `RoomState`
    - recent events         -> `Blackboard.recent_events` / `.ha_events` / `.visual_events`
    - conversation context   -> `ConversationContext`
    - vision context          -> `VisionContext`
    - system status            -> `SystemStatus`
    - tool status                -> `ToolStatus`
    - current behavior             -> `Blackboard.current_behavior` / `.current_state`

Concurrency contract: `scheduler.py` ticks the tree on one thread but
dispatches heavy actions (LLM calls, TTS, tool execution, camera capture)
onto background threads via `actions._dispatch()` (see actions.py) - those
threads write their results back onto this SAME Blackboard when they
finish. `Blackboard.lock` (a `threading.RLock`) guards every mutation for
that reason - the same pattern already used for the camera device in
`luno/vision.py` (`_camera_lock`). Plain reads of a single attribute are
safe without the lock (CPython attribute reads are atomic), but any
read-modify-write or multi-field update should hold `bb.lock`.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class HAEventSeverity(str, Enum):
    """How urgently a Home Assistant event needs to be reacted to. Maps
    directly onto two tiers of the spec's Priority Order: EMERGENCY events
    drive the top-priority "Emergency" node, CRITICAL events drive the
    "Critical Home Assistant events" node, NORMAL events just update
    `Blackboard.room`/`recent_events` for context (read by Proactive/Idle,
    never interrupt anything on their own)."""
    EMERGENCY = "emergency"   # smoke, gas leak, fire - immediate danger
    CRITICAL = "critical"     # water leak, power outage, security/door-while-away
    NORMAL = "normal"         # motion, ordinary door/light/temp changes


@dataclass
class HAEvent:
    """One Home Assistant event as reported by whatever perceives them
    (real wiring: `ha_listener.py`'s event callback). `handled=True` once an
    action has reacted to it, so the same event doesn't re-trigger every
    scheduler tick (~100-200ms) - see `cooldowns.py` for the complementary
    "don't repeat the same KIND of event too often" guard."""
    entity_id: str
    kind: str  # e.g. "motion_detected", "door_opened", "smoke_detected", "water_leak", "power_outage"
    severity: HAEventSeverity
    detected_at: datetime = field(default_factory=utcnow)
    data: Dict[str, Any] = field(default_factory=dict)
    handled: bool = False


@dataclass
class VisualEvent:
    """One event surfaced by Vision Memory (`vision_memory.EventRecord`,
    converted 1:1 by the Watching action - see `actions.run_watching`)."""
    category: str
    description: str
    importance: int
    detected_at: datetime = field(default_factory=utcnow)
    handled: bool = False


# ---------------------------------------------------------------------------
# Sub-state
# ---------------------------------------------------------------------------

@dataclass
class UserState:
    """Who's there and what they're doing - raw signal storage. Turning
    this into a decision ("can we interrupt them?") is `attention.py`'s
    job, not this dataclass's."""
    present: bool = False
    looking_at_luno: bool = False
    speaking: bool = False
    activity: str = "unknown"  # "gaming" | "coding" | "sleeping" | "watching_movie" | "idle" | "unknown"
    emotion: Optional[str] = None  # last emotion Vision Memory reported for this user (see TrackedHuman.emotion)
    identity: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_interacted_at: Optional[datetime] = None


@dataclass
class RoomState:
    """Environment-level facts, mostly mirrored from Home Assistant
    and/or Vision Memory's `WorldState.room` - `None` means "not known
    right now", not "false"."""
    light_on: Optional[bool] = None
    door_closed: Optional[bool] = None
    temperature: Optional[float] = None
    presence: Optional[bool] = None
    dark: Optional[bool] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationContext:
    """State of the CURRENT (or most recent) conversation turn.
    `pending_user_text` is the handoff point between the Listening
    behavior and the Conversation behavior (see conditions.py /
    actions.py) - Listening fills it in once transcription finishes,
    Conversation clears it once a reply has been generated."""
    ongoing: bool = False
    thinking: bool = False          # True while an LLM call is in flight
    pending_user_text: Optional[str] = None
    last_user_text: Optional[str] = None
    last_reply: Optional[str] = None
    last_turn_at: Optional[datetime] = None
    turn_count: int = 0


@dataclass
class VisionContext:
    """Cached, already-summarized Vision Memory output (see
    `luno/vision.py`'s `build_vision_context()`), refreshed by the
    perception step each tick (or every few ticks - it's cheap but no
    need to hammer it at 150ms resolution, see `scheduler.py`)."""
    world_state_summary: str = ""
    recent_events_summary: str = ""
    long_term_summary: str = ""
    last_updated_at: Optional[datetime] = None


@dataclass
class SystemStatus:
    """Coarse health signal - NOT a full metrics system, just enough for
    `conditions.py`'s error-recovery gate to know something's wrong."""
    healthy: bool = True
    errors: List[str] = field(default_factory=list)
    consecutive_errors: int = 0
    last_error_at: Optional[datetime] = None


@dataclass
class ToolStatus:
    """Tracks a tool invocation dispatched OUTSIDE a normal conversation
    turn (e.g. Proactive Behavior directly calling Home Assistant) - see
    the module docstring in `actions.py` for why conversation-internal
    tool calls (handled by the injected `generate_reply`) don't need this."""
    running: bool = False
    name: Optional[str] = None
    started_at: Optional[datetime] = None
    last_result: Optional[Any] = None


# ---------------------------------------------------------------------------
# Blackboard
# ---------------------------------------------------------------------------

class Blackboard:
    """The shared state object. One instance lives for the lifetime of the
    Behavior Tree; `scheduler.py` mutates it every tick, `conditions.py`
    reads it, `actions.py` reads AND writes it (including from background
    threads - see the concurrency note in the module docstring)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()

        self.user = UserState()
        self.room = RoomState()
        self.conversation = ConversationContext()
        self.vision = VisionContext()
        self.system = SystemStatus()
        self.tool = ToolStatus()

        # Set by emotion.py / attention.py each tick - see behavior_tree.tick()
        self.emotion: str = "neutral"
        self.attention_available_for_interruption: bool = True

        # What the tree picked last tick, and the underlying state machine's
        # state mirrored here for cheap reads from conditions.py (avoids
        # every condition needing a reference to the StateMachine object).
        self.current_behavior: Optional[str] = None
        self.current_state: str = "idle"

        self.now: datetime = utcnow()
        self.started_at: datetime = utcnow()

        # Event queues - bounded so a quiet Luno doesn't leak memory over
        # a multi-day uptime. Perception (real wiring: ha_listener.py's
        # callback, vision.py's watch loop) appends to these; actions.py
        # consumes/marks them handled.
        self.ha_events: Deque[HAEvent] = deque(maxlen=100)
        self.visual_events: Deque[VisualEvent] = deque(maxlen=100)

        # Human-readable rolling log across every subsystem - "recent
        # events" in the spec's Blackboard list. Not used for decisions
        # (that's the typed queues above), purely for debugging/inspection
        # (e.g. an admin/debug view later).
        self.recent_events: Deque[str] = deque(maxlen=200)

        # Speech-input handoff (Listening behavior, see conditions.py /
        # actions.py's direct-user-speech node).
        self.wake_word_detected: bool = False

        # Cooperative, best-effort preemption signal - see the "Honest
        # limitation" note in behavior_tree.py's module docstring. Not a
        # hard kill switch (Python threads can't be force-killed safely);
        # long-running actions are expected to check this where they can.
        self.interrupt_requested: bool = False

    # -- convenience mutators (all lock-guarded) ----------------------------

    def push_event(self, text: str) -> None:
        with self.lock:
            self.recent_events.append(f"[{self.now.isoformat(timespec='seconds')}] {text}")

    def push_ha_event(self, event: HAEvent) -> None:
        with self.lock:
            self.ha_events.append(event)
        self.push_event(f"HA: {event.kind} ({event.severity.value}) on {event.entity_id}")

    def push_visual_event(self, event: VisualEvent) -> None:
        with self.lock:
            self.visual_events.append(event)
        self.push_event(f"Vision: {event.description}")

    def unhandled_ha_events(self, severity: Optional[HAEventSeverity] = None) -> List[HAEvent]:
        with self.lock:
            return [
                e for e in self.ha_events
                if not e.handled and (severity is None or e.severity == severity)
            ]

    def unhandled_visual_events(self) -> List[VisualEvent]:
        with self.lock:
            return [e for e in self.visual_events if not e.handled]

    def mark_ha_events_handled(self, events: List[HAEvent]) -> None:
        with self.lock:
            for e in events:
                e.handled = True

    def mark_visual_events_handled(self, events: List[VisualEvent]) -> None:
        with self.lock:
            for e in events:
                e.handled = True

    def record_error(self, message: str) -> None:
        with self.lock:
            self.system.errors.append(message)
            self.system.errors = self.system.errors[-20:]
            self.system.consecutive_errors += 1
            self.system.last_error_at = self.now
            self.system.healthy = self.system.consecutive_errors < 3
        self.push_event(f"ERROR: {message}")

    def clear_errors(self) -> None:
        with self.lock:
            self.system.consecutive_errors = 0
            self.system.healthy = True
