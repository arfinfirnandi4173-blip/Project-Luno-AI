"""
heartbeat.py
============

`HeartbeatMonitor` - runs periodically (via its own thread, kept
independent of `Scheduler` so a heartbeat still fires even if the
Scheduler itself is unhealthy) and publishes a `Heartbeat` event with
uptime, CPU/RAM, active module count, and Event Bus throughput/latency.

`psutil` is optional: if it isn't installed, `cpu_percent`/`ram_mb` are
just `None` in the payload rather than crashing the whole heartbeat -
same honest-degrade pattern used for the WAL pragma fallback in
`vision_memory/database.py` and dozens of other spots this session.

"active planners" / "running tools" from the spec's list are numbers
Core has no way to know on its own (no AI-logic packages are imported
here - see the package docstring) - `gauge_provider` is an optional
injected callable a real integration can wire up to return
`{"running_tools": N, "active_plans": M}`; both default to 0 if absent.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from .events import Heartbeat
from .models import HeartbeatStats
from .utils import log, utcnow

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .module_manager import ModuleManager

DEFAULT_INTERVAL_S = 10.0


class HeartbeatMonitor:
    def __init__(
        self,
        event_bus: "EventBus",
        module_manager: "ModuleManager",
        interval_s: float = DEFAULT_INTERVAL_S,
        gauge_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.event_bus = event_bus
        self.module_manager = module_manager
        self.interval_s = interval_s
        self.gauge_provider = gauge_provider or (lambda: {})
        self._started_at = utcnow()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_stats: Optional[HeartbeatStats] = None

    def start(self) -> None:
        if self._running:
            return
        self._started_at = utcnow()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="luno-core-heartbeat")
        self._thread.start()
        log(f"Heartbeat started (every {self.interval_s}s)", "heartbeat")

    def stop(self) -> None:
        self._running = False
        log("Heartbeat stopped", "heartbeat")

    def beat_now(self) -> HeartbeatStats:
        """Runs one heartbeat immediately (used by tests and by
        `Runtime.status()` for an on-demand snapshot) without waiting
        for the next scheduled tick."""
        stats = self._collect()
        self._last_stats = stats
        self.event_bus.publish(Heartbeat(data=self._stats_to_dict(stats), source="heartbeat"))
        return stats

    def last_stats(self) -> Optional[HeartbeatStats]:
        return self._last_stats

    def _loop(self) -> None:
        while self._running:
            try:
                self.beat_now()
            except Exception as ex:
                log(f"heartbeat collection raised (skipping this beat): {ex}", "heartbeat")
            time.sleep(self.interval_s)

    def _collect(self) -> HeartbeatStats:
        uptime_s = (utcnow() - self._started_at).total_seconds()

        cpu_percent = None
        ram_mb = None
        try:
            import psutil  # type: ignore

            cpu_percent = psutil.cpu_percent(interval=None)
            ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        except Exception:
            pass  # psutil not installed, or platform doesn't support it - honest degrade

        bus_stats = self.event_bus.stats()
        gauges = {}
        try:
            gauges = self.gauge_provider() or {}
        except Exception as ex:
            log(f"gauge_provider raised (using defaults): {ex}", "heartbeat")

        return HeartbeatStats(
            uptime_s=uptime_s,
            cpu_percent=cpu_percent,
            ram_mb=ram_mb,
            active_modules=len(self.module_manager.running_modules()),
            running_tools=int(gauges.get("running_tools", 0)),
            active_plans=int(gauges.get("active_plans", 0)),
            queue_size=bus_stats["queue_size"],
            event_throughput_per_s=(bus_stats["delivered"] / uptime_s) if uptime_s > 0 else 0.0,
            avg_latency_ms=bus_stats["avg_latency_ms"],
        )

    @staticmethod
    def _stats_to_dict(stats: HeartbeatStats) -> Dict[str, Any]:
        return {
            "uptime_s": stats.uptime_s,
            "cpu_percent": stats.cpu_percent,
            "ram_mb": stats.ram_mb,
            "active_modules": stats.active_modules,
            "running_tools": stats.running_tools,
            "active_plans": stats.active_plans,
            "queue_size": stats.queue_size,
            "event_throughput_per_s": stats.event_throughput_per_s,
            "avg_latency_ms": stats.avg_latency_ms,
        }
