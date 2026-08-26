# Change Impact Analysis - Memory Evaluation & Self-Calibration

## 0. Most important rule (restated, not decoration)

An evaluation score is not truth. Luno can learn that a memory is
useful, frequently retrieved, repeatedly confirmed, or worth keeping -
none of those signals alone prove the memory's content is factually
true. Every design decision below is checked against one question: does
this let `evaluation_score` quietly stand in for "this is true" anywhere
(storage, retrieval, context assembly, maintenance, dashboard, tests)?
Where the answer could plausibly be "yes if we're not careful," that
boundary is enforced structurally (a test that reads the function's own
source, not just its behavior), not just documented.

## 1. Pre-flight audit summary

Traced the actual production call path (not just documentation) through
`luno/memory.py`, `luno/memory_context.py`, `main_runtime_demo.py`, and
`luno/dashboard/`:

- **Where memory is selected for a prompt:** `assemble_context()` in
  `memory_context.py`, fed by `MemoryRetriever.retrieve_memories()` -
  relevance-gated, then ranked by `ContextItem._rank_key()`
  (relevance -> importance -> usefulness -> priority), then budget-cut.
- **Where memory is deemed relevant:** `MemoryRetriever`/`analyze_query()`/
  `token_overlap()` in `luno/memory_retrieval/` - the ONE tokenizer/
  relevance engine in this codebase, never touched by this sprint.
- **Where usage is recorded:** `record_memory_usage()` (pre-existing,
  Memory Lifecycle & Maintenance sprint) - increments `retrieval_count`/
  `last_retrieved_at` for manual-memory entries that survive relevance-
  gating AND budget, called once from `main_runtime_demo.py` right after
  `self.memory_retriever.retrieve_memories(text)`.
- **Where usefulness changes:** `apply_positive_feedback()`/
  `apply_negative_feedback()` (Memory Learning & Feedback Loop sprint),
  plus a small usage-driven nudge inside `record_memory_usage()` itself.
- **Where feedback is applied:** `main_runtime_demo.py`'s
  `_handle_memory_feedback_command()` (conversational) and
  `_handle_explicit_memory_command()`'s two feedback branches (explicit
  "memory ini berguna/salah" commands) - 5 call sites total, all
  pre-existing.
- **Where conflict/history is used:** `_tag_ambiguous_conflict()`
  (marks both sides `conflict_status="ambiguous_conflict"`),
  `update_memory()` (old text -> `history`, new text -> current,
  importance-never-decreases), `_classify_conflict()`'s waterfall
  (`refinement_forward`/`refinement_backward`/`correction`/
  `temporal_change`/`ambiguous_conflict`).
- **Where maintenance makes decisions:** `_plan_action_for_entry()` (a
  per-entry base recommendation) + `analyze_memory_maintenance()`'s
  pairwise redundancy sweep - analysis-only, never mutates;
  `apply_maintenance_plan()` is the only executor, called only from an
  explicit command.
- **Where memory enters `PlannerBridgeModule`:** `relevant_memories_early
  = self.memory_retriever.retrieve_memories(text)` near the top of
  `_handle_utterance()`, reused (never re-queried) by usage-tracking,
  session-feedback-target resolution, and `assemble_context()`'s
  `precomputed_relevant_memories` parameter.

This confirms the pre-flight requirement: nothing in this sprint needed
to build a second version of any of the above - every new piece
(`evaluate_memory()`, `calibrate_memory()`, `record_context_selection()`,
`classify_context_outcome()`) is a thin, additive layer reading evidence
that already exists or is now recorded at an existing call site.

## 2. Schema (additive only)

`MANUAL_MEMORY_SCHEMA_VERSION` bumped 3 -> 4 (purely informational -
nothing in this module gates or rejects entries by this number's value,
same as every prior bump). New optional fields, every one defaulting
safely on an entry that predates this sprint:

| Field | Type | Default | Written by |
|---|---|---|---|
| `retrieval_success_count` | int >= 0 | 0 | `record_context_selection()` |
| `retrieval_miss_count` | int >= 0 | 0 | `record_context_selection()` |
| `feedback_event_count` | int >= 0 | 0 | `record_feedback_event()` |
| `correction_count` | int >= 0 | 0 | `update_memory(reason="correction")` |
| `conflict_event_count` | int >= 0 | 0 | `_tag_ambiguous_conflict()` |
| `evaluation_score` | float `[0.0, 1.0]` | 0.5 (neutral) | `calibrate_memory()` ONLY |
| `last_evaluated_at` | ISO string or absent | `None` | `calibrate_memory()` ONLY |

