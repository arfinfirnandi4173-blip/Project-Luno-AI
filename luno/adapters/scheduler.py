"""
scheduler.py
============

`SchedulerAdapter` - a bridge between Core's `Scheduler`
(`luno.core.scheduler.Scheduler`, already built and tested - never
reimplemented here) and external periodic sources: vision polling,
heartbeat checks, memory cleanup, planner cleanup, or any other
recurring job a deployment wants. Pure translation, no business logic:
each configured job simply publishes a `scheduled_<name>` Event when
Core's Scheduler says it's due. Whatever that job is FOR (e.g. actually
polling the camera) is some other adapter's concern - it subscribes to
`scheduled_vision_poll` (or whatever pattern the `EventMapping` routes
to it) the same way it subscribes to anything else.

    Core Scheduler tick -> job due -> SchedulerAdapter -> publish(Event(type="scheduled_<name>"))
                                                                    |
                                                    (routed via EventMapping to whichever
                                                     adapter cares, e.g. VisionAdapter)

This keeps the set of periodic jobs fully configurable (a plain list of
`(name, interval_seconds)` pairs) rather than hardcoded scheduling
logic scattered across adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..core.events import Event
from .base import BaseAdapter
from .utils import log

if TYPE_CHECKING:
    from ..core.scheduler import Scheduler as CoreScheduler

#: (job_name, interval_seconds) - the spec's own examples.
DEFAULT_SCHEDULED_JOBS: List[Tuple[str, float]] = [
    ("vision_poll", 5.0),
    ("heartbeat_check", 30.0),
    ("memory_cleanup", 300.0),
    ("planner_cleanup", 120.0),
]


class SchedulerAdapter(BaseAdapter):
    name = "scheduler_adapter"

    def __init__(self, core_scheduler: "CoreScheduler", jobs: Optional[List[Tuple[str, float]]] = None) -> None:
        super().__init__()
        self.core_scheduler = core_scheduler
        self.jobs = list(jobs) if jobs is not None else list(DEFAULT_SCHEDULED_JOBS)
        self._job_ids: List[str] = []
        self._fire_counts: Dict[str, int] = {}

    def _do_start(self) -> None:
        for job_name, interval_s in self.jobs:
            job_id = self.core_scheduler.schedule_periodic(
                f"adapter:{job_name}", lambda jn=job_name: self._fire(jn), interval_s=interval_s,
            )
            self._job_ids.append(job_id)
        log(f"registered {len(self._job_ids)} periodic job(s): {[n for n, _ in self.jobs]}", self.name)

    def _do_stop(self) -> None:
        for job_id in self._job_ids:
            self.core_scheduler.cancel(job_id)
        self._job_ids.clear()

    def _fire(self, job_name: str) -> None:
        self._fire_counts[job_name] = self._fire_counts.get(job_name, 0) + 1
        self.publish(Event(type=f"scheduled_{job_name}", data={"job_name": job_name}))

    def add_job(self, job_name: str, interval_s: float) -> None:
        """Registers one more periodic job at runtime (e.g. a future
        adapter that needs its own polling cadence) without restarting
        the whole adapter."""
        self.jobs.append((job_name, interval_s))
        job_id = self.core_scheduler.schedule_periodic(
            f"adapter:{job_name}", lambda jn=job_name: self._fire(jn), interval_s=interval_s,
        )
        self._job_ids.append(job_id)

    def handle_event(self, event: Any) -> None:
        """Supports a generic `schedule_job_request` control event
        (`data={"name": ..., "interval_s": ...}`) for dynamically adding
        jobs through the Event Bus instead of calling `add_job()`
        directly - optional, most deployments will just use
        `add_job()`."""
        if event.type != "schedule_job_request":
            return
        job_name = event.get("name")
        interval_s = event.get("interval_s")
        if job_name and interval_s:
            self.add_job(job_name, float(interval_s))

    def _extra_status(self) -> Dict[str, Any]:
        return {"jobs": [n for n, _ in self.jobs], "fire_counts": dict(self._fire_counts)}
