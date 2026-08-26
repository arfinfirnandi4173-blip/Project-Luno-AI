"""
test_llm_max_completion_tokens_compatibility.py
==================================================

Sprint 54 - LLM Stack API Compatibility & Max Completion Tokens
Hardening.

Root cause (see `docs/change_impact/
llm_max_completion_tokens_compatibility.md` for the full writeup):
`luno.adapters.llm.base.OpenAICompatibleClient._payload()` - the ONE
shared request-body builder every `OpenAICompatibleClient` subclass
(`OpenRouterProvider`, `OpenAIProvider`, `LocalProvider`) uses for BOTH
`chat()` (non-streaming) and `stream_chat()` (streaming) - unconditionally
wrote the completion-length JSON key as the literal string
`"max_tokens"`, regardless of the configured model. This is the SAME
class of bug Sprint 53 fixed in the separate, legacy
`luno.adapters.openrouter.OpenRouterAdapter`/`RequestsOpenRouterClient`
- except THIS file (`luno/adapters/llm/base.py`) is the one that is
actually live in production: `bootstrap/adapters.py` constructs
`LLMManagerAdapter()` (not `OpenRouterAdapter`) under the
`adapters["openrouter_adapter"]` key (a legacy variable/module-id name
kept for backward compatibility, not the class it names anymore - see
`luno/adapters/llm_manager.py`'s own module docstring: "Replaces
`luno.adapters.openrouter.OpenRouterAdapter` as the module
`bootstrap/adapters.py` actually constructs and registers"). Session
Summary's `openai_client.chat_completion(..., max_tokens=150)` -
sourced from `LLMManagerAdapter.client` (a `_LegacyClientShim`), not
`OpenRouterAdapter.client` as Sprint 53's own documentation assumed -
resolves to `LLMManagerAdapter.chat_once()` -> the configured provider
client's `.chat()` -> for the DEFAULT `LLM_PROVIDER="openrouter"`, that
is `luno.adapters.llm.openrouter_provider.OpenRouterProvider` (a
DIFFERENT, separate `OpenAICompatibleClient` subclass, confusingly
similarly named) -> `OpenAICompatibleClient._payload()`, the exact
method this file's tests target. This file's tests are the ones that
actually exercise the code path the original bug report hit.

Every HTTP call goes through a local, self-contained fake
`requests.Session`-like double (`_FakeSession`/`_FakeResponse`, the
SAME pattern `luno/adapters/tests/test_openrouter_adapter.py` and
`luno/adapters/llm/tests/test_providers.py` already establish) - no
real network access anywhere. Tests exercise the REAL, unmodified
`OpenRouterProvider`/`OpenAIProvider`/`LocalProvider`/`AnthropicProvider`/
`GeminiProvider` classes (not a re-implementation of `_payload()`), so
this proves the actual production request-building code, not a mock of
the function under test.

Run:
    pytest -q tests/test_llm_max_completion_tokens_compatibility.py
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from luno import config
from luno.adapters.llm.anthropic_provider import AnthropicProvider
from luno.adapters.llm.gemini_provider import GeminiProvider
from luno.adapters.llm.local_provider import LocalProvider
from luno.adapters.llm.models import ProviderConfig
from luno.adapters.llm.openai_provider import OpenAIProvider
from luno.adapters.llm.openrouter_provider import OpenRouterProvider

# ============================================================================
# Local fakes - self-contained (this file lives in the top-level `tests/`
# package, a different package from `luno/adapters/llm/tests/`, so it
# does not cross-import that file's own fakes - same convention Sprint
# 53's own new test file followed).
# ============================================================================

class _FakeResponse:
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


class _FakeSession:
    def __init__(self, post_script=None):
        self.post_script = list(post_script or [_FakeResponse(200, {})])
        self.post_calls: List[Dict[str, Any]] = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "stream": stream})
        item = self.post_script.pop(0) if len(self.post_script) > 1 else self.post_script[0]
        return item() if callable(item) else item

    def close(self):
        pass


def _openai_style_json(text: str = "Hi there", finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "model": "test-model", "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _openai_style_sse(pieces: List[str], finish_reason: str = "stop") -> List[str]:
    lines = []
    for p in pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}))
    lines.append("data: " + json.dumps({
        "choices": [{"delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 4, "completion_tokens": len(pieces), "total_tokens": 4 + len(pieces)},
    }))
    return lines


def _rejects_legacy_max_tokens_session() -> _FakeSession:
    """Simulates a real OpenAI-family provider's actual rejection
    behavior: any request body containing a literal `"max_tokens"` key
    is rejected with the EXACT error text from Sprint 53's original bug
    report; a body containing `config.MAX_TOKENS_PARAM`'s configured
    key succeeds. Mirrors `tests/
    test_memory_session_summary_api_compatibility.py`'s own
    `_ModelAwareFakeSession` - same simulation technique, not a live
    call, never represented as one."""

    class _Session(_FakeSession):
        def __init__(self):
            super().__init__(post_script=[])
            self.post_calls = []

        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "stream": stream})
            if "max_tokens" in json:
                return _FakeResponse(400, {
                    "error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                          "with this model. Use 'max_completion_tokens' instead."}
                })
            return _FakeResponse(200, _openai_style_json("compatible"))

    return _Session()


# ============================================================================
# 1/3/4. Payload uses config.MAX_TOKENS_PARAM; requested token count is
# preserved exactly; a request WITHOUT a token limit stays without one.
# Parametrized across all three real OpenAICompatibleClient subclasses -
# proves Phase 4 item E (subclass/inherited adapter behavior): none of
# them override `_payload()`, so a single fix covers all three.
# ============================================================================

_OPENAI_COMPATIBLE = [
    (OpenRouterProvider, "https://openrouter.ai/api/v1", True),
    (OpenAIProvider, "https://api.openai.com/v1", True),
    (LocalProvider, "http://localhost:1234/v1", False),
]


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_payload_uses_configured_completion_token_param(cls, base_url, requires_key):
    session = _FakeSession(post_script=[_FakeResponse(200, _openai_style_json())])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()

    result = client.chat([{"role": "user", "content": "hi"}], max_tokens=150)
    assert result.text == "Hi there"
    body = session.post_calls[0]["json"]
    assert config.MAX_TOKENS_PARAM in body, f"expected '{config.MAX_TOKENS_PARAM}' key, got {list(body.keys())}"
    assert body[config.MAX_TOKENS_PARAM] == 150, "the requested token count must be preserved exactly"
    assert "max_tokens" not in body or config.MAX_TOKENS_PARAM == "max_tokens", (
        "the old, incompatible key must not ALSO be sent when the configured param is different"
    )


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_legacy_max_tokens_key_never_generated_with_default_config(cls, base_url, requires_key):
    session = _FakeSession(post_script=[_FakeResponse(200, _openai_style_json())])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], max_tokens=150)
    body = session.post_calls[0]["json"]
    assert "max_tokens" not in body, "default config.MAX_TOKENS_PARAM is 'max_completion_tokens' - the legacy key must not be sent"


@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_request_without_token_limit_has_no_token_limit_field(cls, base_url, requires_key):
    """No explicit max_tokens AND no per-provider *_MAX_TOKENS env/config
    default -> the completion-length key is omitted entirely, exactly as
    before this sprint (matches ordinary chat's own behavior - see
    Sprint 53's own equivalent test for `luno.adapters.openrouter`)."""
    session = _FakeSession(post_script=[_FakeResponse(200, _openai_style_json())])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}])  # no max_tokens passed
    body = session.post_calls[0]["json"]
    assert "max_tokens" not in body
    assert config.MAX_TOKENS_PARAM not in body


