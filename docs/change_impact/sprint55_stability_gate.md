# Sprint 55 — Full Verification & System Stabilization

**Status:** COMPLETE (verification/hardening sprint — one real test-reliability
defect found and fixed; zero production regressions found; two verification
items are explicitly marked NOT VERIFIED / NOT POSSIBLE rather than faked).

**Scope:** Sprint 55 was a stability-gate sprint, not a feature sprint. No
production behavior was intentionally changed except the one dashboard
turn-state test-reliability fix documented in Phase 3 below. This document
records what was checked, how, and the exact result — including the
negative/uncertain results — per the sprint's own "no fabricated numbers,
mark unverifiable items NOT VERIFIED" rule.

## Phase 0 — Takeover / reconnaissance

Read (in order) `docs/project_handover.md`, `docs/project_handover.json`,
`ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`, and the
Sprint 52/53/54 change-impact docs. Cross-checked every claim against actual
source:

- `luno/bootstrap/adapters.py` constructs `LLMManagerAdapter` in production
  (not `luno.adapters.openrouter.OpenRouterAdapter` directly) — confirmed
  still true, matching the Sprint 54 finding.
- `luno/adapters/llm/base.py`'s max-completion-tokens/API-compatibility fix
  (Sprint 54) is present in the staged source.
- `luno/memory.py`'s Session Summary compatibility handling (Sprint 53) is
  present.
- `luno/wake_session/manager.py` / `session.py` reviewed line-by-line —
  `ConversationSession` is a pure, lock-guarded, dependency-free state
  machine with a bounded `history` deque; no class-level/global mutable
  state (ruled out as a cross-test-pollution source).

Pre-change baseline recorded: SHA256 of all 15 `config/*.json` files taken
directly from the real device *before* any file was staged into the cloud
workspace, git status on the device checkout (no `.git` present — matches
prior sprints), and the existing documented baseline failure list from
`docs/testing/regression_baseline.md`.

## Phase 1 — Full regression baseline

Staged the complete `luno/` (271 `.py` files) and `tests/` (105 files) trees
plus `main_runtime_demo.py` and `luno/dashboard/static/index.html`, installed
the full `requirements.txt` chain (including the heavy
torch/ultralytics/faster-whisper dependency chain that previous sprints could
not install), and ran the sweep in 12 parallel chunks.

**Collected: 3880 tests** (previous sprints' best was ~788–2900 in narrower
hand-assembled dependency chains — this is the largest and most complete
sweep this project has ever run).

**Result: 3866 passed, 10 failed, 4 skipped.**

Every failure was re-run in isolation and root-caused — none were classified
as "baseline" without proof:

| # | Test | Classification | Root cause |
|---|------|----------------|------------|
| 1 | `test_dashboard_turn_state_recovery.py::test_05_e2e_repeated_failure_recovery_cycle_stays_usable` | **FIXED this sprint** | Test's own live-state polling raced a zero-delay mocked round trip that could complete a full turn cycle faster than the poll interval could observe it — see Phase 3. |
| 2 | `test_llm_tts_streaming_production.py::test_13_latency_regression_default_vs_streaming_corrected_design` | Confirmed timing flake | Passes standalone (12.58s) in 3/3 reruns; fails intermittently only inside the full-file run under this sandbox's CPU constraints — matches the project's pre-existing documented TTS/streaming timing-flake class. |
| 3–5 | `test_state_isolation.py` (3 tests) | 2 of 3: sandbox packaging collision (root-caused, not previously precisely identified); 1 of 3: known flake | The `ultralytics` pip package ships its own top-level `tests/` package that shadows the repo's local `tests/` package for absolute imports (`from tests.conftest import ...`). Confirmed by moving `/usr/local/lib/python3.11/dist-packages/tests` aside — 2 failures disappeared immediately. The third (`test_planner_turn_thread_can_genuinely_outlive_console_stop`) is the same pre-existing GIL/thread-scheduling flake documented since Sprint 43–50. |
| 6 | `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state` | Environment-specific | Asserts `LLM_PROVIDER == "openai"`; requires the real `.env` (deliberately never loaded this sprint — dummy credentials only). |
| 7 | `test_production_launcher.py::test_07` | Environment-specific | "Configuration" health check requires real OpenRouter/Fish Audio credentials; fails identically with dummy credentials, matching the existing documented baseline. |
| 8–9 | `test_real_adapters.py` (2 tests) | Deferred, pre-existing, out of scope | `AttributeError: 'RealWhisperSource' object has no attribute '_device_index'` — the test manually constructs the object via `__new__` (bypassing `__init__`) without setting an attribute the production code now requires. Newly *exposed* (not newly *caused*) now that `speech_recognition` is installed for the first time in this project's sprint lineage. Out of scope for Sprint 55/56 (LLM/dashboard/HA/TTS focus) — documented as a Sprint 57+ finding, not fixed. |
| 10 | `test_mic_device_index.py` (4 sub-failures counted as part of the above categories where applicable) | Environment-specific | Root cause corrected from prior documentation: a root-level `list_microphones.py` utility script does not exist in this checkout at all (confirmed via a full device-root directory listing), same absent-file class as `legacy_main.py`. |

