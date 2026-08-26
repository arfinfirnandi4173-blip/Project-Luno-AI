"""
health.py
=========

`HealthMonitor` - continuous(ish) aggregation of "is everything OK",
covering the spec's list: module alive/stalled (delegated to each
module's own `Module.health()`), event backlog / queue length
(delegated to the Event Bus's `stats()`), and a rolling log of recent
failures. Public API matches the spec exactly: `healthy()`, `report()`,
`last_errors()`.

This does not run its own background thread - `report()`/`healthy()`
compute a fresh snapshot on demand (cheap: it's just aggregating
numbers other components already track), and `Scheduler`/`Heartbeat`
are what call it periodically in real wiring (see `runtime.py`).
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Deque, List, Optional

from .models import HealthReport
from .utils import utcnow

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .module_manager import ModuleManager

DEFAULT_MAX_ERRORS = 200
#: Event Bus queue size above which the backlog itself is treated as an
#: issue - if this is regularly tripped in practice, subscribers are too
#: slow or too numerous for current throughput; see `event_bus.py`.
QUEUE_BACKLOG_WARNING = 500


class HealthMonitor:
    def __init__(
        self,
        module_manager: "ModuleManager",
        event_bus: Optional["EventBus"] = None,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self.module_manager = module_manager
        self.event_bus = event_bus
        self._lock = threading.Lock()
        self._errors: Deque[str] = deque(maxlen=max_errors)

    def record_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(f"[{utcnow().isoformat()}] {message}")

    def healthy(self) -> bool:
        return self.report().healthy

    def report(self) -> HealthReport:
        modules = {}
        issues: List[str] = []
        for name in self.module_manager.all_modules():
            status = self.module_manager.health_of(name)
            modules[name] = status
            if not status.healthy or status.stalled:
                issues.append(f"{name}: {status.message or ('stalled' if status.stalled else 'unhealthy')}")

        if self.event_bus is not None:
            backlog = self.event_bus.stats()["queue_size"]
            if backlog > QUEUE_BACKLOG_WARNING:
                issues.append(f"event bus backlog high: {backlog} events queued")
            # Surface degraded subscribers (see event_bus.py's self-healing
            # design) - these are still subscribed and will recover on
            # their own, but a persistently-degraded one is exactly the
            # kind of silent failure this report exists to catch.
            for degraded in self.event_bus.degraded_subscribers():
                issues.append(
                    f"event bus subscriber degraded: pattern='{degraded['pattern']}' "
                    f"failures={degraded['consecutive_failures']} last_error={degraded['last_error']!r}"
                )

        return HealthReport(healthy=(len(issues) == 0), modules=modules, issues=issues)

    def last_errors(self, limit: int = 20) -> List[str]:
        with self._lock:
            return list(self._errors)[-limit:]
