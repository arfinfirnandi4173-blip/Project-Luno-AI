# Change Impact: Memory Conflict Resolution & Trusted Facts Guard

## Baseline

Before implementation: `luno/` 806 passed / 808 total (2 known-flaky
Barge-in). Named memory/relationship/emotion/personality/runtime batch
(`test_episodic_memory.py` + `test_manual_memory.py` +
`test_memory_guard.py` + `test_memory_intelligence.py` +
`test_memory_regression.py` + `test_memory_retrieval.py` +
`test_state_isolation.py` + `test_relationship_engine.py` +
`test_emotion_engine.py` + `test_persona.py` + `test_runtime_demo.py`):
437 passed. `test_production_launcher.py`: 23 passed / 24 total (1 known
environment-specific). 10 persistent-state files hashed; identical to
the values recorded at the end of the Memory Intelligence & Importance
Engine sprint.

## Architecture Audit

Confirmed (re-reading the full current `luno/memory.py`,
`luno/memory_guard.py`, `luno/memory_retrieval/query.py`, and every
caller):

- `luno/memory.py`'s `_memories` store remains the ONE production
  long-term memory system. `add_memory()` already has a two-phase
  dedup/consolidation pipeline from the prior sprint: (1) exact/near-
  exact substring dedup (reinforces + returns `None`), (2) Jaccard-
  overlap `[0.45, 0.85)` same-topic detection via
  `_find_conflicting_memory()` -> single match updates-with-history,
  tied matches return `"ambiguous"` and fall through to CREATE. This
  sprint's job is to insert a CONFLICT CLASSIFICATION step between (2)
  finding a single candidate and blindly merging into it - today it
  merges unconditionally; it must instead ask "does this genuinely
  conflict, and if so, how?"
- `luno/memory_guard.py`'s `VerifiedFactStore` is structurally
  independent: keyed by `entity_id`, written ONLY from a verified
  `ToolResult` (`should_store_verified_result()` gates on
  `success is True`), never reads or writes `luno.memory._memories` at
  all, and vice versa. No code path in `luno/memory.py` touches
  `VerifiedFactStore` and none needs to for this sprint - confirmed by
  grep, zero cross-references exist today. This sprint does not change
  that; it is audited and reconfirmed, not modified.
- `luno/memory_retrieval/query.py`'s `_WORD_RE`/`token_overlap()` is the
  ONE shared tokenizer every memory-adjacent module already reuses
  (`_find_conflicting_memory`, `update_memory_by_topic`,
  `search_memories`, `make_manual_memory_source`) - this sprint reuses
  it again, no second tokenizer.
- Callers of the functions this sprint touches: `main_runtime_demo.py`
  (`PlannerBridgeModule._handle_explicit_memory_command()`,
  `make_manual_memory_source` registration), `luno/main.py` (legacy
  `save_memory` tool + `detect_remember_command` path, both call
  `add_memory()` - unaffected in signature, receives the same
  additive-only behavior), `luno/bootstrap/modules.py` (reads
  `list_memories()` for proactive triggers - read-only, unaffected).

## Implementation

### Conflict Model (minimal fields only)

- `history` entries (already existed) gain an OPTIONAL `"reason"` key -
  one of `"refinement"`, `"correction"`, `"temporal_change"` - set only
  when a history entry was produced by the new conflict-aware merge
  path. Existing `update_memory()` callers (explicit "ubah memory X jadi
  Y") do not set it (absent key, backward compatible with every existing
  reader that already does `.get(...)`).
