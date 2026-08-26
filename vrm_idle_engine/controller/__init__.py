"""
Controller layer: the ``AvatarStateMachine`` (Idle/Listening/Thinking/...)
and the top-level ``AnimationController`` that owns every animation layer,
ticks them once per frame, and streams the resulting pose over VMC.
"""
from vrm_idle_engine.controller.state_machine import AvatarState, AvatarStateMachine
from vrm_idle_engine.controller.animation_controller import AnimationController

__all__ = ["AvatarState", "AvatarStateMachine", "AnimationController"]
