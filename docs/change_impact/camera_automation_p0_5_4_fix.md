# Camera Automation — P0.5.4-FIX: Use the Real main.py Vision Lifecycle

## Why this sprint exists

The user ran `luno_live_camera_event_observer.py` (delivered in
P0.5.4-LIVE) on their real machine and reported it produced **only**
`scheduled_vision_poll (0.0ms)` and no camera events — while their real
`main.py` runtime, run separately on the same machine, performs genuine
live YOLO detection with dashboard-visible results (person,
television, couch, chair, "person looking away", "person walking").
This proves the camera/RTSP/YOLO pipeline itself works; the defect is
specific to the observer script's failure to attach to the same
working lifecycle `main.py` uses.

## Investigation (traced, not guessed)

1. **`main.py`** (root, line 66): `launcher_config = LauncherConfig.load()`
   — confirmed via grep this is the only `LauncherConfig` usage in
   `main.py`.
2. **`luno/bootstrap/launcher_config.py`**: `LauncherConfig.load()`
   (classmethod, lines 177–215) is the *only* code path that calls
   `load_dotenv()` and then resolves `vision_backend=_backend_from_env
   ("vision")` from the now-populated `os.environ`. A bare
   `LauncherConfig()` constructor never calls `load_dotenv()` and keeps
   only the hardcoded dataclass default, `vision_backend: str =
   BACKEND_MOCK`.
