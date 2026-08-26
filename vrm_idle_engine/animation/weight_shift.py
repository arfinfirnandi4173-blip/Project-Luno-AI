"""
animation/weight_shift.py
===========================

Slow transfer of body weight between the two feet - the thing that keeps a
standing character from looking like it's bolted to the floor. Modeled as a
slow noise-perturbed oscillator driving the hips, with the spine and
shoulders *counter-rotating* against the hips (a mild contrapposto) so the
torso stays visually balanced over whichever foot currently has the weight,
and the feet/ankles tilting slightly to sell the transfer.

Because this layer and `BreathingLayer` both touch the shoulders (and
`MicroMotionLayer` touches the spine), their contributions are combined by
`Pose.compose`'s quaternion composition rather than one overwriting the
other - each layer only needs to reason about its own small slice of motion.
"""

from __future__ import annotations

import math

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import WeightShiftConfig

_NOISE_CHANNEL = 10

_BONE_MASK = (bones.HIPS, bones.SPINE, bones.LEFT_SHOULDER, bones.RIGHT_SHOULDER, bones.LEFT_FOOT, bones.RIGHT_FOOT)


class WeightShiftLayer(AnimationLayer):
    def __init__(self, config: WeightShiftConfig) -> None:
        super().__init__(name="weight_shift", weight=1.0, bone_mask=_BONE_MASK, priority=LayerPriority.AMBIENT)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t

        base = math.sin(2.0 * math.pi * cfg.speed * t)
        n = context.noise.sample(_NOISE_CHANNEL, t, speed=0.08)
        shift = base * (1.0 - cfg.noise_influence) + n * cfg.noise_influence

        hips_yaw = shift * cfg.hip_yaw_deg
        hips_roll = shift * cfg.hip_roll_deg

        pose = Pose()
        pose.set_bone_euler(bones.HIPS, yaw_y=hips_yaw, roll_z=hips_roll)
        pose.set_bone_euler(
            bones.SPINE,
            yaw_y=-hips_yaw * cfg.spine_counter_ratio,
            roll_z=-hips_roll * cfg.spine_counter_ratio * 0.6,
        )
        pose.set_bone_euler(bones.LEFT_SHOULDER, roll_z=-hips_roll * cfg.shoulder_counter_ratio)
        pose.set_bone_euler(bones.RIGHT_SHOULDER, roll_z=hips_roll * cfg.shoulder_counter_ratio)

        # Feet tilt subtly opposite to which side is "receiving" the weight.
        pose.set_bone_euler(bones.LEFT_FOOT, pitch_x=shift * cfg.foot_lift_deg)
        pose.set_bone_euler(bones.RIGHT_FOOT, pitch_x=-shift * cfg.foot_lift_deg)
        return pose
