"""
Avatar data model: the bone name catalogue (matching Unity's HumanBodyBones,
which is what the VMC Protocol uses), the ``Pose`` container every animation
layer reads/writes, and the pipeline that turns a composed animated-delta
pose into a validated, rest-pose-composed, constraint-clamped final pose:

- ``skeleton``     : ``SkeletonRegistry`` - the bone hierarchy/category graph.
- ``rest_pose``    : ``RestPose`` - per-bone bind orientation (identity by
                      default; calibratable per model).
- ``constraints``  : ``ConstraintTable`` - per-axis safety-rail rotation limits.
- ``pose_builder`` : ``FinalPoseBuilder`` - combines all three into the pose
                      actually sent over VMC.
"""
from vrm_idle_engine.avatar.constraints import AxisLimit, BoneConstraint, ConstraintTable
from vrm_idle_engine.avatar.pose import BoneTransform, Pose
from vrm_idle_engine.avatar.pose_builder import FinalPoseBuilder
from vrm_idle_engine.avatar.rest_pose import RestPose
from vrm_idle_engine.avatar.skeleton import BoneInfo, SkeletonRegistry

__all__ = [
    "BoneTransform",
    "Pose",
    "SkeletonRegistry",
    "BoneInfo",
    "RestPose",
    "AxisLimit",
    "BoneConstraint",
    "ConstraintTable",
    "FinalPoseBuilder",
]
