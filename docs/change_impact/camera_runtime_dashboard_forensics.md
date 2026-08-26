# Camera Runtime/Dashboard Disconnect Forensics & Fix (Sprint 69.1)

**Type:** Forensic trace + diagnostics + two structural correctness
fixes + one credential-leak fix, all scoped to the camera open/capture/
status path. No new feature, no change to Home Assistant, memory,
mutation audit, tool registry, browser security, planner, area/group
logic, or unrelated vision behavior.

## The brief

After Sprint 69 shipped, the live dashboard still reported Camera =
DISCONNECTED, and OpenCV still emitted the `cap_ffmpeg_impl` ~30s stream
timeout. The brief's mandatory first step was: do not assume Sprint 69's
implementation is the actual runtime path - trace the complete
production call chain from `scheduled_vision_poll` through to dashboard
telemetry, and answer 10 specific investigation questions before
proposing any fix.

## Forensic trace

**Every camera-touching call site in the repository, traced by direct
source search (not assumption):**

- Repo-wide search for `import cv2` found exactly ONE production file:
  `luno/vision.py`. No other module in the entire codebase imports
  OpenCV.
- Repo-wide AST-based search (not text grep, to avoid docstring/comment
  false positives) for `<x>.VideoCapture(` calls found exactly ONE call
  site: `luno/vision.py::_open_capture_bounded()`, line ~360 (a one-line
  ternary that calls `cv2.VideoCapture(source)` or `cv2.VideoCapture
  (source, backend)` - syntactically two AST `Call` nodes on the same
  line, but architecturally one call site; only one branch ever executes
  per invocation).
- Every camera-touching function anywhere in the project -
  `detect_objects()`, `detect_objects_tracked()`, `_monitor_loop()`
  (the live-preview window), `ask_vision()`, `luno/adapters/real_vision.
  py::RealVisionSource`, and `luno/bootstrap/health.py`'s startup check -
  funnels through this SAME `_capture_frame()`/`probe_camera()` ->
  `_open_camera_with_discovery()` -> `_open_capture_bounded()` chain,
  protected by the same `_camera_lock`. **There is no hidden second
  camera-opening path.**
- `luno/tool_manager/builtin/real_camera_ptz.py` (the Tapo pan/tilt
  control tool) uses the `pytapo` HTTP API exclusively - confirmed it
  never imports `cv2` or touches a `VideoCapture` object. Pan/tilt
  control and video capture are structurally independent in this
  codebase.
- `luno/vision_memory/*.py` was grepped for `cv2`/`VideoCapture`/
  `capture_frame`/`camera` - zero matches. Vision Memory only ever
  receives already-processed text/structured descriptions via its own
  `update()` facade; it never opens the camera itself.
- `scheduled_vision_poll` (the event whose "(0.0ms)" log line appeared
  in the original bug report) was traced to `luno/adapters/scheduler.
  py::SchedulerAdapter` - a generic, config-driven periodic-job
  publisher, unrelated to any specific adapter's business logic. It is
  routed to the "vision" adapter via `EventMapping["scheduled_vision_
  poll"] = ["vision"]` (`luno/adapters/models.py`). Critically,
  `VisionAdapter` **never overrides `handle_event()`** -
  `BaseAdapter.handle_event()`'s documented default is a no-op
  (`"""Internal Event -> external API call. Default: no-op..."""`), and
  `BaseAdapter._process_event()` logs `"'{name}' handled '{event.type}'
  ({elapsed_ms}ms)"` after calling it regardless of whether anything
  happened. **This conclusively explains the "(0.0ms)" log line: it is
  not evidence the vision system polled the camera and found nothing -
  it is evidence of a structurally inert heartbeat event that has never
  done any camera work, even before Sprint 69.** The REAL camera polling
  happens entirely through `RealVisionSource`'s two independent,
  self-scheduling background threads (`_poll_loop()` and `_tracked_
  cycle_loop()`), which are plain Python threads with their own
  `time.sleep()`/`Event.wait()` timing - completely unconnected to the
  Scheduler/EventMapping mechanism.
- The dashboard's `Camera` badge was traced end to end:
  `luno/dashboard/static/index.html` line 840 renders `v.camera_
  connected === true -> connected`, `=== false -> disconnected`, else
  (including `undefined`/`null`) -> `unknown` - the frontend already
  correctly distinguishes "never attempted" from "actually failed" (this
  rules out investigation question 10 - the dashboard is not merely
  showing a stale/default value; a DISCONNECTED badge means `camera_
  connected` is genuinely `false`). That field comes from `luno/
  dashboard/collectors.py::collect_vision()`, a direct passthrough of
  `VisionAdapter._extra_status()["camera_connected"]`, itself set ONLY
  by `VisionAdapter.on_camera_status()` - called by `RealVisionSource.
  _tracked_cycle_once()` with the live result of `vision.camera_
  status()` every cycle. This is a single, live, authoritative path -
  not two independently-derived sources of truth (answers investigation
  question 4: yes, the dashboard reads the exact same state `vision.py`
  produces).

