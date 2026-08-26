"""
cameras.py
==========

LUNO P0.5 (Real Camera Integration) - the generic `CameraIntegration ->
CameraEvent` normalization layer the brief calls for, and the "Camera
Integration Adapter" box in that brief's own architecture diagram:

    Tapo C212 -> Home Assistant -> Camera Integration Adapter ->
    Luno Camera Automation Core -> Existing Event Bus / AutomationEngine

This file is pure, stateless translation logic plus one small,
defensive config loader (`load_camera_profiles`) - no Home Assistant
connection, no Event Bus, no Module lifecycle. `CameraAutomationModule`
(in `module.py`) is the only caller; it owns the actual Event Bus
subscription, dedupe, and cooldown, exactly as P0 already did (this file
does not duplicate any of that - see Section 13 of the P0.5 brief).

--------------------------------------------------------------------
`CameraProfile` - the generic "CameraIntegration" concept
--------------------------------------------------------------------
The brief asks for a generic `CameraIntegration -> CameraEvent` concept
with "Tapo C212 should be one implementation/provider" of it. In
practice, every camera this project would ever integrate through Home
Assistant shares the exact same shape: HOME ASSISTANT has ALREADY
abstracted the vendor's own protocol (Tapo's, or anyone else's) into
generic `binary_sensor`/`camera` entities and plain on/off/unavailable
states BEFORE Luno ever sees them - `HomeAssistantAdapter.
on_state_changed()` (untouched since P0/Sprint 71) carries only
`entity_id`/`old_state`/`new_state`, nothing vendor-specific.

So "Tapo C212 as one provider" does NOT mean a `TapoC212Adapter` class
containing Tapo-specific protocol code (there is none to write - that
would be fabricated, dishonest capability, exactly what the P0 brief's
own "no invented capability" convention this project has followed since
Sprint 71 forbids). It means: `CameraProfile` IS the generic
`CameraIntegration` concept, and "Tapo C212" is realized as ONE
CONFIGURED INSTANCE of it (`camera_id="tapo_c212"`, entity roles pointed
at whatever the operator's real Home Assistant actually calls those
entities). A second camera brand exposed through the SAME Home Assistant
integration needs only a second `CameraProfile` entry - zero new code.

--------------------------------------------------------------------
`CameraEvent` - the normalized internal camera event
--------------------------------------------------------------------
One `CameraEvent` per classified HA state change:

    camera_id    - STABLE, operator-configured (e.g. "tapo_c212"),
                   never derived from a transient HA entity_id or event
                   id (P0.5 brief Section 7).
    kind         - one of `CAMERA_EVENT_KINDS` below.
    entity_id    - the actual HA entity that changed (for traceability).
    old_state/new_state - passed through unmodified from the existing
                   `device_state_changed` event.
    confidence   - ALWAYS `None` in this sprint. `HomeAssistantAdapter.
                   on_state_changed(entity_id, old_state, new_state)` -
                   the existing, protected inbound HA interface - has no
                   attributes/confidence parameter at all; extracting a
                   real confidence value would require changing that
                   interface's own signature, which is explicitly
                   protected infrastructure this sprint must not modify
                   (P0.5 brief Section 1/17: "if a safe integration
                   cannot be achieved without a significant core
                   modification, STOP... do not rewrite the core").
                   Per Section 10's own instruction, "do not invent
                   confidence values" - so this is a documented, honest
                   limitation, not a fabricated number.
    timestamp    - wall-clock time this classification happened.
    source       - the `camera_id` this event's profile belongs to
                   (kept as its own field, distinct from `camera_id`,
                   in case a future sprint adds a non-1:1 source, e.g.
                   one HA entity shared by two logical cameras).

--------------------------------------------------------------------
Classification rules (Sections 5/10/11/12 of the P0.5 brief)
--------------------------------------------------------------------
Each `CameraProfile` declares up to four entity roles - `camera_entity`,
`motion_entity`, `human_entity`, `availability_entity` - all optional
(a profile with only a `motion_entity` configured is valid; it simply
never produces human/availability events). `classify_state_change()`
looks up which profile+role an incoming `entity_id` belongs to and
returns the matching `CameraEvent`, or `None` if the entity is not part
of any configured profile (Section 9 - unknown entities are ignored
safely, never crash, never trigger automation).

Motion/human roles: HA's own binary_sensor convention is `"on"` =
detected, `"off"` = cleared - any OTHER state (`"unavailable"`,
`"unknown"`) is treated as a NON-event for that role here (neither
`*_detected` nor `*_cleared`); an unavailable motion/human sensor is
covered instead by the availability classification below, so the
distinction the brief's Section 12 requires ("camera unavailable" is
NOT "no motion") is preserved rather than collapsed into a false
`motion_cleared`.

Availability role: if a profile has an explicit `availability_entity`
configured (HA's own `binary_sensor` "connectivity" device-class
convention: `"on"` = connected/available, `"off"` = disconnected), that
entity's state is authoritative for `camera_online`/`camera_offline`.
If no `availability_entity` is configured, this file falls back to Home
Assistant's own DOCUMENTED, GENERIC "entity became unavailable"
convention (any entity's `state` can independently become the literal
string `"unavailable"`/`"unknown"` when its own integration loses
contact with the device, regardless of domain) applied to whichever of
`camera_entity`/`motion_entity`/`human_entity` IS configured, in that
preference order - honest reuse of a real, documented HA behavior, not
an invented one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..core.utils import log

#: The complete, closed set of normalized camera event kinds this
#: sprint supports (P0.5 brief Section 5's own worked example list).
#: Deliberately closed, not open-ended - matches this project's
#: existing "no unbounded/free-form kind field" convention (see
#: `luno/automation/models.py`'s own closed `ACTION_TYPES`/
#: `TRIGGER_TYPES` sets for the same rationale).
CAMERA_EVENT_KINDS = frozenset({
    "motion_detected", "motion_cleared",
    "human_detected", "human_cleared",
    "camera_online", "camera_offline",
})

_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", None})
_ON_STATES = frozenset({"on", "detected", "true"})
_OFF_STATES = frozenset({"off", "clear", "cleared", "false"})


@dataclass(frozen=True)
class CameraProfile:
    """The generic `CameraIntegration` concept (P0.5 brief Section 3) -
    a stable `camera_id` plus whichever HA entity roles are actually
    configured for it. Every field except `camera_id` is optional: a
    profile is not required to expose all four roles."""

    camera_id: str
    camera_entity: Optional[str] = None
    motion_entity: Optional[str] = None
    human_entity: Optional[str] = None
    availability_entity: Optional[str] = None

    def entities(self) -> List[str]:
        """Every HA entity_id this profile cares about, in a stable
        order - used both to build the fast entity->role index below
        and by callers that need the flat set (e.g. a dashboard)."""
        return [e for e in (self.camera_entity, self.motion_entity, self.human_entity, self.availability_entity) if e]


@dataclass(frozen=True)
class CameraEvent:
    camera_id: str
    kind: str
    entity_id: str
    old_state: Optional[str]
    new_state: Optional[str]
    confidence: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    #: P0.7 (Vision Context -> Automation Context) - OPTIONAL, additive
    #: fields, all defaulting to values that mean "unknown/not provided"
    #: - the HA-sourced classification path (`classify_state_change()`
    #: below) never sets these and is 100% unaffected (every existing
    #: caller/test that only ever saw the original 8 keys keeps seeing
    #: exactly those 8 keys' values unchanged; `to_dict()` merely gains
    #: 4 new keys no existing reader is required to look at). Only
    #: `VisionCameraEventBridge` (`vision_bridge.py`) ever populates
    #: these, from `vision_context.build_vision_context()` - see that
    #: module's own docstring for the full field-by-field rationale.
    #: Lets an `AutomationEngine` condition read `event.human_present`/
    #: `event.person_count`/`event.detected_objects`/`event.available`
    #: via the EXISTING P0.6 `event.<field>` mechanism - no second
    #: condition engine, no new event type.
    human_present: Optional[bool] = None
    person_count: Optional[int] = None
    detected_objects: Tuple[str, ...] = ()
    available: Optional[bool] = None
    detection_error: Optional[str] = None
    #: P0.8.6 - additive, same "defaults to unknown/not provided, every
    #: existing caller/test unaffected" convention as the four P0.7
    #: fields above. Threaded through from `vision_context.VisionContext.
    #: human_confirmed` by `VisionCameraEventBridge` on EVERY CameraEvent
    #: it builds (not just `kind="human_confirmed"`/`"human_unconfirmed"`
    #: ones) - lets any rule/observer see the current confirmation state
    #: alongside a `human_detected`/`human_cleared` event too, for
    #: richer debugging, without requiring it.
    human_confirmed: Optional[bool] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "kind": self.kind,
            "entity_id": self.entity_id,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "source": self.source,
            "human_present": self.human_present,
            "person_count": self.person_count,
            "detected_objects": self.detected_objects,
            "available": self.available,
            "detection_error": self.detection_error,
            "human_confirmed": self.human_confirmed,
        }


#: entity_id -> (CameraProfile, role_name). `role_name` is one of
#: "camera"/"motion"/"human"/"availability".
EntityRoleIndex = Dict[str, Tuple[CameraProfile, str]]


def build_entity_role_index(profiles: List[CameraProfile]) -> EntityRoleIndex:
    """Pure, deterministic. Later profiles win on a duplicate entity_id
    (should not happen with sane configuration; never raises either
    way - this project's own "malformed config degrades gracefully,
    never crashes startup" convention, matching `CameraPatrolModule.
    _load_routes()`'s own precedent)."""
    index: EntityRoleIndex = {}
    for profile in profiles:
        if profile.camera_entity:
            index[profile.camera_entity] = (profile, "camera")
        if profile.motion_entity:
            index[profile.motion_entity] = (profile, "motion")
        if profile.human_entity:
            index[profile.human_entity] = (profile, "human")
        if profile.availability_entity:
            index[profile.availability_entity] = (profile, "availability")
    return index


def _availability_kind_from_dedicated_entity(new_state: Optional[str]) -> str:
    """For a profile's own explicit `availability_entity` - HA's
    documented `binary_sensor` "connectivity" device-class convention:
    `"on"` = connected/available, everything else (`"off"`,
    `"unavailable"`, `"unknown"`) = offline."""
    state = (new_state or "").strip().lower()
    return "camera_online" if state in _ON_STATES else "camera_offline"


def _availability_kind_from_generic_fallback(new_state: Optional[str]) -> str:
    """No dedicated `availability_entity` configured - HA's own generic
    "entity became unavailable" convention applied to whichever role
    entity fired (see module docstring). A state that ISN'T the
    unavailable/unknown sentinel means the entity is reachable again -
    `camera_online`."""
    state = (new_state or "").strip().lower()
    return "camera_offline" if (state in _UNAVAILABLE_STATES or not state) else "camera_online"


def classify_state_change(
    index: EntityRoleIndex, entity_id: str, old_state: Optional[str], new_state: Optional[str],
) -> Optional[CameraEvent]:
    """The Camera Integration Adapter's own core function - HA-specific
    state change in, normalized `CameraEvent` (or `None`) out. Never
    raises (Section 16 fail-safe) - any unexpected shape simply fails to
    classify rather than propagating an exception; the caller
    (`CameraAutomationModule._handle`) still wraps this in its own
    try/except as defense in depth, unchanged from P0."""
    hit = index.get(entity_id)
    if hit is None:
        return None  # unknown entity - Section 9: ignored safely
    profile, role = hit
    state = (new_state or "").strip().lower()
    is_unavailable = state in _UNAVAILABLE_STATES or not state

    if role == "availability":
        kind = _availability_kind_from_dedicated_entity(new_state)
    elif role in ("motion", "human", "camera"):
        if is_unavailable:
            if profile.availability_entity:
                # An explicit availability_entity exists for this profile
                # and is authoritative - avoid double-reporting offline
                # from every other role's own entity going unavailable
                # in the same disconnect (they typically all do, at once).
                return None
            # No dedicated availability_entity - Section 12's own
            # fallback: this role's entity going unavailable IS the
            # offline signal for this profile (see module docstring).
            kind = _availability_kind_from_generic_fallback(new_state)
        elif role == "camera":
            return None  # camera_entity has no on/off detection semantics of its own
        elif role == "motion":
            if state in _ON_STATES:
                kind = "motion_detected"
            elif state in _OFF_STATES:
                kind = "motion_cleared"
            else:
                return None
        else:  # role == "human"
            if state in _ON_STATES:
                kind = "human_detected"
            elif state in _OFF_STATES:
                kind = "human_cleared"
            else:
                return None
    else:  # pragma: no cover - defensive, role_name is only ever set by build_entity_role_index above
        return None

    return CameraEvent(
        camera_id=profile.camera_id, kind=kind, entity_id=entity_id,
        old_state=old_state, new_state=new_state, confidence=None, source=profile.camera_id,
    )


def load_camera_profiles(path: str) -> List[CameraProfile]:
    """Reads `config/camera_automation.json` FRESH (same "reloadable
    without a restart, malformed input degrades gracefully" precedent
    `CameraPatrolModule._load_routes()`/`AutomationEngine.
    _load_rules_from_disk()` already established). Missing file, empty
    file, malformed JSON, or an individually malformed camera entry are
    ALL handled the same way - skip/log, never raise, never crash
    `Module.start()`. Expected shape:

        {"cameras": {"tapo_c212": {"camera_entity": "camera.tapo_c212",
                                    "motion_entity": "binary_sensor...",
                                    "human_entity": null,
                                    "availability_entity": null}}}

    A camera entry with every role `null`/absent is valid and simply
    never produces a `CameraEvent` (the file ships exactly like this by
    default - see `config/camera_automation.json`'s own committed
    content - no entity_id is ever fabricated)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as ex:
        log(f"load_camera_profiles: could not read {path!r} ({ex}) - treating as zero cameras configured", "camera_automation")
        return []

    cameras_raw = raw.get("cameras") if isinstance(raw, dict) else None
    if not isinstance(cameras_raw, dict):
        return []

    profiles: List[CameraProfile] = []
    for camera_id, entry in cameras_raw.items():
        if not isinstance(camera_id, str) or not camera_id.strip():
            continue
        if not isinstance(entry, dict):
            log(f"load_camera_profiles: skipping camera {camera_id!r} - entry is not an object", "camera_automation")
            continue

        def _entity(key: str) -> Optional[str]:
            value: Any = entry.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        profiles.append(CameraProfile(
            camera_id=camera_id,
            camera_entity=_entity("camera_entity"),
            motion_entity=_entity("motion_entity"),
            human_entity=_entity("human_entity"),
            availability_entity=_entity("availability_entity"),
        ))
    return profiles
