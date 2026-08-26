"""
animation/talking.py
======================

`TalkingLayer` is the integration hook for lip-sync. This engine has no
audio/text pipeline of its own (that lives in whatever AI-assistant system
integrates it), so when no real viseme data is supplied it falls back to a
procedural "generic mouth chatter": while `FrameContext.talking` is true, it
cycles smoothly through the VRM vowel blend shapes (aa/ih/ou/ee/oh) using
noise so the mouth reads as "talking" without repeating a fixed pattern.

Wiring in real lip-sync
-------------------------
Call `TalkingLayer.set_visemes({...})` every frame with your own
analysis/TTS-driven viseme weights (same canonical keys: `vowel_a`,
`vowel_i`, `vowel_u`, `vowel_e`, `vowel_o`) *before* `AnimationController`
ticks; when visemes have been set this way during the current talking
session, they take priority over the generic procedural fallback.
"""

from __future__ import annotations

from typing import Dict, Optional

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar.pose import Pose

_VOWEL_KEYS = ("vowel_a", "vowel_i", "vowel_u", "vowel_e", "vowel_o")
_CH_BASE = 70


class TalkingLayer(AnimationLayer):
    def __init__(self) -> None:
        super().__init__(name="talking", weight=1.0, bone_mask=(), blend_mask=_VOWEL_KEYS, priority=LayerPriority.OVERRIDE)
        self._external_visemes: Optional[Dict[str, float]] = None

    def set_visemes(self, visemes: Dict[str, float]) -> None:
        """Feed real viseme weights in from an external lip-sync system for
        the current frame. Call again every frame while talking; call with
        `None` (or simply stop calling and let `context.talking` go False)
        once the utterance ends.

        Note: only keys in `_VOWEL_KEYS` (this layer's blend mask) will
        actually reach the output pose - any other key is silently dropped
        by `LayeredAnimator`'s masking. Route non-mouth expression data
        through a different layer/key instead of overloading this one."""
        self._external_visemes = dict(visemes)

    def clear_visemes(self) -> None:
        self._external_visemes = None

    def update(self, context: FrameContext) -> Pose:
        pose = Pose()
        if not context.talking:
            self._external_visemes = None
            return pose

        if self._external_visemes is not None:
            for key, value in self._external_visemes.items():
                pose.set_blend(key, value)
            return pose

        # Generic procedural fallback: smoothly cycle mouth shape weights so
        # the mouth stays in continuous, non-repeating motion. This is not a
        # substitute for real lip-sync, just a plausible placeholder.
        t = context.t
        raw = [max(0.0, context.noise.sample(_CH_BASE + i, t, speed=2.2)) for i in range(len(_VOWEL_KEYS))]
        total = sum(raw) or 1.0
        for key, value in zip(_VOWEL_KEYS, raw):
            pose.set_blend(key, value / total)
        return pose
