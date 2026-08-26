"""
manager.py
==========

`AdapterManager` - registers adapters, wires their configured event
routes, and drives their lifecycle. It does NOT reimplement dependency
ordering, startup/shutdown sequencing, or event delivery - all of that
is Core's job (`ModuleManager`, `LifecycleManager`, `Coordinator`,
`EventBus`), already built and tested in `luno.core`. `AdapterManager`
is a thin, adapter-flavored facade on top of those four collaborators,
adding exactly what adapters need beyond a generic `Module`:
enable/disable configuration and automatic event-route wiring from an
`EventMapping`.

"Adapters must never manually instantiate each other": every adapter
reaches the Event Bus only via `BaseAdapter.bind()`, called here, and
never receives a reference to any other adapter.

Two ways to build one:

  - `AdapterManager(module_manager, coordinator, event_bus, ...)` -
    hands in Core components a `Runtime` already owns, so adapters live
    in the exact same engine as every other `Module` in the process.

  - `AdapterManager.standalone()` - builds its own minimal, throwaway
    `EventBus`/`ModuleManager`/`Coordinator`/`HealthMonitor` for
    isolated testing (no `Runtime` required) - exactly the same
    "independently testable" pattern used by every package this
    session.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Dict, List, Optional

from ..core.coordinator import Coordinator
from ..core.dispatcher import Dispatcher
from ..core.event_bus import EventBus
from ..core.health import HealthMonitor
from ..core.lifecycle import LifecycleManager
from ..core.models import ModuleHealthStatus
from ..core.module_manager import ModuleManager
from ..core.exceptions import ModuleStopError
from .exceptions import AdapterNotFoundError
from .models import AdapterConfig, DEFAULT_ADAPTER_EVENT_MAPPING, EventMapping
from .registry import AdapterRegistry
from .utils import log

if TYPE_CHECKING:
    from .base import BaseAdapter


class AdapterManager:
    def __init__(
        self,
        module_manager: ModuleManager,
        coordinator: Coordinator,
        event_bus: EventBus,
        lifecycle: Optional[LifecycleManager] = None,
        health_monitor: Optional[HealthMonitor] = None,
        event_mapping: Optional[EventMapping] = None,
    ) -> None:
        self.module_manager = module_manager
        self.coordinator = coordinator
        self.event_bus = event_bus
        self.health_monitor = health_monitor or HealthMonitor(module_manager, event_bus=event_bus)
        self.lifecycle = lifecycle or LifecycleManager(module_manager, event_bus=event_bus, health_monitor=self.health_monitor)
        self.event_mapping = event_mapping if event_mapping is not None else EventMapping.from_dict(DEFAULT_ADAPTER_EVENT_MAPPING)
        self.registry = AdapterRegistry()
        self._lock = threading.RLock()
        #: adapter name -> list of Coordinator/EventBus subscription ids
        #: for that adapter's routes - tracked here (not in `Coordinator`,
        #: which has no per-target removal API) so `disable()`/
        #: `unregister()` can cleanly tear down routing without leaking
        #: stale subscriptions or double-registering on a later `enable()`.
        self._route_subs: Dict[str, List[str]] = {}

    @classmethod
    def standalone(cls, event_mapping: Optional[EventMapping] = None) -> "AdapterManager":
        """Builds a fully self-contained `AdapterManager` with its own
        Core engine components - no `Runtime` needed. Caller owns
        starting/stopping the returned manager's `event_bus`/`dispatcher`
        (see `start_all()`/`stop_all()`, which do this automatically)."""
        dispatcher = Dispatcher(max_workers=8)
        event_bus = EventBus(dispatcher=dispatcher)
        module_manager = ModuleManager()
        coordinator = Coordinator(event_bus, module_manager)
        health_monitor = HealthMonitor(module_manager, event_bus=event_bus)
        manager = cls(
            module_manager=module_manager, coordinator=coordinator, event_bus=event_bus,
            health_monitor=health_monitor, event_mapping=event_mapping,
        )
        manager.dispatcher = dispatcher
        manager._owns_engine = True
        return manager

    # -- registration -----------------------------------------------------

    def register(self, adapter: "BaseAdapter", config: Optional[AdapterConfig] = None) -> None:
        cfg = config or AdapterConfig(name=adapter.name)
        with self._lock:
            self.registry.register(adapter, cfg)
            adapter.bind(self.event_bus)
            if cfg.enabled:
                self._activate(adapter, cfg)
            else:
                log(f"adapter '{adapter.name}' registered but disabled - not started", "manager")

    def unregister(self, name: str) -> bool:
        with self._lock:
            cfg = self.registry.get_config(name)
            if cfg is not None and cfg.enabled:
                try:
                    self._deactivate(name)
                except ModuleStopError as ex:
                    log(f"cannot unregister '{name}': {ex}", "manager")
                    return False
            return self.registry.unregister(name)

    def _activate(self, adapter: "BaseAdapter", cfg: AdapterConfig) -> None:
        self.module_manager.register(adapter, dependencies=cfg.dependencies, lazy=cfg.lazy)
        sub_ids = [
            self.coordinator.add_route(pattern, adapter.name, priority=priority)
            for pattern, priority in self.event_mapping.subscriptions_for(adapter.name)
        ]
        self._route_subs[adapter.name] = sub_ids
        log(f"adapter '{adapter.name}' activated (dependencies={cfg.dependencies}, lazy={cfg.lazy})", "manager")

    def _deactivate(self, name: str) -> None:
        """Raises `ModuleStopError` (propagated straight from
        `ModuleManager.stop()`) if another still-enabled adapter depends
        on this one - deliberately NOT swallowed here, since silently
        continuing to unregister a module that refused to stop would
        leave `ModuleManager` and this manager's bookkeeping
        inconsistent. Callers (`disable()`/`unregister()`) turn that into
        a clean `False`/re-raise instead of partial teardown."""
        record = self.module_manager.all_modules().get(name)
        if record is not None and record.state.value == "running":
            self.module_manager.stop(name)
        self.module_manager.unregister(name)
        for sub_id in self._route_subs.pop(name, []):
            self.event_bus.unsubscribe(sub_id)
        log(f"adapter '{name}' deactivated", "manager")

    def enable(self, name: str) -> bool:
        with self._lock:
            adapter = self.registry.get(name)
            cfg = self.registry.get_config(name)
            if adapter is None or cfg is None or cfg.enabled:
                return False
            cfg.enabled = True
            self._activate(adapter, cfg)
        self.module_manager.start(name)
        return True

    def disable(self, name: str) -> bool:
        with self._lock:
            cfg = self.registry.get_config(name)
            if cfg is None or not cfg.enabled:
                return False
            try:
                self._deactivate(name)
            except ModuleStopError as ex:
                log(f"cannot disable '{name}': {ex}", "manager")
                return False
            cfg.enabled = False
            return True

    # -- lifecycle ----------------------------------------------------------

    def start_all(self) -> List[str]:
        if getattr(self, "_owns_engine", False):
            self.event_bus.start()
            self.dispatcher.start()
        return self.lifecycle.startup()

    def stop_all(self) -> None:
        self.lifecycle.shutdown()
        if getattr(self, "_owns_engine", False):
            self.dispatcher.stop(wait=False)
            self.event_bus.stop(wait=False)

    def restart_all(self) -> List[str]:
        restarted = []
        for name in self.registry.list_enabled():
            try:
                self.restart(name)
                restarted.append(name)
            except Exception as ex:
                log(f"restart_all: '{name}' failed to restart: {ex}", "manager")
        return restarted

    def restart(self, name: str) -> None:
        """Calls the adapter's OWN `restart()` (its `_do_stop()`/
        `_do_start()` plus its restart-count/log bookkeeping - see
        `base.py`) directly, rather than `ModuleManager.restart()`
        (which only knows the generic `Module.stop()`/`start()` pair and
        has no idea `restart()` even exists as a richer adapter-level
        concept). This is exactly the spec's per-adapter `restart()`
        contract, honored end to end."""
        self.registry.require(name).restart()

    # -- health / status ------------------------------------------------------

    def health_all(self) -> Dict[str, ModuleHealthStatus]:
        return {name: self.module_manager.health_of(name) for name in self.registry.list_enabled()}

    def status_all(self) -> Dict[str, dict]:
        result = {}
        for name in self.registry.list_adapters():
            adapter = self.registry.get(name)
            cfg = self.registry.get_config(name)
            entry = adapter.status() if adapter is not None else {}
            entry["enabled"] = cfg.enabled if cfg is not None else False
            if name in self.module_manager.all_modules():
                entry["module_state"] = self.module_manager.all_modules()[name].state.value
            result[name] = entry
        return result
