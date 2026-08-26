"""
avatar/constraints.py
========================

Bone rotation constraints - a **safety rail**, not a biomechanical IK
solver. This is an important distinction to be honest about: this engine
has no access to a specific model's actual joint limits or bind pose (see
`rest_pose.py`'s docstring), so `ConstraintTable` cannot guarantee a
"physically correct elbow can't bend backward" result for every possible
VRM model. What it *can* guarantee is that no single bone's final rotation
- however many animation layers, random events, and emotion-state postures
happened to add up on top of it this frame - ever exceeds a generous but
finite per-axis range. That turns "someone edits `config/settings.py`,
doubles an amplitude, and the avatar's head spins around" into "the head
turns further than usual, still within a sane range" - a bug becomes
visibly ugly instead of catastrophically broken.

Ranges are deliberately generous (comfortably larger than the sum of every
layer's *designed* contribution to that bone) rather than tight
biomechanical limits, and a handful of bones that only ever bend one
direction in this codebase (elbows, knees) get an asymmetric "hinge" range
to reflect that.

Lookup order for a bone's constraint:
1. An exact per-bone override in `DEFAULT_BONE_OVERRIDES` (or whatever was
   passed to `ConstraintTable(overrides=...)`).
2. A category-wide default (`spine` / `arm` / `leg` / `finger` / `eye`),
   looked up via `SkeletonRegistry.category_of`.
3. A fully permissive `BoneConstraint()` (+-180 deg every axis) if the bone
   isn't even in the skeleton's category map - i.e. constraints never
   accidentally *block* a legitimate bone just because nobody registered it
   yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.skeleton import SkeletonRegistry
from vrm_idle_engine.math.quaternion import Quaternion


@dataclass(frozen=True)
class AxisLimit:
    min_deg: float = -180.0
    max_deg: float = 180.0

    def clamp(self, value_deg: float) -> float:
        return max(self.min_deg, min(self.max_deg, value_deg))


@dataclass(frozen=True)
class BoneConstraint:
    """Per-axis limits for one bone. Any axis left unspecified defaults to
    a fully permissive +-180 deg (i.e. effectively unconstrained)."""
    pitch: AxisLimit = field(default_factory=AxisLimit)
    yaw: AxisLimit = field(default_factory=AxisLimit)
    roll: AxisLimit = field(default_factory=AxisLimit)

    def clamp_euler(self, pitch_deg: float, yaw_deg: float, roll_deg: float) -> tuple:
        return (
            self.pitch.clamp(pitch_deg),
            self.yaw.clamp(yaw_deg),
            self.roll.clamp(roll_deg),
        )


# -- category-wide defaults --------------------------------------------------

DEFAULT_CATEGORY_CONSTRAINTS: Dict[str, BoneConstraint] = {
    "spine": BoneConstraint(pitch=AxisLimit(-30, 30), yaw=AxisLimit(-25, 25), roll=AxisLimit(-20, 20)),
    # Roll gets extra headroom versus pitch/yaw: `RestPose.with_natural_arm_defaults`
    # bakes a +-75 deg roll correction into the upper arms' baseline, and a
    # random-event gesture can add another 45-70 deg of roll delta on top of
    # that composed rotation before this clamp ever sees it.
    "arm": BoneConstraint(pitch=AxisLimit(-110, 110), yaw=AxisLimit(-110, 110), roll=AxisLimit(-145, 145)),
    "leg": BoneConstraint(pitch=AxisLimit(-50, 50), yaw=AxisLimit(-25, 25), roll=AxisLimit(-35, 35)),
    "finger": BoneConstraint(pitch=AxisLimit(-110, 110), yaw=AxisLimit(-30, 30), roll=AxisLimit(-30, 30)),
    "eye": BoneConstraint(pitch=AxisLimit(-35, 35), yaw=AxisLimit(-35, 35), roll=AxisLimit(-10, 10)),
}

# -- per-bone overrides (hinges, and the head/neck/hips getting tighter
# bounds than the generic "spine" category default) -------------------------

DEFAULT_BONE_OVERRIDES: Dict[str, BoneConstraint] = {
    bones.HIPS: BoneConstraint(pitch=AxisLimit(-20, 20), yaw=AxisLimit(-25, 25), roll=AxisLimit(-20, 20)),
    bones.NECK: BoneConstraint(pitch=AxisLimit(-50, 50), yaw=AxisLimit(-70, 70), roll=AxisLimit(-35, 35)),
    bones.HEAD: BoneConstraint(pitch=AxisLimit(-55, 55), yaw=AxisLimit(-75, 75), roll=AxisLimit(-40, 40)),

    # Elbows only ever bend one way in this codebase: POSITIVE pitch, same
    # convention as the knees below. (Originally this was negative, tuned
    # back when the upper arm's rest pose was still identity/T-pose; once
    # `RestPose.with_natural_arm_defaults` started hanging the arm down,
    # that same negative bend curled the forearm back across the torso
    # instead of up/forward - hence the flip. A small amount of negative
    # slack is kept for safety.)
    bones.LEFT_LOWER_ARM: BoneConstraint(pitch=AxisLimit(-15, 130), yaw=AxisLimit(-30, 30), roll=AxisLimit(-50, 50)),
    bones.RIGHT_LOWER_ARM: BoneConstraint(pitch=AxisLimit(-15, 130), yaw=AxisLimit(-30, 30), roll=AxisLimit(-50, 50)),

    bones.LEFT_HAND: BoneConstraint(pitch=AxisLimit(-45, 45), yaw=AxisLimit(-35, 35), roll=AxisLimit(-30, 30)),
    bones.RIGHT_HAND: BoneConstraint(pitch=AxisLimit(-45, 45), yaw=AxisLimit(-35, 35), roll=AxisLimit(-30, 30)),

    # Knees: same hinge idea as elbows, bending positive in this codebase.
    bones.LEFT_LOWER_LEG: BoneConstraint(pitch=AxisLimit(-15, 130), yaw=AxisLimit(-15, 15), roll=AxisLimit(-15, 15)),
    bones.RIGHT_LOWER_LEG: BoneConstraint(pitch=AxisLimit(-15, 130), yaw=AxisLimit(-15, 15), roll=AxisLimit(-15, 15)),

    bones.LEFT_FOOT: BoneConstraint(pitch=AxisLimit(-35, 35), yaw=AxisLimit(-25, 25), roll=AxisLimit(-25, 25)),
    bones.RIGHT_FOOT: BoneConstraint(pitch=AxisLimit(-35, 35), yaw=AxisLimit(-25, 25), roll=AxisLimit(-25, 25)),
    bones.LEFT_TOES: BoneConstraint(pitch=AxisLimit(-35, 35), yaw=AxisLimit(-10, 10), roll=AxisLimit(-10, 10)),
    bones.RIGHT_TOES: BoneConstraint(pitch=AxisLimit(-35, 35), yaw=AxisLimit(-10, 10), roll=AxisLimit(-10, 10)),
}


class ConstraintTable:
    """Resolves and applies a `BoneConstraint` for any bone name."""

    def __init__(
        self,
        skeleton: SkeletonRegistry,
        overrides: Optional[Dict[str, BoneConstraint]] = None,
        category_defaults: Optional[Dict[str, BoneConstraint]] = None,
    ) -> None:
        self._skeleton = skeleton
        self._overrides = dict(DEFAULT_BONE_OVERRIDES)
        if overrides:
            self._overrides.update(overrides)
        self._category_defaults = dict(DEFAULT_CATEGORY_CONSTRAINTS)
        if category_defaults:
            self._category_defaults.update(category_defaults)
        self._permissive = BoneConstraint()

    def set_override(self, bone_name: str, constraint: BoneConstraint) -> None:
        """Register/replace a bone-specific constraint at runtime, e.g.
        after calibrating against a real model's actual joint limits."""
        self._overrides[bone_name] = constraint

    def constraint_for(self, bone_name: str) -> BoneConstraint:
        if bone_name in self._overrides:
            return self._overrides[bone_name]
        category = self._skeleton.category_of(bone_name)
        if category is not None and category in self._category_defaults:
            return self._category_defaults[category]
        return self._permissive

    def apply(self, bone_name: str, rotation: Quaternion) -> Quaternion:
        """Clamp `rotation` to `bone_name`'s constraint, round-tripping
        through Euler angles (see `Quaternion.to_euler_deg`'s docstring for
        why that's an acceptable approximation at this engine's scale)."""
        constraint = self.constraint_for(bone_name)
        pitch, yaw, roll = rotation.to_euler_deg()
        clamped = constraint.clamp_euler(pitch, yaw, roll)
        if clamped == (pitch, yaw, roll):
            return rotation
        return Quaternion.from_euler_deg(*clamped)
