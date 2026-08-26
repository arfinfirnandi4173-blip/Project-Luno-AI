"""
Planner + Execution Queue
=========================

Luno's executive function. The Behavior Tree decides WHAT Luno should do
next (see `luno/behavior_tree/`); this package decides HOW to accomplish
it - turning a user request into one or more structured, dependency-
ordered `ToolCall`s and driving them to completion safely (retries,
rollback, cancellation, pause/resume).

    Whisper -> Behavior Tree -> Planner -> Execution Queue ->
    Tool Manager (future integration) -> OpenRouter (language generation
    when needed) -> Fish Audio -> Unity

This package NEVER talks to hardware or any external service - not Home
Assistant, not Windows, not Spotify, not the browser, not Vision, not
OpenRouter. It only produces and manages generic `ToolCall`s like:

    {"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"}

A real handler gets attached later via `Planner.registry.register(name,
handler)` (the future Tool Manager) - see `executor.ToolRegistry`. Until
a tool is registered, plans referencing it still PLAN and VALIDATE fine
(unless a strict `PlanValidator` with a known-tools list says otherwise);
execution simply raises `ToolNotRegisteredError` for that task. Adding a
brand new tool never requires changing anything in this package.

Quick start (standalone - no hardware/API needed)
----------------------------------------------------
    from luno.planner import Planner

    planner = Planner()
    planner.registry.register("home_assistant", handler=lambda tc: print("HA:", tc))
    planner.registry.register("windows", handler=lambda tc: print("Windows:", tc))
    planner.registry.register("spotify", handler=lambda tc: print("Spotify:", tc))

    plan = planner.create_plan(
        "Luno, open Chrome, turn on the bedroom light, turn off the desk lamp, then play Spotify."
    )
    planner.execute(plan)

    import time; time.sleep(0.5)
    print(planner.get_status(plan.id))

Architecture (see each file's own docstring for the full story)
-------------------------------------------------------------------
    models.py       leaf data types (TaskStatus/PlanStatus/ToolCall/RetryPolicy/ProgressReport)
    task.py          Task + Plan (behavior-rich, reference models.py)
    exceptions.py     every exception this package raises on purpose
    utils.py           id/time helpers
    context.py           PlannerContext - "Context Awareness" input
    parser.py              text -> ParsedStep (heuristic, swappable)
    dependency.py             DependencyGraph - ordering + parallel structure
    validator.py               "reject invalid plans" before execution
    queue.py                     thread-safe per-plan task state container
    executor.py                    runs one task attempt, retries, rollback
    scheduler.py                     PlanRunner - drives one Plan to completion
    planner.py                         the public facade re-exported below

Standalone and decoupled from `main.py`, same shape `luno/vision_memory/`
and `luno/behavior_tree/` were built with - wiring this into the running
Luno (constructing `PlannerContext` from real Vision Memory/Home
Assistant/Behavior Tree state, registering real tool handlers) happens
later, not as part of this package.
"""

from .context import PlannerContext
from .dependency import DependencyGraph
from .exceptions import (
    DependencyCycleError,
    PlanNotFoundError,
    PlannerError,
    ToolNotRegisteredError,
    ValidationError,
)
from .executor import TaskExecutor, ToolRegistry, ToolSpec
from .models import (
    PlanStatus,
    ProgressReport,
    RetryPolicy,
    TaskStatus,
    ToolCall,
)
from .parser import IntentParser, ParsedStep
from .planner import Planner, PlanOptions
from .queue import ExecutionQueue
from .scheduler import PlanRunner
from .task import Plan, Task
from .validator import PlanValidator

__all__ = [
    "Planner", "PlanOptions",
    "PlannerContext",
    "Plan", "Task",
    "TaskStatus", "PlanStatus", "ToolCall", "RetryPolicy", "ProgressReport",
    "ParsedStep", "IntentParser",
    "DependencyGraph",
    "ExecutionQueue",
    "PlanValidator",
    "ToolRegistry", "ToolSpec", "TaskExecutor",
    "PlanRunner",
    "PlannerError", "ValidationError", "DependencyCycleError", "PlanNotFoundError", "ToolNotRegisteredError",
]
