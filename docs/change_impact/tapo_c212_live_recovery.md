# Sprint 70 (Tapo C212 Live Authentication & Auto-Recovery) - Change Impact

**Status:** TARGETED FIX SHIPPED, DIAGNOSIS ONLY for the live-camera
question (see LIVE VERIFICATION below - this sandbox still cannot reach
a real Tapo camera, exactly as Sprint 69 documented).

## Scope

Builds directly on Sprint 69 (`docs/change_impact/
tapo_c212_authentication.md`), which added evidence-based FAILURE
CLASSIFICATION (`classify_tapo_exception()`) but deliberately left the
underlying connection itself un-recoverable - a classified failure was
still just... a failure, reported and done. This sprint adds the
missing piece: a small, bounded, in-memory connection STATE machine
plus a single-retry AUTO-RECOVERY path, wired into the existing
`RealCameraPTZHandler` without introducing any new architecture.

## Phase 0 - baseline

Before any change: `tests/test_sprint69_tapo_c212_auth.py` (27) +
`luno/tool_manager/tests/test_camera_ptz.py` (32) + `tests/
test_camera_ptz_bootstrap.py` (5) = **59 passed, 0 failed**. `config/
*.json` (27 files) SHA-256-hashed. Grepped `real_camera_ptz.py`/
`camera_ptz.py`/`luno/bootstrap/adapters.py` for any hardcoded
credential value - none found.

## Phase 1 - live connection diagnosis

**Attempted, and categorically NOT POSSIBLE from this sandbox**, for
the exact same reason as Sprint 69: `luno.config.TAPO_HOST`/
`TAPO_USERNAME`/`TAPO_PASSWORD` are all unset here (confirmed via direct
attribute inspection), there is no `.env` file in this checkout, and
even with credentials this sandbox's cloud network has no route to a
private-LAN camera. Per this sprint's own explicit STOP CONDITION
("Never fabricate a successful live connection"), no PASS/FAIL result
is claimed here.

**What was shipped instead:** a new, strictly read-only script,
`tapo_ptz_diagnostic.py`, at the repository root - run it on the
machine where `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` are actually
configured:

    python tapo_ptz_diagnostic.py

It attempts exactly ONE real `pytapo.Tapo(host, user, password)`
construction (the library's own real, synchronous auth) and prints the
classified result using the EXACT SAME `classify_tapo_exception()`
function the production tool itself uses - `CONNECTED`, `AUTH_FAILED`,
`SESSION_EXPIRED`, `AUTH_RATE_LIMITED`, `DEVICE_OFFLINE`/
`PORT_UNREACHABLE`/`HOST_UNREACHABLE`, or `UNKNOWN`. It never prints
`TAPO_PASSWORD`/`TAPO_USERNAME` (redacted via the existing
`_redact_credentials()`), never issues a `moveMotor`/`calibrateMotor`/
`savePreset`/`setPreset` call (does not physically move the camera),
and never writes any file. **Running this script on the real machine
IS this sprint's outstanding Phase 1 - its output is the single most
valuable next data point.**

## Phase 2 - separate connection surfaces (re-confirmed unchanged)

Re-ran Sprint 69's own forensic checks: a full-text search of
`real_camera_ptz.py`/`camera_ptz.py`/`luno/bootstrap/adapters.py` for
the literal string `disconnect` still returns zero matches for anything
other than this sprint's own module-comment prose (which discusses the
word for documentation purposes, never emits it in a `ToolResult.
message`); `camera_ptz` still does not appear anywhere in `luno/
dashboard/collectors.py` or `luno/dashboard/static/index.html`. The
three surfaces remain exactly as Sprint 69 documented:

- **A. Tapo PTZ/API connection** - this file, `real_camera_ptz.py`,
  now with the Phase 3/4 state machine and recovery below.
