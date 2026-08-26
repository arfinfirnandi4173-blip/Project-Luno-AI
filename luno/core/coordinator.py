"""
coordinator.py
===============

`Coordinator` - "receives events, coordinates subsystems... routing
only, no AI reasoning."

Implemented as a thin routing table on top of the Event Bus: each
`add_route(event_pattern, module_name)` call subscribes to the bus and,
on a match, forwards the event to that module's `on_event()` hook (see
`Module.on_event` in `module_manager.py`) via the `ModuleManager` -
never by importing or calling the target subsystem directly. The
Coordinator has zero opinion about what a module DOES with an event
once delivered; it only knows who should be told about what, which is
exactly the "routing only" boundary the spec draws.

Worked example (matches the spec's own pipeline sketch):

    coordinator.add_route("speech_recognized", "behavior_tree")
    coordinator.add_route("tool_finished", "context_builder_feed")
    coordinator.add_route("home_assistant_event", "behavior_tree")

Fan-out is native: multiple `add_route()` calls with the same pattern
notify multiple modules; multiple patterns can route to the same
module. Target module resolution goes through `ModuleManager.get()`,
so a route to a `lazy=True` module transparently starts it on first
delivery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

from .events import Event
from .utils import log

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .module_manager import ModuleManager


class Coordinator:
    def __init__(self, event_bus: "EventBus", module_manager: "ModuleManager") -> None:
        self.event_bus = event_bus
        self.module_manager = module_manager
        self._routes: Dict[str, List[str]] = {}
        self._subscription_ids: List[str] = []

    def add_route(self, event_pattern: str, module_name: str, priority: int = 0) -> str:
        self._routes.setdefault(event_pattern, []).append(module_name)
        sub_id = self.event_bus.subscribe(
            event_pattern, lambda e, m=module_name: self._forward(e, m), priority=priority,
        )
        self._subscription_ids.append(sub_id)
        log(f"route added: '{event_pattern}' -> module '{module_name}'", "coordinator")
        return sub_id

    def routes(self) -> Dict[str, List[str]]:
        return {k: list(v) for k, v in self._routes.items()}

    def _forward(self, event: Event, module_name: str) -> None:
        module = self.module_manager.get(module_name)
        if module is None:
            log(f"route target module '{module_name}' not found for event '{event.type}'", "coordinator")
            return
        try:
            module.on_event(event)
        except Exception as ex:
            log(f"module '{module_name}'.on_event() raised for '{event.type}': {ex}", "coordinator")

    def teardown(self) -> None:
        for sub_id in self._subscription_ids:
            self.event_bus.unsubscribe(sub_id)
        self._subscription_ids.clear()
        self._routes.clear()
