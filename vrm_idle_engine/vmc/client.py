"""
vmc/client.py
==============

`VMCClient` is the single point of contact with the network: every OSC
message the engine ever sends goes through here. It wraps `python-osc`'s
`SimpleUDPClient` and exposes small, purpose-specific methods (`send_bone`,
`send_blend`, ...) plus one convenience method, `send_frame`, that the
`AnimationController` calls once per tick with the fully-composited pose.

Kept deliberately dumb: this module does not know *why* a bone should be at
a given rotation, only *how* to put that rotation on the wire. That
separation is what makes the engine easy to test (every animation layer can
be unit-tested with no network involved) and easy to retarget (swapping VMC
for a different protocol later only means rewriting this one file).
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, Mapping, Tuple

from pythonosc.udp_client import SimpleUDPClient

from vrm_idle_engine.math.quaternion import Quaternion
from vrm_idle_engine.vmc import protocol


class VMCClient:
    """Thin, purpose-built OSC sender for the VMC Protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 39539) -> None:
        self.host = host
        self.port = port
        self._osc = SimpleUDPClient(host, port)
        self._start_time = time.time()
        self._last_heartbeat = 0.0

    # -- low-level primitives -------------------------------------------

    def send_bone(self, name: str, quat: Quaternion, pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        """Send one bone's local position + rotation."""
        self._osc.send_message(
            protocol.ADDR_BONE_POS,
            [name, pos[0], pos[1], pos[2], quat.x, quat.y, quat.z, quat.w],
        )

    def send_blend_raw(self, name: str, value: float) -> None:
        """Send one blend shape value under its literal clip name."""
        self._osc.send_message(protocol.ADDR_BLEND_VAL, [name, float(value)])

    def send_blend(self, canonical_key: str, value: float) -> None:
        """Send a blend shape value under every alias registered for
        `canonical_key` in `protocol.BLEND_ALIASES` (VRM0 + VRM1 + any
        model-specific custom names), maximizing receiver compatibility."""
        for name in protocol.resolve_blend_names(canonical_key):
            self.send_blend_raw(name, value)

    def apply_blends(self) -> None:
        """Flush all previously-queued blend shape values. Must be sent once
        per frame after all `send_blend`/`send_blend_raw` calls, per spec."""
        self._osc.send_message(protocol.ADDR_BLEND_APPLY, [])

    def send_time(self) -> None:
        """Send the sender-relative timestamp, mostly useful for receivers
        that want to detect a stalled/disconnected stream."""
        self._osc.send_message(protocol.ADDR_TIME, [time.time() - self._start_time])

    def send_available(self, loaded: int = 1) -> None:
        """Announce stream availability (`/VMC/Ext/OK`)."""
        self._osc.send_message(protocol.ADDR_AVAILABLE, [loaded])

    def maybe_send_heartbeat(self, interval_s: float) -> None:
        """Rate-limited wrapper around `send_available` + `send_time`,
        intended to be called every frame; it internally decides whether
        enough time has passed to actually emit a packet."""
        now = time.time()
        if now - self._last_heartbeat >= interval_s:
            self.send_available(1)
            self.send_time()
            self._last_heartbeat = now

    # -- high-level convenience -------------------------------------------

    def send_frame(
        self,
        bone_rotations: Mapping[str, Quaternion],
        blend_values: Mapping[str, float],
        bone_positions: Mapping[str, Tuple[float, float, float]] | None = None,
    ) -> None:
        """
        Send one complete animation frame: every bone rotation in
        `bone_rotations` (with an optional per-bone position override from
        `bone_positions`), every blend shape value in `blend_values`, and the
        mandatory trailing Blend/Apply message.
        """
        bone_positions = bone_positions or {}
        for bone_name, quat in bone_rotations.items():
            pos = bone_positions.get(bone_name, (0.0, 0.0, 0.0))
            self.send_bone(bone_name, quat, pos)

        for key, value in blend_values.items():
            self.send_blend(key, value)
        self.apply_blends()
