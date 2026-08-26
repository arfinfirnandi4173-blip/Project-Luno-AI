"""
animation/facial.py
======================

Slow blend-shape drift for the face at rest: a resting expression is not a
static "neutral" pose - the corner of the mouth softens and relaxes, brows
lift or settle fractionally, cheeks tighten a little. `FacialIdleLayer`
drives all of this from independent low-frequency noise channels centered
on configurable resting values, so the face is always in gentle motion
without ever looking like it's emoting anything specific.

This layer is intentionally *only* about idle drift; explicit expressions
tied to the avatar's emotional state (smiling because it's happy, frowning
because it's sad, ...) belong to `animation/emotion.py`, which blends in on
top of (and can dominate) this layer via the state machine's weights.
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import FacialIdleConfig

_CH_SMILE = 60
_CH_BROW = 61
_CH_CHEEK = 62

_BLEND_MASK = ("fun", "brow_up", "brow_down", "cheek")


class FacialIdleLayer(AnimationLayer):
    def __init__(self, config: FacialIdleConfig) -> None:
        super().__init__(name="facial", weight=1.0, bone_mask=(), blend_mask=_BLEND_MASK, priority=LayerPriority.DETAIL)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t
        noise = context.noise

        smile = cfg.smile_center + noise.sample(_CH_SMILE, t, cfg.smile_speed) * cfg.smile_amplitude
        brow = noise.sample(_CH_BROW, t, cfg.brow_speed) * cfg.brow_amplitude
        cheek = noise.sample(_CH_CHEEK, t, cfg.cheek_speed) * cfg.cheek_amplitude

        pose = Pose()
        pose.set_blend("fun", max(0.0, min(1.0, smile)))
        pose.set_blend("brow_up", max(0.0, brow))
        pose.set_blend("brow_down", max(0.0, -brow))
        pose.set_blend("cheek", max(0.0, min(1.0, cheek + max(0.0, smile) * 0.3)))
        return pose