- **B. camera streaming/RTSP/OpenCV connection** - `luno/vision.py`
  (Sprint 69/69.1/69.2's own subject), untouched by this sprint.
- **C. dashboard camera connection/status indicator** - driven entirely
  by (B), never by (A) - `camera_ptz` is a ToolManager tool, not a
  dashboard adapter, so it has no representation in `/api/adapters` or
  the dashboard's Camera badge at all.

**"disconnected" originates from (C), which is driven by (B) - never
by (A).** This sprint does not change that fact; it makes (A)'s own
internal failures more specific and recoverable, which is a different,
complementary improvement.

## Phase 3 - connection state (new)

New `PTZConnectionState` in `real_camera_ptz.py` - a small, in-memory-
only, per-HANDLER-INSTANCE state (`self._connection_state`, never a
module global, never persisted): `DISCONNECTED`, `AUTHENTICATING`,
`CONNECTED`, `SESSION_EXPIRED`, `AUTH_FAILED`, `DEVICE_UNREACHABLE` -
the exact 6 values the brief specified. A constructed client is assumed
`CONNECTED` (matches reality: `pytapo.Tapo.__init__()` already
performed real, synchronous auth to get that far). `_category_to_
connection_state()` maps the finer-grained `TapoErrorClass` (7
categories, used for `error_type`/`data["error_class"]`) down to this
coarser 6-value state (e.g. `HOST_UNREACHABLE`/`PORT_UNREACHABLE`/
`DEVICE_OFFLINE` all collapse to `DEVICE_UNREACHABLE`; `AUTH_FAILED`/
`AUTH_RATE_LIMITED` both collapse to `AUTH_FAILED`) - the finer
category is NOT lost, it is still what drives the retry-eligibility
decision in Phase 4 and still what's reported via `error_type`; the
coarser state is purely an additional, simpler-to-consume observability
surface. Exposed via a new read-only `connection_state()` accessor -
**deliberately not wired into the dashboard** (see Phase 6).

## Phase 4 - auto-recovery (new)

New `_invoke()` method wraps every single `pytapo` client call (`_move`/
`_center`/`_save_preset`/`_goto_preset` all now call
`self._invoke("moveMotor", x, y)` etc. instead of `self._client.
moveMotor(x, y)` directly - the SAME per-action `try/except Exception`
blocks still wrap it, unchanged in shape). On success: state ->
`CONNECTED`, return the result, done. On a failure classified as
**recoverable** (`SESSION_EXPIRED`, `DEVICE_OFFLINE`,
`PORT_UNREACHABLE`, or `HOST_UNREACHABLE` - exactly matching the
brief's own "session expired -> recreate/authenticate", "transient
network disconnect -> bounded reconnect" examples) AND a
`client_factory` is configured: state -> `AUTHENTICATING`, call the
factory to rebuild `self._client`, then retry the SAME call **exactly
once more**. Any other outcome propagates as before.

**Per-category policy** (exactly matching the brief's own Phase 4
list):

| Category | Recoverable? | Rationale |
|---|---|---|
| `SESSION_EXPIRED` | Yes - one bounded reconnect+retry | A fresh authenticated client may simply resolve it |
| `DEVICE_OFFLINE` / `PORT_UNREACHABLE` / `HOST_UNREACHABLE` | Yes - one bounded reconnect+retry | A transient network blip may have cleared |
| `AUTH_FAILED` | No | Wrong credentials will still be wrong - brief's own "do not retry endlessly" |
| `AUTH_RATE_LIMITED` | No | Retrying immediately is actively counterproductive - brief's own "stop retrying and report clearly" |
| `UNKNOWN` | No | "preserve safe failure behavior" - never guess |

**Bounded by construction, not by a counter:** `_invoke()` contains
**zero loop constructs** (`while`/`for`) - verified by an AST-based
static guard (`test_J_invoke_source_contains_no_loop_construct`), the
same style of proof Sprint 69.1 established for its own single-call-
site regression guard. "At most one retry, ever" is therefore true by
the SHAPE of the code, not by a value that could someday be bumped. A
dynamic test (`test_J_bounded_to_exactly_two_underlying_calls_even_
when_both_clients_always_fail`) additionally proves at most 2 total
underlying client calls occur even when EVERY client (original and
reconstructed) always fails. No background polling, no blocking sleep/
backoff loop, no additional HA/network calls of any kind were
introduced - the only new network-capable call is the SAME `pytapo.
Tapo(...)` constructor the bootstrap path already calls once, now
potentially called one additional time, only on a recoverable failure.

## Phase 5 - PTZ integration

The exact flow the brief specified now holds structurally: `"geser
kamera kiri"` -> `luno/planner/parser.py` -> `ToolManager` ->
`RealCameraPTZHandler.execute()` -> `_move()` -> `self._invoke(
"moveMotor", ...)` (the "connection check -> authenticate/recover if
safely possible" step, transparently, inside the existing per-action
`try/except`) -> the real PTZ action -> `ToolResult`. **Nothing bypasses
ToolManager or any existing safety layer** - `execute()`'s own
signature, `validate()`, the base-class dispatch, and the existing
`self._lock` are all completely unchanged; recovery is entirely an
implementation detail of what happens between "call the client" and
"get a result" inside a single already-existing method.

## Phase 6 - dashboard status

**"disconnect" is not, and was never, the same connection state as
this file's own PTZ/API connection** - see Phase 2. This sprint
introduces `connection_state()` as a new observability surface for the
PTZ tool's OWN state, but deliberately does **not** wire it into `luno/
dashboard/collectors.py` or the dashboard HTML - doing so would either
(a) fabricate a claim about the SEPARATE streaming path's connectivity
that this tool cannot back up, or (b) require inventing a new shared
abstraction neither subsystem currently has, both of which the brief's
own Phase 6 instruction ("only unify if there is an existing safe
shared abstraction") forbids absent one. Structural guards (`test_O_
dashboard_collectors_do_not_reference_ptz_connection_state`, `test_O_
camera_ptz_still_not_listed_as_an_adapter`) prove this separation holds
today and will fail loudly if a future change accidentally couples them
without going through this decision again.

## Phase 7 - tests

`tests/test_sprint70_tapo_live_recovery.py` - 23 tests, covering
categories A-O: valid authentication (A), invalid credentials never
retried (B), expired session recovers (C), transient network failure
recovers (D), a permanently unreachable device still fails honestly
after exactly one bounded retry (E), rate-limiting stops retrying (F),
an unknown exception preserves safe failure behavior with no retry (G),
a successful reconnect additionally PROVEN to pass through the
`AUTHENTICATING` state (H), a failing reconnect reports the ORIGINAL
error rather than the reconnect's own exception (I), two independent
no-infinite-retry proofs - a dynamic bounded-call-count test and a
static AST-no-loop guard (J), credential redaction on the new recovery
path plus a check that no state-name value resembles a credential (K),
the mock backend proven completely untouched (L), every PTZ action
proven to still work through the new `_invoke()` path, plus an explicit
backward-compatibility proof for callers that omit `client_factory`
entirely (M), persistent-state immutability - both a static no-disk/db/
eval/exec-surface AST guard and a dynamic config-hash-before/after
check across an actual recovery scenario (N), and dashboard/PTZ status
separation (O). All fakes only.

## Phase 8 - security

- `TAPO_PASSWORD`/`TAPO_USERNAME` never appear in any `ToolResult.
  message` or `data` produced by the new recovery path
  (`test_K_credential_never_appears_in_failure_message_on_recovery_
  path`) - the existing `_redact_credentials()` is applied to BOTH the
  original failure's text and (via the same code path) anything the
  retry might surface.
- No session/auth token is ever read, stored, or logged anywhere in
  this file - `pytapo` manages its own session/`stok` value entirely
  internally; this layer never touches it.
- `target` still cannot override `TAPO_HOST` - unchanged from Sprint
  69, re-verified (`_invoke()`'s `method_name` argument is ALWAYS one
  of 5 hardcoded literal strings at each of its 5 call sites in this
  file - never derived from `tool_call.target`/`tool_call.parameters`).
- The retry/reconnect logic cannot become arbitrary network access -
  `client_factory` is a closure over the exact same 3 already-read
  config values used for the FIRST construction; it is supplied ONLY by
  `luno/bootstrap/adapters.py`, never by any tool-call input, and it
  constructs the SAME `pytapo.Tapo` class every time - no new outbound
  destination of any kind is introduced.
- No new generic filesystem/write capability, and no `eval`/`exec`/
  `subprocess`/shell capability anywhere in this file - verified by an
  extended AST static guard (`test_N_module_source_still_has_no_disk_
  or_db_write_surface`, now also checking `eval`/`exec`) and a direct
  grep (`subprocess`, `os.system`, `shell=True` - zero matches).
- No second camera backend was introduced - `RealCameraPTZHandler`
  still owns exactly one `self._client` at a time; `client_factory`
  only ever reconstructs an instance of the SAME class already in use.

## Phase 9 - performance

Measured directly in this sandbox (1000-iteration average per
scenario, pure in-memory fake clients, no real network I/O):

| Scenario | Avg overhead |
|---|---|
| Normal connected PTZ call | 0.008 ms |
| Reconnect attempt (session expired -> fresh client) | 0.012 ms |
| Failed authentication (no retry) | 0.015 ms |
| Exhausted retry (unreachable both times) | 0.026 ms |

All comfortably below the existing 5ms local-decision-logic target -
these numbers exclude real network/auth latency (which only a live
camera can produce; see Phase 1), consistent with the brief's own
"where applicable" caveat.

## Phase 10 - full regression

**Targeted:** `tests/test_sprint70_tapo_live_recovery.py` (23) + `tests/
test_sprint69_tapo_c212_auth.py` (27) + `luno/tool_manager/tests/
test_camera_ptz.py` (32) + `tests/test_camera_ptz_bootstrap.py` (5) +
`tests/test_camera_health_check_timeout.py` + `tests/
test_camera_presence.py` + `tests/test_sprint69_camera_stability.py` +
`tests/test_sprint69_1_camera_dashboard_forensics.py` = **all passed**
except one full-suite-only timing flake in `tests/
test_sprint69_2_camera_state_machine_hardening.py` (a file this sprint
never touched, unrelated to Tapo/PTZ - it exercises `luno/vision.py`'s
own real-thread-timing-bounded read/open paths). Re-ran that ONE file
alone twice: 23/23 passed both times, ~2.3s each - confirming full-
suite-only scheduling sensitivity, not a regression. `luno/tool_manager/
tests/` + `luno/planner/tests/` = 183 passed, 0 failed (60 pre-existing,
unrelated warnings).

**Full repository sweep:** see the `## Sprint 70` entry in `docs/
testing/regression_baseline.md` for the exact count and established-
baseline comparison - never claimed clean without that comparison.

## Phase 11 - persistent state

`config/*.json` (27 files) SHA-256-hashed before this sprint's first
edit; a dedicated dynamic test
(`test_N_config_json_files_unchanged_across_a_recovery_scenario`)
additionally hashes them immediately before and after actually
EXERCISING the full recovery path (a session-expired-then-reconnect
scenario) inside the test process itself, proving no config drift
occurs even during a real recovery attempt, not just "at rest". No
credential, token, or session value is stored anywhere; no new backup
files were created; `luno/mutation_audit.py` (Sprint 65-67) is
untouched by this sprint (nothing in `real_camera_ptz.py`/`adapters.py`
writes through it, exactly as before).

## Phase 12 - documentation

This document; `ARCHITECTURE_GUARD.md` §73; `docs/testing/
regression_baseline.md`'s new `## Sprint 70` section; `docs/
project_handover.md` §19x/20x and updated §22; `docs/project_handover.
json`.

## Known limitations

- The actual root cause of the user's original "disconnect" report is
  still not conclusively identified from this sandbox - unchanged from
  Sprint 69. This sprint makes the PTZ/API path more resilient and
  observable, which is valuable regardless of the ultimate root cause,
  but is not itself proof of what that root cause was.
- Live verification remains impossible from this sandbox - `tapo_ptz_
  diagnostic.py` is the concrete, ready-to-run path for the user to
  close this gap themselves.
- The eager-construction-at-bootstrap / permanent-mock-fallback-if-that-
  FIRST-construction-fails architecture (Sprint 69's own deliberate
  non-change) is still unchanged - this sprint's recovery only applies
  AFTER a real handler is already successfully registered; a failure at
  the very first bootstrap-time construction still falls back to mock
  for the process lifetime, exactly as before. This remains a
  deliberate, documented boundary, not an oversight.

## STOP CONDITIONS considered

- **"Never fabricate a successful live connection": honored** - no live
  PASS/FAIL is claimed anywhere in this document or the final report.
- **"If live camera authentication cannot be performed, do not claim
  the root cause is fixed": honored** - STATUS below reflects this.
- **"If recovery requires a second connection architecture, STOP and
  document instead": NOT triggered** - the shipped recovery reuses the
  existing single-client architecture exactly (see Phase 4/8).
- **"If any change risks bypassing existing ToolManager/security
  boundaries, STOP": NOT triggered** - see Phase 5/8.

## Next Recommended Sprint

1. **Run `tapo_ptz_diagnostic.py` on the real machine** - the single
   highest-value next step, closing Phase 1's gap for good.
2. If the live result is `AUTH_FAILED`, the fix is credential-side
   (re-pair the Camera Account in the Tapo app) - no further Luno code
   change would help, per this sprint's own explicit "wrong credentials
   won't fix themselves by retrying" design.
3. If the live result is `SESSION_EXPIRED`/`DEVICE_OFFLINE`/
   `HOST_UNREACHABLE`/`PORT_UNREACHABLE` on a REAL command through Luno
   (not just the diagnostic script), the new recovery path should
   transparently resolve it on the very next PTZ command - worth
   confirming with a real "pan camera left" retry.
4. Resume and deliver Sprint 69.2 (`luno/vision.py`'s own OpenCV read-
   bound/backoff/dashboard-state hardening) - still the standing,
   independent, code-complete item deferred since Sprint 69.
