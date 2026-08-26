# Change Impact Analysis: Memory Context Assembly & Retrieval Unification

Status: COMPLETE (updated after implementation, testing, and full regression -
see "Post-Implementation Update" at the end of this document for what actually
happened, including two real deviations from the plan below).

Sections 1-7 below are the ORIGINAL pre-implementation plan, left unedited so
the before/after comparison in the Post-Implementation Update is honest and
checkable - do not read them as a description of the final state; read the
Post-Implementation Update for that.

## 1. Sprint Goal (verbatim intent)

Not "make Luno remember more" — memory quantity is already solved by seven prior
sprints (Manual Memory, Memory Intelligence, Memory Conflict Resolution, Memory
Prompt Intelligence, Shared Experience & Episodic Memory, Memory Lifecycle &
Maintenance, Memory Dashboard). This sprint adds exactly one thing: a
deterministic, bounded, conflict-safe **selection** step that decides, for the
current turn only, which of the memory/context pieces Luno already has are
worth handing to the LLM — without creating a second store, a second retrieval
engine, a second tokenizer, a second importance scale, or a second conflict
resolver, and without ever mutating what's already stored.

## 2. Baseline (captured before any implementation code)

Test batch (19 memory/relationship/emotion/personality/runtime files, combined
run): **644 passed / 0 failed**.

`luno/` full suite: **806 passed / 2 failed** (both pre-existing, unrelated to
memory — known-flaky, see `docs/testing/regression_baseline.md`).

`tests/test_production_launcher.py`: **23 passed / 1 failed** — a pre-existing,
known, environment-dependent failure (real outbound network call), unrelated to
memory code; documented in the same baseline doc from the Memory Dashboard
sprint.

Persistent-state hash+mtime snapshot taken before any change, for every
tracked file: `relationship_state.json`, `long_term_memory.json`,
`session_summaries.json`, `habit_memory.json`, `reminders.json`,
`verified_facts.json`, `episodic_memory.json` (absent — created lazily on
first write), and Vision Memory's `config/vision_memory.sqlite3` (+ `-wal`,
`-shm`).

**Caveat found and confirmed by direct experiment**: `config/vision_memory.
sqlite3`/`-wal` are being written to continuously by a process outside this
sandbox — almost certainly a live Luno instance actually running on the user's
own machine, sharing the same mounted project folder, with an active Vision
pipeline. Confirmed by running a combined pytest batch, diffing hashes
(changed), then waiting ~15s with zero test activity in this session and
diffing again (still changed, and `ps aux` inside the sandbox shows no such
process). This drift is external and unrelated to any test run; the other six
tracked files remained byte-identical across repeated pytest runs in this
session, which is the real signal that this session's own test isolation
(`tests/conftest.py`'s autouse fixture, extended over several prior sprints)
continues to work correctly. This caveat will be re-stated, not silently
assumed, in the persistent-state verification step (Step 23) once
implementation tests actually run.

No baseline discrepancy was found relative to the previous sprint's (Memory
Dashboard) final reported state — safe to proceed.

## 3. Architecture Audit Findings

### 3.1 The real production prompt-assembly path

`main_runtime_demo.py`'s `PlannerBridgeModule._handle_utterance()` builds a
plain `notes: List[str]` accumulator. Each independently-built "block" is
computed, wrapped in its own `try/except` (so one failing block degrades to
"adds nothing" rather than breaking the turn), and appended to `notes` only
when non-empty. `notes` is eventually joined into `system_prompt`. This is the
**one and only** real production injection point — not
`luno/core/context_builder.py` (see 3.4 below).

Blocks relevant to this sprint, in their actual current order:

