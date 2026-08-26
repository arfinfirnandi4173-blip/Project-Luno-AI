"""
controller/animation_controller.py
=====================================

`AnimationController` is the single object an integrating application is
expected to talk to. It owns:

- the shared `NoiseField` and RNG every layer draws from,
- every `AnimationLayer` instance, wired into one `LayeredAnimator`,
- the `AvatarStateMachine`,
- the `VMCClient` that actually puts bytes on the wire,

and drives them all at a fixed frame rate (`config.fps`, default 60).

Per frame, `tick()` runs a small pipeline rather than sending a layer's
output straight to the network:

    LayeredAnimator.tick()   ->  animated-delta Pose (layers, masked, composed)
    FinalPoseBuilder.build() ->  final Pose (rest-pose composed, validated
                                  against SkeletonRegistry, constraint-clamped)
    VMCClient.send_frame()   ->  bytes on the wire

See `avatar/pose_builder.py` for why that middle stage exists.

Public API (what an external system - e.g. an AI assistant's response
pipeline - is expected to call):

- `set_state(AvatarState.HAPPY)` - smoothly cross-fade to a new emotional/
  behavioural state.
- `set_gaze_target(yaw_deg, pitch_deg)` / `clear_gaze_target()` - make the
  avatar look at (or stop looking at) a specific direction, e.g. the user's
  detected face position.
- `set_talking(True/False)` + `set_visemes({...})` - drive mouth movement
  from real lip-sync data while an utterance plays.
- `trigger_event("stretch")` - force one of the random-event gestures to
  play immediately instead of waiting for its random schedule.
- `tick()` - advance exactly one frame (call this yourself if you're
  integrating into someone else's event loop instead of using `run()`).
- `run()` / `stop()` - the engine's own blocking fixed-rate loop.
"""

from __future__ import annotations

import random
import time
from typing import Dict, Optional, Tuple

from vrm_idle_engine.animation.arms import ArmIdleLayer
from vrm_idle_engine.animation.blink import BlinkLayer
from vrm_idle_engine.animation.breathing import BreathingLayer
from vrm_idle_engine.avatar.constraints import ConstraintTable
from vrm_idle_engine.avatar.pose_builder import FinalPoseBuilder
from vrm_idle_engine.avatar.rest_pose import RestPose
from vrm_idle_engine.avatar.skeleton import SkeletonRegistry
from vrm_idle_engine.animation.emotion import EmotionLayer
from vrm_idle_engine.animation.eyes import EyeController
from vrm_idle_engine.animation.facial import FacialIdleLayer
from vrm_idle_engine.animation.fingers import FingerIdleLayer
from vrm_idle_engine.animation.head import HeadGazeLayer
from vrm_idle_engine.animation.layer import FrameContext, LayeredAnimator
from vrm_idle_engine.animation.legs import LegIdleLayer
from vrm_idle_engine.animation.micro_motion import MicroMotionLayer
from vrm_idle_engine.animation.random_events import RandomEventLayer
from vrm_idle_engine.animation.talking import TalkingLayer
from vrm_idle_engine.animation.weight_shift import WeightShiftLayer
from vrm_idle_engine.config.settings import EngineConfig
from vrm_idle_engine.controller.state_machine import AvatarState, AvatarStateMachine
from vrm_idle_engine.math.noise import NoiseField
from vrm_idle_engine.utils.logging_utils import get_logger
from vrm_idle_engine.utils.timing import FrameLimiter
from vrm_idle_engine.vmc.client import VMCClient

logger = get_logger(__name__)


# Random events that never touch an arm/hand/hip/leg bone - the subset kept
# active in `body_focus = "chest_up"` mode (see `RandomEventLayer`'s
# `_EVENT_HANDLERS`/`_BONE_MASK` for what each event actually animates).
_CHEST_UP_SAFE_EVENTS = ("deep_breath", "look_at_sky", "head_tilt")