## Answers to the 10 investigation questions

1. **Runtime camera source value:** cannot be determined from this
   sandbox (no access to the user's live `.env`/process). See "What this
   sprint could not determine" below.
2. **Why CAP_FFMPEG is still invoked:** either (a) `camera_source()` is
   genuinely returning a STRING (`CAMERA_URL`, which by this project's
   own deliberate design always uses `[None]`/CAP_ANY, correctly
   reaching FFMPEG for a network stream - see `config.py`'s own Tapo
   auto-derivation, which silently sets `CAMERA_URL` whenever
   `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` are all set, even if the
   user's actual intent for VISION was a local webcam), or (b) the
   running process is not actually executing Sprint 69's `vision.py`
   (stale process not restarted after the update, a deployment/site-
   packages copy shadowing the repo file, or a partial deployment where
   `vision.py` was updated but `config.py` was not, which would raise an
   `AttributeError` reading the new `CAMERA_OPEN_TIMEOUT_S` constant
   rather than reproduce this exact symptom - considered and ruled out
   as an explanation for the FFMPEG log specifically, since that error
   would prevent `cv2.VideoCapture` from ever being reached at all). The
   original bug report's `CAP_OBSENSOR` ("Camera index out of range")
   message is diagnostic evidence that a LOCAL int index was used at
   that time (obsensor only appears in the local-device backend chain,
   never for a string source) - if the CURRENT symptom is genuinely
   identical, that continues to point at (b) over (a), but this sandbox
   cannot prove which without the user's own log output.
3. **Second bypassing open path:** No. Proven by exhaustive repo-wide
   search (see forensic trace above) and encoded as a permanent
   regression guard (`tests/test_sprint69_1_camera_dashboard_forensics.
   py::test_5_exactly_one_production_videocapture_call_site_in_the_
   entire_repo`, AST-based).
4. **Dashboard reads the same state `vision.py` produces:** Yes -
   confirmed by tracing the field from the frontend badge back through
   `collect_vision()` to `VisionAdapter._extra_status()` to `on_camera_
   status()`'s single write site.
5. **Exact state transition producing DISCONNECTED:** `_capture_frame()`
   sets `_camera_connected = False` on any failed open/read, `Vision
   Adapter.on_camera_status()` mirrors it into its own `_camera_
   connected` field on the first real transition, `collect_vision()`
   passes it through unchanged.
6. **Does a failed probe leave an old `VideoCapture` alive:** No - every
   failure path in `_open_capture_bounded()`/`_capture_frame()` releases
   before returning (already covered by Sprint 69's own `test_L_*`/
   `test_F_*` tests; re-verified unchanged this sprint).
7. **Does VisionMemory open/read the camera independently:** No -
   confirmed by direct source search (see forensic trace above).
8. **Does any scheduled/background task create its own camera
   connection:** No - `scheduled_vision_poll` (and the other 3 default
   scheduled jobs: `heartbeat_check`, `memory_cleanup`, `planner_
   cleanup`) are all generic, config-driven periodic publishers with no
   camera-specific logic; only `RealVisionSource`'s own two threads
   touch the camera, both through the single authoritative path.
9. **Is `CAMERA_URL` unexpectedly configured:** Cannot be determined
   from this sandbox - this is precisely the ambiguity the new
   diagnostics (below) are designed to make immediately checkable on
   the user's own machine.
10. **Is the dashboard showing a stale initial status:** No - the
    frontend already correctly renders "unknown" (not "disconnected")
    for a `null`/`undefined` `camera_connected` value; a DISCONNECTED
    badge is real, live evidence that `camera_status()` genuinely
    reported `connected: false` at some point.

## What this sprint could not determine (STOP CONDITION, brief item)

This sandbox has no camera hardware and no Windows/DirectShow/Media
Foundation environment, and has no access to the user's actual running
process, `.env`, or live logs. Given the exhaustive trace above found
**exactly one** production camera-open call site, already using Sprint
69's bounded, explicit-backend mechanism, this sprint could not
reproduce the reported FFMPEG hang from source alone, and could not
conclusively determine whether the runtime symptom is caused by (a) an
intentionally-configured `CAMERA_URL`/Tapo-derived network source
correctly (if unexpectedly) using FFMPEG, or (b) a deployment issue
where Sprint 69's code was not actually the version running when the
symptom was observed. Per the brief's own explicit STOP CONDITION, no
speculative workaround was built for either of these - instead, this
sprint invested in closing the OBSERVABILITY gap that made the question
unanswerable in the first place (see "Diagnostics added" below), so a
single log capture or diagnostic-script run on the actual machine now
settles it definitively.

## Diagnostics added

Every real camera open now logs, via `luno/vision.py`'s new `_log_diag()`
(timestamped, in the module's existing `[Vision]`-prefixed print style):

- `_open_camera_with_discovery()`: one line before iterating candidates
  - source classification and the full ordered candidate-backend list.
- `_open_capture_bounded()`: one line per attempt start (source, backend,
  timeout), one line per outcome (SUCCESS/TIMEOUT/BACKEND_ERROR/
  NOT_OPENED, elapsed ms, and on success the ACTUAL backend OpenCV
  reports via `cap.getBackendName()` - with an explicit warning if it
  doesn't match what was requested, catching the one plausible
  code-level explanation this sprint could not otherwise rule out:
  OpenCV silently falling back to a different backend than the one
  explicitly requested).
- `_capture_frame()`: a `_set_camera_state()` choke point logs every
  REAL state transition (`UNKNOWN -> AVAILABLE`, `AVAILABLE -> BUSY`,
  etc. - never spammed on repeated identical states), and a distinct
  line when a poll tick is skipped entirely because the reopen cooldown
  is still active (making "why didn't this tick even try" directly
  answerable, not just "why did it fail").
- `camera_diagnostic.py` (the read-only script) now also prints the
  platform and the actual backend candidate list that will be requested
  for a local source - checkable in one run whether Sprint 69's fix is
  active on this specific machine's OpenCV build.

**Source classification never exposes credentials or a complete
authenticated URL** (brief's explicit constraint): `_classify_source_
for_log()` renders a local int index verbatim (not sensitive) and
reduces a string `CAMERA_URL` to `"network(scheme=..., host=...)"` -
userinfo (`user:pass@`) is always dropped (never even read) and path/
query are always dropped entirely (a stream key/token could live
there).

## A real, pre-existing credential-leak bug found and fixed

Writing the "no credential leakage" test (required test category 11)
found a genuine bug, independent of anything this sprint added:
`_open_camera_with_discovery()`'s final fallback error message, and two
messages in `_capture_frame()`, built their `reason`/`_camera_last_
error` strings via `f"could not open camera source {source!r}"` /
`f"...{camera_source()!r}"` - the RAW, un-redacted source, which for a
`CAMERA_URL` with embedded credentials would include them verbatim. This
string flows into `camera_status()["error"]`, which `VisionAdapter.
on_camera_status()` publishes as `CameraDisconnected` event data - a
real path for credentials to reach logs/notifications/a future event
history view, dormant only because a failure was usually about a local
index (no credentials to leak) until Sprint 69.1's own new diagnostics
made an equivalent latent bug (this sprint's OWN new state-transition
logging naively passing the same raw `reason` string through) immediately
visible via the new test. Fixed by routing all three sites through
`_classify_source_for_log()` instead of `!r`, and added `_sanitize_
error_text()` - a best-effort redaction applied to any exception's OWN
`str(ex)` message before it is ever stored (an OpenCV exception can
legitimately embed its failing argument, the raw source, in its own
message text - this cannot be perfectly guaranteed for arbitrary
third-party text, hence "best-effort", but real coverage for the
otherwise-unguarded case). `camera_diagnostic.py` had the identical bug
(`print(f"Configured camera_source(): {source!r}")`) and received the
identical fix.

## Two structural correctness fixes

1. **One-cycle status-reporting lag** (`luno/adapters/real_vision.
   py::RealVisionSource._tracked_cycle_once()`): `camera_status()` was
   queried and published BEFORE `capture_frame()` each cycle, meaning
   the reported status always reflected the PREVIOUS cycle's outcome,
   not the one that just ran. Reordered (status now queried/published
   in a `finally` block, AFTER the capture/detect/track attempt) so the
   dashboard/event stream reports the actual result of the cycle that
   just executed - directly the "poll <-> camera state correlation" the
   brief's diagnostics section asks for. At the project's 2fps default
   this lag was invisible in practice (0.5s), but it is a genuine
   correctness gap, not a cosmetic one, and the fix is a straightforward
   reorder with no behavior change to what gets reported, only when.
2. **Missing first-connect event** (`luno/adapters/vision.
   py::VisionAdapter.on_camera_status()`): `CameraReconnected` only
   fired for `previous is False` (an actual disconnect->reconnect),
   silently missing `previous is None -> True` (the very first
   successful open a deployment ever makes, before any failure was ever
   observed). The dashboard's own live `camera_connected` FIELD was
   never affected by this (it updates unconditionally on any real
   transition, event or not) - this only affected anything listening to
   the `CameraReconnected` EVENT specifically. Fixed by firing on any
   `connected is True and previous is not True` transition. No new
   event type was introduced (reuses `CameraReconnected` - "the camera
   is connected now" reads the same to any listener regardless of
   whether it was previously `None` or `False`).

## Single authoritative path - confirmed, not rebuilt

The brief asked to "ensure there is exactly one authoritative camera
connection/state path... or explicitly document why multiple paths are
required." The forensic trace confirmed this was ALREADY true before
this sprint (one `cv2.VideoCapture(` call site, one `_camera_lock`,
every caller funneling through `_capture_frame()`/`probe_camera()`) - no
architectural change was needed here, only a permanent regression test
(`test_5_exactly_one_production_videocapture_call_site_in_the_entire_
repo`) to keep it that way.

## Test coverage

`tests/test_sprint69_1_camera_dashboard_forensics.py` - 15 tests
covering all 11 required categories: local success -> dashboard
CONNECTED; local unavailable -> dashboard DISCONNECTED; network URL ->
CAP_ANY, never a local backend override; no hidden CAP_ANY path for a
local source when real candidates exist; exactly one production
`VideoCapture` call site (AST-based, repo-wide); bounded failure; reopen
cooldown; concurrent probe/capture safety (proven via a concurrent-entry
counter at the production `probe_camera()`/`capture_frame()` call
sites); state-transition correctness; dashboard telemetry correctness
(three tests: end-to-end through the real `RealVisionSource`/
`VisionAdapter`/`collect_vision()` stack proving the reordering fix,
adapter-level authoritative-field check, and the first-connect-event
fix); and no credential leakage (three tests: an end-to-end capture with
a secret-bearing URL asserting the secret never appears in captured
stdout, plus two direct unit tests on `_classify_source_for_log()`). 0
failed on first run (after fixing two test-only bugs found during the
first run - missing `grab()` on two fake `VideoCapture` stand-ins, and
an over-strict AST-node count not accounting for the existing
ternary-with-two-branches call-site pattern - neither was a production
defect).

## Regression

Targeted camera/vision test suite (11 files, 189 tests) + dashboard test
suite (4 files, 83 tests): all passed.

Full repository sweep (`python3 -m pytest tests/ -q --ignore=tests/
test_main_bargein.py --ignore=tests/test_root_main_bargein.py
--timeout=60 --timeout-method=signal`): **3498 passed, 9 failed, 3
skipped, 446s**. All 9 failures match the exact established
environment-gap baseline from Sprint 68/69
(`test_mic_device_index.py` x4, `test_real_adapters.py` x2,
`test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`,
`test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state`),
plus one additional failure -
`test_state_isolation.py::test_planner_turn_thread_can_genuinely_outlive_console_stop`
- a different specific test within the same file Sprint 69's own
baseline already documents as a source of full-suite-only,
order/timing-dependent flakiness (unrelated to planner threading, not
camera/vision code). Re-ran alone immediately after: passed in 1.12s,
confirming it is not a Sprint 69.1 regression. Zero failures touch
`luno/vision.py`, `luno/adapters/real_vision.py`,
`luno/adapters/vision.py`, or `camera_diagnostic.py`. See
`docs/testing/regression_baseline.md`'s Sprint 69.1 section for the full
breakdown.

## Persistent state

`config/*.json` (27 files) SHA-256-identical throughout this sprint's
work, including `long_term_memory.json` unchanged since Sprint 55. No
config migration performed or needed - per the brief's own explicit
"prefer code-path correction over configuration mutation" instruction,
nothing in this sprint touched `CAMERA_URL`/`CAMERA_INDEX`/`TAPO_HOST`
or any other persistent setting.

## Known limitations

- The exact root cause of the user's specific reported symptom (CAP_
  FFMPEG still firing, dashboard still DISCONNECTED, post-Sprint-69)
  could not be conclusively established from source/config/runtime
  evidence available in this sandbox - see "What this sprint could not
  determine" above. The new diagnostics are designed to make this
  answerable from the user's own next log capture or a single
  `camera_diagnostic.py` run on the affected machine.
- `_sanitize_error_text()`'s redaction of a third-party exception's own
  message is best-effort, not a guarantee, for arbitrary free text.
- The backend-mismatch detector (comparing requested vs. actual
  `cap.getBackendName()`) depends on the installed OpenCV build actually
  implementing `getBackendName()` - present in all reasonably recent
  OpenCV-Python versions, but not universally guaranteed.
