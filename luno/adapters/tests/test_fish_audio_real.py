"""
test_fish_audio_real.py
==========================

Regression suite for the real GPT-SoVITS/F5-TTS adapter integration
bug fix - `RealFishAudioClient` (luno/adapters/fish_audio_real.py) and
`FishAudioAdapter`'s corrected `SpeechStarted` timing. No network, no
audio hardware: synthesis is exercised via a scripted fake
`requests.Session`-like double (mirrors `RequestsOpenRouterClient`'s own
established test technique exactly), and playback is exercised via an
injected `play_audio_fn` fake that behaves like real audio output
(blocks for a controllable duration, honors cancel/pause) without ever
touching `sounddevice`.

Covers the task's own numbered regression list:
    1. SpeakRequest -> SpeechStarted -> SpeechFinished
    2/3. Runtime status Talking=True / Conversation Speaking during
         playback - covered here at the request_id/event level; the
         full console-level proof lives in
         tests/test_real_fish_audio_console.py
    6. Cancellation publishes SpeechCancelled exactly once
    7. request_id identical throughout
    8. Consecutive replies do not overlap (console-level)
    9. Streaming replies still work (console-level)
    10. Stress test: 100 sequential SpeakRequests
    11. No thread leaks after repeated playback/cancel cycles
    12. No duplicate SpeechStarted/SpeechFinished

Run:
    python3 -m luno.adapters.tests.test_fish_audio_real
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

from luno.adapters.events import PausePlayback, ResumePlayback, SpeakRequest, StopPlayback  # noqa: E402
from luno.adapters.fish_audio import FishAudioAdapter, PlaybackCancelled  # noqa: E402
from luno.adapters.fish_audio_real import RealFishAudioClient, RealFishAudioConfig, TTSSynthesisError  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402

Result = Tuple[bool, str]


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# ============================================================================
# Fakes - synthesis (HTTP) and playback (audio hardware) boundaries only.
# Everything else exercised below is RealFishAudioClient's OWN real logic.
# ============================================================================

class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"FAKE-WAV-BYTES"):
        self.status_code = status_code
        self.content = content

    def json(self) -> Dict[str, Any]:
        return {"detail": "fake error"}


class FakeSession:
    """Stands in for `requests.Session` - `.post(url, json=..., timeout=...)`.
    `delay_s` simulates real synthesis latency; `fail`/`status_code` for
    error-path scenarios; `calls` recorded for assertions on payload shape."""

    def __init__(self, delay_s: float = 0.05, status_code: int = 200,
                 content: bytes = b"FAKE-WAV-BYTES", raise_exc: Optional[BaseException] = None):
        self.delay_s = delay_s
        self.status_code = status_code
        self.content = content
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: Any = None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        time.sleep(self.delay_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(status_code=self.status_code, content=self.content)


def make_fake_player(duration_s: float = 0.05):
    """Stands in for real `sounddevice`/`soundfile` playback - blocks for
    `duration_s`, calls `control.on_playback_start()` right when "playback"
    would begin (AFTER synthesis, matching the real implementation's
    contract exactly), honors cancel/pause without losing position."""

    def _play_audio(wav_bytes: bytes, control) -> None:
        control.on_playback_start()
        slept = 0.0
        step = 0.005
        while slept < duration_s:
            if control.cancel.is_set():
                raise PlaybackCancelled("playback cancelled")
            if control.pause.is_set():
                time.sleep(step)
                continue
            time.sleep(step)
            slept += step

    return _play_audio


def _mgr_with_real_fish_audio(client: RealFishAudioClient) -> Tuple[AdapterManager, FishAudioAdapter]:
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


# ============================================================================
# 1, 7, 12 - normal lifecycle, request_id preservation, no duplicates
# ============================================================================

def test_normal_playback_full_lifecycle_in_order() -> Result:
    session = FakeSession(delay_s=0.05)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="ref.wav", reference_text="hello"),
        session=session, play_audio_fn=make_fake_player(0.05),
    )
    mgr, fa = _mgr_with_real_fish_audio(client)
    events: List[Tuple[str, Optional[str]]] = []
    for t in ("speech_playback_started", "speech_playback_finished", "speech_playback_cancelled"):
        mgr.event_bus.subscribe(t, lambda e, t=t: events.append((t, e.get("request_id"))))

    mgr.event_bus.publish(SpeakRequest(data={"text": "hello world", "request_id": "abc123"}))
    ok = _wait_until(lambda: len(events) == 2, timeout_s=3.0)
    mgr.stop_all()
    client.close()

    correct_order = events == [("speech_playback_started", "abc123"), ("speech_playback_finished", "abc123")]
    started_count = sum(1 for t, _ in events if t == "speech_playback_started")
    finished_count = sum(1 for t, _ in events if t == "speech_playback_finished")
    ok = ok and correct_order and started_count == 1 and finished_count == 1
    return ok, f"events={events}"


def test_request_id_never_regenerated() -> Result:
    session = FakeSession(delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.02))
    mgr, fa = _mgr_with_real_fish_audio(client)
    seen_ids = set()
    mgr.event_bus.subscribe("speech_playback_started", lambda e: seen_ids.add(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: seen_ids.add(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "test", "request_id": "original-rid-999"}))
    ok = _wait_until(lambda: len(seen_ids) > 0 and all(rid == "original-rid-999" for rid in seen_ids), timeout_s=3.0)
    mgr.stop_all()
    client.close()
    return ok, f"seen_ids={seen_ids}"


# ============================================================================
# Root cause: SpeechStarted must fire AFTER synthesis, not before
# ============================================================================

def test_speech_started_fires_after_synthesis_completes_not_before() -> Result:
    synthesis_delay = 0.3
    session = FakeSession(delay_s=synthesis_delay)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.02))
    mgr, fa = _mgr_with_real_fish_audio(client)

    t0 = time.time()
    started_at: List[float] = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started_at.append(time.time()))
    finished = threading.Event()
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.set())

    mgr.event_bus.publish(SpeakRequest(data={"text": "slow synth", "request_id": "r1"}))
    ok = _wait_until(finished.is_set, timeout_s=3.0)
    mgr.stop_all()
    client.close()

    ok = ok and len(started_at) == 1 and (started_at[0] - t0) >= synthesis_delay * 0.8
    return ok, f"offset_from_publish_s={round(started_at[0]-t0, 3) if started_at else None} synthesis_delay={synthesis_delay}"


# ============================================================================
# 6 - cancellation, both phases
# ============================================================================

def test_cancel_during_playback_publishes_cancelled_exactly_once() -> Result:
    session = FakeSession(delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(1.0))
    mgr, fa = _mgr_with_real_fish_audio(client)
    cancelled: List[Any] = []
    started = threading.Event()
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.set())
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    finished: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "long", "request_id": "cancel-me"}))
    ok1 = _wait_until(started.is_set, timeout_s=3.0)
    mgr.event_bus.publish(StopPlayback(data={"request_id": "cancel-me"}))
    ok2 = _wait_until(lambda: len(cancelled) == 1, timeout_s=3.0)
    time.sleep(0.2)  # settle - must never ALSO see a finished for this turn
    mgr.stop_all()
    client.close()

    ok = ok1 and ok2 and cancelled == ["cancel-me"] and finished == []
    return ok, f"cancelled={cancelled} finished={finished}"


def test_cancel_during_synthesis_never_publishes_started() -> Result:
    session = FakeSession(delay_s=2.0)  # long enough that the test itself controls timing
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.05))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started: List[Any] = []
    cancelled: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(SpeakRequest(data={"text": "will be cancelled early", "request_id": "gap-cancel"}))
    time.sleep(0.2)  # well before the 2s synthesis "completes"
    mgr.event_bus.publish(StopPlayback(data={"request_id": "gap-cancel"}))
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=3.0)
    mgr.stop_all()
    client.close()

    ok = ok and started == [] and cancelled == ["gap-cancel"]
    return ok, f"started={started} cancelled={cancelled}"


def test_synthesis_http_error_publishes_error_without_started() -> Result:
    session = FakeSession(status_code=500)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.02))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started: List[Any] = []
    errors: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: errors.append(e.data))

    mgr.event_bus.publish(SpeakRequest(data={"text": "will fail", "request_id": "http-fail"}))
    ok = _wait_until(lambda: len(errors) == 1, timeout_s=3.0)
    mgr.stop_all()
    client.close()

    has_error_field = bool(errors) and "error" in errors[0]
    ok = ok and started == [] and has_error_field
    return ok, f"started={started} errors={errors}"


def test_synthesis_connection_error_publishes_error() -> Result:
    session = FakeSession(raise_exc=ConnectionError("connection refused"))
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.02))
    mgr, fa = _mgr_with_real_fish_audio(client)
    errors: List[Any] = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: errors.append(e.data))
    mgr.event_bus.publish(SpeakRequest(data={"text": "unreachable server", "request_id": "conn-fail"}))
    ok = _wait_until(lambda: len(errors) == 1, timeout_s=3.0)
    mgr.stop_all()
    client.close()
    return ok, f"errors={errors}"


# ============================================================================
# Pause / resume - genuine, position-preserving, per the mock's own pattern
# ============================================================================

def test_pause_then_resume_preserves_position_and_finishes() -> Result:
    session = FakeSession(delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.3))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started = threading.Event()
    paused_evt = threading.Event()
    resumed_evt = threading.Event()
    finished = threading.Event()
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.set())
    mgr.event_bus.subscribe("speech_playback_paused", lambda e: paused_evt.set())
    mgr.event_bus.subscribe("speech_playback_resumed", lambda e: resumed_evt.set())
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.set())

    mgr.event_bus.publish(SpeakRequest(data={"text": "pausable", "request_id": "p-1"}))
    ok1 = _wait_until(started.is_set, timeout_s=3.0)
    mgr.event_bus.publish(PausePlayback(data={"request_id": "p-1"}))
    ok2 = _wait_until(paused_evt.is_set, timeout_s=3.0)
    time.sleep(0.4)  # well past the 0.3s "duration" - must NOT finish while paused
    still_not_finished = not finished.is_set()
    mgr.event_bus.publish(ResumePlayback(data={"request_id": "p-1"}))
    ok3 = _wait_until(resumed_evt.is_set, timeout_s=3.0)
    ok4 = _wait_until(finished.is_set, timeout_s=3.0)
    mgr.stop_all()
    client.close()

    ok = ok1 and ok2 and still_not_finished and ok3 and ok4
    return ok, f"ok1={ok1} ok2={ok2} still_not_finished={still_not_finished} ok3={ok3} ok4={ok4}"


# ============================================================================
# 10 - stress: 100 sequential SpeakRequests
# ============================================================================

def test_stress_100_sequential_speak_requests() -> Result:
    session = FakeSession(delay_s=0.0)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.0))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started_ids: List[str] = []
    finished_ids: List[str] = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started_ids.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished_ids.append(e.get("request_id")))

    N = 100
    for i in range(N):
        rid = f"stress-{i}"
        mgr.event_bus.publish(SpeakRequest(data={"text": f"message {i}", "request_id": rid}))
        ok = _wait_until(lambda rid=rid: rid in finished_ids, timeout_s=3.0)
        if not ok:
            mgr.stop_all()
            client.close()
            return False, f"request {rid} never finished (i={i})"

    mgr.stop_all()
    client.close()
    ok = len(started_ids) == N and len(finished_ids) == N and set(started_ids) == set(finished_ids)
    no_dupes = len(started_ids) == len(set(started_ids)) and len(finished_ids) == len(set(finished_ids))
    return ok and no_dupes, f"started={len(started_ids)} finished={len(finished_ids)} unique_started={len(set(started_ids))}"


# ============================================================================
# 11 - no thread leaks after repeated playback/cancel cycles
# ============================================================================

def test_no_thread_leaks_after_repeated_playback_cancel_cycles() -> Result:
    baseline = threading.active_count()
    session = FakeSession(delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.05))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started = threading.Event()
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.set())
    cancelled_count = [0]
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled_count.__setitem__(0, cancelled_count[0] + 1))

    for i in range(15):
        started.clear()
        rid = f"leak-check-{i}"
        mgr.event_bus.publish(SpeakRequest(data={"text": "x", "request_id": rid}))
        _wait_until(started.is_set, timeout_s=2.0)
        mgr.event_bus.publish(StopPlayback(data={"request_id": rid}))
        _wait_until(lambda: cancelled_count[0] == i + 1, timeout_s=2.0)

    mgr.stop_all()
    client.close()
    time.sleep(0.3)  # let worker threads actually wind down
    after = threading.active_count()
    # generous margin - this isn't asserting zero-growth to the thread,
    # just that 15 play/cancel cycles didn't leave 15+ threads behind.
    ok = (after - baseline) <= 4
    return ok, f"baseline={baseline} after={after} delta={after - baseline}"


# ============================================================================
# 12 - no duplicate events under concurrency
# ============================================================================

def test_no_duplicate_events_under_concurrent_calls() -> Result:
    session = FakeSession(delay_s=0.02)
    client = RealFishAudioClient(session=session, play_audio_fn=make_fake_player(0.05))
    mgr, fa = _mgr_with_real_fish_audio(client)
    started: List[str] = []
    finished: List[str] = []
    lock = threading.Lock()
    mgr.event_bus.subscribe("speech_playback_started", lambda e: (lock.acquire(), started.append(e.get("request_id")), lock.release()))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: (lock.acquire(), finished.append(e.get("request_id")), lock.release()))

    for i in range(10):
        mgr.event_bus.publish(SpeakRequest(data={"text": f"concurrent {i}", "request_id": f"conc-{i}"}))
    ok = _wait_until(lambda: len(finished) == 10, timeout_s=5.0)
    mgr.stop_all()
    client.close()

    no_dupes = len(started) == len(set(started)) and len(finished) == len(set(finished))
    return ok and no_dupes, f"started={len(started)} unique={len(set(started))} finished={len(finished)}"


# ============================================================================
# Config / payload shape sanity
# ============================================================================

def test_gptsovits_payload_shape_matches_legacy_integration() -> Result:
    session = FakeSession(delay_s=0.0)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(engine="gptsovits", reference_audio="ref.wav", reference_text="hi", gptsovits_host="http://host"),
        session=session, play_audio_fn=make_fake_player(0.0),
    )
    client.play("hello there")
    client.close()
    ok = len(session.calls) == 1
    call = session.calls[0] if session.calls else {}
    body = call.get("json", {})
    ok = ok and call.get("url") == "http://host/tts"
    ok = ok and body.get("text") == "hello there" and body.get("prompt_text") == "hi" and body.get("ref_audio_path") == "ref.wav"
    return ok, f"call={call}"


def test_f5tts_payload_shape_matches_legacy_integration() -> Result:
    session = FakeSession(delay_s=0.0)
    client = RealFishAudioClient(
        config=RealFishAudioConfig(engine="f5tts", reference_audio="ref.wav", reference_text="hi", f5tts_host="http://host5"),
        session=session, play_audio_fn=make_fake_player(0.0),
    )
    client.play("f5 says hi")
    client.close()
    ok = len(session.calls) == 1
    call = session.calls[0] if session.calls else {}
    body = call.get("json", {})
    ok = ok and call.get("url") == "http://host5/tts"
    ok = ok and body.get("gen_text") == "f5 says hi" and body.get("ref_text") == "hi" and body.get("ref_audio_path") == "ref.wav"
    return ok, f"call={call}"


def test_config_from_env() -> Result:
    old = {k: os.environ.get(k) for k in ("TTS_ENGINE", "GPTSOVITS_HOST", "F5TTS_HOST", "REFERENCE_AUDIO", "REFERENCE_TEXT")}
    try:
        os.environ["TTS_ENGINE"] = "f5tts"
        os.environ["GPTSOVITS_HOST"] = "http://gpt-host"
        os.environ["F5TTS_HOST"] = "http://f5-host"
        os.environ["REFERENCE_AUDIO"] = "/path/ref.wav"
        os.environ["REFERENCE_TEXT"] = "reference transcript"
        cfg = RealFishAudioConfig.from_env()
        ok = (cfg.engine == "f5tts" and cfg.gptsovits_host == "http://gpt-host" and cfg.f5tts_host == "http://f5-host"
              and cfg.reference_audio == "/path/ref.wav" and cfg.reference_text == "reference transcript"
              and cfg.timeout_s == 120.0)
        return ok, f"cfg={cfg}"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ============================================================================
# Runner
# ============================================================================

SCENARIOS = [
    test_normal_playback_full_lifecycle_in_order,
    test_request_id_never_regenerated,
    test_speech_started_fires_after_synthesis_completes_not_before,
    test_cancel_during_playback_publishes_cancelled_exactly_once,
    test_cancel_during_synthesis_never_publishes_started,
    test_synthesis_http_error_publishes_error_without_started,
    test_synthesis_connection_error_publishes_error,
    test_pause_then_resume_preserves_position_and_finishes,
    test_stress_100_sequential_speak_requests,
    test_no_thread_leaks_after_repeated_playback_cancel_cycles,
    test_no_duplicate_events_under_concurrent_calls,
    test_gptsovits_payload_shape_matches_legacy_integration,
    test_f5tts_payload_shape_matches_legacy_integration,
    test_config_from_env,
]


def main() -> int:
    print("\n=== Real Fish Audio (GPT-SoVITS/F5-TTS) Adapter Suite ===")
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
    if passed != total:
        print("Failing scenarios:")
        for name, ok in results:
            if not ok:
                print(f"  - {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
