# LUNO P0.6.3 — Unified Vision → Camera Automation Integration

## 1. Objective (restated)

Make sure Camera Automation consumes the same production Vision
pipeline the dashboard already uses — no second YOLO/RTSP/tracker
instance — and that person-presence semantics (`human_detected`/
`human_cleared`) are derived honestly from that one pipeline, including
when the detector itself fails.

## 2. Baseline

Two separate invocations (kept separate deliberately — see Section 9
below for why):

- Main targeted set (P0/P0.5.x/Vision/Sprint72/P0.6/P0.6.1/P0.6.2/
  P0.6.2-FIX + `luno/core`/`luno/adapters` test packages): **454 passed,
  1 skipped, 0 failed.**
- `test_sprint69_1_camera_dashboard_forensics.py` +
  `test_sprint69_camera_stability.py` (isolated): **37 passed, 0
  failed.**

## 3. Audit — the existing Vision architecture (Section 3)

Traced directly from the real code, not documentation:

| Consumer | File | Function | Data source |
|---|---|---|---|
| Dashboard rich view (person/television/couch/chair, tracking IDs, confidence) | `luno/dashboard/collectors.py::collect_vision()` | reads `adapter_manager.status_all()["vision"]`, which is `VisionAdapter._extra_status()` | `VisionAdapter._known_objects`/`_known_humans`, populated in `on_vision_cycle()` from the Sprint 8 tracked-cycle loop (`RealVisionSource._tracked_cycle_loop()` → `luno.vision.detect_objects_tracked()` → `_get_yolo_tracking()` → shared `_get_yolo()` singleton) |
| Camera Automation (`human_detected`/`human_cleared`) | `luno/camera_automation/vision_bridge.py::VisionCameraEventBridge` | subscribes to `CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/`CameraReconnected` | `VisionAdapter._update_person_presence()`, fed by `on_detections()`, fed by `RealVisionSource._poll_once()` → `luno.vision.last_detections()` → the plain presence-watch loop (`_watch_loop()` → `luno.vision.detect_objects()` → `_get_yolo()`, the SAME singleton) |

Both consumers ultimately trace back to the exact same `RealVisionSource`
instance (confirmed: exactly one `RealVisionSource()` construction site
in the whole repo, `luno/bootstrap/adapters.py`) and the exact same
cached YOLO model object (`_get_yolo()` — `_get_yolo_tracking()` already
delegates to it, a prior sprint's own "RAM fix"). There is no second
RTSP connection, no second model load, no second tracker.

**Important nuance, not previously documented:** the dashboard's rich
per-object view and Camera Automation's presence signal are fed by TWO
DIFFERENT, PRE-EXISTING background loops inside the ONE `RealVisionSource`
— `_tracked_cycle_loop()` (Sprint 8, runs at `VISION_FPS`, feeds the
dashboard) and `_poll_loop()` (pre-Sprint-8, runs at
`CAMERA_WATCH_INTERVAL_S`, feeds Camera Automation via
`CameraPersonEntered`/`CameraPersonLeft`). This is existing architecture,
not something this sprint introduces — both loops call the SAME shared
model instance, just on different schedules, exactly as they did before
Camera Automation existed at all.

## 4. Integration point selected (Section 4)

**Already correct, unchanged.** `VisionCameraEventBridge` (P0.5.3)
already consumes option 1 from the brief's own preference order —
"existing Vision event already emitted by production Vision"
(`CameraPersonEntered`/`CameraPersonLeft`/`CameraDisconnected`/
`CameraReconnected`). It imports no Vision/YOLO/RTSP code (confirmed via
AST-based test — see Section 8). No change was made to `luno/
camera_automation/vision_bridge.py` or `module.py` this sprint.

## 5. Person detection semantics (Section 5)

Already correct, unchanged. `VisionAdapter._update_person_presence()`
(pre-existing) already implements exactly the Enter/Stay/Leave/Re-entry
contract the brief describes: rises immediately on first detection, does
not repeat while continuously present, falls only after
`_person_absence_timeout_s` of sustained absence, fires again on
re-entry. `VisionCameraEventBridge` adds no second debounce (confirmed
via AST source check that its `_ingest()` method contains no
`time.sleep`/`threading.Timer`/timeout logic of its own).

## 6. Object data preserved (Section 6)

Confirmed via a dedicated test (`test_12_dashboard_object_list_
preserves_tv_couch_chair_labels`): feeding `VisionAdapter.on_vision_
cycle()` a cycle with `television`/`couch`/`chair` detections still
produces exactly those labels in `_extra_status()["objects"]`. Camera
Automation never reads `_extra_status()`/`_known_objects`/`_known_humans`
at all (confirmed via source scan of `vision_bridge.py`) — it is
structurally incapable of reducing this data, since it never touches it.

## 7. The one genuine gap found and fixed (Section 13)

P0.6.2-FIX's "distinguish detector failure from no detection" fix
(`last_tracked_detection_error()`) only covered `detect_objects_tracked()`
— the Sprint 8 tracked-cycle loop that feeds the **dashboard's** rich
view. It did **not** cover `detect_objects()` — the plain presence-watch
loop that **Camera Automation's own `human_detected`/`human_cleared`**
actually derive from. A detector failure in `detect_objects()` (e.g. the
same `'Conv' object has no attribute 'bn'` signature) was, before this
sprint, exactly as invisible as the tracked-cycle case: it silently
returned `[]`, which `_watch_loop()` stored as "no person" — and if a
person was already being tracked as present, `_update_person_presence()`
would, after the absence timeout, publish a **false**
`CameraPersonLeft()` → `human_cleared`, exactly the "invented state
transition" Section 13 forbids.

### Fix (additive only, same pattern P0.6.2-FIX established)

**`luno/vision.py`** — new `_last_presence_detection_error` module cache
+ `last_presence_detection_error()` getter. `detect_objects()`'s
`[]`/never-raises contract is **unchanged** — every existing caller
(`_watch_loop()`, `ask_vision()`'s hint text) sees zero behavior
difference. The `except` branch now also records the error (reusing the
existing `_yolo_checkpoint_hint()` diagnostic); success (or "no frame to
try") clears it.

**`luno/adapters/real_vision.py`** — `_poll_once()` now checks
`self._vision.last_presence_detection_error()` right after reading
`last_detections()`. If set: publishes the same `SystemError` event
class P0.6.2-FIX already established (`error_type="vision_detection_
failed"`, now with an additional `detector` field —
`"detect_objects"` vs `"detect_objects_tracked"` — so a consumer can
tell which of the two loops failed), and **skips calling
`listener.on_detections()` for that cycle entirely** — this is the
actual fix: rather than calling `on_detections([])` (which would look
exactly like "nobody here" to `_update_person_presence()`), the cycle is
simply skipped, so the presence state machine never even runs for a
cycle where detection didn't genuinely happen. A person already present
stays present; the absence timer simply doesn't advance that cycle.