# ============================================================================
# 5. Streaming path - the SAME `_payload()` call, proven via
# `stream_chat()` rather than `chat()`.
# ============================================================================

@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_streaming_payload_uses_configured_completion_token_param(cls, base_url, requires_key):
    session = _FakeSession(post_script=[_FakeResponse(200, lines=_openai_style_sse(["Hel", "lo"]))])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    assert client.supports_streaming() is True

    chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], max_tokens=150))
    assert "".join(c.delta for c in chunks) == "Hello"
    body = session.post_calls[0]["json"]
    assert body["stream"] is True
    assert config.MAX_TOKENS_PARAM in body
    assert body[config.MAX_TOKENS_PARAM] == 150
    assert "max_tokens" not in body


# ============================================================================
# 6. Tool/function-calling path - confirmed NOT APPLICABLE by code
# inspection: `_payload()` has no separate tool/function-call branch,
# and no `OpenAICompatibleClient` subclass builds one either
# (`supports_tools()` is a pure capability FLAG, never consulted by
# `_payload()`/`chat()`/`stream_chat()` to change the request shape).
# This test proves that a `metadata` dict shaped like it might carry
# tool-calling hints does not interfere with the completion-token fix
# either way - the two are fully independent, exactly as code
# inspection predicts.
# ============================================================================

def test_metadata_with_tool_like_keys_does_not_interfere_with_token_param():
    session = _FakeSession(post_script=[_FakeResponse(200, _openai_style_json())])
    cfg = ProviderConfig(provider="openai", api_key="k", base_url="https://api.openai.com/v1", model="m")
    client = OpenAIProvider(cfg, session=session)
    client.initialize()
    client.chat(
        [{"role": "user", "content": "hi"}], max_tokens=150,
        metadata={"reasoning_effort": "medium"},  # the one metadata-driven extra field this stack actually supports
    )
    body = session.post_calls[0]["json"]
    assert body[config.MAX_TOKENS_PARAM] == 150
    assert body.get("reasoning_effort") == "medium"


# ============================================================================
# 8. Provider/model configuration using the existing config abstraction -
# proven genuinely config-driven (not a second hardcoded literal) by
# pointing the same code at BOTH valid values and observing the wire
# body change accordingly (mirrors Sprint 53's own equivalent test).
# ============================================================================