1. `persona_block` — `build_persona_prompt()`, always included, out of scope.
2. `relationship_block` — `RelationshipContextBuilder.build_prompt_block
   (self.relationship_state)`, called right after `RelationshipEngine.
   observe_turn(...)` updates and persists relationship state for this turn.
   Read-only from this sprint's point of view: the *state update* already
   happens elsewhere (relationship engine's own concern); this sprint only
   ever needs to *read* the resulting block, never trigger the update itself.
3. `explicit_memory_note` + `explicit_memory_block` — `memory.
   build_memory_prompt(query_text=text)`, which internally calls
   `_select_memories_for_prompt(query_text)` (Memory Prompt Intelligence
   sprint): already relevance-gated (via `analyze_query`/`token_overlap`),
   lifecycle-aware (excludes `archived`), conflict-group-aware (ambiguous
   conflicts surfaced together, hedged, never picked), historical-query-aware
   (searches `history[]` only for historical-shaped queries), and
   budget-bounded via `MemoryRetrievalConfig.from_env()`. Manual Memory only.
4. `memory_block` — `build_memory_prompt_block(relevant_memories_early)`,
   where `relevant_memories_early = self.memory_retriever.retrieve_memories
   (text)` was already computed earlier in the same method (a `MemoryRetriever`
   pass across every registered source: `vision_objects`, `vision_human`,
   `vision_events`, `long_term_memory` [Vision Memory's own habits store, NOT
   `luno.memory`], `planner_state`, `episodic_memory`, `manual_memory`).
   Same-source-keyed dedup, recency/staleness scoring, ranking, and
   count+token budget limiting all already happen inside `retrieve_memories()`
   itself.
5. `emotional_context_block` — out of scope, unrelated engine.

**Finding confirmed**: `explicit_memory_block` (item 3) and `memory_block`
(item 4) are two **independent, overlapping** memory-derived prompt blocks.
Both read the same underlying Manual Memory store (`_memories` /
`memory.list_memories()`), both relevance-gate against the same user text
using the same `analyze_query`/`token_overlap` primitives, but through two
separate call paths that were built in two separate sprints and never
unified. This is the concrete "duplicate context injection path" this
sprint's Step 18 targets. `memory_block`'s underlying `MemoryRetriever` pass
additionally covers Vision/Episodic/Planner sources that `explicit_memory_
block` does not touch at all — so the two blocks are not pure duplicates of
each other, but for Manual Memory specifically, the same fact can legitimately
appear rendered twice, in two different sentence templates, in the same
prompt.

### 3.2 Verified Facts — write-only in production today

`VerifiedFactStore` (`self.memory_guard`, `luno/memory_guard.py`) has exactly
one production call site: `self.memory_guard.record(task.result, ...)`,
called only after a tool task completes successfully. Its public read methods
— `get(entity_id)` and `all_facts()` — are never called anywhere in
production code today (confirmed via repo-wide grep; only test files call
them). This is a **real, legitimate gap**, not an assumption: Verified Facts
currently cannot influence what the LLM is told about "what state a thing is
actually in" beyond the turn it was verified on. The new Verified Facts
adapter fills this gap using the existing public methods and the existing
`token_overlap()` relevance primitive — it does not add a new store or a new
write path, and `record()` (the only write path) is untouched.

### 3.3 `make_manual_memory_source()` — existing capability and one real gap

`luno/memory.py`'s `make_manual_memory_source()` (the function registered as
the `MemoryRetriever` source `"manual_memory"`) already implements:
token-overlap relevance, archived-lifecycle exclusion (checked before the
relevance gate), and historical-query detection via the private
`_is_historical_query()` (labeling matched `history[]` entries `"[MANUAL
MEMORY - {category}, historical] The user previously said (later
superseded): ..."`). It does **not** implement ambiguous-conflict-group joint
presentation — that logic exists only inside `_select_memories_for_prompt()`
(the separate path behind `explicit_memory_block`). This means today,
`memory_block` (via `MemoryRetriever`) can silently omit or split an
unresolved conflict's two sides, while `explicit_memory_block` (via
`build_memory_prompt`) handles it correctly. Unifying the two paths through
one assembly layer, reusing `_select_memories_for_prompt`'s conflict-grouping
logic (not re-implementing it), resolves this inconsistency as a natural
side effect of Step 18 — not a separate new feature.

### 3.4 `luno/core/context_builder.py` — a separate, vestigial object

