"""
vision_provider.py
=====================

`VisionProvider` - a small, deliberately narrow abstraction between
`luno.vision.ask_vision()` and whichever remote/local model actually
answers "what's in this image" questions. `luno/vision.py` used to call
a local MiniCPM-V model (via Ollama) directly, inline, with zero
seam between "capture a frame" and "understand the frame" - swapping the
model out meant editing `luno/vision.py` itself. This module exists so
that never has to happen again: `luno.vision` only ever talks to the
`VisionProvider` interface below, never to a specific SDK/HTTP shape.

Two providers are implemented today, selected via `VISION_PROVIDER` in
`.env` (see `luno/config.py`, read by `luno.vision._get_vision_provider()`):

  - `GeminiVisionProvider` - Google's Gemini 2.0 Flash Vision API
    (`generativelanguage.googleapis.com`). The original replacement for
    the old local MiniCPM-V pipeline.
  - `OpenAIVisionProvider` - OpenAI's vision-capable Chat Completions
    API (`api.openai.com/v1/chat/completions`, `gpt-4o`/`gpt-4o-mini`).
    Added when the Gemini free tier's request-per-minute limit turned
    out to be shared with (and gets squeezed by) this project's own
    chat-LLM fallback priority also sometimes landing on Gemini - see
    `OPENAI_VISION_MODEL`'s own comment in config.py.

Switching which one is active is a config change, not a code change -
that's the whole point of the `VisionProvider` interface below. A
future THIRD provider (a different hosted API, a different local model,
...) only needs to implement `analyze_image()` and get wired into
`luno.vision._get_vision_provider()`'s provider-name dispatch - nothing
else in the vision pipeline (camera capture, YOLO hints, Vision Memory
integration, the vision-intent classifier, the conversation-context
injection) needs to change.

Deliberately NOT reusing either chat provider from `luno/adapters/llm/`
(`GeminiProvider`/`OpenAIProvider`, the multi-turn CHAT clients used by
the LLM Manager/routing system) even though each shares an API vendor
with its vision counterpart here - those classes are built around chat
semantics (streaming, multi-turn `messages`, retries/fallback tuned for
conversation latency, cancellation-by-request-id). Vision QA here is a
single-shot, single-image, non-streaming call with its own timeout and
failure-handling needs; sharing the chat classes would mean either
bending them to fit (more `if` branches there than code saved) or
bending vision to fit chat semantics it doesn't have. Both vision
providers DO share the SAME auth-header discipline as their chat
counterparts on purpose (never the API key in the URL/logs) - that's a
deliberate, cheap consistency, not an attempt at code reuse.

Error handling contract: `analyze_image()` NEVER returns a sentinel
value on failure - it always either returns the description text or
raises one of the `VisionProviderError` subclasses below, so a caller
can't accidentally treat a failure as a real answer. `luno.vision`'s
`ask_vision()` is the layer that translates these exceptions back into
its own long-standing `{"description": ...}` / `{"error": ...}` dict
contract (see that module's own docstring) - this module itself has no
opinion on what shape callers want their errors in.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Dict, Optional, Protocol, runtime_checkable

try:
    import requests as _requests
except ImportError:  # pragma: no cover - requests is a hard project dependency (requirements.txt)
    _requests = None


# -- errors -------------------------------------------------------------------

class VisionProviderError(Exception):
    """Base class for every failure `analyze_image()` can raise. Callers
    that don't care about the specific reason can catch just this."""


class VisionProviderNotConfiguredError(VisionProviderError):
    """No API key (or other required setup) - the provider was never
    usable for this call, not a transient failure."""


class VisionProviderEmptyImageError(VisionProviderError):
    """`image` was empty/None, or failed to encode - nothing was ever
    sent over the network."""


class VisionProviderTimeoutError(VisionProviderError):
    """The request was sent but didn't get a response within the
    configured timeout."""


class VisionProviderNetworkError(VisionProviderError):
    """Couldn't reach the provider at all (DNS/connection refused/no
    internet/...) - distinct from a timeout (which DID reach the
    network, just too slowly) so callers/logs can tell the two apart."""


