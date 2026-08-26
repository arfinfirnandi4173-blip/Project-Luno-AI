"""
test_vision_intent.py
========================

`PlannerBridgeModule._classify_vision_intent()`/`_handle_vision_intent()`
(main_runtime_demo.py) - rule-based routing for camera/vision questions
("ada apa di kamera") to the real `luno.vision.ask_vision()` pipeline.

Background (the bug this closes): `luno/vision.py` already shipped a
working `ask_vision()` plus an OpenAI-style `VISION_TOOL` function
schema meant for live LLM function-calling, but the production
event-driven runtime (`main_runtime_demo.py`) never does live
function-calling at all - `NeedLLMResponse` carries no `tools` list.
So a vision question was reachable nowhere: `IntentParser` correctly
returned `tool="unknown"` for it (not a device command), and nothing
else ever queried the camera on the LLM's behalf. This gives vision
questions the same "classify first, inject pre-fetched context" treatment
web search/memory/environmental-intent already get.

Run:
    python3 -m pytest tests/test_vision_intent.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main_runtime_demo as demo  # noqa: E402
import luno.vision as vision_module  # noqa: E402


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


class _ConfiguredOn:
    """Context manager: force `vision_module.is_configured()` to return
    `True`/`False` for the duration of a test, restoring the original
    afterward - mirrors `test_vision_sprint8.py`'s own monkeypatch style
    (plain function reassignment, saved/restored in try/finally)."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._original = None

    def __enter__(self):
        self._original = vision_module.is_configured
        vision_module.is_configured = lambda: self._enabled
        return self

    def __exit__(self, *exc):
        vision_module.is_configured = self._original
        return False


# ============================================================================
# _classify_vision_intent - phrase matching
# ============================================================================

def test_matches_indonesian_ada_apa_di_kamera():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("ada apa di kamera") == "ada apa di kamera"


def test_matches_indonesian_lihat_kamera_variants():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("coba lihat kamera dong") is not None
        assert bridge._classify_vision_intent("liat kamera bentar") is not None
        assert bridge._classify_vision_intent("cek kamera dulu") is not None


def test_matches_indonesian_pegang_apa_without_camera_word():
    """Deliberately in the whitelist - clearly a vision question even
    without the word "kamera" in it."""
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("aku pegang apa ini") is not None
        assert bridge._classify_vision_intent("ini aku pegang apa") is not None


def test_matches_english_phrasings():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("what's on the camera") is not None
        assert bridge._classify_vision_intent("look at the camera") is not None
        assert bridge._classify_vision_intent("what am I holding") is not None
        assert bridge._classify_vision_intent("describe the room") is not None


def test_case_insensitive():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("ADA APA DI KAMERA") is not None
        assert bridge._classify_vision_intent("What's On The Camera") is not None


def test_ordinary_conversation_does_not_match():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("bagaimana cuaca hari ini") is None
        assert bridge._classify_vision_intent("apa kabar") is None
        assert bridge._classify_vision_intent("ini apa") is None  # deliberately too vague, not whitelisted


def test_camera_ptz_commands_never_false_positive_as_vision_questions():
    """Regression guard: PTZ move/preset commands must never accidentally
    also trigger a vision check - the two features are independent and
    use disjoint verb vocabularies ("geser"/"arahkan"/"putar" vs.
    "lihat"/"cek"/"ada apa")."""
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("geser kamera ke kanan") is None
        assert bridge._classify_vision_intent("arahkan kamera ke pintu") is None
        assert bridge._classify_vision_intent("putar kamera ke kiri") is None
        assert bridge._classify_vision_intent("simpan posisi kamera sebagai pintu") is None


def test_empty_text_does_not_match():
    bridge = _bridge()
    with _ConfiguredOn(True):
        assert bridge._classify_vision_intent("") is None
        assert bridge._classify_vision_intent("   ") is None


def test_camera_vision_disabled_never_matches_even_a_clear_phrase():
    """Master switch (`CAMERA_VISION_ENABLED` / `is_configured()`) - off
    means this classifier never even looks at the phrase list, matching
    every other vision.py entry point's own gating."""
    bridge = _bridge()
    with _ConfiguredOn(False):
        assert bridge._classify_vision_intent("ada apa di kamera") is None
        assert bridge._classify_vision_intent("what's on the camera") is None


# ============================================================================
# _handle_vision_intent - actually calling ask_vision()
# ============================================================================

