"""
fish_audio.py
=============

`FishAudioAdapter` - receives `AssistantResponse`, plays it through an
injected `FishAudioClient` interface, and publishes playback
lifecycle events. No Fish Audio implementation here - only the
interface and a mock.

    AssistantResponse / SpeakRequest -> client.play(text, on_playback_start=...) -> SpeechPlaybackFinished
                                                          |
                                        stop_playback / PausePlayback / ResumePlayback
                                        (client.stop() raises PlaybackCancelled from
                                         inside the in-flight play() call)
                                                          v
                                                 SpeechPlaybackCancelled

Sprint 3 (barge-in) addition: `play()` blocks for the whole spoken
duration, so it runs on its OWN dedicated `_playback_executor` - never
on `BaseAdapter`'s single shared worker - for exactly the reason
`OpenRouterAdapter` already needed a separate executor for streaming
requests (see that file's docstring): a control event like
`PausePlayback`/`StopPlayback` arriving while `play()` is still running
must be handled immediately, not queued behind it. `handle_event()`
itself stays fast for every event type - it only ever submits the
actual blocking work and returns.

Real-TTS-adapter bug fix: `SpeechPlaybackStarted` used to be published by
`_play()` BEFORE calling `client.play()` at all - harmless for
`MockFishAudioClient` (there's no separate synthesis phase, "play" IS
the whole simulated duration), but wrong for a real backend
(`fish_audio_real.py`'s `RealFishAudioClient`) where synthesis is a
real, sometimes multi-second HTTP round trip that happens BEFORE any
audio actually starts playing. `FishAudioClient.play()` now takes an
`on_playback_start` callback that the CLIENT invokes itself, exactly
when real audio output is about to begin (after synthesis, not
before) - `_play()` passes a callback that publishes
`SpeechPlaybackStarted` at that moment instead of publishing it
upfront. This is what lets Wake Session/Barge-In observe the correct
`Speaking`/`Talking=True` state and the correct moment interrupts
become meaningful, regardless of which client is plugged in.

Second real-TTS-adapter bug fix: `BargeInModule._do_free_interrupt()`
only ever publishes `stop_playback` `if self.speaking` - i.e. once
`SpeechPlaybackStarted` has already fired. A FREE-mode interrupt that
lands while a reply is dispatched to Fish Audio but still synthesizing
(nothing audible yet) falls into a gap: `BargeInModule` correctly still
publishes `cancel_llm_request` -> `llm_cancelled` for that request_id
(see that module's own comment - originally meant only to stop
`BehaviorTreeModule._speak()` from ever publishing the `SpeakRequest`
in the first place), but if the `SpeakRequest` already reached Fish
Audio by that point, nothing ever told Fish Audio itself to stop. With
`MockFishAudioClient` this was invisible (near-zero synthesis time -
the gap barely exists); with a real, multi-second HTTP round trip it is
real. Rather than teach `BargeInModule` a second, request_id-aware
"stop Fish Audio too" code path (that package is explicitly
out-of-scope for this fix), `FishAudioAdapter` now ALSO listens for
`llm_cancelled` itself (see `DEFAULT_ADAPTER_EVENT_MAPPING` in
`models.py`) and, if the given request_id is one it currently has
in-flight (tracked in `_in_flight_request_ids`, added/removed around
`_play()`), calls `self.client.stop()` - exactly what `StopPlayback`
already does. Scoped by request_id so an unrelated, already-finished
request_id's late `llm_cancelled` can never cancel a DIFFERENT, still
legitimately-in-flight reply.

TTS Chunking/Streaming sprint - `_play()` now plays `event.get("chunks")`
(a `List[str]`, see `SpeakRequest`'s own updated docstring in events.py)
SEQUENTIALLY - one `client.play()` call per chunk, never overlapping -
instead of always treating `event.get("text")` as one block. When
`chunks` is absent (every caller that predates this sprint), a single
one-item list is derived from `text` - the loop then runs exactly once
and behaves BYTE-IDENTICALLY to the pre-chunking code (same
`SpeechPlaybackStarted`-on-first-audio / `SpeechPlaybackFinished`-once /
`SpeechPlaybackCancelled`-once contract `test_fish_audio_barge_in.py`
and `test_fish_audio_real.py` already pin down).

Closing the "gap between chunks" race: `client.stop()`/`client.pause()`/
`client.resume()` (see `FishAudioClient` above) only ever affect a call
CURRENTLY inside `play()` - there is a real window, between one chunk's
`play()` returning and the next chunk's `play()` starting, where nothing
is "in flight" for the client to act on. A `StopPlayback`/`PausePlayback`
that lands in exactly that window would previously have been silently
lost - `stop()`/`pause()` would find `self._active` empty and do
nothing, and the loop would go on to blindly start the next chunk
regardless. `FishAudioAdapter` now keeps its OWN per-utterance control
dict (`self._chunk_control`, keyed by `request_id`) mirroring the exact
pattern `MockFishAudioClient`/`RealFishAudioClient` already use for
their own per-call `_active` entries - `StopPlayback`/`PausePlayback`/
`ResumePlayback` set/clear a `stop`/`pause` `threading.Event` on EVERY
utterance currently in `self._chunk_control` (same "no request_id
targeting, act on everything currently audible/pending" semantics the
docstrings above already establish for `client.stop()`/`pause()`/
`resume()`), and the chunk loop checks its OWN entry between every two
chunks - before starting chunk N+1, not just while chunk N is playing.
Each entry is per-`_play()`-call-scoped (created at the top, discarded
in the `finally` block) exactly like `MockFishAudioClient._active`'s own
entries - a brand-new turn's entry is never affected by a previous,
already-finished turn's now-discarded one, which is what prevents "old
TTS keeps talking after a new turn already started" (a race this
sprint's own Phase 6 requirement explicitly calls out).

TTS Chunk Queue & Cancellation sprint - formalizes (does not replace the
mechanism of) two things the prior sprint already built:
  1. `self._chunk_control`'s values are now `luno.speech_chunk.
     SpeechCancellationToken` objects instead of a bare
     `{"stop": Event, "pause": Event}` dict - same two `threading.Event`s
     underneath, same per-request_id-scoped lifecycle, just a small,
     named, independently-testable class (see that module's own
     docstring) instead of an ad-hoc dict shape.
  2. `event.get("chunks")` may now be EITHER a `List[str]` (the prior
     sprint's original wire format - still fully supported, unchanged
     behavior) OR a `List[dict]` (`SpeechChunk.to_dict()` - this sprint's
     richer, correlation-aware format: `chunk_id`/`request_id`/
     `conversation_id`/`sequence`/`total`/`raw_text`/`text`/`is_final`).
     `_normalize_chunk_entries()` below accepts both, so every
     pre-existing test/caller that builds a plain string list keeps
     working exactly as before - this is purely additive.
"""

from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import BaseAdapter
from .events import (
    AssistantResponse,
    LLMCancelled,
    PausePlayback,
    ResumePlayback,
    SpeakRequest,
    SpeakStreamChunk,
    SpeechChunkPlaybackFinished,
    SpeechPlaybackCancelled,
    SpeechPlaybackFinished,
    SpeechPlaybackPaused,
    SpeechPlaybackResumed,
    SpeechPlaybackStarted,
    StopPlayback,
)
from .utils import log
from ..speech_chunk import SpeechCancellationToken


class PlaybackCancelled(Exception):
    """Raised by `FishAudioClient.play()` implementations (including the
    mock) when `stop()` interrupts an in-progress playback."""