`ContextBuilder`/`LLMContext` in this module is used only by the dashboard's
`/api/context` debug preview endpoint. It is not part of the real production
prompt path (`PlannerBridgeModule._handle_utterance()`'s `notes` list, above),
is not imported by `main_runtime_demo.py`'s actual turn-handling code, and
must not be confused with the real injection point. It is out of scope for
this sprint; nothing here will be modified.

### 3.5 Existing reusable machinery inventory (nothing here will be reimplemented)

- Tokenization/relevance: `luno.memory_retrieval.query.analyze_query()`,
  `token_overlap()` — the only public primitives; no public numeric
  similarity function exists yet (a private Jaccard implementation exists in
  `luno/memory.py` but is scoped to storage-level near-duplicate
  *consolidation*, a different, persistence-affecting concern from this
  sprint's transient, read-only cross-source dedup).
- Retrieval engine: `MemoryRetriever` (`luno/memory_retrieval/retriever.py`)
  — candidate collection across registered sources, recency/staleness
  annotation, same-source-keyed dedup (`(source, raw.id or text)`, not
  cross-source), ranking, and count+token budget limiting via
  `_apply_limits`/`_estimate_tokens` (`len(text)//4`, the same rough estimate
  `_select_memories_for_prompt` independently also uses).
- Budget config: `MemoryRetrievalConfig` (`luno/memory_retrieval/models.py`)
  — `enabled`, `max_results` (env `MAX_MEMORY_RESULTS`, default 5),
  `max_tokens` (env `MAX_MEMORY_TOKENS`, default 400),
  `stale_after_minutes`, `retrieval_mode`, `debug`. `.from_env()` is the only
  supported construction path, matching this project's established
  per-package config convention.
- Importance/lifecycle: `_get_importance()`, `compute_lifecycle()`
  (`luno/memory.py`) — the same functions the Memory Intelligence sprint
  established; `active`/`stale`/`archived` states.
- Historical-query detection: private `_is_historical_query()` +
  `_HISTORICAL_QUERY_MARKERS` (`luno/memory.py`) — reusable but currently
  private; this sprint adds a thin public wrapper (e.g. `is_historical_
  query()`) delegating to it, matching the pattern already established for
  every other "expose an existing private helper" case in prior sprints,
  rather than duplicating its marker list.
- Conflict-group handling: the grouping/hedging logic inside
  `_select_memories_for_prompt()` (`luno/memory.py`) — reused by calling
  into the same helper functions it uses (`compute_lifecycle`,
  `analyze_query`, `token_overlap`, conflict-group note construction), not
  by copying its body.
- Relationship context: `RelationshipContextBuilder.build_prompt_block(state)`
  (`luno/relationship_engine.py`) — already a pure, read-only formatting
  function; reused as-is.
- Verified Facts read: `VerifiedFactStore.get()`/`.all_facts()`
  (`luno/memory_guard.py`) — already public, currently production-unused;
  reused as-is, not reimplemented.
- Episodic Memory source: `episodic_memory.make_episodic_experience_source
  (EpisodicMemoryStore.load)`, registered as MemoryRetriever source
  `"episodic_memory"` — already produces `RelevantMemory(source="episodic_
  memory", ...)` records through the same retrieval pipeline as every other
  source.

## 4. Context Source Inventory (Step 3, confirmed against actual code)

| Source | Owning module | Current prompt path(s) | Notes |
|---|---|---|---|
| Manual Memory | `luno/memory.py` (`_memories`) | `explicit_memory_block` (via `build_memory_prompt`/`_select_memories_for_prompt`) AND `memory_block` (via `MemoryRetriever` source `"manual_memory"`) | The duplicate-injection case; has importance/lifecycle/conflict/history/provenance/maintenance metadata already. |
| Episodic Memory | `luno/episodic_memory.py` (`EpisodicMemoryStore`) | `memory_block` only, via `MemoryRetriever` source `"episodic_memory"` | Category + dedup + temporal wording already handled inside the source function itself. |
| Verified Facts | `luno/memory_guard.py` (`VerifiedFactStore`) | none today (write-only) | Real gap this sprint fills via a new read-only adapter. |
| Relationship Context | `luno/relationship_engine.py` (`RelationshipContextBuilder`) | `relationship_block`, standalone, not relevance-ranked | Not a per-item memory source; a single compact banded note. Will be exposed through the assembly layer's output grouping but not merged into the ranked candidate pool, per Step 15's "keep minimal" guidance and the "relationship score must never override memory relevance" rule. |
| Vision Memory (objects/human/events/habits) | Vision Memory subsystem, via `make_vision_*_source` | `memory_block` only, via `MemoryRetriever` | Already fully covered by `MemoryRetriever`; no separate adapter logic needed beyond what registration already provides. |
| Planner state | `main_runtime_demo.py` (`make_planner_state_source`) | `memory_block` only | Same as above. |
| Session summaries | `luno/memory.py` (`build_session_summary_prompt`) | `session_summary_block`, standalone | Out of scope — not a per-item relevance-ranked memory source; left untouched. |

## 5. Planned Design (subject to refinement during implementation, but not to
   any of the hard constraints below)

- New module: `luno/memory_context.py`. Single responsibility: assemble a
  bounded, deduplicated, relevance-ranked, grouped context payload for the
  current turn. Owns no storage. One-way dependency: conversation code →
  `memory_context` → existing memory/context providers. Never imported by
  the modules it depends on (no circular imports).
- `ContextItem` — a transient dataclass (never persisted), fields per the
  spec: `source, memory_id, text, relevance, importance, lifecycle,
  provenance, conflict_group, historical, priority`. Matches this repo's
  existing dataclass conventions (see `RelevantMemory`, `QueryAnalysis`,
  `MemoryRetrievalConfig` in `luno/memory_retrieval/models.py`).
- Source adapters: thin wrappers, one per source in section 4's table,
  translating each source's existing native shape (`RelevantMemory` for
  everything already flowing through `MemoryRetriever`; raw manual-memory
  dicts plus `_select_memories_for_prompt`'s conflict-group notes for Manual
  Memory's conflict case; `VerifiedFactStore` fact dicts for the new
  Verified Facts adapter; `RelationshipContextBuilder`'s formatted string for
  Relationship Context) into `ContextItem`s. No adapter invents a field a
  source doesn't legitimately have.