3. **`luno/bootstrap/adapters.py`** (`register_all_adapters`, lines
   164–173):
   ```python
   vision_source = None
   if launcher_config.vision_backend == BACKEND_REAL:
       try:
           from luno.adapters.real_vision import RealVisionSource
           vision_source = RealVisionSource()
       except Exception as ex:
           log(f"real Vision backend requested but unavailable ({ex}) - falling back to mock", "bootstrap")
   vision_adapter = VisionAdapter(source=vision_source)
   ```
   If `vision_backend != "real"`, `vision_source` stays `None`, and
   `VisionAdapter.__init__`'s own `self.source = source or
   MockVisionSource()` silently falls back to the mock — with no
   error or warning.
4. **`RealVisionSource`** (`luno/adapters/real_vision.py`) owns the
   two background threads that do real detection work
   (`_poll_loop` and `_tracked_cycle_loop`), both started only inside
   `RealVisionSource.start(listener)`, itself only called from
   `VisionAdapter._do_start()`. `MockVisionSource` starts no such
   threads, opens no RTSP connection, and runs no YOLO inference.
5. **The `scheduled_vision_poll` log line the user saw is unrelated**:
   grep of `luno/adapters/vision.py` for `scheduled_vision_poll`/
   `on_event`/`handle_event` returns zero matches — `VisionAdapter` has
   no handler for it at all. It comes from `SchedulerAdapter._fire
   ("vision_poll")` (`luno/adapters/scheduler.py`), a generic,
   content-free periodic Event Bus tick
   (`Event(type="scheduled_vision_poll", data={"job_name": "vision_poll"})`)
   that exists independently of which Vision backend is active. Its
   presence proves the scheduler is running; it says nothing about
   whether real inference is running.

## Root cause

`luno_live_camera_event_observer.py`'s `main()` called `cfg =
LauncherConfig()` (bare dataclass constructor) instead of `cfg =
LauncherConfig.load()` (the classmethod `main.py` itself uses). Because
the bare constructor never reads `.env`, `cfg.vision_backend` silently
stayed at its hardcoded default (`"mock"`) even though the real `.env`
has `VISION_BACKEND=real`. `register_all_adapters()` therefore never
constructed `RealVisionSource()` and instead ran `VisionAdapter` against
`MockVisionSource()` — no real RTSP connection, no real YOLO inference,
no real `CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected` events, hence zero `camera_automation.camera_event`s
reached the observer. This is a bug confined entirely to the standalone
script I authored — `VisionAdapter`, `RealVisionSource`,
`CameraAutomationModule`, and `VisionCameraEventBridge` were never at
fault and were never touched.

## Fix (minimal, single file)

`luno_live_camera_event_observer.py`, `main()`: changed `cfg =
LauncherConfig()` to `cfg = LauncherConfig.load()`, matching
`main.py` line 66 exactly. Added one explicit, visible warning if
`cfg.vision_backend != "real"` after boot, so this class of bug (or any
future `.env` misconfiguration) is immediately visible in the printed
output instead of silently degrading to the mock backend again.

No other discrepancy was found between the observer's bootstrap
sequence and `main.py`'s: both call the same
`register_all_modules()`/`register_all_adapters()` functions, and every
other `main.py` step not replicated by the observer
(`register_real_tool_handlers`, `register_device_intent_classifier`,
`register_session_summary_client`, `register_intent_classifier`,
`Supervisor`, `register_dashboard`, `ProductionConsole`) is unrelated
to Vision/camera detection — they concern tool routing, intent
classification, the dashboard UI, and the console loop, none of which
gate `RealVisionSource`'s background threads.

**Architecture preserved exactly as required:** `main.py` existing
runtime → existing Vision pipeline → existing Vision events → temporary
observer → `camera_automation.camera_event`. The observer still does
not recreate Vision, does not construct a second YOLO model, does not
open a second RTSP connection, and still only subscribes to the
already-existing `CameraPersonEntered`/`CameraPersonLeft`/
`CameraDisconnected`/`CameraReconnected` events plus the bridge's own
`camera_automation.camera_event` — exactly per the brief's required
architecture.

## Files changed

```text
[MODIFIED] luno_live_camera_event_observer.py   (one config-loading line + one warning block)
[MODIFIED] tests/test_luno_live_camera_event_observer.py  (3 new tests)
[NEW]      docs/change_impact/camera_automation_p0_5_4_fix.md
```

**No file under `luno/` was touched.** Confirmed via `find luno -newer
docs/change_impact/camera_automation_p0_5_4_live.md` — only stale
`__pycache__/*.pyc` bytecode artifacts (not source) appear; zero `.py`
source files under `luno/` changed. This satisfies the brief's Hard
Safety Rule without needing to invoke its "STOP and report" escape
hatch — no production Vision file was implicated by the investigation.

## Tests

New tests (`tests/test_luno_live_camera_event_observer.py`):

- `test_14_observer_uses_launcherconfig_load_not_bare_constructor` —
  static proof `main()`'s source contains `LauncherConfig.load()` and
  does not contain a bare `= LauncherConfig()` call.
- `test_15_main_py_itself_uses_launcherconfig_load` — confirms the
  thing being matched against (`main.py`) still resolves its config via
  `.load()`, so this fix's premise hasn't silently drifted.
- `test_16_observer_warns_when_vision_backend_not_real` — confirms the
  new visible warning path exists in source.

Per the brief's explicit prohibition, **none** of the new/existing
tests claim to verify real camera detection — they verify only that the
observer's *wiring* now matches `main.py`'s own config-resolution entry
point. `test_13_real_bootstrap_simulated_event_reaches_observer`
(existing, unchanged) continues to prove only Event Bus transport via a
simulated event, not hardware.

Before: 13/13 passing (P0.5.4-LIVE). After: **16/16 passing** (13+3).

Targeted regression (Vision/camera/automation-engine suites — the same
set used by every prior sprint in this line):

```text
tests/test_p0_camera_automation.py
tests/test_p0_5_camera_integration.py
tests/test_ha_camera_discovery.py
tests/test_tapo_camera_event_audit.py
tests/test_p0_5_3_vision_camera_bridge.py
tests/test_luno_live_camera_event_observer.py
tests/test_camera_presence.py
tests/test_sprint69_1_camera_dashboard_forensics.py
tests/test_vision_sprint8.py
tests/test_vision_ask_vision.py
tests/test_vision_intent.py
tests/test_vision_intent_classifier.py
tests/test_vision_provider.py
tests/test_sprint72_automation_engine.py
```

Before: 331 passed (346 baseline from P0.5.4-LIVE minus the 15 tests
outside this exact targeted list — recount performed directly this
sprint, not carried over). After: **334 passed, 0 failed** (331+3).
`luno/adapters/tests/test_adapters.py` also re-run separately: 15/15
passing, unchanged.

`tests/test_main_bargein.py` and `tests/test_root_main_bargein.py`
remain the same 2 pre-existing, documented, unrelated
INFRASTRUCTURE-class collection failures (`faster_whisper` not
installed / `legacy_main.py` absent from this checkout) already on
record since long before this sprint — neither file was touched, and
neither is part of this sprint's targeted regression set.

## Live status — honest accounting

**NOT VERIFIED.** I have not run this fixed script against the real
Tapo C212 myself — I structurally cannot: my own tool execution always
occurs in an isolated cloud sandbox with no route to the user's LAN
(re-confirmed every sprint in this line; unchanged this sprint). This
sprint corrects a bug in the *tool*, on the strength of a fully
code-traced root cause — it does not, and cannot, constitute a live
hardware PASS. That evidence can only come from the user re-running the
script on their own machine.

## What the user should do next

```
python luno_live_camera_event_observer.py --duration 120
```

Watch for `vision backend: real` printed near the top of the output
(if it prints `mock` or anything else, the fix did not take effect —
stop and report that line back). Then walk through the same sequence
as before: start empty, walk into frame, remain a few seconds, walk out
of frame, then re-enter.

**Expected proof format** (per the brief), for each detection:

```
CameraPersonEntered  ->  [Vision] camera_person_entered observed
                      ->  [CAMERA EVENT] kind=human_detected camera_id=tapo_c212 ...
```

i.e. the raw Vision event fires first, followed immediately by the
bridge's normalized `camera_automation.camera_event`. If this trace
appears with real walk-in/walk-out timing (not immediately, not on a
fixed 5s cadence, but tied to the person actually entering/leaving
frame), that is the first genuine, hardware-verified proof this whole
sprint line has been building toward — please copy the printed output
back.