class FishAudioClient(ABC):
    @abstractmethod
    def play(self, text: str, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        """Blocks until playback finishes, or raises `PlaybackCancelled`
        if `stop()` was called during playback, or raises any other
        exception for a genuine playback failure. Implementations should
        keep making progress (or block) while paused - see `pause()`.

        `on_playback_start`, if given, MUST be called exactly once,
        immediately before real audio output actually begins - NOT
        before synthesis/generation, NOT before an HTTP request starts.
        If `play()` raises before ever calling it (synthesis failed, or
        `stop()` cancelled the call before playback started), the
        caller correctly never sees a "started" signal for this call at
        all - matching the spec's `SpeakRequest -> SpeechError` path
        (no `SpeechStarted` in between). `MockFishAudioClient` below
        calls it right away since it has no separate synthesis phase;
        `fish_audio_real.RealFishAudioClient` calls it only after
        synthesis completes and audio output is about to start."""

    @abstractmethod
    def stop(self) -> None:
        """Cancels whatever `play()` call is currently in flight, if
        any. A no-op if nothing is playing."""

    def pause(self) -> None:
        """Pause the in-flight `play()` call without losing position -
        `resume()` continues from where it left off. Default: no-op, so
        a minimal `FishAudioClient` that doesn't support true pause
        still satisfies the interface (barge-in falls back to `stop()`
        behavior for such a client)."""

    def resume(self) -> None:
        """Continue a `pause()`-d `play()` call. Default: no-op."""

    # -- TTS Chunk Pipelining sprint - OPTIONAL synthesis/playback split ---
    #
    # Closes the audible-gap bug the prior, read-only Phase 0 audit found
    # (`docs/change_impact/tts_chunk_gap_audit.md`): `play()` bundles
    # synthesis and playback into ONE blocking call, so
    # `FishAudioAdapter._play_stream()` can never start chunk N+1's
    # synthesis until chunk N's `play()` call has fully returned (audible
    # gap == chunk N+1's own synthesis latency, measured ~1.3s per
    # boundary). These three methods are ADDITIVE and OPTIONAL - every
    # existing `FishAudioClient` (including `MockFishAudioClient`, which
    # has no real separate synthesis phase to overlap in the first place)
    # keeps working byte-identically via the unchanged `play()` path.
    # `FishAudioAdapter` only ever takes the new, pipelined code path for
    # a client whose `supports_split_synthesis()` returns `True`.
    def supports_split_synthesis(self) -> bool:
        """True if this client can synthesize text into a standalone
        audio object separately from playing it (see `synthesize()`/
        `play_audio()` below), letting `FishAudioAdapter` prefetch the
        NEXT chunk's audio while the CURRENT chunk is still playing.
        Default `False` - opt-in only."""
        return False

    def synthesize(self, text: str) -> Any:
        """Only ever called when `supports_split_synthesis()` is `True`.
        Returns an opaque, client-defined "audio" object that a LATER
        `play_audio()` call (on this SAME client instance) can play.
        Must raise for a genuine synthesis failure, exactly like `play()`
        would. Not required to be instantly cancellable - a caller that
        stops waiting on this call simply discards whatever it eventually
        produces (see `FishAudioAdapter`'s own "abandon, never force-kill"
        handling, the same discipline `RealFishAudioClient.play()`
        already uses for its own synthesis wait)."""
        raise NotImplementedError

    def play_audio(self, audio: Any, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        """Only ever called when `supports_split_synthesis()` is `True`,
        with an `audio` object this SAME client's own `synthesize()`
        produced. Same blocking/cancellable/`on_playback_start` contract
        as `play()` itself - `stop()`/`pause()`/`resume()` must affect an
        in-flight `play_audio()` call exactly as they already affect an
        in-flight `play()` call."""
        raise NotImplementedError


class MockFishAudioClient(FishAudioClient):
    """Simulates playback with a short sleep, interruptible by `stop()`
    and genuinely pausable/resumable via `pause()`/`resume()` (the sleep
    loop stops advancing its elapsed-time counter while paused, so
    resuming continues the same remaining duration rather than
    restarting). `fail=True` simulates a genuine playback error instead.

    Each `play()` call gets its OWN cancel/pause `Event` pair rather than
    sharing one at the instance level. This matters because `_playback_executor`
    (see `FishAudioAdapter`) runs with `max_workers=2` specifically so a
    SECOND utterance - Sprint 3's CONFIRM prompt ("Do you want to cancel
    the operation?"), spoken while the FIRST reply is merely paused, not
    stopped - can play concurrently on the other worker. If both calls
    shared one `_cancel_event`/`_pause_event` pair, the second call's
    `play()` clearing them at the top would silently un-pause/un-cancel
    whatever the FIRST call was doing - exactly the kind of corruption a
    two-worker executor is supposed to avoid. `stop()`/`pause()`/`resume()`
    still have no `request_id` to target (matching `FishAudioClient`'s own
    interface - a real device has one output channel too), so they simply
    act on every call CURRENTLY in flight, which is the right behavior for
    "the user said stop/pause/resume" - it should affect everything
    audibly happening right now, including an in-progress interjection."""

    def __init__(self, playback_delay_s: float = 0.05, fail: bool = False, synthesis_delay_s: float = 0.0) -> None:
        self.playback_delay_s = playback_delay_s
        self.fail = fail
        #: Simulates a real backend's synthesis/HTTP round trip - purely
        #: for testing that `on_playback_start` fires AFTER this delay,
        #: not before it (the exact timing bug this whole mechanism
        #: exists to prevent). Zero by default, matching the mock's
        #: original "no separate synthesis phase" behavior.
        self.synthesis_delay_s = synthesis_delay_s
        self.played: list = []
        self._lock = threading.Lock()
        self._active: List[Dict[str, threading.Event]] = []

    def play(self, text: str, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        entry = {"cancel": threading.Event(), "pause": threading.Event()}
        with self._lock:
            self._active.append(entry)
        try:
            if self.synthesis_delay_s > 0:
                slept = 0.0
                step = 0.01
                while slept < self.synthesis_delay_s:
                    if entry["cancel"].is_set():
                        raise PlaybackCancelled("playback cancelled during synthesis")
                    time.sleep(min(step, self.synthesis_delay_s - slept))
                    slept += step
            self.played.append(text)
            if self.fail:
                raise RuntimeError("mock Fish Audio playback failure")
            if on_playback_start is not None:
                on_playback_start()
            slept = 0.0
            step = 0.01
            while slept < self.playback_delay_s:
                if entry["cancel"].is_set():
                    raise PlaybackCancelled("playback cancelled")
                if entry["pause"].is_set():
                    time.sleep(step)
                    continue
                time.sleep(min(step, self.playback_delay_s - slept))
                slept += step
        finally:
            with self._lock:
                if entry in self._active:
                    self._active.remove(entry)

    def stop(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["cancel"].set()
                entry["pause"].clear()  # never leave a stopped call stuck "paused"

    def pause(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["pause"].set()

    def resume(self) -> None:
        with self._lock:
            for entry in self._active:
                entry["pause"].clear()


class FishAudioAdapter(BaseAdapter):
    name = "fish_audio"

    def __init__(self, client: Optional[FishAudioClient] = None) -> None:
        super().__init__()
        self.client = client or MockFishAudioClient()
        self._playback_executor: Optional[ThreadPoolExecutor] = None
        #: request_ids currently dispatched to `self.client` (added right
        #: before `client.play()` is called, removed once `_play()`
        #: returns, success or not) - lets the `llm_cancelled` handler
        #: below tell "this specific turn is still with Fish Audio right
        #: now" apart from "some unrelated, already-finished turn was
        #: cancelled" without needing `FishAudioClient.stop()` itself to
        #: become request_id-aware (a bigger interface change than this
        #: fix needs).
        self._in_flight_request_ids: set = set()
        self._in_flight_lock = threading.Lock()
        #: TTS Chunking/Streaming sprint - one entry per currently-playing
        #: (or between-chunks) multi-chunk utterance, keyed by request_id.
        #: See module docstring's "closing the gap between chunks" section.
        #: TTS Chunk Queue & Cancellation sprint - values are now
        #: `SpeechCancellationToken` objects (formalized, same mechanism -
        #: see that class's own docstring) - created at the top of
        #: `_play()`, discarded in its `finally` block, exactly mirroring
        #: `MockFishAudioClient._active`'s own per-call-scoped entries.
        self._chunk_control: Dict[str, SpeechCancellationToken] = {}
        self._chunk_control_lock = threading.Lock()
        #: Bounded retry count for a single chunk's synthesis/playback
        #: failure (Phase 7 - "retry terbatas, skip kalau retry gagal").
        #: Never applies to `PlaybackCancelled` (an intentional stop/
        #: barge-in, not a failure) - only to genuine exceptions.
        self._chunk_retry_limit = 1
        #: LLM Streaming -> Real-Time Speech Pipeline sprint - one live
        #: `queue.Queue` per in-progress STREAMING utterance (fed by
        #: `SpeakStreamChunk` events, consumed by `_play_stream()`).
        #: Bounded generously as a defense-in-depth safety net only - the
        #: real backpressure limit lives upstream, in
        #: `luno.incremental_speech.StreamingSpeechCoordinator` (Phase 10),
        #: which never lets more than its own configured
        #: `max_pending_chunks` be in flight to begin with. A `put` that
        #: still somehow finds this queue full (a caller bypassing that
        #: coordinator) is logged and the chunk is dropped rather than
        #: blocking `handle_event()` (which must stay fast for every
        #: event type - see that method's own docstring).
        self._stream_queues: Dict[str, "queue.Queue"] = {}
        self._stream_queue_lock = threading.Lock()
        self._STREAM_QUEUE_MAXSIZE = 32
        #: How often `_play_stream()`'s wait-for-next-chunk loop wakes up
        #: to re-check cancellation/pause even when nothing has arrived
        #: yet - bounds cancellation latency for a stream that is
        #: currently waiting on the LLM to produce more text.
        self._STREAM_POLL_INTERVAL_S = 0.05
        #: Safety net only (should never trigger in the normal, fully-
        #: wired path - `StreamingSpeechCoordinator` always eventually
        #: sends an `is_final` chunk via `llm_finished`/`llm_error`/
        #: `llm_cancelled`) - if truly nothing arrives for this long, the
        #: worker gives up rather than polling forever.
        self._STREAM_IDLE_TIMEOUT_S = 30.0
        #: TTS Chunk Pipelining sprint - a SEPARATE, small, bounded
        #: executor dedicated to ONE-SLOT prefetch synthesis jobs
        #: (`_play_stream_pipelined()` below). Deliberately NOT
        #: `_playback_executor` (that pool's 2nd worker is reserved for a
        #: genuinely CONCURRENT utterance - e.g. a Barge-In CONFIRM
        #: interjection spoken while a paused reply waits - see that
        #: pool's own docstring; using it for same-turn prefetch would
        #: starve that unrelated, pre-existing concurrency case). At most
        #: ONE prefetch job is ever submitted per active streaming turn
        #: (see `_play_stream_pipelined()`'s own "one-slot" invariant),
        #: so `max_workers=2` here (mirroring `_playback_executor`'s own
        #: sizing) comfortably covers this codebase's normal one-turn-at-
        #: a-time runtime shape (ARCHITECTURE_GUARD.md §2) with headroom
        #: to spare, never growing unbounded.
        self._prefetch_executor: Optional[ThreadPoolExecutor] = None

    def _do_start(self) -> None:
        self._playback_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="luno-fishaudio-playback")
        self._prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="luno-fishaudio-prefetch")

    def _do_stop(self) -> None:
        pool, self._playback_executor = self._playback_executor, None
        prefetch_pool, self._prefetch_executor = self._prefetch_executor, None
        if pool is not None:
            try:
                self.client.stop()
            except Exception:
                pass
            pool.shutdown(wait=False)
        if prefetch_pool is not None:
            # Same "abandon, never force-kill, never block shutdown"
            # discipline `RealFishAudioClient.play()` already uses for
            # its own synthesis wait (fish_audio_real.py) - any prefetch
            # job still running when the adapter stops is left to finish
            # on its own (bounded by the client's own synthesis timeout)
            # and its result is simply never collected.
            prefetch_pool.shutdown(wait=False)

    def handle_event(self, event: Any) -> None:
        """Deliberately fast for every branch - the actual blocking
        `play()` call is submitted to `_playback_executor` and this
        method returns immediately, so a barge-in control event queued
        right behind an `AssistantResponse` on this adapter's own
        (single, shared) worker is never stuck waiting for playback to
        finish first."""
        if event.type in (AssistantResponse.EVENT_TYPE, SpeakRequest.EVENT_TYPE):
            pool = self._playback_executor
            if pool is None:
                log(f"'{self.name}' dropped '{event.type}' - not started", self.name)
                return
            # TTS Chunk Queue & Cancellation sprint - the token is created
            # and registered HERE, synchronously, before `pool.submit()`
            # even returns - NOT inside `_play()` itself. This closes a
            # real race: `pool.submit()` only QUEUES the call, it does not
            # run `_play()` immediately - if a `StopPlayback` published
            # right after this `SpeakRequest` were to reach
            # `_apply_to_all_tokens()` before the worker thread actually
            # started `_play()` and registered its OWN token, that cancel
            # would find nothing to act on and be silently lost ("cancel
            # BEFORE synthesis has even begun" - Phase 4's own explicit
            # requirement). Registering the token before submission means
            # it already exists for `_apply_to_all_tokens()` to find,
            # regardless of how fast the cancel arrives afterward; `_play()`
            # reuses this SAME token (never creates its own) via
            # `_chunk_control`.
            request_id = event.get("request_id") or event.event_id
            token = SpeechCancellationToken(request_id)
            with self._chunk_control_lock:
                self._chunk_control[request_id] = token
            pool.submit(self._play, event, token)
            return
        if event.type == SpeakStreamChunk.EVENT_TYPE:
            self._handle_stream_chunk(event)
            return
        if event.type == PausePlayback.EVENT_TYPE:
            self.client.pause()
            self._apply_to_all_tokens(lambda tok: tok.pause())
            self.publish(SpeechPlaybackPaused(data={"request_id": event.get("request_id")}))
            return
        if event.type == ResumePlayback.EVENT_TYPE:
            self.client.resume()
            self._apply_to_all_tokens(lambda tok: tok.resume())
            self.publish(SpeechPlaybackResumed(data={"request_id": event.get("request_id")}))
            return
        if event.type == StopPlayback.EVENT_TYPE:
            # No output event published here on purpose - the in-flight
            # `_play()` call (if any) will notice `PlaybackCancelled`
            # itself and publish `SpeechPlaybackCancelled` with the
            # correct request_id already attached. `_apply_to_all_tokens`
            # additionally closes the "gap between chunks" race (see
            # module docstring) for any multi-chunk utterance currently
            # between two chunks, where `client.stop()` alone has nothing
            # in-flight to act on.
            self.client.stop()
            self._apply_to_all_tokens(lambda tok: tok.cancel())
            return
        if event.type == LLMCancelled.EVENT_TYPE:
            # Real-TTS-adapter bug fix (see module docstring): a FREE-mode
            # interrupt that arrived before `BargeInModule.speaking` ever
            # became True never publishes `StopPlayback` - only
            # `cancel_llm_request` -> `llm_cancelled`. If the cancelled
            # request_id is one WE currently have in flight (dispatched to
            # `self.client` but not yet finished), treat it exactly like a
            # `StopPlayback` for that call. Scoped by request_id so a
            # stale/unrelated cancellation can never cancel a different,
            # still-legitimate reply.
            rid = event.get("request_id")
            with self._in_flight_lock:
                is_ours = rid is not None and rid in self._in_flight_request_ids
            if is_ours:
                log(f"llm_cancelled for in-flight request_id={rid} - stopping Fish Audio playback/synthesis too", self.name)
                self.client.stop()
                with self._chunk_control_lock:
                    token = self._chunk_control.get(rid)
                if token is not None:
                    token.cancel()  # idempotent - safe even if already cancelled/finished
            return

    def _apply_to_all_tokens(self, action: Callable[[SpeechCancellationToken], None]) -> None:
        """Applies `action` (cancel/pause/resume) to EVERY currently-active
        multi-chunk utterance's `SpeechCancellationToken` - mirrors
        `client.stop()`/`pause()`/`resume()`'s own "no request_id
        targeting, acts on everything currently in flight" semantics (see
        class/module docstrings) so a global `StopPlayback`/
        `PausePlayback`/`ResumePlayback` closes the between-chunks gap the
        same way it already affects whatever `client.play()` call is
        actively running. Idempotent per-token (see
        `SpeechCancellationToken`'s own docstring), so calling this
        repeatedly, or on tokens that are already in the target state, is
        always safe."""
        with self._chunk_control_lock:
            tokens = list(self._chunk_control.values())
        for token in tokens:
            action(token)

    @staticmethod
    def _normalize_chunk_entries(
        raw_chunks: List[Any], text: str, request_id: str, conversation_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Normalizes `event.get("chunks")` into a uniform list of dicts
        with at least `text`/`chunk_id`/`sequence`/`total`/`is_final`/
        `conversation_id` keys - accepts EITHER a plain `str` (the TTS
        Chunking/Streaming sprint's original wire format) or a `dict`
        (`SpeechChunk.to_dict()`, this sprint's richer format) per entry,
        so every pre-existing caller/test using plain strings keeps
        working unchanged. `raw_chunks` empty/falsy degrades to a single
        one-item list derived from `text` (the legacy, pre-chunking
        behavior, byte-identical - see module docstring)."""
        entries = list(raw_chunks or [])
        if not entries:
            entries = [text] if text else [""]
        total = len(entries)
        normalized: List[Dict[str, Any]] = []
        for i, entry in enumerate(entries):
            if isinstance(entry, dict):
                normalized.append({
                    "text": entry.get("text", ""),
                    "chunk_id": entry.get("chunk_id") or f"{request_id}:chunk:{i}",
                    "sequence": entry.get("sequence", i),
                    "total": entry.get("total", total),
                    "is_final": entry.get("is_final", i == total - 1),
                    "conversation_id": entry.get("conversation_id", conversation_id),
                })
            else:
                normalized.append({
                    "text": entry,
                    "chunk_id": f"{request_id}:chunk:{i}",
                    "sequence": i,
                    "total": total,
                    "is_final": i == total - 1,
                    "conversation_id": conversation_id,
                })
        return normalized

    def _handle_stream_chunk(self, event: Any) -> None:
        """Fast, non-blocking - `handle_event()` must stay fast for every
        event type (see its own docstring). The FIRST `SpeakStreamChunk`
        seen for a `request_id` opens a new streaming utterance -
        registers a `SpeechCancellationToken` (same synchronous
        "register-before-submit" pattern as the `SpeakRequest`/
        `AssistantResponse` branch above, closing the identical pre-
        synthesis cancellation race for a streaming request), creates its
        live queue, then submits `_play_stream()` to the SAME
        `_playback_executor` `_play()` already uses - never a second/
        parallel executor. Every subsequent chunk for an ALREADY-open
        request_id is simply enqueued (non-blocking `put_nowait` - see
        `self._stream_queues`'s own docstring for why a full queue here
        is a defensive/should-never-happen case, not the real
        backpressure mechanism)."""
        request_id = event.get("request_id")
        if not request_id:
            log("'speak_stream_chunk' missing request_id - dropped", self.name)
            return
        chunk = event.get("chunk") or {}
        conversation_id = event.get("conversation_id")
        pool = self._playback_executor
        if pool is None:
            log(f"'{self.name}' dropped 'speak_stream_chunk' - not started", self.name)
            return
        with self._stream_queue_lock:
            q = self._stream_queues.get(request_id)
            is_new = q is None
            if is_new:
                q = queue.Queue(maxsize=self._STREAM_QUEUE_MAXSIZE)
                self._stream_queues[request_id] = q
        if is_new:
            token = SpeechCancellationToken(request_id)
            with self._chunk_control_lock:
                self._chunk_control[request_id] = token
            pool.submit(self._play_stream, request_id, conversation_id, token)
        try:
            q.put_nowait(chunk)
        except queue.Full:
            log(
                f"stream queue full for request_id={request_id} - dropping one chunk "
                f"(upstream StreamingSpeechCoordinator backpressure should prevent this)",
                self.name,
            )

    def _play_stream(self, request_id: str, conversation_id: Optional[str], token: SpeechCancellationToken) -> None:
        """Streaming counterpart to `_play()` - SAME client/token/
        terminal-event contract (exactly one of `SpeechPlaybackFinished`/
        `SpeechPlaybackCancelled` published per request_id, chunk-level
        bounded retry-then-skip, `SpeechPlaybackStarted` only on the
        first real audio), fed from a LIVE queue (chunks arriving over
        time via `SpeakStreamChunk`) instead of a precomputed list. A
        chunk whose `text` is empty is a close-only marker (see
        `luno.incremental_speech.IncrementalSpeechBuffer.make_close_marker()`)
        - never passed to `client.play()`, only checked for `is_final`.

        TTS Chunk Pipelining sprint: if `self.client.supports_split_synthesis()`
        is `True`, this entire method is bypassed in favor of
        `_play_stream_pipelined()` below, which overlaps chunk N+1's
        synthesis with chunk N's playback (a bounded one-slot lookahead).
        This method's own body is otherwise BYTE-IDENTICAL to before that
        sprint - it remains the path for `MockFishAudioClient` and any
        other client that does not opt in, so every pre-existing test is
        completely unaffected."""
        if self.client.supports_split_synthesis():
            return self._play_stream_pipelined(request_id, conversation_id, token)
        q = self._stream_queues.get(request_id)
        submitted_at = time.time()
        started_at: List[float] = []
        any_chunk_played = False
        last_exception: Optional[Exception] = None
        idle_timeout_hit = False

        def _on_playback_start(chunk: Dict[str, Any], chunk_dispatched_at: float, audio_start_box: List[float]) -> None:
            now = time.time()
            audio_start_box.append(now)
            if not started_at:
                started_at.append(now)
                log(
                    f"SpeechStreamStarted request_id={request_id} conversation_id={conversation_id} "
                    f"synthesis_time_s={round(now - submitted_at, 3)}",
                    self.name,
                )
                self.publish(SpeechPlaybackStarted(data={"request_id": request_id, "text": chunk.get("text", "")}))
            log(
                f"ChunkAudioStart request_id={request_id} chunk_id={chunk.get('chunk_id')} "
                f"chunk_index={chunk.get('sequence')} chunk_synthesis_time_s={round(now - chunk_dispatched_at, 3)}",
                self.name,
            )

        with self._in_flight_lock:
            self._in_flight_request_ids.add(request_id)
        try:
            idle_since = time.time()
            while True:
                if token.is_cancelled:
                    break
                token.wait_while_paused()
                if token.is_cancelled:
                    break
                if q is None:
                    break
                try:
                    chunk = q.get(timeout=self._STREAM_POLL_INTERVAL_S)
                except queue.Empty:
                    if time.time() - idle_since > self._STREAM_IDLE_TIMEOUT_S:
                        log(
                            f"SpeechStreamIdleTimeout request_id={request_id} - no chunk arrived within "
                            f"{self._STREAM_IDLE_TIMEOUT_S}s (no is_final ever received) - aborting",
                            self.name,
                        )
                        idle_timeout_hit = True
                        break
                    continue
                idle_since = time.time()
                chunk_id = chunk.get("chunk_id") or f"{request_id}:chunk:?"
                sequence = chunk.get("sequence")
                text = chunk.get("text") or ""
                is_final = bool(chunk.get("is_final"))

                if not text:
                    # Close-only marker - the coordinator's explicit "no
                    # more chunks coming" signal when nothing new was left
                    # to flush at stream end. Never played.
                    self.publish(SpeechChunkPlaybackFinished(data={"request_id": request_id, "chunk_id": chunk_id, "sequence": sequence}))
                    if is_final:
                        break
                    continue

                # Bounded retry (same policy/limit as `_play()`'s genuine
                # multi-chunk path) - a streaming request is, by
                # definition, never the legacy single-block case, so
                # retry is always eligible here.
                chunk_dispatched_at = time.time()
                attempts = 0
                while True:
                    audio_start_box: List[float] = []
                    try:
                        self.client.play(
                            text,
                            on_playback_start=lambda c=chunk, t=chunk_dispatched_at, box=audio_start_box: _on_playback_start(c, t, box),
                        )
                        any_chunk_played = True
                        chunk_total_s = round(time.time() - chunk_dispatched_at, 3)
                        playback_only_s = round(time.time() - audio_start_box[0], 3) if audio_start_box else None
                        log(
                            f"ChunkFinished request_id={request_id} chunk_id={chunk_id} chunk_index={sequence} "
                            f"total_s={chunk_total_s} playback_s={playback_only_s}",
                            self.name,
                        )
                        break
                    except PlaybackCancelled:
                        duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                        log(
                            f"SpeechCancelled request_id={request_id} chunk_id={chunk_id} chunk_index={sequence} "
                            f"was_playing={bool(started_at)} elapsed_s={duration}",
                            self.name,
                        )
                        self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": sequence}))
                        return
                    except Exception as ex:
                        last_exception = ex
                        attempts += 1
                        if attempts <= self._chunk_retry_limit:
                            log(f"ChunkRetry request_id={request_id} chunk_id={chunk_id} attempt={attempts} exception={ex!r}", self.name)
                            continue
                        log(f"ChunkSkipped request_id={request_id} chunk_id={chunk_id} exception={ex!r}", self.name)
                        break
                self.publish(SpeechChunkPlaybackFinished(data={"request_id": request_id, "chunk_id": chunk_id, "sequence": sequence}))
                if is_final:
                    break

            if idle_timeout_hit:
                # Phase 12 - distinct from both cancellation and an
                # ordinary chunk-level failure: the STREAM itself was
                # abandoned (no `is_final` ever arrived). Always reported
                # as an explicit error, never silently treated as a
                # normal finish, even if some earlier chunks did play.
                log(f"SpeechError request_id={request_id} exception='stream idle timeout - abandoned'", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": "stream idle timeout - abandoned"}))
                return
            if token.is_cancelled and not any_chunk_played:
                duration = round(time.time() - submitted_at, 3)
                log(f"SpeechCancelled request_id={request_id} chunk_index=0 was_playing=False elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": 0}))
                return
            if token.is_cancelled:
                duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                log(f"SpeechCancelled request_id={request_id} was_playing=True elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id}))
                return
            if not any_chunk_played:
                error_message = str(last_exception) if last_exception is not None else "streaming speech ended with no chunk ever played"
                log(f"SpeechError request_id={request_id} exception={error_message!r}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": error_message}))
                return
            playback_duration = round(time.time() - started_at[0], 3) if started_at else None
            log(f"SpeechFinished request_id={request_id} conversation_id={conversation_id} playback_duration_s={playback_duration}", self.name)
            self.publish(SpeechPlaybackFinished(data={"request_id": request_id}))
        except Exception as ex:
            # Dashboard Turn-State Recovery fix, part 2 (post-Sprint-51) -
            # defense in depth, same reasoning as `_play()`'s own new
            # `except Exception` clause: this is the DEFAULT streaming
            # playback path (`ENABLE_LLM_TTS_STREAMING` defaults on), so
            # an unanticipated exception here is at least as likely to be
            # hit in production as the legacy `_play()` path.
            log(f"SpeechError request_id={request_id} unhandled_exception={ex!r} - publishing SpeechPlaybackCancelled so the turn is never stuck", self.name)
            self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": f"unhandled: {ex}"}))
        finally:
            with self._in_flight_lock:
                self._in_flight_request_ids.discard(request_id)
            with self._chunk_control_lock:
                self._chunk_control.pop(request_id, None)
            with self._stream_queue_lock:
                self._stream_queues.pop(request_id, None)

    def _resolve_audio(self, text: str, future: Optional[Future], token: SpeechCancellationToken) -> Tuple[bool, Any]:
        """Waits for a prefetched chunk's synthesis `Future` (if one is in
        flight for it) or, if nothing was prefetched, synthesizes it fresh
        via a plain synchronous `self.client.synthesize(text)` call.
        Returns `(cancelled, audio)` - `cancelled=True` means `token` was
        cancelled while this was waiting, `audio` is then meaningless and
        must be discarded.

        Uses the SAME cancellation-responsive bounded-poll idiom
        `RealFishAudioClient.play()` already established internally
        (`future.result(timeout=...)` in a loop instead of one unbounded
        wait) so an abandoned/cancelled turn never blocks here waiting on
        a slow in-flight prefetch. Per the "abandon, never force-kill"
        policy (see `FishAudioClient.synthesize()`'s own docstring above),
        an abandoned `Future` is simply never awaited again - its worker
        thread finishes (or fails) on its own and the result is discarded;
        nothing here ever calls `Future.cancel()`."""
        if future is None:
            if token.is_cancelled:
                return True, None
            return False, self.client.synthesize(text)
        while True:
            if token.is_cancelled:
                return True, None
            try:
                return False, future.result(timeout=self._STREAM_POLL_INTERVAL_S)
            except FuturesTimeoutError:
                continue

    def _play_stream_pipelined(self, request_id: str, conversation_id: Optional[str], token: SpeechCancellationToken) -> None:
        """One-slot-prefetch counterpart to `_play_stream()` - identical
        client/token/terminal-event contract in every particular (exactly
        one of `SpeechPlaybackFinished`/`SpeechPlaybackCancelled`
        published per request_id, chunk-level bounded retry-then-skip,
        `SpeechPlaybackStarted` only on the first real audio, close
        markers never synthesized/played). Only called when
        `self.client.supports_split_synthesis()` is `True` (see
        `_play_stream()`'s own dispatch line above).

        The ONLY behavioral difference: while chunk N's audio is playing
        (`self.client.play_audio()`, blocking), this method opportunistically
        peeks the queue (non-blocking) for chunk N+1 and, if a REAL
        (non-close-marker) chunk is already available, submits its
        synthesis to `self._prefetch_executor` right away - so by the time
        chunk N's playback finishes, chunk N+1's audio is often already
        ready. Never more than ONE chunk is prefetched at a time (the
        `prefetch_future`/`prefetch_chunk` pair below is always either
        `None` or a single in-flight job - there is no queue of futures).
        `current` always advances to exactly the next item THIS method
        itself dequeued, in dequeue order - chunks are never reordered by
        which synthesis happens to finish first."""
        q = self._stream_queues.get(request_id)
        submitted_at = time.time()
        started_at: List[float] = []
        any_chunk_played = False
        last_exception: Optional[Exception] = None
        idle_timeout_hit = False

        def _on_playback_start(chunk: Dict[str, Any], chunk_dispatched_at: float, audio_start_box: List[float]) -> None:
            now = time.time()
            audio_start_box.append(now)
            if not started_at:
                started_at.append(now)
                log(
                    f"SpeechStreamStarted request_id={request_id} conversation_id={conversation_id} "
                    f"synthesis_time_s={round(now - submitted_at, 3)}",
                    self.name,
                )
                self.publish(SpeechPlaybackStarted(data={"request_id": request_id, "text": chunk.get("text", "")}))
            log(
                f"ChunkAudioStart request_id={request_id} chunk_id={chunk.get('chunk_id')} "
                f"chunk_index={chunk.get('sequence')} chunk_synthesis_time_s={round(now - chunk_dispatched_at, 3)}",
                self.name,
            )

        def _dequeue_next(idle_since_box: List[float]) -> "tuple[Optional[Dict[str, Any]], bool]":
            """Blocking dequeue with idle-timeout - identical semantics to
            `_play_stream()`'s own inline loop body, factored out here
            because this method dequeues from two call sites (the initial
            "prime the pipe" fetch and every steady-state advance).
            Returns `(chunk_or_None, should_stop)`."""
            nonlocal idle_timeout_hit
            while True:
                if token.is_cancelled:
                    return None, True
                token.wait_while_paused()
                if token.is_cancelled:
                    return None, True
                if q is None:
                    return None, True
                try:
                    chunk = q.get(timeout=self._STREAM_POLL_INTERVAL_S)
                    idle_since_box[0] = time.time()
                    return chunk, False
                except queue.Empty:
                    if time.time() - idle_since_box[0] > self._STREAM_IDLE_TIMEOUT_S:
                        log(
                            f"SpeechStreamIdleTimeout request_id={request_id} - no chunk arrived within "
                            f"{self._STREAM_IDLE_TIMEOUT_S}s (no is_final ever received) - aborting",
                            self.name,
                        )
                        idle_timeout_hit = True
                        return None, True
                    continue

        with self._in_flight_lock:
            self._in_flight_request_ids.add(request_id)
        # At most ONE outstanding prefetch job at a time - `None` when
        # nothing has been prefetched (e.g. right after priming, or the
        # previous iteration had nothing left to peek).
        prefetch_future: Optional[Future] = None
        prefetch_chunk: Optional[Dict[str, Any]] = None
        try:
            idle_since_box = [time.time()]
            current, should_stop = _dequeue_next(idle_since_box)
            while not should_stop and current is not None:
                chunk_id = current.get("chunk_id") or f"{request_id}:chunk:?"
                sequence = current.get("sequence")
                text = current.get("text") or ""
                is_final = bool(current.get("is_final"))

                if not text:
                    # Close-only marker - never synthesized/played, exactly
                    # like `_play_stream()`'s own handling.
                    self.publish(SpeechChunkPlaybackFinished(data={"request_id": request_id, "chunk_id": chunk_id, "sequence": sequence}))
                    if is_final:
                        break
                    current, should_stop = _dequeue_next(idle_since_box)
                    continue

                # Resolve THIS chunk's audio - reuse a matching in-flight
                # prefetch if we have one, else synthesize fresh - bundled
                # with playback in ONE bounded-retry loop below, mirroring
                # `_play_stream()`'s own retry (which likewise bundles
                # synth+play as a single `client.play()` call - a retry
                # here re-synthesizes fresh too, never reuses a stale
                # prefetch future).
                this_future = prefetch_future if prefetch_chunk is current else None
                prefetch_future = None
                prefetch_chunk = None
                next_chunk: Optional[Dict[str, Any]] = None
                prefetch_peeked = False

                chunk_dispatched_at = time.time()
                attempts = 0
                while True:
                    audio_start_box: List[float] = []
                    try:
                        # Voice Output Naturalness & First-Audio Latency
                        # sprint - Phase 5 (cancellation/barge-in safety
                        # audit) bug fix: this streaming sibling of
                        # `_play_pipelined()` never received that method's
                        # own chunk-0 fix (see its matching comment there,
                        # from the "Voice Pipeline Latency & Semantic
                        # Segmentation sprint" / `test_cancel_during_
                        # synthesis_never_publishes_started`). For chunk 0
                        # of a streaming utterance, nothing was ever
                        # prefetched (`this_future` is `None`), which made
                        # `_resolve_audio()` fall through to calling
                        # `self.client.synthesize()` directly, synchronously,
                        # in THIS thread - NOT cancellation-aware on its
                        # own (see the client ABC's own docstring) - so a
                        # `StopPlayback` arriving mid-synthesis had nothing
                        # to interrupt, and stale audio played anyway once
                        # synthesis finished. Reproduced empirically via
                        # `tests/test_real_fish_audio_console.py::
                        # test_voice_interrupt_while_still_synthesizing_real_speech_succeeds`
                        # once this sprint made `ENABLE_LLM_TTS_STREAMING`
                        # the default (the bug was always latent here, just
                        # unreachable in production while streaming
                        # defaulted off). Same fix, reused verbatim: always
                        # submit chunk 0's synthesis to `_prefetch_executor`
                        # first and let `_resolve_audio()` poll that Future
                        # - never a raw in-thread `synthesize()` call - so
                        # chunk 0 gets the exact same cancellation
                        # responsiveness every later, genuinely-prefetched
                        # chunk already had.
                        if this_future is None:
                            this_future = self._prefetch_executor.submit(self.client.synthesize, text)
                        cancelled, audio = self._resolve_audio(text, this_future, token)
                        this_future = None
                        if cancelled:
                            raise PlaybackCancelled("cancelled while resolving audio")

                        # Opportunistically peek the NEXT item (non-blocking,
                        # at most once per chunk) so its synthesis can start
                        # WHILE `current`'s audio plays below - this IS the
                        # entire one-slot prefetch mechanism. Only reached
                        # once resolution of `current` has actually
                        # succeeded, so a resolve failure never wastes a
                        # peek on a chunk about to be retried anyway.
                        if not prefetch_peeked:
                            prefetch_peeked = True
                            if not is_final and q is not None:
                                try:
                                    next_chunk = q.get_nowait()
                                    idle_since_box[0] = time.time()
                                except queue.Empty:
                                    next_chunk = None
                            if next_chunk is not None and (next_chunk.get("text") or ""):
                                prefetch_chunk = next_chunk
                                prefetch_future = self._prefetch_executor.submit(self.client.synthesize, next_chunk.get("text") or "")

                        self.client.play_audio(
                            audio,
                            on_playback_start=lambda c=current, t=chunk_dispatched_at, box=audio_start_box: _on_playback_start(c, t, box),
                        )
                        any_chunk_played = True
                        chunk_total_s = round(time.time() - chunk_dispatched_at, 3)
                        playback_only_s = round(time.time() - audio_start_box[0], 3) if audio_start_box else None
                        log(
                            f"ChunkFinished request_id={request_id} chunk_id={chunk_id} chunk_index={sequence} "
                            f"total_s={chunk_total_s} playback_s={playback_only_s}",
                            self.name,
                        )
                        break
                    except PlaybackCancelled:
                        duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                        log(
                            f"SpeechCancelled request_id={request_id} chunk_id={chunk_id} chunk_index={sequence} "
                            f"was_playing={bool(started_at)} elapsed_s={duration}",
                            self.name,
                        )
                        self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": sequence}))
                        return
                    except Exception as ex:
                        last_exception = ex
                        attempts += 1
                        if attempts <= self._chunk_retry_limit:
                            log(f"ChunkRetry request_id={request_id} chunk_id={chunk_id} attempt={attempts} exception={ex!r}", self.name)
                            this_future = None  # a retry always re-synthesizes fresh
                            continue
                        log(f"ChunkSkipped request_id={request_id} chunk_id={chunk_id} exception={ex!r}", self.name)
                        break
                self.publish(SpeechChunkPlaybackFinished(data={"request_id": request_id, "chunk_id": chunk_id, "sequence": sequence}))
                if is_final:
                    break

                if next_chunk is not None:
                    current = next_chunk
                    should_stop = False
                else:
                    current, should_stop = _dequeue_next(idle_since_box)

            if idle_timeout_hit:
                log(f"SpeechError request_id={request_id} exception='stream idle timeout - abandoned'", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": "stream idle timeout - abandoned"}))
                return
            if token.is_cancelled and not any_chunk_played:
                duration = round(time.time() - submitted_at, 3)
                log(f"SpeechCancelled request_id={request_id} chunk_index=0 was_playing=False elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": 0}))
                return
            if token.is_cancelled:
                duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                log(f"SpeechCancelled request_id={request_id} was_playing=True elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id}))
                return
            if not any_chunk_played:
                error_message = str(last_exception) if last_exception is not None else "streaming speech ended with no chunk ever played"
                log(f"SpeechError request_id={request_id} exception={error_message!r}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": error_message}))
                return
            playback_duration = round(time.time() - started_at[0], 3) if started_at else None
            log(f"SpeechFinished request_id={request_id} conversation_id={conversation_id} playback_duration_s={playback_duration}", self.name)
            self.publish(SpeechPlaybackFinished(data={"request_id": request_id}))
        except Exception as ex:
            # Dashboard Turn-State Recovery fix, part 2 (post-Sprint-51) -
            # defense in depth, same reasoning as `_play()`/`_play_stream()`'s
            # own new `except Exception` clause. This is the pipelined
            # streaming path (`client.supports_split_synthesis() is True`).
            log(f"SpeechError request_id={request_id} unhandled_exception={ex!r} - publishing SpeechPlaybackCancelled so the turn is never stuck", self.name)
            self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": f"unhandled: {ex}"}))
        finally:
            with self._in_flight_lock:
                self._in_flight_request_ids.discard(request_id)
            with self._chunk_control_lock:
                self._chunk_control.pop(request_id, None)
            with self._stream_queue_lock:
                self._stream_queues.pop(request_id, None)

    def _play_pipelined(self, event: Any, token: Optional[SpeechCancellationToken] = None) -> None:
        """One-slot-prefetch counterpart to `_play()` - identical client/
        token/terminal-event contract in every particular (chunk-level
        bounded retry-then-skip, `SpeechPlaybackStarted` only on the first
        real audio, exactly one of `SpeechPlaybackFinished`/
        `SpeechPlaybackCancelled` published per request_id). Only called
        when `self.client.supports_split_synthesis()` is `True` (see
        `_play()`'s own dispatch line below) - `_play()`'s own body below
        that line remains BYTE-IDENTICAL to before this sprint, so
        `MockFishAudioClient` and every pre-existing test using it are
        completely unaffected.

        Voice Pipeline Latency & Semantic Segmentation sprint - closes a
        gap the TTS Chunk Pipelining sprint's own one-slot prefetch design
        never actually reached in the CURRENT default configuration:
        that sprint's `_play_stream_pipelined()` only ever served the
        LIVE, incrementally-arriving `SpeakStreamChunk` queue (gated
        behind `ENABLE_LLM_TTS_STREAMING`, default off) - the ordinary
        `speak_request`/`AssistantResponse` path (what `BehaviorTreeModule
        ._speak()` actually publishes today) called plain `_play()`,
        which never checked `supports_split_synthesis()` and never
        overlapped synthesis with playback. Measured directly (Phase 0/1
        of this sprint - see `tests/test_voice_pipeline_latency.py`): a
        multi-chunk reply through the default path showed ZERO synthesis/
        playback overlap at every chunk boundary - the exact bug the TTS
        Chunk Pipelining sprint set out to fix, just unreachable from the
        code path actually running in production. This method reuses that
        sprint's EXACT mechanism (`_resolve_audio()`, the shared
        `_prefetch_executor`, the same one-slot `prefetch_future`/
        `prefetch_index` invariant - never more than one job outstanding)
        against a PRE-COMPUTED `chunks` list (plain Python list + index)
        instead of a live `queue.Queue` - no new prefetch mechanism, no
        second executor, no duplicated cancellation/retry policy."""
        text = event.get("text", "")
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")
        chunks = self._normalize_chunk_entries(event.get("chunks"), text, request_id, conversation_id)
        total = len(chunks)
        submitted_at = time.time()
        started_at: List[float] = []
        any_chunk_played = False
        last_exception: Optional[Exception] = None

        def _on_playback_start(chunk: Dict[str, Any], chunk_dispatched_at: float, audio_start_box: List[float]) -> None:
            now = time.time()
            audio_start_box.append(now)
            if not started_at:
                started_at.append(now)
                log(
                    f"SpeechStarted request_id={request_id} conversation_id={conversation_id} chunks={total} "
                    f"synthesis_time_s={round(now - submitted_at, 3)}",
                    self.name,
                )
                self.publish(SpeechPlaybackStarted(data={"request_id": request_id, "text": text}))
            log(
                f"ChunkAudioStart request_id={request_id} chunk_id={chunk['chunk_id']} "
                f"chunk_index={chunk['sequence']} of={total} "
                f"chunk_synthesis_time_s={round(now - chunk_dispatched_at, 3)}",
                self.name,
            )

        with self._in_flight_lock:
            self._in_flight_request_ids.add(request_id)
        if token is None:
            token = SpeechCancellationToken(request_id)
            with self._chunk_control_lock:
                self._chunk_control[request_id] = token

        # At most ONE outstanding prefetch job at a time - identical
        # invariant to `_play_stream_pipelined()`'s own `prefetch_future`/
        # `prefetch_chunk` pair, indexed here instead of chunk-identity-
        # compared (a plain list has stable positional identity already).
        prefetch_future: Optional[Future] = None
        prefetch_index: Optional[int] = None
        try:
            idx = 0
            while idx < total:
                if token.is_cancelled:
                    break
                token.wait_while_paused()
                if token.is_cancelled:
                    break

                chunk = chunks[idx]
                i = chunk["sequence"]
                retry_limit = self._chunk_retry_limit if total > 1 else 0
                this_future = prefetch_future if prefetch_index == idx else None
                prefetch_future = None
                prefetch_index = None

                chunk_dispatched_at = time.time()
                attempts = 0
                prefetch_peeked = False
                while True:
                    audio_start_box: List[float] = []
                    try:
                        # Bug found by this sprint's own regression run
                        # (`test_cancel_during_synthesis_never_publishes_started`,
                        # `luno/adapters/tests/test_fish_audio_real.py`):
                        # `self.client.synthesize()` is explicitly NOT
                        # cancellation-aware on its own (see the ABC's own
                        # docstring) - `_resolve_audio()` only becomes
                        # cancellation-RESPONSIVE while polling a `Future`
                        # via its own bounded `future.result(timeout=...)`
                        # loop. For chunk 0 (nothing was ever prefetched
                        # for it), `this_future` was `None` here, which
                        # made `_resolve_audio()` fall through to calling
                        # `self.client.synthesize()` directly, synchronously,
                        # in THIS thread - a `StopPlayback` arriving during
                        # that call had nothing to interrupt it with,
                        # unlike `_play()`'s own `client.play()` call
                        # (which IS cancellation-aware internally). Fix:
                        # always submit synthesis to `_prefetch_executor`
                        # and let `_resolve_audio()` poll that Future -
                        # never a raw in-thread `synthesize()` call - so
                        # chunk 0 gets the exact same cancellation
                        # responsiveness every later, genuinely-prefetched
                        # chunk already had.
                        if this_future is None:
                            this_future = self._prefetch_executor.submit(self.client.synthesize, chunk["text"])
                        cancelled, audio = self._resolve_audio(chunk["text"], this_future, token)
                        this_future = None
                        if cancelled:
                            raise PlaybackCancelled("cancelled while resolving audio")

                        # One-slot prefetch - identical mechanism to
                        # `_play_stream_pipelined()`, just indexing into a
                        # list instead of dequeuing a live queue. Only
                        # peeked once per chunk (guarded by
                        # `prefetch_peeked`) so a retry of THIS chunk never
                        # submits a second, duplicate prefetch job for the
                        # SAME next chunk.
                        if not prefetch_peeked:
                            prefetch_peeked = True
                            if idx + 1 < total:
                                next_chunk = chunks[idx + 1]
                                prefetch_index = idx + 1
                                prefetch_future = self._prefetch_executor.submit(self.client.synthesize, next_chunk["text"])

                        self.client.play_audio(
                            audio,
                            on_playback_start=lambda c=chunk, t=chunk_dispatched_at, box=audio_start_box: _on_playback_start(c, t, box),
                        )
                        any_chunk_played = True
                        chunk_total_s = round(time.time() - chunk_dispatched_at, 3)
                        playback_only_s = round(time.time() - audio_start_box[0], 3) if audio_start_box else None
                        log(
                            f"ChunkFinished request_id={request_id} chunk_id={chunk['chunk_id']} "
                            f"chunk_index={i} of={total} total_s={chunk_total_s} playback_s={playback_only_s}",
                            self.name,
                        )
                        break
                    except PlaybackCancelled:
                        duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                        log(
                            f"SpeechCancelled request_id={request_id} chunk_id={chunk['chunk_id']} "
                            f"chunk_index={i} of={total} was_playing={bool(started_at)} elapsed_s={duration}",
                            self.name,
                        )
                        self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": i}))
                        return
                    except Exception as ex:
                        last_exception = ex
                        attempts += 1
                        if attempts <= retry_limit:
                            log(f"ChunkRetry request_id={request_id} chunk_id={chunk['chunk_id']} chunk_index={i} of={total} attempt={attempts} exception={ex!r}", self.name)
                            this_future = None  # a retry always re-synthesizes fresh
                            continue
                        log(f"ChunkSkipped request_id={request_id} chunk_id={chunk['chunk_id']} chunk_index={i} of={total} exception={ex!r}", self.name)
                        break
                idx += 1

            if token.is_cancelled and not any_chunk_played:
                duration = round(time.time() - submitted_at, 3)
                log(f"SpeechCancelled request_id={request_id} chunk_index=0 of={total} was_playing=False elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": 0}))
                return
            if token.is_cancelled:
                duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                log(f"SpeechCancelled request_id={request_id} was_playing=True elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id}))
                return
            if not any_chunk_played:
                error_message = str(last_exception) if (total == 1 and last_exception is not None) else f"all {total} chunk(s) failed"
                log(f"SpeechError request_id={request_id} exception={error_message!r}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": error_message}))
                return
            playback_duration = round(time.time() - started_at[0], 3) if started_at else None
            log(f"SpeechFinished request_id={request_id} conversation_id={conversation_id} playback_duration_s={playback_duration}", self.name)
            self.publish(SpeechPlaybackFinished(data={"request_id": request_id}))
        except Exception as ex:
            # Dashboard Turn-State Recovery fix, part 2 (post-Sprint-51) -
            # defense in depth, same reasoning as the other three `_play*`
            # methods' own new `except Exception` clause.
            log(f"SpeechError request_id={request_id} unhandled_exception={ex!r} - publishing SpeechPlaybackCancelled so the turn is never stuck", self.name)
            self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": f"unhandled: {ex}"}))
        finally:
            with self._in_flight_lock:
                self._in_flight_request_ids.discard(request_id)
            with self._chunk_control_lock:
                self._chunk_control.pop(request_id, None)

    def _play(self, event: Any, token: Optional[SpeechCancellationToken] = None) -> None:
        """Plays one turn's speech, chunk by chunk, STRICTLY sequentially
        (chunk N+1's `client.play()` is never called until chunk N's own
        `client.play()` call has returned) - never `_playback_executor`-
        parallel playback of two chunks from the SAME turn (the two
        workers remain reserved for the pre-existing "paused reply +
        CONFIRM interjection" concurrency case only, see class docstring).
        `event.get("chunks")` absent/empty degrades to a single one-item
        list derived from `event.get("text")` - the pre-chunking behavior,
        byte-identical (see module docstring).

        `token`, when given, is the SAME `SpeechCancellationToken`
        `handle_event()` already registered in `self._chunk_control`
        BEFORE submitting this call to the executor (closes the "cancel
        arrived before this worker thread even started" race - see that
        method's own comment). If omitted (e.g. a direct/test call to
        `_play()` that bypasses `handle_event()`), a fresh token is
        created and registered here instead, preserving the exact
        pre-existing behavior for any such caller.

        Voice Pipeline Latency & Semantic Segmentation sprint: if
        `self.client.supports_split_synthesis()` is `True`, this entire
        method is bypassed in favor of `_play_pipelined()` above, which
        overlaps chunk N+1's synthesis with chunk N's playback (the same
        bounded one-slot lookahead `_play_stream_pipelined()` already
        established for the LLM-streaming path - see that method's own
        docstring for why this dispatch was missing here). This method's
        own body below is otherwise BYTE-IDENTICAL to before this sprint -
        it remains the path for `MockFishAudioClient` and any other
        client that does not opt in, so every pre-existing test is
        completely unaffected."""
        if self.client.supports_split_synthesis():
            return self._play_pipelined(event, token)
        text = event.get("text", "")
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")
        chunks = self._normalize_chunk_entries(event.get("chunks"), text, request_id, conversation_id)
        total = len(chunks)
        submitted_at = time.time()
        started_at: List[float] = []  # single-slot box - closures can't rebind an outer float

        def _on_playback_start(chunk: Dict[str, Any], chunk_dispatched_at: float, audio_start_box: List[float]) -> None:
            now = time.time()
            audio_start_box.append(now)  # Phase 5 latency instrumentation - see below
            if not started_at:
                started_at.append(now)
                log(
                    f"SpeechStarted request_id={request_id} conversation_id={conversation_id} chunks={total} "
                    f"synthesis_time_s={round(now - submitted_at, 3)}",
                    self.name,
                )
                self.publish(SpeechPlaybackStarted(data={"request_id": request_id, "text": text}))
            # Phase 5 (latency instrumentation) - per-CHUNK synthesis time,
            # for every chunk, not just the first: how long between "this
            # chunk was dispatched to client.play()" and "real audio for
            # THIS chunk actually started" - lets the first-chunk number
            # above (time-to-first-audio for the WHOLE turn) be compared
            # against later chunks' own synthesis cost. Never logs the
            # chunk's own spoken TEXT - only correlation ids/indices/
            # timings (Phase 8 - "don't log raw private conversation text
            # beyond what the existing logger already does"; the existing
            # `SpeechStarted` line above already includes `text=` nowhere,
            # matching that established convention).
            log(
                f"ChunkAudioStart request_id={request_id} chunk_id={chunk['chunk_id']} "
                f"chunk_index={chunk['sequence']} of={total} "
                f"chunk_synthesis_time_s={round(now - chunk_dispatched_at, 3)}",
                self.name,
            )

        with self._in_flight_lock:
            self._in_flight_request_ids.add(request_id)
        if token is None:
            # No pre-registered token (a direct/test call that bypassed
            # `handle_event()`) - fall back to creating/registering one
            # here, exactly like before this sprint's "register before
            # submit" fix.
            token = SpeechCancellationToken(request_id)
            with self._chunk_control_lock:
                self._chunk_control[request_id] = token
        try:
            any_chunk_played = False
            for chunk in chunks:
                i = chunk["sequence"]
                if token.is_cancelled:
                    break
                # Closes the "gap between chunks" race (see module
                # docstring) - a pause requested exactly between two
                # chunks has nothing in-flight for `client.pause()` to
                # act on, so this loop waits here itself instead.
                token.wait_while_paused()
                if token.is_cancelled:
                    break

                # Backward-compat guard: when the caller didn't opt into
                # chunking (`total == 1`, the derived-from-`text` legacy
                # path), retrying must stay OFF and the published error
                # must be the ORIGINAL exception's own message - otherwise
                # this sprint would silently change the pre-existing
                # single-block failure contract (immediate failure, exact
                # `str(ex)`) that `test_fish_audio_real.py`/
                # `test_fish_audio_barge_in.py` already pin down. Retry is
                # a genuinely NEW behavior, only for genuine multi-chunk
                # (`total > 1`) playback.
                retry_limit = self._chunk_retry_limit if total > 1 else 0
                chunk_dispatched_at = time.time()
                attempts = 0
                last_exception: Optional[Exception] = None
                while True:
                    audio_start_box: List[float] = []
                    try:
                        self.client.play(
                            chunk["text"],
                            on_playback_start=lambda c=chunk, t=chunk_dispatched_at, box=audio_start_box: _on_playback_start(c, t, box),
                        )
                        any_chunk_played = True
                        chunk_total_s = round(time.time() - chunk_dispatched_at, 3)
                        # Phase 5 - split the chunk's own total time into
                        # synthesis (dispatch -> real audio start) vs.
                        # playback (real audio start -> play() returned),
                        # when `on_playback_start` actually fired for this
                        # chunk (it may not have, e.g. a chunk that failed
                        # before ever reaching audio - not this branch,
                        # this is only reached on success, so it always has).
                        playback_only_s = round(time.time() - audio_start_box[0], 3) if audio_start_box else None
                        log(
                            f"ChunkFinished request_id={request_id} chunk_id={chunk['chunk_id']} "
                            f"chunk_index={i} of={total} total_s={chunk_total_s} playback_s={playback_only_s}",
                            self.name,
                        )
                        break
                    except PlaybackCancelled:
                        duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                        log(
                            f"SpeechCancelled request_id={request_id} chunk_id={chunk['chunk_id']} "
                            f"chunk_index={i} of={total} was_playing={bool(started_at)} elapsed_s={duration}",
                            self.name,
                        )
                        self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": i}))
                        return
                    except Exception as ex:
                        # Phase 7 - bounded retry, then skip this ONE chunk
                        # and continue with the rest in order (never replay
                        # an earlier chunk, never duplicate, never lose
                        # order - a mid-utterance synthesis hiccup should
                        # not silence the whole reply when Chat already has
                        # the full text regardless).
                        last_exception = ex
                        attempts += 1
                        if attempts <= retry_limit:
                            log(f"ChunkRetry request_id={request_id} chunk_id={chunk['chunk_id']} chunk_index={i} of={total} attempt={attempts} exception={ex!r}", self.name)
                            continue
                        log(f"ChunkSkipped request_id={request_id} chunk_id={chunk['chunk_id']} chunk_index={i} of={total} exception={ex!r}", self.name)
                        break

            if token.is_cancelled and not any_chunk_played:
                duration = round(time.time() - submitted_at, 3)
                log(f"SpeechCancelled request_id={request_id} chunk_index=0 of={total} was_playing=False elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "chunk_index": 0}))
                return
            if token.is_cancelled:
                # Stopped between chunks after at least one had already
                # played - already-published `SpeechPlaybackStarted`
                # means this is a cancellation of the REST, not a full
                # no-op turn; still exactly one terminal event, never both
                # Cancelled and Finished for the same request_id.
                duration = round(time.time() - (started_at[0] if started_at else submitted_at), 3)
                log(f"SpeechCancelled request_id={request_id} was_playing=True elapsed_s={duration}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id}))
                return
            if not any_chunk_played:
                # Every chunk failed (retried-if-applicable, then skipped)
                # and none ever played. For the legacy `total == 1` path
                # this is EXACTLY the pre-chunking single-block failure
                # contract: immediate failure, the original exception's
                # own message. For genuine multi-chunk failures (rare -
                # would mean every chunk of this reply failed synthesis),
                # a descriptive aggregate message is published instead,
                # since there is no single "the" exception to attribute it
                # to. Cancellation (above) is never reported as this kind
                # of generic failure - Phase 7's own explicit requirement.
                error_message = str(last_exception) if (total == 1 and last_exception is not None) else f"all {total} chunk(s) failed"
                log(f"SpeechError request_id={request_id} exception={error_message!r}", self.name)
                self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": error_message}))
                return
            playback_duration = round(time.time() - started_at[0], 3) if started_at else None
            log(f"SpeechFinished request_id={request_id} conversation_id={conversation_id} playback_duration_s={playback_duration}", self.name)
            self.publish(SpeechPlaybackFinished(data={"request_id": request_id}))
        except Exception as ex:
            # Dashboard Turn-State Recovery fix, part 2 (post-Sprint-51) -
            # defense in depth. Every normal exit above already publishes
            # exactly one of `SpeechPlaybackFinished`/`SpeechPlaybackCancelled`
            # and returns immediately, so this branch can only be reached
            # by a genuinely UNANTICIPATED exception escaping the loop
            # itself (not `client.play()` - that is already caught,
            # retried, and turned into a normal `SpeechPlaybackCancelled`
            # above). Before this fix, such an exception would kill this
            # `_playback_executor` worker thread silently (nothing calls
            # `.result()` on the submitted `Future` - see `handle_event()`)
            # with NO terminal event ever published, leaving
            # `SessionManagerModule` stuck exactly like the unguarded
            # `PlannerBridgeModule._handle_utterance()` thread Sprint 51
            # fixed - same bug class, this module's own equivalent gap.
            log(f"SpeechError request_id={request_id} unhandled_exception={ex!r} - publishing SpeechPlaybackCancelled so the turn is never stuck", self.name)
            self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": f"unhandled: {ex}"}))
        finally:
            with self._in_flight_lock:
                self._in_flight_request_ids.discard(request_id)
            with self._chunk_control_lock:
                self._chunk_control.pop(request_id, None)

    def cancel_playback(self) -> None:
        """Public convenience for callers that want to interrupt
        whatever is currently playing (e.g. Behavior Tree noticing an
        emergency mid-sentence) without going through the Event Bus -
        matches the spec's requirement that adapters expose their
        lifecycle/control surface directly, same as `restart()`. Prefer
        publishing `StopPlayback` from anywhere that isn't the object
        directly owning this adapter instance."""
        self.client.stop()
