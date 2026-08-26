"""
avatar/bones.py
================

Bone name catalogue, matching Unity's `HumanBodyBones` naming (the same
names the VMC Protocol's `/VMC/Ext/Bone/Pos` message expects). Centralizing
these names here means every animation layer refers to `bones.HEAD` instead
of the magic string `"Head"`, so a typo becomes an import-time `AttributeError`
name instead of a silent no-op at runtime.
"""

from __future__ import annotations

from typing import List, Tuple

# --- Spine chain -----------------------------------------------------------
HIPS = "Hips"
SPINE = "Spine"
CHEST = "Chest"
UPPER_CHEST = "UpperChest"
NECK = "Neck"
HEAD = "Head"

SPINE_CHAIN: Tuple[str, ...] = (HIPS, SPINE, CHEST, UPPER_CHEST, NECK, HEAD)

# --- Arms --------------------------------------------------------------
LEFT_SHOULDER, RIGHT_SHOULDER = "LeftShoulder", "RightShoulder"
LEFT_UPPER_ARM, RIGHT_UPPER_ARM = "LeftUpperArm", "RightUpperArm"
LEFT_LOWER_ARM, RIGHT_LOWER_ARM = "LeftLowerArm", "RightLowerArm"
LEFT_HAND, RIGHT_HAND = "LeftHand", "RightHand"

LEFT_ARM: Tuple[str, ...] = (LEFT_SHOULDER, LEFT_UPPER_ARM, LEFT_LOWER_ARM, LEFT_HAND)
RIGHT_ARM: Tuple[str, ...] = (RIGHT_SHOULDER, RIGHT_UPPER_ARM, RIGHT_LOWER_ARM, RIGHT_HAND)

# --- Legs --------------------------------------------------------------
LEFT_UPPER_LEG, RIGHT_UPPER_LEG = "LeftUpperLeg", "RightUpperLeg"
LEFT_LOWER_LEG, RIGHT_LOWER_LEG = "LeftLowerLeg", "RightLowerLeg"
LEFT_FOOT, RIGHT_FOOT = "LeftFoot", "RightFoot"
LEFT_TOES, RIGHT_TOES = "LeftToes", "RightToes"

LEFT_LEG: Tuple[str, ...] = (LEFT_UPPER_LEG, LEFT_LOWER_LEG, LEFT_FOOT, LEFT_TOES)
RIGHT_LEG: Tuple[str, ...] = (RIGHT_UPPER_LEG, RIGHT_LOWER_LEG, RIGHT_FOOT, RIGHT_TOES)

# --- Eyes ----------------------------------------------------------------
LEFT_EYE, RIGHT_EYE = "LeftEye", "RightEye"
EYES: Tuple[str, ...] = (LEFT_EYE, RIGHT_EYE)

# --- Fingers -------------------------------------------------------------
FINGER_NAMES: Tuple[str, ...] = ("Thumb", "Index", "Middle", "Ring", "Little")
FINGER_SEGMENTS: Tuple[str, ...] = ("Proximal", "Intermediate", "Distal")
SIDES: Tuple[str, ...] = ("Left", "Right")


def finger_bone_name(side: str, finger: str, segment: str) -> str:
    """Build a finger bone name, e.g. finger_bone_name('Left', 'Index', 'Proximal')
    -> 'LeftIndexProximal'."""
    return f"{side}{finger}{segment}"


def all_finger_bones(side: str | None = None) -> List[str]:
    """All 15 finger bones for one side, or all 30 for both sides if `side`
    is None."""
    sides = (side,) if side else SIDES
    return [
        finger_bone_name(s, finger, segment)
        for s in sides
        for finger in FINGER_NAMES
        for segment in FINGER_SEGMENTS
    ]


ALL_FINGER_BONES: Tuple[str, ...] = tuple(all_finger_bones())

# --- Convenience aggregates ------------------------------------------------
UPPER_BODY: Tuple[str, ...] = SPINE_CHAIN + LEFT_ARM + RIGHT_ARM
LOWER_BODY: Tuple[str, ...] = LEFT_LEG + RIGHT_LEG
ALL_HUMANOID_BONES: Tuple[str, ...] = UPPER_BODY + LOWER_BODY + EYES + ALL_FINGER_BONES
