"""
world_model.py
================

World Model - "the Single Source of Truth" for current device state.

Deliberately a NEW, standalone module - does NOT modify the Event Bus,
Home Assistant WebSocket plumbing, `ToolResult`, `luno.memory_guard`, the
Planner, or the Tool Manager (per this sprint's own "Jangan mengubah
implementasi tersebut"). It's built entirely ON TOP of what those
already provide:

  - `luno.adapters.home_assistant.HomeAssistantAdapter.on_state_changed()`
    already publishes a `device_state_changed` event (`{entity_id,
    old_state, new_state}`) for every real HA `state_changed` WebSocket
    message - see `luno/adapters/home_assistant.py`. This module just
    needs to subscribe to it (`update_from_state_changed`).
  - `RealHomeAssistantSource` (`luno/adapters/real_home_assistant.py`)
    already does a one-time full `get_states()` fetch right after
    connecting, caching it in `_last_states` - that file gained one
    small, additive, backward-compatible method (`get_all_states()`)
    so this module can read it for startup sync
    (`sync_from_states()`) without a second HA connection or any
    polling.
  - `ToolResult.success`/`.data["entity_id"/"actual_state"]` (Reliability
    Sprint) are already the honest, verified truth
    (`update_from_tool_result`) - this module reads exactly those two
    fields and nothing else, never `.message`.

This is NOT a database of things Luno knows ABOUT the user (that's
`luno.memory`/`luno.memory_guard` - preferences, history, "the user
likes 24 degrees"). It is only ever "what is the world like RIGHT NOW"
("light.bedroom = on") - see this sprint's own "Prinsip" section.

Performance (per the sprint spec): no polling, anywhere, ever. Every
update is push-driven from exactly one of: startup sync (once),
`device_state_changed` (Event Bus subscription), or a verified
`ToolResult`. Plain in-memory dict, O(1) `get()`/`exists()`.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _log(message: str) -> None:
    """Matches `luno/memory_guard.py`'s own convention (plain `print()`,
    no dedicated logger) - keeps this module dependency-free, per the
    sprint's own "hindari dependency baru" rule."""
    print(f"[WorldModel] {message}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field(obj: Any, key: str) -> Any:
    """Duck-typed field access, same convention as
    `luno.memory_guard._field()` (kept as a local copy rather than a
    cross-package import - this module stays dependency-free). Accepts:
    a plain dict, a real `luno.core.events.Event` (dict-like `.get()`,
    reads from `.data`), or a real `ToolResult`/similar object (plain
    attribute access - it has no `.get()`)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except TypeError:
            pass
    return getattr(obj, key, None)


class WorldModel:
    """In-memory `{entity_id: {"state", "source", "timestamp"}}`, O(1)
    lookup. Every write funnels through `_apply()` (single choke point,
    per Bagian 9's logging requirement and Bagian 8's optional event),
    which also silently no-ops when the new value is identical to what's
    already stored - "update" here means an actual change, not "was
    called".

    `event_bus`, if given, gets one `world_model_updated` event
    published per real change (Bagian 8 - "tidak wajib bila terlalu
    invasif": passing `None`, the default, makes this a pure,
    zero-coupling in-memory store usable standalone, e.g. in a plain
    unit test with no Event Bus at all).
    """

    def __init__(self, event_bus: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._event_bus = event_bus

    def bind_event_bus(self, event_bus: Any) -> None:
        """Matches the `bind_event_bus(event_bus)` pattern every Module
        in `main_runtime_demo.py` already uses - lets a `WorldModel`
        be constructed before the runtime's Event Bus exists yet (e.g.
        in `PlannerBridgeModule.__init__`) and wired up once it does,
        without requiring the constructor call site and the bind call
        site to be the same place."""
        self._event_bus = event_bus

    # -- Bagian 7: core read API --------------------------------------------

    def get(self, entity_id: str) -> Optional[Any]:
        """O(1). Just the state value (e.g. `"on"`), or `None` if this
        entity has never been observed."""
        with self._lock:
            entry = self._states.get(entity_id)
            return entry["state"] if entry else None

    def exists(self, entity_id: str) -> bool:
        with self._lock:
            return entity_id in self._states

    def all_entities(self) -> Dict[str, Any]:
        """Bagian 7 - `{entity_id: state}`, the plain minimal shape any
        future consumer (Planner/Avatar/Vision/Scheduler) needs, without
        leaking this store's own per-entity metadata (source/timestamp)
        - use `snapshot()` for that."""
        with self._lock:
            return {eid: entry["state"] for eid, entry in self._states.items()}

    # -- low-level write (every update method below funnels through this) --

    def update(self, entity_id: str, state: Any, source: str = "unknown") -> None:
        if not entity_id:
            return
        self._apply(entity_id, state, source)

    def remove(self, entity_id: str) -> None:
        with self._lock:
            existed = self._states.pop(entity_id, None) is not None
        if existed:
            _log(f"Update\nEntity: {entity_id}\nOld: (known)\nNew: (removed)\nSource: remove")

    # -- Bagian 4: update from a verified ToolResult ------------------------

    def update_from_tool_result(self, tool_result: Any, source: str = "tool_result") -> bool:
        """Bagian 4 - reads ONLY `ToolResult.success` and
        `.data["entity_id"/"actual_state"]`, never `.message` (never
        parses natural language). `success` not being exactly `True`
        (missing/False/unreadable) always means: do nothing, state stays
        whatever it was. Returns whether an update was attempted (not
        necessarily whether the value actually changed - see `_apply`)."""
        if _field(tool_result, "success") is not True:
            return False
        data = _field(tool_result, "data")
        if not isinstance(data, dict):
            return False
        entity_id = data.get("entity_id")
        actual_state = data.get("actual_state")
        if not entity_id or actual_state is None:
            return False
        self._apply(entity_id, actual_state, source)
        return True

    # -- Bagian 3: update from a raw HA state_changed event -----------------

    def update_from_state_changed(self, event: Any) -> bool:
        """Accepts a real `Event` (as delivered by `EventBus.subscribe()`
        - dict-like `.get()`, reads from `.data`) or a plain dict with
        `entity_id`/`new_state` keys - exactly `DeviceStateChanged`'s own
        payload shape (`luno/adapters/events.py`), so this can be
        registered directly as an EventBus subscriber for
        `"device_state_changed"` with no adapter change and no shim."""
        entity_id = _field(event, "entity_id")
        new_state = _field(event, "new_state")
        if not entity_id or new_state is None:
            return False
        self._apply(entity_id, new_state, "state_changed")
        return True

    # -- Bagian 2: one-time startup sync -------------------------------------

    def sync_from_states(self, states: Dict[str, Any], source: str = "startup_sync") -> int:
        """Bulk-loads a `{entity_id: state}` mapping - exactly the shape
        `RealHomeAssistantClient.get_all_states()` returns (new,
        additive method - see `luno/adapters/real_home_assistant.py`),
        itself just a copy of the cache `RealHomeAssistantSource`
        already builds from ONE `get_states()` call at connect time.
        Intended to run exactly once, at startup (see
        `luno/bootstrap/adapters.py`). Returns how many entities were
        loaded."""
        count = 0
        for entity_id, state in (states or {}).items():
            if entity_id and state is not None:
                self._apply(entity_id, state, source)
                count += 1
        return count

    # -- Bagian 1: snapshot/restore -------------------------------------------

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Deep copy, including per-entity metadata (source/timestamp) -
        a caller mutating the returned dict can never corrupt live
        state."""
        with self._lock:
            return copy.deepcopy(self._states)

    def restore(self, snapshot: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self._states = copy.deepcopy(snapshot) if snapshot else {}
        _log(f"Update\nEntity: (bulk restore)\nNew: {len(self._states)} entities\nSource: restore")

    # -- internal -------------------------------------------------------------

    def _apply(self, entity_id: str, new_state: Any, source: str) -> None:
        with self._lock:
            old_entry = self._states.get(entity_id)
            old_state = old_entry["state"] if old_entry else None
            if old_entry is not None and old_state == new_state:
                return  # genuinely nothing changed - skip log/event noise
            self._states[entity_id] = {"state": new_state, "source": source, "timestamp": _utcnow_iso()}

        _log(f"Update\nEntity: {entity_id}\nOld: {old_state}\nNew: {new_state}\nSource: {source}")

        if self._event_bus is not None:
            self._publish_updated(entity_id, old_state, new_state, source)

    def _publish_updated(self, entity_id: str, old_state: Any, new_state: Any, source: str) -> None:
        """Bagian 8 - optional `world_model_updated` event. Import kept
        local (not at module top) so `luno.world_model` never requires
        `luno.core` to be importable for the zero-Event-Bus-coupling
        use case (`WorldModel()` with no `event_bus` arg) described in
        the class docstring."""
        try:
            from luno.core.events import Event
            self._event_bus.publish(Event(type="world_model_updated", data={
                "entity_id": entity_id, "old_state": old_state, "new_state": new_state,
                "source": source, "timestamp": _utcnow_iso(),
            }))
        except Exception as ex:
            _log(f"✗ failed to publish world_model_updated for {entity_id}: {ex}")
