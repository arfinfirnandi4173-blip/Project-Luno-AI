# Change Impact: Persistent Adaptive Response Depth Preference

## Summary

Extends the Adaptive Response Depth Learning sprint's conversation-scoped,
in-memory-only depth preference (`luno/response_policy.py`'s
`DepthPreference`/`detect_depth_feedback()`/`apply_depth_feedback()` -
all unchanged by this sprint) with a bounded, cross-session BASELINE that
survives a process restart. One new module,
`luno/response_depth_preference.py`, is the only place any I/O for this
feature happens. `luno/response_policy.py` remains pure - zero I/O, zero
persistence imports, still enforced by
`tests/test_response_policy.py::test_response_policy_module_imports_no_memory_or_persistence_modules`.

## Why this is a preference, not a memory

`long_term_memory.json` and `verified_facts.json` store things believed
to be TRUE about the user or the world (facts, corrections,
tool-verified state). `relationship_state.json` stores a relational
trust/familiarity signal. This feature stores neither - it stores one
bounded signed integer meaning only "the user tends to prefer
shorter/more detailed replies," derived from how the user has reacted to
past reply LENGTH, never from anything the user has told Luno about
themselves or the world. Every existing store was read before this
decision (Phase 1 audit): none of them is semantically appropriate, and
conflating this preference with `relationship_state.json` in particular
(structurally the closest analog) would create an unwanted coupling
where relational trust swings response length, or vice versa - explicitly
out of scope per the sprint brief. A dedicated, minimal file matches this
codebase's own established one-concept-per-file convention (six other
small, single-purpose JSON stores already exist).

Structurally enforced, not just documented: grepping this sprint's own
audit confirmed zero references to `response_depth_preference` /
`DepthPreferenceStore` anywhere in `luno/memory.py`, `luno/memory_guard.py`,
`luno/relationship_engine.py`, or `luno/memory_retrieval/`. This
preference cannot alter memory importance, verified facts, memory text,
retrieval evidence/ranking, or factual confidence, because no code path
connects them.

## Why it is persistent

The conversation-scoped `DepthPreference` (Adaptive Response Depth
Learning sprint) resets every time a conversation ends - by design, at
the time, since persistence was explicitly out of scope for that sprint
(see `docs/change_impact/adaptive_response_depth.md` §8). That means a
user who consistently prefers shorter replies has to "re-teach" Luno
every single conversation. This sprint closes that gap with a SEPARATE,
much smaller signal: not the raw conversation-local bias, but a
conservatively-blended, slow-moving baseline that seeds new conversations
without letting any single conversation dominate it.

## Why it is bounded

`bias` is clamped to `luno.response_policy.DEPTH_BIAS_MIN`/`DEPTH_BIAS_MAX`
(`[-25, 25]`) - the exact same public constants the conversation-local
`DepthPreference.bias` already uses, re-exported from
`response_policy.py` specifically so the persisted range can never
silently drift out of sync with the conversation-local range. This bound
was chosen (in the original Adaptive Response Depth Learning sprint) to
sit well inside the full 0-100 heuristic score range, so the modifier can
only ever nudge a borderline heuristic decision, never override one
that's already solidly within a bucket - e.g. a base score of 90 minus
the maximum possible -25 bias is still 65, still DETAILED. `sample_count`
is bounded by `MAX_SAMPLE_COUNT = 100_000` - a defensive ceiling against
corrupted/malicious input, not a realistic usage limit.

## Why it isn't persisted every turn

`should_persist(local_feedback_count)` only returns `True` once every
`PERSIST_MIN_SAMPLES = 3` real depth-feedback events within ONE
conversation - a single "kepanjangan" nudges the conversation-local
preference (as it always has, since the Adaptive Response Depth Learning
sprint) but does not, by itself, touch the persisted baseline at all.
When the threshold IS crossed, `merge_conversation_into_persistent()` is
a conservative weighted blend (`PERSIST_BLEND_WEIGHT = 0.3`), never an
overwrite - a single merge from a neutral persisted baseline against even
a maximally-biased local conversation (`local_bias == DEPTH_BIAS_MAX`)
only moves the baseline ~30% of the way there in one merge event. This is
the structural mechanism behind the hard requirement: "the user must
never become permanently stuck in SHORT or DETAILED because of a handful
of comments."

