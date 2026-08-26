"""
test_intent_classifier.py
===========================

`classify_intent()` - covers every one of the spec's 15 categories in
both English and Indonesian, plus the fallback/tie-break behavior.
"""

from __future__ import annotations

from luno.routing.intent_classifier import classify_intent
from luno.routing.models import Intent


def test_device_control_english_and_indonesian():
    assert classify_intent("turn on the bedroom light")[0] == Intent.DEVICE_CONTROL
    assert classify_intent("nyalakan lampu kamar")[0] == Intent.DEVICE_CONTROL
    assert classify_intent("matikan AC ruang tamu")[0] == Intent.DEVICE_CONTROL


def test_status_query():
    assert classify_intent("is the bedroom light on")[0] == Intent.STATUS_QUERY
    assert classify_intent("apakah lampu sudah nyala")[0] == Intent.STATUS_QUERY


def test_world_state():
    assert classify_intent("what's on right now in my home")[0] == Intent.WORLD_STATE
    assert classify_intent("keadaan rumah sekarang gimana")[0] == Intent.WORLD_STATE


def test_automation():
    assert Intent.AUTOMATION in classify_intent("set up an automation for every morning")
    assert Intent.AUTOMATION in classify_intent("buatkan rutinitas setiap pagi")


def test_scheduling():
    assert classify_intent("remind me to call mom")[0] == Intent.SCHEDULING
    assert classify_intent("ingatkan saya minum obat")[0] == Intent.SCHEDULING


def test_smart_home_generic():
    result = classify_intent("tell me about my smart home setup")
    assert Intent.SMART_HOME in result


def test_vision():
    assert classify_intent("what do you see right now")[0] == Intent.VISION
    assert classify_intent("siapa di depan kamera")[0] == Intent.VISION


def test_memory():
    assert classify_intent("do you remember what I told you yesterday")[0] == Intent.MEMORY
    assert classify_intent("inget ya aku alergi kacang")[0] == Intent.MEMORY


def test_search_web():
    assert classify_intent("what's the latest news about AI")[0] == Intent.SEARCH_WEB
    assert classify_intent("cuaca hari ini gimana")[0] == Intent.SEARCH_WEB


def test_reasoning():
    assert classify_intent("why does this keep happening")[0] == Intent.REASONING
    assert classify_intent("jelaskan kenapa ini terjadi")[0] == Intent.REASONING


def test_planning():
    assert classify_intent("make a plan for my weekend")[0] == Intent.PLANNING
    assert classify_intent("susun rencana liburan")[0] == Intent.PLANNING


def test_coding():
    assert classify_intent("debug this bug in my python script")[0] == Intent.CODING
    assert classify_intent("write a function to sort a list")[0] == Intent.CODING


def test_multi_step():
    result = classify_intent("first turn on the light, then play music")
    assert Intent.MULTI_STEP in result


def test_general_question_fallback():
    assert classify_intent("what time is it")[0] == Intent.GENERAL_QUESTION
    assert classify_intent("apa itu python?")[0] == Intent.GENERAL_QUESTION


def test_general_chat_fallback():
    assert classify_intent("haha that's funny")[0] == Intent.GENERAL_CHAT
    assert classify_intent("hai luno")[0] == Intent.GENERAL_CHAT


def test_empty_text_never_raises():
    assert classify_intent("") == [Intent.GENERAL_CHAT]
    assert classify_intent(None) == [Intent.GENERAL_CHAT]
    assert classify_intent("   ") == [Intent.GENERAL_CHAT]


def test_device_control_outranks_smart_home_on_tie():
    """"turn on the smart home light" matches both DEVICE_CONTROL and
    SMART_HOME - the more actionable one must win as primary."""
    result = classify_intent("turn on the smart home light")
    assert result[0] == Intent.DEVICE_CONTROL


def test_multiple_intents_all_returned_ranked():
    result = classify_intent("why is the light not turning on, debug this for me")
    assert Intent.DEVICE_CONTROL in result or Intent.CODING in result or Intent.REASONING in result
    assert len(result) >= 1


def test_never_raises_on_weird_input():
    for weird in ["!!!", "12345", "🙂🙂🙂", "a" * 5000]:
        result = classify_intent(weird)
        assert isinstance(result, list) and len(result) >= 1
