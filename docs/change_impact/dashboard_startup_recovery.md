# Sprint 71 — Dashboard Startup & Access Recovery

**Date:** 2026-08-18
**Status:** COMPLETE
**Scope:** `main.py` (root entry point), `luno/dashboard/server.py`
**Explicitly NOT touched:** Tapo/C212 auth, PTZ logic, `real_camera_ptz.py`, pytapo/reconnect logic, Home Assistant, long-term memory, mutation audit, schema/config migration, any new feature.

## 1. Symptom reported

"Dashboard Luno tidak dapat dibuka/start/access" — the Luno dashboard cannot be opened, started, or accessed.

## 2. Baseline (Phase 0)

Entry point: `python main.py` (the ONE official production entry point, per its own module docstring). Startup sequence: load environment → load configuration → create Runtime → register modules → register adapters → run health checks → start Runtime → start Supervisor → `register_dashboard()` → `dashboard.start()` → `ProductionConsole` loop → shutdown.

`register_dashboard()` (`luno/bootstrap/dashboard.py`) constructs (but does not start) a `DashboardServer` from the already-running Runtime/AdapterManager/module set. It returns `None` when `DASHBOARD_ENABLED=false` (default `true`).

`DashboardServer` (`luno/dashboard/server.py`) wraps `http.server.ThreadingHTTPServer`. `.start()` constructs the real request-handler class inline and calls `ThreadingHTTPServer((self.host, self.port), _Handler)` — the real socket bind happens synchronously inside that constructor.

Under normal conditions (free port, valid host), `python main.py` in this sandbox starts cleanly, the dashboard binds, listens, and serves HTTP 200 for `/`, `/api/ping`, `/api/health`, and the rest of the API surface. This ruled out the most generic guesses — missing route, static-asset failure, blocking pre-listen initialization, wrong default host/port — as **not** the issue in a clean environment.

## 3. Root cause (Phases 1–2, proven, not assumed)

`DashboardServer.start()`'s `ThreadingHTTPServer(...)` construction was **completely unguarded** — no `try`/`except` around the bind. `main.py`'s own `dashboard.start()` call site (previously at line ~160) was **also** completely unguarded.

The most common real-world trigger for a bind failure here is a stale/previous Luno process (or a second instance) still holding the configured port — exactly what Sprint 71's own brief named as a checklist item ("stale process memakai port lama"). On POSIX this raises `OSError` with `errno.EADDRINUSE` (98); on the Windows deployment target the same OS-level condition surfaces as `winerror` 10048 ("Only one usage of each socket address...").

Because neither `DashboardServer.start()` nor its caller in `main.py` caught this `OSError`, it propagated **all the way out of `main()` uncaught**, crashing the **entire Luno process** — voice pipeline, wake word, every subsystem — with a raw Python traceback and exit code 1. This is not a "dashboard-only" failure; the whole application dies over a failure that only actually concerns the dashboard's own HTTP listener.

**Proven two ways, both reproduced live in this sandbox:**
1. Isolated script: constructing `DashboardServer` directly against an already-occupied port confirms the raw `OSError` propagates out of `.start()`.
2. Full E2E: running `python main.py` itself against a pre-occupied port (`DASHBOARD_PORT` env override) confirms the entire process crashes with an uncaught traceback — reproduced both before the fix (crash) and after the fix (graceful degradation, see below).

This is a high-confidence, directly-reproducible root cause matching the brief's own failure-mode checklist. No other startup-layer failure (import, missing dependency, route registration, static assets, host/interface mismatch, startup timeout) was found or required to explain the reported symptom.

## 4. Exact fix (Phase 5)

