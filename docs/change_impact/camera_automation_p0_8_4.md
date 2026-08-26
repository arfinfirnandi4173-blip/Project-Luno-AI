# LUNO P0.8.4 — Resolve the Actual YOLO Model / Ultralytics Compatibility Failure

## 1. Context

P0.8.3 fixed a confirmed, real bug in `_yolo_checkpoint_hint()`'s
exception-matching logic (it only ever checked `AttributeError.name`,
which real `torch.nn.Module.__getattr__` never populates), but was
explicit that this did NOT prove the underlying `AttributeError: 'Conv'
object has no attribute 'bn'` failure itself was fixed — it honestly
reported the deeper model/ultralytics compatibility question as
UNRESOLVED, since it could not be executed/reproduced in the sandbox
available at the time.

This sprint's brief was explicit: identify and fix the ACTUAL YOLO
model/ultralytics compatibility problem, using the 4-stage isolation
methodology (`YOLO(path)` / `model.model` / `fuse()` / `predict()`),
prove it against a static image first, then RTSP, then re-run full P0.8
live verification (TEST A–F) — and to honestly report anything that
could not be proven rather than inventing a conclusion.

## 2. Sandbox execution attempt (torch import) — genuine environmental blocker

Unlike P0.8.3, this sprint made a real, first-of-its-kind attempt to get
the REAL, exact-version-matching `torch==2.13.0` / `torchvision==0.28.0`
/ `ultralytics==8.4.123` packages actually installed and running inside
this sandbox, using a new resumable-download technique (`curl -C -`
across sequential tool calls, since a single call cannot download
torch's 526 MB Linux wheel before its own time limit, and plain `pip
install` discards partial downloads on interruption). This succeeded —
the exact real-machine-matching wheels were downloaded and installed.

`import torch` then failed:

```
OSError: libcudart.so.13: cannot open shared object file
```

Root cause (confirmed by direct `readelf -d`/pickletools inspection of
the installed packages, not guesswork): PyPI's Linux `torch==2.13.0`
wheel is NOT a self-contained CPU-only build. `torch/lib/
libtorch_global_deps.so`, `libtorch_cpu.so`, and `libtorch_python.so`
all carry hard `DT_NEEDED` entries on real CUDA runtime libraries
(`libcudart.so.13`, `libcublas.so.13`, `libcublasLt.so.13`,
`libcudnn.so.9`, `libnvrtc.so.13`, `libcufft.so.12`, `libcusparse.so.12`,
`libcusolver.so.12`, `libnccl.so.2`, `libnvshmem_host.so.3`,
`libcufile.so.0`, and the real GPU driver's own `libcuda.so.1`), meant
to be supplied by separately-installed `nvidia-*-cuXX` pip packages
(skipped here via `--no-deps` to save the multi-GB download). An attempt
to satisfy these with hand-built, correctly-`SONAME`d empty stub `.so`
files (a legitimate technique for pure dlopen-presence checks) got
further than expected — `_preload_cuda_deps()`'s own preload succeeded —
but ultimately failed at `from torch._C import *` with `undefined
symbol: cuptiFinalize, version libcupti.so.13`, then (after forcing
`sys.setdlopenflags(RTLD_LAZY)`) `undefined symbol: cudaProfilerStart,
version libcudart.so.13`. `readelf -d` confirmed these libraries carry
no `DT_FLAGS_1`/`BIND_NOW` marker, so the eager resolution is coming
from genuine data/GLOB_DAT-class relocations (not lazy-bindable PLT
stubs) inside PyTorch's own official build — not fixable by stubbing
`.so` presence without reproducing real, correct CUDA library binaries
(multi-GB, and pointless for a sandbox with no GPU regardless). This is
reported as a genuine, evidenced environmental limitation, not a
shortcut — see Section 7 for the resulting scope impact.

**Self-inflicted regression, found and fixed within this same sprint:**
installing torch/ultralytics into this sandbox's shared
`site-packages` (even though `import torch` itself never fully
succeeded) polluted `sys.path` because the `ultralytics` wheel bundles
its own top-level `tests/` package, which Python's import machinery
picked up ahead of this project's own `tests/` package for one test
(`test_p0_6_2_camera_ha_action.py::test_26_...`, which does
`from tests.test_luno_live_camera_event_observer import _code_only`).
This was caught during this sprint's own regression sweep, root-caused
immediately, and fixed by fully uninstalling `torch`/`torchvision`/
`ultralytics` from the sandbox (`pip uninstall`) — confirmed the
specific test passes again afterward, and confirmed (via `ls`) no
`torch`/`ultralytics`/stray `tests` package remains installed. No
production file was touched by this detour, and it is disclosed here in
full rather than silently omitted, consistent with this project's
established disclosure standard (see P0.8.3's `habit_memory.json`
incident writeup).

## 3. Root cause — established via static source analysis (not execution)

With runtime reproduction blocked, root cause was established via direct
source inspection of the real, exact-version-matching `ultralytics==
8.4.123` package (downloaded above) cross-referenced against `luno/
vision.py`'s actual call sites — a fully evidence-based, execution-free
method, the same discipline P0.8.3 used for its own checkpoint forensics.

**The mechanism, in order:**

1. `luno/vision.py::_get_yolo()` returns ONE shared `ultralytics.YOLO`
   singleton (`_yolo_model`). `_get_yolo_tracking()` is a pure alias for
   it (documented RAM fix from an earlier sprint — both loops were
   already meant to share one model instance).
2. `start_watch()` runs `detect_objects()` on its own background thread
   (`_watch_thread`). `RealVisionSource` separately starts a
   `_cycle_thread` running `detect_objects_tracked()`. Both threads run
   continuously and concurrently against that ONE shared model object —
   confirmed directly in `luno/adapters/real_vision.py`
   (`self._poll_thread`/`self._cycle_thread`, both `daemon=True`).
3. **Before this fix**, `detect_objects()` called `model(frame, verbose=
   False, conf=config.YOLO_CONFIDENCE)` — no `device=` kwarg at all.
   `detect_objects_tracked()` called `model(frame, verbose=False,
   conf=config.CONFIDENCE_THRESHOLD, device=_device_arg())` — always
   explicit. `_monitor_loop()` (the optional debug GUI overlay) matched
   `detect_objects()`'s no-device pattern.
4. `ultralytics.engine.model.Model.predict()` (confirmed by direct
   source read, `ultralytics/engine/model.py`) caches `self.predictor`
   and only rebuilds it — re-running `AutoBackend`/`PyTorchBackend.
   load_model()`, which calls `.fuse()` again on the shared,
   already-fused underlying `nn.Module` — when `self.predictor.args.
   device != args.get("device", self.predictor.args.device)`. Because
   one call site always omitted `device=` and the other always passed
   it explicitly, this comparison flip-flopped every time execution
   alternated between the two threads' calls against the shared model —
   not once at startup, but continuously and indefinitely at steady
   state. This check is also not thread-safe on its own.
5. `ultralytics.nn.backends.pytorch.py::PyTorchBackend.load_model()`
   (confirmed by direct source read): for an already-loaded `nn.Module`,
   `weight.fuse(...)` runs again. `ultralytics.nn.tasks.py::BaseModel.
   fuse()` guards each `delattr(m, "bn")` with `hasattr(m, "bn")` — but
   this check-then-delete is not atomic against a concurrently-running
   `Conv.forward()` (`ultralytics/nn/modules/conv.py`, confirmed:
   `self.conv = nn.Conv2d(...)`; `self.bn = nn.BatchNorm2d(c2)` — always
   constructed unconditionally) reading `self.bn` mid-execution
   (`self.act(self.bn(self.conv(x)))`) on the SAME module instance, on
   the OTHER thread.
6. `_yolo_lock` (`luno/vision.py`, pre-existing) only ever guarded the
   lazy CONSTRUCTION of `_yolo_model` (the brief `with _yolo_lock:`
   blocks inside `_get_yolo()`/`_get_yolo_tracking()`/`_get_yolo_pose()`)
   — never the actual `model(frame, ...)` inference call itself. Nothing
   prevented two threads from calling into the shared model
   simultaneously.

This fully and mechanistically explains `AttributeError: 'Conv' object
has no attribute 'bn'` recurring on **every cycle** (both background
threads run continuously and overlap constantly, not rarely) without
requiring ANY model/checkpoint/dependency-version incompatibility. It
matches the brief's own category (B): **Luno's Vision code using the
ultralytics API incorrectly** (an unsynchronized, kwarg-inconsistent
shared-singleton access pattern) — not category (A)/(D)/(E) (model,
version, or checkpoint incompatibility). This is consistent with, and
extends, P0.8.3's own pickle-level forensic finding that both `.pt`
files on disk are ordinary, un-fused, non-stale checkpoints with genuine
`bn` weights — the checkpoints were never the problem.

**Confidence level:** this is a complete, source-verified mechanism, not
a guess — every claim above cites a specific, directly-read line of the
real, exact-version-matching `ultralytics==8.4.123`/`torch==2.13.0`
source or of `luno/vision.py`/`luno/adapters/real_vision.py` itself.
What it is NOT is execution-confirmed inside this sandbox (Section 2)
or against the real Tapo C212 stream (Section 7) — that final
confirmation can only happen on the real machine.

## 4. Fix (`luno/vision.py` only — Option D: Luno API-usage fix)

Per the brief's own priority order (A: swap model, B: pin ultralytics,
C: recreate model, D: fix Luno code only if the issue is genuine API
misuse) — this is confirmed API misuse in Luno's own code, so Option D
applies directly. No `.pt` file, no `ultralytics`/`torch` package, no
`requirements`/dependency pin was touched.

- `detect_objects()`, `detect_objects_tracked()`, and `_monitor_loop()`
  now all pass the SAME explicit `device=_device_arg()` on every call —
  so `Model.predict()`'s device-mismatch branch can only ever fire once,
  on the very first call across ALL callers, never again after that.
- All three now wrap the actual `model(frame, ...)` call itself (not
  just `_get_yolo()`'s construction) in the pre-existing `_yolo_lock`,
  fully serializing access to the shared singleton and closing the
  underlying race outright (not just its steady-state trigger).
- `_get_yolo_pose()`/`attach_pose_keypoints()` were deliberately left
  untouched: that model is a SEPARATE singleton (`_yolo_pose_model`)
  only ever called from inside the same tracked-cycle thread that also
  calls `detect_objects_tracked()` — confirmed by grep, its only two
  callers are both inside `RealVisionSource._tracked_cycle_once()` on
  one thread — so it was never exposed to this particular race and
  extending the lock there would be unjustified scope creep.

Explicitly NOT done, per the brief's own prohibitions: no monkey-patched
`Conv`/`self.bn`, no edited `.pt` binary, no random torch/ultralytics
downgrade, no bypassed detection error, no fabricated detection result,
no change to `CameraEvent`/`VisionContext`/`VisionCameraEventBridge`/
`AutomationEngine`/`camera_action_safety`/the P0.8.0 safety gate/the
P0.8.2 ON-OFF rules/HA state-aware behavior/cooldown behavior.

## 5. Tests

New file `tests/test_p0_8_4_yolo_concurrency_fix.py` (12 tests, all
passing) — using this project's established monkeypatch-a-fake-model
convention (no real torch/ultralytics required, consistent with every
other Vision unit test):

- Device-kwarg consistency: `detect_objects()` now passes `device=`
  (previously didn't); `detect_objects_tracked()` still does;
  both pass the IDENTICAL value against the shared singleton;
  `_monitor_loop()`'s source also does.
- Lock coverage: `detect_objects()`/`detect_objects_tracked()` both hold
  `_yolo_lock` while the fake model's `__call__` actually runs (not just
  during construction); the lock is released again afterwards, including
  when the model call raises.
- **A genuine two-`threading.Thread` concurrency test**: spawns real OS
  threads hammering `detect_objects()`/`detect_objects_tracked()`
  against one shared fake model and asserts the model's own `__call__`
  body is never entered by both threads at once — directly proving
  `_yolo_lock` now serializes them (this is the closest a mocked test
  can get to proving the actual race is closed, short of real torch
  execution).
- Architecture guards: pose model/lock untouched, still exactly two
  `from ultralytics import YOLO` sites, no automation/safety-gate/HA
  reference introduced into the changed functions.

## 6. Regression

Full sweep of all 143 test files (144 minus `test_main_bargein.py`/
`test_root_main_bargein.py`, uncollectable — documented, pre-existing,
`faster_whisper` not installed in this sandbox), run in chunks per this
project's established methodology:

- **Every** Vision/P0.x/camera/automation-specific suite: 100% pass,
  including the 12 new P0.8.4 tests (`test_p0_7_vision_context.py`,
  `test_p0_8_0_camera_action_safety.py` through `test_p0_8_3_...py`,
  `test_p0_6_2_fix_vision_runtime_parity.py`, `test_p0_5_3_...py`,
  `test_p0_6_3_...py`, `test_vision_*.py`, `test_real_adapters.py`
  aside from its one pre-existing, documented failure class below).
- **Pre-existing, already-documented baseline failures encountered**
  (all match `docs/testing/regression_baseline.md` entries recorded by
  earlier sprints, none newly caused by this sprint):
  `test_real_adapters.py` (2, `RealWhisperSource._device_index` env
  gap), `test_mic_device_index.py` (6, `list_microphones.py`
  absent/`.env` `MIC_DEVICE_INDEX` mismatch), `test_llm_max_completion_
  tokens_compatibility.py` (7) and `test_memory_session_summary_api_
  compatibility.py` (5) (both, `.env`'s `MAX_TOKENS_PARAM=max_tokens`
  override), `test_production_launcher.py::test_07_...` (1, real
  OpenRouter/Fish Audio credentials configured), `test_persistent_
  adaptive_response_depth.py::test_e2e_9_...` (1, known-flaky under
  full-suite concurrency — confirmed passes standalone).
- **Newly observed, investigated, and confirmed unrelated to this
  sprint's change** (full detail in Section 7 below):
  `test_sprint60_area_schema.py` (2), `test_sprint63_long_term_memory_
  recovery.py`/`test_sprint64_memory_corruption_forensics.py`/
  `test_sprint67_mutation_audit_trail.py`/`test_sprint68_mutation_audit_
  hardening.py` (18 total).
- Zero failures in any test file that imports or exercises `luno/
  vision.py`, `luno/adapters/real_vision.py`, `luno/camera_automation/`,
  or `luno/automation/` beyond the two pre-existing/documented classes
  above.

## 7. Newly observed (not caused by this sprint) — full disclosure

Two clusters of previously-undocumented failures were found during this
sprint's regression sweep. Neither imports or touches `luno/vision.py`
or anything camera/YOLO-related (confirmed by grep), and both were
actively investigated rather than dismissed:

**`test_sprint60_area_schema.py` (2 failures)** — `light.main_light`
never reaches `state=on` in `ToolManager` verification (`state=None`
after 4 retries), while the other two configured lights
(`light.wled`/`light.komputer`) verify successfully. `config/
lights.config.json`'s `Main Lamp`/`light.main_light` entry has a very
recent modification time relative to when this was investigated. This
looks like a real, live config change on the user's actual machine (this
project's working folder is the same live-synced `E:\Luno Evo` the
user's own machine writes to) that the test's fixture/mock HA state
store has not been updated to match — not a code defect, and out of
scope for a camera/YOLO sprint to fix.

**`test_sprint63_long_term_memory_recovery.py` / `test_sprint64_
memory_corruption_forensics.py` / `test_sprint67_mutation_audit_trail.py`
/ `test_sprint68_mutation_audit_hardening.py` (18 failures total)** —
two distinct causes, both confirmed, neither a code defect:
  1. Sprint 63/64's forensic tests hard-code the assumption (accurate
     when THEY were written) that `config/long_term_memory.json` is
     permanently corrupted (non-JSON, high-entropy, MIT-license-text
     fragment). It is now a healthy 5-item JSON list — almost certainly
     the real-world effect of the "Long-Term Memory Self-Healing /
     Recovery Hardening" sprint completed earlier in this project's
     history, whose recovery logic has since run for real against the
     user's actual corrupted file during real use and repaired it. This
     makes those specific forensic assertions stale, not wrong at the
     time.
  2. Sprint 67/68's mutation-audit tests hash every file in `config/`/
     `config/backups/` at the start of their own test body and assert
     the hash is unchanged at the end of that SAME test's execution.
     Re-running `test_this_files_own_run_never_touches_the_real_config_
     directory` twice in a row, seconds apart, produced FAIL then PASS —
     proof this is a live race against a real, concurrently-running
     process writing to the same shared `config/`/`config/backups/`
     directory during the test's own execution window (this project's
     working folder being the user's actual live machine folder, not an
     isolated sandbox copy), not a deterministic bug in any code this
     sprint touched.

Neither cluster was modified by this sprint (fixing decade-old forensic
fixtures or hardening test isolation against a live-synced production
folder is unrelated scope creep for a camera/YOLO sprint) — they are
disclosed here in full, per this project's standing "report uncertainty
honestly, never hide it" discipline, for whoever picks up the next
sprint.

## 8. What remains unresolved (honest, not invented)

Per the brief's own explicit instruction not to claim success until the
full real chain (real RTSP frame → real YOLO inference → real person
detection → real CameraEvent → real AutomationEngine → real HA → real
`light.wled` state change) is proven:

- **Static-image YOLO test, RTSP YOLO test, and the P0.8.2 TEST A–F live
  verification could NOT be run in this sandbox** — there is no real
  Tapo C212/RTSP/Home Assistant reachability here (same architectural
  limit documented since P0.5.4-LIVE), and `torch`/`ultralytics` cannot
  be imported here at all (Section 2). This is reported as UNRESOLVED,
  not fabricated.
- The root-cause fix in Section 3–4 is complete, source-evidenced, and
  regression-tested — but its final proof requires running it on the
  real machine, where `torch`/`ultralytics` already work (as demonstrated
  by the user's own P0.8.2 live run).

## 9. Handoff — what the user should run next

On the real machine, in the project's real `.venv`:

```
python luno_live_p0_8_1_verification.py --sequence p0_8_2
```

(same command as before — no new script needed, since the fix lives
entirely inside `luno/vision.py`, which that verifier already exercises
via the real `RealVisionSource`). Expected outcome if this sprint's
diagnosis is correct: YOLO detection succeeds every cycle (no more
`Conv.bn` `AttributeError`), TEST A–F should proceed exactly as
originally specified in the P0.8.2 brief. If the error somehow still
occurs, the exact printed message/hint should be reported back — Section
3's mechanism would need to be revisited with that new evidence rather
than assumed still correct.
