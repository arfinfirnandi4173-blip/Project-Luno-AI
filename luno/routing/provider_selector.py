"""
provider_selector.py
======================

`resolve_alias()` - turns a routing-level PROVIDER ALIAS ("deepseek",
"gpt", or any real `luno.adapters.llm.config.PROVIDER_NAMES` entry
passed straight through) into the concrete `(provider, model)` pair
`LLMManagerAdapter` actually understands (`data["provider"]`/
`data["model"]` on `NeedLLMResponse` - see that module's
`_handle_need_llm_response`).

Design note (documented once here, referenced from the module docstring
and this sprint's own outstanding design question): `DEFAULT_PROVIDER`/
`REASONING_PROVIDER` in the spec are named after MODEL FAMILIES
("DeepSeek", "GPT"), not provider adapters - the Multi-LLM Provider
System sprint's `PROVIDER_NAMES` are VENDORS/backends
(`openrouter`/`openai`/`gemini`/`anthropic`/`local`). There is no
"deepseek" or "gpt" entry in that list and there doesn't need to be:
OpenRouter can already reach a DeepSeek model (the project's own `.env`
already sets `OPENROUTER_MODEL` to a DeepSeek slug), and OpenAI's GPT
models are reachable either directly via the `openai` provider (if
`OPENAI_API_KEY` is set) or via OpenRouter otherwise. `resolve_alias()`
is the one, single place that encodes this mapping - `ProviderSelector`/
`DecisionEngine` never hard-code a vendor name themselves.

`ROUTING_DEEPSEEK_MODEL`/`ROUTING_GPT_MODEL` (optional env vars) let a
deployer point either alias at a specific model slug without touching
code; absent, sensible defaults are used (and `OPENROUTER_MODEL` is
reused for "deepseek" - zero new configuration needed for the common
case where OpenRouter is already the only configured provider).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional, Tuple

from .models import REASONING_INTENTS, ComplexityLevel, Intent, complexity_at_least

if TYPE_CHECKING:
    from .config import RoutingConfig

try:
    from luno.adapters.llm.config import PROVIDER_NAMES
except Exception:  # pragma: no cover - keeps this package importable standalone in tests
    PROVIDER_NAMES = ["openrouter", "openai", "gemini", "anthropic", "local"]

_DEEPSEEK_ALIASES = {"deepseek", "deepseek-lightning", "deepseek_lightning", "deepseek-v4", "deepseek4"}
_GPT_ALIASES = {"gpt", "gpt4", "gpt-4", "openai_gpt", "chatgpt"}
#: OpenAI-Primary/DeepSeek-Fallback sprint - the Model Router's two new
#: aliases, distinct from the older single-model "gpt" alias above (kept
#: for backward compat, e.g. `luno.tool_manager.builtin.llm_mode`'s manual
#: voice-override word list). "daily"/"reasoning" resolve to the SAME
#: `openai` provider, just a different configured model id + a different
#: `reasoning_effort` (see `ProviderSelector.select()`), NOT a different
#: vendor - only `DEFAULT_PROVIDER`/`REASONING_PROVIDER` normally point
#: at these; a human can still say "gpt"/"deepseek" and get the older
#: single-model behavior via `luno.tool_manager.builtin.llm_mode`.
_OPENAI_DAILY_ALIASES = {"openai_daily", "daily"}
_OPENAI_REASONING_ALIASES = {"openai_reasoning", "reasoning"}


def resolve_alias(alias: Optional[str]) -> Tuple[str, Optional[str]]:
    """Returns `(provider_name, model_or_None)`. `provider_name` is
    always one of `PROVIDER_NAMES` (falls back to `"openrouter"` for an
    unrecognized alias - fails open to the same default
    `LLMManagerConfig` itself defaults to, never raises)."""
    a = (alias or "").strip().lower()
    if not a:
        return "openrouter", None
    if a in PROVIDER_NAMES:
        return a, None
    if a in _DEEPSEEK_ALIASES:
        model = (os.getenv("DEEPSEEK_MODEL") or os.getenv("ROUTING_DEEPSEEK_MODEL") or os.getenv("OPENROUTER_MODEL") or "deepseek/deepseek-chat").strip()
        return "openrouter", model or None
    if a in _OPENAI_DAILY_ALIASES:
        return _resolve_openai_alias(os.getenv("OPENAI_DAILY_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.6-luna")
    if a in _OPENAI_REASONING_ALIASES:
        return _resolve_openai_alias(os.getenv("OPENAI_REASONING_MODEL") or "gpt-5.6-sol")
    if a in _GPT_ALIASES:
        if os.getenv("OPENAI_API_KEY", "").strip():
            model = (os.getenv("ROUTING_GPT_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1").strip()
            return "openai", model or None
        model = (os.getenv("ROUTING_GPT_MODEL") or "openai/gpt-4.1").strip()
        return "openrouter", model or None
    # unknown alias - fail open: no model override, let LLMManagerAdapter's
    # own configured default/priority order decide.
    return "openrouter", None


def _resolve_openai_alias(model: str) -> Tuple[str, Optional[str]]:
    """Shared by the daily/reasoning aliases: reach `model` directly via
    the `openai` provider if `OPENAI_API_KEY` is configured, otherwise
    fail open through OpenRouter with an `openai/`-prefixed slug (same
    fail-open shape `_GPT_ALIASES` already uses) rather than raising -
    `LLMManagerAdapter` itself is the one place allowed to decide a
    provider is truly unusable (see that module's `_usable_client()`)."""
    model = (model or "").strip()
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai", model or None
    return "openrouter", (f"openai/{model}" if model else None)


class ProviderSelector:
    """Spec's "LLM Routing"/"Adaptive LLM Selection"/"Cost Optimization":
    picks the reasoning-capable alias when the turn's complexity meets
    `config.reasoning_complexity_threshold` OR its intent(s) are
    inherently reasoning-flavored (REASONING/PLANNING/CODING/
    MULTI_STEP), otherwise the cheap/fast default - biased toward the
    cheap default whenever `enable_cost_optimizer` is on and the turn
    doesn't clearly need more (the optimizer's whole job is "don't pay
    for GPT-level reasoning on a routine turn")."""

    def __init__(self, config: "RoutingConfig") -> None:
        self.config = config

    def select(self, intents: List[Intent], complexity: ComplexityLevel, reasoning: List[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
        threshold = ComplexityLevel(self.config.reasoning_complexity_threshold)
        intent_set = set(intents)
        reasoning_intent_hit = bool(intent_set & REASONING_INTENTS)
        complexity_hit = complexity_at_least(complexity, threshold)

        wants_reasoning = reasoning_intent_hit or complexity_hit
        if wants_reasoning and self.config.enable_cost_optimizer and not reasoning_intent_hit and complexity == threshold:
            # borderline: complexity JUST meets the threshold purely on
            # length/vague signals, no actual reasoning-flavored intent -
            # the cost optimizer keeps this one on the cheap provider
            # rather than escalating on a marginal score alone.
            wants_reasoning = False
            reasoning.append(
                f"cost optimizer: complexity={complexity.value} only marginally meets threshold "
                f"'{threshold.value}' with no reasoning-flavored intent - staying on the default provider"
            )

        if wants_reasoning:
            alias = self.config.reasoning_provider_alias
            effort = self.config.reasoning_effort
            reasoning.append(
                f"reasoning provider selected (intent reasoning-flavored={reasoning_intent_hit}, "
                f"complexity={complexity.value} >= threshold={threshold.value}={complexity_hit})"
            )
        else:
            alias = self.config.default_provider_alias
            effort = self.config.daily_reasoning_effort
            reasoning.append(f"default provider selected (complexity={complexity.value}, routine intent)")

        provider, model = resolve_alias(alias)
        reasoning.append(f"alias '{alias}' -> provider='{provider}'" + (f", model='{model}'" if model else ""))
        # `reasoning_effort` only ever means something to the `openai`
        # provider (see `OpenAIProvider._extra_payload_fields()`) - still
        # returned unconditionally here (cheap, and every OTHER provider's
        # client simply never reads it) rather than special-cased per
        # provider name, so a deployer switching DEFAULT_PROVIDER/
        # REASONING_PROVIDER straight to "openai_daily"/"openai_reasoning"
        # later doesn't need this method touched again.
        return alias, provider, model, effort
