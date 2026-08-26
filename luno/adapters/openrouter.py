"""
openrouter.py
=============

`OpenRouterAdapter` - the production translator between the internal
Event Bus and OpenRouter's chat-completions API. It is a pure
translator, same as every other adapter in this package: it builds a
request from a `NeedLLMResponse` event, hands it to an injected
`OpenRouterClient`, and republishes whatever comes back as structured
Events. It never decides anything - no retries beyond the transport
layer's own resilience, no prompt rewriting, no routing logic. Business
logic (when to ask the LLM anything, what the system prompt should say,
what to do with the reply) lives in Behavior Tree / Context Builder,
upstream and downstream of this file respectively.

    NeedLLMResponse ---------> [build request] --------> OpenRouterClient
    CancelLLMRequest -----> [set cancel flag]  ---------> (client's own
    ReloadModel -----------> [re-read OPENROUTER_* env]     loop notices it,
    ConversationReset ----> [cancel matching requests]      tears its HTTP
                                                             connection down)
                                     |
              LLMStarted -> (LLMStreaming -> LLMChunk*) -> LLMFinished -> AssistantResponse
                                     |                                          ^
                                     +----------------------> LLMCancelled -----+
                                     +----------------------> LLMError ---------+

Two `OpenRouterClient` implementations ship here:

    `MockOpenRouterClient`      - no network, canned/scripted responses,
                                   used by every test in this package and
                                   by anything standing the Adapter Layer
                                   up without real credentials.
    `RequestsOpenRouterClient`  - the real one. Plain `requests` calls
                                   against OpenRouter's OpenAI-compatible
                                   REST API (the same library `main.py`
                                   already uses for its other HTTP
                                   integrations - no new dependency).
                                   Handles retry/backoff, timeouts, and
                                   both streaming (SSE) and non-streaming
                                   responses.

Swapping OpenRouter for a different provider later (LM Studio, Ollama,
vLLM, the raw OpenAI API, Anthropic's API, ...) means writing one more
`OpenRouterClient` implementation and constructing the adapter with it -
`OpenRouterAdapter` itself, every event type, `main.py`'s wiring, and
every other Core/adapter package are untouched. That's the entire point
of the interface living here instead of a concrete HTTP call living
inline in `handle_event()`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from .. import config
from .base import BaseAdapter
from .events import (
    AssistantResponse,
    CancelLLMRequest,
    ConversationReset,
    LLMCancelled,
    LLMChunk,
    LLMError,
    LLMFinished,
    LLMStarted,
    LLMStreaming,
    NeedLLMResponse,
    ReloadModel,
)
from .utils import elapsed_ms, log

try:
    import requests as _requests
except ImportError:  # pragma: no cover - requests is an existing project dependency
    _requests = None


# ============================================================================
# Data types
# ============================================================================

@dataclass
class LLMResponse:
    """Result of a non-streaming `chat_completion()` call."""
    text: str
    model: str
    raw: Any = None
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None


@dataclass
class StreamChunk:
    """One item yielded by `stream_chat_completion()`.

    `delta`    - newly arrived text (may be empty, e.g. for a
                 usage-only or finish-reason-only chunk).
    `finished` - True on the chunk that ends the stream (normal
                 completion). No more items follow after one with
                 `finished=True`.
    `finish_reason`/`usage` - only populated on (or by) the final chunk,
                 OpenAI/OpenRouter-style ("stop", "length", "error", ...).
    """
    delta: str = ""
    finished: bool = False
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    raw: Any = None


@dataclass
class OpenRouterConfig:
    """Every knob the spec calls for, read from `OPENROUTER_*`
    environment variables - never hardcoded, never baked into a
    constant. Constructed fresh on every `ReloadModel` event (see
    `OpenRouterAdapter._handle_reload_model`), which is how runtime
    reload / API-key rotation without restarting Luno is supported:
    rotate the key in the environment, publish `ReloadModel`, done.
    """
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: Optional[str] = None
    timeout_s: float = 60.0
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream_default: bool = True
    max_retries: int = 3
    retry_backoff_base_s: float = 1.0
    retry_backoff_max_s: float = 20.0
    debug_log_prompts: bool = False
    #: optional, purely cosmetic headers OpenRouter documents as
    #: recommended (app attribution) - never required, never printed
    #: as if they were secrets since they aren't.
    referer: Optional[str] = None
    app_title: Optional[str] = None

    @classmethod
    def from_env(cls) -> "OpenRouterConfig":
        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def _int(name: str, default: Optional[int]) -> Optional[int]:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        def _opt_float(name: str) -> Optional[float]:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            base_url=(os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").strip().rstrip("/"),
            model=(os.getenv("OPENROUTER_MODEL") or "").strip() or None,
            timeout_s=_float("OPENROUTER_TIMEOUT", 60.0),
            max_tokens=_int("OPENROUTER_MAX_TOKENS", None),
            temperature=_opt_float("OPENROUTER_TEMPERATURE"),
            stream_default=_bool("OPENROUTER_STREAM_DEFAULT", True),
            max_retries=_int("OPENROUTER_MAX_RETRIES", 3) or 0,
            retry_backoff_base_s=_float("OPENROUTER_RETRY_BACKOFF_S", 1.0),
            retry_backoff_max_s=_float("OPENROUTER_RETRY_BACKOFF_MAX_S", 20.0),
            debug_log_prompts=_bool("OPENROUTER_DEBUG_LOG_PROMPTS", False),
            referer=(os.getenv("OPENROUTER_REFERER") or "").strip() or None,
            app_title=(os.getenv("OPENROUTER_APP_TITLE") or "").strip() or None,
        )


# ============================================================================
# Errors - internal only. Never escape the adapter uncaught; every one of
# these is caught in `OpenRouterAdapter._run_request()` and turned into a
# structured `LLMError` event instead.
# ============================================================================

class OpenRouterAPIError(Exception):
    """Base class for every classified failure a client implementation
    can raise. `retryable` drives `RequestsOpenRouterClient`'s own
    retry loop; `status_code` is the HTTP status if there was one."""
    retryable: bool = False
    status_code: Optional[int] = None

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterAuthError(OpenRouterAPIError):
    """401/403 - never retried."""
    retryable = False


class OpenRouterInvalidRequestError(OpenRouterAPIError):
    """400/404/422-class client errors - never retried."""
    retryable = False


class OpenRouterRateLimitError(OpenRouterAPIError):
    """429 - retried with backoff."""
    retryable = True


class OpenRouterServerError(OpenRouterAPIError):
    """500/502/503/504 - retried with backoff."""
    retryable = True


class OpenRouterTimeoutError(OpenRouterAPIError):
    """Request exceeded `OPENROUTER_TIMEOUT`. Retried (a slow upstream
    provider on one attempt often succeeds on the next)."""
    retryable = True


class OpenRouterNetworkError(OpenRouterAPIError):
    """DNS/connection/socket-level failure. Retried."""
    retryable = True


class OpenRouterStreamError(OpenRouterAPIError):
    """Malformed SSE payload, or a mid-stream error chunk from
    OpenRouter itself (`choices[0].finish_reason == "error"`). NOT
    retried - by the time this can be raised, some chunks may already
    have been published as `LLMChunk` events, so silently retrying
    would double-speak partial output to whatever's already consuming
    the stream (e.g. Fish Audio)."""
    retryable = False


# ============================================================================
# Client interface
# ============================================================================

class OpenRouterClient(ABC):
    """The seam between this adapter and however requests actually get
    to OpenRouter. The adapter only ever calls these two methods; it
    never builds a URL, never touches an API key, never picks an HTTP
    library. Swap this one class out and the adapter needs zero changes."""

    @abstractmethod
    def chat_completion(
        self,
        model: str,
        messages: List[Dict[str, str]],
        *,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Blocks until the full response is available. Raises an
        `OpenRouterAPIError` subclass (never a bare/uncaught exception
        type the adapter can't classify) on failure."""

    @abstractmethod
    def stream_chat_completion(
        self,
        model: str,
        messages: List[Dict[str, str]],
        *,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        """Yields `StreamChunk`s as they arrive. Implementations MUST
        check `cancel_event` (if given) between chunks and tear down
        their own connection + stop yielding as soon as it's set - this
        is the actual mechanism behind "cancel the HTTP request if
        possible" from the spec. Raises an `OpenRouterAPIError` subclass
        on failure, same as `chat_completion()`."""

    def reload_config(self, config: OpenRouterConfig) -> None:
        """Optional hook - clients that hold live config (API key, base
        URL, timeout, ...) override this to pick up a fresh
        `OpenRouterConfig` without being reconstructed. Default: no-op
        (a client with nothing reloadable, e.g. the mock, doesn't need
        to override this)."""


# ============================================================================
# Mock client - no network. Used by every test in this package.
# ============================================================================

class MockOpenRouterClient(OpenRouterClient):
    """Canned/scripted responses for tests - no network calls.

    `canned_text`     - reply text (default: an echo of the last user
                         message).
    `fail`            - simulate a plain, non-classified failure.
    `fail_status`     - simulate an HTTP-status-classified failure
                         (401/403/400/404/422/429/500/502/503/504/408) -
                         raised via the same classification path
                         `RequestsOpenRouterClient` uses, so retry tests
                         exercise real adapter/client retry semantics.
    `fail_times`      - fail with `fail_status` this many calls in a
                         row, then succeed (retry-recovery scenarios).
    `network_error`   - simulate a connection-level failure instead of
                         an HTTP-status one (`OpenRouterNetworkError`).
    `timeout_error`    - simulate a request that always times out.
    `malformed`        - simulate a response that can't be parsed
                          (`OpenRouterStreamError`, mid-stream for
                          streaming calls, immediately for non-streaming).
    `delay_s`          - per-call latency, applied before the first
                          byte - also what a timeout test sets above
                          the adapter's configured timeout.
    `chunk_delay_s`    - delay between streamed chunks - what makes
                          cancellation-mid-stream tests deterministic.
    `usage`            - usage dict attached to the final chunk/response.
    """

    def __init__(
        self,
        canned_text: Optional[str] = None,
        fail: bool = False,
        fail_status: Optional[int] = None,
        fail_times: int = 1,
        network_error: bool = False,
        timeout_error: bool = False,
        malformed: bool = False,
        delay_s: float = 0.0,
        chunk_delay_s: float = 0.0,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.canned_text = canned_text
        self.fail = fail
        self.fail_status = fail_status
        self.fail_times = fail_times
        self.network_error = network_error
        self.timeout_error = timeout_error
        self.malformed = malformed
        self.delay_s = delay_s
        self.chunk_delay_s = chunk_delay_s
        self.usage = usage
        self.calls: List[Dict[str, Any]] = []
        self._attempts = 0
        self._lock = threading.Lock()

    def _resolve_text(self, messages: List[Dict[str, str]]) -> str:
        if self.canned_text is not None:
            return self.canned_text
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        return f"(mock reply to: {last_user})"

    def _maybe_raise(self) -> None:
        with self._lock:
            self._attempts += 1
            attempt = self._attempts
        if self.network_error:
            raise OpenRouterNetworkError("mock: connection failed")
        if self.timeout_error:
            raise OpenRouterTimeoutError("mock: request timed out")
        if self.fail_status is not None and attempt <= self.fail_times:
            raise _classify_status(self.fail_status, f"mock: HTTP {self.fail_status}")
        if self.fail:
            raise RuntimeError("mock OpenRouter call failed")

    def chat_completion(
        self, model: str, messages: List[Dict[str, str]], *,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append({
            "model": model, "messages": messages, "system_prompt": system_prompt,
            "temperature": temperature, "max_tokens": max_tokens, "metadata": metadata, "stream": False,
        })
        if self.delay_s:
            time.sleep(self.delay_s)
        self._maybe_raise()
        if self.malformed:
            raise OpenRouterStreamError("mock: malformed response body")
        return LLMResponse(text=self._resolve_text(messages), model=model, raw={"mock": True}, usage=self.usage, finish_reason="stop")

    def stream_chat_completion(
        self, model: str, messages: List[Dict[str, str]], *,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None, **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        self.calls.append({
            "model": model, "messages": messages, "system_prompt": system_prompt,
            "temperature": temperature, "max_tokens": max_tokens, "metadata": metadata, "stream": True,
        })
        if self.delay_s:
            time.sleep(self.delay_s)
        self._maybe_raise()
        text = self._resolve_text(messages)
        words = text.split(" ")
        for i, word in enumerate(words):
            if cancel_event is not None and cancel_event.is_set():
                return
            if self.malformed and i == max(len(words) // 2, 1):
                raise OpenRouterStreamError("mock: malformed chunk mid-stream")
            piece = word if i == 0 else " " + word
            yield StreamChunk(delta=piece)
            if self.chunk_delay_s:
                time.sleep(self.chunk_delay_s)
        if cancel_event is not None and cancel_event.is_set():
            return
        yield StreamChunk(delta="", finished=True, finish_reason="stop", usage=self.usage)


def _classify_status(status_code: int, message: str) -> OpenRouterAPIError:
    if status_code in (401, 403):
        return OpenRouterAuthError(message, status_code=status_code)
    if status_code == 429:
        return OpenRouterRateLimitError(message, status_code=status_code)
    if status_code in (500, 502, 503, 504):
        return OpenRouterServerError(message, status_code=status_code)
    if status_code == 408:
        return OpenRouterTimeoutError(message, status_code=status_code)
    return OpenRouterInvalidRequestError(message, status_code=status_code)


# ============================================================================
# Real client - plain `requests` against OpenRouter's REST API.
# ============================================================================

class RequestsOpenRouterClient(OpenRouterClient):
    """Talks to `POST {base_url}/chat/completions` - OpenRouter's
    OpenAI-compatible endpoint, works with any model string OpenRouter
    routes (`openai/...`, `anthropic/...`, `google/...`, `deepseek/...`,
    `qwen/...`, `mistralai/...`, `meta-llama/...`, `google/gemma-...`,
    whatever - the model is just a string field in the JSON body, never
    special-cased here).

    `session` is injectable so tests can hand in a fake
    `requests.Session`-like object (`.post(url, ...) -> fake Response`
    with `.status_code`/`.json()`/`.iter_lines()`/`.close()`) and
    exercise this exact class - real HTTP mechanics, retry/backoff,
    status classification, SSE parsing - without any network access.
    `sleep_fn` is injectable for the same reason (retry-backoff tests
    don't have to actually wait).
    """

    def __init__(
        self,
        config: Optional[OpenRouterConfig] = None,
        session: Optional[Any] = None,
        sleep_fn: Optional[Any] = None,
    ) -> None:
        if _requests is None and session is None:  # pragma: no cover
            raise RuntimeError("the 'requests' package is required for RequestsOpenRouterClient (or inject a session)")
        self.config = config or OpenRouterConfig.from_env()
        self._session = session or _requests.Session()
        self._sleep = sleep_fn or time.sleep
        self._lock = threading.Lock()

    def reload_config(self, config: OpenRouterConfig) -> None:
        with self._lock:
            self.config = config

    # -- request building ---------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        with self._lock:
            cfg = self.config
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        if cfg.referer:
            headers["HTTP-Referer"] = cfg.referer
        if cfg.app_title:
            headers["X-Title"] = cfg.app_title
        return headers

    def _payload(
        self, model: str, messages: List[Dict[str, str]], system_prompt: Optional[str],
        temperature: Optional[float], max_tokens: Optional[int], stream: bool, **extra: Any,
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
        # Sprint 53 - the completion-length JSON key name is model-
        # dependent (older OpenAI-family models want `max_tokens`,
        # newer ones reject it with "Unsupported parameter: 'max_tokens'
        # ... Use 'max_completion_tokens' instead." and want that key
        # name instead) - reuses the SAME `config.MAX_TOKENS_PARAM`
        # abstraction `luno/main.py`'s legacy OpenAI-SDK call sites
        # already use for exactly this, instead of hardcoding either
        # literal name here. See docs/change_impact/
        # memory_session_summary_api_compatibility.md for the root
        # cause this fixed (Session Summary was the only caller of this
        # adapter that ever passed a non-None `max_tokens`, since no
        # `NeedLLMResponse` publisher and no default `OPENROUTER_MAX_
        # TOKENS` env value populate this for normal chat).
        tokens = max_tokens if max_tokens is not None else cfg.max_tokens
        if tokens is not None:
            body[config.MAX_TOKENS_PARAM] = tokens
        for key, value in extra.items():
            if value is not None:
                body[key] = value
        return body

    # -- non-streaming --------------------------------------------------------

    def chat_completion(
        self, model: str, messages: List[Dict[str, str]], *,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any,
    ) -> LLMResponse:
        with self._lock:
            cfg = self.config
        payload = self._payload(model, messages, system_prompt, temperature, max_tokens, stream=False)

        def _attempt() -> LLMResponse:
            try:
                resp = self._session.post(
                    f"{cfg.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=cfg.timeout_s,
                )
            except getattr(_requests, "Timeout", TimeoutError) as ex:
                raise OpenRouterTimeoutError(f"request timed out: {ex}") from ex
            except Exception as ex:  # connection refused, DNS failure, etc.
                if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                    raise OpenRouterNetworkError(f"network error: {ex}") from ex
                raise

            if resp.status_code >= 400:
                raise _classify_status(resp.status_code, _error_message(resp))

            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError) as ex:
                raise OpenRouterStreamError(f"malformed JSON response: {ex}") from ex

            try:
                choice = data["choices"][0]
                text = choice["message"]["content"] or ""
                finish_reason = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError) as ex:
                raise OpenRouterStreamError(f"unexpected response shape: {ex}") from ex

            return LLMResponse(
                text=text, model=data.get("model", model), raw=data,
                usage=data.get("usage"), finish_reason=finish_reason,
            )

        return _retry(_attempt, cfg, log_prefix=f"chat_completion[{model}]", sleep_fn=self._sleep)

    # -- streaming --------------------------------------------------------------

    def stream_chat_completion(
        self, model: str, messages: List[Dict[str, str]], *,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None, **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        with self._lock:
            cfg = self.config
        payload = self._payload(model, messages, system_prompt, temperature, max_tokens, stream=True)

        def _open_connection():
            try:
                resp = self._session.post(
                    f"{cfg.base_url}/chat/completions", json=payload, headers=self._headers(),
                    timeout=cfg.timeout_s, stream=True,
                )
            except getattr(_requests, "Timeout", TimeoutError) as ex:
                raise OpenRouterTimeoutError(f"request timed out: {ex}") from ex
            except Exception as ex:
                if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                    raise OpenRouterNetworkError(f"network error: {ex}") from ex
                raise
            if resp.status_code >= 400:
                raise _classify_status(resp.status_code, _error_message(resp))
            return resp

        # Retries only ever cover "get a connection that starts producing
        # chunks" - once a real content delta has been yielded downstream
        # (and very possibly already published as an LLMChunk event), a
        # failure is terminal (OpenRouterStreamError, not retried) - see
        # that class's docstring.
        response = _retry(_open_connection, cfg, log_prefix=f"stream_chat_completion[{model}]", sleep_fn=self._sleep)

        try:
            model_used = model
            for raw_line in response.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not raw_line:
                    continue
                if raw_line.startswith(":"):
                    continue  # SSE keep-alive comment (e.g. "OPENROUTER PROCESSING") - ignore per spec
                if not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[len("data:"):].strip()
                if data_str == "[DONE]":
                    return
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError as ex:
                    raise OpenRouterStreamError(f"malformed SSE chunk: {ex}") from ex

                if obj.get("error"):
                    raise OpenRouterStreamError(f"mid-stream error from OpenRouter: {obj['error']}")

                model_used = obj.get("model", model_used)
                choices = obj.get("choices") or []
                delta_text = ""
                finish_reason = None
                if choices:
                    choice = choices[0]
                    delta_text = (choice.get("delta") or {}).get("content") or ""
                    finish_reason = choice.get("finish_reason")
                usage = obj.get("usage")

                if finish_reason:
                    yield StreamChunk(delta=delta_text, finished=True, finish_reason=finish_reason, usage=usage, raw=obj)
                    return
                if delta_text:
                    yield StreamChunk(delta=delta_text, raw=obj)
        finally:
            try:
                response.close()
            except Exception:
                pass


def _error_message(resp: Any) -> str:
    try:
        body = resp.json()
        return str(((body or {}).get("error") or {}).get("message") or body)
    except Exception:
        try:
            return str(resp.text)[:500]
        except Exception:
            return f"HTTP {getattr(resp, 'status_code', '?')}"


def _retry(attempt_fn, cfg: OpenRouterConfig, log_prefix: str, sleep_fn: Any = time.sleep):
    """Shared retry/backoff wrapper for both the streaming and
    non-streaming paths. Only ever retries `OpenRouterAPIError`
    subclasses with `retryable=True` (429/500/502/503/504 and
    connection/timeout failures) - auth and invalid-request errors
    raise on the first attempt, no exceptions swallowed silently.
    `sleep_fn` is injectable so retry-backoff tests don't have to
    actually wait out real exponential delays."""
    attempts = max(1, cfg.max_retries + 1)
    last_ex: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return attempt_fn()
        except OpenRouterAPIError as ex:
            last_ex = ex
            if not ex.retryable or attempt >= attempts:
                raise
            backoff = min(cfg.retry_backoff_max_s, cfg.retry_backoff_base_s * (2 ** (attempt - 1)))
            log(f"{log_prefix}: attempt {attempt}/{attempts} failed ({ex}) - retrying in {backoff:.1f}s", "openrouter")
            sleep_fn(backoff)
    if last_ex is not None:
        raise last_ex
    raise RuntimeError("unreachable: _retry exhausted with no exception recorded")


# ============================================================================
# Adapter
# ============================================================================

@dataclass
class _InFlight:
    request_id: str
    conversation_id: Optional[str]
    correlation_id: Optional[str]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False


class OpenRouterAdapter(BaseAdapter):
    name = "openrouter"

    def __init__(
        self,
        client: Optional[OpenRouterClient] = None,
        default_model: Optional[str] = None,
        config: Optional[OpenRouterConfig] = None,
        request_workers: int = 4,
    ) -> None:
        super().__init__()
        self.config = config or OpenRouterConfig.from_env()
        self.client = client or self._build_default_client()
        #: explicit constructor override always wins over config/env,
        #: same precedence the original mock adapter documented.
        self.default_model = default_model or self.config.model
        self._request_workers = max(1, request_workers)
        self._request_executor: Optional[ThreadPoolExecutor] = None
        self._inflight: Dict[str, _InFlight] = {}
        self._inflight_lock = threading.RLock()

    def _build_default_client(self) -> OpenRouterClient:
        if self.config.api_key and _requests is not None:
            return RequestsOpenRouterClient(self.config)
        return MockOpenRouterClient()

    # -- lifecycle ----------------------------------------------------------

    def _do_start(self) -> None:
        self._request_executor = ThreadPoolExecutor(
            max_workers=self._request_workers, thread_name_prefix="luno-openrouter-req",
        )

    def _do_stop(self) -> None:
        with self._inflight_lock:
            for entry in self._inflight.values():
                entry.cancel_event.set()
            self._inflight.clear()
        pool, self._request_executor = self._request_executor, None
        if pool is not None:
            pool.shutdown(wait=False)

    # -- event dispatch -------------------------------------------------------

    def handle_event(self, event: Any) -> None:
        if event.type == NeedLLMResponse.EVENT_TYPE:
            self._handle_need_llm_response(event)
        elif event.type == CancelLLMRequest.EVENT_TYPE:
            self._handle_cancel(event)
        elif event.type == ReloadModel.EVENT_TYPE:
            self._handle_reload_model(event)
        elif event.type == ConversationReset.EVENT_TYPE:
            self._handle_conversation_reset(event)
        # anything else: not ours, ignore (mirrors every other adapter)

    # -- NeedLLMResponse ----------------------------------------------------

    def _handle_need_llm_response(self, event: Any) -> None:
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")
        correlation_id = event.get("correlation_id") or request_id
        model = event.get("model") or self.default_model
        messages = event.get("messages") or []
        ids = {"request_id": request_id, "conversation_id": conversation_id, "correlation_id": correlation_id}

        if not model:
            log(f"request {request_id}: no model specified and no default model configured", self.name)
            self.publish(LLMError(data={**ids, "error": "no model specified", "error_type": "ConfigurationError", "retryable": False}))
            return

        system_prompt = event.get("system_prompt")
        temperature = event.get("temperature")
        max_tokens = event.get("max_tokens")
        stream = event.get("stream")
        if stream is None:
            stream = self.config.stream_default
        metadata = event.get("metadata") or {}

        entry = _InFlight(request_id=request_id, conversation_id=conversation_id, correlation_id=correlation_id)
        with self._inflight_lock:
            self._inflight[request_id] = entry

        pool = self._request_executor
        if pool is None:
            log(f"request {request_id}: adapter not started, dropping", self.name)
            with self._inflight_lock:
                self._inflight.pop(request_id, None)
            return
        pool.submit(
            self._run_request, entry, model, messages, system_prompt, temperature, max_tokens, bool(stream), metadata,
        )

    def _run_request(
        self, entry: _InFlight, model: str, messages: List[Dict[str, str]],
        system_prompt: Optional[str], temperature: Optional[float], max_tokens: Optional[int],
        stream: bool, metadata: Dict[str, Any],
    ) -> None:
        ids = {"request_id": entry.request_id, "conversation_id": entry.conversation_id, "correlation_id": entry.correlation_id}
        try:
            if entry.cancel_event.is_set():
                return  # cancelled before it even started - LLMCancelled already published by _handle_cancel

            prompt_note = f", messages={len(messages)}"
            if self.config.debug_log_prompts:
                prompt_note += f" prompt={messages!r}"
            log(f"request {entry.request_id} started (model={model}, stream={stream}{prompt_note})", self.name)
            self.publish(LLMStarted(data={**ids, "model": model, "stream": stream}))

            t0 = time.time()
            try:
                if stream:
                    text, usage, finish_reason = self._run_streaming(entry, ids, model, messages, system_prompt, temperature, max_tokens, metadata)
                else:
                    text, usage, finish_reason = self._run_non_streaming(model, messages, system_prompt, temperature, max_tokens, metadata)
            except _Cancelled:
                return  # cancellation already fully handled (event published, bookkeeping done) in _handle_cancel
            except OpenRouterAPIError as ex:
                self._publish_error(ids, model, ex)
                return
            except Exception as ex:  # never let anything escape this thread uncaught
                log(f"request {entry.request_id}: unexpected error: {ex}", self.name)
                self._publish_error(ids, model, ex)
                return

            elapsed = elapsed_ms(t0)
            token_note = f"usage={usage}" if usage else f"~{len(text.split())} words"
            log(f"request {entry.request_id} finished ({elapsed:.1f}ms, {token_note}, finish_reason={finish_reason})", self.name)
            self.publish(LLMFinished(data={**ids, "model": model, "execution_time_ms": elapsed, "usage": usage, "finish_reason": finish_reason}))
            self.publish(AssistantResponse(data={**ids, "text": text, "model": model}))
        finally:
            with self._inflight_lock:
                self._inflight.pop(entry.request_id, None)

    def _run_non_streaming(self, model, messages, system_prompt, temperature, max_tokens, metadata):
        response = self.client.chat_completion(
            model=model, messages=messages, system_prompt=system_prompt,
            temperature=temperature, max_tokens=max_tokens, metadata=metadata,
        )
        return response.text, response.usage, response.finish_reason

    def _run_streaming(self, entry, ids, model, messages, system_prompt, temperature, max_tokens, metadata):
        log(f"request {entry.request_id} streaming started", self.name)
        self.publish(LLMStreaming(data={**ids, "model": model}))
        parts: List[str] = []
        index = 0
        usage = None
        finish_reason = None
        for chunk in self.client.stream_chat_completion(
            model=model, messages=messages, system_prompt=system_prompt,
            temperature=temperature, max_tokens=max_tokens, metadata=metadata,
            cancel_event=entry.cancel_event,
        ):
            if entry.cancel_event.is_set():
                raise _Cancelled()
            if chunk.delta:
                parts.append(chunk.delta)
                index += 1
                self.publish(LLMChunk(data={
                    **ids, "model": model, "delta": chunk.delta, "index": index, "text_so_far": "".join(parts),
                }))
            if chunk.finished:
                usage = chunk.usage
                finish_reason = chunk.finish_reason
        if entry.cancel_event.is_set():
            raise _Cancelled()
        log(f"request {entry.request_id} streaming finished ({index} chunks)", self.name)
        return "".join(parts), usage, finish_reason

    def _publish_error(self, ids: Dict[str, Any], model: str, ex: Exception) -> None:
        retryable = getattr(ex, "retryable", False)
        self.publish(LLMError(data={
            **ids, "model": model, "error": str(ex), "error_type": type(ex).__name__, "retryable": retryable,
        }))

    # -- CancelLLMRequest -----------------------------------------------------

    def _handle_cancel(self, event: Any) -> None:
        request_id = event.get("request_id")
        conversation_id = event.get("conversation_id")
        correlation_id = event.get("correlation_id") or request_id
        with self._inflight_lock:
            entry = self._inflight.get(request_id) if request_id else None
            if entry is not None:
                entry.cancelled = True
                entry.cancel_event.set()
                conversation_id = conversation_id or entry.conversation_id
                correlation_id = correlation_id or entry.correlation_id
        log(f"request {request_id} cancel requested", self.name)
        # Published immediately, synchronously, from the Event Bus's own
        # dispatch of CancelLLMRequest - NOT from the (possibly still
        # blocked-on-network) request thread - so "Behavior Tree should
        # immediately regain control" is actually immediate.
        self.publish(LLMCancelled(data={
            "request_id": request_id, "conversation_id": conversation_id, "correlation_id": correlation_id,
        }))

    # -- ReloadModel ------------------------------------------------------------

    def _handle_reload_model(self, event: Any) -> None:
        new_config = OpenRouterConfig.from_env()
        override_model = event.get("model")
        self.config = new_config
        self.default_model = override_model or new_config.model
        try:
            self.client.reload_config(new_config)
        except Exception as ex:
            log(f"reload_model: client.reload_config raised (continuing with new adapter-side config anyway): {ex}", self.name)
        log(f"config reloaded (model={self.default_model}, base_url={new_config.base_url}, timeout={new_config.timeout_s}s)", self.name)

    # -- ConversationReset ------------------------------------------------------

    def _handle_conversation_reset(self, event: Any) -> None:
        conversation_id = event.get("conversation_id")
        with self._inflight_lock:
            targets = [
                e for e in self._inflight.values()
                if conversation_id is None or e.conversation_id == conversation_id
            ]
            for e in targets:
                e.cancelled = True
                e.cancel_event.set()
        log(f"conversation_reset: cancelled {len(targets)} in-flight request(s)"
            f"{f' for conversation {conversation_id}' if conversation_id else ' (all conversations)'}", self.name)
        for e in targets:
            self.publish(LLMCancelled(data={
                "request_id": e.request_id, "conversation_id": e.conversation_id, "correlation_id": e.correlation_id,
                "reason": "conversation_reset",
            }))

    # -- status -----------------------------------------------------------------

    def _extra_status(self) -> Dict[str, Any]:
        with self._inflight_lock:
            inflight_count = len(self._inflight)
        return {
            "model": self.default_model,
            "base_url": self.config.base_url,
            "stream_default": self.config.stream_default,
            "inflight_requests": inflight_count,
            "client": type(self.client).__name__,
        }


class _Cancelled(Exception):
    """Internal-only control-flow signal - never published, never leaves
    `_run_request()`. Cancellation is already fully handled (event
    published, bookkeeping cleared) by `_handle_cancel()`/
    `_handle_conversation_reset()` by the time this is raised."""
