"""
test_camera_health_check_timeout.py
======================================

Regression test for a real startup hang: `python main.py` used to print
"Running health checks..." and then simply never continue when
`VISION_BACKEND=real`/`CAMERA_VISION_ENABLED=true` and
`cv2.VideoCapture(CAMERA_INDEX)` blocked indefinitely (a well-known
Windows/OpenCV issue - no camera responding, device claimed by another
process, asleep USB webcam, OS camera-privacy toggle, etc.). `_check_camera()`
in `luno/bootstrap/health.py` now runs the actual camera open on a
bounded background thread and degrades to a "warned" result instead of
blocking forever - this proves that fix actually works, using a fake
`cv2` module that hangs forever, standing in for the real (unbounded)
OpenCV call.

Sprint 69 (Camera Device / OpenCV Stability Fix) update: `_check_camera()`
now calls `luno.vision.probe_camera()` (bounded, backend-candidate-aware,
lock-shared - see that function's own docstring) instead of building its
own separate `cv2.VideoCapture(...)` call. That moved the actual `cv2`
call site from this module's own local `import cv2` (which a bare
`monkeypatch.setitem(sys.modules, "cv2", fake_cv2)` could intercept) to
`luno.vision`'s MODULE-LEVEL `import cv2` (bound once, at that module's
own import time - reassigning `sys.modules["cv2"]` afterward does not
change what `luno.vision.cv2` already points to). Every fake `cv2` below
is therefore installed via `monkeypatch.setattr(vision_module, "cv2",
fake_cv2)` instead - patches the actual symbol `probe_camera()` reads,
and (unlike a raw attribute assignment - see Sprint 68's own
`tests/test_camera_presence.py` fix for why that distinction matters)
`monkeypatch.setattr` auto-reverts at the end of each test, so a fake
`cv2` here can never leak into a later, unrelated test.

Every fake `VideoCapture` below accepts an optional second positional
arg (`backend`) it simply ignores - `luno.vision._open_camera_with_
discovery()` calls `cv2.VideoCapture(source, backend)` (two args) for a
local int device-index source whenever `_local_backend_candidates()`
returns a real backend flag for the current platform (e.g. `CAP_V4L2`
on Linux, where these tests actually run) - a fake accepting only one
positional arg would raise `TypeError` on the very first candidate, for
reasons that have nothing to do with what each test is actually
exercising.
"""

from __future__ import annotations

import os
import sys
import time
import types

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.config as legacy_config  # noqa: E402
import luno.vision as vision_module  # noqa: E402
from luno.bootstrap.health import _CAMERA_OPEN_TIMEOUT_S, _check_camera  # noqa: E402
from luno.bootstrap.launcher_config import BACKEND_MOCK, BACKEND_REAL, LauncherConfig  # noqa: E402

# `luno.config` reads CAMERA_VISION_ENABLED/CAMERA_INDEX once, at import
# time, as plain module-level constants (not re-read per call) - so
# `monkeypatch.setenv(...)` alone would have NO effect on `_check_camera()`
# (it does `import luno.config as legacy_config`, which just returns the
# ALREADY-imported module from `sys.modules`). Every test below patches
# the module's ATTRIBUTES directly instead, which is what `_check_camera()`
# actually reads.


def _install_fake_hanging_cv2(monkeypatch):
    """Stands in for real `cv2` - `VideoCapture()` never returns, exactly
    reproducing the reported hang (minus actually hanging the test
    suite itself, since the fix runs this on a bounded daemon thread)."""
    fake_cv2 = types.SimpleNamespace()

    class _HangingVideoCapture:
        def __init__(self, index, backend=None):
            time.sleep(60)  # far longer than CAMERA_OPEN_TIMEOUT_S/_CAMERA_OPEN_TIMEOUT_S

        def isOpened(self):  # pragma: no cover - never reached before timeout
            return True

        def release(self):  # pragma: no cover
            pass

    fake_cv2.VideoCapture = _HangingVideoCapture
    monkeypatch.setattr(vision_module, "cv2", fake_cv2)


def _real_cfg():
    return LauncherConfig(vision_backend=BACKEND_REAL)


