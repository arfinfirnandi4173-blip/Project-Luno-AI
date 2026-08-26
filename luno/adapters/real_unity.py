"""
real_unity.py
==============

Real `UnityClient` implementation for `UnityAdapter` (see `unity.py`) -
wrapping the EXISTING avatar-animation bridges (`luno.vnyan_engine_bridge`
for `AVATAR_BACKEND=vnyan_engine`, `luno.vnyan_bridge` for the older
`AVATAR_BACKEND=vnyan`), selected the exact same way `luno.config`'s own
`AVATAR_BACKEND` setting already does elsewhere in this project. Neither
bridge module is modified - this file only calls their already-public
functions.

Honest mapping note (matching this project's own "CATATAN JUJUR" culture
- see `vnyan_engine_bridge.py`'s docstring): `UnityClient`'s interface
is richer than what either backend actually exposes. Neither bridge has
a generic "play named animation" concept - `vnyan_engine_bridge` only
has two boolean idle-state flags (`set_thinking`/`set_speaking`) plus
expression tags; `vnyan_bridge` (the older backend) has expression tags
only. `send_animation(name, params)` is therefore mapped onto whichever
of those the name plausibly means (`"thinking"`/`"speaking"` toggle the
matching flag when using the engine backend; every other name - most
commonly Behavior Tree node names like `"idle"`/`"listening"` - clears
both flags and, where nothing else fits, falls back to treating the
name as an expression tag) rather than pretending a capability exists
that doesn't. `ping()` is similarly honest: VMC/OSC is a fire-and-forget
UDP protocol with no request/response handshake, so this can only ever
be a "did opening/using the socket raise" check, never a true
reachability guarantee - documented, not hidden, exactly like
`UnityAdapter._do_start()`'s own docstring already expects
("Cheap liveness check").

Opt-in only: `UNITY_BACKEND=real` (see
`luno/bootstrap/launcher_config.py`) - default stays `MockUnityClient`,
zero behavior change unless explicitly enabled.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, Optional

from .unity import UnityClient
from .utils import log

_THINKING_LIKE = {"thinking", "think"}
_SPEAKING_LIKE = {"speaking", "speak", "talking"}


class RealUnityClient(UnityClient):
    def __init__(self) -> None:
        import luno.config as legacy_config
        self._config = legacy_config
        self._backend = (getattr(legacy_config, "AVATAR_BACKEND", "vnyan_engine") or "vnyan_engine").strip().lower()
        self._bridge = self._load_bridge()

    def _load_bridge(self) -> Any:
        if self._backend == "vnyan_engine":
            from luno import vnyan_engine_bridge
            return vnyan_engine_bridge
        from luno import vnyan_bridge
        return vnyan_bridge

    # -- UnityClient -------------------------------------------------------

    def send_animation(self, name: str, params: Optional[Dict[str, Any]] = None) -> None:
        lowered = (name or "").strip().lower()
        if not hasattr(self._bridge, "set_thinking") or not hasattr(self._bridge, "set_speaking"):
            # Older `vnyan_bridge` backend has no idle-state flags at all -
            # the closest honest equivalent is sending it as an expression.
            self.send_expression(name, params)
            return
        if lowered in _THINKING_LIKE:
            self._bridge.set_thinking(True)
            self._bridge.set_speaking(False)
        elif lowered in _SPEAKING_LIKE:
            self._bridge.set_thinking(False)
            self._bridge.set_speaking(True)
        else:
            self._bridge.set_thinking(False)
            self._bridge.set_speaking(False)

    def send_expression(self, name: str, params: Optional[Dict[str, Any]] = None) -> None:
        try:
            self._bridge.send_expression(name)
        except Exception as ex:
            log(f"send_expression('{name}') raised: {ex}", "unity")

    def set_emotion(self, emotion: str) -> None:
        # Neither backend has a separate "emotion" concept distinct from
        # expression tags - `expressions.guess_expression()`-style tags
        # ARE how emotion is communicated to VNyan in this project.
        self.send_expression(emotion)

    def ping(self) -> bool:
        """Best-effort only - see module docstring. VMC/OSC is UDP with
        no handshake, so this can never confirm VNyan is actually
        listening, only that host/port are configured and a local
        socket can be opened for that destination."""
        host = getattr(self._config, "VNYAN_OSC_HOST", None)
        port = getattr(self._config, "VNYAN_OSC_PORT", None)
        if not host or not port:
            return False
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.connect((host, int(port)))
            return True
        except Exception as ex:
            log(f"ping() could not reach {host}:{port}: {ex}", "unity")
            return False
        finally:
            if sock is not None:
                sock.close()
