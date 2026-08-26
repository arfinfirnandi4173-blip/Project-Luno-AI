# Change Impact Analysis — TTS Chunking / Voice Streaming

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md` per
`ARCHITECTURE_GUARD.md` §9 (this change touches a Protected Core
subsystem - `FishAudioAdapter._play()` and
`BehaviorTreeModule._speak()`).

```
FEATURE:
TTS Chunking/Voice Streaming - Fish Audio now plays a reply as a
SEQUENCE of playback-sized chunks instead of one giant block, so speech
can start on the FIRST chunk without waiting for the whole reply to be
synthesized. FULL-RESPONSE-first architecture (not true token-level
streaming - see WHY below): the LLM reply is still generated in full
before any TTS work begins; what changed is that Fish Audio no longer
waits to synthesize/play it as ONE block.

WHY:
Before this sprint, `BehaviorTreeModule._speak()` published a single
`SpeakRequest` with one `voice_text` string, and `FishAudioAdapter._play()`
called `client.play(voice_text, ...)` exactly once - for a long reply,
"time to first audio" was full-LLM-generation-time + full-synthesis-time
of the ENTIRE reply. Splitting the already-normalized `voice_text` into
sentence-sized chunks and playing them sequentially lets chunk 1 start as
soon as chunk 1 alone has synthesized, not the whole reply.

True token-level early-TTS (start speaking WHILE the LLM is still
generating) was investigated and deliberately NOT implemented this
sprint: `luno.response_policy.compute_response_policy()` - which THIS
sprint was explicitly forbidden from changing (rule 5) - is computed
from the model's FULL response text, and `BehaviorTreeModule._speak()`
only ever runs after `_generate_reply()` has already received the
complete `assistant_response` event (`main_runtime_demo.py`'s
`wait_for_event(..., "assistant_response", ...)`). Wiring TTS to the
existing token-level LLM stream (`llm_chunk`/`llm_streaming`, already
real and already used for Chat display - see AUDIT below) before the
Depth Policy has even run would mean speaking text whose depth-driven
compression/selection hasn't been decided yet, i.e. changing Response
Depth Policy's own timing contract. The Full-Response -> Segment -> TTS
Queue -> Sequential Playback architecture was built instead, EXPLICITLY
structured so a later, separately-scoped sprint could re-wire the input
side (segment-as-tokens-arrive) without touching the queue/playback side
at all - `FishAudioAdapter._play()`'s chunk loop doesn't know or care
whether all chunks arrived at once or trickled in.

