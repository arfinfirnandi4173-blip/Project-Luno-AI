"""
vision.py
=========

`VisionAdapter` - translates Gemini-produced scene descriptions,
YOLO-style detections, tracked-object/human-pose cycles, and raw camera
frame/status notifications into internal Events, and feeds observations
into the existing, already-tested `luno.vision_memory` package via its
public `update()` facade (per the integration rule: "Only use their
existing public APIs" - this file never touches `vision_memory`'s
internals). No OpenCV, no Gemini/vision-provider client, no YOLO model
code lives here - only mocks/interfaces/translation, exactly like every
other adapter.

FOUR independent signals feed this adapter (the first three predate
Sprint 8 and are UNCHANGED - Sprint 8's tracked pipeline is purely
additive, so anything already relying on these keeps working identically
whether Vision is running mock or real):

  - `on_scene_description(text)` - a Gemini-style free-text
    description. Forwarded to `vision_memory.update()`; publishes
    `VisionUpdated` always, and `VisionChanged` additionally whenever
    Vision Memory's own importance scoring judged it a MEANINGFUL change
    (i.e. `update()` returned at least one event) - Vision Memory already
    does that scoring, so this adapter never re-implements it.
  - `on_detections(detections)` - a YOLO-style list of
    `{"label": str, "confidence": float}` dicts for the current frame.
    Diffed against the previous frame's label SET (pure adapter-level
    bookkeeping, no Vision Memory write) to publish `ObjectAppeared`/
    `ObjectDisappeared` for ordinary objects and `PersonAppeared`/
    `PersonDisappeared` specifically for the `"person"` label.
  - `on_frame(frame_meta)` - a raw "a frame was captured" tick, used only
    for the `frames_seen` counter today.

Sprint 8 adds a FIFTH, richer signal on top of these, from the real
tracked-object + human-pose pipeline (`luno.vision_tracking.ObjectTracker`
+ `luno.vision_human_state.HumanStateEstimator`, orchestrated by
`real_vision.py`):

  - `on_vision_cycle(cycle: VisionCycleResult)` - one full detect+track
    cycle's worth of STABLE, identity-bearing detections (bounding box,
    confidence, tracking age) and per-person pose ESTIMATES (posture/
    facing/hand_raised/presence - see `vision_human_state.py`'s own
    "no biometric identification" guarantee). Diffed against the
    PREVIOUS cycle (by tracking id, not by label set - a real upgrade
    over the label-only diffing `on_detections()` above does) to publish
    only genuine changes: `ObjectDetected`/`ObjectUpdated`/`ObjectLost`
    for objects, `HumanEntered`/`PoseChanged`/`HumanLeft` for people, one
    `SceneChanged` if ANY of those fired, and `VisionFrameProcessed`
    every cycle regardless (a cheap per-cycle heartbeat for FPS/latency
    dashboard stats - NOT gated by "did anything change", since knowing
    the pipeline is alive and how fast it's running is useful even on a
    static scene).

    Builds a STRUCTURED `SceneObservation` (not a re-parsed text string -
    `vision_memory.update()` accepts either shape, see that module's own
    docstring) directly from the tracked objects/human states and hands
    it to `vision_memory.update()` - a strictly richer integration than
    `on_scene_description()`'s free-text path, since the structured
    fields (label/confidence/activity) never need to survive a lossy
    heuristic-text round trip.

  - `on_camera_status(status)` - `{"connected": bool|None, "source":...,
    "error": str|None}` (see `luno.vision.camera_status()`). Diffed on
    the `connected` field only, publishing `CameraDisconnected`/
    `CameraReconnected` exactly once per real transition - Runtime keeps
    running either way, per spec ("Keep Runtime alive").
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ..core.events import ObjectAppeared, ObjectDisappeared, VisionChanged, VisionUpdated
from .base import BaseAdapter
from .events import (
    CameraDisconnected,
    CameraPersonEntered,
    CameraPersonLeft,
    CameraReconnected,
    HumanEntered,
    HumanLeft,
    HumanPresenceConfirmed,
    HumanPresenceUnconfirmed,
    ObjectDetected,
    ObjectLost,
    ObjectUpdated,
    PersonAppeared,
    PersonDisappeared,
    PoseChanged,
    SceneChanged,
    VisionFrameProcessed,
)
from .utils import log

#: Fallback used only if `luno.config` can't be imported/read (e.g. a
#: test constructing this adapter in isolation) - matches
#: `CAMERA_PERSON_ABSENCE_TIMEOUT_S`'s own default in config.py, kept as
#: a literal here too so this adapter never hard-depends on config being
#: importable to have SOME sane behavior.
_DEFAULT_PERSON_ABSENCE_TIMEOUT_S = 5.0

if TYPE_CHECKING:
    from ..vision_human_state import HumanState
    from ..vision_tracking import TrackedDetection

PERSON_LABEL = "person"

#: A bounding-box center move of at least this fraction of the box's own
#: width/height counts as "moved enough to be an ObjectUpdated", not just
#: detector jitter on an otherwise-static object - see `_object_changed()`.
_BBOX_MOVE_FRACTION = 0.15
#: A confidence swing of at least this much also counts as a meaningful
#: update (independent of position) - e.g. a partially-occluded object
#: becoming fully visible again.
_CONFIDENCE_DELTA = 0.15


@dataclass
class VisionCycleResult:
    """One tracked-detection cycle's output - the Sprint 8 contract
    between a real `VisionSource` (see `real_vision.py`) and
    `VisionAdapter.on_vision_cycle()`. Deliberately a plain, mock-
    friendly dataclass (no dependency on ultralytics/OpenCV types) so
    tests can construct one by hand with zero real camera/model
    involved."""
    objects: List["TrackedDetection"] = field(default_factory=list)
    humans: List["HumanState"] = field(default_factory=list)
    lost_object_ids: List[str] = field(default_factory=list)
    lost_human_ids: List[str] = field(default_factory=list)
    fps: float = 0.0
    latency_ms: float = 0.0


class VisionListener(ABC):
    def on_scene_description(self, description: str) -> None: ...
    def on_detections(self, detections: List[Dict[str, Any]]) -> None: ...
    def on_frame(self, frame_meta: Optional[Dict[str, Any]] = None) -> None: ...
    # Sprint 8 - additive, both have safe no-op defaults so a VisionSource
    # that never calls them (e.g. anything written before Sprint 8) needs
    # no changes at all.
    def on_vision_cycle(self, cycle: "VisionCycleResult") -> None: ...
    def on_camera_status(self, status: Dict[str, Any]) -> None: ...


class VisionSource(ABC):
    @abstractmethod
    def start(self, listener: VisionListener) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class MockVisionSource(VisionSource):
    def __init__(self) -> None:
        self.listener: Optional[VisionListener] = None
        self.running = False

    def start(self, listener: VisionListener) -> None:
        self.listener = listener
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.listener = None

    def simulate_scene(self, description: str) -> None:
        if self.listener:
            self.listener.on_scene_description(description)

    def simulate_detections(self, detections: List[Dict[str, Any]]) -> None:
        if self.listener:
            self.listener.on_detections(detections)

    def simulate_frame(self, frame_meta: Optional[Dict[str, Any]] = None) -> None:
        if self.listener:
            self.listener.on_frame(frame_meta)

    # Sprint 8 - lets dashboard/adapter tests drive the new tracked-cycle
    # path without any real camera/model, exactly like the three
    # `simulate_*` helpers above already do for the older signals.
    def simulate_vision_cycle(self, cycle: "VisionCycleResult") -> None:
        if self.listener:
            self.listener.on_vision_cycle(cycle)

    def simulate_camera_status(self, status: Dict[str, Any]) -> None:
        if self.listener:
            self.listener.on_camera_status(status)


def _bbox_center(bbox) -> Any:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _object_changed(prev: "TrackedDetection", curr: "TrackedDetection") -> bool:
    """Whether `curr` differs enough from `prev` (same tracking id) to be
    worth an `ObjectUpdated` - never fires for "still there, box jittered
    by a pixel or two", which is the normal, expected case on almost
    every cycle and would otherwise flood the Event Bus (spec: "Only
    publish changes")."""
    if prev.label != curr.label:
        return True
    if abs(prev.confidence - curr.confidence) >= _CONFIDENCE_DELTA:
        return True
    px1, py1, px2, py2 = prev.bbox
    width, height = max(1e-6, px2 - px1), max(1e-6, py2 - py1)
    pcx, pcy = _bbox_center(prev.bbox)
    ccx, ccy = _bbox_center(curr.bbox)
    dx, dy = abs(ccx - pcx) / width, abs(ccy - pcy) / height
    return dx >= _BBOX_MOVE_FRACTION or dy >= _BBOX_MOVE_FRACTION


def _human_changed(prev: "HumanState", curr: "HumanState") -> bool:
    return (
        prev.posture != curr.posture
        or prev.facing != curr.facing
        or prev.hand_raised != curr.hand_raised
        or prev.presence != curr.presence
    )


def _human_pose_text(state: "HumanState") -> str:
    from ..vision_human_state import Facing, Posture

    bits = []
    if state.posture != Posture.UNKNOWN:
        bits.append(state.posture.value)
    if state.facing == Facing.TOWARD_CAMERA:
        bits.append("looking toward the camera")
    elif state.facing == Facing.AWAY:
        bits.append("looking away")
    if state.hand_raised:
        bits.append("hand raised")
    return ", ".join(bits)


class VisionAdapter(BaseAdapter, VisionListener):
    name = "vision"

    def __init__(
        self,
        source: Optional[VisionSource] = None,
        vision_memory_module: Optional[Any] = None,
        person_absence_timeout_s: Optional[float] = None,
    ) -> None:
        BaseAdapter.__init__(self)
        self.source = source or MockVisionSource()
        # Injected for testability (so tests don't need a real SQLite-backed
        # vision_memory instance); defaults to the real, already-tested
        # package's module-level facade.
        if vision_memory_module is None:
            from .. import vision_memory as vision_memory_module  # local import: optional dependency, mirrors luno/vision.py's own pattern
        self._vision_memory = vision_memory_module

        self._last_labels: Set[str] = set()
        self._frames_seen = 0

        # Debounced room-level presence (CameraPersonEntered/
        # CameraPersonLeft - see `_update_person_presence()` below).
        # Reads `CAMERA_PERSON_ABSENCE_TIMEOUT_S` lazily, once, at
        # construction (not per-call) - same "local import: optional
        # dependency" convention `luno/vision.py` itself already uses,
        # so this adapter still works (with a sane default) in a test
        # that never sets up `luno.config` at all.
        if person_absence_timeout_s is None:
            try:
                from .. import config as _legacy_config
                person_absence_timeout_s = float(
                    getattr(_legacy_config, "CAMERA_PERSON_ABSENCE_TIMEOUT_S", _DEFAULT_PERSON_ABSENCE_TIMEOUT_S)
                )
            except Exception:
                person_absence_timeout_s = _DEFAULT_PERSON_ABSENCE_TIMEOUT_S
        self._person_absence_timeout_s = max(0.0, person_absence_timeout_s)
        self._person_present_debounced = False
        self._person_last_seen_at: Optional[float] = None

        # Sprint 8 - tracked-cycle bookkeeping (all keyed by tracking id,
        # never by label - a real upgrade over `_last_labels` above's
        # coarser label-set diffing).
        self._known_objects: Dict[str, "TrackedDetection"] = {}
        self._known_humans: Dict[str, "HumanState"] = {}
        self._last_cycle_fps = 0.0
        self._last_cycle_latency_ms = 0.0
        self._last_observations: List[str] = []
        self._camera_connected: Optional[bool] = None

        # P0.8.6 - human-presence AUTOMATION confirmation (separate from,
        # and layered ON TOP OF, the P0.8.5 `_update_person_presence()`
        # debounce above - see that method's own docstring, UNCHANGED by
        # this sprint). Root cause: a single tracked-cycle frame at, say,
        # person=0.506 could already flip `_person_present_debounced` and
        # fire `CameraPersonEntered` -> `human_detected` -> a real
        # `home_assistant.turn_on`, with no confidence floor and no
        # temporal confirmation at all - see docs/change_impact/
        # camera_automation_p0_8_6.md Section 2 for the full trace and
        # the real false-positive evidence this reproduces.
        #
        # `_human_confirmed` is a NEW, separate boolean - computed ONLY
        # from `on_vision_cycle()` (the tracked, count-AND-confidence-
        # aware loop; P0.8.5's own "authoritative source" - `on_
        # detections()`/the presence-watch loop structurally never has
        # per-detection confidence at all, so it cannot participate in
        # this gate, and its own `_update_person_presence()` contract
        # stays 100% unchanged, still covered by tests/test_camera_
        # presence.py's pre-existing, dedicated suite). It requires
        # `config.HUMAN_DETECTION_CONFIRM_CYCLES` CONSECUTIVE cycles,
        # each with at least one person detection at >= `config.HUMAN_
        # DETECTION_CONFIDENCE`, before flipping True - one low-confidence
        # or one-off frame can never confirm presence on its own. Falls
        # back to False the moment a cycle fails to qualify (mirrors
        # `_update_person_presence()`'s own "rise slow(er), the safe
        # direction; there is no separate fall timer needed here since
        # `_person_present_debounced`'s EXISTING absence-timeout already
        # covers the falling edge - see `_update_confirmed_presence()`
        # below for exactly how the two compose).
        #
        # NOT wired into `_update_person_presence()`/`CameraPersonEntered`
        # itself (P0.8.5's fix and `test_camera_presence.py`'s pinned
        # contract are both left fully intact) - instead exposed via
        # `_extra_status()["human_confirmed"]`, read by `vision_context.
        # build_vision_context()` -> `CameraEvent.human_confirmed`, and
        # required as a new condition on the WLED-triggering automation
        # rule (`config/automation_rules.json`) - see the change-impact
        # doc Section 3 for why gating the RULE, not the raw Vision
        # event, is the smallest change that fully satisfies "a single
        # frame must not directly cause human_detected -> WLED ON"
        # without touching any already-tested Vision-layer contract.
        self._human_confirm_streak = 0
        self._human_confirmed = False

    def _do_start(self) -> None:
        self.source.start(self)

    def _do_stop(self) -> None:
        self.source.stop()

    def _extra_status(self) -> Dict[str, Any]:
        return {
            "backend": "mock" if isinstance(self.source, MockVisionSource) else "real",
            "frames_seen": self._frames_seen,
            "person_present": self._person_present_debounced,
            # P0.8.6 - see `_human_confirm_streak`/`_human_confirmed`'s
            # own declaration comment above for the full rationale.
            "human_confirmed": self._human_confirmed,
            "tracked_labels": sorted(self._last_labels),
            "fps": self._last_cycle_fps,
            "latency_ms": self._last_cycle_latency_ms,
            "object_count": len(self._known_objects),
            "human_count": len(self._known_humans),
            "camera_connected": self._camera_connected,
            "objects": [o.to_dict() for o in self._known_objects.values()],
            "humans": [h.to_dict() for h in self._known_humans.values()],
            "latest_observations": list(self._last_observations),
        }

    # -- VisionListener: external system -> internal Events ---------------------

    def on_scene_description(self, description: str) -> None:
        try:
            change_events = self._vision_memory.update(description)
        except Exception as ex:
            log(f"vision_memory.update() raised (continuing, not fatal to the adapter): {ex}", self.name)
            change_events = []

        self.publish(VisionUpdated(data={"description": description}))
        if change_events:
            self.publish(VisionChanged(data={
                "description": description,
                "changes": [getattr(e, "description", str(e)) for e in change_events],
            }))

    def on_detections(self, detections: List[Dict[str, Any]]) -> None:
        labels = {d.get("label") for d in detections if d.get("label")}
        appeared = labels - self._last_labels
        disappeared = self._last_labels - labels

        for label in appeared:
            if label == PERSON_LABEL:
                self.publish(PersonAppeared())
            else:
                self.publish(ObjectAppeared(data={"label": label}))
        for label in disappeared:
            if label == PERSON_LABEL:
                self.publish(PersonDisappeared())
            else:
                self.publish(ObjectDisappeared(data={"label": label}))

        self._last_labels = labels
        self._update_person_presence(PERSON_LABEL in labels)

    def _update_person_presence(self, person_detected: bool) -> None:
        """Debounced ABSENT/PRESENT room-level state machine, additive on
        top of the raw `PersonAppeared`/`PersonDisappeared` diffing
        above (that logic is UNCHANGED - this never replaces it, only
        layers a hardened signal on top). Called every time
        `on_detections()` runs (i.e. every YOLO presence-watch poll,
        `CAMERA_WATCH_INTERVAL_S` apart by default - see
        `real_vision.py`'s `_poll_once()`), REGARDLESS of whether the
        label set changed this poll, which is what lets the timeout
        below actually elapse in real time rather than only being
        checked on a transition.

        Rises IMMEDIATELY on the first detection (no reason to delay
        "someone's here" - a false negative there is the safer failure
        direction). Falls only after `_person_absence_timeout_s` seconds
        of CONTINUOUS non-detection - this is the hysteresis: a single
        missed/occluded frame (YOLO briefly losing the person for one
        poll cycle) does not by itself generate a `CameraPersonLeft`,
        only a genuine, sustained absence does. Publishes at most one
        event per real transition, never repeats while the state is
        unchanged - the exact "avoid event spam while the same person
        remains in the room" requirement this was built for."""
        now = time.time()
        if person_detected:
            self._person_last_seen_at = now
            if not self._person_present_debounced:
                self._person_present_debounced = True
                self.publish(CameraPersonEntered())
            return

        if not self._person_present_debounced:
            return  # already ABSENT - nothing to debounce toward
        if self._person_last_seen_at is None:
            return  # defensive - should be unreachable once PRESENT
        if now - self._person_last_seen_at >= self._person_absence_timeout_s:
            self._person_present_debounced = False
            self.publish(CameraPersonLeft())

    def on_frame(self, frame_meta: Optional[Dict[str, Any]] = None) -> None:
        self._frames_seen += 1

    def _update_confirmed_presence(self, person_confidences: List[float]) -> None:
        """P0.8.6 - the automation-facing CONFIRMATION gate (see
        `_human_confirm_streak`'s own declaration comment in `__init__`
        for the full root-cause/design rationale). Called once per
        tracked cycle, ONLY from `on_vision_cycle()`, with THIS cycle's
        own real person confidences (never a stale/cached value).

        A cycle "qualifies" if at least one person this cycle was
        detected at >= `config.HUMAN_DETECTION_CONFIDENCE`. Consecutive
        qualifying cycles increment a streak; `human_confirmed` only
        flips True once that streak reaches `config.HUMAN_DETECTION_
        CONFIRM_CYCLES` - so one high-confidence frame alone can never
        confirm presence (the literal bug this sprint fixes: person=
        0.506, a single frame, previously reached WLED ON with zero
        confirmation). A single non-qualifying cycle resets the streak
        to zero immediately (a stricter, MORE conservative reset than
        `_update_person_presence()`'s own multi-second absence timeout -
        deliberate: this gate exists specifically to prevent physical
        automation from firing on marginal evidence, so it fails toward
        "not yet confirmed" quickly rather than lingering). Once
        confirmed, `human_confirmed` stays True for as long as each
        cycle keeps qualifying; it drops back to False the moment a
        cycle does not (mirroring, but not reusing, the debounce state
        above - a person stepping just out of frame for one cycle
        un-confirms the AUTOMATION signal even though Vision's own
        room-level presence, correctly, stays debounced-PRESENT via
        `_person_absence_timeout_s` - the automation-safety gate is
        intentionally stricter than the raw presence signal it sits on
        top of)."""
        try:
            from .. import config as _legacy_config
            confidence_floor = float(getattr(_legacy_config, "HUMAN_DETECTION_CONFIDENCE", 0.60))
            confirm_cycles = max(1, int(getattr(_legacy_config, "HUMAN_DETECTION_CONFIRM_CYCLES", 3)))
        except Exception:
            confidence_floor, confirm_cycles = 0.60, 3

        qualifies = any(c >= confidence_floor for c in person_confidences)
        if qualifies:
            self._human_confirm_streak += 1
        else:
            self._human_confirm_streak = 0

        was_confirmed = self._human_confirmed
        self._human_confirmed = self._human_confirm_streak >= confirm_cycles

        # Publish ONLY on a genuine transition (own rising/falling edge -
        # never repeats while the state is unchanged, the exact same
        # "avoid event spam" discipline `_update_person_presence()`
        # itself already follows for `CameraPersonEntered`/
        # `CameraPersonLeft`). This is what lets `VisionCameraEventBridge`
        # (subscribed to these two events as of P0.8.6) hand the WLED
        # automation rule a `camera_automation.camera_event` at EXACTLY
        # the moment confirmation completes - a distinct `kind` from
        # `human_detected`/`human_cleared`, so `CameraAutomationModule`'s
        # existing per-`(camera_id, kind)` dedupe never collides the two
        # (see docs/change_impact/camera_automation_p0_8_6.md Section 3
        # for why gating on a SEPARATE event, not a same-kind re-fire or
        # a same-kind-plus-new-field combination, was required here).
        if self._human_confirmed and not was_confirmed:
            self.publish(HumanPresenceConfirmed())
        elif was_confirmed and not self._human_confirmed:
            self.publish(HumanPresenceUnconfirmed())

    # -- Sprint 8: tracked-object + human-pose cycle -----------------------------

    def on_vision_cycle(self, cycle: VisionCycleResult) -> None:
        self._last_cycle_fps = cycle.fps
        self._last_cycle_latency_ms = cycle.latency_ms
        changed_kinds: List[str] = []

        current_objects = {o.id: o for o in cycle.objects}
        for obj_id, obj in current_objects.items():
            prev = self._known_objects.get(obj_id)
            if prev is None:
                self.publish(ObjectDetected(data=obj.to_dict()))
                changed_kinds.append("object_detected")
            elif _object_changed(prev, obj):
                self.publish(ObjectUpdated(data=obj.to_dict()))
                changed_kinds.append("object_updated")
        for lost_id in cycle.lost_object_ids:
            lost = self._known_objects.get(lost_id)
            self.publish(ObjectLost(data={"id": lost_id, "label": lost.label if lost else None}))
            changed_kinds.append("object_lost")
        self._known_objects = current_objects

        current_humans = {h.tracking_id: h for h in cycle.humans}
        for tracking_id, human in current_humans.items():
            prev = self._known_humans.get(tracking_id)
            if prev is None:
                self.publish(HumanEntered(data=human.to_dict()))
                changed_kinds.append("human_entered")
            elif _human_changed(prev, human):
                self.publish(PoseChanged(data=human.to_dict()))
                changed_kinds.append("pose_changed")
        for lost_id in cycle.lost_human_ids:
            self.publish(HumanLeft(data={"tracking_id": lost_id}))
            changed_kinds.append("human_left")
        self._known_humans = current_humans

        # P0.8.5 fix - CONFIRMED ROOT CAUSE (traced end-to-end, real
        # runtime log evidence + source inspection, see docs/change_impact/
        # camera_automation_p0_8_5.md) of `camera_person_entered` firing
        # while the resulting `camera_automation.camera_event` carries
        # `person_count=0`: `_update_person_presence()` below was ONLY
        # ever called from `on_detections()` above - fed by `detect_
        # objects()`'s own SEPARATE, independently-timed presence-only
        # YOLO call (`CAMERA_WATCH_INTERVAL_S`, default 1.0s cadence) -
        # while `person_count` on the resulting `CameraEvent` is read
        # (via `VisionCameraEventBridge`/`vision_context.build_vision_
        # context()`) from `_known_humans` above, populated ONLY by
        # THIS method, fed by the SEPARATE `detect_objects_tracked()`
        # tracked-cycle loop (`VISION_FPS`, default 0.5s cadence). Two
        # independent, uncoordinated polling loops: whichever one first
        # notices "a person is here" fires the (single, shared,
        # debounced) `CameraPersonEntered`, but the ENRICHMENT data the
        # event carries was always read from the OTHER loop's own,
        # possibly-not-yet-caught-up snapshot - a genuine race, not a
        # detection failure (P0.8.4's separate, already-fixed shared-
        # model concurrency bug is NOT the cause here - this is a
        # DIFFERENT race, one level up the pipeline, between two
        # independent CONSUMERS of real detections, not concurrent
        # writers to one shared model).
        #
        # Fix: this tracked-cycle loop ALSO calls the SAME, already-
        # existing, already-tested `_update_person_presence()` debounce
        # method - additively, alongside (never replacing) the existing
        # `on_detections()` call site above. Both calls share the ONE
        # `_person_present_debounced`/`_person_last_seen_at` state, so
        # this can never double-fire `CameraPersonEntered` while a
        # person remains continuously present (whichever loop notices
        # the transition first wins; the other loop's later call for the
        # same already-True state is a no-op inside `_update_person_
        # presence()` itself - no new debounce logic invented). Because
        # THIS loop is both the faster of the two (0.5s vs 1.0s default
        # cadence) AND the one that actually has `person_count`, it now
        # wins the race in the overwhelming majority of real transitions
        # - and whenever it does, `_known_humans` (just set two lines
        # above, in this SAME call) already reflects this exact cycle's
        # real count by the time `CameraPersonEntered` is published,
        # so `VisionCameraEventBridge`'s synchronous read immediately
        # afterward sees the correct, non-stale `person_count`. Every
        # input here (`len(current_humans) > 0`) is real, current-cycle
        # YOLO-derived data - nothing is fabricated.
        self._update_person_presence(len(current_humans) > 0)

        # P0.8.6 - see `_human_confirm_streak`'s own declaration comment
        # (`__init__` above) for the full rationale. `current_objects`
        # (set two blocks above, from `cycle.objects`) carries the SAME
        # tracking ids as `current_humans` but ALSO carries `.confidence`
        # (`HumanState` deliberately does not - it is a pose/posture
        # estimate, not a detector confidence) - correlating the two by
        # id is how this reaches the real, per-person, current-cycle
        # confidence without inventing a new detection path.
        person_confidences_this_cycle = [
            o.confidence for o in current_objects.values()
            if o.label == "person" and o.id in current_humans
        ]
        self._update_confirmed_presence(person_confidences_this_cycle)

        if changed_kinds:
            self.publish(SceneChanged(data={"changes": changed_kinds}))

        self.publish(VisionFrameProcessed(data={
            "fps": cycle.fps, "latency_ms": cycle.latency_ms,
            "object_count": len(current_objects), "human_count": len(current_humans),
            "backend": "mock" if isinstance(self.source, MockVisionSource) else "real",
        }))

        self._feed_vision_memory(current_objects, current_humans)

    def _feed_vision_memory(self, objects: Dict[str, "TrackedDetection"], humans: Dict[str, "HumanState"]) -> None:
        """Builds a STRUCTURED `SceneObservation` straight from tracked
        detections/human states (no text round-trip) and hands it to the
        existing `vision_memory.update()` facade - see this module's own
        docstring for why this is strictly better than the free-text path
        `on_scene_description()` uses. Never lets a Vision Memory failure
        (e.g. SQLite momentarily locked) propagate - same "non-fatal,
        just logged" contract every other integration point in this
        codebase already follows."""
        from ..vision_memory.models import HumanActivity, HumanObservation, ObjectObservation, RoomObservation, SceneObservation
        from ..vision_memory.utils import utcnow

        _POSTURE_TO_ACTIVITY = {
            "standing": HumanActivity.STANDING,
            "sitting": HumanActivity.SITTING,
            "walking": HumanActivity.WALKING,
        }

        object_observations = [
            ObjectObservation(label=o.label, color=None, location=None, confidence=o.confidence)
            for o in objects.values()
        ]
        human_observations = [
            HumanObservation(
                identity=None,  # NEVER set - see vision_human_state.py's "no identity storage" guarantee
                emotion=None,
                pose=_human_pose_text(h) or None,
                activity=_POSTURE_TO_ACTIVITY.get(h.posture.value, HumanActivity.UNKNOWN),
            )
            for h in humans.values()
        ]

        raw_bits = [o.label for o in object_observations] + [
            f"person {_human_pose_text(h)}".strip() for h in humans.values()
        ]
        raw_description = "; ".join(raw_bits) if raw_bits else "Nothing currently detected in view."

        observation = SceneObservation(
            timestamp=utcnow(), raw_description=raw_description,
            objects=object_observations, humans=human_observations, room=RoomObservation(),
        )
        self._last_observations = ([raw_description] + self._last_observations)[:10]

        try:
            self._vision_memory.update(observation)
        except Exception as ex:
            log(f"vision_memory.update() raised on tracked cycle (continuing, not fatal to the adapter): {ex}", self.name)

    def on_camera_status(self, status: Dict[str, Any]) -> None:
        connected = status.get("connected")
        if connected == self._camera_connected:
            return  # no real transition - avoid flooding
        previous = self._camera_connected
        self._camera_connected = connected
        if connected is False and previous is not False:
            self.publish(CameraDisconnected(data={"source": status.get("source"), "error": status.get("error")}))
        elif connected is True and previous is not True:
            # Sprint 69.1: previously only fired for `previous is False`
            # (an actual disconnect->reconnect), silently missing the
            # `previous is None -> True` case - the VERY FIRST successful
            # open a deployment ever makes, before any failure has ever
            # been observed. `_extra_status()["camera_connected"]` (what
            # the dashboard actually reads - see `luno/dashboard/
            # collectors.py::collect_vision()`) was never affected by
            # this gap, since the field above is set unconditionally a
            # few lines up regardless of whether an event fires - this
            # only affects anything that listens to the
            # CameraReconnected EVENT specifically (logs, notifications,
            # a future history view). Reusing CameraReconnected rather
            # than adding a new event type - no new capability class for
            # what is, from any listener's perspective, simply "the
            # camera is connected now".
            self.publish(CameraReconnected(data={"source": status.get("source")}))
