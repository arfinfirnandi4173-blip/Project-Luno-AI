"""
automation.py
==============

`AutomationToolHandler` - a THIN `ToolHandler` adapter (same
architectural role `camera_patrol.py`/`camera_ptz.py`/`home_assistant.py`
play relative to their own underlying logic) that lets automation
`run`/`enable`/`disable`/`status` commands flow through the exact same
Planner -> `tool_requested` -> `ToolManagerBridgeModule` -> registry
pipeline every other tool call already uses - Sprint 72 Phase 14's own
"LLM hanya boleh memilih registered automation" boundary is enforced
HERE: this handler never accepts arbitrary code, only a `target` string
that must already match a loaded `AutomationRule.id` (or `unknown_
automation` is returned) - see `AutomationEngine.run_automation()`'s own
docstring.

This handler never talks to a camera or Home Assistant device itself. It
only ever calls the already-constructed `AutomationEngine` instance it's
handed at registration time (see `luno/bootstrap/modules.py`).
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["run", "enable", "disable", "status"]


class AutomationToolHandler(ToolHandler):
    name = "automation"
    #: Matches `run_automation()`'s own "never blocks for the duration"
    #: contract (see `AutomationEngine.run_automation()`) - a short
    #: timeout is intentional, not a copy-paste of a device handler's own.
    default_timeout_s = 5.0
    max_timeout_s = 10.0

    def __init__(self, engine: "AutomationEngine") -> None:  # noqa: F821 - see luno/automation/engine.py
        self._engine = engine

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        rule_id = tool_call.target

        if action == "status":
            if rule_id:
                status = self._engine.get_automation_status(rule_id)
                if status is None:
                    return ToolResult.fail(self.name, action, f"No automation called '{rule_id}'.", error_type="unknown_automation")
                return ToolResult.ok(self.name, action, f"Automation '{rule_id}' is {'enabled' if status['enabled'] else 'disabled'}.", data=status)
            statuses = self._engine.get_status()
            return ToolResult.ok(self.name, action, f"{len(statuses)} automation(s) configured.", data={"automations": statuses})

        if not rule_id:
            return ToolResult.fail(self.name, action, "This action requires a target automation id.", error_type="unknown_automation")

        if action == "run":
            result = self._engine.run_automation(rule_id)
        elif action == "enable":
            result = self._engine.enable_automation(rule_id)
        else:  # "disable"
            result = self._engine.disable_automation(rule_id)

        if result.get("ok"):
            return ToolResult.ok(self.name, action, result.get("message", ""), data=result)
        return ToolResult.fail(self.name, action, result.get("message", ""), error_type=result.get("code"), data=result)
