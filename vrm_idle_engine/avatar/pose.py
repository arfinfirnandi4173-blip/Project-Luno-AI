"""
avatar/pose.py
===============

`Pose` is the common currency every animation layer trades in: a partial
snapshot of "where some bones should be, and what some blend shapes should
be worth, right now". Layers never talk to the network directly - each
layer's `update()` returns a `Pose`, and the `LayeredAnimator` (see
`animation/layer.py`) composites every active layer's `Pose` into one final
`Pose` that the `AnimationController` hands to `VMCClient.send_frame`.

Composition algorithm
----------------------
Naively summing Euler angles from multiple layers breaks down once
rotations get large (Euler angles don't commute), and naively averaging
quaternions isn't mathematically meaningful either. Instead, `Pose.compose`
treats every layer's rotation as a small *additive delta* from identity:
each delta is first scaled by the layer's blend weight (by slerping from
identity towards the delta - equivalent to raising the rotation to the
power of the weight), and then all scaled deltas for the same bone are
composed together with quaternion multiplication, in layer order. This is
the standard "additive animation layer" technique used in game engines, and
it degrades gracefully: at the small angles this engine actually produces
(a handful of degrees per layer), the result is visually indistinguishable
from Euler summation but remains correct even if some layer's weight is
fading in/out during a state transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Tuple

from vrm_idle_engine.math.quaternion import Quaternion, slerp


@dataclass
class BoneTransform:
    """A single bone's local rotation (+ rarely-used local position offset)."""
    rotation: Quaternion = field(default_factory=Quaternion.identity)
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)


def _scale_rotation(q: Quaternion, weight: float) -> Quaternion:
    """Scale a rotation's "amount" by `weight` in [0, 1] (0 = identity,
    1 = the full rotation) by slerping from identity towards q."""
    if weight >= 1.0:
        return q
    if weight <= 0.0:
        return Quaternion.identity()
    return slerp(Quaternion.identity(), q, weight)


