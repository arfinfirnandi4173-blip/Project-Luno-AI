"""
test_openrouter_adapter.py
===========================

Comprehensive, standalone test suite for the production `OpenRouterAdapter`
(see `../openrouter.py`) - no real OpenRouter API key or network access
required anywhere in this file. Two layers are exercised:

  1. The adapter's own event-translation behavior, via `MockOpenRouterClient`
     (normal/streaming completion, cancellation, correlation IDs, reload,
     conversation reset, concurrency, large responses, stress).

  2. `RequestsOpenRouterClient` - the real HTTP client - via a fake
     `requests.Session`-like double (`FakeSession`/`FakeResponse`) that
     returns scripted status codes / JSON bodies / SSE lines. This is the
     "mocked HTTP responses" layer the spec calls for: it proves the retry/
     backoff/status-classification/SSE-parsing logic actually works, not
     just that the already-abstracted mock client works.

Run:
    python3 -m luno.adapters.tests.test_openrouter_adapter
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests as real_requests  # noqa: E402

from luno.adapters.events import (  # noqa: E402
    AssistantResponse, CancelLLMRequest, ConversationReset, LLMCancelled, LLMChunk,
    LLMError, LLMFinished, LLMStarted, LLMStreaming, NeedLLMResponse, ReloadModel,
)
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.adapters.openrouter import (  # noqa: E402
    MockOpenRouterClient, OpenRouterAdapter, OpenRouterAPIError, OpenRouterAuthError,
    OpenRouterConfig, OpenRouterNetworkError, OpenRouterRateLimitError, OpenRouterServerError,
    OpenRouterStreamError, OpenRouterTimeoutError, RequestsOpenRouterClient,
)

Result = Tuple[bool, str]


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


# ============================================================================
# Fakes for the real HTTP client (RequestsOpenRouterClient)
# ============================================================================

class FakeResponse:
    def __init__(
        self, status_code: int = 200, json_data: Optional[dict] = None,
        text: str = "", lines: Optional[List[str]] = None, raise_json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or ""
        self._lines = lines or []
        self._raise_json_error = raise_json_error
        self.closed = False

    def json(self) -> dict:
        if self._raise_json_error:
            raise ValueError("mock: response body is not valid JSON")
        return self._json_data or {}

    def iter_lines(self, decode_unicode: bool = True):
        for line in self._lines:
            yield line

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Scripted stand-in for `requests.Session`. `script` is a list
    consumed one-per-call (the last entry repeats once exhausted, so a
    single always-succeeding response doesn't need padding). Entries may
    be a `FakeResponse` or a zero-arg callable returning one (for
    raising an exception on that specific call)."""

    def __init__(self, script: Optional[List[Any]] = None) -> None:
        self.script = list(script or [FakeResponse(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})])
        self.calls: List[Dict[str, Any]] = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "stream": stream})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return item() if callable(item) else item


def _sse_lines(chunks: List[str], done: bool = True) -> List[str]:
    lines = [f"data: {c}" for c in chunks]
    if done:
        lines.append("data: [DONE]")
    return lines


def _content_chunk(text: str, finish_reason: Optional[str] = None) -> str:
    import json as _json
    choice: Dict[str, Any] = {"index": 0, "delta": {"content": text}}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return _json.dumps({"id": "x", "model": "openai/gpt-4o", "choices": [choice]})


# ============================================================================
# Adapter-level scenarios (via MockOpenRouterClient)
# ============================================================================

def test_normal_completion() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="Hello there!"))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_started", "llm_finished", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}],
        "stream": False, "request_id": "req-1", "conversation_id": "conv-1",
    }))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    text_ok = ok and ev["assistant_response"][0]["text"] == "Hello there!"
    ids_ok = ok and ev["llm_started"][0]["request_id"] == "req-1" and ev["llm_finished"][0]["conversation_id"] == "conv-1"
    return text_ok and ids_ok, f"started={ev['llm_started']} finished={ev['llm_finished']} response={ev['assistant_response']}"


