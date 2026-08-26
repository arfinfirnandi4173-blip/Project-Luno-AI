"""
real_home_assistant.py
========================

Real `HomeAssistantSource`/`HomeAssistantClient` implementations for
`HomeAssistantAdapter` (see `home_assistant.py`) - wrapping the
EXISTING, untouched `luno.ha_client.HomeAssistantClient` (a plain
async websocket client - connect/auth/subscribe/listen/get_states/
call_service/disconnect, no Luno-specific device knowledge at all).
Nothing in `luno/ha_client.py` is modified; this file only calls its
already-public async methods from a background asyncio loop it owns.

Deliberately does NOT reuse `luno.ha_listener.run_ha_listener()` /
`luno.devices.build_devices()` - those exist to drive the LEGACY,
procedural device-controller registry (`luno/devices.py`,
`luno/lights.py`-style JSON-configured controllers), a different
concern than "translate a raw HA `state_changed` event into an
internal `HomeAssistantEvent`/`DeviceStateChanged`". Reusing
`HomeAssistantClient`'s own public async API directly (`connect()`,
`subscribe_to_events()`, `listen_and_dispatch(callback)`,
`get_states()`, `call_service()`, `disconnect()`) is the correct reuse
boundary here - `luno.ha_listener`'s reconnect-loop SHAPE is still
mirrored (same exponential backoff, same "listen_and_dispatch must
already be running before get_states()" ordering) because that ordering
constraint is a real property of the underlying protocol, documented in
`ha_listener.py` itself, not something specific to devices.py.

Opt-in only: `HOME_ASSISTANT_BACKEND=real` (see
`luno/bootstrap/launcher_config.py`) - default stays `MockHomeAssistantSource`/
`MockHomeAssistantClient`, zero behavior change unless explicitly enabled.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Optional

from .home_assistant import HomeAssistantClient, HomeAssistantListener, HomeAssistantSource
from .utils import log

_INITIAL_BACKOFF_S = 2.0
_MAX_BACKOFF_S = 60.0
_CALL_TIMEOUT_S = 15.0


class RealHomeAssistantSource(HomeAssistantSource):
    """Runs the reconnect-with-backoff loop on a dedicated background
    thread with its own asyncio event loop - `luno.ha_client`'s client
    is asyncio-only, the Adapter Layer's `HomeAssistantSource` interface
    is plain sync `start()`/`stop()`, so a background loop is the
    correct (and only) bridge, same pattern already established
    elsewhere in this project for HA (`luno/ha_listener.py`'s own
    `ha_loop` + `asyncio.run_coroutine_threadsafe` convention, referenced
    in that module's own docstring)."""

    def __init__(self, ha_client: Optional[Any] = None) -> None:
        if ha_client is None:
            from luno.ha_client import HomeAssistantClient as _RealHAClientImpl
            ha_client = _RealHAClientImpl()
        self.ha_client = ha_client
        self._listener: Optional[HomeAssistantListener] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._last_states: Dict[str, str] = {}
        # RGB color/brightness verification fix - `_last_states` only ever
        # kept the bare state STRING ("on"/"off"), never the entity's
        # ATTRIBUTES dict (rgb_color, brightness, ...) - HA's own
        # `state_changed` event payload (`new_state_obj` below) already
        # carries `attributes`, it was just being discarded. Kept as a
        # SEPARATE dict (not merged into `_last_states`) so every existing
        # reader of `_last_states` (World Model sync, `get_entity_state()`,
        # the on/off verify loop) stays completely untouched.
        self._last_attributes: Dict[str, Dict[str, Any]] = {}

    def start(self, listener: HomeAssistantListener) -> None:
        self._listener = listener
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="luno-ha-real-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.ha_client.disconnect(), loop)
            except Exception as ex:
                log(f"error requesting HA disconnect: {ex}", "home_assistant")
        self._listener = None

    def _run(self) -> None:
        try:
            asyncio.run(self._loop_body())
        except Exception as ex:
            log(f"real HA source loop exited: {ex}", "home_assistant")

    async def _loop_body(self) -> None:
        self.loop = asyncio.get_running_loop()
        backoff = _INITIAL_BACKOFF_S
        while not self._stop_flag.is_set():
            connected = False
            try:
                connected = bool(await self.ha_client.connect())
            except Exception as ex:
                log(f"connect() raised: {ex}", "home_assistant")
            if not connected:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue

            backoff = _INITIAL_BACKOFF_S
            await self.ha_client.subscribe_to_events()
            listen_task = asyncio.create_task(self.ha_client.listen_and_dispatch(self._on_ha_event))

            try:
                states = await self.ha_client.get_states()
                for state in states or []:
                    entity_id = state.get("entity_id")
                    if entity_id:
                        self._last_states[entity_id] = state.get("state")
                        self._last_attributes[entity_id] = state.get("attributes") or {}
            except Exception as ex:
                log(f"initial get_states() failed (continuing): {ex}", "home_assistant")

            await listen_task
            if self._stop_flag.is_set():
                break
            log(f"disconnected - reconnecting in {backoff}s", "home_assistant")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _on_ha_event(self, state_changed_data: Dict[str, Any]) -> None:
        """`state_changed_data` is exactly `ha_client.listen_and_dispatch`'s
        `event_data.get("data", {})` for a `state_changed` event - HA's
        standard shape: `{"entity_id", "old_state": {...}|None, "new_state": {...}}`."""
        listener = self._listener
        if listener is None:
            return
        entity_id = state_changed_data.get("entity_id")
        if not entity_id:
            return
        old_state_obj = state_changed_data.get("old_state") or {}
        new_state_obj = state_changed_data.get("new_state") or {}
        old_state = old_state_obj.get("state") if isinstance(old_state_obj, dict) else None
        new_state = new_state_obj.get("state") if isinstance(new_state_obj, dict) else None
        self._last_states[entity_id] = new_state
        self._last_attributes[entity_id] = (new_state_obj.get("attributes") or {}) if isinstance(new_state_obj, dict) else {}
        try:
            listener.on_state_changed(entity_id, old_state, new_state or "")
        except Exception as ex:
            log(f"listener.on_state_changed raised: {ex}", "home_assistant")


class RealHomeAssistantClient(HomeAssistantClient):
    """Sync `call_service()` facade over the real async
    `luno.ha_client.HomeAssistantClient`, bridged via
    `asyncio.run_coroutine_threadsafe` onto whichever loop
    `RealHomeAssistantSource` is currently running (`source.loop`) -
    mirrors the exact bridging convention `luno/ha_listener.py`'s own
    docstring documents for calling into `ha_client` from another
    thread. If the source hasn't connected yet (`source.loop` is still
    `None`), calls fail fast with a clear error rather than hanging."""

    def __init__(self, source: RealHomeAssistantSource) -> None:
        self.source = source

    def call_service(
        self, domain: str, service: str, entity_id: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        loop = self.source.loop
        if loop is None or not loop.is_running():
            return {
                "success": False, "domain": domain, "service": service,
                "entity_id": entity_id, "data": data or {},
                "error": "Home Assistant source is not connected yet",
            }
        coro = self.source.ha_client.call_service(domain, service, entity_id, dict(data or {}))
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            ok = bool(future.result(timeout=_CALL_TIMEOUT_S))
        except Exception as ex:
            log(f"call_service({domain}.{service}) failed: {ex}", "home_assistant")
            return {
                "success": False, "domain": domain, "service": service,
                "entity_id": entity_id, "data": data or {}, "error": str(ex),
            }
        return {"success": ok, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_all_states(self) -> Dict[str, str]:
        """World Model Sprint - additive, backward-compatible (no
        existing signature touched): a shallow copy of
        `RealHomeAssistantSource._last_states` at this instant - the
        same cache already populated once from `get_states()` right
        after connecting, and kept live afterwards by every
        `state_changed` event (`_on_ha_event`). Intended for exactly
        one call, at startup (see `luno.world_model.WorldModel.
        sync_from_states()` / `luno/bootstrap/adapters.py`) - reading
        this does not touch the network at all, so calling it more than
        once costs nothing but also teaches nothing new beyond what
        `state_changed` will already have delivered by then."""
        return dict(self.source._last_states)

    def get_entity_state(self, entity_id: str, force_refresh: bool = False) -> Optional[str]:
        """Reliability Sprint - state verification: returns the entity's
        CURRENT known state ("on"/"off"/"unavailable"/... or `None` if
        truly never seen).

        Reuses `RealHomeAssistantSource._last_states` - the same cache
        `_on_ha_event()` already keeps up to date in real time off HA's
        own `state_changed` broadcasts - rather than opening a second
        connection or adding a new polling path. If the source has never
        seen this entity at all (cache miss, not "seen and equals
        None"), falls back to one fresh `get_states()` call so a
        never-before-referenced entity can still be verified on first
        use.

        P0.8.7 - `force_refresh=True` (additive; default `False`
        preserves every pre-existing caller's exact behavior, including
        every test that calls this with one positional argument) skips
        the cache-first check entirely and ALWAYS performs a live
        `get_states()` round trip against Home Assistant right now,
        updating the cache from that fresh response before returning -
        used by `RealHomeAssistantHandler._verify_state()`'s post-command
        verification loop specifically so a "verification success" can
        never be reported off a stale `state_changed` push from BEFORE
        the current command (see docs/change_impact/camera_automation_
        p0_8_7.md, objective E - "the final verification must perform a
        fresh HA state query rather than trusting internal/cached
        state"). If the live query itself is impossible right now (the
        background source isn't connected) or fails, this falls back to
        the last known cached value rather than raising or silently
        returning `None` - a genuinely fresh answer is preferred, but a
        stale-but-real answer is still more useful to the caller than no
        answer at all when Home Assistant is briefly unreachable.
        """
        source = self.source

        if not force_refresh and entity_id in source._last_states:
            return source._last_states.get(entity_id)

        loop = source.loop
        if loop is None or not loop.is_running():
            return source._last_states.get(entity_id) if force_refresh else None
        try:
            future = asyncio.run_coroutine_threadsafe(source.ha_client.get_states(), loop)
            states = future.result(timeout=_CALL_TIMEOUT_S) or []
        except Exception as ex:
            log(f"get_entity_state({entity_id}) fresh get_states() fetch failed: {ex}", "home_assistant")
            return source._last_states.get(entity_id) if force_refresh else None
        for state in states:
            if state.get("entity_id") == entity_id:
                value = state.get("state")
                source._last_states[entity_id] = value
                source._last_attributes[entity_id] = state.get("attributes") or {}
                return value
        # P0.8.7 - a fresh, authoritative `get_states()` response was
        # obtained but did NOT contain this entity at all (distinct from
        # "the network call failed"/"not connected yet" above) - this is
        # itself real, honest evidence (the entity is not currently known
        # to Home Assistant), so the STALE cached value must NOT be
        # returned as if it were still valid; `None` is the correct,
        # honest answer here regardless of `force_refresh`.
        return None

    def get_entity_attributes(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """RGB color/brightness verification fix - companion to
        `get_entity_state()`, but for the entity's ATTRIBUTES dict
        (`rgb_color`, `brightness`, ...) rather than the bare state
        string. `set_color`/`set_brightness` have no single canonical
        "on"/"off" to poll for the way `_execute_on_off()`'s verify loop
        does, so `RealHomeAssistantHandler` uses this for a lighter,
        single-read "did the attribute actually move" check instead -
        see that module's own `_verify_light_attributes()`.

        Deliberately NOT part of the `HomeAssistantClient` ABC (same
        "optional capability, callers use `getattr(client, name, None)`
        and skip gracefully if absent" convention `get_all_states()`'s
        own docstring documents for the World Model sync) - callers must
        never assume this exists on every client (e.g. `MockHomeAssistantClient`/
        `FakeHAClient` in tests don't have it).

        Always does a FRESH `get_states()` fetch rather than trusting
        `_last_attributes` alone - unlike the bare on/off state (which
        `_execute_on_off`'s own verify loop already re-polls in a retry
        window), a stale cached attribute would defeat the entire point
        of this check, since it exists specifically to catch "the
        service call was accepted but nothing actually changed"."""
        loop = self.source.loop
        if loop is None or not loop.is_running():
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(self.source.ha_client.get_states(), loop)
            states = future.result(timeout=_CALL_TIMEOUT_S) or []
        except Exception as ex:
            log(f"get_entity_attributes({entity_id}) failed: {ex}", "home_assistant")
            return None
        for state in states:
            if state.get("entity_id") == entity_id:
                attributes = state.get("attributes") or {}
                self.source._last_states[entity_id] = state.get("state")
                self.source._last_attributes[entity_id] = attributes
                return attributes
        return None
