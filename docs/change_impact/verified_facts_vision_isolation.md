# Change Impact Analysis — Verified Facts & Vision Memory Test Isolation

```
FEATURE:
Verified Facts & Vision Memory Test Isolation

GOAL:
Extend the Test State Isolation & Persistent Data Safety sprint's own
mechanism to the two persistent stores it explicitly deferred:
config/verified_facts.json and config/vision_memory.sqlite3.

CURRENT ARCHITECTURE:

Verified Facts (luno/memory_guard.py):
- `VerifiedFactStore.__init__(self, path=None, autosave=True)`:
  `self._path = path or os.path.join(config.DATA_DIR, "verified_facts.json")`
  - resolved ONCE, at CONSTRUCTION time (not save-time, not import-time).
  - No dedicated env-var-backed config constant exists today - only the
    much broader `config.DATA_DIR`.
- NOT a singleton - a fresh `VerifiedFactStore()` instance is constructed
  every time `PlannerBridgeModule.__init__` runs (main_runtime_demo.py:733,
  no `path` argument passed), which happens once per test that constructs
  a console/module stack, via ANY of: `register_all_modules()`,
  `main_runtime_demo.RuntimeDemoConsole(...)`, or
  `main_runtime_demo.PlannerBridgeModule()` directly.
- Writer: `VerifiedFactStore.record()` -> `_save_locked()`, called
  whenever a verified (`success=True`) tool result with an `entity_id`
  is processed - e.g. any "turn on/off the lights" utterance.
- `tests/test_memory_guard.py` ALREADY isolates its OWN direct
  `VerifiedFactStore(path=...)` unit tests via an explicit temp path
  (`_tmp_store()` helper) - but its two end-to-end scenarios
  (`demo.RuntimeDemoConsole(...)`, lines ~310/~351) construct a REAL
  console, which internally builds `VerifiedFactStore()` with NO path -
  these two scenarios were NOT isolated before this sprint.

Vision Memory (luno/vision_memory/api.py + memory.py + database.py):
- Module-level singleton: `_instance: Optional[VisionMemory] = None`,
  lazily created by `_get_memory()`, guarded by `_instance_lock`.
- Path resolution: `_db_path_override` (module global, set via the
  PUBLIC `configure(db_path=...)` function) if set, else
  `_default_db_path()` (reads `config.DATA_DIR` at CALL time, not import
  time) - but only consulted the FIRST time `_get_memory()` creates
  `_instance`; every subsequent call reuses the SAME `_instance` (and
  therefore the SAME open SQLite connection) until `reset()` drops it.
- `reset()` only sets `_instance = None` - does NOT call `.close()` on
  the old connection, and does NOT touch `_db_path_override` - the next
  `_get_memory()` call re-resolves using whatever `_db_path_override`/
  `_default_db_path()` currently says.
- `Database.__init__(self, path)` connects immediately AND runs
  `CREATE TABLE IF NOT EXISTS` schema creation unconditionally - schema
  init against a brand-new empty temp file works with ZERO special
  handling required (idempotent by construction).
- `main_runtime_demo.py`'s own demo `VisionMemoryModule.start()` calls
  `vm.reset()` (line 380) with NO `configure()` call - meaning every
  console built through the demo path (test_runtime_demo.py's
  `_new_console()`, `test_memory_guard.py`'s end-to-end scenarios, and
  anything else constructing `main_runtime_demo.RuntimeDemoConsole`
  directly) drops the singleton and lets the NEXT access re-resolve via
  `_default_db_path()` - which, absent any override, is the REAL
  `config/vision_memory.sqlite3`.
  `luno/bootstrap/modules.py`'s `ProductionVisionMemoryModule.start()`
  deliberately does NOT call `reset()` (correct for production - state
  must survive a restart) - but its FIRST-ever creation in a process
  still touches the real file if nothing configured an override first.
- EXISTING, ALREADY-PROVEN ISOLATION PATTERN:
  `tests/test_vision_sprint8.py::_isolate_vision_memory()` already does
  exactly `vm.reset()` + `vm.configure(db_path=<fresh tempfile.mkdtemp()
  path>)` - proving this is a safe, working mechanism, just not applied
  repository-wide. This sprint REUSES this exact mechanism rather than
  inventing a new one (per this sprint's own "Do NOT create a new
  abstraction" instruction) - only difference: applied via
  `monkeypatch.setattr` on the two module globals directly
  (`_instance`/`_db_path_override`) instead of the permanent public-API
  calls, specifically to get automatic, exception-safe revert semantics
  `configure()`/`reset()` alone do not provide.

EMPIRICALLY CONFIRMED THIS SPRINT (sha256 + mtime diff, BEFORE this
sprint's fix): both `config/verified_facts.json` AND
`config/vision_memory.sqlite3` changed during ordinary test runs
executed earlier in this working session (before this sprint's fixture
existed) - full before/after hashes recorded in the final report. This
was NOT limited to `test_dashboard.py` - `config/vision_memory.sqlite3`
has almost certainly been touched by every prior sprint's
`test_runtime_demo.py` run this session, since `_new_console()` always
goes through the demo `VisionMemoryModule.start()` -> `vm.reset()` path
with no override ever configured before this sprint.

PERSISTENT FILES DISCOVERED (this sprint's scope):
- config/verified_facts.json (VerifiedFactStore)
- config/vision_memory.sqlite3 (+ -wal/-shm sidecar files, WAL journal
  mode - see database.py)

EXACT WRITERS:
- VerifiedFactStore.record() -> _save_locked()
- VisionMemory.update() (and its internal Database write methods) -
  triggered by `vm.update(description)`, called from
  `demo.VisionMemoryModule.on_event()`/`ProductionVisionMemoryModule.on_event()`
  for motion/person_detected/door_open events, AND from
  `PlannerBridgeModule`'s own vision-intent handling.

SINGLETON/CACHE RISKS:
- Vision Memory: process-wide singleton, confirmed above - simply
  redirecting `config.DATA_DIR` (as done for the six JSON files in the
  prior sprint) would NOT be sufficient by itself if `_instance` was
  already created earlier in the same pytest process. Both `_instance`
  and `_db_path_override` must be reset/redirected together.
- Verified Facts: NOT a singleton (fresh instance per PlannerBridgeModule
  construction) - a simple construction-time config read is sufficient,
  same shape as `HabitMemory` (already handled by the prior sprint's
  fixture for a different file).

PROPOSED ISOLATION STRATEGY:
1. Add `VERIFIED_FACTS_FILE` to `luno/config.py`, following the exact
   existing `os.getenv("NAME", os.path.join(DATA_DIR, "file.json"))`
   pattern every other `*_FILE` constant already uses - default value is
   BYTE-IDENTICAL to today's computed default
   (`os.path.join(DATA_DIR, "verified_facts.json")`), so production
   behavior does not change at all.
2. Change `VerifiedFactStore.__init__`'s default from
   `os.path.join(config.DATA_DIR, "verified_facts.json")` to
   `config.VERIFIED_FACTS_FILE` - one-line change, same default value,
   now independently overridable exactly like every sibling store.
3. Extend `tests/conftest.py`'s existing `isolate_persistent_state`
   autouse fixture: add `VERIFIED_FACTS_FILE` to the existing
   `_WRITABLE_STATE_ATTRS` tuple (zero new code needed beyond the one
   entry, reuses the exact same loop already handling six other files),
   and add Vision Memory's `vm.reset()`-equivalent + `vm.configure()`-
   equivalent redirection via `monkeypatch.setattr` on `_instance`/
   `_db_path_override`.

FILES EXPECTED TO CHANGE:
- luno/config.py (add VERIFIED_FACTS_FILE constant)
- luno/memory_guard.py (VerifiedFactStore default path source)
- tests/conftest.py (extend existing fixture)
- tests/test_state_isolation.py (new tests)
- ARCHITECTURE_GUARD.md (new contract entries)
- docs/testing/regression_baseline.md (validation note, if warranted)

FILES EXPECTED NOT TO CHANGE:
- luno/vision_memory/* (api.py/memory.py/database.py/models.py/tracker.py/
  etc.) - the EXISTING configure()/reset() mechanism is reused exactly
  as it already exists; no vision-memory subsystem code changes.
- main_runtime_demo.py - no changes; `VisionMemoryModule.start()`'s
  existing `vm.reset()` call already does the right thing once an
  override is set BEFORE it runs.
- luno/bootstrap/modules.py - no changes; `ProductionVisionMemoryModule`'s
  deliberate "do not reset" behavior is exactly correct for production
  and is preserved unchanged.
- Any existing test's assertions - purely additive.

REGRESSION RISKS:
- `tests/test_vision_sprint8.py`'s own `_isolate_vision_memory()` helper
  runs an ADDITIONAL `configure()` call after the new autouse fixture's
  own redirect - harmless (last-setter-wins, same test still gets an
  isolated path, just its own rather than the fixture's default one).
- `tests/test_memory_guard.py::_tmp_store()`'s explicit `path=` construction
  is unaffected (an explicit constructor argument always wins over
  whatever `config.VERIFIED_FACTS_FILE` says).
- Any test asserting `VerifiedFactStore()`'s DEFAULT path literally
  equals `os.path.join("config", "verified_facts.json")` outside a
  redirected test context would need to also account for
  `config.VERIFIED_FACTS_FILE` - audited, no such test exists.

REGRESSION RISKS - VISION MEMORY SPECIFICALLY:
- Any test that intentionally relies on Vision Memory state PERSISTING
  from one test to the next WITHIN the same test file (e.g. testing
  restart-survival) needs its own explicit, deliberate override handling
  - audited: `test_vision_sprint8.py`'s restart-survival scenarios (if
  any) construct their own explicit path via `_isolate_vision_memory()`
  once and reuse it deliberately across sequential calls WITHIN one test
  function, never relying on cross-TEST-FUNCTION persistence - confirmed
  safe against a fixture that resets before every test function.

ROLLBACK STRATEGY: Revert the four changed files
(luno/config.py, luno/memory_guard.py, tests/conftest.py,
tests/test_state_isolation.py) to their prior-sprint state. Production
behavior reverts to reading `config.DATA_DIR`-derived defaults exactly
as before (since the new default is byte-identical, reverting
`memory_guard.py` alone would already be a no-op for production;
reverting `config.py` simply removes the now-unused constant).
```
