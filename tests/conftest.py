"""
conftest.py
===========

LUNO Test State Isolation & Persistent Data Safety sprint.

Repository-wide pytest fixtures for `tests/` - specifically, the ONE
safety net every test collected under this directory now gets for free:
no automated test run under `tests/` can ever write to Vinn's real
`config/*.json` persistent state files, even if the test constructs a
full, real, production-like module stack via
`luno.bootstrap.modules.register_all_modules()`.

WHY THIS EXISTS
----------------
`register_all_modules()` - the exact function `main.py` calls in
production - constructs a real `PlannerBridgeModule` (which loads
`RelationshipStore.load()` and registers an `episodic_memory` retrieval
source backed by `EpisodicMemoryStore.load`), a real `HabitMemory()`, and
imports `luno.memory` (whose `_load()`/`_load_session_summaries()` already
ran at module-import time) - all of it pointed, by default, at whatever
`luno/config.py`'s `*_FILE` constants currently resolve to, which in a
normal checkout means Vinn's REAL `config/relationship_state.json`,
`config/episodic_memory.json`, `config/long_term_memory.json`,
`config/session_summaries.json`, `config/habit_memory.json`, and
`config/reminders.json`.

Six test files under `tests/` call `register_all_modules()` directly
(`test_production_launcher.py`, `test_dashboard.py`, `test_llm_dashboard.py`,
`test_verification_dashboard.py`, `test_routing_dashboard.py`,
`test_proactive.py`) - see `docs/change_impact/test_state_isolation.md`
for the full caller audit. This sprint EMPIRICALLY CONFIRMED (sha256 +
mtime diff before/after) that `test_dashboard.py` alone - not
`test_production_launcher.py`, the file this sprint's own bug report
initially named - mutates the real `config/relationship_state.json` by
publishing a real `user_utterance` event through a `register_all_modules()`
-built stack with no redirect anywhere in that file.

WHY AN AUTOUSE FIXTURE, NOT PER-FILE EDITS
---------------------------------------------
Editing six files (and remembering to edit every FUTURE file that ever
calls `register_all_modules()`) is exactly the class of bug that already
happened once (the Relationship Engine Foundation sprint fixed this for
`tests/test_runtime_demo.py` alone, months before `test_dashboard.py`
quietly reintroduced the identical class of bug via a different file).
An `autouse=True` fixture here requires ZERO changes to any of the six
files' test function signatures - it wraps every test collected under
`tests/` unconditionally, present and future, closing the underlying
class of bug rather than one more instance of it.

WHY `monkeypatch.setattr`, NOT `monkeypatch.setenv`
------------------------------------------------------
`luno/config.py`'s `*_FILE` constants are computed ONCE via
`os.getenv(...)` at MODULE IMPORT time. By the time any test's fixtures
run, `luno.config` has already been imported (transitively, by nearly
everything) - `monkeypatch.setenv("RELATIONSHIP_STATE_FILE", ...)` at
that point has NO EFFECT on the already-bound `luno.config.RELATIONSHIP_STATE_FILE`
attribute (the exact same observation `tests/test_production_launcher.py`'s
own `test_08_health_check_failure_disables_only_that_subsystem_never_aborts_runtime`
already makes, for `HA_URL`/`HA_TOKEN`, in its own docstring). This
fixture instead does what `tests/test_relationship_engine.py`/
`tests/test_episodic_memory.py` already do per-test: `monkeypatch.setattr`
directly on the live `luno.config` module object. Every writer this
fixture protects reads its own `config.X` attribute FRESH at save time
(`RelationshipStore.save()`, `EpisodicMemoryStore.save()`,
`memory._save()`/`_save_session_summaries()`, `reminders._save()`) or at
construction time within the SAME test (`HabitMemory.__init__` - always
constructed fresh inside `register_all_modules()`) - confirmed by reading
every one of those functions before writing this fixture, not assumed.

WHAT THIS FIXTURE DOES NOT COVER (documented, not silently ignored)
-----------------------------------------------------------------------
As of the Verified Facts & Vision Memory Test Isolation sprint, BOTH
`config/verified_facts.json` and `config/vision_memory.sqlite3` ARE now
covered (see the two dedicated sections below) - this fixture's writer
list and the Vision Memory redirect together now cover every persistent
store discovered by that sprint's own audit
(`docs/change_impact/verified_facts_vision_isolation.md`). Read-only seed
configuration (`LIGHTS_CONFIG_FILE`, `SWITCHES_CONFIG_FILE`,
`SCRIPTS_CONFIG_FILE`, `ENV_TRIGGERS_CONFIG_FILE`, `PERSONA_FILE`,
`APPS_CONFIG_FILE`) remains deliberately NOT redirected - Luno never
writes to those, so real files may be safely read by any test, and
redirecting them would only break tests that legitimately rely on real
seed content (e.g. the 3 real configured lights).

VISION MEMORY - WHY MONKEYPATCHING THE MODULE GLOBALS DIRECTLY
-------------------------------------------------------------------
`luno.vision_memory` is NOT a `config.*_FILE`-backed store - it's a
module-level singleton (`luno.vision_memory.api._instance`), lazily
created by `_get_memory()`, whose path is decided ONCE (at first
creation) from `_db_path_override` (set via the public `configure()`
function) or `_default_db_path()` (`config.DATA_DIR`-derived). Merely
redirecting `config.DATA_DIR` would NOT be sufficient if `_instance`
already exists from an earlier test in the same pytest process - the
stale, already-open SQLite connection would simply keep being reused
(`docs/change_impact/verified_facts_vision_isolation.md`'s "Singleton/
cache risks" section covers this in full, confirmed by reading
`api.py`/`memory.py`/`database.py` directly, not assumed).

`tests/test_vision_sprint8.py::_isolate_vision_memory()` already
establishes the correct, PROVEN-SAFE mechanism: `vm.reset()` (drops
`_instance`) followed by `vm.configure(db_path=<fresh temp path>)` (sets
`_db_path_override`) - this fixture REUSES that exact mechanism (per
this sprint's own "do not create a new abstraction" instruction) rather
than inventing anything new, but applies it via `monkeypatch.setattr`
directly on the two module globals (`_instance`/`_db_path_override`)
instead of the permanent public-API calls, specifically so monkeypatch's
own automatic, exception-safe teardown reverts them too - calling
`reset()`/`configure()` directly provides no such revert (they
permanently mutate the module global with no built-in undo). Since this
fixture runs before EVERY test, each test always gets its OWN fresh
`_db_path_override` regardless of what any earlier test (including
`test_vision_sprint8.py`'s own calls) left behind - no reliance on
teardown-time restoration for correctness, only for hygiene.

PARALLEL SAFETY
-----------------
Each test gets pytest's own per-test `tmp_path` (unique per test, and
unique per `pytest-xdist` worker if that's ever adopted) - no two tests
can ever share the same redirected file, so there is no shared mutable
global test state to race on.
"""

