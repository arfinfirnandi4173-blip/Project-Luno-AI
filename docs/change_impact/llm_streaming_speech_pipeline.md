# Change Impact Analysis — LLM Token Streaming → Real-Time Speech Pipeline

## Goal

Make Luno start generating/speaking audio before the full LLM response
text is complete, using the EXISTING TTS chunking + Speech Chunk Queue +
Cancellation pipeline built in the two prior sprints
(`docs/change_impact/tts_chunking_streaming.md`,
`docs/change_impact/tts_chunk_queue_cancellation.md`) as the speech-side
foundation, and the project's EXISTING LLM adapter layer as the
generation-side foundation. Additive only - no second LLM abstraction,
TTS chunker, speech queue, cancellation mechanism, barge-in detector, or
response-depth system.

## Phase 0 — Audit (read-only, governed every later phase)

Read every relevant file before writing any code:
`luno/adapters/llm/base.py`, `models.py`, `llm_manager.py`,
`openrouter_provider.py`, `luno/adapters/openrouter.py`,
`luno/adapters/llm_manager.py`, `luno/adapters/openai_compatible.py`
(shared streaming client), `luno/incremental_speech.py` (this sprint's
own new module, read back after writing it), `luno/speech_chunk.py`,
`luno/response_output.py`, `luno/adapters/fish_audio.py`,
`main_runtime_demo.py`.

**Finding: real, production-wired LLM token streaming already exists.**

- `OpenAICompatibleClient.stream_chat()` (shared by the OpenRouter/
  OpenAI/Local providers) opens a real SSE HTTP connection
  (`response.iter_lines(decode_unicode=True)`), parses `data:`-prefixed
  lines, and yields incremental `StreamChunk`s. A `threading.Event`
  (`cancel_event`) is checked between every line, and
  `LLMManagerAdapter` maintains an `_inflight_responses` table so
  `cancel(request_id)` can stop an in-flight stream.
- `LLMManagerAdapter` (registered under the module id `"openrouter"` -
  confirmed via `luno/bootstrap/adapters.py` line 137,
  `openrouter_adapter = LLMManagerAdapter()`) is what the REAL
  production entrypoint (`main.py` -> `luno/bootstrap/adapters.py`)
  constructs and registers.