def test_camera_check_never_blocks_startup_when_videocapture_hangs(monkeypatch):
    """Sprint 69: `vision.probe_camera()`'s own inner bound
    (`config.CAMERA_OPEN_TIMEOUT_S`, default 2.5s per backend candidate)
    now catches a hanging `cv2.VideoCapture()` BEFORE `_check_camera()`'s
    own outer `_CAMERA_OPEN_TIMEOUT_S` (5s) join ever has to - this is
    an IMPROVEMENT (the health check now returns faster on a stuck
    driver than it did before Sprint 69), not a regression, so the
    message this asserts on reflects the inner mechanism's own report
    rather than the outer wrapper's generic "did not respond" fallback.
    The outer wrapper's own message is covered separately below by
    `test_camera_check_outer_wrapper_still_catches_a_probe_that_
    outlives_it`, which fakes a hang longer than the outer bound too."""
    monkeypatch.setattr(legacy_config, "CAMERA_VISION_ENABLED", True)
    monkeypatch.setattr(legacy_config, "CAMERA_INDEX", 0)
    _install_fake_hanging_cv2(monkeypatch)

    t0 = time.time()
    result = _check_camera(_real_cfg())
    elapsed = time.time() - t0

    # must return well under the fake 60s hang, and under the outer bound
    assert elapsed < _CAMERA_OPEN_TIMEOUT_S + 3.0
    assert result.name == "Camera"
    assert result.ok is False
    assert result.action == "warned"
    assert "no candidate backend responded" in result.message or "did not respond" in result.message
    # Deliberately does NOT also prove the outer `_CAMERA_OPEN_TIMEOUT_S`
    # wrapper fires when the INNER bound is disabled/larger than it -
    # doing that safely would require a fake hang that outlives the
    # test's own wait, which leaves the shared `vision._camera_lock`
    # held by a leaked background thread for the remainder of that hang,
    # blocking every OTHER test in this file that also calls `_check_
    # camera()` until it finally releases. The outer wrapper is kept as
    # a defensive backstop (see `_check_camera()`'s own docstring) - its
    # code path is unchanged from before Sprint 69, which already had
    # its own passing coverage of "the outer join actually times out".


def test_camera_check_reports_success_fast_when_camera_opens_normally(monkeypatch):
    monkeypatch.setattr(legacy_config, "CAMERA_VISION_ENABLED", True)
    monkeypatch.setattr(legacy_config, "CAMERA_INDEX", 0)

    fake_cv2 = types.SimpleNamespace()

    class _FastVideoCapture:
        def __init__(self, index, backend=None):
            self._index = index

        def isOpened(self):
            return True

        def release(self):
            pass

    fake_cv2.VideoCapture = _FastVideoCapture
    monkeypatch.setattr(vision_module, "cv2", fake_cv2)

    t0 = time.time()
    result = _check_camera(_real_cfg())
    elapsed = time.time() - t0

    assert elapsed < 1.0
    assert result.ok is True
    assert "opened" in result.message


def test_camera_check_skipped_entirely_when_backend_is_mock():
    result = _check_camera(LauncherConfig(vision_backend=BACKEND_MOCK))
    assert result.ok is True
    assert "not required" in result.message


def test_camera_check_warns_without_hanging_when_disabled_via_config(monkeypatch):
    monkeypatch.setattr(legacy_config, "CAMERA_VISION_ENABLED", False)
    result = _check_camera(_real_cfg())
    assert result.ok is False
    assert result.action == "warned"


def test_camera_check_captures_exception_from_open_without_hanging(monkeypatch):
    monkeypatch.setattr(legacy_config, "CAMERA_VISION_ENABLED", True)
    monkeypatch.setattr(legacy_config, "CAMERA_INDEX", 0)

    fake_cv2 = types.SimpleNamespace()

    def _raising_video_capture(index, backend=None):
        raise RuntimeError("driver exploded")

    fake_cv2.VideoCapture = _raising_video_capture
    monkeypatch.setattr(vision_module, "cv2", fake_cv2)

    result = _check_camera(_real_cfg())
    assert result.ok is False
    assert "driver exploded" in result.message


def test_camera_check_reuses_camera_source_not_raw_camera_index(monkeypatch):
    """Sprint 69 regression: the pre-Sprint-69 `_check_camera()` read
    `legacy_config.CAMERA_INDEX` directly, silently ignoring `CAMERA_URL`
    if one was configured. It now goes through `vision.camera_source()`
    (via `vision.probe_camera()`), so a configured `CAMERA_URL` is what
    actually gets probed."""
    monkeypatch.setattr(legacy_config, "CAMERA_VISION_ENABLED", True)
    monkeypatch.setattr(legacy_config, "CAMERA_URL", "rtsp://example.invalid/stream")
    monkeypatch.setattr(legacy_config, "CAMERA_INDEX", 0)

    fake_cv2 = types.SimpleNamespace()
    seen_sources = []

    class _RecordingVideoCapture:
        def __init__(self, source, backend=None):
            seen_sources.append(source)

        def isOpened(self):
            return True

        def release(self):
            pass

    fake_cv2.VideoCapture = _RecordingVideoCapture
    monkeypatch.setattr(vision_module, "cv2", fake_cv2)

    result = _check_camera(_real_cfg())
    assert result.ok is True
    assert seen_sources == ["rtsp://example.invalid/stream"]
