# Camera Automation — P0.5.3: Vision Event → Camera Automation Bridge

## Objective

P0.5.2 discovered that `luno.adapters.vision.VisionAdapter` already
publishes real human-presence and camera-availability events on the
Event Bus (`CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected`), independent of both Home Assistant (unconfirmed
per P0.5.1) and pytapo (no live event API, per P0.5.2). This sprint
connects those already-existing events to the already-existing
`CameraAutomationModule` (P0/P0.5) via a thin bridge — no new computer
vision, no new automation rules.

## Existing Vision event sources (read from the actual implementation)

Confirmed by reading `luno/adapters/events.py` and every publish call
site in `luno/adapters/vision.py` in full — per this sprint's own
Section 3 instruction not to assume the names were correct:

- `CameraPersonEntered` / `CameraPersonLeft` (`luno/adapters/events.py`
  lines 84–106) — debounced, room-level presence, published with **no
  `data` payload at all** (`self.publish(CameraPersonEntered())`, see
  `vision.py::_update_person_presence()`).
- `CameraDisconnected` / `CameraReconnected` (`events.py` lines
  190–204) — published with `data={"source": ..., "error": ...}` /
  `data={"source": ...}` (see `vision.py::on_camera_status()`).
- Also present, but deliberately **not** used by this bridge:
  `HumanEntered`/`HumanLeft` (per-tracked-individual, not room-level —
  see "Mapping" below for why), `PersonAppeared`/`PersonDisappeared`
  (raw, undebounced label-diff — the older, flappier signal
  `CameraPersonEntered`/`CameraPersonLeft` was built to harden),
  `PoseChanged`, `ObjectDetected`/`ObjectUpdated`/`ObjectLost`,
  `SceneChanged`, `VisionFrameProcessed`.

## Mapping

| Vision Event | CameraEvent.kind |
|---|---|
| `CameraPersonEntered` | `human_detected` |
| `CameraPersonLeft` | `human_cleared` |
| `CameraDisconnected` | `camera_offline` |
| `CameraReconnected` | `camera_online` |

**Why `CameraPersonEntered`/`CameraPersonLeft` and not `HumanEntered`/
`HumanLeft`:** `HumanEntered`/`HumanLeft` fire per tracked individual
(`tracking_id`) — with two people in frame that's two independent
events and no single "is anyone here" boolean. Mapping those to one
canonical `human_detected`/`human_cleared` pair per `camera_id` would
require the bridge to re-implement its own presence-counting/debounce
state — exactly the "do not duplicate P0 deduplication" this sprint's
own Section 9 forbids. `CameraPersonEntered`/`CameraPersonLeft` are
already debounced and room-level (matching the granularity
`human_detected`/`human_cleared` already has for the HA-sourced path —
one binary_sensor, one on/off state), so they are the safest canonical
mapping per Section 4's own instruction.

**Motion (Section 5):** the existing Vision event pipeline has **no
generic motion event at all** — only person/object detection (YOLO).
Per the brief's explicit instruction, this bridge never fabricates
`motion_detected`/`motion_cleared`:

```text
Motion source:
    NOT AVAILABLE FROM EXISTING VISION EVENT PIPELINE
```

**Camera ID (Section 6):** `luno.vision`/`VisionAdapter` have no
camera-identity concept — `camera_source()` is documented as "the ONE
place that decides what cv2.VideoCapture(...) opens," a single global
source. The bridge assigns a fixed, configurable id at its own level
(`CAMERA_AUTOMATION_VISION_CAMERA_ID` env var, default `"tapo_c212"` —
matching the single profile key already shipped in
`config/camera_automation.json` since P0.5) — `luno.vision`/
`VisionAdapter` were not touched to introduce this.

**Confidence (Section 7):** always `None`. Confirmed by reading the
publish call sites — none of the four events carry a confidence value.
Matches `CameraEvent.confidence`'s own existing "ALWAYS None" contract
from the HA-sourced path (P0.5).

## Files

```text
[NEW]      luno/camera_automation/vision_bridge.py
[NEW]      tests/test_p0_5_3_vision_camera_bridge.py
[NEW]      docs/change_impact/camera_automation_p0_5_3.md
[MODIFIED] luno/camera_automation/module.py
[MODIFIED] luno/camera_automation/__init__.py
[MODIFIED] luno/bootstrap/modules.py
```

