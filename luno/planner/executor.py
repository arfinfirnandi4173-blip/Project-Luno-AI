"""
executor.py
===========

Knows how to run ONE task attempt - and, on failure, either schedule a
retry (with backoff) or give up - plus how to run a rollback call. It
does NOT decide WHICH tasks are ready or drive a whole plan to completion
- that's `scheduler.PlanRunner`, which calls into this module.

`ToolRegistry` is the seam the spec calls for in "Tool Abstraction" /
"Extensibility": the Planner package never imports Home Assistant,
Windows, Spotify, the browser, or Vision - it only knows the generic
`ToolCall` shape. A future Tool Manager (or, for now, `test_planner.py`)
attaches real behavior via `registry.register(name, handler)`; adding a
brand new tool is exactly that one call, nothing in this package changes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .exceptions import ToolNotRegisteredError
from .models import TaskStatus, ToolCall
from .queue import ExecutionQueue
from .task import Task
from .utils import utcnow

#: A handler receives the task's `ToolCall` and either returns a result
#: (any JSON-ish value is fine) or raises - a raised exception is what
#: `TaskExecutor` treats as "this attempt failed" and feeds into the
#: retry policy.
ToolHandler = Callable[[ToolCall], Any]


def _log(message: str) -> None:
    print(f"[Planner] {message}")


@dataclass
class ToolSpec:
    name: str
    required_params: List[str] = field(default_factory=list)


class ToolRegistry:
    """Thread-safe name -> handler map. `register()` may be called with
    `handler=None` to declare a tool's shape (for `validator.py`'s
    strict-mode checking) before a real handler exists yet - useful in
    tests that want validation to succeed while execution is still
    expected to raise `ToolNotRegisteredError`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: Dict[str, ToolHandler] = {}
        self._specs: Dict[str, ToolSpec] = {}

    def register(self, name: str, handler: Optional[ToolHandler] = None,
                 required_params: Optional[List[str]] = None) -> None:
        with self._lock:
            self._specs[name] = ToolSpec(name=name, required_params=required_params or [])
            if handler is not None:
                self._handlers[name] = handler

    def get_handler(self, name: str) -> Optional[ToolHandler]:
        with self._lock:
            return self._handlers.get(name)

    def has_tool(self, name: str) -> bool:
        with self._lock:
            return name in self._specs or name in self._handlers

    def specs(self) -> Dict[str, ToolSpec]:
        with self._lock:
            return dict(self._specs)


