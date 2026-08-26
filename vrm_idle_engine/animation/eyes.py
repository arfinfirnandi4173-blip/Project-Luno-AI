"""
animation/eyes.py
====================

Eyes are the single biggest "is this alive or not" tell, so this layer
combines three distinct behaviours rather than one:

- **Wandering**: a slow Perlin drift, giving the eyes a lazy, ambient
  resting motion even when nothing else is happening.
- **Saccades**: real eyes jump between fixation points near-instantly and
  rest briefly, rather than smoothly panning; we model this as picking a new
  random offset on an irregular short schedule and letting a fast
  exponential-smoothing snap to it (fast enough to read as a "jump", not a
  glide).
- **Tracking**: when `FrameContext.gaze_override` is set (a focus target,
  e.g. the user's face during a conversation), wandering/saccades are
  suspended and the eyes instead converge smoothly onto that target.

While `FrameContext.talking` is True (and no `gaze_override` is set), the
wander/saccade ranges are scaled down by `EyeConfig.talking_range_scale`
(not fully frozen - a perfectly static gaze reads as dead/glassy on camera)
so the eyes stay closer to forward, consistent with `HeadGazeLayer` doing
the same for the head - together they're what makes the character read as
"addressing the listener" while answering instead of glancing around
mid-sentence.

Output is both eye-bone rotation (`LeftEye`/`RightEye`, the primary and most
widely supported route) *and* the four VRM `look*` blend shapes as a
fallback, since some avatars drive eye direction with blend shapes instead
of (or in addition to) bones.
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar import bones
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import EyeConfig
from vrm_idle_engine.math.curves import exponential_smoothing

_CH_WANDER_YAW = 30
_CH_WANDER_PITCH = 31

_BONE_MASK = (bones.LEFT_EYE, bones.RIGHT_EYE)
_BLEND_MASK = ("look_left", "look_right", "look_up", "look_down")


class EyeController(AnimationLayer):
    def __init__(self, config: EyeConfig) -> None:
        super().__init__(name="eyes", weight=1.0, bone_mask=_BONE_MASK, blend_mask=_BLEND_MASK, priority=LayerPriority.DETAIL)
        self.config = config
        self.current_yaw = 0.0
        self.current_pitch = 0.0
        self._saccade_yaw = 0.0
        self._saccade_pitch = 0.0
        self._next_saccade_at = 0.0
        self._initialized = False

    def _schedule_saccade(self, context: FrameContext) -> None:
        cfg = self.config
        self._saccade_yaw = context.rng.uniform(-cfg.saccade_range_deg, cfg.saccade_range_deg)
        self._saccade_pitch = context.rng.uniform(-cfg.saccade_range_deg * 0.6, cfg.saccade_range_deg * 0.6)
        self._next_saccade_at = context.t + context.rng.uniform(cfg.saccade_min_s, cfg.saccade_max_s)

    def update(self, context: FrameContext) -> Pose:
        cfg = self.config
        t = context.t

        if not self._initialized:
            self._next_saccade_at = t + context.rng.uniform(cfg.saccade_min_s, cfg.saccade_max_s)
            self._initialized = True

        if context.gaze_override is not None:
            focus_yaw, focus_pitch = context.gaze_override
            self.current_yaw = exponential_smoothing(self.current_yaw, focus_yaw, cfg.tracking_smooth_speed, context.dt)
            self.current_pitch = exponential_smoothing(
                self.current_pitch, focus_pitch - cfg.look_down_bias_deg, cfg.tracking_smooth_speed, context.dt
            )
        else:
            # Scale wander/saccade amplitude down while talking so the eyes
            # stay closer to forward without going perfectly static (see
            # module docstring). 1.0 (no scaling) whenever not talking.
            range_scale = cfg.talking_range_scale if context.talking else 1.0

            wander_yaw = context.noise.sample(_CH_WANDER_YAW, t, cfg.wander_speed) * cfg.wander_range_deg * range_scale
            wander_pitch = context.noise.sample(_CH_WANDER_PITCH, t, cfg.wander_speed) * cfg.wander_range_deg * 0.6 * range_scale

            if t >= self._next_saccade_at:
                self._schedule_saccade(context)

            target_yaw = wander_yaw + self._saccade_yaw * range_scale
            target_pitch = wander_pitch + self._saccade_pitch * range_scale - cfg.look_down_bias_deg

            self.current_yaw = exponential_smoothing(self.current_yaw, target_yaw, cfg.saccade_snap_speed, context.dt)
            self.current_pitch = exponential_smoothing(self.current_pitch, target_pitch, cfg.saccade_snap_speed, context.dt)

        pose = Pose()
        pose.set_bone_euler(bones.LEFT_EYE, yaw_y=self.current_yaw, pitch_x=self.current_pitch)
        pose.set_bone_euler(bones.RIGHT_EYE, yaw_y=self.current_yaw, pitch_x=self.current_pitch)

        # Blend-shape fallback (normalized against the saccade range so the
        # shapes reach ~1.0 only at fairly extreme gaze angles).
        span = max(1e-3, cfg.saccade_range_deg)
        yaw_norm = max(-1.0, min(1.0, self.current_yaw / span))
        pitch_norm = max(-1.0, min(1.0, self.current_pitch / span))
        pose.set_blend("look_left", max(0.0, -yaw_norm))
        pose.set_blend("look_right", max(0.0, yaw_norm))
        pose.set_blend("look_up", max(0.0, -pitch_norm))
        pose.set_blend("look_down", max(0.0, pitch_norm))
        return pose
