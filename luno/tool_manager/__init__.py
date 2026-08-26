"""
Tool Manager
============

The universal execution layer for every capability Luno can act on.
Receives generic `ToolCall`s (produced by `luno.planner`, but this
package never imports it - see below), finds the right handler, runs it
safely (timeout, retry, cancellation), and always returns a structured
`ToolResult`. Never generates language, never calls an LLM, never plans -
it only executes.

    Behavior Tree -> Planner -> Execution Queue -> Tool Manager ->
    Registry -> Handlers -> ToolResult -> Planner -> OpenRouter ->
    Fish Audio -> Unity

INDEPENDENCE FROM THE PLANNER: this package does not import anything from
`luno.planner` (or `luno.behavior_tree`, or `luno.vision_memory`, or
`luno.ha_client`, ...). It only knows the generic shape:

    {"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light", "parameters": {}}

`models.ToolCall.from_any()` accepts that shape as a dict OR as any
duck-typed object with the right attributes - including a
`luno.planner.ToolCall` instance directly, with zero coupling in either
direction. See `models.py`'s module docstring for the full reasoning.

Quick start (standalone - no hardware/API needed)
----------------------------------------------------
    from luno.tool_manager import ToolManager
    from luno.tool_manager.builtin import register_all

    manager = ToolManager()
    register_all(manager.registry)  # registers every mock handler

    result = manager.execute({"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"})
    print(result.to_dict())

    # non-blocking:
    handle = manager.execute_async({"tool": "spotify", "action": "play", "target": "lofi"})
    ...
    result = handle.result()

Swapping a mock for a real implementation later
----------------------------------------------------
    from luno.tool_manager.handler import ToolHandler

    class RealHomeAssistantHandler(ToolHandler):
        name = "home_assistant"
        def supported_actions(self): return ["turn_on", "turn_off", "toggle", "run_script", "set_temperature"]
        def execute(self, tool_call, context=None):
            # call luno.ha_client.HomeAssistantClient here for real
            ...

    manager.registry.register("home_assistant", RealHomeAssistantHandler())

Nothing else changes - not the Planner, not any other handler, not the
Tool Manager itself. This is the entire point of the `ToolHandler`
interface (see `handler.py`).

Architecture (see each file's own docstring for the full story)
-------------------------------------------------------------------
    models.py        ToolCall (+ from_any() Planner-independence adapter), RetryPolicy
    result.py          ToolResult - the spec's exact output shape
    exceptions.py         every exception this package raises on purpose
    context.py                ExecutionContext - optional situational awareness
    handler.py                   ToolHandler - the abstract interface every handler implements
    registry.py                     thread-safe name -> handler map
    manager.py                         ToolManager - the execution engine (sync/async/timeout/retry/cancel)
    utils.py                              id/time/logging helpers
    builtin/                                 mock handlers for every tool the spec lists, + DummyHandler for tests
"""

from .context import ExecutionContext
from .exceptions import (
    HandlerCrashError,
    ToolCancelledError,
    ToolManagerError,
    ToolTimeoutError,
    ToolValidationError,
    UnknownActionError,
    UnknownToolError,
)
from .handler import ToolHandler
from .manager import ExecutionHandle, ToolManager
from .models import ExecutionStatus, RetryPolicy, ToolCall
from .registry import ToolRegistry
from .result import ResultStatus, ToolResult

__all__ = [
    "ToolManager", "ExecutionHandle",
    "ToolRegistry",
    "ToolHandler",
    "ToolCall", "RetryPolicy", "ExecutionStatus",
    "ToolResult", "ResultStatus",
    "ExecutionContext",
    "ToolManagerError", "UnknownToolError", "UnknownActionError", "ToolValidationError",
    "ToolTimeoutError", "ToolCancelledError", "HandlerCrashError",
]
