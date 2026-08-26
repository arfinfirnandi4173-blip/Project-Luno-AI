"""
test_providers.py
==================

Per-provider tests for every `LLMProviderClient` implementation - the
sprint's own checklist items:

    (OpenRouter) (OpenAI) (Gemini) (Anthropic) (Local API)
    (Streaming) (Non-streaming) (Authentication failure) (Timeout)
    (Rate limit) (Health monitoring)

No real network access anywhere - every HTTP call goes through a fake
`requests.Session`-like double (`FakeSession`/`FakeResponse`, same
pattern `luno/adapters/tests/test_openrouter_adapter.py` already
established for `RequestsOpenRouterClient`) that returns scripted
status codes/JSON bodies/SSE lines, so this proves the REAL request-
building/response-parsing/error-classification logic in
`base.OpenAICompatibleClient` and each of `gemini_provider.py`/
`anthropic_provider.py`'s own from-scratch implementations - not just
that the already-abstracted `MockProviderClient` works (that one has
its own, separate test coverage via `luno/adapters/tests/
test_llm_manager.py`).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import pytest

from luno.adapters.llm.anthropic_provider import AnthropicProvider
from luno.adapters.llm.errors import (
    ProviderAuthError,
    ProviderNetworkError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from luno.adapters.llm.gemini_provider import GeminiProvider
from luno.adapters.llm.local_provider import LocalProvider
from luno.adapters.llm.models import ProviderConfig
from luno.adapters.llm.openai_provider import OpenAIProvider
from luno.adapters.llm.openrouter_provider import OpenRouterProvider


# ============================================================================
# Fakes
# ============================================================================

class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Optional[dict] = None, lines: Optional[List[str]] = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self._lines = lines or []
        self.closed = False

    def json(self):
        return self._json

    @property
    def text(self):
        return str(self._json)

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


class RaisingCallable:
    """Wraps an exception so `FakeSession.post`/`.get` can raise it on a
    scripted call instead of returning a response."""
    def __init__(self, ex: Exception):
        self.ex = ex

    def __call__(self):
        raise self.ex


class FakeSession:
    def __init__(self, post_script: Optional[List[Any]] = None, get_script: Optional[List[Any]] = None):
        self.post_script = list(post_script or [FakeResponse(200, {})])
        self.get_script = list(get_script or [FakeResponse(200, {})])
        self.post_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "stream": stream})
        item = self.post_script.pop(0) if len(self.post_script) > 1 else self.post_script[0]
        return item() if callable(item) else item

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        item = self.get_script.pop(0) if len(self.get_script) > 1 else self.get_script[0]
        return item() if callable(item) else item

    def close(self):
        pass


def _openai_style_json(text: str = "Hello!", finish_reason: str = "stop", usage: Optional[dict] = None) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "model": "test-model", "usage": usage or {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _openai_style_sse(pieces: List[str], finish_reason: str = "stop") -> List[str]:
    lines = []
    for p in pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}))
    lines.append("data: " + json.dumps({
        "choices": [{"delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 4, "completion_tokens": len(pieces), "total_tokens": 4 + len(pieces)},
    }))
    lines.append("data: [DONE]")
    return lines


# ============================================================================
# OpenAI-compatible providers (OpenRouter / OpenAI / Local) - parametrized
# ============================================================================

_OPENAI_COMPATIBLE = [
    (OpenRouterProvider, "https://openrouter.ai/api/v1", True),
    (OpenAIProvider, "https://api.openai.com/v1", True),
    (LocalProvider, "http://localhost:1234/v1", False),
]


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_chat_non_streaming(cls, base_url, requires_key):
    session = FakeSession(post_script=[FakeResponse(200, _openai_style_json("Hi there"))])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()

    result = client.chat([{"role": "user", "content": "hello"}])
    assert result.text == "Hi there"
    assert result.provider == cls.name
    assert result.prompt_tokens == 5 and result.completion_tokens == 3
    assert session.post_calls[0]["json"]["model"] == "m"
    assert session.post_calls[0]["json"]["stream"] is False


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_streaming(cls, base_url, requires_key):
    session = FakeSession(post_script=[FakeResponse(200, lines=_openai_style_sse(["Hel", "lo"]))])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    assert client.supports_streaming() is True

    chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    text = "".join(c.delta for c in chunks)
    assert text == "Hello"
    assert chunks[-1].finished is True
    assert chunks[-1].total_tokens == 6


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_auth_failure_not_retried(cls, base_url, requires_key):
    session = FakeSession(post_script=[FakeResponse(401, {"error": {"message": "invalid api key"}})])
    cfg = ProviderConfig(provider=cls.name, api_key="bad", base_url=base_url, model="m", max_retries=3)
    client = cls(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderAuthError):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(session.post_calls) == 1  # never retried


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_rate_limit_retried_then_recovers(cls, base_url, requires_key):
    script = [FakeResponse(429, {"error": {"message": "rate limited"}}), FakeResponse(200, _openai_style_json("recovered"))]
    session = FakeSession(post_script=script)
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m", max_retries=2, retry_backoff_base_s=0.01)
    sleeps = []
    client = cls(cfg, session=session, sleep_fn=lambda s: sleeps.append(s))
    client.initialize()
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.text == "recovered"
    assert len(session.post_calls) == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_server_error_retryable(cls, base_url, requires_key):
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m", max_retries=1)
    session = FakeSession(post_script=[FakeResponse(503, {"error": {"message": "down"}})])
    client = cls(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderServerError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retryable is True
    assert len(session.post_calls) == 2  # 1 initial + 1 retry, then exhausted


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_timeout_classified(cls, base_url, requires_key):
    import requests as real_requests
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m", max_retries=0)
    session = FakeSession(post_script=[RaisingCallable(real_requests.exceptions.Timeout("slow"))])
    client = cls(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderTimeoutError):
        client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_network_error_classified(cls, base_url, requires_key):
    import requests as real_requests
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m", max_retries=0)
    session = FakeSession(post_script=[RaisingCallable(real_requests.exceptions.ConnectionError("refused"))])
    client = cls(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderNetworkError):
        client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_health_check(cls, base_url, requires_key):
    from luno.adapters.llm.models import HealthState
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    session = FakeSession(get_script=[FakeResponse(200, {"data": []})])
    client = cls(cfg, session=session)
    client.initialize()
    health = client.health()
    assert health.state == HealthState.HEALTHY

    session2 = FakeSession(get_script=[FakeResponse(401, {})])
    client2 = cls(cfg, session=session2)
    client2.initialize()
    health2 = client2.health()
    assert health2.state == HealthState.AUTH_FAILED


def test_openrouter_missing_key_not_configured():
    cfg = ProviderConfig(provider="openrouter", api_key="", base_url="https://openrouter.ai/api/v1", model="m")
    client = OpenRouterProvider(cfg)
    with pytest.raises(ProviderNotConfiguredError):
        client.initialize()


def test_local_provider_never_requires_api_key():
    cfg = ProviderConfig(provider="local", api_key="", base_url="http://localhost:1234/v1", model="m")
    client = LocalProvider(cfg, session=FakeSession())
    client.initialize()  # must not raise despite empty api_key
    assert client._requires_api_key() is False
    info = client.get_model_info("m")
    assert info.input_cost_per_1m == 0.0  # local inference always reported as $0 metered cost


def test_openrouter_capability_flags():
    cfg = ProviderConfig(provider="openrouter", api_key="k", base_url="https://openrouter.ai/api/v1", model="m")
    client = OpenRouterProvider(cfg, session=FakeSession())
    assert client.supports_streaming() is True
    assert client.supports_tools() is True
    assert client.supports_images() is True
    caps = client.capabilities()
    assert caps.streaming and caps.images


def test_openai_provider_cost_catalog_known_model():
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    client = OpenAIProvider(cfg, session=FakeSession())
    info = client.get_model_info("gpt-4o-mini")
    assert info.input_cost_per_1m == 0.15
    assert info.output_cost_per_1m == 0.6

    info_unknown = client.get_model_info("some-future-model-xyz")
    assert info_unknown.input_cost_per_1m is None  # unknown model -> cost unknown, never guessed


def test_openai_provider_cost_catalog_daily_and_reasoning_models():
    """OpenAI-Primary/DeepSeek-Fallback sprint - verified 2026-08-01
    against https://developers.openai.com/api/docs/models."""
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="gpt-5.6-luna")
    client = OpenAIProvider(cfg, session=FakeSession())
    daily = client.get_model_info("gpt-5.6-luna")
    assert daily.input_cost_per_1m == 0.20
    reasoning = client.get_model_info("gpt-5.6-sol")
    assert reasoning.input_cost_per_1m == 5.0


