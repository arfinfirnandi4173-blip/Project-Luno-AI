"""
context.py
==========

The spec's "Context Awareness" section: everything the Planner is handed
about the current situation before it plans, so it can make better
decisions than pure text-in/tasks-out. This package never gathers this
data itself (it doesn't talk to Vision Memory, Home Assistant, or the
Behavior Tree directly - see the package docstring in `__init__.py`) -
building a `PlannerContext` is the caller's job (real wiring: something in
main.py assembling it from `vision.build_vision_context()`,
`ha_client.get_states()`, the Behavior Tree's `Blackboard`, etc.).

`Planner.create_plan()` currently uses `ha_state` for one concrete,
testable optimization (skipping a `turn_on`/`turn_off` step that's
already in the desired state - see `planner.py`'s `_steps_to_tasks()`).
The other fields are captured here because the spec asks for them, and
because a smarter `parse_fn` (see `parser.py`'s docstring) will want them
even though the default heuristic parser does not read them yet - keeping
them on `PlannerContext` now means nothing about the public API needs to
change when that happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PlannerContext:
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    vision_summary: str = ""
    ha_state: Dict[str, bool] = field(default_factory=dict)  # entity/target -> is_on
    behavior_tree_state: Dict[str, Any] = field(default_factory=dict)
    running_tasks: List[str] = field(default_factory=list)
    current_goals: List[str] = field(default_factory=list)
    recent_events: List[str] = field(default_factory=list)

    @staticmethod
    def empty() -> "PlannerContext":
        return PlannerContext()
