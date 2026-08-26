"""
test_screen_ask_screen.py
=============================

`luno.screen_vision.ask_screen()` - screenshot-diagnosis feature. These
tests exercise `ask_screen()` itself (not a mocked stand-in for it) -
screen capture is faked via the injectable `grab_fn` param (see
`capture_screen()`'s own docstring for why that seam exists - this
sandbox/CI environment has no real display), the vision provider is
faked via `luno.vision.set_vision_provider_for_testing()` (the SAME
singleton `luno.vision.ask_vision()` uses - `screen_vision.py` reuses it
on purpose, see that module's docstring), everything in between
(`_encode_screenshot_for_upload`, the real Pillow resize/JPEG-encode) is
real.

Run:
    python3 -m pytest tests/test_screen_ask_screen.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PIL import Image  # noqa: E402

import luno.config as config  # noqa: E402
import luno.screen_vision as screen_vision_module  # noqa: E402
import luno.vision as vision_module  # noqa: E402
from luno.vision_provider import VisionProviderTimeoutError  # noqa: E402


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


def _fake_screenshot(size=(640, 480), color=(10, 20, 30)):
    return Image.new("RGB", size, color)


#: Saved before any test monkeypatches config - restored unconditionally
#: after every test, same "never let one test's stub leak into the next"
#: discipline `tests/test_vision_ask_vision.py` established for this
#: project (bit that file's own tests once via opt-in-only restoration).
_REAL_SCREEN_VISION_ENABLED = config.SCREEN_VISION_ENABLED


def setup_function(_fn):
    vision_module.set_vision_provider_for_testing(None)
    config.SCREEN_VISION_ENABLED = True


def teardown_function(_fn):
    vision_module.set_vision_provider_for_testing(None)
    config.SCREEN_VISION_ENABLED = _REAL_SCREEN_VISION_ENABLED


# ============================================================================
# master switch
# ============================================================================

def test_ask_screen_returns_error_without_ever_capturing_when_feature_disabled():
    config.SCREEN_VISION_ENABLED = False
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)
    grabbed = []

    result = screen_vision_module.ask_screen("kenapa error", grab_fn=lambda: grabbed.append(1) or _fake_screenshot())

    assert "error" in result
    assert grabbed == []
    assert provider.calls == []


def test_is_configured_reflects_the_config_flag():
    config.SCREEN_VISION_ENABLED = True
    assert screen_vision_module.is_configured() is True
    config.SCREEN_VISION_ENABLED = False
    assert screen_vision_module.is_configured() is False


# ============================================================================
# success path
# ============================================================================

def test_ask_screen_success_returns_description_from_provider():
    provider = _FakeProvider(description="Ada dialog error 'Access Denied' di layar.")
    vision_module.set_vision_provider_for_testing(provider)

    result = screen_vision_module.ask_screen("kenapa ini error", grab_fn=lambda: _fake_screenshot())

    assert result == {"description": "Ada dialog error 'Access Denied' di layar."}
    assert len(provider.calls) == 1
    assert "kenapa ini error" in provider.calls[0]["prompt"]
    assert isinstance(provider.calls[0]["image"], (bytes, bytearray))
    assert len(provider.calls[0]["image"]) > 0


def test_ask_screen_empty_question_falls_back_to_default_diagnosis_prompt():
    provider = _FakeProvider(description="Layarnya keliatan normal.")
    vision_module.set_vision_provider_for_testing(provider)

    result = screen_vision_module.ask_screen("", grab_fn=lambda: _fake_screenshot())

    assert result == {"description": "Layarnya keliatan normal."}
    prompt = provider.calls[0]["prompt"]
    assert "screenshot" in prompt.lower()


# ============================================================================
# failure paths - never raise, always {"error": ...}
# ============================================================================

def test_ask_screen_capture_failure_never_calls_the_provider():
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)

    result = screen_vision_module.ask_screen("kenapa error", grab_fn=lambda: None)

    assert "error" in result
    assert provider.calls == []


def test_ask_screen_grab_fn_raising_is_caught_and_reported_honestly():
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)

    def _exploding_grab():
        raise RuntimeError("no display available")

    result = screen_vision_module.ask_screen("kenapa error", grab_fn=_exploding_grab)

    assert "error" in result
    assert provider.calls == []


def test_ask_screen_provider_error_becomes_honest_error_dict():
    provider = _FakeProvider(error=VisionProviderTimeoutError("Gemini didn't respond within 20s"))
    vision_module.set_vision_provider_for_testing(provider)

    result = screen_vision_module.ask_screen("kenapa error", grab_fn=lambda: _fake_screenshot())

    assert "error" in result
    assert "20s" in result["error"]


def test_ask_screen_provider_unexpected_exception_never_crashes():
    class _ExplodingProvider:
        def analyze_image(self, image, prompt):
            raise RuntimeError("something totally unexpected")

    vision_module.set_vision_provider_for_testing(_ExplodingProvider())
    result = screen_vision_module.ask_screen("kenapa error", grab_fn=lambda: _fake_screenshot())
    assert "error" in result  # degraded honestly, did not raise out of ask_screen()


def test_ask_screen_empty_provider_response_is_treated_as_a_failure():
    vision_module.set_vision_provider_for_testing(_FakeProvider(description=""))
    result = screen_vision_module.ask_screen("kenapa error", grab_fn=lambda: _fake_screenshot())
    assert "error" in result


# ============================================================================
# capture_screen() - injectable seam
# ============================================================================

def test_capture_screen_returns_grab_fn_result():
    img = _fake_screenshot()
    assert screen_vision_module.capture_screen(grab_fn=lambda: img) is img


def test_capture_screen_returns_none_when_grab_fn_raises():
    def _boom():
        raise RuntimeError("no display")
    assert screen_vision_module.capture_screen(grab_fn=_boom) is None


# ============================================================================
# screenshot encoding - resize/compress before upload (never raw/huge images)
# ============================================================================

def test_encode_screenshot_for_upload_downscales_large_images():
    big = _fake_screenshot(size=(3840, 2160))
    encoded = screen_vision_module._encode_screenshot_for_upload(big)
    assert encoded is not None
    decoded = Image.open(__import__("io").BytesIO(encoded))
    assert max(decoded.size) <= config.SCREEN_VISION_MAX_EDGE


def test_encode_screenshot_for_upload_leaves_small_images_unscaled():
    small = _fake_screenshot(size=(320, 240))
    encoded = screen_vision_module._encode_screenshot_for_upload(small)
    assert encoded is not None
    decoded = Image.open(__import__("io").BytesIO(encoded))
    assert decoded.size == (320, 240)


def test_encode_screenshot_for_upload_returns_none_for_missing_image():
    assert screen_vision_module._encode_screenshot_for_upload(None) is None


# ============================================================================
# conversation integration - screen diagnosis actually reaches the LLM
# ============================================================================

def test_end_to_end_screen_description_reaches_need_llm_response_system_prompt():
    import time
    import main_runtime_demo as demo
    from luno.adapters import MockOpenRouterClient

    provider = _FakeProvider(description="Ada popup error 'Connection failed' di pojok kanan bawah.")
    vision_module.set_vision_provider_for_testing(provider)

    original_grab = screen_vision_module.capture_screen
    screen_vision_module.capture_screen = lambda grab_fn=None: _fake_screenshot()
    try:
        client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
        console = demo.RuntimeDemoConsole(openrouter_client=client)
        console.start()
        try:
            llm_events = []
            console.event_bus.subscribe("need_llm_response", lambda e: llm_events.append(e))

            console.event_bus.publish(demo.Event(type="user_utterance", data={
                "text": "screenshot terus liat kenapa error", "request_id": "r1",
                "conversation_id": "conv-screen-e2e",
            }))
            deadline = time.time() + 5
            while time.time() < deadline and not llm_events:
                time.sleep(0.02)

            assert len(llm_events) == 1
            system_prompt = llm_events[0].data.get("system_prompt") or ""
            assert "[Screen] Ada popup error 'Connection failed' di pojok kanan bawah." in system_prompt
            assert len(provider.calls) == 1
        finally:
            console.stop()
    finally:
        screen_vision_module.capture_screen = original_grab


def test_end_to_end_skipped_entirely_when_screen_vision_disabled():
    import time
    import main_runtime_demo as demo
    from luno.adapters import MockOpenRouterClient

    config.SCREEN_VISION_ENABLED = False
    provider = _FakeProvider(description="should never be called")
    vision_module.set_vision_provider_for_testing(provider)

    client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
    console = demo.RuntimeDemoConsole(openrouter_client=client)
    console.start()
    try:
        llm_events = []
        console.event_bus.subscribe("need_llm_response", lambda e: llm_events.append(e))

        console.event_bus.publish(demo.Event(type="user_utterance", data={
            "text": "screenshot terus liat kenapa error", "request_id": "r2",
            "conversation_id": "conv-screen-disabled",
        }))
        deadline = time.time() + 5
        while time.time() < deadline and not llm_events:
            time.sleep(0.02)

        assert len(llm_events) == 1
        system_prompt = llm_events[0].data.get("system_prompt") or ""
        assert "[Screen]" not in system_prompt
        assert provider.calls == []
    finally:
        console.stop()
