# Change Impact Analysis: Memory Prompt Intelligence

**Written BEFORE implementation**, per this sprint's own audit-first
requirement. Updated after implementation only if something material
changed from what's described here (see the note at the bottom).

## Baseline

- `luno/` fast suite: 806 passed / 808 total (2 known-flaky Barge-in
  tests, unchanged root cause - timing-window dependent).
- Named 12-file memory/relationship/emotion/personality/runtime batch
  (`test_episodic_memory.py`, `test_manual_memory.py`,
  `test_memory_conflict.py`, `test_memory_guard.py`,
  `test_memory_intelligence.py`, `test_memory_regression.py`,
  `test_memory_retrieval.py`, `test_state_isolation.py`,
  `test_relationship_engine.py`, `test_emotion_engine.py`,
  `test_persona.py`, `test_runtime_demo.py`): 471 passed / 471.
- `tests/test_production_launcher.py`: 23 passed / 24 (1 known
  environment-specific failure - real `.env` credentials present).
- 10 persistent state files SHA256+mtime hashed, matching the prior
  sprint's final recorded values exactly (see
  `docs/testing/regression_baseline.md`'s "Memory Conflict Resolution &
  Trusted Facts Guard" section).

## Architecture Audit

### `build_memory_prompt()` - the actual target

`luno/memory.py:1175-1196` (pre-sprint). Takes no arguments. Reads
`_memories` directly, builds `facts = [m["text"] for m in _memories if
isinstance(m, dict) and "text" in m]`, and unconditionally joins ALL of
them into one sentence:

```
"Things you know about the user from long-term memory (past sessions): "
"{fact1}; {fact2}; ...; {factN}. Use this naturally when relevant, "
"don't just recite the list back verbatim."
```

No importance filter, no lifecycle filter (archived entries ARE
included), no relevance/query-awareness, no conflict-status awareness,
no history awareness. Every entry, every turn, regardless of size.

### Callers of `build_memory_prompt()` (exhaustive, via grep)

1. `luno/main.py:95`, inside `build_system_prompt()` - the LEGACY,
   superseded, single-script consumer (per `ARCHITECTURE_GUARD.md` §2:
   `luno/main.py` inside the package is "the older loose-file,
   single-script style consumer... superseded but not deleted"; the real
   production entrypoint is root `main.py` -> `luno/bootstrap/*` ->
   `main_runtime_demo.py`). `build_system_prompt()` itself takes no
   arguments and is called bare at `luno/main.py:419`, though the
   enclosing per-turn handler DOES have `user_text` in scope.
2. `main_runtime_demo.py:2580`, inside
   `PlannerBridgeModule._handle_utterance()` - **the real production call
   site**. Currently: `explicit_memory_block = memory.build_memory_prompt()`,
   called with zero arguments even though `text` (the current turn's
   utterance) is already in scope at that point in the method (assigned
   at line 2100). Injected into `notes[]` UNCONDITIONALLY whenever any
   memory exists at all - completely independent of what the user just
   said.
3. Four test call sites in `tests/test_memory_regression.py` (loader
   fail-safe checks) - all call `build_memory_prompt()` bare, asserting
   either `""` (empty store) or that a single known fact appears in the
   full-dump output.
4. One test call site in `tests/test_manual_memory.py::
   test_recall_everything_full_list_still_works_unchanged` - an EXISTING,
   PROTECTED test whose own docstring says: *"Pre-existing, protected
   behavior: is_recall_command + build_memory_prompt() still answer 'apa
   yang kamu ingat tentang aku?' with the FULL list, completely
   independent of the new bounded MemoryRetriever source."* It calls
   `build_memory_prompt()` bare and asserts BOTH of two saved facts
   appear in the output.

### The redundancy this sprint exists to fix

`main_runtime_demo.py` already computes a fully importance/lifecycle/
relevance/conflict-aware memory block earlier in the SAME method:

```python
relevant_memories_early = self.memory_retriever.retrieve_memories(text)   # line 2123
...
memory_block = build_memory_prompt_block(relevant_memories_early)          # line 2796
if memory_block:
    notes.append(memory_block)
```