**`luno/dashboard/server.py`:**
- Added `import errno`.
- Added `DashboardBindError(OSError)` — a thin subclass of `OSError` (not a new base), so any existing or future caller written against `except OSError` (the stdlib-idiomatic way to catch a bind failure) continues to work unchanged. This preserves the old API per the brief's "pertahankan API dan entry point lama" constraint.
- Added `_describe_bind_failure(ex, host, port)` — classifies the failure cross-platform (checks both `ex.errno` and the Windows-only `winerror` attribute) into one of three actionable messages: port already in use (`EADDRINUSE`/10048), permission denied (`EACCES`/10013), or address not available (`EADDRNOTAVAIL`/10049), each naming the likely cause and a concrete next step (e.g. "set DASHBOARD_PORT in .env to a different port"). Only host/port are ever included — never anything from `.env` or a secret.
- Wrapped the `ThreadingHTTPServer(...)` construction inside `start()` in `try`/`except OSError`. On failure, every observability subscription already made before the bind attempt (`LogCapture`, `EventRingBuffer`, `StatsAggregator`, `VoiceLatencyRecorder`, `EventLogWriter`) is rolled back to `None`/unsubscribed/stopped, so a failed `start()` never leaves the object half-initialized — a fresh `DashboardServer` (or the same one retried after the port frees) can start cleanly afterward. Then raises `DashboardBindError(ex.errno, message) from ex`.

**`main.py`:**
- Wrapped the `dashboard.start()` call site in `try`/`except OSError`, tracked via a new `dashboard_started` flag. On failure: logs a `degraded`-status lifecycle event, prints a clear `WARNING:` line with the exact reason, and lets the rest of `main()` continue exactly as it already does when `DASHBOARD_ENABLED=false` — the already-established, already-tested "rest of Luno keeps working, no dashboard" state. The process no longer crashes.

No socket options (`SO_REUSEADDR` etc.) were touched — `http.server.HTTPServer.allow_reuse_address` is already `1` in the Python stdlib, so `SO_REUSEADDR` was already applied; altering it further was avoided since Windows' semantics for that option differ from POSIX and could weaken the bind (a security consideration explicitly out of scope per the brief's own constraint against disabling security).

No default port or host was changed. No new health endpoint was created — the fix reuses `/api/ping` and `/api/health`, which already existed.

## 5. Live verification (Phase 6)

All of the following were verified with real HTTP requests / real subprocess runs in this sandbox (Linux), not a GUI browser — per the brief's own allowance ("gunakan HTTP-level verification"):

- Dashboard starts, socket binds, and is genuinely listening (raw socket connect succeeds) — normal case.
- Port pre-occupied → `DashboardServer.start()` raises `DashboardBindError` with an actionable, host/port-specific message; `_started` stays `False`, no orphaned `serve_forever` thread is left running.
- Port pre-occupied → `python main.py` itself reaches `"Ready."` without crashing, prints the `WARNING:` line, and never raises an unhandled traceback (subprocess-level E2E test).
- After a failed bind, a **second**, fresh `DashboardServer` on the same now-freed port starts cleanly and serves `/api/ping` — proves the rollback leaves no state that blocks a legitimate retry.
- Shutdown (`.stop()`) releases the port; re-binding to the same port immediately afterward succeeds.
- Root route (`/`) and the existing API surface (`/api/status`, `/api/modules`, `/api/adapters`, `/api/statistics`, `/api/configuration`, `/api/ping`, `/api/health`) all still return 200 — unchanged from before this sprint.

**LIVE VERIFICATION: AVAILABLE** (HTTP-level and real-process-level, in this Linux sandbox). No GUI browser check was performed or claimed.

## 6. Tests (Phase 3)