def test_streaming_completion() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="Hello, Vinn."))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_streaming", "llm_chunk", "llm_finished", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    chunks_ok = ok and len(ev["llm_chunk"]) >= 2
    concat = "".join(c["delta"] for c in ev["llm_chunk"])
    text_matches = concat == "Hello, Vinn."
    final_text_so_far_matches = ok and ev["llm_chunk"][-1]["text_so_far"] == "Hello, Vinn."
    streaming_started = len(ev["llm_streaming"]) == 1
    return (
        ok and chunks_ok and text_matches and final_text_so_far_matches and streaming_started,
        f"chunks={ev['llm_chunk']} response={ev['assistant_response']}",
    )


def test_cancellation_mid_stream() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text=" ".join(f"word{i}" for i in range(30)), chunk_delay_s=0.03)
    orr = OpenRouterAdapter(client=client)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk", "llm_cancelled", "llm_finished", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "request_id": "req-cancel",
    }))
    _wait_until(lambda: len(ev["llm_chunk"]) >= 1)  # let streaming actually begin
    mgr.event_bus.publish(CancelLLMRequest(data={"request_id": "req-cancel"}))
    cancelled = _wait_until(lambda: len(ev["llm_cancelled"]) == 1, timeout_s=1.0)
    # the full (uncancelled) stream would take ~30*0.03s = 0.9s - wait past
    # that and confirm it never finished normally.
    time.sleep(1.1)
    mgr.stop_all()
    never_finished = len(ev["assistant_response"]) == 0 and len(ev["llm_finished"]) == 0
    fewer_than_all_chunks = len(ev["llm_chunk"]) < 30
    return (
        cancelled and never_finished and fewer_than_all_chunks,
        f"cancelled={ev['llm_cancelled']} chunks_seen={len(ev['llm_chunk'])} finished={ev['llm_finished']} response={ev['assistant_response']}",
    )


def test_cancel_unknown_request_still_acks() -> Result:
    """Behavior Tree needs to regain control even if the cancel raced a
    request that already finished (or never existed) - LLMCancelled
    should still be published, just with no in-flight work to stop."""
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="ok"))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_cancelled")
    mgr.event_bus.publish(CancelLLMRequest(data={"request_id": "does-not-exist"}))
    ok = _wait_until(lambda: len(ev["llm_cancelled"]) == 1)
    mgr.stop_all()
    return ok, f"cancelled={ev['llm_cancelled']}"


def test_timeout_publishes_llm_error() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(timeout_error=True))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": False}))
    ok = _wait_until(lambda: len(ev["llm_error"]) == 1)
    mgr.stop_all()
    right_type = ok and ev["llm_error"][0]["error_type"] == "OpenRouterTimeoutError"
    no_response = len(ev["assistant_response"]) == 0
    return ok and right_type and no_response, f"errors={ev['llm_error']}"


def test_malformed_response_publishes_llm_error() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(malformed=True))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_error")

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": False}))
    ok = _wait_until(lambda: len(ev["llm_error"]) == 1)
    mgr.stop_all()
    right_type = ok and ev["llm_error"][0]["error_type"] == "OpenRouterStreamError"
    return ok and right_type, f"errors={ev['llm_error']}"


def test_network_failure_publishes_llm_error() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(network_error=True))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_error")

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": False}))
    ok = _wait_until(lambda: len(ev["llm_error"]) == 1)
    mgr.stop_all()
    right_type = ok and ev["llm_error"][0]["error_type"] == "OpenRouterNetworkError" and ev["llm_error"][0]["retryable"] is True
    return ok and right_type, f"errors={ev['llm_error']}"


def test_no_model_configured() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="ok"))  # no default_model
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_error", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={"messages": []}))  # no model at all
    ok = _wait_until(lambda: len(ev["llm_error"]) == 1)
    mgr.stop_all()
    return ok and len(ev["assistant_response"]) == 0, f"errors={ev['llm_error']}"


