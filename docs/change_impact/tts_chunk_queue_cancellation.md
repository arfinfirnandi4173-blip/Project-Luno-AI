# Change Impact Analysis — TTS Chunk Queue + Cancellation/Barge-in

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md` per
`ARCHITECTURE_GUARD.md` §9 (this change touches a Protected Core
subsystem - `FishAudioAdapter._play()`/`handle_event()` and
`BehaviorTreeModule._speak()`).

```
FEATURE:
TTS Chunk Queue + Cancellation/Barge-in. Builds directly on the prior
TTS Chunking/Voice Streaming sprint (text-level segmentation,
`voice_chunks`, sequential per-request playback) WITHOUT replacing it.
Adds two formal contracts on top of the existing chunk list:
  1. A CORRELATION contract (`luno.speech_chunk.SpeechChunk`/
     `build_speech_chunks()`) - every chunk now carries chunk_id,
     request_id, conversation_id, sequence, total, raw_text, text, and
     is_final, instead of being a bare string.
  2. A CANCELLATION contract (`luno.speech_chunk.SpeechCancellationToken`)
     - a typed, per-request_id object formalizing (not replacing) the
     Event-based stop/pause mechanism that already existed, plus a fix
     for a genuine pre-synthesis cancellation race this sprint's own
     reasoning uncovered.

WHY:
The prior sprint made Fish Audio play a reply as a sequence of chunks,
but chunks were plain strings with no correlation identity, and
cancellation state lived in an anonymous `Dict[str, Dict[str,
threading.Event]]` keyed only by request_id, with no formal contract
for "when exactly is this request cancellable" or "what happens to a
chunk that's mid-flight when StopPlayback arrives." This sprint's goal
was Luno speaking sooner (already achieved by the prior sprint),
playing chunks in strict order (already true), and CLEANLY stopping the
whole speech pipeline on barge-in for a MULTI-CHUNK reply specifically -
the prior sprint's single "gap between chunks" fix was necessary but not
sufficient to prove every cancellable point in a multi-chunk request's
lifecycle (before synthesis, during synthesis, after synthesis before
playback, during playback, between chunks, after completion) behaves
correctly and idempotently. LLM token streaming remains explicitly out
of scope, same as before.