Confirmed via `find luno -newer <P0.5.2's change-impact doc>` — exactly
these four `luno/` files, nothing else. `VisionAdapter`
(`luno/adapters/vision.py`), the YOLO/OpenCV/RTSP pipeline
(`luno/vision.py`, `luno/vision_tracking.py`,
`luno/vision_human_state.py`), the Event Bus (`luno/core/event_bus.py`),
`AutomationEngine`, `luno/ha_client.py`, and pytapo integration files
were **not** touched.

### Why each modified file changed

**`luno/camera_automation/module.py`** — two small, additive methods on
`CameraAutomationModule`, explained in full in that file's own updated
docstrings:

- `is_enabled() -> bool` — a read-only accessor (`return self._config.
  enabled`) so the bridge can implement the same "disabled means zero
  footprint" contract `CameraAutomationModule.start()` itself already
  follows, without reaching into `self._config` from outside the class.
- `ingest_external_camera_event(camera_event) -> bool` — the one new
  entry point for a `CameraEvent` that did not originate from
  `device_state_changed` classification. Routes through the exact same
  `_publish_if_not_suppressed()` dedupe/cooldown the HA-sourced branch
  already uses (same key space, `(camera_id, kind)`) — no second
  dedup/cooldown implementation. Re-checks `self._config.enabled` and is
  wrapped in the same try/except pattern as `_on_device_state_changed`.

Nothing in the pre-existing `_handle()`/`_on_device_state_changed()`/
`_publish_if_not_suppressed()` code was changed — both methods are
additive, appended to the class.

**`luno/camera_automation/__init__.py`** — exports the new
`VisionCameraEventBridge` and documents the addition in the package
docstring. No existing export changed.

**`luno/bootstrap/modules.py`** — constructs
`VisionCameraEventBridge(camera_automation=camera_automation_module)`
immediately after `camera_automation_module` itself, adds it to the
existing `bind_event_bus` loop and `register_module` call sequence, and
to the returned module dict. No existing line in this file was changed
— only new lines added, in the same style/position as every other
module's own wiring.

This is the "minimal bootstrap/module wiring if absolutely necessary"
the brief's own Section 17 anticipates and explicitly permits.

## Deduplication (Section 9)

`vision_bridge.py` implements **no** dedupe/cooldown of its own. Every
incoming Vision event is translated and handed unconditionally to
`ingest_external_camera_event()`; that method's existing
`_publish_if_not_suppressed()` is what decides whether a
`camera_automation.camera_event` is actually published. Verified by
`test_19_ingest_external_camera_event_reuses_existing_dedupe_cooldown`.

## Failure isolation (Section 10)

Every subscriber method in the bridge is wrapped in its own
try/except that logs and swallows, mirroring
`CameraAutomationModule._on_device_state_changed`'s own contract.
Verified by `test_11`/`test_12`.

## Lifecycle (Section 11)

`VisionCameraEventBridge` is a `Module` (`dependencies =
["camera_automation"]`, so `ModuleManager`'s existing DFS ordering
starts it after, and stops it before, the module it feeds — no new
ordering mechanism). `start()`/`stop()` use the existing Event Bus
`subscribe()`/`unsubscribe()` — no new thread, no polling loop, no
scheduler.

## Feature flag (Section 12)

`start()` checks `camera_automation_module.is_enabled()` first and
subscribes to nothing at all if `False` — zero runtime footprint when
disabled, same contract `CameraAutomationModule.start()` itself follows.
`ingest_external_camera_event()` independently re-checks the same flag.
Verified by `test_13`/`test_14`/`test_22` (real bootstrap).

## No automation rules (Section 14)

`vision_bridge.py` never reads/writes `config/automation_rules.json`,
never calls a Home Assistant service, never imports `AutomationEngine`,
and contains no "if X then Y" logic anywhere — only event translation
and transport.

## Live verification