No second presence-tracking mechanism was added — this is a single,
additive `if`/`else` around the existing call.

## 8. Tests

`tests/test_p0_6_3_unified_vision_camera_automation.py` — 31 tests:

- **A. Architecture** (6) — exactly one `RealVisionSource()` site; the
  tracked/presence loops share one model singleton; `vision_bridge.py`/
  `module.py` never reference cv2/ultralytics/YOLO/RealVisionSource/
  VideoCapture (AST-based, not substring — a docstring mentioning
  `cv2.VideoCapture(...)` in prose is not a violation); exactly one
  `camera_source()` definition; the bridge's subscribed event types match
  the real `EVENT_TYPE` constants exactly.
- **B. Detection** (5) — person enter/leave reach `human_detected`/
  `human_cleared` end to end through the real bootstrap; a continuously
  present person produces no duplicate `CameraPersonEntered`; leave-then-
  re-enter produces entered→left→entered; the bridge has no second
  debounce of its own.
- **C. Other objects** (2) — `television`/`couch`/`chair` labels survive
  through `on_vision_cycle()` into the dashboard's own status view;
  Camera Automation never reads that richer data.
- **D. Camera state** (2) — disconnect/reconnect reach `camera_offline`/
  `camera_online` end to end.
- **E. Error handling** (7) — the new getter defaults to `None`;
  `detect_objects()` records/clears the Conv.bn-shaped error correctly;
  `_poll_once()` skips `on_detections()` and publishes the distinct
  signal on failure, and calls `on_detections()` normally when there is
  no failure; a person already present is never reported as having left
  due to a detector failure; no `automation.completed` fires when no
  Vision event was ever published (simulating the skip); the failure is
  observable via `system_error`.
