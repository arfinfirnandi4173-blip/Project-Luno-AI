"""
module_manager.py
==================

`Module` - the interface every real subsystem adapter implements
(Whisper, Gemini Vision, Vision Memory, Behavior Tree, Planner, Tool
Manager, Fish Audio, Unity, and any future one) - and `ModuleManager`,
which owns every registered `Module` instance and is the ONLY thing
allowed to create/start/stop them: "Modules should never manually
create each other. Everything comes from Module Manager."

`Module` itself is intentionally tiny (`start`/`stop` required,
everything else optional with a safe default) so that wrapping an
existing standalone package - e.g. `luno.behavior_tree` - is a thin
adapter class, not a rewrite: see the package docstring in
`__init__.py` for a worked example.

Dependency ordering reuses the exact 3-color DFS approach proven in
`luno/planner/dependency.py` (same cycle-path reporting, same
topological-sort guarantee) - no reason to reinvent it for a second,
smaller graph.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .events import Event
from .exceptions import (
    DependencyCycleError,
    ModuleAlreadyRegisteredError,
    ModuleNotFoundError,
    ModuleStartError,
    ModuleStopError,
)
from .models import ModuleHealthStatus, ModuleState
from .utils import log, utcnow


class Module(ABC):
    """Base interface for every subsystem adapter.

    `name` and `dependencies` are read once, at `register()` time (as a
    class attribute or set in `__init__` before registering). Only
    `start()`/`stop()` are required; `health()` and `on_event()` have
    safe defaults so a minimal adapter is just those two methods.
    """

    name: str = ""
    dependencies: List[str] = []

    @abstractmethod
    def start(self) -> None:
        """Called by `ModuleManager.start()`. Raise to signal failure -
        the manager marks the module FAILED and records the error; it
        does not retry automatically (see `lifecycle.py` for that)."""

    @abstractmethod
    def stop(self) -> None:
        """Called by `ModuleManager.stop()`/`restart()`. Exceptions here
        are logged and swallowed - a broken `stop()` must never block
        shutdown of the rest of the system."""

    def health(self) -> ModuleHealthStatus:
        """Default: healthy whenever the manager considers it RUNNING.
        Override to report real liveness/staleness (e.g. "last frame
        processed > 30s ago" for a vision poller)."""
        return ModuleHealthStatus(healthy=True)

    def on_event(self, event: Event) -> None:
        """Called by the Coordinator when this module is the target of a
        route (see `coordinator.py`). Default: no-op - most modules only
        care about a subset of events and register routes accordingly,
        so an un-overridden `on_event` just means "not wired to receive
        anything yet"."""

    def reload(self) -> None:
        """Optional: called by `Runtime.reload()`. Default: no-op."""


@dataclass
class ModuleRecord:
    name: str
    module: Module
    dependencies: List[str]
    state: ModuleState = ModuleState.REGISTERED
    lazy: bool = False
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    restart_count: int = 0


class ModuleManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: Dict[str, ModuleRecord] = {}

    # -- registration -----------------------------------------------------

    def register(self, module: Module, dependencies: Optional[List[str]] = None, lazy: bool = False) -> None:
        name = module.name
        if not name:
            raise ValueError("Module must set a non-empty .name before registering")
        with self._lock:
            if name in self._modules:
                raise ModuleAlreadyRegisteredError(f"Module '{name}' already registered")
            deps = list(dependencies) if dependencies is not None else list(getattr(module, "dependencies", []))
            self._modules[name] = ModuleRecord(name=name, module=module, dependencies=deps, lazy=lazy)
        log(f"registered module '{name}' (dependencies={deps}, lazy={lazy})", "module_manager")

    def unregister(self, name: str) -> bool:
        with self._lock:
            record = self._modules.get(name)
            if record is None:
                return False
            if record.state == ModuleState.RUNNING:
                raise ModuleStopError(f"Cannot unregister running module '{name}' - stop it first")
            self._modules.pop(name)
        log(f"unregistered module '{name}'", "module_manager")
        return True

    # -- dependency ordering ------------------------------------------------

    def dependency_order(self) -> List[str]:
        """Dependencies-first topological order. Startup uses this
        directly; shutdown uses it reversed (see `lifecycle.py`)."""
        with self._lock:
            names = list(self._modules.keys())
            deps_map = {n: list(self._modules[n].dependencies) for n in names}

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in names}
        order: List[str] = []
        path: List[str] = []

        def visit(n: str) -> None:
            color[n] = GRAY
            path.append(n)
            for dep in deps_map[n]:
                if dep not in deps_map:
                    raise ModuleNotFoundError(f"Module '{n}' depends on unregistered module '{dep}'")
                if color[dep] == GRAY:
                    cycle = path[path.index(dep):] + [dep]
                    raise DependencyCycleError("Module dependency cycle: " + " -> ".join(cycle))
                if color[dep] == WHITE:
                    visit(dep)
            path.pop()
            color[n] = BLACK
            order.append(n)

        for n in names:
            if color[n] == WHITE:
                visit(n)
        return order

    # -- start/stop/restart -------------------------------------------------

    def start(self, name: str) -> None:
        with self._lock:
            record = self._require(name)
            for dep in record.dependencies:
                dep_record = self._modules.get(dep)
                if dep_record is None or dep_record.state != ModuleState.RUNNING:
                    raise ModuleStartError(f"Cannot start '{name}': dependency '{dep}' is not running")
            record.state = ModuleState.STARTING

        log(f"starting module '{name}'...", "module_manager")
        try:
            record.module.start()
        except Exception as ex:
            with self._lock:
                record.state = ModuleState.FAILED
                record.error = str(ex)
            log(f"module '{name}' failed to start: {ex}", "module_manager")
            raise ModuleStartError(f"Module '{name}' failed to start: {ex}") from ex

        with self._lock:
            record.state = ModuleState.RUNNING
            record.started_at = utcnow()
            record.error = None
        log(f"module '{name}' running", "module_manager")

    def stop(self, name: str) -> None:
        with self._lock:
            record = self._require(name)
            dependents = [
                r.name for r in self._modules.values()
                if name in r.dependencies and r.state == ModuleState.RUNNING
            ]
            if dependents:
                raise ModuleStopError(f"Cannot stop '{name}': still depended on by running module(s) {dependents}")
            record.state = ModuleState.STOPPING

        log(f"stopping module '{name}'...", "module_manager")
        try:
            record.module.stop()
        except Exception as ex:
            log(f"module '{name}' raised during stop (continuing anyway): {ex}", "module_manager")

        with self._lock:
            record.state = ModuleState.STOPPED
        log(f"module '{name}' stopped", "module_manager")

    def restart(self, name: str) -> None:
        with self._lock:
            record = self._require(name)
            record.state = ModuleState.RESTARTING

        log(f"restarting module '{name}'...", "module_manager")
        try:
            record.module.stop()
        except Exception as ex:
            log(f"module '{name}' raised during restart-stop (continuing anyway): {ex}", "module_manager")

        try:
            record.module.start()
        except Exception as ex:
            with self._lock:
                record.state = ModuleState.FAILED
                record.error = str(ex)
                record.restart_count += 1
            raise ModuleStartError(f"Module '{name}' failed to restart: {ex}") from ex

        with self._lock:
            record.state = ModuleState.RUNNING
            record.started_at = utcnow()
            record.error = None
            record.restart_count += 1
        log(f"module '{name}' restarted", "module_manager")

    # -- health / lazy access -------------------------------------------------

    def health_of(self, name: str) -> ModuleHealthStatus:
        with self._lock:
            record = self._modules.get(name)
        if record is None:
            return ModuleHealthStatus(healthy=False, message="not registered")
        if record.state != ModuleState.RUNNING:
            ok = record.state in (ModuleState.REGISTERED, ModuleState.STOPPED)
            return ModuleHealthStatus(healthy=ok, message=f"state={record.state.value}")
        try:
            return record.module.health()
        except Exception as ex:
            return ModuleHealthStatus(healthy=False, message=f"health() raised: {ex}")

    def get(self, name: str) -> Optional[Module]:
        """Lazy-loading accessor: a module registered with `lazy=True`
        is started on first access here rather than during bulk
        startup - callers (chiefly the Coordinator, routing an event to
        a target module) never need to know or care whether something
        was already running."""
        with self._lock:
            record = self._modules.get(name)
        if record is None:
            return None
        if record.lazy and record.state != ModuleState.RUNNING:
            self.start(name)
        return record.module

    def all_modules(self) -> Dict[str, ModuleRecord]:
        with self._lock:
            return dict(self._modules)

    def running_modules(self) -> List[str]:
        with self._lock:
            return [n for n, r in self._modules.items() if r.state == ModuleState.RUNNING]

    def _require(self, name: str) -> ModuleRecord:
        record = self._modules.get(name)
        if record is None:
            raise ModuleNotFoundError(f"No module registered as '{name}'")
        return record
