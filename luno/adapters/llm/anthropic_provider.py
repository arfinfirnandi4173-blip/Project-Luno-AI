"""
anthropic_provider.py
=======================

`AnthropicProvider` - Anthropic's Messages API (`api.anthropic.com/v1/
messages`). Does NOT subclass `OpenAICompatibleClient` for the same
reason `gemini_provider.py` doesn't - see `base.py`'s docstring.

Translation this module owns:

  - OpenAI-style `messages` (role `system`/`user`/`assistant`) ->
    Anthropic's shape: `system` is a TOP-LEVEL string field, never
    inside the `messages` array (Anthropic rejects a `"system"` role
    there) - `system` messages found in the list are pulled out and
    joined with an explicit `system_prompt=`, same policy as
    `gemini_provider.py`.
  - `max_tokens` is REQUIRED by this API (unlike OpenAI/Gemini, where
    it's optional) - defaults to `config.max_tokens` and, failing that,
    a hardcoded safe fallback (`_DEFAULT_MAX_TOKENS`) rather than
    raising, so a caller that never set one still gets a request that
    goes through.
  - Non-streaming response: `content` is a list of blocks
    (`{"type": "text", "text": "..."}`, ...) - concatenates every
    `text` block (tool-use blocks, if any, are ignored here - handling
    tool_use blocks as first-class output is future work, not this
    sprint's scope for the base chat path).
  - Streaming (real SSE): `content_block_delta` events carry
    `delta.text`; `message_delta` carries the final `stop_reason` +
    cumulative `usage.output_tokens`; `message_start` carries
    `usage.input_tokens`. Every event is JSON on its own `data:` line
    (`event:` lines are informational only - this parses purely off
    each JSON object's own `"type"` field, so the shared SSE line-
    reading shape stays identical to every other provider in this
    package).
  - Auth: `x-api-key` header (NOT `Authorization: Bearer` - Anthropic's
    own convention) plus the required `anthropic-version` header.
  - HTTP 529 ("overloaded_error") is Anthropic-specific - not one of
    the standard codes `errors.classify_http_status()` maps, handled
    here as a retryable `ProviderServerError` before falling through to
    the shared classifier.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from .base import LLMProviderClient, _error_message, retry_call
from .errors import (
    ProviderNetworkError,
    ProviderNotConfiguredError,
    ProviderServerError,
    ProviderStreamError,
    ProviderTimeoutError,
    classify_http_status,
)
from .models import ChatResult, HealthState, LLMStreamChunk, ModelInfo, ProviderCapabilities, ProviderConfig, ProviderHealth

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None


ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 1024

_MODEL_CATALOG = {
    "claude-sonnet-4-5": {"display_name": "Claude Sonnet 4.5", "context_tokens": 200000, "input_cost_per_1m": 3.0, "output_cost_per_1m": 15.0},
    "claude-opus-4-1": {"display_name": "Claude Opus 4.1", "context_tokens": 200000, "input_cost_per_1m": 15.0, "output_cost_per_1m": 75.0},
    "claude-haiku-4-5": {"display_name": "Claude Haiku 4.5", "context_tokens": 200000, "input_cost_per_1m": 1.0, "output_cost_per_1m": 5.0},
}


def _messages_to_anthropic(messages: List[Dict[str, str]], system_prompt: Optional[str]):
    """Returns `(messages, system_text_or_None)` - `messages` filtered
    to only `user`/`assistant` roles (Anthropic's only two)."""
    out = []
    system_parts: List[str] = [system_prompt] if system_prompt else []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "") or ""
        if role == "system":
            system_parts.append(text)
            continue
        out.append({"role": "assistant" if role == "assistant" else "user", "content": text})
    system_text = "\n\n".join(p for p in system_parts if p) or None
    return out, system_text


def _classify_anthropic_status(status_code: int, message: str):
    if status_code == 529:
        return ProviderServerError(message, status_code=status_code)
    return classify_http_status(status_code, message)


