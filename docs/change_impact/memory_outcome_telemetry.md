# Change Impact Analysis - Memory Outcome Telemetry & Closed-Loop Learning

## 0. What this sprint closes

The Memory Evaluation & Self-Calibration sprint built `classify_context_outcome()`
- deterministic, tested, correct - but never wired it into production.
This sprint closes that specific gap and makes the full loop real:

    retrieve -> context selected -> response -> user reaction -> outcome
    -> memory evidence -> evaluation/calibration

Every other piece of that loop (retrieval, context assembly, feedback
detectors, `apply_positive_feedback()`/`apply_negative_feedback()`,
`update_memory()`, `evaluate_memory()`, `calibrate_memory()`) already
existed and is REUSED, not rebuilt.

## 1. Architecture audit - actual production call path (traced, not assumed)

- **`assemble_context()` is called** once per turn, in
  `main_runtime_demo.py`'s `_handle_utterance()` (the same call site the
  Memory Context Assembly and Memory Evaluation sprints already
  established), fed `relevant_memories_early` (computed once, earlier in
  the same method, from `self.memory_retriever.retrieve_memories(text)`).
- **The selected context is stored for the turn** in a local
  `assembled_context` variable, rendered into `memory_context_block`, and
  appended to `notes` for the LLM prompt. Before this sprint, nothing
  ELSE about the selection was retained past this one method call except
  the already-existing `record_context_selection()` call (Memory
  Evaluation sprint) and `_session_feedback_target` (Memory Learning
  sprint, tracks only a single, possibly-ambiguous manual-memory id).
  This sprint adds `MemoryTurnTrace` as the richer, still-transient,
  still-bounded record of that selection.
- **The assistant response finishes being generated** downstream of this
  method, in the OpenRouter adapter (`need_llm_response` ->
  `llm_finished`) - out of scope for this sprint, never touched.
- **The next user turn arrives** as a new `user_utterance` event, routed
  back into `_handle_utterance()` - the SAME method, called again. This
  is where outcome classification against the PREVIOUS turn's trace/
  target must happen, and already does (via `_session_feedback_target`
  and now, formally, `classify_context_outcome()`).
- **Explicit memory feedback is processed** in
  `_handle_explicit_memory_command()` (the "memory ini berguna/salah"
  commands) and **conversational feedback** in
  `_handle_memory_feedback_command()` - both pre-existing, both extended
  this sprint (see §3/§4 below), not replaced.
- **Correction is processed** by the EXISTING `update_memory(reason="correction")`
  path, called from `_handle_memory_feedback_command()`'s correction
  branch - unchanged this sprint except for the dispatch condition that
  reaches it (`outcome == "correction"` instead of a direct
  `detect_memory_feedback_correction()` call - same underlying detector,
  now behind one shared classification function).
- **`classify_context_outcome()` before this sprint** lived in
  `luno/memory.py`, fully implemented, fully tested
  (`tests/test_memory_evaluation.py`), called from NOWHERE in production
  - confirmed by a repo-wide search finding zero call sites outside its
  own tests.
- **`record_memory_usage()` runs** once per turn, right after
  `self.memory_retriever.retrieve_memories(text)` returns, unchanged by
  this sprint.
- **`evaluate_memory()`/`calibrate_memory()` run** wherever a feedback
  event or explicit recalibration happens - 5 pre-existing call sites in
  `main_runtime_demo.py`, 1 in `luno/dashboard/controls.py`
  (`memory_recalibrate()`) - all unchanged in mechanism, all now ALSO
  reached via the reformulated dispatch (§3).

## 2. `MemoryTurnTrace` (Step 3) - new, transient module

