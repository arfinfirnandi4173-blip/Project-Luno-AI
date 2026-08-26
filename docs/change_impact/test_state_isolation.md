# Change Impact Analysis — Test State Isolation & Persistent Data Safety

```
FEATURE:
Test State Isolation & Persistent Data Safety

GOAL:
Prevent automated tests from mutating Vinn's real persistent
configuration/state files (config/*.json), regardless of which test file
constructs a production-like module stack.

KNOWN BUG:
tests/test_production_launcher.py can construct production-like modules
(via register_all_modules()) without redirecting persistent state paths.

EMPIRICALLY CONFIRMED DURING THIS SPRINT'S BASELINE (before any fix):
- Running tests/test_production_launcher.py ALONE did NOT mutate
  config/relationship_state.json - none of its 24 scenarios publish a
  real `user_utterance` event, so `PlannerBridgeModule._handle_utterance()`
  (the only code path that calls RelationshipStore.save()) never runs.
- Running tests/test_dashboard.py ALONE DID mutate config/relationship_state.json
  (interaction_count 0 -> 4, trust 0.0 -> 0.01, confirmed via sha256 +
  mtime diff before/after). That file publishes a real
  `user_utterance` event ("turn on the light") through a
  register_all_modules()-built stack with no redirect anywhere in the
  file.
This directly confirms the sprint brief's own "Do not assume
test_production_launcher.py is the only affected test" - it is not
actually the (only) live offender; test_dashboard.py is.

FILES TO CHANGE:
- tests/conftest.py (NEW) - autouse fixture redirecting every writer-
  capable persistent-state config attribute to a tmp_path-based file for
  every test collected under tests/.
- tests/test_state_isolation.py (NEW) - real-file-protection, env-leak,
  failure-cleanup, and production-launcher-specific regression tests.
- ARCHITECTURE_GUARD.md - new "Test State Isolation" section + contract.
- docs/testing/regression_baseline.md - updated only if results change.

No production code (luno/*, main.py) changes are planned - the audit
found no genuine production-side safety bug (production code always
honors whatever config.*_FILE currently points to; the gap is entirely
in test setup, never redirecting those attributes before constructing a
real module stack).

DIRECTLY AFFECTED SUBSYSTEMS:
- Test infrastructure only (tests/conftest.py is new; no existing test
  file's assertions are touched).

INDIRECTLY AFFECTED SUBSYSTEMS:
- Every test file that transitively imports anything under tests/ and
  therefore picks up the new autouse fixture: primarily the 6 files that
  call register_all_modules() (test_production_launcher.py,
  test_dashboard.py, test_llm_dashboard.py, test_verification_dashboard.py,
  test_routing_dashboard.py, test_proactive.py), but the fixture applies
  repo-wide under tests/ so it also touches (harmlessly - see Isolation
  section below) test_relationship_engine.py/test_episodic_memory.py/
  test_runtime_demo.py, which already redirect these same attributes
  themselves; monkeypatch layering makes this safe (last-applied wins,
  auto-reverted in LIFO order).

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- Relationship state contract, Episodic Memory contract - neither's
  public interface changes; only the FILE PATH tests point them at
  changes, never their behavior.
- Memory contract (luno/memory.py) - same.

EXPECTED REGRESSION RISKS:
- A test that asserts a literal default path (e.g. `config.RELATIONSHIP_STATE_FILE
  == os.path.join("config", "relationship_state.json")`) would break
  under the new autouse fixture - audited during pre-flight, no such test
  was found (test_mic_device_index.py-style precedence tests exist only
  for MIC_DEVICE_INDEX, not for any of the six redirected attributes).
- Multi-turn tests within a single test function that rely on an
  explicit `_luno_config.X = fresh_path` assignment INSIDE the test body
  (test_runtime_demo.py's own pattern) still work: the autouse fixture's
  monkeypatch.setattr runs at fixture-setup time (before the test body),
  so the test's own later plain assignment simply overwrites it for the
  remainder of that test, and monkeypatch's teardown correctly reverts to
  its own pre-test value (the module-level test file redirect, not the
  real path) afterward - traced through and confirmed safe before
  implementation, verified empirically by the full regression sweep.

PROTECTED:
- Production persistent state (config/*.json - all of it, not just the
  four explicitly named in this sprint's FINAL RULE)
- Memory (long_term_memory.json, session_summaries.json)
- Episodic Memory (episodic_memory.json)
- Relationship Engine (relationship_state.json)
- Personality configuration (persona.json - read-only, never written by
  Luno itself, confirmed via grep - no fixture needed, but inventoried)
- Environment configuration (.env - never touched by this sprint at all)

TESTS TO RUN:
- python -m pytest luno/ -q (806 passed / 2 known-flaky baseline,
  UNAFFECTED - tests/conftest.py does not apply to luno/)
- python -m pytest tests/test_production_launcher.py -q
- python -m pytest tests/test_memory_regression.py tests/test_memory_guard.py
  tests/test_memory_retrieval.py tests/test_episodic_memory.py
  tests/test_relationship_engine.py tests/test_emotion_engine.py
  tests/test_persona.py tests/test_runtime_demo.py -q
- python -m pytest tests/test_state_isolation.py -q (new)
- Broader tests/ sweep per docs/testing/regression_baseline.md's own
  documented exclusions/interpretation.

NEW TESTS REQUIRED:
- test_relationship_state_isolated, test_relationship_state_does_not_touch_real_file
- test_episodic_memory_isolated, test_episodic_memory_does_not_touch_real_file
- test_state_environment_does_not_leak
- test_state_isolation_survives_exception
- test_production_launcher_utterance_flow_does_not_touch_real_state (proves
  the ROOT CAUSE class of bug specifically, using a real user_utterance
  through register_all_modules(), the same shape test_dashboard.py uses)

ROLLBACK PLAN: Delete tests/conftest.py and tests/test_state_isolation.py.
No production code changes to revert. The six previously-vulnerable test
files return to their pre-sprint (unsafe) behavior, unchanged from before
this sprint - a clean, complete revert.
```

