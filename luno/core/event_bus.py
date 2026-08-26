"""
event_bus.py
============

The Event Bus - the ONLY channel subsystems talk through. Nothing in
this codebase should ever call another subsystem's method directly;
everything is `publish()`/`subscribe()`. See `coordinator.py` for how
that gets turned into actual module notifications.

Design summary
--------------
`publish()` never blocks: it does a `queue.put_nowait()` and returns.
A single background "pump" thread drains that queue and delivers each
event to matching subscribers, in descending subscriber-priority order.

Sync vs async subscribers is a delivery-concurrency choice, not an
asyncio/threading distinction:
  - a *sync* subscriber is called inline, on the pump thread, blocking
    the delivery of the CURRENT event to subsequent sync subscribers
    until it returns (but never blocking the original publisher).
  - an *async* subscriber is submitted to an injected `Dispatcher`
    (see `dispatcher.py`) and runs concurrently with everything else -
    if no dispatcher was wired in, it degrades to running inline
    instead of silently doing nothing.
A handler that happens to be an `async def` coroutine function is also
supported directly (run via `asyncio.run()` on whichever thread would
otherwise have called it) - a convenience, not a requirement; nothing
here assumes the caller's code is asyncio-based, matching every other
package built this session (all thread-based, not asyncio-based).

Wildcards use `fnmatch` (`"*"`, `"tool_*"`, `"vision_*"`, ...) - cheap,
stdlib-only, and covers everything from "subscribe to literally
everything" to "subscribe to a family of related event types" without
inventing a bespoke pattern language.

Degraded subscriber handling (self-healing): a subscriber that raises
`MAX_CONSECUTIVE_FAILURES` times in a row is assumed to be hitting a
*transient* problem (an API timeout, Home Assistant briefly offline,
...) rather than being permanently gone. Instead of unsubscribing it
forever - which used to make a module go silently deaf with no way
back and no visible trace outside the log - it is marked `degraded`
and throttled with exponential backoff: further deliveries to it are
skipped (not queued, not retried individually) until its backoff
window elapses, at which point the *next* matching event is used as
the retry attempt. One success resets it straight back to healthy.
`subscriber_degraded`/`subscriber_recovered` events are published on
each transition so other subsystems (Health Monitor, dashboard, ...)
can see and react to it, and `degraded_subscribers()` exposes the same
info for polling-style health checks. A subscriber is only ever
removed by an explicit `unsubscribe()` call (or `once=True` firing) -
never automatically - so "one broken subscriber can't spam the log
forever or silently eat CPU" is now achieved via backoff throttling
instead of deletion.
"""

from __future__ import annotations

import asyncio
import fnmatch
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .events import Event
from .utils import generate_id, log

MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_MAX_QUEUE = 20000

#: Backoff schedule for a degraded subscriber - starts short (a handler
#: hitting a blip should recover within a few seconds), doubles on each
#: further failure, capped so a permanently-broken handler still only
#: costs one skipped delivery per minute rather than spamming forever.
DEGRADED_INITIAL_BACKOFF_S = 1.0
DEGRADED_MAX_BACKOFF_S = 60.0
DEGRADED_BACKOFF_MULTIPLIER = 2.0

Handler = Callable[[Event], None]


@dataclass
class Subscription:
    subscription_id: str
    pattern: str
    handler: Handler
    priority: int = 0
    async_mode: bool = False
    once: bool = False
    failure_count: int = 0
    # -- self-healing / degraded-state bookkeeping (see module docstring) --
    degraded: bool = False
    backoff_s: float = 0.0
    next_retry_at: float = 0.0
    degraded_since: Optional[float] = None
    last_error: Optional[str] = None


