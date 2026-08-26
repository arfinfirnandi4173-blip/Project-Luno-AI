"""
models.py
=========

Leaf-level data types: `ToolCall` (the input) and `RetryPolicy` /
`ExecutionStatus` (execution bookkeeping). `ToolResult` (the output) gets
its own file - see `result.py`, per the spec's suggested structure.

INDEPENDENCE FROM THE PLANNER: `luno.planner` already defines its own
`ToolCall` dataclass, and the two are STRUCTURALLY almost identical - by
design, since both follow the same spec example:

    {"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"}

This package does NOT import `luno.planner.ToolCall` (that would violate
"The Tool Manager must remain completely independent from the Planner").
Instead, `ToolCall.from_any()` below accepts a dict, a `ToolCall` already,
or ANY duck-typed object exposing `.tool`/`.action`/`.target` and either
`.parameters` or `.params` - which is enough to accept a `luno.planner.
ToolCall` instance directly, sight unseen, with zero coupling in either
direction. If the Planner's shape ever changes, only `from_any()` here
might need a tweak - nothing structural about this package does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


@dataclass
class ToolCall:
    """Note the field name `parameters` (matching THIS spec's example
    exactly) - `luno.planner.ToolCall` calls the equivalent field `params`.
    `from_any()` bridges that naming difference so it never becomes a
    real integration problem."""
    tool: str
    action: str
    target: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "action": self.action, "target": self.target, "parameters": dict(self.parameters)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        params = data.get("parameters")
        if params is None:
            params = data.get("params") or {}
        return cls(tool=data["tool"], action=data["action"], target=data.get("target"), parameters=dict(params))

    @classmethod
    def from_any(cls, obj: Any) -> "ToolCall":
        """Accepts: a `ToolCall` (returned as-is), a plain dict (via
        `from_dict`), or any object with `.tool`/`.action` attributes
        (e.g. `luno.planner.ToolCall`) - `.target` and `.parameters`/
        `.params` are read defensively since not every caller will have
        them."""
        if isinstance(obj, ToolCall):
            return obj
        if isinstance(obj, dict):
            return cls.from_dict(obj)
        tool = getattr(obj, "tool")
        action = getattr(obj, "action")
        target = getattr(obj, "target", None)
        parameters = getattr(obj, "parameters", None)
        if parameters is None:
            parameters = getattr(obj, "params", None) or {}
        return cls(tool=tool, action=action, target=target, parameters=dict(parameters))


@dataclass
class RetryPolicy:
    """Deliberately a SEPARATE class from `luno.planner.models.RetryPolicy`
    (same independence rationale as `ToolCall` above), and OFF by default
    (`max_retries=0`). The Planner already retries whole TASKS at its own
    layer (see `luno/planner/executor.py`) - this is a lower-level,
    OPTIONAL retry of a single tool INVOCATION (e.g. a flaky call worth
    retrying without re-running an entire task), left disabled by default
    so composing the two doesn't produce surprising double-retries unless
    a caller explicitly opts in here too."""
    max_retries: int = 0
    backoff_s: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_s: float = 10.0

    def delay_for_attempt(self, attempt_number: int) -> float:
        delay = self.backoff_s * (self.backoff_multiplier ** max(0, attempt_number - 1))
        return min(delay, self.max_backoff_s)


class ExecutionStatus(str, Enum):
    """Phases reported to `on_progress` callbacks (see `manager.py`) -
    NOT the same enum as `ResultStatus` in `result.py`, which describes
    the FINAL outcome only."""
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
