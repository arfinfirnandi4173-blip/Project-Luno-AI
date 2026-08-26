"""
luno.routing - Intelligent AI Routing Engine sprint
=====================================================

The "Decision Engine": an ADDITIVE layer that decides, for every user
utterance, three independent questions BEFORE any LLM is ever called:

    1. What does the user actually want? (Intent Classification)
    2. Is the answer already known somewhere cheap/local? (Knowledge
       Routing - World Model -> Long-Term Memory -> Vision Memory ->
       Planner State -> Tool State -> Home Assistant State -> Internet
       Search -> only THEN the LLM)
    3. If an LLM genuinely has to answer, which one - the cheap/fast
       default (aliased "deepseek") or the expensive reasoning-capable
       one (aliased "gpt") - and should this conversation stay pinned to
       whichever it's already using (Conversation Affinity)?

This package is deliberately standalone and dependency-free from the
rest of `luno` (same convention as `luno.world_model`/
`luno.memory_retrieval`/`luno.memory_guard`): every external read (World
Model state, Memory Retrieval results, Planner/Tool state, Home
Assistant state, Tavily search) is handed in as data or an injected
callable by the ONE real call site that wires it up
(`main_runtime_demo.py`'s `PlannerBridgeModule._handle_utterance()`),
never imported/reached into directly. That's what makes every piece
here unit-testable with plain fakes, and what keeps this an ADDITIVE
layer that never bypasses or modifies Runtime, Event Bus, Planner,
Behavior Tree, Tool Manager, Home Assistant, Vision, World Model, Smart
Memory Retrieval, Dashboard, the Multi-Provider LLM Manager, Wake
Session, or Barge-In - every one of those keeps behaving exactly as it
already does; this package only ever adds a `provider`/`model` hint and
an extra context note to the `NeedLLMResponse` PlannerBridgeModule was
already about to publish, and never fabricates a tool result or a piece
of knowledge that wasn't independently verified by the system that
actually owns it.

Modules:
    models.py               - Intent / ComplexityLevel / KnowledgeSource
                               enums, RoutingDecision (the one thing
                               `DecisionEngine.decide()` returns)
    config.py                - RoutingConfig.from_env() (DEFAULT_PROVIDER,
                               REASONING_PROVIDER, SEARCH_PROVIDER,
                               ENABLE_* flags, REASONING_COMPLEXITY_THRESHOLD)
    intent_classifier.py     - classify_intent(text) -> List[Intent]
                               (rule-based, EN+ID, no I/O)
    complexity_estimator.py  - estimate_complexity(text, intents)
    knowledge_router.py      - KnowledgeRouter: cheapest-first source lookup
    web_search_router.py     - WebSearchRouter: Tavily as a knowledge-
                               retrieval step only, never an answer source
    provider_selector.py     - resolve_alias()/ProviderSelector: "deepseek"
                               vs "gpt" alias -> real (provider, model)
    affinity.py               - ConversationAffinityTracker (sticky
                               reasoning provider mid-session)
    stats.py                  - RoutingStats (decision/intent/provider-
                               alias counters + recent decisions, for the
                               Dashboard's Decision Engine panel)
    decision_engine.py        - DecisionEngine: ties all of the above
                               together into one `.decide()` call

See `main_runtime_demo.py::PlannerBridgeModule.__init__`/
`_handle_utterance` for the one real integration point, and
`luno/adapters/llm_manager.py`'s `_handle_need_llm_response`/
`_priority_order` for how `data["provider"]` (this package's output)
is consumed as an optional PER-REQUEST override that leaves every
existing caller (which never sets it) byte-identical to before this
sprint.
"""

from .affinity import ConversationAffinityTracker
from .complexity_estimator import estimate_complexity
from .config import RoutingConfig
from .confirmation import ConfirmationHandler, ConfirmationOutcome, PendingConfirmation
from .decision_engine import DecisionEngine
from .intent_classifier import classify_intent
from .knowledge_router import KnowledgeRouter, KnowledgeSourceResult
from .llm_classifier import ClassifierCache, ClassifierResult, classify_intent_llm
from .mode_state import MODE_AUTO, MODE_MANUAL, RUNTIME_MODE, LLMModeState
from .models import ComplexityLevel, Intent, KnowledgeSource, RoutingDecision
from .provider_selector import ProviderSelector, resolve_alias
from .stats import RoutingStats
from .web_search_router import WebSearchRouter

__all__ = [
    "ConversationAffinityTracker",
    "estimate_complexity",
    "RoutingConfig",
    "ConfirmationHandler",
    "ConfirmationOutcome",
    "PendingConfirmation",
    "DecisionEngine",
    "classify_intent",
    "KnowledgeRouter",
    "KnowledgeSourceResult",
    "ClassifierCache",
    "ClassifierResult",
    "classify_intent_llm",
    "LLMModeState",
    "MODE_AUTO",
    "MODE_MANUAL",
    "RUNTIME_MODE",
    "ComplexityLevel",
    "Intent",
    "KnowledgeSource",
    "RoutingDecision",
    "ProviderSelector",
    "resolve_alias",
    "RoutingStats",
    "WebSearchRouter",
]
