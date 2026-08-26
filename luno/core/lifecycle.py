"""
lifecycle.py
============

`LifecycleManager` - turns `ModuleManager.dependency_order()` into an
actual startup/shutdown sequence, and implements the fault-isolation
requirement: "Subsystem crashes should not terminate Runtime. Restart
failed modules when possible."

Startup walks the dependency order and starts each module; a module
that fails to start does NOT abort the whole sequence - it's recorded
FAILED (via `ModuleManager`) and a `SystemError` event is published,
and startup continues with whatever's next. Anything that depends
(transitively) on a failed module is skipped rather than started on top
of a missing dependency, and reported the same way.

Shutdown is startup's dependency order reversed, matching the spec's
own example (Tool Manager starts after Planner after Behavior Tree, so
it stops first).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .events import SystemError
from .models import ModuleState
from .utils import log

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .health import HealthMonitor
    from .module_manager import ModuleManager


class LifecycleManager:
    def __init__(
        self,
        module_manager: "ModuleManager",
        event_bus: Optional["EventBus"] = None,
        health_monitor: Optional["HealthMonitor"] = None,
    ) -> None:
        self.module_manager = module_manager
        self.event_bus = event_bus
        self.health_monitor = health_monitor

    def startup(self) -> List[str]:
        order = self.module_manager.dependency_order()
        started: List[str] = []
        failed: set = set()

        for name in order:
            record = self.module_manager.all_modules()[name]
            if record.lazy:
                continue  # lazy modules start on first access, not at bulk startup

            blocked_by = [d for d in record.dependencies if d in failed]
            if blocked_by:
                failed.add(name)
                msg = f"skipped - dependency failed: {blocked_by}"
                log(f"'{name}' {msg}", "lifecycle")
                self._report_error(name, msg)
                continue

            try:
                self.module_manager.start(name)
                started.append(name)
            except Exception as ex:
                failed.add(name)
                log(f"'{name}' failed during startup: {ex}", "lifecycle")
                self._report_error(name, str(ex))

        return started

    def shutdown(self) -> None:
        order = list(reversed(self.module_manager.dependency_order()))
        for name in order:
            record = self.module_manager.all_modules().get(name)
            if record is None or record.state != ModuleState.RUNNING:
                continue
            try:
                self.module_manager.stop(name)
            except Exception as ex:
                log(f"'{name}' raised during shutdown (continuing): {ex}", "lifecycle")
                self._report_error(name, f"shutdown: {ex}")

    def restart_failed(self) -> List[str]:
        """Attempts to restart every module currently FAILED - the
        fault-recovery half of "restart failed modules when possible"."""
        restarted: List[str] = []
        for name, record in self.module_manager.all_modules().items():
            if record.state != ModuleState.FAILED:
                continue
            try:
                self.module_manager.start(name)
                restarted.append(name)
                log(f"'{name}' recovered after restart", "lifecycle")
            except Exception as ex:
                log(f"retry-start of '{name}' failed again: {ex}", "lifecycle")
                self._report_error(name, f"restart retry: {ex}")
        return restarted

    def _report_error(self, module_name: str, message: str) -> None:
        if self.health_monitor is not None:
            self.health_monitor.record_error(f"{module_name}: {message}")
        if self.event_bus is not None:
            self.event_bus.publish(SystemError(data={"module": module_name, "error": message}, source="lifecycle"))
