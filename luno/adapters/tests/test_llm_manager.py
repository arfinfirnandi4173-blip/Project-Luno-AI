"""
test_llm_manager.py
====================

Comprehensive test suite for `LLMManagerAdapter` (see
`../llm_manager.py`) - the Multi-LLM Provider System sprint's own
checklist, covering everything not already exercised per-provider in
`luno/adapters/llm/tests/test_providers.py`:

    Runtime switching, Configuration reload, Automatic fallback,
    Provider recovery, Concurrent conversations, Stress test (500
    requests), Conversation continuity, Cost tracking, Health
    monitoring, Regression against existing architecture.

Every scenario here uses `MockProviderClient` (see `../llm/mock.py`) -
no real network access anywhere, no API keys required. All five
providers are always constructed (mirroring what `LLMManagerAdapter`
does in production) so fallback/priority-order behavior is exercised
against the real multi-provider shape, not a single-provider stand-in.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from luno.adapters.events import (
    AssistantResponse,
    CancelLLMRequest,
    ConversationReset,
    LLMCancelled,
    LLMChunk,
    LLMError,
    LLMFinished,
    LLMStarted,
    LLMStreaming,
    NeedLLMResponse,
    ProviderFallbackActivated,
    ProviderHealthChanged,
    ProviderSwitched,
    ReloadModel,
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
    """`(adapter_manager, adapter)` pair, NOT yet started - caller
    builds `clients`/`manager_config` and calls `.start_all()` itself
    (mirrors the flexibility every scenario below needs)."""
    def _build(manager_config=None, clients=None, request_workers=4):
        mgr = AdapterManager.standalone()
        adapter = LLMManagerAdapter(manager_config=manager_config, clients=clients, request_workers=request_workers)
        mgr.register(adapter, AdapterConfig(name="openrouter"))
        return mgr, adapter
    yield _build


# ============================================================================
# Basic event contract (regression: identical to OpenRouterAdapter's)
# ============================================================================

def test_non_streaming_basic_event_contract(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="Hello there!"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_started", "llm_finished", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "stream": False, "request_id": "req-1", "conversation_id": "conv-1",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["text"] == "Hello there!"
        assert ev["assistant_response"][0]["provider"] == "openrouter"
        assert ev["llm_started"][0]["request_id"] == "req-1"
        assert ev["llm_finished"][0]["conversation_id"] == "conv-1"
    finally:
        mgr.stop_all()


def test_streaming_basic_event_contract(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="Hi Vinn"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_streaming", "llm_chunk", "llm_finished", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "s1"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert len(ev["llm_streaming"]) == 1
        assert len(ev["llm_chunk"]) >= 1
        assert "".join(c["delta"] for c in ev["llm_chunk"]) == "Hi Vinn"
        assert ev["assistant_response"][0]["text"] == "Hi Vinn"
    finally:
        mgr.stop_all()


def test_cancellation_mid_stream(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="a long reply here", chunk_delay_s=0.05))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_cancelled", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "c1"}))
        assert _wait_until(lambda: len(ev["llm_chunk"]) >= 1)
        mgr.event_bus.publish(CancelLLMRequest(data={"request_id": "c1"}))
        assert _wait_until(lambda: len(ev["llm_cancelled"]) == 1, timeout_s=1.0)
        time.sleep(0.2)
        assert len(ev["llm_finished"]) == 0  # cancelled request never "finishes" normally
        assert clients["openrouter"].cancelled_request_ids == ["c1"]  # provider's own cancel() was called
    finally:
        mgr.stop_all()


def test_conversation_reset_cancels_only_that_conversation(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="reply text here", chunk_delay_s=0.05))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_cancelled", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "a"}], "stream": True, "request_id": "a1", "conversation_id": "convA"}))
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "b"}], "stream": False, "request_id": "b1", "conversation_id": "convB"}))
        time.sleep(0.06)
        mgr.event_bus.publish(ConversationReset(data={"conversation_id": "convA"}))
        a_cancelled = _wait_until(lambda: any(c["request_id"] == "a1" for c in ev["llm_cancelled"]), timeout_s=1.0)
        b_finished = _wait_until(lambda: any(r["request_id"] == "b1" for r in ev["assistant_response"]), timeout_s=2.0)
        assert a_cancelled and b_finished
    finally:
        mgr.stop_all()


# ============================================================================
# Automatic fallback / provider priority
# ============================================================================

def test_automatic_fallback_on_failure(harness):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", fail_status=429, fail_times=99),
        openai=MockProviderClient(name="openai", canned_text="fallback worked"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "openai", "gemini", "anthropic", "local"], enable_fallback=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated", "llm_error")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "f1"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["text"] == "fallback worked"
        assert ev["assistant_response"][0]["provider"] == "openai"
        assert len(ev["provider_fallback_activated"]) == 1
        assert ev["provider_fallback_activated"][0]["from_provider"] == "openrouter"
        assert ev["provider_fallback_activated"][0]["to_provider"] == "openai"
        assert len(ev["llm_error"]) == 0  # recovered via fallback - no terminal error published
    finally:
        mgr.stop_all()


def test_fallback_exhausted_publishes_llm_error(harness):
    clients = {name: MockProviderClient(name=name, fail=True) for name in PROVIDER_NAMES}
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_fallback=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated", "llm_error")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "f2"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["assistant_response"]) == 0
        assert len(ev["provider_fallback_activated"]) == 4  # tried all 5, fell back 4 times
    finally:
        mgr.stop_all()


def test_fallback_disabled_stops_at_first_failure(harness):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", fail=True),
        openai=MockProviderClient(name="openai", canned_text="should never be reached"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "openai"], enable_fallback=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "provider_fallback_activated", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "f3"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["provider_fallback_activated"]) == 0
        assert len(ev["assistant_response"]) == 0
        assert clients["openai"].calls == []  # never even attempted
    finally:
        mgr.stop_all()


def test_streaming_fallback_only_before_first_chunk(harness):
    """A provider that fails AFTER already yielding content must NOT
    trigger fallback (would double-speak partial output) - only a
    failure before any delta reaches the caller is fallback-eligible."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="partial words then boom", malformed=True),
        openai=MockProviderClient(name="openai", canned_text="should not be used"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "openai"], enable_fallback=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_error", "provider_fallback_activated", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "f4"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1, timeout_s=2.0)
        assert len(ev["llm_chunk"]) >= 1  # some content WAS streamed before the malformed failure
        assert len(ev["provider_fallback_activated"]) == 0  # not retried elsewhere
        assert len(ev["assistant_response"]) == 0
    finally:
        mgr.stop_all()