class Pose:
    """Mutable bag of bone transforms + blend shape values for one frame."""

    def __init__(self) -> None:
        self.bones: Dict[str, BoneTransform] = {}
        self.blends: Dict[str, float] = {}

    # -- convenience setters ------------------------------------------

    def set_bone(self, name: str, rotation: Quaternion, position: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.bones[name] = BoneTransform(rotation, position)

    def set_bone_euler(self, name: str, pitch_x: float = 0.0, yaw_y: float = 0.0, roll_z: float = 0.0,
                        position: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.set_bone(name, Quaternion.from_euler_deg(pitch_x, yaw_y, roll_z), position)

    def set_blend(self, key: str, value: float) -> None:
        self.blends[key] = value

    def add_blend(self, key: str, value: float) -> None:
        """Add to (rather than overwrite) a blend shape value already staged
        in this pose - handy when a layer wants to nudge a shared expression
        (e.g. facial-idle "smile" plus a random-event "smile more")."""
        self.blends[key] = self.blends.get(key, 0.0) + value

    def filtered(self, bone_mask: Optional[Iterable[str]], blend_mask: Optional[Iterable[str]]) -> "Pose":
        """
        Return a copy of this pose containing only the bones/blend shapes
        allowed by the given masks. `None` for either mask means "no
        restriction" for that half of the pose. This is how a layer's
        `bone_mask`/`blend_mask` (see `animation/layer.py`) gets enforced:
        `LayeredAnimator` calls this on every layer's raw output *before*
        composing, so a layer literally cannot affect a bone or blend shape
        it wasn't scoped to touch, regardless of what its `update()` method
        actually computed.
        """
        result = Pose()
        if bone_mask is None:
            result.bones = dict(self.bones)
        else:
            allowed = bone_mask if isinstance(bone_mask, (set, frozenset)) else set(bone_mask)
            result.bones = {k: v for k, v in self.bones.items() if k in allowed}

        if blend_mask is None:
            result.blends = dict(self.blends)
        else:
            allowed_blends = blend_mask if isinstance(blend_mask, (set, frozenset)) else set(blend_mask)
            result.blends = {k: v for k, v in self.blends.items() if k in allowed_blends}
        return result

    # -- composition -------------------------------------------------------

    @staticmethod
    def compose(weighted_layers: Iterable[Tuple["Pose", float]]) -> "Pose":
        """
        Additively compose several (Pose, weight) pairs into a single final
        Pose. See module docstring for the algorithm. Blend shape values are
        weighted-summed and clamped to [0, 1].
        """
        result = Pose()
        bone_rotation_accum: Dict[str, Quaternion] = {}
        bone_position_accum: Dict[str, list] = {}
        blend_accum: Dict[str, float] = {}

        for pose, weight in weighted_layers:
            if weight <= 0.0:
                continue

            for name, transform in pose.bones.items():
                scaled = _scale_rotation(transform.rotation, weight)
                if name in bone_rotation_accum:
                    bone_rotation_accum[name] = scaled.multiply(bone_rotation_accum[name])
                else:
                    bone_rotation_accum[name] = scaled

                px, py, pz = transform.position
                if name in bone_position_accum:
                    acc = bone_position_accum[name]
                    acc[0] += px * weight
                    acc[1] += py * weight
                    acc[2] += pz * weight
                else:
                    bone_position_accum[name] = [px * weight, py * weight, pz * weight]

            for key, value in pose.blends.items():
                blend_accum[key] = blend_accum.get(key, 0.0) + value * weight

        for name, rotation in bone_rotation_accum.items():
            pos = bone_position_accum.get(name, [0.0, 0.0, 0.0])
            result.bones[name] = BoneTransform(rotation, (pos[0], pos[1], pos[2]))

        for key, value in blend_accum.items():
            result.blends[key] = max(0.0, min(1.0, value))

        return result

    @staticmethod
    def compose_prioritized(
        entries: Iterable[Tuple["Pose", float, int]],
        suppression_strength: float = 0.9,
    ) -> "Pose":
        """
        Priority-aware version of `compose`: each `(Pose, weight, priority)`
        entry is one layer's masked output. Wherever *only one* priority
        tier touches a given bone or blend shape, this behaves exactly like
        `compose`. Wherever *multiple* tiers touch the same bone/blend
        (e.g. `ArmIdleLayer`'s ambient sway and `RandomEventLayer`'s
        "stretch" gesture both rotate `LeftUpperArm`), every lower-priority
        contribution to that specific bone/blend has its effective weight
        reduced in proportion to how active the higher-priority
        contribution(s) currently are - so the deliberate gesture visibly
        *dominates* the ambient motion instead of the two fighting or
        simply stacking into an exaggerated combined rotation. Contributions
        within the *same* priority tier are combined exactly as before
        (no suppression between peers).

        `suppression_strength` (0..1) controls how completely a fully-active
        higher-priority layer can silence a lower one: 1.0 = can fully
        override (effective weight -> 0 when the higher layer is at weight
        1.0); lower values always let some ambient motion show through even
        under a dominant gesture. Default 0.9 leaves a faint trace of the
        suppressed layer, which avoids any visible "pop" the instant a
        gesture's own weight eases back down to 0.
        """
        # bone_name -> [(rotation, position, weight, priority), ...]
        bone_entries: Dict[str, list] = {}
        # blend_key -> [(value, weight, priority), ...]
        blend_entries: Dict[str, list] = {}

        for pose, weight, priority in entries:
            if weight <= 0.0:
                continue
            for name, transform in pose.bones.items():
                bone_entries.setdefault(name, []).append((transform.rotation, transform.position, weight, priority))
            for key, value in pose.blends.items():
                blend_entries.setdefault(key, []).append((value, weight, priority))

        result = Pose()

        for name, contributors in bone_entries.items():
            ordered = sorted(contributors, key=lambda c: c[3])  # stable sort by priority asc
            rotation_acc: Optional[Quaternion] = None
            pos_acc = [0.0, 0.0, 0.0]

            for rotation, position, weight, priority in ordered:
                higher_activity = sum(w for (_, _, w, p) in ordered if p > priority)
                higher_activity = min(1.0, higher_activity)
                effective_weight = max(0.0, weight * (1.0 - suppression_strength * higher_activity))
                if effective_weight <= 0.0:
                    continue

                scaled = _scale_rotation(rotation, effective_weight)
                rotation_acc = scaled if rotation_acc is None else scaled.multiply(rotation_acc)

                pos_acc[0] += position[0] * effective_weight
                pos_acc[1] += position[1] * effective_weight
                pos_acc[2] += position[2] * effective_weight

            if rotation_acc is not None:
                result.bones[name] = BoneTransform(rotation_acc, (pos_acc[0], pos_acc[1], pos_acc[2]))

        for key, contributors in blend_entries.items():
            ordered = sorted(contributors, key=lambda c: c[2])
            total = 0.0
            for value, weight, priority in ordered:
                higher_activity = sum(w for (_, w, p) in ordered if p > priority)
                higher_activity = min(1.0, higher_activity)
                effective_weight = max(0.0, weight * (1.0 - suppression_strength * higher_activity))
                total += value * effective_weight
            result.blends[key] = max(0.0, min(1.0, total))

        return result
