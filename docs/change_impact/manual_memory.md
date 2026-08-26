# Change Impact: Manual Memory Management

## Feature

Give the user an explicit, user-controlled memory layer: "Luno, ingat
aku suka Avenged Sevenfold" (save), "apa saja yang kamu ingat tentang
aku?" (recall everything - already existed), "cari memory tentang
PC-ku" (relevance-scored search), "ubah memory GPU-ku jadi RTX 5070"
(update), "lupakan kalau aku suka X" / "hapus memory nomor 12" (delete).

## Architecture Audit

Read first (per this sprint's own Step 1): `ARCHITECTURE_GUARD.md`,
`docs/testing/regression_baseline.md`, `tests/conftest.py`,
`docs/change_impact/test_state_isolation.md`,
`docs/change_impact/verified_facts_vision_isolation.md`. Then the actual
implementations of `luno/memory.py`, `luno/memory_guard.py`,
`luno/memory_retrieval/`, `luno/episodic_memory.py`, `luno/config.py`,
`main_runtime_demo.py`, `luno/bootstrap/`, and every existing
memory-related test.

**Long-Term Memory (`luno/memory.py`)** - already, in spirit, EXACTLY
"Manual Memory": the module's own docstring defines it as "fakta/
preferensi yang secara EKSPLISIT diminta user untuk diingat" - the
sprint's own definition of Manual Memory, word for word. Stores a flat
list of `{"id", "text", "created_at"}` dicts in `config/long_term_memory.json`
(`config.LONG_TERM_MEMORY_FILE`). Written by `add_memory()`/`remove_memory()`/
`clear_all_long_term()`, read by `list_memories()`/`build_memory_prompt()`.
Two real callers of `add_memory()` existed before this sprint:
`main_runtime_demo.py`'s `_handle_explicit_memory_command()` (triggered
by `detect_remember_command()` - a literal "inget ya, ..."/"catat...",
regex-extracted, never LLM-invented) and `luno/main.py`'s LEGACY
`save_memory` LLM tool (the model decides on its own, silently, per
that tool's own docstring - a DIFFERENT provenance philosophy sharing
the same store). `detect_forget_fact_command()`/`is_recall_command()`
already existed too. Dedup was already substring-based (bidirectional
containment, case-insensitive) - not timestamp-based, already handles
the sprint's own literal repeat-3x example correctly.

**Verified Facts (`luno/memory_guard.py`)** - `VerifiedFactStore`, one
fact per `entity_id`, written ONLY from a verified `ToolResult`
(`success=True`), never from LLM text. Isolated in tests via
`config.VERIFIED_FACTS_FILE` (Verified Facts & Vision Memory Test
Isolation sprint). Manual Memory never writes to this store and is
never read as if it were one - see "Interaction with Verified Facts"
below.

**Episodic Memory (`luno/episodic_memory.py`)** - a separate, deterministic,
regex-detected "what meaningful thing happened" log (technical problems
solved, devices configured, milestones), content-fingerprint deduped,
registered as its own `MemoryRetriever` source
(`make_episodic_experience_source`). Explicitly NOT reused for Manual
Memory: an episode is "what happened", a manual memory is "a fact/
preference the user told Luno to remember" - different questions,
already kept deliberately separate by that sprint's own design, and
this sprint does not change that separation.

**Session Summaries** - LLM-generated 1-3 sentence recap of an entire
session, unconditionally triggered at session end, no relevance
filtering, injected every turn via `build_session_summary_prompt()`
(separate function, separate file section). Not touched by this sprint.

**Memory Retrieval (`luno/memory_retrieval/`)** - `MemoryRetriever` owns
a registry of named `MemorySource` callables
(`(QueryAnalysis, MemoryRetrievalConfig) -> List[RelevantMemory]`).
`retrieve_memories(text)` runs every enabled source, applies a recency/
staleness bonus, deduplicates by `(source, raw.id)`, ranks by score,
bounds by `max_results`/`max_tokens`. Sources already registered in
`main_runtime_demo.py`: `vision_objects`, `vision_human`, `vision_events`,
`long_term_memory` (Vision Memory's OWN internal habit/pattern store,
`vm.get_long_term_memory` - a DIFFERENT system from `luno.memory`'s
`_memories`, despite the similar name), `planner_state`,
`episodic_memory`. Adding a new source never requires touching
`retriever.py`/`sources.py` - any zero-arg provider callable works.

