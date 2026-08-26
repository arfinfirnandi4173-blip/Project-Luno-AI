"""
test_vision_provider.py
==========================

`luno.vision_provider` - the `VisionProvider` abstraction plus its two
implementations: `GeminiVisionProvider` (Gemini 2.0 Flash Vision API)
and `OpenAIVisionProvider` (OpenAI's vision-capable Chat Completions
API) - either replaces the old local MiniCPM-V/Ollama pipeline,
selected via `VISION_PROVIDER` in `.env` (default "openai" - see that
setting's own comment in config.py for why: GEMINI_API_KEY's free-tier
quota turned out to be shared with, and squeezed by, this project's
chat-LLM fallback priority also sometimes landing on Gemini).

Every test here uses a FAKE `requests`-shaped session object (`.post()`
returning a fake response with `.status_code`/`.json()`/`.text`) - never
a real network call, never a real API key. See `_FakeSession`/
`_FakeResponse` below.

Run:
    python3 -m pytest tests/test_vision_provider.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from luno.vision_provider import (  # noqa: E402
    GeminiVisionProvider,
    OpenAIVisionProvider,
    VisionProviderAPIError,
    VisionProviderEmptyImageError,
    VisionProviderError,
    VisionProviderMalformedResponseError,
    VisionProviderNetworkError,
    VisionProviderNotConfiguredError,
    VisionProviderTimeoutError,
)

_FAKE_KEY = "test-fake-gemini-key-never-real-123"
_FAKE_OPENAI_KEY = "test-fake-openai-key-never-real-456"
_FAKE_IMAGE = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", raise_on_json=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not valid json")
        return self._json_data


class _FakeSession:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _success_response(text="A white cup on the desk."):
    return _FakeResponse(status_code=200, json_data={
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
    })


def _provider(session=None, api_key=_FAKE_KEY, model="gemini-2.0-flash", timeout_s=5.0):
    return GeminiVisionProvider(api_key=api_key, model=model, timeout_s=timeout_s, session=session)


# ============================================================================
# missing API key / empty image - never even reach the network
# ============================================================================

def test_missing_api_key_raises_not_configured_without_a_network_call():
    session = _FakeSession(response=_success_response())
    provider = _provider(session=session, api_key="")
    try:
        provider.analyze_image(_FAKE_IMAGE, "what is this?")
        assert False, "expected VisionProviderNotConfiguredError"
    except VisionProviderNotConfiguredError:
        pass
    assert session.calls == []  # never even tried


def test_is_configured_reflects_api_key_presence():
    assert _provider(api_key="").is_configured() is False
    assert _provider(api_key=_FAKE_KEY).is_configured() is True


def test_empty_image_raises_without_a_network_call():
    session = _FakeSession(response=_success_response())
    provider = _provider(session=session)
    for empty in (b"", None):
        try:
            provider.analyze_image(empty, "what is this?")
            assert False, "expected VisionProviderEmptyImageError"
        except VisionProviderEmptyImageError:
            pass
    assert session.calls == []


# ============================================================================
# success path
# ============================================================================

def test_successful_response_returns_description_text():
    session = _FakeSession(response=_success_response("Ada kucing di atas meja."))
    provider = _provider(session=session)
    result = provider.analyze_image(_FAKE_IMAGE, "ada apa di kamera")
    assert result == "Ada kucing di atas meja."


def test_request_shape_carries_prompt_and_base64_image_key_in_header_not_url():
    session = _FakeSession(response=_success_response())
    provider = _provider(session=session, model="gemini-2.0-flash")
    provider.analyze_image(_FAKE_IMAGE, "what am I holding?")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert "gemini-2.0-flash:generateContent" in call["url"]
    assert _FAKE_KEY not in call["url"]  # never in the URL/query string
    assert call["headers"]["x-goog-api-key"] == _FAKE_KEY

    body = call["json"]
    parts = body["contents"][0]["parts"]
    assert any(p.get("text") == "what am I holding?" for p in parts)
    image_part = next(p for p in parts if "inline_data" in p)
    assert image_part["inline_data"]["mime_type"] == "image/jpeg"
    import base64
    assert base64.b64decode(image_part["inline_data"]["data"]) == _FAKE_IMAGE


# ============================================================================
# timeout / network failure
# ============================================================================

def test_timeout_raises_vision_provider_timeout_error():
    session = _FakeSession(raise_exc=requests.exceptions.Timeout("timed out"))
    provider = _provider(session=session, timeout_s=3.0)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderTimeoutError"
    except VisionProviderTimeoutError as ex:
        assert "3" in str(ex)  # mentions the configured timeout


def test_connection_error_raises_vision_provider_network_error():
    session = _FakeSession(raise_exc=requests.exceptions.ConnectionError("no route to host"))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderNetworkError"
    except VisionProviderNetworkError:
        pass


# ============================================================================
# API/HTTP failures
# ============================================================================

def test_rate_limit_429_raises_api_error_with_status_code():
    session = _FakeSession(response=_FakeResponse(status_code=429, json_data={"error": {"message": "quota exceeded"}}))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderAPIError"
    except VisionProviderAPIError as ex:
        assert ex.status_code == 429
        assert "quota exceeded" in str(ex) or "rate limit" in str(ex).lower()


def test_auth_error_401_mentions_api_key_check_but_not_the_key_itself():
    session = _FakeSession(response=_FakeResponse(status_code=401, json_data={"error": {"message": "API key invalid"}}))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderAPIError"
    except VisionProviderAPIError as ex:
        assert ex.status_code == 401
        assert _FAKE_KEY not in str(ex)  # never leaks the actual key


def test_server_error_500_raises_api_error():
    session = _FakeSession(response=_FakeResponse(status_code=500, text="internal error"))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderAPIError"
    except VisionProviderAPIError as ex:
        assert ex.status_code == 500


# ============================================================================
# malformed / empty responses
# ============================================================================

def test_non_json_response_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, raise_on_json=True))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_missing_candidates_key_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={"unexpected": "shape"}))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_empty_candidates_list_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={"candidates": []}))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_empty_answer_text_raises_malformed_response_error():
    """A well-formed response with genuinely no text (e.g. safety
    filtering blocked the answer) must be treated as a failure, not a
    silently-accepted empty description."""
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={
        "candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "SAFETY"}],
    }))
    provider = _provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError as ex:
        assert "SAFETY" in str(ex)


# ============================================================================
# API key never leaks
# ============================================================================

def test_api_key_never_appears_in_any_raised_exception_message():
    scenarios = [
        _FakeSession(response=_FakeResponse(status_code=401, json_data={"error": {"message": "bad key"}})),
        _FakeSession(response=_FakeResponse(status_code=500, text="server exploded")),
        _FakeSession(response=_FakeResponse(status_code=200, raise_on_json=True)),
        _FakeSession(raise_exc=requests.exceptions.Timeout()),
        _FakeSession(raise_exc=requests.exceptions.ConnectionError()),
    ]
    for session in scenarios:
        provider = _provider(session=session)
        try:
            provider.analyze_image(_FAKE_IMAGE, "q")
        except VisionProviderError as ex:
            assert _FAKE_KEY not in str(ex)


# ============================================================================
# OpenAIVisionProvider - same contract, mirrored test coverage
# ============================================================================

def _openai_success_response(text="A white cup on the desk."):
    return _FakeResponse(status_code=200, json_data={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    })


def _openai_provider(session=None, api_key=_FAKE_OPENAI_KEY, model="gpt-4o-mini", timeout_s=5.0):
    return OpenAIVisionProvider(api_key=api_key, model=model, timeout_s=timeout_s, session=session)


def test_openai_missing_api_key_raises_not_configured_without_a_network_call():
    session = _FakeSession(response=_openai_success_response())
    provider = _openai_provider(session=session, api_key="")
    try:
        provider.analyze_image(_FAKE_IMAGE, "what is this?")
        assert False, "expected VisionProviderNotConfiguredError"
    except VisionProviderNotConfiguredError:
        pass
    assert session.calls == []


def test_openai_is_configured_reflects_api_key_presence():
    assert _openai_provider(api_key="").is_configured() is False
    assert _openai_provider(api_key=_FAKE_OPENAI_KEY).is_configured() is True


def test_openai_empty_image_raises_without_a_network_call():
    session = _FakeSession(response=_openai_success_response())
    provider = _openai_provider(session=session)
    for empty in (b"", None):
        try:
            provider.analyze_image(empty, "what is this?")
            assert False, "expected VisionProviderEmptyImageError"
        except VisionProviderEmptyImageError:
            pass
    assert session.calls == []


def test_openai_successful_response_returns_description_text():
    session = _FakeSession(response=_openai_success_response("Ada kucing di atas meja."))
    provider = _openai_provider(session=session)
    result = provider.analyze_image(_FAKE_IMAGE, "ada apa di kamera")
    assert result == "Ada kucing di atas meja."


def test_openai_request_shape_carries_prompt_and_base64_image_in_data_url_key_in_header_not_url():
    session = _FakeSession(response=_openai_success_response())
    provider = _openai_provider(session=session, model="gpt-4o-mini")
    provider.analyze_image(_FAKE_IMAGE, "what am I holding?")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert _FAKE_OPENAI_KEY not in call["url"]  # never in the URL/query string
    assert call["headers"]["Authorization"] == f"Bearer {_FAKE_OPENAI_KEY}"

    body = call["json"]
    assert body["model"] == "gpt-4o-mini"
    content = body["messages"][0]["content"]
    text_part = next(p for p in content if p["type"] == "text")
    assert text_part["text"] == "what am I holding?"
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    import base64
    encoded = image_part["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == _FAKE_IMAGE


def test_openai_timeout_raises_vision_provider_timeout_error():
    session = _FakeSession(raise_exc=requests.exceptions.Timeout("timed out"))
    provider = _openai_provider(session=session, timeout_s=3.0)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderTimeoutError"
    except VisionProviderTimeoutError as ex:
        assert "3" in str(ex)


def test_openai_connection_error_raises_vision_provider_network_error():
    session = _FakeSession(raise_exc=requests.exceptions.ConnectionError("no route to host"))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderNetworkError"
    except VisionProviderNetworkError:
        pass


def test_openai_rate_limit_429_raises_api_error_with_status_code():
    session = _FakeSession(response=_FakeResponse(status_code=429, json_data={"error": {"message": "rate limit exceeded"}}))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderAPIError"
    except VisionProviderAPIError as ex:
        assert ex.status_code == 429


def test_openai_auth_error_401_never_leaks_the_key():
    session = _FakeSession(response=_FakeResponse(status_code=401, json_data={"error": {"message": "Incorrect API key provided"}}))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderAPIError"
    except VisionProviderAPIError as ex:
        assert ex.status_code == 401
        assert _FAKE_OPENAI_KEY not in str(ex)


def test_openai_non_json_response_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, raise_on_json=True))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_openai_missing_choices_key_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={"unexpected": "shape"}))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_openai_empty_choices_list_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={"choices": []}))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError:
        pass


def test_openai_empty_answer_text_raises_malformed_response_error():
    session = _FakeSession(response=_FakeResponse(status_code=200, json_data={
        "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
    }))
    provider = _openai_provider(session=session)
    try:
        provider.analyze_image(_FAKE_IMAGE, "q")
        assert False, "expected VisionProviderMalformedResponseError"
    except VisionProviderMalformedResponseError as ex:
        assert "content_filter" in str(ex)


def test_openai_api_key_never_appears_in_any_raised_exception_message():
    scenarios = [
        _FakeSession(response=_FakeResponse(status_code=401, json_data={"error": {"message": "bad key"}})),
        _FakeSession(response=_FakeResponse(status_code=500, text="server exploded")),
        _FakeSession(response=_FakeResponse(status_code=200, raise_on_json=True)),
        _FakeSession(raise_exc=requests.exceptions.Timeout()),
        _FakeSession(raise_exc=requests.exceptions.ConnectionError()),
    ]
    for session in scenarios:
        provider = _openai_provider(session=session)
        try:
            provider.analyze_image(_FAKE_IMAGE, "q")
        except VisionProviderError as ex:
            assert _FAKE_OPENAI_KEY not in str(ex)


# ============================================================================
# provider selection - luno.vision._get_vision_provider() honors
# VISION_PROVIDER
# ============================================================================

def test_get_vision_provider_selects_openai_by_default():
    import luno.vision as vision_module
    import luno.config as legacy_config

    original_setting = legacy_config.VISION_PROVIDER
    vision_module.set_vision_provider_for_testing(None)
    legacy_config.VISION_PROVIDER = "openai"
    try:
        provider = vision_module._get_vision_provider()
        assert isinstance(provider, OpenAIVisionProvider)
    finally:
        legacy_config.VISION_PROVIDER = original_setting
        vision_module.set_vision_provider_for_testing(None)


def test_get_vision_provider_selects_gemini_when_configured():
    import luno.vision as vision_module
    import luno.config as legacy_config

    original_setting = legacy_config.VISION_PROVIDER
    vision_module.set_vision_provider_for_testing(None)
    legacy_config.VISION_PROVIDER = "gemini"
    try:
        provider = vision_module._get_vision_provider()
        assert isinstance(provider, GeminiVisionProvider)
    finally:
        legacy_config.VISION_PROVIDER = original_setting
        vision_module.set_vision_provider_for_testing(None)


def test_get_vision_provider_falls_back_to_openai_on_unrecognized_value():
    import luno.vision as vision_module
    import luno.config as legacy_config

    original_setting = legacy_config.VISION_PROVIDER
    vision_module.set_vision_provider_for_testing(None)
    legacy_config.VISION_PROVIDER = "some-typo-value"
    try:
        provider = vision_module._get_vision_provider()
        assert isinstance(provider, OpenAIVisionProvider)
    finally:
        legacy_config.VISION_PROVIDER = original_setting
        vision_module.set_vision_provider_for_testing(None)


def test_get_vision_provider_is_a_singleton_within_one_selection():
    import luno.vision as vision_module
    import luno.config as legacy_config

    original_setting = legacy_config.VISION_PROVIDER
    vision_module.set_vision_provider_for_testing(None)
    legacy_config.VISION_PROVIDER = "openai"
    try:
        first = vision_module._get_vision_provider()
        second = vision_module._get_vision_provider()
        assert first is second
    finally:
        legacy_config.VISION_PROVIDER = original_setting
        vision_module.set_vision_provider_for_testing(None)
