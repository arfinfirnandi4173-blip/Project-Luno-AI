"""
validator.py
============

The spec's "Validation" section, run BEFORE execution: "Validate tools
exist. Validate parameters. Detect impossible actions. Reject invalid
plans." Returns a list of human-readable error strings instead of
raising - `planner.py`'s `create_plan()` stores them on
`Plan.validation_errors` so a caller can inspect exactly what's wrong
without a try/except, and `execute()` refuses to run a plan that has any.

Tool knowledge is OPTIONAL by design (`known_tools=None` - the default).
The Planner doesn't own the list of real tools (the future Tool Manager
does - see `executor.ToolRegistry`), so with no registry supplied,
validation only catches STRUCTURAL problems (missing tool/action strings,
dependency cycles, references to nonexistent tasks) - it can't yet know
"home_assistant" is a real tool or "teleport" isn't. Pass a populated
`known_tools` dict (or `executor.ToolRegistry.specs()`) for full "does
this tool/parameter actually exist" checking once tools are registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .dependency import DependencyGraph
from .exceptions import DependencyCycleError, ValidationError
from .models import ToolCall
from .task import Task


@dataclass
class ToolSpec:
    """Minimal shape validator.py needs to know about a tool - see
    `executor.ToolSpec` for the fuller version the registry actually
    uses (this one is duplicated as a lightweight, dependency-free stand-in
    so `validator.py` doesn't need to import `executor.py` just for a
    type; the two are structurally compatible, `executor.ToolRegistry.
    specs()` returns objects matching this shape)."""
    name: str
    required_params: List[str] = field(default_factory=list)


class PlanValidator:
    def __init__(self, known_tools: Optional[Dict[str, ToolSpec]] = None) -> None:
        self.known_tools = known_tools or {}

    def validate_tool_call(self, tool_call: ToolCall, index: int = 0) -> List[str]:
        errors: List[str] = []
        if not tool_call.tool:
            errors.append(f"Task {index}: missing tool name")
            return errors
        if not tool_call.action:
            errors.append(f"Task {index}: missing action for tool '{tool_call.tool}'")

        if self.known_tools:
            spec = self.known_tools.get(tool_call.tool)
            if spec is None:
                errors.append(f"Task {index}: unknown tool '{tool_call.tool}'")
                return errors
            for param in spec.required_params:
                if param == "target":
                    if not tool_call.target:
                        errors.append(f"Task {index}: tool '{tool_call.tool}' requires 'target'")
                elif param not in tool_call.params:
                    errors.append(f"Task {index}: tool '{tool_call.tool}' missing required param '{param}'")
        return errors

    def validate_tasks(self, tasks: List[Task]) -> List[str]:
        """Full validation pass: every task's tool call, plus the
        dependency graph as a whole (missing references, cycles). Called
        by `Planner.create_plan()` right after building the task list."""
        errors: List[str] = []
        for i, task in enumerate(tasks):
            errors.extend(self.validate_tool_call(task.tool_call, i))

        try:
            DependencyGraph(tasks)
        except (DependencyCycleError, ValidationError) as ex:
            errors.append(str(ex))

        return errors
