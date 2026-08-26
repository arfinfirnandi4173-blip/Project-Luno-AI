# Camera Device / OpenCV Stability Fix (Sprint 69)

**Type:** Bug fix, scoped strictly to the camera open/capture path. No new
feature, no change to Home Assistant, memory, mutation audit, tool
registry, browser security, planner, or area/group logic, and no change
to unrelated vision behavior (YOLO detection, Gemini/OpenAI vision-
language calls, presence tracking, pose estimation - all untouched).

## The reported problem (verbatim log)

```
[ WARN:0@30.152] global cap_ffmpeg_impl.hpp:453 opencv_ffmpeg_interrupt_callback Stream timeout triggered after 30072.392000 ms
[ WARN:1@30.162] global cap_ffmpeg_impl.hpp:1246 open VIDEOIO/FFMPEG: OpenCV should be configured with libavdevice to open a camera device
[ERROR:0@57.936] global obsensor_uvc_stream_channel.cpp:163 cv::obsensor::getStreamChannelGroup Camera index out of range
[01:29:55.910] [Luno.adapters.vision] 'vision' handled 'scheduled_vision_poll' (0.0ms)
✗ Camera - device index 0 did not respond within 5.0s (camera driver may be stuck/claimed by another app) - continuing without blocking startup
```

## Root cause (evidence-based)

`cv2.VideoCapture(index)` called with **no explicit backend argument**
lets OpenCV's own `CAP_ANY` auto-probe pick whichever backend it tries
first on the host. The log shows that auto-probe reaching **both**
`CAP_FFMPEG` (triggering the ~30s internal
`opencv_ffmpeg_interrupt_callback` stream timeout) **and**
`CAP_OBSENSOR` ("Camera index out of range") before ever reaching a
backend meant for a local USB/integrated webcam. `CAP_OBSENSOR` was
confirmed to genuinely exist in this project's own installed OpenCV
build (4.13.0), validating the log's authenticity.

FFMPEG is the **correct** backend for a network camera stream
(`CAMERA_URL`, RTSP/HTTP) - the bug is only that `CAP_ANY` can *also*
reach FFMPEG/obsensor for a plain local integer index, with no way to
opt out short of naming a backend explicitly. The fix is scoped to
exactly that: local (int index) sources now get an explicit,
platform-appropriate backend candidate list; string (URL) sources are
left completely alone.

`luno/bootstrap/health.py`'s existing startup probe already had a
background-thread + 5s-join pattern that correctly prevented this from
blocking **startup** (the log's own "did not respond within 5.0s ...
continuing without blocking startup" line proves that part was already
working). The more severe, previously-unmitigated bug was that
`luno/vision.py::_capture_frame()` - called by three independent polling
loops, the most frequent being `RealVisionSource._tracked_cycle_loop()`
at up to 2/s (`VISION_FPS`) - had **zero timeout bounding at all**. A
broken camera would be re-opened, and re-hang, on every single poll tick
with no backoff whatsoever - a much better match for the log's repeated
~30-57s stall pattern than a one-time startup issue.

## Honest limitation (per brief item 19/20)

This sandbox is Linux with no `/dev/video0` device node at all, so a raw
`cv2.VideoCapture(0)` fails in ~2ms here (V4L2 fails immediately when no
device exists) - it cannot reproduce the exact ~30s Windows FFMPEG hang
timing. The fix is structural (bounded-timeout wrapper + explicit backend
selection), not a guess tuned to this sandbox's own failure mode, so it
holds regardless of the exact underlying platform timing - but the exact
30-second Windows hang could not be reproduced or timed here. Live
verification on the actual Windows machine is the only way to close that
last gap (see "Live camera verification" below).

## Fix design

**`luno/config.py`** - two new settings:
- `CAMERA_OPEN_TIMEOUT_S` (default 2.5s) - bound per backend-candidate
  open attempt. Chosen so 2 Windows candidates (DSHOW, MSMF) stay within
  the brief's "<=5s total for a failure case" target.
- `CAMERA_REOPEN_COOLDOWN_S` (default 10.0s) - after a failed open, how
  long before the next poll tick is allowed to retry at all.

**`luno/vision.py`** (the module that already owned "the ONE place that
decides what `cv2.VideoCapture(...)` opens", per its own pre-existing
`camera_source()` docstring):
- `CameraState` enum - `UNKNOWN` / `AVAILABLE` / `UNAVAILABLE` / `BUSY` /
  `BACKEND_ERROR`, replacing the previous implicit `isOpened()`
  true/false binary. Honestly documented: `BUSY` vs `UNAVAILABLE` is a
  best-effort guess (OpenCV does not reliably distinguish "no camera" vs
  "camera claimed elsewhere" across platforms/backends), not a
  guaranteed diagnosis.
