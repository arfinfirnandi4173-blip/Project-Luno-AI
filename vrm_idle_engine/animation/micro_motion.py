"""
animation/micro_motion.py
============================

Continuous, very-low-amplitude noise-driven jitter layered on top of the
rhythmic breathing/weight-shift signals. This is what stops the character
from ever looking like it's holding a fixed pose between the "big" motions -
every joint has a small, never-repeating wobble at all times.

Each bone gets its own noise channel (see `math/noise.py`'s `NoiseField`
docstring for why that keeps them decorrelated) so the wobbles don't happen
in visible unison, which is what would make them read as fake.
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import MicroMotionConfig

# Dedicated channel block for this layer (20-39), kept clear of the ranges
# used by breathing (0-9), weight_shift (10-19) and other layers.
_CH_SPINE_PITCH = 20
_CH_SPINE_ROLL = 21
_CH_HAND_L_PITCH = 22
_CH_HAND_L_YAW = 23
_CH_HAND_R_PITCH = 24
_CH_HAND_R_YAW = 25
_CH_KNEE_L = 26
_CH_KNEE_R = 27
_CH_ANKLE_L = 28
_CH_ANKLE_R = 29

_SPINE_ONLY_MASK = (bones.SPINE,)
_FULL_MASK = (
    bones.SPINE, bones.LEFT_HAND, bones.RIGHT_HAND,
    bones.LEFT_LOWER_LEG, bones.RIGHT_LOWER_LEG, bones.LEFT_FOOT, bones.RIGHT_FOOT,
)


class MicroMotionLayer(AnimationLayer):
    def __init__(self, config: MicroMotionConfig) -> None:
        # Layer Mask narrows to spine-only when `include_lower_body` is off
        # (chest-up mode) so hands/knees/ankles never receive a delta from
        # this layer at all, rather than relying on the caller to zero them.
        mask = _FULL_MASK if config.include_lower_body else _SPINE_ONLY_MASK
        super().__init__(name="micro_motion", weight=1.0, bone_mask=mask, priority=LayerPriority.AMBIENT)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t
        speed = cfg.speed
        scale = cfg.global_amplitude_scale
        noise = context.noise

        pose = Pose()

        pose.set_bone_euler(
            bones.SPINE,
            pitch_x=noise.sample(_CH_SPINE_PITCH, t, speed) * cfg.spine_deg * scale,
            roll_z=noise.sample(_CH_SPINE_ROLL, t, speed) * cfg.spine_deg * 0.6 * scale,
        )

        if not cfg.include_lower_body:
            return pose

        pose.set_bone_euler(
            bones.LEFT_HAND,
            pitch_x=noise.sample(_CH_HAND_L_PITCH, t, speed) * cfg.hand_deg * scale,
            yaw_y=noise.sample(_CH_HAND_L_YAW, t, speed) * cfg.hand_deg * scale,
        )
        pose.set_bone_euler(
            bones.RIGHT_HAND,
            pitch_x=noise.sample(_CH_HAND_R_PITCH, t, speed) * cfg.hand_deg * scale,
            yaw_y=noise.sample(_CH_HAND_R_YAW, t, speed) * cfg.hand_deg * scale,
        )

        # Knees/ankles: tiny amounts only - these bones are the easiest to
        # make look broken if overdriven, since a standing character's legs
        # are (mostly) load-bearing and rigid.
        pose.set_bone_euler(bones.LEFT_LOWER_LEG, pitch_x=noise.sample(_CH_KNEE_L, t, speed) * cfg.knee_deg * scale)
        pose.set_bone_euler(bones.RIGHT_LOWER_LEG, pitch_x=noise.sample(_CH_KNEE_R, t, speed) * cfg.knee_deg * scale)
        pose.set_bone_euler(bones.LEFT_FOOT, pitch_x=noise.sample(_CH_ANKLE_L, t, speed) * cfg.ankle_deg * scale)
        pose.set_bone_euler(bones.RIGHT_FOOT, pitch_x=noise.sample(_CH_ANKLE_R, t, speed) * cfg.ankle_deg * scale)

        return pose
