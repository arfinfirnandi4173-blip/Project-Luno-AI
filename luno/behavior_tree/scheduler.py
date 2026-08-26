"""
scheduler.py
============

Runs the Behavior Tree's decision loop on its own daemon thread, ticking
every 100-200ms per the spec's Scheduler section, and owns the
`ThreadPoolExecutor` that every heavy action in `actions.py` dispatches
onto via `actions._dispatch()` - so a slow LLM call/TTS/tool/camera
capture NEVER blocks the tick loop itself (the spec's "Never block speech
recognition / TTS / Unity / Home Assistant / Vision" requirement).

This module does not know what a "tick" DOES - that's entirely
`behavior_tree.BehaviorTree.tick()`. `Scheduler` just calls it on a
schedule and keeps the loop alive if it throws.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Optional

from .blackboard import Blackboard, utcnow

if TYPE_CHECKING:
    from .behavior_tree import BehaviorTree

# Spec: "Behavior Tree updates every 100-200 ms."
DEFAULT_TICK_INTERVAL_S = 0.15
MAX_WORKERS = 4


class Scheduler:
    def __init__(
        self,
        tree: "BehaviorTree",
        blackboard: Blackboard,
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
        perceive: Optional[Callable[[Blackboard], None]] = None,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        """`perceive`, if given, is called at the START of every tick,
        BEFORE `tree.tick()` - the injection point for refreshing sensor
        data onto the Blackboard (Whisper/Vision Memory/YOLO/Home
        Assistant/etc., per the spec's "Read all sensors" step). Kept as
        an injected callable rather than hardcoded imports so this package
        stays runnable/testable without any real sensors wired up - a test
        (or a minimal `main.py` integration) can pass a `perceive` that
        just pushes synthetic Blackboard updates."""
        self.tree = tree
        self.bb = blackboard
        self.tick_interval_s = tick_interval_s
        self._perceive = perceive
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="luno-bt")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.tick_count = 0
        self.last_tick_duration_s = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="luno-behavior-tree")
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._running = False
        if wait and self._thread is not None:
            self._thread.join(timeout=self.tick_interval_s * 3)
        self.executor.shutdown(wait=wait)

    def _loop(self) -> None:
        while self._running:
            cycle_start = time.time()
            try:
                self.bb.now = utcnow()
                if self._perceive is not None:
                    self._perceive(self.bb)
                self.tree.tick()
                self.tick_count += 1
            except Exception as ex:
                # A tick failing must NEVER kill the loop (per the spec's
                # "never crash Luno" safety rule, same principle already
                # applied throughout luno/vision.py and luno/vision_memory/).
                self.bb.record_error(f"scheduler tick failed: {ex}")
                traceback.print_exc()
            self.last_tick_duration_s = time.time() - cycle_start
            sleep_for = max(0.0, self.tick_interval_s - self.last_tick_duration_s)
            time.sleep(sleep_for)

    def submit(self, fn: Callable, *args, **kwargs):
        """Generic escape hatch for anything that wants to run heavy work
        on this Scheduler's executor without going through `actions.
        _dispatch()` (e.g. `perceive` callbacks that themselves need to do
        I/O - though ideally those stay cheap and cache results, see
        `vision.py`'s ambient watch loop pattern for a proven example)."""
        return self.executor.submit(fn, *args, **kwargs)
