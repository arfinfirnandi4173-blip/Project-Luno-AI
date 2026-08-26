"""
real_windows.py
================

`RealWindowsHandler` - the real counterpart to `windows.py`'s
`MockWindowsHandler`, same swap-in shape every other real handler in
this package uses (see `real_home_assistant.py`'s own docstring for the
convention this mirrors).

Only `open_app`/`launch_app` are backed by something real here -
`luno/desktop_control.py`'s `open_app()`, an ALLOWLIST-ONLY launcher
(only apps explicitly named in `config/apps.json` can ever be started -
see that module's own docstring: "GPT tidak pernah diberi akses buat
menjalankan path/executable sembarangan dari luar daftar ini"). Every
other action `MockWindowsHandler` simulates (`close_app`, `shutdown`,
`restart`, `sleep`, `set_volume`, `set_brightness`) has no real
implementation anywhere in this codebase - rather than silently keep
faking success for those (this file's whole reason to exist is
stopping Luno from CLAIMING to have done something it didn't), they
fail with a clear, honest "not implemented" message instead.

Two action names map to the same behavior: `open_app` is what
`luno.planner.parser.IntentParser` actually emits for "open X" -
`launch_app` is only kept as an alias because `MockWindowsHandler`'s
own `_SUPPORTED_ACTIONS` declares that name (a pre-existing mismatch
between the mock and the parser, not introduced here - accepting both
means this handler works correctly regardless of which name a caller
uses).
"""

from __future__ import annotations

from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult
from .windows import _SUPPORTED_ACTIONS

_OPEN_ACTIONS = ("open_app", "launch_app")


class RealWindowsHandler(ToolHandler):
    name = "windows"
    default_timeout_s = 10.0
    max_timeout_s = 20.0

    def supported_actions(self) -> List[str]:
        # "open_app" added alongside the mock's own declared set so
        # validate() accepts what the Planner's parser actually sends.
        return list(dict.fromkeys([*_SUPPORTED_ACTIONS, "open_app"]))

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action in _OPEN_ACTIONS and not tool_call.target:
            return f"Action '{tool_call.action}' requires a 'target' (app name)"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        target = tool_call.target or ""

        if action in _OPEN_ACTIONS:
            from luno.desktop_control import open_app
            ok, message = open_app(target)
            if ok:
                return ToolResult.ok(self.name, action, message, data={"app": target})
            return ToolResult.fail(self.name, action, message, error_type="AppNotFound")

        # Everything else MockWindowsHandler simulates has no real
        # implementation anywhere in this codebase - fail honestly
        # rather than silently keep pretending it worked.
        return ToolResult.fail(
            self.name, action,
            f"'{action}' is not implemented for real Windows control yet (only opening allowlisted "
            "apps from config/apps.json is real today)",
            error_type="NotImplemented",
        )