`self.memory_retriever` has the `"manual_memory"` source registered
(`memory.make_manual_memory_source(memory.list_memories)`), which
already does everything this sprint asks for: token-overlap relevance
gate first, importance/lifecycle/source-aware scoring only among
already-relevant candidates, `archived` exclusion, historical-query-aware
`history[]` surfacing, `MemoryRetrievalConfig`-bounded budget
(`max_results`/`max_tokens`).

So today, EVERY turn that has any saved long-term memory gets TWO
memory-derived notes in the system prompt: one already smart
(`memory_block`), and one still a blind, unconditional full dump
(`explicit_memory_block`, from `build_memory_prompt()`). The second one
is exactly the "legacy prompt path" the sprint brief calls out as not
yet "fully controlled" by the importance/lifecycle intelligence that
already exists elsewhere - confirmed by direct inspection, not assumed.

### `is_recall_command()` is NOT wired into the production bridge

Grepped `main_runtime_demo.py` for `is_recall_command`/
`is_session_recall_command` - zero matches. Only `luno/main.py:342` (the
legacy file) uses `is_recall_command()`, and it does so via
`memory.list_memories()` directly, NOT via `build_memory_prompt()`. This
means the "full recall" behavior the protected test documents is a
property of the FUNCTION `build_memory_prompt()` when called bare, not
of any explicit "user asked to recall everything" code path in
production today. The production bridge has no recall-everything special
case at all right now - `build_memory_prompt()`'s bare call there is
purely the ambient, always-on note.

### Budget/config infrastructure already available for reuse

`luno/memory_retrieval/models.py`'s `MemoryRetrievalConfig` (env-only,
`from_env()`): `max_results` (default 5, env `MAX_MEMORY_RESULTS`),
`max_tokens` (default 400, env `MAX_MEMORY_TOKENS`). `retriever.py`'s
`MemoryRetriever._apply_limits()`/`_estimate_tokens()` already implement
the exact "rank by score, cap by count then rough token estimate
(`len(text)//4`)" pattern this sprint needs. `query.py`'s
`analyze_query()`/`token_overlap()`/`_WORD_RE` are the ONE shared
tokenizer/relevance gate already used by `make_manual_memory_source()`,
`_classify_conflict()`, `_find_conflicting_memory()`,
`update_memory_by_topic()`, `resolve_conflict_by_topic()`. None of this
needs to be duplicated.

### Verified Facts / Episodic Memory boundary

Re-confirmed via grep (`luno/memory_guard.py` mentions `luno.memory` only
in comments explaining the deliberate isolation; zero code coupling).
`luno/episodic_memory.py` was already confirmed self-contained in the
prior sprint's audit (imports only `luno/config.py` +
`luno/memory_retrieval/models.py`+`/query.py`, never `luno.memory`). This
sprint's planned change touches ONLY `_memories`-derived selection inside
`luno/memory.py` - no code path in the plan below reads or writes
`VerifiedFactStore` or `EpisodicMemoryStore`, so this boundary requires
no code change, only confirmation (documented here, per requirement 11).

## Planned Implementation

1. **`build_memory_prompt(query_text=None)`** - add an OPTIONAL kwarg.
   - `query_text` falsy (omitted, `None`, or `""`): **behavior stays
     byte-for-byte identical to today** - the existing unconditional
     full-dump code, unchanged. This is what keeps `luno/main.py`'s call
     site, all 4 `test_memory_regression.py` call sites, and the
     protected `test_recall_everything_full_list_still_works_unchanged`
     test passing with zero modification. It also preserves a legitimate
     "list everything" behavior for any future explicit recall-everything
     caller.
   - `query_text` provided (non-empty): delegates to a new
     `_select_memories_for_prompt(query_text)` helper - importance/
     lifecycle/relevance/conflict-aware selection, bounded by
     `MemoryRetrievalConfig`.
2. **`_select_memories_for_prompt(query_text)`** (new, private) -
   selection policy (full detail in the "Prompt-Selection Policy"
   section below): relevance gate first (reusing `analyze_query`/
   `token_overlap`), archived exclusion (reusing `compute_lifecycle`),
   ambiguous-conflict groups surfaced together as one hedged note (never
   silently resolved), historical-query-aware `history[]` surfacing
   (reusing `_is_historical_query`, already existing), scored using the
   SAME weight formula `make_manual_memory_source()` already uses
   (importance*0.05, +0.05 explicit source, -0.15 stale), bounded by the
   SAME `MemoryRetrievalConfig.max_results`/`max_tokens` the retrieval
   pipeline already reads from `.env`. Purely read-only - never calls
   `_save()`, never mutates any entry.
