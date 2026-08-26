"""
test_decision_engine.py
==========================

End-to-end `DecisionEngine.decide()` - ties every submodule together.
Covers the sprint's own checklist: DeepSeek/GPT selection, provider
fallback interplay (the actual fallback itself is `LLMManagerAdapter`'s
job - covered in `luno/adapters/tests/test_llm_manager.py`'s
`test_provider_override_*` tests; this file only checks the DECISION,
not the network call), conversation affinity persisting across real
`.decide()` calls, cost optimization, knowledge short-circuiting
internet search, config reload, stress (many decisions, no
crash/leak), and concurrent conversations staying isolated.
"""

from __future__ import annotations

import threading
import time

from luno.routing.config import RoutingConfig
from luno.routing.decision_engine import DecisionEngine
from luno.routing.mode_state import LLMModeState
from luno.routing.models import ComplexityLevel, Intent, KnowledgeSource
from luno.routing.web_search_router import WebSearchRouter


def _cfg(**overrides):
    return RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", **overrides)


def _engine(**cfg_overrides):
    return DecisionEngine(_cfg(**cfg_overrides))


def test_routine_turn_routes_to_default_provider():
    engine = _engine()
    d = engine.decide(request_id="r1", text="hai apa kabar")
    assert d.provider_alias == "deepseek"
    assert d.estimated_cost_tier == "low"


def test_reasoning_turn_routes_to_reasoning_provider():
    engine = _engine()
    d = engine.decide(request_id="r2", text="debug this stack trace and explain the root cause")
    assert d.provider_alias == "gpt"
    assert d.estimated_cost_tier == "high"


def test_decision_records_intent_and_complexity():
    engine = _engine()
    d = engine.decide(request_id="r3", text="turn on the bedroom light")
    assert d.primary_intent == Intent.DEVICE_CONTROL
    assert d.complexity in (ComplexityLevel.LOW, ComplexityLevel.MEDIUM)


def test_world_model_knowledge_hit_recorded():
    engine = _engine()
    d = engine.decide(
        request_id="r4", text="is the bedroom light on",
        world_model_entities={"light.bedroom_light": "on"},
    )
    assert d.knowledge_source == KnowledgeSource.WORLD_MODEL
    assert d.knowledge_hit is True
    assert d.needs_internet is False


def test_knowledge_hit_suppresses_internet_search_even_for_search_intent():
    engine = _engine()
    d = engine.decide(
        request_id="r5", text="what's the latest news you saw about my cup",
        world_model_entities={"sensor.cup": "on desk"},
    )
    # world model token overlap on "cup" should short-circuit before
    # ever considering the internet, even though "latest news" looks
    # search-flavored.
    if d.knowledge_hit:
        assert d.needs_internet is False


def test_no_knowledge_and_time_sensitive_text_triggers_internet_when_available():
    fake_search_router = WebSearchRouter(
        search_fn=lambda q, max_results=5: {"answer": "sunny", "results": []},
        deep_search_fn=lambda qs, max_results_per_query=3: {"searches": []},
        is_configured_fn=lambda: True,
    )
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt")
    engine = DecisionEngine(cfg, search_router=fake_search_router)
    d = engine.decide(request_id="r6", text="what's the weather today")
    assert d.needs_internet is True
    assert d.search_context is not None
    assert "sunny" in d.search_context


def test_internet_search_never_fires_when_tavily_unconfigured():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt")
    engine = DecisionEngine(cfg, search_router=WebSearchRouter(
        search_fn=lambda *a, **k: {}, deep_search_fn=lambda *a, **k: {}, is_configured_fn=lambda: False,
    ))
    d = engine.decide(request_id="r7", text="what's the weather today")
    assert d.needs_internet is False
    assert d.search_context is None


def test_enable_web_search_false_never_triggers_search():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", enable_web_search=False)
    engine = DecisionEngine(cfg, search_router=WebSearchRouter(
        search_fn=lambda *a, **k: {"answer": "x", "results": []},
        deep_search_fn=lambda *a, **k: {"searches": []}, is_configured_fn=lambda: True,
    ))
    d = engine.decide(request_id="r8", text="what's the weather today")
    assert d.needs_internet is False