- Two NEW top-level entry fields, used ONLY for the ambiguous case:
  `conflict_status` (absent normally; `"ambiguous_conflict"` when two
  entries are known to contradict but there is not enough deterministic
  evidence to pick a winner) and `conflict_group` (a short shared id
  linking the two/more contradicting entries). No `supersedes`/
  `superseded_by`/`confidence`/`resolution_reason` top-level fields were
  added - the refinement/correction/temporal_change path never leaves
  two live contradicting entries (the losing text is folded into the
  survivor's `history`), so there is no second entry that would ever
  need a `supersedes` pointer, and `resolution_reason` is exactly what
  `history[].reason` already captures per change.

### Detection (`_classify_conflict`)

New deterministic function in `luno/memory.py`, called ONLY after
`_find_conflicting_memory()`'s existing tie/floor logic has already
narrowed things down to exactly one same-category, in-Jaccard-band
candidate (the pre-existing tie-check for MULTIPLE equally-good
candidates is untouched - that is a different kind of ambiguity,
"which candidate", not "what kind of conflict"). Waterfall, in order:

1. **Distinguishing-context check** - a small fixed list of
   qualifier/location words (`pc`, `laptop`, `server`, `vps`, `kantor`,
   `rumah`, `utama`, `cadangan`, `backup`, `primary`, `secondary`,
   `home`, `office`, `main`, `desktop`). If both texts contain at least
   one qualifier from this list and the SETS found in each text are
   disjoint (no overlap), the two facts are about different
   subjects/contexts -> `NO_CONFLICT` -> the candidate is dropped
   entirely (not merged, not flagged, just two independent facts, same
   as if Jaccard had been below the floor). This is what correctly
   keeps "RTX 3060 Ti untuk PC" and "RTX 3070 Ti untuk server" separate
   despite sharing enough vocabulary to land in the consolidation band.
2. **Subset check** - if the OLD entry's token set is a subset of the
   NEW text's token set (or vice versa), the new statement adds detail
   without contradicting anything -> `REFINEMENT`. Handles "Aku pakai
   Windows." -> "Aku pakai Windows 11 Pro." without needing to invoke
   correction language at all.
3. **Correction/temporal wording** - a small, deliberately short regex
   list (Indonesian + English) for explicit correction phrasing
   ("koreksi memory", "ubah", "bukan ... tapi", "ganti ... menjadi",
   "sekarang ...", "actually", "correction", "no longer", "i use ...
   now") vs. a distinct dual-marker temporal pattern ("dulu ... sekarang
   ...", "used to ... now") -> `CORRECTION` or `TEMPORAL_CHANGE`
   respectively. Both mechanically resolve the same way (new text
   becomes current, old text moves into `history` with a reason tag) -
   the label only affects the recorded `reason` and log wording, not the
   persistence shape, matching how similarly the sprint brief's own
   Level 2/Level 3 examples behave.
4. **Otherwise** -> `AMBIGUOUS_CONFLICT`. No subset relation, no
   correction/temporal signal, and not a distinguishing-context split -
   genuinely insufficient evidence. Per the sprint's own core principle,
   this NEVER merges and NEVER guesses: both entries are kept, tagged
   with `conflict_status`/`conflict_group`.

### Resolution Policy

- `NO_CONFLICT` -> candidate dropped, `add_memory()` proceeds to create
  a normal new entry (Level 0, unchanged from "two unrelated facts").
- `REFINEMENT` / `CORRECTION` / `TEMPORAL_CHANGE` -> exactly the
  existing update-with-history path (`update_memory()`), now passing a
  `reason` through to the history entry it writes.
- `AMBIGUOUS_CONFLICT` -> a NEW path: `add_memory()` creates the new
  entry as usual (never blocked), then tags BOTH the new entry and the
  existing candidate with `conflict_status="ambiguous_conflict"` and a
  freshly generated shared `conflict_group` id (or the candidate's
  already-existing group, if it was already part of one), and persists
  both via the existing `_save()`. Nothing is deleted, nothing is
  guessed.

### Explicit User Intent (Step 5)

`_classify_conflict` never looks at `source` to decide whether a
correction signal counts - explicit correction wording in a
`user_explicit`-sourced turn is used exactly as before. `source` still
matters for `compute_lifecycle()`'s decay rate (unchanged from the prior
sprint) and is preserved verbatim on every entry - conflict resolution
does not alter it.

### Importance vs. Truth / Recency vs. Truth (Steps 6/7)

`_classify_conflict` and `_find_conflicting_memory` never read
`importance` or compare timestamps to decide which side "wins" - the
decision is entirely from text-level signals (subset relation,
correction/temporal wording, distinguishing context). `importance`
continues to affect only retrieval ranking and lifecycle decay, exactly
as the prior sprint left it - never conflict resolution.

### Temporal Reasoning & Retrieval (Steps 9/13)

Because a resolved conflict (refinement/correction/temporal_change)
always collapses to ONE live entry (old text -> `history`, never a
second top-level row), a "what's my current X" query structurally can
never retrieve the superseded value through the normal
`make_manual_memory_source()` path - there is nothing else to compete
with. A NEW, small, deterministic historical-intent detector
(`_HISTORICAL_QUERY_RE` - "dulu", "pernah", "sebelumnya", "yang lama",
"used to", "previously", "before") is added: when the CURRENT query
text matches it, `search_memories()` and `make_manual_memory_source()`
ALSO scan each entry's `history` list for token overlap and surface
matching historical entries (labeled distinctly, e.g. "previously:
...", with the `changed_at` timestamp) alongside/instead of the
current-text match. This reuses the same tokenizer, is a few lines, and
does not add a second retrieval engine.

## Compatibility

Purely additive: `conflict_status`/`conflict_group` are new OPTIONAL
top-level keys (absent on every pre-existing entry, absent on every
newly-created entry unless an ambiguous conflict actually occurs);
`history[].reason` is a new OPTIONAL key on history sub-objects. No
existing reader is broken by their absence (every existing accessor in
this codebase already uses `.get(...)` with safe defaults for optional
memory fields, established convention from the last two sprints).
`MANUAL_MEMORY_SCHEMA_VERSION` is NOT bumped again - nothing about the
on-disk SHAPE that matters to any reader changed (the new keys are
exactly the same "entries missing this key are simply older/unaffected"
pattern the current version 2 already tolerates for `importance`/
`history`).

## Persistence Behavior

Unchanged write strategy (`_save()`, plain `json.dump`). No new file, no
new `config.*_FILE` constant, no new `tests/conftest.py` isolation
needed (existing `LONG_TERM_MEMORY_FILE` redirect + `_memories` reset
already cover every entry this sprint adds fields to).

## Retrieval Behavior

`make_manual_memory_source()`'s existing relevance gate
(`token_overlap`) and importance/lifecycle-aware scoring (prior sprint)
are UNCHANGED for the current-text path. The new historical-query branch
is a strictly ADDITIONAL code path, only reached when the query itself
carries historical-intent wording - an ordinary "what's my GPU" question
never triggers it and behaves exactly as before.

## Prompt Behavior

No internal field ever reaches the LLM-facing text. The `[MANUAL MEMORY
- category]` label format is unchanged for current-state results; a
historical result gets its own distinct, still-plain-English label
(e.g. "The user previously said (superseded): ..."), never
`conflict_group=...`/`confidence=...`/raw JSON. `build_memory_prompt()`
(the pre-existing always-on full dump) is untouched - still prints
CURRENT `text` only, never `history`, matching its own prior-documented
"out of scope" status.

## Potential Regression Points

1. `_find_conflicting_memory`'s existing MULTI-CANDIDATE tie check
   (`test_consolidation_ambiguous_case_is_not_guessed`) must keep firing
   BEFORE the new single-candidate conflict classifier ever runs -
   verified by keeping that check as the first gate, entirely unchanged.
2. The two existing single-candidate consolidation tests
   (`test_consolidation_reworded_same_fact_merges_not_duplicates`,
   `test_consolidation_value_conflict_updates_not_duplicates`) must
   still merge under the new classifier - both texts share the same
   qualifier ("PC utamaku"/"PC utamanya" in both sides, so no
   distinguishing-context split) and either land in the subset check
   (refinement) or contain "sekarang" (correction) - traced by hand
   against the new waterfall before writing any code, confirmed both
   still resolve to a merge.
3. `test_update_memory_by_topic_ambiguous_does_not_destroy_state`'s two
   GPU fixtures never reach the classifier at all (their Jaccard,
   ~0.428, sits below the existing 0.45 floor) - completely unaffected
   by this sprint regardless of the new logic.
4. Any new AMBIGUOUS_CONFLICT path must not affect entries that
   previously merged - it only fires for candidates that would
   previously have merged with NO qualifying signal (subset/correction/
   temporal) at all; every existing test's candidate pairs were checked
   by hand and all have a qualifying signal, so none flip into the new
   ambiguous path.

## Test Coverage

`tests/test_memory_conflict.py` (new) per Step 18's own categories: no
conflict (coexistence, distinguishing-context split), refinement,
correction, temporal, ambiguity (no auto-delete, no arbitrary winner),
importance-is-not-truth, provenance, persistence. One new end-to-end
scenario in `tests/test_runtime_demo.py` proving the full production
path (old config stated -> explicit correction -> conflict detected ->
old preserved in history -> new becomes current -> current query
retrieves new -> historical query retrieves old) through the real
`PlannerBridgeModule`, with persona/emotion/relationship/verified
facts/episodic memory/retrieval all confirmed still intact alongside it.

## Rollback Strategy

Every change is additive and independently revertible. Reverting
`_classify_conflict`/`_find_conflicting_memory`'s new waterfall to the
prior sprint's "any single in-band candidate merges unconditionally"
behavior, and reverting the historical-query branches in
`search_memories()`/`make_manual_memory_source()`, fully removes this
sprint's behavior with no data migration needed - `conflict_status`/
`conflict_group`/`history[].reason` being present on some on-disk
entries after a rollback is harmless (nothing reads them once the new
code is gone, exactly like the existing tolerance for any other unknown
field).

## Known Limitations

- The distinguishing-context qualifier list is small and fixed
  (Indonesian + English "PC/laptop/server/..." style words) - a
  different-context split expressed with a qualifier word NOT on this
  list will not be detected, and the pair will fall through to the
  subset/correction/ambiguous checks instead (worst case: correctly
  lands in `AMBIGUOUS_CONFLICT`, never silently merged, so the failure
  mode is "asks for more evidence" rather than "wrongly overwrites").
- Conflict classification is per SINGLE candidate, evaluated once at
  save time - it does not retroactively re-evaluate older entries
  against each other, and does not build a full pairwise conflict graph
  across the whole store (deliberately, to stay deterministic, fast,
  and local per Step 19-equivalent "do not overengineer" discipline
  carried over from the prior sprint).
