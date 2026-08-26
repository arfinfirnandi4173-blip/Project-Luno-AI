"""
exceptions.py
=============

Every exception this package raises on purpose. Callers (real wiring:
whatever in main.py eventually calls `Planner.create_plan()`/`execute()`)
can catch `PlannerError` to handle anything from this package uniformly,
or a specific subclass for a targeted response.
"""

from __future__ import annotations


class PlannerError(Exception):
    """Base class for every exception this package raises intentionally."""


class ValidationError(PlannerError):
    """A plan/task/tool-call failed validation (see `validator.py`) -
    unknown tool, missing required parameter, malformed dependency
    reference, etc. Raised by `Planner.execute()` when asked to run a
    plan that never passed validation; `create_plan()` itself does NOT
    raise this - it returns the plan with `Plan.validation_errors`
    populated instead, so callers can inspect what's wrong without a
    try/except (see planner.py's module docstring for why)."""


class DependencyCycleError(PlannerError):
    """The task dependency graph contains a cycle - see `dependency.py`."""


class PlanNotFoundError(PlannerError):
    """Raised by `Planner.cancel()`/`pause()`/`resume()`/`get_status()`
    when given a `plan_id` that isn't currently tracked."""


class ToolNotRegisteredError(PlannerError):
    """A task's tool has no handler registered in the `ToolRegistry` at
    execution time (see `executor.py`). NOT a validation error - a plan
    can be perfectly valid (structurally) and still fail here if the Tool
    Manager simply hasn't registered that tool yet."""