def test_enable_auto_routing_false_suppresses_provider_override():
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", enable_auto_routing=False)
    engine = DecisionEngine(cfg)
    d = engine.decide(request_id="r9", text="debug this stack trace")
    assert d.provider is None
    assert d.model is None
    # still classifies/logs, just doesn't override
    assert d.primary_intent == Intent.CODING


def test_runtime_manual_mode_overrides_auto_routing_decision():
    """Even a clearly reasoning-flavored turn (would normally pick
    "gpt") must obey a runtime manual pin to a different provider - the
    manual override is a HARD lock, not just another input to the
    scoring."""
    mode_state = LLMModeState()
    engine = DecisionEngine(_cfg(), mode_state=mode_state)
    mode_state.set_manual("openai")
    d = engine.decide(request_id="m1", text="debug this stack trace and explain the root cause")
    assert d.provider_alias == "openai"
    assert d.provider == "openai"
    assert any("manual" in r.lower() for r in d.reasoning)


def test_runtime_manual_mode_without_provider_suppresses_override():
    mode_state = LLMModeState()
    engine = DecisionEngine(_cfg(), mode_state=mode_state)
    mode_state.set_manual(None)
    d = engine.decide(request_id="m2", text="debug this stack trace")
    assert d.provider is None
    assert d.model is None


def test_runtime_manual_mode_wins_over_enable_auto_routing_true():
    """`ENABLE_AUTO_ROUTING=true` (the config default) must not matter
    once the runtime override is set to manual - runtime state always
    takes precedence, see decision_engine.py's own docstring."""
    mode_state = LLMModeState()
    cfg = RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", enable_auto_routing=True)
    engine = DecisionEngine(cfg, mode_state=mode_state)
    mode_state.set_manual("anthropic")
    d = engine.decide(request_id="m3", text="hai apa kabar")
    assert d.provider_alias == "anthropic"
    assert d.provider == "anthropic"


def test_runtime_auto_mode_restores_normal_routing():
    mode_state = LLMModeState()
    engine = DecisionEngine(_cfg(), mode_state=mode_state)
    mode_state.set_manual("openai")
    d1 = engine.decide(request_id="m4a", text="debug this stack trace")
    assert d1.provider_alias == "openai"
    mode_state.set_auto()
    d2 = engine.decide(request_id="m4b", text="debug this stack trace")
    # back to the normal auto-routing alias ("gpt"), no longer locked to
    # the manual pin from before - whatever real provider "gpt" resolves
    # to (openai directly if OPENAI_API_KEY is set, openrouter
    # otherwise) is resolve_alias()'s own concern, not this test's.
    assert d2.provider_alias == "gpt"
    assert not any("manual" in r.lower() for r in d2.reasoning)


def test_default_mode_state_is_the_shared_singleton_when_unset():
    """A `DecisionEngine` constructed without an explicit `mode_state=`
    defaults to auto (the process-wide singleton's own default state) -
    this is what makes the real app's tool handler and engine agree
    with zero extra wiring at the call site."""
    engine = DecisionEngine(_cfg())
    from luno.routing.mode_state import RUNTIME_MODE
    assert engine.mode_state is RUNTIME_MODE


def test_status_exposes_runtime_llm_mode():
    mode_state = LLMModeState()
    engine = DecisionEngine(_cfg(), mode_state=mode_state)
    mode_state.set_manual("gemini")
    status = engine.status()
    assert status["runtime_llm_mode"] == {"mode": "manual", "manual_provider": "gemini"}


def test_affinity_persists_across_decide_calls_same_conversation():
    engine = _engine()
    d1 = engine.decide(request_id="r10a", text="debug this stack trace", conversation_id="conv-x")
    assert d1.provider_alias == "gpt"
    # follow-up still reasoning-flavored (a "why" question continuing the
    # same debugging thread) - affinity keeps it on gpt even though this
    # turn's own complexity score alone might land lower than the threshold.
    d2 = engine.decide(request_id="r10b", text="why would that even happen", conversation_id="conv-x")
    assert d2.provider_alias == "gpt"


