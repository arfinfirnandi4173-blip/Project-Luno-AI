"""
vision_bridge.py
=================

LUNO P0.5.3 (Vision Event -> Camera Automation Bridge).

`VisionCameraEventBridge` - a THIN, additive `Module` that subscribes to
FOUR of the ALREADY-EXISTING, ALREADY-PUBLISHED `luno.adapters.vision.
VisionAdapter` Event Bus events (`camera_person_entered`/
`camera_person_left`/`camera_disconnected`/`camera_reconnected` - see
`luno/adapters/events.py`'s own `CameraPersonEntered`/`CameraPersonLeft`/
`CameraDisconnected`/`CameraReconnected` dataclasses, read directly from
that file and from `luno/adapters/vision.py`'s own publish call sites
before writing this bridge - P0.5.3 brief Section 3's own "do not assume
these names are exactly correct, read the actual implementation"
instruction), normalizes each into the EXISTING `camera_automation.
cameras.CameraEvent` model (P0/P0.5), and hands it to the EXISTING
`CameraAutomationModule.ingest_external_camera_event()` (the ONE new
method that module gained this sprint - see its own docstring for why
that is the safest integration point: it reuses the SAME dedupe/cooldown
the HA-sourced classification path already uses, rather than this bridge
inventing a second one).

This file contains NO computer vision, NO YOLO/OpenCV/RTSP code, and NO
Home Assistant code. `VisionAdapter`, `luno.vision`, `luno.
vision_tracking`, `luno.vision_human_state`, `luno.ha_client`, and every
other piece of "existing protected infrastructure" the P0.5.3 brief's
own Section 1 lists are NOT imported, NOT touched, and NOT modified by
this module - it only ever subscribes to events they already publish.

--------------------------------------------------------------------
Event mapping (P0.5.3 brief Section 4)
--------------------------------------------------------------------
    CameraPersonEntered   -> CameraEvent(kind="human_detected")
    CameraPersonLeft      -> CameraEvent(kind="human_cleared")
    CameraDisconnected    -> CameraEvent(kind="camera_offline")
    CameraReconnected     -> CameraEvent(kind="camera_online")
    HumanPresenceConfirmed   -> CameraEvent(kind="human_confirmed")    (P0.8.6)
    HumanPresenceUnconfirmed -> CameraEvent(kind="human_unconfirmed")  (P0.8.6)

P0.8.6 note: `human_confirmed`/`human_unconfirmed` are DELIBERATELY a
SEPARATE `kind`, not a second publish of `human_detected` carrying a new
field. `CameraAutomationModule._publish_if_not_suppressed()`'s existing
dedupe key is `(camera_id, kind)` - re-publishing `human_detected` a
second time (once confirmed) would compare equal to the FIRST
`human_detected` publish's own dedupe state and be silently dropped,
meaning the WLED-triggering rule would never see a confirmed event at
all. A distinct `kind` sidesteps this entirely, using the EXACT SAME
existing dedupe mechanism (no new one invented) with its own,
independent key. `human_detected`/`human_cleared` themselves, and every
rule listening to them (`camera_human_detected_log`, `camera_multiple_
people_log`), are completely unaffected - still fire on the raw,
unconfirmed presence signal, exactly as before P0.8.6, preserving full
observability of every detection (including low-confidence ones the
debug viewer already shows).

Why `CameraPersonEntered`/`CameraPersonLeft` and NOT `HumanEntered`/
`HumanLeft` (both exist - see `events.py`): `VisionAdapter` publishes
TWO different "a person is present" signal families. `HumanEntered`/
`HumanLeft` fire PER TRACKED INDIVIDUAL (tracking_id) - with two people
in frame, that is two independent HumanEntered events and no single
"is anyone here" boolean; turning that into one canonical human_detected/
human_cleared pair per `camera_id` would require this bridge to
re-implement its own presence-counting/debounce state, which is exactly
the "do not duplicate P0 deduplication" the brief's Section 9 forbids -
it would be a SECOND aggregation layer, not a thin bridge.
`CameraPersonEntered`/`CameraPersonLeft` are already the debounced,
ROOM-LEVEL, hysteresis-protected ("a momentary miss never generates a
spurious enter/leave pair" - see their own docstrings in `events.py`)
signal - exactly the granularity `human_detected`/`human_cleared`
already has for the HA-sourced path (one binary_sensor, one on/off
state). This is the "safest canonical mapping" the brief's own Section 4
asks for when multiple existing events represent the same semantic
state: reuse the one that is ALREADY at the right granularity, never
build a second one.

--------------------------------------------------------------------
Motion (P0.5.3 brief Section 5)
--------------------------------------------------------------------
The existing Vision event pipeline (`luno/adapters/events.py`, read in
full for this sprint) has no generic "motion" event at all - only
person/object detection (YOLO) and per-object tracking events. There is
no PIR-style or frame-differencing motion signal anywhere in
`VisionAdapter`/`RealVisionSource`/`luno.vision`. Per the brief's own
explicit instruction ("Do NOT fabricate motion_detected from human
detection... If it does not [exist]: report Motion source: NOT
AVAILABLE FROM EXISTING VISION EVENT PIPELINE. Do not invent one."),
this bridge NEVER publishes `motion_detected`/`motion_cleared` - see
`test_09_no_motion_fabrication_from_human_events` in this sprint's own
test file for the static/behavioral proof.

--------------------------------------------------------------------
Camera ID (P0.5.3 brief Section 6)
--------------------------------------------------------------------
`VisionAdapter`/`luno.vision` have NO camera-identity concept at all -
`luno.vision.camera_source()` is documented as "the ONE place that
decides what cv2.VideoCapture(...) opens", a single global source, never
plural. There is nothing to "reuse" as a stable id. Per the brief's own
"use the smallest safe adapter-level mapping... do not modify the Vision
core just to introduce a camera ID" instruction, this bridge assigns a
single, fixed, configurable `camera_id` at ITS OWN level (never touching
`luno.vision`/`VisionAdapter`) - `CAMERA_AUTOMATION_VISION_CAMERA_ID`
env var, defaulting to `"tapo_c212"` (matching the single profile key
already shipped, inert, in `config/camera_automation.json` since P0.5 -
the same physical camera this bridge's own events ultimately describe,
per P0.5.2's own architecture-map finding that Vision's RTSP source and
pytapo's PTZ target the same device).

--------------------------------------------------------------------
Confidence (P0.5.3 brief Section 7)
--------------------------------------------------------------------
None of the four subscribed events carry a confidence value (confirmed
by reading their publish call sites in `luno/adapters/vision.py`:
`CameraPersonEntered()`/`CameraPersonLeft()` are published with NO
`data=` at all; `CameraDisconnected`/`CameraReconnected` carry only
`source`/`error`). `confidence` is therefore always `None` on every
`CameraEvent` this bridge builds - never fabricated, and conveniently
already the ONLY value the HA-sourced classification path ever produces
too (see `cameras.py`'s own `CameraEvent.confidence` docstring: "ALWAYS
`None` in this sprint").

--------------------------------------------------------------------
Deduplication (P0.5.3 brief Section 9)
--------------------------------------------------------------------
This bridge implements NO dedupe/cooldown of its own - every incoming
Vision event is translated and handed to `CameraAutomationModule.
ingest_external_camera_event()` unconditionally; THAT method's existing
`_publish_if_not_suppressed()` (shared, unchanged, the exact same
dedupe/cooldown the HA-sourced path already uses) is what decides
whether a `camera_automation.camera_event` is actually published.

--------------------------------------------------------------------
Failure isolation (P0.5.3 brief Section 10)
--------------------------------------------------------------------
Every subscriber method here is wrapped in its own `try/except` that
logs and swallows - matching `CameraAutomationModule._on_device_state_
changed`'s own "this is the ONLY entry point an exception could
originate from, and it can never propagate past this method" contract,
applied to a second module. This is on top of (never a replacement for)
the Event Bus's own existing per-subscriber isolation.

--------------------------------------------------------------------
Lifecycle (P0.5.3 brief Section 11)
--------------------------------------------------------------------
A `Module` (same interface every other adapter implements) with
`dependencies = ["camera_automation"]` (starts after, stops before, the
module it feeds - same DFS-ordering `ModuleManager` already provides for
every other module, no new ordering mechanism). `start()`/`stop()`
subscribe/unsubscribe using the EXISTING Event Bus `subscribe()`/
`unsubscribe()` methods every other module already uses - no second
event loop, no polling thread, no scheduler.

--------------------------------------------------------------------
Feature flag (P0.5.3 brief Section 12)
--------------------------------------------------------------------
`start()` checks `camera_automation_module.is_enabled()` FIRST and
returns without subscribing to anything at all if `camera_automation.
enabled` is `False` - literally zero runtime footprint (no subscription)
when disabled, the same contract `CameraAutomationModule.start()` itself
already follows. Even in the hypothetical case this check were somehow
bypassed, `ingest_external_camera_event()` re-checks the SAME flag
itself and returns `False` without publishing - two independent layers,
same "safe by default" precedent `CameraAutomationConfig`'s own module
docstring already established for entities/cameras_path.

--------------------------------------------------------------------
No automation rules (P0.5.3 brief Section 14)
--------------------------------------------------------------------
This file never reads or writes `config/automation_rules.json`, never
calls a Home Assistant service, never contains any "if X then Y" rule
logic, and never imports `AutomationEngine`. It only ever calls
`CameraAutomationModule.ingest_external_camera_event()` - transport, not
automation.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from ..adapters.events import (
    CameraDisconnected,
    CameraPersonEntered,
    CameraPersonLeft,
    CameraReconnected,
    HumanPresenceConfirmed,
    HumanPresenceUnconfirmed,
)
from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import log
from .cameras import CameraEvent
from .module import CameraAutomationModule
from .vision_context import build_vision_context

#: P0.7 - the existing, generic `SystemError` event type
#: (`luno/core/events.py`) that `RealVisionSource._poll_once()`/`_tracked_
#: cycle_once()` already publish (P0.6.2-FIX/P0.6.3) when a detector
#: failure occurs. This bridge subscribes to it PURELY to track "is
#: Vision detection currently failing" for `VisionContext.detection_
#: error` (Section 5/12: a detector failure must never silently become
#: `human_present=False`) - it is filtered down to only the Vision-
#: detector-failure shape (`error_type == "vision_detection_failed"`),
#: never treated as a generic error monitor.
_SYSTEM_ERROR_EVENT_TYPE = "system_error"

#: P0.5.3 brief Section 6 - see module docstring. A single, fixed,
#: configurable camera id (this bridge's own tiny, self-contained
#: `*_env()` reader - matching `CameraAutomationConfig.from_env()`'s own
#: "every subsystem owns its own env reading" convention, never adding a
#: field to that dataclass itself since this is a distinct input path).
_DEFAULT_VISION_CAMERA_ID = "tapo_c212"


def _default_camera_id_from_env() -> str:
    raw = os.getenv("CAMERA_AUTOMATION_VISION_CAMERA_ID", "").strip()
    return raw or _DEFAULT_VISION_CAMERA_ID


#: The four upstream event TYPE STRINGS this bridge subscribes to -
#: taken directly from the dataclasses' own `EVENT_TYPE`/`type` defaults
#: in `luno/adapters/events.py` (Section 3's own "read the actual
#: implementation, do not assume the names" instruction) rather than
#: retyped string literals that could silently drift from the real
#: values.
_PERSON_ENTERED_EVENT_TYPE = CameraPersonEntered.EVENT_TYPE
_PERSON_LEFT_EVENT_TYPE = CameraPersonLeft.EVENT_TYPE
_CAMERA_DISCONNECTED_EVENT_TYPE = CameraDisconnected.EVENT_TYPE
_CAMERA_RECONNECTED_EVENT_TYPE = CameraReconnected.EVENT_TYPE
#: P0.8.6 - two NEW upstream events (see `luno/adapters/events.py`'s own
#: `HumanPresenceConfirmed`/`HumanPresenceUnconfirmed` docstrings), the
#: SAME "read the actual EVENT_TYPE, never retype a string literal"
#: discipline as the four above.
_PERSON_CONFIRMED_EVENT_TYPE = HumanPresenceConfirmed.EVENT_TYPE
_PERSON_UNCONFIRMED_EVENT_TYPE = HumanPresenceUnconfirmed.EVENT_TYPE


class VisionCameraEventBridge(Module):
    name = "vision_camera_event_bridge"
    #: Starts after, stops before, `CameraAutomationModule` - see module
    #: docstring's "Lifecycle" section. `ModuleManager` already provides
    #: this ordering for every module via its existing DFS dependency
    #: resolver; no new mechanism.
    dependencies: List[str] = ["camera_automation"]

    def __init__(self, camera_automation: CameraAutomationModule, camera_id: Optional[str] = None) -> None:
        self._camera_automation = camera_automation
        self._camera_id = camera_id or _default_camera_id_from_env()
        self._event_bus: Any = None
        self._sub_ids: List[str] = []
        #: Observability only (Section 19's own "state exactly whether
        #: real events were observed" requirement) - counts, never used
        #: for any dedupe/control-flow decision (that stays entirely
        #: inside `CameraAutomationModule`).
        self._events_received = 0
        self._events_ingested = 0
        #: P0.7 - OPTIONAL, plain public attribute (not a constructor
        #: parameter - matches this project's own existing "wired post-
        #: construction, once both modules AND adapters exist" convention,
        #: e.g. `luno/bootstrap/adapters.py::register_device_intent_
        #: classifier()`'s `planner_module.device_intent_client = ...`).
        #: A zero-argument callable returning the SAME public Vision
        #: status snapshot `luno/dashboard/collectors.py::collect_
        #: vision()` already reads (`adapter_manager.status_all().get(
        #: "vision")`) - wired by `luno.bootstrap.adapters.register_
        #: vision_context_reader()`, called from `main.py` for the same
        #: "needs output from BOTH register_all_modules() and register_
        #: all_adapters()" reason those other post-hoc wiring functions
        #: already exist. Left `None` by default - `_ingest()` below
        #: degrades to `vision_context.build_vision_context()`'s own
        #: honest "unavailable, nothing known" default rather than
        #: raising when it is (test/no-adapter-manager contexts, or
        #: simply not wired yet).
        self.vision_status_reader: Optional[Callable[[], Dict[str, Any]]] = None
        #: P0.7 (Section 5/12) - the most recently observed Vision
        #: detector failure (from the EXISTING `system_error`/`vision_
        #: detection_failed` signal - see `_on_system_error` below),
        #: threaded into every `VisionContext` this bridge builds until
        #: the NEXT successful ingest clears it. Never influences
        #: `human_present`/`person_count` itself (those come from the
        #: dashboard's own last-known-good tracked-cycle snapshot,
        #: unaffected by a presence-loop or tracked-loop failure in a
        #: DIFFERENT cycle - see `vision_context.py`'s own docstring).
        self._last_detection_error: Optional[str] = None

    # -- Module lifecycle ---------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        # Section 12 - zero footprint when camera_automation is disabled,
        # same contract CameraAutomationModule.start() itself follows.
        if not self._camera_automation.is_enabled():
            return
        if self._event_bus is None:
            return
        self._sub_ids = [
            self._event_bus.subscribe(_PERSON_ENTERED_EVENT_TYPE, self._on_person_entered),
            self._event_bus.subscribe(_PERSON_LEFT_EVENT_TYPE, self._on_person_left),
            self._event_bus.subscribe(_CAMERA_DISCONNECTED_EVENT_TYPE, self._on_camera_disconnected),
            self._event_bus.subscribe(_CAMERA_RECONNECTED_EVENT_TYPE, self._on_camera_reconnected),
            # P0.7 - see module-level `_SYSTEM_ERROR_EVENT_TYPE` comment.
            self._event_bus.subscribe(_SYSTEM_ERROR_EVENT_TYPE, self._on_system_error),
            # P0.8.6 - see module-level `_PERSON_CONFIRMED_EVENT_TYPE`
            # comment and this file's own module docstring Section 3
            # (added below) for why these are a SEPARATE `kind`, not a
            # second publish of `human_detected`.
            self._event_bus.subscribe(_PERSON_CONFIRMED_EVENT_TYPE, self._on_person_confirmed),
            self._event_bus.subscribe(_PERSON_UNCONFIRMED_EVENT_TYPE, self._on_person_unconfirmed),
        ]

    def stop(self) -> None:
        """Never leaves a subscription orphaned - same `stop()` contract
        every other module in this project follows (exceptions logged
        and swallowed, never block shutdown)."""
        for sub_id in self._sub_ids:
            try:
                if self._event_bus is not None:
                    self._event_bus.unsubscribe(sub_id)
            except Exception as ex:  # pragma: no cover - defensive
                log(f"stop() failed to unsubscribe from event bus (ignored): {ex}", self.name)
        self._sub_ids = []

    def health(self) -> ModuleHealthStatus:
        return ModuleHealthStatus(
            healthy=True,
            message=(
                f"camera_id={self._camera_id!r}, subscribed={len(self._sub_ids) > 0}, "
                f"events_received={self._events_received}, events_ingested={self._events_ingested}, "
                f"vision_context_reader_wired={self.vision_status_reader is not None}"
            ),
        )

    # -- event handling (Section 10 - each isolated) -------------------------

    def _on_person_entered(self, event: Event) -> None:
        self._ingest("human_detected", event, _PERSON_ENTERED_EVENT_TYPE)

    def _on_person_left(self, event: Event) -> None:
        self._ingest("human_cleared", event, _PERSON_LEFT_EVENT_TYPE)

    def _on_camera_disconnected(self, event: Event) -> None:
        self._ingest("camera_offline", event, _CAMERA_DISCONNECTED_EVENT_TYPE)

    def _on_camera_reconnected(self, event: Event) -> None:
        self._ingest("camera_online", event, _CAMERA_RECONNECTED_EVENT_TYPE)

    def _on_person_confirmed(self, event: Event) -> None:
        self._ingest("human_confirmed", event, _PERSON_CONFIRMED_EVENT_TYPE)

    def _on_person_unconfirmed(self, event: Event) -> None:
        self._ingest("human_unconfirmed", event, _PERSON_UNCONFIRMED_EVENT_TYPE)

    def _on_system_error(self, event: Event) -> None:
        """P0.7 (Section 5/12) - tracks the most recent Vision detector
        failure so it can be threaded into the NEXT `VisionContext` this
        bridge builds. Filters out every OTHER `system_error` (a
        different adapter, an unrelated module crash) - this bridge's
        job is Vision Context, not a general error monitor. Isolated
        exactly like every other handler here (Section 10)."""
        try:
            data = event.data or {}
            if data.get("adapter") == "vision" and data.get("error_type") == "vision_detection_failed":
                self._last_detection_error = str(data.get("error") or "vision_detection_failed")
        except Exception as ex:  # pragma: no cover - defensive, see module docstring
            log(f"vision camera event bridge failed handling system_error (isolated, ignored): {ex}", self.name)

    def _ingest(self, kind: str, source_event: Event, source_event_type: str) -> None:
        """Section 10's own isolation boundary - the ONLY place an
        exception could originate from during normal operation, and it
        can never propagate past this method into the Event Bus's own
        dispatch loop (belt-and-suspenders on top of the Bus's existing
        per-subscriber isolation)."""
        try:
            self._events_received += 1

            # P0.7 (Vision Context -> Automation Context) - a pure,
            # synchronous snapshot built ONLY when one of these four
            # already-existing events fires (never a new polling-driven
            # event - see vision_context.py's own module docstring,
            # Section 9). Reader may be unwired (tests, or main.py
            # hasn't reached register_vision_context_reader() yet) -
            # build_vision_context() degrades to its own honest
            # "unavailable, nothing known" default rather than raising.
            status = self.vision_status_reader() if self.vision_status_reader is not None else None
            context = build_vision_context(
                camera_id=self._camera_id, status=status, detection_error=self._last_detection_error,
            )
            # This ingest reached a real event successfully - whatever
            # detector failure was previously observed no longer
            # necessarily applies to right now (Section 12: a failure
            # must not linger forever once detection is clearly working
            # again). Cleared AFTER being read into `context` above, so
            # THIS event still honestly reports it.
            self._last_detection_error = None

            camera_event = CameraEvent(
                camera_id=self._camera_id,
                kind=kind,
                # No real HA entity_id exists for a Vision-sourced event -
                # Section 6/8: never fabricate one that LOOKS like a real
                # HA entity_id (e.g. "camera.tapo_c212"). This value is
                # honestly non-HA, while still traceable to the exact
                # upstream event type that produced it.
                entity_id=f"vision:{source_event_type}",
                # Vision has no HA-style continuous state string (only
                # discrete Entered/Left/Disconnected/Reconnected events) -
                # Section 7/8: never invent an on/off-style value that
                # would misleadingly imply HA semantics.
                old_state=None,
                new_state=None,
                confidence=None,  # Section 7 - never fabricated, see module docstring.
                timestamp=time.time(),
                source="vision",
                # P0.7 - additive Vision Context fields (see cameras.py's
                # own docstring for why these are safe/backward-compatible
                # additions to the shared CameraEvent shape).
                human_present=context.human_present,
                person_count=context.person_count,
                detected_objects=context.detected_objects,
                available=context.available,
                detection_error=context.detection_error,
                human_confirmed=context.human_confirmed,
            )
            if self._camera_automation.ingest_external_camera_event(camera_event):
                self._events_ingested += 1
        except Exception as ex:  # pragma: no cover - defensive, see module docstring
            log(f"vision camera event bridge failed handling {source_event_type!r} (isolated, ignored): {ex}", self.name)
