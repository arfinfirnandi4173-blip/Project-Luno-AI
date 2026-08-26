"""
importance.py
==============

Rule-based scoring for candidate changes detected by `event_detector.py`.
Every candidate change gets a score on a fixed 1-5 scale:

    1 = Ignore     2 = Minor     3 = Useful     4 = Important     5 = Critical

Only changes scoring >= `IMPORTANCE_THRESHOLD` (3) are ever turned into a
stored `EventRecord` - this is the single mechanism that keeps Vision Memory
from flooding storage/the LLM with every trivial flicker.

This is intentionally simple, explicit, per-category base scores plus a
handful of context-driven adjustments - NOT a learned model. It's meant to
be easy to reason about and tune by hand (e.g. adding a label to
`DEFAULT_IMPORTANT_OBJECT_LABELS`), not a black box.
"""

from __future__ import annotations

from typing import Optional

from .models import EventCategory

IMPORTANCE_THRESHOLD = 3

# Labels that, when they appear/disappear, are bumped up a tier - things
# people actually care about losing track of. Passed as `important_labels`
# to `score_change`; override/extend per-deployment instead of editing this
# if you want a different default set.
DEFAULT_IMPORTANT_OBJECT_LABELS = frozenset({
    "keys", "key", "wallet", "phone", "smartphone", "laptop", "bag", "purse", "glasses",
})

# Emotions that bump an EMOTION_CHANGED event up a tier - ordinary neutral
# fluctuation isn't worth an event, a shift into/out of these is.
DEFAULT_NOTABLE_EMOTIONS = frozenset({
    "sad", "angry", "frustrated", "distressed", "crying", "excited", "very happy", "scared",
})

_BASE_SCORES = {
    EventCategory.HUMAN_ENTERED: 4,
    EventCategory.HUMAN_LEFT: 4,
    EventCategory.OBJECT_APPEARED: 3,
    EventCategory.OBJECT_DISAPPEARED: 3,
    EventCategory.OBJECT_MOVED: 2,
    EventCategory.ROOM_LIGHT_CHANGED: 3,
    EventCategory.ROOM_DOOR_CHANGED: 3,
    EventCategory.ACTIVITY_CHANGED: 3,
    EventCategory.EMOTION_CHANGED: 2,
    EventCategory.OTHER: 1,
}


def score_change(
    category: EventCategory,
    *,
    label: Optional[str] = None,
    emotion: Optional[str] = None,
    is_notable_transition: bool = False,
    important_labels: frozenset = DEFAULT_IMPORTANT_OBJECT_LABELS,
    notable_emotions: frozenset = DEFAULT_NOTABLE_EMOTIONS,
) -> int:
    """Score one candidate change on the 1-5 scale.

    Args:
        category: what kind of change this is.
        label: the object label involved, if this is an object event - used
            to bump OBJECT_APPEARED/DISAPPEARED/MOVED for "important" items.
        emotion: the new emotion, if this is an EMOTION_CHANGED event.
        is_notable_transition: caller-supplied flag for an ACTIVITY_CHANGED
            transition worth flagging even beyond the default (e.g. "started
            coding", "opened Unity" - `event_detector.py` decides what
            counts as notable, this function just applies the bump).
        important_labels / notable_emotions: override the default sets above
            per-call if a deployment wants different priorities.
    """
    score = _BASE_SCORES.get(category, 1)

    if category in (EventCategory.OBJECT_APPEARED, EventCategory.OBJECT_DISAPPEARED, EventCategory.OBJECT_MOVED):
        if label and label.strip().lower() in important_labels:
            score += 1

    if category == EventCategory.EMOTION_CHANGED:
        if emotion and emotion.strip().lower() in notable_emotions:
            score += 1

    if category == EventCategory.ACTIVITY_CHANGED and is_notable_transition:
        score += 1

    return max(1, min(5, score))


def should_store(score: int) -> bool:
    """Whether a change scoring `score` is worth persisting as an
    `EventRecord`. The single gate between "detected a change" and
    "remembered it"."""
    return score >= IMPORTANCE_THRESHOLD
