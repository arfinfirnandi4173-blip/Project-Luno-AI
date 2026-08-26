"""
test_vision_intent_classifier.py
====================================

`luno.vision_intent` - the standalone, rule-based vision-question
classifier (same architecture as `luno.environment_intent`'s
`classify_environmental_cue()` - see that module's own docstring for the
"deterministic, not LLM-based" rationale this one shares).

This file tests the module DIRECTLY (no `PlannerBridgeModule`/camera
config involved at all) - `tests/test_vision_intent.py` covers the
integration layer (`main_runtime_demo.py`'s `_classify_vision_intent`/
`_handle_vision_intent`, including the `CAMERA_VISION_ENABLED` gate and
the actual `ask_vision()` call) separately.

Run:
    python3 -m pytest tests/test_vision_intent_classifier.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.vision_intent import VisionIntent, classify_vision_intent  # noqa: E402


# ============================================================================
# positives - straight from the migration task's own example list
# ============================================================================

_POSITIVE_PHRASES = (
    "ada apa di kamera",
    "lihat kamera",
    "lihat kamera dong",
    "coba lihat kamera",
    "kamera lihat apa",
    "di kamera ada apa",
    "apa yang ada di kamera",
    "aku pegang apa",
    "coba lihat ini",
    "lihat ini dong",
    "apa yang sedang aku lakukan",
)


def test_task_example_phrases_are_all_classified_as_vision_requests():
    for phrase in _POSITIVE_PHRASES:
        intent = classify_vision_intent(phrase)
        assert intent.is_vision_request is True, f"expected a vision request for {phrase!r}"
        assert intent.question == phrase


def test_english_phrasings_are_classified_as_vision_requests():
    for phrase in (
        "what's on the camera", "look at the camera", "what am I holding",
        "describe the room", "what do you see",
    ):
        assert classify_vision_intent(phrase).is_vision_request is True


def test_matching_is_case_insensitive():
    assert classify_vision_intent("ADA APA DI KAMERA").is_vision_request is True
    assert classify_vision_intent("What's On The Camera").is_vision_request is True


def test_question_field_is_the_original_text_verbatim_not_lowercased():
    intent = classify_vision_intent("Ada Apa Di Kamera?")
    assert intent.is_vision_request is True
    assert intent.question == "Ada Apa Di Kamera?"


# ============================================================================
# negatives - straight from the migration task's own false-positive list
# ============================================================================

_FALSE_POSITIVE_PHRASES = (
    "lihat status lampu",
    "lihat jadwal",
    "lihat suhu",
    "lihat data listrik",
)


def test_task_false_positive_examples_never_trigger_vision():
    for phrase in _FALSE_POSITIVE_PHRASES:
        intent = classify_vision_intent(phrase)
        assert intent.is_vision_request is False, f"{phrase!r} must NOT be classified as a vision request"
        assert intent.question is None


def test_ordinary_conversation_does_not_trigger_vision():
    for phrase in ("bagaimana cuaca hari ini", "apa kabar", "aku lagi capek"):
        assert classify_vision_intent(phrase).is_vision_request is False


def test_empty_and_whitespace_text_never_matches():
    assert classify_vision_intent("").is_vision_request is False
    assert classify_vision_intent("   ").is_vision_request is False
    assert classify_vision_intent(None).is_vision_request is False  # defensive - never raises


def test_camera_word_alone_without_look_or_question_word_does_not_match():
    """Regression guard mirroring the PTZ parser's own
    `test_camera_word_alone_without_move_verb_falls_back_to_unknown` -
    "kamera" appearing in an unrelated sentence must not, by itself,
    trigger a vision check."""
    assert classify_vision_intent("kenapa kamera nggak nyala").is_vision_request is False
    assert classify_vision_intent("kameranya mahal ya").is_vision_request is False


# ============================================================================
# PTZ / camera-control commands must never be misclassified as vision
# questions - the two features are independent (disjoint verb vocabularies)
# ============================================================================

def test_camera_ptz_commands_never_classified_as_vision_requests():
    for phrase in (
        "geser kamera ke kanan",
        "putar kamera ke kiri",
        "tilt the camera up",
        "arahkan kamera ke bawah",
        "arahkan kamera ke pintu",
        "kalibrasi kamera",
        "center the camera",
        "simpan posisi kamera sebagai pintu",
        "save this position as door",
    ):
        assert classify_vision_intent(phrase).is_vision_request is False, (
            f"PTZ/preset command {phrase!r} must not be misclassified as a vision question"
        )


# ============================================================================
# dataclass shape
# ============================================================================

def test_vision_intent_is_a_frozen_dataclass_with_expected_fields():
    intent = VisionIntent(is_vision_request=True, question="ada apa di kamera")
    assert intent.is_vision_request is True
    assert intent.question == "ada apa di kamera"
    try:
        intent.is_vision_request = False  # type: ignore[misc]
        assert False, "VisionIntent should be frozen/immutable"
    except Exception:
        pass


def test_no_match_result_has_none_question():
    intent = classify_vision_intent("lihat jadwal")
    assert intent == VisionIntent(is_vision_request=False, question=None)
