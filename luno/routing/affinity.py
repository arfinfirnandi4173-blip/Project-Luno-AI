"""
affinity.py
============

`ConversationAffinityTracker` - spec's "Conversation Affinity": "Once a
conversation shifts into deep reasoning (GPT), the Decision Engine
should NOT switch providers mid-session unless the task changes.
General chat afterwards may return to DeepSeek." In-memory,
per-`conversation_id`, no persistence (matches every other per-
conversation tracker in this codebase, e.g. `LLMManagerAdapter._inflight`/
`PlannerBridgeModule._pending_turns` - conversations don't survive a
process restart anywhere in this project, and don't need to).

Thread-safe: multiple conversations can be mid-turn concurrently (each
`PlannerBridgeModule._handle_utterance()` call runs on its own thread -
see that method's own docstring), so this can't assume single-threaded
access.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .models import REASONING_INTENTS, ComplexityLevel, Intent, complexity_at_least

if TYPE_CHECKING:
    from .config import RoutingConfig


class ConversationAffinityTracker:
    def __init__(self, config: "RoutingConfig") -> None:
        self.config = config
        self._lock = threading.Lock()
        self._sticky: Dict[str, str] = {}  # conversation_id -> provider_alias

    def apply(
        self,
        conversation_id: Optional[str],
        computed_alias: str,
        intents: List[Intent],
        complexity: ComplexityLevel,
        reasoning: List[str],
    ) -> Tuple[str, bool]:
        """Returns `(alias_to_actually_use, affinity_applied)`.
        `affinity_applied=True` means the alias was overridden by
        stickiness (not what `provider_selector.py` would have picked
        for THIS turn in isolation)."""
        if not self.config.enable_provider_affinity or not conversation_id:
            return computed_alias, False

        still_reasoning = bool(set(intents) & REASONING_INTENTS) or complexity_at_least(complexity, ComplexityLevel.HIGH)

        with self._lock:
            if computed_alias == self.config.reasoning_provider_alias:
                # genuinely chose the reasoning provider this turn on its
                # own merits - mark the conversation sticky for future
                # turns, nothing to override right now.
                self._sticky[conversation_id] = computed_alias
                return computed_alias, False

            sticky = self._sticky.get(conversation_id)
            if sticky == self.config.reasoning_provider_alias:
                if still_reasoning:
                    reasoning.append(
                        f"conversation affinity: staying on '{sticky}' for conversation "
                        f"{conversation_id} (still mid-reasoning session)"
                    )
                    return sticky, True
                reasoning.append(
                    f"conversation affinity: releasing '{sticky}' for conversation "
                    f"{conversation_id} (topic shifted to routine chat)"
                )
                del self._sticky[conversation_id]

            return computed_alias, False

    def reset(self, conversation_id: Optional[str]) -> None:
        """Called on an explicit conversation boundary (e.g.
        `ConversationEnded`/`ConversationReset`) so a brand new
        conversation never inherits a previous one's sticky provider."""
        if not conversation_id:
            return
        with self._lock:
            self._sticky.pop(conversation_id, None)

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._sticky)
