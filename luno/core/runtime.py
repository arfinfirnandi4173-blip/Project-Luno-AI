"""
runtime.py
==========

`Runtime` - the entry point and owner of every other Core component.
Public API is exactly the spec's list: `start()`, `stop()`, `restart()`,
`reload()`, `health()`, `status()`. Nothing outside `Runtime` should
construct an `EventBus`, `Dispatcher`, `ModuleManager`, etc. directly in
real usage - `Runtime` wires them together once, consistently, and
hands out references (`runtime.event_bus`, `runtime.module_manager`,
...) for real modules/tests to use.

Startup order: Event Bus -> Dispatcher -> Scheduler -> Heartbeat, THEN
every registered module in dependency order (via `LifecycleManager`).
Shutdown is the exact reverse. See `lifecycle.py` for module-level fault
isolation - a module failing to start never prevents `Runtime.start()`
from completing; it's reflected in `status()`/`health()` instead.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .config import CoreConfig
from .context_builder import ContextBuilder
from .coordinator import Coordinator
from .dispatcher import Dispatcher
from .event_bus import EventBus
from .events import SystemStarted, SystemStopping
from .health import HealthMonitor
from .heartbeat import HeartbeatMonitor
from .lifecycle import LifecycleManager
from .models import HealthReport
from .module_manager import Module, ModuleManager
from .scheduler import Scheduler
from .utils import log, utcnow


class Runtime:
    def __init__(self, config: Optional[CoreConfig] = None) -> None:
        self.config = config or CoreConfig.default()

        self.dispatcher = Dispatcher(max_workers=self.config.dispatcher_max_workers)
        self.event_bus = EventBus(dispatcher=self.dispatcher, max_queue=self.config.event_queue_max)
        self.module_manager = ModuleManager()
        self.health_monitor = HealthMonitor(self.module_manager, event_bus=self.event_bus)
        self.lifecycle = LifecycleManager(self.module_manager, event_bus=self.event_bus, health_monitor=self.health_monitor)
        self.coordinator = Coordinator(self.event_bus, self.module_manager)
        self.scheduler = Scheduler(self.dispatcher, tick_interval_s=self.config.scheduler_tick_s)
        self.heartbeat = HeartbeatMonitor(self.event_bus, self.module_manager, interval_s=self.config.heartbeat_interval_s)
        self.context_builder = ContextBuilder()

        self._lock = threading.RLock()
        self._running = False
        self._started_at: Optional[Any] = None

    # -- module registration (delegates to ModuleManager, per spec: -----
    #    "Modules should never manually create each other. Everything
    #    comes from Module Manager.") ------------------------------------

    def register_module(self, module: Module, dependencies: Optional[List[str]] = None, lazy: bool = False) -> None:
        self.module_manager.register(module, dependencies=dependencies, lazy=lazy)

    def add_route(self, event_pattern: str, module_name: str, priority: int = 0) -> str:
        return self.coordinator.add_route(event_pattern, module_name, priority=priority)

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                log("Runtime already running - start() ignored", "runtime")
                return
            self._running = True
            self._started_at = utcnow()

        log("=== Luno Core Runtime starting ===", "runtime")
        self.event_bus.start()
        self.dispatcher.start()
        self.scheduler.start()
        self.heartbeat.start()

        started = self.lifecycle.startup()
        self.event_bus.publish(SystemStarted(data={"modules": started}, source="runtime"))
        log(f"=== Runtime started ({len(started)} module(s) running) ===", "runtime")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                log("Runtime already stopped - stop() ignored", "runtime")
                return
            self._running = False

        log("=== Luno Core Runtime stopping ===", "runtime")
        self.event_bus.publish(SystemStopping(source="runtime"))
        time.sleep(0.05)  # give the pump thread a moment to deliver SystemStopping before we tear it down

        self.lifecycle.shutdown()
        self.heartbeat.stop()
        self.scheduler.stop()
        self.coordinator.teardown()
        self.dispatcher.stop(wait=False)
        self.event_bus.stop(wait=False)
        log("=== Runtime stopped ===", "runtime")

    def restart(self) -> None:
        log("=== Runtime restart requested ===", "runtime")
        self.stop()
        time.sleep(0.1)
        self.start()

    def reload(self) -> None:
        """Reloads Runtime-level config (heartbeat/scheduler intervals)
        and calls `Module.reload()` on every registered module that
        implements it - does NOT stop/start any module (that's what
        `restart()`/`ModuleManager.restart()` are for)."""
        new_config = self.config.reload()
        self.config = new_config
        self.heartbeat.interval_s = new_config.heartbeat_interval_s
        self.scheduler.tick_interval_s = new_config.scheduler_tick_s

        for name, record in self.module_manager.all_modules().items():
            try:
                record.module.reload()
            except Exception as ex:
                log(f"module '{name}'.reload() raised: {ex}", "runtime")
        log("Runtime configuration reloaded", "runtime")

    def health(self) -> HealthReport:
        return self.health_monitor.report()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._running
            uptime_s = (utcnow() - self._started_at).total_seconds() if self._started_at else 0.0
        return {
            "running": running,
            "uptime_s": uptime_s,
            "modules": {n: r.state.value for n, r in self.module_manager.all_modules().items()},
            "event_bus": self.event_bus.stats(),
            "healthy": self.health_monitor.healthy(),
        }
