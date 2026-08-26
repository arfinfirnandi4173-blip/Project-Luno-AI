"""
test_openai_primary_deepseek_fallback.py
==========================================

OpenAI-Primary/DeepSeek-Fallback sprint - `LLMManagerAdapter`-level
regression coverage for:

    - OpenAI answers successfully -> DeepSeek (openrouter) is never
      called at all.
    - OpenAI fails in a fallback-eligible way (timeout, connection/
      network failure, 5xx, rate limit) -> exactly one fallback
      transition to openrouter (DeepSeek), openrouter answers, no
      infinite loop, no duplicate `assistant_response`.
    - The cross-provider model-passthrough bug fix: an explicit model
      override resolved for "openai" (e.g. "gpt-5.6-luna") must NOT be
      sent to openrouter on fallback - openrouter must receive its own
      configured default model instead.
    - OpenAI auth failure (invalid API key) does NOT fall back by
      default - surfaces as a clear `llm_error` instead of silently
      answering via DeepSeek.
    - `LLMFinished`'s new `data["fallback"]` diagnostics flag reflects
      whether a fallback actually happened for that request.

Every scenario uses `MockProviderClient` - no real network access, no
API keys required (matches every other test in this package).
"""

from __future__ import annotations

import time
from typing import Dict, List

import pytest

from luno.adapters.events import (
    AssistantResponse,
    LLMError,
    LLMFinished,
    NeedLLMResponse,
    ProviderFallbackActivated,
)
from luno.adapters.llm.config import PROVIDER_NAMES, LLMManagerConfig
from luno.adapters.llm.mock import MockProviderClient
from luno.adapters.llm_manager import LLMManagerAdapter
from luno.adapters.manager import AdapterManager
from luno.adapters.models import AdapterConfig


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _collect(mgr: AdapterManager, *event_types: str) -> Dict[str, List[dict]]:
    bucket: Dict[str, List[dict]] = {t: [] for t in event_types}
    for t in event_types:
        mgr.event_bus.subscribe(t, (lambda e, t=t: bucket[t].append(e.data)))
    return bucket


def _all_mock_clients(**overrides: MockProviderClient) -> Dict[str, MockProviderClient]:
    clients = {name: MockProviderClient(name=name, configured=False) for name in PROVIDER_NAMES}
    clients.update(overrides)
    return clients


@pytest.fixture
def harness():
    def _build(manager_config=None, clients=None):
        mgr = AdapterManager.standalone()
        adapter = LLMManagerAdapter(manager_config=manager_config, clients=clients)
        mgr.register(adapter, AdapterConfig(name="openrouter"))
        return mgr, adapter
    yield _build


def _openai_primary_cfg(**overrides) -> LLMManagerConfig:
    return LLMManagerConfig(
        provider="openai", priority=["openai", "openrouter", "gemini", "anthropic", "local"],
        enable_fallback=True, **overrides,
    )


# ============================================================================
# OpenAI success - DeepSeek never touched
# ============================================================================

