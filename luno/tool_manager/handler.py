"""
handler.py
==========

`ToolHandler` - the abstract base every tool implementation (real or
mock) must subclass. Handlers are meant to be INTERCHANGEABLE: `manager.
ToolManager` only ever calls `validate()`/`supported_actions()`/
`execute()` through this interface, never anything handler-specific -
which is exactly what lets a mock (`builtin/home_assistant.py`) be
replaced by a real implementation later with zero changes anywhere else
(see the package docstring in `__init__.py`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .context import ExecutionContext
from .models import ToolCall
from .result import ToolResult

#: Fallback timeout for a handler that doesn't override `default_timeout_s`.
DEFAULT_TIMEOUT_S = 10.0
#: Fallback ceiling for a handler that doesn't override `max_timeout_s`.
DEFAULT_MAX_TIMEOUT_S = 60.0


class ToolHandler(ABC):
    """Subclasses set `name` to the tool name they serve (e.g.
    `"home_assistant"`) - this is what `registry.register()` keys on by
    convention, though the registry itself doesn't inspect `.name` (the
    name passed to `register()` is the source of truth; keeping `.name`
    in sync is just good practice for logging/debugging)."""

    name: str = ""
    default_timeout_s: float = DEFAULT_TIMEOUT_S
    max_timeout_s: float = DEFAULT_MAX_TIMEOUT_S

    @abstractmethod
    def supported_actions(self) -> List[str]:
        """The closed set of `action` values this handler accepts."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        """Do the actual work (or, for a mock, fake it) and return a
        `ToolResult` (a plain dict shaped like one is also accepted - see
        `result.ToolResult.coerce`, which `manager.py` applies to
        whatever this returns). May raise - `manager.py` catches any
        exception and reports it as a `handler_crash` `ToolResult` rather
        than propagating it, so a handler is free to let exceptions
        happen naturally instead of wrapping everything in try/except
        itself."""
        raise NotImplementedError

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        """Return `None` if `tool_call` is valid for this handler, else a
        human-readable error message. The default implementation only
        checks the action is recognized - override to add parameter-level
        checks (e.g. a required `target`), calling
        `super().validate(tool_call)` first is usually the right pattern."""
        if tool_call.action not in self.supported_actions():
            supported = ", ".join(self.supported_actions())
            return f"Action '{tool_call.action}' is not supported by '{self.name}' (supported: {supported})"
        return None
