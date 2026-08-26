"""
real_vision.py
================

Real `VisionSource` implementation for `VisionAdapter` (see `vision.py`)
- wrapping the EXISTING, untouched `luno.vision` module (OpenCV camera
capture + YOLO object detection + on-demand Gemini 2.0 Flash scene
understanding). This file orchestrates `luno.vision`'s own functions on
background threads and forwards results into the `VisionListener`
callbacks - it does not reimplement any camera/model code itself.

Opt-in only: `VISION_BACKEND=real` (see
`luno/bootstrap/launcher_config.py`) - default stays `MockVisionSource`,
zero behavior change unless explicitly enabled. Even when enabled,
`luno.vision.is_configured()` (== `CAMERA_VISION_ENABLED`) gates whether
this is usable at all - see `luno/bootstrap/health.py`'s camera check.

TWO independent background loops, both started/stopped together by this
one `VisionSource`:

  1. `_poll_loop()` (UNCHANGED from pre-Sprint-8) - starts `luno.vision`'s
     own `start_watch()` (YOLO presence-only) and, if enabled,
     `start_vision_watch()` (now an inert no-op - the continuous ambient
     scene-description loop it used to start was removed in the Aug 2026
     Gemini migration, see `vision.py`'s own docstring for why), then
     polls their already-maintained "last result" accessors on
     `CAMERA_WATCH_INTERVAL_S`, forwarding into `on_detections()`/
     `on_scene_description()`/`on_frame()` exactly as before.

  2. `_tracked_cycle_loop()` (Sprint 8, NEW) - runs at `config.VISION_FPS`,
     each cycle: capture one frame -> `vision.detect_objects_tracked()`
     (structured label+confidence+bbox) -> `vision.attach_pose_keypoints()`
     (person entries only) -> `ObjectTracker.update()` (stable ids) ->
     `HumanStateEstimator.estimate()` (per tracked person) -> bundles the
     result into a `VisionCycleResult` and calls
     `listener.on_vision_cycle(cycle)`. Also polls `vision.camera_status()`
     every cycle and calls `listener.on_camera_status(status)` (see that
     method's own diffing - this loop does not need to track transitions
     itself). On repeated inference/capture failures, publishes
     `SystemError` (via `listener.publish()`, the adapter's own existing
     method - no new mechanism needed) and, after
     `MAX_CONSECUTIVE_CYCLE_FAILURES` in a row, calls `listener.restart()`
     - reusing `BaseAdapter`'s already-existing stop+start restart
     machinery rather than inventing a second one (see that method's own
     docstring: "If one adapter crashes: restart it. Do not stop
     Runtime.").
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .utils import log
from .vision import VisionCycleResult, VisionListener, VisionSource

#: How many tracked-cycle failures in a row before this source asks the
#: adapter to restart itself - mirrors `BaseAdapter.MAX_CONSECUTIVE_FAILURES`'s
#: own default rather than inventing a different number.
MAX_CONSECUTIVE_CYCLE_FAILURES = 5


class RealVisionSource(VisionSource):
    def __init__(self, poll_interval_s: Optional[float] = None) -> None:
        import luno.config as legacy_config
        import luno.vision as vision_module

        self._config = legacy_config
        self._vision = vision_module
        self._poll_interval_s = poll_interval_s or max(1.0, float(getattr(legacy_config, "CAMERA_WATCH_INTERVAL_S", 5.0) or 5.0))
        self._listener: Optional[VisionListener] = None
        self._stop_flag = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._last_seen_description: Optional[str] = None

        # Sprint 8 - tracked-cycle loop state. `_tracker`/`_human_state`
        # are (re)built in `start()`, NOT here - see that method's own
        # comment for why: `/reload`'s `adapter_manager.restart_all()`
        # calls `stop()`+`start()` again (not `__init__()`), so building
        # these here would freeze TRACKING_TIMEOUT/MAX_OBJECTS at their
        # value from process launch forever, even after a `/reload`. Set
        # to harmless placeholders now; real construction happens on the
        # first (and every subsequent) `start()`.
        self._tracker = None
        self._human_state = None
        self._cycle_thread: Optional[threading.Thread] = None
        self._consecutive_cycle_failures = 0

    def start(self, listener: VisionListener) -> None:
        self._listener = listener
        # Rebuilt on EVERY start() (including the one `/reload`'s
        # `adapter_manager.restart_all()` triggers), reading
        # TRACKING_TIMEOUT/MAX_OBJECTS fresh off `self._config` each
        # time - this is what makes those two knobs genuinely reloadable
        # rather than frozen at process-launch value (see spec: "All
        # configuration must be reloadable"). A restart clearing
        # in-progress tracking state (tracked ids reset) is an accepted,
        # honest side effect of intentionally changing these values -
        # objects/people are re-detected and re-tracked (with fresh ids)
        # within one cycle either way.
        from ..vision_human_state import HumanStateEstimator
        from ..vision_tracking import ObjectTracker

        self._tracker = ObjectTracker(
            tracking_timeout_s=max(0.1, float(getattr(self._config, "TRACKING_TIMEOUT", 5.0) or 5.0)),
            max_tracked=int(getattr(self._config, "MAX_OBJECTS", 20) or 20),
        )
        self._human_state = HumanStateEstimator()
        # A FRESH Event every start() (never `.clear()` on the existing
        # one) - critical for the "cycle thread calls listener.restart()
        # from inside itself" path (see `_tracked_cycle_once` below):
        # `restart()` runs `_do_stop()` (sets THIS flag) then
        # `_do_start()` (this method again) synchronously, all before the
        # calling thread ever returns to its own `while not stop_flag.is_
        # set()` check. Each loop thread captures its OWN flag object as
        # a local variable at thread-start time (passed as an arg, not
        # read off `self` on every iteration) - if `start()` instead
        # cleared the SAME shared object, the about-to-exit old thread
        # would see it cleared again by the new start() and loop forever
        # alongside the new thread, doubling (and, on every subsequent
        # restart, multiplying) the running threads. A fresh object per
        # generation makes that race structurally impossible: the OLD
        # thread's flag was already `.set()` by `_do_stop()` and nothing
        # ever un-sets an existing Event, no matter what `self._stop_flag`
        # gets reassigned to afterward.
        stop_flag = threading.Event()
        self._stop_flag = stop_flag
        try:
            self._vision.start_watch()
        except Exception as ex:
            log(f"vision.start_watch() raised (continuing without YOLO watch): {ex}", "vision")
        if getattr(self._config, "CAMERA_VISION_WATCH_ENABLED", False):
            try:
                self._vision.start_vision_watch()
            except Exception as ex:
                log(f"vision.start_vision_watch() raised (continuing without ambient scene watch): {ex}", "vision")
        self._poll_thread = threading.Thread(target=self._poll_loop, args=(stop_flag,), daemon=True, name="luno-vision-real-source")
        self._poll_thread.start()

        self._consecutive_cycle_failures = 0
        self._cycle_thread = threading.Thread(target=self._tracked_cycle_loop, args=(stop_flag,), daemon=True, name="luno-vision-tracked-cycle")
        self._cycle_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        try:
            self._vision.stop_watch()
        except Exception:
            pass
        try:
            self._vision.stop_vision_watch()
        except Exception:
            pass
        try:
            self._vision.release_camera()
        except Exception:
            pass
        self._listener = None

    # -- loop 1: presence-watch + ambient description (UNCHANGED) -------------

    def _poll_loop(self, stop_flag: threading.Event) -> None:
        while not stop_flag.is_set():
            self._poll_once()
            stop_flag.wait(self._poll_interval_s)

    def _poll_once(self) -> None:
        listener = self._listener
        if listener is None:
            return

        try:
            labels, age_s = self._vision.last_detections()
        except Exception as ex:
            log(f"vision.last_detections() raised: {ex}", "vision")
            labels, age_s = [], None

        # P0.6.3 Section 13 - `detect_objects()` (what `_watch_loop()`
        # calls to produce `last_detections()`) has always returned `[]`
        # (never raised) on a detector failure, exactly like
        # `detect_objects_tracked()` before P0.6.2-FIX - and THIS is the
        # loop `CameraPersonEntered`/`CameraPersonLeft` (and therefore
        # Camera Automation's `human_detected`/`human_cleared`) actually
        # come from (`_update_person_presence()` below reads whatever
        # `on_detections()` reports). Without this check, a detector
        # failure here would look exactly like "no one in frame" to the
        # presence debounce and could eventually produce a FALSE
        # `CameraPersonLeft` for someone who never left - precisely what
        # Section 13 forbids ("must not invent a state transition").
        presence_error = self._vision.last_presence_detection_error()
        if presence_error is not None:
            try:
                from ..core.events import SystemError as SystemErrorEvent
                listener.publish(SystemErrorEvent(data={
                    "adapter": "vision",
                    "error_type": "vision_detection_failed",
                    "detector": "detect_objects",
                    "error": presence_error,
                }))
            except Exception as publish_ex:
                log(f"failed to publish vision_detection_failed SystemError (presence loop, ignored): {publish_ex}", "vision")
            # Deliberately do NOT call `listener.on_detections()` this
            # cycle - skipping it (rather than calling it with a
            # misleading empty list) is what keeps `_update_person_
            # presence()` from ever seeing "nobody detected" for a cycle
            # where detection never actually ran. A person already
            # tracked as present stays present; the debounce timeout
            # simply does not advance this cycle - the same honest
            # "no state transition invented" outcome Section 13 asks
            # for, without adding a second/parallel presence mechanism.
        elif age_s is not None:
            detections: List[Dict[str, Any]] = [{"label": label} for label in labels]
            try:
                listener.on_detections(detections)
            except Exception as ex:
                log(f"listener.on_detections raised: {ex}", "vision")

        try:
            description, desc_age_s = self._vision.last_vision_description()
        except Exception as ex:
            log(f"vision.last_vision_description() raised: {ex}", "vision")
            description, desc_age_s = None, None
        if description and desc_age_s is not None and description != self._last_seen_description:
            self._last_seen_description = description
            try:
                listener.on_scene_description(description)
            except Exception as ex:
                log(f"listener.on_scene_description raised: {ex}", "vision")

        try:
            listener.on_frame()
        except Exception:
            pass

    # -- loop 2: Sprint 8 tracked-object + human-pose cycle ----------------------

    def _tracked_cycle_interval_s(self) -> float:
        fps = float(getattr(self._config, "VISION_FPS", 2.0) or 2.0)
        return 1.0 / fps if fps > 0 else 1.0

    def _tracked_cycle_loop(self, stop_flag: threading.Event) -> None:
        while not stop_flag.is_set():
            interval = self._tracked_cycle_interval_s()
            started_at = time.time()
            self._tracked_cycle_once(interval)
            elapsed = time.time() - started_at
            stop_flag.wait(max(0.0, interval - elapsed))

    def _tracked_cycle_once(self, target_interval_s: float) -> None:
        listener = self._listener
        if listener is None:
            return

        t0 = time.time()
        try:
            frame = self._vision.capture_frame()
            raw_detections = self._vision.detect_objects_tracked(frame=frame)

            # P0.6.2-FIX Section 13/14 - `detect_objects_tracked()` has
            # always returned `[]` (never raised) on a detector failure
            # (e.g. a stale/mismatched YOLO checkpoint) - see that
            # function's own docstring/comments in `luno/vision.py`.
            # That non-fatal contract is unchanged here: this cycle
            # still proceeds and still calls `listener.on_vision_cycle()`
            # below exactly as before (Section 9 - "do not change
            # unrelated architecture"). What's new is ONLY that a real
            # detector failure is now ALSO surfaced as a distinct,
            # additive `SystemError` (the SAME event class/mechanism
            # this loop's own `except` branch below already uses -
            # Section 9's "reuse existing architecture", no second
            # signal type invented) carrying `error_type: "vision_
            # detection_failed"` - so a listener (this project's live
            # observer script) can tell "genuinely empty scene" apart
            # from "the detector itself is broken right now" instead of
            # both looking identical (an empty `raw_detections` list).
            # Never converts this into `human_cleared`/"no human
            # detected" itself - it does not touch `tracked`/`humans`
            # below at all; a currently-tracked person is left to the
            # SAME existing hysteresis/timeout `ObjectTracker` already
            # applies to any missed cycle (Section 9 - not this sprint's
            # scope to change tracking-loss semantics).
            detection_error = self._vision.last_tracked_detection_error()
            if detection_error is not None:
                try:
                    from ..core.events import SystemError as SystemErrorEvent
                    listener.publish(SystemErrorEvent(data={
                        "adapter": "vision",
                        "error_type": "vision_detection_failed",
                        "error": detection_error,
                    }))
                except Exception as publish_ex:
                    log(f"failed to publish vision_detection_failed SystemError (ignored): {publish_ex}", "vision")

            raw_detections = self._vision.attach_pose_keypoints(frame, raw_detections)

            tracked = self._tracker.update(raw_detections)
            lost_object_ids = list(self._tracker.lost_ids)

            humans = []
            lost_human_ids = []
            currently_tracked_person_ids = {d.id for d in tracked if d.label == "person"}
            for detection in tracked:
                if detection.label != "person":
                    continue
                humans.append(self._human_state.estimate(detection))
            for lost_id in lost_object_ids:
                if lost_id.startswith("person#"):
                    lost_human_ids.append(lost_id)
                    self._human_state.forget(lost_id)
            # also forget bookkeeping for any person id that simply isn't
            # in this cycle's tracked set anymore for any other reason -
            # keeps the estimator's own internal state from ever growing
            # beyond currently-live tracks.
            for stale_id in self._human_state.active_ids():
                if stale_id not in currently_tracked_person_ids:
                    self._human_state.forget(stale_id)

            latency_ms = (time.time() - t0) * 1000.0
            actual_fps = 1.0 / target_interval_s if target_interval_s > 0 else 0.0
            cycle = VisionCycleResult(
                objects=tracked, humans=humans,
                lost_object_ids=lost_object_ids, lost_human_ids=lost_human_ids,
                fps=round(actual_fps, 2), latency_ms=round(latency_ms, 1),
            )
            listener.on_vision_cycle(cycle)
            self._consecutive_cycle_failures = 0
        except Exception as ex:
            self._consecutive_cycle_failures += 1
            log(f"tracked vision cycle failed ({self._consecutive_cycle_failures} in a row): {ex}", "vision")
            try:
                from ..core.events import SystemError as SystemErrorEvent
                listener.publish(SystemErrorEvent(data={"adapter": "vision", "error": str(ex)}))
            except Exception:
                pass
            if self._consecutive_cycle_failures >= MAX_CONSECUTIVE_CYCLE_FAILURES:
                log(f"tracked vision cycle hit {self._consecutive_cycle_failures} consecutive failures - restarting the Vision adapter", "vision")
                self._consecutive_cycle_failures = 0
                try:
                    listener.restart()
                except Exception as restart_ex:
                    log(f"Vision adapter restart-after-crash also failed: {restart_ex}", "vision")
        finally:
            # Sprint 69.1: camera status is queried/published AFTER the
            # capture attempt above - it used to run FIRST (before
            # capture_frame()), which meant every published status
            # reflected the PREVIOUS cycle's outcome, not the one that
            # just happened (a systematic one-cycle reporting lag,
            # normally invisible at 2fps but a real gap in the "poll <->
            # camera state correlation" the Sprint 69.1 brief's
            # diagnostics explicitly ask for). Still runs regardless of
            # whether the block above succeeded, raised, or triggered a
            # restart, via `finally` - a disconnected camera is itself
            # the interesting signal, never a reason to skip reporting
            # (same rationale the original code already had, just
            # correctly ordered now).
            try:
                status = self._vision.camera_status()
                listener.on_camera_status(status)
            except Exception as ex:
                log(f"listener.on_camera_status raised: {ex}", "vision")
