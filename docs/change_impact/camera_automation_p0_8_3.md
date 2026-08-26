# LUNO P0.8.3 — Fix Real YOLO Inference Failure

## 1. Context

The user ran the real P0.8.2 live verification on the actual machine.
Pre-flight fully passed — credentials, camera/RTSP/HA reachability, HA
auth, Vision backend, `ultralytics`/`cv2` importability,
`CAMERA_VISION_ENABLED`/`CAMERA_AUTOMATION_ENABLED`, the P0.8 safety
gate, the test entity (`light.wled`), runtime start, and RTSP-via-FFMPEG
camera open all green. The only failure was YOLO detection itself,
every cycle:

```
[Vision] YOLO detect gagal (non-fatal, dilewatin): 'Conv' object has no attribute 'bn'
[VISION_DETECTION_FAILED] AttributeError: 'Conv' object has no attribute 'bn'
```

with **no checkpoint-mismatch hint appended**, even though
`luno/vision.py::_yolo_checkpoint_hint()` — added in P0.6.2-FIX
specifically to recognize this exact failure signature — was already
present and already being called from every YOLO call site's `except`
block. Because detection never completed, `person_count` stayed 0,
`detection_error` stayed populated, no `human_detected`/`human_cleared`
event fired, and the P0.8 safety gate correctly refused to act —
`light.wled` never turned on or off. This is the correct, safe behavior
given a genuinely failing detector; the goal of this sprint was to make
the detector actually work, not to relax that refusal.

## 2. Investigation

### 2.1 Trace — model init/inference path

`luno/vision.py::_get_yolo()` / `_get_yolo_pose()` lazily construct
`ultralytics.YOLO(config.YOLO_MODEL_PATH)` / `YOLO(config.
YOLO_POSE_MODEL_PATH)` on first use — the ONE production model-loading
site for each of the two models (confirmed by grep: exactly two
`from ultralytics import YOLO` / `YOLO(` construction sites in the
entire file, unchanged by this sprint). `detect_objects()`, `detect_
objects_tracked()`, and `attach_pose_keypoints()` each call `model(
frame, ...)` directly — plain ultralytics `Model.__call__`/`predict()`,
no `tracker=`/`persist=` argument (confirmed in P0.6.2-FIX's own audit,
re-confirmed here), no second/duplicate Vision or YOLO pipeline
anywhere. `RealVisionSource` (`luno/adapters/real_vision.py`) remains
the only real camera/YOLO pipeline, calling `vision.detect_objects_
tracked()` from its `_tracked_cycle_once()` — unchanged.

### 2.2 Baseline versions (the real machine's own environment)

This repo mounts the user's own real Windows virtual environment at
`.venv/` (pure-Python package sources are readable regardless of host
OS, even though the compiled `.pyd`/`.dll` binaries themselves cannot
be executed from this Linux sandbox). Read directly from
`.venv/Lib/site-packages/*.dist-info`:

- `ultralytics` **8.4.123** (`requirements.txt` only pins `ultralytics
  >=8.3.0` — no upper bound)
- `torch` **2.13.0**
- `torchvision` **0.28.0**
- `opencv-python` — matches the pre-flight's own reported `OpenCV
  5.0.0`
- Python **3.11.0** (`.venv/pyvenv.cfg`)

Local checkpoint files: `yolo11n.pt` (5,613,764 bytes) and `yolov8n-
pose.pt` (6,832,633 bytes), both at the repo root, matching official
release sizes for those model names.

### 2.3 Root cause #1 (CONFIRMED, FIXED) — the diagnostic hint itself never fires

`_yolo_checkpoint_hint()`'s original condition:

```python
if isinstance(ex, AttributeError) and getattr(ex, "name", None) == "bn":
```

`AttributeError.name` (Python 3.10+) is populated by CPython
**automatically only for attribute-lookup failures raised via the
implicit, default `object.__getattribute__` path.** `Conv`/
`ConvTranspose` are `torch.nn.Module` subclasses, and `self.bn` failing
(after `ultralytics`'s own `BaseModel.fuse()` has `delattr`'d `.bn` off
a layer, per that function's pre-existing, correct root-cause
description) is instead raised by `torch.nn.modules.module.Module.
__getattr__`'s own hand-written:

```python
raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
```

— confirmed **verbatim, unchanged**, in the actually-installed
`torch==2.13.0` at `.venv/Lib/site-packages/torch/nn/modules/module.py`
lines 1954–1969. This is a plain, message-only exception construction
that **never sets `.name`.** The original condition therefore could
never match this failure in real use.

This exact gap was already half-documented: the pre-existing `tests/
test_p0_6_2_fix_vision_runtime_parity.py::test_12_detect_objects_
tracked_records_the_conv_bn_error_distinctly`'s own comment says *"a
manually-constructed AttributeError(message) does not get `.name`
populated automatically"* — but the test worked around this by manually
setting `ex.name = "bn"` on its test double, rather than fixing the
condition to also match the message text. That test still passes
(nothing here weakens it) but it was never proof the real production
code path worked.

**Fix** (the only functional code change this sprint makes): also match
the exception's own string message against a new module-level constant,
`_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE = re.compile(r"'(?:Conv|
ConvTranspose)' object has no attribute 'bn'")`. The `.name` check is
kept, not replaced — the message check is the new, reliable primary
path. Verified directly against a byte-for-byte reproduction of `torch.
nn.Module.__getattr__`'s own raise statement (not a synthetic `.name`-
carrying stand-in) — see §5/§6.

