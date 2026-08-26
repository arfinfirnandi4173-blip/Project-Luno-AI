"""
animation/blink.py
====================

Blink timing modeled as a small explicit state machine
(`waiting -> closing -> [hold] -> opening -> waiting`) rather than a fixed
duration triangle wave, so that three realistic variants can be layered on
top of the "normal" blink at random:

- **Double blink**: opens fully then immediately closes again for a second,
  shorter blink.
- **Slow blink**: a lazy, longer close/open (the kind people do when
  relaxed or thoughtful).
- **Sleepy blink**: closes, *holds shut* briefly, then opens - as if
  fighting drowsiness.

Which variant (if any) happens is chosen by weighted random draw each time a
new blink starts, using probabilities from `BlinkConfig`.
"""

from __future__ import annotations

from vrm_idle_engine.animation.layer import AnimationLayer, FrameContext, LayerPriority
from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.config.settings import BlinkConfig

_WAITING = "waiting"
_CLOSING = "closing"
_HOLD = "hold"
_OPENING = "opening"

_BLEND_MASK = ("blink_left", "blink_right", "blink")


class BlinkLayer(AnimationLayer):
    def __init__(self, config: BlinkConfig) -> None:
        super().__init__(name="blink", weight=1.0, bone_mask=(), blend_mask=_BLEND_MASK, priority=LayerPriority.DETAIL)
        self.config = config

        self._phase = _WAITING
        self._phase_start = 0.0
        self._close_duration = config.close_duration_s
        self._open_duration = config.open_duration_s
        self._hold_duration = 0.0
        self._pending_double = False
        self._next_blink_at = 0.0
        self._initialized = False
        self._value = 0.0

    # -- internal helpers --------------------------------------------

    def _schedule_next(self, context: FrameContext) -> None:
        cfg = self.config
        self._next_blink_at = context.t + context.rng.uniform(cfg.min_interval_s, cfg.max_interval_s)

    def _start_blink(self, context: FrameContext) -> None:
        cfg = self.config
        roll = context.rng.random()

        self._close_duration = cfg.close_duration_s
        self._open_duration = cfg.open_duration_s
        self._hold_duration = 0.0
        self._pending_double = False

        # Mutually-exclusive variant selection via cumulative probability.
        if roll < cfg.sleepy_blink_chance:
            self._hold_duration = cfg.sleepy_hold_s
            self._close_duration *= 1.5
            self._open_duration *= 1.8
        elif roll < cfg.sleepy_blink_chance + cfg.slow_blink_chance:
            self._close_duration *= cfg.slow_blink_multiplier
            self._open_duration *= cfg.slow_blink_multiplier
        elif roll < cfg.sleepy_blink_chance + cfg.slow_blink_chance + cfg.double_blink_chance:
            self._pending_double = True

        self._phase = _CLOSING
        self._phase_start = context.t

    # -- main tick -----------------------------------------------------

    def update(self, context: FrameContext) -> Pose:
        t = context.t

        if not self._initialized:
            self._schedule_next(context)
            self._initialized = True

        if self._phase == _WAITING:
            self._value = 0.0
            if t >= self._next_blink_at:
                self._start_blink(context)

        elif self._phase == _CLOSING:
            elapsed = t - self._phase_start
            frac = elapsed / max(1e-4, self._close_duration)
            self._value = min(1.0, frac)
            if frac >= 1.0:
                self._phase = _HOLD if self._hold_duration > 0.0 else _OPENING
                self._phase_start = t

        elif self._phase == _HOLD:
            self._value = 1.0
            if t - self._phase_start >= self._hold_duration:
                self._phase = _OPENING
                self._phase_start = t

        elif self._phase == _OPENING:
            elapsed = t - self._phase_start
            frac = elapsed / max(1e-4, self._open_duration)
            self._value = max(0.0, 1.0 - frac)
            if frac >= 1.0:
                if self._pending_double:
                    self._pending_double = False
                    self._phase = _WAITING
                    # Very short gap before the second blink of the pair.
                    self._next_blink_at = t + context.rng.uniform(0.08, 0.16)
                else:
                    self._phase = _WAITING
                    self._schedule_next(context)

        pose = Pose()
        pose.set_blend("blink_left", self._value)
        pose.set_blend("blink_right", self._value)
        pose.set_blend("blink", self._value)
        return pose