No `lifecycle` field is ever persisted (unchanged from every prior
sprint - `compute_lifecycle()` stays always-computed-fresh). No
`truth_score` field, or anything shaped like one, was added - not
required by the implementation, and adding one would directly violate
the most-important-rule above.

## 3. Evaluation vs. usefulness vs. usage - three separate concepts, not three names for one thing

- **Usage** = `retrieval_count`/`last_retrieved_at` (Memory Lifecycle
  sprint): "was this memory surfaced by the retriever."
- **Usefulness** = `usefulness_score`/`positive_feedback_count`/
  `negative_feedback_count` (Memory Learning sprint): "did explicit
  feedback and usage suggest this memory is good to keep retrieving."
- **Evaluation** (this sprint) = `evaluation_score`/`confidence`/
  evidence counters: "how much evidence do we have, and in which
  direction, about this memory's overall track record" - a strictly
  READ-derived summary, computed by `evaluate_memory()` from raw
  counters, that becomes persisted advisory metadata only when
  `calibrate_memory()` is explicitly called. It is not a fourth copy of
  "is this memory good" - it deliberately does not read `importance` or
  `usefulness_score` at all (see §6), so it can never simply echo back
  what usefulness already says; it is built from a partially-overlapping
  but distinct evidence set (retrieval SUCCESS/MISS, correction,
  conflict) that usefulness never modeled at all (Step 6/7 are new
  observational capabilities, not a re-scoring of old ones).

## 4. Retrieval outcome tracking - a genuinely new pipeline stage

`record_memory_usage()` already tracks "this memory appeared in the
retriever's own relevance-gated, budget-limited result" (via
`retrieval_count`). This sprint adds a DIFFERENT, later pipeline stage:
`assemble_context()`'s own SEPARATE ranking/budget cut (cross-source
dedup + `ContextItem._rank_key()` + `MemoryRetrievalConfig`'s budget),
which can drop a candidate that `record_memory_usage()` already counted
as "used." `record_context_selection(candidate_ids, selected_ids, now=)`
captures exactly this: every candidate id -> `retrieval_success_count`
if it survived into the final assembled context, `retrieval_miss_count`
if it didn't. This operates on PLAIN ID SETS - never a `ContextItem`/
`RelevantMemory` instance - so `luno/memory_context.py`'s existing
one-way import of `luno/memory.py` is never inverted; the caller
(`main_runtime_demo.py`) does the id extraction from both
`relevant_memories_early` and `assembled_context.items`.

## 5. Context outcome classification - deterministic, no LLM judge

`classify_context_outcome(user_text=None, memory_was_updated=False)`
returns exactly one of `positive`/`negative`/`neutral`/`correction`/
`unknown`. Reuses the EXISTING feedback/correction detectors from the
Memory Learning sprint verbatim (`detect_positive_memory_feedback()`,
`detect_negative_memory_feedback()`, `detect_memory_feedback_correction()`,
the 4 explicit mark-command detectors) - no second detection pass, no
LLM call. `unknown` is always the fallback; an empty/`None` `user_text`
never reads as `positive` (silence is not confirmation). A genuine
content correction always wins as `correction`, regardless of
accompanying text - the strongest, most concrete signal available. This
function is currently available for callers/tests/future wiring; it is
not yet consumed by a specific new call site beyond what
`_handle_memory_feedback_command()`'s existing branches already imply
(their own control flow already IS this classification, made explicit
and reusable here rather than re-derived a second time somewhere else).

## 6. `evaluate_memory()` - the deterministic evaluator

