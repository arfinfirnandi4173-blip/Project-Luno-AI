# Camera Automation — P0.5.4: Live Tapo C212 Camera Event Verification

## Objective

Perform a real-hardware verification of the existing Vision → Bridge →
Camera Automation event pipeline (P0.5.3) against the user's actual
Tapo C212. This is a verification sprint — no new detection logic, no
new automation rules, no Vision/PTZ modifications.

## Result summary

**This sprint's live hardware tests (Sections 6–11 of the governing
brief) could NOT be performed from this working environment.** This
environment is a sandboxed development machine with no network route to
the camera's local network, no route to the configured Home Assistant
host, and no `ultralytics` (YOLO) package installed. The brief's own
Section 2 explicitly instructs "Do NOT use the sandbox as a substitute
for live hardware" — this report follows that instruction: every
hardware-dependent test below is honestly marked `NOT PERFORMED` with
its real, verified reason, never fabricated as `PASS`. Everything that
COULD be verified from this environment (regression, camera-ID
evidence review, pipeline wiring correctness via the existing automated
test suite) was verified and is reported below.

## Environment

- **Host:** this session's sandboxed development environment (not the
  user's real Luno deployment machine).
- **Camera model:** TP-Link Tapo C212 (per `.env`'s `TAPO_HOST`/
  `TAPO_USERNAME`/`TAPO_PASSWORD`/`TAPO_STREAM` configuration — values
  not printed here).
- **RTSP source identity (no secrets):** `luno.config.CAMERA_URL` is
  auto-derived as `rtsp://<TAPO_USERNAME>:<TAPO_PASSWORD>@<TAPO_HOST>:554/<TAPO_STREAM>`
  when `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` are all set and no
  explicit `CAMERA_URL` override is given (`luno/config.py` lines
  725–726) — `TAPO_STREAM=stream1` in this checkout's `.env`. Host and
  credentials are configured (`TAPO_HOST configured: True` when
  checked), never printed.
- **Vision configuration:** `CAMERA_VISION_ENABLED=true`,
  `VISION_BACKEND=real` in this checkout's own `.env` — meaning on a
  machine that CAN reach the camera, the real (not mock) Vision backend
  would already be selected.
- **Test date/time:** 2026-08-20 (this session).

## Pre-flight check (Section 3)

### Camera / network