`luno/memory_turn_trace.py` is a new, small, leaf module (imports
`luno.memory` for `get_conflict_group_member_ids()`; consumes
`ContextItem`/`AssembledContext`/`RelevantMemory` shapes without
importing `luno.memory_context` back into anything - preserving that
module's own one-way dependency direction). `MemoryTurnTrace` fields:
`turn_id`, `context_timestamp`, `candidate_memory_ids`/
`relevant_memory_ids`, `selected_memory_ids`/`rendered_memory_ids`,
`selected_verified_fact_ids`, `selected_experience_ids`,
`selection_reasons` (id -> short string), `retrieval_scores` (id ->
float). Never persisted by this module - `main_runtime_demo.py` holds
the MOST RECENT trace per conversation in a bounded (`max=50`),
session-scoped, REPLACED-not-appended dict (`_last_turn_trace`), reset
on conversation end - the exact same convention `_session_feedback_target`
already established.

**Candidate/relevant and selected/rendered are structurally identical in
this codebase today** - documented honestly in the module's own
docstring rather than inventing daylight that doesn't exist:
`MemoryRetriever`'s per-source `token_overlap()` relevance gate has
already run before any `RelevantMemory` reaches this module (an
irrelevant item is never returned as a "candidate" in the first place),
and `AssembledContext.render()` renders exactly `.items` (nothing
selected is ever separately dropped before rendering). All four names
are kept as distinct fields anyway, for forward compatibility (hard
constraint #20 - additive/backward-compatible), not collapsed to two.

## 3. Selection tracking without double-counting (Step 4/5)

`build_turn_trace()` builds every id set from PLAIN `set()`s, which
handles most double-counting cases by construction: a memory appearing
twice (once current-text, once historical-text - both share the same
underlying `memory_id`) collapses to one entry automatically; a memory
id appearing twice in `assembled_context.items` (hypothetically, from
two sections) likewise collapses to one.

The one case that needed EXPLICIT handling: a selected ambiguous-
conflict-group joint note is rendered under a SYNTHETIC id
(`f"conflict:{group_key}"`, from `memory_context._manual_memory_conflict_items()`)
that is not a real `_memories` entry. Recording evidence against that
synthetic id would silently no-op in `record_context_selection()` (an
unknown id), meaning the conflict's REAL members would never receive
their due evidence credit at all - not double-counting, but
UNDER-counting, an equally real bug this sprint fixes. The new
`get_conflict_group_member_ids(conflict_group)` resolves the synthetic
note back to its real member ids, and `build_turn_trace()` awards each
real member exactly one selection credit for the turn.

The prior sprint's own `main_runtime_demo.py` call site had this exact
gap (a plain set-comprehension keyed off `item.memory_id` directly, with
no conflict-group awareness) - fixed by replacing it with
`build_turn_trace()`.

## 4. Outcome classification wired to production (Step 6)

`_handle_memory_feedback_command()` previously called
`detect_memory_feedback_correction()`/`detect_positive_memory_feedback()`/
`detect_negative_memory_feedback()` independently, in that order, each
gating its own branch. This sprint replaces that with a single
`outcome = memory.classify_context_outcome(text)` call at the top of the
method, and each branch now checks `outcome == "correction"`/`"negative"`/
`"positive"` instead. The observable behavior for every case the prior
sprint's own extensive tests already cover is UNCHANGED (re-verified:
every pre-existing test in `tests/test_memory_learning.py`/
`tests/test_runtime_demo.py` still passes, unmodified, in this sprint's
own regression sweep) - this is a refactor of WHICH function decides,
not a behavior change.

Priority, now literally enforced by `classify_context_outcome()`'s own
checking order: correction > explicit negative > explicit positive >
"clear contextual confirmation" > neutral > unknown. Negative is now
checked before positive (previously the reverse) - the regex sets are
fully anchored and mutually exclusive today (proven by every existing
test still passing unchanged), so this reordering changes zero existing
classifications; it only makes the priority explicit and protects
against a FUTURE regex addition that might overlap.

**"Clear contextual confirmation" (priority 4) is a scope decision, not
an oversight:** this codebase has exactly one deterministic "user is
confirming" detector. Building a second, broader one that tries to infer
confirmation from ordinary conversational continuation is precisely what
hard constraint #7 ("no autonomous mutation berdasarkan tebakan"), hard
constraint #19 ("no LLM judge"), and the sprint's own explicit "jangan
menginfer positive outcome hanya karena user melanjutkan percakapan"
forbid. Priority levels 3 and 4 therefore collapse onto the same check
by design.

## 5. Evidence mapping (Step 7)

New function `record_outcome_evidence(memory_id, outcome)`:

| outcome | evidence mutation |
|---|---|
| `positive` | `retrieval_success_count += 1` (bounded) |
| `negative` | `retrieval_miss_count += 1` (bounded) |
| `correction` | none here - `correction_count` is `update_memory()`'s own, exclusive responsibility |
| `neutral` | none |
| `unknown` | none |

Deliberately does NOT call `apply_positive_feedback()`/
`apply_negative_feedback()` - those remain the caller's job, invoked
explicitly alongside this function at every call site (5 in
`main_runtime_demo.py`, 2 in `luno/dashboard/controls.py`). This keeps
each mutation traceable to one function with one job, rather than one
function silently doing two things.

`retrieval_success_count`/`retrieval_miss_count` are now composed of TWO
evidence sources - the prior sprint's context-selection tracking
(`record_context_selection()`, "was this memory actually used in
context this turn") and this sprint's conversational-outcome tracking
(`record_outcome_evidence()`, "did the user confirm/dispute a memory you
referenced"). Both are legitimate "was retrieving/using this memory a
good idea" evidence and `evaluate_memory()` (unmodified) already treats
`retrieval_success_count`/`retrieval_miss_count` generically as such -
this is an additive composition, not a semantic break, and is documented
at both write sites' own comments plus `ARCHITECTURE_GUARD.md`.

## 6. Correction and ambiguity (Step 8/9) - unchanged safety, re-verified

The correction branch is unchanged in mechanism: resolves target via the
existing `_session_feedback_target`, calls the existing
`update_memory(reason="correction")`, calls the existing
`apply_negative_feedback(reason="user_correction")`, then the prior
sprint's `record_feedback_event()`/`calibrate_memory()`. A bare "itu
salah" (no replacement clause) classifies as `negative`, never
`correction` - `detect_memory_feedback_correction()`'s regex requires an
actual captured replacement value, confirmed by a dedicated test.

Ambiguity safety is unchanged and re-verified end-to-end (Scenario D):
when more than one manual-memory candidate is surfaced in one turn,
`_update_session_feedback_target()` (pre-existing, untouched) clears the
target rather than guessing. Every outcome branch's own "no target -> no
mutation, log and return" guard then fires for correction, negative, AND
positive alike - proven this sprint for all three, not just the
negative case the prior sprint's own ambiguous-feedback test covered.

## 7. Bounded telemetry (Step 10) - no unbounded log anywhere

No new unbounded structure exists anywhere in this sprint's code.
`MemoryTurnTrace` is transient (never written to disk), one-per-
conversation (replaced, never appended) via `_last_turn_trace`, and
every new persistent mutation this sprint performs is a plain, bounded
integer counter on the EXISTING additive schema (`retrieval_success_count`/
`retrieval_miss_count`, already `_MAX_RETRIEVAL_COUNT`-capped from the
prior sprint). No `memory.events = [...]` growth-log was built, matching
the sprint brief's own explicit joke/warning about that exact
anti-pattern.

## 8. Calibration loop boundary (Step 12/13) - unchanged deltas, new trigger condition only

`calibrate_memory()` itself is completely unmodified - still writes only
`evaluation_score`/`last_evaluated_at`. What's new is WHEN it's
triggered: exactly the same 7 call sites as before (5 in
`main_runtime_demo.py`, 2 in `luno/dashboard/controls.py`), now each
additionally preceded by a `record_outcome_evidence()` call where
applicable. `"neutral"`/`"unknown"` outcomes have no branch in
`_handle_memory_feedback_command()` at all, so they structurally cannot
reach `calibrate_memory()` - "no evidence -> no calibration" is enforced
by the absence of a code path, not a runtime check that could be
bypassed. Per-event score deltas are entirely the prior sprint's own,
unmodified, already-bounded constants (`_EVAL_POSITIVE_FEEDBACK_DELTA`
etc.) - a single event still cannot swing a score to either extreme.

## 9. Read-only outcome API (Step 14)

`get_memory_outcome_summary(memory_id)` - a thin reshaping of
`get_memory_evidence_counts()` + `evaluate_memory()`'s live output into
exactly the 8-field shape the sprint brief specifies. Returns `None` for
an unknown id. Never includes `text`/`history`/anything transcript-
shaped - confirmed by a dedicated test asserting those keys are absent.

## 10. Dashboard (Step 15/16) - no new page, and an honest scoping decision

`collect_memory_detail()` gained `outcome_summary`
(`get_memory_outcome_summary()` verbatim) and `selection_explanation`
(`get_memory_selection_explanation()`'s ready-to-render text).
`static/index.html` gained an "Outcome" card row and a "Why selected /
not selected?" panel in the existing detail modal - no new panel/page.

**Scoping decision, documented rather than guessed at:** the sprint
brief's own "Why selected?"/"Why not selected?" example text reads as
per-turn, per-query explainability. The Memory Dashboard, however, is a
stateless HTTP read path with no "current query" of its own - the only
place a per-turn `MemoryTurnTrace` exists is in-process, on one
`PlannerBridgeModule` instance, scoped to one conversation, for one
turn. Plumbing that live, ephemeral state all the way to a separate HTTP
read path would require either (a) persisting a query-by-query replay
log (directly violating hard constraint #16/#17 - bounded, no
transcript), or (b) a live cross-process link into the runtime's
in-memory state (architecturally heavy, fragile, out of proportion for
this sprint's actual gap). Instead, `get_memory_selection_explanation()`
builds a STANDING explanation from persisted, bounded signals
(importance/usefulness/evaluation/lifecycle/selection-history counts) -
"if queried right now, here is this memory's evidence profile" - honest
about what it is and is not, explicitly labeled "(standing, not tied to
one specific past query)" in its own output, never claiming to replay a
specific historical turn it cannot actually reconstruct from bounded
data. Language throughout avoids truth claims - "Evidence suggests this
memory remains useful," never "Memory is TRUE" or "AI decided this."

## 11. Verified Facts / Episodic Memory - audited, not modified

Zero lines of `luno/memory_guard.py`/`luno/episodic_memory.py` changed.
`MemoryTurnTrace.selected_verified_fact_ids`/`selected_experience_ids`
are populated (read-only, for future explainability use) but nothing in
this sprint ever writes evidence onto either - there is no
`record_verified_fact_evidence()`/`record_experience_evidence()`
function anywhere, confirmed by a dedicated test.
`build_turn_trace()`'s own `source == "episodic_memory"` check reads a
pre-existing STRING TAG on `ContextItem` (the same source-name
convention `memory_context._SOURCE_PRIORITY` already uses) - not an
import of or reference to the `EpisodicMemoryStore` class/module,
confirmed via a structural `inspect.getsource()` scan.

## 12. Tests

`tests/test_memory_outcome_telemetry.py` (40 scenarios): selection
tracking (selected-tracked, unselected-relevant-not-counted, irrelevant-
never-a-candidate, conflict-group-counted-once via real member
resolution, historical/duplicate-section non-double-counting via `set`
semantics, Verified Facts/experience ids tracked separately and
read-only, `MemoryTurnTrace` never carries message text, pure/no-`_save()`);
outcome classification matrix incl. silence-is-unknown and the
ambiguous-"itu salah"-is-negative-not-correction distinction; priority
incl. correction-beats-negative, `memory_was_updated` always wins, a
structural source-order proof that negative is checked before positive,
positive-beats-neutral, unknown-stays-unknown; evidence mapping incl.
bounded/no-oscillation and correction-is-a-no-op-here (avoiding a double
mutation path); safety incl. no-guessed-mutation on an unresolved id,
unknown-never-changes-score, dashboard-GET-never-mutates,
text/history/importance-untouched; and the Verified Facts/Episodic
Memory structural isolation scan.

4 new end-to-end scenarios in `tests/test_runtime_demo.py`, all through
the REAL `PlannerBridgeModule`/`RuntimeDemoConsole`:
`test_memory_outcome_telemetry_end_to_end_positive_scenario_a` (save ->
retrieval -> confirm -> both context-selection AND outcome evidence
increment -> dashboard reflects it read-only);
`_negative_scenario_b` (retrieved -> disputed -> unambiguous target
mutated conservatively, nothing deleted, score never crashes to 0);
`_correction_scenario_c` (explicit correction still fully authoritative
via the unchanged `update_memory()` path, proving the new dispatch
refactor didn't regress it); `_ambiguous_scenario_d` (two candidates, no
unique target, zero mutation on either memory, every counter provably
unchanged).

## 13. Backward compatibility

No new persistent schema field - this sprint adds zero new fields to a
manual-memory entry, only new functions that read/write fields the PRIOR
sprint already introduced (`retrieval_success_count`/
`retrieval_miss_count`/`correction_count`). `MANUAL_MEMORY_SCHEMA_VERSION`
remains 4, unchanged. A pre-sprint-4 entry (or any entry that predates
this sprint entirely) behaves identically to before - every accessor
this sprint touches was already backward-compatible from the prior
sprint's own work.

## 14. Risks

- "Clear contextual confirmation"'s collapse onto the explicit-positive
  detector (§4) means a genuinely non-explicit-but-clear confirmation
  ("great, thanks" after a memory-referencing reply, say) is currently
  classified `unknown`, not `positive` - conservative by design (silence/
  ambiguity never becomes positive), but a real, documented limitation
  rather than a bug.
- The dashboard's "why selected" explanation (§10) is standing, not
  per-turn - a user reading it may reasonably expect a literal replay of
  "why did you show me this JUST NOW," which it does not provide.
  Documented in the UI copy itself ("standing, not tied to one specific
  past query") and here.
- `retrieval_success_count`/`retrieval_miss_count`'s two-source
  composition (§5) means the raw number alone no longer maps to one
  narrow definition - mitigated by consistent "Retrieval Success/Miss"
  labeling wherever it's surfaced, never a more specific claim like
  "times selected."
