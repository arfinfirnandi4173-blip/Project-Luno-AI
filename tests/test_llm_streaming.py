"""
test_llm_streaming.py
========================

LLM Streaming -> Real-Time Speech Pipeline sprint - Phase 13, "LLM
STREAMING" scenarios (1-8).

This file does NOT introduce a new streaming abstraction to test - the
Phase 0 audit for this sprint found that `luno.adapters.llm_manager.
LLMManagerAdapter`/`luno.adapters.llm.base.LLMProviderClient.stream_chat()`/
`LLMStreamChunk`/the `llm_streaming`/`llm_chunk`/`llm_finished`/`llm_error`/
`llm_cancelled` event contract ALREADY exist, are ALREADY provider-agnostic
(shared by OpenRouter/OpenAI/Local via `OpenAICompatibleClient`, and
implemented natively by Gemini/Anthropic), and are ALREADY exercised by
`luno/adapters/tests/test_llm_manager.py`/`luno/adapters/llm/tests/test_providers.py`.
This file is a THIN, dedicated confirmation of exactly the 8 scenarios
this sprint's own brief calls out, using the SAME `MockProviderClient`/
`LLMManagerAdapter` harness those existing files already use - not a
second, competing test convention.

No real network access anywhere - `MockProviderClient` only.

Run:
    python3 -m pytest tests/test_llm_streaming.py -q
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import NeedLLMResponse  # noqa: E402
from luno.adapters.llm.config import PROVIDER_NAMES, LLMManagerConfig  # noqa: E402
from luno.adapters.llm.mock import MockProviderClient  # noqa: E402
from luno.adapters.llm_manager import LLMManagerAdapter  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.adapters.models import AdapterConfig  # noqa: E402


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


# ============================================================================
# 1. provider yields multiple partial chunks
# ============================================================================

def test_1_provider_yields_multiple_partial_chunks(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="Memory Luno menyimpan data berdasarkan konteks."))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r1"}))
        assert _wait_until(lambda: len(ev["llm_finished"]) == 1)
        assert len(ev["llm_chunk"]) > 1, "expected multiple partial chunks, not one giant block"
    finally:
        mgr.stop_all()


# ============================================================================
# 2. generic adapter preserves order
# ============================================================================

def test_2_chunk_order_preserved_across_reassembly(harness):
    text = "Satu dua tiga empat lima enam tujuh delapan sembilan sepuluh"
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text=text))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r2"}))
        assert _wait_until(lambda: len(ev["llm_finished"]) == 1)
        reassembled = "".join(c["delta"] for c in ev["llm_chunk"])
        assert reassembled == text
        # text_so_far is monotonically growing and always a prefix of the final text
        for c in ev["llm_chunk"]:
            assert text.startswith(c["text_so_far"])
    finally:
        mgr.stop_all()


# ============================================================================
# 3. final marker handled
# ============================================================================

def test_3_final_marker_reaches_llm_finished_and_assistant_response(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="Selesai."))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_finished", "assistant_response")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r3"}))
        assert _wait_until(lambda: len(ev["assistant_response"]) == 1)
        assert ev["assistant_response"][0]["text"] == "Selesai."
        assert len(ev["llm_finished"]) == 1
    finally:
        mgr.stop_all()


# ============================================================================
# 4. empty partial ignored
# ============================================================================

def test_4_empty_partial_never_reaches_incremental_buffer(harness):
    """`LLMManagerAdapter` itself only publishes `llm_chunk` when
    `chunk.delta` is truthy (see `_run_streaming_with_fallback()`) - an
    empty/whitespace-only partial from the provider never produces an
    `llm_chunk` event at all, which is what
    `StreamingSpeechCoordinator._on_chunk()` also independently guards
    against (belt-and-suspenders - see `test_streaming_speech_integration.py`)."""
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="Halo"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r4"}))
        assert _wait_until(lambda: len(ev["llm_finished"]) == 1)
        assert all(c["delta"] for c in ev["llm_chunk"]), "an empty-delta llm_chunk was published"
    finally:
        mgr.stop_all()


# ============================================================================
# 5. provider error handled
# ============================================================================

def test_5_provider_error_mid_stream_publishes_llm_error_not_finished(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="a somewhat long reply here to fail mid stream", malformed=True))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True, enable_fallback=False)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_error", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r5"}))
        assert _wait_until(lambda: len(ev["llm_error"]) == 1)
        assert ev["llm_finished"] == []
        assert len(ev["llm_chunk"]) >= 1, "partial chunks should have been published before the mid-stream failure"
    finally:
        mgr.stop_all()


# ============================================================================
# 6. cancellation handled
# ============================================================================

def test_6_cancellation_mid_stream_publishes_llm_cancelled(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="a long reply here", chunk_delay_s=0.05))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_cancelled", "llm_finished")
    try:
        from luno.adapters.events import CancelLLMRequest
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "r6"}))
        assert _wait_until(lambda: len(ev["llm_chunk"]) >= 1)
        mgr.event_bus.publish(CancelLLMRequest(data={"request_id": "r6"}))
        assert _wait_until(lambda: len(ev["llm_cancelled"]) == 1)
        assert ev["llm_finished"] == []
    finally:
        mgr.stop_all()


# ============================================================================
# 7. request_id preserved
# ============================================================================

def test_7_request_id_preserved_on_every_chunk(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="a b c d e"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": "req-correlate"}))
        assert _wait_until(lambda: len(ev["llm_finished"]) == 1)
        assert all(c["request_id"] == "req-correlate" for c in ev["llm_chunk"])
    finally:
        mgr.stop_all()


# ============================================================================
# 8. conversation_id preserved
# ============================================================================

def test_8_conversation_id_preserved_on_every_chunk(harness):
    clients = _all_mock_clients(openrouter=MockProviderClient(name="openrouter", canned_text="a b c"))
    cfg = LLMManagerConfig(provider="openrouter", priority=list(PROVIDER_NAMES), enable_streaming=True)
    mgr, adapter = harness(manager_config=cfg, clients=clients)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_finished")
    try:
        mgr.event_bus.publish(NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "request_id": "r8", "conversation_id": "conv-correlate",
        }))
        assert _wait_until(lambda: len(ev["llm_finished"]) == 1)
        assert all(c["conversation_id"] == "conv-correlate" for c in ev["llm_chunk"])
    finally:
        mgr.stop_all()
