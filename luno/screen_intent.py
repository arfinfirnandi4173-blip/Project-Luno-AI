"""
screen_intent.py
===================

Deterministic, rule-based SCREENSHOT-diagnosis classifier - "screenshot
terus liat kenapa ini error" means the user wants Luno to actually look
at the desktop screen, not just chat. Same architecture as
`vision_intent.py`'s `classify_vision_intent()` (regex/keyword matching,
not an LLM call - instant, free, fully deterministic, easy to unit test
exhaustively - see `tests/test_screen_intent.py`), and deliberately kept
as a SEPARATE module/vocabulary from it rather than folding "layar"
("screen") into `vision_intent.py`'s existing "kamera" ("camera")
matching: the two features answer genuinely different questions (what's
on the DESKTOP vs what's in front of the WEBCAM) and a user should be
able to enable/ask for one without accidentally triggering the other -
see `config.SCREEN_VISION_ENABLED` vs `config.CAMERA_VISION_ENABLED`'s
own independent on/off switches.

Scope note: this classifier - and the feature it drives
(`luno.screen_vision.ask_screen()`) - is DIAGNOSIS ONLY (Luno describes
what's on screen and suggests a fix), never autonomous desktop control
(no click/type-anywhere-on-any-app loop). That's a deliberate, much
larger and more security-sensitive scope that was explicitly NOT what
was asked for when this was built - see project history. If that ever
changes, it needs its own permission-gated design, not a silent
extension of this read-only classifier.

Matching strategy, same two-layer approach as `vision_intent.py` for the
same reason (avoid false positives a single broad rule would cause):

  1. An exact/substring WHITELIST of unambiguous full phrases.
  2. A CO-OCCURRENCE rule: a screen/screenshot word ("layar"/"screen"/
     "screenshot") together with a look/question word ("lihat"/"liat"/
     "cek"/"apa"/"look"/"see"/"what"/"check") ANYWHERE in the same
     utterance - catches reasonable word-order variants without
     enumerating every permutation by hand. Requiring BOTH words is what
     keeps this from matching unrelated sentences that merely contain
     "cek" or "lihat" alone (e.g. "cek suhu ruangan", "lihat jadwal
     hari ini" - neither mentions the screen at all).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ScreenIntent:
    """`is_screen_request=False` means `question` is always `None` -
    same "two fields never independently meaningful" convention as
    `vision_intent.VisionIntent`."""
    is_screen_request: bool
    question: Optional[str] = None


def _contains_word(text: str, phrase: str) -> bool:
    """Word-boundary match - same helper convention as
    `vision_intent.py`/`environment_intent.py`/`luno/planner/parser.py`'s
    own `_contains_word`."""
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _contains_any(text: str, phrases) -> bool:
    return any(_contains_word(text, p) for p in phrases)


# -- layer 1: exact/substring whitelist ---------------------------------------

_SCREEN_TRIGGER_PHRASES: Tuple[str, ...] = (
    # Indonesian - explicit screenshot request
    "screenshot", "ambil screenshot", "screenshot dong", "screenshot layar",
    "screenshot layar dong", "tolong screenshot", "coba screenshot",
    "capture layar", "capture screen",
    # Indonesian - "look at the screen" phrasings
    "liat layar", "lihat layar", "cek layar", "coba liat layar",
    "coba lihat layar", "liat layar dong", "lihat layar dong",
    "tolong liat layar", "tolong lihat layar", "tolong cek layar",
    "ada apa di layar", "apa yang ada di layar", "apa yg ada di layar",
    "di layar ada apa", "kenapa layar ini error", "layar ini kenapa error",
    "liat error di layar", "lihat error di layar", "cek error di layar",
    "liat masalah di layar", "lihat masalah di layar",
    # English
    "take a screenshot", "take screenshot", "capture the screen",
    "capture my screen", "look at my screen", "look at the screen",
    "check my screen", "check the screen", "what's on my screen",
    "what is on my screen", "what's on the screen", "see my screen",
    "see what's on my screen",
)

# -- layer 2: co-occurrence (screen word + look/question word) ---------------

_SCREEN_WORDS: Tuple[str, ...] = ("layar", "screen", "screenshot")
_LOOK_OR_QUESTION_WORDS: Tuple[str, ...] = (
    "apa", "what", "lihat", "liat", "cek", "look", "see", "check",
)


def classify_screen_intent(text: str) -> ScreenIntent:
    """Returns a `ScreenIntent`. `text` is passed through unchanged as
    `question` on a match (verbatim, not the matched phrase) - callers
    (see `main_runtime_demo.py`'s `_handle_screen_intent`) hand this
    straight to `luno.screen_vision.ask_screen()`, which already knows
    how to turn a raw utterance (or an empty one, which falls back to a
    sensible default diagnosis prompt) into a screenshot question."""
    lowered = (text or "").lower().strip()
    if not lowered:
        return ScreenIntent(is_screen_request=False)

    if any(phrase in lowered for phrase in _SCREEN_TRIGGER_PHRASES):
        return ScreenIntent(is_screen_request=True, question=text.strip())

    if _contains_any(lowered, _SCREEN_WORDS) and _contains_any(lowered, _LOOK_OR_QUESTION_WORDS):
        return ScreenIntent(is_screen_request=True, question=text.strip())

    return ScreenIntent(is_screen_request=False)
