"""
scene_graph.py
==============

Builds lightweight spatial relations (`SceneRelation` edges, e.g.
`cup#1 LEFT_OF laptop#1`) between currently-present objects/humans from
their free-text `location` fields, and answers simple location queries
directly from the cached `WorldState` - e.g. "where is my cup" - WITHOUT
calling the vision model again, as long as nothing has changed since the
last observation (that's the entire point: the vision model was already
asked once, the answer is sitting in memory).

HONEST LIMITATION: relation extraction is regex/keyword matching over
whatever location phrase the upstream description happened to use (e.g.
"left of the laptop"), not real 3D spatial reasoning. If the description
never mentions a relation to another tracked entity, no edge is created for
that object - it just has no known relations (not an error, just no data).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Union

from .models import SceneRelation, TrackedHuman, TrackedObject, WorldState

# Ordered so a longer/more-specific phrase (e.g. "in front of") is tried
# before a shorter one that could accidentally match a substring of it.
_PREDICATE_PATTERNS: List[tuple] = [
    ("SITTING_ON", re.compile(r"\bsitting (?:on|at)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("IN_FRONT_OF", re.compile(r"\bin front of\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("ON_TOP_OF", re.compile(r"\bon top of\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("LEFT_OF", re.compile(r"\bleft (?:of|side of)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("RIGHT_OF", re.compile(r"\bright (?:of|side of)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("BEHIND", re.compile(r"\bbehind\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("UNDER", re.compile(r"\b(?:under|below)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("NEAR", re.compile(r"\b(?:near|next to|beside)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
    ("ON", re.compile(r"\bon\b\s+(?:the\s+)?(.+)", re.IGNORECASE)),
]


class SceneGraphBuilder:
    """Stateless - takes the current present objects/humans and returns a
    fresh list of relations every call. `memory.py` stores the result on
    `WorldState.relations`."""

    @staticmethod
    def build(
        objects: Dict[str, TrackedObject], humans: Dict[str, TrackedHuman]
    ) -> List[SceneRelation]:
        entities: Dict[str, Union[TrackedObject, TrackedHuman]] = {}
        for oid, obj in objects.items():
            if obj.status.value == "present":
                entities[oid] = obj
        entities.update(humans)  # humans dict only ever holds currently-present ones (see tracker.py)

        # label -> [ids] so a relation target phrase like "the laptop" can be
        # resolved back to a concrete tracked id.
        label_index: Dict[str, List[str]] = {}
        for eid, entity in entities.items():
            label = entity.label if isinstance(entity, TrackedObject) else "user"
            label_index.setdefault(label.strip().lower(), []).append(eid)

        relations: List[SceneRelation] = []
        for eid, entity in entities.items():
            location = getattr(entity, "location", None)
            if not location:
                continue
            predicate, target_phrase = SceneGraphBuilder._extract_predicate(location)
            if predicate is None:
                continue
            target_id = SceneGraphBuilder._resolve_target(target_phrase, label_index, exclude_id=eid)
            if target_id is not None:
                relations.append(SceneRelation(subject_id=eid, predicate=predicate, object_id=target_id))

        return relations

    @staticmethod
    def _extract_predicate(location_text: str) -> tuple:
        for predicate, pattern in _PREDICATE_PATTERNS:
            match = pattern.search(location_text)
            if match:
                return predicate, match.group(1).strip().rstrip(".").lower()
        return None, None

    @staticmethod
    def _resolve_target(
        target_phrase: Optional[str], label_index: Dict[str, List[str]], exclude_id: str
    ) -> Optional[str]:
        if not target_phrase:
            return None
        # Try an exact label match first, then fall back to "does the target
        # phrase CONTAIN a known label" (handles "the wooden desk" matching
        # a tracked "desk" label, etc.)
        candidates = label_index.get(target_phrase)
        if not candidates:
            for label, ids in label_index.items():
                if label in target_phrase or target_phrase in label:
                    candidates = ids
                    break
        if not candidates:
            return None
        for cid in candidates:
            if cid != exclude_id:
                return cid
        return None


def query_location(label_or_id: str, world_state: WorldState) -> Optional[str]:
    """Answer "where is my <label>" directly from cached state - returns the
    known `location` string of the first matching PRESENT object (matched by
    exact id, then by label), or None if nothing matching is currently
    tracked as present. Callers should treat None as "I don't currently know
    where that is" (either it was never observed, or it's been marked
    removed), not as an error."""
    key = label_or_id.strip().lower()

    direct = world_state.objects.get(label_or_id)
    if direct is not None and direct.status.value == "present":
        return direct.location

    for obj in world_state.objects.values():
        if obj.status.value == "present" and obj.label.strip().lower() == key:
            return obj.location

    return None


def query_relations_for(entity_id: str, world_state: WorldState) -> List[SceneRelation]:
    """All stored relations where `entity_id` is either the subject or the
    object - a quick "what do we know about this thing's position" lookup."""
    return [r for r in world_state.relations if r.subject_id == entity_id or r.object_id == entity_id]
