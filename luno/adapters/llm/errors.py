"""
errors.py
=========

Provider-agnostic error hierarchy - the generalized form of
`luno.adapters.openrouter`'s original `OpenRouterAPIError` classes.
Every `LLMProviderClient` implementation classifies whatever its
transport raises (HTTP status codes, SDK-specific exceptions, socket
errors, ...) into exactly one of these, so `LLMManagerAdapter` can make
retry/fallback decisions (`retryable`) and publish structured `LLMError`
events (`error_type` = the class name) without ever needing to know
which of the five providers actually failed.
"""

from __future__ import annotations

from typing import Optional


class ProviderAPIError(Exception):
    """Base class for every classified failure a provider client can
    raise. `retryable` drives the shared `_retry()` backoff loop in
    `base.py`; `fallback_eligible` drives `LLMManagerAdapter`'s
    provider-priority fallback (see that module) - distinct from
    `retryable` because some failures (auth, rate limit) should never be
    retried against the SAME provider but SHOULD trigger falling
    through to the next one in the priority list, while others
    (invalid request - a bug in the payload we built) would fail
    identically on every provider and should not burn a fallback
    attempt."""
    retryable: bool = False
    fallback_eligible: bool = True
    status_code: Optional[int] = None

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthError(ProviderAPIError):
    """401/403, or an SDK-level "missing/invalid credentials" error.
    Never retried against the same provider; DOES trigger fallback
    (another provider may have valid credentials)."""
    retryable = False
    fallback_eligible = True


class ProviderInvalidRequestError(ProviderAPIError):
    """400/404/422-class client errors - a malformed request body,
    unknown model, etc. Never retried, and NOT fallback-eligible by
    default: if the payload itself is malformed the same failure would
    very likely repeat on the next provider too, at the cost of extra
    latency and (for paid providers) a wasted call. `LLMManagerAdapter`
    still exposes an override switch for this - see that module."""
    retryable = False
    fallback_eligible = False


class ProviderRateLimitError(ProviderAPIError):
    """429. Retried with backoff on the SAME provider first (a burst
    limit often clears within seconds); if retries are exhausted, this
    IS fallback-eligible - another provider's rate limit is independent."""
    retryable = True
    fallback_eligible = True


class ProviderServerError(ProviderAPIError):
    """500/502/503/504 - the provider's own infrastructure is having a
    bad time. Retried, then fallback-eligible."""
    retryable = True
    fallback_eligible = True


class ProviderTimeoutError(ProviderAPIError):
    """Request exceeded the configured timeout. Retried, then
    fallback-eligible."""
    retryable = True
    fallback_eligible = True


class ProviderNetworkError(ProviderAPIError):
    """DNS/connection/socket-level failure (also what a local
    OpenAI-compatible server being completely offline looks like).
    Retried, then fallback-eligible."""
    retryable = True
    fallback_eligible = True


class ProviderStreamError(ProviderAPIError):
    """Malformed SSE/stream payload, or a mid-stream error chunk from
    the provider itself. NOT retried - by the time this can be raised,
    some chunks may already have been published as `LLMChunk` events,
    so silently retrying (on this provider OR a fallback one) would
    double-speak partial output to whatever's already consuming the
    stream. NOT fallback-eligible for the same reason."""
    retryable = False
    fallback_eligible = False


class ProviderNotConfiguredError(ProviderAPIError):
    """Raised by `initialize()` (never by `chat()`/`stream_chat()`
    directly) when a provider has no usable credentials/base URL at
    all - e.g. `LLM_PROVIDER=gemini` but `GEMINI_API_KEY` is unset. Lets
    `LLMManagerAdapter` skip straight to the next priority entry without
    spending a real request finding this out. Not retryable; fallback-
    eligible (that's the whole point of it existing)."""
    retryable = False
    fallback_eligible = True


def classify_http_status(status_code: int, message: str) -> ProviderAPIError:
    """Shared status-code -> error-class mapping, reused by every
    `OpenAICompatibleClient` subclass (OpenRouter/OpenAI/Local) and by
    Gemini/Anthropic's own REST clients (both are also plain HTTP APIs
    with conventional status codes)."""
    if status_code in (401, 403):
        return ProviderAuthError(message, status_code=status_code)
    if status_code == 429:
        return ProviderRateLimitError(message, status_code=status_code)
    if status_code in (500, 502, 503, 504):
        return ProviderServerError(message, status_code=status_code)
    if status_code == 408:
        return ProviderTimeoutError(message, status_code=status_code)
    return ProviderInvalidRequestError(message, status_code=status_code)