AUDIT (Phase 0, read-only, before any code changed):
  - Re-read `docs/change_impact/tts_chunking_streaming.md` (prior
    sprint) as source of truth per this sprint's own explicit
    instruction - confirmed its architecture (Full-Response -> Segment
    -> Queue -> Sequential Playback) and its own documented "gap between
    chunks" fix (`_chunk_control` dict, checked between every two
    chunks) were both real and correctly described.
  - Confirmed `client.stop()/pause()/resume()` are still GLOBAL (act on
    whatever is currently in-flight in `MockFishAudioClient`/
    `RealFishAudioClient`'s own `_active` list) - unchanged, not this
    sprint's concern to fix, worked around the same way the prior sprint
    already worked around it (an adapter-level, per-request_id control
    structure that the client itself doesn't need to know about).
  - Confirmed `_playback_executor = ThreadPoolExecutor(max_workers=2)`
    is real, pre-existing, and intentionally sized for the "paused
    reply + Barge-in CONFIRM interjection" concurrency case, NOT for
    running multiple independent requests' chunks in parallel - a fact
    that mattered later when writing scenario 24's test (see FIXES
    below).
  - Confirmed `SessionManagerModule._handle_playback_done()` already
    treats `speech_playback_finished` and `speech_playback_cancelled`
    identically (`SPEAKING -> WAITING_USER`/`IDLE`) - Phase 6's speaking-
    state requirements were already satisfied by the existing state
    machine with zero code changes needed.
  - Identified, by reasoning about `ThreadPoolExecutor.submit()`'s async
    nature (not from a failing test), a genuine gap the prior sprint's
    `_chunk_control` dict did not close: `submit()` only queues work: a
    `StopPlayback` arriving after `SpeakRequest` is accepted but before
    the worker thread has actually started running `_play()` (and
    therefore before the prior sprint's dict entry for that request_id
    even exists) would find nothing to cancel. Fixed proactively, before
    writing scenario 17's test - see FILES CHANGED.

FILES CHANGED:
- luno/speech_chunk.py (NEW): `SpeechChunk` frozen dataclass
  (chunk_id/request_id/conversation_id/sequence/total/raw_text/text/
  is_final + `to_dict()`), `build_speech_chunks(voice_chunks,
  voice_chunks_raw=None, *, request_id, conversation_id=None) ->
  List[SpeechChunk]` (pure, wraps already-segmented text, degrades
  safely if `voice_chunks_raw` is missing/length-mismatched, never
  re-segments), `SpeechCancellationToken` (two `threading.Event`s under
  `__slots__`, `cancel()`/`pause()`/`resume()`/`is_cancelled`/
  `is_paused`/`wait_while_paused()`, `cancel()` is idempotent and always
  clears `pause`).
- luno/response_output.py (extended, additive): `DualResponse` gained
  `voice_chunks_raw: List[str]` (1:1-aligned raw/pre-normalized
  counterpart to the existing `voice_chunks`, reference-only, does not
  change what gets spoken). `_group_sentences_into_chunks()` was
  restructured into `_group_sentences_into_chunk_pairs()` (the real
  logic, now building `_ChunkPair(raw, cleaned)` pairs) with the
  original function kept as a thin backward-compatible wrapper -
  `voice_chunks` itself is byte-identical to before.
- luno/adapters/fish_audio.py (extended): `_chunk_control`'s value type
  changed from an anonymous Event dict to `SpeechCancellationToken`
  (`_apply_to_all_tokens()` replaces the prior `_set_chunk_control_flag()`
  helper). `handle_event()`'s SpeakRequest branch now creates and
  registers the token SYNCHRONOUSLY before `pool.submit()` (the
  pre-synthesis race fix). New `_normalize_chunk_entries()` static
  method accepts each chunk entry as either the prior sprint's plain
  `str` or this sprint's `dict` (`SpeechChunk.to_dict()`) uniformly.
  `_play()` now accepts its token as a parameter (falls back to creating
  one internally for any direct/bypass caller), and its loop checks
  `token.is_cancelled`/calls `token.wait_while_paused()` instead of
  reading the raw dict. All existing retry/skip/terminal-event
  guarantees preserved unchanged.
- main_runtime_demo.py (`BehaviorTreeModule._speak()`): now calls
  `build_speech_chunks(dual.voice_chunks, dual.voice_chunks_raw,
  request_id=request_id, conversation_id=self.conversation_id)` and
  publishes `payload["chunks"] = [c.to_dict() for c in speech_chunks]`
  instead of the prior sprint's plain `dual.voice_chunks` list of
  strings.
- tests/test_response_output.py: one pre-existing end-to-end test
  (`test_e2e_speak_request_carries_chunks_matching_voice_text`) updated
  to assert the new dict shape and its correlation fields, since the
  wire format it was checking legitimately changed. No other test in
  this file was touched.
- tests/test_tts_chunking.py, tests/test_tts_queue.py,
  tests/test_tts_cancellation.py, tests/test_tts_e2e_pipeline.py (all
  NEW) - see TESTS below.

DIRECTLY AFFECTED SUBSYSTEMS:
- Fish Audio Adapter (`FishAudioAdapter.handle_event()`/`_play()`)
- BehaviorTreeModule._speak() (the one call site building the
  `speak_request` payload)
- `luno/speech_chunk.py` (new, self-contained module)

INDIRECTLY AFFECTED SUBSYSTEMS:
- Barge-In (`luno/barge_in/`) - NOT modified. Used completely
  unmodified, exactly as `tests/test_barge_in_console.py` already does.
- Wake Session (`luno/wake_session/`) - NOT modified. Confirmed by
  reading `_dispatch()`/`_handle_playback_done()` that the existing
  state machine already handles cancellation identically to normal
  completion.
- `fish_audio_real.py` (`RealFishAudioClient`) - NOT modified.
- Console/Dashboard - NOT modified.
- Response Depth Policy / Memory / Relationship / Emotion / Persona -
  NOT touched, not even as a read, beyond what the prior sprint already
  established.

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- `SpeakRequest.data["text"]` - unchanged meaning, always present.
- `SpeakRequest.data["chunks"]` - was optional `List[str]`, is now
  optional `List[str] | List[dict]` (backward compatible - both accepted
  uniformly by `_normalize_chunk_entries()`).
- `SpeechPlaybackStarted`/`Finished`/`Cancelled` - fire exactly once per
  turn, same as before. `Cancelled` never carries an `error` field
  (regression-tested, scenario 34).
- `client.stop()/pause()/resume()`'s global "acts on whatever is
  in-flight" semantics - unchanged, now also correctly covers the
  pre-synthesis window via synchronous token registration.
- Speaking state machine - zero changes; no new enum values added.

EXPECTED REGRESSION RISKS:
- Low for the legacy `List[str]` chunk path and the no-`chunks` legacy
  path - both preserved byte-identically, re-verified by the full
  pre-existing Fish Audio suite (64/64 unchanged) plus the prior
  sprint's own `test_fish_audio_chunking.py` (12/12 unchanged).
- Low-to-moderate for the new cancellation-token/correlation path -
  mitigated by 50 new tests (20 chunking-contract + 10 queue + 17
  cancellation + 3 real-pipeline E2E) covering every cancellable point
  in the lifecycle plus 3 real-console barge-in scenarios.
- One self-inflicted bug during implementation (NameError from an
  incomplete rename in `response_output.py`, caught immediately by the
  Phase 0 baseline test batch, fixed before any test file was written -
  see FIXES below) - not a shipped regression.

TESTS TO RUN:
- python3 -m pytest tests/test_tts_chunking.py tests/test_tts_queue.py tests/test_tts_cancellation.py tests/test_tts_e2e_pipeline.py -q (new, 50 tests)
- python3 -m pytest luno/adapters/tests/test_fish_audio_api.py luno/adapters/tests/test_fish_audio_barge_in.py luno/adapters/tests/test_fish_audio_chunking.py luno/adapters/tests/test_fish_audio_real.py -q (regression, unchanged)
- python3 -m pytest tests/test_barge_in_console.py tests/test_real_fish_audio_console.py tests/test_wake_barge_in_integration.py tests/test_wake_session_console.py tests/test_response_output.py -q (regression, unchanged)
- python3 -m pytest luno/ -q (FAST suite)

NEW TESTS (34-scenario brief, all implemented):
- tests/test_tts_chunking.py (20): scenarios 1-10 (short->1 chunk,
  long->multiple, sentence boundaries, word-boundary fallback, no empty
  chunks, punctuation preserved, markdown/code/url via the existing
  normalizer, mixed Indonesian/English, no-punctuation text,
  normalized-to-empty text) + the SpeechChunk correlation contract
  itself (chunk_id determinism, conversation_id incl. None, sequence/
  total, is_final, deterministic order, to_dict() round-trip, raw_text
  alignment, empty input, and a structural guard proving no second
  segmentation/normalization logic exists in this file).
- tests/test_tts_queue.py (10): scenarios 11-16 + 31-33 (strict chunk
  order, no interleaving between requests, request_id/conversation_id
  correlation, worker survives a TTS failure, chunk_control cleanup
  after completion and after cancellation, TTS/playback exception
  cleanup, empty-response cleanup).
- tests/test_tts_cancellation.py (17): 5 SpeechCancellationToken unit
  tests + scenarios 17-24 + 34 (adapter-level cancellation at every
  lifecycle point, idempotency, remaining chunks never play, stale
  request cannot resume, worker remains usable, cancellation never
  reported as a generic error) + scenarios 25-30 (3 real-console
  barge-in integration tests using `RuntimeDemoConsole`/`MockFishAudioClient`/
  `MockOpenRouterClient`, mirroring `tests/test_barge_in_console.py`'s
  own convention).
- tests/test_tts_e2e_pipeline.py (3, Phase 10): scenario A (long
  response -> chunk -> queue -> fake TTS -> sequential playback ->
  completion, real console pipeline), scenario B (barge-in mid-response
  cancels and resets state, real console pipeline, asserts remaining
  chunks were skipped), scenario C (cancel a request, immediately submit
  a new one, old request produces zero stale playback/completion, new
  request plays normally, real console pipeline).

FIXES DURING IMPLEMENTATION (all resolved before shipping):
1. Self-inflicted NameError in `response_output.py`: mid-rename of
   `_group_sentences_into_chunks` left `build_dual_response()` calling a
   name that no longer existed, breaking it entirely (48 test failures
   on the very next baseline run). Fixed by completing the rename
   properly (`_group_sentences_into_chunk_pairs()` + a genuine thin
   wrapper). Re-verified: 128 passed after the fix.
2. `test_e2e_speak_request_carries_chunks_matching_voice_text` (prior
   sprint's own test) legitimately needed updating once `payload["chunks"]`
   became `List[dict]` instead of `List[str]` - rewritten to assert the
   new shape and its correlation fields, not weakened.
3. `_FailNTimesClient`'s fail-count in an early draft of
   `test_31_tts_exception_cleanup_leaves_adapter_usable` was too low
   relative to the adapter's own bounded-retry policy, so the "all
   chunks fail" request actually completed successfully instead of
   failing - fixed by raising `fail_count` high enough to guarantee
   every call (incl. retries) fails within the test's scope.
4. `tests/test_tts_cancellation.py`'s barge-in section initially used
   wrong `RuntimeDemoConsole` attribute names drafted from assumption
   rather than the real source (`console.fish_audio_client` instead of
   passing `fish_audio_client=` at construction time and reading
   `console.fish_audio_adapter`; `console.openrouter_client` instead of
   `console.openrouter_adapter.client`) - fixed by reading
   `main_runtime_demo.py`'s actual `RuntimeDemoConsole.__init__` before
   writing the fix, then running the file for the first time (17/17
   passed on that run, after one further fix below).
5. `test_24_adapter_remains_usable_for_many_requests_after_cancellation`
   initially asserted strict FIFO completion order across 5 independent,
   back-to-back requests - this is not actually guaranteed by the
   adapter's own pre-existing `_playback_executor`
   (`ThreadPoolExecutor(max_workers=2)`, intentionally sized for the
   paused-reply + Barge-in CONFIRM case, which allows up to 2 independent
   requests in flight concurrently). Fixed by asserting the scenario's
   actual requirement instead (every post-cancellation request
   eventually completes exactly once - `sorted(finished) ==
   sorted(expected)`), not requiring an ordering guarantee the
   architecture never made. Chunk-order-WITHIN-one-request remains
   strictly asserted elsewhere (test_11).

ROLLBACK PLAN:
Revert `main_runtime_demo.py`'s `_speak()` to publish
`payload["chunks"] = dual.voice_chunks` (plain strings) again, revert
`luno/adapters/fish_audio.py`'s `_chunk_control`/`handle_event()`/
`_play()` to the prior sprint's Event-dict-based form, revert
`luno/response_output.py`'s `voice_chunks_raw` addition, delete
`luno/speech_chunk.py`, delete `tests/test_tts_chunking.py`/
`test_tts_queue.py`/`test_tts_cancellation.py`/`test_tts_e2e_pipeline.py`,
and revert the one updated assertion in `tests/test_response_output.py`.
Nothing else in the repository imports `luno.speech_chunk` or reads the
new dict-shaped chunk fields, so no other file needs touching. No
persistent state or config schema was touched.
```

## Regression results (measured, this sprint)

- New TTS test suites (this sprint): `tests/test_tts_chunking.py` (20),
  `tests/test_tts_queue.py` (10), `tests/test_tts_cancellation.py` (17),
  `tests/test_tts_e2e_pipeline.py` (3) - **50/50 passed**, all four files
  together in one pytest process.
- Targeted Fish Audio + barge-in + wake-integration + console +
  response_output sweep (`test_fish_audio_api.py`,
  `test_fish_audio_barge_in.py`, `test_fish_audio_chunking.py`,
  `test_fish_audio_real.py`, `test_barge_in_console.py`,
  `test_real_fish_audio_console.py`, `test_wake_barge_in_integration.py`,
  `test_wake_session_console.py`, `test_response_output.py`, plus the 4
  new TTS files) - **226/226 passed**.
- Full `luno/` FAST suite (820 tests collected) - **813 passed, 7
  failed**. All 7 failures confirmed PRE-EXISTING by isolated re-run
  (each file passes 100% when run alone): 5 in
  `luno/text_normalizer/tests/test_text_normalizer.py` (the
  already-documented `LUNO_LANGUAGE` env-leak bug, see
  `docs/change_impact/tts_chunking_streaming.md`'s own LIMITATIONS
  section and `ARCHITECTURE_GUARD.md` §15) and 2 in
  `luno/barge_in/tests/test_barge_in.py` (the already-documented,
  already-tracked timing-flaky pair, see `ARCHITECTURE_GUARD.md` §13).
  Neither group involves any file this sprint touched.
- `tests/test_dashboard.py` was independently re-confirmed to exceed
  this sandbox's per-command tooling budget (a single test took 27s in
  isolation) and to be order-sensitive under full-suite load - both
  already documented, pre-existing, unrelated-to-TTS facts (see
  `ARCHITECTURE_GUARD.md` §5/§15). Not investigated further, per this
  sprint's own "do not fix unrelated pre-existing failures" instruction.
- `tests/test_main_bargein.py`/`tests/test_root_main_bargein.py` fail at
  COLLECTION time in this sandbox (missing `faster_whisper` module,
  missing `legacy_main.py` file respectively) - both already documented,
  pre-existing, environment-only gaps (`ARCHITECTURE_GUARD.md` §15), not
  reachable by any test runner in this environment regardless of this
  sprint's changes.
- A full unrestricted `pytest tests/ luno/` single-process run could not
  be completed within this sandbox's tooling time budget (consistent
  with the already-documented `tests/test_dashboard.py` timing note
  above, plus real-network-retry delays in at least one OpenAI-provider
  test observed during a partial run) - the targeted sweep above,
  combined with the full `luno/` FAST suite, is the regression evidence
  this report relies on, per `ARCHITECTURE_GUARD.md` §5's own documented
  FAST/FULL split rationale.

## Persistent state verification

All 8 tracked persistent files' SHA256 hashes compared against the
Phase 0 baseline snapshot (`/tmp/baseline_hashes_sprint2.txt`) - all 8
**unchanged**: `config/relationship_state.json`,
`config/long_term_memory.json`, `config/episodic_memory.json` (absent in
both), `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3`. No stray `.tmp`/`.bak` files found
anywhere in the repository.

## Known limitations

- `conversation_id` is carried on every `SpeechChunk` and logged, but
  playback is not actively gated by it - see ARCHITECTURE_GUARD.md's new
  subsection for the full reasoning (would require a new
  "current-conversation" oracle from `SessionManagerModule` that doesn't
  exist today; judged out of this sprint's additive-only scope).
  `request_id`-based cancel-before-publish remains the sole mechanism
  preventing stale audio, and is explicitly tested end-to-end.
- No true token-level early-TTS (unchanged from the prior sprint,
  explicitly out of scope again this sprint).
- The legacy `List[str]` chunk wire format is accepted indefinitely -
  no deprecation was requested or implemented.
- The adapter's `_playback_executor` allows up to 2 independent
  requests to be in flight concurrently (pre-existing sizing, unrelated
  to and unchanged by this sprint) - completion order across different,
  back-to-back requests is therefore not strictly FIFO-guaranteed by the
  architecture, only chunk order WITHIN one request is (see FIXES #5
  above; this is a pre-existing architectural fact this sprint's own
  test-writing process surfaced, not a new behavior).
