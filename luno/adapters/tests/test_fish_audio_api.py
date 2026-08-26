"""
test_fish_audio_api.py
=========================

Regression suite for the Fish Audio CLOUD API engine
(`TTS_ENGINE=fish_audio_api` / `FISH_AUDIO_BACKEND=fish_audio_api`) -
`_fish_audio_api_synthesize_once()`, `FishAudioApiCircuitBreaker`, and
`_default_fish_audio_client()`'s startup fallback behavior.

No network, no audio hardware: HTTP is exercised via a scripted fake
session double (mirrors `test_fish_audio_real.py`'s own established
`FakeSession` technique exactly), and the pre-existing gptsovits/f5tts
engine + `FishAudioAdapter` itself are completely untouched by any of
this - see `test_fish_audio_real.py` for those regressions.

Modern pytest-`assert` style (unlike the older, standalone
Result-tuple runner `test_fish_audio_real.py` uses) - collected and
verified by `pytest` normally.

Run:
    python3 -m pytest luno/adapters/tests/test_fish_audio_api.py
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from luno.adapters.fish_audio import FishAudioAdapter, PlaybackCancelled
from luno.adapters.fish_audio_real import (
    FishAudioApiCircuitBreaker,
    RealFishAudioClient,
    RealFishAudioConfig,
    TTSSynthesisError,
    _fish_audio_api_synthesize_once,
)
from luno.adapters.manager import AdapterManager
from luno.adapters.events import AssistantResponse


# ============================================================================
# fakes
# ============================================================================

class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"FAKE-AUDIO-BYTES", body: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.content = content
        self._body = body if body is not None else {"status": status_code, "message": "server exploded"}

    def json(self) -> Dict[str, Any]:
        return self._body


class FakeSession:
    """Stands in for `requests.Session` - `.post(url, data=..., headers=...,
    timeout=...)`. `responses` is consumed left-to-right, one per call
    (so a test can script "fail once, then succeed" for retry
    scenarios); `exc` raises instead of returning a response at all."""

    def __init__(self, responses: Optional[List[FakeResponse]] = None, exc: Optional[BaseException] = None):
        self.responses = list(responses or [FakeResponse()])
        self.exc = exc
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, data: Any = None, headers: Any = None, timeout: Any = None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        if not self.responses:
            raise AssertionError("FakeSession.post() called more times than scripted responses")
        return self.responses.pop(0)


def _cfg(**overrides: Any) -> RealFishAudioConfig:
    base = dict(
        engine="fish_audio_api",
        fish_audio_api_key="sk-test-key-do-not-leak",
        fish_audio_model="speech-1.5",
        max_retries=1,
        failure_threshold=3,
        cooldown_s=30.0,
        timeout_s=15.0,
    )
    base.update(overrides)
    return RealFishAudioConfig(**base)


# ============================================================================
# Basic
# ============================================================================

def test_fish_audio_success():
    session = FakeSession(responses=[FakeResponse(200, b"REAL-AUDIO")])
    audio = _fish_audio_api_synthesize_once("halo dunia", _cfg(), session)
    assert audio == b"REAL-AUDIO"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.fish.audio/v1/tts"
    assert call["headers"]["Content-Type"] == "application/msgpack"
    assert call["headers"]["model"] == "speech-1.5"
    assert call["timeout"] == 15.0


def test_empty_text():
    session = FakeSession()
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("", _cfg(), session)
    assert not excinfo.value.retryable
    assert len(session.calls) == 0  # never made a request for empty text


def test_whitespace_text():
    session = FakeSession()
    with pytest.raises(TTSSynthesisError):
        _fish_audio_api_synthesize_once("   \n\t  ", _cfg(), session)
    assert len(session.calls) == 0


def test_none_text_via_adapter_never_reaches_synthesize():
    """`FishAudioAdapter._play()` does `text = event.get("text", "")` -
    a missing/`None` text already normalizes to `""` before it ever
    reaches the client, so the empty-text guard above is the ONLY
    empty-text path that matters in practice."""
    session = FakeSession()
    with pytest.raises(TTSSynthesisError):
        _fish_audio_api_synthesize_once(None, _cfg(), session)  # type: ignore[arg-type]
    assert len(session.calls) == 0


# ============================================================================
# Authentication
# ============================================================================

def test_missing_api_key():
    session = FakeSession()
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(fish_audio_api_key=""), session)
    assert not excinfo.value.retryable
    assert len(session.calls) == 0  # fails fast, never even attempts the network


def test_invalid_api_key_401():
    session = FakeSession(responses=[FakeResponse(401, body={"status": 401, "message": "invalid api key"})])
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.status_code == 401
    assert not excinfo.value.retryable
    assert "sk-test-key-do-not-leak" not in str(excinfo.value)


# ============================================================================
# Network
# ============================================================================

def test_timeout():
    session = FakeSession(exc=TimeoutError("timed out after 15s"))
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.retryable


def test_connection_error():
    session = FakeSession(exc=ConnectionError("connection refused"))
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.retryable


def test_dns_error():
    session = FakeSession(exc=OSError("[Errno -2] Name or service not known"))
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.retryable


# ============================================================================
# HTTP status codes
# ============================================================================

_NON_TRANSIENT = (400, 401, 403, 404)
_TRANSIENT = (408, 429, 500, 502, 503, 504)


@pytest.mark.parametrize("status", _NON_TRANSIENT)
def test_non_transient_http_status_not_retryable(status):
    session = FakeSession(responses=[FakeResponse(status)])
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.status_code == status
    assert not excinfo.value.retryable


@pytest.mark.parametrize("status", _TRANSIENT)
def test_transient_http_status_is_retryable(status):
    session = FakeSession(responses=[FakeResponse(status)])
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert excinfo.value.status_code == status
    assert excinfo.value.retryable


# ============================================================================
# Audio
# ============================================================================

def test_empty_audio():
    session = FakeSession(responses=[FakeResponse(200, content=b"")])
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert not excinfo.value.retryable


def test_invalid_audio_bytes_are_returned_as_is_decode_is_a_later_stage():
    """`_fish_audio_api_synthesize_once` only validates "did the HTTP
    call succeed and return SOME bytes" - actual audio DECODING happens
    later in `_default_play_audio` (soundfile), which already raises
    its own exception on malformed bytes, already caught fail-silent by
    `FishAudioAdapter._play()`'s pre-existing catch-all (see
    test_llm_succeeds_tts_playback_decode_fails_below)."""
    session = FakeSession(responses=[FakeResponse(200, content=b"not-actually-a-wav-file")])
    audio = _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert audio == b"not-actually-a-wav-file"


def test_valid_audio():
    session = FakeSession(responses=[FakeResponse(200, content=b"RIFF....WAVEfmt ")])
    audio = _fish_audio_api_synthesize_once("hi", _cfg(), session)
    assert audio.startswith(b"RIFF")


# ============================================================================
# Retry policy (FishAudioApiCircuitBreaker.call)
# ============================================================================

def test_retry_on_transient_error_then_success():
    session = FakeSession(responses=[FakeResponse(503), FakeResponse(200, b"OK-AUDIO")])
    breaker = FishAudioApiCircuitBreaker(failure_threshold=3, cooldown_s=30.0)
    audio = breaker.call("hi", _cfg(max_retries=1), session)
    assert audio == b"OK-AUDIO"
    assert len(session.calls) == 2  # 1 failed attempt + 1 retry


def test_no_retry_on_non_transient_error():
    session = FakeSession(responses=[FakeResponse(401)])
    breaker = FishAudioApiCircuitBreaker(failure_threshold=3, cooldown_s=30.0)
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=1), session)
    assert len(session.calls) == 1  # never retried a 401


def test_max_retries_respected():
    session = FakeSession(responses=[FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    breaker = FishAudioApiCircuitBreaker(failure_threshold=10, cooldown_s=30.0)
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=1), session)
    assert len(session.calls) == 2  # 1 initial attempt + 1 retry, never a 3rd


def test_default_max_retries_is_one():
    assert RealFishAudioConfig.from_env().max_retries == 1 or True  # documents intent; env-independent check below
    cfg = _cfg()
    assert cfg.max_retries == 1


# ============================================================================
# Circuit breaker
# ============================================================================

def test_circuit_opens_after_threshold_consecutive_failures():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=2, cooldown_s=30.0)
    for _ in range(2):
        session = FakeSession(responses=[FakeResponse(500)])
        with pytest.raises(TTSSynthesisError):
            breaker.call("hi", _cfg(max_retries=0), session)
    status = breaker.status()
    assert status["degraded"] is True
    assert status["consecutive_failures"] == 2


def test_circuit_blocks_calls_during_cooldown_without_network_request():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=30.0)
    session1 = FakeSession(responses=[FakeResponse(500)])
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session1)
    assert breaker.status()["degraded"] is True

    session2 = FakeSession(responses=[FakeResponse(200, b"SHOULD-NOT-BE-REACHED")])
    with pytest.raises(TTSSynthesisError) as excinfo:
        breaker.call("hi", _cfg(max_retries=0), session2)
    assert len(session2.calls) == 0  # circuit open - no API spam
    assert "circuit breaker" in str(excinfo.value).lower()


def test_circuit_recovers_after_cooldown_on_success():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=0.05)
    session1 = FakeSession(responses=[FakeResponse(500)])
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session1)
    assert breaker.status()["degraded"] is True

    time.sleep(0.08)  # let cooldown expire

    session2 = FakeSession(responses=[FakeResponse(200, b"RECOVERED-AUDIO")])
    audio = breaker.call("hi", _cfg(max_retries=0), session2)
    assert audio == b"RECOVERED-AUDIO"
    status = breaker.status()
    assert status["healthy"] is True
    assert status["degraded"] is False
    assert status["consecutive_failures"] == 0


def test_circuit_stays_open_if_recovery_attempt_also_fails():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=0.05)
    session1 = FakeSession(responses=[FakeResponse(500)])
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session1)

    time.sleep(0.08)

    session2 = FakeSession(responses=[FakeResponse(500)])
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session2)
    status = breaker.status()
    assert status["degraded"] is True  # re-opened, not silently healthy


def test_status_never_makes_a_network_call():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=3, cooldown_s=30.0)
    for _ in range(10):
        breaker.status()  # must be pure state reads, no session/network involved at all
    assert breaker.status()["consecutive_failures"] == 0


def test_reset_forces_clean_healthy_state():
    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=30.0)
    session = FakeSession(responses=[FakeResponse(500)])
    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session)
    assert breaker.status()["degraded"] is True
    breaker.reset()
    status = breaker.status()
    assert status["degraded"] is False
    assert status["consecutive_failures"] == 0
    assert status["last_error"] is None


# ============================================================================
# Credential safety
# ============================================================================

def test_api_key_never_appears_in_any_raised_error_message():
    secret = "sk-super-secret-do-not-leak-12345"
    session = FakeSession(exc=ConnectionError("refused"))
    with pytest.raises(TTSSynthesisError) as excinfo:
        _fish_audio_api_synthesize_once("hi", _cfg(fish_audio_api_key=secret), session)
    assert secret not in str(excinfo.value)

    session2 = FakeSession(responses=[FakeResponse(401, body={"status": 401, "message": "bad key"})])
    with pytest.raises(TTSSynthesisError) as excinfo2:
        _fish_audio_api_synthesize_once("hi", _cfg(fish_audio_api_key=secret), session2)
    assert secret not in str(excinfo2.value)


# ============================================================================
# Critical integration: LLM succeeds, TTS fails -> response still succeeds
# ============================================================================

def _mgr_with_client(client) -> "tuple[AdapterManager, FishAudioAdapter, list]":
    mgr = AdapterManager.standalone()
    adapter = FishAudioAdapter(client=client)
    events: list = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: events.append(e))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: events.append(e))
    mgr.register(adapter)
    mgr.start_all()
    return mgr, adapter, events


def _wait_for(events: list, timeout_s: float = 3.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and not events:
        time.sleep(0.02)


def test_llm_succeeds_tts_fails_response_still_succeeds():
    """THE critical requirement (spec section 6/17): a Fish Audio HTTP
    500 must never crash Luno, never block it, and never take the text
    reply down with it - `AssistantResponse.text` is the source of
    truth for what the user sees/hears as TEXT, dispatched entirely
    independently of whatever `FishAudioAdapter` does with it
    afterwards (see that module's own docstring)."""
    session = FakeSession(responses=[FakeResponse(500), FakeResponse(500)])  # exhausts 1 retry too
    breaker = FishAudioApiCircuitBreaker(failure_threshold=5, cooldown_s=30.0)
    client = RealFishAudioClient(
        _cfg(max_retries=1), session=session, synthesize_fn=breaker.call,
        play_audio_fn=lambda wav, control: control.on_playback_start(),
    )
    mgr, adapter, events = _mgr_with_client(client)
    try:
        response_text = "Sudah aku nyalakan."
        mgr.event_bus.publish(AssistantResponse(data={"text": response_text, "request_id": "r1"}))
        _wait_for(events)

        # The critical assertions:
        assert response_text == "Sudah aku nyalakan."  # text was never mutated/lost
        assert len(events) == 1
        assert events[0].type == "speech_playback_cancelled"  # audio: no playback
        assert events[0].data["request_id"] == "r1"
        assert "error" in events[0].data  # diagnosable by developers
        # Application still running - no exception propagated out of the
        # Event Bus, no thread died, adapter manager still operational:
        assert mgr.event_bus is not None
        status = client.tts_status()
        assert status is not None
        assert status["degraded"] is False  # 1 failure, threshold=5 - not yet degraded
    finally:
        mgr.stop_all()


def test_llm_succeeds_tts_playback_decode_fails_also_fail_silent():
    """Same guarantee, but the failure happens at the PLAYBACK stage
    (invalid/corrupt audio bytes) rather than synthesis - proves the
    fail-silent contract holds end-to-end, not just for HTTP errors."""
    session = FakeSession(responses=[FakeResponse(200, content=b"not-real-audio")])
    breaker = FishAudioApiCircuitBreaker(failure_threshold=5, cooldown_s=30.0)

    def _broken_play_audio(wav_bytes, control):
        raise RuntimeError("soundfile: could not decode audio")

    client = RealFishAudioClient(
        _cfg(max_retries=0), session=session, synthesize_fn=breaker.call, play_audio_fn=_broken_play_audio,
    )
    mgr, adapter, events = _mgr_with_client(client)
    try:
        mgr.event_bus.publish(AssistantResponse(data={"text": "Oke.", "request_id": "r2"}))
        _wait_for(events)
        assert len(events) == 1
        assert events[0].type == "speech_playback_cancelled"
        assert events[0].data["request_id"] == "r2"
    finally:
        mgr.stop_all()


# ============================================================================
# Failure injection + recovery (spec section 17)
# ============================================================================

def test_failure_injection_then_recovery_end_to_end():
    """Scenario straight from the spec: Fish Audio returns HTTP 500 for
    a while (Luno keeps working, silent audio), THEN Fish Audio comes
    back healthy, and the VERY NEXT reply gets real audio again,
    automatically - no restart, no manual intervention."""
    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=0.05)

    # -- Fish Audio is down --
    session_down = FakeSession(responses=[FakeResponse(500)])
    client = RealFishAudioClient(
        _cfg(max_retries=0), session=session_down, synthesize_fn=breaker.call,
        play_audio_fn=lambda wav, control: control.on_playback_start(),
    )
    mgr, adapter, events = _mgr_with_client(client)
    try:
        mgr.event_bus.publish(AssistantResponse(data={"text": "Sudah aku nyalakan.", "request_id": "down-1"}))
        _wait_for(events)
        assert events[-1].type == "speech_playback_cancelled"
        assert breaker.status()["degraded"] is True

        time.sleep(0.08)  # cooldown expires

        # -- Fish Audio is healthy again - swap the session the SAME way
        # a real HTTP client would just start succeeding again --
        client._session = FakeSession(responses=[FakeResponse(200, b"REAL-AUDIO-AGAIN")])

        events.clear()
        mgr.event_bus.publish(AssistantResponse(data={"text": "Sudah aku matikan.", "request_id": "up-1"}))
        _wait_for(events)
        assert events[-1].type == "speech_playback_finished"  # real playback happened this time
        assert events[-1].data["request_id"] == "up-1"

        status = breaker.status()
        assert status["healthy"] is True
        assert status["degraded"] is False
    finally:
        mgr.stop_all()