## Persistent State Inventory

Discovered via `grep -n "_FILE = os.getenv" luno/config.py` plus a
writer-vs-reader audit of every module that consumes each constant
(`json.dump`/`"w"`/`def save`/`def _save` search per module).

| File | Owner | Writer | Env var | Default path | Test override mechanism | Writes read `config.X` | Tests that write | Tests that only read |
|---|---|---|---|---|---|---|---|---|
| `relationship_state.json` | `RelationshipStore` | `RelationshipStore.save()` | `RELATIONSHIP_STATE_FILE` | `config/relationship_state.json` | `monkeypatch.setattr(config, "RELATIONSHIP_STATE_FILE", ...)` (now also autouse fixture) | fresh, at save-time | `test_dashboard.py` (CONFIRMED empirically this sprint), `test_relationship_engine.py`/`test_episodic_memory.py`/`test_runtime_demo.py` (own temp paths, already safe) | `test_llm_dashboard.py`/`test_verification_dashboard.py`/`test_routing_dashboard.py`/`test_proactive.py`/`test_production_launcher.py` (construct but do not currently publish a real `user_utterance`) |
| `episodic_memory.json` | `EpisodicMemoryStore` | `EpisodicMemoryStore.save()` | `EPISODIC_MEMORY_FILE` | `config/episodic_memory.json` | same as above | fresh, at save-time | none observed this sprint (no register_all_modules()-caller currently sends meaningful-experience-shaped text) | same 6 register_all_modules() callers - latent risk if a future utterance matches detection |
| `long_term_memory.json` | `luno.memory` | `memory._save()` | `LONG_TERM_MEMORY_FILE` | `config/long_term_memory.json` | `monkeypatch.setattr(config, "LONG_TERM_MEMORY_FILE", ...)` (new, via fixture) | fresh, at save-time | none observed this sprint (no register_all_modules()-caller sends a "remember" command) | same 6 callers - latent risk |
| `session_summaries.json` | `luno.memory` | `memory._save_session_summaries()` | `SESSION_SUMMARIES_FILE` | `config/session_summaries.json` | same as above | fresh, at save-time | none observed (requires `_on_conversation_ended` with a real LLM summary call) | same 6 callers - latent risk |
| `habit_memory.json` | `HabitMemory` | `HabitMemory._save_locked()` | `HABIT_MEMORY_FILE` | `config/habit_memory.json` | same as above | path resolved at CONSTRUCTION time (inside `register_all_modules()`, fresh each test) | none observed (requires a verified HA turn_on/off during an open arrival window) | same 6 callers - latent risk, included in fixture as trivial extra coverage |
| `reminders.json` | `luno.reminders` | `reminders._save()` | `REMINDERS_FILE` | `config/reminders.json` | same as above | fresh, at save-time | none observed (requires a `set_reminder`-shaped utterance) | same 6 callers - latent risk, included in fixture |
| `verified_facts.json` | `luno.memory_guard.VerifiedFactStore` | `VerifiedFactStore.save()`/internal record calls | none (only `config.DATA_DIR`, or an explicit constructor `path` arg `PlannerBridgeModule` never passes) | `config/verified_facts.json` | **NONE - discovered gap, out of this sprint's explicit 4-file FINAL RULE scope** | at CONSTRUCTION time, via `config.DATA_DIR` | `test_dashboard.py` likely (its "turn on the light" utterance produces a verified fact) | - |
| `vision_memory.sqlite3` | `luno.vision_memory` (module-level singleton) | `VisionMemory` internal writes | none (only `config.DATA_DIR`, or `vm.configure(db_path=...)`) | `config/vision_memory.sqlite3` | **NONE - discovered gap, out of scope.** Also STRUCTURALLY harder: the singleton is created once per PROCESS and only recreated after an explicit `vm.reset()` call - redirecting `config.DATA_DIR` per-test would not reliably isolate it across a whole pytest session | resolved once, cached | possibly, if any test in the whole session is first to call `vm.update()`/similar | - |
| `lights.config.json`/`switches.config.json`/`scripts.config.json`/`environment_triggers.json`/`persona.json`/`apps.json` | various (`devices.py`, `environment_intent.py`, `persona.py`, `desktop_control.py`) | **none found** - confirmed via `grep` for `json.dump`/`"w"`/`def save` in each owning module | various | `config/*.json` | not needed - read-only seed configuration, hand-edited only | n/a | none (never written by Luno at runtime) | all register_all_modules() callers (safe to read the real files) |

