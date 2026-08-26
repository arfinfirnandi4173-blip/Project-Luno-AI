"""
unity.py
========

`UnityAdapter` - receives `EmotionChanged`, `BehaviorChanged`,
`AnimationRequest`, `ExpressionRequest` internal Events and forwards
them to an injected `UnityClient` interface (a real implementation
would wrap the existing `luno/vnyan_bridge.py`/`avatar_dispatch.py`
integration point - NOT reimplemented here). Mock only in this package.

    EmotionChanged/BehaviorChanged   -> client.set_emotion() / client.send_animation()
    AnimationRequest/ExpressionRequest -> client.send_animation() / client.send_expression() -> AnimationFinished
    _do_start()                          -> client.ping() -> AvatarReady
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..core.events import BehaviorChanged, EmotionChanged
from .base import BaseAdapter
from .events import AnimationFinished, AnimationRequest, AvatarReady, ExpressionRequest
from .utils import log


class UnityClient(ABC):
    @abstractmethod
    def send_animation(self, name: str, params: Optional[Dict[str, Any]] = None) -> None: ...

    @abstractmethod
    def send_expression(self, name: str, params: Optional[Dict[str, Any]] = None) -> None: ...

    @abstractmethod
    def set_emotion(self, emotion: str) -> None: ...

    @abstractmethod
    def ping(self) -> bool:
        """Cheap liveness check against the Unity/VMC endpoint - used at
        `_do_start()` to decide whether to publish `AvatarReady`."""


class MockUnityClient(UnityClient):
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.animations_played: list = []
        self.expressions_played: list = []
        self.emotions_set: list = []

    def send_animation(self, name: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.animations_played.append((name, params or {}))

    def send_expression(self, name: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.expressions_played.append((name, params or {}))

    def set_emotion(self, emotion: str) -> None:
        self.emotions_set.append(emotion)

    def ping(self) -> bool:
        return self.reachable


class UnityAdapter(BaseAdapter):
    name = "unity"

    def __init__(self, client: Optional[UnityClient] = None) -> None:
        super().__init__()
        self.client = client or MockUnityClient()

    def _do_start(self) -> None:
        if self.client.ping():
            self.publish(AvatarReady())
        else:
            log("client.ping() reports Unity/VMC endpoint unreachable - no AvatarReady published", self.name)

    def handle_event(self, event: Any) -> None:
        handlers = {
            EmotionChanged.EVENT_TYPE: self._on_emotion_changed,
            BehaviorChanged.EVENT_TYPE: self._on_behavior_changed,
            AnimationRequest.EVENT_TYPE: self._on_animation_request,
            ExpressionRequest.EVENT_TYPE: self._on_expression_request,
        }
        handler = handlers.get(event.type)
        if handler is not None:
            handler(event)

    def _on_emotion_changed(self, event: Any) -> None:
        emotion = event.get("emotion", "neutral")
        self.client.set_emotion(emotion)

    def _on_behavior_changed(self, event: Any) -> None:
        state = event.get("state") or event.get("node") or "idle"
        self.client.send_animation(state, {})
        self.publish(AnimationFinished(data={"name": state, "trigger": "behavior_changed"}))

    def _on_animation_request(self, event: Any) -> None:
        name = event.get("name", "idle")
        params = event.get("params", {})
        self.client.send_animation(name, params)
        self.publish(AnimationFinished(data={"name": name, "trigger": "animation_request"}))

    def _on_expression_request(self, event: Any) -> None:
        name = event.get("name", "neutral")
        params = event.get("params", {})
        self.client.send_expression(name, params)
        self.publish(AnimationFinished(data={"name": name, "trigger": "expression_request"}))