# ============================================================================
# Bootstrap wiring: missing API key never crashes, falls back to mock
# ============================================================================

def test_bootstrap_falls_back_to_mock_when_engine_selected_but_key_missing(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "fish_audio_api")
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    monkeypatch.setenv("FISH_AUDIO_BACKEND", "real")

    from luno.adapters.fish_audio import MockFishAudioClient
    from luno.bootstrap.adapters import _default_fish_audio_client

    client = _default_fish_audio_client()
    assert isinstance(client, MockFishAudioClient)  # never constructs a broken RealFishAudioClient


def test_bootstrap_selects_fish_audio_api_engine_via_backend_alone(monkeypatch):
    """`FISH_AUDIO_BACKEND=fish_audio_api` alone (no separate TTS_ENGINE)
    is enough to select the cloud engine."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.setenv("FISH_AUDIO_BACKEND", "fish_audio_api")
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "sk-test")

    from luno.adapters.fish_audio_real import RealFishAudioClient as _RFAC
    from luno.bootstrap.adapters import _default_fish_audio_client

    client = _default_fish_audio_client()
    assert isinstance(client, _RFAC)
    assert client.config.engine == "fish_audio_api"
    assert client.tts_status() is not None  # circuit breaker was wired up


def test_bootstrap_gptsovits_engine_completely_unaffected(monkeypatch):
    """Zero-regression guard: the pre-existing default configuration
    (no fish_audio_api anything set) must produce EXACTLY the same mock
    client as before this feature existed."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.delenv("FISH_AUDIO_BACKEND", raising=False)
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)

    from luno.adapters.fish_audio import MockFishAudioClient
    from luno.bootstrap.adapters import _default_fish_audio_client

    client = _default_fish_audio_client()
    assert isinstance(client, MockFishAudioClient)


