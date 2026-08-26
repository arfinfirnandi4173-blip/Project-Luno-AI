"""
browser.py
==========

MOCK browser-control handler. Fakes every action `real_browser.py`'s
`RealBrowserHandler` really implements, so the Tool Manager/Planner
pipeline (and everything that assembles `ToolResult`s into system-prompt
notes) can be exercised end-to-end with zero external dependency -
same "mock first, real handler swaps in later with zero Planner/Tool
Manager changes" precedent as `MockCameraPTZHandler`/`MockHomeAssistant
Handler`.

`open`/`navigate`/`search`/`refresh`/`close` are the ORIGINAL action set
(kept byte-for-byte backward compatible - existing callers/tests still
work unchanged); `read`/`screenshot`/`click`/`type`/`scroll`/`download`/
`inspect` are additive.
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = [
    "open", "navigate", "search", "refresh", "close",
    "read", "screenshot", "click", "type", "scroll", "download", "inspect",
]


class MockBrowserHandler(ToolHandler):
    name = "browser"
    default_timeout_s = 10.0
    max_timeout_s = 30.0

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action == "navigate" and not tool_call.target:
            return "Action 'navigate' requires a 'target' (URL or site name)"
        if tool_call.action == "search" and not tool_call.parameters.get("query") and not tool_call.target:
            return "Action 'search' requires a 'target' or parameters.query"
        if tool_call.action in ("click", "type") and not tool_call.target and not tool_call.parameters.get("selector") and not tool_call.parameters.get("coordinates"):
            return f"Action '{tool_call.action}' requires a 'target'/parameters.selector or parameters.coordinates"
        if tool_call.action == "type" and not tool_call.parameters.get("text"):
            return "Action 'type' requires parameters.text"
        if tool_call.action == "download" and not (tool_call.target or tool_call.parameters.get("url")):
            return "Action 'download' requires a 'target'/parameters.url"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        target = tool_call.target or ""

        if action == "open":
            return ToolResult.ok(self.name, action, "[MOCK] Browser opened", data={})
        if action == "navigate":
            return ToolResult.ok(self.name, action, f"[MOCK] Navigated to '{target}'", data={"url": target})
        if action == "search":
            query = tool_call.parameters.get("query") or target
            return ToolResult.ok(
                self.name, action, f"[MOCK] Searched for '{query}'",
                data={"query": query, "results": [{"title": "Example result", "url": "https://example.com", "snippet": "[MOCK] snippet"}]},
            )
        if action == "refresh":
            return ToolResult.ok(self.name, action, "[MOCK] Page refreshed", data={})
        if action == "close":
            return ToolResult.ok(self.name, action, "[MOCK] Browser closed", data={})
        if action == "read":
            return ToolResult.ok(self.name, action, "[MOCK] Read page text", data={"text": "[MOCK] page text", "title": "[MOCK] Page Title", "url": target or "https://example.com"})
        if action == "screenshot":
            return ToolResult.ok(self.name, action, "[MOCK] Screenshot captured", data={"image_bytes": 0})
        if action == "click":
            return ToolResult.ok(self.name, action, f"[MOCK] Clicked '{target or tool_call.parameters.get('selector')}'", data={})
        if action == "type":
            return ToolResult.ok(self.name, action, f"[MOCK] Typed into '{target or tool_call.parameters.get('selector')}'", data={})
        if action == "scroll":
            return ToolResult.ok(self.name, action, f"[MOCK] Scrolled {tool_call.parameters.get('direction', 'down')}", data={})
        if action == "download":
            url = target or tool_call.parameters.get("url")
            return ToolResult.ok(self.name, action, f"[MOCK] Downloaded '{url}'", data={"url": url, "path": "[MOCK] /downloads/file"})
        if action == "inspect":
            return ToolResult.ok(self.name, action, "[MOCK] Inspected current page", data={"title": "[MOCK] Page Title", "url": target or "https://example.com"})

        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
