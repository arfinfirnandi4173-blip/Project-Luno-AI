"""
vision.py
=========

MOCK vision handler - "Return placeholder responses. No camera
integration yet." A real handler would call `luno.vision.ask_vision()`
(camera + Gemini 2.0 Flash) for `look_camera`/`scan_room`, and
`luno.vision_memory.query_location()`/`get_world_state()` for
`query_memory` - both COMPLETE, existing systems this package is
explicitly told not to redesign, just eventually call into from here.

Note: this "vision" Tool Manager tool has never actually been wired up
in the production runtime - `IntentParser` (luno/planner/parser.py)
never produces a `look_camera`/`scan_room`/`query_memory` ToolCall, so
this handler (mock or otherwise) is never reached today. The real,
live vision path is `luno/vision_intent.py`'s classifier ->
`_handle_vision_intent()` in `main_runtime_demo.py` -> `ask_vision()`
directly, bypassing the Planner/Tool Manager entirely (see that
module's own docstring for why - this architecture doesn't do live
LLM function-calling, so tool-shaped vision access was never wired
through here). Left in place, unmodified, in case a future Planner-
driven vision flow wants it - out of scope for the Gemini migration.
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["look_camera", "scan_room", "query_memory"]


class MockVisionHandler(ToolHandler):
    name = "vision"
    default_timeout_s = 15.0  # a real Gemini vision call can be slow - see luno/vision.py
    max_timeout_s = 60.0

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        return super().validate(tool_call)

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action

        if action == "look_camera":
            question = tool_call.parameters.get("question", "What's visible right now?")
            return ToolResult.ok(self.name, action, "[MOCK] Placeholder camera description",
                                  data={"question": question, "description": "(no camera integration yet)"})

        if action == "scan_room":
            return ToolResult.ok(self.name, action, "[MOCK] Placeholder room scan",
                                  data={"objects": [], "humans": [], "note": "no camera integration yet"})

        if action == "query_memory":
            label = tool_call.target or tool_call.parameters.get("label")
            return ToolResult.ok(self.name, action, f"[MOCK] Placeholder memory lookup for '{label}'",
                                  data={"label": label, "location": None, "note": "no Vision Memory integration yet"})

        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