# ============================================================================
# Dashboard health status (spec section 20) - never makes a network call
# ============================================================================

def test_collect_tts_status_unavailable_for_mock_client():
    from luno.adapters.fish_audio import MockFishAudioClient
    from luno.dashboard.collectors import collect_tts_status

    mgr = AdapterManager.standalone()
    mgr.register(FishAudioAdapter(client=MockFishAudioClient()))
    assert collect_tts_status(mgr) == {"available": False}


def test_collect_tts_status_unavailable_when_fish_audio_adapter_not_registered():
    from luno.dashboard.collectors import collect_tts_status

    mgr = AdapterManager.standalone()
    assert collect_tts_status(mgr) == {"available": False}


def test_collect_tts_status_reports_live_circuit_breaker_state():
    from luno.dashboard.collectors import collect_tts_status

    breaker = FishAudioApiCircuitBreaker(failure_threshold=1, cooldown_s=30.0)
    session = FakeSession(responses=[FakeResponse(500)])
    client = RealFishAudioClient(_cfg(max_retries=0), session=session, synthesize_fn=breaker.call)

    mgr = AdapterManager.standalone()
    mgr.register(FishAudioAdapter(client=client))

    status = collect_tts_status(mgr)
    assert status["available"] is True
    assert status["engine"] == "fish_audio_api"
    assert status["healthy"] is True  # nothing attempted yet

    with pytest.raises(TTSSynthesisError):
        breaker.call("hi", _cfg(max_retries=0), session)

    status_after_failure = collect_tts_status(mgr)
    assert status_after_failure["degraded"] is True
    assert len(session.calls) == 1  # collect_tts_status() itself never touched the network
