"""
mode_state.py
==============

`LLMModeState` - a small, thread-safe, IN-PROCESS runtime override for
the "auto vs manual" LLM routing decision, sitting ALONGSIDE
`RoutingConfig.enable_auto_routing` (the .env-only, restart-required
version of the same switch - see that module's own docstring). This one
is meant to be flipped LIVE, by a voice/text command ("pakai llm
manual" / "pakai llm otomatis" / "pakai llm openai" - see
`luno/tool_manager/builtin/llm_mode.py` and `luno/planner/parser.py`'s
`_classify_llm_mode()`), with no .env edit and no restart.

Precedence (see `DecisionEngine.decide()`):
    1. This runtime state, once it's ever been moved away from "auto" -
       always wins, every turn, until moved back.
    2. Otherwise, `RoutingConfig.enable_auto_routing` (the .env-only
       startup default) - unchanged, exactly as it worked before this
       module existed.

A single process-wide instance (`RUNTIME_MODE` below) is shared by every
`DecisionEngine` and every `LLMModeHandler` in the real running app -
there is only ever one real LLM routing decision being made per
process, so one shared state is the correct model. Tests that need
isolation construct their own `LLMModeState()` and pass it to
`DecisionEngine(..., mode_state=...)` explicitly rather than touching
this singleton, so they can run in any order without leaking into each
other or into the real runtime's own state.
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

MODE_AUTO = "auto"
MODE_MANUAL = "manual"


class LLMModeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = MODE_AUTO
        self._manual_alias: Optional[str] = None

    def set_auto(self) -> None:
        with self._lock:
            self._mode = MODE_AUTO
            self._manual_alias = None

    def set_manual(self, alias: Optional[str]) -> None:
        """`alias` - a provider alias/name (e.g. "openai"/"deepseek"/
        "gpt"/"anthropic"/"gemini"/"local"/"openrouter"), lowercased and
        trimmed here so callers don't each have to. `None` (or an
        empty/whitespace string) pins to "manual" WITHOUT a specific
        provider - mirrors the pre-existing `ENABLE_AUTO_ROUTING=false`
        .env behavior: no override at all, `LLMManagerAdapter`'s own
        configured default/fallback order decides."""
        with self._lock:
            self._mode = MODE_MANUAL
            self._manual_alias = (alias or "").strip().lower() or None

    def snapshot(self) -> Tuple[str, Optional[str]]:
        """Returns `(mode, manual_alias)` - `mode` is always
        `MODE_AUTO`/`MODE_MANUAL`; `manual_alias` is only ever non-None
        when `mode == MODE_MANUAL` and a specific provider was pinned."""
        with self._lock:
            return self._mode, self._manual_alias

    def reset(self) -> None:
        """Test/debug helper - back to the process-default auto state."""
        self.set_auto()


#: Process-wide singleton - see module docstring. The real runtime
#: (`main_runtime_demo.py`'s `DecisionEngine`) and the real
#: `LLMModeHandler` tool both read/write this exact object by default.
RUNTIME_MODE = LLMModeState()
