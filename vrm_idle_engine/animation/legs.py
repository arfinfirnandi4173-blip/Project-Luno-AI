"""
animation/legs.py
====================

Continuous idle motion for the thighs and toes - the two humanoid bones
that were previously never touched by any layer (`MicroMotionLayer`
already covers knees and ankles). Kept deliberately subtle: a standing
character's legs are load-bearing, so unlike arms there's no "obviously
correct" bigger motion to add here - too much thigh rotation reads as the
character floating or the knee joint breaking, not as being alive. This
layer's job is just to close the "which bones does nothing ever touch"
gap, not to add a second weight-shift system (that's `WeightShiftLayer`'s
job already, via the hips).
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import LegIdleConfig

_CH_THIGH_L_YAW = 90
_CH_THIGH_L_ROLL = 91
_CH_THIGH_R_YAW = 92
_CH_THIGH_R_ROLL = 93
_CH_TOE_L = 94
_CH_TOE_R = 95

_BONE_MASK = (bones.LEFT_UPPER_LEG, bones.RIGHT_UPPER_LEG, bones.LEFT_TOES, bones.RIGHT_TOES)


class LegIdleLayer(AnimationLayer):
    def __init__(self, config: LegIdleConfig) -> None:
        super().__init__(name="legs", weight=1.0, bone_mask=_BONE_MASK, priority=LayerPriority.AMBIENT)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t
        noise = context.noise

        thigh_l_yaw = noise.sample(_CH_THIGH_L_YAW, t, cfg.thigh_speed) * cfg.thigh_amplitude_deg
        thigh_l_roll = noise.sample(_CH_THIGH_L_ROLL, t, cfg.thigh_speed) * cfg.thigh_amplitude_deg * 0.6
        thigh_r_yaw = noise.sample(_CH_THIGH_R_YAW, t, cfg.thigh_speed) * cfg.thigh_amplitude_deg
        thigh_r_roll = noise.sample(_CH_THIGH_R_ROLL, t, cfg.thigh_speed) * cfg.thigh_amplitude_deg * 0.6

        toe_l = noise.sample(_CH_TOE_L, t, cfg.toe_speed) * cfg.toe_amplitude_deg
        toe_r = noise.sample(_CH_TOE_R, t, cfg.toe_speed) * cfg.toe_amplitude_deg

        pose = Pose()
        pose.set_bone_euler(bones.LEFT_UPPER_LEG, yaw_y=thigh_l_yaw, roll_z=thigh_l_roll)
        pose.set_bone_euler(bones.RIGHT_UPPER_LEG, yaw_y=thigh_r_yaw, roll_z=thigh_r_roll)
        pose.set_bone_euler(bones.LEFT_TOES, pitch_x=toe_l)
        pose.set_bone_euler(bones.RIGHT_TOES, pitch_x=toe_r)
        return pose