## What Was Reused

- `luno/memory.py`'s existing `_memories` store/file/dedup/detectors -
  extended, not replaced or duplicated.
- `luno.memory_retrieval.MemoryRetriever`/`RelevantMemory` - registered
  one more source (`"manual_memory"`), no second retrieval engine.
- `luno.memory_retrieval.query.token_overlap`/`_WORD_RE` - the SAME
  tokenizer every other source already uses, for both the new
  `search_memories()` function and the new `MemorySource`.
- `tests/conftest.py`'s existing `isolate_persistent_state` autouse
  fixture - `LONG_TERM_MEMORY_FILE` was ALREADY in `_WRITABLE_STATE_ATTRS`
  from the prior sprint; no new file constant needed.
- `main_runtime_demo.py`'s existing `_handle_explicit_memory_command()`
  meta-command interception point (checked before planning, same as
  remember/forget/clear-everything already were) - new update/delete
  detectors slot into the SAME method, SAME truthful-confirmation-note
  convention.
- `episodic_memory.py`'s exact `MemorySource` factory shape
  (`make_episodic_experience_source`) as the direct template for
  `make_manual_memory_source`.

## Why No New Component Was Necessary

The sprint's own Step 5 instruction: "If existing long-term memory can
safely represent explicit manual memories, prefer extending it." The
audit found it already does - same semantics (explicit user fact),
same persistence pattern, same detection philosophy (regex-extracted,
never LLM-invented). The only genuine gaps were: no explicit UPDATE
operation, no delete-by-id, no `MemoryRetriever` integration (recall was
an unconditional full-dump only), and no `category`/`source`/`updated_at`
provenance fields. All four are additive - none required changing the
store's existing shape, file, or any existing function's behavior for
existing callers.

## Manual Memory Model

Each entry in `_memories` (JSON list, `config/long_term_memory.json`):

```
{
  "id": str,              # existing, uuid4 hex[:8]
  "text": str,             # existing, exact user-supplied text (regex-extracted)
  "created_at": iso-str,   # existing
  "updated_at": iso-str,   # NEW - stamped by add_memory()/update_memory()
  "category": str,         # NEW - one of MANUAL_MEMORY_CATEGORIES, deterministic keyword classification
  "source": str,           # NEW - "user_explicit" (main_runtime_demo.py path) or "llm_auto" (luno/main.py legacy save_memory tool)
  "schema_version": int,   # NEW - MANUAL_MEMORY_SCHEMA_VERSION = 1
}
```