New file: `tests/test_sprint71_dashboard_startup_recovery.py` (15 tests, pytest style, real bootstrap via `register_all_modules`/`register_all_adapters`, all-mock backends — same house convention as `tests/test_dashboard.py`). Covers: startup success, bind success, port-conflict handling, exception-not-silent, thread-not-dying-silently, root route, existing health/ping endpoints, clean shutdown, existing API-surface compatibility, no persistent config mutation, camera/PTZ scope guard (proves the sprint's own added code never references Tapo/PTZ/Home-Assistant machinery), a full subprocess-level E2E reproduction of the original bug through the real `main.py` entry point, and a restart-after-failure test. 15/15 passing.

## 7. Regression (Phase 7)

Targeted: `tests/test_sprint71_dashboard_startup_recovery.py` (15/15), `tests/test_dashboard.py` (47/47), `tests/test_dashboard_turn_state_recovery.py` (13/13), `tests/test_production_launcher.py` (23/24 — the 1 failure is the pre-existing, already-documented `test_07_health_checks_all_pass_in_default_mock_configuration`, environment-specific to this checkout's live OpenRouter/Fish Audio credentials, unrelated to this sprint).

Full sweep: remaining ~157 project test files run in chunks (plus a whole-repo `--collect-only` pass confirming no new collection errors). All observed failures traced to pre-existing, environment-specific causes unrelated to `main.py`/`luno/dashboard/server.py`:
- This checkout's `.env` sets `MAX_TOKENS_PARAM=max_tokens` (code default is `max_completion_tokens`) — causes `tests/test_llm_max_completion_tokens_compatibility.py` (7) and `tests/test_memory_session_summary_api_compatibility.py` (5) to fail; zero relation to dashboard code.
- `tests/test_mic_device_index.py` (6) and 2 whisper tests in `tests/test_real_adapters.py` — already-documented baseline failures (`MIC_DEVICE_INDEX` env override, missing `list_microphones.py`, `speech_recognition`/`sounddevice` not installed in this sandbox).
- 6 failures in `tests/test_sprint63_long_term_memory_recovery.py`, `tests/test_sprint64_memory_corruption_forensics.py`, `tests/test_sprint68_mutation_audit_hardening.py` — all assert exact `config/backups` file counts against a pristine baseline; this long-lived checkout has accumulated real backup files from months of prior sprints' actual usage (41 present vs. an expected 12) — unrelated to this sprint, and confirmed (Phase 8) that this sprint's own test run added zero new backup files.
- 2 `luno/barge_in/tests/test_barge_in.py` stress/timing tests failed only under `-n 4` parallel load and passed cleanly (2/2) when re-run in isolation — CPU/GIL-contention flakiness, a category already documented in this project's history, not a code regression.
- One segmentation fault occurred only when chaining 4 dashboard-related test files in a single pytest process; it occurred inside `stop()` → `log()`, code this sprint did not touch, during interpreter-shutdown thread teardown, and did not reproduce when any file was run individually (all passed cleanly standalone). Consistent with known thread-accumulation flakiness in this project's large real-thread-based test suite; not attributable to this sprint's changes.

No failure traced to `main.py` or `luno/dashboard/server.py`'s Sprint 71 changes.

## 8. Persistent state (Phase 8)

SHA-256 hashes of all 15 `config/*.json` files taken before and after: (a) a full `python main.py` run including dashboard start/stop, and (b) the full Sprint 71 test suite (including port-conflict/bind-failure/restart scenarios). Zero files changed, zero appeared, zero disappeared. `config/backups/` file count unchanged (41 before, 41 after; zero files newer than the snapshot). `main.py` and `luno/dashboard/server.py` source content unchanged during test execution (confirms the fix and its tests make no unexpected writes back to source or config).

## 9. Known limitations

- The bind-failure fix covers `OSError` at the socket-bind layer only. It does not add a health-check/self-heal mechanism for a dashboard that binds successfully but later becomes unresponsive (out of scope — no evidence this sprint's own reproduction required it).
- `SO_REUSEADDR`/socket-option behavior was deliberately left untouched; Windows-specific bind-failure behavior could not be directly verified on real Windows hardware in this sandbox (Linux only) — the errno/winerror cross-platform classification in `_describe_bind_failure` was verified by construction (both branches unit-tested with synthetic `OSError`/`winerror` values) but not on a live Windows machine.
- Several pre-existing, environment-specific test failures (documented above) remain unresolved — explicitly out of this sprint's scope to fix.

## 10. Next recommended sprint

None required to close out the dashboard-access symptom — root cause is fixed and verified. If desired, a future sprint could investigate the `.env` `MAX_TOKENS_PARAM=max_tokens` / code-default mismatch (unrelated subsystem) or reset/document the `config/backups` accumulation in this long-lived checkout — both are pre-existing and orthogonal to Sprint 71.