def test_invalid_request_not_fallback_eligible_by_default(harness):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", fail_status=400, fail_times=99),
        openai=MockProviderClient(name="openai", canned_text="should not be reached"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "openai"], enable_fallback=True, fallback_on_invalid_request=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "provider_fallback_activated")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "f5"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert len(ev["provider_fallback_activated"]) == 0
    finally:
        mgr.stop_all()


# ============================================================================
# Runtime switching / configuration reload
# ============================================================================

def test_switch_provider_changes_active_and_publishes_event(harness, monkeypatch):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="from openrouter"),
        gemini=MockProviderClient(name="gemini", canned_text="from gemini", configured=True),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "provider_switched", "assistant_response")
    try:
        assert adapter.manager_config.provider == "openrouter"
        ok = adapter.switch_provider("gemini")
        assert ok is True
        assert adapter.manager_config.provider == "gemini"
        assert _wait_until(lambda: len(ev["provider_switched"]) == 1)
        assert ev["provider_switched"][0] == {"from_provider": "openrouter", "to_provider": "gemini", "reason": "config_reload"}

        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "sw1"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "gemini"  # next request uses the new provider
    finally:
        mgr.stop_all()

    assert adapter.switch_provider("not-a-real-provider") is False  # unknown provider -> no-op


def test_provider_configured_reflects_real_usability(harness):
    """Bug fix: `switch_provider()` alone can't distinguish "switched to
    a provider that will actually answer" from "switched to a provider
    that has no API key and will silently fall through to whichever
    other one IS configured" - `provider_configured()` is the honest
    check `dashboard/controls.py::switch_llm_provider()` uses to warn
    about exactly that."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", configured=True),
        openai=MockProviderClient(name="openai", configured=False),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        assert adapter.provider_configured("openrouter") is True
        assert adapter.provider_configured("openai") is False
        assert adapter.provider_configured("not-a-real-provider") is False
    finally:
        mgr.stop_all()


def test_switching_to_unconfigured_provider_still_answers_via_the_configured_one(harness):
    """Confirms the exact user-facing symptom this bug fix addresses:
    switching the ACTIVE provider to one with no API key doesn't error -
    the next request still gets answered, just silently by whichever
    provider IS usable (openrouter here), not the one just switched to."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="from openrouter"),
        openai=MockProviderClient(name="openai", configured=False),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        assert adapter.switch_provider("openai") is True
        assert adapter.manager_config.provider == "openai"  # "selected", but...
        assert adapter.provider_configured("openai") is False  # ...not actually usable

        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "sw2"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openrouter"  # silently fell through, exactly as reported
    finally:
        mgr.stop_all()


