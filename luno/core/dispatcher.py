"""
dispatcher.py
=============

`Dispatcher` - the shared background-execution engine for the rest of
Core. Everything that must not block its caller (the Event Bus's async
subscribers, the Scheduler's due jobs, Runtime's own background work)
goes through here instead of spawning its own ad-hoc thread.

Supports, per the spec:
  - background execution      `submit()`            -> Future, runs ASAP
  - priority queues            `submit_priority()`    -> Future, priority-ordered
  - delayed execution           `submit_delayed()`      -> Future, runs after N seconds
  - scheduled execution           (built on top of this - see `scheduler.py`)

"Dispatch should never block Runtime": every `submit*` method returns
immediately with a `concurrent.futures.Future`; nothing here ever waits
for a task to finish unless the CALLER chooses to call `.result()` on
what came back.
"""

from __future__ import annotations

import itertools
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .utils import log

DEFAULT_MAX_WORKERS = 8


class Dispatcher:
    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="luno-core-dispatch")
        self._priority_queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = itertools.count()
        self._running = False
        self._priority_thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._priority_thread = threading.Thread(
            target=self._priority_loop, daemon=True, name="luno-core-dispatch-priority"
        )
        self._priority_thread.start()
        log("Dispatcher started", "dispatcher")

    def stop(self, wait: bool = False) -> None:
        self._running = False
        self._executor.shutdown(wait=wait)
        log("Dispatcher stopped", "dispatcher")

    # -- public API -------------------------------------------------------

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        """Fire onto the thread pool now, no ordering guarantees relative
        to other `submit()` calls."""
        return self._executor.submit(fn, *args, **kwargs)

    def submit_priority(self, fn: Callable, *args: Any, priority: int = 0, **kwargs: Any) -> Future:
        """Queued, priority-ordered: lower `priority` number runs first
        (same convention as Behavior Tree's node priorities). Requires
        `start()` to have been called - the priority queue is drained by
        a dedicated thread, not the pool itself, so ordering survives
        pool contention."""
        future: Future = Future()
        seq = next(self._seq)
        self._priority_queue.put((priority, seq, fn, args, kwargs, future))
        return future

    def submit_delayed(self, fn: Callable, delay_s: float, *args: Any, **kwargs: Any) -> Future:
        """Runs after `delay_s` seconds, via a non-blocking
        `threading.Timer` (same backoff primitive used by
        `planner/executor.py` and `tool_manager`'s retry backoff)."""
        future: Future = Future()

        def _fire() -> None:
            self._executor.submit(self._run_future, fn, args, kwargs, future)

        timer = threading.Timer(max(0.0, delay_s), _fire)
        timer.daemon = True
        timer.start()
        return future

    # -- internals --------------------------------------------------------

    def _priority_loop(self) -> None:
        while self._running:
            try:
                priority, seq, fn, args, kwargs, future = self._priority_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._executor.submit(self._run_future, fn, args, kwargs, future)

    @staticmethod
    def _run_future(fn: Callable, args: tuple, kwargs: dict, future: Future) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as ex:  # noqa: BLE001 - deliberately broad, see module docstring
            future.set_exception(ex)
