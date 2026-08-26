"""
animation/emotion.py
======================

`EmotionLayer` turns the state machine's *blend weights* (see
`controller/state_machine.py` - every `AvatarState` has a weight in [0, 1],
and several can be simultaneously non-zero mid-transition) into concrete
blend-shape expression values and a subtle whole-body posture lean per
state (e.g. "sad" droops the head and rounds the chest forward; "happy"
lifts the chest and head slightly).

It deliberately reuses `Pose.compose` for the state -> state blending
(building one small `Pose` per active state, weighted by that state's
current blend weight) instead of writing bespoke blending math, since that
is exactly the additive-layer composition problem `Pose.compose` already
solves.

Extending with a new state
----------------------------
Add an entry to `EXPRESSION_PROFILES` and/or `POSTURE_PROFILES` keyed by the
new `AvatarState`'s `.value` string. A state with no entry in either dict
simply contributes nothing extra beyond the idle layers (this is exactly
what `AvatarState.IDLE` and `AvatarState.TALKING` do - talking's mouth
motion is `TalkingLayer`'s job, not this layer's).
"""

from __future__ import annotations

from typing import Dict, Tuple

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose

# canonical blend-shape key -> intensity, per state name
EXPRESSION_PROFILES: Dict[str, Dict[str, float]] = {
    "happy": {"joy": 0.85, "fun": 0.30},
    "sad": {"sorrow": 0.75, "brow_down": 0.20},
    "angry": {"angry": 0.85, "brow_down": 0.35},
    "embarrassed": {"fun": 0.35, "cheek": 0.55},
    "excited": {"joy": 0.60, "fun": 0.50},
    "sleepy": {"cheek": 0.10},
    "thinking": {"brow_down": 0.15},
    "listening": {"fun": 0.08},
}

# bone name -> (pitch_x, yaw_y, roll_z) in degrees, per state name
POSTURE_PROFILES: Dict[str, Dict[str, Tuple[float, float, float]]] = {
    "happy": {bones.CHEST: (-2.0, 0.0, 0.0), bones.HEAD: (-3.0, 0.0, 0.0)},
    "sad": {bones.CHEST: (3.0, 0.0, 0.0), bones.HEAD: (6.0, 0.0, 0.0), bones.NECK: (3.0, 0.0, 0.0)},
    "angry": {bones.CHEST: (-1.5, 0.0, 0.0), bones.HEAD: (2.0, 0.0, 0.0)},
    "embarrassed": {bones.HEAD: (4.0, 0.0, 0.0), bones.NECK: (2.0, 0.0, 0.0)},
    "excited": {bones.CHEST: (-3.0, 0.0, 0.0)},
    "sleepy": {bones.HEAD: (8.0, 0.0, 0.0), bones.NECK: (4.0, 0.0, 0.0)},
    "thinking": {bones.HEAD: (3.0, 8.0, 4.0)},
    "listening": {bones.HEAD: (0.0, 5.0, 0.0)},
}


def _collect_bone_mask() -> Dict[str, None]:
    keys: Dict[str, None] = {}
    for posture in POSTURE_PROFILES.values():
        for bone_name in posture:
            keys[bone_name] = None
    return keys


def _collect_blend_mask() -> Dict[str, None]:
    keys: Dict[str, None] = {}
    for expr in EXPRESSION_PROFILES.values():
        for blend_key in expr:
            keys[blend_key] = None
    return keys


class EmotionLayer(AnimationLayer):
    def __init__(self) -> None:
        # Layer Mask derived directly from the profile tables above, so it
        # can never drift out of sync with what this layer actually emits -
        # add a state's expression/posture entry and the mask widens itself.
        super().__init__(
            name="emotion",
            weight=1.0,
            bone_mask=_collect_bone_mask().keys(),
            blend_mask=_collect_blend_mask().keys(),
            priority=LayerPriority.EXPRESSION,
        )

    def update(self, context: FrameContext) -> Pose:
        weights = context.state_weights or {}
        weighted_layers = []

        for state_name, w in weights.items():
            if w <= 0.0:
                continue
            state_pose = Pose()
            for key, value in EXPRESSION_PROFILES.get(state_name, {}).items():
                state_pose.set_blend(key, value)
            for bone_name, (pitch, yaw, roll) in POSTURE_PROFILES.get(state_name, {}).items():
                state_pose.set_bone_euler(bone_name, pitch_x=pitch, yaw_y=yaw, roll_z=roll)
            weighted_layers.append((state_pose, w))

        if not weighted_layers:
            return Pose()
        return Pose.compose(weighted_layers)
