"""
camera_patrol.py
==================

`CameraPatrolToolHandler` - a THIN `ToolHandler` adapter (same
architectural role `camera_ptz.py`/`home_assistant.py` play relative to
their own underlying logic) that lets patrol's `start`/`stop`/`status`
commands flow through the exact same Planner -> `tool_requested` ->
`ToolManagerBridgeModule` -> registry pipeline every other tool call
already uses - no second dispatch mechanism, no new PTZ implementation.

This handler NEVER talks to the camera itself. It only ever calls the
already-constructed `CameraPatrolModule` instance (`luno/camera_patrol/
controller.py`) it's handed at registration time (see `luno/bootstrap/
modules.py`) - that module is, in turn, the ONLY thing that ever
publishes a further `tool_requested` for `camera_ptz` (see that class's
own docstring). `execute()` here returns almost instantly for every
action, including `"start"` - it kicks off the patrol's OWN background
thread and returns right away, rather than blocking for the whole
patrol's duration (which could be minutes) - see `CameraPatrolModule.
start_patrol()`'s own docstring for why that matters (a blocking
`execute()` here would starve `ToolManager`'s handler-pool timeout and
make a working patrol look like a timeout failure).
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["start", "stop", "status"]


class CameraPatrolToolHandler(ToolHandler):
    name = "camera_patrol"
    #: Both `start`/`stop`/`status` are near-instant (see module
    #: docstring) - a short timeout is intentional, not a copy-paste of
    #: camera_ptz's own (10s/20s) - this handler doing anything close to
    #: that long would itself be a bug.
    default_timeout_s = 5.0
    max_timeout_s = 10.0

    def __init__(self, controller: "CameraPatrolModule") -> None:  # noqa: F821 - see camera_patrol/controller.py
        self._controller = controller

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        if action == "start":
            result = self._controller.start_patrol(route_name=tool_call.target)
            if result.get("ok"):
                return ToolResult.ok(self.name, action, result.get("message", ""), data=result)
            return ToolResult.fail(self.name, action, result.get("message", ""), error_type=result.get("code"), data=result)

        if action == "stop":
            result = self._controller.stop_patrol()
            if result.get("ok"):
                return ToolResult.ok(self.name, action, result.get("message", ""), data=result)
            return ToolResult.fail(self.name, action, result.get("message", ""), error_type=result.get("code"), data=result)

        if action == "status":
            status = self._controller.get_status()
            return ToolResult.ok(self.name, action, f"Patrol is {status['state']}.", data=status)

        # Unreachable given validate() already restricts action to
        # supported_actions(), kept as a defensive fallback.
        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
