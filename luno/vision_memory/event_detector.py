"""
event_detector.py
===================

Turns one `ObjectTracker`/`HumanTracker` pass plus a room-state diff into
the list of `EventRecord`s actually worth remembering. This is where the
spec's change-detection rules live:

    - Nothing important changed / only wording changed  -> ignore.
    - Important object appears/disappears                -> event.
    - Room lighting changes                               -> event.
    - User changes activity                                -> event.

"Worth remembering" = passes `importance.should_store()` (score >= 3) AND
isn't a near-duplicate of something already recorded in the last
`dedup_cooldown_s` seconds (the same object disappearing described slightly
differently three frames in a row from the vision model's normal wording
jitter should not become three separate events).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import importance
from .models import EventCategory, EventRecord, HumanActivity, RoomObservation, TrackedHuman, TrackedObject
from .tracker import HumanTrackingResult, ObjectTrackingResult

_ACTIVITY_PHRASES: Dict[HumanActivity, str] = {
    HumanActivity.TYPING: "started typing",
    HumanActivity.STANDING: "stood up",
    HumanActivity.SITTING: "sat down",
    HumanActivity.WALKING: "started walking",
    HumanActivity.READING: "started reading",
    HumanActivity.SLEEPING: "fell asleep",
    HumanActivity.LOOKING_AT_MONITOR: "started looking at the monitor",
}


@dataclass
class _Candidate:
    category: EventCategory
    description: str
    related_object_id: Optional[str] = None
    related_human_id: Optional[str] = None
    score_kwargs: dict = None  # forwarded to importance.score_change

    def __post_init__(self) -> None:
        if self.score_kwargs is None:
            self.score_kwargs = {}


class EventDetector:
    def __init__(self, dedup_cooldown_s: float = 30.0) -> None:
        self._recent_signatures: Dict[str, datetime] = {}
        self._dedup_cooldown_s = dedup_cooldown_s

    def detect(
        self,
        now: datetime,
        object_result: ObjectTrackingResult,
        human_result: HumanTrackingResult,
        previous_room: RoomObservation,
        new_room: RoomObservation,
    ) -> List[EventRecord]:
        candidates: List[_Candidate] = []
        candidates += [self._object_candidate(EventCategory.OBJECT_APPEARED, o) for o in object_result.appeared]
        candidates += [self._object_candidate(EventCategory.OBJECT_DISAPPEARED, o) for o in object_result.disappeared]
        candidates += [self._object_candidate(EventCategory.OBJECT_MOVED, o) for o in object_result.moved]

        candidates += [self._human_presence_candidate(EventCategory.HUMAN_ENTERED, h) for h in human_result.entered]
        candidates += [self._human_presence_candidate(EventCategory.HUMAN_LEFT, h) for h in human_result.left]
        candidates += [
            self._activity_candidate(h, prev) for h, prev in human_result.activity_changed
        ]
        candidates += [
            self._emotion_candidate(h, prev) for h, prev in human_result.emotion_changed
        ]

        room_candidate = self._room_light_candidate(previous_room, new_room)
        if room_candidate is not None:
            candidates.append(room_candidate)
        door_candidate = self._room_door_candidate(previous_room, new_room)
        if door_candidate is not None:
            candidates.append(door_candidate)

        return self._score_filter_dedupe(candidates, now)

    # -- candidate builders --------------------------------------------------

    @staticmethod
    def _describe_object(obj: TrackedObject) -> str:
        if obj.color:
            return f"{obj.color.capitalize()} {obj.label}"
        return obj.label.capitalize()

    def _object_candidate(self, category: EventCategory, obj: TrackedObject) -> _Candidate:
        subject = self._describe_object(obj)
        if category == EventCategory.OBJECT_APPEARED:
            desc = f"{subject} appeared" + (f" {obj.location}" if obj.location else "") + "."
        elif category == EventCategory.OBJECT_DISAPPEARED:
            desc = f"{subject} removed" + (f" from {obj.location}" if obj.location else "") + "."
        else:  # MOVED
            desc = f"{subject} moved" + (f" to {obj.location}" if obj.location else "") + "."
        return _Candidate(category, desc, related_object_id=obj.id, score_kwargs={"label": obj.label})

    @staticmethod
    def _human_presence_candidate(category: EventCategory, human: TrackedHuman) -> _Candidate:
        name = human.identity or "Someone"
        verb = "entered the room" if category == EventCategory.HUMAN_ENTERED else "left the room"
        return _Candidate(category, f"{name} {verb}.", related_human_id=human.id)

    @staticmethod
    def _activity_candidate(human: TrackedHuman, previous: HumanActivity) -> _Candidate:
        name = human.identity or "User"
        phrase = _ACTIVITY_PHRASES.get(human.activity, f"changed activity to {human.activity.value}")
        # Any genuine activity transition (excluding UNKNOWN on either side,
        # which usually just means the frame's description didn't mention
        # an activity at all rather than a real change) is treated as
        # notable per the spec's own examples ("User started coding",
        # "User stood up") - bumps the importance score up a tier.
        notable = human.activity != HumanActivity.UNKNOWN and previous != HumanActivity.UNKNOWN
        return _Candidate(
            EventCategory.ACTIVITY_CHANGED, f"{name} {phrase}.",
            related_human_id=human.id, score_kwargs={"is_notable_transition": notable},
        )

    @staticmethod
    def _emotion_candidate(human: TrackedHuman, previous: Optional[str]) -> _Candidate:
        name = human.identity or "User"
        return _Candidate(
            EventCategory.EMOTION_CHANGED, f"{name}'s emotion changed to {human.emotion}.",
            related_human_id=human.id, score_kwargs={"emotion": human.emotion},
        )

    @staticmethod
    def _room_light_candidate(previous: RoomObservation, new: RoomObservation) -> Optional[_Candidate]:
        if previous.light_on is None or new.light_on is None or previous.light_on == new.light_on:
            return None
        desc = "Room light turned on." if new.light_on else "Room light turned off."
        return _Candidate(EventCategory.ROOM_LIGHT_CHANGED, desc)

    @staticmethod
    def _room_door_candidate(previous: RoomObservation, new: RoomObservation) -> Optional[_Candidate]:
        if previous.door_closed is None or new.door_closed is None or previous.door_closed == new.door_closed:
            return None
        desc = "Door closed." if new.door_closed else "Door opened."
        return _Candidate(EventCategory.ROOM_DOOR_CHANGED, desc)

    # -- scoring, filtering, dedup --------------------------------------------

    def _score_filter_dedupe(self, candidates: List[_Candidate], now: datetime) -> List[EventRecord]:
        self._prune_old_signatures(now)

        stored: List[EventRecord] = []
        for c in candidates:
            score = importance.score_change(c.category, **c.score_kwargs)
            if not importance.should_store(score):
                continue

            signature = f"{c.category.value}:{c.related_object_id or ''}:{c.related_human_id or ''}:{c.description}"
            last_seen = self._recent_signatures.get(signature)
            if last_seen is not None and (now - last_seen).total_seconds() < self._dedup_cooldown_s:
                continue  # same event already recorded recently - skip the duplicate

            self._recent_signatures[signature] = now
            stored.append(
                EventRecord(
                    id=None, timestamp=now, category=c.category, description=c.description,
                    importance=score, related_object_id=c.related_object_id, related_human_id=c.related_human_id,
                )
            )
        return stored

    def _prune_old_signatures(self, now: datetime) -> None:
        cutoff = timedelta(seconds=self._dedup_cooldown_s)
        stale = [sig for sig, ts in self._recent_signatures.items() if now - ts > cutoff]
        for sig in stale:
            del self._recent_signatures[sig]
