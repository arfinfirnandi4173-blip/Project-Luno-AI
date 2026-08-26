"""
test_llm_classifier.py
=========================

`classify_intent_llm()` / `ClassifierCache` - Efficient LLM Classifier
sprint. Uses a fake `chat_fn` (never a real network call) that returns a
small object exposing `.text`, matching the exact shape
`LLMManagerAdapter.client.chat_completion()`'s `ChatResult` already has
(`getattr(response, "text", None)` is all `classify_intent_llm()` ever
reads off it).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from luno.routing.config import RoutingConfig
from luno.routing.llm_classifier import ClassifierCache, classify_intent_llm
from luno.routing.models import Intent


@dataclass
class _FakeResponse:
    text: str


class _FakeChatFn:
    """Records every call (spy) and returns a scripted response/behavior."""

    def __init__(self, response_text: Optional[str] = None, raise_exc: Optional[Exception] = None, sleep_s: float = 0.0):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.sleep_s = sleep_s
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResponse(text=self.response_text or "")


def _cfg(**overrides) -> RoutingConfig:
    return RoutingConfig(classifier_enabled=True, **overrides)


def _valid_json(intent="device_control", confidence=0.9, needs_confirmation=False, reason="ok") -> str:
    return json.dumps({"intent": intent, "confidence": confidence, "needs_confirmation": needs_confirmation, "reason": reason})


# -- happy path --------------------------------------------------------------

def test_classify_intent_llm_returns_valid_result():
    fn = _FakeChatFn(response_text=_valid_json(intent="search_web", confidence=0.93))
    result = classify_intent_llm("cariin resep nasi goreng enak", fn, _cfg())
    assert result is not None
    assert result.intent == Intent.SEARCH_WEB
    assert result.confidence == 0.93
    assert result.needs_confirmation_hint is False
    assert len(fn.calls) == 1


def test_classify_intent_llm_pins_provider_to_openai_and_attaches_json_schema():
    """(K) Classifier must not change the global provider - it pins
    THIS call to openai via the per-call `provider=` kwarg, never
    mutates any shared config. Also confirms the strict JSON schema
    (`response_format`) is actually attached to the request."""
    fn = _FakeChatFn(response_text=_valid_json())
    classify_intent_llm("something ambiguous", fn, _cfg())
    call = fn.calls[0]
    assert call["provider"] == "openai"
    assert call["metadata"]["response_format"]["type"] == "json_schema"
    assert call["metadata"]["response_format"]["json_schema"]["strict"] is True
    assert call["temperature"] == 0.0


def test_classify_intent_llm_truncates_to_max_input_chars():
    fn = _FakeChatFn(response_text=_valid_json())
    classify_intent_llm("x" * 5000, fn, _cfg(classifier_max_input_chars=50))
    sent = fn.calls[0]["messages"][0]["content"]
    assert len(sent) == 50


# -- fail-closed behavior (G, H, I) -------------------------------------------

def test_invalid_json_fails_closed():
    fn = _FakeChatFn(response_text="not json at all")
    assert classify_intent_llm("ambiguous text", fn, _cfg()) is None


def test_json_missing_required_field_fails_closed():
    fn = _FakeChatFn(response_text=json.dumps({"intent": "device_control", "confidence": 0.9}))
    # missing needs_confirmation/reason is tolerated (defaults applied) -
    # but a genuinely UNKNOWN intent value must not be.
    result = classify_intent_llm("x", fn, _cfg())
    assert result is not None  # confidence/intent present - lenient on the two optional fields


def test_unknown_intent_value_fails_closed():
    fn = _FakeChatFn(response_text=json.dumps({"intent": "launch_the_nukes", "confidence": 0.99, "needs_confirmation": False, "reason": "x"}))
    assert classify_intent_llm("x", fn, _cfg()) is None


def test_confidence_out_of_range_fails_closed():
    fn = _FakeChatFn(response_text=json.dumps({"intent": "device_control", "confidence": 1.7, "needs_confirmation": False, "reason": "x"}))
    assert classify_intent_llm("x", fn, _cfg()) is None


def test_api_exception_fails_closed():
    fn = _FakeChatFn(raise_exc=RuntimeError("network exploded"))
    assert classify_intent_llm("x", fn, _cfg()) is None


def test_timeout_fails_closed():
    """(H) A call that takes longer than CLASSIFIER_TIMEOUT_MS must
    return None promptly - not hang the caller for the full duration."""
    fn = _FakeChatFn(response_text=_valid_json(), sleep_s=2.0)
    t0 = time.monotonic()
    result = classify_intent_llm("x", fn, _cfg(classifier_timeout_ms=100))
    elapsed = time.monotonic() - t0
    assert result is None
    assert elapsed < 1.0, f"took {elapsed:.2f}s - timeout was not enforced"


def test_none_chat_fn_returns_none_without_calling_anything():
    assert classify_intent_llm("x", None, _cfg()) is None


def test_empty_text_returns_none_without_calling():
    fn = _FakeChatFn(response_text=_valid_json())
    assert classify_intent_llm("   ", fn, _cfg()) is None
    assert len(fn.calls) == 0


def test_classifier_never_raises_regardless_of_garbage_response():
    """(G) 'Luno stays alive' - a battery of malformed responses must
    never raise out of classify_intent_llm(), only ever return None."""
    garbage_payloads = [
        "{",
        "null",
        "42",
        '{"intent": null, "confidence": 0.9}',
        '{"intent": "device_control", "confidence": "high"}',
        '{"intent": "device_control", "confidence": null}',
        "",
    ]
    for payload in garbage_payloads:
        fn = _FakeChatFn(response_text=payload)
        result = classify_intent_llm("x", fn, _cfg())
        assert result is None, f"payload {payload!r} should have failed closed"


# -- caching (N) ---------------------------------------------------------------

def test_identical_text_within_ttl_uses_cache_not_a_second_call():
    fn = _FakeChatFn(response_text=_valid_json(confidence=0.85))
    cache = ClassifierCache(ttl_s=30.0)
    cfg = _cfg()
    r1 = classify_intent_llm("bikin ruangan nyaman", fn, cfg, cache=cache)
    r2 = classify_intent_llm("BIKIN RUANGAN NYAMAN", fn, cfg, cache=cache)  # case-insensitive key
    assert r1 == r2
    assert len(fn.calls) == 1  # second call served from cache
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_cache_ttl_zero_disables_caching_entirely():
    fn = _FakeChatFn(response_text=_valid_json())
    cache = ClassifierCache(ttl_s=0)
    cfg = _cfg()
    classify_intent_llm("same text", fn, cfg, cache=cache)
    classify_intent_llm("same text", fn, cfg, cache=cache)
    assert len(fn.calls) == 2  # never cached


def test_cache_expires_after_ttl():
    fn = _FakeChatFn(response_text=_valid_json())
    cache = ClassifierCache(ttl_s=0.05)
    cfg = _cfg()
    classify_intent_llm("expiring text", fn, cfg, cache=cache)
    time.sleep(0.1)
    classify_intent_llm("expiring text", fn, cfg, cache=cache)
    assert len(fn.calls) == 2  # cache entry expired, real second call made


def test_different_text_never_shares_a_cache_entry():
    fn = _FakeChatFn(response_text=_valid_json())
    cache = ClassifierCache(ttl_s=30.0)
    cfg = _cfg()
    classify_intent_llm("text one", fn, cfg, cache=cache)
    classify_intent_llm("text two", fn, cfg, cache=cache)
    assert len(fn.calls) == 2
