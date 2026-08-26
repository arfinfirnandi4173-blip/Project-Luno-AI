"""
context.py
==========

The spec's "Context" section: what a handler may optionally be handed
alongside the `ToolCall` itself - conversation/Planner context, Behavior
Tree state, Vision Memory state, and a Home Assistant state snapshot.
Every field is a plain dict (no imports from `luno.planner`, `luno.
behavior_tree`, `luno.vision_memory`, or `luno.ha_client` - see this
package's independence rule) - building a real `ExecutionContext` from
those live systems is the caller's job, same "gather it outside, hand in
a plain snapshot" shape `luno/behavior_tree/actions.py`'s `Handlers` and
`luno/planner/context.py`'s `PlannerContext` already use.

Entirely optional - `ToolManager.execute()`/`execute_async()` default
`context` to `None`, and every builtin handler in `builtin/` works fine
without one (mocks don't need real state to fake a response).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ExecutionContext:
    planner_context: Dict[str, Any] = field(default_factory=dict)
    behavior_tree_state: Dict[str, Any] = field(default_factory=dict)
    vision_memory_state: Dict[str, Any] = field(default_factory=dict)
    ha_snapshot: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def empty() -> "ExecutionContext":
        return ExecutionContext()
