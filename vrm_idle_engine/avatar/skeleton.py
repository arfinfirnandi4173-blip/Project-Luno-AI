"""
avatar/skeleton.py
=====================

`SkeletonRegistry` is the engine's single source of truth for "what bones
exist and how are they connected". Every other new pipeline stage leans on
it:

- `RestPose` uses it to know which bones need a default (identity) entry.
- `FinalPoseBuilder` uses it to *validate* every bone name a layer produces
  before it's allowed anywhere near the network - an animation layer with a
  typo'd bone name (e.g. "LeftUpperArm " with a trailing space, or a bone
  that doesn't exist in the humanoid rig at all) gets caught and dropped
  here instead of silently confusing whatever VMC receiver is on the other
  end.
- `ConstraintTable` uses each bone's `category` to fall back to a
  category-wide default constraint when no bone-specific one is registered.

The hierarchy (parent links) itself isn't currently used to *transform*
poses (this engine only ever emits local rotations, exactly what
`/VMC/Ext/Bone/Pos` expects, so it never needs to walk the chain to compute
world transforms) - it's modeled here so validation, tooling, and future
features (e.g. IK, or a debug visualizer) have a real skeleton graph to work
against instead of a flat, unstructured list of strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from vrm_idle_engine.avatar import bones


@dataclass(frozen=True)
class BoneInfo:
    """One node in the skeleton graph."""
    name: str
    parent: Optional[str]
    category: str  # "spine" | "arm" | "leg" | "finger" | "eye"


def _build_default_hierarchy() -> Dict[str, BoneInfo]:
    infos: Dict[str, BoneInfo] = {}

    def add(name: str, parent: Optional[str], category: str) -> None:
        infos[name] = BoneInfo(name=name, parent=parent, category=category)

    # Spine chain
    add(bones.HIPS, None, "spine")
    add(bones.SPINE, bones.HIPS, "spine")
    add(bones.CHEST, bones.SPINE, "spine")
    add(bones.UPPER_CHEST, bones.CHEST, "spine")
    add(bones.NECK, bones.UPPER_CHEST, "spine")
    add(bones.HEAD, bones.NECK, "spine")

    # Arms
    add(bones.LEFT_SHOULDER, bones.UPPER_CHEST, "arm")
    add(bones.RIGHT_SHOULDER, bones.UPPER_CHEST, "arm")
    add(bones.LEFT_UPPER_ARM, bones.LEFT_SHOULDER, "arm")
    add(bones.RIGHT_UPPER_ARM, bones.RIGHT_SHOULDER, "arm")
    add(bones.LEFT_LOWER_ARM, bones.LEFT_UPPER_ARM, "arm")
    add(bones.RIGHT_LOWER_ARM, bones.RIGHT_UPPER_ARM, "arm")
    add(bones.LEFT_HAND, bones.LEFT_LOWER_ARM, "arm")
    add(bones.RIGHT_HAND, bones.RIGHT_LOWER_ARM, "arm")

    # Legs
    add(bones.LEFT_UPPER_LEG, bones.HIPS, "leg")
    add(bones.RIGHT_UPPER_LEG, bones.HIPS, "leg")
    add(bones.LEFT_LOWER_LEG, bones.LEFT_UPPER_LEG, "leg")
    add(bones.RIGHT_LOWER_LEG, bones.RIGHT_UPPER_LEG, "leg")
    add(bones.LEFT_FOOT, bones.LEFT_LOWER_LEG, "leg")
    add(bones.RIGHT_FOOT, bones.RIGHT_LOWER_LEG, "leg")
    add(bones.LEFT_TOES, bones.LEFT_FOOT, "leg")
    add(bones.RIGHT_TOES, bones.RIGHT_FOOT, "leg")

    # Eyes
    add(bones.LEFT_EYE, bones.HEAD, "eye")
    add(bones.RIGHT_EYE, bones.HEAD, "eye")

    # Fingers: Proximal -> Intermediate -> Distal, rooted at the hand.
    for side in bones.SIDES:
        hand = bones.LEFT_HAND if side == "Left" else bones.RIGHT_HAND
        for finger in bones.FINGER_NAMES:
            prev_parent = hand
            for segment in bones.FINGER_SEGMENTS:
                name = bones.finger_bone_name(side, finger, segment)
                add(name, prev_parent, "finger")
                prev_parent = name

    return infos


class SkeletonRegistry:
    """Read-mostly graph of every bone this engine knows about."""

    def __init__(self, bone_infos: Dict[str, BoneInfo]) -> None:
        self._bones = dict(bone_infos)

    @classmethod
    def default(cls) -> "SkeletonRegistry":
        """The standard humanoid skeleton built from `avatar/bones.py`'s
        name catalogue. This is what `AnimationController` uses unless a
        caller supplies a custom registry."""
        return cls(_build_default_hierarchy())

    def is_known(self, name: str) -> bool:
        return name in self._bones

    def get(self, name: str) -> Optional[BoneInfo]:
        return self._bones.get(name)

    def parent_of(self, name: str) -> Optional[str]:
        info = self._bones.get(name)
        return info.parent if info else None

    def category_of(self, name: str) -> Optional[str]:
        info = self._bones.get(name)
        return info.category if info else None

    def children_of(self, name: str) -> List[str]:
        return [n for n, info in self._bones.items() if info.parent == name]

    def all_bone_names(self) -> Tuple[str, ...]:
        return tuple(self._bones.keys())

    def __contains__(self, name: str) -> bool:
        return self.is_known(name)

    def __len__(self) -> int:
        return len(self._bones)
