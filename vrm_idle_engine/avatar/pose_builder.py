"""
avatar/pose_builder.py
=========================

`FinalPoseBuilder` is the last stop before a pose leaves the engine. It sits
between `LayeredAnimator.tick()` (which produces an *animated-delta* pose -
every layer's masked, composited contribution, still relative to identity)
and `VMCClient.send_frame()` (which just puts whatever it's handed on the
wire), and does three things, in order, for every bone in the delta pose:

1. **Validate against the `SkeletonRegistry`.** A bone name that isn't part
   of the known humanoid skeleton (a typo, a stale layer touching a bone
   that got renamed, ...) is dropped and logged *once* per unique bad name
   (not every frame - this runs at 60fps, so a per-frame warning would
   flood the log instantly).
2. **Compose onto the `RestPose`.** `final_rotation = rest.multiply(delta)`
   - by default `rest` is identity for every bone (see `rest_pose.py`), so
   this is a no-op unless the engine has been calibrated against a real
   model's actual bind pose, in which case every layer's small delta now
   rotates *around* that model's real rest orientation instead of around
   identity.
3. **Clamp with the `ConstraintTable`.** The composed rotation is clamped
   per-axis to that bone's registered safety-rail range (see
   `constraints.py`) before it's allowed into the final pose.

Blend shape values pass through unchanged (`Pose.compose` already clamps
them to [0, 1]) - constraints and rest-pose composition are a bones-only
concept.
"""

from __future__ import annotations

from typing import Set

from vrm_idle_engine.avatar.constraints import ConstraintTable
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.avatar.rest_pose import RestPose
from vrm_idle_engine.avatar.skeleton import SkeletonRegistry
from vrm_idle_engine.utils.logging_utils import get_logger

logger = get_logger(__name__)


class FinalPoseBuilder:
    def __init__(
        self,
        skeleton: SkeletonRegistry,
        rest_pose: RestPose,
        constraints: ConstraintTable,
    ) -> None:
        self.skeleton = skeleton
        self.rest_pose = rest_pose
        self.constraints = constraints
        self._warned_unknown_bones: Set[str] = set()

    def build(self, delta_pose: Pose) -> Pose:
        """Turn an animated-delta `Pose` (as produced by
        `LayeredAnimator.tick`) into the final `Pose` ready to send over
        VMC: validated, rest-pose-composed, and constraint-clamped."""
        final = Pose()

        for name, transform in delta_pose.bones.items():
            if not self.skeleton.is_known(name):
                if name not in self._warned_unknown_bones:
                    logger.warning("Dropping unknown bone '%s' - not registered in SkeletonRegistry", name)
                    self._warned_unknown_bones.add(name)
                continue

            rest_rotation = self.rest_pose.get(name)
            composed = rest_rotation.multiply(transform.rotation)
            constrained = self.constraints.apply(name, composed)
            final.set_bone(name, constrained, transform.position)

        # Any bone with a calibrated (non-identity) rest rotation that no
        # active layer happened to touch this frame still needs to be sent
        # as rest-rotation-composed-with-identity - otherwise disabling the
        # one layer that used to be that bone's only source of motion (e.g.
        # `body_focus = "chest_up"` turning off `ArmIdleLayer`) would mean
        # the engine stops sending that bone at all, and most VMC receivers
        # snap an unreceived bone back to its raw T/A-pose bind. This is what
        # keeps arms hanging naturally-down instead of reverting to T-pose
        # once their idle-sway layer is disabled.
        for name in self.rest_pose.overridden_bone_names():
            if name in final.bones or not self.skeleton.is_known(name):
                continue
            constrained = self.constraints.apply(name, self.rest_pose.get(name))
            final.set_bone(name, constrained, (0.0, 0.0, 0.0))

        final.blends = dict(delta_pose.blends)
        return final