# ============================================================================
# reasoning_effort passthrough (OpenAI-Primary/DeepSeek-Fallback sprint)
# ============================================================================

def test_openai_provider_injects_reasoning_effort_from_metadata():
    session = FakeSession(post_script=[FakeResponse(200, _openai_style_json("ok"))])
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="gpt-5.6-sol")
    client = OpenAIProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], metadata={"reasoning_effort": "high"})
    assert session.post_calls[0]["json"]["reasoning_effort"] == "high"


def test_openai_provider_drops_invalid_reasoning_effort():
    session = FakeSession(post_script=[FakeResponse(200, _openai_style_json("ok"))])
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="gpt-5.6-luna")
    client = OpenAIProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], metadata={"reasoning_effort": "extremely-high-nonsense"})
    assert "reasoning_effort" not in session.post_calls[0]["json"]


def test_openai_provider_no_metadata_omits_reasoning_effort():
    session = FakeSession(post_script=[FakeResponse(200, _openai_style_json("ok"))])
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="gpt-5.6-luna")
    client = OpenAIProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in session.post_calls[0]["json"]


def test_openrouter_provider_never_receives_reasoning_effort():
    """`_extra_payload_fields()` is a no-op on every provider except
    OpenAI - even if `reasoning_effort` metadata is present, OpenRouter
    (routing to DeepSeek) must never send an OpenAI-only param."""
    session = FakeSession(post_script=[FakeResponse(200, _openai_style_json("ok"))])
    cfg = ProviderConfig(provider="openrouter", api_key="k", base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4-flash")
    client = OpenRouterProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], metadata={"reasoning_effort": "high"})
    assert "reasoning_effort" not in session.post_calls[0]["json"]


