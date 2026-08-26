"""
windows.py
==========

MOCK Windows desktop-control handler - "Return simulated results." A real
handler would call into something like `luno/desktop_control.py`'s
`open_app`/`search_browser`/etc. inside `execute()`, subprocess/`os`
calls for shutdown/restart/sleep, and a platform volume/brightness API -
same swap-in shape as every other handler in this package.
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = [
    "launch_app", "close_app", "shutdown", "restart", "sleep", "set_volume", "set_brightness",
]

#: Actions that are destructive/system-wide enough that a real
#: implementation should almost certainly ask for confirmation first -
#: kept here as metadata a future real handler (or the Behavior Tree,
#: via ExecutionContext) can consult; the mock does not enforce it.
DANGEROUS_ACTIONS = frozenset({"shutdown", "restart"})


class MockWindowsHandler(ToolHandler):
    name = "windows"
    default_timeout_s = 5.0
    max_timeout_s = 20.0

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action in ("launch_app", "close_app") and not tool_call.target:
            return f"Action '{tool_call.action}' requires a 'target' (app name)"
        if tool_call.action == "set_volume" and not (0 <= tool_call.parameters.get("level", -1) <= 100):
            return "Action 'set_volume' requires parameters.level between 0 and 100"
        if tool_call.action == "set_brightness" and not (0 <= tool_call.parameters.get("level", -1) <= 100):
            return "Action 'set_brightness' requires parameters.level between 0 and 100"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        target = tool_call.target or ""

        if action == "launch_app":
            return ToolResult.ok(self.name, action, f"[MOCK] Launched '{target}'", data={"app": target})
        if action == "close_app":
            return ToolResult.ok(self.name, action, f"[MOCK] Closed '{target}'", data={"app": target})
        if action == "shutdown":
            return ToolResult.ok(self.name, action, "[MOCK] System shutdown simulated (no-op)", data={"simulated": True})
        if action == "restart":
            return ToolResult.ok(self.name, action, "[MOCK] System restart simulated (no-op)", data={"simulated": True})
        if action == "sleep":
            return ToolResult.ok(self.name, action, "[MOCK] System sleep simulated (no-op)", data={"simulated": True})
        if action == "set_volume":
            level = tool_call.parameters["level"]
            return ToolResult.ok(self.name, action, f"[MOCK] Volume set to {level}", data={"level": level})
        if action == "set_brightness":
            level = tool_call.parameters["level"]
            return ToolResult.ok(self.name, action, f"[MOCK] Brightness set to {level}", data={"level": level})

        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
