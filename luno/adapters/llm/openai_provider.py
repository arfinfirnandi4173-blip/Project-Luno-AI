"""
openai_provider.py
===================

`OpenAIProvider` - thin `OpenAICompatibleClient` subclass wrapping the
real OpenAI REST API (`https://api.openai.com/v1`). Distinct from the
`OPENAI_API_KEY` / `openai` SDK client `legacy_main.py` builds lazily
for its own unrelated chat-completion call sites (see that file's
`_get_client()`) - this is a plain `requests`-based client, consistent
with every other provider in this package, and reused independently by
`LLMManagerAdapter`.

Model catalog verified live against
https://developers.openai.com/api/docs/models on 2026-08-01
(OpenAI-Primary/DeepSeek-Fallback sprint - the sprint's own suggested
defaults, "gpt-5.4"/"gpt-5.4-mini", are a stale/superseded generation as
of that date and were deliberately NOT hardcoded; the current frontier
family fetched from OpenAI's own docs is "gpt-5.6"). The older gpt-5/
gpt-4.1/gpt-4o entries are kept for backward compat (existing
`OPENAI_MODEL=gpt-4.1` deployments, `resolve_alias()`'s "gpt" alias) -
nothing here removes or renames a previously-working model id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import OpenAICompatibleClient

#: OpenAI's own valid `reasoning_effort` values (Responses/Chat
#: Completions API, per https://developers.openai.com/api/docs/guides/reasoning)
#: - anything else is dropped rather than sent through and rejected by
#: the API as an invalid request.
_VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


class OpenAIProvider(OpenAICompatibleClient):
    name = "openai"

    _SUPPORTS_STREAMING = True
    _SUPPORTS_TOOLS = True
    _SUPPORTS_IMAGES = True
    _SUPPORTS_REASONING = True  # o1/o3/gpt-5.x family

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    _MODEL_CATALOG = {
        # current frontier family (verified 2026-08-01) - see module docstring.
        "gpt-5.6-sol": {"display_name": "GPT-5.6 Sol", "context_tokens": 1050000, "input_cost_per_1m": 5.0, "output_cost_per_1m": 30.0},
        "gpt-5.6-terra": {"display_name": "GPT-5.6 Terra", "context_tokens": 1050000, "input_cost_per_1m": 2.0, "output_cost_per_1m": 12.0},
        "gpt-5.6-luna": {"display_name": "GPT-5.6 Luna", "context_tokens": 1050000, "input_cost_per_1m": 0.20, "output_cost_per_1m": 1.20},
        # older generations - kept for backward compat, not removed/renamed.
        "gpt-5": {"display_name": "GPT-5", "context_tokens": 400000, "input_cost_per_1m": 5.0, "output_cost_per_1m": 15.0},
        "gpt-4.1": {"display_name": "GPT-4.1", "context_tokens": 1000000, "input_cost_per_1m": 2.0, "output_cost_per_1m": 8.0},
        "gpt-4.1-mini": {"display_name": "GPT-4.1 Mini", "context_tokens": 1000000, "input_cost_per_1m": 0.4, "output_cost_per_1m": 1.6},
        "gpt-4o": {"display_name": "GPT-4o", "context_tokens": 128000, "input_cost_per_1m": 2.5, "output_cost_per_1m": 10.0},
        "gpt-4o-mini": {"display_name": "GPT-4o Mini", "context_tokens": 128000, "input_cost_per_1m": 0.15, "output_cost_per_1m": 0.6},
    }

    def _extra_payload_fields(self, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Injects `reasoning_effort` into the request body when the
        Model Router attached one (see `luno.routing.RoutingConfig`'s
        `daily_reasoning_effort`/`reasoning_effort` and
        `main_runtime_demo.py`'s `llm_request_data["metadata"]`).
        Silently dropped if missing/invalid - never a reason to fail the
        whole request over a routing hint gone stale.

        Efficient LLM Classifier sprint: also passes through
        `metadata["response_format"]` verbatim when present - OpenAI's
        Structured Outputs (`{"type": "json_schema", "json_schema": {...},
        "strict": true}`) request-body field. `luno.routing.llm_classifier`
        is the one caller that ever sets this (via `chat_once(...,
        metadata={"response_format": {...}})`); every other caller's
        `metadata` simply doesn't have this key, so this is a no-op for
        them. Only a `dict` is ever forwarded - a malformed/wrong-typed
        value is dropped rather than sent to the API and rejected."""
        fields: Dict[str, Any] = {}
        effort = (metadata or {}).get("reasoning_effort")
        if effort is not None:
            effort = str(effort).strip().lower()
            if effort in _VALID_REASONING_EFFORTS:
                fields["reasoning_effort"] = effort
        response_format = (metadata or {}).get("response_format")
        if isinstance(response_format, dict):
            fields["response_format"] = response_format
        return fields