- **F. Automation** (5) — both existing rules
  (`camera_human_detected_log`/`camera_human_detected_test_action`)
  still match `human_detected` end to end; disconnect/reconnect events
  never trigger either rule; `config/automation_rules.json` is unchanged;
  a diff-scope marker confirms `luno/automation/*`/`luno/camera_
  automation/*` files exist and were not intended to change.
- **G. Dashboard** (3) — `collect_vision()`'s payload shape is unchanged
  and identical whether Camera Automation is enabled or disabled; the
  collector itself never imports/references camera_automation.

## 9. Regression

Same two-invocation split as the baseline (Section 2), plus the new
test file:

- Main targeted set: **454 → 485 passed** (454 + 31 new), **1 skipped
  (unchanged, honest environment-gated)**, **0 failed.**
- `test_sprint69_1_camera_dashboard_forensics.py` +
  `test_sprint69_camera_stability.py` (isolated): **37 passed, 0
  failed** — unchanged from baseline.

**Why these two groups are run separately, not as one combined
invocation (a finding from this sprint, not a new problem introduced by
it):** running `tests/test_vision_sprint8.py` in the *same* pytest
process as `tests/test_sprint69_camera_stability.py` /
`tests/test_sprint69_1_camera_dashboard_forensics.py` produces 13
failures that do **not** reproduce when either file runs alone or when
the sprint69 pair runs alone. Root-caused directly: `test_vision_sprint8.
py`'s own `_install_fake_real_vision()` helper (pre-existing, Sprint 8
era, never touched by this sprint or P0.6.2-FIX) does **raw,
non-restoring** module-level reassignment —
`vision_module.camera_status = lambda: {"connected": ..., "source": ...,
"error": ...}` (no `"state"` key, no try/finally, no `monkeypatch`
fixture) — and several other `luno.vision` functions the same way. If
that file runs before the sprint69 files in the same process, the real
`camera_status()`/`capture_frame()`/etc. stay permanently replaced by
these fakes for the rest of the session, and `test_sprint69_camera_
stability.py`'s own assertions against the real state machine fail with
`KeyError: 'state'`. This is a **pre-existing test-isolation defect**,
confirmed unrelated to any code this sprint (or P0.6.2-FIX) touched —
not fixed here (out of this sprint's file scope;
`tests/test_vision_sprint8.py` is not a preferred or required touched
file for P0.6.3), but documented so no future sprint mistakes it for a
production regression. See `known_limitations` in `project_handover.
json`.

