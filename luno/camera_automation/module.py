"""
module.py
=========

`CameraAutomationModule` - LUNO P0 (Camera Automation / Safe Integration
& Non-Regression Protocol).

--------------------------------------------------------------------
Architecture - a thin, isolated Event Bus consumer, nothing more
--------------------------------------------------------------------
This module owns NO Home Assistant connection, NO PTZ/camera hardware
access, NO second Event Bus, NO second scheduler, and NO second
automation engine. It is a `Module` (same interface `CameraPatrolModule`/
`AutomationEngine` already implement - see `luno/core/module_manager.py::
Module`), bound to the SAME `event_bus` every other module already uses
(`bind_event_bus()`).

Inbound: `luno.adapters.home_assistant.HomeAssistantAdapter.
on_state_changed()` ALREADY and unconditionally publishes a
`device_state_changed` event (`data={"entity_id", "old_state",
"new_state"}`) onto the existing Event Bus for every entity state
change, for both the mock and real HA backends - this was true before
this module existed and required ZERO changes to make it so. This
module simply adds itself as one more subscriber to that
ALREADY-EXISTING, ALREADY-MULTI-CONSUMER event
(`event_bus.subscribe("device_state_changed", ...)`), the same
"observability tap" idiom `luno/dashboard/event_log_writer.py` and
Sprint 72's own `AutomationEngine._on_bus_event` already use.

Outbound: on an allowlisted entity's state change (after dedupe +
cooldown), this module publishes a NEW, distinctly-namespaced event,
`camera_automation.state_changed`, onto the SAME Event Bus - never a
direct call into any other module. Sprint 72's `AutomationEngine`
already supports triggering a rule off an arbitrary event name string
(`{"trigger": {"type": "event", "parameters": {"event_name":
"camera_automation.state_changed"}}}` in `config/automation_rules.json`)
with ZERO changes to that engine, and already has `home_assistant.
turn_on`/`home_assistant.turn_off` (and `camera.*`) actions allowlisted -
so the full TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN pipeline
for a camera-triggered automation is already available end to end
without one line of new automation-engine code. This module's only job
is producing a clean, camera-domain-specific, deduplicated, cooldown-
bounded trigger event for that existing engine to consume.

The event name is deliberately namespaced `camera_automation.*` to avoid
any collision with: the pre-existing, unrelated Vision/webcam pipeline's
`Person*`/`Camera*` events in `luno/adapters/events.py`
(`PersonAppeared`, `CameraPersonEntered`, `CameraDisconnected`, etc.),
Sprint 71's own `camera_patrol_*` events, and Sprint 72's own
`automation.*`/`automation_triggered` events.

--------------------------------------------------------------------
Fail-safe (P0 brief §11) - a broken camera integration must never take
down anything else
--------------------------------------------------------------------
Every line of this module's event-handling path runs inside a `try/
except` that logs and swallows any exception - it never re-raises into
the Event Bus's own dispatch loop. This is intentionally REDUNDANT with
the Event Bus's own existing self-healing behavior (a subscriber that
raises repeatedly is automatically marked degraded and backed off, never
unsubscribed, never crashes the process) - belt-and-suspenders, not a
replacement for it, and proof that this module cannot affect the Event
Bus, LLM, TTS, STT, memory, existing automations, or existing HA
controls even in a total internal failure.

--------------------------------------------------------------------
Feature flag & fail-open-by-omission (P0 brief §10)
--------------------------------------------------------------------
`CameraAutomationConfig.enabled` defaults to `False`. When disabled,
`start()` does not even subscribe to the Event Bus - this module has
LITERALLY ZERO runtime footprint (no subscription, no thread, no
processing) beyond its own inert `__init__`. When enabled but the
`entities` allowlist is empty (also the default), every `device_state_
changed` event is filtered out immediately - still a no-op in practice.

--------------------------------------------------------------------
Memory / persistence (P0 brief §8) - deliberately none
--------------------------------------------------------------------
`_last_state`/`_cooldown_until` are small, bounded (at most one entry
per allowlisted entity_id, and the allowlist is operator-configured, not
unbounded) in-memory dicts. Nothing here is written to disk. No existing
persistence format is read, written, or depended on.

--------------------------------------------------------------------
P0.5 (Real Camera Integration) addition - additive, not a rewrite
--------------------------------------------------------------------
Everything above this note is UNCHANGED from P0 - the flat `entities`
allowlist still produces the exact same raw `camera_automation.
state_changed` relay it always did, byte-for-byte, and every P0 test
still exercises that exact path unmodified.

P0.5 adds a SECOND, independent classification branch inside `_handle`:
if an incoming `device_state_changed` entity_id belongs to a configured
`CameraProfile` (see `cameras.py` - loaded from the new, isolated
`config/camera_automation.json`, reloadable via `reload_cameras()` the
same way `AutomationEngine.reload_rules()`/`CameraPatrolModule.
_load_routes()` already reload their own config), the raw HA state
change is classified into a normalized `CameraEvent` (`motion_detected`/
`motion_cleared`/`human_detected`/`human_cleared`/`camera_online`/
`camera_offline` - see `cameras.py`'s own module docstring for the full
classification rules) and published as `camera_automation.camera_event`
- a SECOND, distinctly-named event, never replacing `camera_automation.
state_changed`.

Both branches share the EXACT SAME dedupe/cooldown state and logic this
module already had (P0.5 brief Section 11/13: "do NOT duplicate
deduplication logic inside the HA adapter... do NOT duplicate cooldown/
state tracking") - `cameras.py` itself is stateless (pure classification
functions only); all dedupe/cooldown bookkeeping happens exactly once,
here, in `_handle`, using a dedupe/cooldown KEY that is now `(camera_id,
kind)` for a classified event or `entity_id` for the legacy raw relay -
never duplicated, never a second implementation.

An entity_id that is in NEITHER a `CameraProfile` NOR the flat
`entities` allowlist is ignored exactly as before (P0.5 brief Section 9)
- unknown entities never crash, never trigger automation, and (new this
sprint) are logged at debug-log level once per entity for operator
visibility while iterating on `config/camera_automation.json`, without
spamming on every repeat.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Set

from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import log
from .cameras import CameraEvent, CameraProfile, EntityRoleIndex, build_entity_role_index, classify_state_change, load_camera_profiles
from .config import CameraAutomationConfig

#: The one existing, already-published event this module consumes.
#: Owned by `luno.adapters.home_assistant.HomeAssistantAdapter` - not
#: redefined or reinterpreted here.
_SOURCE_EVENT_TYPE = "device_state_changed"

#: P0's own raw relay event - UNCHANGED.
OUTPUT_EVENT_TYPE = "camera_automation.state_changed"

#: P0.5's new, normalized-`CameraEvent` output event - distinctly named,
#: never collides with `OUTPUT_EVENT_TYPE` above.
CAMERA_EVENT_TYPE = "camera_automation.camera_event"


class CameraAutomationModule(Module):
    name = "camera_automation"
    dependencies: List[str] = []

    def __init__(self, config: Optional[CameraAutomationConfig] = None) -> None:
        self._config = config or CameraAutomationConfig.from_env()
        self._event_bus: Any = None
        self._lock = threading.RLock()

        #: dedupe/cooldown key -> last-seen "state" (raw new_state for
        #: the legacy relay path, `kind` for the classified path), for
        #: no-op re-fire dedupe. SHARED by both branches (P0.5 brief
        #: Section 11/13 - one dedupe/cooldown implementation, not two).
        self._last_state: Dict[Any, str] = {}
        #: same key space - monotonic deadline before which a change for
        #: that key is suppressed (cooldown). SHARED, same reason.
        self._cooldown_until: Dict[Any, float] = {}

        self._bus_sub_id: Optional[str] = None

        #: P0.5 - loaded from `self._config.cameras_path`, rebuilt by
        #: `reload_cameras()`. Empty by default (nothing configured
        #: ships with real entity ids - see `config/camera_automation.
        #: json`'s own committed content).
        self._camera_profiles: List[CameraProfile] = []
        self._entity_role_index: EntityRoleIndex = {}
        #: entities already logged once as "unknown" - Section 9's own
        #: "optionally produce a debug log" without spamming on repeat.
        self._logged_unknown_entities: Set[str] = set()

    # -- Module lifecycle ---------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        if not self._config.enabled:
            # §10 - disabled means zero footprint: no subscription at all.
            return
        self.reload_cameras()
        if self._event_bus is not None:
            self._bus_sub_id = self._event_bus.subscribe(_SOURCE_EVENT_TYPE, self._on_device_state_changed)

    def is_enabled(self) -> bool:
        """P0.5.3 (Vision Event -> Camera Automation Bridge) - the ONE
        new read-only accessor this sprint adds, mirroring `connection_
        state()` in `real_camera_ptz.py`/`camera_status()` in
        `vision.py`'s own "small public accessor for private state"
        convention. Lets an EXTERNAL bridge module (see `vision_bridge.
        py`) decide whether to even subscribe to its own upstream events
        at `start()` time - the same "disabled means zero footprint"
        contract this module's own `start()` already follows (P0 brief
        §10) - without reaching into `self._config` directly from
        outside this class."""
        return self._config.enabled

    def ingest_external_camera_event(self, camera_event: CameraEvent) -> bool:
        """P0.5.3 (Vision Event -> Camera Automation Bridge) - the ONE
        new entry point for a `CameraEvent` that did NOT originate from
        `device_state_changed`/`_entity_role_index` classification (e.g.
        one built by `vision_bridge.VisionCameraEventBridge` from an
        EXISTING `luno.adapters.vision.VisionAdapter` event). Routes
        through the EXACT SAME shared dedupe/cooldown
        (`_publish_if_not_suppressed`) the HA-sourced classified branch
        above already uses - same key space, `(camera_id, kind)` - so
        this sprint adds no second deduplication/cooldown implementation
        (P0.5.3 brief Section 9) and a human_detected for a given
        `camera_id` from Vision correctly dedupes/cools down against one
        from HA for the SAME `camera_id`/`kind`, should both ever be
        configured at once.

        Respects the SAME feature flag as every other entry point
        (`self._config.enabled`, §12) - returns `False` without
        publishing anything when disabled, so callers never need their
        own separate flag check. Defensively wrapped exactly like `_on_
        device_state_changed` - never raises; a caller failure is still
        the caller's own responsibility to isolate (see `vision_bridge.
        py`'s own docstring), but this module's half of that contract is
        enforced here too, belt-and-suspenders."""
        if not self._config.enabled:
            return False
        try:
            self._publish_if_not_suppressed(
                key=(camera_event.camera_id, camera_event.kind), state=camera_event.kind,
                publish=lambda: self._event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=camera_event.to_dict())),
                # P0.8.8 - see `_publish_if_not_suppressed()`'s own
                # docstring: `state` here is part of `key`, so
                # suppression must be purely cooldown-based.
                dedupe_identical=False,
            )
            return True
        except Exception as ex:  # pragma: no cover - defensive, see module docstring
            log(f"ingest_external_camera_event failed (isolated, ignored): {ex}", self.name)
            return False

    def reload_cameras(self) -> None:
        """P0.5 - reads `config/camera_automation.json` FRESH (same
        "reloadable without a restart" precedent `AutomationEngine.
        reload_rules()` already established). Safe to call at any time,
        including while running - a malformed file simply results in
        zero configured cameras, never an exception (see `cameras.
        load_camera_profiles`'s own docstring)."""
        with self._lock:
            self._camera_profiles = load_camera_profiles(self._config.cameras_path)
            self._entity_role_index = build_entity_role_index(self._camera_profiles)

    def stop(self) -> None:
        """Never leaves a subscription orphaned. Exceptions here are
        swallowed - a broken `stop()` must never block shutdown of the
        rest of the system (same contract every other `Module.stop()` in
        this project follows, e.g. `AutomationEngine.stop()`)."""
        try:
            if self._event_bus is not None and self._bus_sub_id is not None:
                self._event_bus.unsubscribe(self._bus_sub_id)
        except Exception as ex:  # pragma: no cover - defensive
            log(f"stop() failed to unsubscribe from event bus (ignored): {ex}", self.name)
        self._bus_sub_id = None

    def health(self) -> ModuleHealthStatus:
        with self._lock:
            n_tracked = len(self._last_state)
            n_cameras = len(self._camera_profiles)
        return ModuleHealthStatus(
            healthy=True,
            message=(
                f"enabled={self._config.enabled}, allowlisted={len(self._config.entities)}, "
                f"cameras={n_cameras}, tracked={n_tracked}"
            ),
        )

    # -- event handling ---------------------------------------------------

    def _on_device_state_changed(self, event: Event) -> None:
        """§11 fail-safe boundary - this is the ONLY entry point an
        exception could originate from in this module during normal
        operation, and it can never propagate past this method."""
        try:
            self._handle(event)
        except Exception as ex:  # pragma: no cover - defensive, see module docstring
            log(f"camera automation handler failed (isolated, ignored): {ex}", self.name)

    def _handle(self, event: Event) -> None:
        entity_id = event.get("entity_id")
        if not entity_id:
            return

        new_state = event.get("new_state")
        old_state = event.get("old_state")

        # P0.5 - a configured CameraProfile entity always takes this
        # branch, whether or not THIS PARTICULAR transition happens to
        # classify into a CameraEvent (e.g. a camera_entity role's own
        # state moving between two ordinary values classifies to
        # nothing - see cameras.py) - it must never fall through to be
        # treated as "unknown" below just because this one transition
        # produced no event.
        if entity_id in self._entity_role_index:
            camera_event = classify_state_change(self._entity_role_index, entity_id, old_state, new_state)
            if camera_event is not None:
                self._publish_if_not_suppressed(
                    key=(camera_event.camera_id, camera_event.kind), state=camera_event.kind,
                    publish=lambda: self._event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=camera_event.to_dict())),
                    # P0.8.8 - `state` here IS part of `key` (see
                    # `_publish_if_not_suppressed()`'s own docstring) -
                    # suppression must be purely cooldown-based, never a
                    # permanently-true identical-state check.
                    dedupe_identical=False,
                )
            return

        # P0 (unchanged) - the flat allowlist raw relay.
        if entity_id in self._config.entities:
            self._publish_if_not_suppressed(
                key=entity_id, state=new_state,
                publish=lambda: self._event_bus.publish(Event(
                    type=OUTPUT_EVENT_TYPE, data={"entity_id": entity_id, "old_state": old_state, "new_state": new_state},
                )),
            )
            return

        # Neither a configured CameraProfile entity nor the flat
        # allowlist - Section 9: ignored safely, logged once per entity.
        if entity_id not in self._logged_unknown_entities:
            self._logged_unknown_entities.add(entity_id)
            log(f"ignoring device_state_changed for unconfigured entity {entity_id!r} (debug, logged once)", self.name)

    def _publish_if_not_suppressed(
        self, key: Any, state: Optional[str], publish, dedupe_identical: bool = True,
    ) -> None:
        """The ONE shared dedupe/cooldown implementation both the
        classified and legacy relay paths use (P0.5 brief Section 11/13
        - never duplicated). `key` is `(camera_id, kind)` for a
        classified `CameraEvent`, or the raw `entity_id` for the legacy
        relay - the two key spaces never collide (tuple vs. str).

        P0.8.8 (Camera Automation Event Suppression Bug Fix) - `dedupe_
        identical` distinguishes two genuinely different kinds of caller:

        `dedupe_identical=True` (default - the legacy relay path,
        `_handle()`'s `entity_id in self._config.entities` branch,
        UNCHANGED behavior from every prior sprint): `state` is a real,
        independently-varying value (`new_state`, e.g. "on"/"off") that
        is NOT derived from `key`. Here "the same state fired again" is
        a genuine no-op (nothing actually changed) and must be
        suppressed regardless of cooldown (`tests/test_p0_camera_
        automation.py::test_09_dedupe_suppresses_identical_repeat_state`,
        cooldown_s=0.0, pinned since P0) - and a GENUINE transition
        (a different `state`) arriving before cooldown has elapsed is
        still rate-limited by the cooldown check that follows
        (`test_10_cooldown_suppresses_rapid_changes`, also pinned).

        `dedupe_identical=False` (the two classified-`CameraEvent` call
        sites below - `_handle()`'s `entity_id in self._entity_role_
        index` branch and `ingest_external_camera_event()`): `state` is
        `camera_event.kind`, which is PART OF `key` itself (`key =
        (camera_id, kind)`, `state = kind`) - for a fixed `key`, `state`
        is a compile-time constant, so `self._last_state.get(key) ==
        state` is trivially True for every call after the very first
        ever successful publish, for the remaining lifetime of this
        module instance. There is no real "did the value change" question
        to ask here at all - each classified event (a new `camera_
        person_entered`/`human_confirmed`/etc. detection cycle) is its
        own independently meaningful occurrence, not a continuously-
        tracked state. The confirmed production bug: this permanently-
        true equality check made the SAME (no-cooldown-involved) `if
        ... == state: return` branch fire on literally every subsequent
        call, forever, so the cooldown check below it was DEAD CODE for
        every classified event - `camera_human_detected_test_action`
        (the real WLED-triggering rule) could only ever be reached ONCE
        per `camera_id` per process lifetime, explaining why only a
        module restart (observed in production logs as coinciding with a
        camera disconnect/reconnect cycle, which happens to tear down and
        recreate the in-memory Vision/camera pipeline state) ever
        "un-stuck" it. With `dedupe_identical=False`, the state-equality
        gate is skipped entirely for these two call sites - suppression
        is governed PURELY by `_cooldown_until` (a real, resettable,
        monotonic-time deadline), matching the module's own long-
        standing `cooldown_s`-based anti-spam contract instead of an
        accidental permanent lock. `_last_state[key]` is still recorded
        (harmless, and `health()` still reports `len(self._last_state)`
        for observability) - it is simply never consulted as a gate for
        these two call sites."""
        with self._lock:
            now = time.monotonic()
            if dedupe_identical and self._last_state.get(key) == state:
                return  # no-op re-fire: identical value, independent of cooldown
            if now < self._cooldown_until.get(key, 0.0):
                return  # within cooldown window - suppressed regardless of state
            self._last_state[key] = state
            self._cooldown_until[key] = now + self._config.cooldown_s

        if self._event_bus is not None:
            publish()