- `_local_backend_candidates()` - platform-based ordered list of
  explicit `cv2.CAP_*` flags, for LOCAL (int) sources only: Windows
  `[CAP_DSHOW, CAP_MSMF]`, Linux `[CAP_V4L2]`, macOS `[CAP_AVFOUNDATION]`,
  unknown platform (or a flag missing from the installed `cv2` build)
  falls back to `[None]` - i.e. the original `CAP_ANY` behavior, not a
  guess at a nonexistent flag name. String (`CAMERA_URL`) sources always
  get `[None]` - never touched, so FFMPEG-for-network-streams behavior
  is completely unchanged.
- `_open_capture_bounded(source, backend, timeout_s)` - generalizes
  `health.py`'s pre-existing daemon-thread + join pattern into one
  shared implementation. On timeout, a **separate** cleanup thread keeps
  waiting for the orphaned open and releases it whenever it eventually
  completes, so a slow-but-eventually-successful open never leaks a
  device handle just because the caller gave up.
- `_open_camera_with_discovery(source, timeout_s)` - tries each backend
  candidate in order, stops at first success; classifies total failure
  as `BACKEND_ERROR` (a candidate raised) or `UNAVAILABLE` (every
  candidate returned cleanly but never opened, or timed out).
- `_capture_frame()` - rewritten to use the above instead of a raw
  `cv2.VideoCapture()` call, and now checks the reopen cooldown first:
  if the camera is already known to be in a failure state and the
  cooldown hasn't elapsed, it returns `None` **immediately without
  touching `cv2` at all**. This is the fix for the more severe bug
  described above.
- `probe_camera(timeout_s=None)` - new one-shot diagnostic probe, reuses
  the same `_camera_lock`/discovery path, always releases before
  returning, deliberately does **not** update the persistent
  `_camera_state`/`_camera_connected` globals (a diagnostic run must
  never overwrite what the real running app last observed).
- `discover_cameras(max_index=5, timeout_s=None)` - probes indices
  `0..max_index-1` one at a time under the same lock, each fully
  released before the next; never touches `CAMERA_INDEX`/`CAMERA_URL`/
  any config file.
- `camera_status()` - extended (backward compatible) with `state`,
  `state_reason`, `cooldown_remaining_s`.

**`luno/bootstrap/health.py`** - `_check_camera()` now calls
`vision.probe_camera()` instead of its own separate, uncoordinated
`cv2.VideoCapture(legacy_config.CAMERA_INDEX)`. That old separate call
had two real problems beyond the timeout: it read `CAMERA_INDEX`
directly (silently ignoring a configured `CAMERA_URL`), and it opened
the device with no explicit backend and **no shared lock**, meaning it
could run fully concurrently with a real `capture_frame()` call from a
poll loop opening the same device at the same time (brief item 9). The
outer 5-second thread+join wrapper is kept as a defensive backstop.

**`camera_diagnostic.py`** (new, repo root) - read-only CLI:
`python camera_diagnostic.py [--max-index N] [--timeout S]`. Prints the
configured `camera_source()`/`camera_status()`, then a table of
`discover_cameras()` results (state, backend, open/read timing,
resolution, fps, rejection reason) plus a summary. Never writes to
`config.CAMERA_INDEX`/`CAMERA_URL`/any `config/*.json` file.

## Concurrency

