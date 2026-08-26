# Change Impact: Memory Intelligence & Importance Engine

## Files Likely Affected

- `luno/memory.py` - the production path (see below). Extends the entry
  schema (`importance`, `history`, no persisted `lifecycle`), the save
  flow (`add_memory`) with consolidation/conflict logic, `update_memory`
  with history-recording, and the `"manual_memory"` `MemorySource`
  (`make_manual_memory_source`) with importance-aware ranking.
- `main_runtime_demo.py` - two new optional explicit commands ("memory
  ini penting" / "lupakan memory ini") wired into the existing
  `_handle_explicit_memory_command()` meta-command handler.
- `tests/test_memory_intelligence.py` (new)
- `tests/test_runtime_demo.py` (one new end-to-end scenario)
- `ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`,
  `docs/change_impact/memory_intelligence.md` (this file)

Not affected: `luno/episodic_memory.py`, `luno/relationship_engine.py`,
`luno/emotion_engine.py`, `luno/persona.py`, `luno/memory_guard.py`,
`luno/memory_retrieval/retriever.py`/`sources.py`/`models.py`/`query.py`,
`luno/config.py` (no new persistent file, no new `*_FILE` constant),
`tests/conftest.py` (no new store to isolate - `LONG_TERM_MEMORY_FILE`
and the `_memories` reset are already covered by the Manual Memory
Management sprint's fixture).

## Production Path

Confirmed via the same audit method used in the prior two sprints:
`main_runtime_demo.py`'s `PlannerBridgeModule` is the real, live entry
point (`main.py` -> `luno/bootstrap/modules.py::register_all_modules()`
-> `PlannerBridgeModule`). `luno/main.py` is a LEGACY, separate
entrypoint (not `main.py` - confusingly similar name) that still exists
and still calls `memory.add_memory()` via its own `save_memory` LLM
tool - not the file this sprint's runtime behavior targets, but still a
real caller that must not break (see "Backward Compatibility" below).
`luno/memory.py`'s `_memories` store is the ONE production long-term
memory system - already confirmed in the Manual Memory Management
sprint's own audit, re-confirmed here: no second memory store exists
anywhere in the repository.

## Data Model Changes (additive only)

Each entry in `config/long_term_memory.json` gains two new fields:

```json
{
  "id": "...", "text": "...", "created_at": "...", "updated_at": "...",
  "category": "project_context",
  "source": "user_explicit",
  "schema_version": 2,
  "importance": 4,
  "history": [{"text": "previous wording", "changed_at": "iso-ts"}]
}
```

`importance` (int 0-4) and `history` (bounded list, newest last) are
NEW. `MANUAL_MEMORY_SCHEMA_VERSION` bumps from 1 to 2 (matches the
sprint brief's own illustrative JSON) - purely a version-number bump,
nothing in `luno/memory.py` gates or rejects entries by
`schema_version` value (unlike `episodic_memory.py`, which strictly
validates it), so this bump breaks nothing for existing readers.

`lifecycle` (active/stale/archived) is deliberately NOT persisted - it
is a PURE FUNCTION of `(importance, updated_at, source, now)`, computed
on demand via `compute_lifecycle()`. This avoids a background decay
job (explicitly forbidden by Step 19 - "no background agent"), avoids
ever writing a stale-on-disk lifecycle value, and keeps the whole
mechanism trivially testable (inject `now`).

## Backward Compatibility

Every reader of `importance`/`history` uses `.get(...)` with a computed
(not placeholder) default: `_get_importance(entry)` recomputes
importance from `text`/`category` on the fly if the key is absent (an
old schema-v1 entry gets a REAL classification, not an arbitrary flat
number); `history` defaults to `[]`. No entry is ever rejected for
missing these fields, matching the existing precedent in this file
(`build_memory_prompt()` already tolerates entries missing arbitrary
keys via `isinstance(m, dict) and "text" in m` checks). No migration
script is needed or added - old files load and behave correctly
unchanged; the new fields are populated the next time an entry is
naturally re-saved (`add_memory`/`update_memory`).

## Persistence Behavior

Unchanged mechanism: same `config.LONG_TERM_MEMORY_FILE`, same
`_save()` (plain `json.dump`, not atomic-temp-then-replace - this
sprint does NOT change the write strategy, matching Step 16's "jangan
mengganti atomic persistence dengan direct write" by leaving whatever
the existing strategy already is exactly as it is). `history` is
bounded per-entry (`_MAX_MEMORY_HISTORY_ENTRIES`) to prevent unbounded
per-entry growth, independent of the sprint's broader "don't become a
growing messy JSON dump" concern (Step 6/13), which is addressed at the
RETRIEVAL layer (below), not by deleting data.

## Retrieval Behavior

`make_manual_memory_source()`'s existing flat `score=0.6` becomes
`0.6 + importance*0.05 + explicitness_bonus - staleness_penalty`,
computed AFTER the existing `token_overlap` relevance gate (unchanged) -
importance can only influence RANKING AMONG already-relevant
candidates, never rescue an irrelevant memory into the results (an
irrelevant importance=4 memory still fails `token_overlap` and is never
even a candidate - structurally guarantees Step 12's "Guitar Rig" test
case). Lifecycle="archived" memories are excluded from this ambient
source entirely (still findable via `search_memories()`/`list_memories()`
directly - Step 7's "archived: not used in normal retrieval, but
recoverable"). No second retrieval engine, no new budget mechanism: the
EXISTING `MemoryRetriever._apply_limits()` (rank by score, cap by
`max_results`/`max_tokens`) already implements "keep the
highest-scored, drop the rest" - injecting importance into the score is
sufficient to make Step 13's budget priority ("core relevant > important
relevant > useful relevant > temporary") emerge from infrastructure that
already exists, without building a parallel budget system.

## Prompt Behavior

No new prompt section. The `[MANUAL MEMORY - <category>]` label gains
no visible importance/lifecycle text by default (avoiding prompt bloat)
- importance affects ranking/inclusion, not wording, keeping `memory_block`
exactly the same shape it already is. `build_memory_prompt()` (the
pre-existing, unconditional full-dump used for "apa yang kamu ingat
tentang aku?") is UNCHANGED - out of scope for this sprint, matches the
prior sprint's own documented decision to leave it alone.

## Potential Regression Points

1. `add_memory()`'s existing substring-based near-duplicate check runs
   FIRST, unchanged, before any new consolidation logic - the three
   existing dedup tests (`test_exact_duplicate_saved_three_times_creates_one_entry`,
   `test_normalized_duplicate_case_and_punctuation_is_skipped`,
   `test_dedup_is_restart_safe`) must keep passing unmodified.
2. `update_memory()` gaining history-recording must not break the 8
   existing update-related tests in `tests/test_manual_memory.py` - none
   of them assert an exact key-set, only specific key values, so adding
   `history` is safe.
3. `make_manual_memory_source()`'s score change must not break
   `test_relevant_manual_memory_is_retrieved`/
   `test_irrelevant_manual_memory_is_excluded`/
   `test_retrieval_result_count_is_bounded` - none assert an exact score
   value, only relative behavior (retrieved vs not, count bounds).
4. `luno/main.py`'s legacy `save_memory` tool call (`source="llm_auto"`)
   must still work exactly as before - the new consolidation path
   applies uniformly regardless of `source`, which is intentional
   (Step 10 doesn't scope consolidation to one provenance only) and
   verified via a dedicated test.

## Test Coverage

`tests/test_memory_intelligence.py` (new) per the sprint's own Step 17
categories: importance classification, lifecycle, consolidation,
conflict/history, retrieval priority + budget, backward compatibility,
persistence, safety. One new end-to-end scenario in
`tests/test_runtime_demo.py` proving the full production path (utterance
-> detection -> importance classification -> store -> retrieval ->
`PlannerBridgeModule` -> LLM context) and that importance actually
changes retrieval/ranking behavior, not just that a helper function
returns the right number in isolation.

## Rollback Strategy

Every change is additive and independently revertable. Reverting
`add_memory()`/`update_memory()`/`make_manual_memory_source()` to their
Manual Memory Management sprint shapes fully removes the new behavior;
no data migration is needed to roll back, since old-shape entries
(missing `importance`/`history`) were always the tolerated case, not a
special one - a rollback simply stops adding them going forward. The
optional "memory ini penting"/"lupakan memory ini" commands are two
isolated branches in `_handle_explicit_memory_command()`, removable
independently of everything else.
