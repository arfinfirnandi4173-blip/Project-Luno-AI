# Change Impact Analysis - Persistent State Hardening V2

## 0. Goal and scope

Bring the reliability layer of every writable JSON persistent state store
up to the same standard already proven on `config/long_term_memory.json`
(Memory Recovery & Persistence Hardening sprint): backup-before-write,
atomic write (temp file + fsync + `os.replace()`), bounded retention,
corrupted-primary recovery where compatible, and test isolation. This is
an INFRASTRUCTURE-layer sprint - no domain semantics, no schema
migrations, no new fields, no changes to Memory Intelligence, Adaptive
Retrieval, Relationship semantics, Emotion Engine, Verified Facts
semantics, or Episodic semantics. `long_term_memory.json` itself is the
REFERENCE IMPLEMENTATION only - its own persistence code is not
rewritten.

Vision Memory SQLite (`config/vision_memory.sqlite3` + `-wal`/`-shm`) is
explicitly OUT OF SCOPE this sprint unless audit finds a fatal bug
directly causing corruption. **None was found** (see §6) - SQLite is
therefore untouched.

## 1. Pre-flight audit - the six target stores

Full detail per store below. Summary table:

| store | owner module | atomic write today | backup today | schema field | dedicated tests | prod call sites |
|---|---|---|---|---|---|---|
| `relationship_state.json` | `luno/relationship_engine.py` (`RelationshipStore`) | partial (temp+replace, no fsync) | none | `schema_version=1` (hard-reset on mismatch, no migration) | `test_relationship_engine.py`, `test_state_isolation.py` | `main_runtime_demo.py` (loaded once at `PlannerBridgeModule.__init__`) |
| `episodic_memory.json` | `luno/episodic_memory.py` (`EpisodicMemoryStore`) | partial (temp+replace, no fsync) | none | per-entry `schema_version=1` | `test_episodic_memory.py`, `test_state_isolation.py` | `main_runtime_demo.py` (`observe_turn()`) |
| `session_summaries.json` | `luno/memory.py` (module functions) | **none** (naive `open(path,"w")`) | none | none | `test_memory_regression.py` (load only) | `main.py`, `main_runtime_demo.py` (`summarize_and_archive_session()`) |
| `habit_memory.json` | `luno/proactive/habit_memory.py` (`HabitMemory`) | **none** | none | none | **zero dedicated tests** | `luno/bootstrap/modules.py`, `luno/proactive/manager.py` |
| `reminders.json` | `luno/reminders.py` (module functions) | **none** | none | none | **zero dedicated tests** | `luno/main.py` only (legacy entry point, not wired into `main_runtime_demo.py`) |
| `verified_facts.json` | `luno/memory_guard.py` (`VerifiedFactStore`) | **none** | none | none | `test_memory_guard.py`, `test_state_isolation.py`, several memory-suite files | `main_runtime_demo.py` (`.record()`) |

**No shared persistence helper exists today.** Confirmed via repo-wide
search: `relationship_engine.py`/`episodic_memory.py` independently
hand-copied a SUBSET of `memory.py`'s pattern (temp+replace, but no
fsync, no backup); `session_summaries`/`habit_memory`/`reminders`/
`verified_facts` use fully naive direct writes with zero atomicity.
`memory.py`'s six hardening symbols (`_atomic_write_json`,
`_backup_current_memory_file`, `_prune_memory_backups`,
`_load_latest_valid_backup`, `_refuse_if_pytest_targeting_unisolated_path`,
`_MEMORY_BACKUP_RETENTION`) are private to that module and reused by
nothing else.

### 1.1 `config/relationship_state.json`

1. **Owner:** `luno/relationship_engine.py`, `RelationshipStore` (static methods).
2. **Load:** `RelationshipStore.load()` - missing file, empty path, parse
   failure, or non-dict root all fall back to `RelationshipState()`
   default, silently (no logging). `schema_version` mismatch -> full
   default reset, no migration (explicit, documented design choice -
   "fail safely rather than silently interpreting incompatible data").
   Individual fields are clamped/defaulted independently, so a partial
   file still loads what it has.
