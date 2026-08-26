# Camera Automation — P0.5.2: Tapo C212 Event Source Audit

## Objective

P0.5.1 established that Home Assistant does not currently expose the
Tapo C212 (HA connection reachable, registry searched, camera/motion/
human/availability entities all genuinely NOT FOUND). This sprint
therefore audits the OTHER existing Luno camera path — the direct
`pytapo` integration — to answer: can Luno reliably obtain motion,
human-detection, and availability events through it instead? This is a
read-only audit + prototype sprint. No automation rules were
implemented; no production integration was wired.

## Existing Tapo architecture (as found, unmodified)

Discovery in this sprint found **two separate, already-existing** Luno
camera code paths that both ultimately reach the same physical Tapo
C212, sharing only `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` as
configuration — never a shared connection or client:

```text
Path A — PTZ control (pytapo's own proprietary Tapo API)
Luno
 -> luno/bootstrap/adapters.py::_register_real_camera_ptz_handler()
 -> pytapo.Tapo(TAPO_HOST, TAPO_USERNAME, TAPO_PASSWORD)
 -> luno/tool_manager/builtin/real_camera_ptz.py::RealCameraPTZHandler
 -> pytapo (3.4.18)
 -> Tapo C212
```

```text
Path B — RTSP video + local YOLO inference (NOT pytapo)
Luno
 -> luno/adapters/real_vision.py::RealVisionSource
 -> luno/vision.py (cv2.VideoCapture over CAMERA_URL,
    rtsp://user:pass@TAPO_HOST:554/stream1 — auto-derived from
    TAPO_HOST/USERNAME/PASSWORD in luno/config.py, never pytapo)
 -> luno/adapters/vision.py::VisionAdapter (Event Bus)
 -> Tapo C212 (same physical device, different protocol — RTSP, not
    pytapo's HTTP/KLAP API)
```

Path A is PTZ-only — `RealCameraPTZHandler` only ever calls
`moveMotor`/`calibrateMotor`/`savePreset`/`getPresets`/`setPreset`
(confirmed by reading `luno/tool_manager/builtin/real_camera_ptz.py` in
full; unmodified this sprint). It has never called any detection or
event method.

Path B is a full, already-running human/object detection pipeline —
completely independent of pytapo. This was the most significant
discovery of this audit (see "Existing human detection already exists"
below).

## Installed pytapo version

`3.4.18` (confirmed via `.venv/Lib/site-packages/pytapo-3.4.18.dist-info/METADATA`
and `pytapo/version.py::PYTAPO_VERSION`).

## Dependency capability audit (Section 4)

