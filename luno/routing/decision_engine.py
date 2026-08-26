"""
decision_engine.py
====================

`DecisionEngine` - ties every other module in this package into the one
call `PlannerBridgeModule._handle_utterance()` makes per turn:
`.decide(...) -> RoutingDecision`. Pure orchestration - all the actual
policy lives in `intent_classifier.py`/`complexity_estimator.py`/
`knowledge_router.py`/`provider_selector.py`/`affinity.py`/
`web_search_router.py`; this class just calls them in the right order
and assembles the result.

Safety rules (spec's own list, honored here structurally, not just by
convention):
    - "Never bypass Planner/Tool Manager" - this class never executes a
      tool or claims one did; `needs_tools`/tool-state knowledge is
      READ from `plan_tasks` the Planner already ran, never invoked here.
    - "Never fabricate a result" - `knowledge_hit` is only ever True when
      an already-verified source (World Model/Memory/Vision/Planner/Tool
      state) actually matched; internet search results are handed back
      as CONTEXT (`search_context`) for the LLM to synthesize, never
      returned as a ready-made answer.
    - "Always verify smart-home actions" - out of scope for this class
      entirely; that's `PlannerBridgeModule`/`WorldModel.
      update_from_tool_result()`'s job, untouched by this sprint.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .affinity import ConversationAffinityTracker
from .complexity_estimator import estimate_complexity
from .config import RoutingConfig
from .intent_classifier import classify_intent
from .knowledge_router import KnowledgeRouter
from .llm_classifier import ClassifierCache, classify_intent_llm
from .mode_state import MODE_MANUAL, RUNTIME_MODE, LLMModeState
from .models import Intent, RoutingDecision
from .provider_selector import ProviderSelector, resolve_alias
from .stats import RoutingStats
from .web_search_router import WebSearchRouter

#: Ambiguous gate (spec section 5/6/17) - the classifier is only ever
#: invoked when the DETERMINISTIC `classify_intent()` found literally
#: nothing to match at all (fell all the way through to its own
#: GENERAL_QUESTION/GENERAL_CHAT fallback - see that function's own
#: docstring). Any other outcome means at least one real keyword/regex
#: rule fired, which this project's whole `luno/planner/parser.py`/
#: `luno/vision_intent.py`/`luno/environment_intent.py`-style
#: "deterministic first, LLM only as a last resort" convention already
#: treats as confident enough to act on without a second, paid opinion.
_AMBIGUOUS_FALLBACK_INTENTS = frozenset({Intent.GENERAL_QUESTION, Intent.GENERAL_CHAT})

_TIME_SENSITIVE_WORDS = (
    "today", "right now", "currently", "latest", "current", "this week", "news", "weather",
    "hari ini", "sekarang", "terbaru", "terkini", "cuaca", "berita", "harga", "skor", "minggu ini",
)


def _looks_time_sensitive(lower_text: str) -> bool:
    return any(w in lower_text for w in _TIME_SENSITIVE_WORDS)


class DecisionEngine:
    def __init__(
        self,
        config: Optional[RoutingConfig] = None,
        *,
        search_router: Optional[WebSearchRouter] = None,
        knowledge_router: Optional[KnowledgeRouter] = None,
        mode_state: Optional[LLMModeState] = None,
        classifier_client: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config or RoutingConfig.from_env()
        self.knowledge_router = knowledge_router or KnowledgeRouter()
        self.provider_selector = ProviderSelector(self.config)
        self.affinity = ConversationAffinityTracker(self.config)
        self.stats = RoutingStats()
        self.search_router = search_router or WebSearchRouter()
        # Runtime auto/manual LLM mode override (see mode_state.py's own
        # docstring) - defaults to the process-wide singleton so the real
        # app's `LLMModeHandler` tool and this engine always agree on the
        # current mode with zero extra wiring; tests inject their own
        # isolated instance instead of touching the shared singleton.
        self.mode_state = mode_state or RUNTIME_MODE
        # Efficient LLM Classifier sprint - opt-in-by-construction, same
        # convention as `PlannerBridgeModule.device_intent_client`: `None`
        # by default (feature fully inert - `decide()` never calls
        # `classify_intent_llm()` at all when this is `None`, regardless
        # of `config.classifier_enabled`), wired post-hoc by a launcher
        # via `set_classifier_client()` below once a real LLM provider
        # client exists (see `luno/bootstrap/adapters.py::
        # register_intent_classifier()`). Expects the exact
        # `LLMManagerAdapter.client.chat_completion` callable shape.
        self.classifier_client = classifier_client
        self._classifier_cache = ClassifierCache(self.config.classifier_cache_ttl_s)

    def set_classifier_client(self, client: Optional[Callable[..., Any]]) -> None:
        self.classifier_client = client

    def reload_config(self, config: Optional[RoutingConfig] = None) -> None:
        """Spec's "all reloadable" - swaps the config every submodule
        reads live off `self.config`/`self.provider_selector.config`/
        `self.affinity.config` (they all hold a reference, not a copy),
        so a fresh `RoutingConfig.from_env()` takes effect on the very
        next `.decide()` call, no restart needed."""
        self.config = config or RoutingConfig.from_env()
        self.provider_selector.config = self.config
        self.affinity.config = self.config
        self._classifier_cache = ClassifierCache(self.config.classifier_cache_ttl_s)

    def decide(
        self,
        *,
        request_id: str,
        text: str,
        conversation_id: Optional[str] = None,
        relevant_memories: Optional[List[Any]] = None,
        world_model_entities: Optional[Dict[str, Any]] = None,
        tool_state_hit: bool = False,
        tool_state_detail: Optional[str] = None,
        needs_tools: bool = False,
        forced_intent: Optional[Intent] = None,
    ) -> RoutingDecision:
        """`forced_intent` (Efficient LLM Classifier sprint): a ONE-SHOT,
        PER-CALL override - when given, both `classify_intent()` AND the
        GPT-5.4-nano fallback classifier are skipped entirely, and this
        single intent is used directly. This exists ONLY for
        `PlannerBridgeModule` to re-process a turn's ORIGINAL text after
        the user just confirmed a previous `needs_confirmation=True`
        result (see `luno.routing.confirmation.ConfirmationHandler`) -
        without it, re-decide()-ing the same ambiguous text would
        re-trigger the same ambiguous-gate, call the classifier AGAIN
        (wasted, redundant spend) and could even flip to a DIFFERENT
        needs_confirmation outcome, looping the user through "are you
        sure?" indefinitely.

        This is a plain function ARGUMENT, not stored state anywhere on
        `self` - it exists for the duration of exactly one `decide()`
        call and is discarded the moment that call returns. There is no
        field, flag, or cache entry on this class (or anywhere else)
        that remembers "this request/conversation is in bypass mode" -
        the caller must re-supply `forced_intent` explicitly, every
        single time, only for the one re-processed call it applies to.
        A conversation with NO pending confirmation, or a normal fresh
        turn, is completely unaffected - `forced_intent` defaults to
        `None`, which is byte-identical to this parameter not existing
        at all."""
        reasoning: List[str] = []

        used_classifier = False
        classifier_confidence: Optional[float] = None
        needs_confirmation = False
        classifier_latency_ms: Optional[float] = None

        if forced_intent is not None:
            intents = [forced_intent]
            primary = forced_intent
            reasoning.append(
                f"forced_intent={forced_intent.value} - confirmed classification from a previous turn's "
                f"pending confirmation (request-scoped override, not persisted anywhere); classify_intent() "
                f"and the LLM classifier were both skipped for this call"
            )
        else:
            intents = classify_intent(text)
            primary = intents[0]

            # Efficient LLM Classifier sprint - fallback ONLY when the
            # deterministic classifier found nothing at all (see
            # `_AMBIGUOUS_FALLBACK_INTENTS`'s own comment above). Every
            # other branch below (`estimate_complexity`/`knowledge_router`/
            # `provider_selector`) runs on the FINAL `intents`/`primary`
            # after this block - a confident classifier result changes
            # routing exactly the same way a confident deterministic match
            # would have, an unconfident one changes nothing.
            if (
                primary in _AMBIGUOUS_FALLBACK_INTENTS
                and len(intents) == 1
                and self.config.classifier_enabled
                and self.classifier_client is not None
            ):
                result = classify_intent_llm(text, self.classifier_client, self.config, cache=self._classifier_cache)
                if result is not None:
                    used_classifier = True
                    classifier_confidence = result.confidence
                    classifier_latency_ms = result.latency_ms
                    high = self.config.classifier_confidence_threshold
                    low = self.config.classifier_confirmation_threshold
                    if result.confidence >= high:
                        intents = [result.intent]
                        primary = result.intent
                        reasoning.append(
                            f"classifier: no deterministic rule matched - GPT-5.4-nano classified "
                            f"intent={result.intent.value} confidence={result.confidence:.2f} (>= {high:.2f}) -> routing on it"
                        )
                    elif result.confidence >= low:
                        intents = [result.intent]
                        primary = result.intent
                        needs_confirmation = True
                        reasoning.append(
                            f"classifier: no deterministic rule matched - GPT-5.4-nano classified "
                            f"intent={result.intent.value} confidence={result.confidence:.2f} (between {low:.2f} and "
                            f"{high:.2f}) -> routing on it, but flagging needs_confirmation (caller must ask the user "
                            f"before acting, never guess)"
                        )
                    else:
                        reasoning.append(
                            f"classifier: GPT-5.4-nano classified intent={result.intent.value} but confidence="
                            f"{result.confidence:.2f} (< {low:.2f}) - too low to act on, discarded; staying on "
                            f"deterministic fallback intent={primary.value}"
                        )
                else:
                    reasoning.append("classifier: invoked but returned nothing usable (timeout/error/invalid output) - staying on deterministic fallback")

        complexity, score = estimate_complexity(text, intents)
        reasoning.append(f"intent(s)={[i.value for i in intents]}, complexity={complexity.value} (score={score:.2f})")

        knowledge_result = self.knowledge_router.route(
            text=text,
            world_model_entities=world_model_entities,
            relevant_memories=relevant_memories,
            tool_state_hit=tool_state_hit,
            tool_state_detail=tool_state_detail,
        )
        reasoning.append(f"knowledge routing -> source='{knowledge_result.source.value}' hit={knowledge_result.hit}")

        needs_internet = False
        search_queries: List[str] = []
        search_context: Optional[str] = None
        if not knowledge_result.hit and self.config.enable_web_search:
            lower = (text or "").lower()
            should_search = primary == Intent.SEARCH_WEB or _looks_time_sensitive(lower)
            if should_search:
                if self.search_router.is_available():
                    needs_internet = True
                    reasoning.append("no local knowledge source answered + query looks time-sensitive -> Tavily search")
                    raw = self.search_router.search(text, intents, complexity)
                    search_context = self.search_router.format_context(raw)
                    search_queries = list(self.search_router.last_queries)
                else:
                    reasoning.append("would need internet search but Tavily isn't configured (TAVILY_API_KEY unset) - falling through to the LLM's own knowledge")

        alias, provider, model, reasoning_effort = self.provider_selector.select(intents, complexity, reasoning)
        alias, affinity_applied = self.affinity.apply(conversation_id, alias, intents, complexity, reasoning)
        if affinity_applied:
            provider, model = resolve_alias(alias)
            # affinity only ever sticks a conversation to the REASONING
            # alias (see affinity.py) - its effort level applies too.
            reasoning_effort = self.config.reasoning_effort

        runtime_mode, manual_alias = self.mode_state.snapshot()
        if runtime_mode == MODE_MANUAL:
            if manual_alias:
                alias = manual_alias
                provider, model = resolve_alias(alias)
                reasoning.append(
                    f"LLM mode = manual (runtime override, set via voice/text command) - locked to "
                    f"'{manual_alias}', resolved to provider='{provider}'" + (f", model='{model}'" if model else "")
                )
            else:
                reasoning.append(
                    "LLM mode = manual (runtime override, no specific provider pinned) - suppressing "
                    "provider/model override; LLM Manager's own configured default/fallback order is used instead"
                )
                provider, model, reasoning_effort = None, None, None
        elif not self.config.enable_auto_routing:
            reasoning.append("ENABLE_AUTO_ROUTING=false - suppressing provider/model override; LLM Manager's own configured default/fallback order is used instead")
            provider, model, reasoning_effort = None, None, None

        decision = RoutingDecision(
            request_id=request_id,
            text=text,
            conversation_id=conversation_id,
            intents=intents,
            primary_intent=primary,
            complexity=complexity,
            complexity_score=score,
            knowledge_source=knowledge_result.source,
            knowledge_hit=knowledge_result.hit,
            needs_internet=needs_internet,
            needs_tools=needs_tools,
            provider_alias=alias,
            provider=provider,
            model=model,
            affinity_applied=affinity_applied,
            reasoning=reasoning,
            search_queries=search_queries,
            search_context=search_context,
            estimated_cost_tier=("high" if alias == self.config.reasoning_provider_alias else "low"),
            used_classifier=used_classifier,
            classifier_confidence=classifier_confidence,
            needs_confirmation=needs_confirmation,
            classifier_latency_ms=classifier_latency_ms,
            timestamp=time.time(),
        )
        self.stats.record(decision)
        return decision

    # -- introspection for the Dashboard --------------------------------

    def status(self) -> Dict[str, Any]:
        runtime_mode, manual_alias = self.mode_state.snapshot()
        return {
            "config": self.config.to_dict(),
            "stats": self.stats.to_dict(),
            "sticky_conversations": self.affinity.snapshot(),
            "web_search_available": self.search_router.is_available(),
            "runtime_llm_mode": {"mode": runtime_mode, "manual_provider": manual_alias},
        }