from __future__ import annotations

import threading
import time
from typing import Dict

import pytest

from luno import config as _luno_config
from luno import memory as _memory
from luno import mutation_audit as _mutation_audit
from luno import vision_memory as _vm

#: Test-isolation-only safety net (NOT a production fix - production
#: shutdown behavior in `luno/core/runtime.py`/`main_runtime_demo.py` is
#: deliberately untouched by this). Root cause: `PlannerBridgeModule.
#: on_event()` (main_runtime_demo.py) spawns a bare, untracked
#: `threading.Thread(daemon=True, name="luno-planner-turn")` per
#: `user_utterance` - NOT submitted through `Dispatcher`, so nothing in
#: `Runtime.stop()`/`RuntimeDemoConsole.stop()` ever waits for it. A real
#: conversational turn (including its own `RelationshipStore.save()`
#: call inside `_handle_utterance()`) can still be genuinely mid-flight
#: on this thread when a test's `console.stop()` returns and this
#: fixture's `config.*_FILE` redirects are about to be reverted at
#: teardown - if that straggler's save happens to land AFTER the revert,
#: it writes to the REAL path instead of this test's isolated one.
#: Confirmed via a live diagnostic trace during the Chat/Voice Dual
#: Output sprint's own regression sweep (not merely suspected) -
#: `config/relationship_state.json` was found mutated with test-shaped
#: values twice in the same session, both times traced to exactly this
#: thread outliving its test. `_STRAGGLER_THREAD_NAMES` is deliberately a
#: small, explicit set (not "join every new thread") - long-running
#: service threads (Event Bus pump, Heartbeat, Scheduler, Dispatcher pool
#: workers) are expected to keep running independently and joining those
#: here would risk hanging a test instead of protecting one.
_STRAGGLER_THREAD_NAMES = frozenset({"luno-planner-turn"})
_STRAGGLER_DRAIN_TIMEOUT_S = 5.0


