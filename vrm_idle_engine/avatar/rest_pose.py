"""
avatar/rest_pose.py
======================

Every animation layer in this engine computes its output as a small
rotation *delta* - "turn the head yaw_y degrees", not "the head's absolute
local rotation is exactly X". Composing those deltas (`Pose.compose`)
naturally treats identity as the implicit baseline: a bone nobody touches
this frame stays at identity.

That baseline is only correct because this engine has no per-model bind-
pose data by default - identity is the best *generic* assumption (see the
caveat in the top-level README). `RestPose` exists so that assumption is an
explicit, swappable object instead of being silently hard-coded: if you
*do* have the actual VRM model's bind-pose local rotations (exported from
Unity, or captured from a first VMC frame the model itself sends), you can
load them here, and `FinalPoseBuilder` will compose every layer's delta on
top of the *real* rest orientation instead of identity - making the engine
correct for that specific model's rig instead of only "close enough" for
models whose bind pose happens to be near-identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from vrm_idle_engine.avatar import bones
from vrm_idle_engine.math.quaternion import Quaternion


class RestPose:
    """Per-bone rest (bind) orientation. Defaults to identity for every
    bone unless overridden."""

    def __init__(self) -> None:
        self._rotations: Dict[str, Quaternion] = {}

    @classmethod
    def with_natural_arm_defaults(
        cls,
        upper_arm_drop_deg: float = -75.0,
        axis: str = "roll_z",
    ) -> "RestPose":
        """
        A `RestPose` with every bone at identity *except* the upper arms.

        VRM humanoid models are bound in either T-pose (arms fully
        horizontal) or A-pose (arms partway down); in both cases, identity
        local rotation on `UpperArm` reproduces that raw bind orientation.
        An engine that only sends identity when nothing is animating a bone
        therefore leaves the avatar's arms stuck sticking out to the sides -
        exactly the "still T-pose" symptom this default exists to fix.

        This bakes in a generic "rotate the upper arm down towards the
        body" correction as the *default* rest pose, so arms hang naturally
        even with zero calibration. **The correct sign and axis for "down"
        depend on the specific model's rig and cannot be derived from first
        principles without seeing the model move** - this engine has no way
        to render or preview a VRM file itself, so the default here is a
        reasoned guess (rotate around Z, the axis existing gesture code in
        this codebase already swings the arm on), not a verified one. If
        arms swing the wrong way (up instead of down, forward/back instead
        of sideways, or twist oddly), try flipping the sign of
        `upper_arm_drop_deg` first, then try `axis="pitch_x"` or
        `axis="yaw_y"`. `python tools/arm_tuner.py` automates cycling
        through every axis+sign combination with the current one printed to
        the console, so you can watch the avatar in VNyan and identify the
        right one directly instead of guessing blind.

        It is still just a default: call `set()` or `load_from_json()`
        afterwards with a specific model's real bind-pose data to override
        it per bone once you know it.
        """
        rest = cls()
        kwargs_left = {axis: -upper_arm_drop_deg}
        kwargs_right = {axis: upper_arm_drop_deg}
        rest.set(bones.LEFT_UPPER_ARM, Quaternion.from_euler_deg(**kwargs_left))
        rest.set(bones.RIGHT_UPPER_ARM, Quaternion.from_euler_deg(**kwargs_right))
        return rest

    def get(self, bone_name: str) -> Quaternion:
        """The rest rotation for `bone_name`, or identity if not
        explicitly calibrated."""
        return self._rotations.get(bone_name, Quaternion.identity())

    def set(self, bone_name: str, rotation: Quaternion) -> None:
        self._rotations[bone_name] = rotation

    def has_override(self, bone_name: str) -> bool:
        return bone_name in self._rotations

    def overridden_bone_names(self):
        """Every bone name with a non-identity rest rotation set. Used by
        `FinalPoseBuilder` to guarantee a calibrated bone (e.g. the corrected
        arm-hang rotation) is still sent every frame even on a frame/config
        where no animation layer happens to touch that bone - otherwise a
        bone whose *only* source of motion is a now-disabled layer would
        silently stop being sent at all, and most VMC receivers fall back to
        the raw T-pose bind for any bone they never receive data for."""
        return tuple(self._rotations.keys())

    # -- calibration (de)serialization ------------------------------------

    def load_from_dict(self, data: Dict[str, list]) -> None:
        """Load `{bone_name: [x, y, z, w]}` entries, merging into (not
        replacing) whatever is already set."""
        for name, xyzw in data.items():
            if len(xyzw) == 4:
                self._rotations[name] = Quaternion(*xyzw)

    def load_from_json(self, path: str | Path) -> None:
        path = Path(path)
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as fp:
            self.load_from_dict(json.load(fp))

    def save_to_json(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: list(q.as_tuple()) for name, q in self._rotations.items()}
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
