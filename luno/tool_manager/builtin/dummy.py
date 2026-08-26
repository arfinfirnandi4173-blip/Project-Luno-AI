"""
dummy.py
========

`DummyHandler` - a generic, fully-controllable handler for tests (see
`luno/tool_manager/tests/test_tool_manager.py`, which uses this heavily
per the spec's instruction). Not a mock of any real tool - it exists
purely to make `manager.ToolManager`'s behavior (timeouts, retries,
cancellation, delays, failures) exercisable deterministically without
touching any of the other builtin handlers.

Controlled entirely through `ToolCall.parameters`:

    mode: "success" (default) | "failure" | "timeout" | "retry"
    delay_s: float - sleep this long before responding (any mode)
    message: str - custom message for "failure" mode
    retryable: bool - whether a "failure" mode result claims to be retryable
    succeed_on_attempt: int - for "retry" mode, which attempt number succeeds
    _attempt_counter: list[int] - a single-element mutable list a test
        passes in and this handler increments in place, so "retry" mode
        can tell how many times IT PERSONALLY has been invoked across
        retries (can't use `ToolCall.parameters` itself for this since a
        fresh dict isn't guaranteed shared across retries otherwise -
        passing the SAME list object is what makes it work)
"""

from __future__ import annotations

import time
from typing import List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["simulate"]


class DummyHandler(ToolHandler):
    name = "dummy"
    default_timeout_s = 5.0
    max_timeout_s = 300.0  # generous - tests may deliberately request long timeouts to prove they're respected

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        params = tool_call.parameters
        mode = params.get("mode", "success")
        delay_s = params.get("delay_s", 0.0)

        if mode == "timeout":
            # Deliberately hangs far longer than any reasonable test
            # timeout - `ToolManager`'s own timeout enforcement is what's
            # actually being tested here, not this sleep.
            time.sleep(params.get("hang_s", 999.0))
            return ToolResult.ok(self.name, tool_call.action, "should never get here")

        if delay_s:
            time.sleep(delay_s)

        if mode == "success":
            return ToolResult.ok(self.name, tool_call.action, params.get("message", "dummy success"), data=dict(params))

        if mode == "failure":
            return ToolResult.fail(self.name, tool_call.action, params.get("message", "dummy failure"),
                                    error_type="dummy_failure", retryable=bool(params.get("retryable", False)))

        if mode == "retry":
            counter = params.get("_attempt_counter")
            succeed_on = params.get("succeed_on_attempt", 3)
            if counter is not None:
                counter[0] += 1
                if counter[0] < succeed_on:
                    return ToolResult.fail(self.name, tool_call.action,
                                            f"simulated transient failure (attempt {counter[0]})",
                                            error_type="dummy_transient", retryable=True)
                return ToolResult.ok(self.name, tool_call.action, f"succeeded on attempt {counter[0]}")
            return ToolResult.fail(self.name, tool_call.action,
                                    "mode='retry' requires parameters._attempt_counter (a shared [int] list)",
                                    error_type="dummy_misconfigured", retryable=False)

        return ToolResult.fail(self.name, tool_call.action, f"Unknown dummy mode '{mode}'", error_type="dummy_misconfigured")
