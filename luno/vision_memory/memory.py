"""
memory.py
=========

`VisionMemory` is the orchestrator that ties everything else in this
package together into the four memory layers from the spec:

1. **Instant memory** (1-5s, in-process only, never persisted) - a short
   rolling window of raw observations, used ONLY to debounce single-frame
   flicker (an object/human momentarily missed for one frame shouldn't
   register as a real disappearance/departure - see `_PendingRemoval`
   below). Nothing here ever reaches SQLite.
2. **Short-term memory** (`WorldState`, TTL 5-10 min of inactivity) - the
   current, best-known state of the room. Refreshed every `update()` call;
   reset to empty if `update()` hasn't been called for
   `SHORT_TERM_MEMORY_TTL_S`, or on-demand via `clear_short_memory()`.
3. **Event memory** - meaningful, scored, deduplicated changes, persisted
   append-only via `database.Database.insert_event`.
4. **Long-term memory** - habits/patterns promoted from an event signature
   that has recurred `LONG_TERM_PROMOTION_COUNT` times, spanning at least
   `LONG_TERM_PROMOTION_MIN_SPAN_S` of wall-clock time (so five identical
   events fired seconds apart in one sitting do NOT count as a "habit" -
   they need to recur across genuinely separate observations over time).

`update()` never calls a vision model, an LLM, the network, or touches raw
image data - it only does in-memory diffing plus small SQLite writes, which
is what keeps it well under the 50ms budget (see `PERFORMANCE_WARN_S` below
for the self-monitoring check).
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, List

from .database import Database
from .event_detector import EventDetector
from .importance import IMPORTANCE_THRESHOLD
from .models import (
    EventRecord,
    LongTermMemoryRecord,
    ObjectStatus,
    RoomObservation,
    SceneObservation,
    TrackedObject,
    WorldState,
)
from .scene_graph import SceneGraphBuilder
from .tracker import HumanTracker, ObjectTracker
from .utils import utcnow

# -- tunables (spec gives ranges; these are the defaults within them) -------

INSTANT_MEMORY_WINDOW_S = 3.0          # spec range: 1-5s
SHORT_TERM_MEMORY_TTL_S = 10 * 60.0    # spec range: 5-10 min
DEDUP_COOLDOWN_S = 30.0                # min gap before an identical event can fire again
LONG_TERM_PROMOTION_COUNT = 5          # occurrences of the same event signature...
LONG_TERM_PROMOTION_MIN_SPAN_S = 60 * 60.0  # ...spanning at least this long, to count as a habit
PERFORMANCE_WARN_S = 0.05              # self-monitoring: update() should stay under this


class VisionMemory:
    """One instance = one persistent memory store (one SQLite file). Not a
    singleton itself - `api.py` is what exposes a module-level singleton for
    the exact call style the spec asks for (`vision_memory.update(...)`)."""

    def __init__(self, db_path: str) -> None:
        self._db = Database(db_path)
        self._event_detector = EventDetector(dedup_cooldown_s=DEDUP_COOLDOWN_S)

        loaded = self._db.load_world_state()
        self._world_state: WorldState = loaded if loaded is not None else WorldState()

        self._object_id_counters: Dict[str, int] = self._db.get_metadata("object_id_counters", {}) or {}
        self._human_id_counters: Dict[str, int] = self._db.get_metadata("human_id_counters", {}) or {}

        # instant memory: recent raw observations, time-windowed, RAM only.
        self._instant_memory: Deque[SceneObservation] = deque()

        # debounce state for "disappeared"/"left" - object/human id -> the
        # timestamp it was FIRST missed. Cleared on reappearance. Finalized
        # (event fired, status becomes REMOVED/dropped) once the miss has
        # persisted for the whole instant-memory window.
        self._pending_object_removal: Dict[str, datetime] = {}
        self._pending_human_departure: Dict[str, datetime] = {}

    # -- public API (see api.py for the module-level wrapper) ---------------

    def update(self, observation: SceneObservation) -> List[EventRecord]:
        """Ingest one new observation, update all memory layers, and return
        the (possibly empty) list of NEW events this call produced."""
        started = time.perf_counter()
        now = observation.timestamp

        self._remember_instant(observation)
        self._maybe_expire_short_term(now)

        object_result = ObjectTracker.update(
            previous=self._world_state.objects, observations=observation.objects,
            now=now, id_counters=self._object_id_counters,
        )
        human_result = HumanTracker.update(
            previous=self._world_state.humans, observations=observation.humans,
            now=now, id_counters=self._human_id_counters,
        )

        # Debounce mutates object_result.objects/human_result.humans in
        # place (putting flickering entries back to PRESENT) and returns
        # only the FINALIZED (confirmed-gone) subset, which we swap in
        # before handing the results to the event detector.
        object_result.disappeared = self._debounce_object_removals(object_result, now)
        human_result.left = self._debounce_human_departures(human_result, now)

        merged_room = self._merge_room(self._world_state.room, observation.room)
        relations = SceneGraphBuilder.build(object_result.objects, human_result.humans)

        new_state = WorldState(
            objects=object_result.objects, humans=human_result.humans,
            room=merged_room, relations=relations, updated_at=now,
        )

        events = self._event_detector.detect(
            now=now,
            object_result=object_result,
            human_result=human_result,
            previous_room=self._world_state.room,
            new_room=merged_room,
        )

        self._persist(new_state, events, now)
        self._world_state = new_state

        for event in events:
            self._update_long_term_pattern(event, now)

        elapsed = time.perf_counter() - started
        if elapsed > PERFORMANCE_WARN_S:
            print(f"[VisionMemory] ⚠ update() took {elapsed * 1000:.1f}ms (target < {PERFORMANCE_WARN_S * 1000:.0f}ms)")

        return events

    def get_recent_events(self, limit: int = 20, min_importance: int = IMPORTANCE_THRESHOLD) -> List[EventRecord]:
        return self._db.get_recent_events(limit=limit, min_importance=min_importance)

    def get_world_state(self) -> WorldState:
        return self._world_state

    def get_long_term_memory(self) -> List[LongTermMemoryRecord]:
        return self._db.get_long_term_memory()

    def clear_short_memory(self) -> None:
        """Wipes instant memory, short-term world state (both the in-memory
        copy and its DB row), and event-dedup state. Long-term memory is
        untouched - habits already learned survive a short-term reset."""
        self._instant_memory.clear()
        self._pending_object_removal.clear()
        self._pending_human_departure.clear()
        self._world_state = WorldState()
        self._db.clear_world_state()
        self._event_detector = EventDetector(dedup_cooldown_s=DEDUP_COOLDOWN_S)

    def export_json(self, event_limit: int = 50) -> str:
        """Full memory dump as a JSON string - world state, recent events,
        and long-term memory. Never includes raw frames (there are none to
        include - this module never receives or stores image data)."""
        payload = {
            "exported_at": utcnow().isoformat(),
            "world_state": self._world_state.to_dict(),
            "recent_events": [e.to_dict() for e in self.get_recent_events(limit=event_limit, min_importance=1)],
            "long_term_memory": [m.to_dict() for m in self.get_long_term_memory()],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    # -- instant memory -----------------------------------------------------

    def _remember_instant(self, observation: SceneObservation) -> None:
        self._instant_memory.append(observation)
        cutoff = observation.timestamp - timedelta(seconds=INSTANT_MEMORY_WINDOW_S)
        while self._instant_memory and self._instant_memory[0].timestamp < cutoff:
            self._instant_memory.popleft()

    # -- short-term expiry ----------------------------------------------------

    def _maybe_expire_short_term(self, now: datetime) -> None:
        updated_at = self._world_state.updated_at
        if updated_at is not None and (now - updated_at).total_seconds() > SHORT_TERM_MEMORY_TTL_S:
            # Silent reset - a gap this long means the room's state is
            # simply unknown again, not that everything visibly disappeared
            # (there's no vision data for the gap itself), so no removal
            # events are synthesized for the reset.
            self._world_state = WorldState()
            self._pending_object_removal.clear()
            self._pending_human_departure.clear()

    # -- debounce (instant-memory-backed flicker filtering) -------------------

    def _debounce_object_removals(self, object_result, now: datetime) -> List[TrackedObject]:
        """Only confirm an object's disappearance once it has been missing
        continuously for the whole instant-memory window - a single-frame
        miss (the description just didn't happen to mention it that frame)
        gets the object put back to PRESENT instead of firing a false
        "removed" event."""
        finalized: List[TrackedObject] = []
        still_missing_ids = {o.id for o in object_result.disappeared}

        for obj in list(object_result.disappeared):
            first_missed = self._pending_object_removal.get(obj.id)
            if first_missed is None:
                self._pending_object_removal[obj.id] = now
                # Not finalized yet this cycle - keep it PRESENT in the
                # working state using its last known attributes.
                object_result.objects[obj.id] = TrackedObject(
                    id=obj.id, label=obj.label, color=obj.color, location=obj.location,
                    status=ObjectStatus.PRESENT, first_seen=obj.first_seen, last_seen=obj.last_seen,
                )
            elif (now - first_missed).total_seconds() >= INSTANT_MEMORY_WINDOW_S:
                finalized.append(obj)
                self._pending_object_removal.pop(obj.id, None)
            else:
                object_result.objects[obj.id] = TrackedObject(
                    id=obj.id, label=obj.label, color=obj.color, location=obj.location,
                    status=ObjectStatus.PRESENT, first_seen=obj.first_seen, last_seen=obj.last_seen,
                )

        # Anything that reappeared (present again, not in this cycle's
        # missing set) clears its pending-removal timer.
        for oid in list(self._pending_object_removal.keys()):
            if oid not in still_missing_ids:
                self._pending_object_removal.pop(oid, None)

        return finalized

    def _debounce_human_departures(self, human_result, now: datetime) -> List:
        finalized = []
        still_missing_ids = {h.id for h in human_result.left}

        for human in list(human_result.left):
            first_missed = self._pending_human_departure.get(human.id)
            if first_missed is None:
                self._pending_human_departure[human.id] = now
                human_result.humans[human.id] = human  # keep present for now
            elif (now - first_missed).total_seconds() >= INSTANT_MEMORY_WINDOW_S:
                finalized.append(human)
                self._pending_human_departure.pop(human.id, None)
            else:
                human_result.humans[human.id] = human

        for hid in list(self._pending_human_departure.keys()):
            if hid not in still_missing_ids:
                self._pending_human_departure.pop(hid, None)

        return finalized

    # -- room state merge -----------------------------------------------------

    @staticmethod
    def _merge_room(previous: RoomObservation, new_obs: RoomObservation) -> RoomObservation:
        """A frame that doesn't mention the light/door shouldn't erase what
        we already knew - only an EXPLICIT new value overrides the
        previous one."""
        merged_extra = dict(previous.extra)
        merged_extra.update(new_obs.extra)
        return RoomObservation(
            light_on=new_obs.light_on if new_obs.light_on is not None else previous.light_on,
            door_closed=new_obs.door_closed if new_obs.door_closed is not None else previous.door_closed,
            extra=merged_extra,
        )

    # -- persistence ------------------------------------------------------------

    def _persist(self, state: WorldState, events: List[EventRecord], now: datetime) -> None:
        for obj in state.objects.values():
            self._db.upsert_object(obj)
        self._db.save_world_state(state)
        for event in events:
            self._db.insert_event(event)
        self._db.set_metadata("object_id_counters", self._object_id_counters)
        self._db.set_metadata("human_id_counters", self._human_id_counters)

    # -- long-term memory promotion --------------------------------------------

    def _update_long_term_pattern(self, event: EventRecord, now: datetime) -> None:
        # Deliberately keyed on category+description ONLY - NOT
        # related_object_id/related_human_id. Those ids are tied to a
        # specific tracked instance that gets a fresh id every time
        # short-term memory expires/resets (see _maybe_expire_short_term) -
        # e.g. "Vinn" becomes vinn#1 in one session and vinn#2 in the next.
        # A habit is defined by RECURRING across separate sessions, so the
        # key has to survive that reset; the description text itself
        # ("Vinn started typing.") is already stable across sessions as
        # long as the identity/label/phrasing stay the same, which is
        # exactly the signal worth counting.
        pattern_key = f"ltm_pattern:{event.category.value}:{event.description}"
        record = self._db.get_metadata(pattern_key, None)
        bucket = _time_bucket(now)
        if record is None:
            record = {"count": 1, "first_seen": now.isoformat(), "last_seen": now.isoformat(), "buckets": {bucket: 1}}
        else:
            record["count"] += 1
            record["last_seen"] = now.isoformat()
            record["buckets"][bucket] = record["buckets"].get(bucket, 0) + 1
        self._db.set_metadata(pattern_key, record)

        count = record["count"]
        first_seen = datetime.fromisoformat(record["first_seen"])
        span_s = (now - first_seen).total_seconds()
        if count >= LONG_TERM_PROMOTION_COUNT and span_s >= LONG_TERM_PROMOTION_MIN_SPAN_S:
            self._promote_to_long_term(event, record, now)

    def _promote_to_long_term(self, event: EventRecord, record: dict, now: datetime) -> None:
        count = record["count"]
        buckets: Dict[str, int] = record.get("buckets", {})
        dominant_bucket = None
        if buckets:
            top_bucket, top_count = max(buckets.items(), key=lambda kv: kv[1])
            if top_count / count >= 0.6:
                dominant_bucket = top_bucket

        time_qualifier = f" in the {dominant_bucket}" if dominant_bucket else ""
        base = event.description.rstrip(".")
        statement = f"Usually: {base}{time_qualifier}."

        confidence = round(min(0.99, 1.0 - 1.0 / count), 3)
        existing = {m.statement: m for m in self._db.get_long_term_memory()}
        prior = existing.get(statement)
        created_at = prior.created_at if prior is not None else now

        self._db.upsert_long_term_memory(
            LongTermMemoryRecord(
                id=None, statement=statement, confidence=confidence,
                observation_count=count, created_at=created_at, updated_at=now,
            )
        )


def _time_bucket(dt: datetime) -> str:
    hour = dt.hour
    if 0 <= hour < 6:
        return "early morning"
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    return "evening"


