"""
tests/test_sprint69_camera_stability.py
===========================================

Sprint 69 (Camera Device / OpenCV Stability Fix) - covers all 17 test
categories (A-Q) the brief itself requires, plus a security guard and a
read-only check on the new `camera_diagnostic.py` script.

Per item 11 of the brief ("don't just mock cv2.VideoCapture and claim
fixed"), most tests here DO install a fake `cv2` (there is no real camera
in this sandbox to exercise success/failure paths against on demand), but
every one of them proves an actual BEHAVIOR - timeout bounding, backend
selection order, cooldown/backoff, resource release, concurrency
serialization, startup non-blocking - not just "some frame came back".
Test N is the deliberate exception: it uses the REAL, un-mocked `cv2`
against this sandbox's own (absent) camera hardware, so at least one test
in this file proves something about actual hardware behavior rather than
a fake's behavior (see that test's own docstring for the honest limit on
what it can and cannot prove, per item 19).

Isolation (Sprint 68 precedent): `luno.vision` owns several module-level
mutable globals (`_camera`, `_camera_state`, `_camera_state_reason`,
`_camera_cooldown_until`, `_camera_connected`, `_camera_last_error`) that
`_capture_frame()`/`probe_camera()` update as a side effect - these are
NOT `config`/env values `monkeypatch` can auto-revert, so the autouse
fixture below snapshots and restores them around every test in this file,
and releases any camera handle a test may have left open (real or fake)
so nothing leaks into the next test or another test file. This is exactly
the same class of global-state leak Sprint 68 found (and fixed) in
`tests/test_camera_presence.py`'s un-restored `time.time` patch, applied
here to vision.py's own camera state instead.

Run:
    python3 -m pytest tests/test_sprint69_camera_stability.py -v
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import types

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import luno.config as config  # noqa: E402
import luno.vision as vision  # noqa: E402
from luno.bootstrap.health import _check_camera  # noqa: E402
from luno.bootstrap.launcher_config import BACKEND_REAL, LauncherConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_camera_module_state():
    snapshot = dict(
        camera=vision._camera,
        state=vision._camera_state,
        state_reason=vision._camera_state_reason,
        cooldown_until=vision._camera_cooldown_until,
        connected=vision._camera_connected,
        last_error=vision._camera_last_error,
    )
    yield
    try:
        if vision._camera is not None and vision._camera is not snapshot["camera"]:
            vision._camera.release()
    except Exception:
        pass
    vision._camera = snapshot["camera"]
    vision._camera_state = snapshot["state"]
    vision._camera_state_reason = snapshot["state_reason"]
    vision._camera_cooldown_until = snapshot["cooldown_until"]
    vision._camera_connected = snapshot["connected"]
    vision._camera_last_error = snapshot["last_error"]


def _fake_cv2(video_capture_cls, **extra_attrs):
    ns = types.SimpleNamespace()
    ns.VideoCapture = video_capture_cls
    for key, value in extra_attrs.items():
        setattr(ns, key, value)
    return ns


class _FrameStub:
    """Stands in for a real numpy BGR frame - only `.shape` is read by
    `discover_cameras()`'s resolution reporting."""

    def __init__(self, w=1280, h=720):
        self.shape = (h, w, 3)


# ============================================================================
# A - valid/responsive camera index
# ============================================================================

def test_A_valid_responsive_camera_index_returns_frame_and_marks_available(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    release_calls = []

    class _Good:
        def __init__(self, source):
            self.source = source

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            release_calls.append(True)

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Good))

    frame = vision._capture_frame()
    assert frame == "FRAME"
    status = vision.camera_status()
    assert status["state"] == "AVAILABLE"
    assert status["connected"] is True
    assert status["error"] is None
    assert release_calls == [], "a successfully-opened, still-good camera must NOT be released mid-use"


# ============================================================================
# B - camera index doesn't exist
# ============================================================================

def test_B_camera_index_does_not_exist_reports_unavailable(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 99)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _NeverOpens:
        def __init__(self, source):
            pass

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_NeverOpens))

    frame = vision._capture_frame()
    assert frame is None
    status = vision.camera_status()
    assert status["state"] == "UNAVAILABLE"
    assert status["connected"] is False


# ============================================================================
# C - camera busy/claimed by another app
# ============================================================================

def test_C_camera_busy_or_claimed_reports_busy_not_unavailable(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _OpensButReadFails:
        def __init__(self, source):
            pass

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return False, None

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_OpensButReadFails))

    frame = vision._capture_frame()
    assert frame is None
    status = vision.camera_status()
    assert status["state"] == "BUSY"


# ============================================================================
# D - OpenCV backend fails (raises during open)
# ============================================================================