Old entries (pre-sprint, or hand-written test fixtures) simply lack the
four new keys - every reader (`list_memories()`, `build_memory_prompt()`,
`search_memories()`, `make_manual_memory_source()`) tolerates their
absence via `.get(...)` with a safe default or a plain existence check,
never assumes they're present. `MANUAL_MEMORY_CATEGORIES = (preference,
personal_fact, technical_fact, instruction, project_context, other)` -
each has a concrete consumer (shown in `list_memories()`/dashboard
output, included in the `[MANUAL MEMORY - <category>]` retrieval label).
No `confidence` field was added - nothing in the existing architecture
reads one for this store (Vision Memory's OWN `LongTermMemoryRecord.confidence`
is a different system), and an explicit user instruction is not
appropriately represented as an inferred confidence score per the
sprint's own instruction.

## Persistence Choice

Extended `config/long_term_memory.json` via the EXISTING
`config.LONG_TERM_MEMORY_FILE` constant. No `config/manual_memory.json`
was created - the architecture audit found the existing store already
safely represents the required distinction (explicit vs. ordinary), so
Step 5's "only create a dedicated store if the audit proves it's
genuinely necessary" was not met. Confirmed via the final regression run:
no new file appears anywhere under `config/`.

## Retrieval Integration

`make_manual_memory_source(get_memories)` (in `luno/memory.py`, same
factory shape as `episodic_memory.make_episodic_experience_source`):
zero-arg provider (`memory.list_memories`) in, `MemorySource` closure
out. No signal or empty store -> `[]` immediately, no store access
(same discipline every other source follows). Registered as
`"manual_memory"` (NOT `"long_term_memory"` - already taken by Vision
Memory's own internal habit source) in `main_runtime_demo.py`'s
`PlannerBridgeModule.__init__`, alongside every other source. Matched
text uses `token_overlap` (same tokenizer as every other source),
rendered as `[MANUAL MEMORY - <category>] The user explicitly asked you
to remember: <text>.` - the sprint's own suggested `[MANUAL MEMORY]`
provenance label. Bounding/ranking/staleness wording all come free from
the existing `MemoryRetriever` - nothing here reimplements any of it.
Base score 0.6 (vs. 0.5 for `long_term_memory`/`episodic_memory`) - a
small ranking nudge reflecting Step 14's "manual memory may warrant
stronger relevance than automatically-inferred memories", but this
ranking pool is ENTIRELY separate from verified facts / current tool
state (see below) - it never competes with them.

## Intent Detection

New detectors in `luno/memory.py`, same anchored-regex-list style as the
existing `_REMEMBER_PATTERNS`/`_FORGET_FACT_PATTERNS`:

- `detect_update_memory_command(text)` -> `(topic_query, new_text)` or
  `None`. Requires the literal word "memory" after the verb
  (ubah/update/ganti/koreksi) - deliberately more conservative than a
  bare "inget ya..." save, since update destructively replaces content.
- `detect_delete_memory_by_id_command(text)` -> id string or `None`
  ("hapus/delete memory nomor/number/# N").
- `detect_delete_memory_by_topic_command(text)` -> topic string or
  `None` ("hapus/delete memory tentang/soal/about X") - a SEPARATE
  trigger verb from the existing `detect_forget_fact_command`
  ("lupa.../forget..."), both remain independently usable.

Ordinary statements ("GPU-ku sekarang RTX 5070", "PC utamaku pakai RTX
3060 Ti") do NOT match any of these - no trigger verb, no action. "cari
memory tentang X" requires no new meta-command detector at all: it
flows through NORMAL planning, reaches `self.memory_retriever.retrieve_memories(text)`
(already called every turn), and the new source answers it via ordinary
relevance-based retrieval - exactly the same as how "where is my cup?"
already works for vision sources, with zero new interception code.

## Deletion Semantics

`delete_memory_by_id(memory_id)` - removes exactly one entry by its
stable `id`, never affects any other entry. `remove_memory(query_lower)`
(existing, unchanged) is reused for topic-based delete
(`detect_delete_memory_by_topic_command`'s handler in
`main_runtime_demo.py` calls it directly) rather than adding a second
deletion algorithm. No global "clear all manual memory" command was
added by this sprint - `is_clear_everything_command`/`clear_all_long_term()`
already existed pre-sprint and are unchanged (audited, not replaced).
Deletion never touches `config/episodic_memory.json`,
`config/relationship_state.json`, `config/session_summaries.json`, or
any other store - `delete_memory_by_id`/`remove_memory` only ever
mutate `_memories`.

## Interaction with Verified Facts

Manual memory and verified facts are injected through COMPLETELY
SEPARATE prompt paths with no shared ranking pool: verified facts via
`self.memory_guard`/`self.world_model` (`build_verified_action_notes()`,
a per-turn note built directly from `task.result`), manual memory via
`self.memory_retriever`'s `"manual_memory"` source (a `memory_block`
entry). A stale manual memory (e.g. "GPU = RTX 3060 Ti") can never
mathematically outrank a fresh verified reading (e.g. "GPU = RTX 5070")
because they are never compared against each other at all - the
existing verified-facts precedence model is structurally unchanged.

## Interaction with Episodic Memory / Relationship / Emotion / Personality

Not modified. `episodic_memory.py`, `relationship_engine.py`,
`emotion_engine.py`, `persona.py` were read during the audit and are
untouched by this sprint's diff - confirmed via the final file audit
(see the sprint's final report).

## Files Changed

- `luno/memory.py` - additive: new constants, `_classify_memory_category`,
  `get_memory`, `update_memory`, `update_memory_by_topic`,
  `delete_memory_by_id`, `search_memories`, three new detectors,
  `make_manual_memory_source`; `add_memory()` gained an optional
  `source` kwarg (default `"user_explicit"`, preserves prior behavior in
  effect for its primary caller) and now also stamps
  `updated_at`/`category`/`schema_version`.
- `luno/main.py` - one line: legacy `save_memory` tool call site now
  passes `source="llm_auto"` explicitly, so the new field stays honest
  for that caller too. No behavior change (same fact saved, same dedup,
  same tool-call return value).
- `main_runtime_demo.py` - registered the new `"manual_memory"`
  `MemoryRetriever` source in `__init__`; extended
  `_handle_explicit_memory_command()` with update/delete-by-id/
  delete-by-topic handling, same method, same truthful-confirmation
  convention, checked before the pre-existing forget/remember checks.
- `tests/conftest.py` - added `luno.memory._memories` reset (via
  `monkeypatch.setattr`) to the existing autouse fixture - closes a
  latent test-determinism gap (`_memories` is populated once at process
  import time, before any fixture runs; `LONG_TERM_MEMORY_FILE` was
  already redirected, but the in-memory cache was not being reset).

## Files Created

- `tests/test_manual_memory.py` (61 tests)
- `docs/change_impact/manual_memory.md` (this file)

## Files Deliberately NOT Touched

`luno/episodic_memory.py`, `luno/relationship_engine.py`,
`luno/emotion_engine.py`, `luno/persona.py`, `luno/memory_guard.py`,
`luno/memory_retrieval/retriever.py`, `luno/memory_retrieval/sources.py`,
`luno/memory_retrieval/models.py`, `luno/memory_retrieval/query.py`,
`luno/config.py` (no new `*_FILE` constant needed), `luno/bootstrap/`,
every real `config/*` persistent-state file.

## Regression Risk

Low. `add_memory()`'s signature change is backward-compatible (new
optional kwarg with a default); the four new dict keys are additive and
every reader already tolerates their absence (confirmed via
`tests/test_memory_regression.py` still passing unmodified - it asserts
exact dict equality only against hand-written fixture files, never
against `add_memory()`'s own output shape). `luno/main.py`'s one-line
change does not alter behavior, only provenance metadata. The
`tests/conftest.py` `_memories` reset only affects test-time state
(monkeypatch-reverted at teardown) and was verified via the full
regression matrix (`luno/`: 806/808 unchanged; every named memory/
relationship/emotion/persona/runtime/production-launcher/dashboard suite
unchanged) to introduce no new failures.

Known pre-existing limitation, NOT introduced or fixed by this sprint:
`luno.memory_retrieval.query`'s shared tokenizer (`_WORD_RE = [a-zA-Z']+`)
does not capture digits, so a query consisting only of a number (e.g.
searching for "3060" alone) won't token-match a stored "RTX 3060 Ti" -
matching on "RTX"/"GPU"/other surrounding words still works. Not fixed
here since it is shared infrastructure every other source also depends
on and is out of this sprint's scope.

## State-Isolation Strategy

No new persistent file, so no new `config.*_FILE` constant and no new
redirect target were needed - `LONG_TERM_MEMORY_FILE` was already
covered. The one genuine gap found (`_memories`'s import-time-populated,
process-wide in-memory cache never being reset between tests) was
closed with a single additive `monkeypatch.setattr` line in the
EXISTING `isolate_persistent_state` fixture - no second isolation
mechanism. Verified via `tests/test_manual_memory.py`'s own real-file-
protection test (sha256/mtime before/after) and the sprint's full
regression-matrix real-file audit (10 files, all byte-identical
before/after).

## Rollback Considerations

Every change here is additive and independently revertable:
`luno/memory.py`'s new functions/fields can be deleted without touching
`add_memory`/`remove_memory`/`list_memories`/`build_memory_prompt`/
`detect_remember_command`/`detect_forget_fact_command`/`is_recall_command`,
which remain byte-for-byte their pre-sprint selves in behavior. Removing
the `main_runtime_demo.py` source registration and the three new
`_handle_explicit_memory_command` branches fully reverts the feature's
LLM-facing surface with no other code path affected. No data migration
is needed to roll back: old-shape entries (missing the four new keys)
were always tolerated, so a rollback simply stops adding them going
forward - no existing `config/long_term_memory.json` needs to change.