def test_openai_success_never_calls_deepseek(harness):
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", canned_text="hi from openai", default_model="gpt-5.6-luna"),
        openrouter=MockProviderClient(name="openrouter", canned_text="should never be used", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d1",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openai"
        assert len(clients["openrouter"].calls) == 0
        assert len(ev["provider_fallback_activated"]) == 0
        assert ev["llm_finished"][0]["fallback"] is False
    finally:
        mgr.stop_all()


# ============================================================================
# Fallback-eligible OpenAI failures -> DeepSeek(openrouter) recovers
# ============================================================================

@pytest.mark.parametrize("failure_kwargs", [
    {"timeout_error": True},
    {"network_error": True},
    {"fail_status": 503, "fail_times": 99},  # server error
    {"fail_status": 429, "fail_times": 99},  # rate limit
])
def test_openai_fallback_eligible_failure_recovers_via_deepseek(harness, failure_kwargs):
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", default_model="gpt-5.6-luna", **failure_kwargs),
        openrouter=MockProviderClient(name="openrouter", canned_text="deepseek answered", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(max_retries=0), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated", "llm_error", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d2",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["text"] == "deepseek answered"
        assert ev["assistant_response"][0]["provider"] == "openrouter"
        assert len(ev["provider_fallback_activated"]) == 1
        assert ev["provider_fallback_activated"][0]["from_provider"] == "openai"
        assert ev["provider_fallback_activated"][0]["to_provider"] == "openrouter"
        assert len(ev["llm_error"]) == 0
        # exactly one openai attempt, exactly one openrouter attempt - no
        # retry storm, no duplicate answer.
        assert len(clients["openai"].calls) == 1
        assert len(clients["openrouter"].calls) == 1
        assert ev["llm_finished"][0]["fallback"] is True
    finally:
        mgr.stop_all()


def test_openai_fallback_does_not_pass_openai_model_id_to_openrouter(harness):
    """The cross-provider model-passthrough fix: openrouter must receive
    ITS OWN configured default model on fallback, never the OpenAI-
    specific model id ("gpt-5.6-luna" is not a valid OpenRouter slug)."""
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", default_model="gpt-5.6-luna", fail_status=503, fail_times=99),
        openrouter=MockProviderClient(name="openrouter", canned_text="ok", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(max_retries=0), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d3",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert clients["openai"].calls[0]["model"] == "gpt-5.6-luna"
        # NOT "gpt-5.6-luna" - openrouter fell back to its own default.
        assert clients["openrouter"].calls[0]["model"] == "deepseek/deepseek-v4-flash"
    finally:
        mgr.stop_all()


def test_openai_streaming_fallback_does_not_pass_openai_model_id_to_openrouter(harness):
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", default_model="gpt-5.6-luna", fail_status=503, fail_times=99),
        openrouter=MockProviderClient(name="openrouter", canned_text="ok streamed", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(max_retries=0), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": True, "request_id": "d4",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert clients["openrouter"].calls[0]["model"] == "deepseek/deepseek-v4-flash"
    finally:
        mgr.stop_all()


# ============================================================================
# Auth failure - must NOT silently fall back to DeepSeek
# ============================================================================

def test_openai_auth_failure_does_not_fallback_by_default(harness):
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", fail_status=401, fail_times=99, default_model="gpt-5.6-luna"),
        openrouter=MockProviderClient(name="openrouter", canned_text="should never be reached", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "provider_fallback_activated", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d5",
        }))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["provider_fallback_activated"]) == 0
        assert len(ev["assistant_response"]) == 0
        assert clients["openrouter"].calls == []  # never even attempted - no infinite loop, no silent hide
        assert ev["llm_error"][0]["error_type"] == "ProviderAuthError"
    finally:
        mgr.stop_all()


def test_openai_auth_failure_can_opt_into_fallback(harness):
    """`LLM_FALLBACK_ON_AUTH_ERROR=true` (opt-in) restores the older
    cross-vendor-retry behavior for anyone who explicitly wants it -
    proves the new default doesn't remove the capability, just flips it."""
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", fail_status=401, fail_times=99, default_model="gpt-5.6-luna"),
        openrouter=MockProviderClient(name="openrouter", canned_text="deepseek answered anyway", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(fallback_on_auth_error=True), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d6",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openrouter"
        assert len(ev["provider_fallback_activated"]) == 1
    finally:
        mgr.stop_all()


def test_openai_invalid_request_does_not_fallback(harness):
    """Invalid model configuration / malformed request (400/404/422) -
    already the pre-existing default (`fallback_on_invalid_request=False`),
    verified again here specifically for the openai->deepseek pairing."""
    clients = _all_mock_clients(
        openai=MockProviderClient(name="openai", fail_status=400, fail_times=99, default_model="not-a-real-model"),
        openrouter=MockProviderClient(name="openrouter", canned_text="should never be reached", default_model="deepseek/deepseek-v4-flash"),
    )
    mgr, adapter = harness(manager_config=_openai_primary_cfg(), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "provider_fallback_activated")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "not-a-real-model", "messages": [{"role": "user", "content": "hai"}],
            "stream": False, "request_id": "d7",
        }))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["provider_fallback_activated"]) == 0
        assert clients["openrouter"].calls == []
    finally:
        mgr.stop_all()


# ============================================================================
# Both providers unavailable
# ============================================================================

def test_both_openai_and_deepseek_unavailable_surfaces_llm_error(harness):
    clients = {name: MockProviderClient(name=name, configured=False) for name in PROVIDER_NAMES}
    mgr, adapter = harness(manager_config=_openai_primary_cfg(), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hai"}], "stream": False, "request_id": "d8",
        }))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["assistant_response"]) == 0
    finally:
        mgr.stop_all()


# ============================================================================
# Single utterance -> exactly one request per provider, one final answer
# (the "ada apa di kamera?ada apa di kamera?" duplicate-request symptom,
# checked here at the LLM Manager boundary specifically)
# ============================================================================

def test_single_need_llm_response_produces_exactly_one_answer_no_duplicates(harness):
    clients = _all_mock_clients(openai=MockProviderClient(name="openai", canned_text="single answer"))
    mgr, adapter = harness(manager_config=_openai_primary_cfg(), clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "ada apa di kamera?"}],
            "stream": False, "request_id": "d9",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        time.sleep(0.2)  # give any accidental double-publish a chance to show up
        assert len(ev["assistant_response"]) == 1
        assert len(ev["llm_finished"]) == 1
        assert len(clients["openai"].calls) == 1
    finally:
        mgr.stop_all()
