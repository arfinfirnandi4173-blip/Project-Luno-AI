"""
tracker.py
==========

Assigns and maintains PERSISTENT identity for objects and humans across
`VisionMemory.update()` calls - turning a bag of per-frame observations
(`ObjectObservation`/`HumanObservation`, no identity) into identity-bearing
state (`TrackedObject`/`TrackedHuman`, e.g. "cup#1") that stays the same
object across frames instead of a fresh id every time.

HONEST LIMITATION: matching is a label+color+location similarity heuristic,
NOT true visual re-identification - there is no embedding/bounding-box IoU
tracking here, because the upstream signal (a vision-language model's text
description, per this project's current Gemini vision integration) doesn't
carry that kind of structured detection data. Two visually-distinct objects
sharing a label and rough location can get merged into one id, and a single
object described inconsistently across frames (e.g. "white cup" then "mug")
can be seen as two different ones. This is called out explicitly rather than
overstating what a text-description-driven tracker can actually guarantee -
see `models.TrackedObject`'s docstring for the same note. If a future
vision source provides real bounding boxes/embeddings, this is the ONLY
file that would need to change to use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import (
    HumanActivity,
    HumanObservation,
    ObjectObservation,
    ObjectStatus,
    TrackedHuman,
    TrackedObject,
)


@dataclass
class ObjectTrackingResult:
    """Everything `event_detector.py` needs to turn one tracking pass into
    candidate events."""
    objects: Dict[str, TrackedObject] = field(default_factory=dict)
    appeared: List[TrackedObject] = field(default_factory=list)
    disappeared: List[TrackedObject] = field(default_factory=list)
    moved: List[TrackedObject] = field(default_factory=list)  # still present, location changed


@dataclass
class HumanTrackingResult:
    humans: Dict[str, TrackedHuman] = field(default_factory=dict)
    entered: List[TrackedHuman] = field(default_factory=list)
    left: List[TrackedHuman] = field(default_factory=list)
    # (human, previous_activity) for humans whose activity changed this frame
    activity_changed: List[Tuple[TrackedHuman, HumanActivity]] = field(default_factory=list)
    # (human, previous_emotion) for humans whose emotion changed this frame
    emotion_changed: List[Tuple[TrackedHuman, Optional[str]]] = field(default_factory=list)


class ObjectTracker:
    """Stateless matcher - all state (existing objects, id counters) is
    passed in and a fresh result is returned, so `memory.py` fully controls
    persistence/transactions instead of this class owning hidden state."""

    @staticmethod
    def update(
        previous: Dict[str, TrackedObject],
        observations: List[ObjectObservation],
        now: datetime,
        id_counters: Dict[str, int],
    ) -> ObjectTrackingResult:
        """
        Args:
            previous: current world state's objects (id -> TrackedObject),
                including ones already marked REMOVED (kept so a removed
                object's history/id isn't lost - only PRESENT ones are
                eligible to be matched against).
            observations: this frame's raw object observations.
            now: timestamp to stamp new/updated records with.
            id_counters: {label: next_free_number}, mutated in place so the
                caller can persist it (e.g. via `database.Database.
                set_metadata`) for stable ids across restarts.
        """
        result = ObjectTrackingResult()
        present_previous = {oid: o for oid, o in previous.items() if o.status == ObjectStatus.PRESENT}
        matched_ids: set[str] = set()

        # carry over anything not touched this frame (e.g. already-removed
        # history, or - see below - unmatched-but-still-present objects
        # before we decide they're gone) so callers always get the full set.
        result.objects = dict(previous)

        for obs in observations:
            match_id = ObjectTracker._find_match(obs, present_previous, matched_ids)
            if match_id is not None:
                matched_ids.add(match_id)
                existing = present_previous[match_id]
                moved = bool(obs.location) and bool(existing.location) and obs.location != existing.location
                updated = TrackedObject(
                    id=match_id,
                    label=obs.label,
                    color=obs.color or existing.color,
                    location=obs.location or existing.location,
                    status=ObjectStatus.PRESENT,
                    first_seen=existing.first_seen,
                    last_seen=now,
                )
                result.objects[match_id] = updated
                if moved:
                    result.moved.append(updated)
            else:
                new_id = ObjectTracker._assign_id(obs.label, id_counters)
                created = TrackedObject(
                    id=new_id, label=obs.label, color=obs.color, location=obs.location,
                    status=ObjectStatus.PRESENT, first_seen=now, last_seen=now,
                )
                result.objects[new_id] = created
                result.appeared.append(created)

        # anything that WAS present but wasn't matched this frame is gone
        for oid, obj in present_previous.items():
            if oid not in matched_ids:
                removed = TrackedObject(
                    id=obj.id, label=obj.label, color=obj.color, location=obj.location,
                    status=ObjectStatus.REMOVED, first_seen=obj.first_seen, last_seen=now,
                )
                result.objects[oid] = removed
                result.disappeared.append(removed)

        return result

    @staticmethod
    def _find_match(
        obs: ObjectObservation, present_previous: Dict[str, TrackedObject], already_matched: set
    ) -> Optional[str]:
        obs_label = obs.label.strip().lower()
        obs_color = (obs.color or "").strip().lower() or None
        best_id: Optional[str] = None
        best_score = 0
        for oid, existing in present_previous.items():
            if oid in already_matched:
                continue
            if existing.label.strip().lower() != obs_label:
                continue
            score = 1  # label match alone is already a candidate
            existing_color = (existing.color or "").strip().lower() or None
            if obs_color and existing_color:
                if obs_color == existing_color:
                    score += 2
                else:
                    score -= 2  # different reported color -> probably a different object
            if obs.location and existing.location and obs.location.strip().lower() == existing.location.strip().lower():
                score += 1
            if score > best_score:
                best_score = score
                best_id = oid
        return best_id if best_score > 0 else None

    @staticmethod
    def _assign_id(label: str, id_counters: Dict[str, int]) -> str:
        key = label.strip().lower().replace(" ", "_")
        next_n = id_counters.get(key, 0) + 1
        id_counters[key] = next_n
        return f"{key}#{next_n}"


class HumanTracker:
    """Same stateless-matcher pattern as `ObjectTracker`. Matching prefers an
    explicit `identity` string; when identity is unavailable (common - not
    every frame's description names the person), it falls back to
    positional continuity (a single previously-present human continues being
    that same human) rather than manufacturing spurious "left"/"entered"
    event pairs every time a name simply isn't mentioned in that frame's
    description."""

    @staticmethod
    def update(
        previous: Dict[str, TrackedHuman],
        observations: List[HumanObservation],
        now: datetime,
        id_counters: Dict[str, int],
    ) -> HumanTrackingResult:
        result = HumanTrackingResult()
        present_previous = {hid: h for hid, h in previous.items()}
        matched_ids: set[str] = set()
        result.humans = dict(previous)

        remaining_previous = dict(present_previous)

        for obs in observations:
            match_id = HumanTracker._find_match(obs, remaining_previous, matched_ids)
            if match_id is not None:
                matched_ids.add(match_id)
                existing = remaining_previous[match_id]
                updated = TrackedHuman(
                    id=match_id,
                    identity=obs.identity or existing.identity,
                    emotion=obs.emotion if obs.emotion is not None else existing.emotion,
                    pose=obs.pose if obs.pose is not None else existing.pose,
                    activity=obs.activity if obs.activity != HumanActivity.UNKNOWN else existing.activity,
                    first_seen=existing.first_seen,
                    last_seen=now,
                )
                result.humans[match_id] = updated
                if updated.activity != existing.activity:
                    result.activity_changed.append((updated, existing.activity))
                if updated.emotion != existing.emotion:
                    result.emotion_changed.append((updated, existing.emotion))
            else:
                new_id = HumanTracker._assign_id(obs.identity, id_counters)
                created = TrackedHuman(
                    id=new_id, identity=obs.identity, emotion=obs.emotion, pose=obs.pose,
                    activity=obs.activity, first_seen=now, last_seen=now,
                )
                result.humans[new_id] = created
                result.entered.append(created)

        for hid, human in present_previous.items():
            if hid not in matched_ids:
                result.left.append(human)
                # Left humans are removed from the ACTIVE world state (unlike
                # objects, there's no "removed but remembered" slot for a
                # human who's no longer in frame - see memory.py, which drops
                # them from world_state.humans but the `left` event captures
                # that they were here).
                result.humans.pop(hid, None)

        return result

    @staticmethod
    def _find_match(
        obs: HumanObservation, remaining_previous: Dict[str, TrackedHuman], already_matched: set
    ) -> Optional[str]:
        candidates = {hid: h for hid, h in remaining_previous.items() if hid not in already_matched}
        if not candidates:
            return None

        if obs.identity:
            obs_identity = obs.identity.strip().lower()
            for hid, h in candidates.items():
                if h.identity and h.identity.strip().lower() == obs_identity:
                    return hid
            # A named observation with NO matching named human: if there is
            # exactly one unnamed candidate, assume it's them (identity just
            # got confirmed this frame) rather than creating a duplicate.
            unnamed = [hid for hid, h in candidates.items() if not h.identity]
            if len(unnamed) == 1:
                return unnamed[0]
            return None

        # No identity reported this frame: if exactly one candidate remains,
        # assume positional continuity - the alternative (always treating an
        # unnamed observation as "unmatched") would spam human_left/
        # human_entered events on every frame that simply omits a name.
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    @staticmethod
    def _assign_id(identity: Optional[str], id_counters: Dict[str, int]) -> str:
        key = (identity or "user").strip().lower().replace(" ", "_")
        next_n = id_counters.get(key, 0) + 1
        id_counters[key] = next_n
        return f"{key}#{next_n}"
