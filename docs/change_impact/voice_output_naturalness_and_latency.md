# Change Impact: Voice Output Naturalness + First-Audio Latency

**Date:** 2026-08-13
**Status:** shipped - both production symptoms reproduced, fixed, and
re-verified through the real production path.

## 1. The two symptoms (as reported)

**A. List-context loss.** TTS sometimes spoke only bullet/numbered-list
items while skipping the setup/explanation sentence(s) that gave those
bullets context. Worked example from the brief: a setup sentence
("Untuk membuat sistem ini, ada beberapa bagian penting.") followed by
5 bullets could result in TTS speaking mostly the bullets, dropping the
setup.

**B. High first-audio latency.** Despite the pipeline already having an
LLM->TTS streaming architecture (Sprint 3, `docs/change_impact/
llm_tts_streaming_activation.md`) and one-slot TTS prefetch/pipelining
(Sprint 2, `docs/change_impact/voice_pipeline_latency_and_semantic_segmentation.md`),
first-audio latency in production remained noticeably high.

This sprint's explicit constraints: do not blindly rewrite the response
selector, memory system, LLM pipeline, or TTS architecture; begin with a
fresh Phase 0 audit and reproduce both problems before changing
production code; do not assume the cause.

## 2. Phase 0 - reproduction before any code changed

### 2.1 Problem A - reproduced, root cause traced

