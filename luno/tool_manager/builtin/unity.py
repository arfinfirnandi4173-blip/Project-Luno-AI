"""
unity.py
========

MOCK Unity/avatar handler - "Mock implementation." A real handler would
forward these into the existing, COMPLETE avatar pipeline (`luno.
avatar_bridge`/`luno.vnyan_engine_bridge`/VMC OSC messages) instead of
just returning a canned message.
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["wave", "look_at_user", "dance", "change_expression"]


class MockUnityHandler(ToolHandler):
    name = "unity"
    default_timeout_s = 5.0
    max_timeout_s = 15.0

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action == "change_expression" and not (tool_call.target or tool_call.parameters.get("expression")):
            return "Action 'change_expression' requires a 'target' or parameters.expression"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action

        if action == "wave":
            return ToolResult.ok(self.name, action, "[MOCK] Avatar waved", data={})
        if action == "look_at_user":
            return ToolResult.ok(self.name, action, "[MOCK] Avatar looked at user", data={})
        if action == "dance":
            return ToolResult.ok(self.name, action, "[MOCK] Avatar danced", data={})
        if action == "change_expression":
            expression = tool_call.target or tool_call.parameters.get("expression")
            return ToolResult.ok(self.name, action, f"[MOCK] Avatar expression set to '{expression}'",
                                  data={"expression": expression})

        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
