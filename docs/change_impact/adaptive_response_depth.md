# Adaptive Response Depth Learning

Sprint objective (verbatim): "Membuat Response Depth Policy Luno menjadi
adaptive berdasarkan feedback nyata dari user, tanpa mengganti sistem
Response Depth Policy yang sudah ada."

## 1. Baseline (captured before any change)

- Targeted suite (`test_response_policy.py`, `test_response_output.py`,
  `test_voice_output_optimization.py`, `test_runtime_demo.py`,
  `test_memory_outcome_telemetry.py`, `test_memory_evaluation.py`,
  `test_episodic_memory.py`): **424 passed, 0 failed**.
- `config/*.json` SHA256 hashes captured for all six writer-capable files
  (`episodic_memory.json`/`vision_memory.sqlite3` do not exist in this
  checkout).

## 2. Architecture audit

Traced (not assumed) via `luno/response_policy.py` and
`main_runtime_demo.py`'s `PlannerBridgeModule`:

- `compute_response_policy(text, *, previous_score=None, previous_depth=None)`
  (`luno/response_policy.py:266`) is the sole depth authority. Priority
  order, confirmed by reading the function body: (1) explicit-instruction
  phrase match -> immediate `return` (`luno/response_policy.py:286-295`),
  (2) task-type base score, (3) question-complexity additions, (4)
  conversational-continuation nudge from `previous_score`
  (`luno/response_policy.py:360-370`), (5) clamp + bucket into
  SHORT/NORMAL/DETAILED.
- Called exactly once per turn, in `PlannerBridgeModule._handle_utterance()`
  (`main_runtime_demo.py:2685`, now shifted to ~2704 after this sprint's
  edits), immediately after emotion estimation and before
  `NeedLLMResponse` is published.
- `PlannerBridgeModule.__init__` already keeps THREE small,
  conversation-scoped, bounded, **never-persisted** dicts for exactly
  this kind of auxiliary per-turn signal: `_response_depth_context`
  (last resolved SCORE, for continuation), `_last_response_policy` (last
  resolved full `ResponsePolicy`, debug/inspection only), and
  `_session_feedback_target`/`_session_feedback_context` (a completely
  different concept - MEMORY CONTENT feedback target resolution). All
  four are explicitly reset in `_on_conversation_ended()`
  (`main_runtime_demo.py:2532`), with each reset site carrying its own
  comment: "a brand new conversation must not inherit the previous
  conversation's [...] score" - this precedent directly answers this
  sprint's own persistence question (see §5).
