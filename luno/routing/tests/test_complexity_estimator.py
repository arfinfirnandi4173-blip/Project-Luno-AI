"""test_complexity_estimator.py - LOW/MEDIUM/HIGH/EXTREME thresholds."""

from __future__ import annotations

from luno.routing.complexity_estimator import estimate_complexity
from luno.routing.models import ComplexityLevel, Intent


def test_short_casual_message_is_low():
    level, score = estimate_complexity("hai", [Intent.GENERAL_CHAT])
    assert level == ComplexityLevel.LOW


def test_simple_device_command_is_low():
    level, _ = estimate_complexity("turn on the light", [Intent.DEVICE_CONTROL])
    assert level == ComplexityLevel.LOW


def test_reasoning_keyword_pushes_to_medium_or_higher():
    level, score = estimate_complexity("why is this happening", [Intent.REASONING])
    assert level in (ComplexityLevel.MEDIUM, ComplexityLevel.HIGH, ComplexityLevel.EXTREME)
    assert score > 0


def test_coding_plus_debug_reaches_high():
    level, _ = estimate_complexity(
        "debug this stack trace, explain the root cause and refactor the function",
        [Intent.CODING, Intent.REASONING],
    )
    assert level in (ComplexityLevel.HIGH, ComplexityLevel.EXTREME)


def test_extreme_signal_words_reach_extreme():
    level, _ = estimate_complexity(
        "design a system from scratch that handles the entire codebase, refactor everything end to end",
        [Intent.CODING, Intent.PLANNING],
    )
    assert level == ComplexityLevel.EXTREME


def test_multi_step_language_adds_signal():
    low_level, low_score = estimate_complexity("turn on the light", [Intent.DEVICE_CONTROL])
    multi_level, multi_score = estimate_complexity(
        "first turn on the light, then play music, then lock the door", [Intent.MULTI_STEP],
    )
    assert multi_score > low_score


def test_score_never_negative():
    _, score = estimate_complexity("ok", [Intent.GENERAL_CHAT])
    assert score >= 0.0


def test_empty_text_is_low():
    level, score = estimate_complexity("", [Intent.GENERAL_CHAT])
    assert level == ComplexityLevel.LOW


def test_never_raises_on_weird_input():
    for weird in [None, "", "a" * 3000, "😀" * 50]:
        level, score = estimate_complexity(weird, [Intent.GENERAL_CHAT])
        assert isinstance(score, float)
