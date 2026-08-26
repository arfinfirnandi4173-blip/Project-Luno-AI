"""
config.py
=========

`LLMManagerConfig` - everything `LLMManagerAdapter` needs to know
BEFORE it can pick a provider: which one is active, the fallback
priority order, and the global knobs (`ENABLE_FALLBACK`,
`ENABLE_STREAMING`, `MAX_RETRIES`, `TIMEOUT`) the spec's Configuration
section asks for. Built fresh from `os.environ` every time
`LLMManagerAdapter` handles a `ReloadModel` event - same "rotate the
key in the environment, publish `ReloadModel`, done" runtime-reload
story `OpenRouterConfig.from_env()` already established.

`build_provider_config()`/`build_provider_client()` are this module's
other half: the name -> factory registry the spec's "New providers can
be added by implementing the standard adapter interface without
modifying the core architecture" success criterion actually rests on.
Adding provider #6 means one more entry in `_PROVIDER_CLASSES` (and,
if it needs its own env var names, one more branch in
`build_provider_config()`) - `LLMManagerAdapter` itself never changes.

Per-provider environment variables (`DEFAULT_MODEL`/`API_KEY`/
`BASE_URL`/`TIMEOUT`/`MAX_RETRIES` per provider, spec's own naming):

    OPENROUTER_API_KEY   OPENROUTER_BASE_URL   OPENROUTER_MODEL   OPENROUTER_TIMEOUT   OPENROUTER_MAX_RETRIES
    OPENAI_API_KEY       OPENAI_BASE_URL       OPENAI_MODEL       OPENAI_TIMEOUT       OPENAI_MAX_RETRIES
    GEMINI_API_KEY       GEMINI_BASE_URL       GEMINI_MODEL       GEMINI_TIMEOUT       GEMINI_MAX_RETRIES
    ANTHROPIC_API_KEY    ANTHROPIC_BASE_URL    ANTHROPIC_MODEL    ANTHROPIC_TIMEOUT    ANTHROPIC_MAX_RETRIES
    LOCAL_API_BASE (base url)  LOCAL_MODEL   LOCAL_API_KEY (optional)  LOCAL_TIMEOUT  LOCAL_MAX_RETRIES

`OPENROUTER_*` are the SAME env vars `luno.adapters.openrouter.
OpenRouterConfig` already reads - deliberately reused, not renamed, so
`LLM_PROVIDER=openrouter` (the default - zero behavior change for
anyone who hasn't touched their `.env`) produces byte-identical
provider configuration to before this sprint.

"Do not require every provider to have credentials. Only validate the
active provider." - `LLMManagerConfig.from_env()` never raises for a
provider with no credentials; `LLMManagerAdapter._build_client()` skips
provider construction entirely for a `ProviderNotConfiguredError`
(logged, never fatal) - see that module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import ProviderConfig

#: every provider name this package knows about, in the DEFAULT
#: priority order used when `LLM_PROVIDER_PRIORITY` isn't set -
#: OpenRouter first (it alone can route to every other vendor's models,
#: the most likely to have a real key configured), then the direct
#: vendor APIs, Local last (never costs money/rate-limits, but also the
#: only one that requires a human to have a server already running).
PROVIDER_NAMES: List[str] = ["openrouter", "openai", "gemini", "anthropic", "local"]

_DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
    "local": "http://localhost:1234/v1",
}

_ENV_PREFIX = {
    "openrouter": "OPENROUTER", "openai": "OPENAI", "gemini": "GEMINI",
    "anthropic": "ANTHROPIC", "local": "LOCAL",
}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_provider_config(name: str, *, default_timeout_s: float = 60.0, default_max_retries: int = 3) -> ProviderConfig:
    """Reads `{PREFIX}_API_KEY`/`{PREFIX}_BASE_URL`/`{PREFIX}_MODEL`/
    `{PREFIX}_TIMEOUT`/`{PREFIX}_MAX_RETRIES` for `name` - `local` reads
    `LOCAL_API_BASE` (not `LOCAL_BASE_URL`) and `LOCAL_API_KEY`
    (optional - see `local_provider.py`), matching the spec's own
    "Environment Variables" example names exactly."""
    prefix = _ENV_PREFIX.get(name, name.upper())
    if name == "local":
        api_key = os.getenv("LOCAL_API_KEY", "").strip()
        base_url = (os.getenv("LOCAL_API_BASE") or _DEFAULT_BASE_URLS["local"]).strip().rstrip("/")
        model = (os.getenv("LOCAL_MODEL") or "").strip() or None
    elif name == "openrouter":
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        base_url = (os.getenv(f"{prefix}_BASE_URL") or _DEFAULT_BASE_URLS.get(name, "")).strip().rstrip("/")
        # DEEPSEEK_MODEL (OpenAI-Primary/DeepSeek-Fallback sprint) takes
        # precedence over OPENROUTER_MODEL when set - this is what makes
        # an automatic priority-order fallback from "openai" land on a
        # correctly vendor-prefixed DeepSeek slug (see llm_manager.py's
        # fallback-model-reset fix) without every other OpenRouter use
        # having to change. Absent DEEPSEEK_MODEL, behavior is byte-
        # identical to before (OPENROUTER_MODEL alone).
        model = (os.getenv("DEEPSEEK_MODEL") or os.getenv(f"{prefix}_MODEL") or "").strip() or None
    else:
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        base_url = (os.getenv(f"{prefix}_BASE_URL") or _DEFAULT_BASE_URLS.get(name, "")).strip().rstrip("/")
        model = (os.getenv(f"{prefix}_MODEL") or "").strip() or None
    return ProviderConfig(
        provider=name, api_key=api_key, base_url=base_url, model=model,
        timeout_s=_float_env(f"{prefix}_TIMEOUT", default_timeout_s),
        max_tokens=_int_env(f"{prefix}_MAX_TOKENS", None) if os.getenv(f"{prefix}_MAX_TOKENS") else None,
        temperature=_float_env(f"{prefix}_TEMPERATURE", None) if os.getenv(f"{prefix}_TEMPERATURE") else None,
        stream_default=_bool_env(f"{prefix}_STREAM_DEFAULT", True),
        max_retries=_int_env(f"{prefix}_MAX_RETRIES", default_max_retries),
        retry_backoff_base_s=_float_env(f"{prefix}_RETRY_BACKOFF_S", 1.0),
        retry_backoff_max_s=_float_env(f"{prefix}_RETRY_BACKOFF_MAX_S", 20.0),
        debug_log_prompts=_bool_env(f"{prefix}_DEBUG_LOG_PROMPTS", False),
    )


def _provider_classes() -> Dict[str, Any]:
    """Imported lazily (inside a function, not at module import time) so
    importing `config.py` alone never pulls in all five provider
    modules - useful for tests that only want `LLMManagerConfig`."""
    from .anthropic_provider import AnthropicProvider
    from .gemini_provider import GeminiProvider
    from .local_provider import LocalProvider
    from .openai_provider import OpenAIProvider
    from .openrouter_provider import OpenRouterProvider
    return {
        "openrouter": OpenRouterProvider, "openai": OpenAIProvider, "gemini": GeminiProvider,
        "anthropic": AnthropicProvider, "local": LocalProvider,
    }


def build_provider_client(name: str, config: ProviderConfig, *, session: Any = None, sleep_fn: Any = None):
    """The name -> class factory every new provider registers itself
    into - `LLMManagerAdapter` never imports a provider module by name
    directly, only ever calls this. Raises `KeyError` for an unknown
    provider name (a configuration bug: `LLM_PROVIDER`/priority list
    contains a typo or a provider that hasn't been implemented yet) -
    `LLMManagerAdapter` catches and logs this rather than crashing
    Runtime over it."""
    classes = _provider_classes()
    if name not in classes:
        raise KeyError(f"unknown LLM provider '{name}' (known: {', '.join(sorted(classes))})")
    return classes[name](config, session=session, sleep_fn=sleep_fn)


@dataclass
class LLMManagerConfig:
    """Reloadable top-level config - see module docstring. `priority`
    is always a list with `provider` moved to the front (a request
    always tries the CONFIGURED active provider first, even if it's
    listed lower in `LLM_PROVIDER_PRIORITY` - that env var only orders
    the FALLBACK sequence after it)."""
    provider: str = "openrouter"
    priority: List[str] = field(default_factory=lambda: list(PROVIDER_NAMES))
    default_model: Optional[str] = None
    enable_fallback: bool = True
    enable_streaming: bool = True
    max_retries: int = 3
    timeout_s: float = 60.0
    health_poll_interval_s: float = 60.0
    fallback_on_invalid_request: bool = False
    #: OpenAI-Primary/DeepSeek-Fallback sprint - "Must NOT fallback for:
    #: invalid API key / authentication failure - these should produce a
    #: clear configuration/runtime error rather than silently hiding the
    #: problem behind DeepSeek." Default False (opt-IN, unlike
    #: `fallback_on_invalid_request` which existed before and stays
    #: default-off for a DIFFERENT reason - see `_fallback_eligible()` in
    #: `llm_manager.py`). No existing test exercised auth-error fallback
    #: at the manager level before this sprint, so this default doesn't
    #: change any previously-tested/relied-upon behavior.
    fallback_on_auth_error: bool = False

    @classmethod
    def from_env(cls) -> "LLMManagerConfig":
        provider = (os.getenv("LLM_PROVIDER") or "openrouter").strip().lower()
        if provider not in PROVIDER_NAMES:
            provider = "openrouter"

        raw_priority = os.getenv("LLM_PROVIDER_PRIORITY", "")
        if raw_priority.strip():
            priority = [p.strip().lower() for p in raw_priority.split(",") if p.strip()]
            priority = [p for p in priority if p in PROVIDER_NAMES]
        else:
            priority = list(PROVIDER_NAMES)

        # active provider always tried first, regardless of where it
        # sits in the configured priority list.
        priority = [provider] + [p for p in priority if p != provider]
        # append any provider missing from an incomplete custom list,
        # so fallback can still reach every known provider as a last resort.
        priority += [p for p in PROVIDER_NAMES if p not in priority]

        return cls(
            provider=provider,
            priority=priority,
            default_model=(os.getenv("DEFAULT_MODEL") or "").strip() or None,
            enable_fallback=_bool_env("ENABLE_FALLBACK", True),
            enable_streaming=_bool_env("ENABLE_STREAMING", True),
            max_retries=_int_env("MAX_RETRIES", 3),
            timeout_s=_float_env("TIMEOUT", 60.0),
            health_poll_interval_s=_float_env("LLM_HEALTH_POLL_INTERVAL_S", 60.0),
            fallback_on_invalid_request=_bool_env("LLM_FALLBACK_ON_INVALID_REQUEST", False),
            fallback_on_auth_error=_bool_env("LLM_FALLBACK_ON_AUTH_ERROR", False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "priority": list(self.priority), "default_model": self.default_model,
            "enable_fallback": self.enable_fallback, "enable_streaming": self.enable_streaming,
            "max_retries": self.max_retries, "timeout_s": self.timeout_s,
            "health_poll_interval_s": self.health_poll_interval_s,
        }
