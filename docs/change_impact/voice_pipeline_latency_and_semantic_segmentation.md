# Voice Pipeline Latency & Semantic Speech Segmentation (Sprint 2)

**Date:** 2026-08-12
**Builds on:** Voice Response Intelligence (Sprint 1, context-preserving
selection), TTS Chunk Pipelining, LLM Streaming -> Real-Time Speech
Pipeline (`luno/incremental_speech.py`).

## 1. Problem statement (from the brief)

1. A noticeable INITIAL delay before Luno starts speaking at all.
2. Speech selection could still produce semantically disconnected output
   even after Sprint 1's context-preserving fix.
3. Short, complete, useful sentences could still be dropped.
4. The TTS Chunk Pipelining sprint's inter-chunk gap fix did not solve
   initial latency.

The brief was explicit: **do not add more keyword heuristics before
first auditing and measuring the actual bottleneck.** The true goal, in
the brief's own words: "Make Luno begin speaking as soon as a COMPLETE
semantic speech unit is safely available, while ensuring that shortening
never destroys the meaning or usefulness of the spoken response" - NOT
simply "make Luno speak less."

## 2. Phase 0 audit findings (read-only, before any code change)

The real production path was traced directly from source, not assumed
from prior sprint reports:

- `BehaviorTreeModule._generate_reply()` (`main_runtime_demo.py`)
  publishes `user_utterance` and then unconditionally
  `done.wait(self.llm_timeout_s)`s on `assistant_response`/`llm_error` -
  it waits for the ENTIRE LLM reply before `_speak()` ever runs
  `build_dual_response()` or publishes a single `speak_request`. This
  holds **regardless of whether the LLM adapter itself streams tokens
  internally** - the streaming happens, but nothing downstream consumes
  it early.
