"""
config.py
=========

`RoutingConfig` - the spec's own "Configuration" section, env-only and
reloadable (same `from_env()` convention as `LLMManagerConfig`/
`BargeInConfig`/`WakeSessionConfig`/`MemoryRetrievalConfig`).

Environment variables (all optional, sensible defaults - a fresh
checkout with none of these set gets exactly today's behavior:
DeepSeek-equivalent default provider, auto-routing ON, nothing breaks
if left unconfigured):

    DEFAULT_PROVIDER                default cheap/fast provider alias
                                     (default: "deepseek")
    REASONING_PROVIDER              reasoning-heavy provider alias
                                     (default: "gpt")
    SEARCH_PROVIDER                 knowledge-retrieval provider name -
                                     only "tavily" is implemented today
                                     (default: "tavily")
    ENABLE_AUTO_ROUTING             master switch - false makes the
                                     Decision Engine classify/log but
                                     never actually override
                                     provider/model (default: true)
    ENABLE_COST_OPTIMIZER           bias provider selection toward the
                                     cheaper option when capability is
                                     equivalent (default: true)
    ENABLE_PROVIDER_AFFINITY        sticky reasoning provider mid-
                                     conversation (default: true)
    ENABLE_WEB_SEARCH               allow the Decision Engine to trigger
                                     Tavily at all (default: true - still
                                     requires TAVILY_API_KEY to actually
                                     fire, see `web_search_router.py`)
    REASONING_COMPLEXITY_THRESHOLD  complexity level ("low"/"medium"/
                                     "high"/"extreme") at or above which
                                     the reasoning provider is used even
                                     without a reasoning-flavored intent
                                     (default: "high")

Efficient LLM Classifier sprint (see `llm_classifier.py`/`confirmation.py`) -
a small, OPTIONAL GPT-5.4-nano fallback classifier for utterances the
deterministic `intent_classifier.classify_intent()` couldn't place at all
(fell back to GENERAL_QUESTION/GENERAL_CHAT). Fully inert unless BOTH
`CLASSIFIER_ENABLED=true` AND a real classifier client is wired in (see
`luno/bootstrap/adapters.py::register_intent_classifier()` - same opt-in-
by-construction convention as the device-intent classifier):

    CLASSIFIER_ENABLED               master switch (default: false - this
                                      is a genuinely optional feature, not
                                      on by default even with a real LLM
                                      provider configured)
    CLASSIFIER_MODEL                 model id passed to the classifier
                                      call (default: "gpt-5.4-nano")
    CLASSIFIER_CONFIDENCE_THRESHOLD  confidence >= this -> route on the
                                      classifier's own intent, no
                                      confirmation needed (default: 0.80)
    CLASSIFIER_CONFIRMATION_THRESHOLD  confidence >= this but < the high
                                      threshold -> route on the
                                      classifier's intent BUT
                                      `needs_confirmation=True` (default:
                                      0.55). Below this, the classifier's
                                      result is discarded entirely.
    CLASSIFIER_TIMEOUT_MS             per-call timeout (default: 1500)
    CLASSIFIER_MAX_INPUT_CHARS        `text` is truncated to this many
                                      characters before being sent (default:
                                      2000) - see spec section 8, "context
                                      minimization"
    CLASSIFIER_CACHE_TTL_SECONDS      identical `text` within this window
                                      reuses the last result instead of
                                      re-calling the API (default: 30, 0
                                      disables caching)

`DEFAULT_PROVIDER`/`REASONING_PROVIDER` are ALIASES, not necessarily one
of `luno.adapters.llm.config.PROVIDER_NAMES` - see `provider_selector.py`
`resolve_alias()` for how e.g. `"deepseek"` becomes a real
`(provider, model)` pair (today: routed through OpenRouter, since that's
the one provider that can already reach a DeepSeek model - see that
module's own docstring for the full reasoning). Setting either one to a
real provider name directly (e.g. `REASONING_PROVIDER=anthropic`) also
works - `resolve_alias()` passes those straight through.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


_VALID_COMPLEXITY = ("low", "medium", "high", "extreme")


_VALID_REASONING_EFFORT = ("none", "low", "medium", "high", "xhigh", "max")


@dataclass
class RoutingConfig:
    default_provider_alias: str = "deepseek"
    reasoning_provider_alias: str = "gpt"
    search_provider: str = "tavily"
    enable_auto_routing: bool = True
    enable_cost_optimizer: bool = True
    enable_provider_affinity: bool = True
    enable_web_search: bool = True
    reasoning_complexity_threshold: str = "high"
    #: OpenAI `reasoning_effort` value attached to `RoutingDecision` for
    #: the daily/reasoning branches respectively (LLM_DAILY_REASONING_EFFORT/
    #: LLM_REASONING_EFFORT) - only the `openai` provider client reads this
    #: (see `OpenAIProvider._extra_payload_fields()`); every other provider
    #: silently ignores it.
    daily_reasoning_effort: str = "low"
    reasoning_effort: str = "high"
    classifier_enabled: bool = False
    classifier_model: str = "gpt-5.4-nano"
    classifier_confidence_threshold: float = 0.80
    classifier_confirmation_threshold: float = 0.55
    classifier_timeout_ms: int = 1500
    classifier_max_input_chars: int = 2000
    classifier_cache_ttl_s: float = 30.0

    @classmethod
    def from_env(cls) -> "RoutingConfig":
        threshold = (os.getenv("REASONING_COMPLEXITY_THRESHOLD") or "high").strip().lower()
        if threshold not in _VALID_COMPLEXITY:
            threshold = "high"
        daily_effort = (os.getenv("LLM_DAILY_REASONING_EFFORT") or "low").strip().lower()
        if daily_effort not in _VALID_REASONING_EFFORT:
            daily_effort = "low"
        reasoning_effort = (os.getenv("LLM_REASONING_EFFORT") or "high").strip().lower()
        if reasoning_effort not in _VALID_REASONING_EFFORT:
            reasoning_effort = "high"
        return cls(
            default_provider_alias=(os.getenv("DEFAULT_PROVIDER") or "deepseek").strip().lower(),
            reasoning_provider_alias=(os.getenv("REASONING_PROVIDER") or "gpt").strip().lower(),
            search_provider=(os.getenv("SEARCH_PROVIDER") or "tavily").strip().lower(),
            enable_auto_routing=_bool_env("ENABLE_AUTO_ROUTING", True),
            enable_cost_optimizer=_bool_env("ENABLE_COST_OPTIMIZER", True),
            enable_provider_affinity=_bool_env("ENABLE_PROVIDER_AFFINITY", True),
            enable_web_search=_bool_env("ENABLE_WEB_SEARCH", True),
            reasoning_complexity_threshold=threshold,
            daily_reasoning_effort=daily_effort,
            reasoning_effort=reasoning_effort,
            classifier_enabled=_bool_env("CLASSIFIER_ENABLED", False),
            classifier_model=(os.getenv("CLASSIFIER_MODEL") or "gpt-5.4-nano").strip(),
            classifier_confidence_threshold=_float_env("CLASSIFIER_CONFIDENCE_THRESHOLD", 0.80),
            classifier_confirmation_threshold=_float_env("CLASSIFIER_CONFIRMATION_THRESHOLD", 0.55),
            classifier_timeout_ms=_int_env("CLASSIFIER_TIMEOUT_MS", 1500),
            classifier_max_input_chars=_int_env("CLASSIFIER_MAX_INPUT_CHARS", 2000),
            classifier_cache_ttl_s=_float_env("CLASSIFIER_CACHE_TTL_SECONDS", 30.0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_provider_alias": self.default_provider_alias,
            "reasoning_provider_alias": self.reasoning_provider_alias,
            "search_provider": self.search_provider,
            "enable_auto_routing": self.enable_auto_routing,
            "enable_cost_optimizer": self.enable_cost_optimizer,
            "enable_provider_affinity": self.enable_provider_affinity,
            "enable_web_search": self.enable_web_search,
            "reasoning_complexity_threshold": self.reasoning_complexity_threshold,
            "daily_reasoning_effort": self.daily_reasoning_effort,
            "reasoning_effort": self.reasoning_effort,
            "classifier_enabled": self.classifier_enabled,
            "classifier_model": self.classifier_model,
            "classifier_confidence_threshold": self.classifier_confidence_threshold,
            "classifier_confirmation_threshold": self.classifier_confirmation_threshold,
            "classifier_timeout_ms": self.classifier_timeout_ms,
            "classifier_max_input_chars": self.classifier_max_input_chars,
            "classifier_cache_ttl_s": self.classifier_cache_ttl_s,
        }
