"""
vision_context.py
==================

LUNO P0.7 (Vision Context -> Automation Context).

A small, pure, additive normalization layer - takes the SAME public
Vision status snapshot the dashboard already reads
(`adapter_manager.status_all()["vision"]`, i.e. `VisionAdapter.
_extra_status()` merged with `BaseAdapter`'s own generic status fields -
see `luno/dashboard/collectors.py::collect_vision()`, the precedent this
file's own reader follows) and reduces it to the handful of fields
`AutomationEngine` conditions can usefully inspect via the existing
`event.<field>` mechanism (P0.6).

This file contains NO computer vision, NO YOLO/OpenCV/RTSP code, and NO
Event Bus subscription of its own - it is a pure function (`build_
vision_context`) plus one small, frozen dataclass (`VisionContext`).
`VisionCameraEventBridge` (this package's own existing integration
point, unchanged in role since P0.5.3) is the ONLY caller - it invokes
`build_vision_context()` once, synchronously, whenever one of the four
ALREADY-EXISTING Vision Adapter events it already subscribes to fires
(`CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected`) - see that file's own module docstring for why this
is deliberately NOT a new polling loop or a new high-frequency event
(P0.7 brief Section 9): `VisionContext` is STATE, computed on demand from
whatever the dashboard's own already-running tracked-cycle loop most
recently produced, never a second inference, never a second RTSP read.

--------------------------------------------------------------------
Schema (P0.7 brief Section 4) - adapted to this project's own existing
conventions, not copied blindly
--------------------------------------------------------------------
    camera_id         - str, same stable id every other CameraEvent in
                         this package already uses (P0.5 brief Section 7).
    timestamp          - float, wall-clock time this context was built
                         (`time.time()`, same convention `CameraEvent`
                         itself already uses).
    available          - Optional[bool] - `VisionAdapter._extra_status()
                         ["camera_connected"]` passed through unmodified.
                         `None` only if the status snapshot itself is
                         unavailable (e.g. no reader wired - see
                         `vision_bridge.py`).
    human_present       - bool - `person_count > 0`. Derived from the
                         SAME Sprint 8 tracked-cycle data the dashboard's
                         own `human_count`/`humans` fields already show
                         (`_known_humans`) - NOT the separate presence-
                         watch loop's own debounced `CameraPersonEntered`/
                         `CameraPersonLeft` boolean (that remains the
                         authoritative EVENT trigger, unchanged - see
                         P0.7 brief Section 11; this field is
                         supplementary richer state riding along on that
                         same event, not a replacement for it).
    person_count        - int - `status["human_count"]` passed through.
                         Only the tracked-cycle loop can distinguish "1
                         person" from "2 people" at all (the plain
                         presence-watch loop's own `detect_objects()`
                         only ever returns a label SET, no count) - using
                         `human_count` here, rather than re-deriving a
                         count from `objects`, reuses the one place in
                         the existing architecture that already computes
                         it correctly (per-track `HumanState` entries,
                         Sprint 8's own `ObjectTracker`/`HumanStateEstimator`).
    detected_objects     - Tuple[str, ...] - normalized (Section 6:
                         lowercased/stripped, duplicates removed, stable
                         sorted order), taken directly from `status
                         ["objects"]`'s own `label` fields (already
                         COCO-class names this project's existing
                         detector actually returns - see `luno/vision.py`
                         ::`_COCO_LABEL_ALIASES`/`_normalize_label()` for
                         the SEPARATE, existing cosmetic renaming this
                         file does not duplicate or second-guess; this
                         function only normalizes CASE/WHITESPACE, never
                         invents a new class name - P0.7 brief Section 6:
                         "do not invent object classes").
    detection_error      - Optional[str] - passed through from the
                         caller (P0.7 brief Section 5/12: "a detector
                         failure MUST NOT become human_present=false").
                         `VisionCameraEventBridge` is what actually
                         tracks this (via the EXISTING `system_error`/
                         `vision_detection_failed` signal P0.6.2-FIX/
                         P0.6.3 already established) - this module never
                         subscribes to anything itself, it only accepts
                         whatever error string the caller already knows
                         about and threads it through unchanged.

--------------------------------------------------------------------
State preservation on detector failure (P0.7 brief Section 5/12)
--------------------------------------------------------------------
`build_vision_context()` NEVER derives `human_present`/`person_count`
from the SAME cycle a `detection_error` is set for in a way that could
report a false "nobody" - when `detection_error` is provided, this
function does not zero out `human_present`/`person_count` itself; it
simply passes through whatever the (separately, honestly reported)
`status["human_count"]`/`status["objects"]` snapshot says, which is
ALREADY the dashboard's own "last known good" reading (Sprint 8's
tracked-cycle state is not reset just because the presence-watch loop -
a completely different loop - happened to fail this cycle; see
`luno/camera_automation/vision_bridge.py`'s own docstring for exactly
which loop's failure this `detection_error` field can even represent).
The caller is what decides whether a "previous valid state" needs to be
preserved across calls - this function itself is pure/stateless, with no
memory of its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


def normalize_object_label(raw: str) -> str:
    """P0.7 brief Section 6 - "Person"/"person"/" PERSON " all normalize
    to "person". Deliberately ONLY case/whitespace normalization - never
    invents a new class name, never re-maps one COCO class to another
    (that is `luno.vision._normalize_label()`'s own, separate, existing
    job, already applied upstream before this function ever sees a
    label - see this module's own docstring). Returns `""` for a
    non-string/empty input rather than raising - callers filter empty
    results out (a detection with no real label is not a detection)."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


@dataclass(frozen=True)
class VisionContext:
    """P0.7 - the normalized, automation-facing snapshot of "what does
    Vision currently see" - see module docstring for the full field-by-
    field rationale. Immutable (frozen) and small (no raw frames/images/
    credentials/RTSP URLs - P0.7 brief Section 4's own explicit "avoid"
    list) - safe to embed directly in an Event Bus payload."""

    camera_id: str
    timestamp: float = field(default_factory=time.time)
    available: Optional[bool] = None
    human_present: bool = False
    person_count: int = 0
    detected_objects: Tuple[str, ...] = ()
    detection_error: Optional[str] = None
    #: P0.8.6 - `VisionAdapter._extra_status()["human_confirmed"]` passed
    #: through unmodified (see that adapter's own `_update_confirmed_
    #: presence()` docstring for the full confidence/consecutive-cycle
    #: rationale). `False` by default/when unavailable - same "unknown
    #: degrades to the safe, non-triggering value" convention every
    #: other field on this dataclass already follows.
    human_confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "timestamp": self.timestamp,
            "available": self.available,
            "human_present": self.human_present,
            "person_count": self.person_count,
            "detected_objects": self.detected_objects,
            "detection_error": self.detection_error,
            "human_confirmed": self.human_confirmed,
        }