class AnimationController:
    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig()
        self._chest_up = self.config.body_focus == "chest_up"
        if self._chest_up:
            # Force-derive these two rather than trust the caller set them
            # consistently - body_focus is the single source of truth.
            self.config.micro_motion.include_lower_body = False
            self.config.random_events.enabled_events = tuple(
                e for e in self.config.random_events.enabled_events if e in _CHEST_UP_SAFE_EVENTS
            )

        self._noise = NoiseField(
            seed=self.config.noise.seed,
            octaves=self.config.noise.octaves,
            persistence=self.config.noise.persistence,
            lacunarity=self.config.noise.lacunarity,
        )
        self._rng = random.Random(self.config.seed) if self.config.seed else random.Random()

        self.vmc = VMCClient(self.config.vmc.host, self.config.vmc.port)
        self.state_machine = AvatarStateMachine(self.config.state_machine)

        # -- final-pose pipeline: rest pose / skeleton / constraints --------
        # Public attributes on purpose: an integrator with real per-model
        # bind-pose data can call `controller.rest_pose.load_from_json(...)`
        # or `controller.constraints.set_override(...)` before `run()`/`tick()`
        # to calibrate the engine to a specific avatar.
        self.skeleton = SkeletonRegistry.default()
        # `with_natural_arm_defaults` is what brings the arms down out of
        # the raw T/A-pose bind instead of leaving them stuck out to the
        # sides - see RestPose's docstring.
        self.rest_pose = RestPose.with_natural_arm_defaults(
            self.config.rest_pose.upper_arm_drop_deg,
            axis=self.config.rest_pose.upper_arm_drop_axis,
        )
        self.constraints = ConstraintTable(self.skeleton)
        self.pose_builder = FinalPoseBuilder(self.skeleton, self.rest_pose, self.constraints)

        # -- instantiate every layer from its own config slice ------------
        self._breathing = BreathingLayer(self.config.breathing)
        self._weight_shift = WeightShiftLayer(self.config.weight_shift)
        self._micro_motion = MicroMotionLayer(self.config.micro_motion)
        self._arms = ArmIdleLayer(self.config.arms)
        self._legs = LegIdleLayer(self.config.legs)
        self._head = HeadGazeLayer(self.config.head)
        self._eyes = EyeController(self.config.eyes)
        self._blink = BlinkLayer(self.config.blink)
        self._fingers = FingerIdleLayer(self.config.fingers)
        self._facial = FacialIdleLayer(self.config.facial)
        self._emotion = EmotionLayer()
        self._talking = TalkingLayer()
        self._random_events = RandomEventLayer(self.config.random_events)

        if self._chest_up:
            # Bones just sit at the calibrated rest pose (arms hanging
            # naturally, legs standing neutral) instead of receiving a
            # T-pose or getting removed from the skeleton - only the *motion*
            # is disabled. `LayeredAnimator.tick` skips any layer with
            # `enabled = False` before it ever calls `update()`, so this also
            # saves the (small) per-frame CPU cost of computing deltas nobody
            # wants sent.
            for layer in (self._weight_shift, self._arms, self._legs, self._fingers):
                layer.enabled = False

        self.animator = LayeredAnimator()
        for layer in (
            self._breathing,
            self._weight_shift,
            self._micro_motion,
            self._arms,
            self._legs,
            self._head,
            self._eyes,
            self._blink,
            self._fingers,
            self._facial,
            self._emotion,
            self._talking,
            self._random_events,
        ):
            self.animator.add_layer(layer)

        self._start_time = time.perf_counter()
        self._last_tick_time = 0.0
        self._gaze_override: Optional[Tuple[float, float]] = None
        self._talking_flag = False
        self._running = False

    # -- public control API ------------------------------------------

    def set_state(self, state: AvatarState) -> None:
        """Smoothly cross-fade to a new avatar state."""
        self.state_machine.set_state(state, self._elapsed())

    def set_gaze_target(self, yaw_deg: float, pitch_deg: float) -> None:
        """Make the head/eyes look at a specific direction instead of idly
        wandering (e.g. 'look at the user')."""
        self._gaze_override = (yaw_deg, pitch_deg)

    def clear_gaze_target(self) -> None:
        """Resume idle head/eye wandering behaviour."""
        self._gaze_override = None

    def set_talking(self, talking: bool) -> None:
        """Toggle the talking layer on/off. While on with no real viseme
        data supplied via `set_visemes`, a generic procedural mouth-chatter
        placeholder plays instead."""
        self._talking_flag = talking
        if not talking:
            self._talking.clear_visemes()

    def set_visemes(self, visemes: Dict[str, float]) -> None:
        """Feed real per-frame viseme weights from an external lip-sync
        system (keys: vowel_a/vowel_i/vowel_u/vowel_e/vowel_o)."""
        self._talking.set_visemes(visemes)

    def trigger_event(self, name: str, duration: Optional[float] = None) -> None:
        """Force one of the `random_events` gestures (see
        `animation/random_events.py`) to start playing right now."""
        context = self._build_context()
        self._random_events.trigger(name, context, duration)

    # -- frame loop -----------------------------------------------------

    def _elapsed(self) -> float:
        return time.perf_counter() - self._start_time

    def _build_context(self) -> FrameContext:
        now = self._elapsed()
        dt = now - self._last_tick_time if self._last_tick_time else (1.0 / self.config.fps)
        return FrameContext(
            t=now,
            dt=max(1e-4, dt),
            noise=self._noise,
            rng=self._rng,
            state_weights=self.state_machine.weights,
            gaze_override=self._gaze_override,
            talking=self._talking_flag,
        )

    def tick(self) -> None:
        """Advance the engine by exactly one frame and push the resulting
        pose over VMC. Safe to call from an externally-owned loop instead of
        `run()` if you need tighter integration with another event loop."""
        now = self._elapsed()
        self.state_machine.update(now)

        context = self._build_context()
        delta_pose = self.animator.tick(context)
        final_pose = self.pose_builder.build(delta_pose)
        self._last_tick_time = now

        bone_rotations = {name: bt.rotation for name, bt in final_pose.bones.items()}
        bone_positions = {name: bt.position for name, bt in final_pose.bones.items()}
        self.vmc.send_frame(bone_rotations, final_pose.blends, bone_positions)

        if self.config.vmc.send_ok_heartbeat:
            self.vmc.maybe_send_heartbeat(self.config.vmc.ok_heartbeat_interval_s)

    def run(self) -> None:
        """Blocking fixed-rate loop at `config.fps`, running until
        Ctrl+C or `stop()` is called from another thread."""
        limiter = FrameLimiter(self.config.fps)
        self._running = True
        logger.info(
            "AnimationController running -> %s:%s @ %s fps",
            self.config.vmc.host, self.config.vmc.port, self.config.fps,
        )
        try:
            while self._running:
                self.tick()
                limiter.wait()
        except KeyboardInterrupt:
            logger.info("Stopped by user (KeyboardInterrupt).")
        finally:
            self._running = False

    def stop(self) -> None:
        """Signal `run()`'s loop to exit after the current frame."""
        self._running = False