@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_completion_token_param_is_config_driven_not_hardcoded(monkeypatch, cls, base_url, requires_key):
    monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_tokens", raising=False)
    session = _FakeSession(post_script=[_FakeResponse(200, _openai_style_json())])
    cfg = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client = cls(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], max_tokens=150)
    body = session.post_calls[0]["json"]
    assert body["max_tokens"] == 150, "pointing config.MAX_TOKENS_PARAM at the older key name must change the wire output"


# ============================================================================
# 9. Regression - Sprint 53's OpenRouter fix (a SEPARATE file,
# `luno.adapters.openrouter`, deliberately untouched by this sprint)
# remains intact.
# ============================================================================

def test_sprint53_openrouter_adapter_fix_remains_intact():
    from luno.adapters.openrouter import OpenRouterConfig, RequestsOpenRouterClient

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            self.calls.append({"json": json})
            return _FakeResponse(200, {
                "model": json.get("model"),
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            })

    session = _Session()
    client = RequestsOpenRouterClient(OpenRouterConfig(api_key="k"), session=session, sleep_fn=lambda s: None)
    client.chat_completion(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=150)
    body = session.calls[0]["json"]
    assert config.MAX_TOKENS_PARAM in body
    assert body[config.MAX_TOKENS_PARAM] == 150
    assert "max_tokens" not in body


# ============================================================================
# 10. Simulated provider rejection of legacy max_tokens - realistic
# before/after reproduction, not a live call.
# ============================================================================

@pytest.mark.parametrize("cls,base_url,requires_key", _OPENAI_COMPATIBLE)
def test_before_fix_behavior_would_have_reproduced_the_exact_reported_error(monkeypatch, cls, base_url, requires_key):
    """Simulates the pre-Sprint-54 wire behavior (the hardcoded literal
    `"max_tokens"` key `_payload()` used to always send) by pointing
    `config.MAX_TOKENS_PARAM` at that same literal - NOT by re-injecting
    old code. Proves the exact error string from Sprint 53's bug report
    reproduces under that condition, and that the current (fixed)
    default configuration does not."""
    # BEFORE (simulated): old hardcoded key name.
    monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_tokens", raising=False)
    session_before = _rejects_legacy_max_tokens_session()
    cfg_before = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client_before = cls(cfg_before, session=session_before)
    client_before.initialize()
    with pytest.raises(Exception) as excinfo:
        client_before.chat([{"role": "user", "content": "hi"}], max_tokens=150)
    assert "Unsupported parameter: 'max_tokens'" in str(excinfo.value)

    # AFTER (actual current code path): correct key name.
    monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_completion_tokens", raising=False)
    session_after = _rejects_legacy_max_tokens_session()
    cfg_after = ProviderConfig(provider=cls.name, api_key="k" if requires_key else "", base_url=base_url, model="m")
    client_after = cls(cfg_after, session=session_after)
    client_after.initialize()
    result = client_after.chat([{"role": "user", "content": "hi"}], max_tokens=150)
    assert result.text == "compatible"


# ============================================================================
# Boundary-of-scope regression - Anthropic and Gemini are NOT
# `OpenAICompatibleClient` subclasses and must remain byte-for-byte
# unaffected: Anthropic's own `"max_tokens"` field is REQUIRED and
# CORRECT for its API (not this same incompatibility - see
# `anthropic_provider.py`'s own module docstring), and Gemini uses an
# entirely different key (`maxOutputTokens` inside `generationConfig`).
# Pointing `config.MAX_TOKENS_PARAM` at either value must not change
# either provider's own wire behavior at all.
# ============================================================================

@pytest.mark.parametrize("configured_param", ["max_tokens", "max_completion_tokens"])
def test_anthropic_provider_unaffected_by_max_tokens_param_config(monkeypatch, configured_param):
    monkeypatch.setattr(config, "MAX_TOKENS_PARAM", configured_param, raising=False)
    session = _FakeSession(post_script=[_FakeResponse(200, {
        "content": [{"type": "text", "text": "hi"}], "model": "claude-sonnet-4-5",
        "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 4},
    })])
    cfg = ProviderConfig(provider="anthropic", api_key="k", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-5")
    client = AnthropicProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], max_tokens=200)
    body = session.post_calls[0]["json"]
    assert body["max_tokens"] == 200, "Anthropic's own literal 'max_tokens' field must be untouched by config.MAX_TOKENS_PARAM"
    assert "max_completion_tokens" not in body


@pytest.mark.parametrize("configured_param", ["max_tokens", "max_completion_tokens"])
def test_gemini_provider_unaffected_by_max_tokens_param_config(monkeypatch, configured_param):
    monkeypatch.setattr(config, "MAX_TOKENS_PARAM", configured_param, raising=False)
    session = _FakeSession(post_script=[_FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4, "totalTokenCount": 9},
    })])
    cfg = ProviderConfig(provider="gemini", api_key="k", base_url="https://generativelanguage.googleapis.com/v1beta", model="gemini-2.5-flash")
    client = GeminiProvider(cfg, session=session)
    client.initialize()
    client.chat([{"role": "user", "content": "hi"}], max_tokens=200)
    body = session.post_calls[0]["json"]
    assert body["generationConfig"]["maxOutputTokens"] == 200
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body
