"""
vision_tracking.py
===================

Stable multi-frame object tracking for the real Vision pipeline (Sprint
8). Pure logic, zero I/O / camera / model dependency - takes one frame's
worth of raw detections (label + confidence + bounding box) and matches
them against the previous frame's tracked objects via IoU (Intersection-
over-Union), returning a stable, identity-bearing view: the same physical
object keeps the same `TrackedDetection.id` across frames as long as it
keeps reappearing within `tracking_timeout_s` - this is what the spec
calls "Avoid flickering detections."

Deliberately a DIFFERENT tracker from the existing, already-complete
`luno/vision_memory/tracker.py` (see that module's own "NOTE ON IDENTITY"
docstring) - that one assigns IDs to label+color+location text matches
extracted from Gemini's free-text descriptions, one layer further
downstream. This tracker works one layer upstream, right where a real
object detector's bounding boxes come out, frame-to-frame, before
anything reaches Vision Memory at all. `luno/adapters/vision.py` is the
seam between the two: it feeds THIS tracker's stable output into a
`SceneObservation` handed to `vision_memory.update()`, which then runs
its OWN, unrelated tracker on top for the natural-language layer. Neither
file needs to know the other exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

BoundingBox = Tuple[float, float, float, float]  # (x1, y1, x2, y2) - pixel or normalized, caller's choice, just consistent per frame
Keypoint = Tuple[float, float, float]  # (x, y, confidence)


@dataclass
class RawDetection:
    """One instance detected in the CURRENT frame, before any persistent
    identity is assigned - the output shape a `VisionBackend` produces
    for a single frame."""
    label: str
    confidence: float
    bbox: BoundingBox
    keypoints: Optional[List[Keypoint]] = None  # pose models only (person label) - see vision_human_state.py


@dataclass
class TrackedDetection:
    """A persistently-identified detection - what `ObjectTracker.update()`
    returns. `id` (e.g. "cup#3") is stable for as long as the same
    physical object keeps being matched frame-to-frame; a NEW id is only
    assigned once its previous track has gone unseen longer than
    `tracking_timeout_s` (see `ObjectTracker.lost_ids` for what dropped
    out of tracking on a given `update()` call)."""
    id: str
    label: str
    confidence: float
    bbox: BoundingBox
    first_seen: float
    last_seen: float
    tracking_age_s: float
    keypoints: Optional[List[Keypoint]] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "bbox": list(self.bbox),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "tracking_age_s": round(self.tracking_age_s, 2),
        }


def _iou(a: BoundingBox, b: BoundingBox) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class ObjectTracker:
    """Greedy IoU-based tracker: same LABEL + highest-IoU match above
    `iou_threshold` from the previous frame keeps its ID; anything left
    unmatched becomes a new track. Matching is done across the WHOLE
    candidate set at once (highest-IoU pair wins first), not first-come-
    first-served per detection - avoids a mediocre match stealing a track
    that a later detection in the same frame would have matched far
    better.

    Anything not re-matched for longer than `tracking_timeout_s` is
    dropped (reported once, via `lost_ids`, on the `update()` call where
    the timeout was actually crossed) - this is what keeps a momentarily-
    missed detection (one dropped frame, motion blur, a hand briefly
    occluding an object) from flickering a NEW id in and the OLD one out
    every other frame.

    `max_tracked` caps how many DISTINCT tracks are kept alive at once -
    a hard ceiling so a noisy scene can never grow this tracker's memory
    unbounded; the stalest (oldest last_seen) track is evicted to make
    room for a genuinely new detection once the cap is hit.
    """

    def __init__(self, iou_threshold: float = 0.3, tracking_timeout_s: float = 5.0, max_tracked: int = 20) -> None:
        self.iou_threshold = iou_threshold
        self.tracking_timeout_s = tracking_timeout_s
        self.max_tracked = max_tracked
        self._tracks: Dict[str, TrackedDetection] = {}
        self._next_id: Dict[str, int] = {}
        #: populated fresh by each `update()` call - ids that dropped out
        #: of tracking THIS call, either from timeout or max_tracked
        #: eviction. Never accumulates across calls.
        self.lost_ids: List[str] = []

    def _new_id(self, label: str) -> str:
        n = self._next_id.get(label, 0) + 1
        self._next_id[label] = n
        return f"{label}#{n}"

    def update(self, detections: List[RawDetection], now: Optional[float] = None) -> List[TrackedDetection]:
        now = now if now is not None else time.time()
        self.lost_ids = []

        # 1) Evict anything that's timed out BEFORE matching this frame -
        #    a track that's been gone 10 minutes should never "steal" a
        #    fresh detection's identity just because nothing told it to
        #    leave yet.
        for track_id, track in list(self._tracks.items()):
            if now - track.last_seen > self.tracking_timeout_s:
                del self._tracks[track_id]
                self.lost_ids.append(track_id)

        # 2) Score every same-label (detection, existing track) pair above
        #    threshold, then greedily assign highest-scoring pairs first.
        candidates = []
        for d_idx, det in enumerate(detections):
            for track_id, track in self._tracks.items():
                if track.label != det.label:
                    continue
                score = _iou(det.bbox, track.bbox)
                if score >= self.iou_threshold:
                    candidates.append((score, d_idx, track_id))
        candidates.sort(key=lambda c: c[0], reverse=True)

        matched_detection_idx: Dict[int, str] = {}
        matched_track_ids: set = set()
        for score, d_idx, track_id in candidates:
            if d_idx in matched_detection_idx or track_id in matched_track_ids:
                continue
            matched_detection_idx[d_idx] = track_id
            matched_track_ids.add(track_id)

        result: List[TrackedDetection] = []
        for d_idx, det in enumerate(detections):
            track_id = matched_detection_idx.get(d_idx)
            if track_id is not None:
                prev = self._tracks[track_id]
                updated = TrackedDetection(
                    id=track_id, label=det.label, confidence=det.confidence, bbox=det.bbox,
                    first_seen=prev.first_seen, last_seen=now, tracking_age_s=now - prev.first_seen,
                    keypoints=det.keypoints,
                )
                self._tracks[track_id] = updated
                result.append(updated)
                continue

            # Brand-new track candidate - respect max_tracked. Only ever
            # evicts a track NOT already matched/confirmed THIS frame
            # (never steals a slot from something just seen a moment
            # ago); if every existing track was just confirmed present,
            # this one genuinely-new detection is dropped for this frame
            # instead (an honest "scene has more distinct objects than
            # max_tracked allows" degrade, re-considered again next frame).
            if self.max_tracked > 0 and len(self._tracks) >= self.max_tracked:
                evictable = [tid for tid in self._tracks if tid not in matched_track_ids]
                if not evictable:
                    continue
                stalest_id = min(evictable, key=lambda tid: self._tracks[tid].last_seen)
                del self._tracks[stalest_id]
                self.lost_ids.append(stalest_id)

            track_id = self._new_id(det.label)
            updated = TrackedDetection(
                id=track_id, label=det.label, confidence=det.confidence, bbox=det.bbox,
                first_seen=now, last_seen=now, tracking_age_s=0.0, keypoints=det.keypoints,
            )
            self._tracks[track_id] = updated
            result.append(updated)

        return result

    def current_tracks(self) -> List[TrackedDetection]:
        return list(self._tracks.values())

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id.clear()
        self.lost_ids = []
