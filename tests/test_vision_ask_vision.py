"""
test_vision_ask_vision.py
=============================

`luno.vision.ask_vision()` - the ONE stable, long-standing entry point
("Preserve the existing caller interface" - see the migration task this
was written for) that used to call local MiniCPM-V and now calls
`GeminiVisionProvider` through `_query_vision_provider()`/
`_get_vision_provider()`. These tests exercise `ask_vision()` itself
(not a mocked stand-in for it, unlike `tests/test_vision_intent.py`,
which monkeypatches `vision_module.ask_vision` wholesale to test the
CALLER side) - camera capture and the vision provider are both faked,
everything in between (`_encode_frame_for_upload`, `_query_vision_
provider`, the Vision Memory "where is X" shortcut) is real.

Also covers the deeper end-to-end slice of the conversation-integration
requirement: a user utterance -> vision intent classified -> the REAL
`ask_vision()` -> a FAKE camera frame + FAKE Gemini provider -> the
description actually reaching `NeedLLMResponse`'s `system_prompt`.

Run:
    python3 -m pytest tests/test_vision_ask_vision.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.vision as vision_module  # noqa: E402
from luno.vision_provider import VisionProviderError, VisionProviderTimeoutError  # noqa: E402


class _FakeProvider:
    """Structurally matches `VisionProvider` - `analyze_image(image,
    prompt) -> str`, or raises. Records every call for assertions."""

    def __init__(self, description=None, error=None):
        self._description = description
        self._error = error
        self.calls = []

    def analyze_image(self, image, prompt):
        self.calls.append({"image": image, "prompt": prompt})
        if self._error is not None:
            raise self._error
        return self._description


def _install_fake_camera(monkeypatch_frame):
    """Fakes `_capture_frame()` to return a real (small) OpenCV-shaped
    numpy frame - a real array, not a mock, so `_encode_frame_for_upload`
    (real `cv2.imencode`/`cv2.resize` calls) is genuinely exercised."""
    import numpy as np
    frame = np.zeros((480, 640, 3), dtype="uint8") if monkeypatch_frame is None else monkeypatch_frame
    vision_module._capture_frame = lambda: frame


#: Saved BEFORE any test in this file gets a chance to monkeypatch
#: `vision_module._capture_frame`/`detect_objects` - restored by
#: `teardown_function` after EVERY test (not just ones that "remember
#: to"), so a failure/assertion-error partway through one test can never
#: leave a REASSIGNED module-level function leaking into a completely
#: different test file's own tests later in the same pytest run (this
#: bit `tests/test_vision_sprint8.py`'s own camera-mocking tests once -
#: fixed by making restoration unconditional here, not opt-in per test).
_REAL_CAPTURE_FRAME = vision_module._capture_frame
_REAL_DETECT_OBJECTS = vision_module.detect_objects


def setup_function(_fn):
    """Fresh, isolated state before every test in this file - never let
    one test's fake provider/camera/YOLO stub leak into the next."""
    vision_module.set_vision_provider_for_testing(None)
    vision_module._capture_frame = _REAL_CAPTURE_FRAME
    vision_module.detect_objects = _REAL_DETECT_OBJECTS


def teardown_function(_fn):
    vision_module.set_vision_provider_for_testing(None)
    vision_module._capture_frame = _REAL_CAPTURE_FRAME
    vision_module.detect_objects = _REAL_DETECT_OBJECTS


# ============================================================================
# success path
# ============================================================================

def test_ask_vision_success_returns_description_from_provider():
    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: []
    provider = _FakeProvider(description="Ada kucing di atas meja.")
    vision_module.set_vision_provider_for_testing(provider)

    result = vision_module.ask_vision("ada apa di kamera")

    assert result == {"description": "Ada kucing di atas meja."}
    assert len(provider.calls) == 1
    assert "ada apa di kamera" in provider.calls[0]["prompt"]
    assert isinstance(provider.calls[0]["image"], (bytes, bytearray))
    assert len(provider.calls[0]["image"]) > 0


def test_ask_vision_appends_yolo_hint_objects_to_the_prompt():
    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: ["cup", "laptop"]
    provider = _FakeProvider(description="ok")
    vision_module.set_vision_provider_for_testing(provider)

    vision_module.ask_vision("what's on the desk?")

    prompt = provider.calls[0]["prompt"]
    assert "cup" in prompt and "laptop" in prompt


# ============================================================================
# failure paths - never raise, always {"error": ...}
# ============================================================================