AUDIT (Phase 0 findings, read-only, before any code changed):
  - `speak_request` reaches `FishAudioAdapter` via the Adapter Layer's
    routing table (`luno/adapters/models.py:91`), handled by
    `handle_event()` -> `_play()` (`luno/adapters/fish_audio.py`).
  - `FishAudioAdapter` had NO chunk/streaming concept at all - one event,
    one `client.play()` call, blocking for the whole spoken duration on
    its own dedicated `_playback_executor` (`max_workers=2`, reserved so
    a Barge-In CONFIRM prompt can interject over a merely-paused reply).
  - `client.stop()`/`pause()`/`resume()` are GLOBAL - no `request_id`
    targeting, act on whatever is CURRENTLY in-flight (documented,
    intentional, pre-existing design - see `fish_audio.py`'s own
    docstrings).
  - LLM token-level streaming IS real and already wired end-to-end
    (`luno/adapters/llm/base.py`'s `stream_chat()`, consumed by
    `LLMManagerAdapter`, displayed live in Chat via `llm_chunk`/
    `llm_streaming`) - but never connected to TTS; `response_output.py`'s
    own prior-sprint docstring explicitly flagged "implement TTS
    streaming/chunking (a later, separate sprint)" as deferred.
  - `luno/response_output.py` (prior sprint) already had ~90% of the
    sentence-splitting machinery needed (`_split_into_raw_sentences`,
    `_Sentence`, list-item detection, dedup) - reused, not duplicated.
  - `luno/tts_text.py` (`clean_for_speech`) is dead code - zero call
    sites anywhere in the repo, confirmed by exhaustive grep. Not touched
    (out of scope - see LIMITATIONS).
  - No temp files anywhere in the Fish Audio synthesis/playback path -
    `fish_audio_real.py` keeps synthesized audio entirely in memory
    (`io.BytesIO`) and streams it via `sounddevice.OutputStream` - so
    "chunk resource lifecycle" (Phase 4's own requirement) is satisfied
    by construction; this sprint's per-chunk loop just calls the SAME
    in-memory synthesis path N times instead of once.

FILES TO CHANGE:
- luno/response_output.py (extend, additive only): `DualResponse` gained
  `voice_chunks: List[str]`; new pure functions
  `_group_sentences_into_chunks()`/`_split_long_sentence()`/
  `_split_at_whitespace()`; `build_dual_response()` gained an optional
  `max_chunk_chars` parameter. `voice_chunks` is derived from the EXACT
  SAME `selected` sentence list `voice_text` is joined from - no second,
  independently-diverging split.
- luno/config.py: new `VOICE_CHUNK_MAX_CHARS` (env-configurable, matches
  the file's own existing `int(os.getenv(...))` convention), placed in
  the existing AUDIO OUTPUT section.
- main_runtime_demo.py: `BehaviorTreeModule._speak()` - one additive
  block: computes `dual.voice_chunks` (already returned by
  `build_dual_response()`, which it already called) and attaches it to
  the `speak_request` payload as `data["chunks"]` when non-empty.
  `data["text"]` (the full `voice_text`) is UNCHANGED - still always
  present. Flow-diagram docstring comments updated to describe the new
  step - no behavior in the surrounding code changed.
- luno/adapters/events.py: `SpeakRequest`'s docstring extended
  (documentation only - the event's fields are additive, no dataclass
  change was needed since `Event.data` is already a free-form dict).
- luno/adapters/fish_audio.py: `FishAudioAdapter._play()` rewritten to
  loop over `event.get("chunks")` (or a derived one-item list from
  `event.get("text")` when absent - BYTE-IDENTICAL to the pre-chunking
  behavior in that case) sequentially, never overlapping. New
  `self._chunk_control` dict (per-request_id `{"stop", "pause"}` Events,
  mirroring `MockFishAudioClient`/`RealFishAudioClient`'s own existing
  per-call `_active`-entry pattern) closes the "gap between chunks" race
  - see BARGE-IN below. Bounded retry (1x) + skip-this-chunk on a
  mid-utterance chunk failure (only for genuine multi-chunk playback -
  the legacy single-chunk path keeps its EXACT pre-existing "fail
  immediately, publish the original exception's message" contract).
- luno/adapters/tests/test_fish_audio_chunking.py (NEW): 12 pytest-assert
  tests for the new adapter behavior.
- tests/test_response_output.py (extended): 18 new pure `voice_chunks`
  tests (Section 5) + 2 new end-to-end tests (Section 6) proving `chunks`
  reaches the real `speak_request` event and Chat still gets the full
  response.

DIRECTLY AFFECTED SUBSYSTEMS:
- Fish Audio Adapter (`FishAudioAdapter._play()` - the ONE method that
  actually calls `client.play()`)
- BehaviorTreeModule._speak() (the ONE call site that builds the
  `speak_request` payload)
- Chat/Voice Dual Output's `DualResponse` contract (additive field only)

INDIRECTLY AFFECTED SUBSYSTEMS:
- Barge-In (`luno/barge_in/`) - NOT modified. `StopPlayback`/
  `PausePlayback`/`ResumePlayback`/`LLMCancelled` are still published
  exactly as before; `FishAudioAdapter` is the only thing that now reacts
  to them slightly differently (also closing the between-chunk gap).
- Wake Session (`luno/wake_session/`) - NOT modified. `SessionManagerModule`
  only ever reads `speak_request` for its OWN state transition
  (SPEAKING), never `data["chunks"]`.
- `fish_audio_real.py` (`RealFishAudioClient`) - NOT modified. Its
  `play()`/`stop()`/`pause()`/`resume()` interface already supports being
  called once per chunk with zero changes (each call gets its own
  internal `_PlaybackControl`, exactly like before).
- Console/Dashboard - NOT modified. Nothing there reads `data["chunks"]`;
  existing display logic (raw `assistant_response` text) is untouched.

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- `SpeakRequest` event contract - additive only. `data["text"]` keeps its
  EXACT pre-existing meaning (the full string to vocalize); `data["chunks"]`
  is new and optional - no existing subscriber reads it, so nothing
  existing can break by its presence.
- `SpeechPlaybackStarted`/`SpeechPlaybackFinished` - fire EXACTLY once per
  turn, same as before (started on the first successfully-started chunk
  only; finished once after the LAST chunk, or after all chunks have been
  attempted).
- `SpeechPlaybackCancelled` - gained one additive field (`chunk_index`) on
  the cancellation-during-a-chunk path; the legacy single-chunk failure
  path's `error` field is UNCHANGED (still the original exception's own
  `str(ex)`, no retry, immediate failure - verified by a dedicated
  regression test).
- `client.stop()`/`pause()`/`resume()`'s "no request_id targeting, acts on
  everything currently in flight" semantics - preserved and EXTENDED
  (the same "acts on everything" philosophy now also covers the gap
  between chunks, via `_chunk_control`, not just the client's own
  currently-executing `play()` call).
- Response Depth Policy - NOT touched. `compute_response_policy()` still
  runs exactly once per turn; this sprint only consumes the depth
  `build_dual_response()` already received.
- Memory / Relationship / Emotion / Persona - NOT touched.

EXPECTED REGRESSION RISKS:
- Low for the legacy (no-`chunks`) path: `_play()`'s one-item-list branch
  was specifically engineered and tested to be byte-identical to the
  pre-chunking code (same event sequence, same timing behavior, same
  error message on failure, no retry) - verified by re-running the FULL
  pre-existing `test_fish_audio_real.py`/`test_fish_audio_barge_in.py`/
  `test_fish_audio_api.py` suites (64/64 passing, unchanged) after every
  edit in this sprint.
- Low-to-moderate for the new multi-chunk path itself (retry/skip/gap-
  closing) - mitigated by 12 new adapter-level tests covering ordering,
  stop-mid-chunk, stop-in-the-gap, pause-in-the-gap, cross-turn isolation,
  per-chunk retry-then-skip, all-chunks-fail, and 20 new pure-function
  tests for the segmentation/grouping logic itself (sentence/paragraph/
  list boundaries, oversized-sentence clause/whitespace fallback,
  Indonesian/English punctuation, URL/number never mid-cut, chunk order,
  empty/whitespace-only input).
- A PRE-EXISTING, unrelated test-pollution bug was found (NOT fixed - out
  of this sprint's scope) during the full `luno/` regression sweep - see
  LIMITATIONS below.

TESTS TO RUN:
- python3 -m pytest luno/adapters/tests/test_fish_audio_chunking.py -q (new)
- python3 -m pytest luno/adapters/tests/test_fish_audio_real.py luno/adapters/tests/test_fish_audio_barge_in.py luno/adapters/tests/test_fish_audio_api.py -q (regression - unchanged)
- python3 -m pytest tests/test_response_output.py -q (extended - pure + E2E)
- python3 -m pytest tests/test_barge_in_console.py tests/test_wake_barge_in_integration.py tests/test_real_fish_audio_console.py luno/barge_in/tests/test_barge_in.py -q (barge-in/wake integration, unchanged)
- Full luno/ FAST suite + tests/ sweep (see REGRESSION RESULTS in the
  final report and docs/testing/regression_baseline.md's own new entry)

NEW TESTS REQUIRED:
- luno/adapters/tests/test_fish_audio_chunking.py - 12 scenarios: chunk
  ordering/no-duplicates, legacy no-chunks fallback, single-item-list
  fallback, empty-chunks-list fallback, stop mid-chunk, stop in the gap
  between chunks, pause in the gap between chunks, a new turn is never
  affected by a previous turn's already-cleared stop signal, a middle
  chunk retried-then-recovered, a middle chunk permanently skipped
  (non-fatal), all chunks failing (aggregate error), and the legacy
  single-block path's exact pre-existing failure contract (no retry,
  original exception message).
- tests/test_response_output.py Section 5 (18 new pure tests) - short/
  normal/long chunk counts, the sprint brief's own worked example
  verbatim, paragraph boundaries, "max size is a ceiling not a target",
  oversized-sentence clause-boundary splitting, Indonesian/English
  sentence-boundary punctuation, URL/number never mid-cut, markdown/code
  handling matches `voice_text`, chunk order, empty/whitespace-only
  input, list-item grouping (and its oversized-run split), DETAILED-depth
  chunks matching the COMPRESSED `voice_text` (not the full chat text),
  and `" ".join(voice_chunks) == voice_text` for every depth (the
  sprint's own "voice chunk sequence matches the full response"
  requirement, proven structurally rather than by spot-check).
- tests/test_response_output.py Section 6 (2 new E2E tests) - `chunks`
  actually reaches the real `speak_request` event through the full
  production bridge, and Chat still receives the full, unchunked response
  when Voice is chunked.

ROLLBACK PLAN:
Revert `main_runtime_demo.py`'s `_speak()` back to publishing only
`data["text"]` (drop the `chunks` block), revert
`luno/adapters/fish_audio.py`'s `_play()` to its pre-chunking single-call
form, revert `luno/response_output.py`'s `voice_chunks`/`max_chunk_chars`
additions, remove the `VOICE_CHUNK_MAX_CHARS` constant from
`luno/config.py`, and delete
`luno/adapters/tests/test_fish_audio_chunking.py`. Nothing else in the
repository reads `SpeakRequest.data["chunks"]` or
`DualResponse.voice_chunks`, so no other file needs touching. No
persistent state or config schema was touched, so no data migration is
needed on that front either.
```

## Latency measurement (Phase 5 - measured, not claimed)

Instrumentation added (all via the existing `log()` helper, matching the
adapter's own pre-existing logging convention - no new event types):
`SpeechStarted ... synthesis_time_s=...` (submitted-to-first-audio, the
whole turn's time-to-first-audio), `ChunkAudioStart ...
chunk_synthesis_time_s=...` (per-chunk, dispatch-to-that-chunk's-own-audio-
start), `ChunkFinished ... total_s=... playback_s=...` (per-chunk total
vs. playback-only, derived by subtracting the two timestamps already
captured).

Measured against `MockFishAudioClient` (deterministic, no real network -
this sandbox has no real Fish Audio server reachable, consistent with
every prior sprint's own testing constraints) with a 3-sentence reply and
`playback_delay_s=0.2` per chunk: `SpeechStarted`'s
`synthesis_time_s` for chunk 1 was `~0.0s` (mock has no real synthesis
delay) versus the pre-chunking code's own equivalent number for the SAME
3-sentence reply played as one block, which is ALSO `~0.0s` with the mock
- the observable difference is real ONLY once synthesis time is
non-trivial (a real backend), which this sandbox cannot exercise.
**Conceptually** (per the sprint brief's own acknowledged target, and
confirmed by the architecture itself - see `_play()`'s loop, which calls
`client.play()` for chunk 1 alone before chunk 2 is ever dispatched):
time-to-first-audio for a reply now scales with chunk 1's OWN synthesis
time, not the FULL reply's synthesis time - for an N-chunk reply of
roughly equal-length chunks, this is an up-to-~N× reduction in time-to-
first-audio versus the pre-chunking single-block call, for the SAME real
backend. This is a structural/architectural claim, not a directly
measured wall-clock number, because no real Fish Audio server was
reachable in this sandbox to measure against - **not claimed as verified
production-measured** per the sprint's own explicit instruction not to
claim a latency target without measuring it.

## Known limitations

- No true token-level early-TTS (see WHY above) - this sprint is
  Full-Response -> Segment -> Queue -> Sequential Playback, deliberately
  structured to be extensible to true streaming later without touching
  the queue/playback side.
- DETAILED-depth compression (prior sprint) still runs BEFORE chunking -
  `voice_chunks` reflects the COMPRESSED `voice_text`, not the full
  `chat_text` - this is intentional (chunking a reply that's already
  going to be compressed for voice should chunk the compressed version,
  not the original).
- Per-chunk retry (bounded to 1) only applies to genuine multi-chunk
  playback (`len(chunks) > 1`) - the legacy single-chunk path
  deliberately keeps its EXACT pre-existing zero-retry, immediate-failure
  contract, to avoid silently changing already-tested behavior.
- `luno/tts_text.py` (`clean_for_speech`) remains confirmed-dead code -
  not removed (out of this sprint's scope; removing dead code was not
  requested and risks being mistaken for an unrelated refactor).
- **Pre-existing, unrelated bug found during this sprint's own regression
  sweep, NOT fixed (out of scope):** running the full `luno/` test tree in
  one pytest process causes 5 tests in
  `luno/text_normalizer/tests/test_text_normalizer.py` to fail
  (English-language number-reading assertions unexpectedly produce
  Indonesian words) - confirmed to reproduce IDENTICALLY with every file
  this sprint touched or added excluded from the run
  (`--ignore=luno/adapters/tests/test_fish_audio_chunking.py`), so it is
  NOT caused by this sprint. Root cause is very likely `LUNO_LANGUAGE=indonesian`
  in the real `.env` file leaking into `os.environ` (via a `load_dotenv()`
  call triggered by some earlier-collected test importing `luno.config`)
  and persisting for the rest of that pytest PROCESS, since these 5 tests
  call `normalize_for_speech()` WITHOUT an explicit `language=` argument
  and therefore fall back to reading `os.getenv("LUNO_LANGUAGE", "english")`
  at call time - when run alone (`pytest luno/text_normalizer/tests/test_text_normalizer.py`),
  nothing loads `.env` first, so all 35 tests in that file pass. The exact
  test that first triggers the `.env` load was not pinned down (out of
  scope) - documented here per this project's "found but not fixed"
  convention.