- A SEPARATE, pre-existing content-feedback pipeline already exists:
  `luno.memory.classify_context_outcome(text)` (`luno/memory.py:4259`)
  classifies a turn as `correction`/`negative`/`positive`/`neutral`/
  `unknown` for MEMORY CONTENT feedback ("itu salah", "iya benar"), via
  narrow, whole-message-anchored regexes
  (`_POSITIVE_MEMORY_FEEDBACK_RE`/`_NEGATIVE_MEMORY_FEEDBACK_RE`/
  `_CORRECTION_MEMORY_FEEDBACK_RE`, `luno/memory.py:2283-2318`), consumed
  by `PlannerBridgeModule._handle_memory_feedback_command()`
  (`main_runtime_demo.py:2221`). This is a DIFFERENT concept from depth
  feedback and is never touched, imported, or duplicated by this sprint -
  confirmed structurally by `test_M_no_second_memory_retrieval` in the
  new test suite (asserts `luno.memory` never appears in
  `response_policy.py`'s source at all).
- No existing depth-specific feedback detector existed anywhere in the
  codebase before this sprint (confirmed via `grep -rn "kepanjangan|terlalu
  panjang|terlalu singkat|kurang jelas|kurang detail"` across `luno/` and
  `main_runtime_demo.py` - the only hits were unrelated code comments).

## 3. Existing feedback sources considered

Per the brief's own list: explicit positive/negative feedback,
correction, "kepanjangan", "terlalu panjang", "singkat aja", "jelasin
lebih detail", "kurang jelas", "terlalu singkat", and continuation/
follow-up. Audited outcome:

- Positive/negative/correction feedback about memory CONTENT already
  exists (`classify_context_outcome()`) and is entirely separate from
  depth - reused conceptually (same anchored-regex discipline) but never
  imported or called.
- "kepanjangan"/"terlalu panjang"/"terlalu singkat"/"kurang jelas"/
  "kurang detail" - none existed as detectors anywhere; built new,
  narrow, deterministic regexes for these specifically (§4).
- "singkat aja"/"jelaskan detail" already exist as EXPLICIT-INSTRUCTION
  phrases for the CURRENT turn (`_EXPLICIT_SHORT_PHRASES`/
  `_EXPLICIT_DETAILED_PHRASES`) - a structurally different concept
  (request for THIS reply, not feedback about a PREVIOUS one) and
  deliberately NOT reused as depth-feedback triggers (see
  `test_explicit_depth_request_for_current_turn_not_treated_as_feedback`).
- Continuation/follow-up (`previous_score`) already exists and is left
  completely untouched - this sprint's adaptive modifier is applied
  strictly AFTER the continuation nudge, as one more additive step (see
  §4).
- Silence is never treated as feedback (`detect_depth_feedback(None)` /
  `detect_depth_feedback("")` both return `None` - `test_K_ordinary_followup_not_depth_feedback`).

## 4. Depth feedback detector

`luno/response_policy.py`'s new `detect_depth_feedback(user_text)`
(near line 480) returns one of `DEPTH_FEEDBACK_PREFER_SHORT` /
`DEPTH_FEEDBACK_PREFER_DETAILED` / `DEPTH_FEEDBACK_NEUTRAL` / `None`.
Deterministic regex matching only, same discipline as `luno.memory`'s own
feedback detectors (narrow, hand-curated phrase sets, `re.IGNORECASE`) -
no LLM judge, no fuzzy NLP.

| Signal | Trigger phrases (ID/EN) | Match style |
|---|---|---|
| `prefer_short` | "kepanjangan", "terlalu panjang", "too long" | substring search (message may combine with other clauses, e.g. "kepanjangan, singkat aja") |
| `prefer_detailed` | "terlalu singkat", "kurang jelas", "kurang detail", "kurang lengkap", "kurang rinci", "too short", "not clear/detailed enough" | substring search |
| `neutral` | "pas", "udah pas", "segini oke/pas/cukup", "that's (just) right/perfect" | whole-message anchored (same discipline as `luno.memory`'s positive-confirmation regex) |
| none of the above (incl. empty/`None`) | everything else - ordinary conversation, follow-ups, "itu salah", "informasinya kurang" | `None` |

**Content vs. depth, verified directly:** "itu salah" -> `None`
(`test_J_itu_salah_not_depth_feedback`); "yang tadi salah, sekarang
seharusnya 12 volt" (a correction) -> `None`
(`test_I_content_correction_not_depth_feedback`); "informasinya kurang"
(the brief's own explicit ambiguous example) -> `None`
(`test_content_gap_not_auto_depth_feedback` - deliberately requires a
length/clarity qualifier after "kurang", a bare "kurang" alone never
matches).

## 5. Priority order

Implemented exactly as specified, and PROVEN structurally, not just by
convention:

1. **Explicit user request** - `compute_response_policy()`'s existing
   explicit-phrase branches (`luno/response_policy.py:286-295`) `return`
   immediately, before the new adaptive-modifier code is ever reached.
   `test_B_explicit_short_wins_over_detailed_adaptive_preference` /
   `test_C_explicit_detailed_wins_over_short_adaptive_preference` prove
   this directly: an adaptive_modifier of ±25 (the maximum possible) has
   ZERO effect on an explicit turn's score.
2. **Safety / critical-information preservation** - entirely out of this
   module's scope (owned by `luno/response_output.py`'s voice
   optimization layer, untouched by this sprint).
3. **Existing Response Depth Policy** - the full heuristic score
   (task-type, complexity, continuation) is computed FIRST, unchanged.
4. **Adaptive preference** - the new `adaptive_modifier` parameter is
   added to the score LAST, only in the non-explicit path, right before
   the final clamp (`luno/response_policy.py`, new code block right
   before `score = _clamp_score(score)`).
5. **Default NORMAL** - unchanged fallback for empty text / policy
   errors.

## 6. Adaptive model

`DepthPreference` (dataclass, `luno/response_policy.py`): `bias: int`
(signed, clamped to `[-25, 25]`), `feedback_count: int`, `last_updated_at:
Optional[str]` (ISO-8601, observability only).

`apply_depth_feedback(preference, feedback, *, now=None)` - pure,
returns a NEW `DepthPreference`:

```
decayed = current_bias * 0.5                 # event-based decay, see below
new_bias = decayed - 10   if feedback == prefer_short
new_bias = decayed + 10   if feedback == prefer_detailed
new_bias = decayed        if feedback == neutral
new_bias = clamp(new_bias, -25, 25)
```

**Why event-based decay, not wall-clock decay:** every other bounded,
conversation-scoped dict already in `PlannerBridgeModule`
(`_response_depth_context`, `_last_turn_trace`, `_session_feedback_target`)
uses plain count-bounded LRU eviction, never time-based decay - there is
no existing precedent for wall-clock-sensitive state in this codebase's
conventions, and a wall-clock decay would require fragile time-mocking
in every test. Multiplying the existing bias by 0.5 before folding in a
new event achieves the same "recent feedback matters more than old
feedback" property deterministically, with no clock dependency.

**Bounded / non-oscillating, verified directly:**
- One feedback event -> a small, single-digit-to-low-teens modifier
  (`test_D_single_short_feedback_is_a_small_modifier`: bias=-10 after one
  event).
- Six consecutive consistent events -> monotonically approaches but never
  exceeds `±25` (`test_F_.../test_G_...` - geometric decay means each
  additional consistent event has diminishing marginal effect, asymptotically
  approaching the bound rather than reaching it by simple addition).
- One opposing event after an established preference -> the bias moves
  back toward (and typically past) zero, not toward the opposite extreme
  (`test_H_opposing_feedback_does_not_cause_extreme_oscillation` - worked
  example in `apply_depth_feedback()`'s own docstring: -10 then +10 ->
  -10*0.5+10 = +5, nowhere near -25 or +25).

`compute_response_policy()`'s new `adaptive_modifier: Optional[int] = None`
parameter is bounded a SECOND time, defensively, inside that function
itself (`max(_DEPTH_BIAS_MIN, min(_DEPTH_BIAS_MAX, adaptive_modifier))`)
even though every legitimate caller already bounds it via
`apply_depth_feedback()` - a malformed/out-of-range caller value (proven
via `test_adaptive_modifier_out_of_range_is_clamped_defensively`, passing
`9999`) can never push the score further than the module's own designed
bounds allow.

## 7. Scope (conversation-scoped)

`PlannerBridgeModule._depth_preference: Dict[str, DepthPreference]` -
keyed by `conversation_id`, exactly mirroring `_response_depth_context`'s
own scoping. No `conversation_id` -> the update is skipped entirely
(`_update_depth_preference()`'s own guard) - there is no global,
conversation-less preference, satisfying the brief's own "jangan membuat
preference global" rule and preventing one conversation's feedback from
leaking into another's (verified directly:
`test_e2e_5_preference_does_not_leak_across_conversations`).

## 8. Persistence decision: NOT persisted (with reasoning)

The brief explicitly asked this sprint to audit whether persistence is
actually needed before building anything. Conclusion: **no**, and this
is not a corner cut - it follows the codebase's own, already-deliberate
precedent for the closest sibling data:

`_response_depth_context` (the CONTINUATION score, computed by the exact
same `compute_response_policy()` call this sprint extends) is explicitly
documented and implemented as conversation-scoped, in-memory, and reset
at conversation boundaries - `main_runtime_demo.py`'s own comment at the
reset site reads: "a brand new conversation must not inherit the
previous conversation's response-depth continuation score." Building a
persistent, cross-session adaptive-depth store here would directly
contradict this established, deliberate design decision for the
identical category of signal (a response-depth-related, per-turn
auxiliary value), and would be exactly the kind of unjustified new
persistent store the brief itself explicitly forbids ("jangan membuat
persistent store baru tanpa alasan arsitektural yang kuat").

Additionally: `tests/conftest.py`'s `isolate_persistent_state` fixture
enumerates every writer-capable file this codebase's production code can
ever touch (`_WRITABLE_STATE_ATTRS`) - none of them is response-depth- or
preference-related, confirming no existing persistence layer was ever
intended to carry this kind of signal.

If a future sprint wants cross-session depth preference (e.g. "Vinn
always prefers short answers, even in a brand new conversation next
week"), that is a genuinely different, larger feature - it would need
its own persistence schema, its own `luno/persistence.py`-backed
atomic-write store, and its own explicit product decision about whether
a single global user preference is even desired (Vinn is the only user,
but the existing per-conversation reset precedent suggests this was a
deliberate choice, not an oversight, for the closely related continuation
signal) - out of scope for "adaptive... without changing the existing
Response Depth Policy system."

## 9. Integration

```
_handle_utterance(event)
    |
    text, conversation_id
    |
    adaptive_modifier = self._depth_preference[conversation_id].bias   (if present)
    |
    response_policy = compute_response_policy(text, previous_score=..., adaptive_modifier=adaptive_modifier)
    |                                                          [UNCHANGED call site otherwise]
    v
    ... (memory feedback handling, unrelated to depth) ...
    |
    self._update_depth_preference(conversation_id, text)   <- NEW, runs AFTER this turn's own
    |                                                          depth was already computed/published,
    |                                                          so feedback only affects the NEXT turn
    v
    self._depth_preference[conversation_id] = apply_depth_feedback(current, feedback)
```

`response_depth_assigned` is still published exactly once per turn
(unchanged), still carries only `{request_id, depth}` (unchanged shape) -
`BehaviorTreeModule._speak()`'s own `build_dual_response()` call is
completely untouched by this sprint; it still consumes whatever depth
`PlannerBridgeModule` resolved, same as before. Reset:
`PlannerBridgeModule._on_conversation_ended()` now also pops
`_depth_preference[session_id]`, in lockstep with
`_response_depth_context`/`_last_response_policy`.

## 10. Explicit override behavior

Verified via `test_B`/`test_C` (pure) and
`test_e2e_3_explicit_request_overrides_adaptive_preference` (real
production bridge, three consecutive "kepanjangan" feedback events
followed by an explicit "jelaskan secara detail" request still resolves
to DETAILED, score exactly 90, completely unaffected by the accumulated
-25-ish bias).

## 11. Voice behavior

Zero changes to `luno/response_output.py`. `build_dual_response()` still
consumes whatever depth `PlannerBridgeModule` resolves - it has no idea
an adaptive modifier was ever involved, and does not need to. Verified
via `test_R_voice_output_optimization_still_runs` (a full E2E turn still
reaches `speak_request` with a real, non-empty, depth-adapted
`voice_text`).

## 12. Streaming behavior

`luno/incremental_speech.py` was not imported, read, or modified by this
sprint's diff (confirmed: `test_S_streaming_module_not_touched_by_this_sprint`
just checks the module's public surface is intact, and a `git`-less diff
review of this sprint's two touched files - `luno/response_policy.py`,
`main_runtime_demo.py` - confirms neither touches that file). The
adaptive modifier is resolved once per turn, synchronously, BEFORE
`NeedLLMResponse` is even published - the exact same timing the
pre-existing `compute_response_policy()` call already had. No partial-
token-based learning of any kind was implemented (per the brief's own
explicit prohibition) - feedback is only ever read from a COMPLETE user
utterance, after the LLM has already finished responding to the PREVIOUS
turn.

## 13. Tests

`tests/test_adaptive_response_depth.py` - 46 tests: pure detector tests
(content vs. depth distinction, silence/continuation exclusion), pure
accumulator tests (bounded, decay, oscillation resistance, no mutation),
priority-order tests (explicit always wins, zero modifier is a true
no-op, out-of-range clamping), and the full test-matrix letters A-U plus
E2E scenarios 1-5 through the real `RuntimeDemoConsole` bridge.

## 14. E2E

All 5 scenarios from the brief implemented and passing: (1) "kepanjangan"
feedback then a similar request trends SHORT; (2) "kurang detail"
feedback then a similar request's score never decreases; (3) explicit
request overrides adaptive preference; (4) content correction ("itu
salah") never touches the preference dict at all; (5) preference never
leaks across two different `conversation_id`s.

## 15. Regression

- Targeted suite (16 files spanning response policy/output/voice
  optimization/runtime/streaming/barge-in/memory outcome+evaluation+
  episodic+regression+context) + new suite: **604 passed, 0 failed**.
- Full `tests/` tree (same exclusions as the prior sprint's own
  documented baseline: `test_main_bargein.py`/`test_root_main_bargein.py`
  - pre-existing collection errors; `test_dashboard.py` - documented
  sandbox timeout), run in 5 batches: **1590 passed, 10 failed**, all 10
  mapping to the SAME already-documented pre-existing/environment issues
  in `docs/testing/regression_baseline.md` (`test_emotion_engine.py`'s
  known scheduling-jitter flake; `test_mic_device_index.py`/
  `test_production_launcher.py`/`test_real_adapters.py`'s environment-
  dependent failures). **Zero failures in `luno/response_policy.py`,
  `main_runtime_demo.py`, or any file this sprint's diff touched** - and
  notably, the two `test_streaming_e2e.py` timing flakes the prior
  sprint's own sweep observed once did NOT reproduce this time (ran
  clean), reinforcing that classification (sandbox scheduling jitter
  under a large batch, not a real bug).
- No pre-existing failure was "fixed" to make this report look cleaner.

## 16. Persistent-state verification

SHA256 for all six writer-capable `config/*.json` files, captured before
implementation and again after the full regression sweep completed:
**byte-identical, zero diff**. Stray-artifact check (`*.tmp`/`*.bak`/
`*.old`/`*.orig` outside `.venv`/`node_modules`/`__pycache__`): none
found.

## 17. Files changed

- Modified: `luno/response_policy.py` (added `detect_depth_feedback()`,
  `DepthPreference`, `apply_depth_feedback()`, and the
  `adaptive_modifier` parameter on `compute_response_policy()` - all
  additive), `main_runtime_demo.py` (added `_depth_preference` dict,
  `_update_depth_preference()` method, one new call site in
  `_handle_utterance()`, one new pop in `_on_conversation_ended()`, one
  new import line - all additive).
- Created: `tests/test_adaptive_response_depth.py`,
  `docs/change_impact/adaptive_response_depth.md` (this file).
- Deleted: none.

## 18. Bugs found and fixed

None in production code. Two test-authoring issues found and fixed
DURING this sprint's own test-writing (not pre-existing bugs): (1) an
existing purity-guard test
(`test_response_policy_module_imports_no_memory_or_persistence_modules`)
false-positived on a documentation COMMENT in this sprint's own new
code that mentioned `luno.memory` by name for explanatory purposes -
fixed by rewording the comment, not by weakening the guard test (the
guard's invariant - `response_policy.py` never imports memory/
persistence modules - remains fully enforced and is still true). (2) a
mistaken assumption in this sprint's own new
`test_U_no_persistent_state_write_from_adaptive_depth_feedback` that
`RELATIONSHIP_STATE_FILE` would stay byte-identical across ANY turn -
corrected after discovering (and confirming against the pre-existing,
already-passing `test_e2e_isolated_persistent_state_files_untouched_by_a_pure_depth_turn`
in `tests/test_response_policy.py`) that an ordinary conversational
turn's own, pre-existing `RelationshipStore.save()` call legitimately
updates that file on every turn, unrelated to depth feedback - only
`VERIFIED_FACTS_FILE` is the sprint-relevant claim.

## 19. Known limitations

1. **No cross-session persistence** - a deliberate, documented choice
   (§8), not an oversight. Preference resets at every conversation
   boundary, matching `_response_depth_context`'s own precedent exactly.
2. **Feedback detector coverage is intentionally narrow** - like every
   other detector in this codebase, it only recognizes the specific
   phrase shapes it was built for. A user expressing the same sentiment
   in genuinely novel wording (whether adaptive learning helped or hurt
   this exchange, but phrased in a way not covered by these regexes)
   produces no signal at all - a safe, conservative failure mode (no
   adaptation) rather than a false positive.
3. **The `bias` axis is one-dimensional** - a single signed number, not
   three independent per-depth confidences. This was a deliberate
   simplification (documented, not accidental) over the brief's own
   suggested `short_preference`/`normal_preference`/`detailed_preference`
   field names, chosen because `compute_response_policy()`'s own score
   axis is already one-dimensional (0-100, bucketed into three ranges) -
   a single signed modifier composes naturally onto that axis, whereas
   three separate confidences would need their own, separate reconciliation
   logic to produce one modifier anyway.
4. **Decay is event-based, not wall-clock-based** (§6) - a conversation
   that goes quiet for an hour and then resumes still carries whatever
   bias its last feedback event left it at; the bias does not fade with
   real time, only with NEW feedback events. Documented as an intentional
   trade-off favoring deterministic testability over strict real-time
   fidelity, consistent with this codebase's own existing conventions.

## 20. Technical debt

None introduced. Both touched files remain fully backward compatible
(every new parameter/field is optional with a safe default; the
`adaptive_modifier=None` case is byte-for-byte identical to the pre-
sprint function, proven by `test_A_no_adaptive_feedback_identical_to_existing_behavior`).

## 21. Final invariants

- `compute_response_policy(text)` (no `adaptive_modifier`) behaves
  IDENTICALLY to before this sprint - `luno/response_policy.py:266-299`
  (explicit path) and the new modifier block at the end of the same
  function, gated by `if adaptive_modifier:`.
- An explicit user instruction ALWAYS wins over any adaptive preference -
  structurally guaranteed by early-`return` control flow, not a
  comparison check (`luno/response_policy.py:286-295` vs. the modifier
  block after line ~372).
- `_depth_preference` is never read or written outside
  `PlannerBridgeModule` (`main_runtime_demo.py`) and is never persisted
  to any file - grep-verified, zero hits for `_depth_preference` outside
  that one class.
- `luno/response_policy.py` still imports nothing beyond the standard
  library (`re`, `dataclasses`, `datetime`, `typing`) - verified by
  `test_M_no_second_memory_retrieval`/`test_N_no_llm_or_network_call`.
