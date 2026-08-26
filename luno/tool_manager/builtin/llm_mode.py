"""
llm_mode.py
============

`LLMModeHandler` - lets the user flip the Intelligent AI Routing
Engine's auto/manual LLM selection LIVE, by voice/text command, with no
`.env` edit and no restart. `RoutingConfig.enable_auto_routing` (see
`luno/routing/config.py`) is still the `.env`-only STARTUP default; this
tool controls a separate, in-process runtime override
(`luno.routing.mode_state.RUNTIME_MODE`) that always wins over it once
set - see `DecisionEngine.decide()`'s own precedence comment.

There is no mock/real split here (unlike every other builtin handler in
this package) - this tool has no external hardware/network dependency
to fake; it only flips an in-process flag, so the one implementation
below is registered unconditionally by `register_all()`, real from the
start.

Actions:
    set_auto     - hand provider/model selection back to the Decision
                   Engine (the default at startup).
    set_manual    - freeze the provider. `tool_call.target`, if given,
                   is a provider alias/name (see `_KNOWN_ALIASES` below
                   and `luno/routing/provider_selector.py::
                   resolve_alias()`); if omitted, "manual" still takes
                   effect but with no specific provider pinned (mirrors
                   the pre-existing `ENABLE_AUTO_ROUTING=false` `.env`
                   behavior: `LLMManagerAdapter`'s own configured
                   default/fallback order decides).

See `luno/planner/parser.py`'s `_classify_llm_mode()` for the phrasings
that route here ("pakai llm manual", "pakai llm openai", "llm otomatis",
"switch llm to auto", ...).
"""

from __future__ import annotations

from typing import List, Optional

from luno.routing.mode_state import RUNTIME_MODE

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["set_auto", "set_manual"]

# Kept in sync BY CONVENTION (not by import - the routing package is
# deliberately dependency-free from the rest of `luno`, see its own
# package docstring) with `luno/routing/provider_selector.py`'s
# `PROVIDER_NAMES`/`_DEEPSEEK_ALIASES`/`_GPT_ALIASES` and
# `luno/planner/parser.py`'s `_LLM_PROVIDER_WORDS`. `resolve_alias()`
# itself never raises on an unrecognized alias (fails open to
# OpenRouter with no model override) - this set is only used here for a
# HELPFUL error message, not a hard gate the Decision Engine also
# depends on.
_KNOWN_ALIASES = {
    "openrouter", "openai", "gemini", "anthropic", "local",
    "deepseek", "gpt", "chatgpt", "claude",
}


class LLMModeHandler(ToolHandler):
    name = "llm_mode"
    default_timeout_s = 2.0
    max_timeout_s = 5.0

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action == "set_manual" and tool_call.target:
            alias = tool_call.target.strip().lower()
            if alias not in _KNOWN_ALIASES:
                return (
                    f"'{tool_call.target}' isn't a provider I recognize (known: "
                    f"{', '.join(sorted(_KNOWN_ALIASES))}) - use one of those, or just "
                    f"say 'manual' without a name to lock the current default."
                )
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action

        if action == "set_auto":
            RUNTIME_MODE.set_auto()
            return ToolResult.ok(
                self.name, action,
                "LLM routing is back to automatic - I'll pick the best model for each request.",
            )

        if action == "set_manual":
            alias = (tool_call.target or "").strip().lower() or None
            RUNTIME_MODE.set_manual(alias)
            if alias:
                return ToolResult.ok(
                    self.name, action, f"LLM routing is locked to '{alias}' until you change it.",
                    data={"provider_alias": alias},
                )
            return ToolResult.ok(
                self.name, action,
                "LLM routing is set to manual (no specific provider pinned) - using the configured default.",
            )

        # Unreachable given validate() already restricts action to
        # _SUPPORTED_ACTIONS, kept as a defensive fallback.
        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
