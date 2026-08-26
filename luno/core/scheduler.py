"""
scheduler.py
============

`Scheduler` - periodic, one-shot, delayed, and "cron-like" jobs (e.g.
Heartbeat, Vision polling, Health checks, Memory cleanup, Planner
cleanup - the spec's own examples). A lightweight tick loop (default
1s) checks which jobs are due and hands each one to the `Dispatcher`
to actually run - the tick loop itself never runs job bodies inline, so
one slow job can never delay the next tick or starve other jobs.

Honest scope note on "cron-like": this does not parse cron syntax
(`"0 3 * * *"` strings) - that's a real parser dependency for a feature
nobody has asked to actually use yet. Instead, `schedule_predicate()`
takes an arbitrary `datetime -> bool` predicate evaluated once per tick
(e.g. `lambda now: now.hour == 3 and now.minute == 0`), which covers the
same use cases with zero new dependencies and is trivial to swap for a
real cron parser later if one is ever needed - same "swap the input,
keep the contract" pattern used throughout this project.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from .dispatcher import Dispatcher
from .utils import generate_id, log, utcnow

DEFAULT_TICK_INTERVAL_S = 1.0
#: minimum spacing between two firings of the same predicate-based job,
#: so a predicate that stays true for many consecutive ticks (e.g. "the
#: whole hour of 03:xx") only fires once rather than once per tick.
PREDICATE_MIN_SPACING_S = 60.0


@dataclass
class ScheduledJob:
    job_id: str
    name: str
    fn: Callable[[], None]
    interval_s: Optional[float] = None
    run_at: Optional[datetime] = None
    predicate: Optional[Callable[[datetime], bool]] = None
    enabled: bool = True
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    run_count: int = 0


class Scheduler:
    def __init__(self, dispatcher: Dispatcher, tick_interval_s: float = DEFAULT_TICK_INTERVAL_S) -> None:
        self.dispatcher = dispatcher
        self.tick_interval_s = tick_interval_s
        self._lock = threading.RLock()
        self._jobs: Dict[str, ScheduledJob] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="luno-core-scheduler")
        self._thread.start()
        log("Scheduler started", "scheduler")

    def stop(self) -> None:
        self._running = False
        log("Scheduler stopped", "scheduler")

    # -- public API -------------------------------------------------------

    def schedule_periodic(self, name: str, fn: Callable[[], None], interval_s: float) -> str:
        job_id = generate_id("job")
        job = ScheduledJob(
            job_id=job_id, name=name, fn=fn, interval_s=interval_s,
            next_run_at=utcnow() + timedelta(seconds=interval_s),
        )
        with self._lock:
            self._jobs[job_id] = job
        log(f"scheduled periodic job '{name}' every {interval_s}s ({job_id})", "scheduler")
        return job_id

    def schedule_once(self, name: str, fn: Callable[[], None], delay_s: float = 0.0) -> str:
        job_id = generate_id("job")
        job = ScheduledJob(job_id=job_id, name=name, fn=fn, run_at=utcnow() + timedelta(seconds=delay_s))
        with self._lock:
            self._jobs[job_id] = job
        log(f"scheduled one-shot job '{name}' in {delay_s}s ({job_id})", "scheduler")
        return job_id

    def schedule_predicate(self, name: str, fn: Callable[[], None], predicate: Callable[[datetime], bool]) -> str:
        job_id = generate_id("job")
        job = ScheduledJob(job_id=job_id, name=name, fn=fn, predicate=predicate)
        with self._lock:
            self._jobs[job_id] = job
        log(f"scheduled predicate ('cron-like') job '{name}' ({job_id})", "scheduler")
        return job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def enable(self, job_id: str, enabled: bool = True) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.enabled = enabled
            return True

    def all_jobs(self) -> List[ScheduledJob]:
        with self._lock:
            return list(self._jobs.values())

    # -- internals --------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            self._tick_once()
            time.sleep(self.tick_interval_s)

    def _tick_once(self) -> None:
        now = utcnow()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.enabled:
                continue
            due, one_shot = self._is_due(job, now)
            if not due:
                continue
            self.dispatcher.submit(self._run_job, job)
            with self._lock:
                job.last_run_at = now
                job.run_count += 1
                if job.interval_s is not None:
                    job.next_run_at = now + timedelta(seconds=job.interval_s)
                if one_shot:
                    self._jobs.pop(job.job_id, None)

    @staticmethod
    def _is_due(job: ScheduledJob, now: datetime) -> "tuple[bool, bool]":
        if job.interval_s is not None and job.next_run_at is not None:
            return now >= job.next_run_at, False
        if job.run_at is not None:
            return now >= job.run_at, True
        if job.predicate is not None:
            if job.last_run_at is not None and (now - job.last_run_at).total_seconds() < PREDICATE_MIN_SPACING_S:
                return False, False
            try:
                return bool(job.predicate(now)), False
            except Exception as ex:
                log(f"predicate for job '{job.name}' raised (treated as not-due): {ex}", "scheduler")
                return False, False
        return False, False

    @staticmethod
    def _run_job(job: ScheduledJob) -> None:
        try:
            job.fn()
        except Exception as ex:
            log(f"scheduled job '{job.name}' raised: {ex}", "scheduler")
