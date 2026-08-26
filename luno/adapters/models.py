"""
models.py
=========

Small data types shared across the Adapter Layer: `AdapterConfig` (the
enable/disable + dependency/lazy config for one adapter) and
`EventMapping`/`RouteRule` - the configurable event-routing table the
spec calls for ("This mapping should be configurable. Avoid hardcoded
routing.").

`EventMapping` is deliberately plain data (a list of `RouteRule`s,
buildable from a JSON/YAML-friendly `Dict[str, List[str]]` via
`from_dict()`) so the wiring between event types and adapters lives in
configuration, not in `if event.type == "...":` chains buried in
`AdapterManager`. `DEFAULT_ADAPTER_EVENT_MAPPING` below is the spec's
own pipeline example expressed as data, not code - a real deployment is
free to load a different mapping from a config file instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AdapterConfig:
    name: str
    enabled: bool = True
    dependencies: List[str] = field(default_factory=list)
    lazy: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteRule:
    event_pattern: str
    target_adapter: str
    priority: int = 0


class EventMapping:
    """A configurable, data-driven `event_pattern -> adapter name` table.
    `AdapterManager` reads this at registration time (and whenever an
    adapter is enabled) to decide which Event Bus patterns should be
    routed to that adapter's `on_event()` - see `manager.py`.
    """

    def __init__(self, rules: Optional[List[RouteRule]] = None) -> None:
        self._rules: List[RouteRule] = list(rules) if rules else []

    @classmethod
    def from_dict(cls, mapping: Dict[str, List[str]]) -> "EventMapping":
        """`{"event_pattern": ["adapter_a", "adapter_b"], ...}` - the
        natural JSON/YAML shape for a config file."""
        rules: List[RouteRule] = []
        for pattern, targets in mapping.items():
            for target in targets:
                rules.append(RouteRule(event_pattern=pattern, target_adapter=target))
        return cls(rules)

    def add(self, event_pattern: str, target_adapter: str, priority: int = 0) -> None:
        self._rules.append(RouteRule(event_pattern, target_adapter, priority))

    def remove_for(self, target_adapter: str) -> None:
        self._rules = [r for r in self._rules if r.target_adapter != target_adapter]

    def subscriptions_for(self, target_adapter: str) -> List[Tuple[str, int]]:
        return [(r.event_pattern, r.priority) for r in self._rules if r.target_adapter == target_adapter]

    def all_rules(self) -> List[RouteRule]:
        return list(self._rules)

    def to_dict(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for r in self._rules:
            out.setdefault(r.event_pattern, []).append(r.target_adapter)
        return out


#: The spec's own pipeline sketch, expressed as data. `AdapterManager`
#: is constructed with `EventMapping.from_dict(DEFAULT_ADAPTER_EVENT_MAPPING)`
#: by default, but any deployment can pass a different mapping instead -
#: nothing in this package hardcodes routing decisions in Python logic.
DEFAULT_ADAPTER_EVENT_MAPPING: Dict[str, List[str]] = {
    "need_llm_response": ["openrouter"],
    "cancel_llm_request": ["openrouter"],
    "reload_model": ["openrouter"],
    "conversation_reset": ["openrouter"],
    "assistant_response": ["fish_audio"],
    "speak_request": ["fish_audio"],
    # LLM Streaming -> Real-Time Speech Pipeline sprint - the ONE new
    # speech-side event that sprint adds (see
    # `luno/incremental_speech.py`'s module docstring). Routed exactly
    # like `speak_request` above - `FishAudioAdapter` is still the only
    # consumer of anything speech-related.
    "speak_stream_chunk": ["fish_audio"],
    "pause_playback": ["fish_audio"],
    "resume_playback": ["fish_audio"],
    "stop_playback": ["fish_audio"],
    # Real-TTS-adapter bug fix: a barge-in FREE interrupt that lands
    # while a reply is already dispatched to Fish Audio but still
    # synthesizing (nothing playing yet, so `BargeInModule.speaking` is
    # still False and it never publishes `stop_playback` for this turn -
    # see `barge_in/manager.py`'s `_do_free_interrupt`) still always
    # publishes `cancel_llm_request` -> `llm_cancelled` for that
    # request_id. `FishAudioAdapter` listens for that same signal here
    # so it can abandon its OWN in-flight synthesis/playback for that
    # request_id too - closing a gap that was invisible with the mock
    # (near-zero synthesis time) but real against an actual multi-second
    # GPT-SoVITS/F5-TTS HTTP round trip. Purely additive - no change to
    # BargeInModule itself.
    "llm_cancelled": ["fish_audio"],
    "emotion_changed": ["unity"],
    "behavior_changed": ["unity"],
    "animation_request": ["unity"],
    "expression_request": ["unity"],
    "tool_requested": ["home_assistant"],
    "scheduled_vision_poll": ["vision"],
}
