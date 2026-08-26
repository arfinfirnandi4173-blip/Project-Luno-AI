"""
vision_occupancy.py
====================

LUNO P0.9 (Room Occupancy State + Presence Duration).

`RoomOccupancyModule` - a small, additive, OBSERVATIONAL state layer that
sits between the existing, already-hardened human-confirmation pipeline
(`VisionAdapter`'s own `HumanPresenceConfirmed`/`CameraPersonLeft`
events - P0.8.5/P0.8.6, both UNCHANGED by this sprint) and anything that
wants to know "is the room currently occupied, by how many people, and
for how long":

    Camera / YOLO
        v
    Person Detection            (luno/vision.py, luno/adapters/vision.py - unchanged)
        v
    Existing Human Confirmation (VisionAdapter._update_confirmed_presence() - unchanged)
        v
    Room Occupancy State        (THIS FILE - new)
        v
    Automation / Luno Awareness (AutomationEngine, camera_automation - unchanged)

This module does NOT control Home Assistant, WLED, or any other device -
it has no import of `luno.adapters.home_assistant`/`luno.tool_manager`,
no `tool_requested` publish, and no knowledge that `light.wled` exists.
It is a pure, in-memory, Event-Bus-driven state machine: read the
existing pipeline's own confirmed-presence signal, remember what it
means over time, and expose that as a snapshot other components (a
future automation rule, the dashboard, Proactive, voice status) can read
or subscribe to. See ARCHITECTURE_GUARD.md's own P0.9 entry and
`tests/test_p0_9_room_occupancy.py`'s Section N (static/behavioral
architecture-guard tests) for the enforced boundary.

--------------------------------------------------------------------
Why these three input events, and no others (Section 1/2/4/14 of the brief)
--------------------------------------------------------------------
- `HumanPresenceConfirmed` (`luno.adapters.events`, EVENT_TYPE
  "human_presence_confirmed") - VisionAdapter's own P0.8.6 confirmation
  gate: fires ONCE when `HUMAN_DETECTION_CONFIRM_CYCLES` consecutive
  tracked cycles each had at least one person at/above `HUMAN_DETECTION_
  CONFIDENCE`. This is the EXACT signal `config/automation_rules.json`'s
  real WLED-ON rule already keys on (`event.kind == "human_confirmed"`)
  - reusing it here means Occupancy's notion of "occupied" is, by
  construction, never looser than what already drives a real device
  action. NOT a second detection algorithm - this module never touches
  `person_confidences`/`HUMAN_DETECTION_CONFIDENCE`/YOLO/RTSP itself.
- `CameraPersonLeft` (EVENT_TYPE "camera_person_left") - the debounced
  ABSENCE signal `_update_person_presence()` already publishes after
  `CAMERA_PERSON_ABSENCE_TIMEOUT_S` of continuous non-detection. This is
  the SAME signal the real WLED-OFF rule keys on (`event.kind ==
  "human_cleared"` - see P0.8.2/P0.8.9). Deliberately NOT `HumanPresence
  Unconfirmed` - that gate's own docstring explicitly warns it "exists
  specifically to keep physical automation conservative, not to describe
  whether a person is still in the room" (a person stepping just out of
  frame for one tracked cycle unconfirms the AUTOMATION signal while
  Vision's own room-level presence, correctly, stays debounced-PRESENT).
  Occupancy's job is to describe the room, not to gate a device action,
  so it uses the room-presence signal, matching the brief's own "human_
  cleared" vocabulary (the same word already used for the WLED-OFF
  trigger).
- `VisionFrameProcessed` (EVENT_TYPE "vision_frame_processed") - fires
  once per tracked cycle (Sprint 8), already carries `data["human_
  count"]` (`len(current_humans)`, the SAME `_known_humans` count
  `vision_context.build_vision_context()` already reduces to `person_
  count` for `AutomationEngine` conditions - P0.7). This is how Occupancy
  learns about a person-COUNT change (1 -> 2, 2 -> 1) WITHOUT that
  requiring a new `HumanPresenceConfirmed` firing (which only fires on
  its own confirm/unconfirm streak transition, not on every count
  change) and WITHOUT a second YOLO inference or a second tracked-object
  algorithm - `human_count` is read verbatim from the existing event
  payload. The running count is cached on EVERY cycle (cheap: a dict
  read plus an int coercion, well within the <5ms per-event budget
  `AutomationEngine._on_bus_event()`'s own docstring already documents
  for this project's "subscribe to a high-frequency event, do the real
  work elsewhere" precedent), but only ever SURFACED via `get_snapshot()`
  while the room is occupied - Section 2's own "when vacant, person_
  count = 0" requirement is enforced at read time, not by trying to zero
  the cache out on every empty cycle.

--------------------------------------------------------------------
Timestamp semantics (Section 8)
--------------------------------------------------------------------
Two clocks, deliberately never mixed:
  - `time.monotonic()` - the ONLY clock used to compute `presence_
    duration_seconds`. Same convention `AutomationEngine`/`CameraAutomation
    Module` already established (P0.8.8's own dedicated test proves THAT
    module's cooldown is immune to a wall-clock jump) - a duration must
    never be able to go negative or jump because the system clock was
    adjusted (NTP sync, DST, manual change).
  - `luno.core.utils.utcnow()` - the project's existing timezone-aware
    UTC `datetime` helper (already used throughout `luno/core/`), for
    every human-readable ISO-8601 timestamp field (`occupied_since`,
    `vacant_since`, `last_seen`). Never used for duration arithmetic.

--------------------------------------------------------------------
Restart behavior (Section 9)
--------------------------------------------------------------------
This module has NO persistence of its own, and this is a deliberate
choice, not an oversight: occupancy state is inherently a "what is true
right now" signal, and nothing in this project already persists a
comparable "is Vision currently confirmed" state across a restart (the
underlying `VisionAdapter._human_confirmed`/`_human_confirm_streak` are
themselves plain in-memory attributes, reset to their dataclass defaults
on every process start). A `RoomOccupancyModule()` therefore always
starts `state="vacant"`, `person_count=0`, `occupied_since=None` - it
never fabricates an `occupied_since` from a previous run, and only
transitions to "occupied" once a genuine, fresh `HumanPresenceConfirmed`
arrives from the (also freshly-restarted) Vision pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .adapters.events import CameraPersonLeft, HumanPresenceConfirmed, VisionFrameProcessed
from .core.events import Event
from .core.models import ModuleHealthStatus
from .core.module_manager import Module
from .core.utils import log, utcnow

#: The three, and only three, upstream event types this module ever
#: subscribes to - see module docstring. Kept as named constants (not
#: retyped string literals) so a rename of any of these three EVENT_TYPE
#: values upstream cannot silently desync this file.
_HUMAN_CONFIRMED_EVENT_TYPE = HumanPresenceConfirmed.EVENT_TYPE
_HUMAN_CLEARED_EVENT_TYPE = CameraPersonLeft.EVENT_TYPE
_VISION_FRAME_PROCESSED_EVENT_TYPE = VisionFrameProcessed.EVENT_TYPE

STATE_VACANT = "vacant"
STATE_OCCUPIED = "occupied"

#: Transition events this module publishes - see module docstring
#: Section 5. `OCCUPANCY_CHANGED_EVENT_TYPE` is the brief's own "Optional"
#: umbrella event, published alongside (never instead of) the specific
#: one, for a listener that only cares "something changed" without
#: needing to know which direction.
ROOM_OCCUPIED_EVENT_TYPE = "room_occupied"
ROOM_VACANT_EVENT_TYPE = "room_vacant"
OCCUPANCY_CHANGED_EVENT_TYPE = "occupancy_changed"


def _iso(dt) -> str:
    return dt.isoformat()


@dataclass(frozen=True)
class RoomOccupancySnapshot:
    """The single canonical state snapshot (Section 7) - immutable, safe
    to hand to any reader (dashboard, a future automation condition,
    Proactive) without risking a shared-mutable-state bug. Every field
    matches the brief's own worked JSON example exactly.

    P0.10 additive fields (Phase 2) - both derived from the SAME
    `time.monotonic()` bookkeeping already used for `presence_duration_
    seconds`, never a new clock:
      - `occupancy_age_seconds` - time since the MOST RECENT transition,
        in EITHER direction. While occupied this equals `presence_
        duration_seconds` exactly (both measure from `occupied_since`).
        While vacant, this is DIFFERENT and additionally useful: it
        measures "how long has the room been empty" (time since `vacant_
        since`), whereas `presence_duration_seconds` stays frozen at the
        length of the visit that just ended (P0.9's own "do not keep
        increasing after vacant" contract, unchanged).
      - `last_transition` - `"occupied"` or `"vacant"`, whichever
        direction the most recent transition was - `None` only for a
        brand new instance that has never transitioned at all (Section
        8/Phase 8: never fabricated)."""

    state: str
    person_count: int
    occupied_since: Optional[str]
    vacant_since: Optional[str]
    last_seen: Optional[str]
    presence_duration_seconds: float
    occupancy_age_seconds: float
    last_transition: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "person_count": self.person_count,
            "occupied_since": self.occupied_since,
            "vacant_since": self.vacant_since,
            "last_seen": self.last_seen,
            "presence_duration_seconds": self.presence_duration_seconds,
            "occupancy_age_seconds": self.occupancy_age_seconds,
            "last_transition": self.last_transition,
        }


class RoomOccupancyModule(Module):
    name = "room_occupancy"
    #: No functional dependency on any other custom module (camera_
    #: automation, automation_engine) - this module only ever reads the
    #: shared Event Bus, the same "zero coupling beyond the Bus" contract
    #: `VisionCameraEventBridge`'s own module docstring documents for its
    #: OWN upstream Vision events (this module reuses the identical
    #: three of those four/six event types, adds no new coupling).
    dependencies: List[str] = []

    def __init__(self) -> None:
        self._event_bus: Any = None
        self._lock = threading.RLock()
        self._sub_ids: List[str] = []

        self._state: str = STATE_VACANT
        #: The most recently observed `VisionFrameProcessed.data
        #: ["human_count"]` - kept fresh on EVERY tracked cycle
        #: regardless of occupancy state (see module docstring), but
        #: only ever exposed via `get_snapshot()` while `state ==
        #: "occupied"` (Section 2: "When the room is vacant: person_count
        #: = 0").
        self._last_known_human_count: int = 0
        self._occupied_since_monotonic: Optional[float] = None
        self._occupied_since_wall: Optional[str] = None
        self._vacant_since_wall: Optional[str] = None
        self._last_seen_wall: Optional[str] = None
        #: The FINAL, completed duration of the most recently ended
        #: occupied period - frozen the moment the room becomes vacant
        #: (Section 3: "Do not continue increasing it after the room
        #: becomes vacant"), and what `get_snapshot()` reports while
        #: `state == "vacant"`. Reset to 0.0 the moment a NEW occupied
        #: period begins (Section 4 - re-entry never merges with a prior
        #: visit).
        self._final_presence_duration_s: float = 0.0
        #: P0.10 (Phase 2) - the monotonic instant of the MOST RECENT
        #: genuine transition, either direction, and which direction it
        #: was. `None`/`None` for a brand new instance that has never
        #: transitioned - never fabricated (see `RoomOccupancySnapshot`'s
        #: own docstring for the full `occupancy_age_seconds`/`last_
        #: transition` rationale).
        self._last_transition_monotonic: Optional[float] = None
        self._last_transition: Optional[str] = None

    # -- Module lifecycle ---------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        if self._event_bus is None:
            return
        self._sub_ids = [
            self._event_bus.subscribe(_HUMAN_CONFIRMED_EVENT_TYPE, self._on_human_confirmed),
            self._event_bus.subscribe(_HUMAN_CLEARED_EVENT_TYPE, self._on_human_cleared),
            self._event_bus.subscribe(_VISION_FRAME_PROCESSED_EVENT_TYPE, self._on_vision_frame_processed),
        ]

    def stop(self) -> None:
        """Never leaves a subscription orphaned - same `stop()` contract
        every other module in this project follows (exceptions logged
        and swallowed, never block shutdown)."""
        for sub_id in self._sub_ids:
            try:
                if self._event_bus is not None:
                    self._event_bus.unsubscribe(sub_id)
            except Exception as ex:  # pragma: no cover - defensive
                log(f"stop() failed to unsubscribe from event bus (ignored): {ex}", self.name)
        self._sub_ids = []

    def health(self) -> ModuleHealthStatus:
        with self._lock:
            state, count = self._state, self._last_known_human_count
        return ModuleHealthStatus(healthy=True, message=f"state={state}, person_count={count if state == STATE_OCCUPIED else 0}")

    # -- public read API (Section 7) -----------------------------------------

    def get_snapshot(self) -> RoomOccupancySnapshot:
        """Safe to call from any thread/component - a plain, cheap,
        lock-protected read of in-memory state (no I/O, no Event Bus
        publish). This is the ONE canonical occupancy snapshot other
        Luno components should read (Section 13: "only one canonical
        occupancy state owner")."""
        with self._lock:
            return self._build_snapshot_locked()

    def _build_snapshot_locked(self) -> RoomOccupancySnapshot:
        if self._state == STATE_OCCUPIED:
            person_count = max(0, self._last_known_human_count)
            duration = (
                time.monotonic() - self._occupied_since_monotonic
                if self._occupied_since_monotonic is not None else 0.0
            )
        else:
            person_count = 0
            duration = self._final_presence_duration_s
        # P0.10 (Phase 2) - "time in the CURRENT state", either
        # direction, always derived from `time.monotonic()`. While
        # occupied this is identical to `duration` above (both measure
        # from the same `occupied_since` instant); while vacant it is
        # DIFFERENT - "how long has the room been empty" - see
        # `RoomOccupancySnapshot`'s own docstring.
        age = (
            time.monotonic() - self._last_transition_monotonic
            if self._last_transition_monotonic is not None else 0.0
        )
        return RoomOccupancySnapshot(
            state=self._state,
            person_count=person_count,
            occupied_since=self._occupied_since_wall,
            vacant_since=self._vacant_since_wall,
            last_seen=self._last_seen_wall,
            presence_duration_seconds=duration,
            occupancy_age_seconds=age,
            last_transition=self._last_transition,
        )

    # -- event handlers -------------------------------------------------------

    def _on_human_confirmed(self, event: Event) -> None:
        """`human_confirmed` -> occupancy becomes `occupied` (Section 1).
        A genuine vacant->occupied transition creates a NEW `occupied_
        since` and resets `presence_duration_seconds` to 0 (Section 4 -
        re-entry never merges with a prior visit). While ALREADY
        occupied (e.g. this fires again after a brief unconfirm/reconfirm
        blip - `HumanPresenceConfirmed` only fires on its own rising
        edge so this is rare, but handled defensively, never assumed
        away), `occupied_since` is left completely untouched (Section 6:
        person-count changes, and by extension any re-confirmation while
        still occupied, must never reset it) - only `last_seen` and a
        `room_occupied` event's ABSENCE are affected."""
        with self._lock:
            now = utcnow()
            if self._state != STATE_OCCUPIED:
                previous_state = STATE_VACANT
                self._state = STATE_OCCUPIED
                self._occupied_since_monotonic = time.monotonic()
                self._occupied_since_wall = _iso(now)
                self._vacant_since_wall = None
                self._final_presence_duration_s = 0.0
                # P0.10 (Phase 2/4) - this IS the transition instant;
                # `occupancy_age_seconds` and `presence_duration_seconds`
                # are identical from this point forward until the next
                # genuine transition (see snapshot docstring).
                self._last_transition_monotonic = self._occupied_since_monotonic
                self._last_transition = STATE_OCCUPIED
                # `HumanPresenceConfirmed` logically implies at least one
                # real, currently-qualifying person this cycle - a safety
                # floor for the (narrow, cold-start-only) case where no
                # `VisionFrameProcessed` has been observed yet at all,
                # never a fabricated/guessed value once real data exists
                # (the very next tracked cycle's own `VisionFrameProcessed`
                # corrects this to the real count regardless).
                self._last_known_human_count = max(1, self._last_known_human_count)
                self._last_seen_wall = _iso(now)
                snapshot = self._build_snapshot_locked()
                log(
                    f"[VISION OCCUPANCY] state=vacant -> occupied person_count={snapshot.person_count}",
                    self.name,
                )
                self._publish_transition(ROOM_OCCUPIED_EVENT_TYPE, snapshot, previous_state)
            else:
                self._last_seen_wall = _iso(now)

    def _on_human_cleared(self, event: Event) -> None:
        """`human_cleared` -> occupancy becomes `vacant` (Section 1).
        Freezes `presence_duration_seconds` at its final, completed value
        (Section 3) and records `vacant_since`. Idempotent while already
        vacant - never publishes a duplicate `room_vacant` (Section 5)."""
        with self._lock:
            if self._state == STATE_VACANT:
                return
            now = utcnow()
            duration = (
                time.monotonic() - self._occupied_since_monotonic
                if self._occupied_since_monotonic is not None else 0.0
            )
            self._final_presence_duration_s = duration
            self._state = STATE_VACANT
            self._vacant_since_wall = _iso(now)
            self._occupied_since_wall = None
            self._occupied_since_monotonic = None
            self._last_known_human_count = 0
            self._last_seen_wall = _iso(now)
            # P0.10 (Phase 2/4) - vacant_since IS the new transition
            # instant; `occupancy_age_seconds` now measures "how long
            # has the room been empty" going forward.
            self._last_transition_monotonic = time.monotonic()
            self._last_transition = STATE_VACANT
            snapshot = self._build_snapshot_locked()
            log(
                f"[VISION OCCUPANCY] state=occupied -> vacant person_count=0 "
                f"presence_duration={duration:.1f}s",
                self.name,
            )
            self._publish_transition(ROOM_VACANT_EVENT_TYPE, snapshot, STATE_OCCUPIED)

    def _on_vision_frame_processed(self, event: Event) -> None:
        """Keeps `person_count` fresh across a multi-person session
        WITHOUT deriving occupancy STATE from it (Section 2/6/14 - state
        transitions remain the exclusive authority of `_on_human_
        confirmed`/`_on_human_cleared` above; raw tracked-cycle data is
        never used to flip vacant/occupied). Deliberately NOT logged
        (Section 10: "Do not log every frame") - this fires once per
        tracked cycle (Sprint 8, ~0.5-1s cadence), a silent cache
        refresh only."""
        data = event.data or {}
        raw = data.get("human_count", 0)
        try:
            count = max(0, int(raw))
        except (TypeError, ValueError):
            count = 0
        with self._lock:
            self._last_known_human_count = count
            if self._state == STATE_OCCUPIED:
                self._last_seen_wall = _iso(utcnow())

    # -- Event Bus publishing (Section 5) --------------------------------------

    def _publish_transition(
        self, event_type: str, snapshot: RoomOccupancySnapshot, previous_state: str
    ) -> None:
        """Called with `self._lock` already held (an `RLock`, so this
        is safe/reentrant) - `get_snapshot()`'s own locking is not
        re-entered incorrectly. Never raises - a broken publish must
        never leave this module's own in-memory state transition
        incomplete (same defensive-publish convention `AutomationEngine.
        _publish()` already follows).

        P0.10 (Phase 4) - `previous_state` is included in the payload
        so `AutomationEngine`/diagnostics can see which direction this
        transition came from without inferring it from `last_transition`
        (which reflects post-transition state, same value)."""
        if self._event_bus is None:
            return
        payload = snapshot.to_dict()
        payload["previous_state"] = previous_state
        try:
            self._event_bus.publish(Event(type=event_type, data=payload))
            self._event_bus.publish(Event(type=OCCUPANCY_CHANGED_EVENT_TYPE, data=payload))
        except Exception as ex:  # pragma: no cover - defensive
            log(f"failed to publish {event_type}/{OCCUPANCY_CHANGED_EVENT_TYPE} (ignored): {ex}", self.name)