class EventBus:
    def __init__(self, dispatcher: Optional[Any] = None, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self.dispatcher = dispatcher
        self._lock = threading.RLock()
        self._subscriptions: Dict[str, Subscription] = {}
        self._queue: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=max_queue)
        self._running = False
        self._pump_thread: Optional[threading.Thread] = None

        self._published_count = 0
        self._delivered_count = 0
        self._dropped_count = 0
        self._latencies_ms: "deque[float]" = deque(maxlen=200)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True, name="luno-core-eventbus")
        self._pump_thread.start()
        log("Event Bus started", "event_bus")

    def stop(self, wait: bool = False) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put_nowait(None)  # unblocks a pending queue.get()
        except queue.Full:
            pass
        if wait and self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)
        log("Event Bus stopped", "event_bus")

    # -- subscriptions ------------------------------------------------------

    def subscribe(
        self, pattern: str, handler: Handler, priority: int = 0,
        async_mode: bool = False, once: bool = False,
    ) -> str:
        """`pattern` is matched against `event.type` with `fnmatch`
        (`"*"` = everything). Returns a subscription id for
        `unsubscribe()`."""
        sub_id = generate_id("sub")
        with self._lock:
            self._subscriptions[sub_id] = Subscription(
                subscription_id=sub_id, pattern=pattern, handler=handler,
                priority=priority, async_mode=async_mode, once=once,
            )
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            return self._subscriptions.pop(subscription_id, None) is not None

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    # -- publishing -----------------------------------------------------------

    def publish(self, event: Event) -> None:
        """Never blocks. Under extreme, sustained backpressure (delivery
        can't keep up with publish volume) the internal queue can fill up;
        rather than block the publisher (which would violate the "never
        block publishers" requirement), the event is dropped and counted
        in `stats()['dropped']` - a full queue is a health/capacity signal
        for `health.py` to surface, not something `publish()` should ever
        wait around for."""
        if not isinstance(event, Event):
            raise TypeError(f"publish() expects an Event, got {type(event)!r}")
        try:
            self._queue.put_nowait(event)
            with self._lock:
                self._published_count += 1
        except queue.Full:
            with self._lock:
                self._dropped_count += 1
            log(f"queue full - dropped event '{event.type}' ({event.event_id})", "event_bus")

    # -- delivery loop --------------------------------------------------------

    def _pump_loop(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                continue
            self._deliver(event)

    def _deliver(self, event: Event) -> None:
        start = time.time()
        with self._lock:
            matches = [s for s in self._subscriptions.values() if fnmatch.fnmatch(event.type, s.pattern)]
        matches.sort(key=lambda s: -s.priority)

        once_ids: List[str] = []
        for sub in matches:
            self._invoke(sub, event)
            if sub.once:
                once_ids.append(sub.subscription_id)
        for sid in once_ids:
            self.unsubscribe(sid)

        with self._lock:
            self._delivered_count += 1
            self._latencies_ms.append((time.time() - start) * 1000.0)

    def _invoke(self, sub: Subscription, event: Event) -> None:
        if sub.degraded:
            with self._lock:
                still_waiting = time.time() < sub.next_retry_at
            if still_waiting:
                # Backoff window hasn't elapsed yet - skip this delivery
                # rather than hammering a handler that's currently failing.
                # The next matching event after the window elapses becomes
                # the retry attempt (see module docstring).
                return
        if sub.async_mode:
            if self.dispatcher is not None:
                self.dispatcher.submit(self._run_handler, sub, event)
                return
            # No dispatcher wired in - degrade to inline rather than
            # silently dropping "async" delivery.
        self._run_handler(sub, event)

    def _run_handler(self, sub: Subscription, event: Event) -> None:
        try:
            if asyncio.iscoroutinefunction(sub.handler):
                asyncio.run(sub.handler(event))
            else:
                sub.handler(event)
            self._on_handler_success(sub)
        except Exception as ex:
            self._on_handler_failure(sub, event, ex)

    def _on_handler_success(self, sub: Subscription) -> None:
        with self._lock:
            was_degraded = sub.degraded
            sub.failure_count = 0
            sub.degraded = False
            sub.backoff_s = 0.0
            sub.next_retry_at = 0.0
            sub.degraded_since = None
            sub.last_error = None
        if was_degraded:
            log(f"subscriber {sub.subscription_id} ('{sub.pattern}') recovered", "event_bus")
            self.publish(Event(
                type="subscriber_recovered",
                source="event_bus",
                data={"subscription_id": sub.subscription_id, "pattern": sub.pattern},
            ))

    def _on_handler_failure(self, sub: Subscription, event: Event, ex: Exception) -> None:
        with self._lock:
            sub.failure_count += 1
            sub.last_error = str(ex)
            entering_degraded = sub.failure_count >= MAX_CONSECUTIVE_FAILURES and not sub.degraded
            if entering_degraded:
                sub.degraded = True
                sub.degraded_since = time.time()
                sub.backoff_s = DEGRADED_INITIAL_BACKOFF_S
            elif sub.degraded:
                # Already degraded and the retry attempt (the delivery that
                # was allowed through once the previous backoff elapsed)
                # failed again - back off further instead of retrying
                # every single event.
                sub.backoff_s = min(DEGRADED_MAX_BACKOFF_S, sub.backoff_s * DEGRADED_BACKOFF_MULTIPLIER)
            if sub.degraded:
                sub.next_retry_at = time.time() + sub.backoff_s
            backoff_s = sub.backoff_s

        log(f"subscriber {sub.subscription_id} ('{sub.pattern}') raised on '{event.type}': {ex}", "event_bus")
        if entering_degraded:
            log(
                f"subscriber {sub.subscription_id} ('{sub.pattern}') degraded after "
                f"{MAX_CONSECUTIVE_FAILURES} consecutive failures - throttling deliveries "
                f"(next retry in {backoff_s:.0f}s) instead of unsubscribing; it will resume "
                f"automatically once its handler stops raising",
                "event_bus",
            )
            self.publish(Event(
                type="subscriber_degraded",
                source="event_bus",
                data={
                    "subscription_id": sub.subscription_id,
                    "pattern": sub.pattern,
                    "consecutive_failures": sub.failure_count,
                    "error": str(ex),
                    "retry_in_s": backoff_s,
                },
            ))

    # -- health integration --------------------------------------------------

    def degraded_subscribers(self) -> List[Dict[str, Any]]:
        """Snapshot of every currently-degraded subscriber, for
        `HealthMonitor`/dashboards to surface - see module docstring."""
        with self._lock:
            return [
                {
                    "subscription_id": s.subscription_id,
                    "pattern": s.pattern,
                    "consecutive_failures": s.failure_count,
                    "degraded_since": s.degraded_since,
                    "next_retry_at": s.next_retry_at,
                    "last_error": s.last_error,
                }
                for s in self._subscriptions.values() if s.degraded
            ]

    # -- introspection --------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            avg_latency = (sum(self._latencies_ms) / len(self._latencies_ms)) if self._latencies_ms else 0.0
            degraded_count = sum(1 for s in self._subscriptions.values() if s.degraded)
            return {
                "published": self._published_count,
                "delivered": self._delivered_count,
                "dropped": self._dropped_count,
                "queue_size": self._queue.qsize(),
                "avg_latency_ms": avg_latency,
                "subscriber_count": len(self._subscriptions),
                "degraded_subscriber_count": degraded_count,
            }
