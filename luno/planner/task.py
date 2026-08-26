"""
task.py
=======

`Task` (one executable unit) and `Plan` (an ordered/dependency-linked
collection of Tasks produced by one `Planner.create_plan()` call). Both
live here rather than `models.py` specifically to avoid a circular
import - see `models.py`'s module docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import PlanStatus, RetryPolicy, TaskStatus, ToolCall
from .utils import utcnow


@dataclass
class Task:
    """One executable unit - wraps a `ToolCall` with everything needed to
    schedule, retry, and (optionally) roll it back. Mutated in place by
    `queue.ExecutionQueue` (the only thing that should ever change
    `.status` - see that module's concurrency note) as it moves through
    its lifecycle."""

    id: str
    tool_call: ToolCall
    label: str = ""
    depends_on: List[str] = field(default_factory=list)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    rollback_call: Optional[ToolCall] = None

    status: TaskStatus = TaskStatus.WAITING
    attempts: int = 0
    error: Optional[str] = None
    result: Any = None
    rolled_back: bool = False

    created_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def can_retry(self) -> bool:
        """True if another attempt is still allowed AFTER the current
        (already-counted) attempt. `attempts` is incremented by
        `executor.TaskExecutor` right before each dispatch, so after the
        1st attempt `attempts == 1`; with `max_retries=2` that allows
        attempts 2 and 3 (2 retries) before giving up."""
        return self.attempts <= self.retry_policy.max_retries

    def duration_s(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tool_call": self.tool_call.to_dict(),
            "label": self.label,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "attempts": self.attempts,
            "error": self.error,
            "rolled_back": self.rolled_back,
        }


@dataclass
class Plan:
    """A full execution plan: an ordered task list plus the failure-
    handling policy chosen when it was created (`continue_on_failure`,
    `rollback_on_failure` - see the spec's Retry Policy/Rollback sections).
    `tasks` is the SINGLE source of truth for task state - `queue.
    ExecutionQueue` wraps this exact list (no copies), so mutations made
    through the queue are visible on `plan.tasks` immediately."""

    id: str
    source_request: str
    tasks: List[Task] = field(default_factory=list)
    status: PlanStatus = PlanStatus.PENDING
    continue_on_failure: bool = False
    rollback_on_failure: bool = False
    validation_errors: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def get_task(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def is_valid(self) -> bool:
        return not self.validation_errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_request": self.source_request,
            "status": self.status.value,
            "continue_on_failure": self.continue_on_failure,
            "rollback_on_failure": self.rollback_on_failure,
            "validation_errors": list(self.validation_errors),
            "tasks": [t.to_dict() for t in self.tasks],
        }