def test_D_backend_raises_exception_reports_backend_error(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _Explodes:
        def __init__(self, source):
            raise RuntimeError("driver exploded")

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Explodes))

    frame = vision._capture_frame()
    assert frame is None
    status = vision.camera_status()
    assert status["state"] == "BACKEND_ERROR"
    assert "driver exploded" in status["error"]


# ============================================================================
# E - FFMPEG-style timeout for a string (URL) source is bounded, not the
#     reported unbounded ~30s wait
# ============================================================================

def test_E_string_url_source_hang_is_bounded_not_a_30s_ffmpeg_style_wait(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_URL", "rtsp://example.invalid/stream")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 0.2)

    class _HangsLikeFfmpeg:
        def __init__(self, source):
            time.sleep(1.5)  # stands in for the reported ~30s FFMPEG stream timeout

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_HangsLikeFfmpeg))

    t0 = time.time()
    frame = vision._capture_frame()
    elapsed = time.time() - t0

    assert frame is None
    assert elapsed < 1.0, (
        f"capture must return within CAMERA_OPEN_TIMEOUT_S (0.2s), not wait out "
        f"the full hang - took {elapsed:.2f}s"
    )


# ============================================================================
# F - general bounded-timeout proof (+ eventual release once the slow open
#     finally does complete, so a caller giving up never leaks the handle)
# ============================================================================

def test_F_open_capture_bounded_returns_before_hang_completes_and_still_releases_eventually(monkeypatch):
    release_calls = []

    class _SlowThenReleasable:
        def __init__(self, source, backend=None):
            time.sleep(0.5)

        def isOpened(self):
            return True

        def release(self):
            release_calls.append(True)

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_SlowThenReleasable))

    t0 = time.time()
    cap, timed_out, error = vision._open_capture_bounded(0, None, timeout_s=0.1)
    elapsed = time.time() - t0

    assert timed_out is True
    assert cap is None
    assert error is None
    assert elapsed < 0.4, f"must return close to the 0.1s bound, not the 0.5s hang - took {elapsed:.2f}s"

    deadline = time.time() + 2.0
    while not release_calls and time.time() < deadline:
        time.sleep(0.02)
    assert release_calls, "leaked handle: cleanup thread never released the late-arriving capture"


# ============================================================================
# G - VideoCapture constructs but isOpened() is False - must still be
#     released, never leaked
# ============================================================================

def test_G_opened_object_but_isOpened_false_still_gets_released(monkeypatch):
    release_calls = []

    class _FalseOpen:
        def __init__(self, source):
            pass

        def isOpened(self):
            return False

        def release(self):
            release_calls.append(True)

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_FalseOpen))

    cap, timed_out, error = vision._open_capture_bounded(0, None, timeout_s=1.0)
    assert cap is None
    assert timed_out is False
    assert release_calls == [True]


# ============================================================================
# H - read() fails on an ALREADY-open camera (not just at first open) -
#     must drop the stale handle and mark BUSY, not silently keep using it
# ============================================================================

def test_H_read_failure_on_already_open_camera_drops_handle_and_marks_busy(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _GoodThenBad:
        def __init__(self, source):
            self.calls = 0

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            self.calls += 1
            if self.calls == 1:
                return True, "FRAME1"
            return False, None

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_GoodThenBad))

    frame1 = vision._capture_frame()
    assert frame1 == "FRAME1"
    frame2 = vision._capture_frame()
    assert frame2 is None
    assert vision.camera_status()["state"] == "BUSY"
    assert vision._camera is None, "a camera whose read() just failed must be dropped, not reused next call"


# ============================================================================
# I - startup probe never blocks, even on a hanging backend
# ============================================================================

def test_I_startup_check_camera_never_blocks_on_hanging_backend(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_VISION_ENABLED", True)
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 0.2)

    class _Hangs:
        def __init__(self, source):
            time.sleep(2.0)

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Hangs))

    t0 = time.time()
    result = _check_camera(LauncherConfig(vision_backend=BACKEND_REAL))
    elapsed = time.time() - t0

    assert elapsed < 2.0, f"startup check must not wait out the 2.0s hang - took {elapsed:.2f}s"
    assert result.name == "Camera"
    assert result.ok is False
    assert result.action == "warned"


# ============================================================================
# J - scheduled poll after camera unavailable respects cooldown (no
#     repeated hammering of a known-broken camera)
# ============================================================================

def test_J_scheduled_poll_after_unavailable_does_not_reopen_within_cooldown(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_REOPEN_COOLDOWN_S", 10.0)

    construct_calls = []

    class _AlwaysFails:
        def __init__(self, source):
            construct_calls.append(source)

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_AlwaysFails))

    frame1 = vision._capture_frame()
    frame2 = vision._capture_frame()  # simulates the very next scheduled_vision_poll tick
    frame3 = vision._capture_frame()  # and the one after that

    assert frame1 is None and frame2 is None and frame3 is None
    assert len(construct_calls) == 1, (
        f"calls within the cooldown window must not reattempt opening the camera "
        f"at all - saw {len(construct_calls)} real open attempts for 3 poll ticks"
    )


