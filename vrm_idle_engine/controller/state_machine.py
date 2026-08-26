"""
controller/state_machine.py
==============================

`AvatarStateMachine` tracks which high-level behavioural state the avatar is
in (Idle, Listening, Thinking, Talking, Happy, Sad, Angry, Embarrassed,
Excited, Sleepy) and, critically, never *pops* between them. Calling
`set_state(new_state, t)` starts a cross-fade: `weights` (a dict of every
state name -> a value in [0, 1]) smoothly redistributes from the old
distribution to `{new_state: 1.0}` over a configurable duration, eased with
`smoothstep` so the blend has zero velocity at both ends.

`animation/emotion.py`'s `EmotionLayer` is what actually *uses* `weights` to
mix expressions/postures - the state machine itself has no idea what a
"happy" expression looks like, it only manages *how much* of each state
should currently be blended in. This separation keeps state transition
timing/logic independent from what each state visually means.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict

from vrm_idle_engine.config.settings import StateMachineConfig
from vrm_idle_engine.math.curves import smoothstep


class AvatarState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    TALKING = "talking"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    EMBARRASSED = "embarrassed"
    EXCITED = "excited"
    SLEEPY = "sleepy"


class AvatarStateMachine:
    def __init__(self, config: StateMachineConfig, initial: AvatarState = AvatarState.IDLE) -> None:
        self.config = config
        self._current_target = initial
        self._weights: Dict[str, float] = {s.value: (1.0 if s == initial else 0.0) for s in AvatarState}
        self._from_weights: Dict[str, float] = dict(self._weights)
        self._transition_start_t = 0.0
        self._transition_duration = 0.0
        self._transitioning = False

    @property
    def current_state(self) -> AvatarState:
        return self._current_target

    @property
    def is_transitioning(self) -> bool:
        return self._transitioning

    @property
    def weights(self) -> Dict[str, float]:
        """Current per-state blend weight, always summing to ~1.0."""
        return dict(self._weights)

    def set_state(self, new_state: AvatarState, t: float) -> None:
        """Begin (or retarget mid-flight) a smooth cross-fade towards
        `new_state`. Retargeting mid-transition starts the new blend from
        whatever the *current* (possibly still-blending) weights are, so
        rapid state changes never pop."""
        if new_state == self._current_target and not self._transitioning:
            return
        self._from_weights = dict(self._weights)
        self._current_target = new_state
        self._transition_start_t = t
        self._transition_duration = (
            self.config.sleepy_transition_s
            if new_state == AvatarState.SLEEPY
            else self.config.default_transition_s
        )
        self._transitioning = True

    def update(self, t: float) -> None:
        """Advance the current cross-fade. Call once per frame before
        reading `weights`."""
        if not self._transitioning:
            return

        progress = (t - self._transition_start_t) / max(1e-4, self._transition_duration)
        if progress >= 1.0:
            self._weights = {s.value: (1.0 if s == self._current_target else 0.0) for s in AvatarState}
            self._transitioning = False
            return

        eased = smoothstep(progress)
        target_name = self._current_target.value
        new_weights: Dict[str, float] = {}
        for s in AvatarState:
            start_w = self._from_weights.get(s.value, 0.0)
            end_w = 1.0 if s.value == target_name else 0.0
            new_weights[s.value] = start_w + (end_w - start_w) * eased
        self._weights = new_weights
