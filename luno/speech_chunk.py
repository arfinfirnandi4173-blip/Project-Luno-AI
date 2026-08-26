"""
speech_chunk.py
================

TTS Chunk Queue & Cancellation sprint - the explicit, correlation-aware
contract for ONE playback-sized piece of a spoken reply, plus the
cancellation-token abstraction the Speech Queue (`FishAudioAdapter._play()`)
uses to stop cleanly mid-request.

    voice_chunks (List[str], cleaned)  ─┐
    voice_chunks_raw (List[str], raw)  ─┴─> build_speech_chunks() -> List[SpeechChunk]
                                                    |
                                          SpeakRequest.data["chunks"]
                                          (each chunk flattened via .to_dict(),
                                           same "dataclass + to_dict()" convention
                                           already used by luno.routing.RoutingDecision)
                                                    |
                                          FishAudioAdapter._play()
                                          (sequential, one SpeechCancellationToken
                                           per request_id)

This module owns the DATA MODEL only - no event bus, no I/O, no TTS calls,
no text segmentation of its own. `luno.response_output.build_dual_response()`
still owns ALL text segmentation (`voice_chunks`/`voice_chunks_raw`); this
module only ATTACHES correlation identity (chunk_id/request_id/
conversation_id/sequence/total/is_final) on top of that already-computed
text - never a second, independent split.

BACKWARD COMPATIBILITY: `FishAudioAdapter._play()` still accepts a plain
`List[str]` for `event.get("chunks")` (the TTS Chunking/Streaming sprint's
original, simpler wire format) in addition to `List[dict]`
(`SpeechChunk.to_dict()`, this sprint's richer format) - see that
module's own docstring. Nothing in this module requires the event bus or
adapter machinery to exist, so `build_speech_chunks()`/`SpeechCancellationToken`
are independently unit-testable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SpeechChunk:
    """One playback-sized piece of a spoken reply.

    `chunk_id` is DETERMINISTIC (`f"{request_id}:chunk:{sequence}"`) - no
    randomness, no clock, no I/O - so it is reproducible across a test
    run and stable for log correlation. `text` is what actually gets
    sent to the TTS engine (the cleaned/normalized form, same string
    `voice_chunks` already carried before this sprint); `raw_text` is a
    reference/debug copy of the same span BEFORE normalization (see
    `luno.response_output.DualResponse.voice_chunks_raw`) - never itself
    sent to TTS. `is_final` is True on (and only on) the LAST chunk of
    this `request_id`."""

    chunk_id: str
    request_id: str
    conversation_id: Optional[str]
    sequence: int
    total: int
    raw_text: str
    text: str
    is_final: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "sequence": self.sequence,
            "total": self.total,
            "raw_text": self.raw_text,
            "text": self.text,
            "is_final": self.is_final,
        }


def build_speech_chunks(
    voice_chunks: List[str],
    voice_chunks_raw: Optional[List[str]] = None,
    *,
    request_id: str,
    conversation_id: Optional[str] = None,
) -> List[SpeechChunk]:
    """Pure function - wraps already-computed chunk text (from
    `luno.response_output.build_dual_response()`'s `voice_chunks`/
    `voice_chunks_raw`) into the correlation-aware `SpeechChunk` contract.
    NEVER re-segments or re-normalizes text itself - that stays
    `response_output`'s job exclusively (no duplicate text-normalization/
    segmentation logic, per this sprint's own explicit rule).

    `voice_chunks_raw`, if given, must be the SAME length as
    `voice_chunks` (1:1 aligned, exactly what `build_dual_response()`
    guarantees); if omitted or mismatched in length, `raw_text` safely
    degrades to the cleaned `text` itself for every chunk rather than
    raising or silently misaligning.

    Deterministic and side-effect-free: same inputs always produce the
    same `chunk_id`s/ordering. Returns `[]` for an empty `voice_chunks`
    (an empty response never produces chunks - matches
    `DualResponse.voice_chunks == []` for empty `response_text`)."""
    if not voice_chunks:
        return []
    raw_list = voice_chunks_raw if (voice_chunks_raw and len(voice_chunks_raw) == len(voice_chunks)) else voice_chunks
    total = len(voice_chunks)
    return [
        SpeechChunk(
            chunk_id=f"{request_id}:chunk:{i}",
            request_id=request_id,
            conversation_id=conversation_id,
            sequence=i,
            total=total,
            raw_text=raw_list[i],
            text=text,
            is_final=(i == total - 1),
        )
        for i, text in enumerate(voice_chunks)
    ]


class SpeechCancellationToken:
    """Explicit cancellation-state object for ONE in-flight speech
    request (one call to `FishAudioAdapter._play()`).

    This FORMALIZES (does not replace the mechanism of) what the TTS
    Chunking/Streaming sprint already built as a bare
    `{"stop": Event(), "pause": Event()}` dict per request_id
    (`FishAudioAdapter._chunk_control`) - still exactly two
    `threading.Event`s underneath, still created fresh per `_play()` call
    and discarded when it returns, so a brand-new turn's token is NEVER
    affected by a previous, already-finished turn's token (this is what
    makes "stale request cannot resume after a newer request starts"
    hold, without needing any global/shared flag - see
    `luno/adapters/fish_audio.py`'s own module docstring for the "gap
    between chunks" race this closes).

    Idempotent by construction: `cancel()`/`pause()`/`resume()` are each
    just `Event.set()`/`.clear()` - calling any of them any number of
    times, in any order, including after the token is already in that
    exact state, or after the request has already fully finished, is a
    safe no-op (`threading.Event` itself already guarantees this - no
    extra bookkeeping needed to make cancellation idempotent)."""

    __slots__ = ("request_id", "_stop", "_pause")

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._stop = threading.Event()
        self._pause = threading.Event()

    def cancel(self) -> None:
        """Idempotent - safe to call before synthesis starts, during
        synthesis, between chunks, during playback, or after the request
        has already finished (a no-op in that last case, since nothing
        is checking this token anymore by then)."""
        self._stop.set()
        self._pause.clear()  # never leave a cancelled token stuck "paused"

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def is_cancelled(self) -> bool:
        return self._stop.is_set()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def wait_while_paused(self, poll_interval_s: float = 0.02) -> None:
        """Blocks the CALLING (worker) thread while `is_paused` is True
        and `is_cancelled` is still False. Used BETWEEN chunks (see
        `FishAudioAdapter._play()`) to close the "a pause landed in the
        gap between two chunks" race - at that exact moment nothing is
        inside `client.play()` for `client.pause()` to act on, so the
        worker loop itself must be the thing that waits."""
        while self.is_paused and not self.is_cancelled:
            time.sleep(poll_interval_s)
