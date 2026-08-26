"""
home_assistant.py
==================

MOCK Home Assistant handler - per the spec, "Do NOT connect to Home
Assistant yet. Return mock ToolResults." Keeps a small IN-MEMORY device
state dict (`_state`) purely so the mock is internally consistent (e.g.
`turn_on` then a real read of `_state` reflects it) and more useful for
tests than a stateless "always says OK" stub would be - it is still not
talking to anything real.

FUTURE INTEGRATION: a real handler would subclass `ToolHandler` the same
way, implement the same `supported_actions()`, and call `luno.ha_client.
HomeAssistantClient` inside `execute()` instead of touching `_state`.
Swapping it in is exactly:

    registry.register("home_assistant", RealHomeAssistantHandler())

No Planner or Tool Manager changes required - see the package docstring
in `__init__.py`.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

#: P0.14 (Advanced Home Assistant Automation Actions & Script Runner) -
#: `"call_service"` (generic, controlled HA service passthrough) and
#: `"activate_scene"` are the only two genuinely NEW ToolManager actions
#: this sprint adds - every other P0.14 automation action type
#: (toggle/set_temperature/set_color/set_brightness/run_script) maps onto
#: an action this handler already supported before P0.14 existed.
_SUPPORTED_ACTIONS = [
    "turn_on", "turn_off", "toggle", "run_script", "set_temperature", "set_color", "set_brightness",
    "call_service", "activate_scene",
]


class MockHomeAssistantHandler(ToolHandler):
    name = "home_assistant"
    default_timeout_s = 5.0
    max_timeout_s = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = {}  # target -> {"on": bool} or {"temperature": float}, simulated only

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action == "call_service":
            domain = tool_call.parameters.get("domain")
            service = tool_call.parameters.get("service")
            if not domain or not isinstance(domain, str):
                return "Action 'call_service' requires parameters.domain"
            if not service or not isinstance(service, str):
                return "Action 'call_service' requires parameters.service"
            return None
        if tool_call.action not in ("run_script", "call_service") and not tool_call.target:
            return f"Action '{tool_call.action}' requires a 'target' (e.g. a device/entity name)"
        if tool_call.action == "set_temperature" and "value" not in tool_call.parameters:
            return "Action 'set_temperature' requires parameters.value"
        if tool_call.action == "set_color" and "color" not in tool_call.parameters and "rgb" not in tool_call.parameters:
            return "Action 'set_color' requires parameters.color or parameters.rgb"
        if tool_call.action == "set_brightness" and "level" not in tool_call.parameters:
            return "Action 'set_brightness' requires parameters.level"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        target = tool_call.target or ""
        with self._lock:
            if tool_call.action == "turn_on":
                self._state[target] = {"on": True}
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Turned on '{target}'", data={"target": target, "on": True})

            if tool_call.action == "turn_off":
                self._state[target] = {"on": False}
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Turned off '{target}'", data={"target": target, "on": False})

            if tool_call.action == "toggle":
                was_on = self._state.get(target, {}).get("on", False)
                self._state[target] = {"on": not was_on}
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Toggled '{target}' to {'on' if not was_on else 'off'}",
                                      data={"target": target, "on": not was_on})

            if tool_call.action == "run_script":
                script = target or tool_call.parameters.get("script", "unnamed_script")
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Ran script '{script}'", data={"script": script})

            if tool_call.action == "activate_scene":
                self._state[target] = {"on": True}
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Activated scene '{target}'",
                                      data={"target": target, "on": True})

            if tool_call.action == "call_service":
                domain = tool_call.parameters.get("domain")
                service = tool_call.parameters.get("service")
                entity_id = tool_call.parameters.get("entity_id")
                data = tool_call.parameters.get("data") or {}
                self._state[target or f"{domain}.{service}"] = {"domain": domain, "service": service, "data": data}
                return ToolResult.ok(
                    self.name, tool_call.action, f"[MOCK] Called service {domain}.{service} on {entity_id!r}",
                    data={"domain": domain, "service": service, "entity_id": entity_id, "data": data},
                )

            if tool_call.action == "set_temperature":
                value = tool_call.parameters["value"]
                self._state[target] = {"temperature": value}
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Set '{target}' temperature to {value}",
                                      data={"target": target, "temperature": value})

            if tool_call.action == "set_color":
                # "rgb" (explicit [r, g, b] triplet) takes priority over
                # "color" (a name from the fixed palette) when somehow
                # both are present - a hand-built ToolCall bypassing the
                # parser is the only way that could happen, since
                # `_classify_color_set` only ever produces one or the
                # other, never both.
                if "rgb" in tool_call.parameters:
                    rgb = tool_call.parameters["rgb"]
                    self._state.setdefault(target, {})["rgb"] = rgb
                    return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Set '{target}' color to rgb{tuple(rgb)}",
                                          data={"target": target, "rgb": rgb})
                color = tool_call.parameters["color"]
                self._state.setdefault(target, {})["color"] = color
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Set '{target}' color to {color}",
                                      data={"target": target, "color": color})

            if tool_call.action == "set_brightness":
                level = tool_call.parameters["level"]
                self._state.setdefault(target, {})["brightness"] = level
                return ToolResult.ok(self.name, tool_call.action, f"[MOCK] Set '{target}' brightness to {level}%",
                                      data={"target": target, "brightness": level})

        # Unreachable given validate() already restricts action to
        # _SUPPORTED_ACTIONS, kept as a defensive fallback.
        return ToolResult.fail(self.name, tool_call.action, f"Unhandled action '{tool_call.action}'")