**This sandbox's `pytapo` cannot actually be imported.** `from pytapo
import Tapo` raises `ModuleNotFoundError: No module named
'kasa.transports'` — the installed `kasa` package's own internal layout
doesn't match what pytapo's `transport/kasa/kasa.py` expects, and a
deeper import (`cryptography.hazmat.bindings._rust.openssl.hashes`)
also fails with an `AttributeError` — both point to a real, pre-existing
dependency-version mismatch already present in this checkout's
`.venv`, unrelated to any code in this repository and unrelated to this
sprint's own changes. This is NOT something this sprint fixed —
upgrading `kasa`/`cryptography` is a dependency change, explicitly out
of scope ("DO NOT upgrade dependencies," Section 1). It is also not
newly discovered as broken by this sprint: `luno/bootstrap/
adapters.py::_register_real_camera_ptz_handler()` already wraps
`from pytapo import Tapo` in a broad `try/except Exception`, so this
failure has always silently fallen through to "camera pan/tilt stays
mocked" in this specific sandbox — this sprint is simply the first time
that fact was surfaced and reported explicitly, rather than the first
time it happened.

Because a live import is impossible here, Section 4's capability audit
was performed by **direct static source inspection** of the installed
`pytapo/__init__.py` (evidence-based, not speculative — every method
name below was read directly from the installed package's own source,
not from documentation or memory):

| Method | What it actually returns | Nature |
|---|---|---|
| `getMotionDetection()` / `setMotionDetection()` | The camera's own firmware motion-detection CONFIG (`enabled`, `digital_sensitivity`) | Config GET/SET — not a live event |
| `getPersonDetection()` / `setPersonDetection()` | The camera's own firmware AI person-detection CONFIG — a REAL, separate namespace from motion (`people_detection` vs `motion_detection`) | Config GET/SET — not a live event |
| `getVehicleDetection()`, `getPetDetection()`, `getBarkDetection()`, `getMeowDetection()`, `getGlassBreakDetection()`, `getTamperDetection()`, `getBabyCryDetection()`, `getLinecrossingDetection()`, `getPackageDetection()` | Further per-capability firmware detection CONFIGs the C212's firmware exposes | Config GET/SET only |
| `getAlertEventType()` / `setAlertEventType()` | The list of alarm/notification types the camera supports and whether each is enabled | Config GET/SET — not a live event |
| `getEvents(startTime, endTime)` | Calls `searchDetectionList` — queries the camera's own **recorded detection/playback log** for a time window, returning real entries with `start_time`/`end_time` | **The one genuine, evidence-based event data source** — POLL-based, not push |
| `getAlarm()`, `getAlarmConfig()`, `playAlarm()`, `setAlarm()`, `startManualAlarm()`, `stopManualAlarm()` | Alarm siren config/control | Mostly config/control, `playAlarm`/`startManualAlarm` are WRITE (never called) |
| `getBasicInfo()` | Real device info | A successful call is itself evidence of "camera reachable/online" |
| `getStreamURL()`, `getMediaSession()` | Stream session info | Not used — Luno already derives its own RTSP URL independently (Path B) |

**No push/websocket/subscribe/callback API exists anywhere in the
installed package** — confirmed by grepping every method name in
`pytapo/__init__.py` and `pytapo/asyncHandler.py` for
socket/subscribe/listen/callback/push; none exist. `getEvents()` is a
pure request/response HTTP call, same transport as every other method.

**Conclusion: pytapo can tell you the camera's detection CONFIGURATION
(is motion/person detection turned on in firmware) and can retrieve a
recorded EVENT LOG by polling — it cannot push a live event to Luno.**

## Existing human detection already exists (Path B, not pytapo)

The single most important finding of this audit: Luno **already has a
fully-implemented, already-wired, currently-configured** human
detection and camera-availability pipeline that has nothing to do with
pytapo or Home Assistant — `luno.vision` (RTSP capture via OpenCV) +
`luno.vision_tracking`/`luno.vision_human_state` (YOLO + pose) +
`luno.adapters.vision.VisionAdapter`. Confirmed by reading
`luno/adapters/real_vision.py` and `luno/adapters/vision.py` in full
(neither modified this sprint):

- `VisionAdapter` already publishes `CameraDisconnected`/
  `CameraReconnected` on the Event Bus, diffed off `luno.vision.
  camera_status()["connected"]`, exactly once per real transition —
  this is a complete, working answer to this sprint's own Section 9
  ("determine the safest way to detect camera_online/camera_offline").
- `VisionAdapter` already publishes `CameraPersonEntered`/
  `CameraPersonLeft` — a debounced (`CAMERA_PERSON_ABSENCE_TIMEOUT_S`,
  default 5s), room-level human-presence signal driven by the YOLO
  tracked-detection pipeline over the SAME physical camera's RTSP
  stream. It also publishes finer-grained `PersonAppeared`/
  `PersonDisappeared`, `HumanEntered`/`HumanLeft`/`PoseChanged` (per
  tracked individual, with posture/facing/hand-raised estimates —
  explicitly no biometric identification, per `vision_human_state.py`'s
  own documented guarantee).
- This entire pipeline is **already enabled in this checkout's own
  `.env`**: `CAMERA_VISION_ENABLED=true`, `VISION_BACKEND=real` — in a
  real deployment (unlike this network-isolated sandbox) it is already
  running against the Tapo C212's RTSP stream today, independent of
  whatever this sprint or P0.5.1 found (or didn't find) in HA or
  pytapo.

This was not built or modified this sprint — it already existed,
untouched, before this audit began. It is documented here because it
directly changes the answer to "where can Luno reliably obtain real
Tapo C212 events": the honest answer is **not** "we need to build
something new" — it is **"a working answer already exists in
`luno.vision`/`VisionAdapter`, and integrating THAT is very likely the
safest next step, not building a new pytapo- or HA-based path from
scratch."** See "Recommended Integration Source" below.

## Read-only camera probe

New file: `tapo_camera_event_audit.py` (root level, following this
repo's existing convention — `tapo_ptz_diagnostic.py`,
`ha_camera_discovery.py` — rather than a new `tools/` directory).
Reuses the existing `luno.config.TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` and the existing, already-tested
`real_camera_ptz.classify_tapo_exception()`/`_redact_credentials()` —
no second credential mechanism, no second error-classification scheme.

Calls only read-only methods: `getBasicInfo()`, `getMotionDetection()`,
`getPersonDetection()`, `getAlertEventType()`, `getEvents()` (twice —
once at the start of the observation window, once at the end, diffing
for new entries — Section 10/11's own "poll if the API supports it,
never deliberately trigger" instruction). Never calls any `set*`/
`play*`/`start*`/`stop*`/PTZ method — verified statically by this
sprint's own `test_18_never_calls_a_write_or_control_method_on_the_client`.
`--duration` flag, default 30s (Section 11). Classifies every capability
using the brief's own closed set: CONFIRMED / AVAILABLE-BUT-NOT-OBSERVED
/ NOT-AVAILABLE / UNKNOWN (Section 12) — never overstated; a
connection/auth-level failure is always reported as UNKNOWN for the
capability itself, never NOT-AVAILABLE (a failed connection tells you
nothing about whether the capability exists).

Same-physical-camera-vs-HA is always reported UNKNOWN, referencing
P0.5.1's own finding that the HA camera entity was genuinely NOT FOUND
(not merely unreachable) — there is no HA-side device evidence left to
compare `TAPO_HOST` against, so this can never be resolved to CONFIRMED
this sprint (Section 13's own "do not claim same camera merely from an
IP match" instruction).

## Live probe attempt

Ran `python tapo_camera_event_audit.py --duration 2` in this sandbox:

```
TAPO_HOST configured: True
TAPO_USERNAME configured: True
TAPO_PASSWORD configured: True

