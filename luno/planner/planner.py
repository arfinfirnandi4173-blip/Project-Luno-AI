"""
planner.py
==========

The public facade - the ONLY module callers outside this package should
import from (re-exported by `__init__.py`, same pattern as
`vision_memory/api.py` and `behavior_tree/behavior_tree.py`):

    from luno.planner import Planner, PlannerContext, PlanOptions

    planner = Planner()
    plan = planner.create_plan(user_request, context)
    planner.execute(plan)
    planner.cancel(plan.id)
    planner.pause(plan.id)
    planner.resume(plan.id)
    report = planner.get_status(plan.id)
    tasks = planner.get_queue(plan.id)
    planner.clear()

Owns exactly one shared `ThreadPoolExecutor` (all plans' tasks run on it -
see `executor.TaskExecutor`) and a `ToolRegistry` tools get registered
against (real wiring: the future Tool Manager calls `planner.registry.
register(...)` once per tool - see the package docstring in `__init__.py`
for why the Planner itself never imports Home Assistant/Windows/Spotify/
etc.).

`create_plan()` never raises for a BAD request - it always returns a
`Plan`, with `Plan.validation_errors` populated if something's wrong
(unknown tool, dependency cycle, ...). This is deliberate: turning "the
user asked for something we can't safely plan" into an exception would
make the common case (inspect the plan, maybe show the user what's
wrong) awkward. `execute()` DOES raise `ValidationError` if asked to run
a plan that has validation errors, since running an invalid plan is
always a programming mistake, never an expected outcome.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

from .context import PlannerContext
from .dependency import DependencyGraph
from .exceptions import PlanNotFoundError, ValidationError
from .executor import TaskExecutor, ToolRegistry
from .models import PlanStatus, ProgressReport, TaskStatus, ToolCall
from .parser import IntentParser, ParsedStep
from .queue import ExecutionQueue
from .scheduler import PlanRunner
from .task import Plan, Task
from .utils import generate_id, utcnow
from .validator import PlanValidator

DEFAULT_MAX_WORKERS = 8


def _log(message: str) -> None:
    print(f"[Planner] {message}")


@dataclass
class PlanOptions:
    """Per-plan failure-handling policy - see the spec's "Retry Policy"
    and "Rollback" sections. Both default to the SAFER choice: stop on
    failure, don't attempt any rollback unless explicitly asked."""
    continue_on_failure: bool = False
    rollback_on_failure: bool = False


