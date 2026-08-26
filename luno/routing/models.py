"""
models.py
=========

Typed vocabulary shared across `luno.routing`'s own submodules - pure
data, zero I/O, zero imports from the rest of `luno` (same "model
modules stay dependency-free" convention as
`luno/memory_retrieval/models.py`/`luno/world_model.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Intent(str, Enum):
    """The spec's own 15 categories. A single utterance can match more
    than one (`classify_intent()` returns a ranked list) - `primary_intent`
    on `RoutingDecision` is always `intents[0]`."""

    SMART_HOME = "smart_home"
    VISION = "vision"
    MEMORY = "memory"
    WORLD_STATE = "world_state"
    GENERAL_CHAT = "general_chat"
    GENERAL_QUESTION = "general_question"
    SEARCH_WEB = "search_web"
    REASONING = "reasoning"
    PLANNING = "planning"
    CODING = "coding"
    MULTI_STEP = "multi_step"
    SCHEDULING = "scheduling"
    DEVICE_CONTROL = "device_control"
    STATUS_QUERY = "status_query"
    AUTOMATION = "automation"


#: intents that mean "this turn needs real reasoning capability", used by
#: both `provider_selector.py` (pick the reasoning provider) and
#: `affinity.py` (decide whether a conversation is still "mid-reasoning").
REASONING_INTENTS = frozenset({Intent.REASONING, Intent.PLANNING, Intent.CODING, Intent.MULTI_STEP})


class ComplexityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


_COMPLEXITY_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "extreme": 3}


def complexity_at_least(level: ComplexityLevel, threshold: ComplexityLevel) -> bool:
    """`level >= threshold` without relying on Enum ordering (plain
    `str, Enum` members don't support `<`/`>=` themselves)."""
    return _COMPLEXITY_RANK[level.value] >= _COMPLEXITY_RANK[threshold.value]


class KnowledgeSource(str, Enum):
    """Priority order (cheapest/most-local first) - see
    `knowledge_router.py::KnowledgeRouter.route()`, which checks exactly
    this sequence and stops at the first hit."""

    WORLD_MODEL = "world_model"
    LONG_TERM_MEMORY = "long_term_memory"
    VISION_MEMORY = "vision_memory"
    PLANNER_STATE = "planner_state"
    TOOL_STATE = "tool_state"
    HOME_ASSISTANT_STATE = "home_assistant_state"
    INTERNET_SEARCH = "internet_search"
    LLM = "llm"
    NONE = "none"


#: the exact priority order `KnowledgeRouter.route()` walks - kept here
#: (not hard-coded twice) so tests and the Dashboard can both introspect
#: "what order does this actually check things in" from one place.
KNOWLEDGE_SOURCE_PRIORITY: List[KnowledgeSource] = [
    KnowledgeSource.WORLD_MODEL,
    KnowledgeSource.LONG_TERM_MEMORY,
    KnowledgeSource.VISION_MEMORY,
    KnowledgeSource.PLANNER_STATE,
    KnowledgeSource.TOOL_STATE,
    KnowledgeSource.HOME_ASSISTANT_STATE,
    KnowledgeSource.INTERNET_SEARCH,
    KnowledgeSource.LLM,
]


@dataclass
class RoutingDecision:
    """Everything `DecisionEngine.decide()` decided for one turn - the
    ONE object that feeds: (a) `NeedLLMResponse.data["provider"/"model"]`,
    (b) the `RoutingDecisionMade` event (logging/Dashboard), and (c)
    `RoutingStats.record()`. `reasoning` is a human-readable trail (NOT
    meant for the LLM prompt) explaining WHY each choice was made -
    "expose all of this transparently in logs and a Dashboard panel"."""

    request_id: str
    text: str
    intents: List[Intent]
    primary_intent: Intent
    complexity: ComplexityLevel
    complexity_score: float
    knowledge_source: KnowledgeSource
    knowledge_hit: bool
    needs_internet: bool
    needs_tools: bool
    provider_alias: str
    provider: Optional[str]
    model: Optional[str]
    affinity_applied: bool
    conversation_id: Optional[str] = None
    #: OpenAI `reasoning_effort` for this turn (see `RoutingConfig`) -
    #: `None` when routing was suppressed (manual LLM mode / auto-routing
    #: disabled) since no route was actually chosen in that case.
    reasoning_effort: Optional[str] = None
    reasoning: List[str] = field(default_factory=list)
    search_queries: List[str] = field(default_factory=list)
    search_context: Optional[str] = None
    estimated_cost_tier: str = "low"
    #: Efficient LLM Classifier sprint - `True` only when `intent_classifier
    #: .classify_intent()` found NOTHING at all (primary intent fell back to
    #: GENERAL_QUESTION/GENERAL_CHAT) AND the GPT-5.4-nano fallback
    #: classifier (`llm_classifier.py`) was actually invoked - see
    #: `decision_engine.py::decide()`'s own "ambiguous gate" comment. This
    #: is a ROUTING METADATA flag only - it is never proof a tool ran or
    #: succeeded (see that module's docstring's "Preserve Action
    #: Verification" section); `PlannerBridgeModule`/`ToolManager`/
    #: `WorldModel.update_from_tool_result()` remain the only sources of
    #: truth for whether anything actually happened.
    used_classifier: bool = False
    #: The classifier's own self-reported confidence (0.0-1.0), or `None`
    #: when `used_classifier` is `False` (never invoked) or the call itself
    #: failed (timeout/bad JSON/API error - fails closed, see
    #: `llm_classifier.py`).
    classifier_confidence: Optional[float] = None
    #: `True` when the classifier fired with MEDIUM confidence (between
    #: `RoutingConfig.classifier_confirmation_threshold` and
    #: `.classifier_confidence_threshold`) - the caller
    #: (`PlannerBridgeModule`) must ask the user to confirm via
    #: `luno.routing.confirmation.ConfirmationHandler` rather than silently
    #: acting on a guess; `DecisionEngine` itself never talks to the user
    #: (routing only, see this module's own docstring).
    needs_confirmation: bool = False
    #: Wall-clock time the classifier call itself took, or `None` when it
    #: was never invoked - efficiency-test/dashboard telemetry only (spec
    #: section 11/17), never used for any routing decision.
    classifier_latency_ms: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "text": self.text,
            "conversation_id": self.conversation_id,
            "intents": [i.value for i in self.intents],
            "primary_intent": self.primary_intent.value,
            "complexity": self.complexity.value,
            "complexity_score": round(self.complexity_score, 3),
            "knowledge_source": self.knowledge_source.value,
            "knowledge_hit": self.knowledge_hit,
            "needs_internet": self.needs_internet,
            "needs_tools": self.needs_tools,
            "provider_alias": self.provider_alias,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "affinity_applied": self.affinity_applied,
            "reasoning": list(self.reasoning),
            "search_queries": list(self.search_queries),
            "search_context": self.search_context,
            "estimated_cost_tier": self.estimated_cost_tier,
            "used_classifier": self.used_classifier,
            "classifier_confidence": self.classifier_confidence,
            "needs_confirmation": self.needs_confirmation,
            "classifier_latency_ms": self.classifier_latency_ms,
            "timestamp": self.timestamp,
        }
