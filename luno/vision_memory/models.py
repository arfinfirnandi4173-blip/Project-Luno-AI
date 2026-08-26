"""
models.py
=========

Typed data structures shared across every Vision Memory module. Nothing in
this file talks to SQLite, OpenCV, or any vision model - it is pure data.

Two families of types live here:

- **Observation types** (`ObjectObservation`, `HumanObservation`,
  `RoomObservation`, `SceneObservation`) - the *input* shape to
  `memory.VisionMemory.update()`. These describe a single frame's worth of
  raw perception, BEFORE persistent identity has been assigned to anything.
  Producing a `SceneObservation` from a vision model's raw output (e.g.
  Gemini's free-text description) is the job of the integration layer
  (see `utils.parse_description_heuristic` for a basic default) - this
  module deliberately does not care how the observation was produced, only
  that it arrives in this shape. That is what keeps Vision Memory usable
  with any future vision model without changing anything downstream.

- **State types** (`TrackedObject`, `TrackedHuman`, `SceneRelation`,
  `WorldState`, `EventRecord`, `LongTermMemoryRecord`) - persistent,
  identity-bearing records that `tracker.py`/`event_detector.py`/
  `memory.py` build up over time and `database.py` stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ObjectStatus(str, Enum):
    """Whether a tracked object is currently believed to be in the scene."""
    PRESENT = "present"
    REMOVED = "removed"


class HumanActivity(str, Enum):
    """Coarse activity classes a human can be doing. `UNKNOWN` is the safe
    default when the upstream description doesn't clearly indicate one of
    these - callers are free to pass any string through `HumanObservation`
    construction helpers; unrecognized values fall back to UNKNOWN rather
    than raising, since this is inherently a fuzzy classification."""
    TYPING = "typing"
    STANDING = "standing"
    WALKING = "walking"
    SITTING = "sitting"
    LOOKING_AT_MONITOR = "looking_at_monitor"
    SLEEPING = "sleeping"
    READING = "reading"
    UNKNOWN = "unknown"

    @classmethod
    def from_text(cls, value: Optional[str]) -> "HumanActivity":
        if not value:
            return cls.UNKNOWN
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.UNKNOWN


class EventCategory(str, Enum):
    """What kind of change an `EventRecord` represents. Used by
    `importance.py` to look up a base severity score and by callers who want
    to filter/react to specific kinds of events."""
    HUMAN_ENTERED = "human_entered"
    HUMAN_LEFT = "human_left"
    OBJECT_APPEARED = "object_appeared"
    OBJECT_DISAPPEARED = "object_disappeared"
    OBJECT_MOVED = "object_moved"
    ROOM_LIGHT_CHANGED = "room_light_changed"
    ROOM_DOOR_CHANGED = "room_door_changed"
    ACTIVITY_CHANGED = "activity_changed"
    EMOTION_CHANGED = "emotion_changed"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Observation types (input to VisionMemory.update())
# ---------------------------------------------------------------------------

@dataclass
class ObjectObservation:
    """One object as reported for the CURRENT frame - not yet assigned a
    persistent id (that is `tracker.ObjectTracker`'s job)."""
    label: str
    color: Optional[str] = None
    location: Optional[str] = None
    confidence: float = 1.0


@dataclass
class HumanObservation:
    """One human as reported for the CURRENT frame - not yet assigned a
    persistent id."""
    identity: Optional[str] = None
    emotion: Optional[str] = None
    pose: Optional[str] = None
    activity: HumanActivity = HumanActivity.UNKNOWN


@dataclass
class RoomObservation:
    """Environment-level state that isn't tied to a specific object/human.
    `None` means "not reported this frame" (distinct from `False`/closed) -
    diffing logic in `event_detector.py` only reacts to an actual True<->False
    flip, never to a field simply going unreported."""
    light_on: Optional[bool] = None
    door_closed: Optional[bool] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SceneObservation:
    """The structured input to `VisionMemory.update()`. Producing this from
    a vision model's raw output is the integration layer's responsibility -
    see `utils.parse_description_heuristic` for a basic keyword-based
    default that works directly off Gemini's free-text description."""
    timestamp: datetime
    raw_description: str
    objects: List[ObjectObservation] = field(default_factory=list)
    humans: List[HumanObservation] = field(default_factory=list)
    room: RoomObservation = field(default_factory=RoomObservation)


# ---------------------------------------------------------------------------
# State types (persistent, identity-bearing)
# ---------------------------------------------------------------------------

@dataclass
class TrackedObject:
    """A persistently-identified object in the current world state.

    NOTE ON IDENTITY: `id` (e.g. "cup#1") is assigned by `tracker.py` using
    a label+color+location similarity heuristic, NOT true visual
    re-identification (no embeddings, no bounding-box IoU tracking - the
    upstream signal is a text description, not raw detections with boxes).
    Two visually-distinct objects that happen to share a label and rough
    location can be mis-matched. This is a reasoned trade-off for a system
    fed by natural-language scene descriptions rather than a real object
    detector's structured output - if `objects` observations start including
    real bounding boxes (e.g. from YOLO) or embeddings, `tracker.py` is the
    only file that would need to change to use them instead.
    """
    id: str
    label: str
    color: Optional[str]
    location: Optional[str]
    status: ObjectStatus
    first_seen: datetime
    last_seen: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "color": self.color,
            "location": self.location,
            "status": self.status.value,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackedObject":
        return cls(
            id=data["id"],
            label=data["label"],
            color=data.get("color"),
            location=data.get("location"),
            status=ObjectStatus(data["status"]),
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
        )


@dataclass
class TrackedHuman:
    """A persistently-identified human in the current world state. `id` is
    stable per distinct identity string (or a generic slot like "user#1" if
    no identity was ever reported) - see `tracker.HumanTracker`."""
    id: str
    identity: Optional[str]
    emotion: Optional[str]
    pose: Optional[str]
    activity: HumanActivity
    first_seen: datetime
    last_seen: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "identity": self.identity,
            "emotion": self.emotion,
            "pose": self.pose,
            "activity": self.activity.value,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackedHuman":
        return cls(
            id=data["id"],
            identity=data.get("identity"),
            emotion=data.get("emotion"),
            pose=data.get("pose"),
            activity=HumanActivity.from_text(data.get("activity")),
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
        )


@dataclass
class SceneRelation:
    """One directed edge in the scene graph, e.g.
    SceneRelation(subject_id="cup#1", predicate="LEFT_OF", object_id="laptop#1")."""
    subject_id: str
    predicate: str
    object_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {"subject_id": self.subject_id, "predicate": self.predicate, "object_id": self.object_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SceneRelation":
        return cls(subject_id=data["subject_id"], predicate=data["predicate"], object_id=data["object_id"])


@dataclass
class WorldState:
    """The current, best-known state of the environment - what
    `get_world_state()` returns. Always represents "right now" (within the
    short-term memory window), never history - history lives in `events`."""
    objects: Dict[str, TrackedObject] = field(default_factory=dict)
    humans: Dict[str, TrackedHuman] = field(default_factory=dict)
    room: RoomObservation = field(default_factory=RoomObservation)
    relations: List[SceneRelation] = field(default_factory=list)
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objects": {oid: o.to_dict() for oid, o in self.objects.items()},
            "humans": {hid: h.to_dict() for hid, h in self.humans.items()},
            "room": {
                "light_on": self.room.light_on,
                "door_closed": self.room.door_closed,
                "extra": self.room.extra,
            },
            "relations": [r.to_dict() for r in self.relations],
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorldState":
        room_data = data.get("room") or {}
        return cls(
            objects={oid: TrackedObject.from_dict(o) for oid, o in (data.get("objects") or {}).items()},
            humans={hid: TrackedHuman.from_dict(h) for hid, h in (data.get("humans") or {}).items()},
            room=RoomObservation(
                light_on=room_data.get("light_on"),
                door_closed=room_data.get("door_closed"),
                extra=room_data.get("extra") or {},
            ),
            relations=[SceneRelation.from_dict(r) for r in (data.get("relations") or [])],
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
        )


@dataclass
class EventRecord:
    """One stored, meaningful change - what `get_recent_events()` returns.
    Only events that pass `importance.should_store()` (score >= 3) ever
    become an `EventRecord` in the database."""
    id: Optional[int]
    timestamp: datetime
    category: EventCategory
    description: str
    importance: int
    related_object_id: Optional[str] = None
    related_human_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "category": self.category.value,
            "description": self.description,
            "importance": self.importance,
            "related_object_id": self.related_object_id,
            "related_human_id": self.related_human_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventRecord":
        return cls(
            id=data.get("id"),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            category=EventCategory(data["category"]),
            description=data["description"],
            importance=int(data["importance"]),
            related_object_id=data.get("related_object_id"),
            related_human_id=data.get("related_human_id"),
        )


@dataclass
class LongTermMemoryRecord:
    """A habit/pattern promoted from repeated `EventRecord`s - what
    `get_long_term_memory()` returns. See `memory.py`'s promotion logic for
    exactly when this gets created/updated."""
    id: Optional[int]
    statement: str
    confidence: float
    observation_count: int
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "confidence": round(self.confidence, 3),
            "observation_count": self.observation_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LongTermMemoryRecord":
        return cls(
            id=data.get("id"),
            statement=data["statement"],
            confidence=float(data["confidence"]),
            observation_count=int(data["observation_count"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