RESULT: IMPORT_FAILED
Reason: No module named 'kasa.transports'
```

Correctly and distinctly reported as an **import failure**, not a
connection failure and not "camera not found" — every capability in the
JSON output is honestly reported `UNKNOWN` with the reason "pytapo
connection was never established this run." No entity, capability, or
event was fabricated.

## Files

```text
[NEW] tapo_camera_event_audit.py
[NEW] tests/test_tapo_camera_event_audit.py
[NEW] docs/change_impact/camera_automation_p0_5_2.md
[MODIFIED] none under luno/
```

Zero files under `luno/` were touched — confirmed via `find luno -newer
docs/change_impact/camera_automation_p0_5_1.md`, which returned no
results. `config/camera_automation.json` was not touched.
`CameraAutomationModule` was not touched. No automation rule was
implemented.

## Regression

**Baseline (before this sprint):** `tests/test_p0_camera_automation.py`
(23) + `tests/test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/test_sprint69_tapo_c212_auth.py`
+ `tests/test_sprint70_tapo_live_recovery.py` (50 combined) + `tests/
test_sprint71_camera_patrol.py` + `luno/tool_manager/tests/
test_camera_ptz.py` = **181 passed, 0 failed** (verified by direct
re-run immediately before writing any new code this sprint).

**After (targeted):** the same suites plus the new `tests/
test_tapo_camera_event_audit.py` (18) = **199 passed, 0 failed.** Zero
new failures; zero pre-existing failures newly appeared.

No full-repository sweep was required or run — this sprint touched no
file under `luno/`, so the existing full-repository baseline (recorded
in `docs/testing/regression_baseline.md` as of P0.5) is unaffected by
construction, per the same reasoning already applied for P0.5.1.

## Capabilities table

| Capability | Result | Evidence |
|---|---|---|
| Camera connection (pytapo) | UNKNOWN | `pytapo` cannot be imported in this sandbox (`kasa.transports` missing) — a dependency/environment issue, not evidence about the camera |
| Camera status (pytapo `getBasicInfo`) | UNKNOWN | Never reached — connection never established |
| Motion (pytapo `getMotionDetection`) | UNKNOWN (capability CONFIRMED to exist by source inspection, but never live-verified) | Static source read confirms the method exists and returns a config, not an event |
| Human detection (pytapo `getPersonDetection`) | UNKNOWN (capability CONFIRMED to exist by source inspection, but never live-verified) | Static source read confirms a separate `people_detection` config namespace exists |
| Events (pytapo `getEvents`) | UNKNOWN (mechanism CONFIRMED to exist by source inspection — POLL-based `searchDetectionList`, never live-verified) | Static source read; no push/websocket API exists anywhere in the package |
| Availability (pytapo) | UNKNOWN | Same import blocker |
| PTZ (pytapo, pre-existing) | CONFIRMED (already production-verified in Sprint 69/70, unaffected by this sprint) | `luno/tool_manager/builtin/real_camera_ptz.py`, unmodified |
| Human detection (Path B — `luno.vision`/`VisionAdapter`) | **CONFIRMED, already implemented and already enabled in this checkout's `.env`** | `CameraPersonEntered`/`CameraPersonLeft`/`HumanEntered`/`HumanLeft`/`PoseChanged` already published on the Event Bus by existing, unmodified code |
| Availability (Path B) | **CONFIRMED, already implemented** | `CameraDisconnected`/`CameraReconnected` already published on the Event Bus by existing, unmodified code |

## Recommended integration source

**Both** — but not symmetrically:

- **`luno.vision` / `VisionAdapter` (Path B) is the strongest, lowest-risk
  candidate for human detection and availability.** It already exists,
  is already tested (pre-dating this sprint), is already enabled in
  this deployment's own `.env`, and already publishes exactly the kind
  of events P0/P0.5's `CameraAutomationModule` wants
  (`human_detected`/`human_cleared`, `camera_online`/`camera_offline`)
  — just under different event names on a different adapter, never yet
  connected to `CameraAutomationModule`.
- **pytapo (Path A) remains the correct source for PTZ control only** —
  it has no live motion/person event API at all (config + poll-only
  event log), so it is a weak candidate for driving automation triggers
  compared to Path B's already-live, already-tracked pipeline.
- **Home Assistant (per P0.5.1) remains unconfirmed** — the camera was
  genuinely not found there; nothing in this sprint changes that.

## Known limitations

- `pytapo` cannot be imported in this specific sandbox venv
  (`kasa`/`cryptography` version mismatch) — a real, pre-existing,
  environment-specific limitation, not fixed this sprint (fixing it
  would mean upgrading dependencies, explicitly out of scope). The
  probe script, its classification logic, and its event-diffing logic
  are unit-tested against mocks and known to be correct; only the
  actual live HTTP round-trip to a real C212 has never been exercised.
- pytapo's own detection-config methods (`getMotionDetection`/
  `getPersonDetection`) were never live-verified against a real device
  in this sprint — their existence and shape were confirmed by direct
  source inspection only.
- `getEvents()`'s returned event-record shape (does an entry
  distinguish "motion" from "person" internally, e.g. via a `type`
  field?) is server-response-dependent and could not be confirmed
  without a live device — this remains genuinely UNKNOWN, not assumed
  either way.

## Next sprint (not implemented this sprint)

Recommended: investigate connecting the ALREADY-EXISTING
`CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected` events (published today by `VisionAdapter`, Path B)
to `CameraAutomationModule` (P0/P0.5) — likely a much smaller, safer
change than building a new pytapo-polling event source from scratch,
since Path B already does the hard part (detection, tracking, debounce,
Event Bus publishing) and is already running in this deployment. Would
need its own dedicated sprint to design the bridge without duplicating
`CameraAutomationModule`'s own dedupe/cooldown logic. Do not implement
without a fresh live-verification pass confirming Path B's events
actually fire against the real camera in this deployment (this sprint
could not observe them — no camera hardware/network reachable from this
sandbox).
