"""
mock.py
=======

`MockProviderClient` - no network, canned/scripted responses. The
provider-agnostic generalization of `luno.adapters.openrouter.
MockOpenRouterClient`: same scripting knobs, but constructible as ANY
provider name/capability set, so `LLMManagerAdapter`'s tests can stand
up all five providers (or a sixth, imaginary one) without real
credentials. Used by every test in this package and by
`LLMManagerAdapter`'s own default-client fallback when a provider has
no API key configured.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from .base import LLMProviderClient
from .errors import (
    ProviderAPIError,
    ProviderNetworkError,
    ProviderStreamError,
    ProviderTimeoutError,
    classify_http_status,
)
from .models import ChatResult, HealthState, LLMStreamChunk, ModelInfo, ProviderCapabilities, ProviderConfig, ProviderHealth


class MockProviderClient(LLMProviderClient):
    """
    `name`             - which provider this pretends to be (drives
                          `ChatResult.provider`, health/status labels).
    `canned_text`      - reply text (default: an echo of the last user
                          message, prefixed with the provider name so
                          fallback/switching tests can assert on it).
    `fail`/`fail_status`/`fail_times`/`network_error`/`timeout_error`/
    `malformed`        - same failure-injection knobs as
                          `MockOpenRouterClient` - see that class.
    `delay_s`/`chunk_delay_s` - latency injection, same as before.
    `healthy`          - what `health()` reports (default: HEALTHY).
    `usage`            - usage dict attached to the final chunk/response.
    `streaming`/`tools`/`images`/`reasoning` - capability flags this
                          mock reports, so capability-detection tests
                          don't need a real per-provider subclass.
    `configured`       - `initialize()` raises `ProviderNotConfiguredError`
                          when False (simulates "no API key set").
    """

    def __init__(
        self,
        name: str = "mock",
        canned_text: Optional[str] = None,
        fail: bool = False,
        fail_status: Optional[int] = None,
        fail_times: int = 1,
        network_error: bool = False,
        timeout_error: bool = False,
        malformed: bool = False,
        delay_s: float = 0.0,
        chunk_delay_s: float = 0.0,
        healthy: bool = True,
        usage: Optional[Dict[str, Any]] = None,
        streaming: bool = True,
        tools: bool = False,
        images: bool = False,
        reasoning: bool = False,
        configured: bool = True,
        default_model: str = "mock-model",
    ) -> None:
        self.name = name
        self.canned_text = canned_text
        self.fail = fail
        self.fail_status = fail_status
        self.fail_times = fail_times
        self.network_error = network_error
        self.timeout_error = timeout_error
        self.malformed = malformed
        self.delay_s = delay_s
        self.chunk_delay_s = chunk_delay_s
        self.healthy = healthy
        self.usage = usage or {}
        self._streaming = streaming
        self._tools = tools
        self._images = images
        self._reasoning = reasoning
        self.configured = configured
        self.default_model = default_model
        self.calls: List[Dict[str, Any]] = []
        self.cancelled_request_ids: List[str] = []
        self._attempts = 0
        self._lock = threading.Lock()
        self._initialized = False
        self.config = ProviderConfig(provider=name, api_key="mock-key" if configured else "", model=default_model)

    # -- lifecycle ------------------------------------------------------------

    def initialize(self) -> None:
        if not self.configured:
            from .errors import ProviderNotConfiguredError
            raise ProviderNotConfiguredError(f"{self.name}: mock not configured")
        self._initialized = True

    def shutdown(self) -> None:
        self._initialized = False

    def reload(self, config: ProviderConfig) -> None:
        self.config = config

    # -- health / capabilities --------------------------------------------------

    def health(self) -> ProviderHealth:
        if not self.configured:
            return ProviderHealth(self.name, HealthState.UNKNOWN, "not configured")
        if self.healthy:
            return ProviderHealth(self.name, HealthState.HEALTHY, "mock reachable", latency_ms=1.0)
        return ProviderHealth(self.name, HealthState.OFFLINE, "mock offline", latency_ms=1.0)

    def supports_streaming(self) -> bool:
        return self._streaming

    def supports_tools(self) -> bool:
        return self._tools

    def supports_images(self) -> bool:
        return self._images

    def supports_reasoning(self) -> bool:
        return self._reasoning

    def get_model_info(self, model: Optional[str] = None) -> ModelInfo:
        model_id = model or self.default_model
        return ModelInfo(
            id=model_id, provider=self.name, display_name=model_id,
            capabilities=ProviderCapabilities(
                streaming=self._streaming, tools=self._tools, images=self._images,
                vision=self._images, reasoning=self._reasoning, max_context_tokens=32000,
            ),
            input_cost_per_1m=0.0, output_cost_per_1m=0.0,
        )

    # -- internals --------------------------------------------------------------

    def _resolve_text(self, messages: List[Dict[str, str]]) -> str:
        if self.canned_text is not None:
            return self.canned_text
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        return f"({self.name} mock reply to: {last_user})"

    def _maybe_raise(self) -> None:
        with self._lock:
            self._attempts += 1
            attempt = self._attempts
        if self.network_error:
            raise ProviderNetworkError(f"mock({self.name}): connection failed")
        if self.timeout_error:
            raise ProviderTimeoutError(f"mock({self.name}): request timed out")
        if self.fail_status is not None and attempt <= self.fail_times:
            raise classify_http_status(self.fail_status, f"mock({self.name}): HTTP {self.fail_status}")
        if self.fail:
            raise ProviderAPIError(f"mock({self.name}) call failed")

    # -- chat -----------------------------------------------------------------

    def chat(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None, **kwargs: Any,
    ) -> ChatResult:
        model = model or self.default_model
        self.calls.append({
            "model": model, "messages": messages, "system_prompt": system_prompt,
            "temperature": temperature, "max_tokens": max_tokens, "metadata": metadata,
            "stream": False, "request_id": request_id,
        })
        if self.delay_s:
            time.sleep(self.delay_s)
        self._maybe_raise()
        if self.malformed:
            raise ProviderStreamError(f"mock({self.name}): malformed response body")
        usage = self.usage or {}
        return ChatResult(
            text=self._resolve_text(messages), model=model, provider=self.name, raw={"mock": True},
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"), finish_reason="stop", latency_ms=self.delay_s * 1000,
        )

    def stream_chat(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None, request_id: Optional[str] = None, **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        model = model or self.default_model
        if not self._streaming:
            result = self.chat(
                messages, model=model, system_prompt=system_prompt, temperature=temperature,
                max_tokens=max_tokens, metadata=metadata, request_id=request_id,
            )
            yield LLMStreamChunk(delta=result.text)
            yield LLMStreamChunk(delta="", finished=True, finish_reason=result.finish_reason)
            return

        self.calls.append({
            "model": model, "messages": messages, "system_prompt": system_prompt,
            "temperature": temperature, "max_tokens": max_tokens, "metadata": metadata,
            "stream": True, "request_id": request_id,
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
                raise ProviderStreamError(f"mock({self.name}): malformed chunk mid-stream")
            piece = word if i == 0 else " " + word
            yield LLMStreamChunk(delta=piece)
            if self.chunk_delay_s:
                time.sleep(self.chunk_delay_s)
        if cancel_event is not None and cancel_event.is_set():
            return
        usage = self.usage or {}
        yield LLMStreamChunk(
            delta="", finished=True, finish_reason="stop",
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )

    def cancel(self, request_id: str) -> bool:
        self.cancelled_request_ids.append(request_id)
        return True
