"""
openrouter_provider.py
=======================

`OpenRouterProvider` - thin `OpenAICompatibleClient` subclass wrapping
OpenRouter's `https://openrouter.ai/api/v1` endpoint. This is a NEW,
separate implementation from `luno.adapters.openrouter.OpenRouterAdapter`
(deliberately untouched by this sprint - every one of its own tests
keeps passing unmodified, and `bootstrap/adapters.py` continues to
build/register that exact class under the module id `"openrouter"`; see
that file's own comment for why). This module is only used from inside
`LLMManagerAdapter` (`luno.adapters.llm_manager`) as ONE of five
selectable providers - the two `OpenRouterAdapter` classes coexist and
never call each other.

Model catalog is intentionally small (a handful of well-known slugs)
rather than exhaustive - `get_model_info()` on anything not listed
still works (falls back to "cost unknown"), and OpenRouter itself
accepts any slug it routes, catalog entry or not.
"""

from __future__ import annotations

from typing import Optional

from .base import OpenAICompatibleClient


class OpenRouterProvider(OpenAICompatibleClient):
    name = "openrouter"

    _SUPPORTS_STREAMING = True
    _SUPPORTS_TOOLS = True
    _SUPPORTS_IMAGES = True  # many routed models (Claude, GPT-4o, Gemini, ...) accept image content parts
    _SUPPORTS_REASONING = True  # OpenRouter routes reasoning-capable models (o1/o3, DeepSeek-R1, ...)

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    #: display/cost metadata only - OpenRouter accepts any slug it
    #: routes regardless of whether it's listed here.
    _MODEL_CATALOG = {
        "anthropic/claude-sonnet-4.5": {"display_name": "Claude Sonnet 4.5 (via OpenRouter)", "context_tokens": 200000, "input_cost_per_1m": 3.0, "output_cost_per_1m": 15.0},
        "anthropic/claude-opus-4.1": {"display_name": "Claude Opus 4.1 (via OpenRouter)", "context_tokens": 200000, "input_cost_per_1m": 15.0, "output_cost_per_1m": 75.0},
        "openai/gpt-4.1": {"display_name": "GPT-4.1 (via OpenRouter)", "context_tokens": 1000000, "input_cost_per_1m": 2.0, "output_cost_per_1m": 8.0},
        "openai/gpt-5": {"display_name": "GPT-5 (via OpenRouter)", "context_tokens": 400000, "input_cost_per_1m": 5.0, "output_cost_per_1m": 15.0},
        "google/gemini-2.5-pro": {"display_name": "Gemini 2.5 Pro (via OpenRouter)", "context_tokens": 1000000, "input_cost_per_1m": 1.25, "output_cost_per_1m": 5.0},
        "google/gemini-2.5-flash": {"display_name": "Gemini 2.5 Flash (via OpenRouter)", "context_tokens": 1000000, "input_cost_per_1m": 0.075, "output_cost_per_1m": 0.3},
        "deepseek/deepseek-v4-flash": {"display_name": "DeepSeek V4 Flash (via OpenRouter)", "context_tokens": 128000, "input_cost_per_1m": 0.2, "output_cost_per_1m": 0.8},
        "qwen/qwen3-max": {"display_name": "Qwen3 Max (via OpenRouter)", "context_tokens": 128000, "input_cost_per_1m": 0.4, "output_cost_per_1m": 1.2},
    }

    def _headers(self):
        headers = super()._headers()
        with self._lock:
            cfg = self.config
        referer = getattr(cfg, "referer", None) or ""
        app_title = getattr(cfg, "app_title", None) or ""
        if referer:
            headers["HTTP-Referer"] = referer
        if app_title:
            headers["X-Title"] = app_title
        return headers
