# Change Impact Analysis: Memory Lifecycle & Maintenance Engine

**Written BEFORE implementation**, per this sprint's own audit-first
requirement. Updated after implementation only if something material
changed from what's described here (see the note at the bottom).

## Baseline (verified directly against the repository, not assumed)

- `luno/` fast suite: 806 passed / 808 total (2 known-flaky Barge-in
  tests, unchanged root cause).
- Named 13-file memory/relationship/emotion/personality/runtime batch:
  501 passed / 501.
- `tests/test_production_launcher.py`: 23 passed / 24 (1 known
  environment-specific failure).
- All three numbers match the sprint brief's own stated "expected
  historical baseline" exactly - confirmed by actually running the
  suites, not by trusting the brief's numbers.
- 10 persistent state files SHA256+mtime hashed before AND after the
  baseline test run - byte-identical, confirming the test suite itself
  causes no drift. Two files (`config/relationship_state.json`,
  `config/vision_memory.sqlite3`+`-wal`/`-shm`) have DIFFERENT hashes
  than the snapshot recorded at the end of the previous (Memory Prompt
  Intelligence) sprint - this reflects genuine real-world usage of the
  production system between sessions (Vinn actually using Luno), not
  test contamination; the baseline this sprint measures against is the
  freshly-captured snapshot above, not the older one.

## Architecture Audit

### Current lifecycle model - confirmed pure, deterministic, on-demand

`compute_lifecycle(entry, now=None)` (`luno/memory.py`) is a PURE
function of `(importance, updated_at, source, now)`. It is NEVER
persisted, NEVER mutated by a background process, and is recomputed on
demand everywhere it's used (retrieval ranking, prompt selection,
tests). Thresholds scale by `_get_importance(entry)` via
`_LIFECYCLE_THRESHOLDS_DAYS` (a dict keyed 0-4), with a 1.5x slower-decay
multiplier for `source == "user_explicit"`, and a structural
never-archives guarantee for `importance >= 4` (falls back to "stale",
never "archived", once past the stale threshold). This sprint does NOT
replace or duplicate this function - it EXTENDS it with exactly one more
deterministic input (a new, additive, default-`False` flag - see
"Implementation" below), keeping it a pure function of one more optional
input, not a second lifecycle model.

### Current data model on `_memories` entries (confirmed by full read)

`id`, `text`, `created_at`, `updated_at`, `category`, `importance` (0-4),
`history` (bounded list, `_MAX_MEMORY_HISTORY_ENTRIES = 5`), `source`
(`"user_explicit"`/`"llm_auto"`), `schema_version`, and OPTIONALLY
`conflict_status`/`conflict_group` (Memory Conflict Resolution sprint).
No usage-tracking field exists yet - `retrieval_count`/
`last_retrieved_at` are genuinely new.

### Exhaustive caller audit (grep, this session)

- `add_memory`/`update_memory`/`delete_memory_by_id`/`search_memories`/
  `build_memory_prompt`/`make_manual_memory_source`: called only from
  `main_runtime_demo.py` (production) and `luno/main.py` (legacy) among
  production code, plus test files. No other production module touches
  `_memories` directly - confirmed via `grep -rn "memory\._memories"`
  across the repo (only test files and this module itself match).
- `luno.memory_guard.VerifiedFactStore`: structurally separate store
  (`self._facts: Dict[str, Dict]`, keyed by `entity_id`, own JSON file
  `config/verified_facts.json`). Re-confirmed zero code coupling to
  `luno.memory`/`_memories` (grep for `_memories`/`build_memory_prompt`/
  `memory.add_memory` inside `luno/memory_guard.py`: zero matches outside
  comments explaining the deliberate isolation).
- `luno.episodic_memory`: separate store (`EpisodicMemoryStore`, own file
  `config/episodic_memory.json`), imports only `luno/config.py` +
  `luno/memory_retrieval/models.py`+`/query.py` - never `luno.memory`.
  Re-confirmed unchanged since the last two sprints' own audits.
