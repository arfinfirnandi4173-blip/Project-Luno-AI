"""
test_knowledge_router.py
==========================

`KnowledgeRouter.route()` - priority order (World Model -> Long-Term
Memory -> Vision Memory -> Planner State -> Tool State -> Home
Assistant State -> none) using plain fakes, exactly like every other
module in this package (never touches real `luno.world_model`/
`luno.memory_retrieval`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from luno.routing.knowledge_router import KnowledgeRouter
from luno.routing.models import KnowledgeSource


@dataclass
class _FakeMemory:
    text: str
    source: str
    score: float = 1.0
    raw: Any = None


def test_world_model_hit_wins_over_everything_else():
    router = KnowledgeRouter()
    result = router.route(
        text="is the bedroom light on",
        world_model_entities={"light.bedroom_light": "on"},
        relevant_memories=[_FakeMemory(text="a memory", source="long_term_memory")],
        tool_state_hit=True,
    )
    assert result.source == KnowledgeSource.WORLD_MODEL
    assert result.hit is True


def test_long_term_memory_wins_when_no_world_model_match():
    router = KnowledgeRouter()
    result = router.route(
        text="what do I like to eat",
        world_model_entities={"light.bedroom_light": "on"},
        relevant_memories=[_FakeMemory(text="likes pizza", source="long_term_memory")],
    )
    assert result.source == KnowledgeSource.LONG_TERM_MEMORY


def test_vision_memory_checked_before_planner_state():
    router = KnowledgeRouter()
    result = router.route(
        text="where is my cup",
        relevant_memories=[
            _FakeMemory(text="planner ran", source="planner_state"),
            _FakeMemory(text="cup on desk", source="vision_objects"),
        ],
    )
    assert result.source == KnowledgeSource.VISION_MEMORY


def test_planner_state_hit():
    router = KnowledgeRouter()
    result = router.route(
        text="what did you just do",
        relevant_memories=[_FakeMemory(text="ran plan_123", source="planner_state")],
    )
    assert result.source == KnowledgeSource.PLANNER_STATE


def test_tool_state_hit_when_no_memory_source_matched():
    router = KnowledgeRouter()
    result = router.route(
        text="did that work",
        tool_state_hit=True,
        tool_state_detail="home_assistant.turn_on",
    )
    assert result.source == KnowledgeSource.TOOL_STATE
    assert result.detail == "home_assistant.turn_on"


def test_ha_state_hit_as_last_resort_before_internet():
    router = KnowledgeRouter()
    result = router.route(text="something", ha_state_hit=True)
    assert result.source == KnowledgeSource.HOME_ASSISTANT_STATE


def test_no_source_matches_returns_none():
    router = KnowledgeRouter()
    result = router.route(text="tell me a joke")
    assert result.source == KnowledgeSource.NONE
    assert result.hit is False


def test_checked_list_records_every_source_in_order():
    router = KnowledgeRouter()
    result = router.route(text="tell me a joke")
    checked_sources = [c[0] for c in result.checked]
    assert checked_sources == [
        "world_model", "long_term_memory", "vision_memory",
        "planner_state", "tool_state", "home_assistant_state",
    ]


def test_world_model_token_overlap_is_case_insensitive_and_handles_underscores():
    router = KnowledgeRouter()
    result = router.route(
        text="Is the Bedroom Light on?",
        world_model_entities={"light.bedroom_light": "on"},
    )
    assert result.hit is True


def test_empty_world_model_never_false_positives():
    router = KnowledgeRouter()
    result = router.route(text="anything at all", world_model_entities={})
    assert result.source != KnowledgeSource.WORLD_MODEL


def test_never_raises_on_missing_optional_args():
    router = KnowledgeRouter()
    result = router.route(text="")
    assert result.source == KnowledgeSource.NONE