def test_ask_vision_camera_unavailable_never_calls_the_provider():
    vision_module._capture_frame = lambda: None
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)

    result = vision_module.ask_vision("ada apa di kamera")

    assert "error" in result
    assert provider.calls == []


def test_ask_vision_provider_error_becomes_honest_error_dict():
    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: []
    provider = _FakeProvider(error=VisionProviderTimeoutError("Gemini didn't respond within 20s"))
    vision_module.set_vision_provider_for_testing(provider)

    result = vision_module.ask_vision("ada apa di kamera")

    assert "error" in result
    assert "20s" in result["error"]


def test_ask_vision_provider_unexpected_exception_never_crashes():
    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: []

    class _ExplodingProvider:
        def analyze_image(self, image, prompt):
            raise RuntimeError("something totally unexpected")

    vision_module.set_vision_provider_for_testing(_ExplodingProvider())
    result = vision_module.ask_vision("ada apa di kamera")
    assert "error" in result  # degraded honestly, did not raise out of ask_vision()


def test_ask_vision_empty_provider_response_is_treated_as_a_failure():
    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: []
    vision_module.set_vision_provider_for_testing(_FakeProvider(description=""))

    result = vision_module.ask_vision("ada apa di kamera")
    assert "error" in result


# ============================================================================
# Vision Memory "where is X" shortcut - Gemini/camera untouched
# ============================================================================

def test_where_is_question_answered_from_vision_memory_skips_the_camera_and_gemini():
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)
    vision_module._capture_frame = lambda: (_ for _ in ()).throw(AssertionError("camera should not be touched"))

    original_query = vision_module.vision_memory.query_location
    vision_module.vision_memory.query_location = lambda label: "on the desk" if label == "cup" else None
    try:
        result = vision_module.ask_vision("where is my cup?")
    finally:
        vision_module.vision_memory.query_location = original_query

    assert result == {"description": "The cup is on the desk."}
    assert provider.calls == []


# ============================================================================
# frame encoding - resize/compress before upload (never raw/huge frames)
# ============================================================================

def test_encode_frame_for_upload_downscales_large_frames():
    import cv2
    import numpy as np

    big_frame = np.zeros((2000, 3000, 3), dtype="uint8")
    encoded = vision_module._encode_frame_for_upload(big_frame)
    assert encoded is not None
    decoded = cv2.imdecode(np.frombuffer(encoded, dtype="uint8"), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) <= vision_module._MAX_UPLOAD_EDGE_PX


def test_encode_frame_for_upload_leaves_small_frames_unscaled():
    import cv2
    import numpy as np

    small_frame = np.zeros((240, 320, 3), dtype="uint8")
    encoded = vision_module._encode_frame_for_upload(small_frame)
    assert encoded is not None
    decoded = cv2.imdecode(np.frombuffer(encoded, dtype="uint8"), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (240, 320)


def test_encode_frame_for_upload_returns_none_for_empty_frame():
    assert vision_module._encode_frame_for_upload(None) is None


def test_query_vision_provider_reports_encode_failure_honestly():
    result = vision_module._query_vision_provider("q", None)
    assert result == {"error": "Gagal encode gambar dari kamera."}


# ============================================================================
# conversation integration - Gemini's result actually reaches the LLM
# ============================================================================

def test_end_to_end_gemini_description_reaches_need_llm_response_system_prompt():
    import time
    import main_runtime_demo as demo
    from luno.adapters import MockOpenRouterClient

    _install_fake_camera(None)
    vision_module.detect_objects = lambda frame=None: []
    provider = _FakeProvider(description="Ada seseorang duduk di depan komputer.")
    vision_module.set_vision_provider_for_testing(provider)

    original_is_configured = vision_module.is_configured
    vision_module.is_configured = lambda: True
    try:
        client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
        console = demo.RuntimeDemoConsole(openrouter_client=client)
        console.start()
        try:
            llm_events = []
            console.event_bus.subscribe("need_llm_response", lambda e: llm_events.append(e))

            console.event_bus.publish(demo.Event(type="user_utterance", data={
                "text": "ada apa di kamera", "request_id": "r1", "conversation_id": "conv-gemini-e2e",
            }))
            deadline = time.time() + 5
            while time.time() < deadline and not llm_events:
                time.sleep(0.02)

            assert len(llm_events) == 1
            system_prompt = llm_events[0].data.get("system_prompt") or ""
            assert "[Camera] Ada seseorang duduk di depan komputer." in system_prompt
            assert len(provider.calls) == 1
        finally:
            console.stop()
    finally:
        vision_module.is_configured = original_is_configured
