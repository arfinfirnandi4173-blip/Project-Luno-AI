"""
utils.py
========

Small standalone helpers, plus `parse_description_heuristic()` - a basic,
keyword/regex-based default for turning a vision model's raw free-text
description (this project's current Gemini vision integration only
produces free text, no structured JSON - see `luno/vision.py`) into the structured
`SceneObservation` that `memory.VisionMemory.update()` actually consumes.

HONEST LIMITATION: this parser is a pragmatic default, not real NLU. It
matches a small fixed vocabulary of object labels, colors, activities, and
a couple of room-state phrases, and does a best-effort identity guess via a
capitalized-word pattern. It will miss things the description phrases
unusually and can misfire on the identity guess. The RECOMMENDED long-term
fix, once you're ready, is to have the vision model (or a cheap follow-up
LLM call) emit structured JSON directly instead of parsing prose - nothing
else in Vision Memory would need to change, since `SceneObservation` is the
only contract the rest of the module depends on. Pass `known_identity` when
the caller already knows who the user is (e.g. from `luno/persona.py`)
rather than relying on the guess.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable, List, Optional

from .models import HumanActivity, HumanObservation, ObjectObservation, RoomObservation, SceneObservation

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def ensure_aware(dt: datetime) -> datetime:
    """Every timestamp inside Vision Memory must be timezone-aware, because
    they get compared/subtracted against each other constantly (TTL checks,
    debounce windows, long-term-memory span checks) and mixing naive with
    aware datetimes raises `TypeError` at comparison time rather than
    silently doing the wrong thing. External input (e.g. a caller's ISO
    timestamp string with no offset, like the spec's own
    "2026-07-24T18:00:12" example) is exactly where a naive datetime is
    likely to sneak in, so every entry point that accepts a caller-supplied
    timestamp runs it through this first. Naive datetimes are assumed to
    already be UTC (not local time) - the safest assumption without a
    system-configured timezone to convert from."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Text similarity
# ---------------------------------------------------------------------------

def text_similarity(a: str, b: str) -> float:
    """0.0 (completely different) to 1.0 (identical) - used to recognize
    "only the wording changed" between two raw descriptions of what is
    otherwise the same scene, per the spec's change-detection rule."""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a or "", b or "").ratio()


# ---------------------------------------------------------------------------
# Heuristic scene parser
# ---------------------------------------------------------------------------

DEFAULT_OBJECT_LABELS = (
    "cup", "mug", "bottle", "laptop", "keyboard", "mouse", "monitor", "phone",
    "smartphone", "book", "notebook", "bag", "backpack", "glasses", "keys",
    "wallet", "headphones", "remote", "plate", "chair", "desk", "table", "lamp",
)

_COLORS = frozenset({
    "white", "black", "red", "blue", "green", "yellow", "gray", "grey",
    "brown", "silver", "pink", "purple", "orange",
})

_ACTIVITY_PATTERNS = [
    (HumanActivity.TYPING, re.compile(r"\btyping\b", re.IGNORECASE)),
    (HumanActivity.LOOKING_AT_MONITOR, re.compile(r"\blooking at (?:the )?(?:monitor|screen)\b", re.IGNORECASE)),
    (HumanActivity.SLEEPING, re.compile(r"\b(?:sleeping|asleep)\b", re.IGNORECASE)),
    (HumanActivity.READING, re.compile(r"\breading\b", re.IGNORECASE)),
    (HumanActivity.WALKING, re.compile(r"\bwalking\b", re.IGNORECASE)),
    (HumanActivity.STANDING, re.compile(r"\bstanding\b", re.IGNORECASE)),
    (HumanActivity.SITTING, re.compile(r"\bsitting\b", re.IGNORECASE)),
]

_EMOTION_WORDS = (
    "happy", "sad", "angry", "neutral", "focused", "tired", "excited",
    "frustrated", "calm", "bored", "surprised", "distressed", "scared",
)

