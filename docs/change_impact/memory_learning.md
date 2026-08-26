# Change Impact Analysis - Memory Learning & Feedback Loop

**Sprint:** Memory Learning & Feedback Loop (follows Memory Context Assembly, PASS)
**Scope:** additive-only extension of the existing `luno/memory.py` /
`luno/memory_context.py` / `luno/dashboard/` stack. No new memory store, no
new database, no second retrieval engine, no second tokenizer, no second
importance scale, no second lifecycle system, no second conflict resolver,
no second context-assembly pipeline.

## 1. Pre-flight audit summary

Before writing any code, the following were read in full: `ARCHITECTURE_GUARD.md`,
`docs/testing/regression_baseline.md`, `docs/change_impact/memory_context_assembly.md`,
`luno/memory.py` (2864 lines), `luno/memory_context.py`, `luno/memory_retrieval/`
(`models.py`/`retriever.py`), `luno/memory_guard.py`, `luno/episodic_memory.py`
(public surface), `main_runtime_demo.py` (`PlannerBridgeModule.__init__`,
`_handle_explicit_memory_command`, `_handle_utterance`, `_on_conversation_ended`),
and `luno/dashboard/collectors.py` / `controls.py` / `server.py` / `static/index.html`.

**Key finding that changed the plan:** `luno/memory.py` already has a working
usage-tracking system from the Memory Lifecycle & Maintenance sprint -
`record_memory_usage()` increments `retrieval_count`/`last_retrieved_at` for
manual-memory entries that survive relevance-gating AND the retrieval
budget, called from exactly one site (`main_runtime_demo.py`'s
`relevant_memories_early = self.memory_retriever.retrieve_memories(text)`
call site). This already satisfies the sprint brief's Section 5 ("USAGE
TRACKING") requirements almost verbatim. Per the brief's own Final Rule
("jika menemukan desain existing yang lebih aman... audit, jelaskan
conflict, pilih perubahan paling kecil") this sprint does **not** introduce
`usage_count`/`last_used_at` as new fields - it documents that USAGE, in
this codebase, IS `retrieval_count`/`last_retrieved_at`, and builds
USEFULNESS as a genuinely new, separate concept on top of it, satisfying
the brief's own "pisahkan usage dari usefulness" requirement (Section 3)
without duplicating an existing, tested mechanism.

## 2. Schema (additive only)

`MANUAL_MEMORY_SCHEMA_VERSION` bumped 2 -> 3 (informational only, non-gating
- old v1/v2 entries load unchanged, exactly like the 1->2 bump before it).

New optional fields on a `_memories` entry, all backward-compatible via
`.get(...)`-style accessors that default safely for a missing/malformed
value:

| Field | Type | Default | Bounds |
|---|---|---|---|
| `usefulness_score` | float | 0.5 (neutral) | `[0.0, 1.0]` |
| `positive_feedback_count` | int | 0 | `>= 0` |
| `negative_feedback_count` | int | 0 | `>= 0` |

Read accessors: `_get_usefulness()`/`_get_positive_feedback_count()`/
`_get_negative_feedback_count()` (private, same shape as the pre-existing
`_get_importance()`/`_get_retrieval_count()`), plus public wrappers
`get_memory_usefulness()`/`get_memory_positive_feedback_count()`/
`get_memory_negative_feedback_count()`/`get_memory_usefulness_explanation()`
(same "thin public wrapper around a private accessor" convention the
Memory Dashboard sprint already established for `get_memory_importance()`/
`get_memory_retrieval_count()`).

## 3. Usage vs. usefulness (Section 3 of the brief)

- **Usage** = `retrieval_count`/`last_retrieved_at` (pre-existing, unchanged
  mechanism). "Was this memory surfaced/selected this turn."
- **Usefulness** = `usefulness_score`/`positive_feedback_count`/
  `negative_feedback_count` (new this sprint). "Is there evidence this
  memory is actually good."

Never conflated: `record_memory_usage()` still ONLY writes
`retrieval_count`/`last_retrieved_at`/the pre-existing capped importance
reinforcement; the new usefulness usage-nudge (below) is a clearly
separate, additional, much smaller effect folded into the SAME function
(one usage event -> at most one usage-nudge - never a second pass, never
double-counted).

## 4. Usage tracking - audited, not rebuilt

`record_memory_usage()` was re-read line by line and confirmed to already:

1. only count entries whose `.source == "manual_memory"` AND that carry a
   real `raw.get("id")` (i.e. actually reached the caller's final,
   relevance-gated, budget-limited result list);
2. never count a memory that only exists in `_memories`;
3. never count a memory that lost to the relevance gate (a source's
   `token_overlap` check runs before an item can become a `RelevantMemory`
   at all);
4. never count an archived memory (`make_manual_memory_source()` excludes
   `compute_lifecycle(m) == "archived"` entries before relevance gating
   even runs);
5. never count a budget-rejected candidate (`MemoryRetriever._apply_limits()`
   already trims the list BEFORE it reaches `record_memory_usage()`'s one
   call site).

One retrieval event -> one usage event: `memory_context.assemble_context()`
is confirmed (by its own docstring and by a new structural test,
`test_memory_context_assemble_never_double_counts_or_calls_record_usage`)
to never call `record_memory_usage()` itself - it only ever consumes the
SAME `relevant_memories_early` list the caller already passes to
`record_memory_usage()` separately, so there is exactly one usage-recording
call site per turn, unchanged from before this sprint.

**Small additive change made to `record_memory_usage()`:** a bounded
usefulness "usage nudge" (+0.02 per genuine retrieval, capped at 0.7 -
below `MEMORY_USEFULNESS_MAX`) was folded into the SAME per-entry loop that
already increments `retrieval_count` - satisfies Section 9's "successful
retrieval/use -> sedikit naik" without a second usage-tracking pass. There
is deliberately NO penalty branch anywhere (Section 9's "repeated
irrelevant retrieval -> jangan otomatis dihukum" is satisfied by
construction - the function has no way to be called with a memory that
wasn't genuinely, relevantly retrieved, so there is no "irrelevant
retrieval" case to penalize in the first place).

## 5. Positive / negative / correction feedback

- `apply_positive_feedback(memory_id, reason=...)` / `apply_negative_feedback(memory_id, reason=...)`
  (new, in `luno/memory.py`): deterministic, bounded (±0.15 per event,
  clamped to `[0.0, 1.0]`), increment the matching counter, never touch
  `text`/`history`/`importance`, never delete. Both are pure "apply to a
  given id" functions - **target resolution is explicitly NOT their
  job** (Section 6/7's "jika target ambiguous: jangan modify memory" is
  therefore satisfied by construction: a function with no notion of
  "ambiguous" cannot itself resolve one incorrectly - that responsibility
  lives entirely in the caller).
- **Explicit commands** ("memory ini berguna/tidak berguna/benar/salah" -
  `detect_mark_memory_useful_command()` etc.): target the SAME
  `_most_recently_touched_memory()` helper `detect_mark_important_command()`/
  `detect_forget_last_memory_command()` already use - reused, not
  reinvented.
- **Conversational feedback** ("iya benar" / "itu salah" / "yang tadi
  salah, sekarang X" - `detect_positive_memory_feedback()`/
  `detect_negative_memory_feedback()`/`detect_memory_feedback_correction()`):
  carry NO target of their own by design. Target resolution is a NEW,
  session-scoped mechanism in `main_runtime_demo.py`
  (`PlannerBridgeModule._session_feedback_target`, Section 13) - see §7
  below.
- **Correction** ("yang tadi salah, sekarang X"): resolved via the
  EXISTING `update_memory()` (old text -> `history`, new text -> current)
  plus one `apply_negative_feedback()` call to keep feedback metadata
  truthful. No new correction/conflict engine.

## 6. Usefulness model

Bounded `[0.0, 1.0]`, default 0.5 (neutral - "no evidence" is not "known
bad"). Deltas: ±0.15 per explicit feedback event, +0.02 per genuine usage
event (capped at 0.7 without explicit feedback - frequency alone can never
manufacture "highly useful" status, mirroring the existing
`_FREQUENCY_REINFORCEMENT_CEILING` precedent for importance). Never derived
from text length or token count (no such computation exists anywhere in
this section). Explainable on demand via `_explain_usefulness()`/
`get_memory_usefulness_explanation()` - a short, bounded-metadata-only
breakdown (positive count, negative count, usage-nudge note), never a raw
per-event log.

## 7. Importance interaction (Section 10/20's hard rules)

`apply_positive_feedback()`/`apply_negative_feedback()` never write
`importance` - verified by direct test
(`test_positive_feedback_never_raises_importance_to_4`) and by the
structural `inspect.getsource()` isolation test. Protection (`importance
>= 4` or `conflict_status == "ambiguous_conflict"`, via the pre-existing
`_is_protected_from_archival()`) is completely unaffected: a protected
memory can still receive feedback (the feedback is truthful evidence) but
remains protected and intact (`test_negative_feedback_does_not_remove_protected_memory`).

## 8. Retrieval integration (Section 11's required order)

`relevance -> lifecycle -> conflict -> importance -> usefulness -> budget`:

- Relevance/lifecycle/conflict were already enforced upstream of any
  scoring formula (the `token_overlap` gate, the archived-exclusion check,
  the conflict-group adapter) - unchanged.
- `make_manual_memory_source()`'s score formula and
  `_score_memory_for_prompt()` (the two places Manual Memory ranking
  already happened) both gained one additional, small, additive term:
  `(usefulness - 0.5) * 0.05` - a ±0.025 max swing, deliberately smaller
  than one importance level's own 0.05 contribution, so it can only ever
  break a tie among items that already share the same importance band. It
  is applied strictly AFTER the existing importance/staleness terms in the
  same formula, never before.
- `memory_context.ContextItem._rank_key()` gained a THIRD tuple element
  (`usefulness`, `None` for every source that doesn't have the concept)
  positioned between `importance` (second) and `priority` (fourth,
  unchanged) - Python tuple comparison already enforces the required
  ordering: a relevance/importance difference always decides first,
  usefulness only breaks a remaining tie.
- Confirmed by test: `test_retrieval_usefulness_only_breaks_ties_never_outranks_importance`
  (an importance=3/usefulness=0.0 entry still outranks an
  importance=1/usefulness=1.0 entry) and
  `test_retrieval_relevance_still_mandatory_regardless_of_usefulness` (an
  importance=4/usefulness=1.0 entry with zero query relevance never
  appears at all).

## 9. Context assembly (`luno/memory_context.py`)

Read-only guarantee unchanged and re-verified: `assemble_context()` never
calls `record_memory_usage()`, `apply_positive_feedback()`, or
`apply_negative_feedback()` - it only reads `_get_usefulness()` (a pure
accessor) when building a `ContextItem`. `_manual_memory_conflict_items()`'s
merged hedge note now also carries a `usefulness` value (max of the
group's members, same "best of the group" convention `best_importance`
already used) so an unresolved conflict's usefulness participates in
ranking the same way an ordinary item's does.

## 10. Maintenance integration (Section 16)

`_plan_action_for_entry()` gained ONE new branch, inside the existing
`lifecycle == "stale"` case, checked AFTER the pre-existing
retrieval-count-based reinforcement check and BEFORE the pre-existing
archive fallback: if `usefulness_score >= 0.75` (`_USEFULNESS_PROTECTS_FROM_ARCHIVAL`)
and importance hasn't already hit the frequency-reinforcement ceiling,
recommend `reinforce` instead of `archive`. This is the ONLY new decision
this sprint added to the planner, and it only ever makes maintenance MORE
conservative (an entry that would previously have been archived on
low-usage grounds alone is now protected if there's independent, strong
usefulness evidence) - it never adds a new way to reach `archive`,
satisfying Section 16/20's "jangan archive hanya karena usage rendah" /
"maintenance harus tetap conservative". `apply_maintenance_plan()`'s
`reinforce` action is completely unchanged (same pre-existing +1/cap-3
rule) - usefulness never substitutes for or bypasses it.
`memory_health_report()`/`format_memory_health_report()` gained additive
`usefulness` (low/medium/high bucket counts) and
`total_positive_feedback`/`total_negative_feedback` fields, computed the
same read-only way as every existing field in that report.

## 11. Verified Facts guard (Section 14) - audited, not modified

Zero lines of `luno/memory_guard.py` were changed. Confirmed by:
`test_verified_fact_store_has_no_usefulness_or_feedback_concept`
(`VerifiedFact`'s dataclass fields contain no usefulness/feedback keys)
and `test_feedback_functions_never_reference_verified_fact_store_or_episodic_memory`
(structural `inspect.getsource()` scan of every new function this sprint
added). `VerifiedFactStore` facts are never represented as `_memories`
entries, so they remain structurally unreachable by this sprint's code, as
they were before it.

## 12. Episodic Memory (Section 15) - untouched

Zero lines of `luno/episodic_memory.py` were changed. No episode is ever
turned into a Manual Memory entry, and no automatic episode ingestion into
`_memories` was added. The brief's own "gunakan episode sebagai evidence
jika arsitektur existing mendukung" was evaluated and NOT implemented -
the existing architecture has no such read path today, and building one
would be new, non-additive scope beyond a "learn from usage of the
existing memory" sprint; left as a documented limitation (see §17 below)
rather than guessed at.

## 13. Session feedback target (Section 13)

New, `main_runtime_demo.py`-only state:
`PlannerBridgeModule._session_feedback_target: Dict[conversation_id, memory_id]`
- same scoping/reset/bounding convention as the pre-existing
`_last_device_target` (keyed on `conversation_id`, falls back to the
existing `_ENV_CONFIRMATION_KEY` sentinel, reset in `_on_conversation_ended()`,
bounded to 50 entries). Recomputed every turn from THIS turn's own
`relevant_memories_early` (no second retrieval pass): exactly one distinct
manual-memory id surfaced -> becomes the new target; zero or more than one
-> the target is cleared (never a stale or guessed multi-candidate target).
Scoped per-conversation - never leaks across conversations (verified by
the same `_on_conversation_ended` reset every other per-conversation dict
in this file already uses).

## 14. Ordering safety with pre-existing confirmation flows

`_handle_memory_feedback_command()` is checked in `_handle_utterance()`
**only after** the browser-permission, environmental-intent, and routing-
classifier pending-confirmation resolutions have all already run and found
nothing pending for this turn (`explicit_memory_note is None and
env_command_override is None and routing_confirm_override is None and
routing_confirm_forced_intent is None`). This is load-bearing: those three
existing flows already interpret short affirmative/negative replies
("iya"/"tidak"/etc.) when THEY have something pending, and this sprint's
new feedback phrases ("iya benar", "itu salah", ...) are deliberately
longer/more specific than the bare words those flows use, precisely so a
real pending confirmation for one of them is never intercepted by this
newer, additive check instead.

**Known, accepted residual risk (documented, not "fixed" by guessing):**
if a routing/browser/environmental confirmation is pending AND this
conversation also has a session feedback target AND the user's reply
happens to exactly match one of this sprint's feedback phrasings without
matching any of those other flows' own (narrower) accepted replies, this
sprint's feedback handler could claim the turn instead. This is considered
low-probability (the phrase sets barely overlap) and is explicitly
documented rather than solved with a broader, riskier heuristic, per the
brief's own "do not overengineer" instruction.

## 15. Dashboard (Section 17/18)

No new dashboard page - the existing Memory Dashboard's collectors/controls/
routes/HTML panel were all extended additively:

- `collect_memory_overview()`: +`usefulness` (bucket counts),
  +`total_positive_feedback`, +`total_negative_feedback` (straight
  passthrough of `memory_health_report()`'s own new fields - no
  recomputation).
- `collect_memory_list()`: +`sort` parameter (`most_used`/`most_useful`/
  `low_usefulness`/`needs_review`/`recently_reinforced`), +`usage_count`/
  `usefulness`/`positive_feedback_count`/`negative_feedback_count`/
  `needs_review` computed row fields (all via public `luno.memory`
  accessors).
- `collect_memory_detail()`: +`usage_count`/`usefulness`/
  `positive_feedback_count`/`negative_feedback_count`/
  `usefulness_explanation`.
- `controls.py`: +`memory_feedback_positive(id)`/`memory_feedback_negative(id)`
  (thin call-throughs, same shape as `memory_mark_important()`).
- `server.py`: +`sort` query param passthrough on `GET /api/memory/list`,
  +2 new POST routes (`/api/memory/controls/feedback_positive`/`_negative`).
- `static/index.html`: +sort dropdown, +3 new list columns (Uses,
  Usefulness, +/-), +4 new detail cards, +Usefulness Explanation block,
  +Mark Useful/Mark Not Useful buttons, +4 new overview cards.

GET-only browsing never mutates - confirmed by
`test_dashboard_get_never_mutates_usefulness_or_feedback_fields`.

## 16. Tests

`tests/test_memory_learning.py` (66 scenarios - schema/backward-
compatibility, usage tracking confirmation incl. no-double-count/
irrelevant/archived/budget-rejected exclusion, positive/negative feedback
incl. bounding and ambiguous-target-is-caller's-job, correction incl.
history preservation, retrieval integration incl. relevance-mandatory and
tie-breaking-only, persistence incl. simulated restart and old-schema
loading and malformed-metadata safety, maintenance integration incl. the
Section 16 worked example and protected/conflict safety, explainability,
dashboard surface incl. sort modes and no-mutation-on-GET, and Verified
Facts/Episodic Memory structural isolation).

3 new end-to-end scenarios in `tests/test_runtime_demo.py` (matching this
repository's own established precedent of adding memory-sprint E2E
scenarios there, not a separate file):
`test_memory_learning_feedback_loop_end_to_end_positive_confirmation_scenario_a`,
`_correction_scenario_b`, `_ambiguous_feedback_never_mutates` - all through
the real `PlannerBridgeModule`/`RuntimeDemoConsole` production bridge.

## 17. Known limitations / technical debt

- The conversational feedback phrase sets are small, fixed, anchored lists
  (same discipline every detector in this file already uses) - phrasing
  outside those lists is not recognized (accepted: under-recognition is
  safe, over-recognition risks misapplied feedback).
- The residual pending-confirmation-ordering risk documented in §14.
- Episodic Memory is not used as corroborating evidence for feedback
  (§12) - no existing read path supports it; a future sprint could add one
  if genuinely wanted, but that is new scope, not "wire two existing
  things together".
- The usefulness usage-nudge and feedback deltas (0.02/0.15) are hand-
  chosen constants, not learned/tuned against real usage data (consistent
  with this codebase's own established "deterministic, explicit, small
  hand-maintained constants over anything learned" philosophy - see
  `_CONSOLIDATION_MIN`/`_CONSOLIDATION_MAX`'s own history for precedent).
- `needs_review` dashboard sort recomputes `analyze_memory_maintenance()`
  (an O(n^2) bounded sweep) lazily, only when that specific sort mode is
  requested - acceptable at this store's current modest size, same
  accepted trade-off the maintenance planner's own pairwise sweep already
  made.
