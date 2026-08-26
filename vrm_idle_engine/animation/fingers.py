"""
animation/fingers.py
======================

Finger idle: a relaxed resting hand is never perfectly still - fingers drift
by a degree or two, and every so often the whole hand loosens (opens
slightly) or curls a touch tighter. `FingerIdleLayer` gives every one of the
30 finger bones (5 fingers x 3 segments x 2 hands) its own Perlin noise
channel for the constant micro-wiggle, plus an independent per-hand
"gesture" state machine that occasionally drives a brief, eased open/close
motion across the whole hand.

Axis convention caveat: finger curl direction is approximated as rotation
around the local X axis, with a per-side sign flip (`bones.SIDES` mirroring).
Depending on how a specific VRM model's rig was authored this may need the
sign flipped - see the `SIDE_SIGN`-equivalent note in the README.
"""

from __future__ import annotations

from typing import Dict, Optional

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import FingerIdleConfig

_CHANNEL_BASE = 50  # channels 50..59 reserved for the 10 fingers (2 hands x 5)

# Fingers curl "inward"; on a mirrored rig the right hand typically needs the
# opposite sign to curl the same visual direction as the left hand.
_SIDE_SIGN: Dict[str, float] = {"Left": 1.0, "Right": -1.0}


class FingerIdleLayer(AnimationLayer):
    def __init__(self, config: FingerIdleConfig) -> None:
        super().__init__(name="fingers", weight=1.0, bone_mask=bones.all_finger_bones(), priority=LayerPriority.DETAIL)
        self.config = config

        # Assign each (side, finger) a unique, stable noise channel.
        self._channel_of: Dict[tuple, int] = {}
        idx = _CHANNEL_BASE
        for side in bones.SIDES:
            for finger in bones.FINGER_NAMES:
                self._channel_of[(side, finger)] = idx
                idx += 1

        # Per-hand open/close gesture state: None, or {"start": t, "sign": +-1}.
        self._gesture_state: Dict[str, Optional[dict]] = {"Left": None, "Right": None}

    def _update_gestures(self, context: FrameContext) -> None:
        cfg = self.config
        t = context.t
        for side in bones.SIDES:
            state = self._gesture_state[side]
            if state is None:
                # Poisson-process-style trigger: probability this frame is
                # proportional to dt, so the *rate* (chance per minute) stays
                # correct regardless of frame rate.
                prob = (cfg.open_close_chance_per_min / 60.0) * context.dt
                if context.rng.random() < prob:
                    self._gesture_state[side] = {
                        "start": t,
                        "sign": context.rng.choice((-1.0, 1.0)),
                    }
            else:
                if t - state["start"] >= cfg.open_close_duration_s:
                    self._gesture_state[side] = None

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t
        self._update_gestures(context)

        pose = Pose()
        for side in bones.SIDES:
            sign = _SIDE_SIGN[side]
            gesture = self._gesture_state[side]
            gesture_amount = 0.0
            if gesture is not None:
                frac = (t - gesture["start"]) / cfg.open_close_duration_s
                envelope = 1.0 - abs(2.0 * frac - 1.0)  # triangle: 0 -> 1 -> 0
                gesture_amount = envelope * gesture["sign"] * cfg.open_close_amplitude_deg

            for finger in bones.FINGER_NAMES:
                channel = self._channel_of[(side, finger)]
                wiggle = context.noise.sample(channel, t, speed=0.4) * cfg.wiggle_amplitude_deg

                for segment in bones.FINGER_SEGMENTS:
                    base = cfg.base_curl_deg.get(segment, 0.0)
                    angle = sign * (base + wiggle + gesture_amount)
                    name = bones.finger_bone_name(side, finger, segment)
                    pose.set_bone_euler(name, pitch_x=angle)

        return pose
