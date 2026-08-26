"""
animation/random_events.py
=============================

Every 10-40 seconds (configurable), this layer fires one short procedural
"gesture" - touching hair, looking at the sky, a deep breath, a stretch,
shifting feet, touching a cheek, fidgeting with the hands, or tilting the
head - chosen unpredictably so the same gesture never becomes a
recognizable, predictable tic.

Crucially, no gesture is a recorded keyframe clip: each is a small function
of `frac` (0..1 progress through the gesture's lifetime) that computes bone
rotations procedurally using the easing curves in `math/curves.py`. Calling
the same event twice never looks perfectly identical either, because the
duration itself is randomized by +-15% each time it's picked.

Adding a new event
-------------------
1. Add its name to `RandomEventConfig.enabled_events` (config/settings.py).
2. Add an entry to `_EVENT_DURATIONS` below.
3. Write a `_ev_<name>(self, frac, pose, context)` method that fills in
   `pose` for the given progress fraction.
4. Register it in `_EVENT_HANDLERS`.
No other file needs to change.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import RandomEventConfig
from vrm_idle_engine.math.curves import ease_in_out_sine, triangle_envelope

_EVENT_DURATIONS = {
    "hair_touch": 2.5,
    "look_at_sky": 2.0,
    "deep_breath": 3.0,
    "stretch": 3.5,
    "shift_feet": 1.5,
    "cheek_touch": 2.2,
    "play_with_hands": 2.8,
    "head_tilt": 1.8,
}

# Layer Mask: every bone/blend any `_ev_*` handler below is allowed to
# touch, listed explicitly (rather than left unrestricted) so a future
# event handler that reaches for the wrong bone name gets caught by the
# mask instead of silently reaching a bone this layer was never meant to
# drive.
_BONE_MASK = (
    bones.HEAD, bones.NECK, bones.CHEST, bones.UPPER_CHEST, bones.SPINE, bones.HIPS,
    bones.LEFT_SHOULDER, bones.RIGHT_SHOULDER,
    bones.LEFT_UPPER_ARM, bones.RIGHT_UPPER_ARM,
    bones.LEFT_LOWER_ARM, bones.RIGHT_LOWER_ARM,
    bones.LEFT_HAND, bones.RIGHT_HAND,
    bones.LEFT_FOOT, bones.RIGHT_FOOT,
)
_BLEND_MASK = ("fun",)


class RandomEventLayer(AnimationLayer):
    def __init__(self, config: RandomEventConfig) -> None:
        super().__init__(name="random_events", weight=1.0, bone_mask=_BONE_MASK, blend_mask=_BLEND_MASK, priority=LayerPriority.GESTURE)
        self.config = config
        self._active: Optional[Tuple[str, float, float]] = None  # (name, start_t, duration)
        self._next_event_at = 0.0
        self._initialized = False

    # -- scheduling ------------------------------------------------------

    def _schedule_next(self, context: FrameContext) -> None:
        cfg = self.config
        self._next_event_at = context.t + context.rng.uniform(cfg.min_interval_s, cfg.max_interval_s)

    def trigger(self, name: str, context: FrameContext, duration: Optional[float] = None) -> None:
        """Force a specific event to start right now, e.g. so an external
        AI-assistant integration can ask the avatar to 'stretch' or
        'tilt its head' on cue instead of waiting for the random scheduler."""
        d = duration if duration is not None else _EVENT_DURATIONS.get(name, 2.0)
        self._active = (name, context.t, d)

    @property
    def is_playing(self) -> bool:
        return self._active is not None

    # -- main tick -----------------------------------------------------

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t

        if not self._initialized:
            self._schedule_next(context)
            self._initialized = True

        if self._active is None and cfg.enabled_events and t >= self._next_event_at:
            name = context.rng.choice(list(cfg.enabled_events))
            base_duration = _EVENT_DURATIONS.get(name, 2.0)
            duration = base_duration * context.rng.uniform(0.85, 1.15)
            self._active = (name, t, duration)
            self._schedule_next(context)

        pose = Pose()
        if self._active is not None:
            name, start, duration = self._active
            frac = (t - start) / duration
            if frac >= 1.0:
                self._active = None
            else:
                handler = self._EVENT_HANDLERS.get(name)
                if handler is not None:
                    handler(self, frac, pose, context)
        return pose

    # -- individual procedural gestures ------------------------------------
    # Each takes the current progress fraction (0..1) and fills `pose`.
    # `triangle_envelope` rises 0->1 over the first half and falls 1->0 over
    # the second, so every gesture eases smoothly into and back out of its
    # peak instead of snapping.

    def _ev_hair_touch(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        # Elbow bend sign matches the LEFT_LOWER_ARM/RIGHT_LOWER_ARM hinge
        # convention in constraints.py (positive = forearm curls up/forward,
        # not back across the torso) - see that file's docstring.
        pose.set_bone_euler(bones.RIGHT_UPPER_ARM, roll_z=-eased * 45.0)
        pose.set_bone_euler(bones.RIGHT_LOWER_ARM, pitch_x=eased * 60.0, roll_z=eased * 20.0)
        pose.set_bone_euler(bones.RIGHT_HAND, pitch_x=eased * 15.0)
        pose.set_bone_euler(bones.HEAD, roll_z=eased * 4.0, yaw_y=eased * 3.0)

    def _ev_look_at_sky(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        pose.set_bone_euler(bones.NECK, pitch_x=-eased * 10.0)
        pose.set_bone_euler(bones.HEAD, pitch_x=-eased * 18.0)

    def _ev_deep_breath(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        pose.set_bone_euler(bones.CHEST, pitch_x=eased * 6.0)
        pose.set_bone_euler(bones.UPPER_CHEST, pitch_x=eased * 4.0)
        pose.set_bone_euler(bones.LEFT_SHOULDER, roll_z=eased * 2.0)
        pose.set_bone_euler(bones.RIGHT_SHOULDER, roll_z=-eased * 2.0)

    def _ev_stretch(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        pose.set_bone_euler(bones.LEFT_UPPER_ARM, roll_z=eased * 70.0)
        pose.set_bone_euler(bones.RIGHT_UPPER_ARM, roll_z=-eased * 70.0)
        pose.set_bone_euler(bones.SPINE, pitch_x=-eased * 5.0)
        pose.set_bone_euler(bones.CHEST, pitch_x=-eased * 4.0)

    def _ev_shift_feet(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        pose.set_bone_euler(bones.HIPS, yaw_y=eased * 6.0, roll_z=eased * 3.0)
        pose.set_bone_euler(bones.LEFT_FOOT, yaw_y=eased * 8.0)

    def _ev_cheek_touch(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        # Positive LEFT_LOWER_ARM pitch (see _ev_hair_touch's comment) plus a
        # slightly smaller magnitude than before (55 vs. the old 90) so the
        # hand comfortably reaches the cheek without over-rotating the elbow.
        pose.set_bone_euler(bones.LEFT_UPPER_ARM, roll_z=-eased * 50.0)
        pose.set_bone_euler(bones.LEFT_LOWER_ARM, pitch_x=eased * 55.0)
        pose.set_bone_euler(bones.LEFT_HAND, pitch_x=eased * 10.0)
        pose.set_blend("fun", eased * 0.3)

    def _ev_play_with_hands(self, frac: float, pose: Pose, context: FrameContext) -> None:
        envelope = ease_in_out_sine(triangle_envelope(frac))
        wobble = math.sin(frac * math.pi * 4.0) * envelope
        pose.set_bone_euler(bones.LEFT_HAND, pitch_x=wobble * 10.0, yaw_y=wobble * 6.0)
        pose.set_bone_euler(bones.RIGHT_HAND, pitch_x=-wobble * 10.0, yaw_y=-wobble * 6.0)

    def _ev_head_tilt(self, frac: float, pose: Pose, context: FrameContext) -> None:
        eased = ease_in_out_sine(triangle_envelope(frac))
        pose.set_bone_euler(bones.HEAD, roll_z=eased * 12.0, yaw_y=eased * 4.0)
        pose.set_bone_euler(bones.NECK, roll_z=eased * 4.0)

    _EVENT_HANDLERS = {
        "hair_touch": _ev_hair_touch,
        "look_at_sky": _ev_look_at_sky,
        "deep_breath": _ev_deep_breath,
        "stretch": _ev_stretch,
        "shift_feet": _ev_shift_feet,
        "cheek_touch": _ev_cheek_touch,
        "play_with_hands": _ev_play_with_hands,
        "head_tilt": _ev_head_tilt,
    }
