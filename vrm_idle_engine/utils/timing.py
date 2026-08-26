"""
utils/timing.py
=================

`FrameLimiter` paces the main loop to a target frame rate using
`time.perf_counter` + `time.sleep`, without ever accumulating any per-frame
state (no lists, no growing buffers) - important for a process meant to run
continuously for hours without leaking memory.
"""

from __future__ import annotations

import time


class FrameLimiter:
    """Sleeps just enough, each call to `wait()`, to keep the calling loop
    at approximately `fps` iterations per second."""

    def __init__(self, fps: int) -> None:
        self.target_dt = 1.0 / max(1, fps)
        self._last = time.perf_counter()

    def wait(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._last
        remaining = self.target_dt - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.perf_counter()
