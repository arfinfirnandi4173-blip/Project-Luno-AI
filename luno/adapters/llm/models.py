"""
models.py
=========

Shared, provider-agnostic data shapes. Every `LLMProviderClient`
implementation speaks these types at its boundary (`chat()`/
`stream_chat()` in, `ChatResult`/`LLMStreamChunk` out) regardless of how
different the underlying wire format is (OpenAI-style `choices[0].
message.content` vs Gemini's `candidates[0].content.parts[].text` vs
Anthropic's `content[].text` blocks) - translating THAT away is each
provider module's entire job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


@dataclass
class ChatResult:
    """Result of a non-streaming `chat()` call - the provider-agnostic
    replacement for `luno.adapters.openrouter.LLMResponse`."""
    text: str
    model: str
    provider: str
    raw: Any = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    latency_ms: float = 0.0

    @property
    def usage(self) -> Optional[Dict[str, Any]]:
        """OpenAI-shaped usage dict, for callers (`llm_manager.py`'s
        `LLMFinished` event, `memory.summarize_and_archive_session()`'s
        legacy duck-typing) that already expect this exact shape."""
        if self.prompt_tokens is None and self.completion_tokens is None and self.total_tokens is None:
            return None
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens or ((self.prompt_tokens or 0) + (self.completion_tokens or 0)),
        }


@dataclass
class LLMStreamChunk:
    """One item yielded by `stream_chat()` - provider-agnostic
    replacement for `luno.adapters.openrouter.StreamChunk`."""
    delta: str = ""
    finished: bool = False
    finish_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    raw: Any = None


class HealthState(str, Enum):
    """The spec's own six states - `str` subclass so it serializes to
    JSON (Dashboard) without a custom encoder, same convention
    `luno.core.models` already uses for its own enums."""
    HEALTHY = "healthy"
    WARNING = "warning"
    OFFLINE = "offline"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"  # never checked yet (e.g. provider not configured)


@dataclass
class ProviderHealth:
    """Returned by `LLMProviderClient.health()` and cached by
    `LLMManagerAdapter`'s background health-poll loop - see
    `llm_manager.py`."""
    provider: str
    state: HealthState = HealthState.UNKNOWN
    message: str = ""
    checked_at: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "state": self.state.value,
            "message": self.message,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ProviderCapabilities:
    """What `supports_streaming()`/`supports_tools()`/etc. collapse
    into one JSON-friendly shape for the Dashboard's "Provider
    Capabilities" panel and for Planner code that wants to branch on
    capability (e.g. "don't attach an image if the active provider
    can't see it") without calling five separate methods."""
    streaming: bool = True
    tools: bool = False
    images: bool = False
    vision: bool = False
    reasoning: bool = False
    audio: bool = False
    long_context: bool = False
    max_context_tokens: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "streaming": self.streaming, "tools": self.tools, "images": self.images,
            "vision": self.vision, "reasoning": self.reasoning, "audio": self.audio,
            "long_context": self.long_context, "max_context_tokens": self.max_context_tokens,
        }


@dataclass
class ModelInfo:
    """Returned by `get_model_info()` - one entry of a provider's
    exposed model catalog (spec's "Model Selection" section)."""
    id: str
    provider: str
    display_name: Optional[str] = None
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    #: USD per 1,000,000 tokens - best-effort, hand-maintained pricing
    #: (see each provider module's `_MODEL_CATALOG`). `None` means
    #: "unknown model, cost cannot be estimated" - `llm_manager.py`'s
    #: cost tracker treats that as "no estimate" rather than guessing.
    input_cost_per_1m: Optional[float] = None
    output_cost_per_1m: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "provider": self.provider, "display_name": self.display_name or self.id,
            "capabilities": self.capabilities.to_dict(),
            "input_cost_per_1m": self.input_cost_per_1m, "output_cost_per_1m": self.output_cost_per_1m,
        }


@dataclass
class ProviderConfig:
    """Generic per-provider config every `OpenAICompatibleClient`
    subclass (and Gemini/Anthropic) construct themselves from - the
    provider-agnostic replacement for `luno.adapters.openrouter.
    OpenRouterConfig`. Provider-specific extra fields (e.g. Anthropic's
    `anthropic-version` header) live on the subclass, not here."""
    provider: str = ""
    api_key: str = ""
    base_url: str = ""
    model: Optional[str] = None
    timeout_s: float = 60.0
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream_default: bool = True
    max_retries: int = 3
    retry_backoff_base_s: float = 1.0
    retry_backoff_max_s: float = 20.0
    debug_log_prompts: bool = False


def new_request_id(prefix: str = "llm") -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
