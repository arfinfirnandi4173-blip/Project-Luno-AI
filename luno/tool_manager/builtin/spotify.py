"""
spotify.py
==========

MOCK Spotify handler - "Mock implementation." A real handler would call
the Spotify Web API (or a local control surface) inside `execute()`.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["play", "pause", "next", "previous", "volume"]


class MockSpotifyHandler(ToolHandler):
    name = "spotify"
    default_timeout_s = 5.0
    max_timeout_s = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playing = False
        self._volume = 50

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action == "volume" and not (0 <= tool_call.parameters.get("level", -1) <= 100):
            return "Action 'volume' requires parameters.level between 0 and 100"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        with self._lock:
            if action == "play":
                self._playing = True
                track = tool_call.target or tool_call.parameters.get("query")
                message = f"[MOCK] Playing '{track}'" if track else "[MOCK] Playback resumed"
                return ToolResult.ok(self.name, action, message, data={"playing": True, "track": track})

            if action == "pause":
                self._playing = False
                return ToolResult.ok(self.name, action, "[MOCK] Playback paused", data={"playing": False})

            if action == "next":
                return ToolResult.ok(self.name, action, "[MOCK] Skipped to next track", data={"playing": self._playing})

            if action == "previous":
                return ToolResult.ok(self.name, action, "[MOCK] Skipped to previous track", data={"playing": self._playing})

            if action == "volume":
                self._volume = tool_call.parameters["level"]
                return ToolResult.ok(self.name, action, f"[MOCK] Volume set to {self._volume}", data={"volume": self._volume})

        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
