"""
animation/head.py
===================

Head-turning behaviour: rather than a smooth continuous sway, real people
periodically pick a new place to orient their head towards and hold it for a
while (a few seconds), easing into and out of each turn. `HeadGazeLayer`
models exactly that: it retargets to a new random (yaw, pitch) direction on
an irregular schedule, occasionally choosing to look roughly at the camera
(`look_at_camera_chance`), and exponentially smooths its current orientation
towards whatever the current target is.

If `FrameContext.gaze_override` is set (e.g. an AI assistant driving the
avatar wants it to look at the user while they're talking), that overrides
the idle random-retarget behaviour entirely and the head just tracks it.

If `gaze_override` is *not* set but `FrameContext.talking` is True (and
`HeadConfig.face_forward_when_talking`, the default), the idle random
retarget schedule is paused and the head instead eases toward facing
forward - a character mid-answer should read as addressing the listener,
not glancing around the room. This is a fallback default *below*
`gaze_override` in priority, not a replacement for it: an external system
that explicitly wants the head looking at a specific detected face while
talking should still set `gaze_override` and get exactly that.
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import HeadConfig
from vrm_idle_engine.math.curves import exponential_smoothing

_CH_ROLL = 40

_BONE_MASK = (bones.NECK, bones.HEAD)


class HeadGazeLayer(AnimationLayer):
    def __init__(self, config: HeadConfig) -> None:
        super().__init__(name="head", weight=1.0, bone_mask=_BONE_MASK, priority=LayerPriority.DETAIL)
        self.config = config
        self.target_yaw = 0.0
        self.target_pitch = 0.0
        self.current_yaw = 0.0
        self.current_pitch = 0.0
        self._next_retarget_at = 0.0
        self._initialized = False

    def _pick_new_target(self, context: FrameContext) -> None:
        cfg = self.config
        if context.rng.random() < cfg.look_at_camera_chance:
            self.target_yaw = context.rng.uniform(-3.0, 3.0)
            self.target_pitch = context.rng.uniform(-2.0, 2.0)
        else:
            self.target_yaw = context.rng.uniform(-cfg.yaw_range_deg, cfg.yaw_range_deg)
            self.target_pitch = context.rng.uniform(-cfg.pitch_range_deg, cfg.pitch_range_deg)
        self._next_retarget_at = context.t + context.rng.uniform(cfg.retarget_min_s, cfg.retarget_max_s)

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config

        if not self._initialized:
            self._next_retarget_at = context.t + context.rng.uniform(cfg.retarget_min_s, cfg.retarget_max_s)
            self._initialized = True

        if context.gaze_override is not None:
            self.target_yaw, self.target_pitch = context.gaze_override
        elif context.talking and cfg.face_forward_when_talking:
            # Hold near-center instead of consulting the idle retarget
            # schedule at all while talking - the schedule itself stays
            # frozen (not advanced) so a fresh idle look-around begins
            # shortly after speech ends rather than picking up mid-cycle.
            self.target_yaw, self.target_pitch = 0.0, 0.0
        elif context.t >= self._next_retarget_at:
            self._pick_new_target(context)

        self.current_yaw = exponential_smoothing(self.current_yaw, self.target_yaw, cfg.ease_speed, context.dt)
        self.current_pitch = exponential_smoothing(self.current_pitch, self.target_pitch, cfg.ease_speed, context.dt)

        # A head is rarely held perfectly level; a slow independent noise
        # drift keeps it from looking screwed onto the neck.
        roll = context.noise.sample(_CH_ROLL, context.t, speed=0.1) * cfg.roll_influence * 3.0

        pose = Pose()
        # Neck shares a smaller fraction of the total rotation than Head so
        # the turn is distributed naturally along the cervical chain instead
        # of pivoting entirely at the skull.
        pose.set_bone_euler(bones.NECK, yaw_y=self.current_yaw * 0.35, pitch_x=self.current_pitch * 0.3)
        pose.set_bone_euler(bones.HEAD, yaw_y=self.current_yaw * 0.65, pitch_x=self.current_pitch * 0.7, roll_z=roll)
        return pose
