"""
queue.py
========

The spec's "Queue" section: a thread-safe container tracking every
task's status for one Plan. This is the ONLY place task state should be
mutated - `scheduler.PlanRunner` and `executor.TaskExecutor` (including
from background threads) always go through `update_status()`, never set
`task.status` directly, so every transition is lock-guarded and logged in
one place.

Concurrency contract: same discipline as `behavior_tree/blackboard.py`'s
`Blackboard.lock` - one `threading.RLock` guards every mutation. Reads
that need a CONSISTENT view across multiple tasks (e.g. "are all tasks
terminal?") take the lock for the whole scan; a single-field read of an
already-fetched `Task` object is fine without it (Python attribute reads
are atomic under the GIL), which is why `Task` itself carries no lock.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .models import TERMINAL_TASK_STATUSES, TaskStatus
from .task import Task


def _log(message: str) -> None:
    print(f"[Planner] {message}")


class ExecutionQueue:
    """Wraps a Plan's task list - does NOT copy it, so mutations here are
    immediately visible on `Plan.tasks` (see `task.Plan`'s docstring)."""

    def __init__(self, tasks: List[Task]) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Task] = {t.id: t for t in tasks}
        self._order: List[str] = [t.id for t in tasks]

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def all_tasks(self) -> List[Task]:
        with self._lock:
            return [self._tasks[tid] for tid in self._order]

    def by_status(self, status: TaskStatus) -> List[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == status]

    def update_status(self, task_id: str, status: TaskStatus, **fields: Any) -> None:
        """Sets `.status` plus any extra Task fields given as keyword
        args (e.g. `error=...`, `result=...`, `finished_at=...`) in one
        locked operation, and logs the transition - covers the spec's
        Logging requirement for "Task transitions" at the single choke
        point every status change already has to pass through."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            old_status = task.status
            task.status = status
            for key, value in fields.items():
                setattr(task, key, value)
        suffix = f" ({fields['error']})" if fields.get("error") else ""
        _log(f"Task {task_id} [{task.label or task.tool_call.tool + '.' + task.tool_call.action}] "
             f"{old_status.value} -> {status.value}{suffix}")

    def is_complete(self) -> bool:
        with self._lock:
            return all(t.status in TERMINAL_TASK_STATUSES for t in self._tasks.values())

    def has_failures(self) -> bool:
        with self._lock:
            return any(t.status == TaskStatus.FAILED for t in self._tasks.values())

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return {tid: self._tasks[tid].status.value for tid in self._order}