def _drain_straggler_threads(timeout_s: float = _STRAGGLER_DRAIN_TIMEOUT_S) -> None:
    """Joins any live thread whose name is in `_STRAGGLER_THREAD_NAMES`,
    bounded by `timeout_s` total (not per-thread) so a genuinely stuck
    thread can never hang the suite forever. Safe/cheap to call even when
    no such thread exists (`threading.enumerate()` is a fast, in-process
    call, no I/O) - the common case (no straggler) costs a negligible
    list scan, not a fixed sleep, so this does not measurably slow down
    the other ~1400 tests that never touch `RuntimeDemoConsole` at all."""
    deadline = time.time() + timeout_s
    for t in threading.enumerate():
        if t.name in _STRAGGLER_THREAD_NAMES and t.is_alive():
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)

#: Every `luno/config.py` constant that backs a file Luno itself can
#: WRITE at runtime (confirmed via a `json.dump`/`"w"`/`def save`/
#: `def _save` audit of every consuming module - see
#: docs/change_impact/test_state_isolation.md's Persistent State
#: Inventory table for the full per-file writer evidence). Read-only seed
#: configuration (`LIGHTS_CONFIG_FILE`, `SWITCHES_CONFIG_FILE`,
#: `SCRIPTS_CONFIG_FILE`, `ENV_TRIGGERS_CONFIG_FILE`, `PERSONA_FILE`,
#: `APPS_CONFIG_FILE`) is deliberately NOT redirected here - Luno never
#: writes to those, so real files may be safely read by any test, and
#: redirecting them would only break tests that legitimately rely on
#: real seed content (e.g. the 3 real configured lights).
_WRITABLE_STATE_ATTRS = (
    "RELATIONSHIP_STATE_FILE",
    "EPISODIC_MEMORY_FILE",
    "LONG_TERM_MEMORY_FILE",
    "SESSION_SUMMARIES_FILE",
    "HABIT_MEMORY_FILE",
    "REMINDERS_FILE",
    "VERIFIED_FACTS_FILE",
    # Persistent Adaptive Response Depth Preference sprint - a small,
    # bounded, writer-capable JSON store (see
    # luno/response_depth_preference.py's `DepthPreferenceStore`) - added here for
    # the exact same reason every other file in this tuple is here: no
    # test may ever write to Vinn's real
    # config/response_depth_preference.json.
    "RESPONSE_DEPTH_PREFERENCE_FILE",
)