def test_affinity_keeps_sticky_provider_even_when_own_turn_would_pick_default():
    """The precise mechanism test: force a turn that WOULD independently
    resolve to the default provider (low complexity, no reasoning
    intent) but still counts as "mid-reasoning" by complexity alone -
    `affinity_applied` must be True, proving the override actually came
    from stickiness, not from this turn's own classification."""
    engine = _engine()
    engine.decide(request_id="r10c", text="debug this stack trace and explain the architecture", conversation_id="conv-z")
    # a follow-up that scores HIGH complexity (length + "algorithm"/
    # "architecture" signal words) but classifies as plain
    # GENERAL_QUESTION (no REASONING/PLANNING/CODING/MULTI_STEP intent
    # match) - ProviderSelector alone would pick "deepseek" for this;
    # only affinity's own complexity>=HIGH check keeps it on "gpt".
    followup = (
        "what do you think about the algorithm and the architecture we discussed "
        "the other day, does it still make sense to you overall"
    )
    d2 = engine.decide(request_id="r10d", text=followup, conversation_id="conv-z")
    assert d2.primary_intent == Intent.GENERAL_QUESTION  # sanity: NOT independently reasoning-flavored
    assert d2.provider_alias == "gpt"
    assert d2.affinity_applied is True


def test_affinity_releases_on_clear_topic_change():
    engine = _engine()
    engine.decide(request_id="r11a", text="debug this stack trace and explain the root cause", conversation_id="conv-y")
    d2 = engine.decide(request_id="r11b", text="haha thanks, anyway what's up", conversation_id="conv-y")
    assert d2.provider_alias == "deepseek"


def test_stats_recorded_for_every_decision():
    engine = _engine()
    for i in range(5):
        engine.decide(request_id=f"s{i}", text="hai")
    stats = engine.stats.to_dict()
    assert stats["total_decisions"] == 5
    assert stats["by_provider_alias"].get("deepseek") == 5


def test_reload_config_takes_effect_on_next_decision():
    engine = _engine()
    d1 = engine.decide(request_id="rl1", text="debug this stack trace")
    assert d1.provider_alias == "gpt"
    engine.reload_config(RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="deepseek"))
    d2 = engine.decide(request_id="rl2", text="debug this stack trace")
    assert d2.provider_alias == "deepseek"


def test_status_exposes_dashboard_required_fields():
    engine = _engine()
    engine.decide(request_id="st1", text="hai")
    status = engine.status()
    for key in ("config", "stats", "sticky_conversations", "web_search_available"):
        assert key in status


def test_stress_many_sequential_decisions_no_crash():
    engine = _engine()
    texts = [
        "turn on the light", "why is this broken", "hai apa kabar", "remember I like tea",
        "what's the weather today", "debug this function", "is the door locked",
        "make a plan for tomorrow", "what do you see", "cari berita terbaru",
    ]
    t0 = time.time()
    for i in range(500):
        engine.decide(request_id=f"stress-{i}", text=texts[i % len(texts)], conversation_id=f"conv-{i % 10}")
    elapsed = time.time() - t0
    assert engine.stats.to_dict()["total_decisions"] == 500
    assert elapsed < 10.0  # pure in-memory heuristics - must stay fast


