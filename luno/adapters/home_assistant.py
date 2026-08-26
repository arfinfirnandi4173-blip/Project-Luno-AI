"""
home_assistant.py
==================

`HomeAssistantAdapter` - two independent directions, both pure
translation:

  - INBOUND (HA -> Events): a real integration wraps whatever the
    existing `luno/ha_listener.py`/`luno/ha_client.py` websocket
    connection already does (NOT reimplemented here) and calls this
    adapter's `HomeAssistantListener` methods whenever a state changes
    or an automation fires; the adapter publishes `DeviceStateChanged`
    / `AutomationTriggered` / a generic `HomeAssistantEvent`.

  - OUTBOUND (Events -> HA): the adapter listens for `tool_requested`
    events whose `data["tool"] == "home_assistant"` and forwards them
    to an injected `HomeAssistantClient.call_service()`. This is a
    convenience for the adapter layer's own event-driven position in
    the pipeline, NOT a replacement for
    `luno.tool_manager.builtin.home_assistant`'s handler, which remains
    the canonical execution path for Planner-issued tool calls (that
    package is explicitly "MUST NOT be redesigned or modified" and this
    adapter never touches it).

No Home Assistant implementation lives in this file - interfaces and
mocks only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..core.events import HomeAssistantEvent
from .base import BaseAdapter
from .events import AutomationTriggered, DeviceStateChanged
from .utils import log

TOOL_NAME = "home_assistant"


class HomeAssistantListener(ABC):
    def on_state_changed(self, entity_id: str, old_state: Optional[str], new_state: str) -> None: ...
    def on_automation_triggered(self, automation_name: str, data: Optional[Dict[str, Any]] = None) -> None: ...


class HomeAssistantSource(ABC):
    @abstractmethod
    def start(self, listener: HomeAssistantListener) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class MockHomeAssistantSource(HomeAssistantSource):
    def __init__(self) -> None:
        self.listener: Optional[HomeAssistantListener] = None
        self.running = False

    def start(self, listener: HomeAssistantListener) -> None:
        self.listener = listener
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.listener = None

    def simulate_state_change(self, entity_id: str, old_state: Optional[str], new_state: str) -> None:
        if self.listener:
            self.listener.on_state_changed(entity_id, old_state, new_state)

    def simulate_automation(self, name: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.listener:
            self.listener.on_automation_triggered(name, data)


class HomeAssistantClient(ABC):
    @abstractmethod
    def call_service(
        self, domain: str, service: str, entity_id: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...


class MockHomeAssistantClient(HomeAssistantClient):
    def __init__(self) -> None:
        self.calls: list = []

    def call_service(
        self, domain: str, service: str, entity_id: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        call = {"domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}
        self.calls.append(call)
        return {"success": True, **call}


class HomeAssistantAdapter(BaseAdapter, HomeAssistantListener):
    name = "home_assistant"

    def __init__(self, source: Optional[HomeAssistantSource] = None, client: Optional[HomeAssistantClient] = None) -> None:
        BaseAdapter.__init__(self)
        self.source = source or MockHomeAssistantSource()
        self.client = client or MockHomeAssistantClient()

    def _do_start(self) -> None:
        self.source.start(self)

    def _do_stop(self) -> None:
        self.source.stop()

    # -- inbound: HA -> internal Events -----------------------------------

    def on_state_changed(self, entity_id: str, old_state: Optional[str], new_state: str) -> None:
        self.publish(DeviceStateChanged(data={"entity_id": entity_id, "old_state": old_state, "new_state": new_state}))
        self.publish(HomeAssistantEvent(data={
            "kind": "state_changed", "entity_id": entity_id, "old_state": old_state, "new_state": new_state,
        }))

    def on_automation_triggered(self, automation_name: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.publish(AutomationTriggered(data={"name": automation_name, "data": data or {}}))
        self.publish(HomeAssistantEvent(data={"kind": "automation_triggered", "name": automation_name, "data": data or {}}))

    # -- outbound: internal Events -> HA ------------------------------------

    def handle_event(self, event: Any) -> None:
        if event.type != "tool_requested" or event.get("tool") != TOOL_NAME:
            return
        action = event.get("action", "")
        target = event.get("target")
        parameters = event.get("parameters") or {}
        # HA service calls are "domain.service" (e.g. "light.turn_on"); if
        # the action doesn't specify a domain, fall back to HA's generic
        # "homeassistant" domain, which covers turn_on/turn_off/toggle for
        # any entity type - callers that need something more specific can
        # always pass a fully-qualified "domain.service" action.
        if "." in action:
            domain, service = action.split(".", 1)
        else:
            domain, service = "homeassistant", action

        log(f"forwarding tool_requested -> call_service({domain}.{service}, target={target})", self.name)
        result = self.client.call_service(domain, service, entity_id=target, data=parameters)
        self.publish(HomeAssistantEvent(data={"kind": "service_call_result", "action": action, "target": target, "result": result}))