class AnthropicProvider(LLMProviderClient):
    name = "anthropic"

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, config: ProviderConfig, session: Optional[Any] = None, sleep_fn: Optional[Any] = None) -> None:
        self.config = config
        self._session = session
        self._sleep = sleep_fn or time.sleep
        self._lock = threading.Lock()
        self._inflight_responses: Dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    def initialize(self) -> None:
        if not self.config.api_key:
            raise ProviderNotConfiguredError("anthropic: no API key configured (ANTHROPIC_API_KEY)")
        if self._session is None:
            if _requests is None:  # pragma: no cover
                raise ProviderNotConfiguredError("anthropic: the 'requests' package is required")
            self._session = _requests.Session()

    def shutdown(self) -> None:
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass

    def reload(self, config: ProviderConfig) -> None:
        with self._lock:
            self.config = config

    # -- capabilities -----------------------------------------------------------

    def supports_streaming(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return True

    def supports_images(self) -> bool:
        return True

    def supports_reasoning(self) -> bool:
        return True  # extended thinking

    def supports_long_context(self) -> bool:
        return True  # 200k context

    def get_model_info(self, model: Optional[str] = None) -> ModelInfo:
        model_id = model or self.config.model or "claude-sonnet-4-5"
        entry = _MODEL_CATALOG.get(model_id, {})
        return ModelInfo(
            id=model_id, provider=self.name, display_name=entry.get("display_name", model_id),
            capabilities=ProviderCapabilities(
                streaming=True, tools=True, images=True, vision=True, reasoning=True,
                long_context=True, max_context_tokens=entry.get("context_tokens", 200000),
            ),
            input_cost_per_1m=entry.get("input_cost_per_1m"), output_cost_per_1m=entry.get("output_cost_per_1m"),
        )

    def list_models(self) -> List[ModelInfo]:
        return [self.get_model_info(m) for m in _MODEL_CATALOG]

    # -- health -----------------------------------------------------------------

    def health(self) -> ProviderHealth:
        t0 = time.time()
        try:
            with self._lock:
                cfg = self.config
            # Anthropic has no cheap "list models" endpoint pre-dating
            # 2025 tooling in every account tier - a minimal 1-token
            # `messages` call is the documented lightest reachability
            # check, same pattern the SDKs themselves use.
            body = {
                "model": cfg.model or "claude-haiku-4-5", "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            resp = self._session.post(
                f"{cfg.base_url}/messages", json=body, headers=self._headers(), timeout=min(cfg.timeout_s, 10.0),
            )
            latency_ms = (time.time() - t0) * 1000
            if resp.status_code in (200, 400):  # 400 here just means the tiny probe body itself was rejected - auth still proven fine
                return ProviderHealth(self.name, HealthState.HEALTHY, "reachable", latency_ms=latency_ms)
            if resp.status_code in (401, 403):
                return ProviderHealth(self.name, HealthState.AUTH_FAILED, _error_message(resp), latency_ms=latency_ms)
            if resp.status_code == 429:
                return ProviderHealth(self.name, HealthState.RATE_LIMITED, _error_message(resp), latency_ms=latency_ms)
            if resp.status_code == 529:
                return ProviderHealth(self.name, HealthState.WARNING, "overloaded", latency_ms=latency_ms)
            return ProviderHealth(self.name, HealthState.WARNING, f"HTTP {resp.status_code}", latency_ms=latency_ms)
        except getattr(_requests, "Timeout", TimeoutError):
            return ProviderHealth(self.name, HealthState.TIMEOUT, "health check timed out", latency_ms=(time.time() - t0) * 1000)
        except Exception as ex:
            return ProviderHealth(self.name, HealthState.OFFLINE, str(ex), latency_ms=(time.time() - t0) * 1000)

    # -- request building ---------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        with self._lock:
            cfg = self.config
        return {"Content-Type": "application/json", "x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION}

    def _post(self, body: Dict[str, Any], *, stream: bool, timeout_s: float) -> Any:
        with self._lock:
            cfg = self.config
        try:
            return self._session.post(
                f"{cfg.base_url}/messages", json=body, headers=self._headers(), timeout=timeout_s, stream=stream,
            )
        except getattr(_requests, "Timeout", TimeoutError) as ex:
            raise ProviderTimeoutError(f"request timed out: {ex}") from ex
        except Exception as ex:
            if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                raise ProviderNetworkError(f"network error: {ex}") from ex
            raise

    def _body(self, model, anth_messages, system_text, temperature, max_tokens, stream) -> Dict[str, Any]:
        with self._lock:
            cfg = self.config
        body: Dict[str, Any] = {
            "model": model, "messages": anth_messages, "stream": stream,
            "max_tokens": max_tokens if max_tokens is not None else (cfg.max_tokens or _DEFAULT_MAX_TOKENS),
        }
        if system_text:
            body["system"] = system_text
        temp = temperature if temperature is not None else cfg.temperature
        if temp is not None:
            body["temperature"] = temp
        return body

    # -- chat -----------------------------------------------------------------

    def chat(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None, **kwargs: Any,
    ) -> ChatResult:
        with self._lock:
            cfg = self.config
        model = model or cfg.model
        if not model:
            raise ProviderNotConfiguredError("anthropic: no model specified and no default configured")
        anth_messages, system_text = _messages_to_anthropic(messages, system_prompt)
        body = self._body(model, anth_messages, system_text, temperature, max_tokens, stream=False)
        t0 = time.time()

        def _attempt() -> ChatResult:
            resp = self._post(body, stream=False, timeout_s=cfg.timeout_s)
            if resp.status_code >= 400:
                raise _classify_anthropic_status(resp.status_code, _error_message(resp))
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError) as ex:
                raise ProviderStreamError(f"malformed JSON response: {ex}") from ex
            try:
                blocks = data.get("content") or []
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                finish_reason = data.get("stop_reason")
            except (KeyError, TypeError) as ex:
                raise ProviderStreamError(f"unexpected response shape: {ex}") from ex
            usage = data.get("usage") or {}
            prompt_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0) if (prompt_tokens or completion_tokens) else None
            return ChatResult(
                text=text, model=data.get("model", model), provider=self.name, raw=data,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens,
                finish_reason=finish_reason, latency_ms=(time.time() - t0) * 1000,
            )

        return retry_call(_attempt, cfg, log_prefix=f"anthropic.chat[{model}]", sleep_fn=self._sleep)

    def stream_chat(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        cancel_event=None, request_id: Optional[str] = None, **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        with self._lock:
            cfg = self.config
        model = model or cfg.model
        if not model:
            raise ProviderNotConfiguredError("anthropic: no model specified and no default configured")
        anth_messages, system_text = _messages_to_anthropic(messages, system_prompt)
        body = self._body(model, anth_messages, system_text, temperature, max_tokens, stream=True)

        def _open():
            resp = self._post(body, stream=True, timeout_s=cfg.timeout_s)
            if resp.status_code >= 400:
                raise _classify_anthropic_status(resp.status_code, _error_message(resp))
            return resp

        response = retry_call(_open, cfg, log_prefix=f"anthropic.stream_chat[{model}]", sleep_fn=self._sleep)
        if request_id:
            with self._lock:
                self._inflight_responses[request_id] = response
        prompt_tokens: Optional[int] = None
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[len("data:"):].strip()
                if not data_str:
                    continue
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError as ex:
                    raise ProviderStreamError(f"malformed SSE chunk: {ex}") from ex
                obj_type = obj.get("type")
                if obj_type == "error":
                    raise ProviderStreamError(f"mid-stream error from anthropic: {obj.get('error')}")
                if obj_type == "message_start":
                    prompt_tokens = ((obj.get("message") or {}).get("usage") or {}).get("input_tokens")
                    continue
                if obj_type == "content_block_delta":
                    delta = obj.get("delta") or {}
                    text = delta.get("text") or ""
                    if text:
                        yield LLMStreamChunk(delta=text, raw=obj)
                    continue
                if obj_type == "message_delta":
                    delta = obj.get("delta") or {}
                    usage = obj.get("usage") or {}
                    completion_tokens = usage.get("output_tokens")
                    total_tokens = (
                        (prompt_tokens or 0) + (completion_tokens or 0)
                        if (prompt_tokens or completion_tokens) else None
                    )
                    yield LLMStreamChunk(
                        delta="", finished=True, finish_reason=delta.get("stop_reason"), raw=obj,
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens,
                    )
                    return
                # message_stop / content_block_start / content_block_stop /
                # ping - informational only, nothing to yield.
        finally:
            if request_id:
                with self._lock:
                    self._inflight_responses.pop(request_id, None)
            try:
                response.close()
            except Exception:
                pass

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            response = self._inflight_responses.pop(request_id, None)
        if response is None:
            return False
        try:
            response.close()
        except Exception:
            pass
        return True
