"""
scheduler.py
============

`PlanRunner` drives ONE `Plan` to completion on its own background
thread - decides WHICH tasks are ready (via `dependency.DependencyGraph`)
and WHEN to dispatch them (respecting pause/cancel), and applies the
plan-level failure policy (continue-on-failure, rollback-on-failure).
Actually running a single task is `executor.TaskExecutor`'s job; this
module is the loop that keeps calling it.

Never blocks: `Planner.execute()` (see planner.py) constructs a
`PlanRunner` and calls `start()`, which spawns this loop on a daemon
thread and returns immediately - satisfies the spec's "Execution should
never block the Behavior Tree" requirement the same way `behavior_tree/
scheduler.py`'s `Scheduler` does for tree ticks.

Polling, not pure event-driven: the loop wakes up every `poll_interval_s`
(default 50ms) to re-check what's ready, rather than reacting instantly
to each task's completion callback. This is a deliberate simplicity
choice - completion callbacks (`executor.py`'s `on_terminal`) DO fire
immediately and update the queue right away, but the loop itself
re-evaluates on a short fixed cadence, same style as `behavior_tree/
scheduler.py`'s 100-200ms tick. 50ms is negligible next to real tool
execution latency (HA calls, browser automation, etc. are all
much slower than that) and keeps this file simple to reason about and
test deterministically.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Set

from .dependency import DependencyGraph
from .executor import TaskExecutor
from .models import PlanStatus, TaskStatus
from .queue import ExecutionQueue
from .task import Plan, Task
from .utils import utcnow

DEFAULT_POLL_INTERVAL_S = 0.05


def _log(message: str) -> None:
    print(f"[Planner] {message}")


class PlanRunner:
    def __init__(self, plan: Plan, queue: ExecutionQueue, graph: DependencyGraph,
                 executor: TaskExecutor, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S) -> None:
        self.plan = plan
        self.queue = queue
        self.graph = graph
        self.executor = executor
        self.poll_interval_s = poll_interval_s

        self._lock = threading.RLock()
        self._paused = threading.Event()
        self._paused.set()  # not paused by default
        self._cancelled = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._completed_order: List[str] = []

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            # Don't clobber a PAUSED status if `pause()` was already called
            # before `start()` (a legitimate sequence - e.g. "queue this
            # plan up but hold off running it yet") - the `_paused` Event
            # is already cleared in that case, so the loop below still
            # correctly blocks before dispatching anything either way.
            if self.plan.status != PlanStatus.PAUSED:
                self.plan.status = PlanStatus.RUNNING
            self.plan.started_at = utcnow()
        _log(f"Plan {self.plan.id} started ({len(self.queue.all_tasks())} task(s))")
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"luno-plan-{self.plan.id}")
        self._thread.start()

    def pause(self) -> None:
        """Per spec: "Current task may finish before pausing if
        interruption is unsafe" - RUNNING tasks are left alone (their
        thread-pool futures keep going and still call back into the
        queue normally), only the dispatch of NEW ready tasks halts."""
        with self._lock:
            # Allowed from PENDING too (pause a plan that hasn't started
            # running yet, so it comes up already paused - see start()'s
            # matching guard) as well as from RUNNING.
            if self.plan.status in (PlanStatus.RUNNING, PlanStatus.PENDING):
                self.plan.status = PlanStatus.PAUSED
        self._paused.clear()
        _log(f"Plan {self.plan.id} paused")

    def resume(self) -> None:
        with self._lock:
            if self.plan.status == PlanStatus.PAUSED:
                self.plan.status = PlanStatus.RUNNING
        self._paused.set()
        _log(f"Plan {self.plan.id} resumed")

    def cancel(self) -> None:
        """Per spec: cancels the remaining queue immediately (every
        WAITING/RETRYING task becomes CANCELLED right away) and any
        pending retry timers. A task already RUNNING can't be safely
        force-killed (same "cooperative, not preemptive" honesty as
        `behavior_tree/actions.py`) - it's left to finish, but its
        completion callback checks `_cancelled` and no longer applies
        failure/rollback policy once cancellation has started."""
        self._cancelled.set()
        self._paused.set()  # unblock a paused wait so the loop can observe cancellation and exit
        self.executor.cancel_pending_retries()
        for task in self.queue.all_tasks():
            if task.status in (TaskStatus.WAITING, TaskStatus.RETRYING):
                self.queue.update_status(task.id, TaskStatus.CANCELLED)
        with self._lock:
            self.plan.status = PlanStatus.CANCELLED
        _log(f"Plan {self.plan.id} cancelled")

    # -- task completion handling -----------------------------------------

    def _on_task_terminal(self, task: Task) -> None:
        if self._cancelled.is_set():
            return  # plan already cancelled - don't apply failure/rollback policy retroactively
        if task.status == TaskStatus.COMPLETED:
            with self._lock:
                self._completed_order.append(task.id)
        elif task.status == TaskStatus.FAILED:
            self._apply_failure_policy(task)

    def _apply_failure_policy(self, task: Task) -> None:
        _log(f"Plan {self.plan.id}: task {task.id} failed permanently ({task.error})")
        if self.plan.rollback_on_failure:
            self._run_rollback()
        if not self.plan.continue_on_failure:
            for other in self.queue.all_tasks():
                if other.status in (TaskStatus.WAITING, TaskStatus.RETRYING):
                    self.queue.update_status(other.id, TaskStatus.SKIPPED, error="plan halted after task failure")

    def _run_rollback(self) -> None:
        with self._lock:
            order = list(reversed(self._completed_order))
        if order:
            _log(f"Plan {self.plan.id}: rolling back {len(order)} completed task(s)")
        for task_id in order:
            task = self.queue.get(task_id)
            if task is not None and task.rollback_call is not None and not task.rolled_back:
                self.executor.run_rollback(task)

    def _cascade_skip_blocked(self) -> None:
        """A WAITING task whose dependency FAILED/CANCELLED/SKIPPED can
        never become ready - mark it SKIPPED too, so it doesn't sit in
        WAITING forever and so the skip propagates to ITS dependents on
        the next pass (looped to a fixpoint, since one skip can trigger
        another)."""
        blocking = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
        changed = True
        while changed:
            changed = False
            for task in self.queue.all_tasks():
                if task.status != TaskStatus.WAITING or not task.depends_on:
                    continue
                deps = [self.queue.get(d) for d in task.depends_on]
                if any(d is not None and d.status in blocking for d in deps):
                    self.queue.update_status(task.id, TaskStatus.SKIPPED, error="a dependency did not complete")
                    changed = True

    # -- main loop --------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._cancelled.is_set():
            self._paused.wait()
            if self._cancelled.is_set():
                break
            if self.queue.is_complete():
                break

            self._cascade_skip_blocked()

            completed_ids: Set[str] = {t.id for t in self.queue.by_status(TaskStatus.COMPLETED)}
            ready_ids = self.graph.ready_tasks(completed_ids)
            for task_id in ready_ids:
                task = self.queue.get(task_id)
                if task is not None and task.status == TaskStatus.WAITING:
                    self.executor.run_task(task, self.queue, self._on_task_terminal)

            time.sleep(self.poll_interval_s)

        self._finalize()

    def _finalize(self) -> None:
        with self._lock:
            if self._cancelled.is_set():
                self.plan.status = PlanStatus.CANCELLED
            elif self.queue.has_failures():
                self.plan.status = PlanStatus.FAILED
            else:
                self.plan.status = PlanStatus.COMPLETED
            self.plan.finished_at = utcnow()
        _log(f"Plan {self.plan.id} finished: {self.plan.status.value}")