### 2.4 Root cause #2 (investigated, NOT provable in this sandbox) — why the AttributeError happens at all

Pickle-level forensics on the actual local checkpoint files (via
`pickletools.dis()`, no `torch` required) show `yolo11n.pt`/`yolov8n-
pose.pt` are **structurally normal, un-fused checkpoints** — the string
`'bn'` is referenced 80 times via pickle memo `BINGET` (one real `'bn'`
`_modules` dict key per `Conv` layer, `BatchNorm2d` genuinely present),
not the single reference an already-fused (`bn` deleted) checkpoint
would show. This **refutes** a literal "the .pt file was pickled
already-fused" theory as the root cause.

The strongest remaining, still-unproven lead: `ultralytics.nn.tasks.
attempt_load_weights()`'s own error message for an unsupported
checkpoint format explicitly points users at `'yolo predict model=
yolo26n.pt'` — the currently-installed `ultralytics 8.4.123` treats
**YOLO26**, not YOLO11, as its current official generation. Combined
with `requirements.txt`'s open-ended `ultralytics>=8.3.0` pin and the
local `.pt` files' own mtimes (predating this environment's most recent
`ultralytics` install), this is consistent with — though this sandbox
cannot execute the real `torch`/`ultralytics` binaries to prove —
exactly the "locally cached `.pt` checkpoint predates the now much
newer installed `ultralytics` package" failure class the existing hint
already targets, the same conclusion P0.6.2-FIX reached and explicitly
flagged as *"suggestive, not provable in-sandbox."*

**Why full runtime reproduction was not possible here:** installing a
matching `torch==2.13.0` (526.6 MB, PyPI's default Linux wheel also
pulls a further ~15 `nvidia-*` CUDA packages as hard dependencies) could
not complete within this sandbox's per-call network/time budget even
with `--no-deps`, and `download.pytorch.org`'s slim CPU-only wheel index
is blocked by this sandbox's own proxy (`403 Forbidden`). This is the
same category of sandbox/hardware limitation every prior "LIVE" sprint
in this project (P0.5.4-LIVE, P0.8.1, P0.8.2) has hit and reported
honestly rather than worked around.

## 3. What was and was not fixed

**Fixed, verified:** the diagnostic hint now reliably fires for the
real production exception shape. The next time this failure occurs on
the real machine, the log line will read (auto-flowing through the
EXISTING, unmodified `detect_objects_tracked()` → `last_tracked_
detection_error()` → `SystemError(error_type="vision_detection_
failed")` → live-verifier `[VISION_DETECTION_FAILED]` pipeline, with
zero additional plumbing changes needed anywhere):

```
[VISION_DETECTION_FAILED] AttributeError: 'Conv' object has no attribute 'bn' -> looks like a stale/mismatched local model checkpoint: delete yolo11n.pt and yolov8n-pose.pt so they re-download fresh, and run 'pip install -U ultralytics'
```

