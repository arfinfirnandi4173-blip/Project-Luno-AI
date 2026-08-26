"""
test_tts_queue.py
===================

TTS Chunk Queue & Cancellation sprint - dedicated test suite for the
SPEECH QUEUE contract: `FishAudioAdapter._play()`'s sequential,
per-request worker behavior (ordering, isolation between requests,
correlation-id preservation, failure survival, queue cleanup).

Uses `MockFishAudioClient`/a small local scripted failure double - no
network, no real Fish Audio server, no real speaker. Chunks are built via
the REAL `luno.speech_chunk.build_speech_chunks()` (the actual wire
format `main_runtime_demo.py::_speak()` publishes), not hand-rolled
dicts, so these tests exercise the real contract end to end from "chunk
objects" through to "adapter playback".

Run:
    python3 -m pytest tests/test_tts_queue.py -q
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import SpeakRequest  # noqa: E402
from luno.adapters.fish_audio import FishAudioAdapter, FishAudioClient, MockFishAudioClient, PlaybackCancelled  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.speech_chunk import build_speech_chunks  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 3.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _mgr_with_fish_audio(client=None):
    mgr = AdapterManager.standalone()
    fa = FishAudioAdapter(client=client or MockFishAudioClient(playback_delay_s=0.03))
    mgr.register(fa)
    mgr.start_all()
    return mgr, fa


def _speak_request(texts: List[str], request_id: str, conversation_id: Optional[str] = "conv-1"):
    chunks = build_speech_chunks(texts, texts, request_id=request_id, conversation_id=conversation_id)
    return SpeakRequest(data={
        "text": " ".join(texts), "request_id": request_id, "conversation_id": conversation_id,
        "chunks": [c.to_dict() for c in chunks],
    })


class _FailNTimesClient(FishAudioClient):
    """Fails the FIRST `fail_count` calls (regardless of text), then
    succeeds forever after - used to prove the QUEUE WORKER itself
    survives a TTS failure and keeps accepting/playing later requests
    (worker-level resilience, distinct from `test_fish_audio_chunking.py`'s
    own per-CHUNK retry/skip tests)."""

    def __init__(self, fail_count: int, playback_delay_s: float = 0.02) -> None:
        self.fail_count = fail_count
        self.calls = 0
        self.played: List[str] = []
        self.playback_delay_s = playback_delay_s
        self._active: List[Dict[str, threading.Event]] = []
        self._lock = threading.Lock()

    def play(self, text: str, on_playback_start=None) -> None:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError(f"synthesis failure #{self.calls}")
        entry = {"cancel": threading.Event(), "pause": threading.Event()}
        with self._lock:
            self._active.append(entry)
        try:
            self.played.append(text)
            if on_playback_start is not None:
                on_playback_start()
            time.sleep(self.playback_delay_s)
        finally:
            with self._lock:
                if entry in self._active:
                    self._active.remove(entry)

    def stop(self) -> None:
        with self._lock:
            for e in self._active:
                e["cancel"].set()

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass


# ============================================================================
# 11. chunks play strictly in order
# ============================================================================

def test_11_chunks_play_strictly_in_order():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    texts = [f"Bagian {n}." for n in range(8)]
    mgr.event_bus.publish(_speak_request(texts, "order-req"))
    ok = _wait_until(lambda: finished == ["order-req"], timeout_s=3.0)
    mgr.stop_all()

    assert ok, f"played={client.played}"
    assert client.played == texts


# ============================================================================
# 12. multiple requests don't interleave
# ============================================================================

def test_12_multiple_sequential_requests_do_not_interleave():
    """Two SEPARATE, sequentially-published requests (the normal
    "one turn finishes, then the next turn's SpeakRequest is published"
    flow) must never interleave their chunks - request B's chunks only
    ever appear after request A's are fully done."""
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["A1.", "A2.", "A3."], "req-A"))
    ok1 = _wait_until(lambda: finished == ["req-A"], timeout_s=3.0)
    mgr.event_bus.publish(_speak_request(["B1.", "B2.", "B3."], "req-B"))
    ok2 = _wait_until(lambda: finished == ["req-A", "req-B"], timeout_s=3.0)
    mgr.stop_all()

    assert ok1 and ok2
    assert client.played == ["A1.", "A2.", "A3.", "B1.", "B2.", "B3."]


# ============================================================================
# 13. request_id correlation preserved
# ============================================================================

def test_13_request_id_correlation_preserved_through_lifecycle_events():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    started, finished = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Halo."], "correlate-me"))
    ok = _wait_until(lambda: finished == ["correlate-me"], timeout_s=2.0)
    mgr.stop_all()

    assert ok
    assert started == ["correlate-me"]
    assert finished == ["correlate-me"]


# ============================================================================
# 14. conversation_id preserved
# ============================================================================

