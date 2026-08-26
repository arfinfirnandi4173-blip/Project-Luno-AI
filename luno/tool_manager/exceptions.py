"""
exceptions.py
=============

Every exception this package raises ON PURPOSE. In practice
`manager.ToolManager` catches all of these itself and converts them into
a structured `ToolResult` (see the spec's "Never crash the Tool Manager"
rule) - they exist as named types mainly so internal code has precise
things to raise/catch, and so a caller reaching into lower-level pieces
directly (e.g. calling a handler's `validate()` themselves) still gets a
meaningful exception type instead of a bare `Exception`.
"""

from __future__ import annotations


class ToolManagerError(Exception):
    """Base class for every exception this package raises intentionally."""


class UnknownToolError(ToolManagerError):
    """No handler is registered for the requested `ToolCall.tool`."""


class UnknownActionError(ToolManagerError):
    """The handler exists, but doesn't support the requested action."""


class ToolValidationError(ToolManagerError):
    """`ToolHandler.validate()` rejected the call for a reason other than
    an unrecognized action (e.g. a missing/malformed parameter)."""


class ToolTimeoutError(ToolManagerError):
    """A handler did not finish within its allotted timeout."""


class ToolCancelledError(ToolManagerError):
    """Execution was cancelled via `ToolManager.cancel()` before it
    produced a result."""


class HandlerCrashError(ToolManagerError):
    """A handler raised an unexpected exception, or returned something
    that isn't a `ToolResult` (or a dict shaped like one)."""