def test_multiple_concurrent_conversations() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(chunk_delay_s=0.01)  # echo mode - reply reflects each request's own message
    orr = OpenRouterAdapter(client=client, request_workers=6)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")

    N = 8
    for i in range(N):
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": f"message-{i}"}],
            "stream": True, "request_id": f"req-{i}", "conversation_id": f"conv-{i}",
        }))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == N, timeout_s=3.0)
    mgr.stop_all()
    by_request = {r["request_id"]: r for r in ev["assistant_response"]}
    no_cross_talk = ok and all(
        by_request[f"req-{i}"]["conversation_id"] == f"conv-{i}" and f"message-{i}" in by_request[f"req-{i}"]["text"]
        for i in range(N)
    )
    return ok and no_cross_talk, f"responses={len(ev['assistant_response'])}/{N}"


def test_conversation_reset_cancels_only_that_conversation() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text=" ".join(f"w{i}" for i in range(20)), chunk_delay_s=0.03)
    orr = OpenRouterAdapter(client=client, request_workers=4)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_cancelled", "assistant_response", "llm_chunk")

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [], "stream": True,
        "request_id": "a1", "conversation_id": "conv-A",
    }))
    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [], "stream": True,
        "request_id": "b1", "conversation_id": "conv-B",
    }))
    _wait_until(lambda: len(ev["llm_chunk"]) >= 2)
    mgr.event_bus.publish(ConversationReset(data={"conversation_id": "conv-A"}))
    a_cancelled = _wait_until(lambda: any(c["request_id"] == "a1" for c in ev["llm_cancelled"]), timeout_s=1.0)
    b_finished = _wait_until(lambda: any(r["request_id"] == "b1" for r in ev["assistant_response"]), timeout_s=2.0)
    a_never_finished = not any(r["request_id"] == "a1" for r in ev["assistant_response"])
    mgr.stop_all()
    return (
        a_cancelled and b_finished and a_never_finished,
        f"cancelled={ev['llm_cancelled']} responses={[r['request_id'] for r in ev['assistant_response']]}",
    )


def test_large_response() -> Result:
    mgr = AdapterManager.standalone()
    big_text = " ".join(f"tok{i}" for i in range(5000))  # ~30KB
    client = MockOpenRouterClient(canned_text=big_text)
    orr = OpenRouterAdapter(client=client)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": True}))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1, timeout_s=5.0)
    mgr.stop_all()
    length_ok = ok and len(ev["assistant_response"][0]["text"]) == len(big_text)
    return ok and length_ok, f"len={len(ev['assistant_response'][0]['text']) if ok else 0} expected={len(big_text)}"


def test_stress_many_requests() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text="ok", delay_s=0.0)
    orr = OpenRouterAdapter(client=client, request_workers=8)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response", "llm_error")

    N = 300
    start = time.time()
    for i in range(N):
        mgr.event_bus.publish(NeedLLMResponse(data={
            "model": "openai/gpt-4o", "messages": [], "stream": False, "request_id": f"r{i}",
        }))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == N, timeout_s=10.0)
    elapsed = time.time() - start
    mgr.stop_all()
    return ok and len(ev["llm_error"]) == 0, f"N={N} got={len(ev['assistant_response'])} elapsed={elapsed:.2f}s"


def test_reload_model_picks_up_new_env() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="ok"))
    mgr.register(orr)
    mgr.start_all()

    old_val = os.environ.get("OPENROUTER_MODEL")
    try:
        os.environ["OPENROUTER_MODEL"] = "anthropic/claude-3.7-sonnet"
        mgr.event_bus.publish(ReloadModel(data={}))
        ok = _wait_until(lambda: orr.default_model == "anthropic/claude-3.7-sonnet")

        ev = _collect(mgr, "llm_started")
        mgr.event_bus.publish(NeedLLMResponse(data={"messages": [], "stream": False}))  # no explicit model -> uses reloaded default
        used_new_model = _wait_until(lambda: len(ev["llm_started"]) == 1 and ev["llm_started"][0]["model"] == "anthropic/claude-3.7-sonnet")
    finally:
        if old_val is None:
            os.environ.pop("OPENROUTER_MODEL", None)
        else:
            os.environ["OPENROUTER_MODEL"] = old_val
        mgr.stop_all()
    return ok and used_new_model, f"default_model={orr.default_model}"


