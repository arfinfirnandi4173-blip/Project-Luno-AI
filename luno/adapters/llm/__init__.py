"""
luno.adapters.llm
==================

Multi-LLM-Provider System (Sprint - "Multi-LLM Provider System"). This
package is the provider layer underneath `luno.adapters.llm_manager`'s
`LLMManagerAdapter` - it never talks to the Event Bus, never knows
about `NeedLLMResponse`/`AssistantResponse`, and never decides which
provider is "active". Its only job is: given a provider name, produce
an `LLMProviderClient` that can `chat()`/`stream_chat()` against that
provider, all through the exact same interface (`base.LLMProviderClient`)
regardless of which of the five providers it wraps.

    errors.py    - provider-agnostic error hierarchy (auth/rate-limit/
                   server/timeout/network/invalid-request/stream),
                   generalized from `luno.adapters.openrouter`'s
                   original OpenRouter-only classes.
    models.py    - shared dataclasses: `ChatMessage`, `ChatResult`,
                   `LLMStreamChunk`, `ModelInfo`, `ProviderCapabilities`,
                   `ProviderHealth` (+ `HealthState` enum), `ProviderConfig`.
    base.py      - `LLMProviderClient` ABC (the spec's required methods:
                   initialize/shutdown/health/chat/stream_chat/cancel/
                   reload/supports_streaming/supports_tools/
                   supports_images/supports_reasoning/get_model_info),
                   `_retry()` shared backoff helper, and
                   `OpenAICompatibleClient` - a base class for the three
                   providers (OpenRouter, OpenAI, Local) that all speak
                   the same `POST /chat/completions` REST shape.
    mock.py      - `MockProviderClient` - scriptable, no-network client
                   usable as ANY provider name, for every test in this
                   package and in `luno.adapters.llm_manager`'s own tests.
    config.py    - `LLMManagerConfig` (`LLM_PROVIDER`, priority list,
                   per-provider env vars, `ENABLE_FALLBACK`/
                   `ENABLE_STREAMING`, ...) and the provider-name ->
                   factory registry `build_provider_client()`.
    openrouter_provider.py / openai_provider.py / local_provider.py
                 - thin `OpenAICompatibleClient` subclasses (base URL +
                   auth header + capability table only).
    gemini_provider.py / anthropic_provider.py
                 - full, independent REST implementations (different
                   request/response shape from OpenAI's - see each
                   module's own docstring).

Swapping/adding a provider means writing one more `LLMProviderClient`
implementation and registering it in `config.py`'s factory table -
`LLMManagerAdapter`, every event type, Planner, Behavior Tree, Memory
Retrieval, Dashboard, and every other Core/adapter package are
untouched. That is the entire point of this package's existence.
"""

from __future__ import annotations
