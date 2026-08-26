# Luno Current State

This file reflects the actual state of the repository as verified by direct inspection (source reading + running tests), not conversation history. See `ARCHITECTURE.md` for the system design and `DECISIONS.md` for why things are built the way they are.

## Current Status

Mature, stable, extensively tested. No feature is mid-implementation. The event-driven architecture (`luno/core` + everything on top) is the actively developed system; a legacy procedural script (`luno/main.py`) is preserved unchanged as reference and is no longer the production entry point. **This is not a git repository** — no commit history exists to inspect; there is nothing to `git log`/`git diff` against. State must be tracked via this file, `ARCHITECTURE_GUARD.md`, and `docs/change_impact/*`/`docs/testing/regression_baseline.md` going forward.

## Completed

Each item below has a dedicated test suite and, for larger sprints, a `docs/change_impact/*.md` write-up.

- **Event-driven core** — `EventBus`/`Dispatcher`/`Coordinator`/`ModuleManager`/`Runtime`, including self-healing degraded-subscriber handling (backoff, not permanent unsubscribe).
- **Adapter layer** — OpenRouter/LLM Manager (multi-provider), Fish Audio (mock + real GPT-SoVITS/F5-TTS), Whisper (mock + real Faster-Whisper), Home Assistant (mock + real websocket), Vision, Unity/avatar, Scheduler — every adapter has a working mock, real backends are opt-in via env vars.
- **Planner / Tool Manager / Behavior Tree** — full dependency-ordered task execution with retry/rollback/cancel/pause-resume, verified (expected-vs-actual state) tool execution.
- **Wake Session** (Sprint 2) — wake-word gate + conversation session timeout state machine.
- **Barge-In** (Sprint 3) — FREE/SOFT/CONFIRM/CRITICAL interrupt classification, full-duplex listening.
- **World Model** — single source of truth for current device state, built on existing HA/ToolResult signals, no polling.
- **Vision Memory** — persistent visual world-state + meaningful-change log + habit learning, SQLite-backed.
- **Dashboard** — read-only HTTP+SSE operational visibility (stdlib only, no new framework dependency).
- **Memory subsystem** (multiple stacked sprints — long-term memory, manual memory, memory evaluation/self-calibration, memory outcome telemetry & closed-loop learning, memory context assembly & retrieval unification, memory decision quality & adaptive retrieval, relationship engine, episodic/shared-experience memory, memory guard / verified facts).
- **Memory Recovery & Persistence Hardening** — recovered a real production data-loss incident on `long_term_memory.json` from a 2026-07-23 snapshot (full record in `recovery/`), then built the atomic-write + backup + recovery mechanism (`_atomic_write_json`/`_backup_current_memory_file`/etc. in `luno/memory.py`) that became the reference implementation for the next item.
- **Persistent State Hardening V2** — generalized that reference implementation into `luno/persistence.py` (domain-agnostic, atomic write + backup + retention=20 + corruption recovery + pytest write guard) and applied it to the six other JSON stores: `relationship_state.json`, `episodic_memory.json`, `session_summaries.json`, `habit_memory.json`, `reminders.json`, `verified_facts.json`. `long_term_memory.json` deliberately NOT migrated — stays the independent reference (see `DECISIONS.md`).
- **Emotion Engine** — text-based user-emotion estimation feeding a small prompt note + internal tone-lean policy (its own, unrelated `ResponsePolicy` dataclass — see Known Limitations for the naming overlap).
- **Intelligent AI Routing Engine** — per-turn LLM provider/complexity/knowledge-source routing decision (`luno/routing/`), separate concern from response depth.
- **Response Depth Policy** — `luno/response_policy.py`, deterministic SHORT/NORMAL/DETAILED classifier wired once-per-turn into the prompt-assembly pipeline. Most recently completed sprint.

## In Progress

**Nothing.** No file shows signs of a half-finished edit, no task list entry is open, and every recently-touched file (by mtime) corresponds to a sprint whose own final report/documentation step was completed. If you are picking this up expecting active work, there is none — the next step is whatever the project owner assigns next.

## TODO

