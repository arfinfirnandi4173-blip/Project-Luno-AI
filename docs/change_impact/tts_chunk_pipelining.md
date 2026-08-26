# Change Impact: TTS Chunk Pipelining (Synth/Playback Overlap - Gapless Voice Playback)

## 1. Problem

A prior, read-only "PHASE 0 AUDIT - TTS CHUNK GAP / AUDIO PLAYBACK
STALL" sprint (chat-delivered report, no persisted doc file - its
findings are reproduced here) traced the full LLM delta ->
`IncrementalSpeechBuffer` -> `StreamingSpeechCoordinator` ->
`SpeakStreamChunk` -> `FishAudioAdapter` -> `_play_stream()` ->
`client.play()` -> Fish Audio synthesis -> audio queue -> playback
device pipeline and found the root cause of Luno's audible between-
chunk voice gaps: `_play_stream()` called `client.play()` once per
chunk, STRICTLY sequentially - "chunk N+1's `client.play()` is never
called until chunk N's own `client.play()` call has returned", by
explicit prior design (that method's own docstring).

Measured directly with an instrumented harness (the REAL
`FishAudioAdapter` + `RealFishAudioClient` + `AdapterManager`/Event Bus,
only the HTTP transport and audio device faked):
`SynthesisStart[i+1]` occurred at the EXACT same timestamp as
`PlaybackEnd[i]` (within 1ms), and `gap_before_playback[i+1] ~=
synth_latency[i+1]` (measured 1.301-1.303s in that harness). Chunk size
was ruled out as a contributing cause - a representative long
Indonesian response produced 2 sentence-bounded chunks of 152/171 chars
(24/26 words), not tiny fragments; the gap is per-boundary and scales
with synthesis latency, not chunk count/size.

This sprint closes exactly that gap, with a conservative ONE-SLOT
lookahead/prefetch design, and nothing else.

## 2. What was already reused (no reimplementation)

- `RealFishAudioClient.play()` (`fish_audio_real.py`) already internally
  separated synthesis (`self._synthesize`, submitted to `self.
  _synth_executor`, polled via a cancellation-checking `future.result
  (timeout=...)` loop) from playback (`self._play_audio(wav_bytes,
  control)`, blocking) - it simply never exposed these as two
  independently callable methods. This sprint exposes the SAME two
  callables as `synthesize()`/`play_audio()`, it does not invent a new
  synthesis or playback mechanism.
- `_PlaybackControl`/`self._active` (the per-`play()`-call cancel/pause
  bookkeeping `stop()`/`pause()`/`resume()` already act on) - reused
  verbatim by the new `play_audio()`, not duplicated.
- `SpeechCancellationToken`/`_chunk_control`/`_in_flight_request_ids`/
  the terminal-event contract (exactly one of `SpeechPlaybackFinished`/
  `SpeechPlaybackCancelled` per request_id) - reused verbatim by the new
  `_play_stream_pipelined()`, which mirrors `_play_stream()`'s own
  logging/event contract line for line.
- `self._STREAM_POLL_INTERVAL_S`/`self._STREAM_IDLE_TIMEOUT_S`/
  `self._chunk_retry_limit`/`self._STREAM_QUEUE_MAXSIZE` - the SAME
  bounded-poll/retry/idle-timeout constants, not new ones.
- `StopPlayback`/`PausePlayback`/`ResumePlayback` event handling
  (`handle_event()`) - completely untouched; cancellation/pause still
  reach the new pipelined path through the exact same `client.stop()`/
  `client.pause()`/`_apply_to_all_tokens()` calls as before.
- No second Event Bus, no second state machine, no second TTS engine, no
  changes to memory/ranking/response-depth/LLM/prompt-construction/
  TTS-chunking-heuristic/voice-model/voice-config - none of these were
  touched or needed to be.

## 3. The fix - one-slot prefetch

`FishAudioClient` ABC (`fish_audio.py`) gains 3 NEW, OPTIONAL methods,
all opt-in (default behavior preserves `MockFishAudioClient` and every
pre-existing test unchanged):

- `supports_split_synthesis() -> bool` - default `False`.
- `synthesize(text: str) -> Any` - default raises `NotImplementedError`.
- `play_audio(audio: Any, on_playback_start=None) -> None` - default
  raises `NotImplementedError`.

`FishAudioAdapter` gains a NEW, separate, bounded `_prefetch_executor`
(`ThreadPoolExecutor(max_workers=2, thread_name_prefix=
"luno-fishaudio-prefetch")`) - deliberately NOT `_playback_executor`
(that pool's 2nd worker stays reserved for the pre-existing "paused
reply + Barge-in CONFIRM interjection" concurrency case). `_play_stream()`
itself is UNCHANGED except for one dispatch line at the very top:

```python
if self.client.supports_split_synthesis():
    return self._play_stream_pipelined(request_id, conversation_id, token)
```

Its own body below that line is byte-identical to before this sprint -
the exact fallback path for `MockFishAudioClient` and any other client
that doesn't opt in.

`_play_stream_pipelined()` (new method) - the SAME per-chunk state
machine as `_play_stream()`, with one addition: after resolving the
CURRENT chunk's audio (via `_resolve_audio()` - either a matching
in-flight prefetch `Future`, or a fresh synchronous `synthesize()` call)
and BEFORE calling `play_audio()`, it does a single non-blocking
`queue.get_nowait()` for the NEXT chunk and, if a real (non-close-marker)
chunk is already available, submits AT MOST one
`self._prefetch_executor.submit(self.client.synthesize, next_text)` job.
After `play_audio()` returns, `current` advances to exactly that
already-dequeued next item (or a fresh blocking dequeue if nothing was
available). This structurally guarantees:

- **The one-slot bound** - never more than one prefetch job outstanding
  at a time (there is no queue of futures, only a single
  `prefetch_future`/`prefetch_chunk` pair).
- **Strict playback ordering** - `current` always advances to exactly
  the next item THIS method itself dequeued, in dequeue order, never to
  whichever chunk's synthesis happened to finish first.

`RealFishAudioClient` (`fish_audio_real.py`) implements the 3 new
methods as thin wrappers around its existing internals:

- `supports_split_synthesis() -> True`.
- `synthesize(text) -> bytes` - a plain blocking call to the SAME
  `self._synthesize(text, cfg, self._session)` callable `play()` already
  uses internally. Deliberately NOT cancellation-aware (per the ABC's
  own contract) - a caller that stops waiting on it simply discards the
  eventual result.
- `play_audio(audio, on_playback_start)` - creates a fresh
  `_PlaybackControl`, appends it to `self._active` (exactly like
  `play()` does), calls `self._play_audio(audio, control)`, removes it
  in a `finally`. Instance-level `stop()`/`pause()`/`resume()` therefore
  affect an in-flight `play_audio()` call exactly as they already affect
  an in-flight `play()` call.

`play()` itself is left completely BYTE-IDENTICAL - accepting minor
duplication for safety rather than rewriting the Fish Audio integration,
per this sprint's own explicit constraint.

## 4. Cancellation - "abandon, never force-kill"

The SAME policy `RealFishAudioClient.play()` already used internally for
its own synthesis call, now applied consistently to prefetch too. An
abandoned prefetch `Future`'s underlying thread is simply never awaited
again and its eventual result discarded - bounded by the client's own
`timeout_s`, never blocking shutdown, never touching another request's
state.

`_resolve_audio(text, future, token)` (new helper) waits on a given
future via `future.result(timeout=self._STREAM_POLL_INTERVAL_S)` in the
SAME cancellation-checking-loop idiom `RealFishAudioClient.play()`
already established internally for its own synthesis wait (rather than
one unbounded `.result()` call), so a cancelled/abandoned turn never
blocks waiting on a slow in-flight prefetch. Returns `(cancelled, audio)`
- a `cancelled=True` result is raised as `PlaybackCancelled` inside the
same bounded-retry loop `play_audio()` runs in, reusing that loop's
existing cancellation-handling branch rather than adding a second one.

`StopPlayback` reaches an in-flight `play_audio()` call exactly as it
already reached `play()` (same `_PlaybackControl`/`self._active`
mechanism, unchanged `handle_event()`), and a chunk whose resolve/
prefetch is still in flight when cancellation arrives is discarded,
never played. Proven directly by three dedicated tests covering three
distinct cancellation timings: during prefetch synthesis, right after
prefetch synthesis finishes but before use, and mid-playback of the
CURRENT chunk (§7).

## 5. Pause/resume - synthesis keeps progressing

`synthesize()` has NO pause-check by design (per the ABC's own
docstring) - prefetch synthesis keeps running/completing even while
playback is paused. Only `play_audio()` (via its own `_PlaybackControl`
in `self._active`) responds to `pause()`/`resume()`, identically to
`play()` before this sprint. Proven directly:
`test_pause_does_not_cancel_prefetch_synthesis` pauses playback, then
asserts the prefetch's `SynthesisEnd` event still arrives.

## 6. Measured improvement

Using the same instrumented-harness technique as the Phase 0 audit
(real `FishAudioAdapter`+`RealFishAudioClient`+`AdapterManager`, faked
HTTP transport + faked audio device):

- **Realistic case** (chunk playback duration exceeds synthesis latency
  - the audit's own measured 152/171-char, 24/26-word chunks would take
  far longer than ~1.3s to speak at natural speech rate): synthesis
  latency 1.302s, playback duration 2.0s per chunk, 3-chunk turn ->
  inter-chunk gap dropped from the previously-measured 1301-1303ms to
  **~0.5ms** (>99.9% reduction).
- **Adversarial case** (synthesis latency EXCEEDS playback duration:
  1.302s synthesis vs. 0.6s playback) -> residual gap ~703ms, matching
  the expected `max(0, synth_latency - playback_duration)` bound for a
  ONE-slot design. This is the correct, deliberate behavior of a
  bounded prefetch (not unbounded pipelining) - not a bug.

## 7. Tests

`tests/test_tts_chunk_pipelining.py` (new, 19 scenarios, all passing,
stable across repeated runs):

- **Core proof (items 1-2 of the required matrix):**
  `test_synthesis_of_next_chunk_starts_before_current_playback_ends`
  (asserts `SynthStart[1] < PlaybackEnd[0]` for a 3-chunk turn - fails
  against the pre-fix code, passes after) and
  `test_playback_order_is_never_reordered_by_pipelining` (later chunks
  synthesize FASTER than earlier ones; playback order still strictly
  sequential).
- **Chunk-count coverage (3-5):** single-chunk (nothing to prefetch),
  two-chunk, and five-chunk turns, the latter asserting the overlap
  invariant at EVERY boundary, not just the first.
- **Failure/timeout handling (6-7):** a prefetched chunk's synthesis
  failing outright is retried (bounded) then skipped, never crashing the
  turn; a pathologically slow chunk still completes (no deadlock).
- **Cancellation (8-10, 18):** during prefetch synthesis, right after
  prefetch is ready but unused, mid-playback of the current chunk, and a
  direct proof that a cancelled chunk's audio is never played even after
  waiting past when its background synthesis would have completed.
- **Pause/resume (11-13):** pause-then-resume completes successfully;
  prefetch synthesis is NOT paused by a playback pause.
- **Close markers (14-15):** a non-final close marker mid-stream is
  handled without disrupting the pipeline; a trailing final close marker
  after real chunks still finishes the turn.
- **Isolation/leak safety (16-17, 19):** two sequential turns leave zero
  leftover `_stream_queues`/`_chunk_control`/`_in_flight_request_ids`
  state; two concurrent, unrelated request_ids never cross-play each
  other's chunks; 15 sequential turns leave `_prefetch_executor`'s size/
  identity unchanged (no per-turn executor creation, no thread leak).
- **One-slot bound (20):** a lock-guarded concurrency counter wrapping
  the fake HTTP session directly proves at most ~1 prefetch job is ever
  in flight across a 6-chunk turn.

## 8. Regression

- `luno/adapters/tests/test_fish_audio_real.py`: **14/14** (run via
  `python3 -m luno.adapters.tests.test_fish_audio_real` - its custom
  `SCENARIOS`/`main()` runner convention, not plain pytest collection,
  is required for real validation; plain `pytest` collection imports and
  trivially "passes" these without asserting anything).
- `luno/adapters/tests/test_fish_audio_barge_in.py`: **8/8** (same
  convention).
- `luno/adapters/tests/` fish_audio/streaming/barge-in-filtered pytest
  subset: **86 passed**.
- `tests/test_tts_chunk_pipelining.py`: **19 passed**, run 3x
  consecutively with identical results.
- Full `tests/` tree (9 file-group batches; 2 pre-existing collection
  errors excluded, unrelated to this sprint - `test_main_bargein.py`
  missing the `faster_whisper` package, `test_root_main_bargein.py`
  referencing a stale sandbox path/`legacy_main.py`): **1836 passed, 10
  failed** - all 10 map exactly to the already-documented pre-existing
  baseline (6x `test_mic_device_index.py` missing `list_microphones.py`/
  device-index-default env issues, 1x `test_production_launcher.py`
  health-check, 2x `test_real_adapters.py` `RealWhisperSource.
  _device_index` gap, 1x `test_state_isolation.py` sandbox-path
  `inspect.getsource` artifact) - none touch `fish_audio*`/TTS code.
- Full `luno/` tree (2 batches): **813 passed, 7 failed** - all 7 are the
  SAME already-documented `test_barge_in.py` timing flakes (2,
  `test_confirm_mode_interrupt_then_no_resumes`/
  `test_stress_many_ordinary_utterances_then_one_real_interrupt`, both
  pass 3/3 in isolation) and `test_text_normalizer.py`
  `LUNO_LANGUAGE`-env-leak-under-full-sweep failures (5, all pass 35/35
  standalone) - reproducing the EXACT same 813/7 count the immediately-
  prior LLM Streaming sprint's own documented baseline established,
  zero coupling to this sprint's files (confirmed via `grep`).

## 9. Persistent-state verification

All 14 present `config/*.json` files SHA256- and mtime-identical before
and after this sprint's entire implementation and full test run (snapshot
taken before any edit, compared after the full regression sweep). No
stray `.tmp`/`.bak`/`.old`/`.orig` files. `_prefetch_executor` and every
per-request bookkeeping structure this sprint touches
(`_stream_queues`/`_chunk_control`/`_in_flight_request_ids`) are
transient, in-memory, per-process runtime state only - nothing new is
persisted to disk.