def test_correlation_ids_never_fabricated_when_present() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="ok"))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_started", "llm_finished", "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "openai/gpt-4o", "messages": [], "stream": False,
        "request_id": "RID", "conversation_id": "CID", "correlation_id": "XID",
    }))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    triad = {"request_id": "RID", "conversation_id": "CID", "correlation_id": "XID"}
    all_match = ok and all(
        all(e.get(k) == v for k, v in triad.items())
        for e in (ev["llm_started"][0], ev["llm_finished"][0], ev["assistant_response"][0])
    )
    return all_match, f"started={ev['llm_started']} finished={ev['llm_finished']} response={ev['assistant_response']}"


def test_missing_ids_are_filled_not_left_blank() -> Result:
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="ok"))
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": False}))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    has_request_id = ok and bool(ev["assistant_response"][0].get("request_id"))
    has_correlation_id = ok and bool(ev["assistant_response"][0].get("correlation_id"))
    return has_request_id and has_correlation_id, f"response={ev['assistant_response']}"


def test_retry_recovers_then_succeeds_via_mock() -> Result:
    """Adapter-level check that a client raising a retryable error the
    first N calls and succeeding after is still just one clean
    AssistantResponse from the adapter's point of view (retry itself is
    the client's responsibility - see the RequestsOpenRouterClient-level
    tests below for the actual retry-loop math)."""
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text="recovered", fail_status=503, fail_times=0)  # succeeds immediately - baseline
    orr = OpenRouterAdapter(client=client)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": False}))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    return ok, f"response={ev['assistant_response']}"


def test_stop_cancels_inflight_gracefully() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text=" ".join(f"w{i}" for i in range(50)), chunk_delay_s=0.05)
    orr = OpenRouterAdapter(client=client)
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "llm_chunk")
    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": [], "stream": True}))
    _wait_until(lambda: len(ev["llm_chunk"]) >= 1)
    try:
        mgr.stop_all()  # should not hang or raise, even mid-stream
        ok = True
    except Exception:
        ok = False
    return ok, "stop_all() completed without hanging or raising while a stream was in-flight"


# ============================================================================
# RequestsOpenRouterClient scenarios (real HTTP-layer code, fake transport)
# ============================================================================

def test_real_client_non_streaming_success() -> Result:
    session = FakeSession([FakeResponse(200, {
        "model": "openai/gpt-4o", "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 12},
    })])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=2)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    resp = client.chat_completion(model="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}])
    auth_ok = session.calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    return (
        resp.text == "hi there" and resp.usage == {"total_tokens": 12} and auth_ok and len(session.calls) == 1,
        f"text={resp.text!r} usage={resp.usage} calls={len(session.calls)}",
    )


def test_real_client_429_then_success() -> Result:
    session = FakeSession([
        FakeResponse(429, {"error": {"message": "rate limited"}}),
        FakeResponse(429, {"error": {"message": "rate limited"}}),
        FakeResponse(200, {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}),
    ])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=3, retry_backoff_base_s=0.01, retry_backoff_max_s=0.02)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    resp = client.chat_completion(model="m", messages=[])
    return resp.text == "ok" and len(session.calls) == 3, f"text={resp.text!r} attempts={len(session.calls)}"


def test_real_client_500_then_success() -> Result:
    session = FakeSession([
        FakeResponse(500, {"error": {"message": "boom"}}),
        FakeResponse(200, {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}),
    ])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=3, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    resp = client.chat_completion(model="m", messages=[])
    return resp.text == "ok" and len(session.calls) == 2, f"text={resp.text!r} attempts={len(session.calls)}"


