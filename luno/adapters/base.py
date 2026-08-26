"""
base.py
=======

`BaseAdapter` - the common template every adapter in this package
implements. It subclasses `luno.core.module_manager.Module` directly
(so every adapter IS a Core `Module` and can be registered into the
already-proven `ModuleManager`/`LifecycleManager`/`Coordinator`
machinery without Core needing to know "adapter" is a special concept -
see `manager.py`), and adds exactly the extra surface the spec asks
for: `restart()`, `publish()`, `status()`, plus `health()` tuned to
adapter-specific failure tracking.

Design: template method pattern. Concrete adapters override three small
hooks instead of the full lifecycle:

    _do_start()          - connect to / initialize the external system
    _do_stop()            - disconnect / release it
    handle_event(event)    - internal Event -> external API call (translation only)

`BaseAdapter` itself supplies, for free, exactly what the spec's
Logging section asks every adapter to have: structured start/stop/
restart logs, per-event execution-time logging, events-in/events-out
counters, and error/restart counts surfaced through `status()`.

Fault isolation: `on_event()` catches anything `handle_event()` raises,
publishes a `SystemError` event (never lets the exception propagate
into the Event Bus's delivery loop - Core's own Coordinator already
catches this too, this is defense in depth plus adapter-specific
bookkeeping), and tracks consecutive failures. After
`MAX_CONSECUTIVE_FAILURES` in a row, the adapter restarts ITSELF
(`stop()` + `start()`) rather than waiting for something external to
notice - "If one adapter crashes: restart it. Do not stop Runtime."
`AdapterManager`'s own health polling (see `manager.py`) is a second,
coarser safety net on top of this.

Non-blocking event handling: many adapters legitimately need to BLOCK
while `handle_event()` runs - Fish Audio's `play()` waits for playback
to finish, a real OpenRouter call waits on the network. If `on_event()`
ran `handle_event()` inline, one such adapter would stall the Event
Bus's single delivery ("pump") thread for its entire duration, delaying
delivery to every OTHER subscriber of that event queued behind it -
exactly the "event delivery must never block" failure mode `core`'s own
`EventBus` avoids for its own async subscribers. Rather than push every
route through `EventBus.subscribe(..., async_mode=True)` (which would
require every call site to remember to opt in, and complicates
`Coordinator`'s simpler routing table), `BaseAdapter` solves this once,
here: each adapter owns a dedicated single-worker thread pool, and
`on_event()` only ever submits to it and returns immediately. Events for
one adapter are still processed strictly in the order received (one
worker = FIFO), but other adapters - and the Event Bus's own delivery
loop - are never held up waiting for this one to finish.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from ..core.events import Event, SystemError
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from .utils import elapsed_ms, log, utcnow

DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


class BaseAdapter(Module):
    name: str = ""
    dependencies: list = []
    MAX_CONSECUTIVE_FAILURES: int = DEFAULT_MAX_CONSECUTIVE_FAILURES

    def __init__(self) -> None:
        self._event_bus = None
        self._lock = threading.RLock()
        self._started_at: Optional[Any] = None
        self._events_in = 0
        self._events_out = 0
        self._consecutive_failures = 0
        self._last_error: Optional[str] = None
        self._restart_count = 0
        self._last_event_at: Optional[Any] = None
        self._worker_pool: Optional[ThreadPoolExecutor] = None

    # -- wiring ---------------------------------------------------------------

    def bind(self, event_bus: Any) -> None:
        """Called once by `AdapterManager` at registration time - the
        only channel an adapter gets for publishing events. Adapters
        never reach into Runtime, into each other, or construct their
        own Event Bus reference."""
        self._event_bus = event_bus

    # -- Module / spec's required interface ------------------------------------

    def start(self) -> None:
        t0 = time.time()
        self._worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"luno-adapter-{self.name}")
        self._do_start()
        with self._lock:
            self._started_at = utcnow()
            self._consecutive_failures = 0
        log(f"'{self.name}' started ({elapsed_ms(t0):.1f}ms)", self.name)

    def stop(self) -> None:
        t0 = time.time()
        self._do_stop()
        pool, self._worker_pool = self._worker_pool, None
        if pool is not None:
            pool.shutdown(wait=False)
        log(f"'{self.name}' stopped ({elapsed_ms(t0):.1f}ms)", self.name)

    def restart(self) -> None:
        log(f"'{self.name}' restarting...", self.name)
        try:
            self._do_stop()
        except Exception as ex:
            log(f"'{self.name}' raised during restart-stop (continuing): {ex}", self.name)
        t0 = time.time()
        self._do_start()
        with self._lock:
            self._started_at = utcnow()
            self._consecutive_failures = 0
            self._restart_count += 1
            count = self._restart_count
        log(f"'{self.name}' restarted ({elapsed_ms(t0):.1f}ms, restart #{count})", self.name)

    def on_event(self, event: Event) -> None:
        """Never blocks the caller (the Event Bus's delivery thread, via
        `Coordinator`) - see the module docstring. Submits to this
        adapter's own single-worker pool and returns immediately; the
        actual translation happens in `_process_event()`."""
        with self._lock:
            self._events_in += 1
            self._last_event_at = utcnow()
        pool = self._worker_pool
        if pool is None:
            log(f"'{self.name}'.on_event('{event.type}') dropped - adapter is not started", self.name)
            return
        try:
            pool.submit(self._process_event, event)
        except RuntimeError:
            # pool was shut down between the None-check and submit() (a
            # stop() raced this call) - drop the event rather than crash.
            log(f"'{self.name}'.on_event('{event.type}') dropped - adapter stopped mid-dispatch", self.name)

    def _process_event(self, event: Event) -> None:
        t0 = time.time()
        try:
            self.handle_event(event)
            with self._lock:
                self._consecutive_failures = 0
            log(f"'{self.name}' handled '{event.type}' ({elapsed_ms(t0):.1f}ms)", self.name)
        except Exception as ex:
            with self._lock:
                self._consecutive_failures += 1
                self._last_error = str(ex)
                failures = self._consecutive_failures
            log(f"'{self.name}'.handle_event raised on '{event.type}' ({elapsed_ms(t0):.1f}ms): {ex}", self.name)
            self.publish(SystemError(data={"adapter": self.name, "event_type": event.type, "error": str(ex)}))
            if failures >= self.MAX_CONSECUTIVE_FAILURES:
                log(f"'{self.name}' hit {failures} consecutive failures - restarting itself", self.name)
                try:
                    self.restart()
                except Exception as restart_ex:
                    log(f"'{self.name}' restart-after-crash also failed: {restart_ex}", self.name)

    def publish(self, event: Event) -> None:
        if self._event_bus is None:
            log(f"'{self.name}'.publish('{event.type}') dropped - not bound to an event bus yet", self.name)
            return
        if event.source is None:
            event.source = self.name
        self._event_bus.publish(event)
        with self._lock:
            self._events_out += 1

    def health(self) -> ModuleHealthStatus:
        """Unhealthy the moment there's an active failure streak (any
        `consecutive_failures > 0`), not only once the self-restart
        threshold is reached - a module that's failing but hasn't hit
        `MAX_CONSECUTIVE_FAILURES` yet still deserves to show up as
        unhealthy in `AdapterManager.health_all()`/`HealthMonitor`. Note
        this resets back to healthy immediately after a successful event
        OR after a self-restart (both reset `consecutive_failures` to 0) -
        by design, matching "restart it, don't stop Runtime": a freshly
        recovered adapter should read as healthy again, not stay flagged."""
        with self._lock:
            failures = self._consecutive_failures
            last_error = self._last_error
        healthy = failures == 0
        message = (last_error or "") if not healthy else ""
        return ModuleHealthStatus(healthy=healthy, message=message)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            uptime_s = (utcnow() - self._started_at).total_seconds() if self._started_at else 0.0
            base_status = {
                "name": self.name,
                "events_in": self._events_in,
                "events_out": self._events_out,
                "consecutive_failures": self._consecutive_failures,
                "restart_count": self._restart_count,
                "uptime_s": uptime_s,
                "last_error": self._last_error,
                "last_event_at": self._last_event_at.isoformat() if self._last_event_at else None,
            }
        base_status.update(self._extra_status())
        return base_status

    # -- subclass hooks ---------------------------------------------------------

    def _do_start(self) -> None:
        """Connect to / initialize the external system. Default: no-op -
        a mock-only adapter with nothing to connect to can skip this
        entirely."""

    def _do_stop(self) -> None:
        """Disconnect / release the external system. Default: no-op."""

    def handle_event(self, event: Event) -> None:
        """Internal Event -> external API call. Default: no-op - an
        adapter that only PRODUCES events (never consumes any) doesn't
        need to override this."""

    def _extra_status(self) -> Dict[str, Any]:
        """Adapter-specific extra fields merged into `status()`'s
        output. Default: nothing extra."""
        return {}
