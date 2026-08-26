"""
vision_human_state.py
======================

Per-person pose/orientation ESTIMATION for the real Vision pipeline
(Sprint 8) - "Estimate: Standing / Sitting / Walking / Looking toward
camera / Looking away / Hand raised / Present / Left scene" (spec's own
wording is "Estimate", not "detect precisely"). Pure heuristics over a
YOLO pose model's keypoints (nose, eyes, ears, shoulders, elbows, wrists,
hips, knees, ankles - Ultralytics' 17-point COCO keypoint layout) plus
frame-to-frame bounding-box movement from `vision_tracking.ObjectTracker`
- no separate face/gaze model, no landmark-level identity, nothing that
could re-identify WHO a person is.

Explicitly OUT OF SCOPE, by design, matching the spec's own instruction
("Do not perform biometric identification. No face recognition. No
identity storage."):
  - no face embeddings, no face matching against any stored identity
  - no name/identity is ever assigned here - only a per-session tracking
    id (e.g. "person#3") from `vision_tracking.ObjectTracker`, which
    resets the moment the process restarts and is never written to disk
    as a biometric record (Vision Memory's own `HumanObservation.identity`
    field is left `None` for every human this pipeline produces - see
    `luno/adapters/vision.py`)

HONEST LIMITATION (same convention as every other heuristic in this
codebase - see e.g. `luno/planner/parser.py`'s own docstring): keypoint-
based pose estimation is a coarse geometric heuristic, not a trained
activity classifier. It will misclassify unusual poses or camera angles.
Every classification here degrades to a documented default (`UNKNOWN`)
rather than guessing confidently when the input is too sparse (no pose
model configured, or a person only partially visible) to support a real
judgment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from .vision_tracking import Keypoint, TrackedDetection

# Ultralytics YOLO pose (COCO 17-keypoint layout) index -> meaning. Used
# only for readability in this file - the model itself just returns a
# plain list ordered this way.
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE = 11, 12, 13, 14, 15, 16

_MIN_KEYPOINT_CONF = 0.3


class Posture(str, Enum):
    STANDING = "standing"
    SITTING = "sitting"
    WALKING = "walking"
    UNKNOWN = "unknown"


class Facing(str, Enum):
    TOWARD_CAMERA = "toward_camera"
    AWAY = "away"
    UNKNOWN = "facing_unknown"


class Presence(str, Enum):
    PRESENT = "present"
    LEFT_SCENE = "left_scene"


@dataclass
class HumanState:
    """One person's ESTIMATED state for the current frame - everything
    here is derived purely from geometry (bbox + keypoints), never from
    who the person is. `tracking_id` comes from `ObjectTracker` (e.g.
    "person#2"), never a name."""
    tracking_id: str
    posture: Posture
    facing: Facing
    hand_raised: bool
    presence: Presence

    def to_dict(self) -> Dict[str, object]:
        return {
            "tracking_id": self.tracking_id,
            "posture": self.posture.value,
            "facing": self.facing.value,
            "hand_raised": self.hand_raised,
            "presence": self.presence.value,
        }


def _kp(keypoints: Optional[List[Keypoint]], index: int) -> Optional[Keypoint]:
    if not keypoints or index >= len(keypoints):
        return None
    point = keypoints[index]
    if point is None or point[2] < _MIN_KEYPOINT_CONF:
        return None
    return point


def _estimate_posture(bbox, keypoints: Optional[List[Keypoint]]) -> Posture:
    """Hip/knee/ankle vertical-spread heuristic: standing has hips well
    above knees which are well above ankles (roughly straight-legged);
    sitting compresses that (thighs roughly horizontal instead of
    vertical). Falls back to a pure bounding-box aspect-ratio heuristic
    (tall+narrow -> standing, roughly square/wide -> sitting) when
    keypoints aren't available at all (no pose model configured) - a
    strictly weaker signal, used only as a last resort."""
    hip = _kp(keypoints, L_HIP) or _kp(keypoints, R_HIP)
    knee = _kp(keypoints, L_KNEE) or _kp(keypoints, R_KNEE)
    ankle = _kp(keypoints, L_ANKLE) or _kp(keypoints, R_ANKLE)
    if hip and knee and ankle:
        hip_knee = knee[1] - hip[1]
        knee_ankle = ankle[1] - knee[1]
        if hip_knee <= 0 or knee_ankle <= 0:
            return Posture.UNKNOWN
        ratio = hip_knee / knee_ankle
        if ratio < 0.6:
            return Posture.SITTING
        return Posture.STANDING

    x1, y1, x2, y2 = bbox
    width, height = max(1e-6, x2 - x1), max(1e-6, y2 - y1)
    aspect = height / width
    if aspect >= 1.6:
        return Posture.STANDING
    if aspect <= 1.1:
        return Posture.SITTING
    return Posture.UNKNOWN


def _estimate_facing(keypoints: Optional[List[Keypoint]]) -> Facing:
    """Both eyes AND the nose confidently visible => facing the camera;
    the nose visible but only one eye (or neither) => facing away/to the
    side. Requires keypoints at all - no separate face detector, so this
    degrades to UNKNOWN without a pose model."""
    nose = _kp(keypoints, NOSE)
    l_eye = _kp(keypoints, L_EYE)
    r_eye = _kp(keypoints, R_EYE)
    if nose is None:
        return Facing.UNKNOWN
    if l_eye and r_eye:
        return Facing.TOWARD_CAMERA
    return Facing.AWAY


def _estimate_hand_raised(keypoints: Optional[List[Keypoint]]) -> bool:
    """Either wrist keypoint above (smaller y than, in image coordinates
    where y grows downward) its own shoulder keypoint - a plain, honest
    geometric check, not gesture recognition."""
    for wrist_idx, shoulder_idx in ((L_WRIST, L_SHOULDER), (R_WRIST, R_SHOULDER)):
        wrist = _kp(keypoints, wrist_idx)
        shoulder = _kp(keypoints, shoulder_idx)
        if wrist and shoulder and wrist[1] < shoulder[1]:
            return True
    return False


class HumanStateEstimator:
    """Stateful ONLY for walking detection (needs the previous frame's
    bbox center per tracking id) - everything else is a pure per-frame
    function of that one detection's own bbox/keypoints. State is keyed
    by `tracking_id` (from `ObjectTracker`), so it self-cleans the moment
    a track is lost (see `forget()`) - it never accumulates history for a
    person no longer being tracked."""

    def __init__(self, walking_speed_threshold: float = 0.02) -> None:
        # Fraction of the person's OWN bbox width the horizontal center
        # must move per frame to count as "walking" rather than "standing
        # in place" - a fraction of their own bbox rather than an
        # absolute pixel count, so it behaves the same regardless of
        # resolution or how close the person is to the camera.
        self.walking_speed_threshold = walking_speed_threshold
        self._last_center_x: Dict[str, float] = {}

    def estimate(self, detection: TrackedDetection) -> HumanState:
        bbox = detection.bbox
        posture = _estimate_posture(bbox, detection.keypoints)

        x1, _, x2, _ = bbox
        width = max(1e-6, x2 - x1)
        center_x = (x1 + x2) / 2.0
        prev_center = self._last_center_x.get(detection.id)
        if prev_center is not None and posture != Posture.SITTING:
            movement = abs(center_x - prev_center) / width
            if movement >= self.walking_speed_threshold:
                posture = Posture.WALKING
        self._last_center_x[detection.id] = center_x

        facing = _estimate_facing(detection.keypoints)
        hand_raised = _estimate_hand_raised(detection.keypoints)

        return HumanState(
            tracking_id=detection.id, posture=posture, facing=facing,
            hand_raised=hand_raised, presence=Presence.PRESENT,
        )

    def forget(self, tracking_id: str) -> None:
        """Called once a track is lost (see `ObjectTracker.lost_ids`) -
        drops this estimator's own per-id movement history. The CALLER
        decides when to emit `HumanLeft`/`presence=left_scene` (see
        `luno/adapters/vision.py`); this class only forgets its own
        bookkeeping, it doesn't decide when someone left."""
        self._last_center_x.pop(tracking_id, None)

    def active_ids(self) -> List[str]:
        """Every tracking id this estimator currently holds movement
        history for - lets a caller reconcile/prune ids that dropped out
        of tracking for reasons other than an explicit `forget()` call
        (e.g. `ObjectTracker.lost_ids` under-reporting a corner case)
        without reaching into this class's private state."""
        return list(self._last_center_x.keys())