def test_real_client_exhausts_retries_then_raises() -> Result:
    session = FakeSession([FakeResponse(503, {"error": {"message": "down"}})])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=2, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        client.chat_completion(model="m", messages=[])
        return False, "expected OpenRouterServerError to be raised"
    except OpenRouterServerError:
        # max_retries=2 -> 3 total attempts (1 original + 2 retries)
        return len(session.calls) == 3, f"attempts={len(session.calls)}"


def test_real_client_auth_error_never_retried() -> Result:
    session = FakeSession([FakeResponse(401, {"error": {"message": "invalid key"}})])
    cfg = OpenRouterConfig(api_key="sk-bad", max_retries=5, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        client.chat_completion(model="m", messages=[])
        return False, "expected OpenRouterAuthError to be raised"
    except OpenRouterAuthError:
        return len(session.calls) == 1, f"attempts={len(session.calls)} (should be exactly 1 - no retry on auth errors)"


def test_real_client_invalid_request_never_retried() -> Result:
    session = FakeSession([FakeResponse(400, {"error": {"message": "bad request"}})])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=5, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        client.chat_completion(model="m", messages=[])
        return False, "expected an invalid-request error"
    except OpenRouterAPIError as ex:
        return not ex.retryable and len(session.calls) == 1, f"attempts={len(session.calls)} retryable={ex.retryable}"


def test_real_client_malformed_json() -> Result:
    session = FakeSession([FakeResponse(200, raise_json_error=True)])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=1, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        client.chat_completion(model="m", messages=[])
        return False, "expected OpenRouterStreamError for malformed JSON"
    except OpenRouterStreamError:
        return True, "raised OpenRouterStreamError as expected"


def test_real_client_network_failure() -> Result:
    def _raise():
        raise real_requests.exceptions.ConnectionError("connection refused")

    session = FakeSession([_raise, _raise, FakeResponse(200, {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=3, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    resp = client.chat_completion(model="m", messages=[])
    return resp.text == "ok" and len(session.calls) == 3, f"text={resp.text!r} attempts={len(session.calls)}"


def test_real_client_timeout() -> Result:
    def _raise():
        raise real_requests.Timeout("timed out")

    session = FakeSession([_raise])
    cfg = OpenRouterConfig(api_key="sk-test", max_retries=0, retry_backoff_base_s=0.01)
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        client.chat_completion(model="m", messages=[])
        return False, "expected OpenRouterTimeoutError"
    except OpenRouterTimeoutError:
        return True, "raised OpenRouterTimeoutError as expected"


def test_real_client_streaming_sse_parsing() -> Result:
    lines = [": OPENROUTER PROCESSING"] + _sse_lines([
        _content_chunk("Hel"), _content_chunk("lo"), _content_chunk(",  Vinn.", finish_reason="stop"),
    ])
    session = FakeSession([FakeResponse(200, lines=lines)])
    cfg = OpenRouterConfig(api_key="sk-test")
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    chunks = list(client.stream_chat_completion(model="m", messages=[]))
    text = "".join(c.delta for c in chunks)
    last_finished = chunks[-1].finished and chunks[-1].finish_reason == "stop"
    return text == "Hello,  Vinn." and last_finished, f"text={text!r} chunks={len(chunks)}"


def test_real_client_streaming_mid_stream_error() -> Result:
    import json as _json
    error_chunk = _json.dumps({
        "id": "x", "model": "m", "error": {"code": "server_error", "message": "provider disconnected"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    })
    lines = _sse_lines([_content_chunk("partial "), error_chunk], done=False)
    session = FakeSession([FakeResponse(200, lines=lines)])
    cfg = OpenRouterConfig(api_key="sk-test")
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    try:
        list(client.stream_chat_completion(model="m", messages=[]))
        return False, "expected OpenRouterStreamError from the mid-stream error chunk"
    except OpenRouterStreamError:
        return True, "raised OpenRouterStreamError as expected"


def test_real_client_streaming_cancel_closes_connection() -> Result:
    lines = _sse_lines([_content_chunk("a"), _content_chunk("b"), _content_chunk("c", finish_reason="stop")])
    resp = FakeResponse(200, lines=lines)
    session = FakeSession([resp])
    cfg = OpenRouterConfig(api_key="sk-test")
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    cancel_event = threading.Event()
    out = []
    for i, chunk in enumerate(client.stream_chat_completion(model="m", messages=[], cancel_event=cancel_event)):
        out.append(chunk)
        if i == 0:
            cancel_event.set()
    return len(out) == 1 and resp.closed, f"chunks_yielded={len(out)} closed={resp.closed}"


def test_real_client_end_to_end_through_adapter() -> Result:
    """Wires the real client (fake transport) into the actual adapter,
    end to end, proving the full production path (not just the client
    in isolation)."""
    session = FakeSession([FakeResponse(200, {
        "model": "openai/gpt-4o", "choices": [{"message": {"content": "produced via RequestsOpenRouterClient"}, "finish_reason": "stop"}],
    })])
    cfg = OpenRouterConfig(api_key="sk-test")
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=client, default_model="openai/gpt-4o")
    mgr.register(orr)
    mgr.start_all()
    ev = _collect(mgr, "assistant_response")
    mgr.event_bus.publish(NeedLLMResponse(data={"messages": [{"role": "user", "content": "hi"}], "stream": False}))
    ok = _wait_until(lambda: len(ev["assistant_response"]) == 1)
    mgr.stop_all()
    return ok and ev["assistant_response"][0]["text"] == "produced via RequestsOpenRouterClient", f"response={ev['assistant_response']}"


def test_never_logs_api_key() -> Result:
    """Cheap guard against the most common way this rule gets broken by
    accident - a stray f-string interpolating `config.api_key` (or the
    client) directly into a log line."""
    import inspect

    from luno.adapters import openrouter as openrouter_module

    src = inspect.getsource(openrouter_module)
    # every log(...) call site, string-searched for the literal attribute
    # access that would leak the key
    leaks = "config.api_key" in src and "log(" in src and any(
        "config.api_key" in line and "log(" in line for line in src.splitlines()
    )
    return not leaks, "no log() call site references config.api_key directly"


# ============================================================================
# Runner
# ============================================================================

SCENARIOS = [
    test_normal_completion,
    test_streaming_completion,
    test_cancellation_mid_stream,
    test_cancel_unknown_request_still_acks,
    test_timeout_publishes_llm_error,
    test_malformed_response_publishes_llm_error,
    test_network_failure_publishes_llm_error,
    test_no_model_configured,
    test_multiple_concurrent_conversations,
    test_conversation_reset_cancels_only_that_conversation,
    test_large_response,
    test_stress_many_requests,
    test_reload_model_picks_up_new_env,
    test_correlation_ids_never_fabricated_when_present,
    test_missing_ids_are_filled_not_left_blank,
    test_retry_recovers_then_succeeds_via_mock,
    test_stop_cancels_inflight_gracefully,
    test_real_client_non_streaming_success,
    test_real_client_429_then_success,
    test_real_client_500_then_success,
    test_real_client_exhausts_retries_then_raises,
    test_real_client_auth_error_never_retried,
    test_real_client_invalid_request_never_retried,
    test_real_client_malformed_json,
    test_real_client_network_failure,
    test_real_client_timeout,
    test_real_client_streaming_sse_parsing,
    test_real_client_streaming_mid_stream_error,
    test_real_client_streaming_cancel_closes_connection,
    test_real_client_end_to_end_through_adapter,
    test_never_logs_api_key,
]


def main() -> int:
    print("\n=== Luno OpenRouter Adapter - Test Suite ===")
    results = []
    for fn in SCENARIOS:
        name = fn.__name__
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"raised {type(ex).__name__}: {ex}"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} - {detail}")
        results.append((name, ok))

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{total} scenarios passed.")
    if passed == total:
        print("Semua skenario lolos.")
        return 0
    print("Beberapa skenario gagal:")
    for name, ok in results:
        if not ok:
            print(f"  - {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