3. **`main_runtime_demo.py`** - one-line change at the existing call
   site: `memory.build_memory_prompt()` -> `memory.build_memory_prompt(query_text=text)`.
   `text` is already in scope. No other line in that method changes.
4. **No changes** to `luno/main.py`, `luno/memory_guard.py`,
   `luno/episodic_memory.py`, `luno/memory_retrieval/*`, `add_memory()`,
   `update_memory()`, `_classify_conflict()`, or any existing conflict/
   intelligence function - this sprint is additive metadata-consumption
   only.

## Prompt-Selection Policy (planned)

- A query with no retrieval signal at all (`analyze_query().has_any_signal
  == False`, e.g. "what's 5 + 5?") selects nothing - mirrors
  `MemoryRetriever.retrieve_memories()`'s own "don't even query the
  store" rule.
- Relevance is mandatory once a query is available: importance can never
  rescue an irrelevant memory into the selection (Section 7's hard
  guarantee) - the token-overlap gate runs before any scoring.
- `lifecycle() == "archived"` entries are excluded from ordinary
  selection (not deleted, still reachable via `search_memories()`/
  `list_memories()`/`get_memory()` directly) - same precedent
  `make_manual_memory_source()` already established.
- `conflict_status == "ambiguous_conflict"` entries are grouped by
  `conflict_group`; if ANY member is relevant, the WHOLE group surfaces
  together as one explicitly-hedged "conflicting, unresolved information"
  note (both original texts included verbatim) - never picked apart into
  one side, never silently resolved.
- `history[]` is only consulted when the query itself is historical-
  shaped (`_is_historical_query()`, unchanged, reused) - an ordinary
  current-state query never sees history at all.
- Remaining candidates are ranked by the same score shape
  `make_manual_memory_source()` uses, then bounded by
  `MemoryRetrievalConfig.from_env()`'s `max_results`/`max_tokens` (the
  SAME env-configurable budget already governing the other memory-note
  path - no new env var).

## Risks Identified Before Implementation

1. **Test conflict risk**: `test_recall_everything_full_list_still_works_unchanged`
   calls `build_memory_prompt()` bare and expects the full list - resolved
   by making the smart path strictly opt-in via the new kwarg, never the
   default.
2. **Redundancy with `memory_block`**: once `build_memory_prompt(query_text=text)`
   also becomes relevance-gated, its output will often overlap in
   CONTENT (though not exact wording/format) with the existing
   `memory_block` note. This is expected and accepted - the brief
   explicitly asks for the direct prompt path to "obey the same
   intelligence rules already established elsewhere," not to be merged
   away; the two notes differ in phrasing/grouping (one sentence vs. one
   block per memory) and in which source module owns them, and merging
   them into a single path is explicitly out of scope ("The goal is NOT
   to replace MemoryRetriever").
3. **Malformed `conflict_group` values**: a prior sprint's own safety
   test (`test_malformed_conflict_metadata_fails_safely`) writes a
   `conflict_group` that is a `dict`, not a string - unhashable, would
   raise if used directly as a dict key. Mitigation: coerce any
   non-`str`/`int` group key via `str(...)` before using it as a grouping
   key.
4. **Budget starvation for legitimate core memories**: an importance=4
   memory that's relevant but loses a tight-budget tie-break to several
   smaller irrelevant... no - irrelevant ones are excluded before
   scoring, so this can only happen against OTHER relevant, also-
   important memories, which is the intended, correct trade-off (bounded
   prompt, not infinite core-memory accumulation).

## Compatibility

Old (schema v1) entries: `_get_importance()`/`compute_lifecycle()`
already recompute safely from `text`/`category` when `importance`/
`updated_at` are absent - no change needed, already proven by the Memory
Intelligence sprint's own tests. Malformed entries (missing `text`,
wrong types): filtered by the same `isinstance(m, dict) and m.get("text")`
guard every other reader in this file already uses.

## Post-implementation update

None required - implementation matched this plan exactly; see the final
sprint report and `ARCHITECTURE_GUARD.md`'s "Memory Prompt Intelligence"
subsection for the as-built description.