- `luno/vision_memory/importance.py`: a STRUCTURALLY SIMILAR but entirely
  separate 1-5 event-importance scale for Vision Memory's own store
  (SQLite, `config/vision_memory.sqlite3`) - confirmed this is a
  different system with a different scale (1-5 vs. this module's 0-4),
  referenced only as a design precedent in this file's own comments, not
  a shared implementation. This sprint does not touch it.
- `tests/conftest.py`: `LONG_TERM_MEMORY_FILE` already in
  `_WRITABLE_STATE_ATTRS`; `luno.memory._memories` already reset to `[]`
  per-test via the `isolate_persistent_state` autouse fixture. No new
  isolation target needed for this sprint - no new persistent file is
  being introduced.

### Verified Facts / protected-memory boundary

Step 13 requires "protected verified facts" never be auto-archived. Since
`VerifiedFactStore` facts are NEVER represented as `_memories` entries at
all (confirmed above), this requirement is satisfied structurally by
construction - the maintenance engine this sprint builds only ever reads/
writes `_memories`, so it has no code path that could touch a verified
fact in the first place. No code change needed there; documented, not
modified.

## Planned Implementation

All additions go into `luno/memory.py` (EXTENDED again, same file every
prior "extend the memory system" sprint touched - Manual Memory
Management, Memory Intelligence, Memory Conflict Resolution, Memory
Prompt Intelligence). Not a new module: this sprint's entire logic
operates on the SAME `_memories` store using the SAME importance/
lifecycle/conflict fields those sprints already established - genuinely
different data (Episodic Memory, Relationship state) got its own file in
earlier sprints; this sprint's data does not.

### Data model additions (additive only, no schema-version bump needed -
existing fields already tolerate absence via `.get(...)` defaults)

- `retrieval_count` (int, default 0 when absent) - Step 4.
- `last_retrieved_at` (iso string or absent) - Step 4.
- `archived_by_maintenance` (bool, default falsy when absent) +
  `archived_at` (iso string) - the concrete, non-destructive representation
  of Step 10's "archive -> lifecycle/state update only." `compute_lifecycle()`
  gains ONE new check at the top: if this flag is set, return "archived"
  immediately (bypassing the age-based computation) - still a pure
  function, still on-demand, still never auto-set by any background
  process; only ever set by the EXPLICIT `apply_maintenance_plan()`
  execution path (Step 12: "only explicit maintenance commands may mutate
  state" for archive/consolidate specifically).

### Usage tracking (Step 4/5) - `record_memory_usage()`

Hooked into `main_runtime_demo.py`'s EXISTING
`relevant_memories_early = self.memory_retriever.retrieve_memories(text)`
call site (one new try/except block immediately after it, same pattern
every other note-producing call site in that method already uses) -
NOT hooked into `build_memory_prompt(query_text=...)`/
`_select_memories_for_prompt()`, which the Memory Prompt Intelligence
sprint's own tests explicitly proved and guard as read-only/non-mutating
(`test_prompt_generation_never_calls_save`,
`test_prompt_generation_does_not_mutate_entries`) - this sprint's own
"never weaken an existing test" rule means that boundary stays intact.
`record_memory_usage()` only increments for `RelevantMemory` objects
whose `.source == "manual_memory"` (never touching vision/episodic/
planner-state results also present in the same combined, ranked, budget-
limited list) - i.e. only entries that survived relevance gating AND the
retrieval budget, exactly satisfying Step 4's "merely existing in the
database does not count as usage."

Conservative reinforcement (Step 5) is folded into the same function:
every `_REINFORCEMENT_RETRIEVAL_THRESHOLD`-th (5th) genuine retrieval
bumps importance by exactly +1, capped at 3 - frequency can NEVER reach
4 through this path (only the pre-existing explicit-signal path in
`_classify_memory_importance`/`mark_last_memory_important()` can).
Already-importance>=3 entries are skipped entirely (no-op), satisfying
"explicit user-marked important memories remain highest priority."

### Maintenance planner (Steps 6-9) - `analyze_memory_maintenance()`

Analysis-only, never mutates anything. Per-entry base classification
(protected/archived-already/obsolete-wording/stale-with-usage/active),
refined by a bounded pairwise redundancy sweep (reusing the EXISTING
`_CONSOLIDATION_MIN`/`_CONSOLIDATION_MAX` Jaccard band and
`_classify_conflict()` waterfall - no second tokenizer, no second
threshold set) that can upgrade a base "keep"/"archive" recommendation to
"consolidate" (exact/near duplicate) or "review" (correction/temporal/
ambiguous pair that wasn't already merged live, or an existing unresolved
conflict). Deterministic and side-effect-free: same `_memories` state +
same injected `now` always produces the same plan (Step 9's own
requirement), verified by a dedicated test.

Obsolete-wording detection (Step 7) is a NEW small regex
(`_OBSOLETE_WORDING_RE`), reusing `_TEMPORARY_WORDING_RE`'s existing
pattern text (not retyped) plus a few additions from the brief's own
example list ("currently", "untuk sekarang", "lagi coba", "temporary") -
deliberately a SEPARATE regex from the save-time
`_TEMPORARY_WORDING_RE` rather than extending that one in place, since
extending it would silently change SAVE-TIME importance classification
for every future save containing a newly-added word, an unrelated
behavior change out of this sprint's scope. Checked independent of
lifecycle/age (an "active"-by-age entry can still be flagged obsolete by
wording; a "stale" entry with no obsolete wording is not automatically
flagged), satisfying Step 7's explicit "never use age alone."

### Execution (Step 10) - `apply_maintenance_plan()`

Separate, explicit function - never called automatically. `keep`/
`review` are no-ops. `reinforce` bumps importance (same +1/cap-3 rule as
live usage-driven reinforcement). `archive` sets the two new flags
(never deletes). `consolidate` only applies when the plan entry's
`confidence >= _CONSOLIDATION_APPLY_THRESHOLD` (0.75) AND names a
`consolidate_with` survivor id - merges the loser's text into the
survivor's `history` (reason=`"maintenance_consolidation"`, reusing the
EXISTING bounded-history mechanism and the exact merge pattern
`resolve_conflict_by_topic()` already established) and removes the loser
as a top-level entry - the loser's text is never lost, only relocated
into the survivor's history. Defense in depth: even if a plan entry
incorrectly recommends `archive` for a currently-protected entry
(importance>=4 or unresolved conflict), execution refuses and reports
`blocked_protected` rather than trusting the plan blindly.