# ============================================================================
# Gemini
# ============================================================================

def _gemini_json(text="Hi from Gemini", finish_reason="STOP"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3, "totalTokenCount": 7},
    }


def test_gemini_chat_translates_system_prompt_and_roles():
    session = FakeSession(post_script=[FakeResponse(200, _gemini_json())])
    cfg = ProviderConfig(provider="gemini", api_key="k", base_url="https://generativelanguage.googleapis.com/v1beta", model="gemini-2.5-flash")
    client = GeminiProvider(cfg, session=session)
    client.initialize()

    result = client.chat(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        system_prompt="also be nice",
    )
    assert result.text == "Hi from Gemini"
    assert result.prompt_tokens == 4 and result.total_tokens == 7
    body = session.post_calls[0]["json"]
    assert body["systemInstruction"]["parts"][0]["text"] == "also be nice\n\nbe terse"
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}, {"role": "model", "parts": [{"text": "hey"}]}]
    assert "x-goog-api-key" in session.post_calls[0]["headers"]


def test_gemini_streaming():
    lines = [
        "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}),
        "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2, "totalTokenCount": 4}}),
    ]
    session = FakeSession(post_script=[FakeResponse(200, lines=lines)])
    cfg = ProviderConfig(provider="gemini", api_key="k", base_url="https://generativelanguage.googleapis.com/v1beta", model="gemini-2.5-flash")
    client = GeminiProvider(cfg, session=session)
    client.initialize()
    chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert "".join(c.delta for c in chunks) == "Hello"
    assert chunks[-1].finished and chunks[-1].total_tokens == 4


def test_gemini_auth_error():
    session = FakeSession(post_script=[FakeResponse(403, {"error": {"message": "bad key"}})])
    cfg = ProviderConfig(provider="gemini", api_key="bad", base_url="https://generativelanguage.googleapis.com/v1beta", model="m", max_retries=2)
    client = GeminiProvider(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderAuthError):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(session.post_calls) == 1


def test_gemini_missing_key_not_configured():
    cfg = ProviderConfig(provider="gemini", api_key="", base_url="https://generativelanguage.googleapis.com/v1beta", model="m")
    client = GeminiProvider(cfg)
    with pytest.raises(ProviderNotConfiguredError):
        client.initialize()


def test_gemini_capabilities_include_long_context():
    cfg = ProviderConfig(provider="gemini", api_key="k", base_url="x", model="gemini-2.5-pro")
    client = GeminiProvider(cfg, session=FakeSession())
    assert client.supports_long_context() is True
    info = client.get_model_info("gemini-2.5-pro")
    assert info.capabilities.max_context_tokens == 1000000


# ============================================================================
# Anthropic
# ============================================================================

