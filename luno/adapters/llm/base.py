"""
base.py
=======

`LLMProviderClient` - the seam every one of the five providers
implements, and the ONLY surface `LLMManagerAdapter` ever calls. Exact
method set the sprint spec asks for: `initialize()`, `shutdown()`,
`health()`, `chat()`, `stream_chat()`, `cancel()`, `reload()`,
`supports_streaming()`, `supports_tools()`, `supports_images()`,
`supports_reasoning()`, `get_model_info()`. No provider-specific logic
lives outside a concrete subclass of this - `LLMManagerAdapter` never
special-cases "if provider == 'gemini'" anywhere.

Also home to two things every OpenAI-compatible provider (OpenRouter,
OpenAI, Local) shares byte-for-byte: `_retry()` (retry/backoff loop,
generalized from `luno.adapters.openrouter`'s original) and
`OpenAICompatibleClient` (the actual `POST /chat/completions` request/
SSE-parsing logic - written once here, not tripled across three
modules). Gemini and Anthropic do NOT subclass `OpenAICompatibleClient`
- their wire formats are different enough that a shared base would be
more indirection than the ~150 lines it would save; they implement
`LLMProviderClient` directly instead (see their own modules).
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional

from ... import config
from .errors import (
    ProviderAPIError,
    ProviderNetworkError,
    ProviderNotConfiguredError,
    ProviderStreamError,
    ProviderTimeoutError,
    classify_http_status,
)
from .models import ChatResult, HealthState, LLMStreamChunk, ModelInfo, ProviderCapabilities, ProviderConfig, ProviderHealth
from ..utils import log

try:
    import requests as _requests
except ImportError:  # pragma: no cover - requests is an existing project dependency
    _requests = None


# ============================================================================
# Provider interface
# ============================================================================

class LLMProviderClient(ABC):
    """One instance = one configured provider connection. `name` is the
    stable provider id used everywhere (config, events, Dashboard, the
    priority list) - e.g. `"openrouter"`, `"openai"`, `"gemini"`,
    `"anthropic"`, `"local"`."""

    name: str = ""

    # -- lifecycle ------------------------------------------------------------

    @abstractmethod
    def initialize(self) -> None:
        """Validate configuration and set up whatever the transport
        needs (HTTP session, etc.) - never a network call to the
        provider itself (that's what `health()` is for). Raises
        `ProviderNotConfiguredError` if this provider has no usable
        credentials/base URL at all, so `LLMManagerAdapter` can skip it
        during provider selection without spending a real request
        finding that out."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release the transport (close the HTTP session, ...). Never
        raises - best-effort cleanup only."""

    # -- health / capabilities --------------------------------------------------

    @abstractmethod
    def health(self) -> ProviderHealth:
        """A live (but cheap) reachability/auth check - never a full
        `chat()` call. Implementations should prefer the provider's
        lightest available endpoint (models list, etc.) over anything
        that would burn real usage."""

    @abstractmethod
    def supports_streaming(self) -> bool: ...

    @abstractmethod
    def supports_tools(self) -> bool: ...

    @abstractmethod
    def supports_images(self) -> bool: ...

    @abstractmethod
    def supports_reasoning(self) -> bool: ...

    def supports_vision(self) -> bool:
        return self.supports_images()

    def supports_audio(self) -> bool:
        return False

    def supports_long_context(self) -> bool:
        return False

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        """Convenience roll-up of the five `supports_*()` calls plus
        `get_model_info()`'s context length - what the Dashboard's
        "Provider Capabilities" panel and Planner actually consume,
        rather than calling each `supports_*()` separately."""
        info = self.get_model_info(model)
        return ProviderCapabilities(
            streaming=self.supports_streaming(), tools=self.supports_tools(),
            images=self.supports_images(), vision=self.supports_vision(),
            reasoning=self.supports_reasoning(), audio=self.supports_audio(),
            long_context=self.supports_long_context(),
            max_context_tokens=info.capabilities.max_context_tokens if info else None,
        )

    @abstractmethod
    def get_model_info(self, model: Optional[str] = None) -> ModelInfo:
        """`model=None` describes this client's currently-configured
        default model."""

    def list_models(self) -> List[ModelInfo]:
        """The provider's exposed model catalog (spec's "Model
        Selection" section) - default: just the configured default
        model. Providers with a richer static catalog (see each
        module's `_MODEL_CATALOG`) override this."""
        return [self.get_model_info(None)]

    # -- chat ---------------------------------------------------------------

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Blocks until the full response is available. Raises a
        `ProviderAPIError` subclass on failure - never a bare/uncaught
        exception type `LLMManagerAdapter` can't classify."""

    @abstractmethod
    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        request_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        """Yields `LLMStreamChunk`s as they arrive. MUST check
        `cancel_event` (if given) between chunks and tear its own
        connection down as soon as it's set. If this provider doesn't
        support streaming at all (`supports_streaming()` is False),
        implementations fall back internally to one `chat()` call and
        yield its text as a single non-`finished` chunk followed by a
        `finished=True` one - `LLMManagerAdapter` never has to know the
        difference (spec: "If not supported: Fallback automatically to
        normal completion")."""

    def cancel(self, request_id: str) -> bool:
        """Best-effort mid-flight cancellation for a `chat()`/
        `stream_chat()` call started with this `request_id`. Streaming
        cancellation is already fully handled by the caller setting
        `cancel_event` (the primary, always-reliable mechanism); this
        exists for the spec's explicit `cancel()` method and closes the
        underlying connection if this client is still tracking one for
        `request_id`, so the provider's own socket is torn down instead
        of just being ignored client-side. Default: no-op (returns
        `False`, "nothing to cancel") - overridden by clients that
        track in-flight connections (see `OpenAICompatibleClient`)."""
        return False

    def reload(self, config: ProviderConfig) -> None:
        """Re-reads config (API key, base URL, timeout, ...) without
        being reconstructed. Default: no-op - a client with nothing
        reloadable doesn't need to override this."""


# ============================================================================
# Shared retry helper
# ============================================================================

def retry_call(attempt_fn, cfg: ProviderConfig, log_prefix: str, sleep_fn: Any = time.sleep):
    """Generalized form of `luno.adapters.openrouter._retry` - only
    ever retries `ProviderAPIError` subclasses with `retryable=True`
    (429/5xx/timeout/network). Auth and invalid-request errors raise on
    the first attempt. `sleep_fn` is injectable so retry-backoff tests
    don't have to actually wait out real exponential delays."""
    attempts = max(1, cfg.max_retries + 1)
    last_ex: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return attempt_fn()
        except ProviderAPIError as ex:
            last_ex = ex
            if not ex.retryable or attempt >= attempts:
                raise
            backoff = min(cfg.retry_backoff_max_s, cfg.retry_backoff_base_s * (2 ** (attempt - 1)))
            log(f"{log_prefix}: attempt {attempt}/{attempts} failed ({ex}) - retrying in {backoff:.1f}s", "llm")
            sleep_fn(backoff)
    if last_ex is not None:
        raise last_ex
    raise RuntimeError("unreachable: retry_call exhausted with no exception recorded")


def _error_message(resp: Any) -> str:
    try:
        body = resp.json()
        return str(((body or {}).get("error") or {}).get("message") or body)
    except Exception:
        try:
            return str(resp.text)[:500]
        except Exception:
            return f"HTTP {getattr(resp, 'status_code', '?')}"


# ============================================================================
# Shared base for OpenAI-compatible REST providers (OpenRouter/OpenAI/Local)
# ============================================================================

class OpenAICompatibleClient(LLMProviderClient):
    """`POST {base_url}/chat/completions`, OpenAI's request/response
    shape - what OpenRouter, the real OpenAI API, and every "OpenAI-
    compatible" local server (LM Studio, Ollama's OpenAI-compat
    endpoint, vLLM, OpenWebUI, ...) all speak. Subclasses only need to
    supply `name`, the default `base_url`, an auth-header builder, and
    a capability/model table - see `openrouter_provider.py`/
    `openai_provider.py`/`local_provider.py` for how small that leaves
    each one.

    `session`/`sleep_fn` injectable exactly like
    `RequestsOpenRouterClient` - tests hand in a fake
    `requests.Session`-like object and exercise real HTTP mechanics
    (retry/backoff, status classification, SSE parsing) with zero
    network access.
    """

    #: subclasses override - capability defaults for THIS provider.
    _SUPPORTS_STREAMING = True
    _SUPPORTS_TOOLS = True
    _SUPPORTS_IMAGES = False
    _SUPPORTS_REASONING = False

    def __init__(self, config: ProviderConfig, session: Optional[Any] = None, sleep_fn: Optional[Any] = None) -> None:
        self.config = config
        self._session = session
        self._sleep = sleep_fn or time.sleep
        self._lock = threading.Lock()
        self._inflight_responses: Dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    def initialize(self) -> None:
        if not self.config.api_key and self._requires_api_key():
            raise ProviderNotConfiguredError(f"{self.name}: no API key configured")
        if not self.config.base_url:
            raise ProviderNotConfiguredError(f"{self.name}: no base_url configured")
        if self._session is None:
            if _requests is None:  # pragma: no cover
                raise ProviderNotConfiguredError(f"{self.name}: the 'requests' package is required")
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

    def _requires_api_key(self) -> bool:
        """Local (LM Studio/Ollama/vLLM/...) servers usually run with no
        auth at all - overridden to `False` by `local_provider.py`."""
        return True

    # -- capabilities -----------------------------------------------------------

    def supports_streaming(self) -> bool:
        return self._SUPPORTS_STREAMING

    def supports_tools(self) -> bool:
        return self._SUPPORTS_TOOLS

    def supports_images(self) -> bool:
        return self._SUPPORTS_IMAGES

    def supports_reasoning(self) -> bool:
        return self._SUPPORTS_REASONING

    # -- health -----------------------------------------------------------------

    def health(self) -> ProviderHealth:
        t0 = time.time()
        try:
            with self._lock:
                cfg = self.config
            resp = self._session.get(f"{cfg.base_url}/models", headers=self._headers(), timeout=min(cfg.timeout_s, 10.0))
            latency_ms = (time.time() - t0) * 1000
            if resp.status_code == 200:
                return ProviderHealth(self.name, HealthState.HEALTHY, "reachable", latency_ms=latency_ms)
            if resp.status_code in (401, 403):
                return ProviderHealth(self.name, HealthState.AUTH_FAILED, _error_message(resp), latency_ms=latency_ms)
            if resp.status_code == 429:
                return ProviderHealth(self.name, HealthState.RATE_LIMITED, _error_message(resp), latency_ms=latency_ms)
            return ProviderHealth(self.name, HealthState.WARNING, f"HTTP {resp.status_code}: {_error_message(resp)}", latency_ms=latency_ms)
        except getattr(_requests, "Timeout", TimeoutError):
            return ProviderHealth(self.name, HealthState.TIMEOUT, "health check timed out", latency_ms=(time.time() - t0) * 1000)
        except Exception as ex:
            return ProviderHealth(self.name, HealthState.OFFLINE, str(ex), latency_ms=(time.time() - t0) * 1000)

    # -- request building ---------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        with self._lock:
            cfg = self.config
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        return headers

    def _payload(
        self, model: str, messages: List[Dict[str, str]], system_prompt: Optional[str],
        temperature: Optional[float], max_tokens: Optional[int], stream: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            cfg = self.config
        msgs = list(messages)
        if system_prompt:
            msgs = [{"role": "system", "content": system_prompt}] + msgs
        body: Dict[str, Any] = {"model": model, "messages": msgs, "stream": stream}
        temp = temperature if temperature is not None else cfg.temperature
        if temp is not None:
            body["temperature"] = temp
        # Sprint 54 - the completion-length JSON key name is model-
        # dependent (older OpenAI-family models want `max_tokens`,
        # newer ones reject it with "Unsupported parameter: 'max_tokens'
        # ... Use 'max_completion_tokens' instead." and want that key
        # name instead) - reuses the SAME `config.MAX_TOKENS_PARAM`
        # abstraction `luno/main.py`'s legacy call sites and Sprint 53's
        # `luno/adapters/openrouter.py` fix already use, instead of
        # hardcoding either literal name here. This is the base request
        # builder shared by every `OpenAICompatibleClient` subclass
        # (`OpenRouterProvider`, `OpenAIProvider`, `LocalProvider`) via
        # both `chat()` and `stream_chat()` - Gemini/Anthropic do NOT go
        # through this method (their own wire formats are different;
        # Anthropic's `"max_tokens"` is its own correct, required field
        # name, not this same incompatibility, and must not change). See
        # docs/change_impact/llm_max_completion_tokens_compatibility.md
        # for the root cause this fixed.
        tokens = max_tokens if max_tokens is not None else cfg.max_tokens
        if tokens is not None:
            body[config.MAX_TOKENS_PARAM] = tokens
        extra = self._extra_payload_fields(metadata)
        if extra:
            body.update(extra)
        return body

    def _extra_payload_fields(self, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Hook for a subclass to inject extra provider-specific JSON body
        fields driven by per-request `metadata` (e.g. OpenAI's
        `reasoning_effort` - see `openai_provider.py`). Default: nothing -
        only a provider that actually understands a given field should
        override this, so an OpenAI-only key never leaks into an
        OpenRouter/Local request body just because it happened to be
        present in `metadata` for an unrelated reason."""
        return {}

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
            raise ProviderNotConfiguredError(f"{self.name}: no model specified and no default configured")
        payload = self._payload(model, messages, system_prompt, temperature, max_tokens, stream=False, metadata=metadata)
        t0 = time.time()

        def _attempt() -> ChatResult:
            resp = self._post(cfg, payload, stream=False)
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, _error_message(resp))
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError) as ex:
                raise ProviderStreamError(f"malformed JSON response: {ex}") from ex
            try:
                choice = data["choices"][0]
                text = choice["message"]["content"] or ""
                finish_reason = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError) as ex:
                raise ProviderStreamError(f"unexpected response shape: {ex}") from ex
            usage = data.get("usage") or {}
            return ChatResult(
                text=text, model=data.get("model", model), provider=self.name, raw=data,
                prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"), finish_reason=finish_reason,
                latency_ms=(time.time() - t0) * 1000,
            )

        return retry_call(_attempt, cfg, log_prefix=f"{self.name}.chat[{model}]", sleep_fn=self._sleep)

    def _post(self, cfg: ProviderConfig, payload: Dict[str, Any], stream: bool) -> Any:
        try:
            return self._session.post(
                f"{cfg.base_url}/chat/completions", json=payload, headers=self._headers(),
                timeout=cfg.timeout_s, stream=stream,
            )
        except getattr(_requests, "Timeout", TimeoutError) as ex:
            raise ProviderTimeoutError(f"request timed out: {ex}") from ex
        except Exception as ex:
            if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                raise ProviderNetworkError(f"network error: {ex}") from ex
            raise

    def stream_chat(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None, request_id: Optional[str] = None, **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        if not self.supports_streaming():
            result = self.chat(
                messages, model=model, system_prompt=system_prompt, temperature=temperature,
                max_tokens=max_tokens, metadata=metadata, request_id=request_id,
            )
            yield LLMStreamChunk(delta=result.text)
            yield LLMStreamChunk(
                delta="", finished=True, finish_reason=result.finish_reason,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
            )
            return

        with self._lock:
            cfg = self.config
        model = model or cfg.model
        if not model:
            raise ProviderNotConfiguredError(f"{self.name}: no model specified and no default configured")
        payload = self._payload(model, messages, system_prompt, temperature, max_tokens, stream=True, metadata=metadata)

        def _open():
            resp = self._post(cfg, payload, stream=True)
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, _error_message(resp))
            return resp

        response = retry_call(_open, cfg, log_prefix=f"{self.name}.stream_chat[{model}]", sleep_fn=self._sleep)
        if request_id:
            with self._lock:
                self._inflight_responses[request_id] = response
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not raw_line or raw_line.startswith(":"):
                    continue
                if not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[len("data:"):].strip()
                if data_str == "[DONE]":
                    return
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError as ex:
                    raise ProviderStreamError(f"malformed SSE chunk: {ex}") from ex
                if obj.get("error"):
                    raise ProviderStreamError(f"mid-stream error from {self.name}: {obj['error']}")
                choices = obj.get("choices") or []
                delta_text, finish_reason = "", None
                if choices:
                    choice = choices[0]
                    delta_text = (choice.get("delta") or {}).get("content") or ""
                    finish_reason = choice.get("finish_reason")
                usage = obj.get("usage") or {}
                if finish_reason:
                    yield LLMStreamChunk(
                        delta=delta_text, finished=True, finish_reason=finish_reason, raw=obj,
                        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                        total_tokens=usage.get("total_tokens"),
                    )
                    return
                if delta_text:
                    yield LLMStreamChunk(delta=delta_text, raw=obj)
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

    # -- model info ---------------------------------------------------------------

    def get_model_info(self, model: Optional[str] = None) -> ModelInfo:
        model_id = model or self.config.model or "unknown"
        catalog = getattr(self, "_MODEL_CATALOG", {})
        entry = catalog.get(model_id, {})
        return ModelInfo(
            id=model_id, provider=self.name, display_name=entry.get("display_name", model_id),
            capabilities=ProviderCapabilities(
                streaming=self.supports_streaming(), tools=self.supports_tools(), images=self.supports_images(),
                vision=self.supports_images(), reasoning=self.supports_reasoning(),
                max_context_tokens=entry.get("context_tokens"),
            ),
            input_cost_per_1m=entry.get("input_cost_per_1m"), output_cost_per_1m=entry.get("output_cost_per_1m"),
        )

    def list_models(self) -> List[ModelInfo]:
        catalog = getattr(self, "_MODEL_CATALOG", None)
        if not catalog:
            return [self.get_model_info(None)]
        return [self.get_model_info(model_id) for model_id in catalog]