# ============================================================================
# K - retry happens once cooldown elapses
# ============================================================================

def test_K_retry_happens_after_cooldown_elapses(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_REOPEN_COOLDOWN_S", 0.15)

    construct_calls = []

    class _AlwaysFails:
        def __init__(self, source):
            construct_calls.append(source)

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_AlwaysFails))

    vision._capture_frame()
    assert len(construct_calls) == 1
    time.sleep(0.25)
    vision._capture_frame()
    assert len(construct_calls) == 2, "must retry once the cooldown window has actually elapsed"


# ============================================================================
# L - every failure path always release()s (no leaked handles)
# ============================================================================

def test_L_discover_cameras_always_releases_every_opened_capture_before_next_index(monkeypatch):
    release_order = []

    class _OpensAndCloses:
        def __init__(self, source, backend=None):
            self.source = source

        def isOpened(self):
            return True

        def read(self):
            return True, _FrameStub()

        def get(self, prop):
            return 30.0

        def getBackendName(self):
            return "FAKE"

        def release(self):
            release_order.append(self.source)

    fake = _fake_cv2(_OpensAndCloses, CAP_PROP_FPS=5)
    monkeypatch.setattr(vision, "cv2", fake)

    results = vision.discover_cameras(max_index=3, timeout_s=1.0)
    assert len(results) == 3
    assert release_order == [0, 1, 2], "every opened candidate must be released before probing the next index"


# ============================================================================
# M - two concurrent probes never open the device twice at once
# ============================================================================

def test_M_concurrent_probes_are_serialized_never_open_device_twice(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 1.0)

    counter = {"active": 0, "max_seen": 0}
    lock = threading.Lock()

    class _SlowOpen:
        def __init__(self, source):
            with lock:
                counter["active"] += 1
                counter["max_seen"] = max(counter["max_seen"], counter["active"])
            time.sleep(0.15)
            with lock:
                counter["active"] -= 1

        def isOpened(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_SlowOpen))

    threads = [threading.Thread(target=vision.probe_camera) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not any(t.is_alive() for t in threads), "a probe thread did not finish - possible deadlock"
    assert counter["max_seen"] == 1, (
        "_camera_lock must serialize concurrent probes - never open the device "
        f"from two threads at once (observed max concurrent opens: {counter['max_seen']})"
    )


# ============================================================================
# N - REAL (un-mocked) hardware check on this no-camera sandbox machine.
#     Per item 11 ("don't just mock and claim fixed") and item 19 ("don't
#     claim live camera success without hardware") - this proves the
#     no-hardware path is fast/correct against ACTUAL cv2, but explicitly
#     does NOT and cannot prove the reported Windows 30s FFMPEG hang is
#     fixed: this sandbox has no /dev/video* device node at all, so real
#     cv2 fails near-instantly here rather than reproducing that hang's
#     timing (see docs/change_impact/camera_stability_fix.md).
# ============================================================================

def test_N_real_unmocked_discovery_on_this_no_camera_machine_is_fast_and_honest(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 2.0)

    t0 = time.time()
    results = vision.discover_cameras(max_index=1, timeout_s=2.0)
    elapsed = time.time() - t0

    assert elapsed < 5.0, f"real discovery on a no-camera machine must still be fast - took {elapsed:.2f}s"
    assert len(results) == 1
    assert results[0]["state"] in ("UNAVAILABLE", "BACKEND_ERROR")
    assert results[0]["read_ok"] is None or results[0]["read_ok"] is False


# ============================================================================
# O - multiple camera indices are distinguished correctly
# ============================================================================

def test_O_discover_cameras_distinguishes_per_index_state(monkeypatch):
    class _PerIndex:
        def __init__(self, source, backend=None):
            self.source = source

        def isOpened(self):
            return self.source == 1  # only index 1 "exists"

        def read(self):
            return True, _FrameStub(w=1280, h=720)

        def get(self, prop):
            return 15.0

        def getBackendName(self):
            return "FAKE"

        def release(self):
            pass

    fake = _fake_cv2(_PerIndex, CAP_PROP_FPS=5)
    monkeypatch.setattr(vision, "cv2", fake)

    results = vision.discover_cameras(max_index=3, timeout_s=1.0)
    assert [r["state"] for r in results] == ["UNAVAILABLE", "AVAILABLE", "UNAVAILABLE"]
    assert results[1]["resolution"] == [1280, 720]
    assert results[1]["fps"] == 15.0


# ============================================================================
# P - backend fallback tries candidates in order, stops at first success
# ============================================================================

def test_P_backend_fallback_tries_next_candidate_after_first_fails(monkeypatch):
    attempted_backends = []

    class _FirstFailsSecondWorks:
        def __init__(self, source, backend=None):
            attempted_backends.append(backend)
            self._backend = backend

        def isOpened(self):
            return self._backend == 222

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_FirstFailsSecondWorks))
    monkeypatch.setattr(vision, "_local_backend_candidates", lambda: [111, 222])

    cap, state, reason = vision._open_camera_with_discovery(0, per_candidate_timeout_s=1.0)
    try:
        assert attempted_backends == [111, 222], "must try candidates IN ORDER and stop at first success"
        assert state.name == "AVAILABLE"
        assert cap is not None
    finally:
        if cap is not None:
            cap.release()