def test_concurrent_conversations_do_not_corrupt_affinity():
    engine = _engine()
    errors = []

    def worker(conv_id):
        try:
            for _ in range(30):
                d1 = engine.decide(request_id=f"{conv_id}-a-{time.time()}", text="debug this stack trace", conversation_id=conv_id)
                assert d1.provider_alias == "gpt"
                d2 = engine.decide(request_id=f"{conv_id}-b-{time.time()}", text="haha ok thanks", conversation_id=conv_id)
                assert d2.provider_alias == "deepseek"
        except Exception as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(f"conv-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors


def test_needs_tools_flag_passed_through():
    engine = _engine()
    d = engine.decide(request_id="nt1", text="turn on the light", needs_tools=True)
    assert d.needs_tools is True
    d2 = engine.decide(request_id="nt2", text="hai", needs_tools=False)
    assert d2.needs_tools is False


def test_reasoning_trail_is_human_readable_and_nonempty():
    engine = _engine()
    d = engine.decide(request_id="rt1", text="debug this stack trace")
    assert isinstance(d.reasoning, list) and len(d.reasoning) >= 2
    assert all(isinstance(r, str) for r in d.reasoning)


def test_to_dict_is_json_serializable():
    import json
    engine = _engine()
    d = engine.decide(request_id="j1", text="debug this stack trace", conversation_id="c1")
    json.dumps(d.to_dict())  # must never raise


# ============================================================================
# Efficient LLM Classifier sprint - classifier wiring in decide()
# ============================================================================

import json as _json
from dataclasses import dataclass as _dataclass
from typing import Any as _Any, Dict as _Dict, List as _List, Optional as _Optional


@_dataclass
class _FakeResp:
    text: str


class _SpyClassifier:
    """Records every call (spy) and returns a scripted JSON response."""

    def __init__(self, intent: str = "device_control", confidence: float = 0.9, needs_confirmation: bool = False):
        self.intent = intent
        self.confidence = confidence
        self.needs_confirmation = needs_confirmation
        self.calls: _List[_Dict[str, _Any]] = []

    def __call__(self, **kwargs: _Any) -> _FakeResp:
        self.calls.append(kwargs)
        return _FakeResp(text=_json.dumps({
            "intent": self.intent, "confidence": self.confidence,
            "needs_confirmation": self.needs_confirmation, "reason": "test",
        }))


def _classifier_cfg(**overrides) -> RoutingConfig:
    return RoutingConfig(default_provider_alias="deepseek", reasoning_provider_alias="gpt", classifier_enabled=True, **overrides)


# -- (A)/(B) deterministic match -> classifier never called --------------------

def test_clear_ha_command_never_invokes_classifier():
    spy = _SpyClassifier()
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="a1", text="turn off the bedroom light")
    assert d.primary_intent == Intent.DEVICE_CONTROL
    assert d.used_classifier is False
    assert len(spy.calls) == 0


def test_clear_browser_command_never_invokes_classifier():
    spy = _SpyClassifier()
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="b1", text="search the web for RTX 5060 price")
    assert d.primary_intent == Intent.SEARCH_WEB
    assert d.used_classifier is False
    assert len(spy.calls) == 0


def test_classifier_disabled_never_invoked_even_when_ambiguous():
    spy = _SpyClassifier()
    engine = DecisionEngine(RoutingConfig(classifier_enabled=False), classifier_client=spy)
    d = engine.decide(request_id="dis1", text="make the room comfortable")
    assert d.used_classifier is False
    assert len(spy.calls) == 0


def test_classifier_not_wired_never_invoked_even_when_enabled():
    engine = DecisionEngine(_classifier_cfg())  # no classifier_client passed
    d = engine.decide(request_id="nw1", text="make the room comfortable")
    assert d.used_classifier is False


# -- (C) ambiguous request -> classifier called ---------------------------------

def test_ambiguous_request_invokes_classifier():
    spy = _SpyClassifier(intent="smart_home", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="c1", text="make the room comfortable")
    assert len(spy.calls) == 1
    assert d.used_classifier is True


# -- (D)/(E) classifier result routes accordingly at high confidence -----------

def test_high_confidence_classifier_result_becomes_primary_intent():
    spy = _SpyClassifier(intent="device_control", confidence=0.95)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="d1", text="do what we talked about earlier")
    assert d.primary_intent == Intent.DEVICE_CONTROL
    assert d.classifier_confidence == 0.95
    assert d.needs_confirmation is False


def test_classifier_can_route_to_search_web():
    spy = _SpyClassifier(intent="search_web", confidence=0.88)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="e1", text="find out about that thing")
    assert d.primary_intent == Intent.SEARCH_WEB


# -- (F) low/medium confidence policy -------------------------------------------

def test_medium_confidence_sets_needs_confirmation_and_still_routes():
    spy = _SpyClassifier(intent="device_control", confidence=0.65)
    engine = DecisionEngine(_classifier_cfg(classifier_confidence_threshold=0.80, classifier_confirmation_threshold=0.55), classifier_client=spy)
    d = engine.decide(request_id="f1", text="make the room comfortable")
    assert d.primary_intent == Intent.DEVICE_CONTROL
    assert d.needs_confirmation is True
    assert d.used_classifier is True


