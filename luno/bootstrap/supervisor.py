"""
supervisor.py
=============

Background supervision: "If an adapter crashes: Restart only that
adapter. Do not restart the entire Runtime. If restart fails
repeatedly: Mark adapter unhealthy. Publish SystemError. Continue
operating."

Most of this already exists and needed no new code: `BaseAdapter` (see
`luno/adapters/base.py`) already self-restarts after 5 consecutive
per-event handler failures, and `LifecycleManager.startup()`/
`ModuleManager.start()` already mark a module that fails to START as
FAILED without aborting the rest of Runtime startup - see
`luno/core/lifecycle.py`. The ONE genuine gap (confirmed during Sprint 6
research): nothing in the existing codebase ever actually CALLS
`LifecycleManager.restart_failed()` (or the adapter-specific
equivalent, `AdapterManager.restart()`) automatically - a module or
adapter that fails to start just stays FAILED forever, silently, unless
a developer happens to run `/reload` by hand.

`Supervisor` closes that gap: a periodic job (driven by Core's own
`Scheduler`, the same mechanism the spec's own examples - vision
polling, heartbeat checks, memory cleanup - already use) sweeps every
registered module, and for anything currently FAILED:

  - if it's a registered adapter, calls `AdapterManager.restart(name)`
    (the richer `_do_stop()`/`_do_start()` + adapter-level restart
    counting - see `base.py`), preferred over the generic
    `ModuleManager.restart()` for adapters specifically, per the spec's
    own wording ("restart only that adapter").
  - otherwise (a plain Core module - session_manager, barge_in, ...),
    calls `ModuleManager.start(name)` (what `LifecycleManager.
    restart_failed()` itself does internally).

Restart attempts are capped per module (`LauncherConfig.
supervisor_max_restart_attempts`, default 3) - after that many
consecutive failed restart attempts, the module is marked "given up on"
(not retried again this run) and a `SystemError` is published, exactly
matching "mark adapter unhealthy, publish SystemError, continue
operating" rather than hammering a permanently-broken dependency
forever. The counter resets the moment a module is observed RUNNING
again (a successful restart, or a manual `/restart`/`/reload`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Set

from luno.core.events import SystemError
from luno.core.models import ModuleState
from luno.core.utils import log

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig


class Supervisor:
    def __init__(self, runtime: "Runtime", adapter_manager: "AdapterManager", config: "LauncherConfig") -> None:
        self.runtime = runtime
        self.adapter_manager = adapter_manager
        self.config = config
        self._restart_attempts: Dict[str, int] = {}
        self._given_up_on: Set[str] = set()
        self._job_id: Optional[str] = None

    def start(self) -> None:
        if not self.config.supervisor_enabled:
            log("supervisor disabled by config - failed modules will not be auto-restarted", "supervisor")
            return
        self._job_id = self.runtime.scheduler.schedule_periodic(
            "supervisor_sweep", self._sweep, interval_s=self.config.supervisor_interval_s,
        )
        log(f"supervisor active - sweeping every {self.config.supervisor_interval_s}s", "supervisor")

    def stop(self) -> None:
        if self._job_id is not None:
            self.runtime.scheduler.cancel(self._job_id)
            self._job_id = None

    def sweep_once(self) -> None:
        """Exposed for tests/`/health`-triggered manual sweeps - the
        scheduled job just calls this on a timer."""
        self._sweep()

    def _sweep(self) -> None:
        adapter_names = set(self.adapter_manager.registry.list_adapters())
        for name, record in self.runtime.module_manager.all_modules().items():
            if record.state != ModuleState.FAILED:
                self._restart_attempts.pop(name, None)
                self._given_up_on.discard(name)
                continue
            if name in self._given_up_on:
                continue

            attempts = self._restart_attempts.get(name, 0)
            if attempts >= self.config.supervisor_max_restart_attempts:
                self._given_up_on.add(name)
                message = f"'{name}' failed to recover after {attempts} restart attempt(s) - marked unhealthy, continuing to operate"
                log(message, "supervisor")
                self.runtime.event_bus.publish(SystemError(data={"module": name, "error": message}, source="supervisor"))
                continue

            self._restart_attempts[name] = attempts + 1
            try:
                if name in adapter_names:
                    # `AdapterManager.restart()` deliberately calls the
                    # adapter's OWN `restart()` (its `_do_stop()`/
                    # `_do_start()` + adapter-level restart counting -
                    # see `luno/adapters/base.py`), NOT
                    # `ModuleManager.restart()` - by design, per that
                    # method's own docstring. One consequence: it never
                    # touches `ModuleRecord.state`, so a FAILED record
                    # stays FAILED forever even after the adapter itself
                    # successfully reconnected underneath it. Since
                    # nothing else in `luno.core`/`luno.adapters`
                    # reconciles that gap, the supervisor does it here -
                    # this mutates `ModuleManager`'s own bookkeeping
                    # (not `AdapterManager`'s), which is exactly what
                    # `ModuleManager.start()`/`.restart()` would have
                    # done had `AdapterManager` called through to them.
                    self.adapter_manager.restart(name)
                    record.state = ModuleState.RUNNING
                    record.error = None
                else:
                    self.runtime.module_manager.start(name)
                log(f"'{name}' restarted by supervisor (attempt {attempts + 1})", "supervisor")
            except Exception as ex:
                log(f"'{name}' restart attempt {attempts + 1} failed: {ex}", "supervisor")