def test_P_string_url_source_never_gets_a_local_backend_override(monkeypatch):
    """The other half of item 4/13's scoping requirement: a STRING
    (`CAMERA_URL`) source must always use `[None]` (OpenCV's own CAP_ANY
    choice, which correctly reaches FFMPEG for network streams) - never
    the local-camera backend candidate list, even if the platform would
    normally offer one."""
    seen_backends = []

    class _Recorder:
        def __init__(self, source, backend=None):
            seen_backends.append(backend)

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Recorder))
    monkeypatch.setattr(vision, "_local_backend_candidates", lambda: [111, 222])

    cap, state, reason = vision._open_camera_with_discovery("rtsp://example.invalid/stream", per_candidate_timeout_s=1.0)
    try:
        assert seen_backends == [None]
    finally:
        if cap is not None:
            cap.release()


# ============================================================================
# Q - malformed/invalid config edge cases
# ============================================================================

def test_Q_discover_cameras_max_index_zero_returns_empty_list_no_camera_touch(monkeypatch):
    construct_calls = []

    class _ShouldNeverBeCalled:
        def __init__(self, source):
            construct_calls.append(source)

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_ShouldNeverBeCalled))

    results = vision.discover_cameras(max_index=0, timeout_s=0.1)
    assert results == []
    assert construct_calls == []


def test_Q_zero_timeout_does_not_crash_or_hang(monkeypatch):
    class _Instant:
        def __init__(self, source):
            pass

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Instant))

    cap, timed_out, error = vision._open_capture_bounded(0, None, timeout_s=0.0)
    # Must not raise either way - a race between "instant open" and "0s join
    # already expired" is acceptable, an unhandled exception is not.
    assert timed_out in (True, False)
    if cap is not None:
        cap.release()


def test_Q_negative_camera_index_does_not_crash_discovery(monkeypatch):
    """Malformed config (e.g. a bad env var producing a negative index)
    must degrade to a normal UNAVAILABLE/BACKEND_ERROR result, never an
    unhandled exception that could take down a caller."""
    monkeypatch.setattr(config, "CAMERA_INDEX", -1)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _RejectsNegative:
        def __init__(self, source):
            if isinstance(source, int) and source < 0:
                raise ValueError("invalid index")

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_RejectsNegative))

    frame = vision._capture_frame()
    assert frame is None
    assert vision.camera_status()["state"] == "BACKEND_ERROR"


# ============================================================================
# Security (item 17): camera index/backend/config must never become an
# arbitrary command execution path.
# ============================================================================

_FORBIDDEN_PATTERNS = [
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"__import__\s*\("),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bos\.popen\s*\("),
]


def test_security_no_dangerous_constructs_in_camera_code():
    paths = [
        os.path.join(_ROOT, "luno", "vision.py"),
        os.path.join(_ROOT, "luno", "bootstrap", "health.py"),
        os.path.join(_ROOT, "camera_diagnostic.py"),
    ]
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        for pattern in _FORBIDDEN_PATTERNS:
            match = pattern.search(content)
            assert match is None, f"forbidden construct {pattern.pattern!r} found in {path}: {match}"


# ============================================================================
# camera_diagnostic.py: read-only, completes fast, never touches config
# ============================================================================

def _hash_dir(path):
    digest = hashlib.sha256()
    if not os.path.isdir(path):
        return None
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as f:
            digest.update(name.encode("utf-8"))
            digest.update(f.read())
    return digest.hexdigest()


def test_diagnostic_script_is_read_only_and_completes_fast():
    config_dir = os.path.join(_ROOT, "config")
    before = _hash_dir(config_dir)

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, os.path.join(_ROOT, "camera_diagnostic.py"), "--max-index", "1", "--timeout", "1.0"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.time() - t0

    after = _hash_dir(config_dir)

    assert proc.returncode == 0, f"diagnostic script exited non-zero: stderr={proc.stderr!r}"
    assert elapsed < 10.0, f"diagnostic script must not hang - took {elapsed:.2f}s"
    assert "READ-ONLY" in proc.stdout
    assert before == after, "diagnostic script must never modify config/*.json"