def test_reload_model_event_can_override_provider_and_model(harness):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter"),
        anthropic=MockProviderClient(name="anthropic", canned_text="claude speaking", configured=True),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(ReloadModel(data={"provider": "anthropic", "model": "claude-sonnet-4-5"}))
        assert _wait_until(lambda: adapter.manager_config.provider == "anthropic")
        assert adapter.manager_config.default_model == "claude-sonnet-4-5"

        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "r1"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "anthropic"
    finally:
        mgr.stop_all()


def test_config_reload_pushes_fresh_config_into_existing_clients(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        original_config = clients["openrouter"].config
        mgr.event_bus.publish(ReloadModel(data={}))
        assert _wait_until(lambda: clients["openrouter"].config is not original_config)
    finally:
        mgr.stop_all()


def test_a_provider_that_gains_credentials_on_reload_becomes_usable(harness):
    """Simulates rotating a key into the environment then publishing
    ReloadModel - a provider that started `ProviderNotConfiguredError`
    (no key) should become usable without a restart once its
    `reload()`+`initialize()` succeed."""
    flaky = MockProviderClient(name="gemini", configured=False)
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", fail=True), gemini=flaky)
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "gemini"], enable_fallback=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        assert "gemini" in adapter._client_errors  # starts unusable (no key)
        flaky.configured = True  # "key was just added to .env"
        mgr.event_bus.publish(ReloadModel(data={}))
        assert _wait_until(lambda: "gemini" not in adapter._client_errors)
    finally:
        mgr.stop_all()


# ============================================================================
# Provider health monitoring / recovery
# ============================================================================

def test_health_poll_now_reports_per_provider_state(harness):
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", healthy=True),
        openai=MockProviderClient(name="openai", healthy=False, configured=True),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        health = adapter.poll_health_now()
        assert health["openrouter"]["state"] == "healthy"
        assert health["openai"]["state"] == "offline"
    finally:
        mgr.stop_all()


def test_provider_recovery_publishes_health_changed_event(harness):
    flaky = MockProviderClient(name="openai", healthy=False, configured=True)
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter"), openai=flaky)
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "provider_health_changed")
    try:
        adapter.poll_health_now()  # establishes baseline (offline)
        flaky.healthy = True  # provider recovers
        adapter.poll_health_now()
        assert _wait_until(lambda: any(
            e["provider"] == "openai" and e["from_state"] == "offline" and e["to_state"] == "healthy"
            for e in ev["provider_health_changed"]
        ))
    finally:
        mgr.stop_all()


def test_background_health_loop_runs_on_its_own(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), health_poll_interval_s=0.05)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        assert _wait_until(lambda: "openrouter" in adapter.provider_health_all(), timeout_s=1.0)
    finally:
        mgr.stop_all()


# ============================================================================
# Concurrency / stress
# ============================================================================

def test_concurrent_conversations_all_complete_independently(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients, request_workers=8)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        N = 20
        for i in range(N):
            mgr.event_bus.publish(NeedLLMResponse(data={
                "messages": [{"role": "user", "content": f"msg-{i}"}], "stream": (i % 2 == 0),
                "request_id": f"req-{i}", "conversation_id": f"conv-{i}",
            }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == N, timeout_s=5.0)
        conv_ids = {r["conversation_id"] for r in ev["assistant_response"]}
        assert conv_ids == {f"conv-{i}" for i in range(N)}
    finally:
        mgr.stop_all()


def test_stress_500_requests(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="ok"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients, request_workers=16)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "llm_error")
    try:
        N = 500
        t0 = time.time()
        for i in range(N):
            mgr.event_bus.publish(NeedLLMResponse(data={
                "messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": f"stress-{i}",
            }))
        ok = _wait_until(lambda: len(ev["assistant_response"]) + len(ev["llm_error"]) == N, timeout_s=30.0)
        elapsed = time.time() - t0
        assert ok, f"only {len(ev['assistant_response'])} of {N} completed"
        assert len(ev["llm_error"]) == 0
        assert len(ev["assistant_response"]) == N
        assert adapter.stats.requests_for("openrouter") == N
    finally:
        mgr.stop_all()


# ============================================================================
# Conversation continuity across a provider switch
# ============================================================================