- Selection policy: relevance gate first (irrelevant discarded regardless of
  importance) → surviving items scored by importance/lifecycle/provenance/
  recency → cross-source transient dedup (exact normalized text → same
  underlying memory ID → token-similarity via `analyze_query()`-derived
  token sets → source identity where available) → conflict-group-aware
  (both sides of a relevant unresolved conflict kept together, never
  arbitrated) → historical-query-aware (history only surfaced for
  historical-shaped queries, explicitly labeled) → budget-bounded via
  `MemoryRetrievalConfig` (both item-count and token limits; conflict pairs
  and Verified Facts protected preferentially when trimming) → grouped
  output by source/type section, omitting empty sections.
- Production integration: `PlannerBridgeModule._handle_utterance()` will
  call the new assembly layer once, using it to replace the *overlapping
  Manual-Memory rendering* currently duplicated between `explicit_memory_
  block` and `memory_block`, producing a single unified memory-context note
  in place of the two. Exact mechanics (whether `memory_block` and
  `explicit_memory_block` are both replaced by one call, or whether the
  assembly layer wraps `retrieve_memories()` and feeds a unified render back
  through the existing block variables) will be finalized during
  implementation, cross-checked against the compatibility rule below before
  any call site is touched.
- Backward compatibility: `build_memory_prompt()` (including its legacy
  no-`query_text` full-dump behavior), `search_memories()`, `make_manual_
  memory_source()`, episodic retrieval, Verified Facts `record()`/`get()`/
  `all_facts()`, and every dashboard memory API remain callable exactly as
  today. The assembly layer is additive; nothing is deleted.
- Read-only guarantee: assembling context for a turn must never call any
  mutating API (`add_memory`, `archive_memory`, `mark_memory_important`,
  `record_memory_usage`, episodic creation, `VerifiedFactStore.record`,
  relationship state writes). Usage-tracking (`memory.record_memory_usage`)
  remains exactly where it is today — driven by `relevant_memories_early`
  from `MemoryRetriever`, not by the new assembly layer — so no retrieval
  path gets double-counted.