class TaskExecutor:
    """`thread_pool` is injected (real wiring/tests: a single
    `ThreadPoolExecutor` shared across the whole `Planner`, see
    `planner.py`) so nothing here spins up its own threads per task."""

    def __init__(self, registry: ToolRegistry, thread_pool) -> None:
        self.registry = registry
        self.thread_pool = thread_pool
        self._retry_lock = threading.Lock()
        self._retry_timers: Dict[str, threading.Timer] = {}

    def run_task(self, task: Task, queue: ExecutionQueue, on_terminal: Callable[[Task], None]) -> None:
        """Dispatch one attempt of `task` on the thread pool - returns
        immediately, never blocks the caller. `on_terminal` fires (from a
        background thread) once this task has reached a result the
        executor itself won't act on further: COMPLETED, or FAILED after
        retries are exhausted. It is NOT called when a retry gets
        scheduled - `PlanRunner` doesn't need to react to that, the retry
        re-enters the normal WAITING/ready-task flow on its own once the
        backoff timer fires."""
        queue.update_status(task.id, TaskStatus.RUNNING, started_at=utcnow())
        task.attempts += 1

        def _invoke() -> Any:
            handler = self.registry.get_handler(task.tool_call.tool)
            if handler is None:
                raise ToolNotRegisteredError(f"No handler registered for tool '{task.tool_call.tool}'")
            return handler(task.tool_call)

        self.thread_pool.submit(self._safe_run, task, queue, _invoke, on_terminal)

    def _safe_run(self, task: Task, queue: ExecutionQueue, invoke: Callable[[], Any],
                  on_terminal: Callable[[Task], None]) -> None:
        try:
            result = invoke()
        except Exception as ex:
            self._handle_failure(task, queue, ex, on_terminal)
            return
        queue.update_status(task.id, TaskStatus.COMPLETED, result=result, error=None, finished_at=utcnow())
        on_terminal(task)

    def _handle_failure(self, task: Task, queue: ExecutionQueue, ex: Exception,
                         on_terminal: Callable[[Task], None]) -> None:
        if task.can_retry():
            queue.update_status(task.id, TaskStatus.RETRYING, error=str(ex))
            delay = task.retry_policy.delay_for_attempt(task.attempts)
            timer = threading.Timer(delay, self._requeue_after_backoff, args=(task, queue))
            timer.daemon = True
            with self._retry_lock:
                self._retry_timers[task.id] = timer
            timer.start()
            _log(f"Task {task.id} failed (attempt {task.attempts}), retrying in {delay:.1f}s: {ex}")
            return
        # Reliability Sprint - "ToolResult diteruskan utuh": a handler
        # (e.g. `PlannerBridgeModule._tool_bridge_handler`) MAY attach the
        # full failed result payload it received (message/data/error_type/
        # ...) as `ex.tool_result` before raising - preserved here as
        # `task.result` even though the task ultimately FAILED, so a
        # caller reading `task.result` doesn't lose `data` (e.g.
        # `verification_attempts`/`actual_state`) just because the outcome
        # was a failure. Purely additive: `task.result` already exists and
        # already defaults to `None`; a handler that never sets
        # `.tool_result` (i.e. every handler predating this) leaves this
        # at `None`, exactly as before.
        final_fields: Dict[str, Any] = {"error": str(ex), "finished_at": utcnow()}
        tool_result = getattr(ex, "tool_result", None)
        if tool_result is not None:
            final_fields["result"] = tool_result
        queue.update_status(task.id, TaskStatus.FAILED, **final_fields)
        on_terminal(task)

    def _requeue_after_backoff(self, task: Task, queue: ExecutionQueue) -> None:
        with self._retry_lock:
            self._retry_timers.pop(task.id, None)
        if task.status != TaskStatus.RETRYING:
            return  # cancelled/skipped while the backoff timer was pending
        queue.update_status(task.id, TaskStatus.WAITING)

    def cancel_pending_retries(self) -> None:
        """Cancels every scheduled retry timer immediately - called by
        `PlanRunner.cancel()` so a cancelled plan doesn't quietly resurrect
        a task seconds later via a backoff timer that was already in
        flight."""
        with self._retry_lock:
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        for timer in timers:
            timer.cancel()

    def run_rollback(self, task: Task) -> bool:
        """Runs `task.rollback_call` SYNCHRONOUSLY on the calling thread.
        Safe to call synchronously because rollback only ever happens from
        `PlanRunner`'s own background failure-handling path (never from
        the poll loop itself) - see `scheduler.py`. Never raises: a
        rollback failing shouldn't abort rolling back the REST of the
        completed tasks, so the error is recorded on the task and `False`
        is returned instead."""
        if task.rollback_call is None:
            return False
        handler = self.registry.get_handler(task.rollback_call.tool)
        if handler is None:
            task.error = f"{task.error or ''} | rollback skipped: no handler for '{task.rollback_call.tool}'".strip(" |")
            return False
        try:
            handler(task.rollback_call)
            task.rolled_back = True
            _log(f"Task {task.id} rolled back ({task.rollback_call.tool}.{task.rollback_call.action})")
            return True
        except Exception as ex:
            task.error = f"{task.error or ''} | rollback failed: {ex}".strip(" |")
            _log(f"Task {task.id} rollback FAILED: {ex}")
            return False
