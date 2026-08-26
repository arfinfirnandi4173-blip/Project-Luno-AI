"""
animation/breathing.py
========================

Procedural breathing: a sine oscillator (the rhythmic, predictable part of a
real breath cycle) perturbed by low-frequency Perlin noise (the part where no
two breaths are ever quite identical - varying depth, slight rhythm drift).
Drives chest/upper-chest rotation directly, and hands off small fractions of
the same signal to the shoulders, neck and head so the whole upper body
reads as "this person is breathing", not just "this one bone is animated".
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import BreathingConfig

import math

# Reserve dedicated noise channel indices for this layer (must not collide
# with channel indices used by other layers sharing the same NoiseField).
_NOISE_CHANNEL = 0

# Layer Mask: breathing is only ever allowed to touch the upper torso chain.
_BONE_MASK = (bones.CHEST, bones.UPPER_CHEST, bones.LEFT_SHOULDER, bones.RIGHT_SHOULDER, bones.NECK, bones.HEAD)


class BreathingLayer(AnimationLayer):
    def __init__(self, config: BreathingConfig) -> None:
        super().__init__(name="breathing", weight=1.0, bone_mask=_BONE_MASK, priority=LayerPriority.AMBIENT)
        self.config = config

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t

        base = math.sin(2.0 * math.pi * cfg.speed * t)
        # Slow-moving noise perturbation: sampled at a much lower traversal
        # speed than the breath frequency itself so it reads as "organic
        # drift" rather than jitter riding on top of the sine.
        n = context.noise.sample(_NOISE_CHANNEL, t, speed=0.15)
        breath = base * (1.0 - cfg.noise_influence) + n * cfg.noise_influence

        pose = Pose()
        pose.set_bone_euler(bones.CHEST, pitch_x=breath * cfg.chest_amplitude_deg)
        pose.set_bone_euler(bones.UPPER_CHEST, pitch_x=breath * cfg.chest_amplitude_deg * 0.6)
        pose.set_bone_euler(bones.LEFT_SHOULDER, roll_z=breath * cfg.shoulder_amplitude_deg)
        pose.set_bone_euler(bones.RIGHT_SHOULDER, roll_z=-breath * cfg.shoulder_amplitude_deg)
        pose.set_bone_euler(bones.NECK, pitch_x=breath * cfg.neck_amplitude_deg)
        pose.set_bone_euler(bones.HEAD, pitch_x=breath * cfg.head_amplitude_deg)
        return pose