Directly re-probed this sprint (not assumed from prior sprints' memory)
via a raw TCP connect attempt to `TAPO_HOST` on ports 443/554/80:

```
port 443: OSError: [Errno 101] Network is unreachable
port 554: OSError: [Errno 101] Network is unreachable
port 80:  OSError: [Errno 101] Network is unreachable
```

This is a lower-level, more definitive failure than the `HTTP 403`
proxy rejection P0.5.1/P0.5.2 observed for outbound internet traffic —
`Network is unreachable` means this sandbox has no routing path to the
camera's private LAN address space at all (`TAPO_HOST` is a
`192.168.x.x` address). Also re-probed the configured Home Assistant
host (`HA_URL`'s hostname):

```
gaierror: [Errno -3] Temporary failure in name resolution
```

DNS resolution itself fails for the HA hostname in this attempt
(distinct from P0.5.1's `HTTP 403`, but the practical conclusion is
identical: unreachable). **Camera reachable: NO. RTSP stream reachable:
NO. Camera power/exclusivity: cannot be determined from this
environment.**

### Vision

`CAMERA_VISION_ENABLED=true` and `VISION_BACKEND=real` are both already
set in this checkout's `.env`, so `RealVisionSource` (not
`MockVisionSource`) would be selected by a real bootstrap here.
However, `ultralytics` (the library `luno/vision.py::_get_yolo()`
imports lazily — `from ultralytics import YOLO`) is **not installed**
in this sandbox's Python environment (`ModuleNotFoundError: No module
named 'ultralytics'`, confirmed by direct import attempt this sprint) —
a second, independent reason the real YOLO pipeline cannot even
initialize here, on top of the camera being unreachable. `cv2`
(OpenCV) IS installed (`5.0.0`) and importable.

**VisionAdapter starts successfully: NOT VERIFIED (would fail at
`_get_yolo()` even before attempting the unreachable RTSP connection).
YOLO model loads: NOT VERIFIED (`ultralytics` absent). RTSP/OpenCV
pipeline starts: NOT VERIFIED (network unreachable). No initialization
errors: N/A — initialization was not attempted against real
hardware/model, to avoid a misleading partial/mocked result.**

### Camera Automation

Verified via the existing, already-passing automated test suite (not a
live-hardware run, but genuine code-level evidence, distinct from and
clearly labeled apart from hardware verification):

- Camera Automation module loads: confirmed (`tests/
  test_p0_5_3_vision_camera_bridge.py::test_21_bridge_is_registered_by_
  real_bootstrap`, real `register_all_modules()` bootstrap, passing).
- Feature flag/state is known: `CAMERA_AUTOMATION_ENABLED` is unset in
  this checkout's `.env` → `CameraAutomationConfig.enabled` defaults to
  `False` — confirmed via `CameraAutomationModule.is_enabled()` in
  `test_22_disabled_by_default_bridge_never_subscribes_real_bootstrap`.
- Bridge is subscribed: confirmed conditionally — the bridge correctly
  subscribes when `camera_automation.enabled=True` and correctly stays
  unsubscribed when `False` (both paths covered by the existing P0.5.3
  suite).
- Event Bus is running: confirmed — the real `runtime.event_bus` is
  exercised by every real-bootstrap test in the existing suite.

### Existing systems

No existing Vision subscriber or production functionality was disabled
to make this sprint "easier" — this sprint made **zero** production
code changes (see Diff Audit below), so nothing about existing systems
was touched at all.

## Test A–F (Sections 6–11)

Every hardware-dependent test is honestly reported `NOT PERFORMED`,
never `PASS`, per the brief's own explicit instruction not to fabricate
a result:

```text
Idle test:
    NOT PERFORMED
    Reason: no network route from this environment to TAPO_HOST
    (confirmed via direct TCP probe this sprint: "Network is
    unreachable" on ports 443/554/80) and no ultralytics/YOLO installed
    in this environment - both required for a real Vision cycle.
```

```text
Human enter (Test B):
    NOT PERFORMED
    Reason: same as above - no route to the camera at all, so no
    CameraPersonEntered could ever be generated here regardless of
    anyone standing in front of the physical camera.
```

```text
Human stays (Test C):
    NOT PERFORMED
    Reason: depends on Test B having run.
```

```text
Human exit (Test D):
    NOT PERFORMED
    Reason: depends on Test B/C having run.
```

```text
Human re-entry (Test E):
    NOT PERFORMED
    Reason: depends on Test D having run.
```

```text
Availability test (Test F):
    NOT PERFORMED
    Reason: no safe or practical way to disconnect/reconnect a camera
    this environment cannot even reach in the first place; per Section
    11's own instruction, reporting NOT PERFORMED with the actual
    reason rather than fabricating a result.
```

No event trace (Section 14) can be produced this sprint — there is no
real event to trace. Fabricating timestamps/camera IDs/confidence
values for a trace that did not happen would violate this sprint's own
Section 14 ("Use actual values. Do not invent timestamps or IDs.").

## Camera ID verification (Section 15)

No new live evidence was gathered this sprint — the same network
blockers that prevent Tests A–F also prevent any new HA registry query
or pytapo `getBasicInfo()`/`connections` check beyond what P0.5.1 and
P0.5.2 already established. Restating the existing evidence chain
honestly, with nothing upgraded:

- P0.5.1 (live HA discovery): the Tapo C212 camera entity was
  genuinely **NOT FOUND** in Home Assistant (registry was reachable and
  searched at that time, not merely unreachable) — there is no HA-side
  device/`connections` evidence to cross-check `TAPO_HOST` against.
- P0.5.2 (pytapo static audit): a genuine device-`connections`-list
  check (the strongest available evidence method) was designed and
  unit-tested, but never executed against the real device — `pytapo`
  itself fails to import in this sandbox.
- P0.5.3 (bridge): assigns `camera_id="tapo_c212"` purely by
  configuration convention (matching the single profile key already
  shipped in `config/camera_automation.json`), explicitly documented as
  an assumption, not a proof.

**Configuration-level correlation that DOES exist (not new this
sprint, but worth restating precisely):** `luno.vision.CAMERA_URL`
(what `RealVisionSource` actually opens) and `pytapo.Tapo(...)` (what
`RealCameraPTZHandler` actually connects to) are BOTH derived from the
exact same `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` environment
variables (`luno/config.py` lines 718–726). This means Vision and PTZ
are configured to point at the same address — a real, verifiable fact
about this checkout's configuration — but configuration agreement is
not device-identity proof (an operator could, in principle, misconfigure
one to point elsewhere; nothing enforces they resolve to the same
physical hardware beyond both env vars being set correctly by hand).

```text
same_physical_camera:
    UNKNOWN
```

Consistent with P0.5.2/P0.5.3 — not upgraded to CONFIRMED, per this
sprint's own explicit instruction not to infer physical identity from
naming/configuration convention alone.

## Motion (Section 16)

```text
Motion:
    NOT PROVIDED BY CURRENT VISION PIPELINE
```

Unchanged finding from P0.5.3 — no generic motion event exists
anywhere in the Vision event pipeline; this remains expected, not a
failure.

## Event semantics (Section 17)

Not independently re-verified against live behavior this sprint (no
events were observed) — the semantics were already read directly from
source in P0.5.3 (`_update_person_presence()` in
`luno/adapters/vision.py`) and are restated, not reinterpreted:
`human_detected` corresponds to the debounced room-level presence
signal (`CameraPersonEntered`) rising on the first detection in a poll
cycle; `human_cleared` corresponds to `CameraPersonLeft`, which only
fires after `CAMERA_PERSON_ABSENCE_TIMEOUT_S` (default 5.0s) of
continuous non-detection.

## Failure isolation (Section 18)

Not exercised against live hardware this sprint (nothing was running
against a real camera to isolate a failure from). Already covered by
the existing, passing `tests/test_p0_5_3_vision_camera_bridge.py`
Section E (`test_11`/`test_12` — a bridge/`CameraAutomationModule`
exception does not propagate to other subscribers).

## Test results

| Test | Result |
|---|---|
| Idle | NOT PERFORMED |
| Human enter | NOT PERFORMED |
| Human stays | NOT PERFORMED |
| Human exit | NOT PERFORMED |
| Human re-entry | NOT PERFORMED |
| Camera disconnect | NOT PERFORMED |
| Camera reconnect | NOT PERFORMED |

No result above was replaced with PASS or INCONCLUSIVE where NOT
PERFORMED is the accurate, evidenced description — per this sprint's
own explicit instruction, NOT PERFORMED (with a real reason) is an
acceptable, honest outcome, and is what actually happened.

## No automation actions (Sections 4/13/20)

No Home Assistant service was called. No PTZ command was sent
(`moveMotor`/`calibrateMotor`/`savePreset`/`setPreset` were never
invoked — confirmed by the fact that no live pytapo connection was even
attempted this sprint, let alone a control call). No
`automation_rules.json` was created or modified. No real-world action
(light/switch/lock/alarm/notification) was configured or triggered.

## Regression (Section 21)

**Baseline (recorded before any activity this sprint):**
`tests/test_p0_camera_automation.py` (23) + `tests/
test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/
test_tapo_camera_event_audit.py` (18) + `tests/
test_p0_5_3_vision_camera_bridge.py` (26) + `luno/adapters/tests/
test_adapters.py` + `tests/test_camera_presence.py` + `tests/
test_sprint69_1_camera_dashboard_forensics.py` + `tests/
test_vision_sprint8.py` + `tests/test_vision_ask_vision.py` + `tests/
test_vision_intent.py` + `tests/test_vision_intent_classifier.py` +
`tests/test_vision_provider.py` (144) + `tests/
test_sprint72_automation_engine.py` (78) = **333 passed, 0 failed.**

**After (re-run at the end of this sprint):** identical suite,
**333 passed, 0 failed** — unchanged, exactly as expected for a sprint
that made zero production code changes.

`tests/test_sprint68_mutation_audit_hardening.py` spot-checked: 65/67 —
the same 2 pre-existing, unrelated environmental failures documented in
P0.5.1/P0.5.2/P0.5.3 (`config/backups` file count, mutation-audit-dir
baseline — files dated Aug 11–18, predating every camera sprint).

No newly introduced failure exists. No failure was classified as
"pre-existing" without this sprint's own direct baseline evidence
(recorded above, before any activity this sprint).

## Diff audit (Section 22)

```text
Production code modified:
    0
```

Confirmed via `find luno -newer <P0.5.3's own change-impact doc's
timestamp>` — the only result is `luno/2026-08-20.jsonl`, a runtime log
file generated by running the test suite, not a source file. No file
under `luno/` was edited this sprint. No temporary debug instrumentation
was added (none was needed, since no live event was ever produced to
observe).

## Definition of done — honest accounting

- Real Tapo C212 was tested: **NO** (environment cannot reach it).
- Real RTSP/Vision pipeline was tested: **NO**.
- Human enter/exit produced the expected Vision event: **NOT VERIFIED**
  (no event occurred).
- Vision events reached CameraAutomation: **NOT VERIFIED live**; the
  TRANSPORT path was already proven correct in P0.5.3 via a real-bus,
  non-hardware end-to-end test — restated here as existing evidence,
  not new this sprint.
- No event was fabricated: **YES** — confirmed by this report itself.
- No automation action was executed: **YES**.
- Existing Vision behavior remained functional: **YES** (zero code
  changes; full regression clean).
- No PTZ movement occurred: **YES** (no pytapo call was ever made).
- No HA control service was called: **YES**.
- Camera ID relationship was investigated honestly: **YES** — remains
  `UNKNOWN`, restated with precise reasoning, not upgraded.
- Regression remains clean: **YES** (333/333 before and after).

Per this sprint's own "Definition of Done" section: "A test may be
marked INCONCLUSIVE if the hardware/environment prevents verification.
That is acceptable. Fabricating PASS is not." This sprint could not be
completed as a live-hardware proof-of-life test from this environment.
That is the honest, evidenced outcome.

## What the user needs to do to actually complete this sprint

Run this exact verification protocol (Tests A–F above) on the real
Luno deployment machine — the one with actual network access to
`TAPO_HOST` and the actual camera in view — with `CAMERA_AUTOMATION_
ENABLED=true` set for the duration of the test only. A lightweight way
to capture the evidence Section 5/14 ask for without adding permanent
instrumentation: temporarily subscribe a print-only handler to
`camera_automation.camera_event` (and, for cross-checking, to
`camera_person_entered`/`camera_person_left`/`camera_disconnected`/
`camera_reconnected` directly) via the existing Event Bus `subscribe()`
method, run through Tests A–F, then remove the temporary subscription —
no code change to any production file is needed to do this from a
Python REPL or a small one-off script against the running process's
own `runtime.event_bus`, if the process exposes one, or by running the
existing test harness's own `_build_stack()`-style real bootstrap
against the real `VISION_BACKEND=real` configuration in a short-lived
script (similar in spirit to `ha_camera_discovery.py`/
`tapo_camera_event_audit.py`, but observing the ALREADY-RUNNING live
pipeline rather than probing a fresh connection).

## Next sprint

Recommended: re-run this exact P0.5.4 protocol on the user's own
machine. Only after Tests A–F produce real, evidenced PASS/FAIL results
(not NOT PERFORMED) should a future sprint consider writing an actual
`config/automation_rules.json` entry that acts on `human_detected`/
`human_cleared` — still explicitly out of scope here, per this sprint's
own closing principle: prove the camera automation foundation is alive
first; do not redesign or extend it because this environment's network
routing prevented a live check.
