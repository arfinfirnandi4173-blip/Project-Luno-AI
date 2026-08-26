"""
audio_bridge.py
=================

Lets the Chat panel play Luno's REAL synthesized voice (GPT-SoVITS/
F5-TTS, whatever `RealFishAudioClient` is configured with) in the
browser, without changing the real TTS pipeline's own behavior at all.

The problem: `FishAudioClient.play()` (see `fish_audio.py`) blocks
until playback finishes and returns nothing - the synthesized WAV bytes
live entirely inside `RealFishAudioClient.play()`'s own local scope
(`fish_audio_real.py`), handed straight to `sounddevice` and never
exposed to any caller, and `play()`'s own signature (fixed by the
`FishAudioClient` ABC - one of the subsystems this project must not
rewrite) has no `request_id` parameter to tag a captured clip with
directly.

The mechanism, in two parts:

  1. CAPTURE - `RealFishAudioClient.__init__` already accepts an
     injectable `play_audio_fn` (built specifically for testability -
     see that file's own docstring: "both phases are injectable").
     `wrap_play_audio_fn()` wraps whichever function `bootstrap/
     adapters.py` would otherwise have used (`_default_play_audio`,
     real `sounddevice` output) with one extra step: push the WAV bytes
     onto `AudioCaptureStore`'s pending FIFO FIRST, then call the
     original function exactly as before - server-side speaker playback
     is completely unchanged (same bytes, same function, same timing),
     the same "tee" pattern `logs_buffer.py` already uses for stdout.

  2. CORRELATION - the captured bytes have no `request_id` yet at
     capture time. `AudioRequestCorrelator` subscribes to the Event Bus
     for `speech_playback_started` (published, WITH the correct
     `request_id`, by `FishAudioAdapter` itself right as this exact
     `play()` call's `on_playback_start` callback fires - i.e.
     immediately AFTER step 1 captured the bytes, same call chain, same
     thread) and claims the oldest still-pending clip under that
     `request_id`. This is a plain, read-only Event Bus subscription -
     the same technique every other dashboard component uses - not a
     second copy of anything `FishAudioAdapter`/`BargeInModule` already
     track.

Ordering note: under the (rare) case of two truly concurrent `play()`
calls (`FishAudioAdapter`'s own two-worker executor - a paused reply
plus a Barge-In CONFIRM interjection, see that file's docstring), FIFO
claim order is a best-effort match, not a mathematically guaranteed
one; documented here rather than hidden.

Mock backend (`FISH_AUDIO_BACKEND=mock`, the default): there is no real
audio at all (`MockFishAudioClient` never touches `sounddevice`/HTTP),
so nothing is ever pushed onto the pending FIFO and `/api/chat/audio/...`
correctly reports "not available" rather than pretending a clip exists.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any, Callable, Deque, Optional

if TYPE_CHECKING:
    from luno.core.event_bus import EventBus
    from luno.core.events import Event

DEFAULT_MAX_CLIPS = 20
DEFAULT_WAIT_TIMEOUT_S = 25.0


class AudioCaptureStore:
    def __init__(self, max_clips: int = DEFAULT_MAX_CLIPS) -> None:
        self._lock = threading.Condition()
        self._pending: Deque[bytes] = deque()
        self._clips: "OrderedDict[str, bytes]" = OrderedDict()
        self._max_clips = max_clips

    def capture(self, wav_bytes: bytes) -> None:
        """Called from inside the wrapped `play_audio_fn` - stashes bytes
        with no `request_id` yet (see module docstring, part 1)."""
        with self._lock:
            self._pending.append(wav_bytes)
            self._lock.notify_all()

    def claim(self, request_id: Optional[str]) -> None:
        """Called by `AudioRequestCorrelator` on `speech_playback_started`
        - pops the oldest pending clip and files it under `request_id`
        (see module docstring, part 2). A no-op if nothing is pending
        (mock backend, or this playback produced no capturable audio)."""
        if not request_id:
            return
        with self._lock:
            if not self._pending:
                return
            wav_bytes = self._pending.popleft()
            self._clips[request_id] = wav_bytes
            self._clips.move_to_end(request_id)
            while len(self._clips) > self._max_clips:
                self._clips.popitem(last=False)
            self._lock.notify_all()

    def get(self, request_id: str) -> Optional[bytes]:
        with self._lock:
            return self._clips.get(request_id)

    def wait_for(self, request_id: str, timeout_s: float = DEFAULT_WAIT_TIMEOUT_S) -> Optional[bytes]:
        """Blocks (bounded by `timeout_s`) until a clip for `request_id`
        shows up, or returns whatever's already there immediately. Used
        by the `/api/chat/audio/<request_id>` handler so the browser's
        `fetch()` can simply await the response instead of polling -
        synthesis can legitimately take several seconds (see
        `RealFishAudioConfig.timeout_s`)."""
        deadline = time.time() + timeout_s
        with self._lock:
            while request_id not in self._clips:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._lock.wait(timeout=remaining)
            return self._clips.get(request_id)


def wrap_play_audio_fn(store: AudioCaptureStore, original_play_audio_fn: Callable[..., None]) -> Callable[..., None]:
    """Returns a drop-in replacement for `RealFishAudioClient`'s
    `play_audio_fn` constructor argument - see module docstring, part 1."""

    def _wrapped(wav_bytes: bytes, control: Any) -> None:
        try:
            store.capture(wav_bytes)
        except Exception:
            pass  # capturing for the browser must never be able to break real playback
        original_play_audio_fn(wav_bytes, control)

    return _wrapped


class AudioRequestCorrelator:
    """Subscribes to `speech_playback_started` and claims the oldest
    pending captured clip under that event's `request_id` - see module
    docstring, part 2. Constructed once per Runtime (same lifetime as
    `AudioCaptureStore` itself), unsubscribes on `stop()`."""

    def __init__(self, store: AudioCaptureStore, event_bus: "EventBus") -> None:
        self._store = store
        self._event_bus = event_bus
        self._sub_id = event_bus.subscribe("speech_playback_started", self._on_started, priority=-1000)

    def _on_started(self, event: "Event") -> None:
        try:
            self._store.claim(event.data.get("request_id"))
        except Exception:
            pass

    def stop(self) -> None:
        self._event_bus.unsubscribe(self._sub_id)
