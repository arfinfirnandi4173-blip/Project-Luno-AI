"""
incremental_speech.py
======================

LLM Streaming -> Real-Time Speech Pipeline sprint. Bridges the EXISTING,
already-production-wired, real LLM token streaming
(`luno.adapters.llm_manager.LLMManagerAdapter` -> `LLMStreaming`/`LLMChunk`/
`LLMFinished`/`LLMError`/`LLMCancelled` events - see Phase 0 audit in
`docs/change_impact/llm_streaming_speech_pipeline.md`) to the EXISTING
Speech Chunk Queue (`luno.speech_chunk.SpeechChunk`/`FishAudioAdapter`,
from the TTS Chunk Queue & Cancellation sprint), so Luno can start
speaking before the whole LLM reply has finished generating.

    NeedLLMResponse (stream=True, already the default)
            |
    LLMManagerAdapter.stream_chat() (already real, already production)
            |
    llm_streaming / llm_chunk* / llm_finished|llm_error|llm_cancelled
            |
    StreamingSpeechCoordinator (THIS module, new)
            |
    IncrementalSpeechBuffer (THIS module, new - pure buffering/boundary
    detection, reuses `luno.response_output`'s EXISTING sentence splitter
    and `luno.text_normalizer.normalize_for_speech()` - no second
    segmentation/normalization engine)
            |
    SpeechChunk (REUSED, unchanged - luno.speech_chunk)
            |
    SpeakStreamChunk event (THIS sprint's ONE new speech-side event)
            |
    FishAudioAdapter._play_stream() (new method, same client/token/
    terminal-event contract as the existing `_play()`)

NOTHING here duplicates: the LLM streaming contract itself (`LLMStreamChunk`/
`stream_chat()`/`LLMChunk` already exist and are already provider-agnostic -
see Phase 0/1 of the change-impact doc), the sentence-boundary/text-
normalization logic (`luno.response_output._split_into_raw_sentences`/
`_split_long_sentence`/`luno.text_normalizer.normalize_for_speech` are
imported and reused, never reimplemented), the `SpeechChunk` data model
(reused as-is from `luno.speech_chunk`), or the cancellation mechanism
(`SpeechCancellationToken`, reused as-is).

Deliberately DOES NOT run when `luno.config.ENABLE_LLM_TTS_STREAMING` is
False (the default) - every existing, non-streaming behavior is then
completely untouched (see `main_runtime_demo.py::BehaviorTreeModule`'s own
wiring). This module has ZERO effect on anything unless explicitly opted
into, and unless a caller actually starts a turn via
`StreamingSpeechCoordinator.start_turn()`.

RESPONSE-DEPTH-POLICY-SAFE REDESIGN (Voice Pipeline Latency & Semantic
Segmentation - Sprint 3, "Production-Safe LLM -> TTS Streaming
Activation"): Phase 0's audit of THIS sprint found that the ORIGINAL
version of this module (from the "LLM Streaming -> Real-Time Speech
Pipeline" sprint) dispatched EVERY settled sentence to TTS as soon as it
was ready, with NO involvement from `luno.response_output.
build_dual_response()` at all - meaning a streamed turn was spoken in
FULL, uncompressed, regardless of SHORT/NORMAL/DETAILED response-depth
policy, near-duplicate dedup, semantic-unit coherence, or short-sentence
protection. The non-streaming path (`BehaviorTreeModule._speak()`)
DOES apply all of that, via `build_dual_response()`, for every ordinary
turn. This was a genuine, confirmed bypass of response-depth policy -
exactly what this sprint's own Phase 0 instruction says to STOP and fix
before enabling streaming any further ("Do not bypass response-depth
policy... Reuse the existing selection/semantic segmentation logic...
Do NOT create a second selector specifically for streaming").

The fix, implemented entirely in THIS module (no changes needed to
`response_output.py`'s selection logic itself, no changes needed to
`fish_audio.py`'s streaming consumer - `_play_stream()`/
`_play_stream_pipelined()` already read from a live, arbitrarily-timed
queue and need no awareness of when/how chunks are produced):

  1. Only the VERY FIRST settled sentence of a turn is ever dispatched
     to TTS DURING generation - unconditionally speakable at ANY depth/
     budget, because `_select_by_priority()`'s must-keep set (in
     `response_output.py`, unmodified) ALWAYS includes the lead sentence
     (index 0) regardless of depth or budget. Speaking it early is
     therefore PROVABLY never a case of "streaming spoke something the
     non-streaming path would have dropped." The buffer is constructed
     with `min_short_chunk_chars=0` for this purpose specifically, so
     the first dispatched unit is always EXACTLY one raw sentence -
     never a short-sentence merge with sentence 2 - keeping the later
     reconciliation step (below) unambiguous.
  2. Every subsequent LLM delta is still accumulated (`_TurnState.
     full_raw_text`) but NOT dispatched incrementally anymore - Phase 8's
     own reasoning applies directly: correctly applying SHORT/NORMAL's
     budget-based selection requires knowing the total sentence count,
     which streaming, by definition, doesn't have until the LLM
     finishes. Speaking arbitrary further sentences early would risk
     saying MORE than the depth policy allows.
  3. Once `llm_finished` fires, `build_dual_response()` - the SAME,
     single, unmodified selection authority the non-streaming path
     already uses - runs on the COMPLETE accumulated text. Whatever
     content was already spoken as the early first sentence is
     reconciled out of the front of the result (a simple, provably-safe
     prefix strip - see `_on_finished()`'s own docstring for the exact
     alignment guarantee), and the REMAINING selected content is
     dispatched as further `SpeakStreamChunk`s, continuing the SAME
     stream/sequence, ending with the SAME `is_final=True` contract
     `_play_stream()` already expects.

Net effect: a streamed turn now gets a genuine "time to first audio"
latency win for its FIRST sentence (spoken as soon as it settles, before
the LLM finishes generating the rest), while every sentence after that
is selected/compressed/coherence-checked EXACTLY as the non-streaming
path would - response-depth policy, semantic-unit coherence, and
short-sentence protection are never bypassed. This is a smaller latency
win than the original (pre-Sprint-3) design's "speak everything
immediately" behavior measured, but that original number was only
achievable BY bypassing depth policy - not a legitimate baseline to
preserve. See `docs/change_impact/llm_tts_streaming_activation.md` for
the measured before/after numbers under this corrected design.

`max_pending_chunks`/the backpressure-draining machinery below
(`held_chunks`/`pending_dispatched`/`_drain_held()`/`_on_chunk_played()`)
is RETAINED for backward-compatible construction (existing callers/tests
still pass `max_pending_chunks=...`) but no longer meaningfully bounds
anything under this redesign: only ONE chunk is ever dispatched during
generation (nothing to bound), and the post-selection final batch
deliberately bypasses the cap - exactly the same reasoning
`_dispatch_final()`'s own pre-Sprint-3 docstring already established for
a turn's terminal batch ("the LLM has already finished; nothing more is
ever coming"), now simply applying to the WHOLE post-selection remainder
instead of just its tail. Concurrent-synthesis bounding still happens
where it always has - `FishAudioAdapter`'s own one-slot prefetch queue
(`_play_stream_pipelined()`), unmodified.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .response_output import _Chunk, _split_into_raw_sentences, _split_long_sentence, build_dual_response
from .response_policy import DEPTH_NORMAL, ResponsePolicy
from .core.utils import log
from .speech_chunk import SpeechChunk
from .text_normalizer import normalize_for_speech

#: Mirrors `luno.config.VOICE_CHUNK_MAX_CHARS`'s own default - ceiling/
#: safety-net only (see `response_output.py`'s own docstring for why this
#: is never a packing target). Callers normally pass the configured value
#: explicitly; this module never reads config/env itself (same "pure
#: function of its arguments" discipline `response_output.py` already
#: follows).
DEFAULT_MAX_BUFFER_CHARS = 220

#: A settled sentence shorter than this (cleaned-text char length) is held
#: back and merged with the NEXT settled sentence instead of being flushed
#: on its own - "jangan menghasilkan chunk 1-2 kata tanpa alasan" (Phase 5).
DEFAULT_MIN_SHORT_CHUNK_CHARS = 12

#: Below this length, the still-open (not-yet-sentence-terminated) tail is
#: never force-split even if it technically contains a comma/semicolon -
#: avoids chopping an ordinary short clause just because it happens to
#: contain a comma early on.
_MIN_FORCE_SPLIT_CHARS = 40


class IncrementalSpeechBuffer:
    """Pure-ish, single-turn, single-threaded buffering + natural-
    sentence-boundary detector. NOT thread-safe by itself - the caller
    (`StreamingSpeechCoordinator`) only ever calls it from the event bus's
    own delivery thread for a given request_id, exactly like every other
    per-turn ad-hoc event handler already in this codebase
    (`main_runtime_demo.py::BehaviorTreeModule._generate_reply()`'s own
    `_on_ok`/`_on_err`/`_on_depth` closures).

    Boundary priority (Phase 5 of the sprint brief), reusing EXISTING
    machinery at every step:
      1. sentence-ending punctuation (`.`/`!`/`?`/`...`) - via
         `luno.response_output._split_into_raw_sentences`, re-run on the
         growing buffer each `feed()` call; every sentence EXCEPT the
         last (which might still be growing) is "settled" and flushable.
      2. paragraph boundary - already handled BY `_split_into_raw_sentences`
         itself (it splits on blank lines internally).
      3/4/5. strong punctuation (`;`/`:`) / comma-if-long-enough / a hard
         maximum-length cutoff - all THREE handled by ONE reused call to
         `luno.response_output._split_long_sentence()` once the still-open
         tail exceeds `max_buffer_chars` (that function ALREADY implements
         exactly this cascade: clause boundaries first, whitespace
         boundary only as a last resort, never mid-word).
      6. final LLM response - `flush_final()` always flushes whatever
         remains, never drops content, regardless of whether it ever found
         a clean sentence boundary.
    """

    def __init__(
        self,
        *,
        request_id: str,
        conversation_id: Optional[str] = None,
        max_buffer_chars: int = DEFAULT_MAX_BUFFER_CHARS,
        min_short_chunk_chars: int = DEFAULT_MIN_SHORT_CHUNK_CHARS,
        language: Optional[str] = None,
    ) -> None:
        self.request_id = request_id
        self.conversation_id = conversation_id
        self._max_buffer_chars = max(1, max_buffer_chars)
        self._min_short_chunk_chars = max(0, min_short_chunk_chars)
        self._language = language
        self._raw_buffer = ""
        self._pending_short_raw: Optional[str] = None
        self._next_sequence = 0
        self._done = False

    @property
    def opened(self) -> bool:
        """True once at least one `SpeechChunk` has ever been produced by
        this buffer - lets the coordinator know whether a Fish Audio
        stream was actually opened (and therefore whether a synthetic
        close marker is needed at `flush_final()` time if nothing new
        comes out of that final call)."""
        return self._next_sequence > 0

    # -- public API -----------------------------------------------------------

    def feed(self, delta: str) -> List[SpeechChunk]:
        """Appends `delta` (the newly arrived LLM token(s)) to the
        internal buffer and returns zero or more newly-COMPLETE
        `SpeechChunk`s, in order. Never emits an empty chunk. Safe to
        call with `""`/`None` (no-op, returns `[]`)."""
        if self._done or not delta:
            return []
        self._raw_buffer += delta
        settled = self._settle_from_sentence_boundaries()
        settled += self._settle_from_length_threshold()
        return self._sentences_to_chunks(settled, is_final_batch=False)

    def flush_final(self) -> List[SpeechChunk]:
        """Called exactly once, when the LLM stream has truly finished
        (successfully). Flushes EVERYTHING remaining (including a
        still-open, punctuation-less tail, and any held-back short
        sentence) - final LLM output always flushes the remaining buffer
        (Phase 5, rule 6). The LAST chunk this method returns (or the
        last chunk this buffer ever produced, if this call itself yields
        nothing new) is marked `is_final=True` with its `total` set to
        the actual, now-known total chunk count. Idempotent - a second
        call returns `[]`."""
        if self._done:
            return []
        self._done = True
        raw_chunks = _split_into_raw_sentences(self._raw_buffer)
        self._raw_buffer = ""
        chunks = self._sentences_to_chunks(raw_chunks, is_final_batch=True)
        if self._pending_short_raw:
            # Nothing ever arrived to merge the last held-back short
            # sentence into - flush it alone rather than silently
            # dropping content.
            trailing = self._make_chunk(self._pending_short_raw)
            self._pending_short_raw = None
            if trailing is not None:
                chunks.append(trailing)
        if chunks:
            last = chunks[-1]
            chunks[-1] = SpeechChunk(
                chunk_id=last.chunk_id, request_id=last.request_id,
                conversation_id=last.conversation_id, sequence=last.sequence,
                total=last.sequence + 1, raw_text=last.raw_text, text=last.text,
                is_final=True,
            )
        return chunks

    def make_close_marker(self) -> SpeechChunk:
        """A zero-text, `is_final=True` chunk - used by the coordinator
        ONLY when a stream was already opened (`self.opened`) but
        `flush_final()` produced no new chunks (everything had already
        been flushed incrementally via `feed()`), so `FishAudioAdapter`
        still gets an explicit "no more chunks coming" signal. Never
        played (empty `text` - see `FishAudioAdapter._play_stream()`)."""
        seq = self._next_sequence
        self._next_sequence += 1
        return SpeechChunk(
            chunk_id=f"{self.request_id}:chunk:{seq}", request_id=self.request_id,
            conversation_id=self.conversation_id, sequence=seq, total=seq + 1,
            raw_text="", text="", is_final=True,
        )

    # -- internal ---------------------------------------------------------------

    def _settle_from_sentence_boundaries(self) -> List[_Chunk]:
        raw_chunks = _split_into_raw_sentences(self._raw_buffer)
        if len(raw_chunks) <= 1:
            return []
        settled, tail = raw_chunks[:-1], raw_chunks[-1:]
        self._raw_buffer = self._tail_after(settled)
        return settled

    def _tail_after(self, settled: List[_Chunk]) -> str:
        """Returns whatever of `self._raw_buffer` remains AFTER the given
        `settled` chunks, preserving the ORIGINAL whitespace between/after
        them exactly (never a lossy re-join of the split fragments) - a
        later delta that doesn't itself start with a leading space (real
        LLM token streams usually DO attach the space to the following
        token, but this must not silently glue two words together even
        when a provider doesn't). Falls back to a plain space-joined
        reconstruction (the previous, lossy behavior) only if a settled
        chunk's exact text cannot be located in order - should not happen
        in practice (`_split_into_raw_sentences`'s own chunks are always
        left-to-right substrings of its input), but never crashes if it
        somehow does."""
        return self._slice_after(self._raw_buffer, [c.raw for c in settled])

    @staticmethod
    def _slice_after(original: str, settled_texts: List[str]) -> str:
        """Shared cursor-based "find each settled piece in order, return
        whatever is left after the last one" helper - used both for
        sentence-boundary settling and for the length-threshold force-
        split below, so NEITHER path ever silently drops the original
        whitespace between the last settled piece and whatever text
        follows it (the bug this exists to prevent: a reconstructed/
        rejoined tail with no trailing space, glued directly onto the
        NEXT delta with no leading space of its own, producing something
        like "katakata" out of two separate words)."""
        cursor = 0
        for text in settled_texts:
            idx = original.find(text, cursor)
            if idx == -1:
                # Should not happen (`text` was derived FROM `original`
                # by the caller) - the settled pieces the caller already
                # captured are unaffected either way; degrade to treating
                # the buffer as fully consumed (never raises) rather than
                # risking a wrong/duplicated tail.
                return ""
            cursor = idx + len(text)
        return original[cursor:]

    def _settle_from_length_threshold(self) -> List[_Chunk]:
        """Priorities 3/4/5 (strong punctuation / comma-if-long / hard
        max-length cutoff) - all via ONE reused call to
        `_split_long_sentence()`, only once the still-open tail has grown
        past both `_MIN_FORCE_SPLIT_CHARS` and `max_buffer_chars`. Uses
        `_slice_after()` (not `_split_long_sentence()`'s own returned
        "remaining" piece directly) to keep the ORIGINAL trailing
        whitespace of the still-open buffer intact - see that helper's
        own docstring."""
        tail = self._raw_buffer
        if len(tail) < max(self._max_buffer_chars, _MIN_FORCE_SPLIT_CHARS):
            return []
        pieces = _split_long_sentence(tail, self._max_buffer_chars)
        if len(pieces) <= 1:
            return []
        settled_pieces = pieces[:-1]
        self._raw_buffer = self._slice_after(tail, settled_pieces)
        return [_Chunk(p, False) for p in settled_pieces if p.strip()]

    def _sentences_to_chunks(self, raw_chunks: List[_Chunk], *, is_final_batch: bool) -> List[SpeechChunk]:
        out: List[SpeechChunk] = []
        for c in raw_chunks:
            cleaned = normalize_for_speech(c.raw, language=self._language).strip()
            if not cleaned:
                continue  # never emit an empty chunk
            if self._pending_short_raw:
                merged_raw = f"{self._pending_short_raw} {c.raw}".strip()
                self._pending_short_raw = None
                chunk = self._make_chunk(merged_raw)
                if chunk is not None:
                    out.append(chunk)
                continue
            if len(cleaned) < self._min_short_chunk_chars and not is_final_batch:
                # Hold back - Phase 5: "jangan menghasilkan chunk 1-2 kata
                # tanpa alasan" - merge with whatever settles next instead
                # of speaking a bare "Iya." as its own isolated utterance.
                self._pending_short_raw = c.raw
                continue
            chunk = self._make_chunk(c.raw)
            if chunk is not None:
                out.append(chunk)
        return out

    def _make_chunk(self, raw: str) -> Optional[SpeechChunk]:
        cleaned = normalize_for_speech(raw, language=self._language).strip()
        if not cleaned:
            return None
        seq = self._next_sequence
        self._next_sequence += 1
        return SpeechChunk(
            chunk_id=f"{self.request_id}:chunk:{seq}", request_id=self.request_id,
            conversation_id=self.conversation_id, sequence=seq, total=-1,
            raw_text=raw, text=cleaned, is_final=False,
        )


# ============================================================================
# StreamingSpeechCoordinator - wires LLMChunk (existing) to SpeakStreamChunk
# (new, additive) through IncrementalSpeechBuffer + bounded backpressure.
# ============================================================================

@dataclass
class _TurnState:
    request_id: str
    conversation_id: Optional[str]
    buffer: IncrementalSpeechBuffer
    subscriptions: List[Any] = field(default_factory=list)
    #: chunks flushed by the buffer but not yet published to Fish Audio
    #: (backpressure holding area - Phase 10: "prefer buffering text
    #: daripada menghasilkan unlimited audio jobs"). Sprint 3: retained
    #: for structural/API compatibility but never populated under the
    #: redesign - see module docstring's "RESPONSE-DEPTH-POLICY-SAFE
    #: REDESIGN" section.
    held_chunks: List[SpeechChunk] = field(default_factory=list)
    #: chunks published to Fish Audio, not yet confirmed finished
    #: (`speech_chunk_playback_finished`) - the live backpressure counter.
    #: Sprint 3: no longer meaningfully incremented (see above).
    pending_dispatched: int = 0
    opened_stream: bool = False
    completed: bool = False
    cancelled: bool = False
    failed: bool = False
    #: Sprint 3 (Production-Safe LLM -> TTS Streaming Activation) fields
    #: below - see module docstring's "RESPONSE-DEPTH-POLICY-SAFE
    #: REDESIGN" section for the full design.
    #:
    #: Exact concatenation of every `delta` this turn's `llm_chunk`
    #: events have carried, in arrival order - the single source of
    #: truth `build_dual_response()` runs on once `llm_finished` fires
    #: (never the buffer's own internal, partially-consumed state).
    full_raw_text: str = ""
    #: This turn's resolved response-depth string (`"short"`/`"normal"`/
    #: `"detailed"`), learned via the SAME `response_depth_assigned`
    #: event `BehaviorTreeModule._generate_reply()`'s own `_on_depth`
    #: closure already subscribes to - never recomputed here.
    depth: Optional[str] = None
    #: Sibling of `depth` - `ResponsePolicy.explicit`, ALSO carried by
    #: `response_depth_assigned` (Sprint 3 - see that event's own publish
    #: site in `main_runtime_demo.py` for why: without it, an explicit
    #: "jelaskan semuanya secara detail" request would still lose content
    #: to budget-based compression here too, exactly like the
    #: non-streaming path did before that fix).
    explicit: bool = False
    #: Voice Output Mode sprint - sibling of `depth`/`explicit` above,
    #: ALSO carried by `response_depth_assigned` (same event, same
    #: correlation mechanism, one more field - see that event's own
    #: publish site in `main_runtime_demo.py`). `None` until
    #: `_on_depth_assigned()` fires; `build_dual_response()`'s own
    #: `resolve_voice_output_mode()` already treats `None` as "SHORT",
    #: so a turn that streams before this arrives (should not happen -
    #: `start_turn()` subscribes before `NeedLLMResponse` is published,
    #: same ordering guarantee `depth` already relies on) still behaves
    #: exactly like today, never crashes.
    voice_output_mode: Optional[str] = None
    #: True once the one, always-safe early sentence has been dispatched
    #: - after this flips True, no further per-chunk dispatch happens
    #: until `_on_finished()`'s post-selection reconciliation.
    first_unit_dispatched: bool = False
    #: The exact RAW text of that one early-dispatched sentence - used
    #: by `_on_finished()` to strip the already-spoken prefix off the
    #: front of the full-text selection result.
    first_unit_raw_text: Optional[str] = None
    #: True once at least one real (non-empty-text) `SpeechChunk` has
    #: been published for this turn - the authoritative "was anything
    #: actually spoken via streaming" flag this sprint's `_on_finished()`/
    #: `_on_error()` use in place of the old `buffer.opened` check (that
    #: property no longer reliably reflects "was dispatched", since the
    #: buffer keeps settling sentences internally after the coordinator
    #: stops forwarding them).
    any_dispatched: bool = False
    #: Phase 11 - latency observability (log-only, no raw conversation
    #: text - matches this codebase's existing "don't log transcript
    #: content" convention already used by `FishAudioAdapter`).
    request_received_at: float = field(default_factory=time.time)
    llm_stream_started_at: Optional[float] = None
    first_token_at: Optional[float] = None
    first_sentence_ready_at: Optional[float] = None
    first_chunk_dispatched_at: Optional[float] = None
    llm_completed_at: Optional[float] = None
    speech_completed_at: Optional[float] = None
    ttfa_logged: bool = False


class StreamingSpeechCoordinator:
    """Owns, per active `request_id`, the ad-hoc event-bus subscriptions
    that feed `IncrementalSpeechBuffer` from the EXISTING `llm_streaming`/
    `llm_chunk`/`llm_finished`/`llm_error`/`llm_cancelled` events, applies
    bounded backpressure, and publishes `SpeakStreamChunk` events to the
    EXISTING `FishAudioAdapter` (extended with a streaming-aware worker -
    see `luno/adapters/fish_audio.py::_play_stream()`).

    Deliberately mirrors `BehaviorTreeModule._generate_reply()`'s own
    long-standing "subscribe for this ONE request_id, unsubscribe when
    done" pattern - no new cross-module wiring convention is introduced.
    Multiple turns are supported (state keyed by request_id, cleaned up
    on completion/cancellation/error) but this codebase's runtime shape
    only ever has one active turn at a time in practice (see
    `ARCHITECTURE_GUARD.md` §2)."""

    def __init__(
        self,
        event_bus: Any,
        *,
        max_pending_chunks: int = 4,
        max_buffer_chars: int = DEFAULT_MAX_BUFFER_CHARS,
        publish_stream_chunk: Optional[Callable[[str, Optional[str], Dict[str, Any]], None]] = None,
    ) -> None:
        self._event_bus = event_bus
        self._max_pending_chunks = max(1, max_pending_chunks)
        self._max_buffer_chars = max_buffer_chars
        #: Injectable for tests / for a caller that wants to publish
        #: `SpeakStreamChunk` itself through a different bus - defaults to
        #: publishing a real `SpeakStreamChunk` on `event_bus`.
        self._publish_stream_chunk = publish_stream_chunk or self._default_publish
        self._turns: Dict[str, _TurnState] = {}
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start_turn(self, request_id: str, conversation_id: Optional[str], *, language: Optional[str] = None) -> None:
        """Begins listening for this turn's LLM stream. Must be called
        BEFORE `NeedLLMResponse` is published for this `request_id` (same
        ordering requirement `_generate_reply()`'s own subscribe-then-
        publish pattern already follows), so no `llm_chunk` can possibly
        arrive before the subscription exists."""
        if self._event_bus is None:
            return
        buffer = IncrementalSpeechBuffer(
            request_id=request_id, conversation_id=conversation_id,
            max_buffer_chars=self._max_buffer_chars, language=language,
            # Sprint 3: 0, not the module default (12) - guarantees the
            # one early-dispatched sentence is ALWAYS exactly one raw
            # sentence, never a short-sentence merge with the sentence
            # after it. This keeps `_on_finished()`'s prefix-reconciliation
            # against `build_dual_response()`'s own output unambiguous -
            # see that method's docstring. Short-sentence PROTECTION
            # itself (Sprint 2's `_has_confirmation_lead()` etc.) still
            # fully applies later, inside `build_dual_response()`, for
            # everything after this first sentence.
            min_short_chunk_chars=0,
        )
        state = _TurnState(request_id=request_id, conversation_id=conversation_id, buffer=buffer)
        subs = [
            self._event_bus.subscribe("llm_streaming", self._make_handler(request_id, self._on_stream_started)),
            self._event_bus.subscribe("llm_chunk", self._make_handler(request_id, self._on_chunk)),
            self._event_bus.subscribe("llm_finished", self._make_handler(request_id, self._on_finished)),
            self._event_bus.subscribe("llm_error", self._make_handler(request_id, self._on_error)),
            self._event_bus.subscribe("llm_cancelled", self._make_handler(request_id, self._on_cancelled)),
            self._event_bus.subscribe("speech_chunk_playback_finished", self._make_handler(request_id, self._on_chunk_played, match_field="request_id")),
            self._event_bus.subscribe("speech_playback_started", self._make_handler(request_id, self._on_speech_playback_started)),
            self._event_bus.subscribe("speech_playback_finished", self._make_handler(request_id, self._on_speech_playback_done)),
            self._event_bus.subscribe("speech_playback_cancelled", self._make_handler(request_id, self._on_speech_playback_done)),
            # Sprint 3 - learns this turn's resolved response-depth via
            # the SAME event `BehaviorTreeModule._generate_reply()`'s own
            # `_on_depth` closure already subscribes to (published once by
            # `PlannerBridgeModule` after its own `compute_response_policy()`
            # call). Never recomputed/re-detected here.
            self._event_bus.subscribe("response_depth_assigned", self._make_handler(request_id, self._on_depth_assigned)),
        ]
        state.subscriptions = subs
        with self._lock:
            self._turns[request_id] = state
            # Defensive bound (mirrors `BehaviorTreeModule._cancelled_request_ids`'s
            # own `deque(maxlen=64)`) - a turn that never reaches `_speak()`'s
            # `forget_turn()` call (e.g. an abandoned/never-finished
            # request) must not let this dict grow unboundedly across a
            # long session. Evicts the OLDEST entry only - never the one
            # just inserted.
            if len(self._turns) > 64:
                oldest_id = next(iter(self._turns))
                if oldest_id != request_id:
                    stale = self._turns.pop(oldest_id)
                    for sub in stale.subscriptions:
                        try:
                            self._event_bus.unsubscribe(sub)
                        except Exception:
                            pass

    def cancel_turn(self, request_id: str) -> None:
        """Idempotent. Stops feeding/publishing further chunks for this
        turn immediately - does NOT itself touch `FishAudioAdapter`'s own
        playback (that is already, separately, driven by `StopPlayback`/
        `llm_cancelled` reaching `FishAudioAdapter` directly, unchanged -
        see that module). This only prevents the COORDINATOR from
        producing/publishing any MORE chunks for an already-cancelled
        turn ("cancel pending text chunking" - Phase 3)."""
        state = self._pop_if_present(request_id, mark_cancelled=True)
        if state is not None:
            state.held_chunks.clear()

    def wait_until_settled(self, request_id: str, timeout_s: float = 2.0) -> None:
        """`BehaviorTreeModule._generate_reply()`'s own `assistant_response`/
        `llm_error` wait and this coordinator's `llm_finished`/`llm_error`
        handling are two INDEPENDENT event-bus subscriptions of events the
        LLM adapter publishes back-to-back for the same completion - the
        event bus's dispatcher does not guarantee one is fully delivered/
        processed before the other even starts (each subscriber may run
        on its own dispatcher worker thread). Without this wait,
        `_speak()` could call `forget_turn()` a few microseconds BEFORE
        `_on_finished()` gets to run `flush_final()` for the SAME turn,
        silently dropping the still-buffered trailing sentence and
        leaving `FishAudioAdapter`'s stream waiting on a close marker
        that would then never arrive (until its own 30s idle-timeout
        safety net kicks in). Bounded, short poll only - in the normal
        (non-racing) case `state.completed` is already `True` by the time
        this is called and it returns immediately; a `request_id` that
        was never `start_turn()`-ed (streaming disabled, or a turn this
        coordinator never saw) also returns immediately (`state is None`
        - nothing to wait for)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = self._get(request_id)
            if state is None or state.completed or state.cancelled or state.failed:
                return
            time.sleep(0.005)

    def is_turn_streamed_and_completed(self, request_id: str) -> bool:
        """True once this turn's speech was fully, successfully spoken
        via streaming - `BehaviorTreeModule._speak()` uses this to skip
        publishing its own, would-be-duplicate `SpeakRequest` (Phase 9:
        "jangan membuat dua jalur audio yang dapat berbicara bersamaan")."""
        with self._lock:
            state = self._turns.get(request_id)
            return bool(state and state.completed and not state.cancelled and not state.failed)

    def forget_turn(self, request_id: str) -> None:
        with self._lock:
            self._turns.pop(request_id, None)

    # -- event handlers (each pre-bound to exactly one request_id) --------------

    def _make_handler(self, request_id: str, fn: Callable[[str, Any], None], *, match_field: str = "request_id"):
        def _handler(event: Any) -> None:
            if event.get(match_field) != request_id:
                return
            fn(request_id, event)
        return _handler

    def _on_stream_started(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None:
            return
        state.llm_stream_started_at = time.time()
        log(f"StreamingSpeechStarted request_id={request_id} conversation_id={state.conversation_id}", "incremental_speech")

    def _on_depth_assigned(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None:
            return
        state.depth = event.get("depth")
        state.explicit = bool(event.get("explicit", False))
        state.voice_output_mode = event.get("voice_output_mode")

    def _on_chunk(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None or state.cancelled or state.failed:
            return
        delta = event.get("delta") or ""
        if not delta:
            return  # Phase 13 scenario 4 - empty partials ignored
        now = time.time()
        if state.first_token_at is None:
            state.first_token_at = now
            # Phase 11 latency observability - TTFT (time to first token).
            # Never logs the token/delta TEXT itself, only timing (matches
            # `FishAudioAdapter`'s own established "no raw conversation
            # text in logs" convention).
            log(f"TTFT request_id={request_id} ttft_s={round(now - state.request_received_at, 3)}", "incremental_speech")
        # Sprint 3 - the single source of truth `_on_finished()` runs
        # `build_dual_response()` on, once the full reply is known. Fed
        # unconditionally, regardless of dispatch phase (see below).
        state.full_raw_text += delta
        new_chunks = state.buffer.feed(delta)
        if new_chunks and state.first_sentence_ready_at is None:
            state.first_sentence_ready_at = now
            log(f"TTFS request_id={request_id} ttfs_s={round(now - state.request_received_at, 3)}", "incremental_speech")
        # Sprint 3 (RESPONSE-DEPTH-POLICY-SAFE REDESIGN - see module
        # docstring): only the VERY FIRST settled sentence of this turn
        # is ever dispatched during generation - provably safe at any
        # depth/budget (`_select_by_priority()`'s must-keep set always
        # includes the lead sentence). Everything settled after that is
        # intentionally NOT dispatched here - `full_raw_text` already
        # captured it; the correct remaining content can only be known
        # once the complete response is selected in `_on_finished()`.
        if new_chunks and not state.first_unit_dispatched:
            first = new_chunks[0]
            state.first_unit_dispatched = True
            state.first_unit_raw_text = first.raw_text
            self._dispatch_first(state, first)

    def _on_speech_playback_started(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None or state.llm_stream_started_at is None:
            return
        # Phase 11 - TTFA (time to first audio). Guarded so this only
        # logs once even if `speech_playback_started` were somehow
        # delivered more than once (defensive - the adapter contract
        # already guarantees exactly one).
        if not state.ttfa_logged:
            log(f"TTFA request_id={request_id} ttfa_s={round(time.time() - state.request_received_at, 3)}", "incremental_speech")
            state.ttfa_logged = True

    def _on_speech_playback_done(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None:
            return
        if state.speech_completed_at is None:
            state.speech_completed_at = time.time()
            total_s = round(state.speech_completed_at - state.request_received_at, 3)
            log(f"SpeechCompleted request_id={request_id} total_response_latency_s={total_s}", "incremental_speech")

    def _on_finished(self, request_id: str, event: Any) -> None:
        """Sprint 3 (RESPONSE-DEPTH-POLICY-SAFE REDESIGN - see module
        docstring). Runs `build_dual_response()` - the SAME, single,
        unmodified selection authority `BehaviorTreeModule._speak()`
        already uses for every non-streaming turn - on the COMPLETE
        accumulated `full_raw_text`, then reconciles out whatever was
        already spoken early (`state.first_unit_raw_text`) before
        dispatching the remainder.

        ALIGNMENT GUARANTEE the reconciliation below relies on: the one
        early-dispatched sentence is always EXACTLY sentence index 0 of
        the original text (the buffer was built with
        `min_short_chunk_chars=0` specifically so it is never a merge of
        two sentences - see `start_turn()`). `build_dual_response()`'s
        own selection (`_dedupe()` then `_select_by_priority()`,
        unmodified) can never reorder sentences and always keeps index 0
        (the must-keep lead sentence) - and `_dedupe()` can never remove
        index 0 either, since nothing precedes it to be a near-duplicate
        of. So the first entry of `dual.voice_chunks_raw` is GUARANTEED
        to either equal, or begin with, `state.first_unit_raw_text`
        (equal in the overwhelming common case; "begins with" only when
        `_group_sentences_into_chunk_pairs()`'s list-item grouping
        happens to fold sentence 0 together with sentence 1 into one
        playback-sized chunk). If that guarantee is ever somehow violated
        (defensive-only branch, should not be reachable), this method
        does NOT guess - it stops after the already-spoken sentence
        rather than risk duplicating or dropping content (hard
        constraint: never duplicate audio)."""
        state = self._get(request_id)
        if state is None or state.cancelled or state.failed:
            return
        state.llm_completed_at = time.time()
        log(f"LLMCompleted request_id={request_id} llm_total_s={round(state.llm_completed_at - state.request_received_at, 3)}", "incremental_speech")

        full_text = state.full_raw_text
        if not full_text.strip():
            # Nothing ever arrived (e.g. an empty final response) - let
            # `_speak()`'s normal fallback handle it, exactly as if
            # streaming had never been enabled for this turn.
            state.completed = False
            return

        # Sprint 3 - wrapped in a minimal `ResponsePolicy` (not a bare
        # depth string) so `build_dual_response()`'s own "explicit
        # DETAILED skips compression entirely" rule actually applies -
        # see `_TurnState.explicit`'s own docstring.
        resolved_policy = ResponsePolicy(depth=state.depth or DEPTH_NORMAL, score=0, reasons=[], explicit=state.explicit)
        dual = build_dual_response(
            full_text, resolved_policy, language=state.buffer._language,
            request_id=request_id, max_chunk_chars=self._max_buffer_chars,
            voice_output_mode=state.voice_output_mode,
        )
        raw_chunks = list(dual.voice_chunks_raw) if dual.voice_chunks_raw else ([dual.voice_text] if dual.voice_text else [])
        cleaned_chunks = list(dual.voice_chunks) if dual.voice_chunks else ([dual.voice_text] if dual.voice_text else [])
        if state.first_sentence_ready_at is None:
            state.first_sentence_ready_at = state.llm_completed_at
            log(f"TTFS request_id={request_id} ttfs_s={round(state.first_sentence_ready_at - state.request_received_at, 3)}", "incremental_speech")

        remaining_raw, remaining_cleaned = self._reconcile_remaining(state, raw_chunks, cleaned_chunks)
        if remaining_raw is None:
            # Defensive-only: the alignment guarantee above didn't hold.
            # Never risk duplicate/dropped content - stop cleanly with
            # just the already-spoken first sentence, loudly logged for
            # investigation.
            log(
                f"StreamingSelectionMismatch request_id={request_id} - already-spoken prefix did not align with "
                f"the full-text selection result; stopping without further dispatch to avoid duplicate/dropped audio",
                "incremental_speech",
            )
            if state.first_unit_dispatched:
                self._publish_stream_chunk(request_id, state.conversation_id, state.buffer.make_close_marker().to_dict())
            state.completed = state.any_dispatched
            return

        self._dispatch_remaining(state, remaining_raw, remaining_cleaned)
        state.completed = state.any_dispatched
        # Held state is intentionally kept (not popped) until
        # `is_turn_streamed_and_completed()`/`forget_turn()` is called by
        # `_speak()` - avoids a race where `_speak()` asks "was this
        # streamed?" microseconds after `llm_finished` and finds nothing.

    def _reconcile_remaining(
        self, state: "_TurnState", raw_chunks: List[str], cleaned_chunks: List[str],
    ) -> "tuple[Optional[List[str]], Optional[List[str]]]":
        """Strips the already-spoken first sentence off the front of
        `raw_chunks`/`cleaned_chunks` (both from `build_dual_response()`,
        in order, same length) - see `_on_finished()`'s own docstring for
        the exact alignment guarantee this relies on. Returns
        `(None, None)` if that guarantee is ever violated (caller must
        then stop, never guess)."""
        if not state.first_unit_dispatched or not raw_chunks:
            return raw_chunks, cleaned_chunks
        spoken = (state.first_unit_raw_text or "").strip()
        if not spoken:
            return raw_chunks, cleaned_chunks
        first_raw = raw_chunks[0].strip()
        if first_raw == spoken:
            return raw_chunks[1:], cleaned_chunks[1:]
        if first_raw.startswith(spoken):
            tail_raw = first_raw[len(spoken):].strip()
            if not tail_raw:
                return raw_chunks[1:], cleaned_chunks[1:]
            tail_cleaned = normalize_for_speech(tail_raw, language=state.buffer._language).strip()
            new_raw = [tail_raw] + raw_chunks[1:]
            new_cleaned = ([tail_cleaned] if tail_cleaned else []) + cleaned_chunks[1:]
            return new_raw, new_cleaned
        return None, None

    def _dispatch_first(self, state: "_TurnState", chunk: SpeechChunk) -> None:
        """Publishes the ONE early, provably-safe sentence - see
        `_on_chunk()`'s own call site."""
        if not chunk.text:
            return
        state.any_dispatched = True
        state.opened_stream = True
        if state.first_chunk_dispatched_at is None:
            state.first_chunk_dispatched_at = time.time()
        self._publish_stream_chunk(state.request_id, state.conversation_id, chunk.to_dict())

    def _dispatch_remaining(self, state: "_TurnState", remaining_raw: List[str], remaining_cleaned: List[str]) -> None:
        """Publishes whatever `_reconcile_remaining()` determined still
        needs to be spoken, as further `SpeechChunk`s continuing this
        turn's SAME sequence numbering, ending with `is_final=True` -
        exactly the contract `FishAudioAdapter._play_stream()` already
        expects, regardless of whether these chunks arrive one-by-one
        during generation or all together here. Bypasses
        `max_pending_chunks` deliberately - by this point the LLM has
        already finished; nothing more is ever coming for this
        request_id (same reasoning this method's pre-Sprint-3
        predecessor, `_dispatch_final()`, already established for a
        turn's terminal batch - now applying to the whole post-selection
        remainder). If there is nothing left to dispatch but something
        WAS already spoken early, sends a close marker so the stream
        ends cleanly instead of idling out."""
        seq = state.buffer._next_sequence
        pairs = [(r, c) for r, c in zip(remaining_raw, remaining_cleaned) if c and c.strip()]
        if not pairs:
            if state.first_unit_dispatched:
                self._publish_stream_chunk(state.request_id, state.conversation_id, state.buffer.make_close_marker().to_dict())
            return
        final_seq = seq + len(pairs) - 1
        for i, (raw, cleaned) in enumerate(pairs):
            is_final = i == len(pairs) - 1
            chunk = SpeechChunk(
                chunk_id=f"{state.request_id}:chunk:{seq}", request_id=state.request_id,
                conversation_id=state.conversation_id, sequence=seq,
                total=(final_seq + 1) if is_final else -1,
                raw_text=raw, text=cleaned, is_final=is_final,
            )
            seq += 1
            state.any_dispatched = True
            state.opened_stream = True
            if state.first_chunk_dispatched_at is None:
                state.first_chunk_dispatched_at = time.time()
            self._publish_stream_chunk(state.request_id, state.conversation_id, chunk.to_dict())
        state.buffer._next_sequence = seq

    def _on_error(self, request_id: str, event: Any) -> None:
        """Phase 12: LLM failure AFTER partial text must never silently
        look like a complete response. Marks this turn `failed` (never
        `completed`) so `_speak()`'s normal error-apology fallback runs
        exactly as it already does today for the non-streaming path -
        whatever partial audio already reached Fish Audio is allowed to
        finish playing naturally (not violently cut off mid-word); any
        text this turn had accumulated but never dispatched is simply
        discarded (it would have continued a sentence/response that will
        now never be completed by the LLM - Sprint 3: `build_dual_response()`
        is deliberately NEVER run on a known-partial/truncated
        `full_raw_text`, since that could select different content than
        a genuinely complete reply would). If the one early sentence WAS
        already dispatched, sends an explicit close marker so
        `FishAudioAdapter._play_stream()` ends cleanly right away instead
        of waiting on its own idle-timeout safety net."""
        opened = False
        with self._lock:
            existing = self._turns.get(request_id)
            if existing is not None:
                opened = existing.first_unit_dispatched
        state = self._pop_if_present(request_id, mark_failed=True)
        if state is not None:
            state.held_chunks.clear()
            if opened:
                self._publish_stream_chunk(request_id, state.conversation_id, state.buffer.make_close_marker().to_dict())

    def _on_cancelled(self, request_id: str, event: Any) -> None:
        self.cancel_turn(request_id)

    def _on_chunk_played(self, request_id: str, event: Any) -> None:
        state = self._get(request_id)
        if state is None:
            return
        state.pending_dispatched = max(0, state.pending_dispatched - 1)
        self._drain_held(state)

    # -- backpressure / dispatch --------------------------------------------------

    def _dispatch_or_hold(self, state: _TurnState, chunks: List[SpeechChunk]) -> None:
        state.held_chunks.extend(chunks)
        self._drain_held(state)

    def _dispatch_final(self, state: _TurnState, chunks: List[SpeechChunk]) -> None:
        """Called only from `_on_finished()` for a turn's terminal batch
        (whatever was still backpressure-held PLUS this final flush) -
        dispatched immediately, bypassing the normal `max_pending_chunks`
        cap `_dispatch_or_hold()`/`_drain_held()` enforce. Backpressure
        (Phase 10) exists to stop an ONGOING stream from generating audio
        jobs faster than TTS can speak them - it does not apply to a
        provably-final, already-bounded batch (the LLM has already
        finished; nothing more is ever coming for this request_id).
        Holding it back here instead would only risk it being silently
        lost: `_speak()` calls `forget_turn()` as soon as
        `is_turn_streamed_and_completed()` is true, and a chunk still
        sitting in `held_chunks` at that moment would never get another
        chance to drain (no future `speech_chunk_playback_finished` can
        ever arrive for state that no longer exists)."""
        for chunk in chunks:
            state.pending_dispatched += 1
            state.opened_stream = True
            if state.first_chunk_dispatched_at is None:
                state.first_chunk_dispatched_at = time.time()
            self._publish_stream_chunk(state.request_id, state.conversation_id, chunk.to_dict())

    def _drain_held(self, state: _TurnState) -> None:
        while state.held_chunks and state.pending_dispatched < self._max_pending_chunks:
            chunk = state.held_chunks.pop(0)
            state.pending_dispatched += 1
            state.opened_stream = True
            if state.first_chunk_dispatched_at is None:
                state.first_chunk_dispatched_at = time.time()
            self._publish_stream_chunk(state.request_id, state.conversation_id, chunk.to_dict())

    def _default_publish(self, request_id: str, conversation_id: Optional[str], chunk_dict: Dict[str, Any]) -> None:
        from .adapters.events import SpeakStreamChunk
        self._event_bus.publish(SpeakStreamChunk(data={
            "request_id": request_id, "conversation_id": conversation_id, "chunk": chunk_dict,
        }))

    # -- internal helpers ---------------------------------------------------------

    def _get(self, request_id: str) -> Optional[_TurnState]:
        with self._lock:
            return self._turns.get(request_id)

    def _pop_if_present(self, request_id: str, *, mark_cancelled: bool = False, mark_failed: bool = False) -> Optional[_TurnState]:
        with self._lock:
            state = self._turns.get(request_id)
            if state is None:
                return None
            if mark_cancelled:
                state.cancelled = True
            if mark_failed:
                state.failed = True
            for sub in state.subscriptions:
                try:
                    self._event_bus.unsubscribe(sub)
                except Exception:
                    pass
            state.subscriptions = []
            return state