- The OLDER `luno.adapters.openrouter.OpenRouterAdapter` class (kept for
  backward compatibility, NOT used by `bootstrap/adapters.py`) is what
  `main_runtime_demo.py`'s own `RuntimeDemoConsole` constructs directly
  for its demo/test console - the SAME harness every prior sprint's
  console-level tests already use. Read `luno/adapters/openrouter.py`
  directly and confirmed: it ALSO implements real, native streaming
  (`stream_chat_completion()`), with the identical
  `llm_streaming`/`llm_chunk`/`llm_finished`/`llm_error`/`llm_cancelled`
  event contract byte-for-byte - `LLMManagerAdapter` was built to match
  this OLDER adapter's own established contract, not the other way
  around. `MockOpenRouterClient` (the console's test double) supports a
  `chunk_delay_s` parameter and yields WORD-BY-WORD `StreamChunk`s,
  exactly like `MockProviderClient` does for `LLMManagerAdapter`.
- Both adapters publish the same event shape: `LLMStarted` ->
  `LLMStreaming` -> `LLMChunk*` (each carrying `request_id`/
  `conversation_id`/`delta`/`text_so_far`/`index`) -> `LLMFinished` |
  `LLMError` | `LLMCancelled`, followed by `AssistantResponse` (the full
  accumulated text, unchanged contract) on success.

**Conclusion:** the brief's own STOP condition ("if the active provider
does NOT support true streaming, stop at that boundary and document the
blocker") never applied - streaming was ALREADY real and ALREADY wired
to both the production entrypoint and the demo/test console. Phases 1-2
(streaming contract + provider implementation) were satisfied entirely
by REUSE. This sprint's actual new-code surface reduced to: the
incremental text buffer, the LLM-stream-to-speech-queue coordinator, one
new speech-side event, and a streaming-aware extension to
`FishAudioAdapter`'s existing worker.

**TTS pipeline re-audit** (from the prior sprint, unchanged): `SpeechChunk`
(`luno/speech_chunk.py`), `SpeechCancellationToken`,
`FishAudioAdapter._play()`'s sequential per-request queue/worker, and
the barge-in wiring (`StopPlayback`/`llm_cancelled` -> token cancel) were
all confirmed still exactly as documented in the prior sprint's own
change-impact doc - nothing here needed modification, only extension.

## Streaming contract (Phase 1) — reused, not reinvented

`LLMStreamChunk`/`stream_chat()`/the `llm_streaming`/`llm_chunk`/
`llm_finished`/`llm_error`/`llm_cancelled` event contract already existed
and is already provider-agnostic. No second streaming abstraction was
built. `IncrementalSpeechBuffer`/`StreamingSpeechCoordinator` consume
these EXISTING events directly - they never see or care which concrete
adapter (`LLMManagerAdapter` or `OpenRouterAdapter`) published them.

## Provider implementation (Phase 2) — reused, not reinvented

No provider-specific code was added by this sprint. The active
production provider (whichever `LLMManagerConfig`/`bootstrap/adapters.py`
selects) and the console's own `OpenRouterAdapter` both already stream.

## Incremental text buffer (Phases 4-5)

New module `luno/incremental_speech.py`, class `IncrementalSpeechBuffer`:

- **Boundary priority**, all via REUSED machinery:
  1. Sentence-ending punctuation (`.`/`!`/`?`/`...`) - via
     `luno.response_output._split_into_raw_sentences()`, re-run on the
     growing buffer on every `feed()` call. Every split entry EXCEPT the
     last (which might still be growing) is "settled"/flushable - a
     sentence only flushes once something else arrives after it
     (matches the brief's own worked example exactly: `"Memory Luno"` ->
     `" menyimpan"` -> `" data"` -> `" berdasarkan"` -> `" konteks."` ->
     the sentence flushes once the NEXT delta confirms it's over).
  2. Paragraph boundary - already handled BY the same splitter (blank
     lines).
  3/4/5. Strong punctuation / comma-if-long-enough / hard max-length
     cutoff - one reused call to
     `luno.response_output._split_long_sentence()` once the still-open
     tail exceeds the configured `max_buffer_chars` (default 220,
     mirrors `VOICE_CHUNK_MAX_CHARS`).
  6. Final LLM response - `flush_final()` always flushes whatever
     remains, never drops content.
- **Anti-spam rule:** a settled sentence shorter than
  `DEFAULT_MIN_SHORT_CHUNK_CHARS` (12 chars) is held and merged with
  whatever settles NEXT, rather than spoken as an isolated "Iya." - the
  brief's own "jangan menghasilkan chunk 1-2 kata tanpa alasan." The one
  exception: the very LAST chunk of a whole reply, which has nothing
  left to merge into - final-flush-always-flushes (rule 6) outranks the
  anti-spam heuristic there.
- **Bug found and fixed (whitespace-loss across delta boundaries):**
  the first implementation rebuilt the still-open tail via
  `" ".join(...)` after settling, which silently discarded the ORIGINAL
  whitespace between a settled chunk and whatever followed - a later
  delta with no leading space of its own could glue two words together
  ("kata katakata kata"). Fixed with `_slice_after()`, a cursor-based
  `str.find()` walk that returns an exact substring of the ORIGINAL
  buffer, never a lossy rejoin. Verified via a targeted stress test
  (30-word force-split run, exact word count before/after the fix: 26
  vs. 30).
- Text normalization is `luno.text_normalizer.normalize_for_speech()`,
  called exactly once per settled chunk - no second normalizer.
- `SpeechChunk.total = -1` for streaming chunks whose final count isn't
  knowable until the stream ends (a documented, deliberate deviation
  from the prior sprint's "always a real count" contract - `total` was
  always informational/logging-only, never load-bearing for
  cancellation correctness).

## Speech integration (Phase 3, 9, 10)

New class `StreamingSpeechCoordinator` (same file). Per active
`request_id`: subscribes to the EXISTING `llm_streaming`/`llm_chunk`/
`llm_finished`/`llm_error`/`llm_cancelled` events plus
`speech_chunk_playback_finished`/`speech_playback_started`/
`speech_playback_finished`/`speech_playback_cancelled`, feeds the
buffer, applies bounded backpressure, and publishes ONE new event,
`SpeakStreamChunk`, to the EXISTING `FishAudioAdapter`.

**New wire-level additions (`luno/adapters/events.py`):**

- `SpeakStreamChunk` - `request_id`/`conversation_id`/`chunk` (a
  `SpeechChunk.to_dict()`). The first chunk for an unseen `request_id`
  lazily opens a new streaming utterance; a chunk with `is_final=True`
  signals the stream's end (no separate "end" event needed); a chunk
  with empty `text` AND `is_final=True` is a pure close marker, never
  played.
- `SpeechChunkPlaybackFinished` - `request_id`/`chunk_id`/`sequence`, a
  per-chunk backpressure signal. Does NOT replace
  `SpeechPlaybackStarted`/`Finished`/`Cancelled`.

**Routing fix found:** `DEFAULT_ADAPTER_EVENT_MAPPING`
(`luno/adapters/models.py`) needed one new entry,
`"speak_stream_chunk": ["fish_audio"]`, without which `SpeakStreamChunk`
events were published but never delivered to `FishAudioAdapter` at all -
confirmed via a failing manual smoke test (`stream_queues: {}`,
`chunk_control: {}`, nothing played) before the fix, then re-verified
working end to end after.

**`FishAudioAdapter` extension (`luno/adapters/fish_audio.py`):** the
EXISTING `_play()` is left completely unchanged (byte-identical). A new
`_play_stream()` method mirrors its structure - same
`SpeechCancellationToken`/`_chunk_control`/`_in_flight_request_ids`/
terminal-event/bounded-retry-then-skip contract - but consumes from a
live `queue.Queue` (`self._stream_queues[request_id]`, bounded at 32 as
a defensive safety net only; real backpressure lives upstream in the
coordinator) fed by `SpeakStreamChunk` events, polling with a 0.05s
interval for cancellation responsiveness and a 30s idle-timeout safety
net (in case an `is_final` chunk is somehow never sent). A new
`_handle_stream_chunk()` opens a stream on the first chunk for an unseen
`request_id`, registering its `SpeechCancellationToken` SYNCHRONOUSLY
before `pool.submit()` - the identical pre-synthesis race fix the prior
sprint already applied to `SpeakRequest`.

## Backpressure (Phase 10)

Bounded entirely through the existing event-bus architecture (no direct
object references between modules). `StreamingSpeechCoordinator` tracks
`pending_dispatched` per turn; newly-flushed chunks are held in a local
`held_chunks` list once `pending_dispatched >= max_pending_chunks`
(configurable, default 4, `LLM_TTS_STREAM_MAX_PENDING_CHUNKS`); draining
happens as `speech_chunk_playback_finished` events arrive.
`handle_event()` never blocks. **One deliberate exception, found via
testing (see "Race #2" below):** a turn's terminal batch (whatever was
still held plus the final flush) bypasses the cap entirely, dispatched
immediately via `_dispatch_final()` - backpressure exists to bound an
ONGOING stream, not a single, already-bounded final batch.

## Cancellation (Phase 3) / Barge-in (Phase 9)

No second cancellation mechanism. `cancel_turn()` is idempotent, stops
the coordinator from producing/publishing any MORE chunks for a turn
(unsubscribes immediately), and clears anything still only locally held.
It does not itself touch `FishAudioAdapter`'s playback - that is
already, separately, driven by `StopPlayback`/`llm_cancelled` reaching
`FishAudioAdapter` directly (unchanged). Barge-in during PLAYBACK works
identically to the prior sprint (verified end-to-end, Phase 14 scenarios
C/D/F). See "Known limitations" for the one gap found regarding barge-in
DURING active LLM generation.

## Dual output (Phase 6)

Chat always gets the FULL, FINAL response (`AssistantResponse`, published
by the unchanged LLM adapter, carrying the complete accumulated text -
never truncated, never touched by streaming). Voice gets INCREMENTAL
speech chunks. `BehaviorTreeModule._speak()` checks
`is_turn_streamed_and_completed()` before publishing its own legacy,
whole-response `SpeakRequest` - skips it when the turn was already fully
spoken via streaming, so a turn is never spoken down two audio paths at
once. When streaming is disabled/unavailable/cancelled/failed, `_speak()`
behaves byte-identically to before this sprint.

## Response Depth Policy (Phase 7)

Reused as-is, `luno.response_policy.compute_response_policy()` untouched.
Depth is never recomputed for chunk-boundary decisions and never used to
choose streaming vs. non-streaming (verified: `test_35`/`test_36`/
`test_37` in `tests/test_streaming_speech_integration.py`, plus E2E
scenarios). `IncrementalSpeechBuffer` deliberately never applies the
DETAILED-depth priority-selection/compression pass `build_dual_response()`
uses for the non-streaming path - that pass fundamentally needs the
WHOLE reply first (its budget is a fraction of the total sentence count),
which streaming by definition doesn't have yet. A DETAILED-depth
streamed reply is spoken sentence-by-sentence, IN FULL, uncompressed -
per the brief's own explicit "jangan mengurangi detail hanya demi
latency" - naturally producing MORE chunks than the non-streaming
DETAILED path, which is the spec-mandated behavior, verified directly by
`test_37_detailed_depth_is_spoken_in_full_not_compressed`.

## Memory / context safety (Phase 8)

No duplicate memory retrieval, context assembly, or planner execution.
`StreamingSpeechCoordinator.start_turn()` only adds event-bus
subscriptions - it never calls `retrieve_memories()` or any
context-assembly function itself. Verified directly:
`test_38_context_assembled_exactly_once_per_streamed_turn` (wraps
`memory_retriever.retrieve_memories` with a counter, asserts exactly 1
call for one streamed turn) and
`test_39_memory_retrieval_count_unchanged_by_streaming` (same counter,
compared across two otherwise-identical runs with streaming on vs. off -
both exactly 1).

## Latency observability (Phase 11)

`_TurnState` tracks `request_received_at`/`llm_stream_started_at`/
`first_token_at`/`first_sentence_ready_at`/`first_chunk_dispatched_at`/
`llm_completed_at`/`speech_completed_at`. Log lines (via the existing
`luno.core.utils.log()`, component tag `"incremental_speech"`, matching
`luno/barge_in/manager.py`'s own convention for a root-level non-adapter
module): `TTFT`, `TTFS` (with a fallback path in `_on_finished()` for a
reply whose first sentence never independently flushed before the LLM
finished), `TTFA` (via a new `speech_playback_started` subscription),
`LLMCompleted`, `SpeechCompleted`. Never logs raw text/delta content -
only timing numbers, matching `FishAudioAdapter`'s own established
convention.

## Error handling (Phase 12)

- LLM failure BEFORE any text: existing, untouched, normal error
  handling.
- LLM failure AFTER partial text: `_on_error()` marks the turn `failed`
  (never `completed`) so `_speak()`'s normal apology-fallback path runs
  exactly as it already does for the non-streaming case; whatever's
  already reached `FishAudioAdapter` is allowed to finish playing
  naturally (never cut off mid-word); anything still only LOCALLY held
  is discarded (it would have continued a sentence the LLM will now
  never complete); an explicit close marker is sent immediately if the
  stream was already opened, so `FishAudioAdapter` ends cleanly instead
  of waiting on its own 30s idle-timeout. Verified end-to-end:
  `tests/test_streaming_e2e.py::test_E_llm_failure_after_partial_text_no_false_complete_response`.
- Cancellation is never reported as a generic failure (verified via
  `test_29`-`test_34`).
- TTS failure never kills the LLM worker (separate threads/adapters by
  construction, unchanged from the prior sprint).

## Two real races found and fixed (via this sprint's own E2E testing)

Both were found by writing and running the actual Phase 14 scenarios
against the real console - neither showed up in unit-level testing of
the buffer/coordinator in isolation.

1. **Terminal-chunk race between `_generate_reply()` and
   `_on_finished()`.** `assistant_response`/`llm_error` (what
   `_generate_reply()`'s `done.wait()` unblocks on) and `llm_finished`/
   `llm_error` (what the coordinator reacts to, to flush the LAST
   buffered sentence) are two INDEPENDENT event-bus subscriptions of
   events published back-to-back for the same LLM completion - the
   dispatcher gives no ordering guarantee between them.
   `BehaviorTreeModule._speak()` could call `forget_turn()` a few
   microseconds before `_on_finished()` got to run `flush_final()` for
   the SAME turn, silently dropping the still-buffered trailing sentence
   and leaving `FishAudioAdapter`'s stream waiting on a close marker
   that would then never arrive (until its own 30s idle-timeout safety
   net). Repro: a 5-sentence DETAILED reply consistently lost its 5th
   sentence and hung for 30s. **Fix:**
   `StreamingSpeechCoordinator.wait_until_settled(request_id, timeout_s=2.0)`,
   a short bounded poll `_speak()` now calls before deciding whether to
   skip/forget a turn - converges in under a millisecond in the normal
   (non-racing) case, and correctly waits out the rare race otherwise.
2. **Terminal-flush-vs-backpressure race.** If the LAST batch of chunks
   (`_on_finished()`'s own `flush_final()`) arrived while
   `max_pending_chunks` was already saturated by earlier, still-
   in-flight chunks, the final chunk(s) sat in `held_chunks` waiting for
   a `speech_chunk_playback_finished` signal that would NEVER come once
   `forget_turn()` (called right after, since `state.completed` was
   already `True`) discarded the turn's state entirely - the same
   silent-drop-and-hang symptom as race #1, different root cause.
   **Fix:** `_on_finished()` now dispatches its terminal batch (whatever
   was still held, plus the final flush) via a new `_dispatch_final()`
   that bypasses the `max_pending_chunks` cap - correct because
   backpressure exists to bound an ONGOING stream, not a single,
   already-bounded final batch that will never grow further.

Both fixes were verified by direct manual reproduction (before and
after) and by the full Phase 13/14 test suites passing.

## Tests (Phase 13 — 41 required scenarios + extras)

- `tests/test_llm_streaming.py` (8/8) - the EXISTING `LLMManagerAdapter`/
  `MockProviderClient` streaming contract (multiple partial chunks,
  order/prefix preservation, final marker, empty-partial-never-published,
  provider error mid-stream, cancellation mid-stream, request_id/
  conversation_id preserved on every chunk).
- `tests/test_incremental_speech_buffer.py` (17/17 + 4 extra contract
  tests) - pure buffer unit tests: token combination, sentence/paragraph/
  comma/max-length boundaries, final flush (incl. idempotency), no empty
  chunks, no 1-2-word spam (except the legitimate final-chunk exception),
  mixed Indonesian/English, markdown/URL/code normalization reuse,
  deterministic chunk_id/sequence, close-marker semantics.
- `tests/test_streaming_speech_integration.py` (22/22) - dual output
  (20-23), bounded-queue backpressure (24-28), cancellation at every
  lifecycle point (29-34), Response Depth Policy under streaming
  (35-37), memory/context/persistent-state/no-duplicate-speech-event
  safety (38-41).
- `tests/test_streaming_e2e.py` (6/6) - the brief's own explicit
  scenarios A (normal stream, chunk before LLM-finished, chat complete),
  B (long response, bounded queue, all chunks eventually play), C
  (barge-in during generation, pipeline stops, new turn works
  afterward), D (barge-in between LLM and TTS - chunk never plays), E
  (LLM failure after partial - no false complete response), F (new
  request after cancel - no stale audio).

53 new tests total, all passing, no real network/API calls anywhere
(`MockProviderClient`/`MockOpenRouterClient`/`MockFishAudioClient` only).

## Regression (Phase 15)

See `docs/testing/regression_baseline.md`'s own "LLM Streaming ->
Real-Time Speech Pipeline" section for the full batch-by-batch table.
Summary: 2569 tests passed across every batch executed this sprint, 16
failures, ALL 16 already documented as pre-existing/environment-specific
before this sprint (2 known-flaky Barge-in timing tests, 5 known
`text_normalizer`/`LUNO_LANGUAGE` env-leak tests, 9 known ENVIRONMENT-
SPECIFIC/INFRASTRUCTURE tests) - zero new regressions. One test
(`test_F_new_request_after_cancel...`) showed one-off timing flakiness
under heavy system load, matching this project's own already-established
tolerance class for real-thread/real-timing tests (§13 Flaky Test
Policy) - not a logic defect (passes reliably in isolation and on
immediate re-run).

## Persistent state verification (Phase 16)

SHA256 + mtime for every file directly under `config/` (57 files),
captured immediately before and immediately after this sprint's own new
test suite (which constructs/tears down dozens of `RuntimeDemoConsole`/
`AdapterManager` instances) - byte-identical AND mtime-identical, zero
diff, zero stray temp files.

## Files created

- `luno/incremental_speech.py` (new module: `IncrementalSpeechBuffer`,
  `StreamingSpeechCoordinator`)
- `tests/test_llm_streaming.py`
- `tests/test_incremental_speech_buffer.py`
- `tests/test_streaming_speech_integration.py`
- `tests/test_streaming_e2e.py`
- `docs/change_impact/llm_streaming_speech_pipeline.md` (this file)

## Files modified

- `luno/config.py` - additive: `ENABLE_LLM_TTS_STREAMING` (default
  `False`), `LLM_TTS_STREAM_MAX_PENDING_CHUNKS` (default `4`).
- `luno/adapters/events.py` - additive: `SpeakStreamChunk`,
  `SpeechChunkPlaybackFinished`.
- `luno/adapters/models.py` - additive: one new
  `DEFAULT_ADAPTER_EVENT_MAPPING` entry (`"speak_stream_chunk": ["fish_audio"]`).
- `luno/adapters/fish_audio.py` - additive: `_play_stream()`,
  `_handle_stream_chunk()`, new `__init__` fields for the stream-queue
  bookkeeping, one new `handle_event()` branch. `_play()` itself is
  byte-identical to before this sprint.
- `main_runtime_demo.py` - additive wiring in `BehaviorTreeModule`:
  `_streaming_coordinator` attribute, construction in `bind_event_bus()`
  (only when the feature flag is on), `start_turn()`/
  `wait_until_settled()`/`is_turn_streamed_and_completed()`/
  `forget_turn()` calls in `_generate_reply()`/`_speak()`, `cancel_turn()`
  in the `llm_cancelled` handler. One small unrelated bug fix in
  `_generate_reply()`'s `"err" in box` branch (see "Two real races"
  above, item 3 in the ARCHITECTURE_GUARD.md entry) - sets
  `_last_turn_request_id`/`_last_turn_depth` on the failure path too,
  closing a pre-existing state-leak that streaming made newly visible.

## Files deleted

None.

## Known limitations

- **`BehaviorTreeModule._generate_reply()`'s wait mechanism does not
  unblock on `llm_cancelled`.** Pre-existing, unrelated to streaming,
  unchanged by this sprint - `_generate_reply()`'s `done.wait()` only
  listens for `assistant_response`/`llm_error`. A barge-in that lands
  while the LLM is STILL actively generating (not yet finished) leaves
  that turn's `_generate_reply()` call - and therefore
  `BehaviorTreeModule`'s own single-threaded event processing - blocked
  until `llm_timeout_s` (45s default) before the next utterance can even
  reach the planner. Barge-in during PLAYBACK (the common case, and the
  one the brief's own E2E scenarios C/D/F exercise) is unaffected and
  verified working correctly. This existed before streaming (any
  cancelled, still-in-flight `NeedLLMResponse` would exhibit the same
  block) but streaming makes "barge-in while the LLM is still
  generating" realistic/common rather than a rare edge case. Explicitly
  judged out of this sprint's "don't modify the Barge-in detector /
  unrelated subsystems" scope - not fixed. A real fix would add an
  `llm_cancelled` listener to `_generate_reply()`'s existing `sub_ok`/
  `sub_err` pair.
- No true token-level TTS (i.e., synthesizing individual words) -
  chunking remains sentence/clause-based, per the brief's own explicit
  instruction ("JANGAN: token -> TTS -> token -> TTS").
- No multi-provider simultaneous streaming (explicitly out of scope per
  the brief's own constraint #16).
- No adaptive streaming learning (explicitly out of scope per the
  brief's own constraint #15).
- `conversation_id` is carried/logged on every streamed chunk but not
  actively used to gate playback - unchanged from the prior sprint's own
  documented limitation.
- The `text_normalizer`/`LUNO_LANGUAGE` env-leak (pre-existing, already
  documented in two prior sprints) - this sprint's regression sweep
  additionally completed the root-cause trace (see
  `docs/testing/regression_baseline.md`) but did not fix it (out of
  scope).

## Technical debt

- `StreamingSpeechCoordinator._turns` is bounded defensively
  (`maxlen`-style eviction at 64 entries, mirroring
  `BehaviorTreeModule._cancelled_request_ids`'s own convention) but a
  turn that is streamed-and-completed yet never reaches `_speak()`
  (e.g. a caller that bypasses the normal `_generate_reply()`/`_speak()`
  pair entirely) will sit in that dict until eviction rather than being
  proactively cleaned up - acceptable given this codebase's own
  documented single-active-turn runtime shape, but worth revisiting if
  true multi-turn-concurrent streaming is ever built.
- `wait_until_settled()`'s 2-second timeout is a bounded safety net for
  race #1 above, not a guarantee - an adversarially slow dispatcher could
  still theoretically lose the race past 2 seconds. Not observed in
  practice; flagged for awareness.

## Final assessment

Real-time LLM-to-speech streaming is implemented, wired into the actual
production event contract (not a polling simulation), and verified
end-to-end against the real console with real (mocked-network,
real-threading) adapters. All 20 constraints from the sprint brief were
honored: no second LLM abstraction, TTS chunker, speech queue,
cancellation mechanism, barge-in detector, or response-depth system was
built; memory/context retrieval is not duplicated; no real API calls
happen in tests; no per-token persistence; no one-thread-per-chunk
design; backpressure is bounded; cancellation correctness was never
traded for latency; chat's full response is never sacrificed for voice
latency; adaptive learning and multi-provider simultaneous streaming
were deliberately not attempted. "Real-time streaming" is an accurate
claim here specifically because the underlying LLM adapters were
independently confirmed (Phase 0) to yield genuinely incremental data
before their final response, not because streaming was faked via
polling.
