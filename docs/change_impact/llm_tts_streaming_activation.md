# Change Impact: Production-Safe LLM -> TTS Streaming Activation (Sprint 3)

**Date:** 2026-08-12
**Status:** shipped (streaming path fixed and verified production-safe;
checked-in default flag deliberately left unchanged - see §7).

## 1. Original bottleneck / limitation

The "Voice Pipeline Latency & Semantic Speech Segmentation" sprint
(Sprint 2, see `docs/change_impact/voice_pipeline_latency_and_semantic_segmentation.md`)
measured the default (non-streaming) path's first-audio latency at a
median of 2.4056s, versus 0.3747s with `ENABLE_LLM_TTS_STREAMING=true`
(84.4% improvement) - but deliberately did NOT flip that flag by
default, citing a documented known limitation (barge-in during LLM
generation blocked the next turn until `llm_timeout_s`) and the fact
that the streaming path had never been audited for whether it bypasses
response-depth policy.

This sprint's brief: make the existing streaming pipeline
production-safe, prove it exhaustively, and make a considered decision
about the feature flag - explicitly forbidden from rebuilding the
pipeline from scratch, adding a second response selector/ranker, or
introducing a second semantic segmentation system.

## 2. Existing architecture reused (nothing rebuilt)

- `luno.incremental_speech.StreamingSpeechCoordinator` /
  `IncrementalSpeechBuffer` - the entire streaming dispatch machinery,
  unmodified in shape (subscriptions, `_TurnState`, `SpeechChunk`
  publishing) - only the DECISION of what/when to dispatch changed.
- `luno.response_output.build_dual_response()` - the SAME, single
  selection/compression authority the non-streaming path already uses.
  Not modified. Not duplicated. Streaming now calls it once per turn,
  on the complete accumulated text.
- `luno.response_policy.ResponsePolicy` - unmodified dataclass, now
  constructed properly (with `explicit`) at both call sites instead of
  a bare depth string being passed where a policy object was expected.
- `FishAudioAdapter`'s existing one-slot prefetch pipelining
  (`_play_stream_pipelined()`) - unmodified, still the only synthesis/
  playback overlap mechanism.
- `BargeInModule`'s existing `_cancelled_request_ids` suppression
  mechanism - unmodified, reused to guarantee a cancelled turn's
  already-in-flight text is never spoken.

## 3. Root causes found (Phase 0 audit + one empirical finding)

### 3.1 Response-depth policy bypass (the sprint's central concern)

The pre-existing `StreamingSpeechCoordinator` dispatched every settled
sentence to TTS the moment `IncrementalSpeechBuffer.feed()` produced it,
via `_dispatch_or_hold()`. It never called `build_dual_response()` at
all. Confirmed by tracing `BehaviorTreeModule._speak()`'s own early
return: `if self._streaming_coordinator.is_turn_streamed_and_completed(request_id): ... return` -
skipping the entire depth-selection call for a streamed turn. A user
asking for a SHORT answer that happened to stream would hear the FULL,
uncompressed reply.

### 3.2 `explicit` flag silently dropped on the real event path