**Net result: zero genuine new regressions.** One real test-reliability
defect found and fixed (Phase 3). All other failures are confirmed
environment-specific, confirmed flakes, or pre-existing out-of-scope gaps.

## Phase 2 — Sprint 53/54 live API verification

**LIVE VERIFICATION: NOT POSSIBLE.**

The cloud sandbox's network egress is blocked to arbitrary hosts (only
allowlisted package registries are reachable). This was confirmed two ways,
not merely assumed:

1. A direct `curl` to an external host failed (exit 56, connection refused).
2. A real test run in which `luno.adapters.openrouter` genuinely attempted a
   live HTTPS call to `openrouter.ai` failed with
   `ProxyError('Unable to connect to proxy', OSError('Tunnel connection
   failed: 403 Forbidden'))` — i.e. the code path executed exactly as
   production would, and the network layer itself rejected the call.

No live provider/HA smoke test could be run this sprint. This is recorded
honestly rather than faked. The Sprint 53/54 fixes were instead verified by
static source inspection (Phase 0) plus the full mocked-adapter regression
suite (Phase 1), which does exercise every code path up to (but not
including) the actual socket call.

## Phase 3 — Dashboard turn-state recovery

Verified via `tests/test_dashboard_turn_state_recovery.py` (13 tests) and
`tests/test_dashboard_turn_state_recovery_ttspath.py` (never previously run
in this project's sprint lineage as far as this session's records show — now
confirmed passing). Combined: **18/18 passing**, covering: normal turn,
planner exception, LLM error, LLM cancellation, TTS failure before/after
playback, playback cancellation, dashboard disconnect, repeated
failure→recovery→new-command cycling, and shutdown mid-turn.

Core invariant re-verified: **no terminal failure leaves the session
permanently stuck in THINKING.**

### The one real finding and fix

