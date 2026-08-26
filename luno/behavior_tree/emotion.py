"""
emotion.py
==========

Estimates Luno's read of the current emotional context - the spec's
"Emotion Behavior": "Estimate emotional state using: Conversation, Voice,
Vision, Context." The result (`Blackboard.emotion`) is read by other
modules to adjust HOW they act (speech style, facial animation, idle
animation, response timing) - this module only produces the estimate, it
does not itself change speech/animation.

HONEST LIMITATION: this is a small rule-based heuristic over whatever
signals are already sitting on the Blackboard (vision-detected human
emotion/activity, conversation error streaks, time of day), not a real
affect-recognition model. "Voice" tone specifically isn't analyzed here at
all yet - `EmotionSignals.conversation_sentiment` is a slot for a future
upstream signal (e.g. a cheap sentiment pass over the transcript, or
prosody analysis) to plug into without changing anything downstream, same
"swap the input, keep the contract" pattern used by
`vision_memory.utils.parse_description_heuristic`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .blackboard import Blackboard


class Emotion(str, Enum):
    HAPPY = "happy"
    NEUTRAL = "neutral"
    FOCUSED = "focused"
    TIRED = "tired"
    CONFUSED = "confused"
    EXCITED = "excited"
    CONCERNED = "concerned"


@dataclass
class EmotionSignals:
    """Explicit input to `EmotionEstimator.estimate()` - kept as its own
    dataclass (rather than passing the whole Blackboard in) so the
    estimation RULES are testable in isolation from how the signals get
    gathered."""
    conversation_sentiment: Optional[str] = None   # "positive" | "negative" | "neutral" | None
    user_vision_emotion: Optional[str] = None       # from vision_memory TrackedHuman.emotion
    user_activity: Optional[str] = None              # from Blackboard.user.activity
    consecutive_errors: int = 0
    conversation_ongoing: bool = False
    hour_of_day: int = 12


_NEGATIVE_VISION_EMOTIONS = frozenset({"sad", "angry", "frustrated", "distressed", "scared", "surprised"})
_TIRED_VISION_EMOTIONS = frozenset({"tired", "sleepy", "bored"})
_POSITIVE_VISION_EMOTIONS = frozenset({"happy", "excited", "calm"})
_FOCUSED_ACTIVITIES = frozenset({"typing", "coding", "reading", "looking_at_monitor"})


class EmotionEstimator:
    """Stateless - `estimate()` is a pure function of `EmotionSignals`, so
    it's trivially unit-testable without a live Blackboard."""

    @staticmethod
    def estimate(signals: EmotionSignals) -> Tuple[Emotion, float]:
        """Returns (Emotion, confidence 0.0-1.0). Rules are checked in
        order of how strong/unambiguous the signal is - first match wins,
        same "ordered pattern list" approach as
        `luno/expressions.py:guess_expression()` and
        `vision_memory.utils.parse_description_heuristic`."""

        # Repeated system errors matter more than anything else here - a
        # confused/broken Luno should sound cautious, not chipper.
        if signals.consecutive_errors >= 2:
            return Emotion.CONCERNED, 0.7

        if signals.user_vision_emotion in _NEGATIVE_VISION_EMOTIONS:
            return Emotion.CONCERNED, 0.6

        if signals.user_vision_emotion in _TIRED_VISION_EMOTIONS or signals.user_activity == "sleeping":
            return Emotion.TIRED, 0.6

        if signals.conversation_sentiment == "negative":
            return Emotion.CONCERNED, 0.55

        if signals.conversation_sentiment == "positive" or signals.user_vision_emotion in _POSITIVE_VISION_EMOTIONS:
            return Emotion.HAPPY, 0.55

        if signals.user_activity in _FOCUSED_ACTIVITIES:
            return Emotion.FOCUSED, 0.5

        if signals.conversation_ongoing:
            return Emotion.EXCITED, 0.4  # mild default lift while actively chatting

        # Very late/very early hours with nothing else going on - low
        # confidence, just a gentle nudge toward a quieter demeanor.
        if signals.hour_of_day >= 23 or signals.hour_of_day < 5:
            return Emotion.TIRED, 0.3

        return Emotion.NEUTRAL, 0.3

    @staticmethod
    def from_blackboard(bb: Blackboard) -> Emotion:
        """Gathers `EmotionSignals` off `bb`, estimates, writes the result
        to `bb.emotion`, and returns it. Called once per tick by
        `behavior_tree.py` - see its module docstring."""
        signals = EmotionSignals(
            conversation_sentiment=None,  # no sentiment source wired up yet - see HONEST LIMITATION above
            user_vision_emotion=bb.user.emotion,
            user_activity=bb.user.activity,
            consecutive_errors=bb.system.consecutive_errors,
            conversation_ongoing=bb.conversation.ongoing,
            hour_of_day=bb.now.hour,
        )
        emotion, _confidence = EmotionEstimator.estimate(signals)
        bb.emotion = emotion.value
        return emotion