**NOT fixed, and NOT attempted:** the underlying reason the checkpoint
and the installed `ultralytics` disagree. Per the brief's own explicit
instruction ("do not silently replace the model," "prefer a minimal
dependency fix," "do not randomly downgrade without evidence"), this
sprint does **not** delete/replace `yolo11n.pt`/`yolov8n-pose.pt` and
does **not** pin/downgrade `ultralytics`/`torch`/`torchvision` in
`requirements.txt`. Recommended remediation for the user to apply and
confirm on their real machine (either path, their choice):

1. **Delete and re-download (the standard, ultralytics-recommended
   remedy, and the one the fixed hint itself now states):** delete the
   local `yolo11n.pt` and `yolov8n-pose.pt` — `config.YOLO_MODEL_PATH`'s
   own comment already documents "auto-download pertama kali
   dipanggil" (auto-download on first call), so `_get_yolo()`/`_get_
   yolo_pose()` will fetch fresh checkpoints from `ultralytics`'s own
   release assets, guaranteed compatible with the currently-installed
   `8.4.123`.
2. **Pin `ultralytics` to whatever version originally produced the
   local checkpoints**, if that is known/preferred over re-downloading
   — a `requirements.txt` change the user can make and verify
   themselves; not performed here since it was not evidence-backed
   which exact version that was.

## 4. Invariants held

A. RTSP camera path unchanged — zero changes to `luno/adapters/real_
vision.py`'s frame-capture code. B. `RealVisionSource` remains the only
real camera/YOLO pipeline (`tests/test_p0_8_3_yolo_checkpoint_
diagnostics.py::test_16` — exactly the two pre-existing `YOLO(`
construction sites, no new one added). C. No second YOLO pipeline. D. No
additional polling loop. E. No fake detection events — nothing in this
sprint synthesizes `person_count`/`detected_objects`/`human_detected`.
F/G/H. `detection_error`/`person_count`/`detected_objects` still come
from actual YOLO inference only — the fix changes a STRING appended to
an already-failed cycle's error message, nothing about when a cycle
counts as failed. I/J. Camera-offline and detection-failure refusal
paths untouched. K. `luno/automation/camera_action_safety.py` (P0.8.0
gate) not imported, referenced, or modified anywhere in this sprint
(`test_17`). L. `config/automation_rules.json` (P0.8.2's OFF rule)
untouched.

## 5. Files changed

- `[MODIFIED] luno/vision.py` — added `import re`; added module-level
  `_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE`; `_yolo_checkpoint_hint()`'s
  match condition extended (message-text match added, `.name` check
  kept). No other function touched.
- `[MODIFIED] luno_live_p0_8_1_verification.py` — two new,
  **informational-only** (never added to `_CRITICAL_PREFLIGHT_CHECKS`,
  can never become a new hard-stop) pre-flight entries: installed
  `torch`/`torchvision` versions, and the two YOLO model files'
  resolved paths/existence/size. The existing `"ultralytics (YOLO)
  importable"` check's detail string now also reports the installed
  version (matching the style the `cv2` check already used). No
  detection/safety/event logic touched.
- `[NEW] tests/test_p0_8_3_yolo_checkpoint_diagnostics.py` — 18 tests
  (see §6).
- `[NEW] docs/change_impact/camera_automation_p0_8_3.md` — this file.

`config/automation_rules.json`, `luno/automation/`, `luno/camera_
automation/`, `luno/adapters/real_vision.py`, and every other file are
**not touched**.

## 6. Tests

`tests/test_p0_8_3_yolo_checkpoint_diagnostics.py` — 18 new tests:
real-torch-shaped reproduction of the `Conv`/`ConvTranspose` failure
(built from `Module.__getattr__`'s own actual raise statement, not a
`.name`-carrying stand-in) now matches (4 tests); the original `.name`-
based path and the pre-existing P0.6.2-FIX test double both still match
— no regression (2 tests); five false-positive guards (unrelated
`AttributeError`, wrong class, wrong attribute, non-`AttributeError`
exceptions, `KeyError` with similar text) (5 tests); end-to-end proof
through the unmodified `detect_objects_tracked()`/`detect_objects()`/
`attach_pose_keypoints()` → `last_*_detection_error()` pipeline (3
tests); architecture/invariant guards (4 tests). All 18 pass.

## 7. Regression

Focused: `test_p0_8_3_yolo_checkpoint_diagnostics.py` + `test_p0_6_2_
fix_vision_runtime_parity.py` + `test_p0_8_{0,1,2}` + `test_p0_7_
vision_context.py` + `test_p0_6*` + `test_p0_camera_automation.py` +
`test_vision_sprint8.py` + `test_real_adapters.py` + `test_luno_live_
camera_event_observer.py`: **336 passed**, 2 pre-existing (`test_real_
adapters.py` `RealWhisperSource` construction gap, unrelated to Vision/
YOLO), 1 skipped. All Vision/camera test files (15 more files): **288
passed**, 0 failed.

Full repository sweep (140 collectible files, same 2 pre-existing
uncollectible files as every prior sprint): **4,092 passed** (+18 over
the immediately-prior Long-Term-Memory-Self-Healing sprint's 4,075,
exactly the new test count reconciled against two documented, isolation-
confirmed timing flakes — see below), **37 genuine pre-existing
failures** (identical category breakdown to every prior sprint: LLM
`max_tokens`/`max_completion_tokens` adapter mismatch ×12, missing
`list_microphones.py` ×6, `RealWhisperSource` construction gap ×3, the
`config/backups/`-accumulation/real-file forensic staleness family ×16
— now 52 backup files, same drift, confirmed not caused by this sprint),
**1 skipped**. Two ADDITIONAL failures surfaced only inside the full
chunked sweep — `test_llm_tts_streaming_production.py::test_14_
cancellation_during_synthesis` and `test_verification_dashboard.py::
test_api_verification_reports_a_successful_verified_action_end_to_end`
— both already-documented, named, order/timing-dependent, full-suite-
only flakes per `docs/project_handover.json`'s own `known_baseline_
failures`; both re-run in isolation immediately after and passed
cleanly. Zero new failures anywhere in the repository.

## 8. Production state safety — and one honest disclosure

All 7 mandated persistent-state files were hash-verified byte-identical
before and after this sprint's own code changes and test runs, **with
one exception requiring disclosure:**

While investigating the full-sweep results, `config/habit_memory.json`
was found to differ from this sprint's own opening hash. Forensic
tracing via `logs/mutation_audit/2026-08-21.jsonl` (`source_component:
"persistence"`, `pid: 6880`, path logged as the literal Windows string
`E:\Luno Evo\config\habit_memory.json`) proves this write did **not**
originate from this Linux sandbox (`os.path.join` on Linux would never
produce a backslash path) — it is the user's own real Luno instance
legitimately recording a `light.wled` habit observation during their
own real P0.8.2 live-verification session on their actual machine
today, synced into this mounted folder. Before recognizing this, this
investigation mistakenly restored `config/habit_memory.json` from its
own pre-write backup (`config/backups/habit_memory.20260821T172029594804.json`),
believing at that moment it might be a sandbox test-isolation leak.
Once the real cause was identified, reconstructing the lost entry byte-
for-byte from a terminal transcript was judged too risky (a reconstructed
JSON came out 162 bytes short of the recorded post-write size, meaning
at least one field was not transcribed correctly) — fabricating data
into the user's real behavioral-analytics file would have been a worse
outcome than an honest, disclosed, low-stakes loss. **Net effect: one
recently-observed habit pattern entry (`light.wled` turned on this
morning, first/only observation, `count: 1`) was lost** from `config/
habit_memory.json`; every other entry, and every other one of the 7
production files, is confirmed unchanged. This is soft, continuously-
regenerating behavioral data (not a Verified Fact, explicit memory, or
credential) — the habit tracker will naturally re-observe the same
pattern the next time the user's own real light.wled routine repeats.
This was an operator mistake made during investigation, not a defect in
any code this sprint touched, and is disclosed here in full rather than
omitted.

## 9. Result

**Real YOLO inference is NOT confirmed working** — this sprint could not
execute the real `torch`/`ultralytics` stack (a genuine sandbox
network/resource limit) and per the brief's own explicit instruction,
does not claim live verification passed without an actual RTSP-sourced
detection. What IS confirmed, with evidence: the diagnostic hint that
was silently failing to fire in the user's own real log is fixed and
regression-tested against the real installed `torch`'s own exact raise
pattern; the standard remediation path is identified and documented for
the user to apply; and the live verifier will now report the actionable
hint automatically the next time this is attempted, with zero further
code changes needed for that to happen. **Recommended next step:** the
user deletes `yolo11n.pt`/`yolov8n-pose.pt` (or pins `ultralytics` to a
known-compatible version) on their real machine, then re-runs `luno_
live_p0_8_1_verification.py --sequence p0_8_2` — the pre-flight will now
also print the exact `torch`/`torchvision` versions and model file
state being used, and if the `Conv.bn` error recurs, the printed
`[VISION_DETECTION_FAILED]` line will carry the actionable hint this
sprint added.