class VisionProviderAPIError(VisionProviderError):
    """The provider responded, but with an error - bad request, auth
    failure, rate limit, server error, etc. `status_code` is populated
    when known (HTTP responses); `None` for provider-specific error
    shapes without one."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class VisionProviderMalformedResponseError(VisionProviderError):
    """The provider responded with HTTP 2xx, but the body wasn't valid
    JSON, or didn't have the shape this provider expects (e.g. no
    candidates, an empty answer)."""


# -- protocol -------------------------------------------------------------------

@runtime_checkable
class VisionProvider(Protocol):
    """Anything that can look at ONE image and answer ONE question about
    it. `image` is already-encoded bytes (JPEG - see
    `luno.vision._encode_frame_for_upload()`), never a raw OpenCV/numpy
    frame - keeps this interface free of any camera/CV library
    dependency. Returns the answer text, or raises a
    `VisionProviderError` subclass - see this module's own docstring for
    why "never returns a sentinel" is a deliberate contract."""

    def analyze_image(self, image: bytes, prompt: str) -> str:
        ...


# -- Gemini implementation -----------------------------------------------------

class GeminiVisionProvider:
    """`VisionProvider` backed by Gemini 2.0 Flash's `generateContent`
    REST endpoint. Structurally-typed against `VisionProvider` above (no
    explicit inheritance needed - that's the point of `Protocol`), so
    tests can substitute any object with a matching `analyze_image()`
    without importing this class at all.

    Configuration is read from `luno.config` LAZILY, once, at
    construction time (not at import time, not per-call) - reused as-is
    from the existing `GEMINI_API_KEY`/`GEMINI_VISION_MODEL`/
    `GEMINI_VISION_TIMEOUT_S` settings (see config.py's own comments) so
    this never becomes a second, parallel configuration mechanism. Every
    constructor argument is optional purely for testability (tests pass
    an explicit `api_key`/`session` rather than touching real env vars
    or making real HTTP calls)."""

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        base_url: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        if api_key is None or model is None or timeout_s is None:
            from . import config as _legacy_config
            api_key = api_key if api_key is not None else _legacy_config.GEMINI_API_KEY
            model = model if model is not None else _legacy_config.GEMINI_VISION_MODEL
            timeout_s = timeout_s if timeout_s is not None else _legacy_config.GEMINI_VISION_TIMEOUT_S

        self._api_key = api_key or ""
        self._model = model or "gemini-2.0-flash"
        self._timeout_s = float(timeout_s) if timeout_s else 20.0
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._session = session  # lazily created below if still None when first needed

    def is_configured(self) -> bool:
        """Cheap pre-flight check - lets callers (see
        `luno.vision.is_configured()`/health checks) report "vision API
        unavailable, no key configured" up front instead of only
        discovering it after opening the camera and encoding a frame."""
        return bool(self._api_key)

    def _get_session(self) -> Any:
        if self._session is None:
            if _requests is None:  # pragma: no cover
                raise VisionProviderNotConfiguredError(
                    "the 'requests' package is required for GeminiVisionProvider"
                )
            self._session = _requests.Session()
        return self._session

    def analyze_image(self, image: bytes, prompt: str) -> str:
        if not self._api_key:
            raise VisionProviderNotConfiguredError(
                "GEMINI_API_KEY is not set - vision questions can't be answered until it is (see .env)"
            )
        if not image:
            raise VisionProviderEmptyImageError("no image bytes were provided (empty/None frame)")

        body: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt or "Describe what's visible in this image, briefly and naturally."},
                        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode("ascii")}},
                    ],
                }
            ]
        }
        # NEVER put the key in the URL (query strings end up in proxy/
        # access logs far more often than headers do) - same convention
        # `luno/adapters/llm/gemini_provider.py`'s chat client already
        # uses for the exact same reason.
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        url = f"{self._base_url}/models/{self._model}:generateContent"

        session = self._get_session()
        try:
            resp = session.post(url, json=body, headers=headers, timeout=self._timeout_s)
        except getattr(_requests, "Timeout", TimeoutError) as ex:
            raise VisionProviderTimeoutError(
                f"Gemini didn't respond within {self._timeout_s:.0f}s"
            ) from ex
        except Exception as ex:
            if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                raise VisionProviderNetworkError(f"couldn't reach Gemini: {ex}") from ex
            raise

        if resp.status_code >= 400:
            message = self._error_message(resp)
            if resp.status_code == 429:
                raise VisionProviderAPIError(f"Gemini rate limit hit: {message}", status_code=429)
            if resp.status_code in (401, 403):
                raise VisionProviderAPIError(f"Gemini auth error (check GEMINI_API_KEY): {message}", status_code=resp.status_code)
            raise VisionProviderAPIError(f"Gemini API error (HTTP {resp.status_code}): {message}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as ex:
            raise VisionProviderMalformedResponseError(f"Gemini returned a non-JSON response: {ex}") from ex

        try:
            candidates = data["candidates"]
            if not candidates:
                raise VisionProviderMalformedResponseError("Gemini returned no candidates")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as ex:
            raise VisionProviderMalformedResponseError(f"unexpected response shape from Gemini: {ex}") from ex

        if not text:
            # Distinct from a malformed SHAPE - the response was well-
            # formed, Gemini just didn't produce any text (e.g. safety
            # filtering blocked the answer). Still a failure the caller
            # needs to know about, not a silent empty string treated as
            # a real (if unhelpful) description.
            finish_reason = (candidates[0].get("finishReason") or "unknown") if candidates else "unknown"
            raise VisionProviderMalformedResponseError(f"Gemini returned an empty answer (finishReason={finish_reason})")

        return text

    @staticmethod
    def _error_message(resp: Any) -> str:
        """Best-effort human-readable error text from a Gemini error
        response - falls back to the raw response body/status line if
        the body isn't the expected `{"error": {"message": ...}}` JSON
        shape. Never includes request headers/the API key - only ever
        reads the RESPONSE body."""
        try:
            data = resp.json()
            msg = (data.get("error") or {}).get("message")
            if msg:
                return str(msg)
        except Exception:
            pass
        try:
            return (resp.text or "")[:300] or f"HTTP {resp.status_code}"
        except Exception:
            return f"HTTP {getattr(resp, 'status_code', '?')}"


# -- OpenAI implementation ------------------------------------------------------

class OpenAIVisionProvider:
    """`VisionProvider` backed by OpenAI's vision-capable Chat
    Completions endpoint (`gpt-4o`/`gpt-4o-mini`, ...). Structurally the
    same shape as `GeminiVisionProvider` above (same constructor
    pattern, same lazy config read, same exception mapping) - the two
    intentionally read almost as mirror images of each other so
    switching `VISION_PROVIDER` in `.env` is a config change, not a
    "relearn a different class shape" change.

    Configuration reused from `luno.config`'s existing `OPENAI_API_KEY`
    (also used by the chat-LLM OpenAI provider - same "don't invent a
    second config mechanism" reasoning `GeminiVisionProvider` already
    documents for `GEMINI_API_KEY`) plus `OPENAI_VISION_MODEL`/
    `OPENAI_VISION_TIMEOUT_S`."""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        base_url: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        if api_key is None or model is None or timeout_s is None:
            from . import config as _legacy_config
            api_key = api_key if api_key is not None else _legacy_config.OPENAI_API_KEY
            model = model if model is not None else _legacy_config.OPENAI_VISION_MODEL
            timeout_s = timeout_s if timeout_s is not None else _legacy_config.OPENAI_VISION_TIMEOUT_S

        self._api_key = api_key or ""
        self._model = model or "gpt-4o-mini"
        self._timeout_s = float(timeout_s) if timeout_s else 20.0
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._session = session  # lazily created below if still None when first needed

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_session(self) -> Any:
        if self._session is None:
            if _requests is None:  # pragma: no cover
                raise VisionProviderNotConfiguredError(
                    "the 'requests' package is required for OpenAIVisionProvider"
                )
            self._session = _requests.Session()
        return self._session

    def analyze_image(self, image: bytes, prompt: str) -> str:
        if not self._api_key:
            raise VisionProviderNotConfiguredError(
                "OPENAI_API_KEY is not set - vision questions can't be answered until it is (see .env)"
            )
        if not image:
            raise VisionProviderEmptyImageError("no image bytes were provided (empty/None frame)")

        data_url = f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}"
        body: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or "Describe what's visible in this image, briefly and naturally."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        # `Authorization: Bearer ...` - never the key in the URL/logs,
        # same discipline as `GeminiVisionProvider`'s `x-goog-api-key`
        # header (also matches `luno/adapters/llm/base.py`'s own
        # `OpenAICompatibleClient._headers()` convention for the chat
        # client this deliberately does NOT share code with - see this
        # module's own docstring).
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/chat/completions"

        session = self._get_session()
        try:
            resp = session.post(url, json=body, headers=headers, timeout=self._timeout_s)
        except getattr(_requests, "Timeout", TimeoutError) as ex:
            raise VisionProviderTimeoutError(
                f"OpenAI didn't respond within {self._timeout_s:.0f}s"
            ) from ex
        except Exception as ex:
            if _requests is not None and isinstance(ex, _requests.exceptions.RequestException):
                raise VisionProviderNetworkError(f"couldn't reach OpenAI: {ex}") from ex
            raise

        if resp.status_code >= 400:
            message = self._error_message(resp)
            if resp.status_code == 429:
                raise VisionProviderAPIError(f"OpenAI rate limit hit: {message}", status_code=429)
            if resp.status_code in (401, 403):
                raise VisionProviderAPIError(f"OpenAI auth error (check OPENAI_API_KEY): {message}", status_code=resp.status_code)
            raise VisionProviderAPIError(f"OpenAI API error (HTTP {resp.status_code}): {message}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as ex:
            raise VisionProviderMalformedResponseError(f"OpenAI returned a non-JSON response: {ex}") from ex

        try:
            choices = data["choices"]
            if not choices:
                raise VisionProviderMalformedResponseError("OpenAI returned no choices")
            text = (choices[0].get("message", {}) or {}).get("content", "")
            text = (text or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as ex:
            raise VisionProviderMalformedResponseError(f"unexpected response shape from OpenAI: {ex}") from ex

        if not text:
            finish_reason = (choices[0].get("finish_reason") or "unknown") if choices else "unknown"
            raise VisionProviderMalformedResponseError(f"OpenAI returned an empty answer (finish_reason={finish_reason})")

        return text

    @staticmethod
    def _error_message(resp: Any) -> str:
        """Best-effort human-readable error text from an OpenAI error
        response - same "response body only, never headers/the API key"
        contract as `GeminiVisionProvider._error_message`."""
        try:
            data = resp.json()
            msg = (data.get("error") or {}).get("message")
            if msg:
                return str(msg)
        except Exception:
            pass
        try:
            return (resp.text or "")[:300] or f"HTTP {resp.status_code}"
        except Exception:
            return f"HTTP {getattr(resp, 'status_code', '?')}"