Repeated CONSISTENT feedback across many conversations gradually pulls
the baseline further in one direction (each merge nudges it ~30% closer
to whatever the current conversation's local bias is). Repeated
CONFLICTING feedback (a run of "kepanjangan" followed later by a run of
"terlalu singkat") pulls the baseline back toward neutral gradually,
never snapping straight to the opposite extreme in a single merge -
verified by
`tests/test_persistent_adaptive_response_depth.py::test_T_repeated_opposing_merges_pull_back_toward_neutral_not_flip_to_extreme`.

## Why explicit user instructions always win

Unchanged, structural guarantee inherited from `response_policy.py`
itself: the explicit-instruction branches in `compute_response_policy()`
(`_EXPLICIT_SHORT_PHRASES`/`_EXPLICIT_DETAILED_PHRASES`) `return` before
`adaptive_modifier` is ever read. An explicit "jawab singkat"/"jelaskan
detail" instruction for the CURRENT turn always overrides the persisted
baseline regardless of its magnitude, by construction - not a runtime
comparison that could be bypassed by a sufficiently strong stored bias.
Verified in both directions:
`test_e2e_5_explicit_instruction_always_overrides_persisted_preference`
(explicit SHORT overrides a persisted DETAILED-leaning baseline, bias
= +25) and
`test_e2e_11_explicit_detailed_instruction_overrides_a_persisted_short_preference`
(explicit DETAILED overrides a persisted SHORT-leaning baseline, bias =
-25).

## Corruption/recovery behavior

`DepthPreferenceStore.load()` delegates to `luno.persistence.safe_load_json()`
with `recover_from_backup=False` (a deliberate choice, not an oversight -
see below). Missing file, non-JSON content, non-dict JSON, or a
mismatched `schema_version` all fall back to `PersistedDepthPreference()`
(`bias=0, sample_count=0`) - identical to a fresh install's behavior,
never raises. Within a matching schema version, `bias` and `sample_count`
are each independently clamped (NaN/Infinity/non-numeric -> `0`,
out-of-range -> clamped to the nearer bound), so a partially hand-edited
file loads what it validly can rather than being discarded wholesale.

**Why `recover_from_backup=False` here, unlike some of the six
Persistent State Hardening V2 stores:** this store is continuously
re-derived, low-stakes evidence, not irreplaceable source-of-truth data.
If the primary is corrupted, falling back to neutral and letting the
baseline re-accumulate naturally over the next few conversations is an
acceptable, simpler failure mode than reaching for a possibly-stale
backup - unlike e.g. `long_term_memory.json`, where losing user-stated
facts is a real loss. `luno.persistence.atomic_write_json()`'s
pre-write backup still runs on every save regardless (so backups DO
exist on disk, in `config/backups/`, for manual inspection/recovery if
ever needed) - this store simply doesn't opt into automatic backup
recovery on load.

`DepthPreferenceStore.save()` returns `True`/`False`, never raises - a
persistence failure must never break the turn that triggered it.

## Privacy implications

The on-disk schema is exactly three keys:
`{"schema_version": 1, "bias": <int>, "sample_count": <int>}`. No raw
user feedback text, no conversation transcript, no query history, no
response history, and no timestamps are ever written. Verified against
the REAL production write path (not just the unit-level `to_dict()`), by
`tests/test_persistent_adaptive_response_depth.py::test_e2e_8_on_disk_schema_matches_spec_exactly`,
which drives three real "kepanjangan, singkat aja" turns through the
actual `PlannerBridgeModule` pipeline and then asserts the resulting
on-disk file contains exactly those three keys and neither
"kepanjangan" nor "singkat" appears anywhere in the serialized JSON.

## Concurrency/isolation behavior

Two separate concerns, both verified end-to-end:

1. **Cross-conversation isolation within one process.** A frozen
   snapshot of the persisted baseline
   (`PlannerBridgeModule._depth_preference_startup_bias`) is taken ONCE
   at process start and never updated again for the life of that
   process. A brand-new conversation always seeds from this frozen
   snapshot, never from the live, mutable
   `self._persistent_depth_preference` - so one conversation's
   mid-process, threshold-triggered merge can never leak into a
   DIFFERENT, concurrently-open conversation started later in the same
   run. Verified by
   `test_e2e_4_concurrent_conversations_in_same_process_do_not_leak_mid_run_learning`.
   Cross-SESSION learning is unaffected - the next process restart calls
   `DepthPreferenceStore.load()` again and picks up everything merged
   during the prior run.

2. **Thread safety of the read-merge-write sequence.**
   `PlannerBridgeModule._persistent_depth_preference_lock` (a plain
   `threading.Lock()`) guards every read-merge-write of
   `self._persistent_depth_preference` + `DepthPreferenceStore.save()`
   in both `_update_depth_preference()` and `_on_conversation_ended()` -
   two conversations' background turn threads crossing the persistence
   threshold at close to the same moment cannot race-corrupt the shared
   in-memory value or interleave writes to the on-disk file. Verified by
   `test_e2e_9_concurrent_conversations_saving_simultaneously_do_not_corrupt_the_file`
   (two threads, three feedback turns each, concurrently).

## Known limitations

- **FIXED by the Conversation_ended Lifecycle Routing sprint
  (2026-08-11) - see `docs/change_impact/conversation_ended_lifecycle_routing.md`.**
  The paragraph below is preserved as the original, accurate-at-the-time
  record of the gap this sprint's own "Known limitations" section
  identified but deliberately left unfixed (out of scope for that
  sprint). `add_route("conversation_ended", "planner")` now exists in
  both `main_runtime_demo.py` and `luno/bootstrap/modules.py` - the
  SECONDARY best-effort final-merge path described below is reachable
  through the real Event Bus in production as of that later sprint.
  Original text: **`_on_conversation_ended()`'s best-effort final merge
  is not currently reachable via the live Event Bus in production.**
  Same pre-existing gap `ARCHITECTURE_GUARD.md` §15 and `CURRENT_STATE.md`
  already document: `conversation_ended` events are not routed to the
  `"planner"` module (no `add_route("conversation_ended", "planner")`
  exists in `main_runtime_demo.py`'s route table). The PRIMARY
  persistence trigger (`should_persist()`, checked every turn from
  `_handle_utterance()`) is unaffected and remains fully reachable in
  production - only the SECONDARY "flush leftover evidence below the
  %3 threshold on conversation end" path is currently unreachable
  outside direct test calls. This sprint does not fix that pre-existing
  routing gap (out of scope, a real behavior change to event routing
  per the same reasoning already on record) - the final-merge code is
  implemented and tested so it becomes automatically functional the
  moment that gap is fixed separately.
- **No automatic backup-recovery on corruption** (see "Corruption/
  recovery behavior" above) - a deliberate choice for this specific,
  low-stakes, continuously-re-derived store, not a gap.
- **Learning happens only from the narrow, hand-curated depth-feedback
  phrase set `detect_depth_feedback()` already recognizes** (unchanged
  by this sprint) - phrasing outside that set is simply not detected as
  feedback at all, exactly as before this sprint existed.

## Tests

`tests/test_persistent_adaptive_response_depth.py` (33 scenarios):
schema/clamping/round-trip (A-J), `DepthPreferenceStore` load/save via
`luno.persistence` including corrupted-schema fallback (K-N), atomic
write + real pre-write backup verification (U), test-isolation-redirect
assertion (V), `should_persist()`/`merge_conversation_into_persistent()`
threshold and conservative-blend policy (O-T), and eleven end-to-end
scenarios through the real `RuntimeDemoConsole`/`PlannerBridgeModule`
production pipeline (process-restart learning, threshold-gated
persistence in both the SHORT and DETAILED directions, in-process
cross-conversation isolation, explicit-instruction priority in both
directions, conversation-end best-effort merge with and without prior
feedback, on-disk schema/privacy audit, concurrent-save thread-safety
smoke test).

No pre-existing test in `tests/test_adaptive_response_depth.py` or
`tests/test_response_policy.py` was modified - both suites remain green,
unmodified, confirming this sprint's "preserve exact existing behavior
when no stored preference exists" requirement holds (see
`docs/testing/regression_baseline.md`'s "Persistent Adaptive Response
Depth Preference" entry for the exact regression numbers).