def test_14_conversation_id_preserved_on_every_chunk_object():
    chunks = build_speech_chunks(["A.", "B."], ["A.", "B."], request_id="r1", conversation_id="my-conversation")
    assert all(c.conversation_id == "my-conversation" for c in chunks)
    # and it round-trips through .to_dict() -> the actual SpeakRequest wire format
    dicts = [c.to_dict() for c in chunks]
    assert all(d["conversation_id"] == "my-conversation" for d in dicts)


# ============================================================================
# 15. worker survives TTS failure
# ============================================================================

def test_15_worker_survives_tts_failure_and_plays_next_request():
    """A request whose chunk(s) fail synthesis entirely must not kill the
    adapter's own worker/executor - a LATER, unrelated request must still
    play normally afterward."""
    client = _FailNTimesClient(fail_count=1, playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled, finished = [], []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["will fail"], "fail-req"))
    ok1 = _wait_until(lambda: cancelled == ["fail-req"], timeout_s=2.0)

    mgr.event_bus.publish(_speak_request(["will succeed"], "ok-req"))
    ok2 = _wait_until(lambda: finished == ["ok-req"], timeout_s=2.0)
    mgr.stop_all()

    assert ok1, f"cancelled={cancelled}"
    assert ok2, f"finished={finished} played={client.played}"
    assert client.played == ["will succeed"]


# ============================================================================
# 16. queue cleanup after completion
# ============================================================================

def test_16_chunk_control_entry_removed_after_request_completes():
    client = MockFishAudioClient(playback_delay_s=0.02)
    mgr, fa = _mgr_with_fish_audio(client)
    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Halo."], "cleanup-req"))
    ok = _wait_until(lambda: finished == ["cleanup-req"], timeout_s=2.0)
    time.sleep(0.05)  # let the `finally` block in `_play()` actually run
    mgr.stop_all()

    assert ok
    with fa._chunk_control_lock:
        assert "cleanup-req" not in fa._chunk_control
    with fa._in_flight_lock:
        assert "cleanup-req" not in fa._in_flight_request_ids


def test_16b_chunk_control_entry_removed_after_cancellation_too():
    client = MockFishAudioClient(playback_delay_s=0.3)
    mgr, fa = _mgr_with_fish_audio(client)
    started, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["Panjang.", "Lebih panjang lagi."], "cancel-cleanup-req"))
    ok1 = _wait_until(lambda: started == ["cancel-cleanup-req"], timeout_s=2.0)
    from luno.adapters.events import StopPlayback
    mgr.event_bus.publish(StopPlayback(data={"request_id": "cancel-cleanup-req"}))
    ok2 = _wait_until(lambda: cancelled == ["cancel-cleanup-req"], timeout_s=2.0)
    time.sleep(0.05)
    mgr.stop_all()

    assert ok1 and ok2
    with fa._chunk_control_lock:
        assert "cancel-cleanup-req" not in fa._chunk_control


# ============================================================================
# Error handling (Phase 7, scenarios 31-33) - queue-worker-level view
# ============================================================================

def test_31_tts_exception_cleanup_leaves_adapter_usable():
    client = _FailNTimesClient(fail_count=10_000, playback_delay_s=0.01)  # every call fails, incl. retries
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled, finished = [], []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))

    mgr.event_bus.publish(_speak_request(["X.", "Y.", "Z."], "all-fail-req"))
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    mgr.stop_all()

    assert ok, f"cancelled={cancelled} finished={finished}"
    assert finished == []
    assert "error" in cancelled[0]
    with fa._chunk_control_lock:
        assert "all-fail-req" not in fa._chunk_control  # cleanup still ran


def test_32_playback_exception_cleanup_via_finally():
    """Same guarantee as test_31 but confirms cleanup happens through the
    `finally` block regardless of WHICH phase failed (this double fails
    unconditionally, standing in for either synthesis or playback failure
    - `FishAudioClient`'s interface doesn't distinguish the two phases at
    this level, `fish_audio_real.py`'s own two-phase split is internal to
    that concrete client)."""
    client = _FailNTimesClient(fail_count=1, playback_delay_s=0.01)
    mgr, fa = _mgr_with_fish_audio(client)
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    mgr.event_bus.publish(_speak_request(["oops"], "playback-fail-req"))
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=2.0)
    mgr.stop_all()

    assert ok
    with fa._in_flight_lock:
        assert "playback-fail-req" not in fa._in_flight_request_ids


def test_33_empty_response_cleanup_publishes_finished_not_error():
    """An empty/whitespace-only reply degrades to a single empty-string
    chunk (see `_normalize_chunk_entries()`) - `MockFishAudioClient.play("")`
    is a normal (near-instant) success, not a failure, so this must
    finish cleanly, not error."""
    client = MockFishAudioClient(playback_delay_s=0.01)
    mgr, fa = _mgr_with_fish_audio(client)
    finished, cancelled = [], []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    mgr.event_bus.publish(SpeakRequest(data={"text": "", "request_id": "empty-req", "conversation_id": "c1"}))
    ok = _wait_until(lambda: finished == ["empty-req"] or cancelled, timeout_s=2.0)
    mgr.stop_all()

    assert ok
    assert finished == ["empty-req"]
    assert cancelled == []