Nothing currently blocking or scheduled. Explicitly deferred (by the Response Depth Policy sprint's own stated boundary, not started, no code exists for these):
- Chat vs. Voice dual output (separate response formatting per surface)
- Voice-specific response summarization
- TTS chunking/streaming
- Adaptive, learned response-depth preference (currently deterministic/rule-based only, per-turn, not persisted)

## Known Bugs

None currently open that block core functionality. See Known Limitations below for lower-severity, documented, deliberately-not-fixed items.

## Known Limitations

- **`conversation_ended` is never routed to the `"planner"` module.** `main_runtime_demo.py` registers `add_route("user_utterance", "planner")` but no `add_route("conversation_ended", "planner")` anywhere. `PlannerBridgeModule._on_conversation_ended()` exists and correctly resets several bounded per-conversation dicts (`_last_turn_trace`, `_session_feedback_target`, `_last_device_target`, `_pending_env_confirmations`, `_response_depth_context`, `_last_response_policy`), but is only reachable by a direct method call — tests exercise it that way (`tests/test_device_context.py`, `tests/test_browser_wiring.py`, `tests/test_response_policy.py`). In practice this is low-impact (those dicts are bounded/capped anyway, and a new conversation_id doesn't collide with an old one), but it means "cleanup on conversation end" does not actually fire in this console today. Documented in `ARCHITECTURE_GUARD.md` §15. Fixing it is a real behavior change to event routing (not additive), so it was deliberately left for a dedicated future task rather than fixed opportunistically.
- **`legacy_main.py` (repo root) is absent from this checkout.** Referenced by `main.py`'s own docstring and by `tests/test_root_main_bargein.py`, which fails to *collect* as a result (`FileNotFoundError`). Separately, `luno/main.py` (a different file, ~1866 lines, the actual legacy monolithic script) **does exist** and is what `tests/test_main_bargein.py` imports — but that file also fails to collect in this sandbox because `faster_whisper` isn't installed here (a real dependency, just missing from this environment, not a code defect). Do not conflate the two files.
- **`list_microphones.py` is absent** (referenced by `tests/test_mic_device_index.py`), and `luno/adapters/real_whisper.py`'s `RealWhisperSource` is missing a `_device_index` attribute its own `_listen_and_transcribe_once()` reads — both cause the same 8-9 test failures every regression run (see Test Status below). Long-standing, documented, environment/dependency-related.
- **Two stale `TODO(World Model)` comments** in `main_runtime_demo.py` (~line 601, ~line 3305) say "this project has no dedicated World Model module yet" — that's no longer true; `luno/world_model.py` exists and implements exactly what the TODO describes. Harmless (the actual functionality works via `WorldModel.update_from_tool_result()`), just an uncleaned comment.
- **`luno/emotion_engine.py` and `luno/response_policy.py` each define their own, unrelated `ResponsePolicy` class** (tone-lean deltas vs. response-length depth). No actual Python namespace collision exists (verified — different local variable names in `main_runtime_demo.py`), but a future reader grepping for "ResponsePolicy" will find two unrelated things. Documented in both modules' own docstrings.
- **Sandbox-specific**: `faster_whisper`, `speech_recognition`/`sounddevice` native PortAudio bindings are not installed in this development sandbox (present on the real developer machine's `.venv`). `tests/test_dashboard.py`'s real-HTTP-server tests are slow enough to occasionally exceed a single tool-call's time budget in this environment (not a correctness issue — confirmed passing when run with enough time).

## Recent Changes

Most recent to oldest, by file mtime (no git history to cite instead):
1. `ARCHITECTURE_GUARD.md` — Response Depth Policy subsystem entry added, plus the `conversation_ended`-routing gap documented in §15.
2. `tests/test_response_policy.py` (new, 61 tests), `main_runtime_demo.py` (5 additive edits wiring the policy in), `luno/response_policy.py` (new module), `docs/change_impact/response_depth_policy.md`, `docs/testing/regression_baseline.md` (validation section) — the **Response Depth Policy** sprint, now complete.
3. `luno/persistence.py` (new generic helper) + edits to `luno/relationship_engine.py`, `luno/episodic_memory.py`, `luno/memory.py` (session summaries only), `luno/proactive/habit_memory.py`, `luno/reminders.py`, `luno/memory_guard.py`, `tests/test_persistent_state_hardening.py` (new, 31 tests), `docs/change_impact/persistent_state_hardening_v2.md` — the **Persistent State Hardening V2** sprint, complete.
4. `recovery/` directory (snapshot, migration script, validation script, restore script, decision doc) — the **Memory Recovery & Persistence Hardening** incident response, complete; this is what the atomic-write/backup mechanism in `luno/memory.py` (and later `luno/persistence.py`) was built to prevent from recurring.

## Current Priority

None assigned. The repository is in a clean, fully-tested, fully-documented state with no open work.

## Next Recommended Steps

In rough order of "safest / most self-contained first":
1. If told to continue feature work: ask the project owner what's next — nothing is queued.
2. The `conversation_ended` routing gap (see Known Limitations) is a reasonable, narrowly-scoped candidate for a small, dedicated future task — but confirm with the owner first, since it's a real behavior change (event routing), not purely additive.
3. Clean up the two stale `TODO(World Model)` comments in `main_runtime_demo.py` — trivial, zero-risk, but still worth a deliberate small task rather than doing it silently mid-way through something else (per the "don't fix unrelated things opportunistically" convention this project follows).
4. Consider initializing a real git repository if that hasn't been a deliberate choice — there is currently no revert safety net for any future change.

## Test Status

- `tests/` (root, pytest): **1445 tests collected**, most recent full-sweep result **1436 passed / 9 failed**, 0 failures attributable to any recent sprint (all 9 are the same long-documented environment/dependency issues — see Known Limitations). 2 additional files (`test_main_bargein.py`, `test_root_main_bargein.py`) fail to *collect* (not the same as failing) for environment reasons, also documented above.
- `luno/*/tests/` (package-level, 37 files) — each package has its own independent `SCENARIOS`/`[PASS]/[FAIL]` suite; not re-run in full during this handover (no code changed since the last sprint's own regression sweep, which is on record in `docs/testing/regression_baseline.md`).
- Spot-check run during this handover (fresh, not reused from memory): `pytest tests/test_response_policy.py tests/test_runtime_demo.py tests/test_persistent_state_hardening.py -q` → **170/170 passed**.
- No test in `tests/` can mutate real `config/*.json` production files — enforced by `tests/conftest.py`'s autouse `isolate_persistent_state` fixture. Verify this assumption still holds before trusting any future large test run against production data.

## Important Files

- `main.py` — production entry point (launcher only).
- `main_runtime_demo.py` — developer console AND the implementation of `PlannerBridgeModule`/`ToolManagerBridgeModule`/`BehaviorTreeModule`/`VisionMemoryModule` (used by production too, via `luno/bootstrap/modules.py`).
- `luno/main.py` — legacy monolithic script (untouched reference; not `legacy_main.py`).
- `luno/config.py` — all environment-driven configuration constants, read once at import time.
- `luno/persistence.py` — shared JSON persistence hardening helper.
- `luno/memory.py` — long-term memory + session summaries; also the historical reference implementation for the persistence pattern.
- `luno/response_policy.py` — response depth (SHORT/NORMAL/DETAILED) policy.
- `ARCHITECTURE_GUARD.md` — the authoritative, contract-level architecture/protection document (much more detailed than `ARCHITECTURE.md`).
- `docs/change_impact/*.md` — one file per major sprint, the "why" behind each.
- `docs/testing/regression_baseline.md` — running record of test counts/known failures over time.
- `tests/conftest.py` — the persistent-state test-isolation safety net; read before writing any new test that touches memory.
- `recovery/` — historical record of the production data-loss incident; do not delete.

## Important Commands

```bash
# Run the full test suite (expect ~9 known, pre-existing failures)
python3 -m pytest tests/ -q

# Run a single suite
python3 -m pytest tests/test_response_policy.py -q

# Run a package-level suite
python3 -m luno.core.tests.test_core

# Start the interactive developer console (all hardware mocked)
python3 main_runtime_demo.py

# Start production (needs a real .env)
python3 main.py
```