class Planner:
    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        validator: Optional[PlanValidator] = None,
        parse_fn: Optional[Callable[[str], List[ParsedStep]]] = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        self.registry = tool_registry if tool_registry is not None else ToolRegistry()
        self.validator = validator if validator is not None else PlanValidator()
        self._parse_fn = parse_fn if parse_fn is not None else IntentParser.parse
        self._thread_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="luno-planner")

        self._lock = threading.RLock()
        self._plans: Dict[str, Plan] = {}
        self._runners: Dict[str, PlanRunner] = {}

    # -- planning -----------------------------------------------------------

    def create_plan(self, user_request: str, context: Optional[PlannerContext] = None,
                     options: Optional[PlanOptions] = None) -> Plan:
        """Spec performance target: under 100ms for a normal request -
        the heuristic parser + validation this does are all pure
        in-process string/graph work, no I/O, so that target is easily
        met (see `test_planner.py`'s performance assertion)."""
        start = time.time()
        context = context if context is not None else PlannerContext.empty()
        options = options if options is not None else PlanOptions()

        steps = self._parse_fn(user_request)
        tasks = self._steps_to_tasks(steps, context)
        errors = self.validator.validate_tasks(tasks)

        plan = Plan(
            id=generate_id("plan"),
            source_request=user_request,
            tasks=tasks,
            status=PlanStatus.FAILED if errors else PlanStatus.PENDING,
            continue_on_failure=options.continue_on_failure,
            rollback_on_failure=options.rollback_on_failure,
            validation_errors=errors,
        )
        with self._lock:
            self._plans[plan.id] = plan

        elapsed_ms = (time.time() - start) * 1000
        if errors:
            _log(f"Plan {plan.id} FAILED validation in {elapsed_ms:.1f}ms: {errors}")
        else:
            _log(f"Plan {plan.id} created with {len(tasks)} task(s) in {elapsed_ms:.1f}ms: \"{user_request}\"")
        return plan

    def _steps_to_tasks(self, steps: List[ParsedStep], context: PlannerContext) -> List[Task]:
        tasks: List[Task] = []
        previous_id: Optional[str] = None
        for step in steps:
            task_id = generate_id("task")
            depends_on = [previous_id] if (previous_id and step.depends_on_previous) else []
            task = Task(
                id=task_id,
                tool_call=ToolCall(tool=step.tool, action=step.action, target=step.target, params=step.params),
                depends_on=depends_on,
                label=step.label or f"{step.tool}.{step.action}",
            )
            self._apply_context_shortcuts(task, step, context)
            tasks.append(task)
            previous_id = task_id
        return tasks

    @staticmethod
    def _apply_context_shortcuts(task: Task, step: ParsedStep, context: PlannerContext) -> None:
        """Context Awareness in action: if `context.ha_state` already
        shows a `turn_on`/`turn_off` target in the desired state, mark
        the task COMPLETED as a no-op right away instead of dispatching a
        redundant Home Assistant call. Deliberately COMPLETED, not
        SKIPPED - a dependent task must still be able to proceed
        normally, and only COMPLETED satisfies a dependency (see
        `dependency.DependencyGraph.ready_tasks`); SKIPPED is reserved for
        "this never got a chance to run because something upstream
        failed", a different situation that SHOULD keep blocking
        dependents (see `scheduler.PlanRunner._cascade_skip_blocked`)."""
        if step.tool != "home_assistant" or step.action not in ("turn_on", "turn_off") or not step.target:
            return
        known_is_on = context.ha_state.get(step.target)
        if known_is_on is None:
            return
        desired_on = step.action == "turn_on"
        if known_is_on == desired_on:
            task.status = TaskStatus.COMPLETED
            task.result = "already in desired state (context) - no-op"
            task.finished_at = utcnow()

    # -- execution -----------------------------------------------------------

    def execute(self, plan: Union[Plan, str]) -> None:
        """Non-blocking - starts a `PlanRunner` on its own background
        thread and returns immediately (spec: "Execution should never
        block the Behavior Tree")."""
        plan = self._resolve_plan(plan)
        if plan.validation_errors:
            raise ValidationError(f"Cannot execute plan {plan.id}, it failed validation: {plan.validation_errors}")
        if plan.status != PlanStatus.PENDING:
            raise ValidationError(f"Plan {plan.id} is not in PENDING state (currently {plan.status.value})")

        queue = ExecutionQueue(plan.tasks)
        graph = DependencyGraph(plan.tasks)
        task_executor = TaskExecutor(self.registry, self._thread_pool)
        runner = PlanRunner(plan, queue, graph, task_executor)

        with self._lock:
            self._runners[plan.id] = runner
        runner.start()

    def cancel(self, plan_id: str) -> None:
        self._get_runner(plan_id).cancel()

    def pause(self, plan_id: str) -> None:
        self._get_runner(plan_id).pause()

    def resume(self, plan_id: str) -> None:
        self._get_runner(plan_id).resume()

    # -- inspection -----------------------------------------------------------

    def get_status(self, plan_id: str) -> ProgressReport:
        plan = self._get_plan(plan_id)
        tasks = plan.tasks
        completed = [t.id for t in tasks if t.status == TaskStatus.COMPLETED]
        failed = [t.id for t in tasks if t.status == TaskStatus.FAILED]
        running = [t.id for t in tasks if t.status == TaskStatus.RUNNING]
        remaining = [t.id for t in tasks if t.status in (TaskStatus.WAITING, TaskStatus.RETRYING)]
        errors = [f"{t.id}: {t.error}" for t in tasks if t.error]

        estimated_completion = None
        durations = [t.duration_s() for t in tasks if t.duration_s() is not None]
        if durations and remaining:
            avg = sum(durations) / len(durations)
            estimated_completion = utcnow().timestamp() + avg * len(remaining)
            from datetime import datetime, timezone
            estimated_completion = datetime.fromtimestamp(estimated_completion, tz=timezone.utc)

        return ProgressReport(
            plan_id=plan.id,
            plan_status=plan.status,
            current_tasks=running,
            completed_tasks=completed,
            remaining_tasks=remaining,
            failed_tasks=failed,
            total_tasks=len(tasks),
            estimated_completion=estimated_completion,
            errors=errors,
        )

    def get_queue(self, plan_id: Optional[str] = None) -> Union[List[Task], Dict[str, List[Task]]]:
        if plan_id is not None:
            return list(self._get_plan(plan_id).tasks)
        with self._lock:
            return {pid: list(p.tasks) for pid, p in self._plans.items()}

    def get_plan(self, plan_id: str) -> Plan:
        return self._get_plan(plan_id)

    def clear(self) -> int:
        """Drops every plan that's reached a TERMINAL status (COMPLETED/
        FAILED/CANCELLED) from tracking. Running/paused/pending plans are
        left alone. Returns how many were removed."""
        terminal = {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}
        with self._lock:
            to_remove = [pid for pid, p in self._plans.items() if p.status in terminal]
            for pid in to_remove:
                self._plans.pop(pid, None)
                self._runners.pop(pid, None)
        if to_remove:
            _log(f"Cleared {len(to_remove)} finished plan(s)")
        return len(to_remove)

    def shutdown(self, wait: bool = False) -> None:
        """Not part of the spec's public API list, but needed for clean
        process/test teardown - stops accepting new work on the shared
        thread pool."""
        self._thread_pool.shutdown(wait=wait)

    # -- internals -----------------------------------------------------------

    def _resolve_plan(self, plan: Union[Plan, str]) -> Plan:
        if isinstance(plan, Plan):
            return plan
        return self._get_plan(plan)

    def _get_plan(self, plan_id: str) -> Plan:
        with self._lock:
            plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"No plan with id '{plan_id}'")
        return plan

    def _get_runner(self, plan_id: str) -> PlanRunner:
        with self._lock:
            runner = self._runners.get(plan_id)
        if runner is None:
            raise PlanNotFoundError(f"No running/executed plan with id '{plan_id}'")
        return runner