## 6. Hard Constraints Carried Into Implementation (verbatim from the sprint spec)

No new memory database, persistent file, retrieval engine, tokenizer,
importance scale, lifecycle system, or conflict-resolution implementation.
No merging Verified Facts into ordinary memory or Episodic Memory into Manual
Memory. Importance never overrides relevance. Ambiguous conflicts never
auto-resolved. Historical values never presented as current. No mutation
during assembly. No persona/emotion behavior changes. No unrelated
refactoring. No test weakened to pass. No LLM call added for relevance
determination. Deterministic and explainable throughout.

## 7. Risks Identified Before Implementation

- The two-path Manual Memory duplication (3.1) means unifying the injection
  point touches a currently-working, tested production code path
  (`PlannerBridgeModule._handle_utterance`) — will require the end-to-end
  test (Step 22) to prove no regression before considering this integrated.
- `config/vision_memory.sqlite3`/`-wal` external drift (section 2) means the
  persistent-state verification step (Step 23) must document this caveat
  explicitly each time, rather than treating any drift on those two files as
  a signal of test contamination — already precedented from the Memory
  Dashboard sprint.
- Cross-source dedup thresholds (token-similarity tier) are a new, sprint-
  scoped constant; must be chosen conservatively (favor under-merging over
  accidentally hiding a genuinely-distinct memory) and documented, not
  copied from the storage-level `_CONSOLIDATION_MIN`/`_CONSOLIDATION_MAX`
  values (a different, higher-stakes, persistence-affecting concern).

This document will be updated after implementation (Step 26) to reflect the
actual final design, deviations from this plan (if any), and their
justification.

---

## Post-Implementation Update

### What was built (matches the plan in sections 1-7 almost exactly)

`luno/memory_context.py` was created as planned: `ContextItem` dataclass,
`AssembledContext` result wrapper, source adapters
(`relevant_memory_to_context_item`, `_manual_memory_conflict_items`,
`_verified_fact_items`), cross-source dedup (`deduplicate_context_items`),
budget (`_apply_budget`), grouping/rendering (`group_context_items`,
`render_context_block`), and the public entry point `assemble_context()`.
Two small, additive pieces were added to `luno/memory.py`: a public
`is_historical_query()` wrapper around the existing private
`_is_historical_query()`, and `group_ambiguous_conflict_entries()` factored
out of `_select_memories_for_prompt()`'s own inline grouping loop (that
function was refactored to call the new one - verified byte-identical
selection behavior via the full pre-existing `tests/test_memory_conflict.py`/
`tests/test_memory_prompt_intelligence.py` suites passing unchanged before
any other implementation work began).

`main_runtime_demo.py`'s `PlannerBridgeModule._handle_utterance()` was wired
exactly as planned: the `explicit_memory_block` call site
(`memory.build_memory_prompt(query_text=text)`) was removed entirely, and the
`memory_block` call site (`build_memory_prompt_block(relevant_memories_early)`)
was replaced with one `memory_context.assemble_context(...)` call, reusing
the already-computed `relevant_memories_early` (no second retrieval pass).
Relationship context was deliberately left OUT of the `assemble_context()`
call (`relationship_state=None`) - the existing `relationship_block` note,
built earlier in the same method via `RelationshipContextBuilder.
build_prompt_block()`, was left completely untouched, per section 3.1's own
plan and Step 15's "keep minimal" guidance.

### Deviations from the pre-implementation plan

1. **Ambiguous-conflict-group members must be excluded from the base
   `MemoryRetriever`-derived pool, not just supplemented by the merged note.**
   The plan (section 5) described adding a merged conflict note "on top of"
   the base pool but did not anticipate that `make_manual_memory_source()`
   (the pre-existing `MemoryRetriever` source) has NO conflict-group
   awareness at all and renders each member as an ordinary standalone item
   regardless of `conflict_status`. Left unfiltered, a relevant conflict
   group would appear TWICE - once as a plain, uncontested-looking fact
   (from the base pool) and once as the correctly-hedged merged note (from
   the new adapter) - which is worse than the pre-sprint behavior for this
   specific case (`_select_memories_for_prompt()`'s conflict-grouping, which
   `explicit_memory_block` used to expose, DID already suppress the
   individual sides). Fixed by explicitly filtering ambiguous-conflict-group
   members out of the base pool inside `assemble_context()` itself -
   `make_manual_memory_source()` and `MemoryRetriever` remain completely
   unmodified; the filter lives entirely in the new module.