Before this fix, `health.py`'s startup probe and any real
`capture_frame()` call from a poll loop could open the same device from
two threads simultaneously (undefined/racy behavior in OpenCV).
`probe_camera()` now shares the exact same `_camera_lock` every other
camera operation already used - no new locking mechanism was introduced
(brief item 9's explicit "don't build a new state system that isn't
needed"). Verified directly: 5 threads calling `probe_camera()`
concurrently against a fake backend that records concurrent-entry count
never showed more than 1 active open at a time (`tests/
test_sprint69_camera_stability.py::test_M_*`).

## Resource cleanup

Every code path that ends without a usable capture releases it:
opened-but-`isOpened()`-false, opened-then-`read()`-failed, and the
timeout+eventual-late-arrival case (via the cleanup thread). Verified
directly (not just asserted) via `release()` call-count tracking across
every failure path in the new test file.

## Test coverage

`tests/test_sprint69_camera_stability.py` - 22 tests covering all 17
brief-mandated categories (A-Q) plus a security guard and a diagnostic-
script check:

- A/B/C/D - valid open, nonexistent index, busy/claimed (read fails
  after a successful open), backend raises an exception.
- E/F - a hanging string-URL open and a hanging bounded-open call both
  return within the configured timeout, not the full hang; F also
  proves the late-arriving capture is still eventually released.
- G/H - an opened-but-never-ready capture is released; a **later**
  `read()` failure on an already-open camera correctly drops the stale
  handle and marks `BUSY` (not just the first-open path).
- I - `_check_camera()` never blocks on a hanging backend.
- J/K - a scheduled-poll retry within the cooldown window makes zero
  real open attempts; a retry after the cooldown elapses does reopen.
- L - `discover_cameras()` releases every opened candidate before
  moving to the next index.
- M - concurrent probes are serialized by `_camera_lock`, proven via a
  concurrent-entry counter, never just asserted.
- N - the one **real, un-mocked** integration test against this
  sandbox's actual (absent) camera hardware - proves the no-hardware
  path is fast and correctly classified, honestly scoped to what it can
  prove (see "Honest limitation" above).
- O/P - `discover_cameras()` distinguishes per-index state correctly;
  backend fallback tries candidates in order and stops at first
  success; a string source never receives a local-backend override.
- Q - `max_index=0`, a `0.0`s timeout, and a negative configured index
  all degrade cleanly, never raise.
- A security-guard test greps the three changed/new files for
  `eval(`/`exec(`/`shell=True`/`subprocess`/`__import__(`/`os.system(`/
  `os.popen(` (word-boundary regex, to avoid false positives like
  `retrieval(` containing the substring `eval(`) - none found.
- A diagnostic-script test runs `camera_diagnostic.py` as a real
  subprocess, hashes `config/*.json` before and after, and asserts the
  hashes are identical and the run completes in under 10s.

Two **pre-existing** test files needed updates as a direct, necessary
consequence of this fix (not incidental breakage):

- `tests/test_camera_health_check_timeout.py` - its fake `cv2` modules
  were installed via `monkeypatch.setitem(sys.modules, "cv2", ...)`,
  which only affects code doing a **fresh** `import cv2` at call time.
  `_check_camera()` now calls into `luno.vision`, which does a
  **module-level** `import cv2` bound once at import time - the exact
  same class of patch-target mismatch Sprint 68 found and fixed in
  `test_camera_presence.py`. Rewritten to
  `monkeypatch.setattr(vision_module, "cv2", fake_cv2)` instead, and
  every fake `VideoCapture` updated to accept an optional `backend`
  second argument.
- `tests/test_vision_sprint8.py` - two tests set `cv2.VideoCapture` via
  a plain lambda taking only one positional argument. On this Linux
  sandbox, `_local_backend_candidates()` now returns a real
  `[CAP_V4L2]`, so `_open_capture_bounded()` calls
  `cv2.VideoCapture(source, backend)` with two arguments - fixed by
  accepting an optional `backend=None` parameter. Separately,
  `test_02_camera_disconnect_then_automatic_reconnect` assumed an
  immediate retry succeeds on the very next `capture_frame()` call after
  a failed read - that assumption is now genuinely false by design (the
  whole point of the Sprint 69 cooldown is to stop exactly that
  immediate-retry hammering), so the test was updated to use a
  controllable fake clock and advance it past
  `CAMERA_REOPEN_COOLDOWN_S` before expecting the reconnect - not a
  weakened test, a corrected one.

## Persistent state

`config/*.json` (27 files) hashed before and after all Sprint 69 work
(including the full regression sweep); `config/long_term_memory.json`
confirmed unchanged at its established
`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a` hash
(unchanged since Sprint 55). No config migration was performed or
needed.

## Live camera verification

Not performed - this sandbox has no camera hardware and cannot run
Windows/DirectShow/Media Foundation. The diagnostic script
(`camera_diagnostic.py`) is the intended tool for verifying this fix
against the actual reported hardware/OS; its output (detected indices,
which backend succeeded, timing, resolution/fps, per-candidate
rejection reasons) should be captured on the real machine as the next
verification step.

## Known limitations

- `BUSY` vs `UNAVAILABLE` classification is a best-effort guess (see
  `CameraState`'s own docstring) - OpenCV does not expose a reliable,
  cross-platform "this device is claimed by another process" signal.
- The exact ~30s Windows FFMPEG hang timing was not reproduced in this
  Linux sandbox (no local camera device node exists here at all).
- `_local_backend_candidates()`'s Windows candidate list (`CAP_DSHOW`,
  `CAP_MSMF`) is evidence-based from the bug report and standard OpenCV
  Windows backend documentation, but has not been exercised against
  real Windows hardware in this sandbox.