An additional 5-file spot-check (`test_memory_retrieval.py`,
`test_production_launcher.py`, `test_real_adapters.py`,
`test_screen_ask_screen.py`, `test_state_isolation.py`) found ONE
regression this sprint genuinely caused — and fixed:
`test_real_adapters.py::test_real_vision_source_forwards_detections_
and_description_once` failed with `AttributeError: '_FakeVisionModule'
object has no attribute 'last_presence_detection_error'`, because that
test's own hand-written fake stand-in for `luno.vision` (predates this
sprint) did not implement the new getter `_poll_once()` now calls.
Fixed by adding a two-line `last_presence_detection_error()` returning
`None` to `_FakeVisionModule` in `tests/test_real_adapters.py` — the
same kind of test-compatibility update P0.6.2-FIX made to 4 other
pre-existing test files when its own additive change required it.
After the fix: 3 remaining failures in that 5-file spot-check
(`test_production_launcher.py::test_07` — OpenRouter/Fish Audio external
network health checks, proxy-blocked in this sandbox;
`test_real_adapters.py::test_real_whisper_source_*` — 2 tests, an
unrelated pre-existing `real_whisper.py` bug), identical to what
P0.6.2-FIX already documented as pre-existing and unrelated.

## 10. Live verification (Section 17/18)

**Not performed by the agent — same structural sandbox limitation as
every prior sprint in this line** (no `ultralytics`, no network route to
the camera). The user must run the real `main.py`, confirm the Vision
Dashboard still shows real detections, and perform the
Empty→Enter→Stay→Leave→Re-enter walk-test while watching both the
dashboard and the AutomationEngine log simultaneously (Section 18).

**Result classification: BLOCKED** (for the agent's own live-hardware
attempt) — per Section 25's own definitions. Everything provable from
inside this sandbox (architecture audit, the Section 13 fix, all 31 new
tests, full regression) is a genuine, verified PASS at the code level;
only the live walk-test itself remains unverified by the agent.

## 11. Performance (Section 19)

No new inference call was added — this sprint only added a getter call
(`last_presence_detection_error()`, a dict/attribute read, not a model
call) and a conditional skip inside `_poll_once()`. The two pre-existing
polling loops (`_poll_loop()` at `CAMERA_WATCH_INTERVAL_S`,
`_tracked_cycle_loop()` at `VISION_FPS`) are unchanged in cadence and
unchanged in which model functions they call. No before/after CPU/memory
measurement was performed in this sandbox (no real camera/GPU load to
measure) — this is an honest limitation, not a claimed measurement.

## 12. Diff audit — files touched this sprint

- `[MODIFIED]` `luno/vision.py` — additive only (new cache + getter;
  `detect_objects()`'s existing contract unchanged).
- `[MODIFIED]` `luno/adapters/real_vision.py` — additive only (new
  check + conditional skip + one new `system_error` publish call inside
  `_poll_once()`).
- `[MODIFIED]` `tests/test_real_adapters.py` — additive only (one new
  method on a pre-existing test fake, required by the above).
- `[NEW]` `tests/test_p0_6_3_unified_vision_camera_automation.py` — 31
  tests.
- `[NEW]` `docs/change_impact/camera_automation_p0_6_3.md` — this file.

**Not touched:** `luno/camera_automation/*.py`, `luno/automation/*.py`,
`config/automation_rules.json`, `luno/bootstrap/modules.py`, `main.py`,
the dashboard frontend (`luno/dashboard/static/index.html`), any YOLO
model file, RTSP configuration, the Home Assistant client. The audit
proved none of these needed to change — the "unified" architecture the
brief asks for already existed; only the Section 13 error-semantics gap
was real and required a fix.

## 13. Limitations (honest, not glossed over)

- Live hardware verification remains BLOCKED for the agent — same
  structural sandbox limitation as every prior sprint.
- The pre-existing `test_vision_sprint8.py` cross-file test-pollution
  issue (Section 9) was found and documented but not fixed — out of this
  sprint's scope. It only manifests in a specific file-combination order
  that no prior sprint's regression command happened to use; every
  regression command in this project's history (including this sprint's
  own) avoids it by construction, so it has never caused a false
  "production regression" report — but a future sprint touching test
  infrastructure should be aware of it.
- No before/after performance numbers were measured (Section 19) — no
  real camera/GPU load exists in this sandbox to measure against.
