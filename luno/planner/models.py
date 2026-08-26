"""
models.py
=========

Pure, leaf-level data types shared across the package - zero dependencies
on any other `luno.planner` submodule (mirrors `vision_memory/models.py`'s
role). `Task` and `Plan` are NOT here, deliberately - they reference each
other and carry more behavior (state-transition helpers), so they live in
`task.py` to avoid a circular import between "the types" and "the things
built from the types". Everything in THIS file, `task.py` can safely
import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """The spec's exact task lifecycle states."""
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"
    SKIPPED = "skipped"


#: Statuses a task will never leave on its own - used throughout the
#: package (queue.py/dependency.py/scheduler.py) to detect "this task is
#: done, one way or another".
TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
)

#: Statuses that should block a dependent task from ever becoming ready -
#: see dependency.py / scheduler.py's cascade-skip logic. Deliberately
#: excludes COMPLETED (the only status that lets a dependent proceed).
BLOCKING_TASK_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED})


class PlanStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Tool call - the ONLY thing the Planner ever hands to the outside world
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A generic, tool-agnostic instruction - exactly the shape given in
    the spec's example:

        {"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"}

    The Planner constructs these and NEVER executes them directly (see
    the package docstring in `__init__.py`) - `executor.ToolRegistry` is
    where a real handler (the future Tool Manager) gets attached."""
    tool: str
    action: str
    target: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "action": self.action, "target": self.target, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        return cls(tool=data["tool"], action=data["action"], target=data.get("target"), params=dict(data.get("params") or {}))


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """`max_retries=0` (the default) means "try once, no retries" - a task
    fails permanently on its first failure unless a caller explicitly
    opts into retries. Backoff is exponential, capped at `max_backoff_s`."""
    max_retries: int = 0
    backoff_s: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_s: float = 30.0

    def delay_for_attempt(self, attempt_number: int) -> float:
        """`attempt_number` is 1 for the delay BEFORE the 2nd attempt (the
        1st retry), 2 for the delay before the 3rd attempt, etc."""
        delay = self.backoff_s * (self.backoff_multiplier ** max(0, attempt_number - 1))
        return min(delay, self.max_backoff_s)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

@dataclass
class ProgressReport:
    """What `Planner.get_status()` returns - the spec's "Progress
    Reporting" list, verbatim: current task, completed tasks, remaining
    tasks, estimated completion, errors."""
    plan_id: str
    plan_status: PlanStatus
    current_tasks: List[str]
    completed_tasks: List[str]
    remaining_tasks: List[str]
    failed_tasks: List[str]
    total_tasks: int
    estimated_completion: Optional[datetime]
    errors: List[str]

    @property
    def percent_complete(self) -> float:
        if self.total_tasks == 0:
            return 100.0
        done = len(self.completed_tasks) + len(self.failed_tasks)
        return round(100.0 * done / self.total_tasks, 1)