def build_vision_context(
    camera_id: str,
    status: Optional[Dict[str, Any]],
    detection_error: Optional[str] = None,
) -> VisionContext:
    """Pure function - `status` is whatever `adapter_manager.status_all()
    .get("vision")` currently returns (the SAME public snapshot `luno/
    dashboard/collectors.py::collect_vision()` already reads - see
    module docstring). `None`/`{}`/a malformed status (e.g. Vision
    adapter not registered, or the reader hasn't been wired yet - see
    `vision_bridge.py`) degrades to the safe, honest "unavailable,
    nothing known" default (`available=None`, `human_present=False`,
    `person_count=0`, `detected_objects=()`) rather than raising -
    matching this project's own established "malformed/missing input
    degrades gracefully, never crashes the caller" convention (e.g.
    `cameras.load_camera_profiles`)."""
    status = status or {}

    objects_raw = status.get("objects")
    labels = set()
    if isinstance(objects_raw, list):
        for obj in objects_raw:
            if not isinstance(obj, dict):
                continue
            normalized = normalize_object_label(obj.get("label"))
            if normalized:
                labels.add(normalized)
    detected_objects = tuple(sorted(labels))

    person_count_raw = status.get("human_count", 0)
    try:
        person_count = max(0, int(person_count_raw))
    except (TypeError, ValueError):
        person_count = 0

    return VisionContext(
        camera_id=camera_id,
        timestamp=time.time(),
        available=status.get("camera_connected"),
        human_present=person_count > 0,
        person_count=person_count,
        detected_objects=detected_objects,
        detection_error=detection_error,
        human_confirmed=bool(status.get("human_confirmed", False)),
    )
