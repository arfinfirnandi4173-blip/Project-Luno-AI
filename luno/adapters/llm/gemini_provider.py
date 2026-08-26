"""
gemini_provider.py
===================

`GeminiProvider` - Google's Generative Language REST API
(`generativelanguage.googleapis.com`). Does NOT subclass
`OpenAICompatibleClient` - Gemini's wire format differs enough from
OpenAI's that sharing a base would mean more `if provider == "gemini"`
branches inside the shared class than code actually saved (see
`base.py`'s own docstring for this design call).

Translation this module owns, so nothing above `LLMProviderClient` ever
has to know Gemini's shape exists:

  - OpenAI-style `messages` (`role` in `system`/`user`/`assistant`) ->
    Gemini `contents` (`role` in `user`/`model` only) + a separate
    `systemInstruction` field. `system` messages in the list (and/or an
    explicit `system_prompt=`) are pulled OUT of `contents` and merged
    into `systemInstruction` - Gemini rejects a `"system"` role inside
    `contents` outright.
  - Non-streaming: `POST /v1beta/models/{model}:generateContent`.
  - Streaming: `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`
    - real SSE, `data: <GenerateContentResponse chunk>` lines; each
      chunk's `candidates[0].content.parts[].text` is a DELTA (Gemini
      does not repeat prior text), so no accumulation bookkeeping is
      needed converting it into `LLMStreamChunk.delta`.
  - `usageMetadata.{prompt,candidates,total}TokenCount` ->
    `ChatResult`/`LLMStreamChunk`'s `prompt_tokens`/`completion_tokens`/
    `total_tokens`.
  - Auth: `x-goog-api-key` header (the documented alternative to the
    `?key=` query-string form - kept out of the URL so it never ends up
    in a log line/proxy access log by accident).
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
    ProviderStreamError,
    ProviderTimeoutError,
    classify_http_status,
)
from .models import ChatResult, HealthState, LLMStreamChunk, ModelInfo, ProviderCapabilities, ProviderConfig, ProviderHealth

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None


_MODEL_CATALOG = {
    "gemini-2.5-pro": {"display_name": "Gemini 2.5 Pro", "context_tokens": 1000000, "input_cost_per_1m": 1.25, "output_cost_per_1m": 5.0},
    "gemini-2.5-flash": {"display_name": "Gemini 2.5 Flash", "context_tokens": 1000000, "input_cost_per_1m": 0.075, "output_cost_per_1m": 0.3},
    "gemini-2.0-flash": {"display_name": "Gemini 2.0 Flash", "context_tokens": 1000000, "input_cost_per_1m": 0.075, "output_cost_per_1m": 0.3},
}


def _messages_to_contents(messages: List[Dict[str, str]], system_prompt: Optional[str]):
    """Returns `(contents, system_instruction_text_or_None)`."""
    contents = []
    system_parts: List[str] = [system_prompt] if system_prompt else []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "") or ""
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})
    system_text = "\n\n".join(p for p in system_parts if p) or None
    return contents, system_text


class GeminiProvider(LLMProviderClient):
    name = "gemini"

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, config: ProviderConfig, session: Optional[Any] = None, sleep_fn: Optional[Any] = None) -> None:
        self.config = config
        self._session = session
        self._sleep = sleep_fn or time.sleep
        self._lock = threading.Lock()
        self._inflight_responses: Dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    def initialize(self) -> None:
        if not self.config.api_key:
            raise ProviderNotConfiguredError("gemini: no API key configured (GEMINI_API_KEY)")
        if self._session is None:
            if _requests is None:  # pragma: no cover
                raise ProviderNotConfiguredError("gemini: the 'requests' package is required")
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
        return True  # 2.5 series "thinking" models

    def supports_long_context(self) -> bool:
        return True  # 1M-token context window

    def get_model_info(self, model: Optional[str] = None) -> ModelInfo:
        model_id = model or self.config.model or "gemini-2.5-flash"
        entry = _MODEL_CATALOG.get(model_id, {})
        return ModelInfo(
            id=model_id, provider=self.name, display_name=entry.get("display_name", model_id),
            capabilities=ProviderCapabilities(
                streaming=True, tools=True, images=True, vision=True, reasoning=True,
                long_context=True, max_context_tokens=entry.get("context_tokens", 1000000),
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
            resp = self._session.get(
                f"{cfg.base_url}/models", headers=self._headers(), timeout=min(cfg.timeout_s, 10.0),
            )
            latency_ms = (time.time() - t0) * 1000
            if resp.status_code == 200:
                return ProviderHealth(self.name, HealthState.HEALTHY, "reachable", latency_ms=latency_ms)
            if resp.status_code in (401, 403):
                return ProviderHealth(self.name, HealthState.AUTH_FAILED, _error_message(resp), latency_ms=latency_ms)
            if resp.status_code == 429:
                return ProviderHealth(self.name, HealthState.RATE_LIMITED, _error_message(resp), latency_ms=latency_ms)
            return ProviderHealth(self.name, HealthState.WARNING, f"HTTP {resp.status_code}", latency_ms=latency_ms)
        except getattr(_requests, "Timeout", TimeoutError):
            return ProviderHealth(self.name, HealthState.TIMEOUT, "health check timed out", latency_ms=(time.time() - t0) * 1000)
        except Exception as ex:
            return ProviderHealth(self.name, HealthState.OFFLINE, str(ex), latency_ms=(time.time() - t0) * 1000)

    # -- request building ---------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        with self._lock:
            cfg = self.config
        return {"Content-Type": "application/json", "x-goog-api-key": cfg.api_key}

    def _post(self, url: str, body: Dict[str, Any], *, stream: bool, timeout_s: float) -> Any:
        try:
            return self._session.post(url, json=body, headers=self._headers(), timeout=timeout_s, stream=stream)
        except getattr(_requests, "Timeout", TimeoutError) as ex:
            raise ProviderTimeoutError(f"request timed out: {ex}") from ex
        except Exception as ex:
            if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                raise ProviderNetworkError(f"network error: {ex}") from ex
            raise

    def _body(self, contents, system_text, temperature, max_tokens) -> Dict[str, Any]:
        with self._lock:
            cfg = self.config
        body: Dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        gen_config: Dict[str, Any] = {}
        temp = temperature if temperature is not None else cfg.temperature
        if temp is not None:
            gen_config["temperature"] = temp
        tokens = max_tokens if max_tokens is not None else cfg.max_tokens
        if tokens is not None:
            gen_config["maxOutputTokens"] = tokens
        if gen_config:
            body["generationConfig"] = gen_config
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
            raise ProviderNotConfiguredError("gemini: no model specified and no default configured")
        contents, system_text = _messages_to_contents(messages, system_prompt)
        body = self._body(contents, system_text, temperature, max_tokens)
        t0 = time.time()

        def _attempt() -> ChatResult:
            url = f"{cfg.base_url}/models/{model}:generateContent"
            resp = self._post(url, body, stream=False, timeout_s=cfg.timeout_s)
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, _error_message(resp))
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError) as ex:
                raise ProviderStreamError(f"malformed JSON response: {ex}") from ex
            try:
                candidate = data["candidates"][0]
                parts = candidate.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                finish_reason = candidate.get("finishReason")
            except (KeyError, IndexError, TypeError) as ex:
                raise ProviderStreamError(f"unexpected response shape: {ex}") from ex
            usage = data.get("usageMetadata") or {}
            return ChatResult(
                text=text, model=model, provider=self.name, raw=data,
                prompt_tokens=usage.get("promptTokenCount"), completion_tokens=usage.get("candidatesTokenCount"),
                total_tokens=usage.get("totalTokenCount"), finish_reason=finish_reason,
                latency_ms=(time.time() - t0) * 1000,
            )

        return retry_call(_attempt, cfg, log_prefix=f"gemini.chat[{model}]", sleep_fn=self._sleep)

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
            raise ProviderNotConfiguredError("gemini: no model specified and no default configured")
        contents, system_text = _messages_to_contents(messages, system_prompt)
        body = self._body(contents, system_text, temperature, max_tokens)

        def _open():
            url = f"{cfg.base_url}/models/{model}:streamGenerateContent?alt=sse"
            resp = self._post(url, body, stream=True, timeout_s=cfg.timeout_s)
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, _error_message(resp))
            return resp

        response = retry_call(_open, cfg, log_prefix=f"gemini.stream_chat[{model}]", sleep_fn=self._sleep)
        if request_id:
            with self._lock:
                self._inflight_responses[request_id] = response
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
                if obj.get("error"):
                    raise ProviderStreamError(f"mid-stream error from gemini: {obj['error']}")
                candidates = obj.get("candidates") or []
                delta_text, finish_reason = "", None
                if candidates:
                    candidate = candidates[0]
                    parts = candidate.get("content", {}).get("parts", [])
                    delta_text = "".join(p.get("text", "") for p in parts)
                    finish_reason = candidate.get("finishReason")
                usage = obj.get("usageMetadata") or {}
                if finish_reason:
                    yield LLMStreamChunk(
                        delta=delta_text, finished=True, finish_reason=finish_reason, raw=obj,
                        prompt_tokens=usage.get("promptTokenCount"), completion_tokens=usage.get("candidatesTokenCount"),
                        total_tokens=usage.get("totalTokenCount"),
                    )
                    return
                if delta_text:
                    yield LLMStreamChunk(delta=delta_text, raw=obj)
            # stream ended without an explicit finishReason chunk - still
            # signal completion so callers never hang waiting for `finished`.
            yield LLMStreamChunk(delta="", finished=True, finish_reason="stop")
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