`test_05_e2e_repeated_failure_recovery_cycle_stays_usable` failed
deterministically (100% of reruns) when run after `test_01` in the same
process, but passed standalone. Direct Python reproduction (bypassing pytest
entirely for clean, unbuffered evidence) showed the *actual* system recovers
correctly every time — the test's own `_wait_until(lambda: _state(console)
== "thinking", 12.0)` helper polls the *live* state every 20ms, and a
zero-delay mocked LLM+TTS round trip (especially in an already-warmed-up
interpreter, i.e. the second+ `RuntimeDemoConsole` constructed in the same
process) can complete the *entire* THINKING→...→WAITING_USER cycle between
two poll samples. The test was asserting on a live snapshot that had already
moved past the state it was checking for.

This is the *opposite* of a stuck-at-THINKING bug — it is proof recovery
happens, just faster than a coarse poll can observe mid-flight.

**Fix (test file only, no production code changed):** added a
`_reached_state_since(console, state_value, since)` helper to
`tests/test_dashboard_turn_state_recovery.py` that checks
`session.history` (the fact that a transition into that state happened at or
after a given wall-clock time) instead of racing a live poll, and used it as
an `or`-fallback alongside the existing live check in
`_one_normal_turn()`/`_one_failing_turn()`. Verified: `test_01` → `test_05`
(previously failed 100%) now passes 3/3 reruns; full file serial run: 13/13
passing; combined with the TTS-path file: 18/18 passing.

## Phase 4 — TTS path verification

`luno/adapters/fish_audio.py` and `luno/wake_session/manager.py` reviewed;
`test_dashboard_turn_state_recovery_ttspath.py`'s TTS-failure-before-playback
and TTS-failure-after-playback scenarios both pass, confirming the
THINKING→TTS-starts→TTS-fails→terminal-recovery path holds. No arbitrary
timers were added or found to be needed.

**Non-blocking observation (not a correctness bug, flagged for Sprint 57):**
`fish_audio.py`'s stream worker threads (`_play_stream`-style methods) block
on `queue.get(timeout=self._STREAM_POLL_INTERVAL_S)` until an internal
30-second idle timeout fires. When a test process exits with an abandoned
stream still active, Python's `ThreadPoolExecutor` atexit hook joins these
before full interpreter shutdown, adding up to 30s of *process* teardown time
after pytest has already printed its summary. This does not affect test
correctness or results (confirmed by capturing unbuffered output and
grepping the summary line independently of the outer process's exit), but is
worth a bounded-shutdown follow-up in a future sprint.

## Phase 5 — Observability / logging validation

Unit suites: `tests/test_runtime_observability.py`,
`tests/test_real_world_capture.py`, `tests/test_replay_engine.py` — **47/47
passing**.

**Real end-to-end capture → approve → replay → diff cycle**, run against a
real `RuntimeDemoConsole` (not just unit-level mocks), entirely against a
scratch directory (`/tmp/sprint55_realworld_e2e`), never touching
`tests/real_world/`:

1. Started a real console, forced wake, published a real
   `speech_recognized` event.
2. `console.mark_test(note=..., scenario=..., base_dir=SCRATCH)` → captured
   case `real_000001` with `status="candidate"`, file written to
   `<scratch>/candidate/real_000001.json`.
3. `set_case_status(case_id, "reviewed", base_dir=SCRATCH)` →
   `set_case_status(case_id, "approved", base_dir=SCRATCH)` — both
   succeeded, file moved directories correctly.
4. `load_case(case_id, base_dir=SCRATCH)` → loaded the approved case.
5. `replay_case(approved_case)` → returned a `ReplayResult` with
   `verdict="REVIEW"` (correct: no annotated expected behavior was set on
   this probe case, so REVIEW — not a false PASS or a crash — is the
   correct verdict).

**Confirmed replay never invokes a real LLM:** the replay run's log output
shows no `openrouter` request lifecycle lines and no proxy/network errors
(contrast with Phase 2, where a *real* LLM call attempt produced a visible
403 proxy rejection) — replay operated purely from the captured case data.

**Diagnostic note on the API surface** (useful for future agents): neither
`luno.test_capture` nor `luno.replay` exposes a monkeypatchable
`REAL_WORLD_DIR` module constant — the actual constant is
`test_capture.DEFAULT_BASE_DIR`, used only as a function *default parameter
value* (bound at `def` time, not read fresh per call), and
`main_runtime_demo.py`'s `RuntimeDemoConsole.mark_test()` has its own
independent `base_dir: str = "tests/real_world"` parameter. The correct way
to redirect capture/replay I/O for a scratch probe is to pass `base_dir=`
explicitly through every call (`mark_test()`, `set_case_status()`,
`load_case()`), not to monkeypatch a module attribute.

**Cleanup:** the scratch directory was removed after the probe. See Phase 6
for a real, related finding this probe surfaced.

## Phase 6 — Persistent state safety

Compared all 15 `config/*.json` files byte-for-byte (SHA256) between the
device's real files and this session's working copy, both before and after
the entire Phase 1 regression sweep plus every manual probe run this sprint.

**Finding:** 14 of 15 files matched byte-for-byte throughout. One file,
`config/relationship_state.json`, was found to differ after Phase 5's manual
E2E probe. Root cause: the probe constructed a real `RuntimeDemoConsole()`
directly via a raw `python3` script — **not** through pytest — and so never
benefited from `tests/conftest.py`'s autouse `isolate_persistent_state`
fixture (which redirects config file paths to `tmp_path` for anything
running under pytest). The console processed a real conversational turn and
correctly, by design, updated `relationship_state.json` in place
(`interaction_count` 1141→1152, `trust`/`last_interaction_timestamp`
updated) — this is expected production behavior for a manually-run console,
not a bug. The file was restored to the exact original bytes (verified via
base64 round-trip against the live device file, SHA256-confirmed identical)
before this document was written. **The real device file was never touched
by any part of this session** — only this session's own cloud-workspace
scratch copy was affected, and it has been fully restored.

**All 15 `config/*.json` files verified byte-identical to the device
originals as of the end of this sprint.**

This is recorded as a methodology note: any future non-pytest manual probe
against a real console must either run inside a pytest context (to get
`isolate_persistent_state` for free) or pass explicit scratch paths for
every persistent-state file the console touches, exactly as this sprint
already did correctly for the real-world capture directories.

## Phase 7 — Performance

Measured the lifecycle/observability paths actually touched or verified
this sprint, against the <5ms/op target:

| Path | Result |
|------|--------|
| `ConversationSession.transition_to()` | 0.0018 ms/op (5000 ops) |
| `EventBus.publish()` (synchronous call overhead) | 0.013 ms/op (3000 ops) |
| `EventLogWriter._on_event()` (redact + real JSONL write + real text-log write, actual disk I/O) | 0.048 ms/op (1000 ops) |

All three are more than two orders of magnitude under the 5ms/op target.

## Phase 8 — Documentation

This document. `ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`,
`docs/project_handover.md`, and `docs/project_handover.json` are being
updated with a Sprint 55 section/entry in the same delivery as this file.

## Honest summary of what was and wasn't verified

- **Verified for real, with evidence:** full regression sweep (3880
  collected), dashboard turn-state recovery (18/18, one real fix), TTS
  failure paths, observability unit tests (47/47) plus a genuine E2E
  capture→approve→replay cycle, persistent-state safety (byte-for-byte,
  including catching and correctly explaining a self-inflicted deviation),
  performance on the paths this sprint actually touched.
- **Explicitly NOT verified / NOT possible:** live LLM/HA provider smoke
  tests (network egress blocked — proven, not assumed).
- **Out of scope, deferred, documented, not fixed:** `test_real_adapters.py`'s
  `_device_index` gap (pre-existing test-code issue, unrelated to Sprint
  55/56's focus areas).
- **Surfaced but out of scope:** the real device's
  `config/long_term_memory.json` is not valid UTF-8/plain JSON (looks
  encrypted or corrupted) and is silently falling back to an empty
  long-term-memory store in production. `luno/memory.py` already handles
  this gracefully (no crash), but it likely means real long-term memory data
  is not currently loading for the user. This is unrelated to Sprint 55/56's
  LLM/dashboard/HA/TTS scope and was not touched, but is flagged here
  prominently as a significant real-world finding worth a dedicated future
  sprint.