def _anthropic_json(text="Hi from Claude", stop_reason="end_turn"):
    return {
        "content": [{"type": "text", "text": text}], "model": "claude-sonnet-4-5",
        "stop_reason": stop_reason, "usage": {"input_tokens": 5, "output_tokens": 4},
    }


def test_anthropic_chat_uses_top_level_system_and_required_max_tokens():
    session = FakeSession(post_script=[FakeResponse(200, _anthropic_json())])
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-5")
    client = AnthropicProvider(cfg, session=session)
    client.initialize()

    result = client.chat([{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}])
    assert result.text == "Hi from Claude"
    assert result.prompt_tokens == 5 and result.completion_tokens == 4 and result.total_tokens == 9
    body = session.post_calls[0]["json"]
    assert body["system"] == "be terse"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 1024  # default fallback - Anthropic requires this field
    headers = session.post_calls[0]["headers"]
    assert headers["x-api-key"] == "k"
    assert headers["anthropic-version"]
    assert "Authorization" not in headers  # NOT Bearer-style, unlike OpenAI-compatible providers


def test_anthropic_streaming_accumulates_across_event_types():
    lines = [
        "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
        "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}),
        "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}),
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        "data: " + json.dumps({"type": "message_stop"}),
    ]
    session = FakeSession(post_script=[FakeResponse(200, lines=lines)])
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-5")
    client = AnthropicProvider(cfg, session=session)
    client.initialize()
    chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert "".join(c.delta for c in chunks) == "Hello"
    assert chunks[-1].finished and chunks[-1].finish_reason == "end_turn"
    assert chunks[-1].prompt_tokens == 5 and chunks[-1].completion_tokens == 2 and chunks[-1].total_tokens == 7


def test_anthropic_rate_limit_retried():
    script = [FakeResponse(429, {"error": {"message": "slow down"}}), FakeResponse(200, _anthropic_json("ok now"))]
    session = FakeSession(post_script=script)
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="https://api.anthropic.com/v1", model="m", max_retries=2, retry_backoff_base_s=0.01)
    client = AnthropicProvider(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.text == "ok now"
    assert len(session.post_calls) == 2


def test_anthropic_overloaded_529_is_retryable_server_error():
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="https://api.anthropic.com/v1", model="m", max_retries=1)
    session = FakeSession(post_script=[FakeResponse(529, {"error": {"message": "overloaded"}})])
    client = AnthropicProvider(cfg, session=session, sleep_fn=lambda s: None)
    client.initialize()
    with pytest.raises(ProviderServerError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retryable is True
    assert len(session.post_calls) == 2


def test_anthropic_missing_key_not_configured():
    cfg = ProviderConfig(provider="anthropic", api_key="", base_url="https://api.anthropic.com/v1", model="m")
    client = AnthropicProvider(cfg)
    with pytest.raises(ProviderNotConfiguredError):
        client.initialize()


def test_anthropic_capabilities():
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="x", model="claude-opus-4-1")
    client = AnthropicProvider(cfg, session=FakeSession())
    assert client.supports_reasoning() is True
    assert client.supports_images() is True
    info = client.get_model_info("claude-opus-4-1")
    assert info.input_cost_per_1m == 15.0 and info.output_cost_per_1m == 75.0


# ============================================================================
# Cross-provider: cancel()
# ============================================================================

@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_openai_compatible_cancel_closes_inflight_stream(cls, base_url, requires_key):
    lines = _openai_style_sse(["a", "b", "c", "d", "e"])
    session = FakeSession(post_script=[FakeResponse(200, lines=lines)])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    cancel_event = threading.Event()
    gen = client.stream_chat([{"role": "user", "content": "hi"}], cancel_event=cancel_event, request_id="req-x")
    next(gen)  # pull one chunk so the stream is actually open and tracked
    assert client.cancel("req-x") is True
    assert client.cancel("req-x") is False  # already removed - nothing left to cancel


def test_gemini_cancel_and_anthropic_cancel_return_false_when_nothing_inflight():
    cfg_g = ProviderConfig(provider="gemini", api_key="k", base_url="x", model="m")
    assert GeminiProvider(cfg_g, session=FakeSession()).cancel("nope") is False
    cfg_a = ProviderConfig(provider="anthropic", api_key="k", base_url="x", model="m")
    assert AnthropicProvider(cfg_a, session=FakeSession()).cancel("nope") is False