Attempted the same live-verification step P0.5/P0.5.1/P0.5.2 each
attempted: this sandbox has no camera hardware and no network route to
the RTSP stream (`CAMERA_URL`), so `RealVisionSource`'s own camera
open would fail here exactly as `luno.vision`'s own `camera_status()`
already documents for a genuinely unreachable camera — there is nothing
new to observe from this sandbox specifically. **No real Tapo C212
event was observed this sprint** — this is honestly reported, not
glossed over.

What WAS verified for real: `test_23_enabled_e2e_vision_event_reaches_
camera_automation_camera_event` runs the REAL, full `register_all_
modules()`/`register_all_adapters()` bootstrap, the REAL Event Bus, and
publishes an event of the exact same type `VisionAdapter` itself
publishes (`CameraPersonEntered`) — proving the TRANSPORT path (Event
Bus → bridge → `CameraAutomationModule.ingest_external_camera_event()` →
`camera_automation.camera_event`) genuinely works end to end, with zero
mocking of the bridge or `CameraAutomationModule` themselves. This is
event-transport verification, explicitly distinct from live-camera
verification (Section 13's own stated purpose) — it does not, and does
not claim to, prove that a real Tapo C212 currently produces these
Vision events in the user's own deployment.

## Tests

`tests/test_p0_5_3_vision_camera_bridge.py` — 26 tests across 9
sections (A: mapping x4, B: unknown event, C: confidence x4
parametrized, D: camera ID x4, E: failure isolation x2, F: feature flag
x3, G: no motion fabrication x2, H: `CameraAutomationModule` additions
x3, I: real bootstrap E2E x3). All 26 pass.

**Baseline (recorded before any new code this sprint):** `luno/
adapters/tests/test_adapters.py` + `tests/test_camera_presence.py` +
`tests/test_sprint69_1_camera_dashboard_forensics.py` + `tests/
test_vision_sprint8.py` + `tests/test_vision_ask_vision.py` + `tests/
test_vision_intent.py` + `tests/test_vision_intent_classifier.py` +
`tests/test_vision_provider.py` = 144 passed. `tests/
test_p0_camera_automation.py` (23) + `tests/
test_p0_5_camera_integration.py` (36) + `tests/
test_ha_camera_discovery.py` (8) + `tests/
test_tapo_camera_event_audit.py` (18) = 85 passed. `tests/
test_sprint72_automation_engine.py` = 78 passed. **Total baseline: 307
passed, 0 failed.**

**After:** the same 307, plus the new 26 = **333 passed, 0 failed.**
Zero new failures. `tests/
test_sprint68_mutation_audit_hardening.py` spot-checked again: 65/67
(the same 2 pre-existing, unrelated environmental failures documented
in P0.5.1/P0.5.2 — files dated Aug 11–18, predating every camera
sprint).

## Known limitations

- No real Tapo C212/RTSP event was observed this sprint (no camera
  hardware/network reachable from this sandbox) — the bridge's event
  TRANSPORT is proven end to end; whether `VisionAdapter` actually
  fires these events against the real camera in the user's own
  deployment was not, and could not be, verified here.
- `CameraPersonEntered`/`CameraPersonLeft` are room-level, not
  per-person — if a future need arises for "which specific person"
  granularity, that would require a different (likely `HumanEntered`/
  `HumanLeft`-based, with its own aggregation) design, not an extension
  of this bridge.
- The default `camera_id` (`"tapo_c212"`) is an assumption based on
  P0.5.2's own architecture-map finding that Vision's RTSP source and
  pytapo's PTZ target the same physical device — not independently
  re-verified this sprint.

## Next sprint (not implemented this sprint)

Recommended: a live-verification pass on the user's own machine (where
the RTSP stream and camera hardware are actually reachable) — start
Luno with `CAMERA_AUTOMATION_ENABLED=true` and `VISION_BACKEND=real`,
walk in front of the camera, and confirm a real `camera_automation.
camera_event` with `kind="human_detected"` is actually published (the
Dashboard's existing automation status view, or a temporary Event Bus
subscriber, would show this). Only after that live proof should a
future sprint consider writing an actual `automation_rules.json` entry
that acts on these events — explicitly out of scope for this sprint and
for the one recommended next, per this sprint's own closing principle:
"connect them," not "automate yet."