Pure function of `(entry, now)`. Reads ONLY raw evidence counters +
already-existing pure helpers (`compute_lifecycle()`, the maintenance
planner's own `_OBSOLETE_WORDING_RE`, `conflict_status`) - **never**
`importance` or `usefulness_score`, enforced by a dedicated
`inspect.getsource()` test in addition to a behavioral one (two entries,
identical evidence, different `importance`/`usefulness_score` ->
identical `evaluate_memory()` output).

Score construction (all deltas bounded, summed, then clamped to
`[0.0, 1.0]`):

- Positive feedback: `+0.12` per event.
- Negative feedback: `-0.12` per event.
- Correction: `-0.18` per event (stronger than a bare negative - a
  correction is concrete evidence the PREVIOUS wording was disputed
  enough to be replaced, not just disliked).
- Successful context selection (`retrieval_success_count`): `+0.015`
  each, but the USAGE-ONLY contribution (when zero explicit feedback/
  correction exists at all) is separately capped below 0.75
  (`_EVAL_USAGE_ONLY_CEILING`) - mirrors `_USEFULNESS_USAGE_NUDGE_CEILING`'s
  precedent: usage alone can never manufacture a high score, no matter
  how large the count.
- Unconfirmed repeated retrieval (`retrieval_miss_count >=
  5` with zero `retrieval_success_count`/positive feedback ever): a
  single, bounded, ONE-TIME `-0.10` penalty - not compounded per extra
  miss (Step 4's own "retrieved repeatedly but never confirmed" signal,
  deliberately weak and capped so it can't dominate).
- Unresolved conflict: `-0.10` (evidence, not a verdict about which
  side is correct - both sides accrue this if applicable).
- Obsolete/temporary wording: `-0.08` (same regex the maintenance
  planner already uses, not a new pattern).
- Stale-by-age with zero confirming evidence: `-0.05`.
- Historical survival bonus: `+0.05` when the entry has `history`
  (survived at least one update/correction) AND no negative
  evidence has accrued since - Step 5's "historical truth must not be
  sacrificed for current-state simplicity": a memory is not punished
  merely for having a past, only for negative evidence.

`confidence` is a SEPARATE axis (Step 9): it grows only with total
evidence VOLUME (`evidence_units * 0.08`, capped at 0.95 - never full
certainty from finite evidence), independent of direction. It is always
recomputed fresh and never persisted, the same treatment
`compute_lifecycle()` already receives in this codebase.

`recommendation` is one of `keep`/`reinforce`/`review`/`deprioritize`/
`archive_candidate` - deliberately a SEPARATE, advisory-only vocabulary
from the maintenance planner's own executable `keep`/`reinforce`/
`archive`/`consolidate`/`review` action set (see §7). An unresolved
conflict always forces `review`, checked FIRST, regardless of any other
evidence - evaluation can never make a conflict silently disappear
(Step 10's explicit requirement). A score landing in the ambiguous
mid-range (`[0.4, 0.6]`) at low confidence only escalates to `review`
when REAL interaction evidence exists (positive/negative/correction/
successful use) - **bug found and fixed during this sprint's own
development**: an earlier draft escalated ANY low-confidence mid-range
score to `review`, including a plain, unconfirmed, merely-stale memory
whose only "evidence" was the passive age-based stale penalty - that
misclassified every ordinary stale memory as "ambiguous, needs review"
instead of the correct, pre-existing `archive` outcome. Fixed by
requiring genuine interaction evidence (not passive/environmental
signals) before this branch can fire; caught via manual maintenance-
planner testing before any test suite run, not shipped.

## 7. Calibration (`calibrate_memory()`) - the only writer

`calibrate_memory(memory_id, now=None)` runs `evaluate_memory()` fresh
against the live entry and persists EXACTLY `evaluation_score` +
`last_evaluated_at` - nothing else. Verified by a dedicated test that
diffs the entry's key set before/after and asserts the only possible new
keys are those two. Never `text`, `history`, `importance`,
`conflict_group`, `source`, or a `lifecycle` field. Never called
automatically or on a schedule - no background job anywhere calls it
(Step 19 across both sprints: no autonomous mutation). Called
synchronously from: (1) `main_runtime_demo.py`'s 5 existing feedback call
sites (2 explicit mark-commands, 3 conversational branches), immediately
after the pre-existing `apply_positive_feedback()`/
`apply_negative_feedback()`/`update_memory()` call; (2) the dashboard's
new `memory_recalibrate()` control (explicit user action); (3) directly
in tests.

A memory that repeatedly calibrates high becomes a more-trusted advisory
signal for the maintenance planner over time (§8) - it NEVER becomes,
and is never treated as, a Verified Fact. `luno/memory_guard.py` is
never imported or referenced anywhere in this sprint's new code,
confirmed by a structural `inspect.getsource()` scan (same technique the
Memory Learning sprint's own isolation test established) plus a direct
check that `VerifiedFact`'s dataclass fields contain no evaluation
concept at all.

## 8. Maintenance integration (Step 10) - advisory, one-way-conservative

No new maintenance engine, no new executable action. `_plan_action_for_entry()`'s
existing `lifecycle == "stale"` branch gained a THIRD check, positioned
AFTER the pre-existing retrieval-count reinforcement check and the
Memory Learning sprint's own usefulness check:

- `evaluate_memory()["recommendation"] == "reinforce"` (and importance
  below the reinforcement ceiling) -> upgrades the default `archive`
  outcome to `reinforce`.
- `evaluate_memory()["recommendation"] == "review"` -> upgrades to
  `review` (only reachable here when real interaction evidence exists at
  low confidence - the pure "unresolved conflict" case is already caught
  earlier by `_is_protected_from_archival()`, before evaluation is even
  consulted).
- Otherwise -> unchanged, falls through to the pre-existing `archive`
  default.

Neither new branch can ever escalate PAST what the base planner already
decided - this can only make maintenance MORE conservative (same
one-way-only shape as the usefulness integration before it), never a
new way to reach `archive`/delete something that usage/usefulness/
obsolete-wording didn't already flag. Verified by a dedicated test that
a plain, zero-evidence stale memory still defaults to `archive` exactly
as before this sprint (guards against the bug described in §6
reappearing at the planner level). `analyze_memory_maintenance()` itself
remains pure (a before/after-equality test covers this even with
evaluation now integrated) and `apply_maintenance_plan()` - the only
executor - is completely unchanged.

## 9. Verified Facts guard - audited, not modified

Zero lines of `luno/memory_guard.py` changed. `VerifiedFact`'s dataclass
fields contain no evaluation/evidence concept (`evaluation_score`,
`retrieval_success_count`, `correction_count` all absent), confirmed by
a direct `dataclasses.fields()` check. `VerifiedFactStore` facts remain
structurally unreachable from `_memories`-based code, unchanged from
every prior sprint's own confirmation of this boundary - reconfirmed
here by a structural `inspect.getsource()` scan of every new function
this sprint added (`evaluate_memory`, `calibrate_memory`,
`record_context_selection`, `classify_context_outcome`,
`record_feedback_event`, every new accessor, `_plan_action_for_entry`).

## 10. Episodic Memory - untouched

Zero lines of `luno/episodic_memory.py` changed - same structural
isolation scan covers this boundary too. No episode is evaluated,
calibrated, or otherwise touched by this sprint's new code.

## 11. Dashboard (Step 11/12) - no new page

`collect_memory_overview()` gained `evaluation_recommendations` (a tally
of `evaluate_memory()`'s LIVE recommendation across every current entry
- pure, safe to compute on every GET). `collect_memory_list()` gained 4
new sort modes (`highest_evaluation`/`lowest_evaluation`/
`low_confidence`/`recently_evaluated`) and 4 new row fields
(`evaluation_score`/`evaluation_confidence`/`evaluation_recommendation`/
`last_evaluated_at`), computed LIVE via `evaluate_memory()` per row (not
read-only from the possibly-stale persisted `evaluation_score`), so the
list always reflects current evidence even for a memory that has never
been explicitly calibrated. `collect_memory_detail()` gained the full
live `evaluation` dict, the separately-persisted
`last_calibrated_evaluation_score`/`last_evaluated_at` (exposed
alongside the live numbers, honestly, rather than picking one silently),
`evidence_counts`, and `evaluation_explanation` (Step 12's "Why this
score?" text, explicitly avoiding any language implying the system knows
absolute truth - "Evaluation"/"Confidence"/"Recommendation", never
"Truth"/"Verified"). `controls.py` gained `memory_recalibrate()`; the two
existing feedback controls now also call `record_feedback_event()` +
`calibrate_memory()`, matching the runtime bridge's own synchronous
pattern. `server.py` gained one new POST route. `static/index.html`
gained 4 sort options, 4 overview cards, 2 list columns, 4 detail cards,
a "Why this score?" panel, and a Recalibrate button - purely additive,
no new panel/page. A dedicated test (monkeypatched `_save()`) confirms
`collect_memory_overview()`/`collect_memory_list()`/
`collect_memory_detail()` never trigger a write, even though they now
call `evaluate_memory()` per entry on every GET.

## 12. Tests

`tests/test_memory_evaluation.py` (94 scenarios): schema/backward-
compatibility for every new field; `evaluate_memory()`'s full evidence
matrix (empty, positive, negative, correction stronger-than-negative,
usage-capped-below-ceiling, unconfirmed-repeat-capped-not-compounded,
obsolete wording, stale, historical-survival-bonus, ambiguous-conflict-
always-review, non-interaction with importance, non-interaction with
usefulness - both behaviorally AND structurally via source inspection),
bounds, determinism, purity (no mutation, no `_save()` call);
explainability format and "none recorded yet" fallback; `calibrate_memory()`'s
determinism, persistence, backward compatibility, bounds, and the
"writes only these two fields" guarantee (text/history/importance/
source/conflict_group all individually asserted unchanged);
`record_context_selection()`'s success/miss split and unknown-id safety;
`classify_context_outcome()`'s full signal matrix incl. silence-never-
positive; retrieval-ranking non-interaction (`ContextItem` has no
evaluation field at all, `_rank_key()`'s own source never mentions
evaluation, an irrelevant-but-maximally-evaluated memory never outranks
a merely-relevant one, importance still outranks usefulness with
evaluation evidence present, a real `assemble_context()` call still
gates on relevance); Verified Facts field-absence check; maintenance
integration incl. the "zero-evidence stale memory still defaults to
archive" regression guard from §6 and the unresolved-conflict-stays-
protected guarantee; the 5 `main_runtime_demo.py` wiring points
(`record_feedback_event`, `update_memory(reason="correction")` ->
`correction_count`, `_tag_ambiguous_conflict()` -> `conflict_event_count`);
dashboard rendering for all new fields/sort modes plus no-mutation-on-
GET; and the Verified Facts/Episodic Memory structural isolation scan
plus a direct "no `truth_score` field anywhere" check.

Two new end-to-end scenarios in `tests/test_runtime_demo.py`, both
through the REAL `PlannerBridgeModule`/`RuntimeDemoConsole`:

- `test_memory_evaluation_self_calibration_end_to_end_positive_scenario_d`:
  save -> real retrieval (drives both the pre-existing usage tracking AND
  this sprint's new context-selection tracking through the real
  `assemble_context()` call) -> user confirms -> `evaluation_score`/
  evidence change, `last_evaluated_at` set, text/history/importance
  unchanged -> the real dashboard collectors render the result with no
  further mutation.
- `test_memory_evaluation_self_calibration_end_to_end_correction_weakens_scenario_e`:
  retrieved -> user corrects with a replacement value -> the EXISTING
  correction/history mechanism remains authoritative (old value in
  history, new value current, `correction_count` bumped) ->
  `evaluation_score` becomes weaker than the neutral 0.5 baseline ->
  memory preserved, never deleted, importance/id unchanged.

`tests/test_memory_learning.py`'s own
`test_schema_version_bumped_and_non_gating` assertion was updated from 3
to 4 to track the new (intentional, documented) schema version bump -
the only existing test file this sprint modified, and only that one line
plus its comment.

## 13. Known limitations / technical debt

- Score deltas (`_EVAL_*_DELTA` constants) are hand-chosen, not learned
  from real usage data - same accepted philosophy as `usefulness_score`'s
  own deltas.
- `evaluate_memory()`'s advisory `recommendation` vocabulary and the
  maintenance planner's executable action vocabulary are deliberately
  not identical - only `reinforce`/`review` currently route into a
  planner branch; `deprioritize`/`archive_candidate` are informational
  only (surfaced on the dashboard, not yet wired to any additional
  planner action). Documented here so this asymmetry is never mistaken
  for an oversight.
- `record_context_selection()`'s candidate pool is scoped to manual-
  memory entries reachable via `relevant_memories_early` - vision/
  episodic/planner-state sources have no `_memories`-backed entry to
  write evidence onto, so they never accrue `retrieval_success_count`/
  `retrieval_miss_count` (same scoping precedent `usefulness`/
  `importance` already established on `ContextItem`).
- `classify_context_outcome()` is implemented and tested but not yet
  wired into a specific new logging/telemetry call site beyond what
  `_handle_memory_feedback_command()`'s own existing control flow
  already implies - available for future use (e.g. a future analytics
  pass over conversation outcomes) without needing further design.