def test_low_confidence_discarded_no_guess_stays_on_fallback():
    spy = _SpyClassifier(intent="device_control", confidence=0.30)
    engine = DecisionEngine(_classifier_cfg(classifier_confirmation_threshold=0.55), classifier_client=spy)
    d = engine.decide(request_id="f2", text="make the room comfortable")
    # discarded - stays on the deterministic fallback (GENERAL_CHAT/QUESTION),
    # never silently acts on a near-guess.
    assert d.primary_intent in (Intent.GENERAL_CHAT, Intent.GENERAL_QUESTION)
    assert d.needs_confirmation is False
    assert d.used_classifier is True  # still recorded as invoked for telemetry
    assert d.classifier_confidence == 0.30


# -- (G)/(H)/(I) classifier failure -> graceful fallback, engine stays alive ----

def test_classifier_exception_never_crashes_decide():
    def _raising(**kwargs):
        raise RuntimeError("boom")
    engine = DecisionEngine(_classifier_cfg(), classifier_client=_raising)
    d = engine.decide(request_id="g1", text="make the room comfortable")
    assert d.used_classifier is False
    assert d.primary_intent in (Intent.GENERAL_CHAT, Intent.GENERAL_QUESTION)


def test_classifier_invalid_json_never_crashes_decide():
    engine = DecisionEngine(_classifier_cfg(), classifier_client=lambda **k: _FakeResp(text="not json"))
    d = engine.decide(request_id="g2", text="make the room comfortable")
    assert d.used_classifier is False


# -- (J) classifier never executes anything --------------------------------------

def test_classifier_result_never_sets_needs_tools_or_touches_tool_state():
    spy = _SpyClassifier(intent="device_control", confidence=0.95)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="j1", text="make the room comfortable", needs_tools=False, tool_state_hit=False)
    assert d.needs_tools is False  # untouched by the classifier - caller-supplied only
    assert d.knowledge_hit is False


# -- (K) classifier never changes the global/active provider --------------------

def test_classifier_call_uses_openai_alias_without_touching_config_default():
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    cfg = _classifier_cfg()
    engine = DecisionEngine(cfg, classifier_client=spy)
    engine.decide(request_id="k1", text="make the room comfortable")
    assert spy.calls[0]["provider"] == "openai"
    # the ENGINE's own default/reasoning provider aliases are completely
    # untouched by the classifier call having happened.
    assert engine.config.default_provider_alias == "deepseek"
    assert engine.config.reasoning_provider_alias == "gpt"


# -- (M) classifier confidence is never proof of tool success -------------------

def test_classifier_confidence_is_not_a_knowledge_or_tool_state_hit():
    """A high-confidence CLASSIFICATION must never be conflated with a
    verified tool result - `knowledge_hit`/`tool_state_hit` only ever
    come from the caller's own already-verified data, never derived
    from `classifier_confidence`."""
    spy = _SpyClassifier(intent="device_control", confidence=0.99)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="m1", text="make the room comfortable")
    assert d.knowledge_hit is False
    assert d.classifier_confidence == 0.99  # recorded as its own separate field, not merged into knowledge_hit


# -- forced_intent: one-shot, request-scoped, never persistent ------------------

def test_forced_intent_skips_classify_intent_and_the_classifier_entirely():
    spy = _SpyClassifier(intent="search_web", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="fi1", text="make the room comfortable", forced_intent=Intent.DEVICE_CONTROL)
    assert d.primary_intent == Intent.DEVICE_CONTROL
    assert d.used_classifier is False
    assert d.needs_confirmation is False
    assert len(spy.calls) == 0  # classifier never even invoked - forced_intent short-circuits it


def test_forced_intent_does_not_persist_to_the_next_call():
    """The exact hard requirement: forced_intent must be one-shot,
    never a stored/global bypass flag - the VERY NEXT call on the same
    engine, same conversation, without forced_intent, must classify
    normally again."""
    spy = _SpyClassifier(intent="search_web", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d1 = engine.decide(request_id="p1", text="make the room comfortable", conversation_id="conv-p", forced_intent=Intent.DEVICE_CONTROL)
    assert d1.primary_intent == Intent.DEVICE_CONTROL
    assert len(spy.calls) == 0

    d2 = engine.decide(request_id="p2", text="make the room comfortable", conversation_id="conv-p")
    # no forced_intent this time - classifier SHOULD be invoked again,
    # proving the previous call's bypass did not leak into this one.
    assert len(spy.calls) == 1
    assert d2.used_classifier is True


# -- (O) request_id preserved throughout -----------------------------------------

def test_request_id_preserved_on_classifier_path():
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(), classifier_client=spy)
    d = engine.decide(request_id="rid-12345", text="make the room comfortable")
    assert d.request_id == "rid-12345"


