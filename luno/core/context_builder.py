"""
context_builder.py
===================

`ContextBuilder` - assembles the ONE structured object the rest of the
pipeline (OpenRouter, in real wiring) needs: Conversation Memory, Vision
Memory, Behavior Tree State, Planner State, Current Tool Results, Home
Assistant Snapshot, Long-Term Memory, Current Emotion, Current Activity,
Current Time.

Explicitly out of scope, per the spec ("No LLM calls. No formatting for
personalities."):
  - never calls OpenRouter or any other LLM
  - never renders a prompt string or applies any persona/personality
    formatting - that's `main.py`/`persona.py`'s job, using this
    object's data as raw material

Gathers everything through injected zero-argument provider callables -
the exact same "hand in a snapshot, not a live dependency" shape as
`planner/context.py`'s `PlannerContext` and `behavior_tree/actions.py`'s
`Handlers` - so this file never imports `luno.vision_memory`,
`luno.behavior_tree`, `luno.planner`, or `luno.tool_manager` directly,
and is fully testable with plain functions/lambdas standing in for real
providers. A provider that raises or is simply absent yields that
field's documented default rather than failing the whole build - one
broken/slow provider should never block the entire context from being
assembled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .utils import log, utcnow

#: keys this builder knows how to ask for - passed a zero-arg callable
#: returning that field's value.
PROVIDER_KEYS = (
    "conversation_memory", "vision_memory", "behavior_tree_state", "planner_state",
    "tool_results", "ha_snapshot", "long_term_memory", "current_emotion", "current_activity",
)

_DEFAULTS: Dict[str, Any] = {
    "conversation_memory": list,
    "vision_memory": dict,
    "behavior_tree_state": dict,
    "planner_state": dict,
    "tool_results": list,
    "ha_snapshot": dict,
    "long_term_memory": list,
    "current_emotion": lambda: "neutral",
    "current_activity": lambda: "unknown",
}


@dataclass
class LLMContext:
    conversation_memory: List[Dict[str, Any]] = field(default_factory=list)
    vision_memory: Dict[str, Any] = field(default_factory=dict)
    behavior_tree_state: Dict[str, Any] = field(default_factory=dict)
    planner_state: Dict[str, Any] = field(default_factory=dict)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    ha_snapshot: Dict[str, Any] = field(default_factory=dict)
    long_term_memory: List[str] = field(default_factory=list)
    current_emotion: str = "neutral"
    current_activity: str = "unknown"
    current_time: datetime = field(default_factory=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_memory": self.conversation_memory,
            "vision_memory": self.vision_memory,
            "behavior_tree_state": self.behavior_tree_state,
            "planner_state": self.planner_state,
            "tool_results": self.tool_results,
            "ha_snapshot": self.ha_snapshot,
            "long_term_memory": self.long_term_memory,
            "current_emotion": self.current_emotion,
            "current_activity": self.current_activity,
            "current_time": self.current_time.isoformat(),
        }


class ContextBuilder:
    def __init__(self, providers: Optional[Dict[str, Callable[[], Any]]] = None) -> None:
        self.providers: Dict[str, Callable[[], Any]] = dict(providers) if providers else {}

    def register_provider(self, key: str, fn: Callable[[], Any]) -> None:
        if key not in PROVIDER_KEYS:
            log(f"registering provider for unrecognized key '{key}' (allowed anyway)", "context_builder")
        self.providers[key] = fn

    def build(self) -> LLMContext:
        values = {key: self._get(key) for key in PROVIDER_KEYS}
        return LLMContext(current_time=utcnow(), **values)

    def _get(self, key: str) -> Any:
        provider = self.providers.get(key)
        default_factory = _DEFAULTS[key]
        if provider is None:
            return default_factory()
        try:
            value = provider()
            return value if value is not None else default_factory()
        except Exception as ex:
            log(f"context provider '{key}' raised (using default): {ex}", "context_builder")
            return default_factory()