3. **Save:** `RelationshipStore.save(state)` - temp file (`{path}.tmp`)
   + `os.replace()`, but **no fsync**, **no backup**. Returns
   `True`/`False`, never raises.
4. **Path source:** `config.RELATIONSHIP_STATE_FILE`
   (`os.getenv("RELATIONSHIP_STATE_FILE", ...)`).
5. **Schema:** `schema_version` int field, `RELATIONSHIP_SCHEMA_VERSION = 1`.
6. **Backup:** none.
7. **Atomic write:** partial (temp+replace, no fsync).
8. **Existing tests:** `tests/test_relationship_engine.py`, `tests/test_state_isolation.py`.
9. **Prod call sites:** `main_runtime_demo.py` (`PlannerBridgeModule.__init__` loads once; `RelationshipEngine.observe_turn()` computes updates - the actual `.save()` call site is inside `relationship_engine.py`'s own update flow, reached from the same per-turn path).
10. **Test isolation:** `RELATIONSHIP_STATE_FILE` is in `tests/conftest.py`'s `_WRITABLE_STATE_ATTRS`, redirected by the autouse `isolate_persistent_state` fixture. Effective because `RelationshipStore.load()` is called fresh per construction, not at import time.

### 1.2 `config/episodic_memory.json` (currently ABSENT from this checkout)

Confirmed absent - not a bug, not data loss: `luno/episodic_memory.py`
fully exists and owns this file, it has simply never been written in
this checkout (consistent with "missing file -> feature starts fresh").
Per this sprint's own instruction, this store is NOT created just to
have something to test - its absence is documented, not manufactured.

1. **Owner:** `luno/episodic_memory.py`, `EpisodicMemoryStore` (static methods).
2. **Load:** `EpisodicMemoryStore.load()` - missing file, parse failure,
   or non-list root -> `[]`. Malformed individual entries dropped
   silently (list comprehension skips `None` from `EpisodicExperience.from_dict()`),
   including per-entry `schema_version` mismatches.
3. **Save:** `EpisodicMemoryStore.save(experiences)` - temp+replace,
   same as relationship_state - no fsync, no backup.
4. **Path source:** `config.EPISODIC_MEMORY_FILE`.
5. **Schema:** per-entry `schema_version` int, `EPISODIC_SCHEMA_VERSION = 1`.
6. **Backup:** none. **Atomic write:** partial (temp+replace, no fsync).
7. **Existing tests:** `tests/test_episodic_memory.py`, `tests/test_state_isolation.py`, indirectly `tests/test_memory_prompt_intelligence.py`.
8. **Prod call sites:** `main_runtime_demo.py` (`episodic_memory.observe_turn()` per turn; registered as a memory-retrieval source at `PlannerBridgeModule.__init__`).
9. **Test isolation:** `EPISODIC_MEMORY_FILE` in `_WRITABLE_STATE_ATTRS`, effective for the same reason as relationship_state.

### 1.3 `config/session_summaries.json`

1. **Owner:** `luno/memory.py` (module-level functions + module-level global `_session_summaries`).
2. **Load:** `_load_session_summaries()` - missing file -> `[]`, silent.
   Parse failure -> logs `[Memory] ✗ Failed to load ...`, falls back to
   `[]`. **Runs once at module IMPORT time**, not lazily.
3. **Save:** `_save_session_summaries()` - **fully naive**
   `open(path,"w")` + `json.dump()`. No temp file, no atomicity at all.
   Failure logged, never raises.
4. **Path source:** `config.SESSION_SUMMARIES_FILE`.
5. **Schema:** none - bare list of `{id, summary, turn_count, ended_at}`.
6. **Backup:** none. **Atomic write:** none.
7. **Existing tests:** `tests/test_memory_regression.py` (load-only coverage - "must not raise" on malformed/missing). No test exercises the save path directly (LLM-summary generation makes it hard to isolate).
8. **Prod call sites:** `luno/main.py`, `main_runtime_demo.py` via `summarize_and_archive_session()`.
9. **Test isolation:** `SESSION_SUMMARIES_FILE` is in `_WRITABLE_STATE_ATTRS` (path redirected), but the fixture's own docstring explicitly leaves the in-memory `_session_summaries` global UNRESET - a caveat already known and accepted before this sprint, out of this sprint's scope to fix (that's a test-isolation nuance, not a persistence-hardening gap - noted here for completeness only).

### 1.4 `config/habit_memory.json`

1. **Owner:** `luno/proactive/habit_memory.py`, `HabitMemory` (instance-based, constructed once by the bootstrap module registry).
2. **Load:** `HabitMemory._load()` - missing file -> no-op, `_patterns` stays `{}`. Any exception (parse failure, wrong shape) -> caught by a bare `except Exception: pass`, **no logging at all**.
3. **Save:** `HabitMemory._save_locked()` (caller must hold `self._lock`) - **fully naive** `open(path,"w")` + `json.dump`. No temp file. Broad except, no logging on failure either.
4. **Path source:** constructor param -> `config.HABIT_MEMORY_FILE` -> hardcoded `os.path.join("config","habit_memory.json")` as a last-resort fallback if even the config import fails.
5. **Schema:** none - `{"patterns": [...]}`.
6. **Backup:** none. **Atomic write:** none.
7. **Existing tests: NONE.** No dedicated test file exists for this module's persistence at all - confirmed via repo-wide search.
8. **Prod call sites:** `luno/bootstrap/modules.py` (construction), `luno/proactive/manager.py` (`record_verified_action()`, `confirm()`, `decline()` all write).
9. **Test isolation:** `HABIT_MEMORY_FILE` in `_WRITABLE_STATE_ATTRS`, effective because `HabitMemory.__init__` reads the config constant fresh at construction time (always after the fixture has patched it, in every test that builds the full module stack).

### 1.5 `config/reminders.json`

1. **Owner:** `luno/reminders.py` (module-level functions + module-level global `_reminders`).
2. **Load:** `_load()` - missing file -> `[]`, silent. Parse failure -> logs `[Reminders] ✗ Failed to load ...`, falls back to `[]`. **Runs once at module IMPORT time.**
3. **Save:** `_save()` - **fully naive** direct write. Failure logged, never raises.
4. **Path source:** `config.REMINDERS_FILE`.
5. **Schema:** none - bare list of `{id, message, trigger_at, created_at, fired}`.
6. **Backup:** none. **Atomic write:** none.
7. **Existing tests: NONE.** No test file calls into `luno/reminders.py` at all (confirmed - the only "reminder"-adjacent matches in the test suite are unrelated: generic examples in `test_main_bargein.py`'s comments, and `GoalType.HEALTH_REMINDER` in `test_proactive.py`, a completely different proactive-speech concept).
8. **Prod call sites:** `luno/main.py` ONLY - notably NOT wired into `luno.bootstrap.modules.register_all_modules()` or `main_runtime_demo.py` at all. This store sits outside the "modern" runtime stack the other five are part of.
9. **Test isolation:** `REMINDERS_FILE` is in `_WRITABLE_STATE_ATTRS` (path redirected), but same import-time-load caveat as `session_summaries` - `_load()` runs at `luno.reminders` import time, before any per-test patch takes effect for that particular import. Only relevant to the one test file (`test_main_bargein.py`) that imports `luno.main`; that file is separately excluded from the collectible suite already (missing `faster_whisper`, pre-existing, unrelated to this sprint).

### 1.6 `config/verified_facts.json`

1. **Owner:** `luno/memory_guard.py`, `VerifiedFactStore` (instance-based).
2. **Load:** `VerifiedFactStore._load()` - missing file -> no-op, `_facts` stays `{}`. Non-dict root -> silently ignored, stays `{}` (no explicit log for this specific case). Any exception -> caught, logged via `_log()`.
3. **Save:** `VerifiedFactStore._save_locked()` - **fully naive** direct write. Failure caught, logged, never raises. Gated by `self._autosave` (default `True`).
4. **Path source:** constructor param -> `config.VERIFIED_FACTS_FILE`.
5. **Schema:** none - `{entity_id: {...}}` dict, newest overwrites per entity.
6. **Backup:** none. **Atomic write:** none.
7. **Existing tests:** `tests/test_memory_guard.py` (primary), `tests/test_state_isolation.py`, plus several memory-suite files reference it (mostly negative "must not import this" isolation checks).
8. **Prod call sites:** `main_runtime_demo.py` (`PlannerBridgeModule.__init__` constructs once; `.record()` is the sole write path, called from tool-result handling).
9. **Test isolation:** `VERIFIED_FACTS_FILE` in `_WRITABLE_STATE_ATTRS`, effective the same way as `HabitMemory` (fresh construction per test, after the fixture patches the constant).

## 2. Reference implementation contract (Phase 1) - `long_term_memory.json`

Documented as a CONTRACT, not copy-pasted blindly. `luno/memory.py`'s
existing, unmodified functions:

- `_backup_current_memory_file()` - copies the CURRENT on-disk file (if
  any) into `config/backups/` as `long_term_memory.<UTC timestamp>.json`
  BEFORE every write. Best-effort, never raises (a backup failure must
  not itself corrupt anything, but see §3 below for how this sprint's
  helper tightens that for the new stores).
- `_atomic_write_json(path, data)` - `tempfile.mkstemp()` in the SAME
  directory as `path`, write + `f.flush()` + `os.fsync(f.fileno())`,
  then `os.replace(tmp_path, path)`. Cleans up the `.tmp` file on any
  exception before re-raising - primary file is left completely
  untouched on failure.
- `_prune_memory_backups()` - keeps at most `_MEMORY_BACKUP_RETENTION`
  (20) backups, oldest deleted first, `keep = max(1, ...)` - never
  deletes the last backup.
- `_load_latest_valid_backup()` - on primary parse failure, tries each
  backup newest-first, returns the first that parses successfully.
- `_refuse_if_pytest_targeting_unisolated_path(path)` - raises
  `RuntimeError` if `PYTEST_CURRENT_TEST` is set AND the target path is
  not under the system temp directory. Inert outside pytest.

This sequence - **backup current -> write temp -> flush -> fsync ->
replace -> prune** - is the CONTRACT this sprint extends to the other
six stores, via a shared, domain-agnostic helper (§3), not by copying
these six functions verbatim into six more modules.

## 3. Common persistence helper - `luno/persistence.py` (new, per Phase 2)

No existing helper was found to reuse or extend (§1's cross-check). A
new, small, domain-agnostic module was created:

```
luno/persistence.py
  atomic_write_json(path, data, *, backup=True, retention=20) -> None
  safe_load_json(path, default, *, recover_from_backup=True) -> (data, source)
  backup_dir_for(path) -> str
  list_backups(path) -> List[str]
  prune_backups(path, retention=20) -> None
  refuse_if_pytest_targeting_unisolated_path(path) -> None
```

The helper knows NOTHING about memory importance, relationship trust,
habit semantics, reminder semantics, verified-fact semantics, or
episodic semantics - it operates purely on `(path, data)` where `data`
is already a JSON-serializable Python object. Dependency direction is
domain module -> `luno/persistence.py` -> filesystem, never the reverse.
`luno/memory.py`'s own six functions are left exactly as they are (per
Phase 1's "do not rewrite the reference implementation") - the new
helper is a PARALLEL, reusable extraction of the same pattern for the
other six stores, not a replacement of `memory.py`'s own code.

## 4. Backup policy applied

`<basename>.<UTC timestamp>.json` in a `backups/` subdirectory next to
each store's primary file (e.g. `config/backups/relationship_state.<ts>.json`),
`MAX_BACKUPS = 20`, floor of 1 (never delete the last backup). Backup
happens BEFORE the temp-write step; if backup fails, the write is
REFUSED (stronger than `memory.py`'s own best-effort backup - chosen
deliberately per this sprint's own Phase 3 rule: "backup failure HARUS
mencegah destructive overwrite"). Retention pruning only runs after a
successful backup, and pruning failure never blocks or corrupts the
primary write.

## 5. Atomic write applied

Temp file in the SAME directory as the primary (never `/tmp`, never a
different filesystem), write + flush + `os.fsync()`, then
`os.replace()`. Verified by failure-injection tests per store: primary
content before == primary content after when the write is interrupted
before `os.replace()`.

## 6. Schema safety / SQLite boundary

No schema version fields added to any of the six stores. No new fields
of any kind (`persistence_version`, `truth_score`, `reliability_score`,
`recovery_score`, etc.). All existing load-time behavior for
missing/empty/malformed files is PRESERVED exactly (see §1's per-store
"load" notes) - `safe_load_json()`'s `default` parameter is supplied
per-call-site to match each store's own pre-existing fallback shape
(`RelationshipState()`, `[]`, `[]`, `{"patterns": []}` / `{}`
internally, `[]`, `{}`).

**SQLite:** `luno/vision_memory/database.py` opens
`config/vision_memory.sqlite3` with `PRAGMA journal_mode=WAL`, and
already contains its own defensive fallback/warning if WAL mode isn't
supported by the underlying filesystem. No fatal corruption was found
during this audit - the `.sqlite3` file itself is intact and openable.
**Untouched this sprint, as instructed.**

**Separate finding, documented not fixed (out of scope):** this sandbox's
`config/` directory is FUSE-mounted (per the environment's own mount
description), and 617 `.fuse_hidden*` artifact files (~20MB total,
all created 2026-08-05 through 2026-08-07, i.e. BEFORE this sprint's own
baseline snapshot and unrelated to any of this session's work) were
found alongside the tracked JSON/SQLite files. This is the well-known
FUSE behavior where an application's unlink/replace of a still-open file
leaves the old inode as a hidden file until every handle closes - the
`vision_memory.sqlite3` WAL/SHM churn (frequent open/close/replace
cycles under `PRAGMA journal_mode=WAL`) across many prior sprint
sessions is the most plausible cause, consistent with `database.py`'s
own comment acknowledging WAL's reliance on filesystem-level shared
memory-mapping support that a FUSE mount does not fully provide. Per
this sprint's own Phase 11 instruction ("Jangan langsung delete
artifact... STOP dan investigate"), these files are documented here,
NOT deleted, and flagged to Vinn separately in this sprint's final
report - cleanup, if wanted, is a decision for Vinn to make explicitly,
not something this sprint performs unilaterally.

## 7. Pytest write guard

`_refuse_if_pytest_targeting_unisolated_path()` is reused from
`luno/persistence.py` (extracted, not copy-pasted per-store) and called
from `atomic_write_json()` itself, so every store that adopts the shared
helper gets the guard automatically, for free, with no per-module
boilerplate. Confirmed compatible with `tests/conftest.py`'s existing
`isolate_persistent_state` fixture (already redirects all six paths via
`_WRITABLE_STATE_ATTRS`) - the guard is defense-in-depth for the same
failure class the Memory Recovery incident was caused by (a bare script
bypassing pytest entirely), not a replacement for the fixture.

## 8. What this sprint does NOT do

No second memory/retrieval/evaluation system. No new schema versions.
No destructive migration. No change to Adaptive Retrieval, Memory
Intelligence, Relationship semantics, Emotion Engine, Verified Facts
semantics, or Episodic semantics. No SQLite changes. No deletion of the
617 `.fuse_hidden*` artifacts found during audit (documented, not
touched).