**Scope decision:** per this sprint's own HARD SAFETY RULE and FINAL RULE
(explicitly naming exactly `relationship_state.json`/`episodic_memory.json`/
`long_term_memory.json`/`session_summaries.json`), those four are the
mandatory fix target. `habit_memory.json`/`reminders.json` were included
in the same fixture at zero extra cost/risk (same safe `config.X`-attribute,
save/construction-time-read pattern, already had a proper env-var-backed
config constant) - closing two more latent gaps discovered by the same
audit for free. `verified_facts.json` and `vision_memory.sqlite3` are
DOCUMENTED but NOT fixed this sprint: neither has an existing dedicated
env var (only the much broader `config.DATA_DIR`, which also backs
read-only seed configuration - redirecting it would require either a new
production config constant, out of this narrowly-scoped regression-safety
sprint per §14's "production code should not change unless the audit
identifies a genuine production-side safety bug," or would incorrectly
also redirect legitimate read-only fixtures tests rely on). Flagged as
the explicit "Next recommended step."

## `register_all_modules()` Caller Audit

| Caller | Creates persistent stores? | Inherits process env? | Currently writes state? | Requires temp paths? | Cleanup occurs? (pre-fix) |
|---|---|---|---|---|---|
| `main.py` (production) | Yes (by design - this is production) | Yes (by design) | Yes (by design) | No - this IS the real path | n/a - not a test |
| `tests/test_production_launcher.py` | Yes (`RelationshipStore.load()`, `EpisodicMemoryStore` source registration, `HabitMemory()`, `VerifiedFactStore()`, `legacy_memory`/`vm` module-level singletons) | Yes | No (empirically confirmed this sprint - no scenario publishes `user_utterance`) | Yes (defensive - any future scenario could) | No (pre-fix: no redirect at all) |
| `tests/test_dashboard.py` | Same as above | Yes | **Yes (empirically confirmed this sprint)** | Yes | No (pre-fix) |
| `tests/test_llm_dashboard.py` | Same as above | Yes | Not observed (no `user_utterance` publish found) | Yes (defensive) | No (pre-fix) |
| `tests/test_verification_dashboard.py` | Same as above | Yes | Not observed | Yes (defensive) | No (pre-fix) |
| `tests/test_routing_dashboard.py` | Same as above | Yes | Not observed | Yes (defensive) | No (pre-fix) |
| `tests/test_proactive.py` | Same as above | Yes | Not observed directly, but exercises `HabitMemory`/`ProactiveModule` machinery most directly of any test file | Yes (defensive) | No (pre-fix) |

All six are fixed uniformly by the same `tests/conftest.py` autouse
fixture - no per-file code changes were needed (see Fix section of the
final report).
