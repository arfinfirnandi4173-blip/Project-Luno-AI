# LUNO P0.6.2-FIX — Vision Runtime Parity / YOLO Detection Recovery

## 1. What triggered this sprint

For the first time in this project's history, the user ran a delivered
observer script (`luno_live_camera_event_observer.py`) on their real
machine and reported **real, live output** (not a sandbox simulation).
The result:

- RTSP camera open: **SUCCESS** (`actual_backend='FFMPEG'`, camera state
  `UNKNOWN -> AVAILABLE`).
- Tracked-object (YOLO) detection: **FAILED**, every cycle, with:

  ```
  'Conv' object has no attribute 'bn'
  ```

- The user noted `main.py`'s own runtime had *previously* produced real
  detections (`person`, `television`, `couch`, `chair`, "person looking
  away", "person walking") from the same camera — so the brief framed
  this as a **runtime-parity** question: why can `main.py`'s Vision path
  detect a person while this observer's cannot, using what should be the
  same pipeline?

## 2. Baseline (measured before any change)

Targeted regression set (P0/P0.5.x/Vision/Sprint72/P0.6/P0.6.1/P0.6.2 +
`luno/core`/`luno/adapters` test packages): **428 passed, 0 failed.**

## 3. Source-of-truth trace (Section 3) — main.py vs. the observer

Both were read directly, not assumed:

| Aspect | `main.py` | `luno_live_camera_event_observer.py` | Identical? | Consequence |
|---|---|---|---|---|
| Config resolution | `LauncherConfig.load()` (line 66) | `LauncherConfig.load()` (unchanged since P0.5.4-FIX) | **Yes** | `.env`'s `VISION_BACKEND=real` is honored by both |
| Bootstrap sequence | `register_all_modules(runtime, launcher_config)` then `register_all_adapters(runtime, launcher_config)` | Identical call sequence, identical argument shape | **Yes** | Both build the exact same module/adapter graph |
| `VisionSource` construction site | `luno/bootstrap/adapters.py` (the only place `RealVisionSource()` is ever constructed — confirmed by a direct count: exactly 1 occurrence in the whole repo) | Same file, same site (reached via the same `register_all_adapters()` call) | **Yes** | There is no second/duplicate Vision implementation anywhere in this project |
| Detection function (tracked path) | `luno.vision.detect_objects_tracked()` via `RealVisionSource._tracked_cycle_loop()` | Same function, same call path (the observer never touches `luno.vision` directly — it only boots the same adapters `main.py` boots) | **Yes** | Whatever main.py's tracked loop does, the observer's tracked loop does too |
| Model getter | `_get_yolo_tracking()` → delegates to `_get_yolo()` (shared singleton, see Section 8 below) | Same | **Yes** | One resident model instance either way |
| Model path | `config.YOLO_MODEL_PATH` (`.env` override or default `"yolo11n.pt"`) | Same `luno.config` module, same attribute | **Yes** | No possibility of the two resolving to different weights |
| Confidence / IoU / image size / tracker config / classes / half precision / device | All read from `luno.config` (`CONFIDENCE_THRESHOLD`, `_device_arg()`, etc.) inside `luno/vision.py` itself | Same — the observer never overrides any of these | **Yes** | No config drift is possible; both processes read the same `.env`/`luno/config.py` |
| Python executable / ultralytics / torch / OpenCV versions | Whatever the user's `.venv` has installed | Same `.venv` (the observer is a script in the same repo, run with the same interpreter) *in principle* — but **not directly observed by the agent**, since the agent cannot execute this on the user's machine | **Presumed yes, not directly verified** | See Section 6 fix below — the observer now prints these values so this can be *verified*, not just assumed, on every future run |

**Conclusion: "runtime parity" between main.py and the observer was
already true before this sprint.** There is no duplicate Vision
implementation, no second config system, and no different model/tracker
wrapper. The observer is, and always was (since P0.5.4-FIX), a thin
bootstrap of the exact same production stack `main.py` boots.

## 4. Model identity audit (Section 5)

Both processes resolve the identical `config.YOLO_MODEL_PATH` /
`config.YOLO_POSE_MODEL_PATH` through the identical `_get_yolo()`
/`_get_yolo_pose()` cached singletons in `luno/vision.py`. No model
replacement was made or needed — this was never a "wrong model" problem.

## 5. Dependency/checkpoint audit (Section 6)

- `requirements.txt` pins `ultralytics>=8.3.0` — an **open lower bound**,
  not an exact pin. Any `ultralytics` version `>= 8.3.0` currently
  installed satisfies it, including one released long after these
  checkpoint files were last verified to work.
- `yolo11n.pt` and `yolov8n-pose.pt` are **committed binary files at the
  repo root** — static, not re-downloaded automatically on every run.
  They only change if manually deleted or replaced.
- `luno/vision.py` already contained a diagnostic helper,
  `_yolo_checkpoint_hint(ex)`, **written before this sprint**, whose
  entire docstring describes this *exact* failure signature
  (`AttributeError` with `.name == "bn"`) as ultralytics' own
  `BaseModel.fuse()` permanently `delattr`-ing `.bn` off `Conv`/
  `ConvTranspose` layers on first real inference — and states that if
  the on-disk `.pt` checkpoint predates the *currently installed*
  `ultralytics` version, the reconstructed module graph can disagree
  with what today's `Conv.forward()` expects, producing exactly this
  error.

This is strong, code-based (not speculative) evidence that the
underlying trigger is a **stale/mismatched local checkpoint vs. the
currently installed `ultralytics` package** on the user's machine — not
a Luno code defect in how the model is loaded or invoked.

**No dependency was changed in this sprint** (Section 2 of the brief:
"do not pip install a newer/older ultralytics... unless the audit
proves it is the actual root cause" — the audit is *suggestive*, not
proof obtainable from inside this sandbox, since the agent cannot
inspect the user's actual installed `ultralytics`/checkpoint versions).
Instead, the fix below gives the user the exact evidence needed to
decide for themselves (Section 7 below).

## 6. Fusion / double-instantiation audit (Section 7/8)

- `grep -n "fuse" luno/vision.py` → the word "fuse" appears **only**
  inside the `_yolo_checkpoint_hint()` docstring (describing
  ultralytics' own internal behavior). **`luno/vision.py` never calls
  `.fuse()` explicitly, anywhere.** The "double fusion" hypothesis is
  **disproven** by direct inspection of the actual code.
- `_get_yolo_tracking()`'s own docstring documents a *prior* fix (a "RAM
  bug" fix) that collapsed what used to be two independent
  `YOLO(config.YOLO_MODEL_PATH)` instances into one shared, cached
  singleton — `_get_yolo_tracking()` now simply returns `_get_yolo()`.
  There is exactly **one** resident detection-model instance shared by
  both the presence-watch loop (`detect_objects()`) and the Sprint 8
  tracked-cycle loop (`detect_objects_tracked()`).
- Both `detect_objects()` and `detect_objects_tracked()` call the model
  the same plain way: `model(frame, verbose=False, conf=..., device=...)`
  — **no** `tracker=`/`persist=True` argument in either. There is no
  tracker/model-wrapper divergence between the two call sites (confirmed
  by direct source inspection, and by
  `test_10_detect_objects_tracked_calls_model_the_same_plain_way_as_detect_objects`
  in this sprint's new test file).

**Conclusion: the fusion/double-instantiation hypothesis is disproven
from the actual code.** Whatever fusion ultralytics performs happens
once, internally, on the first real inference call — not something this
codebase's own code triggers twice.

## 7. The one genuine code defect found (Section 13) — and the actual fix

`luno/vision.py::detect_objects_tracked()` has always had (Sprint 8,
unchanged) a contract of `except Exception: return []` — it never
raises. That is *correct* for its callers (a polling loop must not
crash on a bad frame), **but it meant a genuine detector failure (e.g.
this exact Conv.bn error) was indistinguishable from "the model ran
fine and legitimately found nobody in frame."** Both produced an empty
list. Downstream, `RealVisionSource._tracked_cycle_once()` would then
call `listener.on_vision_cycle()` with empty `objects`/`humans` exactly
as if the room were empty — and if a person had previously been tracked,
the existing hysteresis/hold-timeout in `VisionAdapter` could eventually
emit `CameraPersonLeft` → `human_cleared`, even though the real cause
was "the detector broke," not "they left." This is precisely the defect
Section 13 of the brief asks to be fixed, and it is a genuine, provable
Luno code gap — independent of whatever ultimately turns out to cause
the Conv.bn error itself.

### Fix (additive only, 3 files)

**`luno/vision.py`** — new module-level cache
`_last_tracked_detection_error: Optional[str]` plus a new getter,
`last_tracked_detection_error()`. `detect_objects_tracked()`'s own
`[]`/never-raises **contract is unchanged** — every existing caller
sees zero behavior difference. What's new: the `except` branch now also
records `f"{type(ex).__name__}: {ex}{_yolo_checkpoint_hint(ex)}"` into
that cache (reusing the *existing* checkpoint-hint diagnostic, not a new
one); a successful cycle (or a cycle with no frame to try at all — a
camera hiccup is not a detector failure) clears it back to `None`.

**`luno/adapters/real_vision.py`** — `_tracked_cycle_once()` now checks
`self._vision.last_tracked_detection_error()` right after calling
`detect_objects_tracked()`. If set, it publishes the **existing**
`SystemError` event class (the same one `supervisor.py`/`lifecycle.py`/
`BaseAdapter` already use — no new event type invented) with
`data={"adapter": "vision", "error_type": "vision_detection_failed",
"error": <message>}`. The cycle still proceeds and still calls
`listener.on_vision_cycle()` with empty results exactly as before — this
sprint does not change tracking-loss/hysteresis semantics, only adds a
distinct, additive failure signal alongside the existing behavior.

**`luno_live_camera_event_observer.py`** — subscribes to `system_error`,
filters to `error_type == "vision_detection_failed"`, and reports a
distinct `[VISION_DETECTION_FAILED]` line (never `human_cleared`, never
silently folded into "no detection this cycle"). The final evidence
block now has its own "Vision detector health" section, separate from
the Vision/camera_automation counts, with an explicit cross-check note
if `camera_person_left` also fired in the same window. The script also
now prints **real runtime versions** (Python/executable, `ultralytics.
__version__`, `torch.__version__` + CUDA availability, OpenCV version,
resolved `YOLO_MODEL_PATH`/`YOLO_POSE_MODEL_PATH`) right after
pre-flight (Section 6) — the one piece of Section 3's comparison table
this sandbox could not fill in directly, now filled in automatically on
the user's own next run.

### What was deliberately NOT changed

- `detect_objects_tracked()`'s return contract (`[]`, never raises).
- Tracking/hysteresis/`human_cleared` timeout logic in
  `luno/adapters/vision.py`/`VisionAdapter` — untouched.
- `luno/camera_automation/module.py`, `luno/camera_automation/
  vision_bridge.py`, `luno/automation/engine.py`, `luno/automation/
  models.py`, `luno/automation/conditions.py` — untouched.
- `config/automation_rules.json` — both rules (`camera_human_detected_
  log`, `camera_human_detected_test_action`) byte-for-byte unchanged.
- No dependency version, no `requirements.txt` change, no model file
  deleted/replaced.

## 8. Detection smoke test (Section 10)

`tests/test_p0_6_2_fix_vision_runtime_parity.py::
test_21_real_yolo_detects_a_person_in_a_real_photo_if_environment_supports_it`
is written to run for real: it calls the actual production
`detect_objects_tracked()` against a real photograph containing a person
(`grace_hopper.jpg`, already present on disk inside this project's own
installed `matplotlib` package — not fabricated for this test) and
asserts `"person"` appears in the results with no detection error.

**Honest limitation:** this sandbox has no `ultralytics` package
installed (`python -c "import ultralytics"` → `ModuleNotFoundError`) and
no network route to download it, so this test **skips itself** here
(runtime-checked `pytest.skip`, not a hardcoded/fabricated pass) and
will only actually execute — and prove real detection — on a machine
where `ultralytics`/`cv2` are importable, such as the user's real
machine.

## 9. Tests

`tests/test_p0_6_2_fix_vision_runtime_parity.py` — 21 tests:

- **A. Configuration** (5) — observer uses `LauncherConfig.load()` (not
  a bare constructor), warns rather than silently using mock, never
  hardcodes a model path/weights filename, prints Section 6 runtime
  versions, new getter defaults to `None`.
- **B. Runtime parity** (5) — main.py/observer bootstrap call sequences
  compared directly; exactly one `RealVisionSource()` construction site;
  no explicit `.fuse()` call anywhere in `luno/vision.py`;
  `detect_objects_tracked()`/`detect_objects()` share one model
  instance; both call the model the same plain way (no tracker/persist
  argument divergence).
- **C. Error handling** (7) — `detect_objects_tracked()`'s `[]`/
  never-raises contract is unchanged on failure; the Conv.bn error is
  recorded distinctly (with the checkpoint hint still attached); the
  error clears after a successful cycle; a missing frame is not
  misreported as a detector failure; `RealVisionSource._tracked_cycle_
  once()` publishes the new `system_error` signal on failure and does
  NOT publish it on success; the observer's `on_system_error` handler
  never references `human_cleared`/`camera_person_left` (static proof).
- **D. Safety** (3) — both automation rules unchanged on disk (target
  still `light.wled`); this sprint's file scope never touches
  `luno/automation/*`/`luno/camera_automation/*`; the existing
  `event.kind`-matching condition mechanism is unaffected
  (`camera_online` still never matches `human_detected`).
- **E. Smoke test** (1) — see Section 8 above.

## 10. Regression

Targeted set (same suite as the baseline in Section 2, plus this
sprint's new file): **448 passed, 1 skipped (honest, documented — see
Section 8), 0 failed.** (428 baseline + 21 new = 449; one of the 21 is
the environment-gated skip.)

An additional 8-file sweep of every other test file in the repo that
references `luno.vision`/`real_vision`/camera in any way (`test_camera_
health_check_timeout.py`, `test_memory_retrieval.py`, `test_production_
launcher.py`, `test_real_adapters.py`, `test_screen_ask_screen.py`,
`test_sprint69_1_camera_dashboard_forensics.py`, `test_sprint69_camera_
stability.py`, `test_state_isolation.py`) found 3 failures, all
**outside this sprint's diff scope and pre-existing/environmental**, not
caused by this fix:

- `test_production_launcher.py::test_07_health_checks_all_pass_in_
  default_mock_configuration` — fails on `['OpenRouter', 'Fish Audio
  API']`, both external network health checks; this sandbox's outbound
  proxy returned `403 Forbidden` reaching `api.openai.com` in the same
  run (visible in captured log output) — a sandbox network restriction,
  not a Vision/camera issue. Neither `luno/adapters/openrouter.py` nor
  any Fish Audio file was touched this sprint.
- `test_real_adapters.py::test_real_whisper_source_calls_listener_in_
  order_for_nonempty_text` / `test_real_whisper_source_skips_empty_
  transcription` — both fail inside `luno/adapters/real_whisper.py`
  (`AttributeError: 'RealWhisperSource' object has no attribute
  '_device_index'`), a file never opened or modified this sprint, and
  unrelated to Vision/YOLO/camera code entirely.

A full literal `pytest -q` run over the entire repository could not be
completed within this environment's per-tool-call time budget (attempts
timed out around 3 minutes; the two `tests/test_*main_bargein.py` files
also fail to even collect here due to a pre-existing missing
`legacy_main.py` file, unrelated to this sprint) — this is an honest,
documented limitation of this sandbox, not a fabricated full-suite pass.
The targeted set above is the same style of regression gate every prior
sprint in this project has used and is judged sufficient: it directly
covers every file this sprint touched plus every camera/Vision/
automation-adjacent test file in the repository.

## 11. Live verification (Section 17/18)

**Not performed by the agent — same structural, repeatedly-confirmed
sandbox limitation as every prior sprint in this line** (no `ultralytics`
installed, no network route to the user's camera). This sprint cannot
prove, from inside this sandbox, that the Conv.bn error is actually
resolved on the user's real machine, because the root cause (Section 5)
is believed to be a version/checkpoint state that only exists on that
machine.

**Result classification: BLOCKED** (for the agent's own live-hardware
attempt) — per the brief's own Section 18 definitions: the environment
prevents the test (no camera, no ultralytics, no network). This is
distinct from **FAIL** (runtime reachable but detection still broken)
and must never be reported as **PASS**.

### What changed for the user's next run

The fix itself does not (and structurally cannot, without touching
dependencies the brief forbids touching blindly) *guarantee* the Conv.bn
error stops occurring — that depends on facts only observable on the
user's machine. What this sprint guarantees instead:

1. If the Conv.bn error still occurs, the user will now see a distinct
   `[VISION_DETECTION_FAILED]` line instead of the failure silently
   looking like "no detections this cycle."
2. The observer now prints the exact `ultralytics`/`torch`/OpenCV/Python
   versions in use, so the user (or a future sprint) can directly compare
   them against when the checkpoint files were last known-good, instead
   of guessing.
3. If, after reviewing that printed evidence, the user confirms a
   version/checkpoint mismatch, the codebase's own pre-existing
   diagnostic (`_yolo_checkpoint_hint()`) already states the fix:
   `pip install -U ultralytics` and delete `yolo11n.pt`/
   `yolov8n-pose.pt` so they re-download fresh weights compatible with
   the installed version. This sprint deliberately leaves that decision
   and action to the user (Section 2's own instruction), now backed by
   hard evidence instead of a guess.

## 12. Diff audit — files touched this sprint

- `[MODIFIED]` `luno/vision.py` — additive only (new cache + getter;
  existing `detect_objects_tracked()` contract unchanged).
- `[MODIFIED]` `luno/adapters/real_vision.py` — additive only (new
  `system_error` publish call inside the existing try block; no other
  line changed).
- `[MODIFIED]` `luno_live_camera_event_observer.py` — additive only
  (new `_print_runtime_versions()`, new `on_system_error` handler, new
  subscription, extended evidence block, extended module docstring).
  Root-level script, not under `luno/`.
- `[NEW]` `tests/test_p0_6_2_fix_vision_runtime_parity.py` — 21 tests.
- `[NEW]` `docs/change_impact/camera_automation_p0_6_2_fix.md` — this
  file.

**Not touched:** `config/automation_rules.json`, `luno/camera_
automation/*.py`, `luno/automation/*.py`, `luno/adapters/vision.py`
(the `VisionAdapter`/tracking/hysteresis logic itself), `requirements.
txt`, any `.pt` model file, `.env`.

## 13. Limitations (honest, not glossed over)

- The actual, ultimate root cause of the Conv.bn error can only be
  confirmed on the user's real machine — this sprint provides the
  evidence and the fix for its *symptom* (silent failure masking), not
  a guaranteed cure for its *cause*.
- The detection smoke test (Section 8) is written to prove real
  detection but is environment-skipped here — it has never actually
  executed against a real model in this project's history; it will only
  do so when run somewhere ultralytics is installed.
- A full literal whole-repository `pytest` run could not complete within
  this environment's tool-call time budget; the targeted regression set
  (matching every prior sprint's own practice) is the basis for this
  sprint's "no unexplained regression" claim.