def test_handle_vision_intent_success_returns_camera_note():
    bridge = _bridge()
    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: {"description": "A white cup on the desk."}
    try:
        with _ConfiguredOn(True):
            note = bridge._handle_vision_intent("ada apa di kamera", "r1")
    finally:
        vision_module.ask_vision = original
    assert note == "[Camera] A white cup on the desk."


def test_handle_vision_intent_passes_matched_text_as_question():
    bridge = _bridge()
    captured = {}

    def _fake_ask_vision(question):
        captured["question"] = question
        return {"description": "ok"}

    original = vision_module.ask_vision
    vision_module.ask_vision = _fake_ask_vision
    try:
        with _ConfiguredOn(True):
            bridge._handle_vision_intent("What am I holding right now?", "r1")
    finally:
        vision_module.ask_vision = original
    assert captured["question"] == "What am I holding right now?"


def test_handle_vision_intent_error_result_returns_honest_note():
    bridge = _bridge()
    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: {"error": "Kamera nggak bisa diakses."}
    try:
        with _ConfiguredOn(True):
            note = bridge._handle_vision_intent("cek kamera dulu", "r1")
    finally:
        vision_module.ask_vision = original
    assert note == "[Camera] Couldn't check the camera just now: Kamera nggak bisa diakses."


def test_handle_vision_intent_exception_degrades_to_none_never_raises():
    bridge = _bridge()

    def _raises(question):
        raise RuntimeError("Ollama connection refused")

    original = vision_module.ask_vision
    vision_module.ask_vision = _raises
    try:
        with _ConfiguredOn(True):
            note = bridge._handle_vision_intent("ada apa di kamera", "r1")
    finally:
        vision_module.ask_vision = original
    assert note is None


def test_handle_vision_intent_non_matching_text_never_calls_ask_vision():
    bridge = _bridge()
    calls = []
    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: calls.append(question) or {"description": "should not happen"}
    try:
        with _ConfiguredOn(True):
            note = bridge._handle_vision_intent("bagaimana cuaca hari ini", "r1")
    finally:
        vision_module.ask_vision = original
    assert note is None
    assert calls == []


def test_handle_vision_intent_disabled_never_calls_ask_vision():
    bridge = _bridge()
    calls = []
    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: calls.append(question) or {"description": "should not happen"}
    try:
        with _ConfiguredOn(False):
            note = bridge._handle_vision_intent("ada apa di kamera", "r1")
    finally:
        vision_module.ask_vision = original
    assert note is None
    assert calls == []


# ============================================================================
# end-to-end through the real event bus - the note actually reaches
# NeedLLMResponse's system_prompt
# ============================================================================

def test_end_to_end_vision_note_reaches_need_llm_response():
    import time
    from luno.adapters import MockOpenRouterClient

    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: {"description": "Ada kucing di atas meja."}
    try:
        with _ConfiguredOn(True):
            client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
            console = demo.RuntimeDemoConsole(openrouter_client=client)
            console.start()
            try:
                llm_events = []
                console.event_bus.subscribe("need_llm_response", lambda e: llm_events.append(e))

                console.event_bus.publish(demo.Event(type="user_utterance", data={
                    "text": "ada apa di kamera", "request_id": "r1", "conversation_id": "conv-vision",
                }))
                deadline = time.time() + 5
                while time.time() < deadline and not llm_events:
                    time.sleep(0.02)

                assert len(llm_events) == 1
                system_prompt = llm_events[0].data.get("system_prompt") or ""
                assert "[Camera] Ada kucing di atas meja." in system_prompt
            finally:
                console.stop()
    finally:
        vision_module.ask_vision = original


def test_end_to_end_ordinary_chat_never_gets_a_camera_note():
    import time
    from luno.adapters import MockOpenRouterClient

    calls = []
    original = vision_module.ask_vision
    vision_module.ask_vision = lambda question: calls.append(question) or {"description": "should not happen"}
    try:
        with _ConfiguredOn(True):
            client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
            console = demo.RuntimeDemoConsole(openrouter_client=client)
            console.start()
            try:
                llm_events = []
                console.event_bus.subscribe("need_llm_response", lambda e: llm_events.append(e))

                console.event_bus.publish(demo.Event(type="user_utterance", data={
                    "text": "apa kabar", "request_id": "r1", "conversation_id": "conv-vision-2",
                }))
                deadline = time.time() + 5
                while time.time() < deadline and not llm_events:
                    time.sleep(0.02)

                assert len(llm_events) == 1
                system_prompt = llm_events[0].data.get("system_prompt") or ""
                assert "[Camera]" not in system_prompt
                assert calls == []
            finally:
                console.stop()
    finally:
        vision_module.ask_vision = original
