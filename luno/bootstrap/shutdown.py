"""
shutdown.py
============

`ShutdownCoordinator` - signal handling (Ctrl+C / SIGTERM) plus the
exact graceful-shutdown sequence the spec lists:

    Ctrl+C
      -> stop accepting new events
      -> cancel active LLM requests
      -> stop GPT-SoVITS playback
      -> stop microphone
      -> stop camera
      -> flush Vision Memory
      -> flush pending logs
      -> stop adapters
      -> stop Runtime
      -> exit cleanly

Most of the middle steps ALREADY exist and needed no new code, once
adapters are stopped in the right order: `OpenRouterAdapter._do_stop()`
already cancels every in-flight request (see `luno/adapters/openrouter.py`)
and `FishAudioAdapter._do_stop()` already calls `client.stop()` (see
`luno/adapters/fish_audio.py`); the microphone/camera loops
(`RealWhisperSource`/`RealVisionSource`, both new this sprint) each stop
cleanly inside their own adapter's `_do_stop()` too. Calling
`adapter_manager.stop_all()` (which stops every registered adapter, in
dependency-reverse order, via `LifecycleManager.shutdown()`) already
performs "cancel active LLM requests / stop GPT-SoVITS playback / stop
microphone / stop camera" correctly, in one call, using each
subsystem's OWN existing shutdown logic - this file's job is
orchestration and ordering, not reimplementing any of that.

"No orphan threads. No hanging executors. No forced process
termination." - every background thread spawned anywhere in this
codebase (Event Bus pump, Scheduler tick loop, Heartbeat, each
adapter's worker pool, `RealWhisperSource`'s mic loop, `RealVisionSource`'s
poll loop, `RealHomeAssistantSource`'s asyncio loop) is already a daemon
thread (confirmed during Sprint 6 research - every `threading.Thread(...)`
call in `luno/core`/`luno/adapters` passes `daemon=True`), so nothing
here needs to forcibly join or kill anything to guarantee clean process
exit; this coordinator still waits briefly for each stage to settle so
shutdown is deterministic and observable in the log, not just "exit and
hope".
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from luno.core.utils import log

from .logging_setup import Timer, configure_logging, log_lifecycle

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from .supervisor import Supervisor
    from luno.dashboard import DashboardServer

_SETTLE_S = 0.15


class ShutdownCoordinator:
    def __init__(
        self,
        runtime: "Runtime",
        adapter_manager: "AdapterManager",
        supervisor: Optional["Supervisor"] = None,
        dashboard: Optional["DashboardServer"] = None,
    ) -> None:
        self.runtime = runtime
        self.adapter_manager = adapter_manager
        self.supervisor = supervisor
        self.dashboard = dashboard
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._already_shutting_down = False
        self._accepting_events = True

    # -- signal handling ----------------------------------------------------

    def install_signal_handlers(self) -> None:
        """SIGINT (Ctrl+C) is always installable. SIGTERM is POSIX-only
        and only installable from the main thread - both failure modes
        (Windows, non-main-thread) are caught and logged rather than
        raised, since a launcher must still be able to run (relying on
        Ctrl+C / KeyboardInterrupt alone in that case) even where a full
        signal handler can't be registered."""
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
        except (ValueError, AttributeError, OSError) as ex:
            log(f"could not install SIGTERM handler ({ex}) - Ctrl+C/SIGINT still works", "shutdown")

    def _on_signal(self, signum: int, frame: Any) -> None:
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        log(f"received {name} - beginning graceful shutdown", "shutdown")
        self._shutdown_event.set()

    def request_shutdown(self) -> None:
        """Non-signal trigger - e.g. the developer console's `/quit`."""
        self._shutdown_event.set()

    def wait_for_shutdown_signal(self, poll_s: float = 0.25) -> None:
        """Blocks the main thread until a shutdown was requested (signal
        or `request_shutdown()`), polling rather than a bare
        `Event.wait()` so Windows still delivers `KeyboardInterrupt`
        promptly (a known CPython quirk: blocking, uninterruptible waits
        can delay Ctrl+C handling on Windows)."""
        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(poll_s)
        except KeyboardInterrupt:
            self._shutdown_event.set()

    def is_accepting_events(self) -> bool:
        return self._accepting_events

    # -- the graceful sequence itself ----------------------------------------

    def shutdown(self) -> None:
        with self._lock:
            if self._already_shutting_down:
                return
            self._already_shutting_down = True

        log_lifecycle("shutdown", "Graceful shutdown started")

        # 1) stop accepting new events - real Whisper/Vision sources keep
        #    running until their own adapter.stop() below, but this flag
        #    is checked by main.py's own input loop (and any future
        #    caller) so nothing NEW gets published once shutdown begins.
        self._accepting_events = False

        # 2) supervisor stops trying to restart anything - a module
        #    going FAILED as a direct RESULT of shutdown must never be
        #    "helpfully" restarted mid-teardown.
        if self.supervisor is not None:
            with Timer() as t:
                self.supervisor.stop()
            log_lifecycle("supervisor", "Supervisor stopped", duration_ms=t.ms)

        # 2b) stop the Dashboard's HTTP server (Sprint 7) BEFORE anything
        #     else tears down - a browser-driven control call (e.g.
        #     "Restart Runtime") racing the shutdown sequence would be
        #     genuinely confusing; closing the listening socket first
        #     means no new dashboard request can be accepted once
        #     shutdown has begun. The dashboard is not a Core `Module`
        #     (see `luno/dashboard/__init__.py`'s own docstring for why),
        #     so it isn't covered by `adapter_manager.stop_all()`/
        #     `runtime.stop()` below - it needs its own explicit step.
        if self.dashboard is not None:
            with Timer() as t:
                try:
                    self.dashboard.stop()
                except Exception as ex:
                    log(f"dashboard.stop() raised (continuing shutdown): {ex}", "shutdown")
            log_lifecycle("dashboard", "Dashboard stopped", duration_ms=t.ms)

        # 3), 4), 5), 6) cancel active LLM requests / stop GPT-SoVITS
        #    playback / stop microphone / stop camera - all four already
        #    happen correctly, per-adapter, inside adapter_manager.stop_all()
        #    (see module docstring for exactly which adapter does what).
        with Timer() as t:
            try:
                self.adapter_manager.stop_all()
            except Exception as ex:
                log(f"adapter_manager.stop_all() raised (continuing shutdown): {ex}", "shutdown")
        log_lifecycle("adapters", "All adapters stopped", duration_ms=t.ms)
        time.sleep(_SETTLE_S)

        # 7) flush Vision Memory - writes are already synchronous/committed
        #    per `vm.update()` call (SQLite-backed, no separate write-behind
        #    buffer in this package today), so there is nothing to flush
        #    beyond an optional close() if the package ever adds one -
        #    checked defensively rather than assumed.
        with Timer() as t:
            self._flush_vision_memory()
        log_lifecycle("vision_memory", "Vision Memory flushed", duration_ms=t.ms)

        # 8) flush pending logs
        with Timer() as t:
            configure_logging(None)  # closes the optional log file handle, if any
        log_lifecycle("logging", "Logs flushed", duration_ms=t.ms)

        # 9) stop Runtime itself (Scheduler, Heartbeat, Coordinator,
        #    Dispatcher, Event Bus - reverse of Runtime.start()'s own order)
        with Timer() as t:
            self.runtime.stop()
        log_lifecycle("runtime", "Runtime stopped", duration_ms=t.ms)

        log_lifecycle("shutdown", "Goodbye")

    @staticmethod
    def _flush_vision_memory() -> None:
        try:
            from luno import vision_memory as vm
            memory = getattr(vm, "_get_memory", None)
            if memory is None:
                return
            instance = memory()
            close_fn = getattr(instance, "close", None) or getattr(instance, "flush", None)
            if callable(close_fn):
                close_fn()
        except Exception as ex:
            log(f"vision_memory flush step raised (non-fatal): {ex}", "shutdown")
