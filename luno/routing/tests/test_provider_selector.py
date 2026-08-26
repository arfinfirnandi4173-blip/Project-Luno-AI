"""
test_provider_selector.py
============================

`resolve_alias()` (deepseek/gpt alias -> real provider,model) and
`ProviderSelector.select()` (spec's "LLM Routing"/"Cost Optimization").
"""

from __future__ import annotations

import os

import pytest

from luno.routing.config import RoutingConfig
from luno.routing.models import ComplexityLevel, Intent
from luno.routing.provider_selector import ProviderSelector, resolve_alias


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "ROUTING_DEEPSEEK_MODEL", "ROUTING_GPT_MODEL", "OPENROUTER_MODEL", "OPENAI_MODEL",
        "OPENAI_DAILY_MODEL", "OPENAI_REASONING_MODEL", "DEEPSEEK_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_resolve_deepseek_alias_routes_through_openrouter():
    provider, model = resolve_alias("deepseek")
    assert provider == "openrouter"
    assert model  # some default deepseek model slug


def test_resolve_deepseek_alias_reuses_openrouter_model_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    provider, model = resolve_alias("deepseek")
    assert provider == "openrouter"
    assert model == "deepseek/deepseek-v4-flash"


def test_resolve_gpt_alias_without_openai_key_routes_through_openrouter():
    provider, model = resolve_alias("gpt")
    assert provider == "openrouter"
    assert "gpt" in model.lower()


def test_resolve_gpt_alias_with_openai_key_routes_direct(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    provider, model = resolve_alias("gpt")
    assert provider == "openai"


def test_resolve_real_provider_name_passthrough():
    provider, model = resolve_alias("anthropic")
    assert provider == "anthropic"
    assert model is None


def test_resolve_unknown_alias_fails_open_to_openrouter():
    provider, model = resolve_alias("totally-unknown-thing")
    assert provider == "openrouter"
    assert model is None


def test_resolve_empty_alias():
    provider, model = resolve_alias("")
    assert provider == "openrouter"
    provider2, model2 = resolve_alias(None)
    assert provider2 == "openrouter"


def test_selector_picks_default_for_routine_low_complexity():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt")
    selector = ProviderSelector(cfg)
    reasoning = []
    alias, provider, model, effort = selector.select([Intent.GENERAL_CHAT], ComplexityLevel.LOW, reasoning)
    assert alias == "deepseek"
    assert reasoning  # explanation trail populated
    assert effort == cfg.daily_reasoning_effort


def test_selector_picks_reasoning_for_coding_intent_even_at_low_complexity():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt")
    selector = ProviderSelector(cfg)
    alias, provider, model, effort = selector.select([Intent.CODING], ComplexityLevel.LOW, [])
    assert alias == "gpt"
    assert effort == cfg.reasoning_effort


def test_selector_picks_reasoning_when_complexity_exceeds_threshold():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", reasoning_complexity_threshold="high")
    selector = ProviderSelector(cfg)
    alias, _, _, _ = selector.select([Intent.GENERAL_QUESTION], ComplexityLevel.EXTREME, [])
    assert alias == "gpt"


def test_cost_optimizer_holds_back_on_marginal_threshold_match():
    cfg = RoutingConfig(
        default_provider_alias="deepseek", reasoning_provider_alias="gpt",
        reasoning_complexity_threshold="high", enable_cost_optimizer=True,
    )
    selector = ProviderSelector(cfg)
    # exactly at threshold, no reasoning-flavored intent - cost optimizer keeps it cheap
    alias, _, _, _ = selector.select([Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias == "deepseek"


def test_cost_optimizer_disabled_escalates_on_marginal_threshold_match():
    cfg = RoutingConfig(
        default_provider_alias="deepseek", reasoning_provider_alias="gpt",
        reasoning_complexity_threshold="high", enable_cost_optimizer=False,
    )
    selector = ProviderSelector(cfg)
    alias, _, _, _ = selector.select([Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias == "gpt"


def test_extreme_complexity_always_escalates_regardless_of_cost_optimizer():
    cfg = RoutingConfig(
        default_provider_alias="deepseek", reasoning_provider_alias="gpt",
        reasoning_complexity_threshold="high", enable_cost_optimizer=True,
    )
    selector = ProviderSelector(cfg)
    alias, _, _, _ = selector.select([Intent.GENERAL_QUESTION], ComplexityLevel.EXTREME, [])
    assert alias == "gpt"


def test_selector_with_direct_provider_names_as_aliases():
    """A deployer can set DEFAULT_PROVIDER=anthropic directly (not an
    alias at all) - resolve_alias must pass it straight through."""
    cfg = RoutingConfig(default_provider_alias="anthropic", reasoning_provider_alias="gemini")
    selector = ProviderSelector(cfg)
    alias, provider, model, effort = selector.select([Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    assert alias == "anthropic"
    assert provider == "anthropic"


# ---------------------------------------------------------------------------
# OpenAI-Primary/DeepSeek-Fallback sprint - openai_daily/openai_reasoning
# aliases + reasoning_effort plumbing
# ---------------------------------------------------------------------------

def test_resolve_openai_daily_alias_with_key_routes_direct(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_DAILY_MODEL", "gpt-5.6-luna")
    provider, model = resolve_alias("openai_daily")
    assert provider == "openai"
    assert model == "gpt-5.6-luna"


def test_resolve_openai_reasoning_alias_with_key_routes_direct(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_REASONING_MODEL", "gpt-5.6-sol")
    provider, model = resolve_alias("openai_reasoning")
    assert provider == "openai"
    assert model == "gpt-5.6-sol"


def test_resolve_openai_daily_alias_without_key_fails_open_to_openrouter():
    provider, model = resolve_alias("openai_daily")
    assert provider == "openrouter"
    assert model.startswith("openai/")


def test_resolve_deepseek_alias_prefers_deepseek_model_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("OPENROUTER_MODEL", "some-other-slug")
    provider, model = resolve_alias("deepseek")
    assert provider == "openrouter"
    assert model == "deepseek/deepseek-v4-flash"


def test_selector_daily_route_uses_openai_daily_alias_and_low_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_DAILY_MODEL", "gpt-5.6-luna")
    cfg = RoutingConfig(default_provider_alias="openai_daily", reasoning_provider_alias="openai_reasoning", daily_reasoning_effort="low", reasoning_effort="high")
    selector = ProviderSelector(cfg)
    alias, provider, model, effort = selector.select([Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    assert alias == "openai_daily"
    assert provider == "openai"
    assert model == "gpt-5.6-luna"
    assert effort == "low"


def test_selector_reasoning_route_uses_openai_reasoning_alias_and_high_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_REASONING_MODEL", "gpt-5.6-sol")
    cfg = RoutingConfig(default_provider_alias="openai_daily", reasoning_provider_alias="openai_reasoning", daily_reasoning_effort="low", reasoning_effort="high")
    selector = ProviderSelector(cfg)
    alias, provider, model, effort = selector.select([Intent.CODING], ComplexityLevel.LOW, [])
    assert alias == "openai_reasoning"
    assert provider == "openai"
    assert model == "gpt-5.6-sol"
    assert effort == "high"
