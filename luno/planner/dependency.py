"""
dependency.py
=============

Understands task ORDERING - the spec's "Dependency Graph" and "Parallel
Execution" sections. Two read models over the same `Task.depends_on`
data, for two different consumers:

    - `ready_tasks()`   - DYNAMIC: "given what's completed so far, which
                          WAITING tasks can run right now?" - used every
                          poll cycle by `scheduler.PlanRunner`. Naturally
                          returns MULTIPLE ids whenever independent tasks
                          are simultaneously ready - that's the entire
                          mechanism parallel execution runs on: nothing
                          special has to happen for "parallel", it falls
                          out of `PlanRunner` just dispatching every id
                          this returns instead of only one.

    - `topological_levels()` - STATIC: a full "level N depends only on
                          levels < N" grouping, ignoring runtime status.
                          Not used by execution at all - it exists so a
                          plan's parallel structure can be inspected/
                          asserted directly (see test_planner.py), and as
                          a second, independent way to prove there's no
                          cycle.
"""

from __future__ import annotations

from typing import Dict, List, Set

from .exceptions import DependencyCycleError, ValidationError
from .models import TaskStatus
from .task import Task


class DependencyGraph:
    def __init__(self, tasks: List[Task]) -> None:
        self.tasks_by_id: Dict[str, Task] = {t.id: t for t in tasks}
        self._validate_references()
        self._detect_cycle()

    def _validate_references(self) -> None:
        for task in self.tasks_by_id.values():
            for dep_id in task.depends_on:
                if dep_id not in self.tasks_by_id:
                    raise ValidationError(f"Task '{task.id}' depends on unknown task '{dep_id}'")

    def _detect_cycle(self) -> None:
        """Standard 3-color DFS. Raises `DependencyCycleError` with the
        actual cycle path (e.g. "a -> b -> c -> a") rather than just
        "there's a cycle somewhere" - much faster to debug a bad plan."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in self.tasks_by_id}
        path: List[str] = []

        def visit(tid: str) -> None:
            color[tid] = GRAY
            path.append(tid)
            for dep_id in self.tasks_by_id[tid].depends_on:
                if color[dep_id] == GRAY:
                    cycle = path[path.index(dep_id):] + [dep_id]
                    raise DependencyCycleError(f"Circular task dependency: {' -> '.join(cycle)}")
                if color[dep_id] == WHITE:
                    visit(dep_id)
            path.pop()
            color[tid] = BLACK

        for tid in self.tasks_by_id:
            if color[tid] == WHITE:
                visit(tid)

    def ready_tasks(self, completed_ids: Set[str]) -> List[str]:
        """WAITING tasks whose every dependency is in `completed_ids`.
        Only COMPLETED counts as "satisfied" - a dependency that FAILED/
        CANCELLED/SKIPPED must never silently let a dependent through;
        `scheduler.PlanRunner` cascades those to SKIPPED before calling
        this each cycle (see its module docstring), so by the time this
        runs, an unsatisfiable WAITING task has already stopped being
        WAITING."""
        ready = []
        for tid, task in self.tasks_by_id.items():
            if task.status != TaskStatus.WAITING:
                continue
            if all(dep_id in completed_ids for dep_id in task.depends_on):
                ready.append(tid)
        return ready

    def topological_levels(self) -> List[List[str]]:
        """Groups every task id into levels where level N's tasks depend
        only on ids in levels < N. Two ids in the SAME level have no
        dependency relationship and are safe to run concurrently - this
        is the structural proof that the engine supports parallel
        execution, independent of any particular plan's runtime status."""
        remaining = dict(self.tasks_by_id)
        resolved: Set[str] = set()
        levels: List[List[str]] = []
        while remaining:
            level = [tid for tid, t in remaining.items() if all(d in resolved for d in t.depends_on)]
            if not level:
                # _detect_cycle() already ran in __init__, so this should
                # be unreachable - kept as a defensive backstop.
                raise DependencyCycleError("Unresolvable dependency graph")
            level.sort()
            levels.append(level)
            for tid in level:
                remaining.pop(tid)
            resolved.update(level)
        return levels