_LIGHT_ON_RE = re.compile(r"\b(?:room )?light(?:s)? (?:is |are )?on\b", re.IGNORECASE)
_LIGHT_OFF_RE = re.compile(r"\b(?:room )?light(?:s)? (?:is |are )?off\b", re.IGNORECASE)
_DOOR_CLOSED_RE = re.compile(r"\bdoor (?:is )?closed\b", re.IGNORECASE)
_DOOR_OPEN_RE = re.compile(r"\bdoor (?:is )?open\b", re.IGNORECASE)

# Matches "<name> is/was <...>" to guess an identity - deliberately narrow
# (requires "is"/"was" right after the name) to cut down on false positives
# from sentence-starting words like "The"/"There"/"This".
_IDENTITY_RE = re.compile(r"\b([A-Z][a-z]{1,20})\s+(?:is|was)\b")
_IDENTITY_STOPWORDS = frozenset({"The", "There", "This", "That", "It", "A", "An", "Room", "Light"})


def parse_description_heuristic(
    description: str,
    timestamp: Optional[datetime] = None,
    known_identity: Optional[str] = None,
    object_labels: Iterable[str] = DEFAULT_OBJECT_LABELS,
) -> SceneObservation:
    """Best-effort `SceneObservation` from a raw description string. See
    module docstring for the honesty caveat - this is a default, not a
    guarantee of correct extraction."""
    text = description or ""
    ts = ensure_aware(timestamp) if timestamp is not None else utcnow()

    room = RoomObservation(
        light_on=True if _LIGHT_ON_RE.search(text) else (False if _LIGHT_OFF_RE.search(text) else None),
        door_closed=True if _DOOR_CLOSED_RE.search(text) else (False if _DOOR_OPEN_RE.search(text) else None),
    )

    objects = _extract_objects(text, object_labels)
    humans = _extract_humans(text, known_identity)

    return SceneObservation(timestamp=ts, raw_description=text, objects=objects, humans=humans, room=room)


def _extract_objects(text: str, object_labels: Iterable[str]) -> List[ObjectObservation]:
    results: List[ObjectObservation] = []
    for label in object_labels:
        pattern = re.compile(rf"\b(?:(\w+)\s+)?{re.escape(label)}\b([^.]*)", re.IGNORECASE)
        match = pattern.search(text)
        if not match:
            continue
        maybe_color = (match.group(1) or "").strip().lower()
        color = maybe_color if maybe_color in _COLORS else None
        location_tail = (match.group(2) or "").strip()
        # Strip a leading copula ("cup IS on the desk" -> "on the desk") so
        # the stored location phrase reads as a location, not a sentence
        # fragment - also makes phrasing more consistent across frames that
        # happen to word the same fact differently ("cup on the desk" vs
        # "cup is on the desk"), which helps tracker.py's location-based
        # matching and avoids spurious "moved" detections from wording
        # jitter alone.
        location_tail = re.sub(r"^(?:is|are|was|were)\s+", "", location_tail, flags=re.IGNORECASE)
        location = location_tail if location_tail else None
        results.append(ObjectObservation(label=label, color=color, location=location))
    return results


def _extract_humans(text: str, known_identity: Optional[str]) -> List[HumanObservation]:
    if not text.strip():
        return []

    identity = known_identity
    if identity is None:
        match = _IDENTITY_RE.search(text)
        if match and match.group(1) not in _IDENTITY_STOPWORDS:
            identity = match.group(1)

    # Only report a human at all if the text plausibly refers to one (an
    # identity was found/given, or a person-indicating word appears) -
    # otherwise an empty-room description would spuriously create a human.
    person_indicator = re.search(r"\b(?:person|user|someone|he|she|they)\b", text, re.IGNORECASE)
    if identity is None and not person_indicator:
        return []

    activity = HumanActivity.UNKNOWN
    for act, pattern in _ACTIVITY_PATTERNS:
        if pattern.search(text):
            activity = act
            break

    emotion = None
    lowered = text.lower()
    for word in _EMOTION_WORDS:
        if re.search(rf"\b{word}\b", lowered):
            emotion = word
            break

    return [HumanObservation(identity=identity, emotion=emotion, pose=None, activity=activity)]
