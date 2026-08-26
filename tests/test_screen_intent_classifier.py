"""
test_screen_intent_classifier.py
====================================

`luno.screen_intent` - the standalone, rule-based screenshot-diagnosis
classifier (same architecture/rationale as `luno.vision_intent`'s
`classify_vision_intent()` - see that module's own tests,
`tests/test_vision_intent_classifier.py`, for the sibling suite this one
deliberately mirrors).

This file tests the module DIRECTLY (no `PlannerBridgeModule`/
`SCREEN_VISION_ENABLED` config involved at all) -
`tests/test_screen_ask_screen.py` covers the integration layer
(`luno.screen_vision.ask_screen()`) separately.

Run:
    python3 -m pytest tests/test_screen_intent_classifier.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.screen_intent import ScreenIntent, classify_screen_intent  # noqa: E402


# ============================================================================
# positives
# ============================================================================

_POSITIVE_PHRASES = (
    "screenshot",
    "ambil screenshot",
    "screenshot dong",
    "screenshot layar",
    "liat layar",
    "lihat layar",
    "cek layar",
    "coba lihat layar",
    "ada apa di layar",
    "di layar ada apa",
    "kenapa layar ini error",
    "liat error di layar",
)


def test_task_example_phrases_are_all_classified_as_screen_requests():
    for phrase in _POSITIVE_PHRASES:
        intent = classify_screen_intent(phrase)
        assert intent.is_screen_request is True, f"expected a screen request for {phrase!r}"
        assert intent.question == phrase


def test_english_phrasings_are_classified_as_screen_requests():
    for phrase in (
        "take a screenshot", "capture my screen", "look at my screen",
        "what's on my screen", "check my screen", "see what's on my screen",
    ):
        assert classify_screen_intent(phrase).is_screen_request is True


def test_matching_is_case_insensitive():
    assert classify_screen_intent("SCREENSHOT DONG").is_screen_request is True
    assert classify_screen_intent("What's On My Screen").is_screen_request is True


def test_question_field_is_the_original_text_verbatim_not_lowercased():
    intent = classify_screen_intent("Screenshot Dong?")
    assert intent.is_screen_request is True
    assert intent.question == "Screenshot Dong?"


# ============================================================================
# negatives - conversation/other-feature phrases must never trigger this
# ============================================================================

_FALSE_POSITIVE_PHRASES = (
    "lihat status lampu",
    "lihat jadwal",
    "cek suhu ruangan",
    "cek harga bitcoin",
)


def test_unrelated_lihat_cek_phrases_never_trigger_screen_request():
    for phrase in _FALSE_POSITIVE_PHRASES:
        intent = classify_screen_intent(phrase)
        assert intent.is_screen_request is False, f"{phrase!r} must NOT be classified as a screen request"
        assert intent.question is None


def test_ordinary_conversation_does_not_trigger_screen_request():
    for phrase in ("bagaimana cuaca hari ini", "apa kabar", "aku lagi capek"):
        assert classify_screen_intent(phrase).is_screen_request is False


def test_empty_and_whitespace_text_never_matches():
    assert classify_screen_intent("").is_screen_request is False
    assert classify_screen_intent("   ").is_screen_request is False
    assert classify_screen_intent(None).is_screen_request is False  # defensive - never raises


def test_screen_word_alone_without_look_or_question_word_does_not_match():
    """Regression guard mirroring vision_intent's own
    `test_camera_word_alone_without_look_or_question_word_does_not_match`
    - "layar"/"screen" appearing in an unrelated sentence must not, by
    itself, trigger a screen check."""
    assert classify_screen_intent("layar laptopku pecah").is_screen_request is False
    assert classify_screen_intent("screen protector-nya kotor").is_screen_request is False


# ============================================================================
# camera (webcam) vision questions must never be misclassified as screen
# requests - the two features are independent, disjoint vocabularies
# ============================================================================

def test_camera_vision_questions_never_classified_as_screen_requests():
    for phrase in (
        "ada apa di kamera",
        "lihat kamera",
        "apa yang ada di kamera",
        "aku pegang apa",
        "what's on the camera",
        "describe the room",
    ):
        assert classify_screen_intent(phrase).is_screen_request is False, (
            f"camera question {phrase!r} must not be misclassified as a screen request"
        )


# ============================================================================
# dataclass shape
# ============================================================================

def test_screen_intent_is_a_frozen_dataclass_with_expected_fields():
    intent = ScreenIntent(is_screen_request=True, question="screenshot dong")
    assert intent.is_screen_request is True
    assert intent.question == "screenshot dong"
    try:
        intent.is_screen_request = False  # type: ignore[misc]
        assert False, "ScreenIntent should be frozen/immutable"
    except Exception:
        pass


def test_no_match_result_has_none_question():
    intent = classify_screen_intent("lihat jadwal")
    assert intent == ScreenIntent(is_screen_request=False, question=None)
