"""
animation/arms.py
====================

Continuous idle sway for the upper arms and elbows.

This layer only makes sense together with `RestPose.with_natural_arm_defaults`
(see `avatar/rest_pose.py`): that rest pose brings the arms down from their
raw T/A-pose bind orientation to a natural hang at the sides, and this layer
adds the small, never-repeating noise-driven sway *around* that hang - a
slight side-to-side swing, a little forward/back drift, and a hint of elbow
bend - so the arms read as relaxed and alive instead of either "frozen out
to the sides" (no rest-pose correction) or "frozen straight down" (rest-pose
correction with nothing animating on top of it).
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import ArmIdleConfig

_CH_SWAY_L = 80
_CH_SWAY_R = 81
_CH_FWD_L = 82
_CH_FWD_R = 83
_CH_ELBOW_L = 84
_CH_ELBOW_R = 85

_BONE_MASK = (bones.LEFT_UPPER_ARM, bones.RIGHT_UPPER_ARM, bones.LEFT_LOWER_ARM, bones.RIGHT_LOWER_ARM)


class ArmIdleLayer(AnimationLayer):
    def __init__(self, config: ArmIdleConfig) -> None:
        super().__init__(name="arms", weight=1.0, bone_mask=_BONE_MASK, priority=LayerPriority.AMBIENT)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t
        noise = context.noise

        sway_l = noise.sample(_CH_SWAY_L, t, cfg.sway_speed) * cfg.sway_amplitude_deg
        sway_r = noise.sample(_CH_SWAY_R, t, cfg.sway_speed) * cfg.sway_amplitude_deg
        fwd_l = noise.sample(_CH_FWD_L, t, cfg.sway_speed * 0.7) * cfg.forward_amplitude_deg
        fwd_r = noise.sample(_CH_FWD_R, t, cfg.sway_speed * 0.7) * cfg.forward_amplitude_deg
        # Elbow bend biased positive - see constraints.py's LEFT_LOWER_ARM/
        # RIGHT_LOWER_ARM hinge range docstring for why this sign (matches
        # the knee's convention: with the upper arm hanging down via
        # RestPose, a positive pitch here curls the forearm forward/up
        # instead of back across the torso).
        elbow_l = abs(noise.sample(_CH_ELBOW_L, t, cfg.elbow_speed)) * cfg.elbow_amplitude_deg
        elbow_r = abs(noise.sample(_CH_ELBOW_R, t, cfg.elbow_speed)) * cfg.elbow_amplitude_deg

        pose = Pose()
        pose.set_bone_euler(bones.LEFT_UPPER_ARM, roll_z=sway_l, pitch_x=fwd_l)
        pose.set_bone_euler(bones.RIGHT_UPPER_ARM, roll_z=sway_r, pitch_x=fwd_r)
        pose.set_bone_euler(bones.LEFT_LOWER_ARM, pitch_x=elbow_l)
        pose.set_bone_euler(bones.RIGHT_LOWER_ARM, pitch_x=elbow_r)
        return pose