# ============================================================================
# Efficiency test (spec section 17) - 100 clear commands, 100 ambiguous ones.
# Verifies ACTUAL invocation counts, not just "was called".
# ============================================================================

#: Verified via `classify_intent()` directly (see luno/routing/tests -
#: these are NOT guesses): every one of these matches a real
#: deterministic rule, so the ambiguous-gate must never open for any of
#: them.
_CLEAR_COMMANDS = [
    "turn on the bedroom light", "turn off the kitchen light", "matikan lampu kamar",
    "search the web for RTX 5060 price", "debug this stack trace", "what do you see",
    "remember that I like tea", "is the light on", "set a timer for 10 minutes",
    "lock the door",
]
#: Verified via `classify_intent()` directly: every one of these falls
#: all the way through to GENERAL_CHAT/GENERAL_QUESTION (no rule
#: matches) - genuinely ambiguous by this classifier's own rules.
_AMBIGUOUS_COMMANDS = [
    "make the room comfortable", "do what we talked about earlier", "you know what I mean",
    "handle that thing from before", "take care of it",
]


def test_efficiency_clear_commands_invoke_classifier_approximately_zero_times():
    # cache disabled - counting genuine invocation ATTEMPTS, not conflating
    # with cache hits (caching itself is covered separately in
    # test_llm_classifier.py).
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(classifier_cache_ttl_s=0), classifier_client=spy)
    for i in range(100):
        engine.decide(request_id=f"eff-clear-{i}", text=_CLEAR_COMMANDS[i % len(_CLEAR_COMMANDS)])
    assert len(spy.calls) == 0
    stats = engine.stats.to_dict()
    assert stats["classifier_calls"] == 0
    assert stats["classifier_bypassed"] == 100


def test_efficiency_ambiguous_commands_invoke_classifier_only_when_needed():
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(classifier_cache_ttl_s=0), classifier_client=spy)
    for i in range(100):
        engine.decide(request_id=f"eff-amb-{i}", text=_AMBIGUOUS_COMMANDS[i % len(_AMBIGUOUS_COMMANDS)])
    # every single one of these is genuinely ambiguous (no deterministic
    # rule matches any of them) - the classifier must fire for all 100,
    # not "approximately zero" like the clear-command test above.
    assert len(spy.calls) == 100
    stats = engine.stats.to_dict()
    assert stats["classifier_calls"] == 100
    assert stats["classifier_avg_latency_ms"] >= 0.0


def test_efficiency_mixed_batch_only_calls_classifier_for_the_ambiguous_half():
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(classifier_cache_ttl_s=0), classifier_client=spy)
    clear = "turn on the bedroom light"
    ambiguous = "make the room comfortable"
    for i in range(100):
        text = clear if i % 2 == 0 else ambiguous
        engine.decide(request_id=f"eff-mix-{i}", text=text)
    assert len(spy.calls) == 50
    stats = engine.stats.to_dict()
    assert stats["classifier_calls"] == 50
    assert stats["classifier_bypassed"] == 50


def test_efficiency_caching_further_reduces_repeated_ambiguous_calls():
    """With caching ON (the default), repeating the SAME ambiguous text
    should reduce real classifier invocations well below the call count -
    demonstrating section 9's caching requirement actually saves calls
    in the efficiency scenario, not just in isolation."""
    spy = _SpyClassifier(intent="device_control", confidence=0.9)
    engine = DecisionEngine(_classifier_cfg(classifier_cache_ttl_s=30.0), classifier_client=spy)
    same_ambiguous_text = "make the room comfortable"
    for i in range(100):
        engine.decide(request_id=f"eff-cache-{i}", text=same_ambiguous_text)
    assert len(spy.calls) == 1  # every repeat after the first was a cache hit
