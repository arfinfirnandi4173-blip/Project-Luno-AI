"""
manager.py
==========

`ToolManager` - the spec's central execution engine. Looks up the right
handler in a `ToolRegistry`, validates the call, runs it with an
enforced timeout, retries transient failures if asked, supports
cancellation and progress callbacks, and ALWAYS returns a structured
`ToolResult` - it never raises out of `execute()`/`execute_async()` for
anything a caller did (unknown tool, bad params, a crashing handler,
timeout) - see the spec's "Never crash the Tool Manager" rule.

Honest limitation on timeouts and cancellation: Python cannot safely
force-kill a running thread. A handler's `execute()` call is dispatched
onto this manager's own thread pool specifically so a WAIT can be time-
bounded (`Future.result(timeout=...)`) - if that wait expires, this
returns a TIMEOUT `ToolResult` and stops waiting, but the underlying
call keeps running in the background until it naturally finishes; its
eventual result is simply discarded. Cancellation is the same story:
`cancel()` sets a cooperative flag that's checked before each attempt and
during retry backoff (both of which stop IMMEDIATELY), but a handler call
already in flight when `cancel()` is called is not interrupted - once it
finishes, cancellation still wins as the FINAL reported outcome (see
`_run_with_retries`), it just can't stop that one call's side effects
from having already happened. This is the exact same "cooperative, not
preemptive" tradeoff already documented in `behavior_tree/actions.py` and
`planner/scheduler.py` - a real Home Assistant/Windows/browser handler
should design its own operations to be safely abandon-able where
possible (e.g. short HTTP calls with their own timeouts), rather than
relying on this layer to interrupt them.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .context import ExecutionContext
from .models import ExecutionStatus, RetryPolicy, ToolCall
from .registry import ToolRegistry
from .result import ResultStatus, ToolResult
from .utils import generate_id, log

DEFAULT_MAX_WORKERS = 16

#: `(execution_id, phase, attempt) -> None` - see `execute_async()`.
ProgressCallback = Callable[[str, str, int], None]


@dataclass
class ExecutionHandle:
    """Returned by `execute_async()` - a lightweight, thread-safe handle
    for waiting on or cancelling one in-flight execution. NOT the result
    itself - call `.result()` (blocks) or check `.done()`/inspect
    `.future` directly for non-blocking polling."""
    execution_id: str
    tool_call: ToolCall
    future: Future
    _cancel_event: threading.Event = field(repr=False)

    def result(self, timeout: Optional[float] = None) -> ToolResult:
        return self.future.result(timeout=timeout)

    def cancel(self) -> None:
        self._cancel_event.set()

    def done(self) -> bool:
        return self.future.done()


def _elapsed_ms(start: float) -> float:
    return (time.time() - start) * 1000.0


class ToolManager:
    """Owns TWO separate thread pools, not one - this matters. Each
    execution has an OUTER worker (`_run_with_retries`, mostly idle,
    blocked waiting on the current attempt) and, per attempt, an INNER
    worker (`_invoke_handler`, doing the actual work, bounded by a
    timeout). Sharing a single pool between those two roles is a
    self-starvation bug: under enough concurrent load, outer workers can
    fill every slot in the pool while each one waits on an inner task that
    can never get a slot to run on - every execution then "times out"
    despite the handler being perfectly healthy, purely because the pool
    ran out of room for the thing the timeout logic itself depends on
    (caught by this package's own stress test - see test_tool_manager.py).
    Two pools removes the shared bottleneck entirely: at most
    `max_workers` executions are ever "in flight" at the orchestration
    level, and each one is always guaranteed a handler-pool slot when it
    needs one."""

    def __init__(self, registry: Optional[ToolRegistry] = None, max_workers: int = DEFAULT_MAX_WORKERS,
                 max_handler_workers: Optional[int] = None) -> None:
        self.registry = registry if registry is not None else ToolRegistry()
        self._thread_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="luno-toolmgr")
        self._handler_pool = ThreadPoolExecutor(
            max_workers=max_handler_workers or max_workers, thread_name_prefix="luno-toolmgr-handler"
        )
        self._lock = threading.RLock()
        self._executions: Dict[str, ExecutionHandle] = {}

    # -- public API -----------------------------------------------------------

    def execute(
        self, tool_call: Any, context: Optional[ExecutionContext] = None,
        timeout_s: Optional[float] = None, retry_policy: Optional[RetryPolicy] = None,
    ) -> ToolResult:
        """Synchronous convenience wrapper - blocks the CALLER's thread
        until a result (or timeout) is ready. Still runs the handler on
        the shared thread pool internally, so it composes fine with
        `execute_async()` calls happening concurrently elsewhere; it's
        the WAITING that's synchronous here, not the execution itself.
        Prefer `execute_async()` from any context that must never block
        (e.g. the Behavior Tree's tick loop - see that package's
        `actions._dispatch()` for the equivalent pattern)."""
        handle = self.execute_async(tool_call, context=context, timeout_s=timeout_s, retry_policy=retry_policy)
        return handle.result()

    def execute_async(
        self, tool_call: Any, context: Optional[ExecutionContext] = None,
        timeout_s: Optional[float] = None, retry_policy: Optional[RetryPolicy] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ExecutionHandle:
        """Never blocks - dispatches onto the thread pool and returns an
        `ExecutionHandle` immediately."""
        call = ToolCall.from_any(tool_call)
        execution_id = generate_id("exec")
        cancel_event = threading.Event()
        policy = retry_policy if retry_policy is not None else RetryPolicy()

        future = self._thread_pool.submit(
            self._run_with_retries, execution_id, call, context, timeout_s, policy, cancel_event, on_progress
        )
        handle = ExecutionHandle(execution_id=execution_id, tool_call=call, future=future, _cancel_event=cancel_event)
        with self._lock:
            self._executions[execution_id] = handle
        log(f"Execution {execution_id} queued: {call.tool}.{call.action}")
        return handle

    def cancel(self, execution_id: str) -> bool:
        """Requests cancellation - see the module docstring for exactly
        what this can and can't guarantee. Returns False if
        `execution_id` isn't tracked (already finished, or never
        existed)."""
        with self._lock:
            handle = self._executions.get(execution_id)
        if handle is None:
            return False
        handle.cancel()
        log(f"Execution {execution_id} cancellation requested")
        return True

    def get_handle(self, execution_id: str) -> Optional[ExecutionHandle]:
        with self._lock:
            return self._executions.get(execution_id)

    def shutdown(self, wait: bool = False) -> None:
        self._thread_pool.shutdown(wait=wait)
        self._handler_pool.shutdown(wait=wait)

    # -- internals -----------------------------------------------------------

    def _run_with_retries(
        self, execution_id: str, call: ToolCall, context: Optional[ExecutionContext],
        timeout_s: Optional[float], policy: RetryPolicy, cancel_event: threading.Event,
        on_progress: Optional[ProgressCallback],
    ) -> ToolResult:
        attempt = 0
        while True:
            attempt += 1
            if cancel_event.is_set():
                return self._finish(execution_id, self._cancelled_result(call))

            self._report_progress(on_progress, execution_id, ExecutionStatus.RUNNING, attempt)
            result = self._run_single_attempt(call, context, timeout_s, execution_id)

            if cancel_event.is_set():
                # Cancelled WHILE the attempt was in flight - cancellation
                # always wins as the reported outcome, even if the
                # underlying call happened to succeed/fail around the
                # same time (see module docstring).
                return self._finish(execution_id, self._cancelled_result(call))

            if result.success or not result.retryable or attempt > policy.max_retries:
                return self._finish(execution_id, result)

            delay = policy.delay_for_attempt(attempt)
            log(f"Execution {execution_id} retryable failure (attempt {attempt}): {result.message} - retrying in {delay:.2f}s")
            self._report_progress(on_progress, execution_id, ExecutionStatus.RETRYING, attempt)
            self._interruptible_sleep(delay, cancel_event)

    def _run_single_attempt(
        self, call: ToolCall, context: Optional[ExecutionContext], timeout_s: Optional[float], execution_id: str,
    ) -> ToolResult:
        start = time.time()

        handler = self.registry.get(call.tool)
        if handler is None:
            log(f"Execution {execution_id}: unknown tool '{call.tool}'")
            return ToolResult.fail(call.tool, call.action, f"Unknown tool '{call.tool}'", error_type="unknown_tool")

        error = handler.validate(call)
        if error is not None:
            error_type = "unknown_action" if call.action not in handler.supported_actions() else "validation_error"
            log(f"Execution {execution_id}: validation failed ({error_type}): {error}")
            return ToolResult.fail(call.tool, call.action, error, error_type=error_type)

        effective_timeout = self._resolve_timeout(handler, timeout_s)
        inner_future = self._handler_pool.submit(self._invoke_handler, handler, call, context)
        try:
            result = inner_future.result(timeout=effective_timeout)
        except FuturesTimeoutError:
            log(f"Execution {execution_id}: timed out after {effective_timeout}s")
            return ToolResult.fail(
                call.tool, call.action, f"Timed out after {effective_timeout}s",
                error_type="timeout", status=ResultStatus.TIMEOUT, retryable=True,
                execution_time_ms=_elapsed_ms(start),
            )
        except Exception as ex:
            log(f"Execution {execution_id}: handler crashed: {ex}")
            return ToolResult.fail(
                call.tool, call.action, f"Handler crashed: {ex}",
                error_type="handler_crash", retryable=True, execution_time_ms=_elapsed_ms(start),
            )

        result.execution_time_ms = _elapsed_ms(start)
        return result

    @staticmethod
    def _invoke_handler(handler, call: ToolCall, context: Optional[ExecutionContext]) -> ToolResult:
        raw = handler.execute(call, context)
        return ToolResult.coerce(raw, call.tool, call.action)

    @staticmethod
    def _resolve_timeout(handler, requested: Optional[float]) -> float:
        if requested is None:
            return handler.default_timeout_s
        return min(requested, handler.max_timeout_s)

    @staticmethod
    def _cancelled_result(call: ToolCall) -> ToolResult:
        return ToolResult.fail(call.tool, call.action, "Execution cancelled", error_type="cancelled",
                                status=ResultStatus.CANCELLED, retryable=False)

    @staticmethod
    def _interruptible_sleep(delay: float, cancel_event: threading.Event) -> None:
        """Sleeps in small slices instead of one `time.sleep(delay)` call,
        so a cancellation requested during retry backoff takes effect
        almost immediately instead of waiting out the full delay."""
        slept = 0.0
        step = 0.05
        while slept < delay and not cancel_event.is_set():
            time.sleep(min(step, delay - slept))
            slept += step

    @staticmethod
    def _report_progress(callback: Optional[ProgressCallback], execution_id: str,
                          status: ExecutionStatus, attempt: int) -> None:
        if callback is None:
            return
        try:
            callback(execution_id, status.value, attempt)
        except Exception as ex:
            log(f"Execution {execution_id}: on_progress callback raised (ignored): {ex}")

    def _finish(self, execution_id: str, result: ToolResult) -> ToolResult:
        with self._lock:
            self._executions.pop(execution_id, None)
        tag = "OK" if result.success else result.status.value.upper()
        log(f"Execution {execution_id} finished [{tag}] {result.tool}.{result.action} "
            f"({result.execution_time_ms:.1f}ms): {result.message}")
        return result