## 10. Known limitations

- The one-slot bound is deliberate, per this sprint's own explicit
  constraints (no multi-chunk buffering, no unbounded queues) - only ONE
  chunk ahead is ever prefetched. A pathological stream where EVERY
  chunk's synthesis latency exceeds its own playback duration still has
  a residual, bounded gap of `max(0, synth_latency - playback_duration)`
  per boundary (§6) - not a full-elimination guarantee for arbitrarily
  slow synthesis, by design.
- `synthesize()` is not itself cancellation-aware (per the ABC's own
  contract) - an abandoned prefetch's underlying HTTP call still runs to
  completion or its own `timeout_s`, just discarded rather than awaited.
  This mirrors `RealFishAudioClient.play()`'s own pre-existing synthesis-
  abandonment behavior, not a new risk this sprint introduced.
- `play()` and the new `synthesize()`/`play_audio()` pair on
  `RealFishAudioClient` duplicate a small amount of `_PlaybackControl`/
  `self._active` bookkeeping rather than sharing a single internal
  helper - an accepted tradeoff for leaving `play()` provably untouched
  (byte-identical) rather than risking a shared-refactor regression.

## 11. Scope / what was explicitly NOT changed

- `IncrementalSpeechBuffer`/`StreamingSpeechCoordinator`/
  `SpeakStreamChunk`/backpressure (`max_pending_chunks`) - untouched.
- The TTS chunking heuristic (`_group_sentences_into_chunks()`/
  `_split_long_sentence()`) - untouched.
- The Fish Audio synthesis/playback HTTP and audio-device primitives
  (`_default_synthesize`/`_default_play_audio`) - untouched.
- Voice model/voice config (`RealFishAudioConfig`) - untouched.
- Memory/ranking/response-depth/prompt-construction - untouched.
- The Event Bus, `AdapterManager`, and every other adapter - untouched.
- `_play()` (the legacy, non-streaming single-turn path) and `_play_stream()`'s
  own body (below its one new dispatch line) - byte-identical to before
  this sprint.
- No new Event Bus, no new state machine, no new global mutable state.
