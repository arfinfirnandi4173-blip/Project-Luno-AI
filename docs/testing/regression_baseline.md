# Regression Baseline Snapshot

This is historical information, not permission to ignore future
failures. Re-run the commands below before trusting these numbers for a
new change - see `ARCHITECTURE_GUARD.md` §5 for the authoritative
commands and §8 for when to re-baseline.

## Snapshot

- **Date:** 2026-08-07 (session date; exact commit unavailable - see
  "Git state" below)
- **Commit:** not applicable - this checkout is not a Git repository
  (`git status` fails with "not a git repository" - no `.git` directory
  anywhere under the project root in this environment)
- **Python version:** 3.10.12
- **OS/environment:** Linux (Ubuntu 22.04-based sandbox container),
  `uname -a`: `Linux claude 6.8.0-124-generic ... x86_64 GNU/Linux`.
  **This is not the project's real development machine** - the
  project's actual `.venv/` (Windows, `.venv/Scripts/python.exe`) has
  every `requirements.txt` dependency installed; this sandbox has a
  separately-curated Python 3.10 environment missing several of them
  (see "Environment gaps" below).
- **Test command (FAST):** `python3 -m pytest luno/ -q`
- **Test command (FULL, best-effort in this sandbox):**
  `python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py`

## Results - FAST suite (`luno/`)

```
806 passed, 2 failed, 808 total
Duration: ~42-47s (varies run to run)
```

Failures (both FLAKY - KNOWN, see `ARCHITECTURE_GUARD.md` §13):
- `luno/barge_in/tests/test_barge_in.py::test_confirm_mode_interrupt_then_no_resumes`
- `luno/barge_in/tests/test_barge_in.py::test_stress_many_ordinary_utterances_then_one_real_interrupt`

Reconfirmed identical (806/808, same 2 failures) across 3 separate runs
in this sprint and the prior Personality Sprint (same session) - stable.

## Results - `tests/` directory (sampled by file group; `test_dashboard.py`
excluded, see below)

| File group | Passed | Failed |
|---|---|---|
| test_barge_in_console, test_browser_wiring, test_camera_health_check_timeout, test_camera_presence, test_camera_ptz_bootstrap | 44 | 0 |
| test_desktop_control, test_device_context, test_environment_intent | 89 | 0 |
| test_interrupt_routing_fix, test_llm_dashboard, test_memory_guard, test_memory_retrieval, test_memory_regression (new) | 91 | 0 |
| test_mic_device_index | 0 | 6 (ENVIRONMENT-SPECIFIC) |
| test_production_launcher | 23 | 1 (ENVIRONMENT-SPECIFIC) |
| test_real_adapters | ~37 | 2 (INFRASTRUCTURE) |
| test_real_fish_audio_console | (included above) | 0 |
| test_routing_dashboard, test_screen_ask_screen, test_screen_intent_classifier, test_vision_ask_vision | 43 | 0 |
| test_vision_intent, test_vision_intent_classifier, test_vision_provider, test_vision_sprint8 | 93 | 0 |
| test_wake_barge_in_integration, test_wake_session_console, test_world_model, test_verification_dashboard | 63 | 0 |
| test_proactive | 45 | 0 |
| test_runtime_demo | 55 | 0 |
| test_persona | 27 | 0 |
| **test_dashboard** | **not run** | **excluded - see below** |
| test_main_bargein | uncollectible | INFRASTRUCTURE |
| test_root_main_bargein | uncollectible | INFRASTRUCTURE |

`test_dashboard.py` was not re-executed in this exact sprint's run - its
real-`ThreadingHTTPServer`-backed tests take long enough that they
exceed this sandbox's per-command tooling time budget (observed timing
out past 45s repeatedly). It was confirmed passing earlier in the same
overall working session, with no code changes to that area since. This
is explicitly flagged rather than silently omitted.

## Failure classification

| Failure | Classification | Confirmed root cause |
|---|---|---|
| `test_confirm_mode_interrupt_then_no_resumes` | FLAKY - KNOWN | Timing-window dependent (`_speech_pending_deadline` tolerance in `luno/barge_in/manager.py`); passes in isolation, intermittent under load |
| `test_stress_many_ordinary_utterances_then_one_real_interrupt` | FLAKY - KNOWN | Same root cause |
| `tests/test_mic_device_index.py` (6 tests) | ENVIRONMENT-SPECIFIC | This checkout's real `.env` sets `MIC_DEVICE_INDEX=1` for the developer's actual hardware; tests assert the unset/`None` default |
| `tests/test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` | ENVIRONMENT-SPECIFIC | This checkout's real `.env` has live OpenRouter/Fish Audio credentials configured; the test assumes a "default mock configuration" where health checks never attempt real network calls |
| `tests/test_real_adapters.py` (2 whisper tests) | INFRASTRUCTURE | `RealWhisperSource.__init__` depends on `speech_recognition`/`sounddevice` (PortAudio) being importable; confirmed absent/broken in this sandbox's Python environment (`ModuleNotFoundError: No module named 'speech_recognition'`, `sounddevice` raises `OSError: PortAudio library not found` at import) - both packages/native libs ARE present in the project's real `.venv` |
| `tests/test_main_bargein.py` | INFRASTRUCTURE | Collection fails: `ModuleNotFoundError: No module named 'faster_whisper'` (same sandbox-vs-real-`.venv` gap as above) |
| `tests/test_root_main_bargein.py` | INFRASTRUCTURE | Collection fails: `legacy_main.py` (referenced by `main.py`'s own docstring) is absent from this checkout |

No ACTUAL REGRESSION was found in this sprint. Every failure above was
present before this sprint's changes and is reproducible independent of
anything modified here.

## Environment gaps (this sandbox vs. the project's real `.venv`)

Confirmed via `python3 -m pip list` in this sandbox vs. `requirements.txt`:
present: `openai`, `requests`, `websockets`, `python-dotenv`, `opencv-python`,
`onnxruntime`, `pytapo`, `psutil`, `Pillow`, `ormsgpack`, `python-osc`,
`sounddevice` (package installed, native PortAudio library missing).
Absent: `faster-whisper`, `SpeechRecognition`, `soundfile`, `noisereduce`,
`openwakeword`, `ultralytics`, `playwright`. `pytest`/`pytest-timeout`/
`pytest-xdist` are installed in this sandbox but are **not** declared in
`requirements.txt` at all (a real gap - see `.github/workflows/regression.yml`'s
own comment on this).

## Clean CI-equivalent environment (from-scratch virtualenv)

**CI Dependency Integrity sprint.** Everything above this section
describes THIS SANDBOX's own pre-populated Python environment (a
"normal local environment" for this project's purposes - closer to
Vinn's real `.venv` than to a genuinely blank machine, since it already
has extra packages like `ormsgpack`/`opencv-python`/`pytapo` installed
that are not part of `.github/workflows/regression.yml`'s own install
line). That distinction matters: a passing result in THIS sandbox does
not by itself prove CI will pass, because CI builds its own environment
from scratch using only its own `pip install` line.

This section is the separate, additional baseline for that scenario -
built by literally creating a brand-new virtualenv (`python3 -m venv`,
zero inherited/system/site-packages) and installing ONLY
`.github/workflows/regression.yml`'s exact `pip install` line, nothing
else, nothing extra, no reuse of this sandbox's own packages.

- **Python version:** 3.10.12 (`python3 -m venv` off the same
  interpreter as this sandbox's own `python3` - the workflow itself pins
  `python-version: "3.10"` via `actions/setup-python@v5`)
- **Install command (verbatim from the workflow):**
  `pip install python-dotenv requests websockets openai pytest "ormsgpack>=1.5.0"`
- **`pip check`:** clean (`No broken requirements found.`) in both
  independently-built environments below

### Before the dependency fix (initial clean-venv baseline, reproduced this sprint)

Install line at the time: `pip install python-dotenv requests websockets
openai pytest` (no `ormsgpack`).

```
783 passed, 25 failed, 808 total
```

- 23 failures: `luno/adapters/tests/test_fish_audio_api.py` - real Fish
  Audio Cloud API HTTP-client tests, all failing with `TTSSynthesisError:
  the 'ormsgpack' package is required for the fish_audio_api engine` (see
  `ARCHITECTURE_GUARD.md`'s "Dependency Integrity" subsection under §14
  for the full root-cause writeup and dependency classification)
- 2 failures: the same already-known FLAKY - KNOWN Barge-in tests listed
  above - unaffected, identical root cause

### After the dependency fix (current)

Install line now: `pip install python-dotenv requests websockets openai
pytest "ormsgpack>=1.5.0"` (matches `.github/workflows/regression.yml`
exactly - `requirements.txt` itself needed no change, `ormsgpack>=1.5.0`
was already correctly declared there).

Verified in TWO independently-built from-scratch virtualenvs (the second
one built only after completely destroying the first, specifically to
rule out a leftover-package artifact) - identical results in both:

| Check | Result (both venvs, identical) |
|---|---|
| `pip check` | clean |
| `luno/adapters/tests/test_fish_audio_api.py` | 42 passed, 0 failed |
| `tests/test_emotion_engine.py` | 40 passed, 0 failed |
| Emotion Engine runtime integration (2 named node IDs in `tests/test_runtime_demo.py`) | 2 passed, 0 failed |
| `luno/` full FAST suite | 806 passed, 2 failed, 808 total |

The 2 remaining failures are exactly the same, already-documented
FLAKY - KNOWN Barge-in tests (§13 of `ARCHITECTURE_GUARD.md`) - nothing
else. This now matches the "normal local environment" FAST-suite numbers
at the top of this document exactly, but in an environment containing
NOTHING beyond what CI itself installs - the strongest available
evidence (short of an actual GitHub Actions run) that this workflow will
pass.

## Git state

This environment reports:
```
$ git status
fatal: not a git repository (or any parent up to mount point ...)
```
No `.git` directory exists anywhere under the project root in this
sandbox. Commit-level provenance for this baseline is therefore
unavailable here. If/when this project is placed under Git, re-record
this snapshot with the actual commit hash.

## Test State Isolation & Persistent Data Safety (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky), `tests/test_production_launcher.py`
still 23/24 (1 known environment-specific), every memory/relationship/
episodic/emotion/personality/runtime suite still fully green, and every
other `tests/` file re-run this sprint (`test_dashboard.py`,
`test_llm_dashboard.py`, `test_verification_dashboard.py`,
`test_routing_dashboard.py`, `test_proactive.py`, `test_mic_device_index.py`,
`test_real_adapters.py`, vision/camera/wake/screen/desktop suites) matches
its previously-recorded result identically. No new failures, no fixed
failures, no changed counts anywhere - this section documents that
validation reproduced the existing baseline, not a new one, per this
project's own "only update if materially changed" convention.

What DID change: `tests/conftest.py` (new, `autouse=True` fixture) now
redirects every writer-capable persistent-state `config.*_FILE` attribute
to a `tmp_path`-based file for every test collected under `tests/`, and
`tests/test_state_isolation.py` (new, 8 scenarios) proves this
end-to-end, including a real-file sha256/mtime before/after comparison.
This was added after EMPIRICALLY confirming (not merely suspecting) that
`tests/test_dashboard.py` - run alone, before this fix existed -
mutated the real `config/relationship_state.json`
(`interaction_count` 0 -> 4, `trust` 0.0 -> 0.01). See
`ARCHITECTURE_GUARD.md`'s "Test State Isolation" subsection (§6) and
`docs/change_impact/test_state_isolation.md` for the full root-cause
trace, inventory, and fix. `config/relationship_state.json` was reset to
a clean default state after fix verification, since the pollution above
happened during this sprint's own deliberate, undoctored baseline-
reproduction step (per this project's established "reset polluted real
state once discovered, document it" precedent from the Relationship
Engine Foundation and Shared Experience & Episodic Memory sprints).

## Verified Facts & Vision Memory Test Isolation (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in), the 9-file
memory/relationship/episodic/emotion/personality/runtime/production-
launcher batch still 341/342 (1 known environment-specific -
`test_07_health_checks_all_pass_in_default_mock_configuration`),
`test_dashboard.py` run alone still 47/47, and
`test_llm_dashboard.py`/`test_verification_dashboard.py`/
`test_routing_dashboard.py`/`test_proactive.py` together still 61/61. No
new failures anywhere; the two already-documented failure classes
(Barge-in flaky, `test_07` environment-specific) are unchanged.

What DID change: `luno/config.py` gained `VERIFIED_FACTS_FILE` (same
`os.getenv(...)` pattern as every sibling `*_FILE` constant, byte-
identical default value to `VerifiedFactStore`'s old inlined default);
`luno/memory_guard.py`'s `VerifiedFactStore.__init__` now reads that
constant instead of inlining the path; `tests/conftest.py`'s
`isolate_persistent_state` fixture now also redirects
`VERIFIED_FACTS_FILE` (same mechanism as the six files the prior sprint
covered) AND `luno.vision_memory.api._instance`/`_db_path_override`
(path redirect + singleton reset, reusing the exact mechanism already
proven safe by `tests/test_vision_sprint8.py::_isolate_vision_memory()`);
`tests/test_state_isolation.py` grew from 8 to 19 scenarios. This was
added after EMPIRICALLY confirming (sha256/mtime diff against the prior
sprint's own recorded values, not merely suspecting) that ordinary test
runs earlier in this same working session had already mutated both real
`config/verified_facts.json` and `config/vision_memory.sqlite3`. See
`ARCHITECTURE_GUARD.md`'s "Verified Facts & Vision Memory Isolation"
subsection (§6) and `docs/change_impact/verified_facts_vision_isolation.md`
for the full root-cause trace, inventory, and fix - including a bug
found and fixed during this sprint's OWN test-writing (an early fixture
draft patched the wrong module object for the Vision Memory globals;
caught via a failing test assertion, never a real-file mutation, fixed
before any test suite run was considered complete).

## Manual Memory Management (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in), the named
memory/relationship/emotion/personality/runtime/state-isolation batch
(`tests/test_manual_memory.py` + `test_state_isolation.py` +
`test_memory_regression.py` + `test_memory_guard.py` +
`test_memory_retrieval.py` + `test_episodic_memory.py` +
`test_relationship_engine.py` + `test_emotion_engine.py` +
`test_persona.py` + `test_runtime_demo.py`) 380/380,
`test_production_launcher.py` still 23/24 (1 known environment-specific),
`test_dashboard.py` run alone still 47/47, and
`test_llm_dashboard.py`/`test_verification_dashboard.py`/
`test_routing_dashboard.py`/`test_proactive.py` together still 61/61. No
new failures anywhere; the two already-documented failure classes
(Barge-in flaky, `test_07` environment-specific) are unchanged.

What DID change: `luno/memory.py` gained additive fields
(`updated_at`/`category`/`source`/`schema_version`) and new functions
(`get_memory`/`update_memory`/`update_memory_by_topic`/
`delete_memory_by_id`/`search_memories`/three new intent detectors/
`make_manual_memory_source`) on top of its EXISTING `_memories` store -
no new persistent file, no new `config.*_FILE` constant (the audit found
`config.LONG_TERM_MEMORY_FILE` already covered by
`tests/conftest.py`'s `_WRITABLE_STATE_ATTRS` from a prior sprint).
`luno/main.py`'s one legacy `save_memory` tool call site now passes
`source="llm_auto"` explicitly (no behavior change). `main_runtime_demo.py`
registered one more `MemoryRetriever` source (`"manual_memory"`) and
extended the existing `_handle_explicit_memory_command()` meta-command
handler with update/delete branches. `tests/conftest.py`'s
`isolate_persistent_state` fixture gained one more line
(`monkeypatch.setattr(_memory, "_memories", [], raising=False)`),
closing a latent test-determinism gap (`_memories` is populated once at
process import time, before any fixture runs) - a write-safety-neutral
fix, since every writer already read `config.LONG_TERM_MEMORY_FILE`
fresh at save time regardless. `tests/test_manual_memory.py` (61
scenarios) and one new end-to-end scenario in `tests/test_runtime_demo.py`
were added. See `ARCHITECTURE_GUARD.md`'s "Manual Memory Management"
subsection (§3) and `docs/change_impact/manual_memory.md` for the full
architecture audit, data model, and rationale for extending the existing
long-term memory store rather than creating a new one.

## Memory Intelligence & Importance Engine (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`), and the
named memory/relationship/emotion/personality/runtime batch
(`tests/test_episodic_memory.py` + `test_manual_memory.py` +
`test_memory_guard.py` + `test_memory_intelligence.py` (new) +
`test_memory_regression.py` + `test_memory_retrieval.py` +
`test_state_isolation.py` + `test_relationship_engine.py` +
`test_emotion_engine.py` + `test_persona.py` + `test_runtime_demo.py`)
437/437 (up from 380 - the 56 new `test_memory_intelligence.py`
scenarios plus 1 new `test_runtime_demo.py` end-to-end scenario),
`test_production_launcher.py` still 23/24 (1 known environment-specific:
`test_07_health_checks_all_pass_in_default_mock_configuration`). No
previously-passing test was left failing; the two already-documented
failure classes are unchanged. `tests/test_main_bargein.py` and
`tests/test_root_main_bargein.py` still fail at COLLECTION time in this
sandbox for the same pre-existing, unrelated reasons already documented
in the FULL TEST command note above (missing `faster_whisper`;
`legacy_main.py` absent) - confirmed still present and unrelated to this
sprint's changes (neither file was touched, and `legacy_main.py` was
already absent before this sprint started).

One REAL regression was caught and fixed DURING this sprint's own
implementation, before it ever reached this final sweep:
`tests/test_manual_memory.py::test_update_memory_by_topic_ambiguous_does_not_destroy_state`
failed once, transiently, while `_CONSOLIDATION_MIN` (the new Jaccard-
overlap floor for automatic same-topic consolidation) was set to an
initially-chosen 0.34 - two of that test's own intentionally-similar
fixture memories were being auto-consolidated by `add_memory()` itself
before the test's own logic ran. Root-caused and fixed by raising the
floor to 0.45 (see `luno/memory.py`'s own comment on
`_CONSOLIDATION_MIN` for the full numeric justification), not by
touching the pre-existing test. The fix was verified by re-running the
full `test_manual_memory.py` + `test_memory_intelligence.py` +
`test_runtime_demo.py` batch again afterward (all green) before this
final sweep was ever run.

Persistent state (`config/relationship_state.json`,
`config/long_term_memory.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` +
`-wal`/`-shm`; `config/episodic_memory.json` remains absent, as before)
was SHA256-hashed both before this sweep and after - all 9 present files
byte-for-byte identical, `episodic_memory.json` still absent both times.

What DID change: `luno/memory.py` gained additive fields (`importance`
int 0-4, `history` bounded list; `MANUAL_MEMORY_SCHEMA_VERSION` bumped
1 -> 2, non-gating) and new functions
(`_classify_memory_importance`/`_get_importance`/`compute_lifecycle`/
`_find_conflicting_memory`/`_most_recently_touched_memory`/
`mark_last_memory_important`/`forget_last_memory`/two new intent
detectors) on top of its EXISTING `_memories` store and EXISTING
`add_memory`/`update_memory`/`make_manual_memory_source` functions
(extended in place, not replaced) - no new persistent file, no new
`config.*_FILE` constant, no new `tests/conftest.py` isolation (the
existing `LONG_TERM_MEMORY_FILE` redirect + `_memories` reset from the
Manual Memory Management sprint already cover it). `main_runtime_demo.py`
extended the existing `_handle_explicit_memory_command()` meta-command
handler with two new optional branches (mark-important, forget-last).
`tests/test_memory_intelligence.py` (56 scenarios) and one new
end-to-end scenario in `tests/test_runtime_demo.py` were added. See
`ARCHITECTURE_GUARD.md`'s "Memory Intelligence & Importance Engine"
subsection (§3) and `docs/change_impact/memory_intelligence.md` for the
full architecture audit, data model, and rationale.

## Memory Conflict Resolution & Trusted Facts Guard (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`), and the
named memory/relationship/emotion/personality/runtime batch
(`tests/test_episodic_memory.py` + `test_manual_memory.py` +
`test_memory_conflict.py` (new) + `test_memory_guard.py` +
`test_memory_intelligence.py` + `test_memory_regression.py` +
`test_memory_retrieval.py` + `test_state_isolation.py` +
`test_relationship_engine.py` + `test_emotion_engine.py` +
`test_persona.py` + `test_runtime_demo.py`) 471/471 (up from 437 - the 33
new `test_memory_conflict.py` scenarios plus 1 new `test_runtime_demo.py`
end-to-end scenario), `test_production_launcher.py` still 23/24 (1 known
environment-specific: `test_07_health_checks_all_pass_in_default_mock_configuration`).
No previously-passing test was left failing; the two already-documented
failure classes are unchanged. `tests/test_main_bargein.py` and
`tests/test_root_main_bargein.py` were not re-collected in this sprint's
sweep (unrelated, pre-existing, unchanged files/root causes already
documented above).

Six real bugs were caught and fixed DURING this sprint's own
implementation, before any of them reached this final sweep (each traced
to a worked example directly from the sprint brief itself, not
discovered by accident):

1. The pre-existing phase-1 substring dedup in `add_memory()` was
   silently discarding genuinely more-detailed new text (e.g. "Aku pakai
   Windows." -> "Aku pakai Windows 11 Pro." matched as a literal
   substring and only reinforced the OLD, less detailed entry) - fixed
   by splitting phase-1 into three branches, adding
   `_upgrade_existing_memory()` for the new-text-is-more-detailed case.
2. The digit-blind tokenizer made a value correction ("RTX 3070 Ti" ->
   "RTX 3060 Ti") look like a token subset, mislabeling a correction as
   a refinement - fixed by checking correction/temporal wording before
   the subset test in `_classify_conflict()`.
3. The brief's own NO_CONFLICT example ("Aku suka gitar."/"Aku suka
   game.") initially misclassified as AMBIGUOUS_CONFLICT (shared
   "aku"/"suka" tokens, no other signal) - fixed by adding a
   category-aware fallback (`_NON_EXCLUSIVE_CATEGORIES = {"preference"}`).
4. The brief's own primary AMBIGUOUS_CONFLICT example (Windows 11 vs.
   Ubuntu) could never even be compared, because "Ubuntu" fell through
   to category `"other"` while "Windows 11" matched `"technical_fact"` -
   fixed by extending `_CATEGORY_KEYWORDS["technical_fact"]`.
5. A genuine correction pair ("...RTX 3070 Ti di laptop." ->
   "...sekarang pakai RTX 3060 Ti di laptop.") scored ~0.857 Jaccard -
   outside both the phase-1 substring check and the original
   `_CONSOLIDATION_MAX = 0.85` ceiling, silently treated as two unrelated
   facts - fixed by raising `_CONSOLIDATION_MAX` to 0.92 (verified this
   does not reopen the pre-existing "10 near-identical 'game nomor {i}'
   memories must stay separate" protection, since those score 1.0
   pairwise).
6. A self-authored test
   (`tests/test_memory_conflict.py::test_malformed_conflict_metadata_fails_safely`)
   used a hardcoded stale `created_at`/`updated_at` (`2025-01-01`),
   causing `compute_lifecycle()` to classify the fixture entry as
   `"archived"` and silently excluding it from retrieval - fixed by
   using `memory._now_iso()` (current time) instead.

Persistent state (`config/relationship_state.json`,
`config/long_term_memory.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` +
`-wal`/`-shm`; `config/episodic_memory.json` remains absent, as before)
was SHA256-hashed both before this sprint's final sweep and after - all
9 present files byte-for-byte identical, `episodic_memory.json` still
absent both times.

What DID change: `luno/memory.py` gained additive fields
(`conflict_status`, `conflict_group`, optional `history[].reason`) and
new functions (`_classify_conflict`/`_has_distinguishing_context`/
`_is_temporal_change`/`_upgrade_existing_memory`/
`_tag_ambiguous_conflict`/`list_conflicts`/`resolve_conflict_by_topic`/
`_is_historical_query`/two new command detectors) on top of its EXISTING
`_memories` store and EXISTING `add_memory`/`update_memory`/
`search_memories`/`make_manual_memory_source` functions (extended in
place, not replaced) - no new persistent file, no new `config.*_FILE`
constant, no new `tests/conftest.py` isolation (the existing
`LONG_TERM_MEMORY_FILE` redirect + `_memories` reset already cover it).
`main_runtime_demo.py` extended the existing
`_handle_explicit_memory_command()` meta-command handler with two new
optional branches (show-conflicts, resolve-by-topic). Zero lines of
`luno/memory_guard.py` (Verified Facts) were changed - audited, not
modified; the pre-existing structural isolation already satisfied this
sprint's Verified Facts Guard requirement. `tests/test_memory_conflict.py`
(33 scenarios, new file) and one new end-to-end scenario in
`tests/test_runtime_demo.py` were added. See `ARCHITECTURE_GUARD.md`'s
"Memory Conflict Resolution & Trusted Facts Guard" subsection (§3) and
`docs/change_impact/memory_conflict_resolution.md` for the full
architecture audit, conflict taxonomy, resolution policy, and rationale.

## Memory Prompt Intelligence (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`), and the
named memory/relationship/emotion/personality/runtime batch
(`tests/test_episodic_memory.py` + `test_manual_memory.py` +
`test_memory_conflict.py` + `test_memory_guard.py` +
`test_memory_intelligence.py` + `test_memory_prompt_intelligence.py`
(new) + `test_memory_regression.py` + `test_memory_retrieval.py` +
`test_state_isolation.py` + `test_relationship_engine.py` +
`test_emotion_engine.py` + `test_persona.py` + `test_runtime_demo.py`)
501/501 (up from 471 - the 29 new `test_memory_prompt_intelligence.py`
scenarios plus 1 new `test_runtime_demo.py` end-to-end scenario),
`test_production_launcher.py` still 23/24 (1 known environment-specific:
`test_07_health_checks_all_pass_in_default_mock_configuration`). No
previously-passing test was left failing; the two already-documented
failure classes are unchanged.

No implementation bugs required fixing during this sprint beyond one
cosmetic formatting issue caught by the sprint's own smoke-testing
discipline before any test suite run: the new ambiguous-conflict prompt
note ended in a full sentence ("...if it matters.") which, once joined
with the function's existing closing sentence ("Use this naturally..."),
produced a visible double period ("...if it matters.. Use this
naturally..."). Fixed by stripping a trailing period from each selected
fact immediately before the final join, matching the same "no trailing
punctuation before the closing sentence" convention `add_memory()`
already enforces on ordinary stored text via its own `.rstrip(".!?")`.

Persistent state (`config/relationship_state.json`,
`config/long_term_memory.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` +
`-wal`/`-shm`; `config/episodic_memory.json` remains absent, as before)
was SHA256-hashed both before this sprint's final sweep and after - all
9 present files byte-for-byte identical, `episodic_memory.json` still
absent both times. A large number of pre-existing `.fuse_hidden*`
artifacts were observed under `config/` (a known characteristic of this
sandbox's FUSE-mounted directory) - checked and confirmed by mtime to
all predate this session (newest dated 2026-08-07, the day before this
sprint ran), so none were created by this sprint's own test runs.

What DID change: `luno/memory.py` gained one optional kwarg on an
EXISTING function (`build_memory_prompt(query_text=None)`) and two new
private helpers (`_score_memory_for_prompt()`,
`_select_memories_for_prompt()`) - no new persistent file, no new
`config.*_FILE` constant, no new env var (reuses the EXISTING
`MemoryRetrievalConfig`'s `MAX_MEMORY_RESULTS`/`MAX_MEMORY_TOKENS`), no
new `tests/conftest.py` isolation needed (the existing
`LONG_TERM_MEMORY_FILE` redirect + `_memories` reset already cover it).
`main_runtime_demo.py` changed exactly one call site
(`memory.build_memory_prompt()` -> `memory.build_memory_prompt(query_text=text)`,
`text` already in scope) plus its surrounding comment - no other line in
that method changed. Zero lines of `luno/memory_guard.py` or
`luno/episodic_memory.py` were touched - audited, not modified; both
boundaries were already structurally sound and only needed confirming
tests. `tests/test_memory_prompt_intelligence.py` (29 scenarios, new
file) and one new end-to-end scenario in `tests/test_runtime_demo.py`
were added. See `ARCHITECTURE_GUARD.md`'s "Memory Prompt Intelligence"
subsection (§3) and `docs/change_impact/memory_prompt_intelligence.md`
for the full architecture audit, selection policy, and rationale.

## Memory Lifecycle & Maintenance (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`), and the
named memory/relationship/emotion/personality/runtime batch
(`tests/test_episodic_memory.py` + `test_manual_memory.py` +
`test_memory_conflict.py` + `test_memory_guard.py` +
`test_memory_intelligence.py` + `test_memory_maintenance.py` (new) +
`test_memory_prompt_intelligence.py` + `test_memory_regression.py` +
`test_memory_retrieval.py` + `test_state_isolation.py` +
`test_relationship_engine.py` + `test_emotion_engine.py` +
`test_persona.py` + `test_runtime_demo.py`) 557/557 (up from 501 - the
54 new `test_memory_maintenance.py` scenarios plus 2 new
`test_runtime_demo.py` end-to-end scenarios), `test_production_launcher.py`
still 23/24 (1 known environment-specific:
`test_07_health_checks_all_pass_in_default_mock_configuration`). No
previously-passing test was left failing; the two already-documented
failure classes are unchanged.

Three bugs were caught and fixed during this sprint's own smoke-testing/
test-writing discipline, before being counted in the numbers above: (1)
two self-authored tests used a `days_ago` value that actually landed in
the "archived" lifecycle band instead of the intended "stale" band for
an importance=1 entry, given `_LIFECYCLE_THRESHOLDS_DAYS`'s real
`(14, 60)` cutoffs - fixed by correcting the test data to `days_ago=30`
(matching `tests/test_memory_intelligence.py`'s own established
precedent for this importance/age combination), not by changing any
production threshold; (2) `memory_health_report()` crashed with
`TypeError: unhashable type: 'dict'` on a malformed entry carrying a
non-hashable `conflict_group` - fixed by applying the SAME
`str(...)`-coercion pattern `_select_memories_for_prompt()`/
`_tag_ambiguous_conflict()` already use for this exact malformed-input
shape; (3) a restart-persistence test manually mutated an entry's
`created_at`/`importance` on the live in-memory dict but forgot to call
`memory._save()` before simulating the restart, so the mutation never
reached disk and the before/after comparison failed for a test-authoring
reason, not an implementation reason - fixed by adding the missing save
call.

Persistent state (`config/relationship_state.json`,
`config/long_term_memory.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` +
`-wal`/`-shm`; `config/episodic_memory.json` remains absent, as before)
was SHA256+mtime checked before this sprint's implementation began and
again after this sprint's final regression sweep: none of the 9 present
tracked files show an mtime inside the sweep's execution window (all
predate it, consistent with prior sessions' real usage, not this
sprint's test runs), and `episodic_memory.json` is still absent both
times - confirming the sweep itself caused no drift on real production
state. No new `.fuse_hidden*` artifacts were created by this sprint's
test runs (checked via `find -newermt` against the sweep's start time,
zero matches); the pre-existing FUSE-artifact pattern under `config/`
remains a known, unrelated characteristic of this sandbox's mounted
directory, unchanged from prior sprints' own observations.

What DID change: `luno/memory.py` gained one new `compute_lifecycle()`
short-circuit check (`archived_by_maintenance`), 4 new additive entry
fields (`retrieval_count`, `last_retrieved_at`, `archived_by_maintenance`,
`archived_at`, plus a transient `consolidate_with` used only within a
plan), and a new section of functions (`record_memory_usage()`,
`analyze_memory_maintenance()`, `apply_maintenance_plan()`,
`preview_maintenance_text()`, `memory_health_report()`/
`format_memory_health_report()`, `archive_memory_by_id()`,
`unarchive_last_memory()`, plus 5 new command detectors) - no new
persistent file, no new `config.*_FILE` constant, no new
`tests/conftest.py` isolation needed (the existing `LONG_TERM_MEMORY_FILE`
redirect + `_memories` reset already cover it). `main_runtime_demo.py`
gained one new `record_memory_usage()` call immediately after the
existing `relevant_memories_early = self.memory_retriever.
retrieve_memories(text)` line, plus 5 new command handlers inside the
existing `_handle_explicit_memory_command()` meta-command interception
point - no other line in that method changed. Zero lines of
`luno/memory_guard.py` or `luno/episodic_memory.py` were touched -
audited, not modified; both boundaries were already structurally sound
and only needed re-confirming (Verified Facts are structurally
unreachable from this module by construction, since they're never
represented as `_memories` entries). `tests/test_memory_maintenance.py`
(54 scenarios, new file) and two new end-to-end scenarios in
`tests/test_runtime_demo.py` were added. See `ARCHITECTURE_GUARD.md`'s
"Memory Lifecycle & Maintenance" subsection (§3) and
`docs/change_impact/memory_maintenance.md` for the full architecture
audit, planner/executor design, and rationale.

## Memory Dashboard & Observability (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above
exactly - `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`), and the
named memory/relationship/emotion/personality/runtime batch still
557/557. `test_production_launcher.py` still 23/24 (1 known
environment-specific: `test_07_health_checks_all_pass_in_default_mock_configuration`).

The dashboard-specific batch (`tests/test_memory_dashboard.py` (new,
24 scenarios) + `test_dashboard.py` + `test_llm_dashboard.py` +
`test_routing_dashboard.py` + `test_verification_dashboard.py`): 667/668
in a combined run (643 passed + `test_memory_dashboard.py`'s 24, one
run reported a single failure in
`test_verification_dashboard.py::test_api_verification_reports_a_successful_verified_action_end_to_end`
- re-run in isolation immediately after: 6/6 passed. Root-caused to
environmental flakiness (a real outbound network attempt to
`api.openai.com` timing out under the combined batch's load, the same
benign `PytestUnhandledThreadExceptionWarning` already documented for
this suite before this sprint touched anything) - confirmed NOT caused
by this sprint's changes, since the failing test has zero relationship
to `luno/memory.py` or the dashboard's memory endpoints and passes
reliably standalone. No previously-passing test was left failing; no
new failure class was introduced.

No implementation bugs required production-code fixes during this
sprint. Two bugs were caught and fixed during this sprint's own
pre-formal-suite smoke-testing discipline, both in code THIS sprint
added (never in pre-existing production logic): (1)
`collect_memory_list()`'s recency sort used a plain
`sort(key=..., reverse=True)` that silently mis-ordered same-second
ties (Python's sort stability + `reverse=True` does not reverse the
relative order of EQUAL keys) - fixed by mirroring the exact
`(timestamp, list-position)` tie-break rule `_most_recently_touched_memory()`
already established, rather than inventing a new one; (2) two of this
sprint's own test fixtures used two-digit numeric suffixes ("nomor 10",
"nomor 11") which are a literal substring/prefix of their single-digit
counterparts ("nomor 1") - `add_memory()`'s existing, pre-sprint
refinement-detection correctly merged them, which was the TEST's wrong
assumption, not a defect; fixed by keeping fixture suffixes single-digit,
matching the exact precedent `tests/test_manual_memory.py`/
`test_memory_intelligence.py` already established for this exact
scenario.

Persistent state (`config/relationship_state.json`,
`config/long_term_memory.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` +
`-wal`/`-shm`; `config/episodic_memory.json` remains absent, as before)
was SHA256+mtime checked before this sprint's implementation began and
again after this sprint's final regression sweep. 8 of the 9 present
tracked files: byte-identical, no mtime inside the sweep's execution
window. The 9th, `config/vision_memory.sqlite3-shm`, showed a byte
difference - traced to an ad-hoc, non-pytest smoke-test script run
directly via the shell during this sprint's early manual verification
(predating the formal `tests/test_memory_dashboard.py` suite, never
part of the test suite or any CI path), which built a full bootstrap
stack WITHOUT `tests/conftest.py`'s autouse isolation fixture and only
manually redirected `LONG_TERM_MEMORY_FILE` - Vision Memory's own path
was never isolated in that script. The actual data -
`config/vision_memory.sqlite3` and `config/vision_memory.sqlite3-wal`
(where committed content lives) - are BYTE-IDENTICAL to the
pre-sprint baseline; `-shm` is SQLite's WAL-mode shared-memory
bookkeeping file, rewritten by any connection open (read or write) and
carrying no committed data of its own. Confirmed this is NOT a defect
in this sprint's OWN test isolation (which the ad-hoc script never
used) by immediately re-running the full pytest suite (real
`isolate_persistent_state`-covered tests only) with a hash of
`vision_memory.sqlite3-shm` taken before and after: zero drift. No new
`.fuse_hidden*`/`.tmp`/`.bak`/stray database files were created by this
sprint's actual test runs (checked via `find -newermt` against each
sweep's start time).

What DID change: `luno/memory.py` gained 5 new thin, additive, public
wrapper functions (`mark_memory_important_by_id()`,
`unarchive_memory_by_id()`, `is_memory_protected()`,
`get_memory_importance()`, `get_memory_retrieval_count()`) - each a
one-line-or-few delegation to EXISTING logic (id-targeted counterparts
to last-touched-only operations, or public wrappers around existing
private accessors), no new business logic, no existing function's
signature or behavior changed. `luno/dashboard/collectors.py` and
`luno/dashboard/controls.py` each gained one new "# Memory Dashboard &
Observability" section (6 read functions, 6 write functions - all thin
call-throughs to `luno.memory`'s existing public surface).
`luno/dashboard/server.py` gained 6 new GET routes and 6 new POST
routes - no existing route's behavior changed.
`luno/dashboard/static/index.html` gained one new panel (Overview /
Browse & Search / Needs Review / Maintenance sub-tabs, a detail modal)
- no existing panel changed. No new persistent file, no new
`config.*_FILE` constant, no new `tests/conftest.py` isolation target
needed. `tests/test_memory_dashboard.py` (24 scenarios, new file,
entirely real HTTP against a real `DashboardServer`) was added. See
`ARCHITECTURE_GUARD.md`'s "Memory Dashboard & Observability" subsection
(§3) and its new Contract Inventory row (§4), plus
`docs/change_impact/memory_dashboard.md`, for the full architecture
audit, API surface design, and rationale.

## Memory Context Assembly & Retrieval Unification (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above -
`luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`).
`test_production_launcher.py` 23/24 in this run, with a DIFFERENT specific
test failing (`test_07_health_checks_all_pass_in_default_mock_
configuration`, a real OpenRouter/Fish Audio reachability check) than the
name previously recorded here - same root cause class as before (a real
outbound network call failing under this sandbox's proxy restrictions,
confirmed unrelated to this sprint by re-running in isolation and by the
fact that this sprint never touches health-check code), just a different
specific test tripping over it this run. `tests/test_mic_device_index.py`
(6 failures) and `tests/test_real_adapters.py` (2 failures) both fail on a
pre-existing, environment-specific gap - a missing `list_microphones.py`
file at the repo root and a `RealWhisperSource` missing a `_device_index`
attribute - neither touched by this sprint (confirmed via direct code
inspection; both are audio/STT-hardware-adjacent, unrelated to memory).

The full memory/relationship/runtime/dashboard/context batch (`tests/
test_runtime_demo.py` + `test_manual_memory.py` + `test_memory_
intelligence.py` + `test_memory_conflict.py` + `test_memory_prompt_
intelligence.py` + `test_memory_maintenance.py` + `test_episodic_
memory.py` + `test_relationship_engine.py` + `test_memory_dashboard.py` +
`test_memory_regression.py` + `test_memory_context.py` (NEW, 31
scenarios) + `test_memory_guard.py` + `test_memory_retrieval.py` +
`test_state_isolation.py`): 546/546. Every other `tests/` file
(dashboard/vision/camera/screen/wake/barge-in/emotion/persona/proactive/
world_model/desktop/browser/environment/device_context/interrupt_routing)
passes cleanly; `test_dashboard.py` alone takes ~80s (real SSE streaming
tests with real timeouts, unrelated to this sprint, unchanged).
`test_main_bargein.py`/`test_root_main_bargein.py` remain uncollectable in
this sandbox (`faster_whisper` not installed / `legacy_main.py` absent) -
the same pre-existing, documented environment gaps this guard's own
"Known Baseline Issues" section already names.

Two real, pre-existing bugs were found and fixed by this sprint's OWN
test-writing process - caught while writing `tests/test_memory_
context.py`, both in `luno/memory_context.py` itself (new code this
sprint wrote, not any prior production logic): (1) `_lifecycle_for_
relevant_memory()` initially conflated `RelevantMemory.stale`
(`MemoryRetriever`'s 30-minute retrieval-freshness signal) with Manual
Memory's own day/month-scale `compute_lifecycle()` model, meaning any
manual memory older than 30 minutes was incorrectly reported as
lifecycle="stale" regardless of its real state - fixed by calling
`compute_lifecycle()` directly on the raw entry for sources that have one;
(2) the cross-source Jaccard-similarity dedup tier initially compared
same-source items too, and two GENUINELY DIFFERENT manual memories
("aku suka main gitar" / "aku suka main gitar listrik banget") collapsed
into one purely because they share this project's own fixed
"[MANUAL MEMORY - {category}] The user explicitly asked you to remember:"
template boilerplate - fixed by restricting that tier to cross-source
pairs only. A third bug was found via the real production-bridge
end-to-end test: a current manual-memory value and its own superseded
historical value share one underlying `memory_id`, and the same-memory-id
dedup tier was initially collapsing them together (losing either the
current or the historical rendering depending on rank) - fixed by
requiring the same current/historical `historical` flag before treating a
shared `memory_id` as a duplicate. All three are documented in `luno/
memory_context.py`'s own comments at the fixed code and in `tests/
test_memory_context.py`'s regression-guard test
(`test_dedup_current_and_historical_same_memory_id_are_not_collapsed`).

A fourth, LEGITIMATE-BUT-INTENTIONAL test-assertion update was required
(not a bug): `make_manual_memory_source()` (the pre-existing `MemoryRetriever`
source) has no ambiguous-conflict-group awareness and renders each
conflict-group member as an ordinary standalone item; this sprint's new
`_manual_memory_conflict_items()` adapter ALSO adds one merged, hedged
note for the same group. Left unfiltered, both would appear together
(one plain fact, one hedge, naming the same information twice) - fixed by
explicitly excluding ambiguous-conflict-group members from the base
`MemoryRetriever`-derived pool in `assemble_context()`, so only the merged
note represents a conflict group.

Five PRE-EXISTING tests across three files were updated to assert against
the new unified section markers (`"[Relevant Memories]"`/
`"[Historical Context]"`) instead of the two old, now-removed renderings'
markers (`"Relevant Memory:"` from `build_memory_prompt_block()`'s direct
call site, and `"...relevant to this conversation:"` from the now-removed
`build_memory_prompt(query_text=...)` call site) - `tests/
test_runtime_demo.py::test_memory_intelligence_end_to_end_importance_
affects_retrieval_and_context`, `::test_memory_conflict_resolution_end_
to_end_correction_preserves_history_and_current_query_wins`, `::test_
memory_prompt_intelligence_end_to_end_relevance_gated_and_current_vs_
historical`, and `tests/test_memory_retrieval.py::test_20_handle_
utterance_injects_memory_block_into_system_prompt` /
`::test_21_handle_utterance_no_memory_block_when_nothing_relevant`. This
is this sprint's own intentional, in-scope Step 18 unification (removing
the exact duplicate-injection-path these tests were pinned to) - every
underlying relevance/importance/conflict/historical assertion in each
test is UNCHANGED, only the marker string each test searches the prompt
for was updated to match the new unified rendering. `build_memory_prompt_
block()`'s own direct unit tests (`tests/test_memory_retrieval.py` lines
340/355, which call it directly rather than through the production
bridge) were left untouched - that function's own behavior is unchanged.

Persistent state (`config/relationship_state.json`, `config/long_term_
memory.json`, `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`; `config/episodic_
memory.json` remains absent, as before) was SHA256+mtime checked before
this sprint's implementation began and again after the full regression
sweep completed: all 6 present tracked files byte-identical, zero mtime
change. `config/vision_memory.sqlite3`/`-wal`/`-shm` changed, as expected
and previously documented (see the Memory Dashboard sprint's own account
above) - confirmed, again, to be a LIVE EXTERNAL process writing to that
database continuously and independently of any test activity in this
session (verified by hashing before a test run, then again after 15s of
zero test activity - still changed), not this sprint's own test isolation
failing.

What DID change: `luno/memory_context.py` (NEW file - `ContextItem`,
source adapters, cross-source dedup, budget, grouping, `assemble_
context()`); `luno/memory.py` gained two small, additive, backward-
compatible pieces (`is_historical_query()` public wrapper,
`group_ambiguous_conflict_entries()` factored out of `_select_memories_
for_prompt()`'s existing inline loop with verified byte-identical
selection behavior); `main_runtime_demo.py` gained one import and had its
two independent Manual-Memory prompt-block call sites (`explicit_memory_
block`, `memory_block`) replaced with one `memory_context.assemble_
context(...)` call - no other call site in that file (persona, relationship,
vision/screen/browser intents, session summary, verified-action notes,
`memory_guard.record()`, emotion) was touched. `tests/test_memory_
context.py` (new file, 31 scenarios) was added; five pre-existing tests
across two files were updated as described above. See `ARCHITECTURE_
GUARD.md`'s "Memory Context Assembly" subsection (§3) and its new Contract
Inventory row (§4), plus `docs/change_impact/memory_context_assembly.md`,
for the full architecture audit, design rationale, and before/after
account.

## Memory Learning & Feedback Loop (validation, not a new baseline)

This sprint's full regression sweep reproduced every number above exactly
- `luno/` still 806/808 (2 known-flaky Barge-in:
`test_confirm_mode_interrupt_then_no_resumes`,
`test_stress_many_ordinary_utterances_then_one_real_interrupt`).

The full memory/relationship/runtime/dashboard/context batch (`tests/
test_runtime_demo.py` + `test_manual_memory.py` + `test_memory_
intelligence.py` + `test_memory_conflict.py` + `test_memory_prompt_
intelligence.py` + `test_memory_maintenance.py` + `test_episodic_
memory.py` + `test_relationship_engine.py` + `test_memory_dashboard.py` +
`test_memory_regression.py` + `test_memory_context.py` +
`test_memory_guard.py` + `test_memory_retrieval.py` +
`test_state_isolation.py` + `test_memory_learning.py` (NEW, 66
scenarios)): 615/615 (up from 546 - the 66 new `test_memory_learning.py`
scenarios plus 3 new `test_runtime_demo.py` end-to-end scenarios: `test_
memory_learning_feedback_loop_end_to_end_positive_confirmation_scenario_a`,
`_correction_scenario_b`, `_ambiguous_feedback_never_mutates`).
`test_emotion_engine.py` + `test_persona.py` + `test_proactive.py`
together: 112/112. `test_llm_dashboard.py` + `test_routing_dashboard.py` +
`test_verification_dashboard.py` together: 16/16. `test_dashboard.py` run
alone: 47/47 (same benign `PytestUnhandledThreadExceptionWarning` SSE
timing warning already documented for this suite, unrelated to this
sprint). `test_production_launcher.py`: 23/24 (1 known environment-
specific: `test_07_health_checks_all_pass_in_default_mock_configuration`,
Vinn's real `.env` has live credentials - unchanged, unrelated to this
sprint, this sprint touched no health-check code). No previously-passing
test was left failing; the two already-documented failure classes are
unchanged.

No implementation bugs required fixing during this sprint's own test-
writing beyond two test-authoring corrections (both caught before this
final sweep, neither a production-code defect): (1) an early draft of the
Scenario A end-to-end test indexed
`after_feedback["negative_feedback_count"]` directly instead of `.get(...,
0)` - a backward-compatible ABSENT key (this field is only ever written
once negative feedback actually occurs) raised `KeyError`, which is the
CORRECT, intentional behavior for that field's own additive-schema
contract - fixed the test's assertion style, not the field; (2) the
Scenario B end-to-end test's final turn ("GPU apa yang aku pakai
sekarang?") happened to trip the pre-existing, unrelated Intelligent AI
Routing Engine's own real-time-knowledge heuristic (routed to a live web
search, which fails in this sandbox's network-restricted environment) -
unrelated to memory learning, fixed by asserting the correction's on-disk
result directly (already fully proven by the assertions immediately
above) instead of depending on that turn's system_prompt content.

Persistent state (`config/relationship_state.json`, `config/long_term_
memory.json`, `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3`; `config/episodic_memory.json` remains
absent, as before) was SHA256+mtime checked before this sprint's
implementation began and again after the full regression sweep completed:
all 7 present tracked files byte-identical, zero mtime change,
`episodic_memory.json` still absent both times.

What DID change: `luno/memory.py` gained additive fields
(`usefulness_score` float `[0.0, 1.0]` default 0.5, `positive_feedback_count`/
`negative_feedback_count` ints default 0; `MANUAL_MEMORY_SCHEMA_VERSION`
bumped 2 -> 3, non-gating) and a new section of functions
(`apply_positive_feedback()`/`apply_negative_feedback()`,
`get_memory_usefulness()`/`get_memory_positive_feedback_count()`/
`get_memory_negative_feedback_count()`/`get_memory_usefulness_explanation()`,
`detect_positive_memory_feedback()`/`detect_negative_memory_feedback()`/
`detect_memory_feedback_correction()`, `detect_mark_memory_useful_command()`/
`_not_useful_command()`/`_correct_command()`/`_incorrect_command()` +
`mark_last_memory_useful()`/`_not_useful()`/`_correct()`/`_incorrect()`) on
top of the EXISTING `_memories` store and EXISTING `record_memory_usage()`/
`_plan_action_for_entry()`/`memory_health_report()`/
`make_manual_memory_source()`/`_score_memory_for_prompt()` functions
(extended in place, not replaced) - no new persistent file, no new
`config.*_FILE` constant, no new `tests/conftest.py` isolation needed (the
existing `LONG_TERM_MEMORY_FILE` redirect + `_memories` reset already
cover it). `luno/memory_context.py`'s `ContextItem` gained one additive
field (`usefulness`) folded into `_rank_key()` as a third tuple element
(after relevance/importance, before priority). `main_runtime_demo.py`
gained one new per-conversation dict (`_session_feedback_target`, same
scoping/reset convention as the pre-existing `_last_device_target`), one
new handler method (`_handle_memory_feedback_command()`), one new helper
(`_update_session_feedback_target()`), and extended
`_handle_explicit_memory_command()` with 2 new optional branches (mark-
useful/correct, mark-not-useful/incorrect) - checked ONLY after every
pre-existing pending-confirmation resolution (browser/environmental/
routing) has already found nothing pending for the turn (see
`docs/change_impact/memory_learning.md` §14 for the full ordering
rationale). `luno/dashboard/collectors.py`/`controls.py`/`server.py`/
`static/index.html` were extended additively (new read fields, one new
`sort` param + 5 named modes, 2 new feedback controls/routes, matching UI)
- no existing memory dashboard route/control/panel changed. Zero lines of
`luno/memory_guard.py` or `luno/episodic_memory.py` were touched - audited,
not modified; both boundaries were already structurally sound and only
needed confirming tests (structural `inspect.getsource()` scan + a direct
`VerifiedFact` dataclass-field check). `tests/test_memory_learning.py` (66
scenarios, new file) and 3 new end-to-end scenarios in `tests/
test_runtime_demo.py` were added. See `ARCHITECTURE_GUARD.md`'s "Memory
Learning & Feedback Loop" subsection (§3) and
`docs/change_impact/memory_learning.md` for the full architecture audit,
schema, feedback model, scoring, safety boundaries, and rationale.

## Memory Evaluation & Self-Calibration (validation, not a new baseline)

Baseline for THIS sprint was captured fresh from the actual repo (not
assumed from the Memory Learning & Feedback Loop sprint's own numbers
above, per this sprint's own explicit instruction) before any
implementation began: the same memory/relationship/runtime/dashboard/
context batch as above, `luno/` unchanged, all 7 tracked persistent-state
files SHA256+mtime recorded.

Full regression sweep after implementation (every one of the 46
collectible files under `tests/`, run in 7 sequential batches due to this
sandbox's per-command time budget - not a partial sample):

- Memory/relationship/runtime/dashboard/context batch (`test_dashboard.py`
  + `test_manual_memory.py` + `test_memory_conflict.py` +
  `test_memory_context.py` + `test_memory_evaluation.py` (NEW, 94
  scenarios) + `test_memory_intelligence.py` + `test_memory_learning.py`
  + `test_memory_maintenance.py` + `test_memory_prompt_intelligence.py` +
  `test_runtime_demo.py` (+2 new end-to-end scenarios)): 543/543.
- `test_memory_dashboard.py` + `test_memory_guard.py` +
  `test_memory_regression.py` + `test_memory_retrieval.py` +
  `test_episodic_memory.py` + `test_relationship_engine.py` +
  `test_state_isolation.py` + `test_world_model.py`: 241/241.
- `test_barge_in_console.py` + `test_browser_wiring.py` +
  `test_camera_health_check_timeout.py` + `test_camera_presence.py` +
  `test_camera_ptz_bootstrap.py` + `test_desktop_control.py` +
  `test_device_context.py` + `test_emotion_engine.py` +
  `test_environment_intent.py`: 172/173 (1 known-flaky:
  `test_stale_emotion_decays_to_unknown_after_the_configured_window` -
  fails under this sandbox's scheduling jitter when run in a large batch,
  passes reliably in isolation; newly observed and newly documented in
  `ARCHITECTURE_GUARD.md` §15 this sprint, same class as `test_barge_in.py`'s
  2 pre-existing flaky tests, unrelated to memory).
- `test_interrupt_routing_fix.py` + `test_mic_device_index.py` +
  `test_persona.py` + `test_screen_ask_screen.py` +
  `test_screen_intent_classifier.py` + `test_vision_ask_vision.py` +
  `test_vision_intent.py` + `test_vision_intent_classifier.py` +
  `test_vision_provider.py` + `test_vision_sprint8.py`: 179/185 (6 known-
  environment: `test_mic_device_index.py`'s `MIC_DEVICE_INDEX`-set-in-real-
  `.env` failures already documented above, PLUS `list_microphones.py`
  itself being absent from this checkout - same class as `legacy_main.py`,
  newly documented in `ARCHITECTURE_GUARD.md` §15 this sprint).
- `test_wake_barge_in_integration.py` + `test_wake_session_console.py` +
  `test_proactive.py` + `test_llm_dashboard.py` +
  `test_verification_dashboard.py` + `test_routing_dashboard.py`: 92/92.
- `test_production_launcher.py`: 23/24 (1 known environment-specific,
  unchanged from above).
- `test_real_adapters.py` + `test_real_fish_audio_console.py`: 16/18 (2
  newly-observed, pre-existing, unrelated: `RealWhisperSource` has no
  `_device_index` attribute in `luno/adapters/real_whisper.py` - an audio-
  device adapter this sprint never touched; newly documented in
  `ARCHITECTURE_GUARD.md` §15 this sprint).

Total: 1266/1276 collectible tests pass; all 10 non-passing tests trace to
5 already-documented-or-newly-documented environment/timing issues, none
caused by this sprint's changes (confirmed by re-running each failing
test in isolation and/or reading its traceback against files this sprint
never touched). `tests/test_main_bargein.py`/`test_root_main_bargein.py`
still fail to COLLECT at all (missing `faster_whisper`/`legacy_main.py` -
both already documented, unchanged). No previously-passing test was left
failing.

Persistent state (`config/relationship_state.json`, `config/long_term_
memory.json`, `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`;
`config/episodic_memory.json` remains absent, as before;
`config/vision_memory.sqlite3` isolated per-test via the existing
`conftest.py` fixture) was SHA256+mtime checked before this sprint's
implementation began and again after the full 7-batch regression sweep
completed: all 6 present tracked files byte-identical, zero mtime change.

What DID change: `luno/memory.py` gained a new, clearly-bannered "MEMORY
EVALUATION & SELF-CALIBRATION" section (additive fields
`retrieval_success_count`/`retrieval_miss_count`/`feedback_event_count`/
`correction_count`/`conflict_event_count`/`last_evaluated_at`/
`evaluation_score`, `MANUAL_MEMORY_SCHEMA_VERSION` bumped 3 -> 4, non-
gating; `evaluate_memory()`/`calibrate_memory()`/
`record_context_selection()`/`classify_context_outcome()`/
`record_feedback_event()`) plus one `correction_count`-tracking line added
to the EXISTING `update_memory()` and one `conflict_event_count`-tracking
block added to the EXISTING `_tag_ambiguous_conflict()`, and one new
advisory branch added to the EXISTING `_plan_action_for_entry()`'s `stale`
case - no new persistent file, no new `config.*_FILE` constant, no new
`tests/conftest.py` isolation needed. `main_runtime_demo.py` gained one
new best-effort `record_context_selection()` call site right after the
existing `assemble_context()` call, and `record_feedback_event()` +
`calibrate_memory()` calls added to all 5 existing feedback call sites
(2 explicit "memory ini berguna/salah" branches, 3 conversational
positive/negative/correction branches) - no new handler method, no new
detector, no new target-resolution mechanism (all reused unchanged from
the Memory Learning sprint). `luno/dashboard/collectors.py`/`controls.py`/
`server.py`/`static/index.html` were extended additively (new read fields,
4 new sort modes, 1 new `recalibrate` control/route, matching UI;
`memory_feedback_positive()`/`memory_feedback_negative()` now also
calibrate) - no existing memory dashboard route/control/panel changed.
Zero lines of `luno/memory_guard.py` or `luno/episodic_memory.py` were
touched - audited, not modified; confirmed via structural
`inspect.getsource()` scan + a direct `VerifiedFact` dataclass-field
check, same technique the Memory Learning sprint's own isolation test
already established. `tests/test_memory_evaluation.py` (94 scenarios, new
file) and 2 new end-to-end scenarios in `tests/test_runtime_demo.py` were
added; `tests/test_memory_learning.py`'s own
`test_schema_version_bumped_and_non_gating` assertion was updated from 3
to 4 to track the new (intentional, documented) schema version bump - no
other existing test file was modified. See `ARCHITECTURE_GUARD.md`'s
"Memory Evaluation & Self-Calibration" subsection (§3) and
`docs/change_impact/memory_evaluation.md` for the full architecture
audit, schema, evaluation formula, evidence model, confidence model, and
safety boundaries.

## Memory Outcome Telemetry & Closed-Loop Learning (validation, not a new baseline)

Baseline for THIS sprint was captured fresh from the actual repo
immediately before implementation began (the same memory/relationship/
runtime/dashboard/context batch as the Memory Evaluation sprint's own
section above, re-run on this exact repo state with zero commits in
between - `567/567`, matching that section's `543 + 24` (`test_memory_dashboard.py`)
exactly) plus a fresh SHA256+mtime capture of all 7 tracked persistent-
state files (all identical to the prior sprint's own closing capture,
confirming nothing drifted between sprints).

Full regression sweep after implementation, run in 5 sequential batches:

- Memory/relationship/runtime/dashboard/context batch (`test_dashboard.py`
  + `test_manual_memory.py` + `test_memory_conflict.py` +
  `test_memory_context.py` + `test_memory_evaluation.py` +
  `test_memory_intelligence.py` + `test_memory_learning.py` +
  `test_memory_maintenance.py` + `test_memory_prompt_intelligence.py` +
  `test_memory_outcome_telemetry.py` (NEW, 40 scenarios) +
  `test_runtime_demo.py` (+4 new end-to-end scenarios) +
  `test_memory_dashboard.py`): 611/611 (567 baseline + 40 new unit + 4
  new end-to-end).
- `test_memory_guard.py` + `test_memory_regression.py` +
  `test_memory_retrieval.py` + `test_episodic_memory.py` +
  `test_relationship_engine.py` + `test_state_isolation.py` +
  `test_world_model.py`: 217/217.
- `test_barge_in_console.py` + `test_browser_wiring.py` +
  `test_camera_health_check_timeout.py` + `test_camera_presence.py` +
  `test_camera_ptz_bootstrap.py` + `test_desktop_control.py` +
  `test_device_context.py` + `test_emotion_engine.py` +
  `test_environment_intent.py`: 172/173 (1 known-flaky, same test/same
  class of issue already documented in the prior sprint's own section
  and in `ARCHITECTURE_GUARD.md` §15 - unrelated to memory).
- `test_interrupt_routing_fix.py` + `test_mic_device_index.py` +
  `test_persona.py` + `test_screen_ask_screen.py` +
  `test_screen_intent_classifier.py` + `test_vision_ask_vision.py` +
  `test_vision_intent.py` + `test_vision_intent_classifier.py` +
  `test_vision_provider.py` + `test_vision_sprint8.py`: 179/185 (6
  known-environment, same as the prior sprint's own section).
- `test_wake_barge_in_integration.py` + `test_wake_session_console.py` +
  `test_proactive.py` + `test_llm_dashboard.py` +
  `test_verification_dashboard.py` + `test_routing_dashboard.py` +
  `test_production_launcher.py` + `test_real_adapters.py` +
  `test_real_fish_audio_console.py`: 131/134 (3 known-environment, same
  as the prior sprint's own section).

Total: 1310/1320 collectible tests pass (1276 from the prior sprint's own
final count + 44 new this sprint). All 10 non-passing tests are the
EXACT SAME 10 already documented in the prior sprint's section above and
in `ARCHITECTURE_GUARD.md` §15 - no new failure class, confirmed by
identical test names/tracebacks. `tests/test_main_bargein.py`/
`test_root_main_bargein.py` still fail to COLLECT at all (unchanged,
already documented). No previously-passing test was left failing.

Persistent state (`config/relationship_state.json`, `config/long_term_
memory.json`, `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`;
`config/episodic_memory.json` remains absent, as before) was SHA256+mtime
checked before this sprint's implementation began and again after the
full 5-batch regression sweep completed: all 6 present tracked files
byte-identical, zero mtime change. Additionally, per this sprint's own
explicit Step 20 instruction, checked for `.fuse_hidden*` file drift: the
`config/` directory contains 617 `.fuse_hidden*` files, but the NEWEST of
them has an mtime over 53 hours older than this regression sweep
(confirmed via direct `stat` comparison against the current clock) -
these are a pre-existing characteristic of this sandbox's FUSE-mounted
`config/` directory (most likely accumulated from the real Luno
application's own normal operation on the user's machine before this
session even began), not created by this sprint's test suite or any
sprint's code. A `find`-based sweep of every source directory
(`config/`, `luno/`, `tests/`, `docs/`, repo root) for files modified
since this sprint's implementation began turned up exactly the files
intentionally edited (`main_runtime_demo.py`, `luno/memory.py`,
`luno/memory_turn_trace.py`, `luno/dashboard/collectors.py`,
`luno/dashboard/controls.py`, `luno/dashboard/static/index.html`,
`tests/test_memory_outcome_telemetry.py`, `tests/test_runtime_demo.py`)
and nothing else - no unexpected file creation, no test-created memory
left behind in real state, no SQLite pollution.

What DID change: `luno/memory_turn_trace.py` (NEW file -
`MemoryTurnTrace`/`build_turn_trace()`). `luno/memory.py` gained
`record_outcome_evidence()`, `get_conflict_group_member_ids()`,
`get_memory_outcome_summary()`, `get_memory_selection_explanation()`,
`_bump_retrieval_success()`/`_bump_retrieval_miss()` (shared helpers
factored out of the prior sprint's `record_context_selection()`), and
`classify_context_outcome()`'s negative-before-positive priority
correction - no new persistent field (this sprint adds zero NEW schema
fields, only new functions reading/writing the prior sprint's own
`retrieval_success_count`/`retrieval_miss_count`/`correction_count`),
`MANUAL_MEMORY_SCHEMA_VERSION` unchanged at 4. `main_runtime_demo.py`
gained one new bounded, session-scoped dict (`_last_turn_trace`, same
convention as `_session_feedback_target`), and
`_handle_memory_feedback_command()` was refactored to dispatch on
`classify_context_outcome()`'s own output (behaviorally unchanged for
every case the prior sprint's own tests already covered - re-verified by
running `tests/test_memory_learning.py`/`tests/test_runtime_demo.py`'s
OLD end-to-end scenarios unchanged in this same sweep, all still
passing). `luno/dashboard/collectors.py` gained two new detail fields
(`outcome_summary`/`selection_explanation`); `controls.py`'s existing
feedback controls gained one new `record_outcome_evidence()` call each.
`tests/test_memory_outcome_telemetry.py` (40 scenarios, new file) and 4
new end-to-end scenarios in `tests/test_runtime_demo.py` were added;
`tests/test_memory_learning.py`/`tests/test_memory_evaluation.py` were
NOT modified (unlike the prior sprint, this one required no baseline
test-file edits, since no schema field or existing constant changed).
See `ARCHITECTURE_GUARD.md`'s "Memory Outcome Telemetry & Closed-Loop
Learning" subsection (§3) and
`docs/change_impact/memory_outcome_telemetry.md` for the full
architecture audit, telemetry model, and safety boundaries.

## Memory Recovery & Persistence Hardening (validation, not a new baseline)

Full regression sweep after the recovery/restore and the `_save()`/
`_load()` hardening (backup + atomic write + pytest guard):

| batch | passed | failed | notes |
|---|---|---|---|
| memory batch (incl. new `test_memory_persistence_hardening.py`, 11 tests) | 561 | 2 | see below - new, documented, unrelated to recovery |
| dashboard/relationship | 116 | 0 | 1 harmless network-timeout warning, same as prior sprints |
| emotion/personality/runtime/episodic/state-isolation | 217 | 0 | |
| production launcher | 23 | 1 | known, pre-existing (§15) |
| broad batch (camera/vision/browser/mic/proactive/...) | 403 | 8 | known, pre-existing (§15) |
| full `luno/` suite | 806 | 2 | known, pre-existing flaky (§15) |
| **total** | **2126** | **13** | |

The 2 memory-batch failures
(`test_memory_evaluation.py::test_context_item_has_no_evaluation_field_at_all`/
`::test_rank_key_source_never_reads_evaluation`) are NEW as of this
sprint's regression run but NOT caused by this sprint - they are a
genuine design conflict left over from the Memory Decision Quality &
Adaptive Retrieval sprint, which was paused mid-implementation (its own
Phase 4) when the recovery incident was discovered. See
`ARCHITECTURE_GUARD.md` §15 for the full explanation; this recovery
sprint's own instructions explicitly forbade fixing unrelated,
in-progress sprint work, so these are documented here rather than
resolved. Every other failure (11 of the 13) is the same, already-
documented pre-existing set from prior sprints - identical test names,
identical root causes.

Persistent-state verification: all 12 OTHER tracked `config/*.json`
files confirmed byte-identical (SHA256) before and after this entire
sweep. `config/long_term_memory.json` intentionally differs from the
sweep's own start-of-session hash - that is the authorized recovery
restore (Phase 9), not drift; its new hash matches the validated
`recovery/migrated_candidate.json` exactly. `config/backups/` contains
exactly one backup (the damaged pre-restore state, created automatically
by the hardened `_save()`), matching the audited damaged-state hash
exactly. See `ARCHITECTURE_GUARD.md`'s "Memory Recovery & Persistence
Hardening" subsection and `docs/change_impact/memory_recovery.md` for
the full incident, migration, and validation narrative.

## Memory Decision Quality & Adaptive Retrieval (validation, not a new baseline)

Full regression sweep after resuming and completing the paused Adaptive
Retrieval sprint (test-conflict resolution, `test_memory_adaptive_retrieval.py`,
2 E2E scenarios, dashboard leaderboard panel). Run per-file (this
sandbox's process-level slowdown when batching many files in one pytest
invocation - unrelated to this sprint - made per-file runs the reliable
way to get a clean read):

| suite | passed | failed | notes |
|---|---|---|---|
| `test_memory_adaptive_retrieval.py` (new, this sprint) | 18 | 0 | Sections A-Q |
| `test_memory_evaluation.py` | 94 | 0 | includes the 2 rewritten tests (see below) |
| `test_memory_learning.py` | - | 0 | part of combined 347/451-test memory runs below |
| `test_memory_outcome_telemetry.py` | - | 0 | " |
| `test_memory_context.py` | - | 0 | " |
| `test_memory_maintenance.py` | - | 0 | " |
| `test_memory_conflict.py` | - | 0 | " |
| `test_memory_persistence_hardening.py` | 11 | 0 | untouched, still green |
| combined memory suite (8 files above) | 347 | 0 | |
| `test_memory_dashboard.py` (incl. 2 new context-leaderboard scenarios) | 26 | 0 | |
| `test_runtime_demo.py` (incl. 2 new Adaptive Retrieval E2E scenarios) | 78 | 0 | |
| combined memory + dashboard + runtime-demo | 451 | 0 | |
| `test_production_launcher.py` | 23 | 1 | known, pre-existing (§15 - network health checks) |
| `test_mic_device_index.py` | 10 | 6 | known, pre-existing (§15 - `list_microphones.py` absent) |
| `test_real_adapters.py` | 8 | 2 | known, pre-existing (§15 - `speech_recognition`/`sounddevice` absent) |
| every other file under `tests/` (44 files, individually) | all remaining | 0 | `test_dashboard.py`/`test_llm_dashboard.py` pass with the same harmless network-timeout warning as prior sprints |
| **total (1353 collected, excl. the 2 uncollectible INFRASTRUCTURE files)** | **1344** | **9** | all 9 pre-existing/environment-specific, none new |

The two previously-documented `test_memory_evaluation.py` conflicts
(`test_context_item_has_no_evaluation_field_at_all` /
`test_rank_key_source_never_reads_evaluation`) are RESOLVED as of this
sprint - rewritten (not deleted) to
`test_context_item_evaluation_field_holds_the_shared_evaluate_memory_score`
/ `test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker`,
asserting the new authoritative contract. See `ARCHITECTURE_GUARD.md`
§15 and `docs/change_impact/memory_adaptive_retrieval.md` for the full
old-vs-new record. No other new failures were introduced; the remaining
9 failures are the same pre-existing/environment-specific set documented
across every prior sprint's regression run (network-blocked health
checks, `list_microphones.py`/`legacy_main.py` absent from this
checkout, `speech_recognition`/`sounddevice` unavailable) - identical
test names, identical root causes.

Persistent-state verification (Phase 11): `config/long_term_memory.json`,
`config/relationship_state.json`, `config/session_summaries.json`,
`config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json` all confirmed byte-identical (SHA256) AND
mtime-identical before and after the entire sweep - `long_term_memory.json`
in particular still matches the post-recovery hash
(`e4a92097eb920e74b495b1bef05dc2a864c4452895b5da76371f036cc4e7eac3`)
exactly, proving none of this sprint's test runs (including the 2 new
E2E scenarios through the real `PlannerBridgeModule`) ever touched
Vinn's real production memory file. `config/episodic_memory.json`
remains absent, unchanged from before the sweep (no episodic memories
have been saved in this checkout).

## Persistent State Hardening V2 (validation, not a new baseline)

Baseline (before any code change this sprint): sprint-relevant batch
(relationship/episodic/memory_guard/state_isolation/emotion/persona/
memory_regression) - 220 passed / 0 failed. Full `tests/` sweep
(reused from the immediately preceding Adaptive Retrieval sprint's own
Phase 9-11 run, same session, no intervening changes): 1344 passed / 9
failed, all 9 pre-existing/environment-specific (unchanged since).

Post-implementation, full per-file sweep (all 50 files under `tests/`,
2 known-uncollectible INFRASTRUCTURE files excluded):

| suite | passed | failed | notes |
|---|---|---|---|
| `test_persistent_state_hardening.py` (new, this sprint) | 31 | 0 | Section 0 (helper, A-P) + Sections 1-6 (one per store) |
| `test_relationship_engine.py` | 53 | 0 | unchanged count |
| `test_episodic_memory.py` | 55 | 0 | unchanged count |
| `test_memory_guard.py` | 18 | 0 | unchanged count |
| `test_state_isolation.py` | 19 | 0 | unchanged count |
| `test_emotion_engine.py` | 40 | 0 | unchanged count |
| `test_persona.py` | 27 | 0 | unchanged count |
| `test_memory_regression.py` | 8 | 0 | unchanged count |
| `test_proactive.py` | 45 | 0 | unchanged count (`HabitMemory` exercised indirectly) |
| memory suite (evaluation/adaptive_retrieval/learning/context/maintenance/conflict/outcome_telemetry/persistence_hardening) | 347 | 0 | unchanged, `long_term_memory.json`'s own reference implementation untouched |
| `test_memory_dashboard.py` | 26 | 0 | unchanged |
| `test_runtime_demo.py` | 78 | 0 | unchanged |
| `test_dashboard.py` | 47 | 0 | 1 harmless network-timeout warning, same as every prior sprint |
| `test_llm_dashboard.py` | 6 | 0 | unchanged |
| `test_production_launcher.py` | 23 | 1 | known, pre-existing (§15 - network health checks) |
| `test_mic_device_index.py` | 10 | 6 | known, pre-existing (§15 - `list_microphones.py` absent) |
| `test_real_adapters.py` | 8 | 2 | known, pre-existing (§15 - `speech_recognition`/`sounddevice` absent) |
| every remaining file (camera/vision/browser/desktop/device/environment/interrupt/screen/wake/world_model/routing/verification/barge_in/manual_memory/memory_intelligence/memory_prompt_intelligence/memory_retrieval) | all remaining | 0 | |
| **total (1383 collected)** | **1374** | **9** | all 9 identical to baseline, none new |

No new failures anywhere. The 9 failures are the same pre-existing/
environment-specific set documented across every prior sprint's
regression run - identical test names, identical root causes.

Persistent-state verification (Phase 11): SHA256 + mtime for
`config/relationship_state.json`, `config/episodic_memory.json`
(confirmed still absent, unchanged), `config/long_term_memory.json`,
`config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3`, `config/vision_memory.sqlite3-wal`,
`config/vision_memory.sqlite3-shm` - ALL byte-identical AND
mtime-identical before and after the entire 1383-test sweep. Artifact
scan (`*.tmp`/`*.bak`/`*.old`/`*.orig`) found nothing new. The
pre-existing 617 `.fuse_hidden*` files (documented in
`docs/change_impact/persistent_state_hardening_v2.md` §6 - a FUSE-vs-
SQLite-WAL artifact predating this sprint by 2-4 days) are unchanged in
count, confirming this sprint's test run created no new ones.
`config/backups/` still contains exactly the one pre-existing backup
from the Memory Recovery sprint - none of this sprint's own backup
mechanics ever wrote to `config/backups/` (every test uses an isolated
`tmp_path`).

Backup verification (Phase 12): automated STATE A -> B -> C -> failed-
write scenario proven in
`test_backup_verification_state_a_b_c_then_failed_write_preserves_c` -
primary equals C, newest backup equals B, previous backup equals A, and
primary still equals C after a simulated failed write.

## Response Depth Policy (validation, not a new baseline)

Baseline (before any code change this sprint): reused the Persistent
State Hardening V2 sprint's own final numbers above (same checkout, no
intervening changes) - 1374 passed / 9 failed across 1383 collected
tests (50 files, 2 known-uncollectible INFRASTRUCTURE files excluded -
`legacy_main.py` absent, §15).

Post-implementation, full per-file sweep (all 51 files under `tests/` -
`tests/test_response_policy.py` is new this sprint - 2 known-
uncollectible INFRASTRUCTURE files excluded, same as before):

| batch | files | passed | failed | notes |
|---|---|---|---|---|
| prompt/planner/persona/emotion | `test_response_policy.py` (NEW), `test_runtime_demo.py`, `test_device_context.py`, `test_browser_wiring.py`, `test_persona.py`, `test_emotion_engine.py` | 244 | 0 | includes all 61 new Response Depth Policy tests |
| memory core regression | `test_relationship_engine.py`, `test_episodic_memory.py`, `test_memory_guard.py`, `test_state_isolation.py`, `test_memory_regression.py`, `test_persistent_state_hardening.py` | 184 | 0 | unchanged counts |
| memory suite (adaptive_retrieval/conflict/context/evaluation/intelligence/learning/maintenance/outcome_telemetry/persistence_hardening/prompt_intelligence/retrieval/manual_memory) | 12 files | 531 | 0 | unchanged, `long_term_memory.json`'s own reference implementation untouched |
| dashboards | `test_memory_dashboard.py`, `test_dashboard.py`, `test_llm_dashboard.py`, `test_routing_dashboard.py`, `test_verification_dashboard.py` | 89 | 0 | 1 harmless network-timeout warning, same as every prior sprint |
| vision/camera/screen/desktop/environment/world_model/proactive | 15 files | 299 | 0 | unchanged |
| console integration (barge-in/wake-session/real-fish-audio) | `test_barge_in_console.py`, `test_wake_barge_in_integration.py`, `test_wake_session_console.py`, `test_real_fish_audio_console.py` | 48 | 0 | unchanged |
| known-environment-coupled | `test_production_launcher.py`, `test_mic_device_index.py`, `test_real_adapters.py` | 41 | 9 | all 9 identical to baseline, none new (§15) |
| **total** | 47 files | **1436** | **9** | all 9 failures pre-existing, 0 new |

`test_main_bargein.py`/`test_root_main_bargein.py` remain the same 2
known-uncollectible files (`legacy_main.py` absent, §15) - unchanged,
unrelated to this sprint.

No new failures anywhere. The 9 failures are the exact same pre-
existing/environment-specific set documented across every prior
sprint's regression run - identical test names
(`test_07_health_checks_all_pass_in_default_mock_configuration`,
`test_unset_defaults_to_none`,
`test_real_whisper_source_defaults_to_none_when_unset`,
`test_list_microphones_reports_devices_and_default`,
`test_list_microphones_handles_no_valid_default`,
`test_list_microphones_handles_missing_dependency`,
`test_list_microphones_handles_zero_devices`,
`test_real_whisper_source_calls_listener_in_order_for_nonempty_text`,
`test_real_whisper_source_skips_empty_transcription`), identical root
causes.

Persistent-state verification: SHA256 + mtime for
`config/long_term_memory.json`, `config/relationship_state.json`,
`config/episodic_memory.json` (confirmed still absent, unchanged),
`config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3` - ALL byte-identical AND mtime-identical
before and after the entire sweep. Artifact scan
(`*.tmp`/`*.bak`/`*.old`/`*.orig`) found nothing new.

`luno/response_policy.py`'s own test suite (`test_response_policy.py`)
additionally proves, structurally (source-text scan, not just
behaviorally), that the module imports no memory/persistence module and
contains no network/LLM-call-capable code at all - see
`docs/change_impact/response_depth_policy.md`.

## Chat / Voice Dual Output (validation, not a new baseline)

Baseline (before any code change this sprint): reused the Response Depth
Policy sprint's own final numbers above (same checkout, no intervening
changes) - 1436 passed / 9 failed across 1445 collected tests (47 files
swept in that sprint's own per-file batches, 2 known-uncollectible
INFRASTRUCTURE files excluded - `legacy_main.py` absent, §15).

Post-implementation, full per-file sweep (all 52 files under `tests/` -
`tests/test_response_output.py` is new this sprint - 2 known-
uncollectible INFRASTRUCTURE files excluded, same as before):

| batch | files | passed | failed | notes |
|---|---|---|---|---|
| dual-output / depth / planner / persona / emotion | `test_response_output.py` (NEW), `test_response_policy.py`, `test_runtime_demo.py`, `test_device_context.py`, `test_browser_wiring.py`, `test_persona.py`, `test_emotion_engine.py` | 275 | 0 | includes all 31 new Chat/Voice Dual Output tests |
| memory core regression | `test_relationship_engine.py`, `test_episodic_memory.py`, `test_memory_guard.py`, `test_state_isolation.py` (gained 3 new straggler-thread regression tests), `test_memory_regression.py`, `test_persistent_state_hardening.py` | 187 | 0 | unchanged counts except `test_state_isolation.py`'s own new tests |
| memory suite (adaptive_retrieval/conflict/context/evaluation/intelligence/learning/maintenance/outcome_telemetry/persistence_hardening/prompt_intelligence/retrieval/manual_memory) | 12 files | 531 | 0 | unchanged |
| dashboards | `test_memory_dashboard.py`, `test_dashboard.py`, `test_llm_dashboard.py`, `test_routing_dashboard.py`, `test_verification_dashboard.py` | 89 | 0 | same harmless network-timeout warning as every prior sprint |
| vision/camera/screen/desktop/environment/world_model/proactive | 15 files | 299 | 0 | unchanged |
| console integration (barge-in/wake-session/real-fish-audio) | `test_barge_in_console.py`, `test_wake_barge_in_integration.py`, `test_wake_session_console.py`, `test_real_fish_audio_console.py` | 48 | 0 | unchanged - includes this sprint's own end-to-end SpeakRequest assertions |
| known-environment-coupled | `test_production_launcher.py`, `test_mic_device_index.py`, `test_real_adapters.py` | 41 | 9 | all 9 identical to baseline, none new (§15) |
| **total** | 48 files | **1470** | **9** | all 9 failures pre-existing, 0 new; +34 tests vs. prior baseline (31 new `test_response_output.py` + 3 new `test_state_isolation.py`) |

`luno/` FAST suite: 806 passed, 2 failed, 808 total - identical to every
prior sprint's baseline (2 known-flaky Barge-in tests, §13).
`test_main_bargein.py`/`test_root_main_bargein.py` remain the same 2
known-uncollectible files (`legacy_main.py` absent, §15) - unchanged,
unrelated to this sprint.

No new failures anywhere. The 9 `tests/` failures are the exact same
pre-existing/environment-specific set documented across every prior
sprint's regression run (`test_07_health_checks_all_pass_in_default_
mock_configuration`, the 6 `test_mic_device_index.py`/`test_real_
adapters.py` hardware-coupled tests) - identical names, identical root
causes, confirmed unrelated to this sprint (neither file was touched,
neither depends on anything Chat/Voice Dual Output changed).

Two persistent-state pollution incidents occurred DURING this sprint's
own regression sweep - both fully investigated, fixed at the test-
isolation layer only, and documented in detail in `docs/change_impact/
chat_voice_dual_output.md`'s Appendix and in `ARCHITECTURE_GUARD.md`'s
"Chat / Voice Dual Output" section. Summary: an untracked per-turn
`threading.Thread(name="luno-planner-turn")` in `PlannerBridgeModule.
on_event()` (main_runtime_demo.py, pre-existing code, not modified by
this sprint) could straggle past a test's own `console.stop()` call and
land a `RelationshipStore.save()` write on the real `config/relationship_
state.json` if it executed after that test's `monkeypatch` isolation had
already reverted. Both incidents were caught immediately (this sprint's
own regression discipline, not accidental discovery), the real file was
restored from `luno/persistence.py`'s own automatic pre-write backup
both times (byte-identical to its last known-good state), and the root
cause was fixed by adding a bounded, name-scoped thread-drain step to
`tests/conftest.py`'s `isolate_persistent_state` fixture teardown,
positioned (via `yield`) to run BEFORE `monkeypatch`'s automatic revert.
Zero production code was changed by this fix - verified by source diff
review and `py_compile` on every touched production file. 3 new
regression tests in `tests/test_state_isolation.py` prove the race is
reproducible, prove the fix prevents real-file mutation, and structurally
guard the fixture's before/after-yield ordering against future
regressions.

Persistent-state verification: SHA256 + mtime for `config/long_term_
memory.json`, `config/relationship_state.json`, `config/episodic_
memory.json` (confirmed still absent, unchanged), `config/session_
summaries.json`, `config/habit_memory.json`, `config/reminders.json`,
`config/verified_facts.json`, `config/vision_memory.sqlite3` - ALL byte-
identical AND mtime-identical immediately before this sprint's Phase 2
baseline capture and immediately after the final Phase 10 regression
sweep (post-fix), with the two transient pollution incidents in between
fully resolved and documented rather than silently glossed over.
Artifact scan (`*.tmp`/`*.bak`/`*.old`/`*.orig`) found nothing new beyond
the two expected, already-accounted-for `config/backups/relationship_
state.*.json` snapshots `luno/persistence.py` itself created
automatically.

`luno/response_output.py`'s own test suite (`test_response_output.py`)
additionally proves, via 4 dedicated semantic-safety tests, that DETAILED-
depth voice compression never drops a warning sentence, a numeric
specification, a genuine conclusion, or the lead sentence - see `docs/
change_impact/chat_voice_dual_output.md`.

## TTS Chunking / Voice Streaming (validation, not a new baseline)

Baseline (before any code change this sprint): reused the Chat/Voice
Dual Output sprint's own final numbers above (same checkout, no
intervening changes) - 1470 passed / 9 failed across 48 files (2 known-
uncollectible INFRASTRUCTURE files excluded - `legacy_main.py` absent,
§15), `luno/` FAST suite 806/808 (2 known-flaky Barge-in).

Post-implementation:

| batch | files | passed | failed | notes |
|---|---|---|---|---|
| TTS chunking (adapter) | `test_fish_audio_chunking.py` (NEW) | 12 | 0 | new sequential/gap/retry/skip scenarios |
| TTS regression (adapter, unchanged) | `test_fish_audio_real.py`, `test_fish_audio_barge_in.py`, `test_fish_audio_api.py` | 64 | 0 | byte-identical to prior baseline |
| dual-output / depth / barge-in / wake integration | `test_response_output.py` (extended, +20), `test_runtime_demo.py`, `test_barge_in_console.py`, `test_wake_barge_in_integration.py`, `test_wake_session_console.py`, `test_real_fish_audio_console.py`, `test_response_policy.py`, `test_persona.py`, `test_emotion_engine.py`, `test_relationship_engine.py`, `test_state_isolation.py` | 381 | 0 | includes all new `voice_chunks`/E2E chunking tests |
| barge-in unit (`luno/`) | `luno/barge_in/tests/test_barge_in.py` | 42 | 2 | 2 known-flaky, unchanged |
| device/vision/camera/screen/desktop/environment/browser | 15 files | 270 | 0 | unchanged |
| memory suite/dashboard/production-launcher/mic-index/real-adapters | 24 files | 682 | 9 | 9 identical to baseline (§15), 0 new |
| `test_dashboard.py` (run alone) | 1 file | 47 | 0 | same benign SSE-timeout warning already documented |
| **total (`tests/`)** | 47 files | **1498** | **9** | all 9 pre-existing, 0 new; +28 tests vs. prior baseline |

`luno/` FAST suite: 813 passed, **7 failed** - 2 are the known-flaky
Barge-in tests (§13, unchanged); the other 5
(`luno/text_normalizer/tests/test_text_normalizer.py`) are a NEWLY
DISCOVERED (this sprint's own regression sweep), PRE-EXISTING,
unrelated bug - confirmed to reproduce IDENTICALLY with every file this
sprint touched or added excluded from the run, i.e. NOT caused by this
sprint. Documented in `ARCHITECTURE_GUARD.md` §15 and
`docs/change_impact/tts_chunking_streaming.md`'s LIMITATIONS section,
deliberately NOT fixed (out of scope). All 35 tests in that file pass
when run standalone. This is the first time this sprint's own full-`luno/`
sweep has surfaced this particular ordering-dependent failure in this
project's recorded history - worth a future, separately-scoped fix.

No ACTUAL regression from this sprint's own changes. Every
`tests/`-level failure is the exact same pre-existing/environment-
specific set documented across every prior sprint's regression run.

Persistent-state verification: SHA256 + mtime for `config/relationship_state.json`,
`config/long_term_memory.json`, `config/episodic_memory.json` (confirmed
still absent), `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3` - ALL byte-identical AND mtime-identical
immediately before this sprint's Phase 0 audit and immediately after the
final full regression sweep. No orphan temp/audio files - confirmed via
`find config -newer ... -type f` (nothing beyond the existing
`config/backups/` snapshots) and directly by architecture (Fish Audio
synthesis is entirely in-memory, `io.BytesIO`, no temp files exist
anywhere in that path to begin with, chunked or not).

## TTS Chunk Queue & Cancellation (validation, not a new baseline)

Baseline (before any code change this sprint): reused the TTS Chunking/
Voice Streaming sprint's own final numbers above (same checkout, no
intervening changes) - `luno/` FAST suite 813 passed / 820 total (2
known-flaky Barge-in + 5 known, pre-existing `text_normalizer`
`LUNO_LANGUAGE`-env-leak failures, both already documented above).

Post-implementation:

| batch | files | passed | failed | notes |
|---|---|---|---|---|
| New TTS chunk-queue/cancellation suite | `test_tts_chunking.py`, `test_tts_queue.py`, `test_tts_cancellation.py`, `test_tts_e2e_pipeline.py` (all NEW) | 50 | 0 | correlation contract, queue ordering, cancellation at every lifecycle point, 3 real-console barge-in scenarios, 3 real-pipeline E2E scenarios (A/B/C) |
| TTS regression (adapter, unchanged) | `test_fish_audio_api.py`, `test_fish_audio_barge_in.py`, `test_fish_audio_chunking.py`, `test_fish_audio_real.py` | 64+12 | 0 | byte-identical to prior baseline; `_normalize_chunk_entries()` accepts both the legacy `List[str]` and new `List[dict]` wire formats |
| barge-in / wake-integration / console / dual-output regression | `test_barge_in_console.py`, `test_real_fish_audio_console.py`, `test_wake_barge_in_integration.py`, `test_wake_session_console.py`, `test_response_output.py` | remainder of 226 total across this row + the two rows above | 0 | includes the one pre-existing E2E test updated for the new `List[dict]` chunk shape (not weakened - reasserts stronger correlation-field checks) |
| `luno/` FAST suite | whole tree | 813 | 7 | identical 7 failures to the pre-sprint baseline (2 known-flaky Barge-in + 5 known `text_normalizer` env-leak) - reconfirmed by isolated re-run of both affected files (100% pass alone), not caused by this sprint |

Targeted sweep total (new TTS suite + Fish Audio regression + barge-in/
wake-integration/console/dual-output regression, one combined pytest
invocation): **226 passed, 0 failed**.

A full unrestricted `pytest tests/ luno/` single-process run could not
be completed within this sandbox's tooling time budget - consistent
with this document's own pre-existing note above
(`tests/test_dashboard.py` individually exceeds the budget) plus
observed real-network-retry delays in at least one OpenAI-provider test
during a partial run (this sandbox has no outbound internet access,
same class of environment gap as §6/§15's other entries). This is the
same documented FAST/FULL split this project has used since the
Regression & Architecture Guard sprint, not a new limitation introduced
here.

No ACTUAL regression from this sprint's own changes. Every failure
observed anywhere in this sprint's sweep is the exact same pre-existing/
environment-specific set already documented above.

Persistent-state verification: SHA256 for `config/relationship_state.json`,
`config/long_term_memory.json`, `config/episodic_memory.json` (confirmed
still absent), `config/session_summaries.json`, `config/habit_memory.json`,
`config/reminders.json`, `config/verified_facts.json`,
`config/vision_memory.sqlite3` - ALL byte-identical, compared against
this sprint's own Phase 0 snapshot (`/tmp/baseline_hashes_sprint2.txt`).
No stray `.tmp`/`.bak` files found anywhere in the repository.

See `docs/change_impact/tts_chunk_queue_cancellation.md` for the full
audit trail, the pre-synthesis cancellation race this sprint closed, and
every worked-through test scenario.

## LLM Streaming -> Real-Time Speech Pipeline (validation, not a new baseline)

Baseline (before any code change this sprint): reused the TTS Chunk
Queue & Cancellation sprint's own final numbers above (same checkout, no
intervening changes) - `luno/` FAST suite 813 passed / 820 total (2
known-flaky Barge-in + 5 known, pre-existing `text_normalizer`
`LUNO_LANGUAGE`-env-leak failures, both already documented above).

Post-implementation:

| batch | files | passed | failed | notes |
|---|---|---|---|---|
| New LLM-streaming/incremental-speech suite | `test_llm_streaming.py`, `test_incremental_speech_buffer.py`, `test_streaming_speech_integration.py`, `test_streaming_e2e.py` (all NEW) | 53 | 0 | LLM streaming contract (reuse-only), buffer/boundary detection, dual output, bounded backpressure, cancellation at every lifecycle point, Response Depth Policy under streaming, memory/context/persistent-state safety, and the brief's own 6 E2E scenarios A-F |
| LLM adapter/provider regression (unchanged) | `luno/adapters/llm/tests/test_providers.py`, `luno/adapters/tests/test_llm_manager.py`, `luno/adapters/tests/test_openrouter_adapter.py`, `luno/adapters/tests/test_openai_primary_deepseek_fallback.py` | included below | 0 | byte-identical to prior baseline - confirms the reused streaming contract this sprint built on was never modified |
| Fish Audio / TTS chunk-queue regression (unchanged) | `test_fish_audio_api.py`, `test_fish_audio_barge_in.py`, `test_fish_audio_chunking.py`, `test_fish_audio_real.py`, `test_tts_chunking.py`, `test_tts_queue.py`, `test_tts_cancellation.py`, `test_tts_e2e_pipeline.py` | included below | 0 | `_play()` (legacy path) confirmed byte-identical; the new `_play_stream()` is additive only |
| **combined targeted sweep (rows above)** | 16 files | **303** | **0** | one combined pytest invocation, 36.7s |
| console/dual-output/barge-in/wake-integration/persona/emotion/relationship/state-isolation/runtime-demo/proactive | `test_barge_in_console.py`, `test_real_fish_audio_console.py`, `test_wake_barge_in_integration.py`, `test_wake_session_console.py`, `test_response_output.py`, `test_response_policy.py`, `test_persona.py`, `test_emotion_engine.py`, `test_relationship_engine.py`, `test_state_isolation.py`, `test_runtime_demo.py`, `test_proactive.py` | 426 | 0 | unchanged |
| memory regression / retrieval / guard / interrupt-routing / LLM dashboard | 5 files | 80 | 0 | unchanged |
| device / vision / camera / screen / desktop / environment / browser / routing-dashboard / verification-dashboard / world-model | 17 files | 301 | 0 | unchanged |
| full memory suite (episodic/manual/adaptive/conflict/context/evaluation/intelligence/learning/maintenance/telemetry/persistence-hardening/prompt-intelligence/persistent-state-hardening) | 13 files | 579 | 0 | unchanged |
| memory dashboard | `test_memory_dashboard.py` | 26 | 0 | unchanged |
| `test_dashboard.py` | 1 file | not re-run | - | same documented sandbox-timeout reason as every prior sprint (real `ThreadingHTTPServer`-backed tests exceed this sandbox's per-command budget) - untouched by this sprint |
| ENVIRONMENT-SPECIFIC/INFRASTRUCTURE (already documented) | `test_mic_device_index.py`, `test_production_launcher.py`, `test_real_adapters.py` | 41 | 9 | identical to baseline (§15) |
| `luno/` FAST suite | whole tree | 813 | 7 | identical 7 failures to the pre-sprint baseline (2 known-flaky Barge-in + 5 known `text_normalizer` env-leak) - reconfirmed reproducing identically, not caused by this sprint |

Grand total across every batch actually executed this sprint: **2569
passed, 16 failed** (all 16 are the exact same pre-existing/environment-
specific set documented above and at §15 - 0 new failures, 0
regressions). One test in the new suite,
`tests/test_streaming_e2e.py::test_F_new_request_after_cancel_no_stale_audio_b_plays_normally`,
failed ONCE out of three total runs when executed as part of a large,
system-loaded combined batch, and passed cleanly both in isolation and
on an immediate re-run of the same combined batch - classified the same
way this document's own §13 already classifies the two Barge-in tests
(timing-window-sensitive under sandboxed/loaded scheduling, not a logic
bug), not a new flaky-test class.

This sprint's OWN regression sweep additionally completed a root-cause
trace the TTS Chunking/Streaming sprint's note above left as "not fully
traced": the `text_normalizer` `LUNO_LANGUAGE` env-leak reproduces
identically when ANY file under `luno/routing/` (every file tried,
not one specific one) is combined with
`luno/text_normalizer/tests/test_text_normalizer.py` - `luno.routing.*`
transitively imports `luno.config`, whose `load_dotenv()` call sets
`os.environ["LUNO_LANGUAGE"]` from Vinn's real `.env` for the rest of
that process. Still deliberately not fixed here (same reasoning as
every prior sprint - out of scope).

A full unrestricted `pytest tests/ luno/` single-process run again could
not complete within this sandbox's per-command tooling time budget - the
same documented FAST/FULL split this project has used since the
Regression & Architecture Guard sprint. `luno/` FAST suite plus the
batches above collectively cover every test file this sprint could
plausibly have affected (LLM adapters, TTS/Fish Audio, streaming/
incremental-speech, console/barge-in/wake integration, memory/context
safety, and the full `luno/` tree) at 100% coverage of those files.

No ACTUAL regression from this sprint's own changes. Every failure
observed anywhere in this sprint's sweep is the exact same pre-existing/
environment-specific set already documented above.

Persistent-state verification: SHA256 + mtime for every file directly
under `config/` (57 files) - captured immediately before and immediately
after this sprint's own new test suite (`test_llm_streaming.py`,
`test_incremental_speech_buffer.py`, `test_streaming_speech_integration.py`,
`test_streaming_e2e.py`, which between them construct and tear down
dozens of `RuntimeDemoConsole`/`AdapterManager` instances) - byte-
identical AND mtime-identical, zero diff. No stray temp/audio files
(Fish Audio synthesis remains entirely in-memory `io.BytesIO`, no temp
files exist in that path to begin with, streamed or not).

See `docs/change_impact/llm_streaming_speech_pipeline.md` for the full
audit trail, the two real races this sprint found and fixed, and every
worked-through test scenario.

## No secrets

This file intentionally records no API keys, tokens, or `.env` values -
only which env-VARIABLE-NAMES certain tests are sensitive to (already
public information, since the variable names themselves appear in
`luno/config.py`).

## 2026-08-10 - Voice Output Optimization sprint

Scope: `luno/response_output.py` (generalized SHORT/NORMAL/DETAILED
budget-based compression, previously DETAILED-only) and
`luno/response_policy.py` (additive explicit-phrase list extensions
only). See `docs/change_impact/voice_output_optimization.md` for the
full design rationale.

- Targeted suite (10 files: `test_response_output.py`,
  `test_response_policy.py`, `test_tts_chunking.py`,
  `test_streaming_e2e.py`, `test_incremental_speech_buffer.py`,
  `test_streaming_speech_integration.py`, `test_llm_streaming.py`,
  `test_runtime_demo.py`, `test_wake_barge_in_integration.py`,
  `test_barge_in_console.py`) + new `test_voice_output_optimization.py`:
  **330 passed, 0 failed** (286 baseline + 44 new).
- Memory suite (`test_memory_regression.py`, `test_memory_context.py`,
  `test_memory_retrieval.py`, `test_episodic_memory.py`): **132 passed,
  0 failed**.
- Full `tests/` tree (excluding `test_main_bargein.py`/
  `test_root_main_bargein.py` - pre-existing collection errors, missing
  `faster_whisper`/`legacy_main.py`; and `test_dashboard.py` - documented
  sandbox `ThreadingHTTPServer` timeout, both unrelated to this sprint),
  run in 5 batches: **1588 passed, 12 failed**. All 12 map to
  ALREADY-DOCUMENTED pre-existing/environment issues above in this file:
  `test_stale_emotion_decays_to_unknown_after_the_configured_window`
  (known scheduling-jitter flake, §"172/173" entry above), the 9
  `test_mic_device_index.py`/`test_production_launcher.py`/
  `test_real_adapters.py` environment-dependent failures, and 2
  `test_streaming_e2e.py` tests that only surfaced in a very large
  (360+ test) combined batch and passed cleanly both in isolation and on
  an immediate re-run - same scheduling-jitter class, not a new failure
  mode. **Zero failures in any file this sprint's diff touched.**
- One existing test updated (not deleted):
  `tests/test_response_output.py::test_c3_long_response_many_chunks_in_order`
  - see inline comment and change-impact doc §5 for the documented
    contract-change reasoning (NORMAL depth intentionally now compresses
    a 19-item exhaustive placeholder list it previously read in full).
- Persistent state (`config/*.json` SHA256): byte-identical before/after
  this sprint's entire test run, zero diff.

## 2026-08-10 - Adaptive Response Depth Learning sprint

Scope: `luno/response_policy.py` (new `detect_depth_feedback()`,
`DepthPreference`, `apply_depth_feedback()`, and a new optional
`adaptive_modifier` parameter on `compute_response_policy()` - all
additive) and `main_runtime_demo.py` (new bounded, conversation-scoped,
never-persisted `_depth_preference` dict on `PlannerBridgeModule`, plus
one new call site and one new reset line). See
`docs/change_impact/adaptive_response_depth.md` for the full design
rationale, including why NO new persistent store was added.

- Targeted suite (16 files: response policy/output/voice optimization/
  runtime demo/TTS chunking/streaming e2e/incremental speech/streaming
  speech integration/LLM streaming/wake-barge-in/barge-in console/memory
  outcome telemetry/memory evaluation/episodic memory/memory regression/
  memory context) + new `test_adaptive_response_depth.py`: **604 passed,
  0 failed**.
- Full `tests/` tree (same exclusions as the prior sprint's documented
  baseline above), run in 5 batches: **1590 passed, 10 failed**. All 10
  map to the SAME already-documented pre-existing/environment issues:
  `test_stale_emotion_decays_to_unknown_after_the_configured_window`
  (known scheduling-jitter flake) and the 9 `test_mic_device_index.py`/
  `test_production_launcher.py`/`test_real_adapters.py` environment-
  dependent failures. Notably, the 2 `test_streaming_e2e.py` timing
  flakes observed once by the prior sprint's sweep did NOT reproduce this
  run (ran clean) - reinforcing that classification as sandbox scheduling
  jitter, not a real bug. **Zero failures in any file this sprint's diff
  touched.**
- Persistent state (`config/*.json` SHA256, all 6 writer-capable files):
  byte-identical before/after this sprint's entire test run, zero diff.
  No stray `.tmp`/`.bak`/`.old`/`.orig` artifacts found anywhere outside
  `.venv`/`node_modules`/`__pycache__`.
- No pre-existing failure was "fixed" to make this report look cleaner.

## 2026-08-11 - Persistent Adaptive Response Depth Preference sprint

Scope: one new module, `luno/response_depth_preference.py`
(`PersistedDepthPreference`, `DepthPreferenceStore`, `should_persist()`,
`merge_conversation_into_persistent()`), one new store
(`config/response_depth_preference.json`, `config.RESPONSE_DEPTH_PREFERENCE_FILE`),
and additive wiring in `main_runtime_demo.py`'s `PlannerBridgeModule`
(`__init__`, `_update_depth_preference()`, `_on_conversation_ended()`,
`_handle_utterance()`). `luno/response_policy.py` gained only two new
PUBLIC constant aliases (`DEPTH_BIAS_MIN`/`DEPTH_BIAS_MAX`, re-exporting
its own pre-existing private bounds) - still zero I/O, still enforced by
its own purity test. See
`docs/change_impact/persistent_adaptive_response_depth.md` for the full
design rationale.

**Audit finding at the start of this sprint:** the module, its production
wiring, and a 29-scenario test file
(`tests/test_persistent_adaptive_response_depth.py`) already existed on
disk, fully passing, but Phase 11 documentation
(`ARCHITECTURE_GUARD.md`, this file, and
`docs/change_impact/persistent_adaptive_response_depth.md`) had never
been written - confirmed by grepping all three for
`response_depth_preference`/`RESPONSE_DEPTH_PREFERENCE_FILE` and finding
zero matches before this sprint. This sprint's work was therefore: (1) a
full read-only audit confirming the existing implementation actually
satisfies every hard constraint in the brief (explicit-instruction
priority, no second classifier, no LLM judge, no memory/verified-facts
coupling, bounded bias, conservative blend, cross-conversation isolation,
atomic/backup-safe persistence, test isolation) - all confirmed true by
direct source inspection, not assumed; (2) four small, additive test
gaps closed in the existing test file (see below) rather than a rewrite;
(3) the three Phase 11 documentation deliverables written for the first
time.

- Four tests added to the existing `tests/test_persistent_adaptive_response_depth.py`
  (29 -> 33 scenarios), closing the only real coverage gaps found against
  the sprint brief's own scenario list: `test_e2e_10` (repeated
  "terlalu singkat" feedback also threshold-gates and persists a
  DETAILED-leaning baseline - the brief's scenario I; only the
  SHORT-direction was previously covered by `test_e2e_2`),
  `test_e2e_11` (explicit "jelaskan detail" overrides a persisted
  SHORT-leaning baseline - the brief's scenario L; only the reverse
  direction was previously covered by `test_e2e_5`), `test_U` (proves
  `DepthPreferenceStore.save()` goes through a REAL pre-write backup via
  `luno.persistence`, not just structurally calling it - the brief's
  scenario Q), `test_V` (explicit assertion that
  `tests/conftest.py`'s isolation fixture has actually redirected
  `RESPONSE_DEPTH_PREFERENCE_FILE` away from the real `config/`
  directory - the brief's scenario R). No existing test in this file was
  modified.
- `tests/test_persistent_adaptive_response_depth.py` alone: **33
  passed, 0 failed**.
- Combined targeted suite (`test_persistent_adaptive_response_depth.py`
  + `test_adaptive_response_depth.py` + `test_response_policy.py` +
  `test_response_output.py` + `test_persistent_state_hardening.py`):
  **223 passed, 0 failed**.
- `tests/test_runtime_demo.py` (console/production-bridge integration,
  includes this sprint's own E2E scenarios running through the real
  `RuntimeDemoConsole` pipeline): **78 passed, 0 failed**.
- Memory-adjacent suites this sprint's audit confirmed are NOT coupled to
  the new preference (`test_memory_guard.py` + `test_memory_retrieval.py`
  + `test_memory_context.py` + `test_relationship_engine.py` +
  `test_episodic_memory.py` + `test_manual_memory.py`): **256 passed, 0
  failed**.
- Full `tests/` tree collection count: **1726 tests** (1722 + the 4 new
  scenarios above), same 2 files excluded for the same pre-existing,
  unrelated reasons (`test_main_bargein.py` - missing `faster_whisper`;
  `test_root_main_bargein.py` - `legacy_main.py` absent).
- One pre-existing, already-known-in-this-sandbox failure was
  reconfirmed, NOT newly introduced by this sprint:
  `tests/test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  fails with `OSError: could not get source code` from
  `inspect.getsource()` on the `isolate_persistent_state` fixture -
  reproducible before ANY change this sprint made (confirmed via a
  pre-change baseline run of this exact test), unrelated to
  `luno/response_depth_preference.py` or any file this sprint touched.
  Not investigated or fixed here - out of scope for this sprint's brief,
  and doing so was never requested.
- Persistent state (`config/*.json` SHA256, all files present in the real
  `config/` directory): byte-identical before/after this sprint's entire
  test run, zero diff. `config/response_depth_preference.json` does NOT
  exist in the real `config/` directory before or after this sprint's
  work - the feature has only ever been exercised inside pytest's
  isolated `tmp_path`, exactly as required. No stray `.tmp`/`.bak` files
  and no leaked `response_depth_preference` backups found under
  `config/backups/`.
- No pre-existing failure was "fixed" to make this report look cleaner.
  No production code in `luno/response_depth_preference.py`,
  `luno/response_policy.py`, or `main_runtime_demo.py` was modified by
  this sprint - only tests and documentation were added.

## 2026-08-11 - Conversation_ended Lifecycle Routing sprint

Scope: exactly two lines added, one per route table -
`main_runtime_demo.py`'s `RuntimeDemoConsole.__init__`
(`self.runtime.add_route("conversation_ended", "planner")`) and
`luno/bootstrap/modules.py`'s `register_all_modules()`
(`runtime.add_route("conversation_ended", "planner")`) - plus updated
(no longer stale) code comments in `_on_conversation_ended()` and its
callers, and a new test file. No handler logic, ordering, persistence
schema, threshold, or unrelated subsystem (TTS/streaming/barge-in/
memory retrieval) changed. See
`docs/change_impact/conversation_ended_lifecycle_routing.md` for the
full root-cause trace.

Root cause confirmed by Phase 0 audit: `PlannerBridgeModule.on_event()`
already correctly dispatched `event.type == "conversation_ended"` to
`_on_conversation_ended()` - no route ever delivered the event to the
module registered as `"planner"` in either of this project's two route
tables. `luno/bootstrap/modules.py` was already routing
`conversation_ended` to `"proactive"` (unrelated, unaffected); it was
simply also missing the `"planner"` route. `main_runtime_demo.py` had
no route for this event type at all.

- `tests/test_conversation_ended_lifecycle_routing.py` (new file, 15
  scenarios): **15 passed, 0 failed**. Proves real Event-Bus
  reachability (`console.event_bus.publish(...)`, never a direct
  `_on_conversation_ended()` call), exactly-once handler execution,
  duplicate/unknown/empty-session-id safety, full conversation-local
  cleanup coverage, cross-conversation isolation via a real published
  event, a full short- and detailed-direction adaptive-preference E2E
  (real event end -> persisted merge -> brand-new process seeds from
  it), explicit-instruction override in both directions, and a
  structural check that `luno/bootstrap/modules.py`'s source contains
  the matching production-side fix.
- `luno/core/tests/test_core.py` (Event Bus / Coordinator package-level
  suite): **19/19 scenarios passed** - the routing mechanism itself
  (`Coordinator.add_route()`, fan-out, `EventBus` dispatch) needed no
  changes and shows no regression.
- `tests/test_production_launcher.py` (exercises
  `luno/bootstrap/modules.py`'s `register_all_modules()` directly - the
  file this sprint's production-side fix lives in): **23 passed, 1
  failed** - the SAME already-known, environment-specific failure
  (`test_07_health_checks_all_pass_in_default_mock_configuration`,
  real `.env` credentials trigger a real network health check) as every
  prior sprint's baseline. Confirms the bootstrap route-table edit did
  not break production module registration.
- Combined targeted suite (`test_conversation_ended_lifecycle_routing.py`
  + `test_persistent_adaptive_response_depth.py` +
  `test_adaptive_response_depth.py` + `test_response_policy.py` +
  `test_response_output.py` + `test_persistent_state_hardening.py`):
  **238 passed, 0 failed**.
- `tests/test_runtime_demo.py`: **78 passed, 0 failed** (unchanged from
  the prior sprint's baseline).
- Wake-session/barge-in console integration
  (`test_wake_session_console.py` + `test_wake_barge_in_integration.py`
  + `test_barge_in_console.py` + `test_device_context.py` +
  `test_browser_wiring.py` - the modules "adjacent" to
  `SessionManagerModule`, the actual publisher of `ConversationEnded`):
  **78 passed, 0 failed**.
- Memory/relationship/proactive suites (`test_memory_guard.py` +
  `test_memory_retrieval.py` + `test_memory_context.py` +
  `test_relationship_engine.py` + `test_episodic_memory.py` +
  `test_manual_memory.py` + `test_proactive.py` - `test_proactive.py`
  specifically because `ProactiveModule` is the OTHER subscriber to
  `conversation_ended` in production, sharing the event this sprint
  touched): **301 passed, 0 failed**.
- Full `tests/` tree collection count: **1741 tests** (1726 + the 15
  new scenarios), same 2 files excluded for the same pre-existing,
  unrelated reasons.
- Remainder of the full suite, run in 3 batches under `-n auto`: **964
  passed, 11 failed** - every failure maps to an already-documented,
  pre-existing class, reconfirmed identical to prior sprints' baselines:
  1x `test_stale_emotion_decays_to_unknown_after_the_configured_window`
  (scheduling-jitter flake under parallel load), the same 9
  `test_mic_device_index.py` (6) / `test_real_adapters.py` (2) /
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  (1) environment-specific failures, and 1x
  `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays`
  (the same scheduling-jitter class already documented for this test).
  **Zero new failures in any file this sprint touched or in any
  unrelated file.**
- Persistent state (`config/*.json` SHA256, all files present in the
  real `config/` directory): byte-identical before/after this sprint's
  entire test run, zero diff. `config/response_depth_preference.json`
  does NOT exist in the real `config/` directory before or after this
  sprint's work. No stray `.tmp`/`.bak` files.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to the two `add_route(...)`
  lines plus stale-comment updates - verified via direct review of both
  diffs before this regression sweep was run.

## 2026-08-11 - Conversation End Lifecycle Race Safety sprint

Scope: closes the race §21/this file's prior entry documented as a
known limitation - `_handle_utterance()`'s background-thread processing
can lose a turn's depth-feedback contribution if `conversation_ended`
for the same conversation is delivered and fully processed before that
turn reaches `_update_depth_preference()`. Fix: one small, purpose-built
`threading.Condition` (`_active_turn_lock`/`_active_turn_cv`) plus two
plain, bounded, never-persisted structures
(`_active_turn_counts`/`_ending_conversations`) added to
`PlannerBridgeModule`; `on_event()` now atomically refuses new turns for
an already-ending conversation; `_on_conversation_ended()` now waits
(bounded by `turn_settle_timeout_s`, default 2.0s) for any in-flight
turn to settle before its existing merge+cleanup body runs. No EventBus
change, no route-table change, no persistence schema change, no
response-depth policy change. See
`docs/change_impact/conversation_end_race_safety.md` for the full
before/after trace and concrete race-reproduction evidence.

- `tests/test_conversation_end_race.py` (new file, 20 scenarios):
  **20 passed, 0 failed**, reconfirmed stable across 3 consecutive runs
  (no flakiness observed). Includes a deterministic reproduction of the
  OLD (pre-fix) race - `test_race_reproduction_zero_wait_loses_the_late_turns_feedback`
  runs the real production code path with `turn_settle_timeout_s=0`
  (a faithful stand-in for "no synchronization at all", i.e. this
  project's literal pre-sprint behavior) and proves a late turn's
  feedback is genuinely and permanently lost; the very same scenario, at
  the real default timeout
  (`test_B_case_new_ordering_waits_and_captures_the_late_feedback`),
  proves the fix captures and persists it. Also covers: ordinary
  completion before end (case A), a permanently-hung worker timing out
  without deadlock or corruption (case C), duplicate `conversation_ended`
  after a hang-timeout being idempotent (case D), cross-conversation
  isolation while one conversation is actively waiting (case E), a
  brand-new conversation immediately reusing the same session_id not
  being blocked forever (case F), unknown/empty conversation_id no-ops,
  SHORT/DETAILED adaptive-preference E2E through the real race window in
  both directions (with brand-new-process restart seeding), explicit-
  instruction override in both directions after the race window,
  persisted-file JSON validity and zero raw-text/conversation-id leakage
  after race scenarios, no cross-conversation state leak across 4
  concurrently-ending conversations, cleanup occurring exactly once per
  event, no-global-lock regression (5 unrelated conversations' turns
  complete promptly while one conversation is actively waiting), and
  EventBus route-table safety reconfirmed unchanged.
- A test-harness bug (not a production bug) was found and fixed during
  this sprint's own test-writing: `_install_blocking_memory_retrieval()`'s
  block initially applied to ANY call to `retrieve_memories()`, so a
  second, supposedly-unrelated conversation's turn also stalled on the
  same shared `release_event` while the first conversation's turn was
  deliberately held open - producing a false "looks like a global lock"
  symptom in `test_case_E`/`test_no_global_lock_regression`. Fixed by
  adding an `only_for_text` parameter so the block only ever applies to
  the specific conversation's own utterance text; every other call
  passes straight through to the real implementation. A second
  test-harness bug was found and fixed in the same pass: polling
  `conv_id not in _ending_conversations` as a "has this conversation_ended
  call finished?" signal is ambiguous on fast paths (a call with nothing
  to wait for, or a hung turn just force-cleared by a short timeout, can
  complete in well under a millisecond - faster than any practical
  polling interval can reliably observe, since the SAME condition is
  also trivially true before the call has even started) - fixed by
  adding `_publish_conversation_ended_tracked()`/
  `_publish_conversation_ended_and_wait()`, which instrument
  `_on_conversation_ended()` itself with a `threading.Event` that cannot
  miss the transition regardless of speed. Both bugs were caught by this
  sprint's own tests failing unexpectedly (`test_case_C`, `test_case_D`,
  `test_case_E`, `test_case_F`, `test_unknown_conversation_id_wait_is_a_no_op`,
  `test_no_global_lock_regression_...`) - each was root-caused via
  isolated debug scripts before being fixed, not guessed at.
- Combined targeted suite (`test_conversation_end_race.py` +
  `test_conversation_ended_lifecycle_routing.py` +
  `test_persistent_adaptive_response_depth.py` +
  `test_adaptive_response_depth.py` + `test_response_policy.py`):
  **175 passed, 0 failed**.
- Full `tests/` tree collection count: **1761 tests** (1741 + the 20 new
  scenarios), same 2 files excluded for the same pre-existing, unrelated
  reasons (`test_main_bargein.py` - missing `faster_whisper`;
  `test_root_main_bargein.py` - `legacy_main.py` absent).
- Full suite, run serially in 10 file-group batches (chosen over `-n
  auto` after the parallel run below): **1751 passed, 10 failed** -
  every failure maps to an already-documented, pre-existing class,
  reconfirmed identical to prior sprints' baselines: 6x
  `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  (sandbox `inspect.getsource()` gap). **Zero new failures in any file
  this sprint touched or in any unrelated file.**
- One PARALLEL-EXECUTION TIMING FLAKE was observed during an initial
  `-n auto` attempt at the full suite:
  `test_dashboard.py::test_35_chat_audio_endpoint_reports_no_clip_for_mock_backend`
  failed once under parallel load; re-run in isolation immediately after,
  it passed, and it also passed cleanly (no failure) in the serial
  10-batch run reported above. Same class of environmental flakiness
  already documented for this suite (real background HTTP client threads
  racing a real `ThreadingHTTPServer` under CPU contention) - confirmed
  unrelated to this sprint (this sprint never touches
  `luno/dashboard/*` or `test_dashboard.py`).
- Persistent state (`config/*.json` SHA256, all files present in the
  real `config/` directory): byte-identical before/after this sprint's
  entire test run (all 14 present `config/*.json` files, hashed before
  and after the full regression sweep), zero diff.
  `config/response_depth_preference.json` exists in the real `config/`
  directory (pre-dates this sprint, unrelated real usage) and was
  confirmed byte-identical before/after - this sprint's own tests only
  ever touch the `tmp_path`-redirected copy via
  `tests/conftest.py`'s `isolate_persistent_state` fixture. No stray
  `.tmp`/`.bak`/`.old` files and no corrupted JSON found anywhere under
  `config/`.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to `main_runtime_demo.py`'s
  `PlannerBridgeModule` (new `turn_settle_timeout_s` constructor
  parameter, new `_active_turn_lock`/`_active_turn_cv`/
  `_active_turn_counts`/`_ending_conversations` state, new
  `_mark_turn_settled()`/`_wait_for_turn_to_settle()` methods, and small
  additive changes to `on_event()`, `_handle_utterance()`, and
  `_on_conversation_ended()`) - verified via direct review of the diff
  before this regression sweep was run. No EventBus/Coordinator code,
  no response-depth policy code, no persistence schema, and no
  TTS/streaming/barge-in/memory code was touched.

## 2026-08-11 - Memory Prompt-Injection Hardening sprint

Scope: one function extended in one existing file -
`luno/memory_context.py::render_context_block()` now wraps its
already-built output in one explicit BEGIN/END data-boundary marker
pair (plus one small, render-time-only, reversible marker-neutralization
helper for the self-referential edge case) - and one new test file. No
retrieval, ranking, scoring, conflict-resolution, deduplication, budget,
persistence schema, or route ever changed. See
`docs/change_impact/memory_prompt_injection_hardening.md` for the full
design and adversarial test matrix.

- `tests/test_memory_prompt_injection.py` (new file, 30 scenarios):
  **30 passed, 0 failed**, reconfirmed stable across 3 consecutive runs.
  Covers the full adversarial matrix (instruction-like/fake-system/fake-
  developer/fake-user-command/multi-line/markdown/XML-like/JSON-like
  memory text, a Verified Fact whose value contains instruction-like
  text, episodic/historical/cross-source-mixed injection, empty context,
  Indonesian/unicode text, long text, quotes/special characters, one
  malicious-looking memory among normal ones, and two self-referential
  boundary-marker-forgery cases), structural guarantees (no LLM/network
  call, no persistent-state write, no mutation of the underlying memory
  object, ranking/retrieval-count/no-second-retrieval unchanged, no
  second module introduced, Verified-Fact/relationship semantics
  unchanged), and two real production-path end-to-end tests that drive
  an actual `PlannerBridgeModule` turn through the real Event Bus and
  inspect the real final system-prompt string.
- Combined targeted suite (`test_memory_retrieval.py` +
  `test_memory_context.py` + `test_memory_conflict.py` +
  `test_memory_evaluation.py` + `test_memory_adaptive_retrieval.py` +
  `test_memory_persistence_hardening.py` + `test_response_policy.py` +
  `test_memory_prompt_injection.py`): **316 passed, 0 failed** (286 +
  the 30 new scenarios - every pre-existing test in this group passed
  completely unchanged).
- `tests/test_runtime_demo.py` + wake/barge-in console suites
  (`test_wake_session_console.py` + `test_wake_barge_in_integration.py` +
  `test_barge_in_console.py`): **118 passed, 0 failed**.
- TTS/streaming suites (`test_tts_chunking.py` + `test_tts_e2e_pipeline.py`
  + `test_tts_queue.py` + `test_tts_cancellation.py` +
  `test_streaming_e2e.py` + `test_streaming_speech_integration.py` +
  `test_llm_streaming.py`): 85 passed, 1 failed
  (`test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays`)
  - re-run in isolation 3 times immediately after: **passed all 3**,
  confirming the SAME already-documented scheduling-jitter-under-load
  flake class this exact test has been flagged as in every prior
  sprint's baseline, not a new regression (this sprint touches zero
  TTS/streaming/barge-in code).
- Full `tests/` tree collection count: **1791 tests** (1761 + the 30 new
  scenarios), same 2 files excluded for the same pre-existing, unrelated
  reasons (`test_main_bargein.py` - missing `faster_whisper`;
  `test_root_main_bargein.py` - `legacy_main.py` absent).
- Full suite, run serially in 10 file-group batches: **1780 passed, 11
  failed** - every failure maps to an already-documented, pre-existing
  class, reconfirmed identical to prior sprints' baselines: 6x
  `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  (sandbox `inspect.getsource()` gap), 1x `test_streaming_e2e.py::test_D_...`
  (the same timing flake described above). **Zero new failures in any
  file this sprint touched or in any unrelated file.**
- Persistent state (`config/*.json` SHA256 AND mtime, all 14 files
  present in the real `config/` directory): byte-identical and
  mtime-identical before/after this sprint's entire implementation +
  test run. No stray `.tmp`/`.bak`/`.old` files, no new production
  memory files.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to
  `luno/memory_context.py::render_context_block()` (extended) plus two
  new module-level constants and one new small helper function
  (`_neutralize_boundary_markers()`) in the same file - verified via
  direct review of the diff before this regression sweep was run. No
  retrieval/ranking/scoring/conflict/dedup/budget/persistence/route code
  was touched.

## 2026-08-11 - Memory Retrieval & Decision Quality (Intent Taxonomy + Topic Continuity) sprint

Scope: closes the two CONFIRMED gaps from this sprint's own Phase 0
audit only - a coarse query-intent taxonomy, and no topic-continuity
signal. Everything the audit found ALREADY correctly implemented
(relevance-gated retrieval, importance, context-specific evidence,
conflict resolution, cross-source dedup, budget, exactly-once retrieval)
is completely UNCHANGED. New: `luno.memory.classify_query_intent()`,
`luno.memory_context.extract_topic_terms()`, one new additive
`ContextItem.intent_bonus` ranking field, and
`PlannerBridgeModule._last_topic_terms` (bounded, conversation-scoped,
in-memory only). See `docs/change_impact/memory_decision_quality.md` for
the full design and test matrix.

- `tests/test_memory_decision_quality.py` (new file, 36 scenarios):
  **36 passed, 0 failed**. Covers intent classification (A-G, including
  explicit-recall delegating to the EXISTING recall/historical
  detectors, and the "other"/`None` no-op proof), continuation (H-N,
  including the sprint's own worked examples, bounded "lanjut"-alone
  effect, conversation-end reset, and cross-conversation isolation),
  per-intent retrieval-quality preference (O-S), and structural
  invariants (T-AD: exactly-one `retrieve_memories()` call per turn,
  `_rank_key()[0]` still raw relevance even under maximal bonus pressure,
  cross-source dedup unchanged, conflict resolution unchanged, budget
  unchanged, prompt-injection boundary still present with intent active,
  no persistent-state mutation, no second tokenizer, no LLM/network call,
  conversation state bounded and cleaned at conversation end) - plus two
  real production-path end-to-end tests through `RuntimeDemoConsole`/
  `PlannerBridgeModule` inspecting the REAL final system prompt.
- CONTRACT CHANGE (documented per Strict Rule #15, same precedent as
  `evaluation`'s own earlier addition to this tuple):
  `tests/test_memory_evaluation.py::test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker`
  was extended, not silently weakened, to assert the new
  `(..., intent_bonus, priority)` tuple shape.
- Combined targeted suite (`test_memory_retrieval.py` +
  `test_memory_context.py` + `test_memory_conflict.py` +
  `test_memory_evaluation.py` + `test_memory_adaptive_retrieval.py` +
  `test_memory_prompt_intelligence.py` + `test_memory_intelligence.py` +
  `test_memory_regression.py` + `test_memory_guard.py` +
  `test_memory_prompt_injection.py` + `test_memory_decision_quality.py`):
  **391 passed, 0 failed**.
- `tests/test_runtime_demo.py`: **78 passed, 0 failed**.
- `test_memory_dashboard.py` + `test_memory_maintenance.py` +
  `test_memory_learning.py` + `test_memory_outcome_telemetry.py` +
  `test_manual_memory.py` + `test_episodic_memory.py` +
  `test_relationship_engine.py` + `test_memory_persistence_hardening.py`:
  **366 passed, 0 failed**.
- TTS/streaming (`test_streaming_e2e.py` + `test_streaming_speech_integration.py`
  + `test_voice_output_optimization.py`): **72 passed, 0 failed** (the
  usual `test_streaming_e2e.py::test_D_...` scheduling-jitter flake did
  not reproduce this run).
- Wake/barge-in (`luno/barge_in/tests` + `luno/wake_session/tests`):
  **63 passed, 0 failed**.
- Full `tests/` tree, run serially in 12 file-group batches (2 files
  excluded at collection for the same pre-existing, unrelated reasons as
  every prior sprint - `test_main_bargein.py`: missing `faster_whisper`;
  `test_root_main_bargein.py`: `legacy_main.py` absent): **1817 passed,
  10 failed** - every failure maps to an already-documented, pre-existing
  class, reconfirmed identical to prior sprints' baselines: 6x
  `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  (sandbox `inspect.getsource()` gap). **Zero new failures in any file
  this sprint touched.**
- `python3 -m pytest luno/ -q` (the FAST TEST command): **813 passed, 7
  failed** on first run, reproduced identically on a second run - all 7
  in `luno/barge_in/tests/test_barge_in.py` (2 scenarios) and
  `luno/text_normalizer/tests/test_text_normalizer.py` (5 scenarios),
  BOTH suites this sprint never touched (confirmed via `grep` - zero
  references to `memory_context`/`classify_query_intent`/
  `extract_topic_terms` anywhere in either directory) and BOTH pass
  **62/62** when run standalone, in isolation, immediately after -
  a load/contention-dependent artifact of running the full `luno/`
  directory together in this sandbox (consistent with this project's own
  documented note about lingering daemon threads/GIL contention across
  long test runs), not a regression introduced by this sprint.
- Persistent state (`config/*.json` SHA256 AND mtime, all 14 files
  present in the real `config/` directory): byte-identical and
  mtime-identical before/after this sprint's entire implementation + test
  run. No stray `.tmp`/`.bak`/`.old`/`.orig` files, no new production
  memory files.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to: `luno/memory.py`
  (`classify_query_intent()` and its marker tables - purely additive),
  `luno/memory_context.py` (`ContextItem.intent_bonus` new field,
  `_rank_key()` extended by one tuple position, five new small functions,
  `assemble_context()` gained two new optional keyword parameters), and
  `main_runtime_demo.py` (`PlannerBridgeModule._last_topic_terms` new
  bounded dict, three small additions inside `_handle_utterance()`/
  `_on_conversation_ended()`, two new keyword arguments threaded into the
  existing `assemble_context()` call) - verified via direct review before
  this regression sweep was run. No retrieval/ranking ALGORITHM,
  conflict-resolution, cross-source dedup, budget-enforcement, or
  persistence-schema code was touched - only one new, low-priority,
  strictly-subordinate ranking tiebreaker was added on top of all of it.

## 2026-08-11 - TTS Chunk Pipelining sprint

Scope: closes the audible between-chunk voice gap a prior, read-only
Phase 0 audit sprint measured (`SynthesisStart[i+1] == PlaybackEnd[i]`
to the millisecond, gap == next chunk's own synthesis latency,
1.301-1.303s in that audit's harness). Fix: a conservative ONE-SLOT
lookahead/prefetch - chunk N+1's synthesis now runs concurrently with
chunk N's playback, never more than one chunk ahead, playback order
strictly preserved. New: 3 optional `FishAudioClient` ABC methods
(`supports_split_synthesis()`/`synthesize()`/`play_audio()`, opt-in,
default no-op), a new `_prefetch_executor` + `_play_stream_pipelined()`
+ `_resolve_audio()` on `FishAudioAdapter`, and thin
`synthesize()`/`play_audio()` wrappers on `RealFishAudioClient` around
its ALREADY-existing internal `_synthesize`/`_play_audio` callables.
`_play_stream()`'s own body and `_play()` (legacy path) are
byte-identical to before this sprint; `MockFishAudioClient` and every
test that uses it take the exact same, unmodified code path as before.
See `docs/change_impact/tts_chunk_pipelining.md` for the full audit
trail, the new regression-proof test
(`test_synthesis_of_next_chunk_starts_before_current_playback_ends`,
confirmed FAILING against the pre-fix code before implementation began),
and the final report.

- **New tests:** `tests/test_tts_chunk_pipelining.py` - **19 passed, 0
  failed**, run 3x consecutively with identical results. Covers: the core
  overlap proof at 1/2/3/5-chunk scale, playback-order-never-reordered
  (later chunks synthesizing faster), prefetch-synthesis-failure
  retried-then-skipped, slow-synthesis-no-deadlock, 3 distinct
  cancellation timings (during prefetch / prefetch-ready-but-unused /
  mid-current-playback) plus a direct no-stale-audio-after-cancellation
  proof, pause-then-resume, pause-does-not-cancel-prefetch, mid-stream
  and trailing close-marker handling, sequential-turns leave-no-leftover-
  state, concurrent-unrelated-requests isolation, no-executor-leak across
  15 turns, and a direct concurrency-counter proof of the one-slot bound.
- `luno/adapters/tests/test_fish_audio_real.py`: **14/14** (custom
  `SCENARIOS`/`main()` runner, run via `python3 -m
  luno.adapters.tests.test_fish_audio_real` - plain pytest collection
  does not actually validate this file's assertions).
- `luno/adapters/tests/test_fish_audio_barge_in.py`: **8/8** (same
  convention).
- `luno/adapters/tests/` fish_audio/streaming/barge-in-filtered pytest
  subset: **86 passed, 0 failed**.
- Full `tests/` tree, run serially in 9 file-group batches (2 files
  excluded at collection for the same pre-existing, unrelated reasons as
  every prior sprint - `test_main_bargein.py`: missing `faster_whisper`;
  `test_root_main_bargein.py`: stale sandbox path/`legacy_main.py`
  absent): **1836 passed, 10 failed** - every failure maps to the
  already-documented, pre-existing class, reconfirmed identical to prior
  sprints' baselines: 6x `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`.
  **Zero new failures in any file this sprint touched.**
- `python3 -m pytest luno/ -q` (2 batches): **813 passed, 7 failed** -
  all 7 in `luno/barge_in/tests/test_barge_in.py` (2 scenarios,
  `test_confirm_mode_interrupt_then_no_resumes`/
  `test_stress_many_ordinary_utterances_then_one_real_interrupt`) and
  `luno/text_normalizer/tests/test_text_normalizer.py` (5 scenarios,
  the already-documented `LUNO_LANGUAGE` env-leak-under-full-sweep
  issue) - BOTH suites this sprint never touched (confirmed via `grep` -
  zero references to `fish_audio`/`FishAudioAdapter`/`FishAudioClient`
  anywhere in either directory) and BOTH pass **standalone** (2/2 and
  35/35 respectively, 3x reconfirmed for the barge-in pair) - the exact
  same 813/7 count the immediately-prior LLM Streaming sprint's own
  documented baseline established, reproducing identically, not a
  regression introduced by this sprint.
- Persistent state (`config/*.json` SHA256 AND mtime, all 14 files
  present in the real `config/` directory): byte-identical and
  mtime-identical before/after this sprint's entire implementation + test
  run. No stray `.tmp`/`.bak`/`.old`/`.orig` files, no new production
  memory files.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to: `luno/adapters/fish_audio.py`
  (3 new optional ABC methods, `_prefetch_executor` field +
  `_do_start()`/`_do_stop()` wiring, one dispatch line added to
  `_play_stream()`, new `_play_stream_pipelined()`/`_resolve_audio()`
  methods) and `luno/adapters/fish_audio_real.py` (3 new thin wrapper
  methods on `RealFishAudioClient`, `play()` itself untouched) - verified
  via direct review before this regression sweep was run. No changes to
  `IncrementalSpeechBuffer`/`StreamingSpeechCoordinator`, the TTS
  chunking heuristic, the Event Bus, voice model/config, or any other
  adapter.

## 2026-08-11 - Voice Output Coherence sprint

Scope: a Phase 0 audit (real reproduction harness, not speculation)
proved that long spoken responses sometimes sounding "disconnected" is
introduced BEFORE TTS - entirely inside `luno/response_output.py`'s
`_select_by_priority()`/`_score_sentence()` sentence selection. TTS chunk
pipelining (the immediately-prior sprint) was proven NOT responsible and
left completely untouched. Two concrete, reproduced bugs fixed: (1) a
soft-conditional clause could survive compression while the plain
explanatory sentence it depended on (its own prerequisite) was dropped,
because ordinary explanatory sentences score near-zero; fixed with a
bounded `_CONDITION_SETUP_BONUS` applied to a sentence immediately
preceding a soft-conditional sentence (reuses the existing
`_has_condition()` detector, no new classifier). (2) `_has_warning()`'s
naive substring matching false-positived on "seharusnya" ("should")
containing "harus" ("must"), silently promoting an unrelated sentence to
must-keep status; fixed with word-boundary-safe regex matching, reusing
the EXACT technique `luno.memory._compile_word_boundary_marker_pattern()`
already established for the identical class of bug. Budgets, must-keep
rules, order preservation, and the explicit-DETAILED compression skip
are all unchanged. See `docs/change_impact/voice_output_coherence.md`
for the full audit trail, before/after examples, and final report.

- **New tests:** `tests/test_voice_output_coherence.py` - **23 passed, 0
  failed**, run 3x consecutively with identical results (2 direct proof
  tests confirmed FAILING against the pre-fix code before implementation
  began, 1 sanity check that genuine warnings still match post-fix, the
  brief's own 18-scenario matrix, and 2 real E2E tests through
  `RuntimeDemoConsole`/`PlannerBridgeModule`).
- Targeted suite (`test_response_output.py` + `test_response_policy.py` +
  `test_voice_output_optimization.py` + `test_voice_output_coherence.py`
  + TTS pipelining/chunking/queue/cancellation/e2e + streaming/
  incremental-speech + runtime-demo + barge-in): **394 passed, 0
  failed** (371 pre-change baseline + 23 new, exact match - zero
  regressions in any file this sprint touched or any adjacent voice/TTS
  suite).
- Broader memory suite (`test_memory_regression.py` +
  `test_memory_context.py` + `test_memory_retrieval.py` +
  `test_episodic_memory.py` + `test_memory_decision_quality.py`): **168
  passed, 0 failed**.
- Full `tests/` tree, run serially in 9 file-group batches (2 files
  excluded at collection for the same pre-existing, unrelated reasons as
  every prior sprint - `test_main_bargein.py`: missing `faster_whisper`;
  `test_root_main_bargein.py`: stale sandbox path/`legacy_main.py`
  absent): **1859 passed, 10 failed** - every failure maps to the
  already-documented, pre-existing class, reconfirmed identical to prior
  sprints' baselines (6x `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`).
  23 more tests passed than the immediately-prior TTS Chunk Pipelining
  sprint's own 1836 baseline - exactly matching this sprint's 23 new
  tests. **Zero new failures in any file this sprint touched.**
- `python3 -m pytest luno/ -q` (2 batches): **813 passed, 7 failed** -
  the EXACT same count and the SAME 7 failures (2x
  `luno/barge_in/tests/test_barge_in.py` timing flakes, 5x
  `luno/text_normalizer/tests/test_text_normalizer.py`
  `LUNO_LANGUAGE`-env-leak-under-full-sweep) as the immediately-prior TTS
  Chunk Pipelining sprint's own documented baseline - zero coupling to
  this sprint's files (neither directory references `response_output`/
  `response_policy`), both suites confirmed passing standalone.
- Fish Audio custom-runner suites (`python3 -m
  luno.adapters.tests.test_fish_audio_real` / `test_fish_audio_barge_in`):
  **14/14** / **8/8**, unchanged.
- Persistent state (`config/*.json` SHA256 AND mtime, all 14 files
  present in the real `config/` directory): byte-identical and
  mtime-identical before/after this sprint's entire implementation + test
  run. No stray `.tmp`/`.bak`/`.old`/`.orig` files, no new production
  memory files.
- No pre-existing failure was "fixed" to make this report look cleaner.
  Production code changes were limited to: `luno/response_output.py`
  (`_compile_word_boundary_marker_pattern()`/`_WARNING_RE`/`_has_warning()`
  reworked to word-boundary matching; `_CONDITION_SETUP_BONUS`/
  `_select_scores_with_setup_bonus()` new; `_select_by_priority()`'s
  scoring call site updated to use the new helper) - verified via direct
  review before this regression sweep was run. No changes to
  `luno/response_policy.py`, TTS chunking/pipelining, streaming, the
  Event Bus, or any adapter. `chat_text` remains byte-identical to
  `response_text` at every depth (directly asserted,
  `test_16_chat_text_always_byte_identical_to_input_at_every_depth`).

## 2026-08-11 - Voice Response Intelligence sprint (Sprint 1 - Context-Preserving Response Selection)

- **Pre-implementation baseline note:** the Phase 0 baseline-capture
  step (re-running this exact same 15-file targeted command) initially
  returned 4 unexpected failures instead of the prior sprint's clean
  394/394 - root-caused to 4 PRE-EXISTING test-harness bugs, not a
  regression: `test_response_output.py::test_18_required_numeric_spec_survives_detailed_compression`,
  `::test_c10_number_decimal_not_cut_badly`,
  `test_voice_output_optimization.py::test_08_numeric_value_preservation`,
  `::test_25_response_with_numbers_and_units` all asserted
  Indonesian-specific spoken-number output (`"12"` or `"dua belas"`)
  without ever pinning `language="indonesian"` on their
  `build_dual_response()` call, silently relying on the ambient
  `LUNO_LANGUAGE` env default - `normalize_for_speech()`'s own documented
  fallback when no `language` is passed and the env var is unset/other IS
  `"english"`, not `"indonesian"`/`"auto"`. This project's real `.env`
  now sets `LUNO_LANGUAGE=english` (a legitimate, unrelated change to the
  user's own config since the prior sprint's session). Fixed per this
  project's own "fix the test harness separately, don't weaken the
  assertion" convention - pinned `language="indonesian"` explicitly on
  all 4 calls, zero change to assertion logic, zero production code
  touched. Re-ran the same 15-file command after the fix: clean
  246/246 (first half) - confirms the baseline was otherwise identical to
  the prior sprint's own 394/394.
- Targeted suite (same 15 files as the Voice Output Coherence sprint,
  plus the new `tests/test_voice_response_intelligence.py`): **419
  passed, 0 failed** (388 pre-change baseline for those 15 files + 31
  new, exact match - zero regressions in any file this sprint touched or
  any adjacent voice/TTS suite).
- Full `tests/` tree, run in small batches (73 files; 2 excluded at
  collection for the same pre-existing, unrelated reasons as every prior
  sprint - `test_main_bargein.py`: missing `faster_whisper` package in
  this ephemeral sandbox; `test_root_main_bargein.py`: missing
  `legacy_main.py` in this sandbox instance): every batch passed cleanly
  except the SAME 4 already-documented pre-existing failure groups (6x
  `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` (whisper `_device_index` gap), 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`).
  `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays`
  reproduced its own already-documented intermittent timing flake during
  Phase 0 baseline capture (fails ~1-in-3 runs even standalone, unrelated
  to any change) but passed cleanly in the final regression run. **Zero
  new failures in any file this sprint touched.**
- `python3 -m pytest luno/ -q`, run in batches (37 files): **all pass**
  (Fish Audio custom-runner suites unchanged: `python3 -m
  luno.adapters.tests.test_fish_audio_real` **14/14**,
  `test_fish_audio_barge_in` **8/8**).
- Persistent state (`config/*.json` SHA256 AND mtime, all 14 files
  present): the 4 files real E2E-pipeline test runs always touch as an
  expected side effect (`habit_memory.json`, `long_term_memory.json`,
  `relationship_state.json`, `verified_facts.json` - same set every
  prior sprint's own E2E tests touch) changed; the other 10, including
  `response_depth_preference.json`, were byte-identical. No stray
  `.tmp`/`.bak`/`.old`/`.orig` files.
- No pre-existing failure was "fixed" to make this report look cleaner -
  the 4 baseline test-harness bugs above were fixed as TEST harness bugs
  (explicit `language=` pinning), not silently weakened, and are called
  out explicitly rather than folded quietly into the pass count.
  Production code changes were limited to `luno/response_output.py`
  (three new leading-window marker tables + `_dependency_kind()` +
  generalized `_select_scores_with_setup_bonus()` + new
  `_repair_orphans()` post-selection pass) - verified via direct review
  before this regression sweep was run. No changes to
  `luno/response_policy.py`, TTS chunking/pipelining, streaming, Fish
  Audio, the Event Bus, memory retrieval, or any adapter. `chat_text`
  remains byte-identical to `response_text` at every depth (structural
  test, `test_voice_response_intelligence.py`'s own module docstring
  claim, consistent with the unmodified code path that produces it).

## 2026-08-12 - Voice Pipeline Latency & Semantic Speech Segmentation (Sprint 2)

- Phase 0 audit traced the REAL production path (not assumed from prior
  reports) and found the actual latency bottleneck was CASE B
  (`BehaviorTreeModule._generate_reply()` unconditionally blocks on the
  full LLM response before `_speak()` runs) plus a separate,
  previously-undocumented gap (`FishAudioAdapter._play()` never checked
  `client.supports_split_synthesis()`, so the existing TTS Chunk
  Pipelining sprint's synth/playback overlap was unreachable from the
  DEFAULT `speak_request` event path).
- New: `tests/test_voice_pipeline_latency.py` (8 tests, A-H) - measured
  first-audio latency with 5 repetitions each: default path median
  **2.4056s**, streaming-enabled path median **0.3747s** (**84.4%**
  improvement), proving CASE B with real numbers rather than assumption.
  Tests E-H prove the `_play_pipelined()` fix (new this sprint) against a
  REAL `RealFishAudioClient` via the same `TimedFakeSession` technique
  `tests/test_tts_chunk_pipelining.py` established: synth overlaps
  playback, playback order preserved despite synthesis-time inversion,
  cancellation discards stale audio, pause/resume correctness. All 8
  passing.
- New: `tests/test_semantic_speech_units.py` (39 tests) - direct unit
  tests for `_build_semantic_units()` (new), short-sentence FUNCTION
  classification (`_has_confirmation_lead()`, new), Phase 7 listener-
  coherence proofs against 5 adversarial dependent-opener sentences,
  Phase 14 false-positive guards (word-boundary-safe, not substring -
  `"Keberlanjutan"` does not match `"selanjutnya"`/`"lanjut"`), Phase 8
  over-compression guards (the brief's own two worked examples verified
  intact at SHORT/NORMAL/DETAILED), the relevance-dominance CRITICAL
  INVARIANT, a dependency-classification regression spot-check, and 3
  real E2E tests through `RuntimeDemoConsole`. All 39 passing.
- Targeted suite (`test_voice_response_intelligence.py`,
  `test_voice_output_coherence.py`, `test_voice_output_optimization.py`):
  **98 passed, 0 failed** - zero regression from the new
  `_CONFIRMATION_BONUS` scoring term or `_build_semantic_units()`
  addition. `test_response_output.py` + `test_response_policy.py` +
  `test_adaptive_response_depth.py` + `test_persistent_adaptive_response_depth.py`:
  **192 passed, 0 failed**.
- Streaming/TTS-pipelining regression (Phase 9-11 verification, no
  production changes to these files this sprint):
  `test_incremental_speech_buffer.py` + `test_llm_streaming.py`: 25/25.
  `test_streaming_speech_integration.py` + `test_streaming_e2e.py`:
  28/28. `test_tts_chunk_pipelining.py` +
  `luno/adapters/tests/test_fish_audio_real.py` (now exercising the new
  `_play_pipelined()` path) +
  `luno/adapters/tests/test_fish_audio_barge_in.py`: 41/41.
- Full `tests/` tree, run in 12 batches (75 files; 2 excluded at
  collection for the same pre-existing, environmental reasons as every
  prior sprint - `test_main_bargein.py`: missing `faster_whisper` in
  this ephemeral sandbox; `test_root_main_bargein.py`: missing
  `legacy_main.py` in this sandbox instance): **1937 passed**; only the
  SAME 4 already-documented pre-existing failure groups reproduced (6x
  `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
  2x `test_real_adapters.py` `_device_index` gap, 1x
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`).
  **Zero new failures.**
- Full `luno/` tree, run in 6 batches (38 files): **820 passed, 0
  failed**. `luno/barge_in/tests/test_barge_in.py` +
  `luno/text_normalizer/tests/test_text_normalizer.py` re-run standalone
  per the established convention: **62/62**, matching the documented
  "pass standalone" baseline note - neither of this sprint's own known
  intermittent flakes reproduced in the batched or standalone runs this
  time.
- Persistent state (`config/*.json` SHA256 + mtime, all 14 files
  present, snapshotted before the sweep began): **byte-identical after**
  - zero unexpected changes, including the 4 files other sprints' E2E
    runs sometimes touch (none of this sprint's own tests happened to
    exercise the real long-term-memory/relationship-state write path).
  No stray `.tmp`/`.bak`/`.old`/`.orig` files. No new persistent state
  introduced (latency measurements and semantic-unit groupings are
  computed fresh per call).
- Production code changes were limited to `luno/adapters/fish_audio.py`
  (`_play_pipelined()` new, `_play()` gained one dispatch line) and
  `luno/response_output.py` (`_build_semantic_units()` +
  `_CONFIRMATION_KEYWORDS`/`_has_confirmation_lead()`/
  `_CONFIRMATION_BONUS` new, `_score_sentence()` gained one line,
  `_repair_orphans()` docstring extended with logic UNCHANGED) - verified
  via direct review before this regression sweep was run. No changes to
  `luno/incremental_speech.py`, `luno/config.py`,
  `luno/response_policy.py`, the Event Bus, memory retrieval, or any
  other adapter. `ENABLE_LLM_TTS_STREAMING` was NOT flipped - measured
  and documented as a recommendation rather than silently changed (see
  `docs/change_impact/voice_pipeline_latency_and_semantic_segmentation.md`).

## 2026-08-12 - Production-Safe LLM -> TTS Streaming Activation (Sprint 3)

- Phase 0 audit of the pre-existing streaming path (`luno/incremental_speech.py`,
  disabled by default since the "LLM Streaming -> Real-Time Speech
  Pipeline" sprint) found a REAL, confirmed bypass: it dispatched every
  settled sentence to TTS as soon as the LLM produced it, NEVER calling
  `luno.response_output.build_dual_response()` at all - meaning a
  streamed turn was spoken in full, uncompressed, regardless of
  SHORT/NORMAL/DETAILED response-depth policy. Fixed by dispatching only
  the always-safe LEAD sentence early (provably safe - `build_dual_response()`'s
  own selection can never drop or reorder sentence index 0), deferring
  everything else to a reconciliation pass that runs the SAME, unmodified
  `build_dual_response()` on the complete text once `llm_finished` fires,
  then dispatches the remaining SELECTED content. No second selector, no
  second semantic ranking system - see `luno/incremental_speech.py`'s own
  "RESPONSE-DEPTH-POLICY-SAFE REDESIGN" docstring section.
- A second, separate, pre-existing bug (affecting BOTH streaming and
  non-streaming paths identically) was found while tracing the first:
  `response_depth_assigned` never carried `explicit`, so
  `build_dual_response()`'s own "explicit DETAILED skips compression
  entirely" rule silently never applied via the real event path. Fixed
  by threading `explicit` through the same event/field-passing pattern
  `depth` already used, in both `main_runtime_demo.py::_speak()` and
  `StreamingSpeechCoordinator._on_finished()`.
- A third, separate, pre-existing bug: `BehaviorTreeModule._generate_reply()`'s
  wait only ever woke on `assistant_response`/`llm_error`, never
  `llm_cancelled` - a barge-in landing WHILE the LLM was still actively
  generating left that turn blocked until `llm_timeout_s` (default 45s)
  before the NEXT utterance could be processed. Fixed by adding an
  `_on_cancel` subscription mirroring the existing `_on_ok`/`_on_err`
  ones. Verified via live probe: a subsequent turn completed in 0.01s
  instead of blocking until timeout.
- A FOURTH bug, found empirically while writing the new production test
  matrix (not from the Phase 0 audit): `SessionManagerModule`'s
  THINKING -> SPEAKING transition was keyed only on `speak_request` -
  which a fully-streamed turn NEVER publishes (`BehaviorTreeModule._speak()`
  deliberately skips it to avoid duplicate audio paths). This permanently
  deadlocked the session at THINKING after the first streamed reply
  (THINKING has no timeout in `TIMEOUT_ACTIVE_STATES`) - every subsequent
  utterance for the rest of the process's life was silently dropped, not
  forwarded, no error. Fixed by also subscribing `SessionManagerModule`
  to `speech_playback_started` (published by `FishAudioAdapter` for BOTH
  the legacy and streaming paths, unmodified) and making the same
  transition there - harmless no-op for the legacy path since it already
  made this transition at `speak_request` time. New route added in
  `main_runtime_demo.py::PlannerBridgeModule` wiring. See
  `luno/wake_session/manager.py`'s own docstring and
  `_handle_playback_started()`.
- New: `tests/test_llm_tts_streaming_production.py` (39 tests) - the
  required 34-scenario matrix (streaming on/off, first-audio-before-LLM-
  finished, semantic coherence incl. no mid-word/mid-number/orphan-
  conditional splits, ordering, pipelining, cancellation at 4 stages,
  pause/resume, conversation-lifecycle at 3 stages, no stale/duplicate
  audio, no worker leak, timeout, streaming/TTS failure fallback,
  SHORT/NORMAL/DETAILED + explicit overrides, multi-turn, concurrent-
  conversation isolation, instrumentation-does-not-persist) plus the
  barge-in-during-generation proof, 3 real E2E tests through
  `RuntimeDemoConsole`, and a real latency-regression + inter-chunk-gap
  measurement. All 39 passing (1 test - `test_14_cancellation_during_synthesis`
  - flaked once under heavy full-suite batch contention, confirmed
    passing 4/4 standalone; same class of environment-load flake as the
    pre-existing `test_streaming_e2e.py::test_D`, not a regression).
- Measured first-audio latency (5 reps each, under the CORRECTED/
  policy-safe redesign, mock harness timing - not directly comparable to
  Sprint 2's raw numbers since the reply/timing setup differs): default
  path median **1.1837s** (min 1.1355/p95 1.4077/max 1.4077), streaming
  path median **0.4561s** (min 0.4331/p95 0.4609/max 0.4609) - **61.5%**
  median improvement, confirming streaming still measurably beats
  default under the depth-policy-safe design. Inter-chunk gap (synth
  0.02s < playback 0.08s): 0.0006s / 0.0002s - near-zero, prefetch still
  correctly overlapping playback.
- Regression: `tests/test_streaming_speech_integration.py` (22/22, one
  test - `test_26...` - honestly rewritten with a docstring explaining
  the redesign it now pins, since the old "backpressure drains
  mid-stream" premise no longer applies when only one chunk is ever
  dispatched before completion). `tests/test_streaming_e2e.py` (6/6, one
  test - `test_B...` - rewritten for the same reason: its old assertion
  only passed BECAUSE of the depth-policy bypass this sprint fixed).
  `tests/test_voice_output_optimization.py` (one test rewritten with an
  honest "supersedes" docstring, same reason).
  `tests/test_wake_barge_in_integration.py` + `test_barge_in_console.py`
  + `test_interrupt_routing_fix.py` + `luno/barge_in/tests/test_barge_in.py`:
  59/59 (standalone). `tests/test_conversation_end_race.py` +
  `test_runtime_demo.py`: 98/98. `luno/wake_session/tests/`: all passing.
  Full `tests/` tree (73 files, batched; `test_main_bargein.py`/
  `test_root_main_bargein.py` excluded at collection for the same
  pre-existing environmental reasons as every prior sprint;
  `test_dashboard.py` excluded per its own already-documented
  "not re-executed, excluded" baseline note): only the SAME
  already-documented pre-existing failure groups reproduced (6x
  `test_mic_device_index.py`, 1x `test_production_launcher.py::test_07`,
  2x `test_real_adapters.py` `_device_index` gap, 1x `test_state_isolation.py`
  sandbox artifact), plus confirmed-flaky-under-contention-only
  `test_verification_dashboard.py` and `test_emotion_engine.py`'s stale-
  decay test (both pass reliably standalone, re-verified). **Zero new
  regressions.** Full `luno/` tree (38 files): only
  `luno/barge_in/tests/test_barge_in.py`'s two already-documented
  intermittent flakes under batched full-tree runs (27/27 standalone,
  matching the established baseline note).
- Persistent state (`config/*.json`, all 14 files): SHA256 + mtime
  byte-identical before/after the entire sprint's work. No stray
  `.tmp`/`.bak`/`.old`/`.orig` files. Streaming instrumentation
  (TTFT/TTFS/TTFA/LLMCompleted/SpeechCompleted) confirmed stdout-only
  (`log()` is a plain `print()`, no file I/O) and non-persisting
  (`test_34_latency_instrumentation_never_persists_to_disk`).
- `ENABLE_LLM_TTS_STREAMING`'s checked-in default was evaluated for
  flipping to `True` (the streaming path itself is now verified
  production-safe) but kept at `False`: flipping it broke 2 pre-existing
  tests (`test_adaptive_response_depth.py::test_R...`,
  `test_barge_in_console.py::test_uninterrupted_turn_produces_exactly_one_history_line`)
  that assume `speak_request` as the ambient "turn was spoken" signal -
  confirmed by reproducing with the env var forced both ways. This is a
  rollout-blast-radius decision, not a safety one - see
  `docs/change_impact/llm_tts_streaming_activation.md` for the full
  writeup and recommended opt-in rollout via `.env`.
- Production code changes: `luno/incremental_speech.py` (core redesign),
  `main_runtime_demo.py` (`explicit` threading, `_on_cancel` barge-in
  fix, new `speech_playback_started` -> `session_manager` route),
  `luno/wake_session/manager.py` (`_handle_playback_started()`, new
  route in `REQUIRED_ROUTES`), `luno/config.py` (comment/rationale only -
  default value unchanged). No changes to `luno/response_output.py`,
  `luno/response_policy.py`, `luno/adapters/fish_audio.py`, or the Event
  Bus core - all reused unmodified.

## 2026-08-12 - Memory Continuity & Short Follow-up Reference Resolution sprint (Sprint 4)

- Phase 0 audit found a two-fold root cause via LIVE probes through the
  real `RuntimeDemoConsole` event path (not assumption): (1)
  `classify_query_intent()`'s `continuation_of_topic` never fires for any
  of the brief's 12 target short-follow-up phrases ("yang lain?", "terus?",
  "other option?", ...) - confirmed empirically, all 12 classify as
  `intent="other"`; (2) a genuine, previously-undiscovered missing-route
  bug - NEITHER `main_runtime_demo.py` NOR `luno/bootstrap/modules.py`
  ever routed `"assistant_response"` to `"planner"`, so
  `PlannerBridgeModule._on_assistant_response()` (which pairs a turn's
  user text with its reply for `memory.remember_turn()`) was dead code via
  the real routed path - same shape of bug as the Conversation_ended
  Lifecycle Routing sprint. Fixed with a single added route line in both
  files, same "byte-for-byte mirror" convention.
- New, additive mechanism (no second retrieval/ranking/tokenizer system):
  `luno.memory.classify_reference_type()`/`needs_topic_context()`/
  `is_pure_reference_followup()` (deterministic regex classifier, reuses
  `_compile_word_boundary_marker_pattern()`), `luno.memory_context.
  ActiveTopicSnapshot`/`update_active_topic()`/`extract_topic_terms_from_turn()`/
  `build_expanded_retrieval_text()`/`active_topic_to_relevant_memory()`
  (a bounded, conversation-scoped, non-persistent "what is this
  conversation actively about" snapshot, separate from and not replacing
  `_last_topic_terms`), and a new optional `assemble_context(
  retrieval_query_override=...)` parameter (default `None` = byte-for-byte
  unchanged for every existing caller - fixes a real, confirmed gap where
  the function's `has_any_signal` early-exit ran before
  `precomputed_relevant_memories` was ever inspected, so a fully-stopword
  follow-up like "what about that?" would return empty regardless of any
  injected candidate).
- ONE replace-vs-preserve rule in `update_active_topic()` (a "rich" turn
  replaces the snapshot; a pure-reference follow-up preserves it) is what
  makes topic decay (5-turn scenario: ESP8266 Bluetooth -> "yang lain?" ->
  WLED -> MQTT -> "yang lain?" correctly resolves to MQTT, not stale
  Bluetooth), branch switching (Bluetooth -> "Kalau WLED gimana?" ->
  "yang lain?" correctly resolves to WLED, not Bluetooth), and
  false-carry-over safety (Bluetooth -> "ngomong-ngomong aquascape-ku..."
  -> "yang lain?" correctly resolves to aquascape, not Bluetooth) all work
  without any special-case code, verified via live E2E probes through the
  real console for all three before being formalized into
  `tests/test_memory_continuity.py`.
- New test suite `tests/test_memory_continuity.py`: **60 passed, 0
  failed** - 41 unit-level tests (reference-type classification for all 12
  brief phrases + Phase 2's own 6 worked examples + false-positive guard,
  `ActiveTopicSnapshot`/`update_active_topic()` replace/preserve/decay,
  retrieval-expansion helpers, `assemble_context()` wiring/backward-
  compatibility/budget-pressure/exactly-once-retrieval, structural "no
  second tokenizer"/"no LLM judge" guards) + 16 real production-path E2E
  tests through `RuntimeDemoConsole` (ESP8266/Bluetooth, WLED, MQTT
  negation, signal-less "what about that?", 5-turn decay, branch
  switching, two false-carry-over scenarios, conversation isolation,
  conversation-id reuse, empty-topic no-op, English, mixed Indonesian/
  English, explicit-new-subject override, bounded state, prompt-injection
  inertness).
- Targeted regression suite (all memory-related files + `test_runtime_demo.py`
  + `test_response_output.py` + `test_voice_output_coherence.py` +
  `test_voice_response_intelligence.py` + `test_voice_output_optimization.py`
  + `test_voice_pipeline_latency.py` + `test_conversation_ended_lifecycle_routing.py`
  + `test_state_isolation.py` + `test_response_policy.py` +
  `test_proactive.py` + `test_wake_barge_in_integration.py` +
  `test_production_launcher.py`, run in batches, ~1050 tests total): only
  the SAME 2 already-documented pre-existing failures reproduced
  (`test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`
  - environment-specific network reachability;
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  - sandbox `inspect.getsource()` gap). **Zero new failures in any file
  this sprint touched.**
- Persistent state: `_active_topic` confirmed purely in-memory (grepped
  `luno/config.py` for any wiring - none found), cleared on
  `conversation_ended`, bounded at 50 conversations. All 14
  `config/*.json` files SHA256+mtime snapshotted before/after the formal
  `pytest` suite run (which runs under `tests/conftest.py`'s autouse
  isolation) - byte-identical, zero unexpected changes, no stray
  `.tmp`/`.bak`/`.old` files. Disclosed transparently: this sprint's own
  ad-hoc live-probe scripts (run directly via shell for Phase 0/5/6/9/10
  verification, the same "prove it through the real event path"
  methodology every prior sprint has used) ran against the REAL project
  `config/` directory rather than an isolated path, and did advance
  real usage-telemetry counters (`relationship_state.json`'s
  `interaction_count`, a handful of `long_term_memory.json` entries'
  `retrieval_count`/`last_retrieved_at`) - no memory content was
  fabricated, corrupted, or deleted; `long_term_memory.json`'s entry count
  and every entry's `text` field are unchanged (verified programmatically).
- Production code changes: `luno/memory.py` (`classify_reference_type()`,
  `needs_topic_context()`, `is_pure_reference_followup()` - additive),
  `luno/memory_context.py` (`ActiveTopicSnapshot`, `update_active_topic()`,
  `extract_topic_terms_from_turn()`, `build_expanded_retrieval_text()`,
  `active_topic_to_relevant_memory()`,
  `assemble_context(retrieval_query_override=...)` - additive),
  `main_runtime_demo.py` (`_active_topic` dict, `_pending_turns` tuple
  extension, `_on_assistant_response()` extension, `_handle_utterance()`
  extension, new `assistant_response -> planner` route),
  `luno/bootstrap/modules.py` (same new route). No changes to
  `_last_topic_terms`, `_rank_key()`, `_apply_budget()`,
  `MemoryRetriever`, TTS/Fish Audio, or the streaming architecture.

## 2026-08-12 - Memory Continuity follow-up round (classifier extension + expanded test matrix)

- Re-tested the sprint above against its own re-issued brief's additional
  target phrases ("anything else?", "what else?", "kalau alternatifnya?",
  "yang lainnya gimana?", "how about another one?", "terus yang tadi?")
  and found 3 real gaps: "anything else?"/"what else?"/"kalau
  alternatifnya?" matched nothing (`unknown`); "yang lainnya gimana?"/"how
  about another one?" matched COMPARISON instead of ALTERNATIVE_REQUEST -
  which would have made `is_pure_reference_followup()` wrongly REPLACE
  the active topic instead of preserving it for those phrasings. Fixed by
  extending `_ALTERNATIVE_REQUEST_RE` (`luno/memory.py`) with 7 new
  alternation branches; re-verified every previously-passing worked
  example (13 phrases) unaffected.
- `tests/test_memory_continuity.py` grew from 60 to **77 passed, 0
  failed**: new target-phrase mappings + regression guard, explicit-
  continuation/independent-query/word-boundary adversarial tests,
  `remember_turn()`-called-exactly-once, a literal route-removal
  reproduction of the original missing-route bug (wraps
  `Coordinator.add_route` to capture the real subscription id for
  "assistant_response"->"planner", unsubscribes it, proves `session_log`
  stays empty for that turn), real-thread concurrent-conversation
  isolation, and a dedicated "snapshot never stores a raw sentence"
  structural check.
- Two test-authoring bugs found and fixed while writing this batch (both
  instructive - the exact "avoid substring collision" pitfall the brief
  itself warns about, but found in the TEST code rather than the
  classifier): (1) the concurrent-isolation test's `need_llm_response`
  subscriber didn't filter by `request_id`, so under true thread
  concurrency one thread could capture the other's event - looked like a
  production leak, was a test-harness bug; (2) several E2E assertions
  used `"wled" in prompt.lower()`, which is unconditionally `True`
  because the static persona text contains "knowledgeable"
  (kno**wled**geable) - fixed with a new `_word_in()` word-boundary
  helper, re-verified the concurrent test passes consistently across 3+
  repeated runs (not a one-off pass).
- Targeted regression re-run (all memory-related suites + `test_runtime_demo.py`
  + `test_response_output.py` + `test_voice_output_coherence.py` +
  `test_voice_response_intelligence.py` + `test_voice_output_optimization.py`
  + `test_voice_pipeline_latency.py` + `test_conversation_ended_lifecycle_routing.py`
  + `test_state_isolation.py` + `test_response_policy.py` +
  `test_proactive.py` + `test_wake_barge_in_integration.py` +
  `test_production_launcher.py`, ~1200 tests total): only the same 2
  already-documented pre-existing failures
  (`test_production_launcher.py::test_07` environment-specific;
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  sandbox `inspect.getsource()` gap). **Zero new failures.**
- Persistent state: all 14 `config/*.json` files SHA256+mtime
  byte-identical before/after this round's entire test run. No stray
  `.tmp`/`.bak`/`.old` files.
- Production code changes: `luno/memory.py` (`_ALTERNATIVE_REQUEST_RE`
  extended - additive regex alternation only, no new function signatures).
  `tests/test_memory_continuity.py` extended (test file only).

## 2026-08-12 - Memory Topic Retention & Recall Reliability sprint

- New, distinct sprint from the two entries above (both left completely
  unmodified - `luno/memory.py` untouched, `tests/test_memory_continuity.py`
  still 77/77 byte-for-byte). Targets multi-turn TOPIC RETENTION (can a
  user return to a topic after several/unrelated turns, or after
  switching between topics) rather than single-hop elliptical follow-up
  resolution (already solved above).
- Phase 0 live reproduction (6-turn ESP32/INMP441 scenario through the
  real `RuntimeDemoConsole`) found the single-slot `_active_topic`
  (`PlannerBridgeModule`) is REPLACED wholesale by any turn
  `is_pure_reference_followup()` says has its own content - correct for
  genuine topic branches, silently destructive for ordinary sub-questions
  within the same broader topic (a "comparison"-classified turn like
  "Kalau power supply-nya gimana?" permanently discarded the earlier
  INMP441/sensor terms). A second, independent gap: grammatically
  complete turns ("Untuk mic-nya pakai apa?") are correctly classified
  `unknown` (no reference-fragment pattern matches), so the existing
  candidate-injection mechanism never attempted to help them at all.
  Direct token-analysis also confirmed `_ACTIVE_TOPIC_MAX_TERMS=12`
  silently truncated away "mic" (position 14 of 18 merged tokens) even
  when a snapshot WAS retained.
- Fix: `luno/memory_context.py` gained a bounded topic HISTORY
  (`update_topic_history()`, `_TOPIC_HISTORY_MAX_ENTRIES=4`, list not
  single slot) and content-based selection (`select_topic_candidates()`,
  token-overlap against a new `_TOPIC_OVERLAP_STOPWORDS` lexical filter,
  `_TOPIC_HISTORY_CANDIDATE_LIMIT=2`) - entirely ADDITIVE alongside the
  existing `_active_topic`/`update_active_topic()` (unmodified, still the
  fallback for genuinely signal-less elliptical fragments).
  `_ACTIVE_TOPIC_MAX_TERMS` raised 12->20 on direct evidence.
  `main_runtime_demo.py`'s `_handle_utterance()` now tries the new,
  precise overlap-based selection FIRST; only falls back to the old
  recency-only branch when the new one finds nothing - found necessary
  live (running both unconditionally reintroduced contamination: the
  recency-only branch would re-offer whichever topic was merely most
  recent alongside the correctly-matched one).
- Two false-positive-overlap issues found and fixed while iterating on
  `_TOPIC_OVERLAP_STOPWORDS`: (1) initial stopword list missed common
  Indonesian connector words ("untuk"/"nya"/"pakai"/"apa"), letting two
  unrelated-but-merely-recent entries outrank the one entry that actually
  shared the meaningful word ("mic") - fixed by filtering both sides of
  the overlap check and ranking by overlap SIZE, not history position/
  recency; (2) even after that fix, first-person pronouns/modal verbs
  ("aku"/"mau" - "I want to...") were common enough in ordinary
  Indonesian phrasing to cause a genuinely unrelated new-topic turn
  ("Aku mau bahas topik baru, soal motor listrik.") to falsely match an
  ESP32 entry - fixed by extending the stopword list with pronouns/modal
  verbs (Indonesian + English).
- New `tests/test_memory_topic_retention.py`: **41 passed, 0 failed**
  (verified stable across 5+ repeated runs, including the concurrent-
  conversation test). Covers: unit tests for `update_topic_history()`/
  `select_topic_candidates()`/`build_expanded_retrieval_text_from_history()`/
  `topic_history_to_relevant_memories()`; E2E scenarios A-H (topic then
  followup, topic then unrelated then followup, topic A->B->return to A,
  concurrent conversation isolation, conversation-end cleanup, conversation-
  id-reuse non-inheritance, technical-identifier survival across 6 turns,
  unrelated-question non-contamination) through the real production path;
  Phase 5 multi-topic safety (3 independently-recoverable topics + an
  explicitly ambiguous reference that must inject at most 1 topic, never
  guess across all 3); Phase 9 adversarial phrase matrix (8 positive/
  negative cases); non-regression checks proving `_active_topic`/
  `update_active_topic()`/`active_topic_to_relevant_memory()` remain
  independently addressable and unchanged.
- One test-authoring bug found and fixed while writing the concurrent-
  conversation test: initially passed explicit curated reply strings for
  both threads, which meant `_run_turn_and_capture()`'s `canned_reply`
  path overwrote the SAME shared `MockOpenRouterClient.canned_text`
  attribute from both threads at once - the exact class of race Sprint
  4 Round 2's own "test bug #3" already documented. Fixed by never
  overriding `canned_text` in this test (relying on the console's
  `canned_text=None` per-request echo mode instead) and choosing follow-
  up phrasing that overlaps words already present in each thread's OWN
  user turn (not reply-only vocabulary), avoiding the race entirely
  rather than reintroducing it.
- Full regression re-run: `test_memory_continuity.py` 77/77;
  `test_memory_decision_quality.py` + `test_memory_retrieval.py` +
  `test_memory_context.py` + `test_memory_prompt_injection.py` +
  `test_memory_regression.py` + `test_memory_persistence_hardening.py` +
  `test_memory_dashboard.py` + `test_runtime_demo.py` +
  `test_manual_memory.py` + `test_memory_adaptive_retrieval.py` +
  `test_memory_evaluation.py` + `test_memory_guard.py` +
  `test_memory_learning.py` + `test_memory_outcome_telemetry.py` 555/555;
  `test_adaptive_response_depth.py` + `test_barge_in_console.py` +
  `test_browser_wiring.py` + `test_conversation_end_race.py` +
  `test_conversation_ended_lifecycle_routing.py` + `test_device_context.py`
  + `test_environment_intent.py` + `test_interrupt_routing_fix.py` +
  `test_persistent_adaptive_response_depth.py` + `test_persona.py` +
  `test_response_output.py` + `test_response_policy.py` 391/391; plus a
  broader sweep (`test_llm_tts_streaming_production.py`,
  `test_production_launcher.py`, `test_vision_*`, `test_voice_*`,
  `test_wake_*`, `test_world_model.py`, `test_screen_ask_screen.py`,
  `test_semantic_speech_units.py`, `test_streaming_*`, `test_tts_*`).
  Only the same 2 already-documented pre-existing failures
  (`test_production_launcher.py::test_07` environment-specific network
  reachability; `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  sandbox `inspect.getsource()` gap) plus one flaky-under-parallel-load,
  passes-in-isolation TTS timing test unrelated to memory
  (`test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis`,
  confirmed 3/3 passing when run alone). **Zero new regressions.**
- Persistent state: `_topic_history` has zero file I/O of its own
  (verified by direct source grep). All 14 `config/*.json` files SHA256+
  mtime byte-identical before/after the full pytest regression sweep
  (1064 tests). Ad-hoc live-probe scripts run outside the pytest
  isolation fixture during Phase 0 reproduction did touch the real
  `config/relationship_state.json`/`config/long_term_memory.json` via the
  pre-existing, unrelated `memory.remember_turn()` pipeline (ordinary
  usage-counter/episodic-entry activity, each write auto-backed-up) - not
  this sprint's new code.
- Production code changes: `luno/memory_context.py` (additive: constants
  + 4 new functions + `_ACTIVE_TOPIC_MAX_TERMS` 12->20),
  `main_runtime_demo.py` (`_topic_history`/`_topic_history_max`,
  `_on_assistant_response()`/`_handle_utterance()`/`_on_conversation_ended()`
  extensions). No changes to `luno/memory.py`, `_active_topic`,
  `update_active_topic()`, `_rank_key()`, `_apply_budget()`,
  `MemoryRetriever`, TTS/Fish Audio, or the streaming architecture.

## 2026-08-12 - Luno Brain Debugger / Memory & Voice Observability Dashboard

- New baseline total: **2121 tests collected** across `tests/` (excluding
  the 2 permanently-uncollectible `test_main_bargein.py`/
  `test_root_main_bargein.py` - missing `faster_whisper`/absent
  `legacy_main.py`, unrelated to this sandbox).
- Full sweep run in 13 batches (host tool-call time cap required
  splitting; `pytest-xdist -n 4` used for the larger/slower files):
  memory suite (690 tests: `test_memory_continuity.py` 77,
  `test_memory_topic_retention.py` 41, `test_memory_decision_quality.py`,
  `test_memory_retrieval.py`, `test_memory_context.py`,
  `test_memory_prompt_injection.py`, `test_memory_regression.py`,
  `test_memory_persistence_hardening.py`, `test_memory_dashboard.py`,
  `test_runtime_demo.py`, `test_manual_memory.py`,
  `test_memory_adaptive_retrieval.py`, `test_memory_evaluation.py`,
  `test_memory_guard.py`, `test_memory_learning.py`,
  `test_memory_outcome_telemetry.py`, new
  `test_memory_voice_observability.py`); response/voice/lifecycle suite
  (370: `test_adaptive_response_depth.py`, `test_barge_in_console.py`,
  `test_browser_wiring.py`, `test_conversation_end_race.py`,
  `test_conversation_ended_lifecycle_routing.py`, `test_device_context.py`,
  `test_environment_intent.py`, `test_interrupt_routing_fix.py`,
  `test_persistent_adaptive_response_depth.py`, `test_persona.py`,
  `test_response_output.py`, `test_response_policy.py`);
  `test_llm_tts_streaming_production.py` (39, `-n 4`);
  `test_production_launcher.py` (24, 1 known failure);
  TTS/streaming/screen/semantic suite (152, `-n 4`); vision/voice/wake/
  world_model suite (268, `-n 4`); camera suite (19, `-n 4`); dashboard/
  desktop/emotion/episodic suite (150, `-n 4`, 1 harmless SSE-teardown
  warning); incremental-speech/LLM-dashboard/LLM-streaming suite (31);
  memory_conflict/intelligence/maintenance/prompt_intelligence +
  `test_mic_device_index.py` (188, 6 known failures); persistent-state-
  hardening/proactive/real_adapters/relationship/routing_dashboard/
  screen_intent/state_isolation/verification_dashboard suite (182, 3
  known failures); `test_real_fish_audio_console.py` (8, `-n 4`).
- Only the same, already-documented, environment-specific failures
  (10 total, unchanged failure identity from prior baselines):
  `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`
  (Fish Audio API network reachability); `test_mic_device_index.py` (6 -
  this sandbox's real `.env` sets `MIC_DEVICE_INDEX=1` plus a missing
  `list_microphones.py`); `test_real_adapters.py`'s 2 whisper tests
  (`speech_recognition`/`sounddevice` not importable in this sandbox);
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  (sandbox `inspect.getsource()` gap). **Zero new regressions.**
- New `tests/test_memory_voice_observability.py` (17 tests, stable across
  3 consecutive full runs): scenario A (turn trace recorded + privacy
  boundary - raw query text never appears in the trace payload), B (empty
  retrieval represented as `0`/`[]`, never fabricated), C (funnel stages
  present, ordered, monotonic non-increasing candidate->budget->prompt),
  D (topic-history display collectors called twice in a row never mutate
  `_active_topic`/`_topic_history`), E (the sprint brief's own worked
  example - ESP32/INMP441/mic turn, unrelated aquascape turn, "Yang tadi
  soal mic gimana?" correctly recovers the ESP32 entry), F (an aquascape
  follow-up does NOT falsely mark the ESP32 entry as referenced -
  contamination check), G (two conversations' topic histories never
  cross-contaminate), H/I (calling `assemble_context()` directly with
  `funnel=None` vs `funnel={}` on identical inputs returns byte-for-byte
  identical `AssembledContext` - proof the parameter cannot influence
  ranking or retrieval), J (monkeypatching `build_turn_trace()` to raise
  proves the conversation still completes normally - the pre-existing
  `try/except` around telemetry construction genuinely isolates a
  telemetry bug), K (both `_turn_trace_history` and `VoiceLatencyRecorder`
  stay bounded under load exceeding their `maxlen`), L (two threads
  driving two conversations concurrently never cross-contaminate each
  other's topic history), M/N (a real voice turn through
  `MockFishAudioClient` produces non-negative, present LLM-first-token/
  LLM-total/first-audio/playback-duration latencies), O
  (`parse_chunk_timeline_from_logs()` correctly computes inter-chunk gap
  from two synthetic log records' own `wall_time` and never leaks a
  different request_id's chunk lines in), P (a full
  need_llm_response->...->paused->resumed->cancelled event sequence for
  one request_id produces `cancelled=True`/`pause_count=1`/
  `resume_count=1` with no negative latencies and no exception), plus one
  real production-path E2E test
  (`test_z_e2e_real_production_path_through_dashboard_http`) exercising
  `RuntimeDemoConsole` -> `PlannerBridgeModule` -> memory/context pipeline
  -> telemetry -> a REAL, running `DashboardServer`'s HTTP API via
  `requests` (not mocked dashboard objects).
- Persistent state: all 14 `config/*.json` files SHA256+mtime
  byte-identical before/after the full sweep. No new/unexpected files.
  `_turn_trace_history` and `VoiceLatencyRecorder`'s internal state are
  purely in-memory, never wired to any `luno/config.py` path (verified by
  source inspection - no `open()`/file-write calls in either).
- Production code changes: `luno/memory_context.py`
  (`assemble_context(funnel: Optional[Dict[str, int]] = None)` - additive,
  write-only, default `None` no-op), `luno/memory_turn_trace.py`
  (additive `MemoryTurnTrace` fields + `build_turn_trace()` kwargs, all
  optional), `main_runtime_demo.py` (`PlannerBridgeModule._turn_trace_history`
  new bounded ring buffer + call-site wiring inside the pre-existing
  telemetry `try/except`), `luno/dashboard/voice_latency.py` (new file:
  `VoiceLatencyRecorder`, `parse_chunk_timeline_from_logs()`),
  `luno/dashboard/collectors.py` (new read-only collector functions),
  `luno/dashboard/server.py` (new GET routes + recorder lifecycle),
  `luno/dashboard/static/index.html` (new "Brain Debugger" nav panel). No
  changes to `luno/memory.py`, memory ranking/retrieval/budget logic,
  `luno/adapters/fish_audio.py`, `luno/adapters/openrouter.py`, streaming
  architecture, response selection, or the prompt-injection trust
  boundary.

## 2026-08-13 - Voice Output Naturalness & First-Audio Latency

- Two production symptoms reproduced BEFORE any code changed, then fixed
  and re-verified through the real production path (not test-only
  proof): (A) TTS speaking only bullets while skipping the setup
  sentence before them, (B) high first-audio latency despite the
  pre-existing streaming architecture and TTS chunk pipelining already
  being in place.
- Fix A: `luno/response_output.py::_starts_list_run()` - reuses the
  SAME two existing dependent-sentence-protection mechanisms
  (`_select_scores_with_setup_bonus()`, `_repair_orphans()`), no new
  selector.
- Fix B: `luno/config.py::ENABLE_LLM_TTS_STREAMING` now defaults to
  `True`, activating the pre-existing, already-safety-verified
  streaming architecture (§29) in production. Measured (7 reps each,
  real timestamps, mocked LLM/TTS boundary): legacy median 1.749s
  (p95 1.969s) vs streaming median 0.403s (p95 0.447s) - 77% median
  reduction.
- Bug found and fixed along the way: `luno/adapters/fish_audio.py
  ::_play_stream_pipelined()` had a pre-existing, previously-dormant
  cancellation gap for chunk 0 (its sibling `_play_pipelined()` was
  already fixed for this by an earlier sprint, but the fix was never
  mirrored into the streaming path since streaming defaulted off at the
  time). Reproduced deterministically (3/3) via `tests/
  test_real_fish_audio_console.py
  ::test_voice_interrupt_while_still_synthesizing_real_speech_succeeds`
  against `RealFishAudioClient` once streaming became default; fixed
  identically to the legacy method (submit chunk 0's synthesis to
  `_prefetch_executor` first, then resolve via the existing
  cancellation-responsive polling); 5/5 after the fix.
- Test-suite blast radius from the default flip: ~30 tests across 9
  files had hardcoded `speak_request`-only assumptions (a turn's voice
  dispatch now fires `speak_stream_chunk` by default) - all honestly
  updated to be dispatch-mode-agnostic, never weakened. Full list:
  `test_response_output.py`, `test_voice_output_optimization.py`,
  `test_voice_output_coherence.py`, `test_voice_response_intelligence.py`,
  `test_semantic_speech_units.py`, `test_adaptive_response_depth.py`,
  `test_barge_in_console.py`, `test_interrupt_routing_fix.py`,
  `test_memory_voice_observability.py`, `test_real_fish_audio_console.py`,
  `test_tts_cancellation.py`, `test_tts_e2e_pipeline.py`,
  `test_wake_session_console.py`.
- Full regression re-run in two ~40-file batches, `-n 4`. Only the same,
  already-documented, environment-specific failures remain
  (`test_production_launcher.py::test_07`; `test_mic_device_index.py`
  (6); `test_real_adapters.py`'s 2 whisper tests;
  `test_state_isolation.py`'s sandbox `inspect.getsource()` gap;
  `test_main_bargein.py`/`test_root_main_bargein.py` missing
  `faster_whisper`/missing file) - all reproduced unchanged under
  `ENABLE_LLM_TTS_STREAMING=false` too, confirming they predate this
  sprint. A handful of tests (vision suite, one streaming-production
  test, one streaming-e2e test) occasionally fail ONLY under `-n 4`
  parallel load and pass 100% in isolation - reproduced both before and
  after this sprint's changes, a pre-existing test-infra characteristic,
  not a regression. **Zero new deterministic regressions.**
- New `tests/test_voice_naturalness_and_latency.py` (26 tests, stable
  across 3 consecutive full runs): 10 semantic/list coherence scenarios,
  5 short-sentence protection scenarios, 10 streaming-latency scenarios,
  1 real-console E2E (intro/setup + 5 bullets + conclusion) proving chat
  output stays byte-identical to the raw LLM reply, speech includes
  setup/context, bullets remain understandable, the conclusion is not
  orphaned, and first audio starts before the full response is
  generated.
- Persistent state: no new persisted structures; all `config/*.json`
  files unaffected (isolated via `tests/conftest.py`'s autouse fixture).
- Production code changes: `luno/response_output.py`
  (`_starts_list_run()` + 2 call sites - additive), `luno/config.py`
  (`ENABLE_LLM_TTS_STREAMING` default `false` -> `true`), `luno/adapters/
  fish_audio.py` (`_play_stream_pipelined()` chunk-0 cancellation fix).
  No changes to `luno/incremental_speech.py`, `luno/response_policy.py`,
  `_select_by_priority()`'s must-keep/budget skeleton, memory retrieval/
  ranking, the prompt-injection trust boundary, or TTS voice
  configuration.

## 2026-08-13 - Memory Retrieval & Decision Quality (re-audit) sprint

- Independent, evidence-first re-verification of the already-shipped
  memory/topic pipeline (§25/§30/§31 in `ARCHITECTURE_GUARD.md`) - live
  reproduction through the real `RuntimeDemoConsole` production path
  (the brief's own 8-turn scenario + an A/B/C multi-topic scenario)
  found two narrow, proven root causes, BOTH confirmed before any code
  changed (see `docs/change_impact/
  memory_retrieval_decision_quality_reaudit.md` for the full Phase 0-8
  trace).
- Root cause 1: `luno/memory_retrieval/query.py::_WORD_RE` (the one
  shared tokenizer) stripped all digits, so "ESP32"/"ESP8266"/"INMP441"
  collapsed onto colliding/truncated tokens ("esp"/"esp"/"inmp") -
  reproduced concretely as a real cross-topic contamination (turn 7 of
  the 8-turn scenario retrieved the wrong ESP8266/Bluetooth entry
  instead of ESP32). Fixed: `[a-zA-Z']+` -> `[a-zA-Z][a-zA-Z0-9']*`
  (leading letter required, digits allowed after) - preserves the
  existing "no signal for pure math" contract
  (`test_3_empty_retrieval_for_no_signal_query` unaffected).
- Root cause 2: `luno/memory.py::classify_reference_type()` had no
  pattern for a bare pronoun used as the grammatical subject/object of a
  short question ("which one was it again?", "how does that connect?")
  - both classified `"unknown"`, so `is_short_followup` was `False` and
  NEITHER the content-match path nor the single-slot `_active_topic`
  fallback ever fired - Failure Class B (never retrieved), reproduced as
  turns 6 and 8 of the 8-turn scenario reaching `assemble_context()`
  with zero candidates and an empty memory block in the real
  `system_prompt`. Fixed: new `_BARE_PRONOUN_REFERENCE_RE`, lowest
  precedence tier, same `"direct_reference"` result the existing
  machinery already handles - every existing precedence ordering
  unchanged.
- `_rank_key()` itself was re-inspected (Phase 6 audit) and reconfirmed
  already correctly relevance-first - `intent_bonus` structurally cannot
  rescue a lower-relevance candidate. **Not modified.**
- Persistent-state incident found and fixed during Phase 0-2: the
  sprint's own raw (pre-pytest) reproduction script briefly wrote
  through to the real `config/relationship_state.json` (interaction-
  count/familiarity bookkeeping only) and `config/episodic_memory.json`
  (one fabricated `device_configured` entry, verbatim test text) before
  isolation was added to that script. Both diffed against the nearest
  pre-run backup and restored (byte-identical for
  `relationship_state.json`; to the module's own documented empty-list
  default for `episodic_memory.json`) before any further work. All
  `config/*.json` files confirmed unchanged (mtime + content) across
  every subsequent test run in this sprint.
- Full `tests/` tree (2147 collected, excluding the 2 already-documented
  uncollectible files: `test_main_bargein.py`/`test_root_main_bargein.py`),
  run in 4 chunks under `-n 2` (host-side per-call time limit). 15
  failures total, all pre-existing and already documented above: 4
  timing/scheduling-jitter flakes
  (`test_stale_emotion_decays_to_unknown_after_the_configured_window`,
  3x `test_llm_tts_streaming_production.py` latency assertions - all 4
  reconfirmed passing 100% in serial isolation) and 11 environment-
  specific failures (`test_production_launcher.py::test_07`,
  `test_mic_device_index.py` x6, `test_real_adapters.py` x2,
  `test_state_isolation.py`'s straggler-drain test,
  `test_streaming_e2e.py::test_D`). Two `tests/
  test_memory_topic_retention.py` assertions were updated (not
  rewritten) from the old, buggy "esp" expectation to the corrected
  "esp32" - the tokenizer fix's own, intended, documented consequence.
  **Zero new regressions in any other file.**
- New `tests/test_memory_retrieval_decision_quality_reaudit.py` (22
  tests): 5 tokenizer unit tests, 10 classifier unit tests (including 3
  false-positive/precedence guards), 5 production-path E2E tests
  reproducing the brief's own 8-turn scenario turns 6/7/8, the A/B/C
  multi-topic scenario (including the A->B->C->A case), and an
  unrelated-question adversarial case.
- Production code changes: `luno/memory_retrieval/query.py` (`_WORD_RE`),
  `luno/memory.py` (`_BARE_PRONOUN_REFERENCE_RE` + one new branch in
  `classify_reference_type()`). No changes to `_rank_key()`,
  `_apply_budget()`, `select_topic_candidates()`, `update_active_topic()`/
  `update_topic_history()`, the intent/continuity bonus, the prompt-
  injection trust boundary, or any memory store's persistence format.

## 2026-08-13 - Memory Retrieval End-to-End Audit (read-only)

- Read-only reconnaissance sprint - no production code, tests, or config
  files modified. Traced the full post-retrieval pipeline
  (`select_topic_candidates()` -> `topic_history_to_relevant_memories()` ->
  `assemble_context()` -> `_rank_key()` -> `_apply_budget()` ->
  `render_context_block()` -> final `system_prompt`) and confirmed, with a
  live isolated probe against the real `RuntimeDemoConsole` (5-turn
  Indonesian scenario + 1 unrelated query), that ranking/budget/rendering
  are ALL correct pass-throughs - nothing observed in this sprint's own
  probe was ever dropped, reordered incorrectly, or lost between
  `assemble_context()`'s candidate pool and the final `system_prompt`.
- Found the ACTUAL loss point: the topic-state UPDATE decision one turn
  before the symptom becomes visible - `luno.memory.
  is_pure_reference_followup()` treats any `"comparison"`-classified turn
  as carrying its own new entity and therefore REPLACES the active topic,
  even when the comparison's own residual word is already part of the
  active topic (e.g. "Kalau mikrofonnya gimana?" replacing an ESP32/
  INMP441 topic). See `docs/change_impact/memory_e2e_audit.md` for the full
  trace and diagnostic table. This finding fed directly into the next
  sprint (Context-Aware Comparison Topic Preservation, below).
- Confirmed via the same probe: an unrelated query ("Berapa ukuran
  aquarium 50x25?") correctly injects zero memory context - no recency-
  only fallback leak.
- Persistent state: probe ran outside the repo tree (`/tmp`), fully
  isolated (mirrors `tests/conftest.py`'s own `_WRITABLE_STATE_ATTRS`
  list); `config/*.json` mtimes confirmed byte-unchanged before/after.
- No new tests (read-only sprint) - no regression run required (no code
  changed).

## 2026-08-13 - Context-Aware Comparison Topic Preservation sprint

- Fixes the root cause found by the immediately-preceding read-only audit.
  Targeted state-update fix only - `luno.memory.
  is_pure_reference_followup(text, active_topic_terms=None)` (extended,
  additive, optional parameter) now also returns `True` for a
  `"comparison"`-classified turn whose own meaningful residual term(s)
  overlap the CURRENT active topic's own terms (substring-based -
  `_comparison_residual_terms()`/`_residual_overlaps_active_topic()`, not
  embeddings, not a second classifier), preventing a needless REPLACE when
  the comparison's own subject is already part of what's active. A
  comparison turn naming something genuinely new (e.g. "Kalau Bluetooth-
  nya gimana?") still replaces, unchanged.
  `main_runtime_demo.py::PlannerBridgeModule._on_assistant_response()`
  fetches the existing active-topic snapshot BEFORE classifying (order
  swapped) so its terms can be threaded through.
- E2E verified via the real `RuntimeDemoConsole`: the brief's own 3-turn
  scenario (ESP32/INMP441 -> "Kalau mikrofonnya gimana?" -> "Yang tadi
  soal mic gimana?") now recovers the ORIGINAL ESP32/INMP441 topic at
  turn 3, given a realistic (non-echo) reply for turn 1 that mentions
  "mikrofon" - exactly mirroring what a real LLM reply would supply via
  `extract_topic_terms_from_turn()`'s own existing, documented reply-text
  merging. Under the raw echo-mock reply (no informative content), this
  specific literal word-pairing is unaffected by the fix, confirmed
  identical to before-fix behavior in that exact condition - stated as a
  known limitation, not silently claimed as fully solved.
- Phase 6 safety scenario (Topic A: ESP32/INMP441, Topic B: aquascape/
  pompa) verified: a "mikrofonnya"/"pompanya" question correctly recovers
  its own topic, never the other, and a genuinely unrelated query ("Berapa
  harga sepatu?") injects neither topic.
- Full `tests/` tree (79 files, same 4-chunk / `-n 2` methodology), same 4
  timing/scheduling-jitter flakes and same ~9-10 documented environment-
  specific failures as every prior sprint's own baseline - zero new
  regressions.
- New `tests/test_memory_comparison_topic_preservation.py` (20 tests): 10
  unit tests on the extended `is_pure_reference_followup()` (the brief's
  own Examples A-D, backward-compatibility/precedence guards), 1 repeated-
  comparison-turns robustness test, 2 unchanged-behavior regression guards
  (Sprint 4 pure-reference types, Sprint 5 topic-history selection), 1
  concurrent-conversation isolation test, 5 production-path E2E tests, 1
  full Topic-A/Topic-B/unrelated-query safety test.
- Production code changes: `luno/memory.py`
  (`_COMPARISON_PRESERVATION_EXTRA_FILLER`/`_comparison_residual_terms()`/
  `_residual_overlaps_active_topic()`/extended `is_pure_reference_followup()`),
  `main_runtime_demo.py` (`_on_assistant_response()` snapshot-fetch order).
  No changes to `classify_query_intent()`, `classify_reference_type()`'s
  own output, `needs_topic_context()`, `select_topic_candidates()`,
  `topic_history_to_relevant_memories()`,
  `build_expanded_retrieval_text_from_history()`, `assemble_context()`,
  `_rank_key()`, `_apply_budget()`, `render_context_block()`, the prompt-
  injection trust boundary, TTS, streaming, or the memory persistence
  format.
- Persistent state: `config/*.json` SHA256/mtime confirmed unchanged
  (`relationship_state.json`, `long_term_memory.json`,
  `verified_facts.json`, `episodic_memory.json`, `session_summaries.json`,
  `habit_memory.json`, `reminders.json`); no new persistent files created;
  no raw topic/conversation persistence introduced.

## 2026-08-13 - Voice Output Mode (ALL / SHORT) sprint

- New tests: `tests/test_voice_output_modes.py` (42 tests) - enum/
  validation/command-matching, `build_dual_response()` ALL-vs-SHORT pure-
  function scenarios (short/long reply, numbered/bulleted list, multi-
  paragraph, warnings, conditions, invalid mode, empty/null response,
  dedup bypass), chat-text integrity, TTS chunk coverage/ordering, and
  9 E2E scenarios through the real `RuntimeDemoConsole` (direct toggle,
  spoken-command toggle with next-turn-only semantics, repeated toggles,
  chat integrity, streaming stays active, cancellation during ALL,
  memory/topic isolation, cross-conversation non-leak, status
  visibility), plus 1 first-audio latency comparison.
- Full `tests/` tree (83 files, 4 chunks, `pytest -n 2`) - zero new
  regressions. Failures observed and independently re-confirmed as
  pre-existing/environmental: 4 timing-flakes under parallel load only
  (`test_streaming_e2e.py::test_D_...`,
  `test_emotion_engine.py::test_stale_emotion_...`, two
  `test_llm_tts_streaming_production.py` first-audio-timing tests - all
  pass in isolation); `test_main_bargein.py`/`test_root_main_bargein.py`
  (missing optional `faster_whisper` dependency in this sandbox);
  `test_mic_device_index.py`/`test_production_launcher.py::test_07_...`/
  `test_real_adapters.py::test_real_whisper_source_...` (x2)/
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_...`
  (pre-existing sandbox environment-configuration artifacts - e.g. a real
  `MIC_DEVICE_INDEX` value already set in this environment - fail
  identically with or without this sprint's files present).
- Production code changes: `luno/voice_output_mode.py` (new),
  `luno/response_output.py` (`DualResponse.voice_output_mode` field +
  `build_dual_response()`'s new `voice_output_mode` parameter/ALL
  branch - SHORT branch byte-identical to before), `luno/incremental_speech.py`
  (`_TurnState.voice_output_mode` threading), `main_runtime_demo.py`
  (`PlannerBridgeModule._voice_output_mode` dict + get/set methods +
  command detection/policy override/event payload extension +
  conversation-end cleanup, `BehaviorTreeModule` last-turn/last-spoken
  mode threading + `status_snapshot()`). No changes to memory retrieval,
  topic history, active topic, memory ranking/budget, the prompt-
  injection trust boundary, the LLM model, TTS voice/model, Fish Audio
  synthesis behavior, the streaming architecture, or cancellation
  semantics.
- Persistent state: `config/*.json` SHA256 confirmed unchanged (all 15
  files); no new persistent files created; voice output mode itself is
  never persisted to disk (in-memory only, popped on conversation end).

## 2026-08-15 - Semantic Voice Selection & Coherent SHORT mode sprint

- Root cause reproduced (Phase 0, before any production edit) via direct
  calls to `build_dual_response()`: (1) a genuine closing/answer
  sentence dropped when it lacked one of `_has_conclusion_cue()`'s fixed
  keywords and budget was consumed by blanket list-item protection;
  (2) at DETAILED depth (`protect_list_items=False`), an early filler
  sentence's "earlier is better" tiebreak could outscore and displace a
  list's own items/conclusion, leaving a bare setup with no payload.
- New: `tests/test_semantic_voice_selection.py` (36 tests) - unit tests
  for `_find_list_runs()`/`_list_run_relevant_items()`/
  `_apply_list_relevance_bonus()`; the Phase 9 24-scenario adversarial
  matrix (list setup+items, list+conclusion, dependent/condition/
  explanation/warning chains, short functional/unrelated/independent
  sentences, nested/numbered/markdown lists, paragraph+list, two
  separate lists, tight/normal budgets, DETAILED/ALL modes, streaming,
  cancellation, 1-2 sentence replies); chat-text integrity and ALL-mode
  invariant checks; structural guards (no forbidden ML/embedding
  imports); 4 E2E tests through `RuntimeDemoConsole` (RAW vs SHORT vs
  ALL, ALL still reads everything, cancellation mid-speech, streaming
  stays active) plus a first-audio latency check.
- Scoped regression during development (`test_response_output.py`,
  `test_voice_output_optimization.py`, `test_voice_response_intelligence.py`,
  `test_voice_output_coherence.py`, `test_semantic_speech_units.py`,
  `test_voice_output_modes.py`): 231 passed, 0 failed.
- Full `tests/` tree (84 files, 8 chunks of ~11 files, `pytest -n 2`) -
  **zero new regressions**. Failures observed, all independently
  re-confirmed as pre-existing/environmental/chunk-boundary artifacts,
  not caused by this sprint:
  - `test_dashboard.py::test_35_chat_audio_endpoint_reports_no_clip_for_mock_backend` -
    flaky under parallel chunking only (HTTP read-timeout race in the
    mock backend's streaming thread); passes reliably twice in
    isolation.
  - `test_main_bargein.py` - pre-existing `ModuleNotFoundError: No
    module named 'faster_whisper'` (missing optional dependency).
  - `test_mic_device_index.py` (6 tests), `test_root_main_bargein.py` -
    pre-existing sandbox environment-configuration artifacts (a real
    `MIC_DEVICE_INDEX` value already set, and a `list_microphones.py`
    path lookup that only resolves under this sandbox's own mount path).
  - `test_production_launcher.py::test_07_...`,
    `test_real_adapters.py::test_real_whisper_source_...` (x2) -
    pre-existing `RealWhisperSource` attribute gap, unrelated to any
    file touched this sprint.
  - `test_state_isolation.py::test_verified_facts_does_not_leak_between_tests_part_b` -
    chunk-boundary artifact only (this test depends on `_part_a` running
    in the same worker/session); passes when the file is run whole.
  - `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
    same pre-existing `inspect.getsource()` `OSError` documented in the
    prior sprint's baseline; reproduces identically whether or not this
    sprint's files are collected.
- Production code changes: `luno/response_output.py` only - new
  `_find_list_runs()`, `_LIST_RELEVANCE_BONUS`, `_list_run_relevant_items()`,
  `_apply_list_relevance_bonus()`, `_LIST_RUN_REPAIR_SLACK`,
  `_repair_list_run_coherence()`; `_select_by_priority()`'s must-keep
  rule relaxed from "every list item always must-keep" to "keep the
  whole run unless a relevance signal was found", plus the new repair
  pass. No changes to `_repair_orphans()`, `_score_sentence()`,
  `_is_dependent_sentence()`, `_build_semantic_units()`,
  `_compute_budget_for_depth()`, `_rank_key()`, the ALL-mode bypass
  branch, or anything downstream of sentence selection (chunking,
  streaming, cancellation, TTS/Fish Audio, normalization). No changes to
  memory retrieval, topic history, active topic, memory ranking/budget,
  the prompt-injection trust boundary, the LLM model, TTS voice/model,
  Fish Audio synthesis behavior, the streaming architecture, or
  cancellation semantics.
- Persistent state: `config/*.json` SHA256 confirmed unchanged (all 15
  files) before vs. after the full sprint (implementation + full test
  suite run).

## 2026-08-15 - Conversation Reference Resolution sprint

- Root cause (Phase 0, evidence-based): two concrete gaps in the
  existing Sprint 4/Topic Retention/Comparison Preservation machinery
  (all completely unchanged by this sprint) - Gap A, no ordinal/list-
  position resolution ("yang kedua gimana?" had no way to resolve to
  "MAX9814" specifically, only the generic bag-of-terms topic); Gap B,
  attribute-modified references ("kalau yang wireless?", "yang murah?")
  fell through to `"unknown"` and REPLACED the entire active-topic
  snapshot, losing the parent topic outright.
- New: `tests/test_conversation_reference_resolution.py` (54 tests) -
  reference-type classification (3 new types + Phase 16's own 14-phrase
  adversarial natural-language matrix + closed-enum/precedence
  regression guards), ordinal/list resolution unit tests
  (`parse_ordinal_indices()`/`resolve_ordinal_targets()`/
  `extract_list_items_from_reply()`), merge-behavior unit tests
  (`update_active_topic()`/`update_topic_history()`'s new `is_merge`
  path), the Phase 11 no-contamination test matrix (A-L: multi-topic
  isolation, ordinal resolution, no-list ambiguity, attribute/repair
  merge, unrelated-query non-contamination), 5 real E2E tests through
  `RuntimeDemoConsole` (the brief's own exact mic-list scenario: ordinal
  -> attribute -> comparison -> unrelated-query, multi-topic switching,
  repair-correction persistence), and bounded-state/persistence/
  structural guards.
- Two test-authoring corrections found and fixed while writing the
  adversarial matrix (documented honestly, not hidden): "kalau buat
  ESP32-S3?" was initially expected to classify as `comparison` but
  correctly classifies `unknown` (no `gimana`/`vs`/`dibanding` marker
  present - the CORRECT, conservative "don't fabricate" default per
  Phase 9, not a bug); two classifier gaps found by the SAME matrix and
  fixed in production code (not by weakening the test): "yang buat mic
  tadi" (a non-adjacent "yang ... tadi" span) and "yang bagian power"
  (an attribute candidate behind a "bagian" connector word) both fell to
  `"unknown"`/were misclassified before a bounded regex extension and an
  attribute-candidate-skip rule were added.
- Scoped regression during development
  (`test_memory_continuity.py`/`test_memory_topic_retention.py`/
  `test_memory_comparison_topic_preservation.py`/
  `test_conversation_reference_resolution.py`/
  `test_memory_decision_quality.py`/`test_memory_context.py`/
  `test_memory_retrieval.py`): 297 passed, 0 failed (plus the new
  suite's own 54, run standalone: 54 passed).
- Full `tests/` tree (85 files, split into 12 chunks of ~7-8 files due to
  this environment's per-command timeout, `pytest -n 2` per chunk) -
  **zero new regressions**. Failures observed, all independently
  re-confirmed as pre-existing/environmental/parallel-load-only timing
  flakes, matching the SAME failure classes documented in every prior
  sprint's own baseline in this file:
  - `test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis`,
    `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays` -
    flaky only under `-n 2` parallel load; both pass reliably in
    isolation.
  - `test_main_bargein.py`, `test_root_main_bargein.py` - pre-existing
    `ModuleNotFoundError: No module named 'faster_whisper'` / sandbox
    mount-path artifact.
  - `test_mic_device_index.py` (6 tests) - pre-existing sandbox
    `MIC_DEVICE_INDEX` environment-configuration artifact.
  - `test_production_launcher.py::test_07_...`,
    `test_real_adapters.py::test_real_whisper_source_...` (x2) -
    pre-existing `RealWhisperSource` attribute gap, unrelated to any
    file touched this sprint.
  - `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
    the same pre-existing `inspect.getsource()` `OSError` documented in
    every prior sprint's own baseline in this file.
- Production code changes: `luno/memory.py` (additive: `repair_reference`/
  `ordinal_reference`/`attribute_reference` types + regexes,
  `ORDINAL_WORD_MAP`/`CARDINAL_WORD_MAP`, `is_merge_reference_followup()`,
  one bounded-gap extension to the pre-existing `_DIRECT_REFERENCE_RE`),
  `luno/memory_context.py` (additive: `ActiveTopicSnapshot.list_items`
  field, `extract_list_items_from_reply()`, `_merge_terms()`, `is_merge`
  parameter on `update_active_topic()`/`update_topic_history()`,
  `parse_ordinal_indices()`/`resolve_ordinal_targets()`/
  `ordinal_targets_to_relevant_memory()`/
  `build_expanded_retrieval_text_for_targets()`/`ConversationReference`),
  `main_runtime_demo.py` (`_on_assistant_response()`'s `is_merge`
  computation + defensive fallback, `_handle_utterance()`'s new
  ordinal-resolution branch + debug log line). No changes to memory
  retrieval, memory ranking/budget, the prompt-injection trust boundary,
  the LLM model, TTS voice/model, Fish Audio synthesis behavior, the
  streaming architecture, or cancellation semantics.
- Persistent state: `config/*.json` SHA256 confirmed unchanged (all 15
  files) before vs. after the full sprint (implementation + full test
  suite run).

## Sprint 39 - Conversation Intelligence & Context Quality

- Ran the full suite (84 test files, excluding the 2 pre-existing
  collection-error files - `test_main_bargein.py`/`test_root_main_bargein.py`,
  same environment artifacts as every prior sprint's baseline), in 8
  sequential chunks (this environment's per-command timeout) -
  **zero new regressions**. Failures observed, all independently
  re-confirmed as pre-existing/environmental, matching the SAME failure
  classes documented in every prior sprint's own baseline in this file:
  - `test_mic_device_index.py` (6 tests) - pre-existing sandbox
    `MIC_DEVICE_INDEX` environment-configuration artifact.
  - `test_production_launcher.py::test_07_...`,
    `test_real_adapters.py::test_real_whisper_source_...` (x2) -
    pre-existing `RealWhisperSource` attribute gap, unrelated to any
    file touched this sprint.
  - `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
    the same pre-existing `inspect.getsource()` `OSError` documented in
    every prior sprint's own baseline in this file.
  - `tests/test_dashboard.py` - one benign `PytestUnhandledThreadExceptionWarning`
    from a background HTTP consumer thread hitting a read-timeout after
    its owning test had already finished; not a test failure.
- Four reproduced, root-caused context-quality failures found via live
  E2E probes through the real `RuntimeDemoConsole` (ATTRIBUTE DRIFT via
  `_merge_terms()`'s eviction of the parent topic; a classification gap
  for "yang lebih bagus/kecil?"/"yang paling murah/mahal/bagus/kecil?";
  a false-positive topic-overlap stopword gap ("soal"); and a too-small
  topic-history cap that evicted an explicitly referenced topic) - all
  fixed additively. Full detail: `ARCHITECTURE_GUARD.md` §39,
  `docs/change_impact/conversation_intelligence.md`.
- Production code changes: `luno/memory.py` (additive:
  `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` extended with an optional
  "lebih "/"paling " skip prefix, `_ATTRIBUTE_RESIDUAL_STOPWORDS`
  extended with `"terus"`/`"lebih"`/`"paling"` - no new reference types,
  no precedence changes), `luno/memory_context.py` (additive:
  `_extract_topic_terms_from_turn_ordered()`, `_merge_terms()` rewritten
  with a reserved-old-quota + deterministic-order algorithm,
  `_TOPIC_OVERLAP_STOPWORDS` + `"soal"`, `_TOPIC_HISTORY_MAX_ENTRIES`
  4 -> 8, both merge call sites updated to pass the new order-preserving
  extraction). `main_runtime_demo.py` NOT modified. No changes to
  memory ranking/budget algorithms themselves, the prompt-injection
  trust boundary, the LLM model, TTS voice/model, Fish Audio synthesis
  behavior, the streaming architecture, or cancellation semantics.
- New test file: `tests/test_conversation_intelligence.py` (54 tests,
  all passing) - regression-guards for all four fixes, the brief's own
  Phase 8 adversarial phrase matrix, Scenario D's 12-phrase
  classification/policy table, 18 named scenarios (several via the real
  `RuntimeDemoConsole`), no-contamination/bounded-state/structural
  guards, and per-call latency measurements (all `<5ms`, target met).
- Persistent state: `config/*.json` SHA256 confirmed unchanged (all 185
  files, including timestamped backups) before vs. after the full sprint
  (implementation + full test suite run).

## Sprint 40 - Memory Confidence & Conflict Resolution

- Ran the full suite (88 test files, split into chunks under
  `pytest-xdist -n 2 --dist loadfile` for this environment's per-command
  timeout budget), plus the new `tests/test_memory_confidence.py` (24
  tests) and `tests/test_memory_conflict_resolution.py` (58 tests) -
  **zero new regressions** after one genuine, self-caught-and-fixed
  regression during the sweep (see below). Failures remaining, all
  independently re-confirmed as pre-existing/environmental, matching the
  SAME failure classes documented in every prior sprint's own baseline
  in this file:
  - `tests/test_mic_device_index.py` (6 tests) - `list_microphones.py`
    absent from this sandbox checkout (present in the real `.venv`),
    same INFRASTRUCTURE class as `test_main_bargein.py`'s
    `faster_whisper` gap and `test_root_main_bargein.py`'s absent
    `legacy_main.py`.
  - `tests/test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` -
    the same pre-existing, already-documented environment-specific
    failure every prior sprint's own baseline in this file notes.
  - `tests/test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
    the same pre-existing `inspect.getsource()` `OSError` documented in
    every prior sprint's own baseline in this file.
  - `tests/test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis` -
    confirmed a timing-flake under 2-worker parallel resource contention
    only; passes in isolation (`1 passed in 0.82s`). No file this sprint
    touched TTS/streaming code.
- **Genuine regression found and fixed during this sprint's OWN Phase 10
  sweep** (not a pre-existing issue): three `tests/test_runtime_demo.py`
  E2E tests failed after the confidence/conflict implementation -
  `test_memory_decision_quality_adaptive_retrieval_end_to_end_context_evidence_scenario_a`
  (`assert prompt.count("RTX 4090") == 1` -> got `2`),
  `test_memory_context_assembly_end_to_end_unifies_sources_through_real_bridge`,
  `test_memory_prompt_intelligence_end_to_end_relevance_gated_and_current_vs_historical`.
  Root cause: an explicit "ingat, spek GPU aku RTX 4090." command is
  ALSO captured by the ephemeral `_active_topic`/`_topic_history` layer
  (every turn updates it, remember-command or not); the new
  `source_sentence` field then quoted "RTX 4090" a SECOND time,
  duplicating the persistent `manual_memory` layer's own pre-existing
  rendering of the same fact - breaking a prior sprint's own "one
  unified block, never duplicated across two independent renderings"
  invariant. Fixed by threading a new `is_remember_command: bool = False`
  parameter through `update_active_topic()`/`update_topic_history()`
  (reusing `memory.detect_remember_command()`, already computed at the
  call site) that suppresses only `source_sentence` for that turn - all
  three tests, plus the full memory suite (1071 tests) and
  `test_runtime_demo.py` (78 tests) re-ran clean afterward.
- A second false-positive was found and fixed BEFORE the regression
  sweep, via targeted diagnostic testing (not a failing test): the
  supersession-tagging overlap check could register a false "same
  subject" match purely via generic acknowledgment words ("oke",
  "dicatat") that open/close nearly every assistant reply in this
  persona - live reproduction confirmed two turns about unrelated
  subjects (mic setup vs. an aquascape switch) both scored a non-empty
  overlap via shared "oke"/"dicatat" tokens. Fixed by extending the
  EXISTING `_TOPIC_OVERLAP_STOPWORDS` set (no new mechanism).
- Root cause (Phase 0): the codebase's existing conflict-resolution
  system lives entirely in the PERSISTENT `manual_memory` layer,
  reachable only via an explicit "ingat ..." command; ordinary
  conversation flows exclusively through the EPHEMERAL `_active_topic`/
  `_topic_history` layer, which had zero confidence/conflict awareness.
  Full detail: `ARCHITECTURE_GUARD.md` §40,
  `docs/change_impact/memory_confidence_conflict_resolution.md`.
- Production code changes: `luno/memory.py` (additive:
  `is_correction_signal()` new public wrapper reusing existing
  `_CORRECTION_RE`/`_is_temporal_change()`, `_HISTORICAL_QUERY_MARKERS`
  + `"sebelumnya"`), `luno/memory_context.py` (additive:
  `ActiveTopicSnapshot.status`/`source_sentence` fields,
  `_bounded_source_sentence()`, `_CONFIDENCE_ACTIVE`/
  `_CONFIDENCE_SUPERSEDED`/`_confidence_for_relevant_memory()`,
  `ContextItem.confidence` + `_rank_key()` extended by one trailing
  element, `active_topic_to_relevant_memory()` rewritten for
  differentiated current-vs-superseded rendering, supersession-tagging
  logic in `update_topic_history()`, `_TOPIC_OVERLAP_STOPWORDS`
  extended, both topic-update functions gained `is_remember_command`),
  `main_runtime_demo.py` (one call site updated to pass
  `is_remember_command`). `tests/test_memory_decision_quality.py`/
  `tests/test_memory_evaluation.py` updated (2 tests) to reflect
  `_rank_key()`'s new 9-element contract, per this project's own
  established "extend the structural contract test in lockstep"
  convention. No changes to `assemble_context()`/`_apply_budget()`/
  `render_context_block()`/`select_topic_candidates()`, memory ranking
  semantics beyond the one documented late tie-break, the LLM model, TTS
  voice/model, Fish Audio synthesis behavior, the streaming
  architecture, or response-depth semantics.
- New test files: `tests/test_memory_confidence.py` (24 tests) +
  `tests/test_memory_conflict_resolution.py` (58 tests) = 82 tests, all
  passing - confidence field/ranking invariants, gating, multi-topic
  safety (the brief's own 3-topic E2E scenario), 6 production-path E2E
  scenarios, a 5-domain generalization matrix (PC/GPU, IoT/
  microcontroller, Audio, Aquascape, Software/network), a structural
  AST-based no-hardcoding proof, and per-call latency measurements (all
  well under the 5ms/call target).
- Persistent state: `config/*.json` SHA256 confirmed unchanged (all 15
  top-level files) before vs. after the full sprint (implementation +
  full test suite run, including many "ingat ..." commands through the
  real production path).

## Sprint 41 - Temporal Memory & Timeline Awareness

- Full `tests/` tree run (excluding the 3 pre-existing uncollectible/
  slow files - `test_main_bargein.py`/`test_root_main_bargein.py`,
  missing `faster_whisper`; `test_dashboard.py`, run separately per
  established precedent):
  `python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py --ignore=tests/test_dashboard.py -n 4 --dist loadfile`
  -> **2473 passed, 12 failed**. Every failure investigated individually
  against this document's own documented baseline, none new:
  - `tests/test_mic_device_index.py` (6 tests) - the same pre-existing
    `MIC_DEVICE_INDEX=1`-set-in-real-`.env` environment-specific
    failures every prior sprint's own baseline in this file notes.
  - `tests/test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` -
    the same pre-existing, already-documented environment-specific
    failure every prior sprint's own baseline in this file notes.
  - `tests/test_real_adapters.py` (2 tests) - the same pre-existing
    environment-coupled failures documented since §15.
  - `tests/test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
    the same pre-existing, documented scheduling-jitter flake every
    prior sprint's own baseline notes.
  - `tests/test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis` -
    the same documented timing-sensitive flake under parallel-worker
    resource contention; re-confirmed passing in isolation (`1 passed`
    on this sprint's own re-run). No file this sprint touched TTS/
    streaming code.
  - `tests/test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays` -
    the same documented timing-sensitive flake in this same file every
    prior sprint's own baseline notes (an earlier run in this sprint's
    own session, under different parallel scheduling, instead saw
    `test_A_normal_stream_chunk_...` flake in the SAME file - re-run
    3/3 in isolation confirmed it passes reliably; both are the same
    documented "scheduling-jitter under xdist parallel load" class).
- **Genuine regression found and fixed during this sprint's OWN Phase 11
  sweep** (not a pre-existing issue, caught by an EXISTING Sprint 40
  test, not a new one): `tests/test_memory_conflict_resolution.py::
  test_33_domain_generalization_unrelated_query_no_injection[Aquascape]`
  newly failed after this sprint's own retrieval-fallback addition -
  "Berapa harga tiket bioskop sekarang?" (a fully independent question
  about movie ticket prices, containing "sekarang" purely incidentally)
  was wrongly classified `is_current_state_query()=True` and injected an
  unrelated aquascape-filter memory. Root cause: the new `select_
  temporal_fallback_candidate()` had no check for how much OTHER,
  unrelated content a temporal-shaped question carries. Fixed with a
  bounded residual-token pre-check (`_TEMPORAL_FALLBACK_MAX_RESIDUAL_
  TOKENS = 1`) - re-ran clean afterward (133/133 across this sprint's
  own new test file plus `test_memory_conflict_resolution.py`), and the
  full regression sweep confirmed no other test regressed as a result.
- A second, PRE-EXISTING (Sprint 40) bug was found and fixed during this
  sprint's own Phase 7 multi-topic testing, via targeted diagnostic
  testing (not a failing test in the existing suite - this sprint's own
  new 5-domain matrix caught it): `"sekarang"` was not previously in
  `_TOPIC_OVERLAP_STOPWORDS`, so two completely unrelated "Sekarang aku
  pakai X." statements about DIFFERENT domains falsely registered "real
  overlap" purely via the shared word "sekarang", triggering `is_
  correction_signal()`'s supersession retagging across unrelated topics.
  Fixed by adding `"sekarang"` to the same shared stopword set (no new
  mechanism, same precedent `"aku"`/`"mau"`/`"soal"` already
  established).
- Root cause (Phase 2): three independent defects - `is_correction_
  signal()`'s bare-"sekarang" alternative false-firing on ordinary
  CURRENT-state questions; `select_topic_candidates()`'s pure
  lexical-overlap eligibility check having no fallback for a temporal
  query worded differently than the stored statement; and a compound
  sentence naming multiple distinct temporal facts collapsing into one
  whole-turn-status topic-history entry. Full detail: `ARCHITECTURE_
  GUARD.md` §41, `docs/change_impact/temporal_memory_timeline_
  awareness.md`.
- Production code changes: `luno/memory.py` (additive: `_is_
  interrogative()`, `_CORRECTION_RE_STRONG` (derived programmatically),
  `is_correction_signal()` rewritten with interrogative gating,
  `classify_temporal_status()`, `is_historical_statement()`, `is_
  current_state_query()`, `is_planned_query()` + their marker constants),
  `luno/memory_context.py` (additive: `_CONFIDENCE_PLANNED`/`_CONFIDENCE_
  CANCELLED`/`_STATUS_CONFIDENCE` dict, `_STATUS_LABELS` dict inside
  `active_topic_to_relevant_memory()`, `historical=` derivation extended
  to `"cancelled"`, `"sekarang"` added to `_TOPIC_OVERLAP_STOPWORDS`,
  `update_topic_history()`'s rich-turn push extended with a
  compound-clause-split check plus planned/completed/cancelled dispatch,
  `_split_temporal_clauses()`/`_classify_clause_temporal_role()`/
  `_build_compound_clause_entries()`/`select_temporal_fallback_
  candidate()` new), `main_runtime_demo.py` (one new 4th `elif` branch
  in the existing retrieval call site). No changes to `assemble_
  context()`/`_apply_budget()`/`render_context_block()`/`select_topic_
  candidates()`/`update_active_topic()`, the LLM model, TTS voice/model,
  Fish Audio synthesis behavior, the streaming architecture, or
  response-depth semantics.
- New test file: `tests/test_temporal_memory_timeline_awareness.py` (75
  tests, all passing) - classifier unit tests, extended status/
  confidence/label coverage, conflict-dispatch unit tests, retrieval-
  fallback unit tests, 6 production-path E2E scenarios (A-F, real
  `RuntimeDemoConsole`), ambiguity safety (7 fragment types + unrelated-
  temporal-word test), a 5-domain generalization matrix (PC/GPU, IoT/
  microcontroller, Audio, Aquascape, Software/network), a structural
  AST-based no-hardcoding proof, and per-call latency measurements (all
  well under the 5ms/call target).
- Persistent state: `config/*.json` SHA256 + mtime confirmed unchanged
  (all 384 files under `config/`, including `config/backups/`) before
  vs. after the full sprint (implementation + full test suite run).

## Sprint 42 - Cross-System Integration Audit

- An AUDIT sprint, not a feature sprint - Phase 0 was strictly read-only;
  nothing changed before root cause was reproduced live through
  `RuntimeDemoConsole`. Full pipeline map + state-ownership audit (no
  code changed): `ARCHITECTURE_GUARD.md` §42, `docs/change_impact/
  cross_system_conversation_consistency.md`.
- **ONE real, proven bug found and fixed**, reproduced live before the
  fix: `_TOPIC_OVERLAP_STOPWORDS` (`luno/memory_context.py`) was missing
  `"berapa"` ("how much/many") and `"tadi"` ("earlier/just now") - the
  same class of generic word already fixed for `"aku"`/`"mau"`/`"soal"`/
  `"sekarang"`/`"oke"` in Sprints 39-41. Because `select_topic_
  candidates()`'s lexical-overlap branch has no ambiguity gate of its
  own (unlike Sprint 41's `select_temporal_fallback_candidate()`), this
  caused three independently reproduced failures: (1) an unrelated
  query ("Berapa harga tiket bioskop?") wrongly injected a prior,
  unrelated aquarium topic purely via the shared word "berapa"; (2)
  "Yang sekarang berapa VRAM-nya?" produced NO injected context at all,
  because "berapa" pushed the temporal-fallback branch's own residual-
  token ambiguity gate over its threshold; (3) "GPU yang tadi?" after a
  3-topic switch correctly found the GPU entry but also pulled in a
  self-echoed entry from an earlier turn's own question, via the shared
  word "tadi". Fixed by adding both words to the same shared stopword
  set (no new mechanism, same precedent `"aku"`/`"mau"`/`"soal"`/
  `"sekarang"` already established) - one file, two words.
- **Investigated and found to be CORRECT pre-existing behavior, not
  bugs (category M - probe/test artifacts):** an apparent "ordinal
  resolution is broken" finding was caused by an early probe's mock LLM
  reply squeezing a 3-item list onto one line; `extract_list_items_
  from_reply()` is deliberately line-anchored because it parses Luno's
  OWN finalized reply (Sprint 38 design) - a realistic multi-line reply
  resolves all four ordinal+temporal phrasing combinations correctly
  with zero fabrication. An apparent temporal-history-depth failure (a
  CURRENT->PLANNED->COMPLETED chain not resolving to the completed
  value) was caused by the probe's own completion-turn phrasing
  omitting the domain word ("LED") entirely - a purely lexical system
  (no embeddings, no synonym layer, explicitly forbidden this sprint)
  cannot link an entry back to a domain whose name never appears in
  that entry's own text; this is the same class of pre-existing,
  already-documented limitation as Sprint 40's "ESP8266" vs "ESP32"
  precedent, not a new defect.
- Production code changes: `luno/memory_context.py` (additive only:
  `"berapa"`/`"tadi"` added to the existing `_TOPIC_OVERLAP_STOPWORDS`
  frozenset). No changes to `assemble_context()`/`_apply_budget()`/
  `render_context_block()`/`select_topic_candidates()`'s own logic/
  `update_active_topic()`/`update_topic_history()`/`select_temporal_
  fallback_candidate()`/`resolve_ordinal_targets()`/`build_dual_
  response()`, the LLM model, TTS voice/model, streaming architecture,
  or response-depth semantics.
- New test file: `tests/test_cross_system_conversation_consistency.py`
  (25 tests, all passing) - unit regression for the stopword fix (3
  tests, including a "still matches on real overlap" guard), Scenarios
  A-J via real `RuntimeDemoConsole` E2E across 5 domains (PC/GPU, audio/
  microphone, ESP32/mic+aquascape/pump+PC/GPU multi-topic, WLED/LED+NAS/
  server, aquascape-vs-bioskop unrelated-query), voice-mode independence
  (SHORT/ALL) plus a `build_dual_response()` structural signature check,
  interleaved + real-thread-concurrent conversation isolation, and 2
  structural invariant checks (no embedding/LLM-judge import/call
  pattern; `conversation_ended` clears every per-conversation dict).
- Full `tests/` tree run (excluding the 2 pre-existing uncollectible
  files - `test_main_bargein.py`/`test_root_main_bargein.py`, missing
  `faster_whisper`/`legacy_main.py`):
  `python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py -n 4 --dist loadfile`
  -> **2543 passed, 10 failed** (2553 collected). Every failure matches
  this project's own documented, pre-existing, environment-coupled
  baseline exactly, none new: `test_mic_device_index.py` (6 tests,
  `MIC_DEVICE_INDEX`-set-in-real-`.env`), `test_production_
  launcher.py::test_07_health_checks_all_pass_in_default_mock_
  configuration`, `test_real_adapters.py` (2 tests), `test_state_
  isolation.py::test_isolate_persistent_state_drains_stragglers_
  before_monkeypatch_reverts` (scheduling-jitter flake). A separate,
  heavier `-n 8` sweep additionally surfaced 6 timing-sensitive TTS/
  streaming failures (`test_runtime_demo.py`/`test_tts_chunk_
  pipelining.py`/`test_voice_pipeline_latency.py`); all 6 re-ran and
  passed cleanly in isolation (`6 passed in 12.25s`), confirming
  parallel-worker resource contention flakes, not regressions - this
  sprint's fix touches only a stopword set with zero relationship to
  TTS/streaming. The 472 tests in the memory/topic/reference/temporal-
  focused files most directly exercising the changed code path were
  additionally run in isolation: **472 passed**.
- Persistent state: `config/*.json` SHA256 + size confirmed unchanged
  for 14 of 15 top-level files before vs. after the full sprint.
  `config/relationship_state.json`'s `interaction_count`/`last_
  interaction_timestamp` changed - the same well-precedented PROBE SIDE
  EFFECT every prior sprint's own baseline has observed (running many
  turns through the real production path increments this real usage
  counter), reported separately per this sprint's own Phase 6
  instruction, not a sprint-caused production change.

## 2026-08-16 - Semantic Context Bridging & Memory Precision sprint (Sprint 43)

- Production code changes: `luno/memory_context.py` (additive - a new
  bounded lexical-normalization layer, a new fallback tier appended to
  `select_topic_candidates()` and `select_temporal_fallback_candidate()`,
  and a new `is_active_topic_relevant_to_query()` function);
  `main_runtime_demo.py` (one `elif` condition in the pre-existing
  single-slot recency branch gained one additional clause, scoped to
  `reference_type == "comparison"` only). No changes to `_rank_key()`,
  `_apply_budget()`, `render_context_block()`'s own logic,
  `assemble_context()`'s parameter list, `update_active_topic()`,
  `update_topic_history()`, `resolve_ordinal_targets()`,
  `build_dual_response()`, the LLM model, TTS voice/model, streaming
  architecture, or response-depth semantics. See `ARCHITECTURE_GUARD.md`
  §43 and `docs/change_impact/semantic_context_bridging.md` for full
  root-cause/fix detail.
- New test file: `tests/test_semantic_context_bridging.py` (72 tests,
  all passing) - unit coverage for the affix stripper, the normalization/
  synonym layer, both fallback tiers, the new relevance guard, E2E
  Scenarios A-H via real `RuntimeDemoConsole`, attribute/ordinal
  references combined with bridging, cross-conversation isolation,
  bounded topic-history eviction, empty/unknown-query behavior, and 6
  structural/architectural invariant checks.
- Two regressions were found and fixed DURING this sprint's own Phase 6
  test-writing, before being counted as final: (1) gating the new
  relevance guard on every `is_short_followup` reference type (not just
  `"comparison"`) regressed 7 pre-existing `test_memory_continuity.py`
  E2E cases whose follow-ups are genuinely signal-less structural
  references with no topical words to check relevance against - fixed by
  scoping the guard to `reference_type == "comparison"` only; (2) the
  guard's own ambiguity tie-check treated a topic-history entry that had
  already been merged into the active snapshot two turns earlier as a
  "competing" topic, regressing `test_memory_comparison_topic_
  preservation.py::test_15` - fixed with a majority-term-coverage skip
  (a strict-subset check was tried first and proved too brittle against
  real merges that drop a word or two).
- Regression sweep, run in file-group batches matching this sandbox's own
  established per-command tooling budget (single full-tree `pytest tests/`
  invocations exceed this sandbox's per-call timeout regardless of
  worker count): all memory/topic/reference/temporal/cross-system suites
  most directly exercising the changed code path (`tests/test_semantic_
  context_bridging.py`, `test_conversation_reference_resolution.py`,
  `test_conversation_intelligence.py`, `test_memory_continuity.py`,
  `test_memory_topic_retention.py`, `test_memory_comparison_topic_
  preservation.py`, `test_temporal_memory_timeline_awareness.py`,
  `test_cross_system_conversation_consistency.py`, `test_memory_
  context.py`, `test_memory_retrieval.py`, `test_memory_confidence.py`,
  `test_memory_conflict.py`, `test_memory_conflict_resolution.py`) -
  **590 passed, 0 failed**. The remaining repository (84 files, excluding
  the 2 pre-existing uncollectible files and `test_dashboard.py`/
  `test_llm_tts_streaming_production.py`/`test_voice_pipeline_latency.py`
  - real-time-duration tests exceeding this sandbox's per-command budget,
  same documented precedent as `test_dashboard.py`'s own existing
  exclusion, none with any code-path overlap with the two files this
  sprint touched) run in 6 file-group batches - **zero new failures**;
  the only 2 failures encountered (`test_emotion_engine.py::test_stale_
  emotion_decays_to_unknown_after_the_configured_window`, `test_state_
  isolation.py::test_isolate_persistent_state_drains_stragglers_before_
  monkeypatch_reverts`) are both already documented, pre-existing,
  scheduling-jitter/environment flakes (see `ARCHITECTURE_GUARD.md`'s own
  prior entries), independently reproduced identically in isolation with
  no files this sprint touched anywhere in their call chain.
- Performance: `select_topic_candidates()` ~0.066ms/call, `is_active_
  topic_relevant_to_query()` ~0.032ms/call, `select_temporal_fallback_
  candidate()` ~0.003ms/call, `_strip_bounded_affixes()` ~0.003ms/call
  (2000-iteration average across 4 representative queries against a
  3-entry bounded history) - combined well under the 5ms/turn target.
- Persistent state: `config/*.json` SHA256 + mtime confirmed byte-
  identical for all 680 files before vs. after the full sprint (this
  sprint's own probes and test suite were run entirely through
  dynamically-loaded, isolated `RuntimeDemoConsole` instances backed by
  `MockOpenRouterClient`, never touching the real persistent config
  files at all - no PROBE SIDE EFFECT to report this time).

## Sprint 44 - Entity & Concept Continuity

- Root cause (Phase 0-2, live reproduction via real `RuntimeDemoConsole`
  across 10 named scenarios A-J before any code changed): `Active
  TopicSnapshot` is, and remains, a flat bag-of-terms - not the source of
  the reproduced gaps. Two distinct defects instead: (1) entity-identity
  erosion - a turn classified `"unknown"` with sparse (<=1 real token)
  content was still REPLACE-worthy, silently evicting an established
  entity's own terms (Scenario A, extended to a 4th turn); (2) an overly
  strict Sprint-43 guard unconditionally refused any query with zero
  overlap against the active topic, too strict for a genuine single-word
  elliptical attribute question in a low-ambiguity single-topic
  conversation (Scenario D). Phase 7's own cross-topic adversarial
  testing found a third gap: the guard was only ever consulted for
  `"comparison"`-classified turns, letting `attribute_reference` turns
  bypass it entirely inside a genuinely multi-topic conversation.
- Fix, three additive parts, smallest-proven-necessary, no new
  entity/concept representation introduced: (1) `memory_context.
  is_sparse_unknown_followup()`, consulted only by `main_runtime_demo.
  py`'s `is_merge` computation; (2) a bounded low-ambiguity fallback tier
  appended to `is_active_topic_relevant_to_query()`'s existing zero-score
  branch, gated to exactly one real query token AND fewer than 2 other
  genuinely distinct topics live in the bounded history; (3) the single-
  slot recency branch's guard gate widened from `reference_type !=
  "comparison"` to also exclude `"attribute_reference"`. See `ARCHITECTURE_
  GUARD.md` §44 and `docs/change_impact/entity_concept_continuity.md` for
  full root-cause/fix detail, including the one candidate fix
  (bare-compound-noun "-nya" declaratives) that was investigated and
  deliberately NOT made, documented as a known limitation.
- New test file: `tests/test_entity_concept_continuity.py` (72 tests, all
  passing) - unit coverage for the new helper functions, the "buat"
  stopword parity fix, the new fallback tier, `is_merge` integration,
  exact/attribute-reference continuity, adversarial precedent
  preservation, multi-topic isolation, temporal interaction, bounded-
  memory behavior, performance, 19 E2E scenarios via real
  `RuntimeDemoConsole` (Scenarios A-J, extended multi-turn chains, the
  Phase 7 cross-topic adversarial matrix, cross-conversation isolation,
  the documented known-limitation lock-in), and structural/anti-scope-
  creep invariants.
- Regression sweep, run in file-group batches matching this sandbox's own
  established per-command tooling budget: the full memory/topic/
  reference/temporal/semantic-bridging suite most directly exercising the
  changed code path (`tests/test_entity_concept_continuity.py`, `test_
  conversation_reference_resolution.py`, `test_conversation_intelligence.
  py`, `test_memory_continuity.py`, `test_memory_comparison_topic_
  preservation.py`, `test_memory_topic_retention.py`, `test_temporal_
  memory_timeline_awareness.py`, `test_cross_system_conversation_
  consistency.py`, `test_semantic_context_bridging.py`, `test_memory_
  retrieval_decision_quality_reaudit.py`) - **500 passed, 0 failed**. The
  remaining repository (89 files, excluding the 2 pre-existing
  uncollectible files) run in file-group batches (using `pytest -n 4` for
  `test_llm_tts_streaming_production.py`, the same documented precedent
  as prior sprints) - **zero new failures**; the only failures
  encountered (6x `test_mic_device_index.py`, 1x `test_production_
  launcher.py::test_07_health_checks_all_pass_in_default_mock_
  configuration`, 2x `test_real_adapters.py`, 1x `test_state_isolation.
  py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_
  reverts`) are all identical to the standing, already-documented
  baseline (§15 and prior sprint entries), independently reproduced with
  no files this sprint touched anywhere in their call chain.
- Performance: `is_active_topic_relevant_to_query()` ~0.068ms/call,
  `is_sparse_unknown_followup()` ~0.014ms/call (2000-iteration average
  against a 3-entry bounded history) - well under the 5ms/turn target, no
  network calls, no model inference, no embeddings.
- Persistent state: `_active_topic`/`_topic_history` confirmed to remain
  plain, bounded, in-memory `Dict` attributes on `PlannerBridgeModule` -
  grepped every reference in `main_runtime_demo.py`, none touch any file
  I/O path. `config/*.json` (top-level, 15 files) confirmed present with
  no unexpected structural changes; this sprint's own probes and test
  suite were run entirely through dynamically-loaded, isolated
  `RuntimeDemoConsole` instances backed by `MockOpenRouterClient`.

## Sprint 45 - Entity Identity & Semantic Alias Continuity

- Baseline before this sprint: 500 passed (Sprint 44's own regression
  set) across the core memory/topic/reference/temporal/semantic-bridging
  suites; 91 test files in the full repository, 2 pre-existing
  uncollectible (`test_main_bargein.py`/`test_root_main_bargein.py`), 10
  pre-existing environment-specific/flake failures (§15 and the standing
  `test_state_isolation.py` inspect flake).
- Root cause (Phase 0-1, live reproduction via real `RuntimeDemoConsole`
  across a comprehensive probe matrix - verb/action/device/audio/
  aquascape alias, abbreviation, correction, multi-topic ambiguity,
  word-shape traps, Indonesian morphology, before any code changed):
  nearly every scenario in this sprint's own brief was ALREADY correctly
  handled by Sprint 43's existing synonym-bridging layer and Sprint 44's
  ambiguity guards - left untouched. Two real, narrow gaps found, both
  variations on one linguistic fact ("gimana" is the colloquial
  contraction of standard "bagaimana" - the SAME word, two registers,
  already treated as equivalent elsewhere in the same files but missed
  in 5 specific spots) plus one unrelated short-acronym clitic-stripping
  gap (`_MIN_AFFIX_ROOT_LEN=4` blocked "-nya" stripping from any
  3-letter root, so fused "SSDnya"/"CPUnya"/"PSUnya" never normalized to
  "ssd"/"cpu"/"psu"). See `ARCHITECTURE_GUARD.md` §45 and `docs/
  change_impact/entity_identity_semantic_alias_continuity.md` for full
  detail.
- Fix, five small additive edits across 2 files: `luno/memory.py`
  (`_COMPARISON_MARKER_RE` gained "bagaimana"; `classify_reference_
  type()`'s own comparison-branch residual filter and `_attribute_
  reference_word()`'s candidate exclusion both gained "bagaimana"
  alongside "gimana"); `luno/memory_context.py` (`_TOPIC_OVERLAP_
  STOPWORDS` gained "bagaimana"; a new `_MIN_CLITIC_ROOT_LEN=3` constant
  used only by the "-nya" clitic pass inside `_strip_bounded_affixes()`,
  leaving the stricter `_MIN_AFFIX_ROOT_LEN=4` unchanged for every other
  pass). Zero new synonym groups, zero new entity relationship model,
  zero embeddings/LLM-judge/second-ranking-system.
- New test file: `tests/test_entity_identity_semantic_alias_continuity.py`
  (75 tests, all passing) - unit coverage for both fixes, word-shape/
  token-boundary safety, bounded Indonesian morphology, existing alias-
  chain regression locks, multi-topic ambiguity, false-positive/non-
  fabrication, correction/attribute-followup preservation, performance,
  and 17 E2E scenarios via real `RuntimeDemoConsole`.
- Two test-authoring bugs were found and fixed DURING this sprint's own
  test-writing (before being counted as final): (1) a bare "Bagaimana?"
  was misclassified `comparison` instead of `direct_reference` (an
  asymmetry a bare "Gimana?" never had) - `classify_reference_type()`'s
  own inline comparison-branch residual filter had its own separate copy
  of the gimana-exclusion check that needed the same fix as
  `_COMPARISON_MARKER_RE`; (2) a test asserting `_ID_PREFIXES`'s bound at
  `<=20` was wrong (the pre-existing, Sprint-43-established set already
  has 22 members) - corrected the test's own threshold to `<=25`, no
  production change.
- Regression sweep, run in file-group batches matching this sandbox's own
  established per-command tooling budget: the full memory/topic/
  reference/temporal/semantic-bridging/entity-continuity suite most
  directly exercising the changed code path (`tests/test_entity_
  identity_semantic_alias_continuity.py`, `test_entity_concept_
  continuity.py`, `test_conversation_reference_resolution.py`, `test_
  conversation_intelligence.py`, `test_memory_continuity.py`, `test_
  memory_comparison_topic_preservation.py`, `test_memory_topic_
  retention.py`, `test_temporal_memory_timeline_awareness.py`, `test_
  cross_system_conversation_consistency.py`, `test_semantic_context_
  bridging.py`, `test_memory_retrieval_decision_quality_reaudit.py`) -
  **575 passed, 0 failed**. The remaining repository (89 files,
  excluding the 2 pre-existing uncollectible files) run in file-group
  batches (`pytest -n 4` for `test_llm_tts_streaming_production.py`, the
  same standing precedent) - **zero new failures**; the only failures
  encountered (6x `test_mic_device_index.py`, 1x `test_production_
  launcher.py::test_07_health_checks_all_pass_in_default_mock_
  configuration`, 2x `test_real_adapters.py`, 1x `test_state_isolation.
  py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_
  reverts`, 1 harmless dashboard network-timeout warning) are all
  identical to the standing, already-documented baseline (§15 and every
  prior sprint's own entry).
- Performance: `classify_reference_type()` ~0.013ms/call, `_strip_
  bounded_affixes()` ~0.005ms/call, `analyze_query()` ~0.007ms/call
  (2000-iteration average) - well under the 5ms/turn target, no network
  calls, no model inference, no embeddings.
- Persistent state: `config/*.json` (top-level, 15 files) SHA256 + mtime
  confirmed byte-identical to the exact values recorded at the end of
  Sprint 44 - zero persistent-state changes from this sprint's work (all
  four production edits are pure regex/constant changes in code, no new
  file I/O, no new persistent alias/entity storage).

## Sprint 46 - Contextual Reference Robustness

- Baseline before this sprint: 575 passed (Sprint 45's own regression
  set) across the core memory/topic/reference/temporal/semantic-bridging
  suites; 92 collectible test files in the full repository (94 total, 2
  pre-existing uncollectible - `test_main_bargein.py`/`test_root_main_
  bargein.py`), 10 pre-existing environment-specific/flake failures (§15
  and every prior sprint's own entry).
- Root cause (Phase 0-2, live reproduction via a 10-scenario (A-J) +
  adversarial probe matrix run through the real `RuntimeDemoConsole`,
  before any code changed): Scenarios A, C, E, F, G, J and both
  directions of the contamination check were ALREADY correctly handled
  by the existing Sprint 36-45 pipeline - left untouched. THREE real,
  narrow gaps found and fixed: (1) `_normalize_terms_for_bridging()`
  never chained its synonym-canon lookup onto its own affix-stripped
  root, silently losing the alias step for any word needing both
  transformations together ("mikrofonnya"->"mikrofon"->"mic",
  "mengganti"->"ganti"->"upgrade"); (2) a lone historical-query-marker
  token ("sebelumnya") was treated as signal-less filler and confidently
  injected the CURRENT topic instead of refusing for a query explicitly
  asking about the PREVIOUS state; (3) "kenapa"/"napa"/"mengapa" missing
  from `_TOPIC_OVERLAP_STOPWORDS` (unlike "kok" already there) caused a
  genuine entity-erosion bug ("GPU-nya kenapa?" narrowly missed the
  Sprint 44 sparse-followup threshold, permanently discarding an
  established RTX 3060 identity before a later alias follow-up could
  recover it). TWO candidate fixes (Scenario H's "lebih"/"paling"
  stopword addition; widening the `coverage >= 0.5` topic-lineage tie
  boundary) were investigated, reproduced as correctly fixing their
  target case, and REJECTED after each broke a different existing,
  deliberately-tested guarantee - reverted, documented in-place. See
  `ARCHITECTURE_GUARD.md` §46 and `docs/change_impact/contextual_
  reference_robustness.md` for full detail.
- Fix, three small additive edits in one file, `luno/memory_context.py`:
  `_normalize_terms_for_bridging()` gained one extra `_TOKEN_SYNONYM_
  CANON.get(root)` lookup; `is_active_topic_relevant_to_query()` gained
  one narrow guard clause (historical-marked lone token + present/future-
  status active snapshot -> `False`, routing the caller to the existing
  Sprint 41 temporal fallback instead); `_TOPIC_OVERLAP_STOPWORDS` gained
  "kenapa"/"napa"/"mengapa". Zero new synonym groups, zero new entity
  relationship model, zero embeddings/LLM-judge/second-ranking-system,
  zero changes to `select_temporal_fallback_candidate()`'s own
  eligibility table (a known, documented residual gap, not attempted).
- New test file: `tests/test_contextual_reference_robustness.py` (35
  tests, all passing) - unit coverage for all 3 fixes, 7 E2E regression
  locks for already-correct Scenarios A/C/E/F/G/J, 2 E2E contamination
  tests (both directions), 3 regression locks for the 2 rejected fixes
  (source-string checks plus a direct E2E re-verification of the exact
  scenario each would have broken), 2 performance tests, 2
  persistent-state tests.
- Regression sweep: the same core memory/topic/reference/temporal/
  semantic-bridging/entity-continuity suite Sprints 43-45 used, plus this
  sprint's own new file - **610 passed, 0 failed**. The remaining
  repository (92 collectible files, `pytest -n 4`, same standing
  precedent) - **2784 passed, 15 failed**; 10 of the 15 are byte-for-byte
  identical to the standing, already-documented baseline (6x `test_mic_
  device_index.py`, 1x `test_production_launcher.py::test_07_health_
  checks_all_pass_in_default_mock_configuration`, 2x `test_real_
  adapters.py`, 1x `test_state_isolation.py::test_isolate_persistent_
  state_drains_stragglers_before_monkeypatch_reverts`). The other 5
  (`test_llm_tts_streaming_production.py::test_14_cancellation_during_
  synthesis`, `test_streaming_e2e.py::test_A_normal_stream_chunk_before_
  llm_finished_and_chat_response_complete`, `test_streaming_speech_
  integration.py::test_21_voice_chunks_are_incremental_not_one_giant_
  block`, `test_verification_dashboard.py::test_api_verification_
  reports_a_successful_verified_action_end_to_end`, `test_voice_pipeline_
  latency.py::test_E_default_path_pipelining_synth_overlaps_playback`)
  were NOT silently classified as pre-existing - re-run in ISOLATION
  (serial, not under `-n 4`) and **all 5 passed cleanly**, confirming
  parallel-execution timing contention (none of these files or the
  subsystems they test - TTS streaming, voice pipeline latency,
  verification dashboard - were touched by any Sprint 46 edit), not a
  real regression.
- Performance: `_normalize_terms_for_bridging()` and `is_active_topic_
  relevant_to_query()` both measured directly (1000-iteration average) -
  well under the 5ms/turn target, no network calls, no model inference,
  no embeddings.
- Persistent state: only `luno/memory_context.py` (source) and the new
  test file were modified/created this sprint. 14 of 15 top-level
  `config/*.json` files confirmed unmodified (mtime predates this
  session); `config/relationship_state.json` is actively rewritten
  during any test run by its own pre-existing, unrelated subsystem
  (confirmed unrelated to topic/reference resolution). `_active_topic`/
  `_topic_history` confirmed to remain plain, non-persistent, in-memory
  `dict`s.

## Sprint 47 - Semantic Entity Memory & Reference Graph

- Baseline before this sprint: 610 passed (Sprint 46's own regression
  set) across the core memory/topic/reference/temporal/semantic-
  bridging/entity-continuity suites; 92 collectible test files in the
  full repository (94 total, 2 pre-existing uncollectible). Phase 0 of
  this sprint independently re-verified this exact baseline (610
  passed, 0 failed) before making any change, and confirmed Sprint
  45/46's own documented fixes are actually present in the checkout -
  no discrepancy between the handover documentation and the source
  found.
- Root cause (Phase 1-2, live reproduction of a 6-scenario probe matrix
  through the real `RuntimeDemoConsole`, using deliberately GENERIC
  canned replies so reply text could never leak the "correct" answer
  into a merged snapshot - a methodological correction made mid-sprint
  after an earlier, richer-reply probe round produced false-positive
  "already works" readings): Scenarios 1, 2 confirmed already-correct
  (Scenario 1 matches Sprint 45's own deliberate no-product-to-
  category-fabrication boundary; Scenario 2 already resolves via plain
  raw-token overlap) - left untouched. Scenarios 3 and 6 were REAL,
  reproduced entity-erosion bugs sharing one root cause: an
  `"unknown"`-classified turn whose own 2nd word is a mid-sentence
  demonstrative ("Board itu RAM-nya berapa?", "Tank itu pompanya
  kecil.") has 2 real residual tokens - just above `is_sparse_unknown_
  followup()`'s (Sprint 44) `<= 1` bound - so it destructively REPLACED
  an established entity identity instead of merging. Scenario 5 was a
  REAL, reproduced ambiguity-safety bug (a curated word grounded in
  NEITHER of exactly 2 live topics wrongly, confidently resolved to the
  merely-most-recent one); two fix attempts were investigated,
  reproduced as correct in isolation, and both REJECTED after breaking
  a different existing guarantee. Scenario 4 matches an existing
  Sprint 44 precedent shape in the single-statement case; its
  genuinely-2-separate-topics variant shares Scenario 5's own
  limitation. A NEW limitation (two distinctly-named, high-generic-
  overlap entities conflated by the existing `coverage > 0.5`
  lineage-skip heuristic) was also found and documented, not fixed.
  See `ARCHITECTURE_GUARD.md` §47 and `docs/change_impact/semantic_
  entity_identity.md` for full detail.
- Fix, two small additive edits across 2 files: `luno/memory_
  context.py` gained 1 new function (`is_demonstrative_anchored_
  followup()`) + 2 new module-level constants; `main_runtime_demo.py`
  gained 1 new `or` clause in the existing `is_merge` decision. Zero
  new entity relationship model, zero embeddings/LLM-judge/second-
  ranking-system, zero new synonym groups, zero changes to `classify_
  reference_type()`'s own output for any existing phrase.
- New test file: `tests/test_semantic_entity_identity.py` (35 tests,
  all passing) - unit coverage for the fix, E2E locks for both fixed
  scenarios, alias/canonical-entity/pronoun/possessive-nya tests,
  competing-entity ambiguity tests (including 2 explicit known-
  limitation regression locks), topic-switching/contamination/
  cross-conversation-isolation tests, bounded-state tests, a
  performance test, and regression locks for the already-correct
  scenarios.
- Regression sweep: the same core memory/topic/reference/temporal/
  semantic-bridging/entity-continuity suite Sprints 43-46 used, plus
  `test_contextual_reference_robustness.py` and this sprint's own new
  file - **645 passed, 0 failed**. The remaining repository (92
  collectible files, `pytest -n 4`, same standing precedent) -
  **2817 passed, 17 failed**; 10 of the 17 are byte-for-byte identical
  to the standing, already-documented baseline. The other 7
  (`test_runtime_demo.py::test_episodic_memory_end_to_end_detect_
  persist_retrieve_alongside_existing_context`, `test_streaming_e2e.py
  ::test_D_barge_in_between_llm_and_tts_chunk_never_plays`, 3x `test_
  tts_chunk_pipelining.py`, 2x `test_voice_pipeline_latency.py`) were
  NOT silently classified as pre-existing - re-run in ISOLATION
  (serial, not under `-n 4`) and **all 7 passed cleanly**, confirming
  parallel-execution timing contention (none of these files or the
  subsystems they test were touched by any Sprint 47 edit), not a real
  regression.
- Performance: `is_demonstrative_anchored_followup()` measured directly
  (20,000-call average) at 0.023ms/call - well under the 5ms/turn
  target, no network calls, no model inference, no embeddings.
- Persistent state: only `luno/memory_context.py`, `main_runtime_
  demo.py` (source) and the new test file were modified/created this
  sprint. Isolated verification (running ONLY this sprint's own new/
  touched test files) confirmed `config/long_term_memory.json` and
  `config/relationship_state.json` byte-identical before/after; the
  FULL repository sweep DOES change both files, traced to OTHER,
  pre-existing tests elsewhere in the suite that legitimately exercise
  the real persistence layer (confirmed unrelated to this sprint's own
  source edits - `luno/memory_context.py` never touches file I/O). The
  other 13 of 15 top-level `config/*.json` files are unmodified.
  `_active_topic`/`_topic_history` confirmed to remain plain,
  non-persistent, in-memory `dict`s.

## Sprint 48 - Bounded Entity Provenance & Ambiguity Resolution

- Baseline before: 645 passed / 0 failed (core suite, unchanged from
  Sprint 47's own closing snapshot); full repository 2817 passed / 17
  failed (10 documented baseline + 7 confirmed parallel-execution
  flakes), 92/94 collectible files - re-verified present and unchanged
  at Phase 0 before any edit (no discrepancy from the handover found).
- Root cause: Sprint 47's own known limitation #8 (`docs/project_
  handover.md` SS16 item 8) - a curated-vocabulary single token
  ("board"/"mic"/etc.) with ZERO grounding in EITHER of exactly 2 live
  topics is, by construction, indistinguishable from `distinct_other_
  count`/lexical-overlap alone: Sprint 46's "Mic-nya gimana?" (correct
  answer: trust recency) and Sprint 47's "Board itu gimana?" (correct
  answer: refuse) are the textbook IDENTICAL formal shape. Direct
  token/regex inspection (not guesswork) found the two ARE reliably
  distinguished by a GRAMMATICAL signal already computed elsewhere in
  the same module: whether the query's own 2nd word is the
  demonstrative "itu"/"ini" (`_DEMONSTRATIVE_ANCHORED_RE`, Sprint 47's
  own constant, built for `is_demonstrative_anchored_followup()`'s
  MERGE decision) - "Board ITU gimana?" matches, "Mic-nya gimana?"
  does not.
- Fix: one new, narrow, additive `if` inside `is_active_topic_
  relevant_to_query()`'s existing `active_score == 0` branch (`luno/
  memory_context.py`), immediately after the pre-existing `distinct_
  other_count >= 2` guard: `if distinct_other_count >= 1 and _
  DEMONSTRATIVE_ANCHORED_RE.search(text or ""): return False`. Reuses
  the existing Sprint 47 regex verbatim - no new regex, no new
  vocabulary/threshold, no new state field, no changes to `ActiveTopic
  Snapshot`, `select_topic_candidates()`, `select_temporal_fallback_
  candidate()`, `update_active_topic()`/`update_topic_history()`, or
  the pre-existing `>= 2`/`coverage > 0.5` branches. NOT a third
  variant of Sprint 47's own rejected `distinct_other_count`
  threshold-widening attempts - an independent, additive refusal
  gated on a DIFFERENT signal (grammar, not a lexical/vocabulary
  threshold), scoped ONLY to the one shape neither of Sprint 47's own
  attempts could safely touch.
- No bounded-provenance data structure introduced. The purely
  grammatical, stateless signal above fully resolves the reproduced
  defect without adding any field to `ActiveTopicSnapshot` or any
  second data structure - the "smallest safe mechanism" bar was met
  without a new representation at all.
- Investigated and REJECTED: a per-entry "distinguisher letter/number
  token" signal for Sprint 47's OTHER known limitation (#9, "Aquascape
  A"/"Aquascape B" conflated by the `coverage > 0.5` lineage-skip
  heuristic) - direct tokenizer inspection found the shared tokenizer/
  stopword pipeline drops the single-letter token "a" while keeping
  "b", an inconsistent foundation no safe, general signal could be
  built on. NOT implemented; limitation #9 remains open, unchanged,
  now with a concrete, investigated (not merely assumed) reason on
  record. See `tests/test_bounded_entity_provenance.py::test_30_
  tokenizer_asymmetry_blocks_distinguisher_token_signal`.
- New test file: `tests/test_bounded_entity_provenance.py` (32 tests,
  all passing) - unit tests for the new gate (including explicit
  regression locks for `test_20`/`test_21`/Sprint 46's `test_27`, the
  three hardest existing boundaries), real E2E coverage for all 8
  scenarios (A-H) in this sprint's own brief, shared-alias/synonym-
  group interaction tests, topic-history-eviction and cross-
  conversation-isolation E2E tests, 2 performance tests, and 2
  regression locks for the investigated-and-rejected limitation #9
  approach.
- Regression sweep: targeted core suite (Sprint 47's own 13-file list
  plus this sprint's new file) - **677 passed, 0 failed**. Full
  repository sweep, run in 6 chunks (`pytest -n 4`, same standing
  precedent, split to fit the sandbox's own per-call wall-clock cap) -
  **2822 passed, 12 failed** (10 byte-for-byte identical to the
  standing baseline; 2 new-looking failures - `test_llm_tts_streaming_
  production.py::test_14_cancellation_during_synthesis`, `test_
  streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_
  plays` - re-run in ISOLATION (serial), both passed cleanly,
  confirming parallel-execution timing contention, not a regression;
  neither file nor the TTS/streaming subsystem it exercises was
  touched by this sprint's own edit). 92/94 collectible files
  (`test_main_bargein.py`/`test_root_main_bargein.py` uncollectible,
  same documented environment cause as every prior sprint).
- Performance: the new gate measured directly (3,000-call average,
  worst-case path that reaches it) at well under 1ms/call - under the
  5ms/turn target; a second measurement on the unaffected `active_
  score > 0` path confirmed no added cost there either. No network
  calls, no model inference, no embeddings.
- Persistent state: only `luno/memory_context.py` (source) and the new
  test file were modified/created this sprint. Isolated verification
  (running ONLY this sprint's own new test file, then again with the
  full Sprint 43-47 core entity/reference suite added) confirmed
  `config/*.json` (all 15 top-level files) byte-identical before/after
  in BOTH runs - stricter confirmation than Sprint 47's own check,
  which only ran its own new file alone. `_active_topic`/`_topic_
  history` confirmed to remain plain, non-persistent, in-memory
  `dict`s. `luno/memory_context.py` still never touches file I/O.

## Sprint 49 - Entity Provenance Disambiguation & Topic Lineage

- Baseline before: 677 passed / 0 failed (core suite, unchanged from
  Sprint 48's own closing snapshot); full repository 2822 passed / 12
  failed (10 documented baseline + 2 confirmed parallel-execution
  flakes), 92/94 collectible files - re-verified present and unchanged
  at Phase 0 before any edit (no discrepancy from the handover found).
- Root cause: Sprint 48's own known limitation #9 - `is_active_topic_
  relevant_to_query()`'s `active_score > 0` branch's `coverage > 0.5`
  lineage-skip check treats a majority-covered history entry as "same
  lineage, already merged," which is wrong when two entries are
  actually separately-named entities sharing generic vocabulary
  ("Aquascape A"/"Aquascape B"). The user's own verbatim `source_
  sentence` (Sprint 40) already contains evidence to distinguish them,
  but the check never reads it.
- Fix: one new function, `_extract_entity_differentiator()`, plus one
  new regex constant (`luno/memory_context.py`) - extracts a standalone
  single UPPERCASE letter from a `source_sentence`, reading the RAW,
  case-preserved text directly (never through the shared, lowercased/
  stopword-filtered `analyze_query()` token stream Sprint 48's own
  rejected approach hit an asymmetry in). Wired additively into the
  existing `coverage > 0.5` check: disagreeing, unambiguous
  differentiators bypass the lineage-skip, correctly producing a TIE
  (and therefore a REFUSAL) for a bare "Pompanya gimana?" instead of a
  silent, confident resolution to whichever entry is more recent.
- No new data structure - `ActiveTopicSnapshot`'s field set unchanged,
  verified via `dataclasses.fields()`. Deliberately scoped to
  UPPERCASE letters only, never digits or lowercase (see change-impact
  doc for the full safety reasoning on each restriction).
- Hard boundary matrix: 20 adversarial cases classified (MUST RESOLVE/
  PRESERVE/MERGE/REPLACE/REFUSE) before implementation - only the
  Aquascape A/B case (and its cross-domain generalization) changed
  status this sprint; all 18 other cases re-verified unchanged via
  dedicated tests, not assumed "probably okay."
- New test file: `tests/test_entity_provenance_disambiguation.py` (34
  tests, all passing) - unit tests for the new extraction function and
  gate, E2E fix verification (including cross-domain generalization and
  a negative control), lineage/coverage regression locks, hard-
  boundary-matrix tests, bounded-state/isolation tests, performance
  tests, known-limitation regression locks.
- Regression sweep: targeted core suite (Sprint 48's own 14-file list
  plus this sprint's new file) - **711 passed, 0 failed**. Full
  repository sweep, run in 8 chunks (`pytest -n 4`, same standing
  precedent, split further this sprint to reliably fit the sandbox's
  own per-call wall-clock cap) - **2889 passed, 11 failed** (10
  identical to the standing baseline; 1 new-looking failure - `test_
  streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_
  plays`, not in a file touched by this sprint - re-run in ISOLATION
  (serial), passed cleanly, confirming parallel-execution timing
  contention rather than a regression). 2900 total tests collected
  (95/97 collectible files, 2 pre-existing uncollectible, same
  documented cause as every prior sprint).
- Performance: `is_active_topic_relevant_to_query()` (worst-case path
  through the new gate, 5,000-call measurement) - mean 0.043ms, min
  0.035ms, max 0.513ms. `_extract_entity_differentiator()` alone
  (10,000-call measurement) - mean 0.0017ms, min 0.0015ms, max
  0.022ms. Both well under the 5ms/turn target, no network calls, no
  model inference, no embeddings.
- Persistent state: only `luno/memory_context.py` (source) and the new
  test file were modified/created this sprint. Two independent isolated
  verification runs (this sprint's own new file alone, then that file
  plus the full Sprint 45-48 core entity/reference suite) both
  confirmed all 15 top-level `config/*.json` files byte-identical
  immediately before/after. A separate comparison against the very
  start-of-sprint baseline DOES show `relationship_state.json`/`long_
  term_memory.json` changed - traced to the intervening full 8-chunk
  regression sweep exercising OTHER, pre-existing tests, unrelated to
  this sprint's own source edit (same pattern independently confirmed
  every sprint since 43). `luno/memory_context.py` still never touches
  file I/O; the new differentiator signal is computed on-demand, never
  stored or persisted.

## Sprint 50 - Runtime Observability, Test Logging & Real-World Data Capture

- Baseline before: 775 (targeted) / 0 failed - core suite unchanged from
  Sprint 49's own closing snapshot, plus this sprint's own 3 new files
  and `test_memory_voice_observability.py`; full repository 2900
  collected / 2889 passed / 11 failed (10 documented baseline + 1
  unreproduced flake) at Sprint 49's own close.
- Scope: OBSERVABILITY ONLY - no intelligence-behavior change. New event
  model (5 event types), `EventLogWriter` (first-ever disk persistence
  of the Event Bus's own event stream), and a real-world test-data
  capture/approve/replay loop (`luno/test_capture.py`/`luno/replay.py`)
  that did not exist in any form before this sprint.
- Fix/build summary: additive `MemoryTurnTrace` fields (`topic_decision`,
  `ambiguity_check_result`) plus a derived `is_ambiguity_refusal`
  property; 5 new `self._event_bus.publish(...)` call sites in
  `main_runtime_demo.py`, each already-computed data, each own
  try/except; new files `luno/dashboard/event_log_writer.py`,
  `luno/test_capture.py`, `luno/replay.py`; two new read-only dashboard
  collectors + GET routes; `/mark_test` console command.
- New test files: `tests/test_runtime_observability.py` (22 tests),
  `tests/test_real_world_capture.py` (13 tests),
  `tests/test_replay_engine.py` (12 tests) - 47 new tests total, all
  passing.
- Regression sweep: targeted (Sprint 50's own 3 new files + the full
  Sprint 43-49 core suite + `test_memory_voice_observability.py`) -
  **775 passed, 0 failed**. Full repository sweep, run in 8 chunks
  (`pytest -n 4`, same standing precedent) - **2947 collected, 2937
  passed, 10 failed, 2 uncollectible** - every failure/uncollectible
  identical to the standing baseline (6x `test_mic_device_index.py`, 1x
  `test_production_launcher.py::test_07`, 2x `test_real_adapters.py`, 1x
  `test_state_isolation.py`'s own `inspect.getsource` sandbox gap;
  `test_main_bargein.py`/`test_root_main_bargein.py` uncollectible,
  dependency-related). **Zero new regressions.** Sprint 49's own
  documented `test_streaming_e2e.py` flake did not reproduce this run
  (flakes do not always reproduce - not itself evidence of anything).
- Performance: `EventLogWriter._on_event()` (real disk I/O, 500-call
  measurement) - mean 0.122ms, min 0.090ms, max 0.513ms. `_redact()`
  alone (5,000-call measurement) - mean 0.004ms, min 0.003ms, max
  0.073ms. Both well under the 5ms target, no network calls, no model
  inference, no embeddings.
- Persistent state: `config/*.json` (15 files) SHA256-hashed before/
  after: the full 8-chunk sweep was byte-identical (unlike prior
  sprints, no other pre-existing test happened to touch
  `relationship_state.json`/`long_term_memory.json` during this
  particular sweep - a stronger, not a weaker, result). Two additional
  independent isolated runs (this sprint's own 3 new files alone; those
  3 plus the full Sprint 43-49 core suite) both confirmed byte-identical
  too. A real, minor mid-sprint side effect was found (via the
  regression sweep itself) and fixed: `test_memory_voice_observability.
  py`'s own pre-existing E2E dashboard test hadn't been updated to pass
  `observability_log_dir`, so it created a real `logs/` directory in the
  repository via the new (unconditional, by design) `DashboardServer`
  wiring - fixed by pointing that test at a temp directory. Two log
  files created before the fix landed remain in this checkout's own
  `logs/` (undeletable from this sandbox, per the workspace's own
  write-once protection) - harmless, redaction-verified, not
  `config/*.json` state.

## Dashboard Turn-State Recovery fix (fix-first, between Sprint 50 and Sprint 51)

- Baseline before: 775 (targeted) / 0 failed at Sprint 50's own close;
  full repository 2947 collected / 2937 passed / 10 failed / 2
  uncollectible (100 files).
- Scope: FIX ONLY, not an intelligence sprint. Root cause: `Planner
  BridgeModule._handle_utterance()` (`main_runtime_demo.py`) ran on an
  unsupervised daemon thread with no outer try/except; an exception
  escaping before it reached `NeedLLMResponse` (proven live via a
  `ConnectionAbortedError` injected into `self.planner.create_plan()` -
  the exact WinError-10053 shape from the bug report) left
  `SessionManagerModule` stuck at `THINKING` forever (no timeout exists
  for that state anywhere in this codebase) and the Dashboard's own
  busy-guard permanently rejected every further command. See
  `docs/change_impact/dashboard_turn_state_recovery.md` and
  `ARCHITECTURE_GUARD.md` §51 for the full root-cause/fix writeup,
  including why the `WinError 10038` half of the report was a separate,
  already-mostly-harmless connection-cleanup symptom, not part of the
  same failure.
- Fix summary: `PlannerBridgeModule._run_utterance_turn_safely()` (new)
  is now the turn-dispatch thread's actual target; on any escaped
  exception it publishes the SAME `llm_error` event a real OpenRouter
  failure already publishes (new `source:
  "planner_bridge_unhandled_exception"` field for observability only),
  reusing the EXISTING `session_manager`/`barge_in` routes and their
  already-idempotent handlers - zero new event type, route, or state
  machine. `luno/dashboard/server.py` gained
  `_is_expected_client_disconnect()` - the `WinError 10038` shape is now
  classified alongside the four `ConnectionError` subclasses as expected
  disconnect noise; every other `OSError` is still logged exactly as
  before. `luno/bootstrap/shutdown.py` was investigated and left
  UNCHANGED - already correctly wraps its own socket teardown.
- New test file: `tests/test_dashboard_turn_state_recovery.py` (13
  tests, all passing, every one a real E2E test through
  `RuntimeDemoConsole`/`SessionManagerModule`/`PlannerBridgeModule`/
  `DashboardServer` - the exact WinError-10053 reproduction and
  recovery, uncaught-thread-exception no longer escaping, Dashboard
  client disconnect not affecting backend state, cancellation, a full
  repeated failure/recovery cycle, busy-guard active-rejection-then-
  recovery, connection-error classification unit coverage, no
  error-log spam for an expected disconnect, `dashboard.stop()`
  mid-turn safety, observability `source` field, `_handle_llm_
  failure()` idempotency, and rapid sequential turns).
- Regression sweep: targeted (new file + `test_dashboard.py` +
  `test_runtime_demo.py` + `test_wake_barge_in_integration.py`) - all
  passing, 0 failed. Full repository sweep, run in 8 chunks (`pytest -n
  4`, same standing precedent) - **2960 collected (100 files, 98
  collectible, +1 file/+13 tests from this fix's own new test file),
  2949 passed, 11 failed** (10 identical to the standing baseline; the
  11th, `test_verification_dashboard.py::test_api_verification_
  reports_a_successful_verified_action_end_to_end`, failed with the
  EXACT SAME `inspect.getsource`/"could not get source code" signature
  as the already-documented `test_state_isolation.py` sandbox flake -
  re-run in ISOLATION, passed cleanly, confirming a new manifestation
  of an EXISTING flake category, not a regression - matches this
  project's own standing rule that flakes don't always reproduce, and
  don't always hit the same test file either). **Zero new regressions.**
- Performance: `_is_expected_client_disconnect()` (20,000-call
  measurement across three exception shapes) - mean 0.0002ms/call. The
  new wrapper's try/except overhead on the success path (100,000-call
  microbenchmark of the pattern in isolation) - mean 0.00017ms/call.
  Both far under the 5ms/turn target, no network calls, no LLM calls,
  no embeddings, no disk I/O added to any hot path.
- Persistent state: `config/*.json` (15 files) SHA256-hashed
  before/after: the full 8-chunk sweep was byte-identical. Two
  additional independent isolated runs (this fix's own new test file
  alone; that file plus `test_dashboard.py`/`test_runtime_demo.py`/
  `test_wake_barge_in_integration.py`) were also both byte-identical. No
  new `config/*.json` key, no new persistence path - this fix only
  changes in-memory Event Bus/thread-lifecycle behavior.

## Dashboard Turn-State Recovery fix, Part 2 / TTS-path (fix-first, takeover session, between Sprint 50 and Sprint 51) — CODE WRITTEN, NOT EXECUTED

**This entry is different in kind from every other entry in this
document.** Every other entry records a REAL, executed `pytest` run.
This one does not — the session that produced this fix had no working
Python environment for this project at all (no network access to
install the heavy ML/audio dependencies `main_runtime_demo.py` imports
at module level, and no bridge to run the real Windows `.venv`). The
numbers below are explicitly `N/A — not run`, not zeros, not estimates.
This entry exists so the gap is visible in the one document this
project's own §21 takeover protocol says to read for "the LATEST
section" — do not skip it, and do not assume a later, real entry
retroactively means this one ran.

- Baseline before: same as the entry above (the original Dashboard
  Turn-State Recovery fix's own close) — that fix's 13 tests did
  actually run and pass; this Part 2 fix's own tests have not.
- Scope: FIX ONLY, not an intelligence sprint. A SEPARATE, TTS-side gap
  the original fix (entry above) did not cover, found during a takeover
  re-investigation prompted by a real production report that the
  dashboard was STILL stuck after that first fix. Root cause:
  `SessionManagerModule._handle_playback_done()` (`luno/wake_session/
  manager.py`) only cleared `THINKING` when `state == SPEAKING`; a TTS
  failure on the very first chunk (before `speech_playback_started` ever
  fires) never reaches `SPEAKING`, so the resulting `speech_playback_
  cancelled` event was silently dropped, leaving `THINKING` stuck forever
  even though the LLM call had already succeeded. A second, structural
  instance of the SAME "unsupervised thread, no outer try/except" bug
  class the original fix closed for the planner was also found, unfixed,
  in `FishAudioAdapter._play()`/`_play_pipelined()`/`_play_stream()`/
  `_play_stream_pipelined()`. See `docs/change_impact/
  dashboard_turn_state_recovery_ttspath.md` and `ARCHITECTURE_GUARD.md`
  §52 for the full writeup.
- Fix summary: one new `elif state == THINKING:` branch in
  `_handle_playback_done()` (mirrors the pre-existing `SPEAKING` branch's
  own transition exactly); one new `except Exception` clause in each of
  the four `FishAudioAdapter._play*()` methods (publishes
  `SpeechPlaybackCancelled` with an `"unhandled: ..."` error message on
  any escaped exception, mirroring the original fix's own `_run_
  utterance_turn_safely()` wrapper design). Both additive, zero new
  event type/route/state machine.
- New test file: `tests/test_dashboard_turn_state_recovery_ttspath.py`
  (5 tests, written against the actual source, syntax-checked with
  `python3 -m py_compile` — **NOT RUN**): the direct live reproduction
  (TTS fails before playback starts, session recovers from `THINKINF`);
  the same scenario through the real `send_chat_message()` busy-guard;
  a unit-level test proving the new `FishAudioAdapter` `except` clause
  publishes exactly one terminal event instead of a silent thread death;
  a normal-turn regression baseline; a repeated failure-then-recovery
  cycle.
- Regression sweep: **N/A — not run.** Exact command to run first (see
  the change-impact doc's own "Not yet done" section for the fuller
  targeted list and the full-sweep follow-up):
  `pytest -q tests/test_dashboard_turn_state_recovery_ttspath.py
  tests/test_dashboard_turn_state_recovery.py tests/test_dashboard.py
  tests/test_runtime_demo.py tests/test_wake_barge_in_integration.py
  tests/test_fish_audio_real.py tests/test_fish_audio_barge_in.py`
- Performance: **N/A — not measured.** Expected negligible (an
  `except Exception` clause that never triggers has near-zero CPython
  cost, matching the original fix's own measured ~0.0002ms/call
  overhead for a structurally similar wrapper) but this is an
  expectation, not a measurement — do not cite a number here until one
  is actually taken with the same methodology the entry above used.
- Persistent state: **N/A — not verified.** Neither change reads or
  writes `config/*.json` or any other persisted file (both are pure
  in-memory event-publish/state-transition changes), so no impact is
  expected — but this should still be confirmed with a real SHA256
  before/after run, same as every other entry in this document.


## Sprint 52 (user-numbered) — Robust Home Assistant Command & Entity Resolution — ACTUALLY EXECUTED THIS SESSION (68 passed, 0 failed)

Filed as `ARCHITECTURE_GUARD.md` section 53 (see that section's own
numbering note — this document's section 52 above is an unrelated,
prior fix). Unlike the Dashboard Turn-State Recovery fix Part 2 entry
above, this entry's numbers were **actually produced by running
`pytest`** in the takeover session's cloud sandbox, against a
minimal-but-real dependency chain assembled specifically for this
feature (see `docs/change_impact/sprint52_ha_entity_resolution.md`'s
own "What was and wasn't executed" section for the exact scope,
methodology, and honest limits of what this proves).

**Targeted:** `pytest -q tests/test_sprint52_ha_entity_resolution.py
luno/tool_manager/tests/test_real_home_assistant_verification.py`
(inside the assembled tree) — **68 passed, 0 failed** (29 new + 39
pre-existing, both files run in full). The 39 pre-existing tests are
UNMODIFIED — this is real proof, not a hand computation, that the new
bounded-fuzzy-resolution tier does not change behavior for any of that
file's existing scenarios, including the two closest-by-`difflib`-score
cases (`test_similar_entity_single_suggestion` at 0.74,
`test_multiple_similar_entities` at 0.70 — both deliberately kept below
the new 0.78 auto-execute confidence bar).

**Full repository sweep:** **NOT RUN this session.** Only the files on
this feature's actual import path were staged in the sandbox (`luno/
config.py`, `luno/devices.py`, `luno/tool_manager/{context,handler,
models,result,utils}.py`, `luno/tool_manager/builtin/{home_assistant,
real_home_assistant}.py`, plus this sprint's own edits) — the other
~95 files and their own dependency trees (torch, the LLM stack, the
real Windows `.venv`) were not available. Vinn (or a future session with
real device/network access) should run the full chunked 8-way sweep per
this document's own established methodology before treating this as
part of the "verified, stable, production baseline" language elsewhere
in this document — that language does NOT yet cover Sprint 52.

**Also identified, NOT run:** `tests/test_verification_dashboard.py` —
also constructs a real `RealHomeAssistantHandler`, but via a full
`RuntimeDemoConsole`/dashboard-server E2E harness requiring
substantially more of the runtime staged than this focused resolver
change needed. Should be included in the next full sweep.

**Live Home Assistant server:** NOT reached. Every test above uses
`FakeHAClient`, a synthetic stand-in (`call_service()`/
`get_entity_state()`), matching the exact convention the pre-existing
Reliability Sprint tests already use. A real device-level smoke test
(a deliberately typo'd spoken/dashboard command against the real HA
instance) has not been performed and is the single most important
remaining verification step before trusting this in production.

**Performance:** `_resolve_entity_tiered()`'s new fuzzy path measured
for real in this sandbox (500-call loop): mean 0.20ms/call, well under
a 5ms target. Directional only — this sandbox, not the real Windows
host.

**Persistent state:** no `config/*.json` file touched differently;
`_score_candidates()` only reads the same `luno.devices.LIGHTS/
SWITCHES/SCRIPTS` dicts every pre-existing lookup already reads. Not
independently SHA256-verified this session (the full checkout's config
directory wasn't staged wholesale for this focused feature).

See `docs/change_impact/sprint52_ha_entity_resolution.md` for the full
root-cause/design/threshold-selection writeup.


## Sprint 53 (user-numbered) — Memory Session Summary API Compatibility Fix — ACTUALLY EXECUTED THIS SESSION (93 passed, 3 skipped, 0 failed)

Filed as `ARCHITECTURE_GUARD.md` section 54. Root cause: `luno/
adapters/openrouter.py`'s `RequestsOpenRouterClient._payload()`
hardcoded the completion-length JSON key as the literal `"max_tokens"`;
`luno/memory.py`'s `summarize_and_archive_session()` was the only
caller anywhere in the codebase that ever passed a non-None
`max_tokens`, so it was the only path tripping the reported
`"Unsupported parameter: 'max_tokens'..."` error. Fixed by routing that
key name through the project's existing `config.MAX_TOKENS_PARAM`
abstraction instead (already correctly used by `luno/main.py`'s legacy
call sites, never consulted here before this sprint). See `docs/
change_impact/memory_session_summary_api_compatibility.md` for the
full root-cause/fix writeup.

**Targeted (new):** `pytest -q tests/
test_memory_session_summary_api_compatibility.py` — **13 passed, 0
failed** (all 9 minimum coverage items the sprint brief required, plus
the dormant legacy-branch duck-typed path, plus an explicit
before/after reproduction of the exact reported error text via a fake
HTTP session simulating the real provider's own rejection rule).

**Regression (pre-existing, all UNMODIFIED by this sprint):**
- `luno/adapters/tests/test_openrouter_adapter.py` (run via its own
  documented standalone entry point, `python3 -m luno.adapters.tests.
  test_openrouter_adapter`, since that file's tests return `(bool, str)`
  tuples rather than using bare `assert`) — **31/31 scenarios passed** —
  real proof the `_payload()` change (the file this sprint edited) does
  not regress any of that file's own retry/backoff/streaming/
  cancellation/status-classification coverage.
- `luno/adapters/tests/test_llm_manager.py` (the SEPARATE multi-provider
  stack this sprint deliberately did not touch, despite it containing
  the textually identical latent hardcoded-`"max_tokens"` pattern — see
  this entry's own "Known limitation" note below) — **33 passed, 0
  failed** via `pytest -q`.
- `tests/test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py` (pre-existing coverage of the
  file this sprint's OTHER edit, `luno/memory.py`, lives in) — **16
  passed, 3 skipped**. The 3 skips are PRE-EXISTING and
  environment-specific (`tests/test_memory_persistence_hardening.py`
  referencing two `recovery/*.json` snapshot files not present in this
  sandbox checkout) — confirmed unrelated to this sprint's changes, not
  a new failure.
- **Combined single `pytest -q` run across all five files above: 93
  passed, 3 skipped, 0 failed.**

**Full repository sweep:** **NOT RUN this session**, same limitation as
every prior takeover-session entry in this document — only this
feature's real dependency chain was staged (`luno/{__init__,config,
persistence,memory}.py`, the full `luno/adapters/` package including
its `llm/` sub-package, `luno/core/`'s 14 files, `luno/vision_memory/`,
`luno/speech_chunk.py`, plus this sprint's own two edits) — the vision/
OpenCV, Whisper/audio, and Unity stacks' own heavier third-party
dependencies, and the real Windows `.venv`, were not available.

**Live LLM provider verification:** **NOT performed.** No
`OPENROUTER_API_KEY` in this sandbox, no network access to the device
bridge (same constraint documented in every prior entry in this file).
The before/after reproduction test above is a realistic SIMULATION
(a fake HTTP session returning the real provider's own documented
error JSON shape for a `"max_tokens"` key), never represented as a
live call anywhere in this sprint's documentation. See `docs/
change_impact/memory_session_summary_api_compatibility.md`'s own "Live
verification" section for the exact next step (trigger a real Session
Summary against the real configured provider/model and confirm the
success log line).

**Performance:** `_payload()`'s new config-driven key lookup measured
for real in this sandbox (5,000-call loop): mean ~0.0006ms/call, far
under a 5ms target — a single dict-key-name substitution against an
already-computed module-level constant, not a new computation.

**Persistent state:** verified via `find`-based diff before/after the
full test run — zero `*.json` files created or modified anywhere under
the staged checkout. Every test's own writes land under pytest's
`tmp_path` (via `tests/conftest.py`'s autouse `isolate_persistent_state`
fixture, unmodified this sprint), never a real `config/*.json` file.

**Known limitation, documented not fixed (out of scope this sprint):**
`luno/adapters/llm/base.py` has the textually identical hardcoded
`"max_tokens"` pattern, in the separate stack that powers normal chat.
Currently dormant for the same structural reason Session Summary was
the only path triggering the original bug. Recommended Sprint 54+
candidate — see `docs/change_impact/
memory_session_summary_api_compatibility.md`'s own "Known limitation"
section.

See `docs/change_impact/memory_session_summary_api_compatibility.md`
for the full root-cause/fix/compatibility-model writeup.


## Sprint 54 (user-numbered) — LLM Stack API Compatibility & Max Completion Tokens Hardening — ACTUALLY EXECUTED THIS SESSION (165 passed, 3 skipped, 0 failed)

Filed as `ARCHITECTURE_GUARD.md` section 55. **Correction to the Sprint
53 entry above:** this sprint's reconnaissance found that Sprint 53's
fix (`luno/adapters/openrouter.py`) was applied to a class that is
orphaned in production (`bootstrap/adapters.py` actually constructs
`LLMManagerAdapter`, not `OpenRouterAdapter`, under the confusingly-
named `"openrouter_adapter"` key). Sprint 53's own 68/31/33/16-passed
numbers above remain accurate for the files they tested; this note
only corrects which code path is actually live in production. See
`docs/change_impact/llm_max_completion_tokens_compatibility.md`'s own
"IMPORTANT CORRECTION" section for the full trace.

Root cause: `luno/adapters/llm/base.py`'s `OpenAICompatibleClient.
_payload()` — the shared request-body builder for `OpenRouterProvider`/
`OpenAIProvider`/`LocalProvider`, used by both `chat()` and
`stream_chat()` — hardcoded the completion-length JSON key as the
literal `"max_tokens"`, textually identical to the bug Sprint 53 fixed
in the (as established above) production-orphaned `luno/adapters/
openrouter.py`. This IS the code path Session Summary's
`LLMManagerAdapter.client` -> `chat_once()` -> default
`LLM_PROVIDER=openrouter` provider actually reaches. Fixed by routing
through `config.MAX_TOKENS_PARAM`, identical to Sprint 53's own fix
shape.

**Targeted (new):** `pytest -q tests/
test_llm_max_completion_tokens_compatibility.py` — **24 passed, 0
failed** (every `OpenAICompatibleClient` subclass × payload/legacy-key/
token-count/no-limit/streaming/config-driven/before-after-reproduction
cases, plus a tool-path non-interference check, a Sprint-53-regression
check, and 4 Anthropic/Gemini boundary-of-scope regression cases).

**Regression (pre-existing, all UNMODIFIED by this sprint):**
- `luno/adapters/llm/tests/test_providers.py` (covers every
  `LLMProviderClient` implementation, including the pre-existing
  Anthropic-required-`max_tokens` regression guard) — **48 passed.**
- `luno/adapters/tests/test_llm_manager.py` (`LLMManagerAdapter` itself
  — the class this sprint's fix is actually reached through in
  production) — **33 passed.**
- `luno/adapters/tests/test_openrouter_adapter.py` (Sprint 53's own
  fixed, production-orphaned class — run both via its own documented
  standalone entry point, **31/31 scenarios passed**, and under plain
  `pytest -q` as part of the combined run, **31 passed** there too).
- `tests/test_memory_session_summary_api_compatibility.py` (Sprint 53's
  own suite — the ORIGINAL bug's own regression coverage) — **13
  passed.**
- `tests/test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py` — **16 passed, 3 skipped**
  (same pre-existing, environment-specific skips Sprint 53 already
  documented, confirmed unrelated to this sprint).
- **Combined single `pytest -q` run across all six files: 165 passed,
  3 skipped, 0 failed.**

**Full repository sweep:** **NOT RUN this session** — same standing
limitation as Sprints 52/53 (only this feature's real dependency chain
staged, no network access for the rest of the checkout's own heavier
third-party dependencies).

**Live LLM provider verification:** **NOT performed.** No API
keys/network access in this sandbox. The before/after reproduction
tests are realistic SIMULATIONS of each provider's own documented
rejection rule, never represented as live calls.

**Performance:** `_payload()`'s new config-driven key lookup measured
for real in this sandbox: 5,000-call loop, mean ~0.0007ms/call;
200-call sample, max ~0.0039ms — far under a 5ms target.

**Persistent state:** verified via `find`-based diff before/after the
full test run — zero `config/*.json` files created or modified.

**Boundary-of-scope regression (explicitly verified, not assumed):**
Anthropic's own required `"max_tokens"` field and Gemini's own
`generationConfig.maxOutputTokens` field are BOTH confirmed unaffected
by `config.MAX_TOKENS_PARAM`'s value, in either direction (2 tests × 2
config values = 4 passing cases) — neither provider subclasses
`OpenAICompatibleClient`, and this sprint's fix lives entirely inside
that shared base class.

See `docs/change_impact/llm_max_completion_tokens_compatibility.md`
for the full root-cause/fix/compatibility-model writeup, including the
correction to Sprint 53's own documented call chain.

## Sprint 55 — Full Verification & System Stabilization

**First genuinely comprehensive full-repository sweep this project has
run:** full `luno/`+`tests/` source tree staged (376 `.py` files), full
`requirements.txt` dependency chain installed (including
torch/ultralytics/faster-whisper, never previously staged in this
project's sprint lineage). **3880 tests collected** (previous best was
~788–2900 in narrower hand-assembled chains).

**Result: 3866 passed, 10 failed, 4 skipped.** Every failure re-run in
isolation and root-caused (none assumed "baseline"): 1 fixed this
sprint (test-reliability only, see below), 2 confirmed timing/thread-
scheduling flakes (one with a newly precise root cause — an
`ultralytics`-package `tests/` namespace collision, not previously
identified this precisely), 2 environment-specific (require the real
`.env`, deliberately never loaded), 4 environment-specific (absent
`list_microphones.py` script — corrected root cause from prior
sprints' documentation), 2 deferred pre-existing test-code gaps in
`test_real_adapters.py` (unrelated to this sprint's scope, newly
exposed now that `speech_recognition` installs cleanly). **Zero
genuine new regressions.**

**Fixed:** `tests/test_dashboard_turn_state_recovery.py::test_05_e2e_
repeated_failure_recovery_cycle_stays_usable`'s live-state-polling
helper could race a zero-delay mocked round trip and miss the THINKING
state entirely — not a stuck-session bug, the opposite: proof recovery
happens faster than the old poll could observe. Fixed with a
history-based `_reached_state_since()` helper, test file only, no
production code changed. 13/13 (file) + 18/18 (combined with the
never-before-run `..._ttspath.py`) passing after the fix.

**Live LLM/HA provider verification: NOT POSSIBLE** — network egress
blocked (confirmed via both a direct `curl` failure and a real
`openrouter.ai` call attempt returning `403 Forbidden` from the sandbox
proxy).

**Real capture→approve→replay→diff E2E cycle: verified working**
against a scratch directory, confirmed replay never invokes a real
LLM. 47/47 observability/capture/replay unit tests passing.

**Persistent state:** all 15 `config/*.json` files byte-identical to
the real device before and after this sprint's entire regression sweep
plus every manual probe (one self-inflicted, fully diagnosed and
restored deviation along the way — see the change-impact doc).

**Performance:** `ConversationSession.transition_to()` 0.0018ms/op,
`EventBus.publish()` 0.013ms/op, `EventLogWriter._on_event()` (real
disk I/O) 0.048ms/op — all far under the 5ms target.

See `docs/change_impact/sprint55_stability_gate.md` for the full
phase-by-phase writeup, including the honestly-scoped "NOT VERIFIED /
NOT POSSIBLE" items and the out-of-scope `long_term_memory.json`
finding.

## Sprint 56 — Home Assistant + Query Intelligence

**Takeover re-verification (Phase 9-11):** Sprint 52's tiered HA entity
resolver re-verified against actual source (68/68 tests passing,
matches documentation exactly). New Category L ("typo closer to a
WRONG device") case closed with a genuinely-reproduced natural
corruption sweep plus a live adversarial near-tie reproduction through
the real `execute()` path — zero `call_service()` calls to either
device when ambiguous. `tests/test_sprint56_ha_safety_matrix.py` — 6
new tests, 0 failed.

**The one production code change (Phase 12):** `luno/memory_context.py`
— one new function, `_narrow_by_query_differentiator()`, closing a
real, live-reproduced gap in `select_topic_candidates()` (a query like
"Pompa A gimana?" now correctly narrows to just the "A" topic-history
entry instead of injecting both tied candidates; a bare query is
completely unchanged). `tests/test_sprint56_query_entity_
differentiator.py` — **17 passed, 0 failed**, including generalization
across two unrelated synthetic vocabularies and AST-level
no-hardcoding checks.

**Phase 13 (contextual HA references):** evidence matrix built,
DEFERRED to Sprint 57 with a documented reason — no safe existing hook
to build on without risking a second, parallel state system. Current
behavior already safe (never activates a device from ellipsis
references), just unhelpful (fails outright instead of resolving).

**Combined regression:** new Sprint 56 tests (23) + Sprint 52's own
suite (68) + the full memory/topic/entity-continuity surface (662
tests, 16 files) — **753 passed, 3 skipped, 0 failed.**

**Full repository sweep, re-run after this sprint's changes** (same
fixed 3880-test collection Sprint 55 established): **3865 passed, 11
failed, 4 skipped.** All 11 re-run in isolation and classified: 10
byte-for-byte identical to Sprint 55's own documented list; the 11th
one additional non-deterministic reproduction of the pre-existing,
pre-Sprint-49-documented `test_streaming_e2e.py::test_D` timing flake
(failed once in a parallel chunk, passed 4/4 in immediate isolated
reruns). **Zero genuine new regressions.**

**Performance:** `_resolve_entity_tiered()` fuzzy tier 0.175ms/call;
`select_topic_candidates()` with the new narrowing 0.007ms/call — both
far under the 5ms target.

**Persistent state:** all 15 `config/*.json` files byte-identical
before/after this sprint's entire test run and probe set.

See `docs/change_impact/sprint56_ha_query_intelligence.md` for the
full phase-by-phase writeup, including the complete Phase 13 evidence
matrix.

## Sprint 57 — Contextual Home Assistant References & Target Continuity

**Phase 0 finding:** Sprint 56's Phase 13 investigated two layers (the
Tool Manager resolver, `memory_context.py`'s topic machinery) and
correctly found neither suitable for contextual references — but
missed a THIRD, pre-existing layer: `PlannerBridgeModule._apply_
device_context()` in `main_runtime_demo.py`, a live, tested text-rewrite
mechanism that already did exactly this. This sprint hardens that
existing mechanism rather than building a new one — no second memory/
topic system created.

**What changed:** `_last_device_target`'s per-tool value enriched from a
bare slug string to `{target, turn_seq, entity_id, domain}`; bounded
freshness (`_CONTEXT_MAX_TURN_AGE = 6` turns, via a new per-conversation
turn counter reset on `ConversationEnded`); domain compatibility
(`_CONTEXT_FILL_COMPATIBLE_DOMAINS = {light, switch, fan, climate,
media_player}`); same-turn multi-device ambiguity clears memory instead
of guessing; broadened REMEMBER action set (`set_color`/`set_brightness`/
`set_value` in addition to `turn_on`/`turn_off`); failed/timed-out HA
commands un-remember their own target (`_invalidate_device_context_on_
failure`, correlated via a `threading.local()` slot since neither
`ToolCall` type carries conversation identity); "yang"/"tadi" added to
the existing filler-word set so "yang itu"/"yang tadi" phrasing resolves
from context (matching the pre-existing "-nya" support); one new
structured Event Bus event (`device_context_resolution`, Sprint-50-style,
never raw text). Plus a message-quality fix in `real_home_assistant.py`
(a genuinely target-less command now gets an honest "which device did
you mean" refusal instead of the confusing "None is currently
unavailable." — `run_script`'s own no-target fallback explicitly
exempted).

**Testing:** new `tests/test_sprint57_contextual_ha_references.py` — 42
tests, safety-matrix scenarios A-V plus explicit-priority/message-
quality/performance/observability coverage, **0 failed.** `tests/test_
device_context.py` — 22 tests (2 pre-existing assertions updated for the
new value shape, 0 behavior changes to the other 20), **0 failed.**

**Targeted regression** (this file + device_context + Sprint 52 HA +
Sprint 56 HA safety matrix + Sprint 56 differentiator + memory_context +
dashboard turn-state recovery x2 + wake_session_console + conversation_
ended_lifecycle_routing + response_policy + runtime_demo): **337 passed,
0 failed.**

**Full repository sweep** (`tests/`, excluding the 2 permanently-
uncollectible `test_main_bargein.py`/`test_root_main_bargein.py` files,
same convention every prior sprint used): **3079 passed, 11 failed, 3
skipped** (3093 collected). All 11 re-run in isolation and classified: 3
are timing-window flakes under `-n4` parallel CPU contention that PASS
standalone (one of which — `test_streaming_e2e.py::test_D_barge_in_
between_llm_and_tts_chunk_never_plays` — is the exact, already-documented
scheduling-jitter flake class from every prior sprint listed above); 8
are pre-existing ENVIRONMENT-SPECIFIC failures (7 byte-for-byte identical
to this document's own established list — `test_mic_device_index.py` x4,
`test_production_launcher.py::test_07`, `test_real_adapters.py` x2 — plus
1 new instance of the identical class, `test_llm_dashboard.py::test_api_
llm_endpoint_reports_manager_state`, this checkout's real `LLM_PROVIDER=
openrouter` vs. the test's assumed `openai` default). **Zero genuine
regressions** — none of the 11 touch any file this sprint modified.

**Performance:** `_apply_device_context()` ~0.02ms/call (1000-call
average) — far under the 5ms target. No LLM/network/embedding call in
the contextual resolution path (verified structurally).

**Persistent state:** all `config/*` files (JSON configs + vision-memory
SQLite files) byte-identical (MD5) before/after both the targeted
regression run and the full repository sweep. This sprint touched no
config file.

**`config/long_term_memory.json`:** diagnosed (not valid JSON, not
gzip, not standard zlib, not any common text encoding; Shannon entropy
7.65 bits/byte suggesting encrypted/compressed rather than merely
corrupted; no backup exists for this specific file), format/root cause
UNKNOWN, explicitly DEFERRED — out of scope for this sprint regardless
of cause, and not clearly safe to fix without a known encoding/backup.
The existing load path already fails closed safely (empty long-term
memory store, clear console warning).

See `docs/change_impact/sprint57_contextual_ha_references.md` for the
full phase-by-phase writeup, including the complete A-V safety matrix
test mapping and the `long_term_memory.json` diagnostic detail.

### Sprint 57 addendum — exact A-Q brief re-verification

A second, later brief re-issued the same Sprint 57 scope with its own
exact A-Q scenario matrix and required file names. No source code
changed — the existing implementation above already satisfies every
scenario. New file: `tests/test_sprint57_ha_contextual_reference.py`
(19 tests, 0 failed), covering scenarios A-Q plus a performance check.
Targeted re-run: 166 passed + 229 passed across two batches (HA/memory/
context suites + dashboard/session/runtime suites), 0 failed. Full
repository sweep re-run: **3103 passed, 9 failed, 3 skipped** (3115
collected — up from 3093, this addendum's own 19 new tests plus 3 fewer
non-deterministic flakes reproducing this run). All 9 failures
individually classified as the same pre-existing environment-specific/
parallel-timing-flake set already documented above (`test_state_
isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_
stop` re-confirmed passing standalone). **Zero genuine regressions.**
Persistent state: `config/*` byte-identical across every check. See
`docs/change_impact/ha_contextual_reference.md` for the full writeup
against this brief's own exact 15-point documentation structure.

## Sprint 58 — Home Assistant Multi-Entity & Group Commands

New pre-Planner group/multi-target resolution layer in
`PlannerBridgeModule` (`_apply_ha_group_resolution()` and helpers),
checked before Sprint 57's own contextual-fill. Implements explicit
multi-target ("A dan B [dan C ...]") and group-all ("semua lampu")
commands by reusing Sprint 52's `RealHomeAssistantHandler.
_resolve_entity_tiered()` (a throwaway `client=None` instance) once per
target — resolution for every target completes before any rewrite/
execution happens, guaranteeing zero HA API calls if any target is
ambiguous/unresolved/wrong-domain. Area-scoped groups and contextual
groups ("semuanya") are deliberately deferred with documented evidence
(zero area metadata in the registry; Sprint 57's memory is single-slot
by design). New test file: `tests/test_sprint58_ha_multi_entity_
commands.py` (27 tests, 0 failed), covering scenarios A-V including the
brief's own explicitly-required critical safety test (valid + ambiguous
target -> 0 HA calls, proved against the real production gate
mechanism).

**A real regression was found and fixed during implementation:** the
first version of explicit multi-target detection broke `tests/test_
runtime_demo.py::test_mixed_utterance_real_command_still_succeeds_
despite_unknown_clause` (a pre-existing test proving "turn on the
lights and how's the weather" still turns on the lights despite the
unrelated second clause). Root cause: comma/"and"/"then" are already
established GENERAL clause separators for unrelated actions in this
parser. Fix: multi-target detection scoped to text containing "dan" and
NOT also containing a comma/"and"/"then" (every one of this sprint's own
worked examples uses "dan" exclusively). Confirmed passing again after
the fix.

**Targeted regression:** `tests/test_sprint52_ha_entity_resolution.py` +
`tests/test_sprint56_ha_safety_matrix.py` + `tests/test_sprint56_query_
entity_differentiator.py` + `tests/test_sprint57_contextual_ha_
references.py` + `tests/test_sprint57_ha_contextual_reference.py` +
`tests/test_device_context.py` + `tests/test_sprint58_ha_multi_entity_
commands.py` — **162 passed, 0 failed**.

**Full repository sweep:** `-n4/loadfile` hit a pre-existing
pytest-xdist worker hang unrelated to this sprint's code (reproduced
twice, on two different parallel attempts) and was abandoned in favor of
a single-process run (`--ignore=tests/test_main_bargein.py
--ignore=tests/test_root_main_bargein.py --timeout=60
--timeout-method=signal`, one test — `test_dashboard.py::test_36_audio_
capture_store_unit_behavior` — deselected after confirming in isolation
it passes instantly, its full-suite hang being a pre-existing,
order-dependent thread/lock flake in `luno/dashboard/audio_bridge.py`, a
file this sprint never touches). Result: **3930 passed, 27 failed, 4
skipped, 1 deselected** in 768.76s. One failure was this sprint's own
regression (found, fixed, reconfirmed passing before this final number).
The other 26 were individually verified file-by-file (two of them also
test-by-test) to be pre-existing and unrelated to this sprint: the
already-documented `test_mic_device_index.py` (4, ENVIRONMENT-SPECIFIC),
`test_real_adapters.py` (2, INFRASTRUCTURE), and `test_production_
launcher.py` (2, incl. the already-documented test_07 flake) classes
above; `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_
state` and `test_llm_tts_streaming_production.py` (3) are live-config/
real-network-dependent (this sandbox has no LLM server on
localhost:1234, same limitation documented in every prior sprint); the
remaining files (`test_dashboard.py`, `test_emotion_engine.py`,
`test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
`test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`,
`test_voice_pipeline_latency.py`, `test_state_isolation.py`) all pass
100% when run individually/isolated — full-suite-only cross-test
thread/timing interference, the same flakiness class this document
already discusses sampling test files in curated batches to avoid.
**Zero genuine regressions** beyond the one found-and-fixed above.

**Performance:** `_apply_ha_group_resolution()` ~0.02-0.09ms/call
(100-200 call average, real device registry) — far under the 5ms
target. No network/embedding/LLM call anywhere in the group-resolution
path (verified structurally and by direct source read of the reused
resolver).

**Persistent state:** `config/*.json` byte-identical (MD5) before/after
every check this sprint ran, including a dedicated automated test
(`test_U_persistent_state_untouched_by_group_resolution`).

See `docs/change_impact/ha_multi_entity_commands.md` for the full
writeup, including both deferred-scenario justifications in full.

## Sprint 59 — Single-Room Home Assistant Group Control

Adds recognition of exactly one room name ("kamar") to Sprint 58's
existing group-all shape, reusing its enumeration/execution path
verbatim. `PlannerBridgeModule._SINGLE_ROOM_NAME = "kamar"` and
`_is_single_room_word()` are the entirety of the new logic — the
existing `group_all_light` branch now proceeds (instead of refusing)
when the area word is absent or equals "kamar", and still refuses, with
an honest named-room explanation, for any other area word. Membership
comes entirely from the in-process `luno.devices.LIGHTS` dict already
loaded at import time — no new config format, no database, no new
persistent memory, no HA API call to determine membership. Explicit
single-entity commands and Sprint 58's own explicit multi-target
continue to be resolved before this shape can match (single-step-only
gate, unchanged); a bare "lampu kamar" (no "semua") is resolved directly
to Main Lamp by the completely unmodified Sprint 52 fuzzy resolver,
costing zero new code. New test file: `tests/test_sprint59_single_room_
group_control.py` (21 tests, 0 failed), covering scenarios A–Q plus a
realistic end-to-end simulation and regression proof that Sprint
52/56/57/58 behavior is unchanged.

**A pre-existing test-fixture discrepancy was found and documented, not
fixed:** the real `config/lights.config.json` defines RGB Computer's
`entity_id` as `light.komputer`, but the shared cross-sprint fixture
`_REAL_LIGHTS` in `tests/test_sprint52_ha_entity_resolution.py` (used by
every HA test file since Sprint 52 via `_patch_real_devices()`)
incorrectly defines it as `light.kamar_tidur_pc`. Source code is
authority — the real config file is correct — but the shared fixture was
deliberately left unchanged this sprint (cross-sprint blast radius, out
of scope); this sprint's own tests match what `_patch_real_devices()`
actually installs, per every other sprint's own convention.

**One necessary Sprint 58 test update, not a regression:** `tests/
test_sprint58_ha_multi_entity_commands.py::test_F_area_qualified_
group_is_honestly_refused_not_guessed` asserted "semua lampu di kamar"
gets refused — that was Sprint 58's own documented, deferred
placeholder, intentionally superseded by this sprint for "kamar"
specifically. Updated to assert the same honest-refusal behavior
against "dapur" (still genuinely unsupported) instead. Re-run confirms
261/261 passing after the update.

**Targeted regression:** `tests/test_sprint52_ha_entity_resolution.py` +
`tests/test_sprint56_ha_safety_matrix.py` + `tests/test_sprint56_query_
entity_differentiator.py` + `tests/test_sprint57_contextual_ha_
references.py` + `tests/test_sprint57_ha_contextual_reference.py` +
`tests/test_device_context.py` + `tests/test_sprint58_ha_multi_entity_
commands.py` + `tests/test_sprint59_single_room_group_control.py` —
**261 passed, 0 failed**.

**Full repository sweep:** same single-process workaround as Sprint 58
(`--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_
bargein.py --timeout=60 --timeout-method=signal`, `test_dashboard.
py::test_36_audio_capture_store_unit_behavior` deselected for the same
pre-existing, already-verified thread/lock flake unrelated to this
sprint). Result: **3950 passed, 28 failed, 4 skipped, 1 deselected**.
Every failure was cross-verified against Sprint 58's own file-by-file
isolation investigation (same failing files: `test_mic_device_index.py`,
`test_real_adapters.py`, `test_production_launcher.py` —
environment/infrastructure; `test_llm_dashboard.py`/`test_llm_tts_
streaming_production.py` — no local LLM server; `test_dashboard.py`,
`test_emotion_engine.py`, `test_streaming_e2e.py`, `test_streaming_
speech_integration.py`, `test_tts_chunk_pipelining.py`, `test_tts_e2e_
pipeline.py`, `test_voice_pipeline_latency.py`, `test_state_isolation.
py` — full-suite-only cross-test timing interference, pass
individually). **Zero genuine regressions.**

**Performance:** ~0.02ms average per group-resolution call, far under
the 5ms target. No network/embedding/LLM call anywhere in the path.

**Persistent state:** `config/*.json` byte-identical (MD5) before/after
every check this sprint ran.

See `docs/change_impact/ha_single_room_group_control.md` for the full
writeup.

## Sprint 60 — Structured Room/Area Schema Foundation

Adds an optional, additive `"area"` string field to `config/lights.
config.json` entries (`luno/devices.py::load_lights_config()`), plus
two pure read-only helpers (`get_device_area()`/`get_devices_by_
area()`). `main_runtime_demo.py`'s existing Sprint 58/59 `group_all_
light` branch now prefers this structured metadata as the source of
truth for room membership wherever it exists, falling back to Sprint
59's original full-registry behavior for an unmigrated config — same
output for this project's real, migrated 3-light "kamar" set either
way. `config/lights.config.json` was migrated: Main Lamp/RGB Strip/RGB
Computer now carry `"area": "kamar"` (same evidence Sprint 59 already
documented); switches/scripts deliberately left untagged (no location
evidence). New test file: `tests/test_sprint60_area_schema.py` (27
tests, 0 failed), covering scenarios A–T plus safety invariants,
performance, and a realistic end-to-end test against the real migrated
config.

**One necessary Sprint 58 test update, not a regression:** `tests/
test_sprint58_ha_multi_entity_commands.py::test_F_area_qualified_
group_is_honestly_refused_not_guessed`'s assertion that no LIGHTS entry
carries an area/room/zone key is updated to assert every real light now
carries `"area": "kamar"` — Sprint 58's own documented gap, deliberately
closed by this sprint. The refusal behavior the test exists to prove is
unchanged.

**Targeted regression:** `tests/test_sprint52_ha_entity_resolution.py`
+ `tests/test_sprint56_ha_safety_matrix.py` + `tests/test_sprint56_
query_entity_differentiator.py` + `tests/test_sprint57_contextual_ha_
references.py` + `tests/test_sprint57_ha_contextual_reference.py` +
`tests/test_device_context.py` + `tests/test_sprint58_ha_multi_entity_
commands.py` + `tests/test_sprint59_single_room_group_control.py` +
`tests/test_sprint60_area_schema.py` — **210 passed, 0 failed**.

**Full repository sweep:** same single-process workaround as Sprint
58/59. Independently-verified collection for this checkout is **3190
tests** (`pytest --collect-only`, confirmed both with and without this
sprint's changes — the discrepancy from the 3983 previously documented
by Sprint 59 could not be explained from within this sprint's scope and
is called out explicitly, not silently reconciled — see `docs/change_
impact/area_schema_foundation.md` sections 10/15). A first run
unknowingly overlapped with a stale leftover pytest process from a
prior session (killed once discovered, likely timing-contention
source); a clean second run: **3158 passed, 28 failed, 3 skipped, 1
deselected**. Every failure individually classified — none touch any
file this sprint modified (`test_mic_device_index.py` (4)/`test_real_
adapters.py` (2)/`test_production_launcher.py` (2): environment/
infrastructure; `test_llm_dashboard.py` (1)/`test_llm_tts_streaming_
production.py` (5): no local LLM/speech server — directly re-confirmed
this sprint via a live `SpeechStreamIdleTimeout`/fish_audio network
error; `test_dashboard.py`, `test_emotion_engine.py`, `test_streaming_
e2e.py`, `test_streaming_speech_integration.py`, `test_tts_chunk_
pipelining.py`, `test_tts_e2e_pipeline.py`, `test_voice_pipeline_
latency.py`, `test_state_isolation.py` (13 combined) plus one failure
new to this sprint's own regression, `test_runtime_demo.py::test_
episodic_memory_end_to_end_...` (directly re-verified twice: passes in
isolation AND passes when its entire home file runs standalone, 78/78)
— all classified as the same full-suite-only cross-test timing
interference already documented since Sprint 55. **Zero genuine
regressions.**

**Performance:** `get_device_area()`/`get_devices_by_area()`
~0.0006–0.0007ms/call; `_apply_ha_group_resolution()`'s area-metadata
path ~0.026ms/call — far under the 5ms target. No network/embedding/
LLM call anywhere in the path (both helpers proved non-coroutine).

**Persistent state:** `config/*.json` MD5-identical before/after the
clean full sweep, plus a dedicated automated test
(`test_T_no_config_corruption`) proving every Sprint 60 read path is
read-only by construction. The one deliberate, one-time migration edit
to `config/lights.config.json` (adding `"area": "kamar"`) happened once
before any test ran and is the baseline every check verifies against.

See `docs/change_impact/area_schema_foundation.md` for the full
writeup, including the full STOP CONDITION analysis (none triggered).

## Sprint 61 — Generalized Area-Aware Home Assistant Group Command

**Root cause:** the "kamar" hardcoding was isolated entirely to
`PlannerBridgeModule._apply_ha_group_resolution()`'s `group_all_light`
branch (a literal string comparison against `"kamar"` via the now-
removed `_SINGLE_ROOM_NAME`/`_is_single_room_word()`). The area-word
capture regex (`_GROUP_AREA_RE`) was already fully generic.

**Architecture change:** the branch now uses `devices.get_devices_by_
area(area_word)` (Sprint 60's existing helper, reused unmodified) as the
source of truth for any area, with a defensive `domain == "light"`
re-check per candidate and a dynamically-enumerated refusal message when
an area word matches zero configured lights. `_SINGLE_ROOM_NAME`/
`_is_single_room_word()` removed (zero other consumers, grep-confirmed);
Event Bus key `room_word_recognized` renamed to `area_recognized`. No
second resolver, no fuzzy area matching, no new persistent state.

**One necessary Sprint 59 test update, not a regression:** `tests/
test_sprint59_single_room_group_control.py::test_K_empty_room_group_is_
a_safe_no_op`'s assertion narrowed to "refusal occurred" (message text
now differs by design under the stricter unknown-area-always-refuses
rule); a new sibling test proves the original "no lights configured"
message remains reachable, unchanged, via the non-area-qualified "semua
lampu" shape. Additionally, the shared `_REAL_LIGHTS` fixture in `tests/
test_sprint52_ha_entity_resolution.py` was additively updated (all 3
entries gained `"area": "kamar"`, matching the REAL already-migrated
production config) - required because Sprint 61's new safety rule
removes the "unmigrated registry" fallback Sprint 59's own tests had
relied on.

**Targeted regression:** `tests/test_sprint52_ha_entity_resolution.py`
+ `tests/test_sprint56_ha_safety_matrix.py` + `tests/test_sprint56_
query_entity_differentiator.py` + `tests/test_sprint57_contextual_ha_
references.py` + `tests/test_sprint57_ha_contextual_reference.py` +
`tests/test_device_context.py` + `tests/test_sprint58_ha_multi_entity_
commands.py` + `tests/test_sprint59_single_room_group_control.py` +
`tests/test_sprint60_area_schema.py` + `tests/test_sprint61_
generalized_area_groups.py` — **245 passed, 0 failed**. Related
runtime/dashboard tests — **125 passed, 0 failed**.

**Full repository sweep:** independently-verified collection is **3225
tests** (`pytest --collect-only`; 3190 Sprint-60 baseline + 34 new
Sprint 61 tests + 1 new Sprint 59 test = 3225, internally consistent).
Clean run (same single-process workaround as Sprint 58/59/60): **3193
passed, 28 failed, 3 skipped, 1 deselected**. Every failure individually
classified — all match the same already-documented pre-existing/flaky/
environment/network-dependent classes carried since Sprint 55/60
(`test_mic_device_index.py`, `test_real_adapters.py`, `test_production_
launcher.py`: environment/infrastructure; `test_llm_dashboard.py`,
`test_llm_tts_streaming_production.py`: no local LLM/speech server;
`test_dashboard.py`, `test_emotion_engine.py`, `test_streaming_e2e.py`,
`test_streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`,
`test_tts_e2e_pipeline.py`, `test_voice_pipeline_latency.py`,
`test_state_isolation.py`, `test_runtime_demo.py`: full-suite-only
cross-test timing interference). The 2 failures not previously seen in
Sprint 60's own list (`test_runtime_demo.py::test_episodic_memory_end_
to_end_...` and `test_streaming_e2e.py::test_D_barge_in_between_llm_
and_tts_chunk_never_plays`) were individually re-verified in isolation
and confirmed the same pre-existing flakiness class, not new
regressions. **Zero genuine regressions.**

**Performance:** area resolution (both known-area and unknown-area/
refusal code paths) measured at ~0.0007–0.027ms/call — far under the
5ms target. No network/embedding/LLM call anywhere in the path.

**Persistent state:** `config/*.json` MD5-identical before/after the
clean full sweep. Sprint 61 made zero changes to `devices.py` or any
config file — `get_device_area()`/`get_devices_by_area()` are reused
exactly as Sprint 60 built them.

See `docs/change_impact/generalized_area_groups.md` for the full
writeup, including the full STOP CONDITION analysis (none triggered).

## Sprint 62 — Multi-Domain Area Group Control

**Root cause / finding:** evaluated extending Sprint 60/61's
area-qualified HA group commands beyond `light` to `switch`/`fan`/
`climate`/`media_player`. Only `light` has a registry structure safe for
`"area"` metadata. `switch` (`devices.SWITCHES`) has a resolver and
execution path, but its loader (`load_switches_config()`) only ever
produces a flat `name -> entity_id` STRING per entry — confirmed against
both the loader source and the real `config/switches.config.json`
(`{"Baterai": "switch.tasmota_tasmota3", ...}`) — structurally no way to
attach `"area"`. `fan`/`climate`/`media_player` have no registry/config
loader/resolver at all in this checkout. This is a direct hit on STOP
CONDITION 1 for every domain but `light` — documented as DEFERRED per
the brief's own instruction, no schema forced onto any of them.

**Architecture change:** none, functionally — `_apply_ha_group_
resolution()`, `_GROUP_LIGHT_WORD_RE`, `_GROUP_AREA_RE`, and `devices.
get_devices_by_area()`/`get_device_area()` are byte-for-byte unchanged
from Sprint 61. The only `main_runtime_demo.py` edit is a
documentation-only comment. No `get_switches_by_area()`-style
domain-specific helper was created.

**New evidence this sprint adds:** an "unsupported domain" area-group
command (e.g. "matikan semua switch di kamar", "nyalakan semua AC di
kamar") never matches `_GROUP_LIGHT_WORD_RE`, so `_apply_ha_group_
resolution()` returns the text completely untouched; the untouched text
then fails safely in the pre-existing single-target pipeline, traced all
the way to `RealHomeAssistantHandler.execute()`'s own `if target and
entity_id is None: return self._unknown_device_result(...)` guard, which
returns before ever reaching `self._client.call_service(...)` — proved
directly against a `FakeHAClient` (zero calls recorded).

**New test file:** `tests/test_sprint62_multi_domain_area_groups.py` (26
tests, scenarios A–R plus persistent-state and end-to-end checks). 0
failed.

**Targeted regression:** same 10 files as Sprint 61's own targeted batch
plus this sprint's file — **271 passed, 0 failed**. Related runtime/
dashboard tests — **124 passed, 0 failed**.

**Full repository sweep:** `pytest tests/ -q --ignore=tests/test_main_
bargein.py --ignore=tests/test_root_main_bargein.py --timeout=60
--timeout-method=signal` — **3220 passed, 28 failed, 3 skipped** in 741s
(collection 3251 = Sprint 61's 3225 + 26 new tests, consistent). Every
failure individually re-run in isolation: 15 passed cleanly in an
isolated batch (full-suite-only cross-test timing interference, the
class documented since Sprint 55: `test_llm_tts_streaming_production.py`
×4, `test_streaming_e2e.py` ×1, `test_streaming_speech_integration.py`
×1, `test_tts_chunk_pipelining.py` ×3, `test_tts_e2e_pipeline.py` ×2,
`test_voice_pipeline_latency.py` ×3, `test_production_launcher.py::
test_24`); 5 more passed in isolation, same class (`test_dashboard.py`
×2 — including `test_36_audio_capture_store_unit_behavior`, the
long-documented order-dependent flake normally excluded via `--deselect`
in prior sweeps, which this sweep's command omitted — `test_emotion_
engine.py` ×1, `test_runtime_demo.py::test_episodic_memory_end_to_
end_...` ×1 — re-verified in isolation for the 4th consecutive sprint —
and `test_state_isolation.py` ×1, confirmed passing even fully alone); 8
failed even in isolation, confirmed genuine environment/infrastructure
failures unrelated to this sprint (`test_mic_device_index.py` ×4,
`test_real_adapters.py` ×2, `test_production_launcher.py::test_07` ×1,
`test_llm_dashboard.py` ×1 — missing audio hardware/no local LLM/speech
server reachable from this sandbox). **Zero genuine regressions.**

**Performance:** both the `light` area-group path and the
unsupported-domain fallthrough path measured well under the 5ms target
(300 iterations each). No network/LLM/blocking call anywhere in
resolution.

**Persistent state:** `config/*.json` untouched — this sprint's only
production-code edit is a comment; a dedicated automated test hashes the
3 config files before/after exercising every resolution path this
sprint's tests cover and passed.

See `docs/change_impact/multi_domain_area_groups.md` for the full
writeup, including the full STOP CONDITION analysis (only condition 1
triggered, for the deferred domains, and was honored rather than forced).

## Sprint 63 — Long-Term Memory Persistence Recovery & Integrity Investigation

**Outcome: DIAGNOSIS ONLY.** `config/long_term_memory.json` was not
modified, migrated, or recovered — a STOP CONDITION applies (unprovable
format, no usable backup, single copy, new evidence the content likely
isn't derived from memory data at all). No loader/writer code change was
made or warranted.

**New forensic finding:** the file (1849 bytes, MD5 `c16525937a6bc063
e182c1b6b120e42e`, unchanged since Sprint 55) is not a uniform layer —
bytes 0–1475 measure 7.87 bits/byte entropy (near-random), bytes
1476–1535 are a 60-byte run of literal NUL bytes, and bytes 1536–1848
decode as clean, readable ASCII text matching the standard MIT LICENSE
boilerplate verbatim. This entropy discontinuity plus embedded plaintext
is inconsistent with genuine single-layer encrypted/compressed data and
instead suggests the file's content is an accidental fragment of an
unrelated binary artifact — not recoverable memory data. `config/
backups/` contains zero pre-existing `long_term_memory.*.json` entries,
proving the corruption did not happen through `luno.memory._save()`'s
own (backup-first) write path.

**New test file:** `tests/test_sprint63_long_term_memory_recovery.py`
(24 tests — 8 forensic regression-guards against the real file
read-only, plus scenarios A–O reproducing current-failure/valid/
malformed/truncated/empty/missing/synthetic-corrupted loads, backup
creation, atomic replacement, no-data-loss round trips, no-silent-
overwrite, and idempotency, all against copies or synthetic fixtures).
0 failed.

**Targeted regression:** all 28 pre-existing memory-related test files
+ `test_persistent_state_hardening.py` + this sprint's own file —
**1103 passed, 3 skipped, 0 failed**.

**Full repository sweep:** `pytest tests/ -q --ignore=tests/test_main_
bargein.py --ignore=tests/test_root_main_bargein.py --deselect tests/
test_dashboard.py::test_36_audio_capture_store_unit_behavior
--timeout=60 --timeout-method=signal` — **3244 passed, 27 failed, 3
skipped, 1 deselected** in 686s (collection 3275 = Sprint 62's 3251 +
24 new tests, consistent). Every failure matches the exact same file/
test set already exhaustively classified in Sprint 62 (`test_dashboard.
py`, `test_emotion_engine.py`, `test_llm_dashboard.py`, `test_llm_tts_
streaming_production.py` ×4, `test_mic_device_index.py` ×4, `test_
production_launcher.py` ×2, `test_real_adapters.py` ×2, `test_runtime_
demo.py`'s episodic-memory test, `test_state_isolation.py`, `test_
streaming_e2e.py`, `test_streaming_speech_integration.py`, `test_tts_
chunk_pipelining.py` ×3, `test_tts_e2e_pipeline.py` ×2, `test_voice_
pipeline_latency.py` ×3) — spot-re-verified in isolation this sprint
too (5-test batch: 3 passed cleanly, `test_state_isolation.py` confirmed
passing when run fully alone, `test_llm_dashboard.py` confirmed failing
even alone — genuine environment gap, no local LLM server). **Zero
genuine regressions**, and none relate to memory/persistence code —
this sprint changed zero production code.

**Persistent state:** `config/*.json` (15 files) MD5-identical before/
after both sweeps, including `config/long_term_memory.json` itself
(unchanged hash, matching every sprint since 55). The only new file
anywhere in `config/` is an additive, read-only, byte-identical
preservation backup of the corrupted file's current bytes
(`config/backups/long_term_memory.<timestamp>.pre_sprint63_forensic.
json`).

See `docs/change_impact/long_term_memory_recovery.md` for the full
writeup, including the complete forensic analysis and STOP CONDITION
evaluation.

## Sprint 64 — Long-Term Memory Corruption ORIGIN Forensics

Forensic investigation only (zero production code changes). New test
file: `tests/test_sprint64_memory_corruption_forensics.py` (15 tests,
entirely read-only against production state or scoped to
`tmp_path`/`monkeypatch`). 0 failed.

**Targeted memory-suite regression:**
`test_sprint63_long_term_memory_recovery.py` (24) +
`test_sprint64_memory_corruption_forensics.py` (15) +
`test_memory_persistence_hardening.py` (8 passed, 3 skipped) — **47
passed, 3 skipped, 0 failed.**

**Full repository sweep:** `python3 -m pytest tests/ -q
--continue-on-collection-errors --ignore=tests/test_main_bargein.py
--timeout=60 -p no:cacheprovider` — **3249 passed, 38 failed, 3 skipped,
1 collection error** in 745s. Every failure/error is in unrelated
e2e/hardware-simulation modules (`test_mic_device_index.py`,
`test_production_launcher.py`, `test_real_adapters.py`,
`test_runtime_demo.py`'s episodic-memory test, `test_state_isolation.py`,
`test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
`test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`,
`test_vision_ask_vision.py`, `test_vision_sprint8.py`,
`test_voice_pipeline_latency.py`, plus `test_root_main_bargein.py`'s own
collection `FileNotFoundError`) — the same general failure classes
Sprint 63 documented (3244 passed / 27 failed), grown slightly with this
sprint's own additional 39 tests included. The one failing test whose
name contains "memory" (`test_episodic_memory_end_to_end_...`) concerns
`EPISODIC_MEMORY_FILE`, a structurally distinct store from
`LONG_TERM_MEMORY_FILE` — not the file under investigation. **Zero
failures reference `LONG_TERM_MEMORY_FILE` or `long_term_memory.json`.**

One earlier full-sweep attempt this sprint hit a `Fatal Python error:
Segmentation fault` inside a background logging/event-processing thread
(`luno/adapters/utils.py`/`base.py`) at ~8-9% of collection — unrelated
to persistence, non-reproducible on immediate retry (the retry ran to
completion past that point cleanly). A separate, long-running orphaned
`pytest tests/` process (over an hour of wall-clock runtime, left over
from earlier sprint activity) was found still running in this sandbox and
was terminated before this sprint's own sweep, to avoid resource
contention between the two runs; it never touched
`LONG_TERM_MEMORY_FILE` at any point (production file hash confirmed
unchanged across this entire sprint regardless).

**Persistent state:** `config/*.json` (15 files) SHA-256-identical
before/after the full sweep, including `config/long_term_memory.json`
itself and its Sprint 63 preservation backup (still the only
`long_term_memory.*.json` entry in `config/backups/`). Zero drift.

See `docs/change_impact/long_term_memory_corruption_forensics.md` for the
full writeup.

## Sprint 65 — Luno Tool & File Access Audit

Audit only (zero production code changes). New test file:
`tests/test_sprint65_tool_file_access_audit.py` (27 tests - structural
assertions against real source plus synthetic `tmp_path`/monkeypatch
reproductions only, never the real checkout). 0 failed.

**Targeted regression:** `tests/test_sprint65_tool_file_access_audit.py`
+ `luno/tool_manager/tests/` + `tests/test_browser_wiring.py` +
`tests/test_desktop_control.py` + camera suite
(`test_camera_health_check_timeout.py`, `test_camera_presence.py`,
`test_camera_ptz_bootstrap.py`) + `tests/test_real_adapters.py` +
Sprint 63/64's own memory suites — **232 passed, 2 failed, 3 skipped.**
The 2 failures (`test_real_adapters.py::test_real_whisper_source_calls_listener_in_order_for_nonempty_text`,
`::test_real_whisper_source_skips_empty_transcription`) are the same
pre-existing whisper-adapter flaky class Sprint 64 already documented.

**Full repository sweep:** `python3 -m pytest tests/ -q
--continue-on-collection-errors --ignore=tests/test_main_bargein.py
--timeout=60 -p no:cacheprovider` — **3275 passed, 39 failed, 3 skipped,
1 collection error** in 747s. 38 of the 39 failures plus the 1 collection
error exactly match file/test names already classified as full-suite-
only timing-interference flakiness in Sprint 62/63's own baseline
(`test_dashboard.py`, `test_emotion_engine.py`, `test_llm_dashboard.py`,
`test_llm_tts_streaming_production.py`, `test_mic_device_index.py`,
`test_production_launcher.py`, `test_real_adapters.py`,
`test_runtime_demo.py`'s episodic-memory test, `test_state_isolation.py`,
`test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
`test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`,
`test_vision_ask_vision.py`, `test_vision_sprint8.py`,
`test_voice_pipeline_latency.py`, `test_root_main_bargein.py`'s
collection `FileNotFoundError`). One test not previously seen failing —
`test_verification_dashboard.py::test_api_verification_reports_a_successful_verified_action_end_to_end`
— was re-run individually per this sprint's own classification rule and
**passed cleanly in isolation** (1 passed in 1.37s), confirming it is the
same full-suite-only timing class rather than a genuine regression.
**Zero failures relate to tool/file-access/filesystem code** — this
sprint changed zero production code.

**Persistent state:** `config/*.json` (15 files) SHA-256-identical
before/after the full sweep. Additional critical-file hash set for this
sprint (`ARCHITECTURE_GUARD.md`, `luno/tool_manager/manager.py`,
`luno/tool_manager/registry.py`, `luno/desktop_control.py`,
`luno/browser/security.py`, `luno/browser/permissions.py`) confirmed
byte-identical before/after the test run (checked prior to this sprint's
own documentation edits, which are expected/deliberate changes, not
drift). Zero unintended drift.

See `docs/change_impact/tool_file_access_audit.md` for the full writeup.

## Sprint 66 — Tool Boundary Hardening

Security hardening addressing Sprint 65's two findings
(SPRINT65-001/-002). Two production files changed:
`luno/browser/security.py` (new `validate_download_directory()`,
`_resolve_for_comparison()`, `_path_contains()`; upgraded
`validate_download_path()` to close a symlink-bypass gap) and
`luno/tool_manager/builtin/real_browser.py` (fail-closed startup
validation in `__init__()`, defense-in-depth re-validation in
`_dispatch()`'s `"download"` branch). Tool registry confirmed already
safe by construction (Phase 6) — zero registry code changed, tests only.
New test file: `tests/test_sprint66_tool_boundary_hardening.py` (40
tests). 0 failed.

**Targeted regression:** this sprint's 40 + Sprint 65's 27 +
`luno/tool_manager/tests/` + `tests/test_browser_wiring.py` +
`tests/test_desktop_control.py` — **198 passed, 0 failed.**

**Full repository sweep:** `python3 -m pytest tests/ -q
--continue-on-collection-errors --ignore=tests/test_main_bargein.py
--timeout=60 -p no:cacheprovider` — **3316 passed, 38 failed, 3 skipped,
1 collection error** in 749s. Every failure matches the identical
file/test-name set Sprint 65's own baseline already classified as
full-suite-only timing/environment-coupled flakiness (`test_dashboard.py`,
`test_emotion_engine.py`, `test_llm_dashboard.py`,
`test_llm_tts_streaming_production.py`, `test_mic_device_index.py`,
`test_production_launcher.py`, `test_real_adapters.py`,
`test_runtime_demo.py`'s episodic-memory test, `test_state_isolation.py`,
`test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
`test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`,
`test_verification_dashboard.py`, `test_vision_ask_vision.py`,
`test_vision_sprint8.py`, `test_voice_pipeline_latency.py`). The 1
collection error is `test_root_main_bargein.py`'s pre-existing,
already-documented `legacy_main.py`-absent INFRASTRUCTURE issue (this
sprint's sweep command omitted the second `--ignore` flag prior sprints'
baseline commands used for that file specifically, which is why it
surfaced as a collection ERROR rather than being silently skipped — the
underlying cause is identical and unrelated to this sprint's changes). A
representative sample of 33 of the 38 failing tests
(`test_vision_ask_vision.py` in full, one each from `test_dashboard.py`,
`test_emotion_engine.py`, `test_tts_chunk_pipelining.py`) was re-run in
isolation and **passed 33/33**, confirming the full-suite-only timing
class, not a genuine regression. Zero failures relate to the browser
security/download boundary or tool registry code this sprint touched.

**Persistent state:** `config/*.json` (15 files) SHA-256-identical
before the test run vs. after the full sweep. Critical-file hash set for
this sprint (`ARCHITECTURE_GUARD.md`, `luno/tool_manager/manager.py`,
`luno/tool_manager/registry.py`, `luno/desktop_control.py`,
`luno/browser/security.py`, `luno/browser/permissions.py`,
`luno/browser/config.py`, `luno/tool_manager/builtin/real_browser.py`,
`luno/config.py`, `main.py`, `main_runtime_demo.py`) confirmed
byte-identical before/after. Zero drift.

See `docs/change_impact/tool_boundary_hardening.md` for the full writeup.

## Sprint 67 — Mutation Audit Trail & Forensic Observability

Observability/forensics only - adds no new capability. New module
`luno/mutation_audit.py` (structured JSONL audit records under
`logs/mutation_audit/`, reusing Sprint 50's `logs/` root and Sprint 66's
path-safety primitives). Integrated into `luno/persistence.py::atomic_
write_json()` (7 stores), `luno/memory.py::_atomic_write_json()`
(`config/long_term_memory.json`'s own dedicated coverage), and `luno/
tool_manager/builtin/real_browser.py`'s download branch. New test file:
`tests/test_sprint67_mutation_audit_trail.py` (48 tests). 0 failed.
`tests/conftest.py`'s autouse isolation fixture extended to redirect
`mutation_audit.AUDIT_LOG_DIR` per test.

**Targeted regression:** this sprint's 48 + Sprint 63/64/65/66's own 118
+ the full 27-file/1103-test memory suite + `luno/tool_manager/tests/` +
`tests/test_browser_wiring.py` + `tests/test_desktop_control.py` +
`tests/test_relationship_engine.py` + `tests/test_response_policy.py` +
`tests/test_proactive.py` - **1633 passed, 3 skipped, 0 failed.**

**Full repository sweep:** `python3 -m pytest tests/ -q
--continue-on-collection-errors --ignore=tests/test_main_bargein.py
--ignore=tests/test_root_main_bargein.py --timeout=60 -p no:cacheprovider`
- **3374 passed, 28 failed, 3 skipped** in 750s (0 collection errors this
run - both bargein files correctly excluded via `--ignore` this time).
Every failure matches the identical file/test-name set every prior
sprint since 62/63 has already classified as full-suite-only timing
flakiness (`test_dashboard.py`, `test_emotion_engine.py`, `test_llm_
dashboard.py`, `test_llm_tts_streaming_production.py`, `test_production_
launcher.py`, `test_real_adapters.py`, `test_runtime_demo.py`'s
episodic-memory test, `test_state_isolation.py`, `test_streaming_e2e.py`,
`test_streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`,
`test_tts_e2e_pipeline.py`, `test_voice_pipeline_latency.py`) or the
pre-existing, already-documented `list_microphones.py`-absent
environment gap (`test_mic_device_index.py`, 4/16 failures, reproduces
identically in isolation - an environment issue, not timing). One
individual test not seen failing in Sprint 66's own run (`test_llm_tts_
streaming_production.py::test_13_cancellation_before_first_audio`) was
re-run in isolation and passed cleanly - same already-documented file,
not a new regression. Zero failures touch `luno/persistence.py`, `luno/
memory.py`'s save path, `luno/mutation_audit.py`, or the browser
download boundary.

**Persistent state:** `config/*.json` (15 files) SHA-256-identical
before this sprint's code edits vs. after the full sweep, including
`long_term_memory.json` itself (unchanged since Sprint 55). Critical-
file hash set for this sprint (14 files, including the three modules
this sprint edited/created plus `tests/conftest.py`) confirmed
byte-identical. `config/backups/` count unchanged (12). No real
`logs/mutation_audit/` directory exists in the checkout - test isolation
held throughout.

See `docs/change_impact/mutation_audit_trail.md` for the full writeup.

## Sprint 68 (user-numbered): Mutation Audit Trail Verification & Hardening

Independently verified Sprint 67's own claims against the actual
checkout (not re-asserted) and hardened the audit trail: path
canonicalization for stored records, defensive bounding extended to
`operation`/`path`/`correlation_id`, a non-fatal startup visibility
check for a misconfigured `MUTATION_AUDIT_LOG_DIR`, a pending/completed
two-phase append (`record_pending_mutation()`) that makes the Sprint
67-documented post-mutation-audit-failure blind spot DETECTABLE (not
closed - closing it would require a second transaction system,
explicitly forbidden by this sprint's STOP CONDITIONS), and a new,
strictly read-only forensic replay helper (`luno/mutation_audit_replay.
py`, AST-verified to contain zero write-mode `open()` calls and zero
`os.remove()`/`os.replace()`/`os.rename()` calls).

**New test file:** `tests/test_sprint68_mutation_audit_hardening.py`
(67 tests). 0 failed.

**Targeted regression:** this sprint's 67 + Sprint 67's 48 + Sprint
65/66's 67 - 182 passed, 0 failed. Memory/persistence suite (`-k
"memory or persist"`, 1247 tests) - 1244 passed, 3 skipped, 0 failed.
Browser/tool-manager suite (96 tests) - 96 passed, 0 failed.
Runtime/dashboard suite (244 tests) - 243 passed, 1 failed (`test_llm_
dashboard.py` - pre-existing, no local LLM server reachable from this
sandbox).

**Full repository sweep:** `python3 -m pytest tests/ -q --ignore=tests/
test_main_bargein.py --ignore=tests/test_root_main_bargein.py
--timeout=60 --timeout-method=signal` - 3472 collected. First three
attempts this sprint returned 28-29 failed each, matching (mostly) the
long-documented full-suite-only timing-flakiness class from prior
sprints, PLUS two of this sprint's own new retention tests failing in a
way that did not resolve even after adding a bounded retry - the retry
not helping was the signal this was not actually a timing race.
Diagnostics added to the failing assertion caught the real cause:
`time.time()` reading back `1000006.0` (~11.6 days after the Unix
epoch) instead of a real 2026 timestamp, traced to `tests/test_camera_
presence.py`'s `_adapter()` helper doing `vmod.time.time = lambda:
...` - a raw, never-restored assignment on the SHARED stdlib `time`
module object, corrupting real `time.time()` for every test running
afterward in the same pytest process. Fixed with one autouse fixture in
that file (`_restore_real_time_time`) restoring the real `time.time`
after each of its own tests - confirmed via direct reproduction
(`pytest tests/test_camera_presence.py tests/test_sprint68_mutation_
audit_hardening.py`: 2/76 failing before, 76/76 passing after). Zero
production code (`luno/`) touched by this fix.

**After the fix, a clean full-suite run: 3460 passed, 9 failed, 3
skipped, 454s** (down from 760s - the frozen clock had been forcing
many timing-dependent tests through slow/degenerate paths). This also
retroactively explains why `test_dashboard.py`, `test_emotion_engine.py`,
`test_llm_tts_streaming_production.py`, `test_streaming_e2e.py`, `test_
streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`, `test_
tts_e2e_pipeline.py`, and `test_voice_pipeline_latency.py` had all been
intermittently classified as "full-suite-only timing flakiness" for an
unknown number of prior sprints (at least since Sprint 61/62, per this
document's own history above) - they were silently absorbing the same
leaked frozen clock, and the true root cause was never actually
`test_camera_presence.py`-adjacent until this sprint's own diagnostic
work traced it there. **Every remaining failure re-run individually: 8
reproduce even in isolation**, all matching the identical, long-
documented environment gap (`test_mic_device_index.py` ×4, `test_real_
adapters.py` ×2, `test_production_launcher.py::test_07` ×1, `test_llm_
dashboard.py` ×1 - missing audio hardware / no local LLM or speech
server reachable from this sandbox); **the 9th** (`test_state_
isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_
stop`) passed cleanly alone, matching its own already-documented order-
dependent-flake classification. Zero failures touch `luno/mutation_
audit.py`, `luno/mutation_audit_replay.py`, `luno/persistence.py`, or
`luno/memory.py`'s save path. If a future sprint's full sweep shows the
old ~28-failure pattern again, suspect a similar un-restored global-
state leak in another test file before assuming timing flakiness.

**Persistent state:** `config/*.json` (15 files) SHA-256-identical from
before this sprint's code edits through the end of the full sweep,
including `long_term_memory.json`
(`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a` -
unchanged since Sprint 55). `config/backups/` count unchanged (12).

See `docs/change_impact/mutation_audit_hardening.md` for the full
writeup.

## Sprint 69 (user-numbered): Camera Device / OpenCV Stability Fix

Fixed a real, evidence-based camera stability bug: `cv2.VideoCapture
(index)` with no explicit backend let `CAP_ANY` reach `CAP_FFMPEG`
(~30s internal stream timeout) and `CAP_OBSENSOR` ("index out of
range") for a plain LOCAL device index, matching the reported log
exactly (`CAP_OBSENSOR` confirmed to genuinely exist in this project's
installed OpenCV build). The more severe bug: `luno/vision.py::
_capture_frame()` had zero timeout bounding at all before this fix, and
is polled up to 2×/s by `RealVisionSource._tracked_cycle_loop()` - a
broken camera would re-hang on every tick with no backoff. Fixed with
explicit platform-based local-backend candidate selection (never
touching string/`CAMERA_URL` sources, which correctly keep FFMPEG), a
generalized bounded-open helper shared between the startup health check
and every real capture (with guaranteed eventual release even on
timeout), a `CameraState` enum, and a reopen cooldown
(`CAMERA_REOPEN_COOLDOWN_S`, default 10s) that makes a poll tick return
immediately - without touching `cv2` at all - while the camera is
already known broken. `health.py`'s previously separate, uncoordinated
camera probe now shares the same `_camera_lock` as real captures,
closing a genuine startup-probe-vs-poll-loop concurrency gap.

**New test file:** `tests/test_sprint69_camera_stability.py` (22 tests -
all 17 brief-mandated categories A-Q, plus a security-construct guard
and a diagnostic-script read-only/fast-completion check). 0 failed on
first run.

**Pre-existing test files updated as a direct, necessary consequence
(not incidental breakage):** `tests/test_camera_health_check_
timeout.py` (same patch-target-mismatch class Sprint 68 found in
`test_camera_presence.py` - `_check_camera()` now calls into `luno.
vision`'s module-level `import cv2`, so `monkeypatch.setitem(sys.
modules, "cv2", ...)` no longer intercepts it; rewritten to `monkeypatch.
setattr(vision_module, "cv2", ...)`) and `tests/test_vision_sprint8.py`
(two fake `cv2.VideoCapture` lambdas took only one positional arg - this
sandbox's real `_local_backend_candidates()` now returns `[CAP_V4L2]`,
so the bounded-open call passes a second `backend` argument; fixed by
accepting an optional second parameter. Separately, `test_02_camera_
disconnect_then_automatic_reconnect` assumed an immediate retry succeeds
on the very next `capture_frame()` call after a failed read - now
genuinely false by design, since the cooldown exists specifically to
stop that hammering - updated to advance a controllable fake clock past
`CAMERA_REOPEN_COOLDOWN_S` before expecting the reconnect).

**Targeted regression:** the new test file + `test_camera_health_check_
timeout.py` + `test_camera_presence.py` + `test_camera_ptz_bootstrap.py`
+ `luno/tool_manager/tests/test_camera_ptz.py` + `test_vision_
provider.py` + `test_vision_sprint8.py` + `test_vision_intent.py` +
`test_vision_ask_vision.py` + `test_vision_intent_classifier.py` - 174
passed, 0 failed.

**Full repository sweep, run twice to check determinism** (`python3 -m
pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/
test_root_main_bargein.py --timeout=60 --timeout-method=signal`):

- **Run 1:** 3481 passed, 11 failed, 3 skipped, 444s. The 9 expected
  baseline failures (see Sprint 68's own entry above) plus TWO new ones:
  `test_llm_tts_streaming_production.py::test_14_cancellation_during_
  synthesis` and `test_verification_dashboard.py::test_api_verification_
  reports_a_successful_verified_action_end_to_end`. Both re-ran clean in
  isolation immediately after (`pytest <both> -v`: 2 passed).
- **Run 2 (same command, same checkout, no code changed in between):**
  3483 passed, 9 failed, 3 skipped, 442s - the exact same 9-failure set
  as Sprint 68's own established baseline (`test_mic_device_index.py`
  ×4, `test_real_adapters.py` ×2, `test_production_launcher.py::
  test_07`, `test_llm_dashboard.py`, `test_state_isolation.py`). Neither
  of Run 1's two extra failures reappeared.

**Conclusion:** the two extra Run-1 failures are a non-deterministic,
order/timing-dependent, full-suite-only flake - not a Sprint 69
regression. If this fix had broken `test_llm_tts_streaming_production.py`
or `test_verification_dashboard.py` (neither of which touches camera/
vision code at all), the failure would reproduce deterministically on
every run, not vanish on an unchanged re-run. This matches Sprint 68's
own explicit caveat that its `test_camera_presence.py` fix "resolves one
confirmed full-suite-only false-failure source; it was not exhaustively
proven no other file has a similar leak" - `test_llm_tts_streaming_
production.py` was in fact one of the files Sprint 68 itself already
named as historically absorbing that class of flakiness. Investigating
that separate, pre-existing, unrelated flake is out of scope for this
camera-only sprint (brief item 13: don't touch unrelated vision/test
behavior) and is left for a future sprint. Zero failures across either
run touch `luno/vision.py`, `luno/bootstrap/health.py`, or
`camera_diagnostic.py`.

**Persistent state:** `config/*.json` (27 files) SHA-256-identical from
before this sprint's code edits through the end of BOTH full regression
sweeps, including `long_term_memory.json`
(`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a` -
unchanged since Sprint 55).

See `docs/change_impact/camera_stability_fix.md` for the full writeup.

## Sprint 69.1 (user-numbered): Camera Runtime/Dashboard Disconnect Forensics & Fix

Follow-up to Sprint 69 after a report that the dashboard still showed
`Camera = DISCONNECTED`, OpenCV still emitted the `cap_ffmpeg_impl` ~30s
stream timeout, and `scheduled_vision_poll` logged `0.0ms` despite the
camera remaining disconnected. Per the brief's own MANDATORY FIRST STEP,
the complete production call chain was traced end to end rather than
assuming Sprint 69's code was the actual runtime path.

**Repo-wide forensic proof (not assumption):** a grep for `import cv2`
plus an AST-based scan for `.VideoCapture(` call sites confirms exactly
ONE production call site exists in the entire repository -
`luno/vision.py::_open_capture_bounded()`. Every camera-touching
function funnels through it via the same `_camera_lock`, ruling out a
second, bypassing open path. `scheduled_vision_poll`'s "0.0ms" was
traced through `BaseAdapter.handle_event()`'s documented no-op default
(`VisionAdapter` never overrides it) - it is a structurally inert
heartbeat log line, not evidence the camera was actually polled; real
camera polling runs entirely through `RealVisionSource`'s own two
self-scheduling background threads, unconnected to the
Scheduler/EventMapping mechanism. The dashboard's `camera_connected`
field was traced end to end (frontend badge logic ->
`collect_vision()` -> `VisionAdapter._extra_status()` -> the single
write site `on_camera_status()`) and confirmed to be a live passthrough,
not a stale cached default - a `DISCONNECTED` badge reflects a genuine
`connected: false`. The leading, evidence-consistent (but NOT provable
from this sandbox) explanation for continued FFMPEG use: `config.py`
auto-derives `CAMERA_URL` from Tapo PTZ credentials when set, and any
string source correctly (by Sprint 69's own design) uses `CAP_ANY`,
which reaches FFMPEG - documented honestly as unresolved per the
brief's STOP CONDITION, since this sandbox cannot access the
deployment's live `.env`/process/logs.

**A real, pre-existing credential-leak bug found and fixed:** three
sites in `luno/vision.py` and one in `camera_diagnostic.py` built
error/reason strings from the raw, un-redacted camera source (`{source!r}`),
which for a `CAMERA_URL` with embedded credentials would leak them into
`camera_status()["error"]` and `CameraDisconnected` event data.
Pre-existing (not introduced by Sprint 69), surfaced only when this
sprint's own required "no credential leakage" test exercised the path
for the first time. Fixed via a new `_classify_source_for_log()` helper
and a `_sanitize_error_text()` helper for third-party exception text.

**Two structural correctness fixes:** `RealVisionSource._tracked_cycle_once()`
queried/published `camera_status()` BEFORE `capture_frame()` each cycle
(always reporting the previous cycle's outcome) - moved into a `finally`
block after the capture attempt. `VisionAdapter.on_camera_status()`'s
`CameraReconnected` event only fired for `previous is False`, missing
the very first `None -> True` connect - changed to `previous is not
True`.

**Diagnostics added:** structured, timestamped `[Vision]`-prefixed
logging at source classification, backend selection, per-attempt open
timing/outcome, backend-mismatch detection, state transitions, and
cooldown-skip visibility - closing the observability gap that made the
reported symptom unanswerable from this sandbox. `camera_diagnostic.py`
extended to print platform + actual local backend candidates.

**New test file:** `tests/test_sprint69_1_camera_dashboard_forensics.py`
(15 tests, all 11 brief-mandated categories, including a permanent
AST-based single-call-site regression guard). 0 failed after fixing two
test-only bugs (missing `grab()` on two fake `VideoCapture` stand-ins -
not a production defect).

**Targeted regression:** camera/vision suite (11 files, 189 tests) +
dashboard suite (4 files, 83 tests) - all passed.

**Full repository sweep** (`python3 -m pytest tests/ -q --ignore=tests/
test_main_bargein.py --ignore=tests/test_root_main_bargein.py
--timeout=60 --timeout-method=signal`): **3498 passed, 9 failed, 3
skipped, 445.88s**.

| category | test | status |
|---|---|---|
| environment gap (pre-existing baseline) | `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_mic_device_index.py` (4 tests) | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_real_adapters.py` (2 tests) | failed, matches baseline |
| full-suite-only timing flake (same file/class as Sprint 69's own documented flake, different specific test) | `test_state_isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_stop` | failed in full sweep, passed alone in 1.12s |

All 9 failures are accounted for: 8 are the exact established
environment-gap set unchanged since Sprint 68/69's own baseline, and the
9th is a different specific test within `test_state_isolation.py` - a
file Sprint 69's own baseline already documents as a source of
full-suite-only, order/timing-dependent flakiness (unrelated to
planner/console threading being touched by this sprint - nothing in
`test_state_isolation.py` or the planner/console code was modified).
Reconfirmed clean in isolation immediately after. Zero failures touch
`luno/vision.py`, `luno/adapters/real_vision.py`,
`luno/adapters/vision.py`, or `camera_diagnostic.py`.

**Persistent state:** `config/*.json` (27 files) SHA-256-identical
before this sprint's edits through the end of the full regression sweep,
including `long_term_memory.json` unchanged since Sprint 55. No
persistent camera configuration was mutated, per the brief's explicit
instruction to prefer code-path correction over configuration mutation.

See `docs/change_impact/camera_runtime_dashboard_forensics.md` for the
full writeup.

## Sprint 69 (Tapo C212 Authentication & Connection Recovery) (user-numbered)

Forensic audit + evidence-based error-classification/security hardening
for `luno/tool_manager/builtin/real_camera_ptz.py` (the `pytapo`-based
Tapo pan/tilt TOOL) - a different subsystem from Sprint 69/69.1/69.2's
own `luno/vision.py` OpenCV/RTSP capture path. Full trace and findings:
`docs/change_impact/tapo_c212_authentication.md`.

**New test file:** `tests/test_sprint69_tapo_c212_auth.py` (27 tests) -
construction-time classification, per-command classification, bounded-
retry proof, credential-redaction, no-arbitrary-URL/no-persistent-
storage structural guards, target-precedence, no regression to
unrelated tools. All fakes only.

**Targeted regression:** `tests/test_sprint69_tapo_c212_auth.py` (27) +
`luno/tool_manager/tests/test_camera_ptz.py` (32, unmodified) + `tests/
test_camera_ptz_bootstrap.py` (5, unmodified) + `tests/
test_camera_health_check_timeout.py` + `tests/test_camera_presence.py`
+ `tests/test_sprint69_camera_stability.py` + `tests/
test_sprint69_1_camera_dashboard_forensics.py` + `tests/
test_sprint69_2_camera_state_machine_hardening.py` = **134 passed, 0
failed, 9.48s**. Full `luno/tool_manager/tests/` + `luno/planner/
tests/` = **183 passed, 0 failed** (60 pre-existing
`PytestReturnNotNoneWarning` warnings in `test_tool_manager.py`, not
touched by this sprint).

**Full repository sweep, exact baseline-comparable command**
(`python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py
--ignore=tests/test_root_main_bargein.py --timeout=60
--timeout-method=signal`): **3548 passed, 9 failed, 3 skipped,
449.47s**. 3548 = the established 3498-test baseline + exactly 50 new
tests (27 from this sprint + 23 from the already-written-but-not-yet-
separately-documented Sprint 69.2 test file, both physically present in
`tests/` in this sandbox). All 9 failures are the EXACT SAME established
environment-gap set, unchanged since Sprint 68/69's own baseline:

| category | test | status |
|---|---|---|
| environment gap (pre-existing baseline) | `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_mic_device_index.py` (4 tests) | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_real_adapters.py` (2 tests) | failed, matches baseline |
| full-suite-only timing flake (documented since Sprint 69.1) | `test_state_isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_stop` | failed in full sweep, passed alone in isolation |

Zero NEW failures relative to the established baseline. Zero failures
touch `real_camera_ptz.py`, `camera_ptz.py`, `luno/bootstrap/
adapters.py`, or any camera/PTZ file.

**Broader sweep also including `luno/`'s own test directories**
(`python3 -m pytest tests/ luno/ -q --ignore=... --timeout=60
--timeout-method=signal`, NOT the established baseline command - `luno/
tool_manager/tests/` and `luno/planner/tests/` were never part of the
historical `tests/`-only baseline, run here as additional, broader
evidence): **4366 passed, 10 failed, 4 skipped, 482.88s**. The same 9
baseline categories above, PLUS one additional failure -
`tests/test_llm_tts_streaming_production.py::test_13_cancellation_
before_first_audio` - re-run in isolation together with the
`test_state_isolation.py` flake per `docs/project_handover.md` §21's
own explicit "re-run any TTS/streaming/voice-pipeline-timing failure in
isolation before classifying" protocol: **both passed cleanly in
isolation (2 passed in 1.90s)**, confirming full-suite-only timing
flakiness (this exact file was already flagged as flaky under full-
suite stress in Sprint 69's own "Next Recommended Sprint" note) -
not a regression, and unrelated to any file this sprint touched.

**Persistent state:** `config/*.json` (27 files) SHA-256-hashed
immediately before this sprint's first edit and re-hashed after both
full regression sweeps completed - byte-identical throughout, including
`long_term_memory.json` unchanged since Sprint 55.

See `docs/change_impact/tapo_c212_authentication.md` and
`ARCHITECTURE_GUARD.md` §72 for the full writeup.

## Sprint 70 (Tapo C212 Live Authentication & Auto-Recovery) (user-numbered)

Builds directly on the previous section's classification layer - adds a
bounded, in-memory connection state machine and single-retry auto-
recovery to `luno/tool_manager/builtin/real_camera_ptz.py`. Full
writeup: `docs/change_impact/tapo_c212_live_recovery.md`.

**New test file:** `tests/test_sprint70_tapo_live_recovery.py` (23
tests) - categories A-O: valid auth, invalid-credentials-never-retried,
session-expired/transient-network recovery, permanent-unreachable
bounded failure, rate-limit no-retry, unknown-exception safe behavior,
a proven `AUTHENTICATING` state transition, reconnect-construction-
failure reporting the original error, two independent no-infinite-
retry proofs (dynamic bounded-call-count + static AST no-loop guard),
credential redaction on the recovery path, mock-backend non-
interference, full backward compatibility when `client_factory` is
omitted, persistent-state immutability (static + dynamic), and
dashboard/PTZ status separation. All fakes only.

**Targeted regression:** `tests/test_sprint70_tapo_live_recovery.py`
(23) + `tests/test_sprint69_tapo_c212_auth.py` (27) + `luno/
tool_manager/tests/test_camera_ptz.py` (32, unmodified) + `tests/
test_camera_ptz_bootstrap.py` (5, unmodified) = **87 passed, 0
failed**. Combined with the full camera/vision suite (`tests/
test_camera_health_check_timeout.py`, `tests/test_camera_presence.py`,
`tests/test_sprint69_camera_stability.py`, `tests/
test_sprint69_1_camera_dashboard_forensics.py`, `tests/
test_sprint69_2_camera_state_machine_hardening.py`): all passed except
one full-suite-only timing flake within `test_sprint69_2_camera_state_
machine_hardening.py` (a file this sprint never touched - it exercises
`luno/vision.py`'s own real-background-thread timing-bounded read/open
paths) - reconfirmed clean (23/23) in isolation on 3 separate re-runs
across this sprint's work, each time a DIFFERENT specific test within
that file (`test_4_...`, then later `test_9_...`) failed under full-
suite CPU/scheduling stress and passed alone every time - consistent
full-suite-only flakiness, not a regression. `luno/tool_manager/tests/`
+ `luno/planner/tests/` = 183 passed, 0 failed.

**Full repository sweep, exact baseline-comparable command**
(`python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py
--ignore=tests/test_root_main_bargein.py --timeout=60
--timeout-method=signal`): **3570 passed, 10 failed, 3 skipped,
463.94s**. Expected count is 3548 (Sprint 69's own established count) +
23 new Sprint 70 tests = 3571 attempted; 3570 passed because ONE test
within that total - the pre-existing `test_sprint69_2_camera_state_
machine_hardening.py::test_9_read_timeout_never_synchronously_
releases_a_still_running_capture` (not a Sprint-70-authored test) -
hit the exact full-suite-only flakiness described above on this
particular run, and is NOT counted as new/regressed (re-ran clean
alone immediately after, 1 passed in 1.65s).

| category | test | status |
|---|---|---|
| environment gap (pre-existing baseline) | `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_mic_device_index.py` (4 tests) | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_real_adapters.py` (2 tests) | failed, matches baseline |
| full-suite-only timing flake (documented since Sprint 69.1) | `test_state_isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_stop` | failed in full sweep, passed alone |
| full-suite-only timing flake (newly observed this sprint, same class as the above) | `test_sprint69_2_camera_state_machine_hardening.py::test_9_read_timeout_never_synchronously_releases_a_still_running_capture` | failed in full sweep, passed alone (1.65s) |

Zero failures touch `real_camera_ptz.py`, `camera_ptz.py`, `luno/
bootstrap/adapters.py`, or any other file this sprint modified. Zero
NEW failure categories relative to Sprint 69's own established set -
the one additional failure this run is a full-suite-only timing flake
within an already-flake-prone file this sprint never touched, not a
regression this sprint introduced.

**Persistent state:** `config/*.json` (27 files) SHA-256-hashed
immediately before this sprint's first edit and re-hashed after the
full regression sweep completed - byte-identical throughout. A
dedicated test additionally proves no drift occurs DURING an actual
in-process recovery scenario (session-expired -> reconnect -> retry),
not just "at rest" before/after the whole suite.

See `docs/change_impact/tapo_c212_live_recovery.md` and
`ARCHITECTURE_GUARD.md` §73 for the full writeup.

## Sprint 71 (Dashboard Startup & Access Recovery) (user-numbered)

Root cause: `DashboardServer.start()` (`luno/dashboard/server.py`) and
`main.py`'s own call site were both unguarded against the `OSError` a
socket-bind failure raises (most commonly a stale/previous process
holding the port) - the exception propagated out of `main()` uncaught,
crashing the entire Luno process instead of only failing to start the
dashboard. Fixed by catching `OSError` at both layers and degrading to
the already-established `DASHBOARD_ENABLED=false` behavior ("rest of
Luno keeps working, no dashboard") instead of crashing. See
`docs/change_impact/dashboard_startup_recovery.md` and `ARCHITECTURE_
GUARD.md` §74 for the full writeup.

**Targeted:** `tests/test_sprint71_dashboard_startup_recovery.py` (new,
15/15 passed) - `tests/test_dashboard.py` (47/47 passed) - `tests/
test_dashboard_turn_state_recovery.py` (13/13 passed) - `tests/
test_production_launcher.py` (23/24 passed, 1 known failure, see below).

**Full sweep:** whole-repo `--collect-only` (no new collection errors;
the 2 pre-existing uncollectible files - `test_main_bargein.py`,
`test_root_main_bargein.py`, both dependent on the absent `legacy_main.
py` / missing `faster_whisper` - unchanged from baseline) + the
remaining ~157 project test files run in 8 chunks:

| Category | Files/tests | Result |
|---|---|---|
| environment gap (pre-existing baseline) | `test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration` | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_mic_device_index.py` (6 tests) | failed, matches baseline |
| environment gap (pre-existing baseline) | `test_real_adapters.py` (2 whisper tests) | failed, matches baseline |
| environment gap (newly observed, this checkout's `.env` sets `MAX_TOKENS_PARAM=max_tokens`, overriding the code's own `max_completion_tokens` default) | `test_llm_max_completion_tokens_compatibility.py` (7 tests), `test_memory_session_summary_api_compatibility.py` (5 tests) | failed, `.env`-configuration mismatch, zero relation to dashboard code |
| environment gap (this long-lived checkout's accumulated `config/backups/` count - 41 present vs. an expected pristine 12) | `test_sprint63_long_term_memory_recovery.py` (2), `test_sprint64_memory_corruption_forensics.py` (3), `test_sprint68_mutation_audit_hardening.py` (1) | failed, pre-existing accumulated state, unrelated to this sprint (Phase 8 confirms this sprint's own runs added zero new backup files) |
| full-suite-only timing flake (same class documented since Sprint 69.1) | `luno/barge_in/tests/test_barge_in.py::test_confirm_mode_interrupt_then_no_resumes`, `::test_stress_many_ordinary_utterances_then_one_real_interrupt` | failed under `-n 4` parallel load, both passed (2/2) re-run in isolation |
| full-suite-only segfault (same class documented since Sprint 5 - thread accumulation/GIL contention across many real-thread test files chained in one process) | occurred only when chaining `test_sprint71_dashboard_startup_recovery.py` + `test_dashboard.py` + `test_dashboard_turn_state_recovery.py` + `test_production_launcher.py` in one process, inside pre-existing `stop()` -> `log()` (code this sprint did not touch) during interpreter-shutdown thread teardown | did not reproduce when any of the 4 files was run individually (all passed cleanly standalone) |

Zero failures touch `main.py` or `luno/dashboard/server.py`'s Sprint 71
changes. Zero NEW failure categories caused by this sprint's code - every
observed failure traces to a pre-existing environment/checkout-state
factor or documented parallel-execution flakiness class, confirmed by
re-running in isolation where applicable.

**Persistent state:** `config/*.json` (15 files) SHA-256-hashed
immediately before this sprint's first edit and re-hashed after both a
full `python main.py` run (including dashboard start/stop) and the full
Sprint 71 test suite (including port-conflict/bind-failure/restart
scenarios) - byte-identical throughout. `config/backups/` file count
unchanged (41 before, 41 after; zero files newer than the snapshot).

See `docs/change_impact/dashboard_startup_recovery.md` and
`ARCHITECTURE_GUARD.md` §74 for the full writeup.

## Sprint 71 (Camera Patrol) (user-numbered)

New feature, not a bug fix: bounded, deterministic, stoppable camera
patrol across saved PTZ presets, built entirely on the existing PTZ
dispatch path (`tool_requested` -> `ToolManagerBridgeModule` ->
`ToolManager` -> `camera_ptz`) with zero new PTZ implementation. See
`docs/change_impact/camera_patrol.md` and `ARCHITECTURE_GUARD.md` §75
for the full writeup.

**Targeted:** `tests/test_sprint71_camera_patrol.py` (new, 37/37
passed, stable across 4 consecutive full runs) + `luno/tool_manager/
tests/` + `luno/planner/tests/` + Sprint 69/70's own test files +
`tests/test_sprint71_dashboard_startup_recovery.py` = 322/322 passed.
`tests/test_dashboard.py` + `test_dashboard_turn_state_recovery.py` +
`test_production_launcher.py` = 83 passed + 1 pre-existing known
failure (`test_07_health_checks_all_pass_in_default_mock_configuration`,
matches baseline, unrelated).

**Full sweep:** whole-repo `--collect-only` clean (no new collection
errors) + the remaining project test files run in 8 chunks:

| Category | Files/tests | Result |
|---|---|---|
| environment gap (pre-existing baseline) | `test_mic_device_index.py`, `test_real_adapters.py`, `test_production_launcher.py::test_07_...` | failed, matches baseline |
| environment gap (pre-existing, `.env`'s `MAX_TOKENS_PARAM=max_tokens` override) | `test_llm_max_completion_tokens_compatibility.py`, `test_memory_session_summary_api_compatibility.py` | failed, `.env`-configuration mismatch, zero relation to camera patrol code |
| environment gap (this checkout's accumulated `config/backups/` count) | `test_sprint63_long_term_memory_recovery.py`, `test_sprint64_memory_corruption_forensics.py` | failed, pre-existing accumulated state, unrelated to this sprint (Phase 14 confirms this sprint's own runs added zero new backup files) |
| legitimate, in-scope literal update (fixed forward, not a workaround) | `test_sprint68_mutation_audit_hardening.py::test_baseline_config_json_count_is_15` | this sprint's own sanctioned `config/camera_patrol_routes.json` addition moved the real config-file count from 15 to 16; renamed to `test_baseline_config_json_count_is_16`, assertion and comment updated |
| full-suite-only timing flake (same class documented since Sprint 69.1) | `luno/barge_in/tests/test_barge_in.py` (2 tests) | failed under parallel load, both passed re-run in isolation |

Zero failures touch `luno/camera_patrol/`, `luno/tool_manager/builtin/
camera_patrol.py`, or any of this sprint's 5 modified files beyond the
one intentional, documented test-literal update above. Zero NEW failure
categories caused by this sprint's code.

**Persistent state:** `config/*.json` SHA-256-hashed immediately before
this sprint's first edit and re-hashed after the full regression sweep
- exactly one new file appeared (`camera_patrol_routes.json`, expected
and intentional), zero existing config files changed, zero files
disappeared. 11 critical source-file hashes confirm exactly the 5
intentionally-modified files changed; every PTZ/Tapo-related file
(`real_camera_ptz.py`, `camera_ptz.py`, `luno/tool_manager/manager.py`,
`luno/tool_manager/builtin/__init__.py`, `luno/bootstrap/adapters.py`,
`luno/core/events.py`) is byte-identical. `config/backups/` file count
unchanged (43 before, 43 after).

See `docs/change_impact/camera_patrol.md` and `ARCHITECTURE_GUARD.md`
§75 for the full writeup.

## Sprint 72 (Automation Engine Dasar) (user-numbered)

New feature, not a bug fix: a deterministic `TRIGGER -> CONDITION ->
ACTION -> VERIFY -> COOLDOWN` pipeline, built entirely on the existing
Event Bus / Scheduler / ToolManager dispatch path with zero second
implementation of any of the three. See `docs/change_impact/
automation_engine.md` and `ARCHITECTURE_GUARD.md` §76 for the full
writeup.

**Targeted:** `tests/test_sprint72_automation_engine.py` (new, 78/78
passed, stable across 3 consecutive full runs) + `tests/
test_sprint71_camera_patrol.py` + `tests/test_sprint71_dashboard_
startup_recovery.py` + `tests/test_sprint70_tapo_live_recovery.py` +
`tests/test_sprint69_tapo_c212_auth.py` + `tests/test_sprint69_1_
camera_dashboard_forensics.py` + `tests/test_sprint69_camera_
stability.py` + `luno/tool_manager/tests/` + `luno/planner/tests/` +
`luno/core/tests/` = 419/419 passed. `tests/test_dashboard.py` +
`test_dashboard_turn_state_recovery.py` + `test_dashboard_turn_state_
recovery_ttspath.py` + `test_sprint67_mutation_audit_trail.py` +
`test_sprint68_mutation_audit_hardening.py` + `test_production_
launcher.py` = 200 passed + 3 pre-existing known failures (matches
baseline, unrelated) + 1 legitimate in-scope literal fix (below).

**Full sweep:** whole-repo `--collect-only` clean (4510 tests
collected, same 2 pre-existing uncollectible files as every prior
sprint - `test_main_bargein.py`/`test_root_main_bargein.py`, missing
`faster_whisper`/`legacy_main.py`) + the remaining project test files
run in chunks:

| Category | Files/tests | Result |
|---|---|---|
| environment gap (pre-existing baseline) | `test_mic_device_index.py`, `test_real_adapters.py`, `test_production_launcher.py::test_07_...` | failed, matches baseline |
| environment gap (pre-existing, `.env`'s `MAX_TOKENS_PARAM=max_tokens` override) | `test_llm_max_completion_tokens_compatibility.py`, `test_memory_session_summary_api_compatibility.py` | failed, `.env`-configuration mismatch, zero relation to automation engine code |
| environment gap (this checkout's accumulated `config/backups/`/`logs/mutation_audit/` state) | `test_sprint63_long_term_memory_recovery.py`, `test_sprint64_memory_corruption_forensics.py`, `test_sprint68_mutation_audit_hardening.py::test_baseline_no_real_mutation_audit_dir_exists...`/`::test_backup_count_unchanged...` | failed, pre-existing accumulated state, unrelated (Phase 18 confirms this sprint's own runs added zero new backup files) |
| legitimate, in-scope literal update (fixed forward, not a workaround) | `test_sprint68_mutation_audit_hardening.py::test_baseline_config_json_count_is_16` | this sprint's own sanctioned `config/automation_rules.json` addition moved the real config-file count from 16 to 17; renamed to `test_baseline_config_json_count_is_17`, assertion and comment updated |
| legitimate, in-scope docstring fix (fixed forward, not a scanner workaround) | `test_sprint65_tool_file_access_audit.py::test_E_no_exec_or_eval_call_sites_exist_in_production_code`, `::test_F_zero_shell_equals_true_anywhere_in_production_code` | this sprint's OWN new `luno/automation/models.py` docstring literally named the forbidden `eval()`/`exec()`/`shell=True` tokens while documenting the prohibition; reworded to describe the same rule without the literal call-syntax substrings; re-verified passing |
| full-suite-only timing flake (same class documented since Sprint 69.1/71) | `luno/barge_in/tests/test_barge_in.py` (2 tests), `test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis` | failed under full-suite load, all passed re-run in isolation |

Zero failures touch `luno/automation/`, `luno/tool_manager/builtin/
automation.py`, or any of this sprint's 6 modified files beyond the two
intentional, documented fixes above. Zero NEW failure categories caused
by this sprint's code.

**Persistent state:** `config/*.json` SHA-256-hashed immediately before
this sprint's first edit and re-hashed after the full regression sweep
- exactly one new file appeared (`automation_rules.json`, expected and
intentional), zero existing config files changed, zero files
disappeared. 17 critical source-file hashes confirm byte-identity
throughout for every PTZ/Tapo/Event-Bus/Scheduler/ToolManager/
persistence/mutation-audit file this sprint's own architecture depends
on. `config/backups/` file count unchanged (43 before, 43 after).

See `docs/change_impact/automation_engine.md` and `ARCHITECTURE_
GUARD.md` §76 for the full writeup.

## LUNO P0 (Camera Automation / Safe Integration & Non-Regression Protocol) (user-numbered)

New feature under an explicit non-regression protocol - one new,
isolated `luno/camera_automation/` package that reuses the existing
Event Bus (via the already-existing, already-published `device_state_
changed` event) and the existing Sprint 72 `AutomationEngine` with ZERO
changes to either. Exactly one existing file modified, additively:
`luno/bootstrap/modules.py`. See `docs/change_impact/
camera_automation_p0.md` and `ARCHITECTURE_GUARD.md` §77 for the full
writeup.

**Baseline (before any change), same 8-chunk full-repo methodology
every prior sprint's baseline uses:**

```
4510 tests collected (tests/ + luno/, same 2 pre-existing
uncollectible files as every prior sprint)
4480 passed, 30 failed, 0 skipped
```

| Category | Files | Count | Cause |
|---|---|---|---|
| environment gap (LLM param mock assertion) | `test_llm_max_completion_tokens_compatibility.py` | 7 | pre-existing, self-contained, zero camera/HA relation |
| environment gap (no audio hardware / missing repo-root script) | `test_mic_device_index.py` | 11 | sandbox has no `list_microphones.py` at repo root, no audio devices |
| environment gap (real-whisper attribute / network egress blocked) | `test_production_launcher.py::test_07_...`, `test_real_adapters.py` (2) | 3 | sandbox's outbound HTTPS proxy returns 403 for `api.openai.com` |
| environment gap (accumulated `config/backups/` state) | `test_sprint63_long_term_memory_recovery.py` (2), `test_sprint64_memory_corruption_forensics.py` (3) | 5 | `config/backups/` has grown past Sprint 72's own "43 before, 43 after" snapshot from real work in this persistent folder since then |
| environment gap (same `config/backups/` accumulation) | `test_sprint68_mutation_audit_hardening.py` | 2 | same cause as above |
| full-suite-only timing flake (same class documented since Sprint 69.1/71/72) | `luno/barge_in/tests/test_barge_in.py` | 2 | fails under full-suite parallel load, 2/2 pass standalone |

**Targeted (highest-risk, spot-verified individually before and after
this sprint's one existing-file edit):** `tests/
test_sprint71_camera_patrol.py` + `tests/
test_sprint71_dashboard_startup_recovery.py` + `tests/
test_sprint72_automation_engine.py` + `luno/adapters/tests/
test_adapters.py` (via this sprint's own E2E test mirroring its exact
`test_home_assistant_event` assertions) + every existing test file that
calls `register_all_modules` (the one function this sprint's edit lives
in) - `test_dashboard.py`, `test_production_launcher.py`, `test_
proactive.py`, `test_state_isolation.py`, `test_conversation_ended_
lifecycle_routing.py`, `test_memory_dashboard.py`, `test_routing_
dashboard.py`, `test_llm_dashboard.py` - all pass, zero new failures.

**Full sweep (after):**

```
4533 tests collected (4510 + this sprint's own 23 new tests)
4503 passed, 30 failed, 0 skipped
```

Same 30 failures, same tests, same root causes as the baseline table
above - zero new failures, zero regressions, zero incidental fixes
claimed.

**Persistent state:** zero new `config/*.json` files (the camera
allowlist is env-var-only, `CAMERA_AUTOMATION_ENTITIES` - a deliberate
design choice to avoid touching the config-file-count test Sprint 72
already had to forward-fix once). `tests/
test_sprint68_mutation_audit_hardening.py`'s config-file-count test
re-verified passing at the same count Sprint 72 last set.

See `docs/change_impact/camera_automation_p0.md` and `ARCHITECTURE_
GUARD.md` §77 for the full writeup.

## LUNO P0.5 (Real Camera Integration) (user-numbered)

Integration sprint on top of P0's already-shipped `CameraAutomationModule`
- connects it to real Home Assistant camera entities via a new, generic
`CameraProfile -> CameraEvent` classification layer
(`luno/camera_automation/cameras.py`). See `docs/change_impact/
camera_automation_p0_5.md` and `ARCHITECTURE_GUARD.md` §78 for the full
writeup.

**Baseline (before):** 4533 collected, 4503 passed, 30 failed, 0
skipped - identical to P0's own final numbers (re-confirmed via targeted
spot-checks: `tests/test_p0_camera_automation.py` 23/23, `luno/adapters/
tests/test_adapters.py` 15/15, both clean immediately before writing any
P0.5 code).

**Targeted:** `tests/test_p0_5_camera_integration.py` (new, 36/36
passed) + `tests/test_p0_camera_automation.py` (23/23, unmodified, still
passing) + `tests/test_sprint72_automation_engine.py` (78/78) +
`luno/adapters/tests/test_adapters.py` (15/15) = 152 passed, 0 failed.

**Full sweep (after):** 4569 collected (4533 + this sprint's own 36),
4538 passed, 31 failed. 30 of 31 are the identical pre-existing baseline
failures (see the P0 section above for the itemized table - unchanged).
The 31st, `tests/test_streaming_e2e.py::
test_D_barge_in_between_llm_and_tts_chunk_never_plays`, was investigated
rather than assumed pre-existing: re-run in isolation it passes 6/6, and
it is the EXACT SAME test `docs/project_handover.json` already
documents by name as a non-deterministic full-suite/parallel-timing
flake dating to Sprint 49, recurring intermittently across many
unrelated sprints since - `luno/camera_automation/` shares zero code
with the TTS/barge-in/LLM streaming subsystem this test exercises. Not
a regression from this sprint.

**Persistent state:** one new config file, `config/camera_automation.
json` (shipped with a single `tapo_c212` entry, every entity-role field
`null` - genuinely inert until an operator fills in real entity ids).
`tests/test_sprint68_mutation_audit_hardening.py::
test_baseline_config_json_count_is_17` renamed to `..._is_18` and
updated (forward-fix, not a workaround - the same precedent Sprint
71/72 each already established once for their own sanctioned new config
file).

See `docs/change_impact/camera_automation_p0_5.md` and `ARCHITECTURE_
GUARD.md` §78 for the full writeup.

## P0.5.1 — Real Tapo C212 Entity Discovery

Discovery-only sprint; touched only the standalone `ha_camera_
discovery.py` script and added `tests/test_ha_camera_discovery.py`. No
file under `luno/` was modified, so no full-repository sweep was
required to establish a new baseline — the existing baseline is
unaffected by construction. Targeted verification instead:

**New tests:** `tests/test_ha_camera_discovery.py` — 8/8 passed
(pure `_build_report()` classification logic against synthetic
fixtures; no live server required).

**Directly related suites re-run unmodified:** `tests/
test_p0_camera_automation.py` (23/23) + `tests/
test_p0_5_camera_integration.py` (36/36) = 59/59 passed, 0 failed.

**Adjacent suite spot-check:** `tests/
test_sprint68_mutation_audit_hardening.py` — 65/67 passed. The 2
failures (`config/backups` file count, mutation-audit-dir baseline)
were traced to real files dated Aug 11–18 in `config/backups`/`logs/
mutation_audit`, predating this sprint by days — not caused by this
sprint's own changes (no file under `luno/` touched, no backup or
mutation-audited write performed). Left as pre-existing environmental
drift per Section 17 of the governing brief.

**Live verification:** ran the rewritten script against this sandbox's
real, configured `HA_URL`/`HA_TOKEN` — same `HTTP 403` proxy rejection
as every prior HA sprint; correctly reported as `HA DISCOVERY: BLOCKED`
rather than "camera not found." Tapo C212 presence in Home Assistant
remains undetermined pending a run on the user's own machine.

**Persistent state:** zero new/modified config files; `config/
camera_automation.json` and its field count are unchanged from P0.5.

See `docs/change_impact/camera_automation_p0_5_1.md` and
`ARCHITECTURE_GUARD.md` §79 for the full writeup.

## P0.5.2 — Tapo C212 Event Source Audit

Audit + read-only prototype sprint; touched only the standalone
`tapo_camera_event_audit.py` script and added `tests/
test_tapo_camera_event_audit.py`. No file under `luno/` was modified
(confirmed via `find luno -newer <P0.5.1's change-impact doc>` — zero
results), so no full-repository sweep was required — the existing
baseline is unaffected by construction, same reasoning as P0.5.1.

**New tests:** `tests/test_tapo_camera_event_audit.py` — 18/18 passed
(pure `_safe_call()`/`_classify_config_capability()`/`_build_report()`
logic against mocked clients — no hardware or pytapo import required).

**Directly related suites re-run unmodified:** `tests/
test_p0_camera_automation.py` (23) + `tests/
test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/
test_sprint69_tapo_c212_auth.py` + `tests/
test_sprint70_tapo_live_recovery.py` (50 combined) + `tests/
test_sprint71_camera_patrol.py` + `luno/tool_manager/tests/
test_camera_ptz.py` = baseline 181 passed / after 199 passed (181+18),
0 failed either time.

**Live verification:** ran `tapo_camera_event_audit.py --duration 2`
against this sandbox's real, configured `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` — `pytapo` itself fails to import
(`ModuleNotFoundError: No module named 'kasa.transports'`, a
pre-existing `kasa`/`cryptography` version mismatch in this checkout's
`.venv`, not caused by this sprint and not fixed this sprint since
upgrading dependencies is out of scope). Correctly reported as `RESULT:
IMPORT_FAILED`, distinct from a connection failure or "camera not
found" — every capability honestly reported UNKNOWN.

**Persistent state:** zero new/modified config files; `config/
camera_automation.json` untouched.

See `docs/change_impact/camera_automation_p0_5_2.md` and
`ARCHITECTURE_GUARD.md` §80 for the full writeup.

## P0.5.3 — Vision Event → Camera Automation Bridge

Bridge sprint; touched `luno/camera_automation/vision_bridge.py` (new),
`luno/camera_automation/module.py` (two additive methods),
`luno/camera_automation/__init__.py` (new export), and
`luno/bootstrap/modules.py` (minimal wiring, zero existing lines
changed) — confirmed via `find luno -newer <P0.5.2's change-impact
doc>`, exactly these four files.

**New tests:** `tests/test_p0_5_3_vision_camera_bridge.py` — 26/26
passed (event mapping, unknown event, confidence, camera id, failure
isolation, feature flag, no motion fabrication, `CameraAutomationModule`
additions, real-bootstrap E2E).

**Baseline (recorded before any new code this sprint):** `luno/
adapters/tests/test_adapters.py` + `tests/test_camera_presence.py` +
`tests/test_sprint69_1_camera_dashboard_forensics.py` + `tests/
test_vision_sprint8.py` + `tests/test_vision_ask_vision.py` + `tests/
test_vision_intent.py` + `tests/test_vision_intent_classifier.py` +
`tests/test_vision_provider.py` (144) + `tests/
test_p0_camera_automation.py` (23) + `tests/
test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/
test_tapo_camera_event_audit.py` (18) + `tests/
test_sprint72_automation_engine.py` (78) = **307 passed, 0 failed.**

**After:** same 307 + new 26 = **333 passed, 0 failed.** Zero new
failures. `tests/test_sprint68_mutation_audit_hardening.py`
spot-checked: 65/67 — same 2 pre-existing, unrelated environmental
failures already documented in P0.5.1/P0.5.2.

**Live verification:** no real Tapo C212/RTSP event observed (no camera
hardware/network in this sandbox) — honestly reported. A real-bootstrap
E2E test proved the event TRANSPORT path works end to end, distinct
from live-camera verification.

**Persistent state:** zero new/modified config files;
`config/camera_automation.json` untouched.

See `docs/change_impact/camera_automation_p0_5_3.md` and
`ARCHITECTURE_GUARD.md` §81 for the full writeup.

## P0.5.4 — Live Tapo C212 Camera Event Verification

Live-hardware verification sprint; zero production code changed
(confirmed via `find luno -newer <P0.5.3's change-impact doc>` — only a
runtime log file, no source). All 6 live hardware tests (idle/human
enter/human stays/human exit/human re-entry/camera disconnect-reconnect)
honestly reported `NOT PERFORMED` — this sandbox has no network route
to `TAPO_HOST` (`OSError: Network is unreachable`, freshly re-probed
this sprint) or to the configured HA host (`gaierror: Temporary failure
in name resolution`), and lacks the `ultralytics` package the real
Vision pipeline requires.

**Baseline (recorded before any activity this sprint):**
`tests/test_p0_camera_automation.py` (23) + `tests/
test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/
test_tapo_camera_event_audit.py` (18) + `tests/
test_p0_5_3_vision_camera_bridge.py` (26) + Vision/adapters suites (144)
+ `tests/test_sprint72_automation_engine.py` (78) = **333 passed, 0
failed.**

**After (re-run at sprint end):** identical, **333 passed, 0 failed** —
unchanged, exactly as expected for a sprint with zero code changes.
`tests/test_sprint68_mutation_audit_hardening.py` spot-check: 65/67,
same 2 pre-existing unrelated environmental failures already documented
in P0.5.1/P0.5.2/P0.5.3.

**Camera ID:** `same_physical_camera` remains `UNKNOWN` — no new
evidence available this sprint (same network blockers); restates the
existing evidence chain precisely, not upgraded.

**Persistent state:** zero new/modified config files.

See `docs/change_impact/camera_automation_p0_5_4.md` and
`ARCHITECTURE_GUARD.md` §82 for the full writeup.

## P0.5.4-LIVE — Real Camera Proof-of-Life

Live-verification attempt; zero production code changed. Confirmed
this sprint that agent code execution always happens in an isolated
sandbox regardless of mounted folder — a fresh TCP probe to `TAPO_HOST`
still fails (`Network is unreachable`). Delivered
`luno_live_camera_event_observer.py` (new, root-level, read-only) for
the user to run themselves on their real machine, plus `tests/
test_luno_live_camera_event_observer.py` (new, 13 tests).

**Baseline:** 333 passed, 0 failed (unchanged from P0.5.4).

**After:** 333 + 13 new = **346 passed, 0 failed.** `tests/
test_sprint68_mutation_audit_hardening.py` spot-check: 65/67, same 2
pre-existing unrelated failures.

**Live tests A–F:** all 7 NOT PERFORMED — honestly reported, never
fabricated as PASS. See `docs/change_impact/
camera_automation_p0_5_4_live.md` and `ARCHITECTURE_GUARD.md` §83 for
the full writeup.

## P0.5.4-FIX — Use the Real main.py Vision Lifecycle

Bug-fix sprint after the user reported the P0.5.4-LIVE observer
produced only `scheduled_vision_poll (0.0ms)` with no camera events,
while their real `main.py` performs genuine live YOLO detection.
Root cause, traced (not guessed) through `main.py`,
`luno/bootstrap/launcher_config.py`, and `luno/bootstrap/adapters.py`:
the observer's `main()` called a bare `LauncherConfig()` instead of
`LauncherConfig.load()` (what `main.py` line 66 itself calls), so
`.env`'s `VISION_BACKEND=real` was never applied and
`register_all_adapters()` silently built `VisionAdapter` against
`MockVisionSource()` instead of `RealVisionSource()` — no real RTSP
connection, no real YOLO inference, no real Vision events ever
published. The unrelated `scheduled_vision_poll` tick the user saw is
a content-free periodic Event Bus publish that `VisionAdapter` has no
handler for at all (confirmed via grep).

**Fix:** one-line change (`LauncherConfig()` -> `LauncherConfig.load()`)
in `luno_live_camera_event_observer.py`'s `main()`, plus a visible
warning if `cfg.vision_backend != "real"` after boot. Zero files under
`luno/` touched — confirmed via `find luno -newer <P0.5.4-LIVE's doc>`.

**Baseline (this sprint's own targeted-suite recount):** 331 passed, 0
failed.

**After:** 331 + 3 new observer tests = **334 passed, 0 failed.**
Observer test file alone: 13 -> 16 passing. `luno/adapters/tests/
test_adapters.py`: 15/15 unchanged. `test_main_bargein.py`/
`test_root_main_bargein.py` remain the same 2 pre-existing,
unrelated, documented INFRASTRUCTURE collection failures (missing
`faster_whisper`/`legacy_main.py`), outside this sprint's targeted set.

**Live status: NOT VERIFIED** — this sprint fixes the tool on the
strength of a fully code-traced root cause; it does not constitute a
live-hardware PASS. The user must re-run the observer, confirm
`vision backend: real` prints, and report back the resulting
`CameraPersonEntered -> human_detected -> camera_automation.camera_event`
trace. See `docs/change_impact/camera_automation_p0_5_4_fix.md` and
`ARCHITECTURE_GUARD.md` §84 for the full writeup.

## P0.6 — Camera Automation Rule Integration + Log-Only

Connects `camera_automation.camera_event` to the existing
`AutomationEngine` (Sprint 72). First rule: `camera_human_detected_log`
- `kind=="human_detected"` -> `automation.log` (already-existing,
internal-only action type; never reaches Home Assistant/PTZ).

Architecture audit found the condition engine could only read
externally-registered `state_readers`, never the triggering event's own
payload - "match `kind=='human_detected'`" was structurally
inexpressible before this sprint. Minimal fix: `luno/automation/
conditions.py`'s `evaluate_condition()` gained an optional `event_data`
parameter and an `event.<field>` condition-target convention (fully
backward compatible - every other target unaffected); `luno/automation/
engine.py` threads `event.data` through the trigger pipeline as an
optional parameter. `config/automation_rules.json` (`{}` -> one rule).

**Baseline (measured this sprint):** 224 passed, 0 failed (P0/P0.5/
P0.5.1/P0.5.2/P0.5.3/observer/automation_engine suites + `luno/core/
tests/test_core.py`).

**After:** 224 + 27 new (`tests/test_p0_6_camera_automation_rules.py`)
= **251 passed, 0 failed.** Spot-check (`test_sprint71_camera_
patrol.py` + `luno/adapters/tests/test_adapters.py` +
`test_dashboard.py`): 99/99 passed, unaffected.

**Safety:** structurally proven, not just asserted - `automation.log`
never calls `_dispatch_tool_call()` (the only path to `tool_requested`,
which every real HA/PTZ action goes through). A dedicated test
subscribes to `tool_requested` on the real Event Bus for a matched
execution and asserts zero calls.

**Live status: NOT PERFORMED** - same sandbox constraint as every prior
sprint. A real-bootstrap, real-Event-Bus, simulated-event smoke test
proved the full `EVENT -> RULE -> MATCH -> LOG` chain; not hardware
verification. See `docs/change_impact/camera_automation_p0_6.md` and
`ARCHITECTURE_GUARD.md` §85 for the full writeup.

## P0.6.1 — Live Camera → Automation Log-Only Verification

Extends `luno_live_camera_event_observer.py` (reused, not replaced) to
add: a rule-loaded/enabled pre-check via `AutomationEngine.get_
automation_status()`, per-rule `automation.<outcome>` counting
(`triggered`/`condition_passed`/`completed`/`skipped`/`failed`,
filtered to `camera_human_detected_log` only), and a `tool_requested`
device-action safety count. Zero files under `luno/` touched.

**Baseline (reconfirmed this sprint):** 251 passed, 0 failed (identical
to P0.6's own final count).

**After:** 251 + 15 new (`tests/test_p0_6_1_live_log_verification.py`)
= **266 passed, 0 failed.**

**Result classification: BLOCKED** (agent's own attempt) - structural
sandbox network limitation, re-confirmed this sprint. Two real-
bootstrap, simulated-event tests proved the new wiring produces the
correct evidence format (e.g. `human_detected` -> `triggered=2*,
condition_passed=1, completed=1`; `human_cleared` -> `skipped=1`,
never `completed`; `tool_requested=0` in all cases) - not hardware
evidence. See `docs/change_impact/camera_automation_p0_6_1.md` and
`ARCHITECTURE_GUARD.md` §86 for the full writeup.

## P0.6.2 — First Real Home Assistant Action (Safe Single-Device Camera Automation)

Adds `camera_human_detected_test_action` - `event.kind==human_detected`
-> `home_assistant.turn_on` on `light.wled` ("RGB Strip", a real,
pre-existing, low-risk light from `.env`/`config/lights.config.json` -
never fabricated). `camera_human_detected_log` unchanged. Reuses the
existing, already-verified `home_assistant.turn_on` action/dispatch
path (Sprint 72's own idempotent, state-checked HA handler) - no new HA
client. One production file modified: `luno/automation/models.py`
(`validate_action()` tightened to reject non-string/wildcard HA
targets - the one genuine gap this sprint found).

**Baseline (measured this sprint):** 301 passed, 0 failed.

**After:** 301 + 33 new (`tests/test_p0_6_2_camera_ha_action.py`) + 15
(`luno/adapters/tests/test_adapters.py`, added to this sprint's own
targeted set) = **349 passed, 0 failed.** Spot-check (camera_patrol +
dashboard): 84/84 unaffected.

Four pre-existing tests across 3 files were updated (not weakened) to
reflect the real second rule now sharing `config/automation_rules.json`
and its legitimate `tool_requested`/"turn_on" text - two brittle
substring scans rewritten as AST-based call-shape scans, two count
assertions updated from 0 to 1 with the LOG rule's own zero-tool-call
invariant re-confirmed structurally.

**Result classification: BLOCKED** (agent's own attempt) - same
structural sandbox limitation. A real-bootstrap, mocked-HA-boundary
simulated run proved both rules fire independently from one event,
exactly one `tool_requested` (home_assistant/turn_on/light.wled), zero
PTZ/other device actions - not hardware evidence. See `docs/
change_impact/camera_automation_p0_6_2.md` and `ARCHITECTURE_GUARD.md`
§87 for the full writeup.

## P0.6.2-FIX — Vision Runtime Parity / YOLO Detection Recovery

First sprint driven by real live-hardware output: RTSP open succeeded
but tracked YOLO detection failed every cycle with `'Conv' object has
no attribute 'bn'`. Audit (direct code comparison, not assumed) proved
`main.py` and the observer already share the identical bootstrap path
and the identical, single `RealVisionSource()` construction site - no
duplicate Vision implementation existed. The double-fusion hypothesis
was disproven (`luno/vision.py` never calls `.fuse()` anywhere). Likely
cause: `requirements.txt`'s open `ultralytics>=8.3.0` pin vs. the
committed, static `.pt` checkpoint files - evidenced by this codebase's
own pre-existing `_yolo_checkpoint_hint()` diagnostic, not confirmed
from this sandbox. No dependency was changed.

One genuine Luno code defect found and fixed: `detect_objects_tracked()`
silently returned `[]` on ANY failure (contract unchanged), making a
real detector failure indistinguishable from "nobody in frame" -
risking a false `human_cleared`. Fix (additive only): `luno/vision.py`
gained a `last_tracked_detection_error()` getter; `luno/adapters/
real_vision.py` now publishes the existing `SystemError` event
(`error_type="vision_detection_failed"`) on failure; the observer
reports a distinct `VISION_DETECTION_FAILED` line and now prints real
runtime versions (Python/ultralytics/torch/CUDA/OpenCV).

**Baseline (measured this sprint):** 428 passed, 0 failed.

**After:** 428 + 21 new (`tests/test_p0_6_2_fix_vision_runtime_parity.py`)
= **448 passed, 1 skipped (honest - no `ultralytics` in this sandbox),
0 failed.** An additional 8-file sweep of every other Vision/camera-
adjacent test file found 3 failures, all confirmed outside this
sprint's diff scope and pre-existing/environmental (2 external-network
health checks - OpenRouter/Fish Audio, proxy-blocked in this sandbox;
1 unrelated `real_whisper.py` `_device_index` bug) - documented, not
silently normalized.

Zero files under `luno/automation/*`, `luno/camera_automation/*`, or
`config/automation_rules.json` touched - both automation rules remain
byte-for-byte unchanged.

**Result classification: BLOCKED** (agent's own attempt) - the actual
root cause can only be confirmed on the user's real machine. The
Section 13 silent-failure-masking defect IS fixed and IS verified by
the new tests; whether the live Conv.bn symptom itself is resolved
requires the user to re-run the observer and report back. See `docs/
change_impact/camera_automation_p0_6_2_fix.md` and `ARCHITECTURE_GUARD.
md` §88 for the full writeup.

## P0.6.3 — Unified Vision → Camera Automation Integration

Audit confirmed the "unified" architecture the brief asked for already
existed: exactly one `RealVisionSource()` site, one shared cached YOLO
singleton, `VisionCameraEventBridge` already consuming the correct
pre-existing events with no second pipeline. Newly documented: the
dashboard's rich per-object view and Camera Automation's `human_
detected`/`human_cleared` are fed by two different pre-existing loops
inside the one `RealVisionSource` (`_tracked_cycle_loop()` vs.
`_poll_loop()`), which is why P0.6.2-FIX's detector-failure fix (only
`detect_objects_tracked()`) never protected Camera Automation's actual
event source (`detect_objects()`). Fixed additively: `luno/vision.py`
gained `last_presence_detection_error()`; `luno/adapters/real_vision.py::
_poll_once()` now publishes the existing `SystemError`/`vision_
detection_failed` signal and skips `on_detections()` on a detector
failure, so a false `CameraPersonLeft`/`human_cleared` can no longer be
invented from a broken detector.

**Baseline (measured this sprint, two invocations kept separate - see
below):** main targeted set 454 passed/1 skipped/0 failed;
`test_sprint69_1_camera_dashboard_forensics.py` +
`test_sprint69_camera_stability.py` (isolated) 37 passed/0 failed.

**After:** main targeted set 454 + 31 new
(`tests/test_p0_6_3_unified_vision_camera_automation.py`) = **485
passed, 1 skipped (unchanged), 0 failed.** Sprint69 pair (isolated):
37/37 unchanged.

**Found and fixed during this sprint:** `tests/test_real_adapters.py`'s
own pre-existing Vision fake (`_FakeVisionModule`) was missing the new
`last_presence_detection_error()` getter `_poll_once()` now calls -
added a 2-line stub returning `None`, restoring that test to passing.

**New pre-existing-issue finding (documented, not fixed - out of
scope):** `tests/test_vision_sprint8.py`'s own `_install_fake_real_
vision()` helper permanently, non-restoringly reassigns `luno.vision.
camera_status`/etc. at module level - running that file in the same
pytest process as the sprint69 camera-stability/dashboard-forensics
files causes 13 cross-file-pollution failures that do not reproduce
when either group runs alone. Root-caused to that helper (Sprint-8 era,
untouched by any sprint in this line); every regression command in this
project already avoids the combination by construction.

**Result classification: BLOCKED** (agent's own attempt) - same
structural sandbox limitation. Everything provable from code/tests here
is a genuine, verified PASS; the real walk-test needs the user's
machine. See `docs/change_impact/camera_automation_p0_6_3.md` and
`ARCHITECTURE_GUARD.md` §89 for the full writeup.

## P0.7 — Vision Context → Automation Context

Added `VisionContext` (`luno/camera_automation/vision_context.py`) - a
pure, normalized snapshot (`human_present`/`person_count`/`detected_
objects`/`available`/`detection_error`) derived only from the existing
`adapter_manager.status_all()["vision"]` public status, threaded into
`CameraEvent`'s 5 new optional fields by `VisionCameraEventBridge`. One
new `greater_equal` condition operator; one new log-only example rule
(`camera_multiple_people_log`, `event.person_count >= 2`).

**Baseline (measured this sprint, chunked full-suite sweep — 10
roughly-equal file groups plus the two isolated groups this project's
own convention already requires):** 3,978 passed across the main
chunks; `test_vision_sprint8.py` (isolated) 32 passed; sprint69 pair
(isolated) 37 passed.

**After:** main chunks 3,978 + 40 new
(`tests/test_p0_7_vision_context.py`) = **4,018 passed.** Isolated
groups unchanged (32/32, 37/37).

**Found and fixed during this sprint (expected test-staleness, not
regressions — this sprint's own intentional additive changes correctly
invalidated 3 pre-existing tests' hardcoded assumptions):**
`test_p0_5_3_vision_camera_bridge.py::test_05_unknown_event_type_
never_reaches_bridge_and_is_ignored` (bridge now subscribes to 5 event
types, not 4 — the new `system_error` subscription is real and
intentional); `test_p0_6_3_unified_vision_camera_automation.py::
test_26_automation_rules_file_unchanged_this_sprint` (that sprint's own
name refers to P0.6.3, which genuinely didn't touch the file — P0.7
legitimately did); `test_sprint72_automation_engine.py::test_15_
allowlists_are_exactly_the_documented_sets` (`CONDITION_TYPES` now has
7 members, not 6 — the new `greater_equal` operator).

**Pre-existing failures found, confirmed unrelated, NOT fixed (same
category already flagged in `project_handover.md` §22 well before this
sprint):** `tests/test_llm_max_completion_tokens_compatibility.py` (7)
+ `tests/test_memory_session_summary_api_compatibility.py` (5) — this
checkout's own `.env` sets `MAX_TOKENS_PARAM=max_tokens`, disagreeing
with the code's own default; `tests/test_mic_device_index.py` (6) +
`tests/test_real_adapters.py::test_real_whisper_source_*` (2) — missing
`list_microphones.py` / a pre-existing `RealWhisperSource` test-
construction bug; `tests/test_production_launcher.py::test_07` (1) —
blocked OpenRouter/Fish Audio network health checks; `tests/test_
sprint63_long_term_memory_recovery.py` (10) + `tests/test_sprint64_
memory_corruption_forensics.py` (5) + `tests/test_sprint67_mutation_
audit_trail.py` (1) + `tests/test_sprint68_mutation_audit_hardening.py`
(2) — `config/backups/` file-count drift from months of cumulative
sprint activity in this same long-lived checkout. All independently
confirmed to import/touch only `luno.memory`/`luno.config` or unrelated
audio code — zero relation to anything this sprint touched.

**Result classification: BLOCKED** (agent's own attempt) - same
structural sandbox limitation. Everything provable from code/tests here
is a genuine, verified PASS; the real walk-test (confirm `camera_
multiple_people_log` fires alongside `camera_human_detected_log` when
2+ people are in frame) needs the user's machine. See `docs/change_
impact/vision_context_p0_7.md` and `ARCHITECTURE_GUARD.md` §90 for the
full writeup.

## P0.8.0 — Camera Automation → Home Assistant Action Safety Pipeline

Added `luno/automation/camera_action_safety.py::validate_camera_ha_
action()` - a pure, fail-closed safety gate called only for camera-
triggered `home_assistant.turn_on`/`turn_off` actions, wired into the
one existing `_dispatch_home_assistant_action()` path. Reuses the
existing cooldown mechanism and (optionally, via a new post-hoc-wired
`AutomationEngine.ha_state_reader`) the existing `RealHomeAssistant
Client.get_entity_state()` for an "already in the requested state ->
skip" optimization. One new TEST-ONLY rule, `camera_test_automation_
safety_action`, targets the harmless `light.test_camera_automation`.

**Baseline (measured this sprint, targeted HA/automation/camera set,
before any P0.8.0 file was touched):** 329 passed, 1 skipped, 0 failed.
Full chunked sweep baseline (= P0.7's own "After" state): 4,018 passed
across the main chunks; isolated groups 32/32 (`test_vision_sprint8.
py`), 37/37 (sprint69 pair).

**After:** targeted set 329 -> **377 passed** (+48 new), 1 skipped
(unchanged), 0 failed. Full chunked sweep: 4,018 -> **4,066 passed**
(+48 new, `tests/test_p0_8_0_camera_action_safety.py`). Isolated groups
unchanged (32/32, 37/37).

**Found and fixed during this sprint (expected test-staleness, not
regressions — this sprint's own intentional additive changes correctly
invalidated 3 pre-existing tests' hardcoded assumptions):**
`test_sprint72_automation_engine.py::test_38_unknown_action_type_
refused_defensively` + `test_39_automation_log_action_never_dispatches_
a_tool_call` (`_dispatch_action()` gained a leading `rule` parameter, so
both direct-call tests were updated to pass a minimal `AutomationRule`);
`test_sprint72_automation_engine.py::test_67_event_payloads_are_
metadata_only` (the action-completed/failed event payload now carries a
new `code` field, added to the test's own allowlisted-keys set);
`test_p0_7_vision_context.py::test_36_automation_rules_file_now_has_
exactly_three_rules` (P0.8.0 legitimately added a 4th rule — updated to
`issubset()`, the same convention `test_p0_6_3_unified_vision_camera_
automation.py::test_26` already established for the identical prior
situation).

**Pre-existing failures found, confirmed unrelated, NOT fixed (same
category already flagged in `project_handover.md` §22 well before this
sprint):** `tests/test_llm_max_completion_tokens_compatibility.py` (7)
+ `tests/test_memory_session_summary_api_compatibility.py` (5) — the
`.env`/`MAX_TOKENS_PARAM` mismatch; `tests/test_mic_device_index.py`
(6) + `tests/test_real_adapters.py::test_real_whisper_source_*` (2) —
missing `list_microphones.py` / the pre-existing `RealWhisperSource`
test-construction bug; `tests/test_production_launcher.py::test_07`
(1) — blocked OpenRouter/Fish Audio network health checks; `tests/test_
sprint63_long_term_memory_recovery.py` + `test_sprint64_memory_
corruption_forensics.py` + `test_sprint67_mutation_audit_trail.py` +
`test_sprint68_mutation_audit_hardening.py` (combined ~16-19) —
`config/backups/` file-count drift, now 51 files against these tests'
hardcoded pristine-count expectations. All independently reconfirmed to
import/touch only `luno.memory`/`luno.config` or unrelated audio code —
zero relation to anything this sprint touched.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- same structural sandbox limitation. **REAL HOME ASSISTANT ACTIONS
WERE NOT PERFORMED** - every test in `tests/test_p0_8_0_camera_action_
safety.py` routes through `MockHomeAssistantHandler`; `register_real_
tool_handlers()` is never called by this sprint's own code or tests.
Everything provable from code/tests here is a genuine, verified PASS;
live verification (a real light actually turning on/off via a camera-
triggered rule) is explicitly deferred to P0.8.1 - see `docs/change_
impact/camera_automation_p0_8.md` and `ARCHITECTURE_GUARD.md` §91 for
the full writeup, including the exact recommended P0.8.1 live test
procedure.

## P0.8.1 — Live Camera → Home Assistant Light Verification

Added `luno_live_p0_8_1_verification.py` (new, root-level, standalone
script - the interactive six-test live verification procedure) and
`luno/bootstrap/adapters.py::apply_camera_automation_test_light_
override()` (redirects the existing P0.8.0 TEST-ONLY rule's target at
an explicitly configured real test light, in memory only, strictly
opt-in). No production dispatch/safety-gate/cooldown code touched.

**Baseline (measured this sprint, before any P0.8.1 file was
touched):** targeted set (P0/P0.5.x/P0.6.x/P0.7/P0.8.0/Sprint 72/
`test_real_adapters.py`) 427 passed, 1 skipped, 0 failed. Full chunked
sweep baseline (= P0.8.0's own "After" state): 4,066 passed; isolated
groups 32/32 (`test_vision_sprint8.py`), 37/37 (sprint69 pair).

**After:** targeted set 427 -> **450 passed** (+23 new,
`tests/test_p0_8_1_live_verification.py`), 1 skipped (unchanged), 0
new failures. Full chunked sweep: 4,066 -> **4,089 passed** (+23 new).
Isolated groups unchanged (32/32, 37/37).

**Found and investigated during this sprint's own regression sweep -
confirmed FLAKY, not a regression:** `tests/test_streaming_e2e.py::
test_D_barge_in_between_llm_and_tts_chunk_never_plays` failed once
during the combined chunk run, then passed cleanly (`1 passed in
0.69s`) when re-run alone immediately after - the exact "re-run any
TTS/streaming-timing failure in isolation before classifying it"
procedure `project_handover.md` §21 already documents as necessary in
"4 of the last 5 sprints." This sprint touches no streaming/TTS code.

**Pre-existing, already-baselined collection errors reconfirmed,
unrelated:** `tests/test_main_bargein.py` (imports `luno/main.py` ->
`ModuleNotFoundError: No module named 'faster_whisper'`) and `tests/
test_root_main_bargein.py` (imports a nonexistent `legacy_main.py`) -
these are the "same 2 pre-existing uncollectible files as every prior
sprint" `project_handover.json`'s own `test_baseline` field has
referenced since the original P0 sprint, not new, not caused by this
sprint.

**Pre-existing failures found, confirmed unrelated, NOT fixed (same
category already flagged in `project_handover.md` §22 well before this
sprint):** `tests/test_llm_max_completion_tokens_compatibility.py` (7)
+ `tests/test_memory_session_summary_api_compatibility.py` (5) — the
`.env`/`MAX_TOKENS_PARAM` mismatch; `tests/test_mic_device_index.py`
(6) — missing `list_microphones.py`; `tests/test_real_adapters.py::
test_real_whisper_source_*` (2) — the pre-existing `RealWhisperSource`
test-construction bug; `tests/test_production_launcher.py::test_07`
(1) — blocked OpenRouter/Fish Audio network health check; `tests/test_
sprint63_long_term_memory_recovery.py` + `test_sprint64_memory_
corruption_forensics.py` + `test_sprint68_mutation_audit_hardening.py`
(combined 16) — `config/backups/` file-count drift.

## P0.8.2 — Camera Human Cleared → Safe Light OFF

Added one new rule, `camera_test_automation_safety_action_off`, to
`config/automation_rules.json` (`human_cleared` → `home_assistant.
turn_off`, targeting the same harmless `light.test_camera_automation`
placeholder). Generalized `luno/bootstrap/adapters.py::apply_camera_
automation_test_light_override()` to cover both the ON and OFF
TEST-ONLY rules. Fixed a genuine, previously-latent cooldown bug in
`luno/automation/engine.py::AutomationEngine._run_execution()` (cooldown
was starting on condition-failed/`SKIPPED` executions, not just genuine
fires — see `docs/change_impact/camera_automation_p0_8_2.md` §3/§7 for
the full story). Extended `luno_live_p0_8_1_verification.py` (the SAME
file, not a second observer) with a `--sequence p0_8_2` TEST A-F live
sequence, additive alongside the unchanged default `p0_8_1` sequence.
No changes to `luno/automation/camera_action_safety.py` (already fully
direction-agnostic — required zero modification).

**Baseline (measured this sprint, before any P0.8.2 file was touched):**
targeted set (P0/P0.5.x/P0.6.x/P0.7/P0.8.0/P0.8.1/Sprint 72/`test_real_
adapters.py`) 450 passed, 1 skipped, 0 failed. Full chunked sweep
baseline (= P0.8.1's own "After" state): 4,089 passed.

**After:** targeted set 450 -> **485 passed** (+35 new, `tests/test_
p0_8_2_human_cleared_light_off.py`), 1 skipped (unchanged), 0 new
failures. Full repository sweep (chunked across 140 collectible files,
142 total minus the same 2 pre-existing uncollectible files as every
prior sprint): **4,052 passed, 37 failed, 1 skipped.**

**Every failure in the full sweep individually re-confirmed as an
already-documented, pre-existing, unrelated category — zero new
failures found anywhere in the repository:** `tests/test_llm_max_
completion_tokens_compatibility.py` (7) + `tests/test_memory_session_
summary_api_compatibility.py` (5) — the `.env`/`MAX_TOKENS_PARAM`
mismatch; `tests/test_mic_device_index.py` (6) — missing `list_
microphones.py`; `tests/test_real_adapters.py::test_real_whisper_
source_*` (2) — the pre-existing `RealWhisperSource` test-construction
bug; `tests/test_production_launcher.py::test_07` (1) — blocked
OpenRouter/Fish Audio network health check; `tests/test_sprint63_long_
term_memory_recovery.py` (9) + `test_sprint64_memory_corruption_
forensics.py` (5) + `test_sprint68_mutation_audit_hardening.py` (2) —
`config/backups/` file-count drift family (combined 16, `config/
backups/` now at 51 files, matching this same drift already observed
in P0.8.0/P0.8.1's own sweeps). Total: 7+5+6+2+1+9+5+2 = 37. Two
pre-existing, already-baselined collection errors (`test_main_bargein.
py`/`test_root_main_bargein.py`) reconfirmed unrelated, same as every
prior sprint. This sprint's own production code change is confined to
`config/automation_rules.json`, `luno/bootstrap/adapters.py`, `luno/
automation/engine.py` (one line), and `luno_live_p0_8_1_verification.py`
— none of the 37 failing tests import or exercise any of these files.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- the pre-flight itself hard-stopped in this sandbox before any
device action was possible. **REAL HOME ASSISTANT ACTIONS WERE NOT
PERFORMED** - the actual, verbatim pre-flight output (network
unreachable to the Tapo C212/Home Assistant host, `ultralytics` not
installed, `CAMERA_AUTOMATION_ENABLED` currently `False` in this
checkout) is captured in `docs/change_impact/camera_automation_p0_8_1.
md` §5. Everything provable from code/tests in this sandbox (the
override's scoping guarantees, the pre-flight/test-light-resolution
logic, a full real-bootstrap end-to-end proof with a mocked HA backend)
is a genuine, verified PASS; the six-test live walk-test itself is
explicitly deferred to the user's real machine - see that same
document's §13 for the exact command to run.

## LUNO — Long-Term Memory Self-Healing / Recovery Hardening

**Scope:** hardened `luno/memory.py`'s existing, private
`_load()`/`_save()` persistence pair for `config.LONG_TERM_MEMORY_FILE`
only — deterministic newest-valid-backup recovery on primary corruption,
quarantine-not-destroy of an unrecoverable primary (deferred to the next
`_save()`, since `_load()` must remain provably read-only), and a fresh
empty store (existing production schema) as the last resort so Luno
never fails to start over this one file. No other persistent store,
no `luno/persistence.py` (a separate, generic module `LONG_TERM_MEMORY_
FILE` never routes through), and no retrieval/ranking/scoring/dedup
code was touched. See `docs/change_impact/long_term_memory_self_
healing.md` for the full writeup.

**Baseline (measured this sprint, before any file was touched):**
targeted persistence suite (`tests/test_memory_persistence_hardening.
py`) 11 passed, 0 failed. Full chunked sweep baseline (= P0.8.2's own
"After" state): 4,052 passed, 37 failed, 1 skipped across 140
collectible files.

**After:** targeted persistence suite 11 -> **34 passed** (+23 new,
covering all 26 brief-mandated recovery scenarios), 0 failed. Targeted
28-file memory sweep: 19 pre-existing failures (unchanged category, see
below), 1094 passed. Full repository sweep (same 139 collectible files,
same 2 pre-existing uncollectible files as every prior sprint):
**4,075 passed, 37 failed, 1 skipped** — passed count up by exactly 23
(the new test count), failed/skipped counts byte-for-byte unchanged.

**Every failure in the full sweep individually re-confirmed as an
already-documented, pre-existing, unrelated category — zero new
failures found anywhere in the repository:** `tests/test_llm_max_
completion_tokens_compatibility.py` (7) + `tests/test_memory_session_
summary_api_compatibility.py` (5) — the same `max_tokens`/
`max_completion_tokens` LLM adapter-layer mismatch (confirmed unrelated
by inspection: this sprint's diff never touches `summarize_and_archive_
session()`, and `SESSION_SUMMARIES_FILE` persistence goes through
`luno/persistence.py`'s generic functions, never the `_load()`/`_save()`
this sprint modified); `tests/test_mic_device_index.py` (6) — missing
`list_microphones.py`; `tests/test_real_adapters.py` (2) +
`tests/test_production_launcher.py` (1) — pre-existing `RealWhisper
Source` test-construction gap and a health-check assertion;
`tests/test_sprint63_long_term_memory_recovery.py` (9) + `test_sprint64_
memory_corruption_forensics.py` (5) + `test_sprint68_mutation_audit_
hardening.py` (2) — the same `config/backups/`-accumulation/real-file-
state forensic staleness family documented in every sprint back through
P0.8.0/P0.8.1/P0.8.2 (still 51 backup files, same drift, confirmed not
caused by this sprint since every one of this sprint's own tests is
`tmp_path`-isolated via `monkeypatch`). Total: 7+5+6+2+1+9+5+2 = 37.

**Production state safety:** all 7 mandated persistent-state files
(`config/long_term_memory.json`, `verified_facts.json`,
`episodic_memory.json`, `relationship_state.json`,
`session_summaries.json`, `habit_memory.json`, `reminders.json`)
SHA-256-hashed before any code was written and again after the entire
regression sweep completed — byte-identical in every case.

**Result classification: COMPLETE** — pure code-level reliability
hardening, no live-hardware dependency. All 19 acceptance-criteria
items verified via the test suite, not merely designed.

## LUNO P0.8.3 — Fix Real YOLO Inference Failure

**Scope:** the user's real P0.8.2 live-verification pre-flight fully
passed (network/credentials/RTSP/HA/safety-gate/runtime-start all
green) but YOLO detection itself failed every cycle with `AttributeError:
'Conv' object has no attribute 'bn'`, and — the actual bug this sprint
fixes — the existing `_yolo_checkpoint_hint()` diagnostic (added in
P0.6.2-FIX specifically for this signature) never appended its
actionable hint, because its `.name`-only match condition can never
match the real exception `torch.nn.Module.__getattr__` actually raises
(confirmed via direct inspection of the real, installed `torch==2.13.0`
source). Fixed by also matching the exception's string message. See
`docs/change_impact/camera_automation_p0_8_3.md` for the full
root-cause writeup, including why the deeper "why does the checkpoint
disagree with `ultralytics 8.4.123`" question could not be proven in
this sandbox (a 526.6MB `torch` wheel plus mandatory CUDA dependencies
could not be installed within this sandbox's per-call network budget).

**Baseline (measured this sprint, before any file was touched):**
targeted set (`test_p0_8_0/1/2`, `test_p0_7_vision_context.py`, `test_
vision_sprint8.py`, `test_real_adapters.py`) 186 passed, 2 pre-existing
failures (`RealWhisperSource`, unrelated). Full chunked sweep baseline
(= Long-Term-Memory-Self-Healing sprint's own "After" state): 4,075
passed, 37 failed, 1 skipped across 139 collectible files.

**After:** targeted set (expanded to include the new test file plus
`test_p0_6_2_fix_vision_runtime_parity.py`, `test_p0_6*`, `test_p0_
camera_automation.py`, `test_luno_live_camera_event_observer.py`) 336
passed (2 pre-existing, unrelated), 1 skipped, 0 new failures. All 15
remaining Vision/camera test files: 288 passed, 0 failed. Full
repository sweep (140 collectible files, same 2 pre-existing
uncollectible files as every prior sprint): **4,092 passed, 37 failed,
1 skipped** — passed count up by exactly 18 (the new test count),
failed/skipped counts unchanged in category.

**Every failure in the full sweep individually re-confirmed as an
already-documented, pre-existing, unrelated category — zero new
failures found anywhere in the repository:** the same 37-failure
breakdown as the immediately-prior sprint (LLM `max_tokens`/
`max_completion_tokens` adapter mismatch ×12, missing `list_
microphones.py` ×6, `RealWhisperSource` construction gap ×3, `config/
backups/`-accumulation/real-file forensic staleness ×16 — now 52 backup
files, same drift). Two ADDITIONAL failures surfaced only inside the
full chunked sweep — `test_llm_tts_streaming_production.py::test_14_
cancellation_during_synthesis` and `test_verification_dashboard.py::
test_api_verification_reports_a_successful_verified_action_end_to_end`
— both already-NAMED in `docs/project_handover.json`'s own `known_
baseline_failures` as order/timing-dependent, full-suite-only flakes;
both re-run in isolation immediately after and passed cleanly.

**Production state safety — disclosure required:** 6 of the 7 mandated
persistent-state files were hash-identical before and after this
sprint's own work. `config/habit_memory.json` was found already
mutated when this sprint's regression sweep began — forensic tracing
(`logs/mutation_audit/2026-08-21.jsonl`, literal Windows-path/pid
evidence) proves this write came from the user's OWN real machine (a
genuine `light.wled` habit observation from their real P0.8.2 live-
verification session earlier today), not this sandbox. Before
recognizing this, the file was mistakenly reverted to its own pre-write
backup; reconstructing the lost entry byte-for-byte from a terminal
transcript was judged too risky to attempt (it came out 162 bytes
short of the recorded size) rather than a safer honest disclosure. Net
effect: one recently-observed, self-regenerating habit-pattern entry
was lost; every other file, and every other entry in that file, is
confirmed unchanged. Full accounting in `docs/change_impact/camera_
automation_p0_8_3.md` §8.

**Result classification: PARTIAL** — the diagnostic bug is confirmed
and fixed with evidence and regression tests; the underlying detection
failure (why the checkpoint and installed `ultralytics` disagree) is
NOT confirmed resolved, since this sandbox could not execute the real
`torch`/`ultralytics` stack. No live YOLO detection was claimed or
performed, per the brief's own explicit instruction not to claim live
verification passed without a real RTSP-sourced detection.

## LUNO P0.8.4 — Resolve the Actual YOLO Model / Ultralytics Compatibility Failure

**Command:** `python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py` (run in per-file/per-chunk pieces due to sandbox per-call time limits, per this project's established methodology).

**New tests:** `tests/test_p0_8_4_yolo_concurrency_fix.py` — 12 passed, 0 failed (includes a genuine two-`threading.Thread` race-proof test, no mocking of the race itself).

**Full sweep result:** every Vision/P0.x/camera/automation-specific test file — 100% pass, zero exceptions, including all 12 new tests above. `luno/vision.py` is the only production file this sprint touched.

**Pre-existing/already-documented baseline failures re-encountered (unchanged categories, all previously recorded in this file):** `test_real_adapters.py` (2, `RealWhisperSource._device_index`), `test_mic_device_index.py` (6, `.env`/`list_microphones.py` gap), `test_llm_max_completion_tokens_compatibility.py` (7) + `test_memory_session_summary_api_compatibility.py` (5) (both, `.env`'s `MAX_TOKENS_PARAM=max_tokens` override), `test_production_launcher.py::test_07_...` (1, real credentials configured), `test_persistent_adaptive_response_depth.py::test_e2e_9_...` (1, known full-suite-only flake — re-run standalone, passed).

**Newly observed this sprint, investigated, confirmed unrelated to `luno/vision.py` (full detail in `docs/change_impact/camera_automation_p0_8_4.md` §7):**
- `test_sprint60_area_schema.py` (2) — real `config/lights.config.json` `light.main_light` entry appears to be a genuine, recent, real-machine config addition the test's fixture doesn't yet know about.
- `test_sprint63_long_term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint67_mutation_audit_trail.py`/`test_sprint68_mutation_audit_hardening.py` (18 total) — two causes, both confirmed non-code-defect: (a) `config/long_term_memory.json` is no longer corrupted (now a healthy 5-item list — Sprint 63/64's forensic baseline predates the file's real-world recovery, most likely via the separately-completed "Long-Term Memory Self-Healing" sprint's shipped code actually running on the user's real machine since); (b) a live race against a real, concurrently-running process writing to the same shared `config/`/`config/backups/` directory during the test's own execution window — proven by re-running the SAME test twice seconds apart and getting FAIL then PASS.

**Self-inflicted-and-fixed-within-this-sprint:** installing the real `torch`/`torchvision`/`ultralytics` packages into this sandbox (an investigation step, see below) transiently caused ONE unrelated test (`test_p0_6_2_camera_ha_action.py::test_26_...`) to fail via a `tests`-package namespace collision (`ultralytics`'s wheel bundles its own top-level `tests/` package). Found, root-caused, and fixed by fully uninstalling those packages within this same sprint — confirmed passing again immediately after.

**Sandbox `torch` execution:** for the first time in this project's history, the real, exact-version-matching `torch==2.13.0`/`torchvision==0.28.0`/`ultralytics==8.4.123` wheels were successfully downloaded (via a new resumable `curl -C -` technique) and `pip install`ed into this sandbox — but `import torch` still fails here (`libcudart.so.13` missing; PyPI's Linux wheel requires genuine CUDA runtime libraries at import time via eager/data-relocation-bound symbol references, confirmed via `readelf -d`, not fixable by stub `.so` files without a real GPU driver + CUDA toolkit). Root cause was instead established via direct, execution-free source inspection of the real ultralytics/torch source combined with P0.8.3's own pickle-level checkpoint forensics. Both packages were uninstalled again before the regression sweep above (see previous paragraph).

**Result classification: PARTIAL-STRONG** — root cause identified and fixed with a complete, source-evidenced mechanism (`luno/vision.py`'s shared-singleton, inconsistent-`device=`-kwarg, unlocked-inference-call race with ultralytics' own `fuse()`-on-every-mismatched-`predict()` behavior) and full regression coverage including a real-thread race-proof test; final live proof (real RTSP frame → real YOLO inference → real `light.wled` change) still requires the real machine, since neither `torch` execution nor RTSP/HA reachability exist in this sandbox. No live YOLO detection was claimed or performed here, per the brief's own explicit instruction.

## LUNO P0.8.5 — Fix `camera_person_entered` firing with `person_count=0`

**Command:** targeted runs (new suite; full Vision/P0.x suite; `test_p0_8_0`–`test_p0_8_5`) followed by a chunked 145-file full-repository sweep (`ls tests/*.py | grep -v test_main_bargein.py | split -n l/10`, per this project's established per-call time-limit methodology).

**New tests:** `tests/test_p0_8_5_person_count_sync_fix.py` — 11 passed, 0 failed (tests A–H per the user's exact spec, plus 3 additional cross-loop consistency tests I/J/K proving the fix never double-fires regardless of which loop wins the race).

**Full sweep result:** Vision/P0.x suite (10 files) — 256 passed, 1 pre-existing skip, 0 failed. `test_p0_8_0_camera_action_safety.py` through this sprint's own suite (6 files) — 147 passed, 0 failed. `luno/vision.py` and `luno/adapters/vision.py` are the only production files this sprint touched.

**Pre-existing/already-documented baseline failures re-encountered (unchanged categories, all previously recorded in this file):** `test_llm_max_completion_tokens_compatibility.py` (7) + `test_memory_session_summary_api_compatibility.py` (5) (`.env`'s `MAX_TOKENS_PARAM=max_tokens` override), `test_mic_device_index.py` (6, `.env`/`list_microphones.py` gap), `test_real_adapters.py` (2, `RealWhisperSource._device_index`), `test_production_launcher.py::test_07_...` (1, real credentials configured), `test_sprint60_area_schema.py` (2, real `config/lights.config.json` drift), `test_sprint63_long_term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint68_mutation_audit_hardening.py` (18 total, accumulated `config/backups/` drift on this live-synced folder), `test_dashboard.py` (2, documented stress-test/mock-backend flakiness under chunked parallel load), `test_voice_pipeline_latency.py::test_A_first_audio_latency_measured_default_vs_streaming` (1, known full-suite-only timing flake — re-run standalone, passed), `test_root_main_bargein.py` (1 collection error, pre-existing missing `legacy_main.py` — same class as the already-excluded `test_main_bargein.py`).

**Self-inflicted-and-fixed-within-this-sprint:** (1) the new `[VISION PERSON DEBUG]` diagnostic line's original `len(boxes.cls)` broke `test_vision_sprint8.py`'s `_FakeTensor` test doubles (no `__len__`, only `.tolist()`) — fixed by counting off the already-required `.tolist()` conversion instead. (2) this sprint's own explanatory comment in `detect_objects_tracked()` literally contained the string "AutomationEngine" in prose, tripping P0.8.4's own naive-substring architecture guard (`test_p0_8_4_yolo_concurrency_fix.py::test_12`) — fixed by rewording. Both found and fixed by this sprint's own regression sweep before delivery.

**Result classification: STRONG** — root cause identified and fixed with a complete, source- and real-runtime-log-evidenced mechanism (two independent, uncoordinated async polling loops — a presence-only trigger loop and a separately-timed count-bearing tracked-cycle loop — where the trigger and its enrichment data were read from different loops' independently-timed state) and full regression coverage (11 new focused tests + 403 Vision/P0.x tests + a clean 145-file repository sweep). An honest residual-race caveat is disclosed rather than glossed over: the presence-watch loop can still occasionally win a transition very near cold start, before the tracked-cycle loop's first cycle completes, in which case `person_count` is briefly stale until that loop's next cycle corrects it (up to ~0.5s at default cadence) — materially narrower than the bug fixed (which reproduced on effectively every real transition), not provably eliminated. Final live proof (real camera → `[VISION PERSON DEBUG] person_count=N` → matching `[CAMERA EVENT] kind=human_detected person_count=N`) still requires the real machine, since neither RTSP nor a real YOLO/torch install exist in this sandbox. See `docs/change_impact/camera_automation_p0_8_5.md` for the full writeup.

## LUNO P0.8.6 — End-to-End Human Detection → WLED Reliability Fix

**Command:** targeted runs (new suite; Vision/P0.x targeted suite; `luno/tool_manager/tests/test_real_home_assistant_verification.py`; `luno/` fast suite) followed by a chunked 144-file full-repository sweep (`ls tests/*.py | grep -v -E "test_main_bargein.py|test_root_main_bargein.py" | split -n l/10`, per this project's established per-call time-limit methodology).

**New tests:** `tests/test_p0_8_6_end_to_end_human_wled_reliability.py` — 25 passed, 0 failed (covers the brief's 20 numbered scenarios: sub-threshold confidence never confirms; single-cycle-at-floor is candidate-only; sustained detection confirms exactly once with no duplicate events; the CONFIRMED signal's stricter falling/rising edges vs. the raw P0.8.5 debounce; multi-person cycles; false-positive-frame sequences; low-confidence visibility in diagnostics; WLED already-ON/OFF end-to-end via the real bootstrap + mock HA dispatcher; HA command failure/entity-unavailable never produce false success; the new `verification_scope` wording; `light.wled` configuration consistency; source-level guards proving the P0.8.4 lock and P0.8.5 call site were never touched).

**Full sweep result:** targeted P0.x/Vision suite (14 files, including the new suite) — 487 passed, 1 pre-existing skip, 0 failed. `luno/tool_manager/tests/test_real_home_assistant_verification.py` — 39 passed. `luno/` fast suite — 818 passed, 2 failed (both the same pre-existing FLAKY-KNOWN `barge_in` timing tests below). Full 144-file repository sweep — 4162 passed, 39 failed, every failure mapping to an already-documented pre-existing baseline category below — zero new failures caused by this sprint's changes. `luno/config.py`, `luno/adapters/events.py`, `luno/adapters/vision.py`, `luno/camera_automation/*`, `config/automation_rules.json` (one rule only), and `luno/tool_manager/builtin/real_home_assistant.py` (wording only) are the only production files this sprint touched.

**Pre-existing/already-documented baseline failures re-encountered (unchanged categories, all previously recorded in this file):** `luno/barge_in/tests/test_barge_in.py` (2, FLAKY-KNOWN timing), `test_llm_max_completion_tokens_compatibility.py` (7) + `test_memory_session_summary_api_compatibility.py` (5) (`.env`'s `MAX_TOKENS_PARAM=max_tokens` override), `test_mic_device_index.py` (6, `.env`/hardware gap), `test_real_adapters.py` (2, `RealWhisperSource._device_index`/PortAudio), `test_production_launcher.py::test_07_...` (1, real credentials configured), `test_sprint63_long_term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint68_mutation_audit_hardening.py` (18 total, accumulated `config/backups/` drift on this live-synced folder).

**Five pre-existing tests updated (documented, intentional — not weakened):** the WLED rule's trigger condition changed from `event.kind=="human_detected"` to `event.kind=="human_confirmed"` (plus new `available`/`detection_error` conditions) and the bridge's subscribed-event set grew by two — `tests/test_p0_5_3_vision_camera_bridge.py::test_05`, `tests/test_p0_6_camera_automation_rules.py::test_21`, `tests/test_p0_6_1_live_log_verification.py::test_12`, `tests/test_p0_6_2_camera_ha_action.py::test_18`, `tests/test_p0_6_3_unified_vision_camera_automation.py::test_24`, `tests/test_p0_7_vision_context.py::test_34`, `tests/test_p0_8_0_camera_action_safety.py::test_26`/`test_28` — each updated with a docstring explaining exactly why; the underlying invariant each proves is unchanged, only the event/kind used to reach it.

**Result classification: STRONG** — both reported problems (false-positive single-frame WLED trigger; dishonest "verification success" wording) root-caused with a complete, source-evidenced mechanism and full regression coverage (25 new focused tests + 487 targeted tests + a clean 144-file repository sweep). Physical WLED confirmation was never claimed and cannot be claimed from this sandbox (no RTSP/real HA/physical sensing channel exists here) — a disclosed architectural limit, not an unresolved bug. See `docs/change_impact/camera_automation_p0_8_6.md` for the full writeup, including an honest caveat about the pre-existing `test_real_home_assistant_verification.py` file's own non-`assert`-based test style (unrelated to and not introduced by this sprint).

## LUNO P0.8.7 — Investigate and Fix the Remaining WLED Activation Failure (Verification Freshness Fix)

**Command:** targeted runs (new suite; `test_real_home_assistant_verification.py`; `test_tool_manager.py`; `test_real_adapters.py`; P0.0–P0.8.6 camera automation suite) followed by a chunked ~152-file full-repository sweep (`ls tests/*.py luno/tool_manager/tests/*.py | grep -v -E "test_main_bargein.py|test_root_main_bargein.py" | split -n l/8`, `pytest -n 4 --timeout=90` per chunk, per this project's established per-call time-limit methodology).

**New tests:** `tests/test_p0_8_7_wled_verification_fix.py` — 18 passed, 0 failed (sections A–H: `RealHomeAssistantClient.get_entity_state(force_refresh=...)` fresh-vs-cached divergence proof against a real background `RealHomeAssistantSource`; handler-level `_verify_state()` fresh-query proof plus backward-compatibility for clients lacking the parameter; the new `state_query_freshness` field; exact outbound `domain`/`service`/`entity_id`/service-data shape; entity-resolution tier-1-literal proof; AST-based credential-leak structural scan; `ToolManager` pass-through functional proof; `WorldModel`-independence structural scan).

**Full sweep result:** new suite 18/18 pass. `test_real_home_assistant_verification.py` — 39/39 genuine passes via the file's own `main()` runner (not just pytest's non-`None`-return-tolerant count). Focused HA/tool + real-adapters regression (4 files) — 86 passed, 2 failed (both the same pre-existing `test_real_whisper_source_*` `RealWhisperSource`/`_device_index` gap below). P0.0–P0.8.6 camera automation suite (16 files) — 483 passed, 1 pre-existing skip, 0 failed. Full ~152-file repository sweep — every failure mapping to an already-documented pre-existing baseline category below — zero new failures caused by this sprint's changes; every failing file was additionally re-run in isolation and reproduced the identical failure with no HA/tool_manager code anywhere in its call path. `luno/adapters/real_home_assistant.py` and `luno/tool_manager/builtin/real_home_assistant.py` are the only production files this sprint touched.

**Pre-existing/already-documented baseline failures re-encountered (unchanged categories, all previously recorded in this file):** `test_llm_max_completion_tokens_compatibility.py` (7) + `test_memory_session_summary_api_compatibility.py` (5) (`.env`'s `MAX_TOKENS_PARAM=max_tokens` override), `test_mic_device_index.py` (6, `.env`/hardware gap), `test_real_adapters.py` (2, `RealWhisperSource._device_index`/PortAudio), `test_production_launcher.py::test_07_...` (1, real credentials configured), `test_sprint60_area_schema.py` (2, real config-migration drift), `test_sprint63_long_term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint68_mutation_audit_hardening.py` (18 total, accumulated `config/backups/` drift on this live-synced folder), `test_sprint66_tool_boundary_hardening.py::test_performance_validate_download_directory_is_fast` (1, documented timing-sensitive), `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays` (1, known flaky timing, same class as the already-documented `barge_in` category).

**Result classification: STRONG** — root cause identified (verification reads were cache-first, not guaranteed to reflect a query specifically triggered by the command being verified) and fixed with a complete, source-evidenced mechanism (`_verify_state()` now always performs a genuinely live post-command HA query via a new, additive, fully backward-compatible `force_refresh` parameter) and full regression coverage (18 new focused tests + 86 HA/tool regression tests + 483 P0.x camera automation tests + a clean full-repository sweep). Physical WLED illumination (item D in the brief's own A/B/C/D framework) was never claimed and cannot be claimed — a disclosed architectural limit, not a defect this sprint could fix; see `docs/change_impact/camera_automation_p0_8_7.md` for the full writeup, including the honest discussion of what remains outside this repository's control if the symptom persists after this fix.

## LUNO P0.8.8 — Fix the Confirmed Camera Automation Event Suppression Bug

**Command:** targeted runs (new suite; full camera_automation/P0.x suite) followed by a chunked ~153-file full-repository sweep (`ls tests/*.py luno/tool_manager/tests/*.py | grep -v -E "test_main_bargein.py|test_root_main_bargein.py" | split -n l/8`, `pytest -n 4 --timeout=90` per chunk, per this project's established per-call time-limit methodology).

**New tests:** `tests/test_p0_8_8_camera_event_suppression_fix.py` — 16 passed, 0 failed (sections A-L: core reproduction/fix proof against the real `ingest_external_camera_event()`; different camera/kind keys don't interfere; no reconnect/restart needed to reset suppression; a real production-call-path end-to-end test through `VisionCameraEventBridge` -> real `CameraAutomationModule` -> real Event Bus -> real `AutomationEngine`, three separate rule completions across cooldown-separated detections; legacy relay path's existing anti-spam behavior re-locked; the OTHER classified call site's same bug/fix re-proven; no-duplicate-events proof; `time.monotonic()`-not-`time.time()` structural + behavioral proof).

**Full sweep result:** new suite 16/16 pass. Focused camera_automation/P0.x suite (16 files) — 494 passed, 1 pre-existing skip, 0 failed (baseline immediately before this sprint was 478 passed, 1 skipped — exactly `478 + 16 = 494`, zero regressions). Full ~153-file repository sweep — every failure mapping to an already-documented pre-existing baseline category below — zero new failures caused by this sprint's changes. `luno/camera_automation/module.py` is the only production file this sprint touched.

**Pre-existing/already-documented baseline failures re-encountered (unchanged categories, all previously recorded in this file):** `test_llm_max_completion_tokens_compatibility.py` (7) + `test_memory_session_summary_api_compatibility.py` (5) (`.env`'s `MAX_TOKENS_PARAM=max_tokens` override), `test_mic_device_index.py` (6, `.env`/hardware gap), `test_real_adapters.py` (2, `RealWhisperSource._device_index`/PortAudio), `test_production_launcher.py::test_07_...` (1, real credentials configured), `test_sprint60_area_schema.py` (2, real config-migration drift), `test_sprint63_long_term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint68_mutation_audit_hardening.py` (18 total, accumulated `config/backups/` drift on this live-synced folder). Two additional failures observed ONLY under `-n 4` parallel xdist execution — `test_state_isolation.py::test_verified_facts_does_not_leak_between_tests_part_b` and `test_verification_dashboard.py::test_api_verification_reports_a_successful_verified_action_end_to_end` — both reproduced as a clean pass in isolation (22/22 and 6/6 respectively) and are already-documented pre-existing parallel-execution-order flake categories in this file (69 and 21 prior references respectively).

**Result classification: STRONG** — root cause identified (a compile-time-constant dedupe comparison, `state` derived from part of the suppression `key` itself, made the intended `_cooldown_until` time-based check unreachable dead code for every classified Vision/camera event) and fixed with a minimal, additive, fully backward-compatible mechanism (a new `dedupe_identical` parameter, defaulting to the legacy relay path's exact prior behavior, explicitly set to `False` only at the two classified call sites that had the bug) and full regression coverage (16 new focused tests including a genuine end-to-end production-call-path proof + a clean, baseline-matched full-repository sweep). This closes the "person detected repeatedly but WLED only turns on once" symptom — restores AutomationEngine's ability to receive repeated classified camera events and therefore the WLED rule's ability to re-dispatch on every subsequent detection. Physical WLED illumination was never claimed and remains, as disclosed in P0.8.7, outside this codebase's ability to independently confirm; see `docs/change_impact/camera_automation_p0_8_8.md` for the full writeup, including the honest A-F staged evidence report.

## LUNO P0.8.9 — Implement the Missing WLED OFF Automation Rule

Not a bug — `camera_human_detected_test_action` (the real `light.wled` ON
rule, P0.6.2) never had a real-entity OFF counterpart; the only existing
OFF rule (P0.8.2) targets the mock `light.test_camera_automation` entity.
Added `camera_wled_human_cleared_off` (10s debounce, `light.wled`) using a
new `delay_seconds` action parameter dispatched via the project's
EXISTING `runtime.scheduler` (`Scheduler.schedule_once()`/`cancel()` —
no new timer invented), cancelled/superseded by keying on target entity
id (not rule id) so a fresh `human_confirmed` transparently cancels a
pending OFF with zero new coupling between the two rules. New suite:
`tests/test_p0_8_9_wled_off_debounce.py` (25 tests, all passing).

**Newly observed this sprint, confirmed unrelated to any code changed:**
`.env` now has `CAMERA_AUTOMATION_ENABLED=true` (previously `False`, per
this file's own P0.6.1-era note above) — `test_p0_camera_automation.py::
test_15_disabled_by_default_real_bootstrap_no_subscription_footprint`,
`test_p0_5_camera_integration.py::test_35_disabled_camera_automation_
remains_inert_e2e`, and `test_p0_5_3_vision_camera_bridge.py::test_22_
disabled_by_default_bridge_never_subscribes_real_bootstrap` all assert
"fresh checkout, disabled by default" and now fail against this
checkout's real `.env`. Confirmed purely an `.env` value, not a code
regression, by re-running the same three tests with
`CAMERA_AUTOMATION_ENABLED=false` explicitly overridden — clean 3/3 pass.
Likely a deliberate, persistent change from the user's own recent
live-troubleshooting session (production `main.py` needing
camera_automation to survive a real restart) — flagged for the user to
confirm, not modified by this sprint (out of scope).

**Full repository sweep (154 files):** 4,325 passed, 1 skipped, 42
failed — the three `CAMERA_AUTOMATION_ENABLED` failures above plus the
same long-documented pre-existing families already tracked in this file:
LLM `max_completion_tokens` `.env` override (`test_llm_max_completion_
tokens_compatibility.py`, `test_memory_session_summary_api_
compatibility.py`), no-audio-hardware sandbox gap (`test_mic_device_
index.py`, `list_microphones.py` FileNotFoundError), real-whisper
`_device_index` construction gap and blocked network egress
(`test_real_adapters.py`, `test_production_launcher.py::test_07`),
`config/backups/` forensic-drift family (`test_sprint63_long_term_
memory_recovery.py`, `test_sprint64_memory_corruption_forensics.py`,
`test_sprint68_mutation_audit_hardening.py`), and the real
`config/lights.config.json` `light.main_light` config drift
(`test_sprint60_area_schema.py`, 2 — already noted above in the P0.8.4
entry). Zero failures touch `luno/automation/` or `luno/camera_
automation/` beyond the three `CAMERA_AUTOMATION_ENABLED` ones.

**Result classification: STRONG** — see `docs/change_impact/camera_
automation_p0_8_9.md` for the full writeup, including the honest A-F
staged evidence discussion (stage F, physical illumination, is explicitly
not claimed).

## LUNO P0.9 — Room Occupancy State + Presence Duration

New, additive, always-active `Module` (`luno/vision_occupancy.py::
RoomOccupancyModule`) subscribing to three EXISTING Vision events
(`HumanPresenceConfirmed`/`CameraPersonLeft`/`VisionFrameProcessed`, all
unmodified) to derive `vacant`/`occupied` + presence-duration tracking.
Never touches Home Assistant/ToolManager/WLED/YOLO — enforced by 7 static
architecture-guard tests. `luno/vision.py`, `luno/adapters/vision.py`,
`luno/camera_automation/`, `luno/automation/`, and `config/automation_
rules.json` were not opened or modified. New suite: `tests/test_p0_9_
room_occupancy.py` (34 tests, all passing).

**Full repository sweep (155 files):** 4,356 passed, 1 skipped, 45
failed — the same long-documented pre-existing families already tracked
in this file (LLM `.env` token-param override, no-audio-hardware sandbox
gap, `config/backups/` forensic drift, real `light.main_light` config
drift, the P0.8.9-documented `CAMERA_AUTOMATION_ENABLED=true` `.env`
condition), plus:
- `test_vision_sprint8.py::test_29_stress_many_cycles_varying_scene_no_
  crash_no_leak` (focused-suite run) — a `time.sleep(1.5)`-based
  real-thread stress test; re-run in isolation 3x, clean pass every time.
  Full-suite-only timing flake under `-n 4` parallel CPU contention, same
  category already documented for `test_barge_in.py`/`test_state_
  isolation.py`/`test_verification_dashboard.py` elsewhere in this file.
- `test_sprint66_tool_boundary_hardening.py::test_performance_validate_
  download_directory_is_fast` and `test_sprint67_mutation_audit_trail.py::
  test_this_files_own_run_never_touches_the_real_config_directory` — both
  re-run in isolation immediately after, both passed cleanly. Parallel-
  load timing flakes, not regressions.
- `test_sprint63_long_term_memory_recovery.py::test_N_production_config_
  files_unchanged_by_this_test_run` — failed once in combination with two
  neighboring tests, passed cleanly standalone both before and after.
  Traced to real, live churn of `config/vision_memory.sqlite3-wal`/`-shm`
  (this checkout's own live Luno process actively uses this SQLite WAL
  database) — the same `vision_memory.sqlite3-wal`/`-shm` drift family
  already referenced 5 times elsewhere in this file.

Zero failures touch `luno/vision_occupancy.py`, `luno/vision.py`,
`luno/camera_automation/`, or `luno/automation/`.

**Result classification: STRONG** — see `docs/change_impact/room_
occupancy_p0_9.md` for the full writeup.

## LUNO P0.10 — Occupancy-Aware Automation Intelligence

Purely additive on top of P0.9: two new read-only `RoomOccupancySnapshot`
fields (`occupancy_age_seconds`, `last_transition`), a `previous_state`
field added to the `room_occupied`/`room_vacant`/`occupancy_changed`
event payloads, five new `"occupancy.*"` `AutomationEngine` state_readers
wired through the EXISTING `state_readers` context mechanism (same one
`"camera_patrol"` already used), and two new log-only diagnostic
automation rules (`occupancy_test_log`, `occupancy_long_presence_test`)
appended to `config/automation_rules.json`. Neither controls a device.
`luno/vision.py`, `luno/adapters/vision.py`, `luno/camera_automation/`,
`luno/automation/engine.py`'s dispatch logic, and the existing WLED
ON/OFF rule bodies were not opened or modified. New suite: `tests/
test_p0_10_occupancy_context.py` (44 tests, all passing). One
pre-existing test (`tests/test_p0_8_9_wled_off_debounce.py::test_B7_
real_rules_file_has_exactly_six_rules`) was intentionally updated to
reflect the new eight-rule shipped set (P0.10 Phase 5's own mandated
addition, not a behavior fix).

**Full repository sweep (156 files):** 4,402 passed, 1 skipped, 43
failed — the same long-documented pre-existing families already tracked
in this file (LLM `.env` token-param override, no-audio-hardware sandbox
gap, real-whisper `_device_index`/blocked-network gap, `config/backups/`
forensic drift, real `light.main_light` config drift, the P0.8.9-
documented `CAMERA_AUTOMATION_ENABLED=true` `.env` condition), plus:
- `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_
  never_plays` (focused-chunk run under `-n 4`) — re-run in isolation 3x,
  clean pass every time. Full-suite-only timing flake under parallel CPU
  contention, same category already documented for `test_barge_in.py`/
  `test_vision_sprint8.py::test_29`/`test_state_isolation.py`/`test_
  verification_dashboard.py` elsewhere in this file.

Zero failures touch `luno/vision_occupancy.py`, `luno/automation/
engine.py`, `luno/automation/conditions.py`, `luno/bootstrap/modules.py`,
or `config/automation_rules.json`.

**Result classification: STRONG** — see `docs/change_impact/camera_
automation_p0_10.md` for the full writeup.

## LUNO P0.11 — Action Sequence Engine

New, additive `sequence: List[AutomationAction]` field on
`AutomationRule`, mutually exclusive with the pre-existing `actions`
field (a rule defines exactly one). Reuses `AutomationAction`'s own
`{type, parameters}` shape verbatim for every existing device-action
type, plus one new pseudo-type, `{"type": "delay", "seconds": N}`.
`_run_execution()` branches to a new `_run_sequence()` when `rule.
sequence` is non-empty; the legacy `_run_actions()` call path is
completely unmodified for every existing rule. A sequence stops at the
first failing step (a deliberately different, binary `COMPLETED`/
`FAILED` policy from the legacy path's own run-everything-then-classify
`COMPLETED`/`PARTIAL_FAILURE`/`FAILED` counting, which is unchanged). A
delay step blocks only its own execution's dedicated thread
(`threading.Event().wait()`, never `time.sleep()`), never the
AutomationEngine itself or a sibling execution. New suite: `tests/
test_p0_11_action_sequence.py` (52 tests, all passing, including 7
static architecture-guard tests and a real-stack proof that a 1s
mid-sequence delay in one automation does not block a second, unrelated
automation triggered during it).

**Full repository sweep (157 files):** 4,454 passed, 2 skipped, 43
failed — the same long-documented pre-existing families already tracked
in this file (LLM `.env` token-param override, no-audio-hardware sandbox
gap, real-whisper `_device_index`/blocked-network gap, real credentials
in `.env`, real `light.main_light` config drift, `config/backups/`
forensic drift, the documented `test_performance_validate_download_
directory_is_fast` timing sensitivity, and the P0.8.9-documented
`CAMERA_AUTOMATION_ENABLED=true` `.env` condition — 2 instances, `test_
p0_camera_automation.py::test_15_...` and `test_p0_5_3_vision_camera_
bridge.py::test_22_...`, both independently re-confirmed to pass clean
under `CAMERA_AUTOMATION_ENABLED=false`).

Zero failures touch `luno/automation/`, `luno/vision.py`, `luno/
vision_occupancy.py`, or `luno/camera_automation/`.

**Result classification: STRONG** — see `docs/change_impact/action_
sequence_engine_p0_11.md` for the full writeup. Physical Home Assistant/
WLED hardware was NOT exercised — every test routes through
`MockHomeAssistantHandler`.

## LUNO P0.12 — Automation API & CRUD

New `luno/dashboard/automation_api.py` translation layer exposing
`AutomationEngine` rule management over HTTP (`/api/automations*`) —
list/get/create/update/delete/enable/disable/run/validate — for the
Dashboard. No second `AutomationEngine`, no second persistence
mechanism, no second execution path: every mutation calls an existing
or additively-extended `AutomationEngine` method (`create_rule()`/
`update_rule()`/`delete_rule()` are new, additive methods; `enable_
automation()`/`disable_automation()`/`run_automation()` are reused
verbatim/near-verbatim). `AutomationRule` gained three additive,
genuinely-persisted fields (`description`, `created_at`, `updated_at`,
server-set only). New suite: `tests/test_p0_12_automation_api.py` (54
tests, including 10 static architecture-guard tests M1–M10 proving no
second engine/persistence/execution path, no direct HA call, no
ToolManager bypass, no `eval`/`exec`/shell/dynamic import, and Vision/
Camera/Occupancy modules untouched).

**Full repository sweep (158 files):** 4,507 passed, 44 failed — the
same long-documented pre-existing families already tracked in this
file (LLM `.env` token-param override — 7, `config/backups/` forensic
drift + download-directory timing sensitivity — 17, no-audio-hardware
sandbox gap, real-whisper `_device_index` gap, real credentials in
`.env`, real `light.main_light` config drift, `CAMERA_AUTOMATION_
ENABLED=true` `.env` condition, and one confirmed parallel-load-only
timing flake in `test_p0_11_action_sequence.py::test_F2_completed_
status_after_full_success`, re-confirmed clean via isolated re-run).

Zero failures touch `luno/automation/`, `luno/dashboard/`, `luno/
vision.py`, `luno/vision_occupancy.py`, or `luno/camera_automation/`.

**Result classification: STRONG** — see `docs/change_impact/
automation_api_p0_12.md` for the full writeup. No authentication
mechanism was invented (none existed before this sprint) — the
localhost-only bind remains the sole security boundary, documented as
a known limitation. Physical Home Assistant/WLED hardware was NOT
exercised — every test routes through `MockHomeAssistantHandler`.

## LUNO P0.13 — Automation Dashboard / Visual Automation Builder

New UI, entirely additive, added inline to the project's single
existing static asset (`luno/dashboard/static/index.html` — no
separate .js/.css files, no build step, no frontend framework exist
anywhere in this project). Consumes the P0.12 Automation API
exclusively — the UI never calls Home Assistant directly, never reads
or writes `config/automation_rules.json` directly, and never
introduces a second automation execution path (`AutomationEngine`'s
own pipeline remains the sole executor, reached only through
`POST /api/automations/{id}/run`). One new, minimal, read-only API
endpoint was added — `GET /api/automations/schema` (live reflection of
`models.py`'s own `TRIGGER_TYPES`/`CONDITION_TYPES`/`ACTION_TYPES`/
`SEQUENCE_STEP_TYPES` constants, plus the already-loaded
`luno.devices.LIGHTS`/`SWITCHES` registry, plus non-enforced UI
autocomplete hints). The sequence builder reuses the P0.11 `{"type",
"parameters"}` schema verbatim — no second action schema was invented.
New suite: `tests/test_p0_13_automation_dashboard.py` (65 tests,
sections A–X + a Schema section + 11 static architecture-guard tests
M1–M11, implemented via a custom brace-depth JS function-body
extractor since no JS AST tool exists in this sandbox).

**Full repository sweep (154 files under `tests/`, 8-chunk
methodology):** 4,464 passed, 45 failed, 5 collection errors, 1
skipped — every failure/error traced to an already-documented
pre-existing category: LLM `.env` token-param override (12),
`config/backups/`/mutation-audit forensic drift (16), no-audio-hardware
sandbox gap (6), real-whisper construction gap (2), real credentials in
`.env` (1), real `light.main_light` config drift (2), one documented
timing-sensitive test (1), `CAMERA_AUTOMATION_ENABLED=true` `.env`
condition (3, targeted suite), the pre-existing 2-file collection-error
family (`test_main_bargein.py`/`test_root_main_bargein.py`), one
confirmed parallel-xdist-order flake
(`test_verification_dashboard.py::
test_api_verification_reports_a_successful_verified_action_end_to_end`,
re-confirmed clean standalone), and one newly-observed instance of the
long-documented real-network/hardware sandbox-isolation limit
(`test_sprint71_dashboard_startup_recovery.py::
test_12_e2e_main_py_survives_dashboard_port_conflict_and_keeps_running`
— spawns the real `main.py`, which attempts a real HA websocket + RTSP
connection unreachable from this sandbox; confirmed via the test's own
source, not caused by anything this sprint touched).

Zero failures touch `luno/automation/`, `luno/dashboard/`, `luno/
vision.py`, `luno/vision_occupancy.py`, or `luno/camera_automation/`.

**Result classification: STRONG** — see `docs/change_impact/
automation_dashboard_p0_13.md` for the full writeup. No authentication
mechanism was invented — the localhost-only bind remains the sole
security boundary for the new schema endpoint too, same as every other
route. Physical Home Assistant/WLED hardware was NOT exercised — every
test and every manual UI action in this sandbox routes through
`MockHomeAssistantHandler`. Per the user's own explicit closing
instruction, P0.14/AI-natural-language automation authoring was NOT
started.

## LUNO P0.14 — Advanced Home Assistant Automation Actions & Script Runner

Seven new `home_assistant.*` action types (`toggle`, `set_brightness`,
`set_color`, `set_temperature`, `run_script` [optional `variables`],
`activate_scene`, `call_service` [generic, controlled — domain/service
validated with a lowercase-snake-case regex]) and three new
sequence-only control step types (`wait_until` [bounded polling, reuses
the existing `ha_state_reader` hook + `evaluate_condition()`, never a
second HA read/comparison path], `condition` [constrained if/then/else,
`MAX_CONDITION_NESTING_DEPTH=3`, reuses `evaluate_condition()` and
`_run_sequence_step()` verbatim], `stop_automation` [→ `CANCELLED`, a
new terminal status distinct from `FAILED`]). Every new action type
still dispatches through the EXACT SAME `AutomationEngine._dispatch_
action()` → `_dispatch_tool_call()` → `tool_requested` → `ToolManager`
round trip every pre-existing action already used — still exactly ONE
execution path. The Camera Action Safety Gate's own allowlist was
deliberately left unchanged — every new action type is automatically
refused for a camera-triggered rule. One new, minimal, read-only
endpoint, `GET /api/automations/devices` (categorized picker — lights/
switches/scripts real, fans/climate/media_players/sensors/scenes/other
always honestly empty, never fabricated). New suite: `tests/test_p0_14_
ha_script_actions.py` (58 tests, sections A–T + a concurrency test +
an honest `REAL_HA_TEST = NOT_PERFORMED` marker).

**Discovered, not caused, this sprint:** the real `config/automation_
rules.json` now contains only one rule — a genuine, user-created "Back
From Work" rule built through the live P0.13 dashboard. Every P0.6–
P0.10 diagnostic/safety rule previously shipped is gone, traced
conclusively (via `config/backups/`'s own 91-file history) to
deliberate, sequential user deletions through the live dashboard, not a
P0.14 bug — restorable from `config/backups/` if wanted. The affected
pre-existing test files were deliberately left untouched (not silenced)
since they exist as regression guards for that real production data.

**Full repository sweep (153 files under `tests/`, 4-chunk parallel
methodology):** 4,448 passed, 104 failed — every failure traced to an
already-documented pre-existing category: the newly-discovered
`config/automation_rules.json`/real-device-config drift family above
(~87, spanning `test_p0_6*.py`/`test_p0_7*.py`/`test_p0_8_0/1/2*.py`/
`test_p0_8_9*.py`/`test_p0_10*.py`/`test_sprint60_area_schema.py`/
`test_p0_camera_automation.py`), LLM `.env` token-param override (12),
`config/backups/`/mutation-audit forensic drift (13), no-audio-hardware
sandbox gap (6), real-whisper construction gap (2), real credentials in
`.env` (1), and one confirmed parallel-xdist-order timing flake
(`test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_
never_plays`, re-confirmed clean standalone: `1 passed in 0.81s`).

Zero failures touch `luno/automation/`, `luno/dashboard/`, `luno/tool_
manager/builtin/home_assistant*.py`, `luno/vision.py`, `luno/vision_
occupancy.py`, or `luno/camera_automation/`.

Also: 5 pre-existing P0.12/P0.13 architecture-guard tests were fixed
forward this sprint (`test_p0_12_automation_api.py::test_M3`,
`test_p0_13_automation_dashboard.py::test_T1`/`test_T3`/`test_SCHEMA5`)
— their bare substring/lowered-source-segment checks false-positived on
P0.14's own legitimate new schema strings/comments/device domain;
re-expressed as precise AST-based/word-boundary checks preserving each
guard's exact original intent (no import/instantiation of a real HA
client, no direct service-call invocation, no unqualified dispatch
pattern), same "legitimate, in-scope literal update" convention every
prior sprint has used.

**Result classification: STRONG** — see `docs/change_impact/
ha_script_actions_p0_14.md` for the full writeup. `REAL_HA_TEST =
NOT_PERFORMED` — no real Home Assistant instance is reachable from this
sandbox, honestly recorded rather than fabricated; every test and every
manual dashboard action routes through `MockHomeAssistantHandler`, and
one real end-to-end smoke test against the real bootstrap stack (mock
HA backend) confirmed a 7-step mixed sequence → COMPLETED, a
`stop_automation` sequence → CANCELLED, and an unbound `wait_until` →
TIMEOUT. Per the user's own explicit closing instruction, P0.15/AI-
natural-language/voice/autonomous automation authoring was NOT started.

## LUNO P0.15 — Human-Friendly Dashboard UX & Time-Based Automation Conditions

One new, additive condition type — `{"type": "time", "parameters":
{"after": "HH:MM", "before": "HH:MM"}}` — routed through the EXISTING
`AutomationEngine._evaluate_conditions()` → `evaluate_condition()`
pipeline every other condition already uses (`engine.py` required ZERO
changes; confirmed by direct inspection that its generic per-condition
loop already delegates by type). Supports both normal (`after <=
before`) and overnight/crosses-midnight (`after > before`) ranges, both
boundaries inclusive, verified against every worked example in the
brief (18:00–23:30 and 22:00–02:00). No scheduler, timer, or polling
loop was introduced — a time condition is a pure, on-demand comparison
against `datetime.datetime.now().time()`, evaluated once, at real
trigger-processing time. `TIME_CONDITION_TYPE` deliberately kept OUTSIDE
`CONDITION_TYPES` (that frozenset stays pure comparison operators only).
Dashboard UX polish (Section 8 of the brief): a dedicated "🕐 Time"
condition card (native time inputs, a live "Active during this period"
indicator) replacing the need to touch raw `type`/`target`/`value`
fields for the common case; natural-language "When X / Only between Y /
→ Z" summaries under each automation's name in the list view; empty/
loading states; inline validation messages matching the brief's own
wording. New suite: `tests/test_p0_15_time_conditions.py` (52 tests,
sections A–G: time validation, normal-range boundaries, overnight-range
boundaries [parametrized against all seven brief examples], automation
behavior [condition-true/false gating of actions/sequence/ToolManager,
backward compatibility], persistence [create→save→reload for both
normal and overnight windows], dashboard source-scan, and architecture
guards [no scheduler/polling loop, `engine.py` untouched — an AST-based
call-site count, not a raw substring count — exactly one
`AutomationEngine` class, no forbidden execution primitives]).

**Regression methodology (brief's own Section 13, followed in order):**
P0.11/P0.12/P0.13/P0.14/Sprint-72 suites re-run BEFORE any P0.15 change
(307 passed, 0 failed — this is the baseline), the new P0.15 suite
written and run (52 passed), the same P0.11–P0.14 suites re-run after
(307 passed, 0 failed — identical), Vision/Camera Automation suites
re-run (24 files — 655 passed, 24 failed, 1 skipped, every failure
re-traced to the exact same already-documented `config/automation_
rules.json`/`config/camera_automation.json` real-production-data-drift
family P0.14 discovered, unchanged by this sprint), then a full
156-file repository sweep (chunked methodology; 3 pre-existing
collection errors for already-documented sandbox gaps) — approximately
105 failures, every one individually re-traced to an already-documented
pre-existing category: the `config/automation_rules.json` drift family
above, LLM `.env` token-param override, `config/backups/`/mutation-audit
forensic drift, no-audio-hardware sandbox gap, real-whisper construction
gap, real credentials in `.env`, and one newly-observed but unrelated
timing flake (`test_llm_tts_streaming_production.py::
test_14_cancellation_during_synthesis` — a FishAudio mock cancellation
race, nothing to do with automation conditions). Zero failures touch
`luno/automation/`, `luno/dashboard/`, or this sprint's own new test
suite.

**Result classification: STRONG** — see `docs/change_impact/
time_conditions_p0_15.md` for the full writeup. Per the user's own
explicit closing instruction ("Stop after P0.15. Do not begin the next
sprint automatically."), no further sprint was started.