def test_conversation_id_preserved_across_provider_switch(harness):
    """Conversation history itself lives in Context Builder/Planner
    (LLMManagerAdapter never stores message history) - this proves the
    ONE thing actually in this adapter's control (`conversation_id`
    round-tripping through every event) survives an active-provider
    switch mid-conversation, i.e. switching providers never loses or
    corrupts which conversation a reply belongs to."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="turn 1 reply"),
        openai=MockProviderClient(name="openai", canned_text="turn 2 reply", configured=True),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "turn 1"}], "stream": False,
            "request_id": "t1", "conversation_id": "conv-X",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)

        adapter.switch_provider("openai")

        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [
                {"role": "user", "content": "turn 1"}, {"role": "assistant", "content": "turn 1 reply"},
                {"role": "user", "content": "turn 2"},
            ],
            "stream": False, "request_id": "t2", "conversation_id": "conv-X",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 2)

        assert all(r["conversation_id"] == "conv-X" for r in ev["assistant_response"])
        assert ev["assistant_response"][0]["provider"] == "openrouter"
        assert ev["assistant_response"][1]["provider"] == "openai"
        assert ev["assistant_response"][1]["text"] == "turn 2 reply"
    finally:
        mgr.stop_all()


# ============================================================================
# Cost tracking
# ============================================================================

def test_cost_tracking_per_provider_conversation_and_day(harness):
    from luno.adapters.llm.models import ModelInfo

    clients = _all_mock_clients(openrouter=MockProviderClient(
        name="openrouter", canned_text="priced reply",
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000, "total_tokens": 1_500_000},
    ))
    # give the mock client a known catalog entry so cost estimation is exact
    clients["openrouter"].get_model_info = lambda model=None: ModelInfo(
        id="priced-model", provider="openrouter", input_cost_per_1m=2.0, output_cost_per_1m=4.0,
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
            "request_id": "cost1", "conversation_id": "conv-cost",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)

        stats = adapter.stats.to_dict()
        provider_bucket = stats["by_provider"]["openrouter"]
        assert provider_bucket["requests"] == 1
        assert provider_bucket["prompt_tokens"] == 1_000_000
        assert provider_bucket["completion_tokens"] == 500_000
        # 1M * $2/1M + 0.5M * $4/1M = $2 + $2 = $4
        assert provider_bucket["estimated_cost_usd"] == pytest.approx(4.0)

        conv_bucket = stats["by_conversation"]["conv-cost"]
        assert conv_bucket["requests"] == 1
        assert conv_bucket["estimated_cost_usd"] == pytest.approx(4.0)

        today = time.strftime("%Y-%m-%d", time.gmtime())
        assert stats["by_day"][today]["openrouter"]["requests"] == 1
    finally:
        mgr.stop_all()


def test_cost_tracking_unknown_model_marks_estimate_incomplete(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(
        name="openrouter", canned_text="no pricing", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    ))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "cost2"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        bucket = adapter.stats.to_dict()["by_provider"]["openrouter"]
        # MockProviderClient's default get_model_info() reports $0/1M -
        # a KNOWN price of zero, not "unknown" - estimate stays complete.
        assert bucket["cost_is_estimate_complete"] is True
        assert bucket["estimated_cost_usd"] == 0.0
    finally:
        mgr.stop_all()


def test_failed_requests_recorded_as_failures_in_stats(harness):
    clients = _all_mock_clients(**{name: MockProviderClient(name=name, fail=True) for name in PROVIDER_NAMES})
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_fallback=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_error")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "fail1"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        bucket = adapter.stats.to_dict()["by_provider"]["openrouter"]
        assert bucket["requests"] == 1 and bucket["failures"] == 1
    finally:
        mgr.stop_all()


# ============================================================================
# Backward compatibility (legacy `.client`/`.default_model` shim)
# ============================================================================

def test_legacy_client_shim_matches_chat_completion_duck_type(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="legacy shape works"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), default_model="pinned-model")
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        assert adapter.default_model == "pinned-model"
        response = adapter.client.chat_completion(model=None, messages=[{"role": "user", "content": "hi"}], max_tokens=50)
        assert hasattr(response, "text")
        assert response.text == "legacy shape works"
    finally:
        mgr.stop_all()


def test_is_mock_active_provider_reflects_real_vs_mock(harness):
    real_looking = MockProviderClient(name="openrouter", canned_text="real-ish")
    clients = _all_mock_clients(openrouter=real_looking)
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        assert adapter.is_mock_active_provider is True  # MockProviderClient is always "mock" for this check

        adapter.switch_provider("openai")  # openai client here is unconfigured (mock, configured=False)
        assert adapter.is_mock_active_provider is True
    finally:
        mgr.stop_all()


# ============================================================================
# Regression: capability/status surface
# ============================================================================

def test_capabilities_for_and_list_all_models(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", tools=True, images=True))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        caps = adapter.capabilities_for("openrouter")
        assert caps["tools"] is True and caps["images"] is True

        models = adapter.list_all_models()
        assert "openrouter" in models
        assert models["openrouter"][0]["provider"] == "openrouter"
    finally:
        mgr.stop_all()


def test_status_exposes_dashboard_required_fields(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    try:
        status = adapter.status()
        for key in (
            "active_provider", "default_model", "priority", "enable_fallback", "enable_streaming",
            "configured_providers", "unconfigured_providers", "health", "stats", "last_fallback",
        ):
            assert key in status, f"missing dashboard-required field '{key}'"
    finally:
        mgr.stop_all()


# ============================================================================
# Intelligent AI Routing Engine sprint - per-request provider override
# (data["provider"] on NeedLLMResponse - see _priority_order(requested_provider))
# ============================================================================

def test_provider_override_tries_requested_provider_first(harness):
    """Global active provider is 'openrouter', but this ONE request asks
    for 'openai' via `data["provider"]` - it must be answered by openai,
    not openrouter, with zero global config change."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="from openrouter"),
        openai=MockProviderClient(name="openai", canned_text="from openai"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
            "request_id": "ov1", "provider": "openai",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openai"
        assert ev["assistant_response"][0]["text"] == "from openai"
        assert clients["openrouter"].calls == []  # never even tried
        # global config completely untouched by the per-request override
        assert adapter.manager_config.provider == "openrouter"
        assert adapter._priority_order()[0] == "openrouter"
    finally:
        mgr.stop_all()


def test_provider_override_falls_back_to_configured_order_on_failure(harness):
    """The requested override still participates in normal fallback -
    it's tried FIRST, and on failure the REST of the configured priority
    order (minus the override, in its original relative order) is tried
    next - here: openai (override, fails) -> openrouter (next in the
    configured list, fails) -> gemini (succeeds)."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", fail_status=500, fail_times=99),
        openai=MockProviderClient(name="openai", fail_status=500, fail_times=99),
        gemini=MockProviderClient(name="gemini", canned_text="gemini saved the day"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=["openrouter", "openai", "gemini", "anthropic", "local"], enable_fallback=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "provider_fallback_activated")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
            "request_id": "ov2", "provider": "openai",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "gemini"
        assert ev["provider_fallback_activated"][0]["from_provider"] == "openai"
        assert ev["provider_fallback_activated"][1]["from_provider"] == "openrouter"
        assert clients["openai"].calls != []  # override WAS tried, first
    finally:
        mgr.stop_all()


def test_provider_override_unknown_provider_name_is_ignored(harness):
    """An unrecognized/unusable override must fail OPEN (use the normal
    configured priority order) rather than error the request."""
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="normal order"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "llm_error")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
            "request_id": "ov3", "provider": "not-a-real-provider",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openrouter"
        assert len(ev["llm_error"]) == 0
    finally:
        mgr.stop_all()


def test_provider_override_absent_is_identical_to_no_override(harness):
    """Regression guard for the sprint's own backward-compatibility
    claim: a request with no `data["provider"]` key at all behaves
    exactly like before this sprint - normal configured priority order,
    no override log noise, no behavior change."""
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="unchanged"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "ov4",
        }))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["provider"] == "openrouter"
    finally:
        mgr.stop_all()


def test_provider_override_is_per_request_not_sticky(harness):
    """Two consecutive requests with different overrides must each be
    independently honored - the override never leaks from one request
    into the next (it's not stored on `self.manager_config` at all)."""
    clients = _all_mock_clients(
        openrouter=MockProviderClient(name="openrouter", canned_text="or"),
        openai=MockProviderClient(name="openai", canned_text="oa"),
        gemini=MockProviderClient(name="gemini", canned_text="ge"),
    )
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES))
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "ov5a", "provider": "openai"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "ov5b", "provider": "gemini"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 2)
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False, "request_id": "ov5c"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 3)

        by_request = {e["request_id"]: e["provider"] for e in ev["assistant_response"]}
        assert by_request["ov5a"] == "openai"
        assert by_request["ov5b"] == "gemini"
        assert by_request["ov5c"] == "openrouter"
    finally:
        mgr.stop_all()