@pytest.fixture(autouse=True)
def isolate_persistent_state(tmp_path, monkeypatch) -> Dict[str, str]:
    """Runs before EVERY test collected under `tests/` - redirects every
    writer-capable persistent-state file (JSON stores AND Vision
    Memory's SQLite database) to a fresh, test-private path under
    pytest's own `tmp_path` (auto-cleaned by pytest itself, never shared
    between tests). Returns the mapping (attr name -> temp path) for
    tests that want to assert against their own isolated path (e.g.
    `tests/test_state_isolation.py`).

    Uses `monkeypatch`, so EVERY redirect here is automatically reverted
    at the end of the test - including when the test raises an exception
    (pytest's fixture finalizers always run, this is not something this
    fixture has to implement itself) - see `test_state_isolation_survives_exception`
    for the regression test proving this.

    A test that needs a SPECIFIC path for its own purposes (e.g.
    `tests/test_relationship_engine.py`'s per-test `monkeypatch.setattr`,
    `tests/test_runtime_demo.py`'s module-level redirect, or
    `tests/test_vision_sprint8.py`'s own `_isolate_vision_memory()`) can
    still freely reassign `config.X`/`vm._db_path_override` itself
    afterward - this fixture only sets a safe DEFAULT floor, it never
    claims exclusive ownership of these attributes, and monkeypatch
    cleanly layers multiple `setattr` calls to the same attribute (last
    one wins during the test, all reverted in LIFO order at teardown)."""
    paths: Dict[str, str] = {}
    for attr in _WRITABLE_STATE_ATTRS:
        temp_path = str(tmp_path / f"{attr}.json")
        monkeypatch.setattr(_luno_config, attr, temp_path, raising=False)
        paths[attr] = temp_path

    # Sprint 67 (Mutation Audit Trail) - `luno.persistence.atomic_write_json()`
    # and `luno.memory`'s own private mirror of that same contract now
    # write ONE `luno.mutation_audit` JSONL record per call (see both
    # modules' own Sprint 67 additions). Without this redirect, every one
    # of the ~1100+ memory/persistence-touching tests in this suite would
    # append real records into Vinn's actual `logs/mutation_audit/`
    # directory - harmless to `config/*.json` (mutation_audit never
    # writes there), but exactly the kind of real-filesystem test
    # side-effect this fixture exists to prevent everywhere else, so it
    # is prevented here too rather than left as a partial gap.
    mutation_audit_dir = str(tmp_path / "mutation_audit")
    monkeypatch.setattr(_mutation_audit, "AUDIT_LOG_DIR", mutation_audit_dir, raising=False)
    monkeypatch.setattr(_mutation_audit, "_rotation_attempted", False, raising=False)
    paths["MUTATION_AUDIT_LOG_DIR"] = mutation_audit_dir

    # Vision Memory - see module docstring's "VISION MEMORY" section
    # above for why both globals must be redirected together.
    #
    # IMPORTANT: `_instance`/`_db_path_override` are module-level globals
    # defined in `luno/vision_memory/api.py` - `luno/vision_memory/__init__.py`
    # re-exports the *functions* (`update`, `reset`, `configure`, ...) but
    # deliberately does NOT re-export these two globals themselves (see
    # `__init__.py`'s `from .api import (...)` list). Patching them on
    # `_vm` (the package, i.e. `luno.vision_memory`) would therefore only
    # ever create a new, disconnected attribute on the package module -
    # `_get_memory()` (defined inside `api.py`) reads its OWN module's
    # globals, never the package's, so a patch on the wrong module object
    # is silently a no-op as far as `_get_memory()` is concerned. `_vm.api`
    # is the actual submodule object (bound onto the package automatically
    # the moment `__init__.py` did `from .api import ...`), so patching
    # THAT is what actually reaches `_get_memory()`. Caught via a failing
    # `os.path.exists(isolated_path)` assertion in
    # `tests/test_state_isolation.py::test_vision_memory_isolated` during
    # this sprint - see that file for the regression tests proving this
    # fix actually works.
    vision_db_path = str(tmp_path / "vision_memory.sqlite3")
    monkeypatch.setattr(_vm.api, "_instance", None, raising=False)
    monkeypatch.setattr(_vm.api, "_db_path_override", vision_db_path, raising=False)
    paths["VISION_MEMORY_DB"] = vision_db_path

    # Manual Memory Management sprint - `LONG_TERM_MEMORY_FILE` above only
    # redirects the FILE PATH; `luno.memory._memories` is a module-level
    # list populated ONCE, at process import time, by `luno.memory`'s own
    # `_load()` call (`luno/memory.py`: "load sekali saat modul pertama
    # kali diimpor") - which normally runs during test COLLECTION, before
    # this fixture (or any other) has had a chance to redirect anything.
    # Without this reset, `_memories` would carry whatever was in the
    # REAL `config/long_term_memory.json` at the moment pytest started for
    # every test that reads it (`list_memories()`/`build_memory_prompt()`/
    # the `"manual_memory"` MemoryRetriever source) - not a real-file
    # WRITE-safety issue (every writer already reads `config.
    # LONG_TERM_MEMORY_FILE` fresh at save time, so writes always land on
    # the isolated path above), but a real TEST-DETERMINISM issue: a
    # test's memory-related assertions could silently depend on whatever
    # facts happen to be in Vinn's real file that day. `monkeypatch.setattr`
    # (not a plain reassignment) so this is automatically, exception-
    # safely reverted at teardown exactly like every other redirect in
    # this fixture - see `tests/test_manual_memory.py` for the regression
    # tests proving this reset actually takes effect and doesn't leak
    # between tests. Deliberately scoped to `_memories` only - `luno.memory`'s
    # OTHER module-level global (`_session_summaries`) has the identical
    # structural staleness property but is a separate concept from Manual
    # Memory (session summaries vs. explicit user-requested facts) and no
    # test in this sprint depends on it being reset, so it is left
    # untouched per this sprint's own "minimal, scoped changes" rule.
    monkeypatch.setattr(_memory, "_memories", [], raising=False)

    yield paths

    # Drain BEFORE monkeypatch's own automatic revert runs (pytest tears
    # fixtures down in reverse dependency order - this fixture depends on
    # `monkeypatch`, so `monkeypatch`'s revert happens AFTER this
    # generator resumes and returns, meaning any straggler joined here
    # still sees the isolated `config.*_FILE` values, never the real
    # ones - see `_drain_straggler_threads()`'s own docstring above for
    # the full root-cause writeup this closes).
    _drain_straggler_threads()
