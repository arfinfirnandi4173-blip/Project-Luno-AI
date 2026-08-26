"""
vision_intent.py
===================

Deterministic, rule-based vision-question classifier - "ada apa di
kamera" ("what's on the camera") means the user wants Luno to actually
look through the camera, not just chat. Same architecture as
`environment_intent.py`'s `classify_environmental_cue()`: regex/keyword
matching, not an LLM call - instant, free, fully deterministic, and easy
to unit test exhaustively (see `tests/test_vision_intent.py`).

Unlike `environment_intent.py`, there's no confirm-first two-turn state
machine here - a vision question is READ-ONLY (nothing gets turned
on/off, no side effect to double-check before acting on), so classifying
it and answering immediately (via `luno.vision.ask_vision()`) is safe.
`environment_intent.py`'s confirm-first design exists specifically
because acting on a wrong guess there means flipping a real device;
acting on a wrong guess here just means one wasted (but harmless) camera
+ Gemini round-trip.

Matching strategy, deliberately layered to avoid the false positives a
single broad rule would cause ("lihat status lampu"/"lihat jadwal"/
"lihat suhu" all contain "lihat" but have nothing to do with the
camera):

  1. An exact/substring WHITELIST of unambiguous full phrases - covers
     both phrasings that mention "kamera"/"camera" explicitly AND a
     small set of phrasings that are clearly vision questions even
     without the word ("aku pegang apa", "describe the room").
  2. A CO-OCCURRENCE rule: the word "kamera"/"camera" together with a
     look/question word ("lihat"/"liat"/"cek"/"apa"/"look"/"see"/"what")
     ANYWHERE in the same utterance - catches reasonable word-order
     variants ("di kamera ada apa", "kamera lihat apa") without having
     to enumerate every permutation by hand. Requiring BOTH words is
     what keeps this from matching things like "lihat status lampu" (no
     "kamera") or "geser kamera ke kanan"/PTZ commands (no look/question
     word) - see `tests/test_vision_intent.py`'s own regression guards
     for both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class VisionIntent:
    """`is_vision_request=False` means `question` is always `None` - the
    two fields are never independently meaningful, so callers can check
    just `is_vision_request` and trust `question` follows."""
    is_vision_request: bool
    question: Optional[str] = None


def _contains_word(text: str, phrase: str) -> bool:
    """Word-boundary match - same helper convention as
    `environment_intent.py`/`luno/planner/parser.py`'s own
    `_contains_word`."""
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _contains_any(text: str, phrases) -> bool:
    return any(_contains_word(text, p) for p in phrases)


# -- layer 1: exact/substring whitelist ---------------------------------------

_VISION_TRIGGER_PHRASES: Tuple[str, ...] = (
    # Indonesian - explicitly mentions the camera
    "ada apa di kamera", "apa yang ada di kamera", "apa yg ada di kamera",
    "di kamera ada apa",
    "apa yang kamu lihat", "apa yg kamu lihat", "kamu lihat apa",
    "coba lihat kamera", "coba liat kamera", "lihat kamera", "liat kamera",
    "lihat kamera dong", "liat kamera dong",
    "cek kamera", "kamera lihat apa", "kamera liat apa", "kamera ngeliat apa",
    # Indonesian - clearly a vision question without saying "kamera"
    "aku pegang apa", "ini aku pegang apa", "aku lagi pegang apa",
    "coba lihat ini", "lihat ini dong", "liat ini dong", "coba liat ini",
    "apa yang sedang aku lakukan", "apa yg sedang aku lakukan",
    "jelasin ruangan ini", "coba jelasin ruangan", "coba deskripsiin ruangan",
    # English
    "look at the camera", "look through the camera", "what's on the camera",
    "what is on the camera", "what do you see", "what can you see",
    "describe the room", "describe what you see", "what am i holding",
    "what am i wearing", "how do i look", "what am i doing",
)

# -- layer 2: co-occurrence (camera word + look/question word) ---------------

_CAMERA_WORDS: Tuple[str, ...] = ("kamera", "camera")
_LOOK_OR_QUESTION_WORDS: Tuple[str, ...] = (
    "apa", "what", "lihat", "liat", "cek", "look", "see",
)


def classify_vision_intent(text: str) -> VisionIntent:
    """Returns a `VisionIntent`. `text` is passed through unchanged as
    `question` on a match (verbatim, not the matched phrase) - callers
    (see `main_runtime_demo.py`'s `_handle_vision_intent`) hand this
    straight to `luno.vision.ask_vision()`, which already knows how to
    turn a raw utterance into a sensible camera prompt."""
    lowered = (text or "").lower().strip()
    if not lowered:
        return VisionIntent(is_vision_request=False)

    if any(phrase in lowered for phrase in _VISION_TRIGGER_PHRASES):
        return VisionIntent(is_vision_request=True, question=text.strip())

    if _contains_any(lowered, _CAMERA_WORDS) and _contains_any(lowered, _LOOK_OR_QUESTION_WORDS):
        return VisionIntent(is_vision_request=True, question=text.strip())

    return VisionIntent(is_vision_request=False)