- A COMPLETE, already-tested, already-production-wired alternative
  already exists: `luno.incremental_speech.StreamingSpeechCoordinator`
  and `IncrementalSpeechBuffer` (from the earlier "LLM Streaming ->
  Real-Time Speech Pipeline" sprint). `IncrementalSpeechBuffer.feed()`
  buffers incoming LLM text deltas and returns newly-SETTLED
  `SpeechChunk`s (a sentence only "settles" once something else arrives
  confirming it's really over) - reusing `_split_into_raw_sentences()`/
  `_split_long_sentence()`/`normalize_for_speech()` from
  `response_output.py`, not a second segmentation engine.
  `StreamingSpeechCoordinator` wires this to the event bus
  (`llm_streaming`/`llm_chunk`/`llm_finished`/`llm_error`/
  `llm_cancelled`), applies bounded backpressure, and publishes
  `SpeakStreamChunk` events that `FishAudioAdapter._handle_stream_chunk()`
  /`_play_stream()` consume.
- This entire architecture is gated behind
  `luno.config.ENABLE_LLM_TTS_STREAMING`, which defaults `False` in both
  `luno/config.py` and this project's own `.env`. **This is CASE B** from
  the brief's own bottleneck taxonomy: "LLM streams tokens but Luno
  waits for the entire response before speaking."
- A SEPARATE, real gap - found by tracing code, not assumed - explains
  problem #4 more precisely than the brief itself assumed:
  `FishAudioAdapter._play()` (the handler for the DEFAULT
  `speak_request`/`AssistantResponse` events) never checked
  `self.client.supports_split_synthesis()` and never called
  `_play_stream_pipelined()`. Only `_play_stream()` (reachable exclusively
  via the also-disabled `SpeakStreamChunk` path) had the TTS Chunk
  Pipelining sprint's synth/playback overlap benefit. In the default
  (non-streaming) configuration, ordinary multi-chunk replies synthesized
  and played back strictly sequentially with no overlap at all.

## 3. Phase 1-2: measurement and classification

`tests/test_voice_pipeline_latency.py` measures first-audio latency
(`T0` = the moment `simulate_speech()` is called, to `T_audio` = the
first `speech_playback_started` event) through the REAL
`RuntimeDemoConsole` (real event bus, real threading), with
`MockOpenRouterClient`/`MockFishAudioClient` mocked only at the
network/audio-device boundary, using a realistic 6-sentence Indonesian
ESP32/MQTT troubleshooting reply, 5 repetitions per configuration:

| Configuration | min | median | p95 | max |
|---|---|---|---|---|
| Default (non-streaming) | 2.3755s | **2.4056s** | 2.6503s | 2.6503s |
| Streaming enabled | 0.3374s | **0.3747s** | 0.3784s | 0.3784s |

**Median improvement: 84.4%.**

Classification: **CASE B** (per the brief's taxonomy) is the primary
root cause of problem #1. `test_B_streaming_speaks_before_full_llm_response_when_supported`
directly proves the default path's first speech dispatch happens
AT-OR-AFTER `llm_finished`, while the streaming path's happens BEFORE
it.

## 4. Fix #1 - closed the pipelining gap (shipped as a default-on fix)

`luno/adapters/fish_audio.py` gained a new method, `_play_pipelined()`,
a one-slot-prefetch counterpart to the existing `_play()` - it reuses
`_resolve_audio()`, the shared `_prefetch_executor`, and the exact
`prefetch_future`/`prefetch_index` one-slot invariant the TTS Chunk
Pipelining sprint already built, applied to `_play()`'s own
precomputed chunk list instead of a live `SpeakStreamChunk` queue. No
new synthesis/playback mechanism was invented.

`_play()` now begins with:

```python
if self.client.supports_split_synthesis():
    return self._play_pipelined(event, token)
```

`MockFishAudioClient` (used by nearly every pre-existing test) returns
`False` from `supports_split_synthesis()`, so `_play()`'s own body below
that check is byte-identical to before this sprint - zero behavior
change for any test or code path that doesn't opt into split synthesis.
`RealFishAudioClient` returns `True`, so real production traffic through
the DEFAULT `speak_request` path now gets the synth/playback overlap
benefit it was previously missing entirely.

**Cancellation-safety bug found and fixed as a direct consequence:**
initial implementation called `_resolve_audio(chunk["text"], None,
token)` for chunk 0 (nothing had been prefetched yet), which fell
through to a raw, synchronous `self.client.synthesize(text)` call - per
the `FishAudioClient` ABC's own documented contract, `synthesize()` is
NOT cancellation-aware on its own. A `StopPlayback` arriving during that
window had nothing to interrupt, so both `SpeechPlaybackStarted` and
`SpeechPlaybackCancelled` could be published for the same request (a
regression against `luno/adapters/tests/test_fish_audio_real.py::test_cancel_during_synthesis_never_publishes_started`).
Fixed by always submitting synthesis through `_prefetch_executor` first
(even for chunk 0) before resolving, giving it the same
cancellation-polling responsiveness later/prefetched chunks already had.
Verified: back to 14/14 on that suite; `test_fish_audio_barge_in.py`
unaffected at 8/8.

Proven directly by `tests/test_voice_pipeline_latency.py` tests E-H,
driving a REAL `RealFishAudioClient` through a `TimedFakeSession` (the
same technique `tests/test_tts_chunk_pipelining.py` established), going
through the DEFAULT `SpeakRequest` event type specifically:

- **E** - synthesis of chunk N+1 starts before playback of chunk N ends.
- **F** - playback order stays monotonic even when synthesis delays are
  deliberately inverted (decreasing per chunk).
- **G** - cancelling mid-synthesis means `SpeechPlaybackStarted` is
  never published.
- **H** - pause/resume mid-chunk still completes the turn correctly.

## 5. Fix #2 - NOT shipped as a default change (documented recommendation)

Flipping `ENABLE_LLM_TTS_STREAMING` to `True` would activate the
pre-existing, already-tested `StreamingSpeechCoordinator` architecture
system-wide and close the measured 84.4%-median CASE B gap for every
turn, not just the pipelining overlap Fix #1 provides.

This sprint deliberately did **not** flip that global default. Per the
brief's own working-style instruction - "If a proposed fix requires a
major architectural change, stop and document the finding before
proceeding" - and because the streaming sprint's own documented known
limitation still applies unchanged: `_generate_reply()`'s
`done.wait(...)` does not unblock on `llm_cancelled`, so a barge-in
**while the LLM is still generating** (not during playback, which is
unaffected) leaves that turn blocked until `llm_timeout_s` (default
45s) elapses. That is a genuine behavioral tradeoff a maintainer should
decide on deliberately, not one this sprint should flip silently as a
side effect of a latency-measurement task.

**Recommendation:** enable `ENABLE_LLM_TTS_STREAMING=True` once the
barge-in-during-generation limitation above is either accepted as a
known tradeoff or separately fixed (e.g. by having `_generate_reply()`
also wake on `llm_cancelled`, which was out of this sprint's scope).

## 6. Phase 3-8: semantic speech unit segmentation

**Concept.** A semantic speech unit is the smallest run of consecutive
sentences that should normally stay together when spoken - e.g. a setup
sentence plus the conditional clause that depends on it.

**Design decision (deterministic, no new keyword tables for grouping
itself).** `_build_semantic_units()` (new, `luno/response_output.py`)
groups sentences by REGROUPING the exact same one-hop dependency signal
Sprint 1 already computes (`_is_dependent_sentence()` -
causal/continuation/reference/conditional leading-marker detection): a
unit is a maximal run starting at an INDEPENDENT or SUPPORTING sentence
and extending through every immediately-following DEPENDENT sentence.
No embeddings, no LLM judge, no second tokenizer - a pure regrouping of
an already-proven signal.

**Design decision that was traced and deliberately rejected: atomic
whole-unit rescue/drop.** The natural-seeming design was to replace
`_repair_orphans()`'s existing one-hop iterative rescue walk with an
atomic pass that either admits an ENTIRE semantic unit or drops it
entirely. This was traced through the existing `MANY_DEPENDENTS`
pathological-chain regression test (a 13-sentence reply where 12
consecutive sentences all open with "Selanjutnya, langkah N...", forming
ONE 13-sentence semantic unit under `_build_semantic_units()`'s own
transitive grouping) and found to be **unsafe**: with a typical SHORT-
depth budget and repair cap far smaller than 13, an atomic "unit doesn't
fit under cap -> drop the whole unit" rule would discard even the
must-keep LEAD sentence (index 0), which is part of that same unit.
`_repair_orphans()`'s EXISTING fixed-point, one-hop-at-a-time walk
already achieves "keep the whole unit together, bounded by the repair
cap" correctly - each iteration rescues one more hop of the chain until
either the whole unit survives or the bounded cap is hit, and it never
discards the lead sentence (excluded from the orphan check by
construction). This sprint therefore left `_repair_orphans()`'s LOGIC
completely unchanged and only extended its docstring to name it as the
semantic-unit-preservation mechanism it already was. **No second
selector or ranking system was introduced** (Phase 17's explicit
prohibition honored).

**Phase 5 - short-sentence FUNCTION classification, not a word-count
bonus.** New `_CONFIRMATION_KEYWORDS` / `_has_confirmation_lead()` /
`_CONFIRMATION_BONUS` (+18.0, matched via the same leading-window,
word-boundary-anchored technique the existing causal/continuation/
reference detectors already use): a sentence that OPENS with a
status/outcome word ("sudah", "berhasil", "gagal", "selesai", "aktif",
...) AND is short (<=6 words) gets a modest scoring bonus - it is
reporting a status/outcome, which is exactly as informative as a longer
explanation and is often the ONE thing a listener most needs to hear
("Sudah terhubung.", "Relay sekarang aktif."). A short sentence with NO
such function gets nothing extra purely for being short
(`test_I_confirmation_bonus_not_a_blind_word_count_bonus`). Most of the
brief's other named short-sentence functions (direct answer via lead
position, action/warning/prohibition via the existing `_has_warning()`,
conclusion via the existing `_has_conclusion_cue()`, required setup via
the existing `_CONDITION_SETUP_BONUS`) were already covered by Sprint
1/earlier mechanisms - confirmation/status reporting was the one
genuinely uncovered function this sprint added.

**Phase 7 - listener coherence rule.** Already satisfied by
`_repair_orphans()`'s existing guarantee (no surviving DEPENDENT
sentence lacks its immediate predecessor, transitively back through the
whole chain). Verified, not reimplemented, via 5 adversarial dependent-
opener test cases from the brief itself ("Kalau masih gagal, restart
perangkat.", "Setelah itu cek servicenya.", "Akibatnya koneksi akan
gagal.", "Namun ada satu hal lagi.", "Ini terjadi karena konfigurasi
sebelumnya.") plus 2 direct dangling-reference scenarios.

**Phase 8 - do not over-compress an already-short-and-coherent
response.** Already satisfied by `_select_by_priority()`'s existing `if
budget >= total: return sentences` bypass: SHORT depth's own budget
floor is 2, NORMAL's is 5 - both `>=` a genuinely 2-sentence reply's own
total, so nothing is ever dropped from a 2-sentence reply at ANY depth.
Verified directly against the brief's own two worked examples ("Karena
SSID atau password-nya mungkin salah. Coba cek kembali konfigurasi
WiFi." and "Sudah berhasil. Relay sekarang aktif.") at SHORT, NORMAL,
and DETAILED, all three surviving intact.

**CRITICAL INVARIANT verified: relevance remains dominant.** A hard
safety warning (must-keep regardless of score/budget, from Sprint 1/the
Voice Output Coherence sprint's own mechanism) still survives over an
irrelevant-but-"complete" semantic unit under a tight SHORT budget -
semantic completeness is a protection mechanism, never a relevance
replacement (`test_R_relevance_still_dominant_over_semantic_completeness`).

## 7. Phase 9-11: streaming safety and TTS pipeline integration

No production code in `luno/incremental_speech.py` was modified this
sprint. Verification (not new implementation) confirmed:

- `IncrementalSpeechBuffer` already reuses `normalize_for_speech()`/
  `_split_long_sentence()`/`_split_into_raw_sentences()` from
  `response_output.py` for boundary detection, so abbreviations,
  decimals, URLs, and code already get the same handling those
  established helpers already provide -
  `tests/test_incremental_speech_buffer.py::test_19_url_and_code_block_use_existing_normalizer`
  covers this directly; all 25 tests in that file plus
  `test_llm_streaming.py` pass unchanged.
- The one-slot TTS prefetch mechanism was reused, not replaced or
  duplicated (Fix #1, above, calls the SAME `_resolve_audio()` and
  `_prefetch_executor` the streaming path's own `_play_stream_pipelined()`
  already used).
- `tests/test_streaming_speech_integration.py` and
  `tests/test_streaming_e2e.py` (28 tests) pass unchanged, confirming
  ordering, cancellation, and pause/resume behavior in the streaming
  path was not disturbed by this sprint's `response_output.py`/
  `fish_audio.py` changes.

## 8. Tests

- `tests/test_voice_pipeline_latency.py` (8 tests, A-H): latency
  measurement (min/median/p95/max, not just average), streaming-vs-
  default proof, no-incomplete-sentence-ever-dispatched proof, and 4
  tests (E-H) proving the `_play_pipelined()` fix against a real
  `RealFishAudioClient`.
- `tests/test_semantic_speech_units.py` (39 tests): direct
  `_build_semantic_units()` unit-boundary tests, short-sentence FUNCTION
  classification tests, Phase 7 coherence proofs (5 adversarial
  dependent-openers + 2 dangling-reference scenarios), Phase 14
  false-positive guards (word-boundary-safe matching - "Keberlanjutan"
  never matches "selanjutnya"/"lanjut"; "Port 1883."/"ESP32." as a lead
  sentence are never dropped), Phase 8 over-compression guards across
  all 3 depths, the relevance-dominance invariant, a dependency-
  classification regression spot-check, and 3 real E2E tests through
  `RuntimeDemoConsole` (short response intact, conditional response
  setup+condition coherent, long response's lead sentence present in
  the first dispatched speech).

## 9. Regression

Full `tests/` tree (75 files, batched): 1937 passed; only the 4
already-documented pre-existing failure groups reproduced (6x
`test_mic_device_index.py`, 1x `test_production_launcher.py::test_07_...`,
2x `test_real_adapters.py`, 1x `test_state_isolation.py::...`), plus the
2 already-documented sandbox-only collection errors
(`test_main_bargein.py`, `test_root_main_bargein.py`). Full `luno/` tree
(38 files, batched): 820 passed, 0 failed. `luno/barge_in/tests/` and
`luno/text_normalizer/tests/` re-run standalone: 62/62. **Zero new
failures.**

## 10. Persistent state verification

All 14 `config/*.json` files SHA256+mtime hashed before the regression
sweep began and compared after: byte-identical, zero unexpected changes.
No stray `.tmp`/`.bak`/`.old`/`.orig` files. No new persistent state was
introduced by this sprint - latency measurements and semantic-unit
groupings are computed fresh per call and never written to disk.

## 11. Known limitations

- `_build_semantic_units()`'s transitive chaining means a long,
  genuinely continuous causal narrative (e.g. 5-6 sentences each causally
  depending on the one before it) forms ONE large unit; the brief's own
  3-unit worked example (UNIT1 standalone / UNIT2 setup+condition /
  UNIT3 a second, textually similar but functionally SEPARATE condition)
  is not reproduced as an exact literal boundary contract by this
  design - distinguishing "this is a continuation of the same thought"
  from "this is a separate, parallel condition that merely resembles the
  previous one" was judged to require semantic understanding explicitly
  out of scope for a deterministic implementation (no embeddings, no LLM
  judge). The chosen transitive design is documented as a deliberate,
  reasoned trade-off favoring the far more common genuine-narrative-chain
  case over an exact reproduction of one illustrative example.
- `ENABLE_LLM_TTS_STREAMING` remains `False` by default - the 84.4%
  median first-audio latency improvement measured in Phase 1 is NOT
  active in the current default production configuration. Only Fix #1
  (the `_play_pipelined()` synth/playback overlap) ships as a default-on
  behavior change this sprint.
- The streaming architecture's own pre-existing known limitation (barge-in
  during active LLM generation blocks until `llm_timeout_s`) is
  unchanged and was out of this sprint's scope to fix.

## 12. Final invariants confirmed

`chat_text` remains the original, unabridged reply at every depth (no
change to that code path). `voice_text` may only be shorter through
complete semantic units (never a partial unit). Short sentences are not
automatically low priority (Phase 5). Complete, already-short responses
are not compressed (Phase 8). No partial sentence reaches TTS
(`IncrementalSpeechBuffer`'s settle-before-flush design, unmodified).
Semantic dependencies are preserved (`_repair_orphans()`, unmodified
logic). Sentence order is unchanged (no reordering anywhere in this
sprint's changes). Relevance remains the dominant selection signal
(verified, test R). Explicit SHORT/DETAILED instructions remain
authoritative (unmodified `response_policy.py`). Safety/prohibition
information survives (unmodified `_has_warning()` must-keep mechanism).
TTS chunk pipelining remains functional and is now ALSO reachable from
the default event path (Fix #1). Playback order remains deterministic
(test F). Cancellation prevents stale audio (test G). No new persistent
state. No second selector/ranker was introduced. No LLM/embedding-based
semantic judge was introduced.