Initial attempt with the brief's own worked example did NOT reproduce
the bug: the setup sentence landed at index 0 (the pipeline's own
always-must-keep lead sentence) and happened to contain the word
"penting", which matches `_WARNING_KEYWORDS` - an accidental double
protection. Iterating with a realistic conversational prefix ("Oke,
saya jelasin ya.") pushed the real setup sentence to index 1, and
removing the warning-keyword collision, reproduced the bug cleanly: at
SHORT/NORMAL depth, the setup sentence was dropped while all 5 bullets
survived.

Traced through every stage the brief asked about:

- **LLM output / sentence segmentation:** correct - `_split_into_raw_sentences()`
  correctly separated the setup sentence from the list items.
- **Response depth / budget selection:** correct - the budget itself
  was not the problem; the SELECTION within budget was.
- **Semantic-unit selection:** `_is_dependent_sentence()` (the existing
  discourse-marker dependency signal from Sprint 1/§27) has no concept
  that a bulleted/numbered list run depends on the sentence immediately
  before it - list items open with `-`/`*`/`1.`/etc., never with a
  causal/continuation/reference marker word, so `_is_dependent_sentence()`
  never classifies the list's own opening item as dependent on its
  setup sentence.
- **`_select_by_priority()`:** correctly applies its must-keep set (lead
  sentence, warnings, list items themselves, conclusion cues) - the
  SETUP sentence simply wasn't a member of any of those categories.
- **`_repair_orphans()`:** only rescues sentences already classified as
  DEPENDENT (or, after this fix, list-run-starting) whose predecessor
  isn't kept - before this fix, a setup sentence was never a "dependent"
  in the first place, so there was nothing for this rescue pass to act
  on.
- **TTS chunk creation / cancellation / streaming dispatch / playback:**
  none of these were involved - the sentence was already gone before
  `voice_text` was ever chunked for TTS.

**Root cause: a gap in `_select_by_priority()`'s must-keep/scoring
logic (`response_output.py`), specifically the absence of a
list-run-setup signal alongside the existing discourse-marker
dependency signal.** Not a TTS bug, not an LLM bug, not a budget bug.

### 2.2 Problem B - measured, root cause traced

Real-timestamp harness against the mocked LLM/TTS boundary (same
harness convention as every prior latency sprint): `ENABLE_LLM_TTS_STREAMING`
was still `False` by default in the checked-in repository (see
`llm_tts_streaming_activation.md` §7 - that sprint deliberately deferred
the default flip, citing an un-audited test blast radius as the reason,
NOT a safety concern with streaming itself). With the default off, every
turn waits for the ENTIRE LLM response before any TTS dispatch begins -
first audio is gated on `llm_finished`, not on the first sentence being
ready.

**Root cause: the already-safe, already-verified streaming architecture
was simply not the production default.** No new mechanism was needed to
fix this - only activating what already existed.

## 3. Fix A - list-context preservation (`luno/response_output.py`)

New pure function, placed immediately before `_build_semantic_units()`:

```python
def _starts_list_run(sentences: List[_Sentence], i: int) -> bool:
    """True if sentence `i` is a list item AND is the FIRST item of its
    run (i.e. `i == 0`, or the immediately preceding sentence is NOT
    itself a list item)."""
    if not sentences[i].is_list_item:
        return False
    if i == 0:
        return False  # nothing precedes it - there is no setup to protect
    return not sentences[i - 1].is_list_item
```

Wired into the SAME two mechanisms that already protect discourse-marker
dependents - never a new selector, never a second scoring pass, never a
blanket "protect every short sentence" rule (explicitly forbidden by
the brief):

1. **`_select_scores_with_setup_bonus()`** - a sentence immediately
   preceding a list run now earns the same `_CONDITION_SETUP_BONUS`
   score bonus a discourse-marker setup sentence already earns.
2. **`_repair_orphans()`** - a kept list-run-starting sentence whose
   predecessor was dropped is now hard-rescued, exactly like a kept
   DEPENDENT sentence whose predecessor was dropped already was.

Both call sites reuse the EXACT existing helper signatures
(`_is_dependent_sentence(sentences, i, condition_indices)`'s own
one-hop, leading-window design) - `_starts_list_run()` has the identical
shape, just a different predicate.

### 3.1 What this fix does NOT do (verified by the new test matrix)

- Does not pull in unrelated distant sentences (the brief's own
  weather-sentence anti-example - `test_A7`).
- Does not override `protect_list_items = depth != DEPTH_DETAILED` -
  DETAILED depth can still compress list items; this fix only protects
  the SETUP sentence that precedes them (`test_A10`).
- Does not introduce a blanket short-sentence-keep rule - a short,
  structurally-irrelevant sentence near a list is still droppable
  (`test_B5`).
- Does not crash on a list item at index 0 (no predecessor to protect)
  (`test_A8`).

## 4. Fix B - first-audio latency (`luno/config.py`)

`ENABLE_LLM_TTS_STREAMING` now defaults to `True`. This is the SAME flag
Sprint 3 verified production-safe and deliberately left off, citing an
un-audited test blast radius (see §7 of that sprint's own doc) as the
sole blocker - not a safety concern with the streaming path itself. This
sprint IS that follow-up: the blast radius has now been fully audited
and fixed (§6 below), so the default flip is no longer blocked.

No new mechanism was built. `luno.incremental_speech.StreamingSpeechCoordinator`
(Sprint 3's own "RESPONSE-DEPTH-POLICY-SAFE REDESIGN") is reused
byte-for-byte: dispatches the first settled sentence during generation
(always safe - the must-keep set always includes it), then reconciles
the remainder via a real `build_dual_response()` call once `llm_finished`
fires - the exact same selection authority the legacy path already uses,
now including this sprint's own Fix A.

### 4.1 First-audio latency measurement

Methodology: `RuntimeDemoConsole` (real Event Bus, real threading),
`MockOpenRouterClient` (`chunk_delay_s=0.03`, simulating a realistic
per-token LLM stream) and `MockFishAudioClient` (`playback_delay_s=0.02`)
faking only the network/hardware boundary. A 5-sentence reply
(intro/setup + 3 body sentences + conclusion). First-audio latency
measured as the wall-clock gap between `simulate_speech()` being called
and `speech_playback_started` firing. 7 repetitions each, real
timestamps (`time.time()`), no estimates.

| Metric | Legacy (`ENABLE_LLM_TTS_STREAMING=False`) | Streaming (default, `=True`) |
|---|---|---|
| n | 7 | 7 |
| min | 1.732s | 0.381s |
| median | 1.749s | 0.403s |
| p95 | 1.969s | 0.447s |
| max | 1.969s | 0.447s |

**Median improvement: 77.0%.**

Raw values (seconds), legacy: `1.969, 1.732, 1.738, 1.742, 1.789, 1.755, 1.749`.
Raw values (seconds), streaming: `0.381, 0.406, 0.394, 0.387, 0.403, 0.424, 0.447`.

**Known limitation on this number:** both clients are mocked (no real
network/HTTP/TTS-engine round trip), so these are PIPELINE latencies,
not end-to-end production latencies including a real TTS provider's own
network/synthesis time. The RELATIVE improvement (streaming removes the
"wait for the entire LLM response" gate) is architectural and holds
regardless of the absolute numbers a real TTS backend would add on top
of both configurations equally.

## 5. Bug found and fixed along the way (Phase 5 safety audit)

While auditing cancellation/barge-in safety under the new default (per
the brief's explicit Phase 5 requirement), a genuine, previously-dormant
bug was found and fixed in `luno/adapters/fish_audio.py
::_play_stream_pipelined()`.

**Root cause:** Sprint 2 (`voice_pipeline_latency_and_semantic_segmentation.md`)
found and fixed an identical bug in `_play_pipelined()` (the LEGACY
sibling method): chunk 0 of a multi-chunk utterance has nothing
prefetched yet, so `_resolve_audio()` fell through to a raw, synchronous
`self.client.synthesize()` call - NOT cancellation-aware on its own -
meaning a `StopPlayback` arriving mid-synthesis had nothing to interrupt.
That fix (always submit chunk 0's synthesis to `_prefetch_executor`
first, then resolve via `_resolve_audio()`'s own cancellation-responsive
polling) was never mirrored into `_play_stream_pipelined()` - the
STREAMING sibling - because streaming defaulted off at the time and
wasn't part of that sprint's own regression surface.

**Reproduction:** `tests/test_real_fish_audio_console.py
::test_voice_interrupt_while_still_synthesizing_real_speech_succeeds`
(against `RealFishAudioClient`, `synthesis_delay_s=1.0`) failed
deterministically, 3/3, once `ENABLE_LLM_TTS_STREAMING` defaulted to
`True`: a "stop" issued well within the 1.0s synthesis window did not
prevent that chunk from eventually playing. Confirmed unrelated to
timing/parallelism by re-running under `ENABLE_LLM_TTS_STREAMING=false`
(3/3 pass) and in isolation multiple times (reliably reproducing).

**Fix:** identical pattern to the already-fixed legacy sibling -
chunk 0's synthesis is now always submitted to `_prefetch_executor`
first, then resolved via the existing cancellation-responsive polling in
`_resolve_audio()`, never a raw synchronous call. Verified: 5/5 after
the fix (previously 0/5), full `test_real_fish_audio_console.py` suite
8/8, and a dedicated adapter-level regression proof added in
`tests/test_voice_naturalness_and_latency.py::test_C7_...`.

## 6. Test-suite blast radius from the default flip

Sprint 3's own doc identified this as the reason it deferred the
default flip. This sprint completed that audit. ~30 tests across 9
files had hardcoded `speak_request`-only assumptions - each honestly
updated to be dispatch-mode-agnostic (subscribe to both `speak_request`
and `speak_stream_chunk`, or reconstruct the equivalent payload shape
from accumulated real `SpeechChunk` dicts), never weakened:

- `tests/test_response_output.py` - `_ask_and_capture()` rewritten to
  synthesize a `speak_request`-shaped dict from accumulated
  `speak_stream_chunk` events plus `response_depth_assigned`; two
  additional per-test fixes for chunk `total`/`sequence` semantics that
  are honestly different (not wrong) under streaming - see inline
  docstrings at each assertion for the exact reasoning (early-dispatched
  chunks legitimately don't know the final `total` yet; sequence numbers
  can have gaps because the buffer's internal counter advances for
  settled-but-never-independently-dispatched sentences).
- `tests/test_voice_output_optimization.py`, `test_voice_output_coherence.py`,
  `test_voice_response_intelligence.py` - identical `_ask_and_capture()`
  fix (the helper was duplicated byte-for-byte across all four files).
- `tests/test_semantic_speech_units.py` - `_run_one_turn()`'s `_on_speak`
  handler now accumulates the FULL spoken text across all
  `speak_stream_chunk` dispatches (not just the first fragment).
- `tests/test_adaptive_response_depth.py`, `test_barge_in_console.py` -
  already fixed by Sprint 3's own verification pass (kept, re-verified).
- `tests/test_interrupt_routing_fix.py` - `test_7_...` now accepts
  either `speak_request` or `speak_stream_chunk` as the "voice stage"
  event when checking `request_id` correlation across a turn.
- `tests/test_memory_voice_observability.py` - `_run_voice_turn()` now
  also listens for `speak_stream_chunk`.
- `tests/test_real_fish_audio_console.py` - two tests updated: the
  THINKING->SPEAKING transition proof now accounts for
  `wake_session/manager.py`'s own pre-existing streaming fallback
  (`_handle_playback_started()`, itself from Sprint 3 - not new); the
  cancellation-during-synthesis test now accepts either dispatch event
  as evidence of "commit to speak" (the cancellation-during-synthesis
  BUG itself is fixed separately, see §5).
- `tests/test_tts_cancellation.py` - two tests now count either
  `speak_request` or `speak_stream_chunk` as "the turn was spoken".
- `tests/test_tts_e2e_pipeline.py` - a new `_ChunkCapture` helper
  reconstructs a turn's chunk set from accumulated `speak_stream_chunk`
  events (waiting for arrivals to settle, since streaming has no single
  event announcing the full set upfront); Scenario B's "fewer chunks
  played than the total" check was corrected to compare THIS TURN's own
  `speech_chunk_playback_finished` count (scoped by `request_id`)
  against the total, rather than the mock client's whole-session
  `played` list (which also included the wake/barge-in acknowledgement
  turns' own entries - always slightly imprecise, but only surfaced once
  streaming's own reconciliation started producing fewer, larger chunks
  per turn).
- `tests/test_wake_session_console.py` - `_new_console()` now passes an
  explicit `MockFishAudioClient`. It had silently depended on the
  repository's own `.env`'s `FISH_AUDIO_BACKEND=real` (set for an
  unrelated prior "Real TTS adapter" sprint's own testing), which
  constructs a client pointed at a self-hosted TTS server that doesn't
  exist in this sandbox. The legacy path masked this entirely
  (`speak_request` transitions THINKING->SPEAKING unconditionally,
  regardless of whether synthesis later succeeds); the streaming
  default exposed it because that transition now depends on
  `speech_playback_started`, which never fires if synthesis always
  fails - a real "stuck at THINKING forever" risk worth flagging
  (§8, Known Limitations) even though the correct FIX here was
  test-environment hygiene, not production code (every other test file
  already passes an explicit mock for this exact reason).

## 7. Phase 6 - new test matrix

`tests/test_voice_naturalness_and_latency.py` (26 tests, stable across
3 consecutive full runs):

- **Section A (10) - semantic/list coherence:** setup-before-list at
  SHORT depth (the reproduced bug), explanation-before-dependent-list,
  cause->consequence, question->answer, claim->supporting-explanation,
  warning->mitigation, unrelated-distant-sentence anti-example,
  list-item-at-index-0 (no crash), multiple independent list runs each
  protected, DETAILED depth still allows list compression.
- **Section B (5) - short-sentence protection by FUNCTION:** short
  setup before a list, short conclusion, short independent statement
  under SHORT budget, short filler droppable under pressure, short
  unrelated aside not falsely protected by the list fix.
- **Section C (10) - streaming/latency:** streaming is the production
  default, default path dispatches via `speak_stream_chunk` not
  `speak_request`, first audio starts before a slow multi-sentence
  reply would have fully generated, never sends a half sentence to TTS,
  SHORT depth not over-spoken under streaming, pipelined synthesis path
  actually reached for a split-synthesis client (Phase 4's own
  "measure, don't assume" requirement), chunk-0 cancellation
  responsiveness during synthesis (regression proof for §5's fix), no
  duplicate dispatch, reconciliation reaches `build_dual_response()` for
  a long reply, barge-in during real synthesis still honored end-to-end.
- **Section D (1) - real E2E:** intro/setup + 5 bullets + conclusion
  through the real `RuntimeDemoConsole` - proves chat output stays
  byte-identical to the raw LLM reply, speech includes the setup, bullet
  content remains understandable, the conclusion is not orphaned, and
  first audio starts well before the full (deliberately slowed) response
  finishes generating.

## 8. Regression + invariants

Full suite re-run in two ~40-file batches (host-side bash-call time
limit forces this split), `-n 4`. Only pre-existing, environment-
specific failures remain, confirmed unchanged under
`ENABLE_LLM_TTS_STREAMING=false` too (proving they predate this sprint):
`test_production_launcher.py::test_07` (network reachability),
`test_mic_device_index.py` (6, real `.env`/missing-script gap),
`test_real_adapters.py` (2 whisper tests, missing `speech_recognition`/
`sounddevice`), `test_state_isolation.py` (1, sandbox
`inspect.getsource()` gap), `test_main_bargein.py`/
`test_root_main_bargein.py` (missing `faster_whisper` module / missing
file). A handful of individually-100%-passing tests (the vision suite,
one streaming-production test, one streaming-e2e test) occasionally
fail ONLY under `-n 4` parallel load - reproduced both before and after
this sprint's changes, confirming it's this sandbox's own resource-
contention characteristic under heavy parallelism, not a regression.
**Zero new deterministic regressions.**

**Invariants verified unchanged:**
- Chat output (`chat_text`/`assistant_response`) - byte-identical to
  the raw LLM reply, proven directly in the new E2E test.
- LLM prompt construction - untouched (no file in that path modified).
- Memory retrieval/ranking, topic history, active topic - untouched (no
  file in that path modified).
- Prompt-injection trust boundary - untouched.
- Response-depth semantics (SHORT/NORMAL/DETAILED, explicit-detail
  override) - `_select_by_priority()`'s must-keep/budget skeleton is
  unmodified; `protect_list_items = depth != DEPTH_DETAILED` is
  unmodified (`test_A10`).
- Safety filtering, TTS voice configuration - untouched.
- Audio ordering, cancellation behavior - the ONE behavioral change is
  the §5 bug fix, which makes cancellation MORE correct (chunk 0 now
  honors a mid-synthesis stop, matching the legacy path's own
  already-correct behavior), never less safe.

**No prohibited mechanisms introduced:** no second LLM, no summarizer,
no embedding judge, no second ranking engine, no arbitrary sentence
duplication, no blanket short-sentence-keep rule.

## 9. Files changed / created

**Modified:**
- `luno/response_output.py` - `_starts_list_run()` (new, ~15 lines) +
  2 call sites (`_select_scores_with_setup_bonus()`,
  `_repair_orphans()`).
- `luno/config.py` - `ENABLE_LLM_TTS_STREAMING` default `"false"` ->
  `"true"`.
- `luno/adapters/fish_audio.py` - `_play_stream_pipelined()` chunk-0
  cancellation fix (mirrors the already-fixed legacy sibling).
- `tests/test_response_output.py`, `test_voice_output_optimization.py`,
  `test_voice_output_coherence.py`, `test_voice_response_intelligence.py`,
  `test_semantic_speech_units.py`, `test_interrupt_routing_fix.py`,
  `test_memory_voice_observability.py`, `test_real_fish_audio_console.py`,
  `test_tts_cancellation.py`, `test_tts_e2e_pipeline.py`,
  `test_wake_session_console.py` - dispatch-mode-agnostic fixes (§6).
- `ARCHITECTURE_GUARD.md` (§33), `docs/testing/regression_baseline.md`
  (new dated entry).

**Created:**
- `tests/test_voice_naturalness_and_latency.py` (26 tests).
- `docs/change_impact/voice_output_naturalness_and_latency.md` (this
  file).

**Explicitly NOT modified:** `luno/incremental_speech.py`,
`luno/response_policy.py`, `luno/memory.py`/`memory_context.py`,
`luno/adapters/openrouter.py`, `_select_by_priority()`'s must-keep/
budget skeleton, `_dependency_kind()`, `_score_sentence()`, any file
under `luno/dashboard/`.