### Dry-run / health report / commands (Steps 11, 12, 14)

`preview_maintenance_text()` renders `analyze_memory_maintenance()`'s
output grouped by action, matching the brief's own example format -
calls the planner only, never the executor. `memory_health_report()`
(dict) + `format_memory_health_report()` (text) compute the Step 14
breakdown (total/active/stale/archived, importance histogram, usage
histogram, potential duplicates/conflicts, review-required, protected
count) - read-only, reuses `analyze_memory_maintenance()` for the
duplicate/conflict/review counts rather than re-deriving them.

Eight deterministic, anchored command detectors (Step 12's own example
list, no extras added "for feature count," matching the Memory Conflict
Resolution sprint's own precedent for scope discipline): health report,
analysis/preview (two synonymous trigger phrases -> the same preview
output), run maintenance (two synonymous trigger phrases -> the same
execution), archive-by-id, un-archive-last-touched. Wired into
`main_runtime_demo.py`'s existing `_handle_explicit_memory_command()`
meta-command interception point (same one every previous memory sprint's
commands already use) - ordinary conversation never reaches these
branches, satisfying Step 12/15's "only explicit commands trigger
maintenance, never ordinary conversation."

### Bounded maintenance (Step 15)

`analyze_memory_maintenance()` is O(n) for the base per-entry pass plus
O(n²) for the pairwise redundancy sweep - acceptable because it ONLY
ever runs when an explicit maintenance command is detected, never on
ordinary conversation turns (which continue using the existing, already-
bounded `MemoryRetriever`/`build_memory_prompt(query_text=...)` paths,
untouched by this sprint except for the one usage-tracking hook). No
scheduler, no background job, matching Step 15's explicit "no background
scheduler in this sprint."

## Compatibility

Old entries (missing `retrieval_count`/`last_retrieved_at`/
`archived_by_maintenance`): every new accessor (`_get_retrieval_count()`,
the new `compute_lifecycle()` flag check) defaults safely via
`.get(...)`, matching the existing `_get_importance()` precedent exactly.
Malformed entries (wrong types, non-dict list items): every new function
reuses the SAME `isinstance(m, dict) and m.get("text")` guard already
used throughout this file.

## Risks Identified Before Implementation

1. **Reinforcement automatically mutating state on ordinary turns** - Step
   12 says "only explicit maintenance commands may mutate state," which
   at first reading seems to conflict with Step 5's usage-driven
   reinforcement running automatically. Resolved by reading Step 12 in
   its own context (about the MAINTENANCE planner/executor specifically,
   the heading is literally "MANUAL COMMANDS") - usage tracking/
   reinforcement is ordinary bookkeeping analogous to the ALREADY-
   existing `_reinforce_existing_memory()` (which already auto-bumps
   importance on every exact-duplicate `add_memory()` hit, no command
   needed, established two sprints ago) - not "maintenance" in the
   archive/consolidate/destructive sense.
2. **`compute_lifecycle()` is documented elsewhere as "NEVER stored,
   NEVER mutated"** - the new `archived_by_maintenance` flag is stored
   metadata that INFLUENCES the computation, not the lifecycle VALUE
   itself being stored - `compute_lifecycle()` still recomputes on every
   call, still returns a value never written back to the entry. This
   preserves the letter and spirit of that guarantee.
3. **Double-counting usage across two retrieval paths** - only the
   `MemoryRetriever` path (`memory_block`) is instrumented, not
   `build_memory_prompt(query_text=...)` (`explicit_memory_block`), to
   avoid violating the Memory Prompt Intelligence sprint's own read-only
   test guarantees. Documented as an intentional, honest scope boundary.

## Post-implementation update

None required - implementation matched this plan; see the final sprint
report and `ARCHITECTURE_GUARD.md`'s "Memory Lifecycle & Maintenance"
subsection for the as-built description.