Separate from streaming, found while tracing the fix for 3.1 (affects
BOTH streaming and non-streaming identically - a genuine pre-existing
bug, not introduced by this sprint). `PlannerBridgeModule` published
`response_depth_assigned` with only `{"request_id", "depth"}`, never
`explicit`. Both `main_runtime_demo.py::_speak()` and the streaming
coordinator then called `build_dual_response(text, depth_string, ...)`
with a bare STRING where a `ResponsePolicy` object (or nothing) was
expected. `response_output.py::_resolve_explicit()` does
`getattr(response_policy, "explicit", False)` - silently `False` for a
string. Result: `build_dual_response()`'s own `skip_compression =
explicit and depth == DEPTH_DETAILED` rule NEVER activated via the real
event path, only when a unit test called `build_dual_response()`
directly with a full `ResponsePolicy`.

**Empirical proof (before fix):** a live probe through the real
non-streaming `_speak()` path, requesting "jelaskan detail cara kerja
regulator ESP32" (explicit-detailed) with 5 distinct markers
(alpha/beta/gamma/delta/epsilon) in the canned reply, preserved only
3/5 (lost delta/epsilon to budget compression that should have been
skipped entirely).

### 3.3 Barge-in during active LLM generation blocked the next turn

`BehaviorTreeModule._generate_reply()`'s `done.wait(self.llm_timeout_s)`
only ever woke on `assistant_response`/`llm_error` - never
`llm_cancelled`. A barge-in landing while the LLM was STILL generating
(not yet finished) left that turn's `_generate_reply()` call - and
therefore the module's single-threaded event processing - blocked until
`llm_timeout_s` (default 45s) before the NEXT utterance could even be
forwarded to the planner. This is the exact limitation Sprint 2's own
writeup cited as the reason it did not flip the default.

**Empirical proof:** live probe via `console.simulate_speech()` for both
the initial question and the "stop" barge-in (the established real-path
testing convention, not a direct handler call) - before the fix, a
subsequent turn never completed within a 10s join; after the fix,
`elapsed_s=0.01`.

### 3.4 Session state deadlock after any fully-streamed reply (new finding)

Found empirically while writing the Phase 12 test matrix - not
anticipated by the Phase 0 audit, which is itself worth noting as a
lesson: audits of "does streaming bypass policy X" do not automatically
surface "does streaming silently fail to satisfy an assumption a THIRD,
unrelated module makes about how replies get spoken."

`SessionManagerModule` (`luno/wake_session/manager.py`) only transitions
`ConversationState.THINKING -> SPEAKING` when it observes a
`speak_request` event (`_handle_speak_request()`). A fully-streamed
reply never publishes `speak_request` at all -
`BehaviorTreeModule._speak()` deliberately skips it once
`is_turn_streamed_and_completed()` is `True`, specifically to avoid two
audio paths speaking the same turn. Consequence: the session state
machine stayed at THINKING **permanently** after the very first
successfully-streamed reply. `THINKING` is not a member of
`TIMEOUT_ACTIVE_STATES`, so nothing ever times it out either. Every
later `speech_recognized` event fell into
`_handle_speech_recognized()`'s "AWAKENING/THINKING/SPEAKING - busy, not
forwarded" branch - silently, with no error, no log distinguishing it
from ordinary busy-state suppression. This is strictly worse than 3.3:
that bug delayed one turn; this one could end a conversation's
usability entirely, silently, for its remaining lifetime.

**Empirical proof:** a debug harness confirmed `session_manager.session.state`
stuck at `ConversationState.THINKING` indefinitely (checked every 0.05s
for 1s, never moved) after one streamed turn completed; after the fix,
the same harness shows `ConversationState.WAITING_USER` immediately.

## 4. Exact production changes

- **`luno/incremental_speech.py`** (the core redesign): `_TurnState`
  gained `full_raw_text`, `depth`, `explicit`, `first_unit_dispatched`,
  `first_unit_raw_text`, `any_dispatched`. `start_turn()` now
  constructs the buffer with `min_short_chunk_chars=0` (so the one early
  dispatch is always exactly one raw sentence, never a short-sentence
  merge) and subscribes to `response_depth_assigned`. `_on_chunk()`
  dispatches only the FIRST settled chunk via a new `_dispatch_first()`,
  accumulating everything else into `full_raw_text`. `_on_finished()`
  was rewritten: builds a `ResponsePolicy` from the turn's own
  depth/explicit, calls `build_dual_response()` on the complete text,
  reconciles the already-spoken first-sentence prefix via a new
  `_reconcile_remaining()`, and dispatches the remainder via a new
  `_dispatch_remaining()`. The pre-existing `_dispatch_or_hold()`/
  `_dispatch_final()`/`_drain_held()`/`_on_chunk_played()` methods remain
  present (for structural/API compatibility - `held_chunks`/
  `pending_dispatched` are still constructible) but are no longer called
  from the new `_on_chunk()`/`_on_finished()` bodies.
- **`main_runtime_demo.py`**: `PlannerBridgeModule`'s
  `response_depth_assigned` publish site now includes
  `"explicit": response_policy.explicit`. `BehaviorTreeModule` gained
  `_last_turn_explicit`. `_generate_reply()` gained an `_on_cancel`
  subscription to `llm_cancelled` (closes 3.3) and threads `explicit`
  through `box`/`_last_turn_explicit` alongside the existing `depth`
  pattern. `_speak()` now constructs a full `ResponsePolicy(...,
  explicit=explicit)` instead of passing a bare depth string to
  `build_dual_response()`. A new route,
  `speech_playback_started -> session_manager`, was added to
  `PlannerBridgeModule`'s Event Bus wiring (closes 3.4).
- **`luno/wake_session/manager.py`**: `REQUIRED_ROUTES` gained
  `"speech_playback_started"`. `_dispatch()` routes it to a new
  `_handle_playback_started()`, which makes the same THINKING/IDLE ->
  SPEAKING transition `_handle_speak_request()` already makes, guarded
  identically (skips the wake-acknowledgement's own request_id). Module
  docstring extended to document the deadlock and its fix.
- **`luno/config.py`**: comment/rationale for `ENABLE_LLM_TTS_STREAMING`
  extended to document this sprint's findings and the Phase 10 decision
  (see §7). **The default value itself is unchanged (`False`).**
- **No changes** to `luno/response_output.py`, `luno/response_policy.py`,
  `luno/adapters/fish_audio.py`, the Event Bus core, or any other
  adapter/module.

## 5. Before/after first-audio latency

Measured via `tests/test_llm_tts_streaming_production.py::test_latency_regression_default_vs_streaming_corrected_design`,
5 repetitions each, mock harness timing (`chunk_delay_s=0.03`,
`playback_delay_s=0.05` - a different reply/timing setup than Sprint
2's own measurement, so the absolute numbers are not directly
comparable; the point of this measurement is confirming streaming still
measurably wins under the CORRECTED, depth-policy-safe design, not
reproducing Sprint 2's exact figures):

| | min | median | p95 | max |
|---|---|---|---|---|
| Default (non-streaming) | 1.1355s | **1.1837s** | 1.4077s | 1.4077s |
| Streaming (corrected design) | 0.4331s | **0.4561s** | 0.4609s | 0.4609s |

**Median improvement: 61.5%.**

## 6. Before/after inter-chunk gap

Measured via `test_latency_inter_chunk_gap_near_zero_when_synthesis_faster_than_playback`
(real `FishAudioAdapter` + real `RealFishAudioClient` + real
`AdapterManager` Event Bus, only the HTTP/audio-hardware boundary
faked, mirroring `tests/test_tts_chunk_pipelining.py`'s own established
technique): with synthesis (0.02s) faster than playback (0.08s), the
gap between one chunk's `PlaybackEnd` and the next chunk's
`PlaybackStart` measured **0.0006s and 0.0002s** across the 2
transitions in a 3-chunk turn - near-zero, confirming prefetch
synthesis is still correctly overlapping playback (this sprint did not
modify the pipelining mechanism itself, only confirmed it remains
intact under the streaming redesign).

## 7. Feature-flag decision

**The streaming path itself is now verified production-safe** - all
four bugs in §3 were found and fixed, each with empirical proof, and
the full 39-scenario test matrix plus the existing regression suite
pass.

**The checked-in `ENABLE_LLM_TTS_STREAMING` default was still kept at
`False`.** This is a deliberate, considered decision, and it is
important to be precise about WHY: it is a rollout-blast-radius
decision, not a safety decision.

While verifying the flip, two pre-existing tests broke:
`tests/test_adaptive_response_depth.py::test_R_voice_output_optimization_still_runs`
and
`tests/test_barge_in_console.py::test_uninterrupted_turn_produces_exactly_one_history_line`.
Both were confirmed to break specifically because of the flag (re-run
with `ENABLE_LLM_TTS_STREAMING` forced both ways via the environment,
isolating the cause) - not because streaming misbehaves, but because
both tests listen for the LEGACY `speak_request` event specifically as
their "the turn was spoken" signal, and a fully-streamed turn correctly
never publishes it (see §3.4's fix rationale - `_speak()` deliberately
skips it to avoid two audio paths speaking the same turn).

This means flipping the default has real, non-trivial ambient behavior
change for any existing test/integration that implicitly assumed
`speak_request` as always-present. A full audit of every such call site
across the codebase (there may be more beyond the two found so far) is
outside this sprint's bounded scope.

**Decision:** recommend `ENABLE_LLM_TTS_STREAMING=true` as an explicit,
per-deployment opt-in via `.env` for anyone who wants the latency win
today - fully supported, fully verified, safe. Leave the checked-in
repository default at `False` until a follow-up sprint audits and
updates every test/integration that assumes the legacy signal.
**Rollback, if ever enabled, is the same env var set back to `false` -
no code change required either way**, satisfying the "easy rollback, no
code modification needed to disable" requirement regardless of which
direction the default sits.

## 8. Barge-in results

All tested via the real `RuntimeDemoConsole` event path
(`tests/test_llm_tts_streaming_production.py`), never a direct handler
call:

- Before first audio (LLM still generating, nothing dispatched yet):
  clean, no audio starts.
- During first chunk's synthesis: clean, chunk never plays (verified
  directly against `FishAudioAdapter`/`AdapterManager`).
- During first chunk's playback: clean cancellation event observed.
- With a prefetched (one-slot-ahead) chunk in flight: prefetched audio
  confirmed never plays after cancellation.
- Barge-in landing WHILE the LLM is still actively generating (the
  original known limitation): confirmed closed - a subsequent turn
  completes in ~0.01s instead of blocking until `llm_timeout_s`.

## 9. Cancellation results

Covered by the same barge-in scenarios above plus: no stale audio leaks
into the next turn after a cancellation (`test_21`); no duplicate audio
for a fully-streamed turn - confirmed no legacy `speak_request` is ALSO
published alongside `speak_stream_chunk` for the same turn (`test_22`);
no thread/turn-state leak across many consecutive streamed turns -
`StreamingSpeechCoordinator._turns` confirmed empty after 5 turns
(`test_23`).

## 10. Semantic segmentation results

First speech unit is always a complete, coherent sentence, never a
mid-word/mid-number/mid-sentence fragment (`test_04`, `test_06_07`,
`test_08`). Short complete sentences ("Sudah terhubung.") survive intact
as the first unit when genuinely first (`test_05`). Orphaned
conditional/causal sentences never survive without their setup sentence
(`test_09`, `test_10`) - this guarantee comes from the pre-existing,
unmodified `_repair_orphans()`/`_build_semantic_units()` machinery from
Sprints 2 and prior, not anything new this sprint added. Chunks are
always dispatched in strict sequence order (`test_11`).

## 11. Fallback results

Streaming failure (malformed stream) falls back cleanly with exactly
one apology, no `assistant_response` published for the failed attempt
(`test_25`). TTS failure does not crash the turn or hang - a terminal
`speech_playback_cancelled` surfaces (`test_26`). LLM timeout still
produces the "gave up waiting" apology, not a hang (`test_24`). No
scenario produced a duplicate spoken response - the user is never
guaranteed to hear the same semantic unit twice, by construction: only
ONE early dispatch ever happens per turn, and the reconciliation pass
either finds a clean prefix match or defensively refuses to dispatch
further rather than guess.

## 12. Test counts

`tests/test_llm_tts_streaming_production.py`: **39 passed** (34 required
scenarios + the barge-in-during-generation proof + 3 real E2E tests + a
latency-regression test + an inter-chunk-gap test). One test
(`test_14_cancellation_during_synthesis`) flaked once under heavy
full-suite batch contention in one sweep run, confirmed passing 4/4
standalone immediately after - the same class of environment-load
timing flake as the pre-existing, already-documented
`tests/test_streaming_e2e.py::test_D`, not a regression.

## 13. Regression results

Full `tests/` tree (73 files, batched; `test_main_bargein.py`/
`test_root_main_bargein.py` excluded at collection for the same
pre-existing sandbox-environment reasons as every prior sprint -
missing `faster_whisper`/`legacy_main.py`; `test_dashboard.py` excluded
per its own already-documented "not re-executed in sandbox" baseline
note): only the SAME already-documented pre-existing failure groups
reproduced (6x `test_mic_device_index.py`, 1x
`test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
2x `test_real_adapters.py` `_device_index` gap, 1x
`test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
sandbox artifact), plus 2 tests
(`tests/test_verification_dashboard.py::test_api_verification_reports_a_successful_verified_action_end_to_end`,
`tests/test_emotion_engine.py::test_stale_emotion_decays_to_unknown_after_the_configured_window`)
that failed only under heavy batched contention and were confirmed
passing reliably standalone (3/3 and 3/3 respectively) - not
regressions. **Zero new regressions.**

Full `luno/` tree (38 files, one batch): only
`luno/barge_in/tests/test_barge_in.py`'s two already-documented
intermittent flakes under full-tree batched runs, re-confirmed passing
27/27 standalone, matching the established "pass standalone,
occasionally flake only under full-tree interleaving" baseline note.

## 14. Persistent-state verification

All 14 `config/*.json` files: SHA256 hash, mtime, and size captured
before this sprint's work began and after the entire sweep completed -
**byte-identical, zero unexpected changes**. No stray `.tmp`/`.bak`/
`.old`/`.orig` files anywhere under `config/`. Streaming instrumentation
(TTFT/TTFS/TTFA/LLMCompleted/SpeechCompleted timing logs) confirmed to
be stdout-only - `luno.core.utils.log()` is a plain `print()`, no file
I/O whatsoever - and non-persisting, directly verified by
`test_34_latency_instrumentation_never_persists_to_disk` (no new files
appear under `DATA_DIR` after a streamed turn).

## 15. Files created/modified

**Modified:**
- `luno/incremental_speech.py` - core redesign (§4)
- `main_runtime_demo.py` - `explicit` threading, barge-in fix, new route (§4)
- `luno/wake_session/manager.py` - `_handle_playback_started()`, new route (§4)
- `luno/config.py` - comment/rationale only, default value unchanged
- `tests/test_streaming_speech_integration.py` - one test honestly rewritten (supersession)
- `tests/test_streaming_e2e.py` - one test honestly rewritten (supersession)
- `tests/test_voice_output_optimization.py` - one test honestly rewritten (supersession)

**Created:**
- `tests/test_llm_tts_streaming_production.py` - the 39-scenario production test matrix
- `docs/change_impact/llm_tts_streaming_activation.md` - this document

**Documentation updated (append-only, no prior entries rewritten):**
- `ARCHITECTURE_GUARD.md` §29
- `docs/testing/regression_baseline.md` - new dated entry

## 16. Known limitations

- The checked-in `ENABLE_LLM_TTS_STREAMING` default remains `False` -
  see §7 for the full rollout-blast-radius reasoning. This is a
  deliberate scope boundary, not an oversight: a complete audit of every
  test/integration across the codebase that might implicitly assume
  `speak_request` as an always-present signal was not attempted this
  sprint.
- `max_pending_chunks`/backpressure bookkeeping in
  `luno/incremental_speech.py` is now vestigial under the new design
  (only one chunk is ever dispatched pre-completion, and the final batch
  deliberately bypasses the cap) - retained purely for backward-
  compatible construction since existing test call sites still pass
  `max_pending_chunks=...`. A future cleanup sprint could remove it
  entirely once nothing depends on the parameter's mere presence.
- `_dispatch_or_hold()`/`_dispatch_final()`/`_drain_held()`/
  `_on_chunk_played()` remain present in `luno/incremental_speech.py`
  but are dead code under the new `_on_chunk()`/`_on_finished()` bodies
  - left in place rather than deleted this sprint to keep the diff
  minimal and reviewable; a follow-up cleanup could remove them.
- Latency numbers in §5 use a different mock timing setup than Sprint
  2's own measurement (different reply length/chunk delay), so they are
  not directly comparable figure-for-figure - both demonstrate the same
  qualitative result (streaming meaningfully beats default) under their
  respective harnesses.

## 17. Rollback procedure

No code changes are required to disable streaming - it already defaults
to `False`. If a deployment has opted in via `ENABLE_LLM_TTS_STREAMING=true`
in its `.env` and needs to roll back, set it to `false` (or unset it)
and restart. No other configuration, code, or data changes are needed
either direction - the flag is read once per module bind
(`BehaviorTreeModule.bind_event_bus()`), so a restart is sufficient, no
hot-reload gap to worry about.