2. **`RelevantMemory.stale` is not a substitute for `compute_lifecycle()`.**
   The plan's `_lifecycle_for_relevant_memory()` sketch (implicitly) assumed
   `RelevantMemory.stale` could stand in for the active/stale/archived model.
   These are two unrelated concepts: `.stale` is `MemoryRetriever`'s own
   30-MINUTE retrieval-freshness signal (`MemoryRetrievalConfig.
   stale_after_minutes`, meant for vision-style "how long ago was this
   observed" annotations); `compute_lifecycle()` is Manual Memory's own
   day/month-scale model. Using `.stale` for lifecycle meant any manual
   memory older than 30 minutes was reported `lifecycle="stale"` regardless
   of its real state - caught by this sprint's own test suite before it ever
   reached the production wiring. Fixed: `_lifecycle_for_relevant_memory()`
   now calls `compute_lifecycle()` directly on the raw entry for any source
   that has one (currently only Manual Memory); every other source still
   falls back to `.stale` as the closest available freshness signal, since
   they have no `compute_lifecycle()`-shaped record at all.
3. **Cross-source token-similarity dedup must exclude same-source pairs.**
   The plan (Step 9) described a single Jaccard-similarity tier without
   distinguishing same-source from cross-source pairs. In practice, every
   item from one source shares that source's fixed rendering template (e.g.
   "[MANUAL MEMORY - {category}] The user explicitly asked you to remember:
   ..."), and that shared boilerplate alone was enough to push two
   genuinely-different same-source facts over an 0.8 Jaccard floor in this
   sprint's own test suite. Fixed by restricting tier 3 to cross-source
   pairs only; same-source dedup is already correctly handled by tiers 1-2
   plus `MemoryRetriever`'s own pre-existing same-source `_deduplicate()`.
4. **Same-`memory_id` dedup must also respect the current/historical
   distinction.** Not explicitly anticipated in the plan's dedup hierarchy
   description (section 9's tier 2, "same underlying memory ID"). A current
   rendering and its own historical (superseded) rendering of ONE manual
   memory entry legitimately share the same `memory_id` but are
   deliberately different, both-must-survive content - naively treating
   "same memory_id" as always-a-duplicate silently collapsed one into the
   other (which one survived depended on rank, so this could either hide
   the current value entirely or - worse - present the historical value as
   if it were the only one there, violating Step 12's "never present an old
   value as current state" hard rule). Found via this sprint's own
   production-bridge end-to-end test (turn 4 of the updated conflict-
   resolution end-to-end test). Fixed by requiring the SAME `historical`
   flag before treating a shared `memory_id` as a duplicate.

None of these four fixes required touching any pre-existing function outside
`luno/memory_context.py` itself - all four are contained entirely within the
new module's own logic, caught and fixed before this sprint's implementation
was considered complete.

### Test-assertion updates (in-scope, not test-weakening)

Five pre-existing tests were updated to match Step 18's intentional,
in-scope rendering change (the removal of the two old, independently-
rendered Manual-Memory blocks in favor of one unified, section-labeled
block): `tests/test_runtime_demo.py`'s three memory end-to-end tests (from
the Memory Intelligence, Memory Conflict Resolution, and Memory Prompt
Intelligence sprints) and `tests/test_memory_retrieval.py`'s two production-
bridge tests. Every one of these tests previously asserted on the literal
surface text of one of the two old renderings (`"Relevant Memory:"` or
`"...relevant to this conversation:"`) - text that this sprint intentionally
replaces by design. Each test's underlying claim (relevance-gating,
importance-never-overriding-relevance, conflict-group preservation, current-
vs-historical separation, Verified Facts/persona/episodic isolation) is
UNCHANGED; only the marker string each test searches for was updated to the
new unified section header (`"[Relevant Memories]"` / `"[Historical
Context]"`). `build_memory_prompt_block()`'s own direct unit tests (which
call it directly, not through the production bridge) were left untouched -
that function's behavior did not change. See `docs/testing/regression_
baseline.md`'s own account of this sprint for the full list with line-level
detail.

### Test results

- `tests/test_memory_context.py` (NEW): 31/31 passing - Basic (4),
  Importance (2), Lifecycle (3), Sources (4), Dedup (5), Conflict (3),
  Historical (2), Budget (3), Safety (4), Determinism (1).
- New production-bridge end-to-end test
  (`tests/test_runtime_demo.py::test_memory_context_assembly_end_to_end_
  unifies_sources_through_real_bridge`): passing - proves Verified Facts now
  surface when relevant (closing the previously write-only gap from section
  3.2), an unrelated manual memory never leaks in, only one unified block
  appears per turn, and the Verified Facts store / manual memory store are
  both provably unmutated by context assembly itself.
- Full `tests/test_runtime_demo.py`: 67/67 passing (66 pre-existing + 1 new).
- Combined memory/relationship/dashboard/context/state-isolation batch (14
  files): 546/546 passing.
- `luno/` full suite: 806/808 (2 pre-existing, known-flaky Barge-in failures,
  unrelated to memory).
- `tests/test_production_launcher.py`: 23/24 (1 pre-existing, environment-
  specific network-reachability failure, unrelated to memory).
- Every other `tests/` file: passing, with two known pre-existing,
  environment-specific gaps unrelated to this sprint (`tests/test_mic_
  device_index.py`, `tests/test_real_adapters.py` - both audio/STT-hardware-
  adjacent, missing files/attributes in this sandbox checkout, confirmed via
  direct code inspection to be untouched by this sprint).
- `tests/test_main_bargein.py`/`test_root_main_bargein.py` remain
  uncollectable in this sandbox (missing `faster_whisper` package / missing
  `legacy_main.py` file) - pre-existing, documented environment gaps.

See `docs/testing/regression_baseline.md`'s own "Memory Context Assembly &
Retrieval Unification" section for the complete, authoritative regression
account (exact test names, counts, and root-cause analysis for every
non-passing test).

### Persistent state verification

`config/relationship_state.json`, `config/long_term_memory.json`,
`config/session_summaries.json`, `config/habit_memory.json`, `config/
reminders.json`, `config/verified_facts.json` (`config/episodic_memory.json`
remains absent, as before every prior sprint) were SHA256+mtime checked
immediately before implementation began and again after the full regression
sweep completed: byte-identical, zero mtime change, for all 6 present files.
`config/vision_memory.sqlite3`/`-wal`/`-shm` changed, as expected - re-
confirmed (hash before a test run, then again after 15s of zero test
activity, still changed) to be a live, external process writing to that
database independently of this session's own test activity, not a test-
isolation failure.

### Architecture documentation

`ARCHITECTURE_GUARD.md` gained a new "Memory Context Assembly" subsection
(§3, between "Memory Dashboard & Observability" and "Production Launcher")
and a new Contract Inventory row (§4) - see that document for the
authoritative, final architecture description.

### Final risk assessment (updated from section 7's pre-implementation list)

- The two-path Manual Memory duplication WAS successfully unified, proven
  safe by the full regression sweep plus the new end-to-end test - no
  longer an open risk.
- `config/vision_memory.sqlite3`/`-wal` external drift remains an accepted,
  documented environmental characteristic of this sandbox, not a defect.
- The cross-source Jaccard similarity floor (0.8) is now correctly scoped to
  cross-source pairs only (see deviation #3 above), but remains a new,
  unvalidated-against-real-traffic constant - flagged as ongoing technical
  debt in `ARCHITECTURE_GUARD.md`'s own "Known risks" list for this section.
- `MockHomeAssistantHandler`'s missing `entity_id` in its `ToolResult.data`
  (discovered while writing the Verified Facts end-to-end test) is a
  separate, pre-existing, out-of-scope gap in the MOCK tool handler, not a
  defect in this sprint's Verified Facts adapter - documented, not fixed,
  per this sprint's own "no unrelated refactoring" constraint.
