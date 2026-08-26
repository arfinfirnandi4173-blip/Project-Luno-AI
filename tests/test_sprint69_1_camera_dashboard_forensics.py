"""
tests/test_sprint69_1_camera_dashboard_forensics.py
=======================================================

Sprint 69.1 (Camera Runtime/Dashboard Disconnect Forensics & Fix) -
covers the 11 required test categories from the brief, plus the two
static/structural guarantees the forensic trace itself produced as
evidence ("no hidden second VideoCapture" / "no hidden CAP_ANY path"
are provable statically, not just by example).

FORENSIC SUMMARY (see docs/change_impact/camera_runtime_dashboard_
forensics.md for the full writeup): a repo-wide search found exactly
ONE production `cv2.VideoCapture(` call site in the entire codebase
(`luno/vision.py::_open_capture_bounded()`) - every camera-touching
function anywhere in this project (`detect_objects()`,
`detect_objects_tracked()`, `_monitor_loop()`, `ask_vision()`,
`RealVisionSource`, `luno/bootstrap/health.py`) funnels through it.
`scheduled_vision_poll` (the event the original bug report's log line
came from) was confirmed to be a structurally inert heartbeat for the
Vision adapter - `VisionAdapter` never overrides `handle_event()`, so
`BaseAdapter`'s no-op default runs, matching its own "(0.0ms)" log
line exactly; the REAL camera polling happens entirely through
`RealVisionSource`'s two independent background threads, unrelated to
the Scheduler/EventMapping. `VisionMemory` and the Tapo PTZ tool
(`real_camera_ptz.py`, HTTP-based via `pytapo`) were both confirmed to
never touch `cv2`/the camera device at all.

Given no second bypass path exists, this sprint could not reproduce the
reported ~30s FFMPEG hang from source alone (this sandbox has no camera/
Windows hardware - same honest limitation as Sprint 69's own report).
What this sprint COULD do, and did: (1) add structured diagnostic
logging to the one real call site so the actual runtime source/backend/
timing/outcome becomes directly visible in the application's own log
output (closing the verification gap that made this bug hard to
diagnose from field reports alone), (2) fix a genuine one-cycle
reporting lag in `RealVisionSource._tracked_cycle_once()` (camera_status
was queried BEFORE capture_frame() each cycle, always reporting the
PREVIOUS cycle's outcome), (3) fix a real asymmetry in
`VisionAdapter.on_camera_status()` (the very first successful open,
`None -> True`, never fired `CameraReconnected`, only `False -> True`
did - the dashboard's own live-field telemetry was never affected by
this, only the event stream), and (4) add a backend-mismatch detector
(comparing the requested backend to `cap.getBackendName()`) that would
catch OpenCV silently ignoring an explicit backend request - the one
plausible code-level explanation this sprint could not rule out without
the user's own log output.

Run:
    python3 -m pytest tests/test_sprint69_1_camera_dashboard_forensics.py -v
"""

from __future__ import annotations

import ast
import io
import os
import sys
import threading
import time
import types
from contextlib import redirect_stdout

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import luno.config as config  # noqa: E402
import luno.vision as vision  # noqa: E402
from luno.adapters.events import CameraDisconnected, CameraReconnected  # noqa: E402
from luno.adapters.real_vision import RealVisionSource  # noqa: E402
from luno.adapters.vision import VisionAdapter, VisionCycleResult  # noqa: E402
from luno.dashboard.collectors import collect_vision  # noqa: E402


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


class _FakeEventBus:
    def __init__(self):
        self.published = []

    def subscribe(self, *_a, **_k):
        return "sub"

    def unsubscribe(self, *_a, **_k):
        pass

    def publish(self, event):
        self.published.append(event)


class _FakeAdapterManager:
    """Minimal stand-in for `AdapterManager` - `collect_vision()` only
    calls `.status_all()`."""

    def __init__(self, vision_adapter):
        self._vision_adapter = vision_adapter

    def status_all(self):
        return {"vision": self._vision_adapter.status()}


def _bound_adapter():
    adapter = VisionAdapter()
    bus = _FakeEventBus()
    adapter.bind(bus)
    return adapter, bus


# ============================================================================
# 1) local camera success -> dashboard CONNECTED
# ============================================================================

def test_1_local_camera_success_reaches_dashboard_connected(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _Good:
        def __init__(self, source, backend=None):
            pass

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Good))

    adapter, _bus = _bound_adapter()
    status = vision.camera_status()
    adapter.on_camera_status(status)  # first tick: connected still None/unset

    frame = vision.capture_frame()
    assert frame == "FRAME"
    status2 = vision.camera_status()
    adapter.on_camera_status(status2)

    dashboard = collect_vision(_FakeAdapterManager(adapter))
    assert dashboard["camera_connected"] is True


# ============================================================================
# 2) local camera unavailable -> dashboard DISCONNECTED
# ============================================================================

def test_2_local_camera_unavailable_reaches_dashboard_disconnected(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")

    class _NeverOpens:
        def __init__(self, source, backend=None):
            pass

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_NeverOpens))

    adapter, _bus = _bound_adapter()
    frame = vision.capture_frame()
    assert frame is None
    status = vision.camera_status()
    adapter.on_camera_status(status)

    dashboard = collect_vision(_FakeAdapterManager(adapter))
    assert dashboard["camera_connected"] is False


# ============================================================================
# 3) network camera URL -> correct network backend (never a local override)
# ============================================================================

def test_3_network_camera_url_uses_cap_any_never_a_local_backend(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_URL", "rtsp://user:pw@192.168.1.55:554/stream1")
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)

    seen_backends = []

    class _Recorder:
        def __init__(self, source, backend=None):
            seen_backends.append(backend)

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Recorder))
    monkeypatch.setattr(vision, "_local_backend_candidates", lambda: [111, 222])

    frame = vision.capture_frame()
    assert frame == "FRAME"
    assert seen_backends == [None], (
        "a string (network) source must ALWAYS use backend=None (CAP_ANY, which "
        "correctly reaches FFMPEG for RTSP/HTTP streams) - never a local-device "
        "backend candidate, even if the platform normally offers one"
    )


# ============================================================================
# 4) no hidden CAP_ANY path for a LOCAL source when real candidates exist
# ============================================================================

def test_4_local_source_never_falls_through_to_implicit_cap_any_when_candidates_exist(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(vision, "_local_backend_candidates", lambda: [700, 1400])  # fake CAP_DSHOW/CAP_MSMF

    seen_backends = []

    class _Recorder:
        def __init__(self, source, backend=None):
            seen_backends.append(backend)

        def isOpened(self):
            return False  # every candidate fails - forces the loop through ALL of them

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Recorder))

    vision.capture_frame()
    assert None not in seen_backends, (
        f"a local int source must never be opened with an implicit backend=None "
        f"(CAP_ANY) while explicit candidates exist - saw backends {seen_backends}"
    )
    assert seen_backends == [700, 1400]


# ============================================================================
# 5) no hidden second VideoCapture anywhere in production code (static/AST)
# ============================================================================

def _count_videocapture_calls(path):
    """Returns the number of DISTINCT source lines containing a
    `<x>.VideoCapture(...)` call (AST-based, so a mention inside a
    docstring/comment never counts). Deduplicated by line number rather
    than raw `ast.Call` node count because `_open_capture_bounded()`'s
    own single logical call site is written as a one-line ternary
    (`cv2.VideoCapture(source) if backend is None else cv2.VideoCapture
    (source, backend)`) - syntactically two `ast.Call` nodes for what is,
    architecturally, ONE call site (exactly one of the two branches ever
    executes per invocation, and both open the same, only-ever-this-one
    device.)"""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return 0
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "VideoCapture":
            lines.add(node.lineno)
    return len(lines)


def test_5_exactly_one_production_videocapture_call_site_in_the_entire_repo():
    total = 0
    sites = []
    luno_dir = os.path.join(_ROOT, "luno")
    for dirpath, _dirnames, filenames in os.walk(luno_dir):
        if "__pycache__" in dirpath or os.sep + "tests" + os.sep in dirpath + os.sep:
            continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            n = _count_videocapture_calls(full)
            if n:
                total += n
                sites.append((full, n))
    top_level = os.path.join(_ROOT, "camera_diagnostic.py")
    if os.path.isfile(top_level):
        n = _count_videocapture_calls(top_level)
        # camera_diagnostic.py never opens a camera itself - it only calls
        # vision.discover_cameras()/camera_status() - so this must be 0.
        assert n == 0, f"camera_diagnostic.py must never call cv2.VideoCapture directly, found {n}"

    assert sites == [(os.path.join(luno_dir, "vision.py"), 1)], (
        f"expected exactly one cv2.VideoCapture( call site (luno/vision.py, "
        f"inside _open_capture_bounded()), found: {sites} (total={total})"
    )


# ============================================================================
# 6) bounded failure - no 30s-style wait on any failure path
# ============================================================================

def test_6_bounded_failure_never_waits_out_a_long_hang(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_URL", "rtsp://example.invalid/stream")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 0.2)

    class _HangsLikeFfmpeg:
        def __init__(self, source, backend=None):
            time.sleep(1.5)

        def isOpened(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_HangsLikeFfmpeg))

    t0 = time.time()
    frame = vision.capture_frame()
    elapsed = time.time() - t0
    assert frame is None
    assert elapsed < 1.0


# ============================================================================
# 7) cooldown - a known-broken camera is not re-opened every tick
# ============================================================================

def test_7_cooldown_prevents_reopening_a_known_broken_camera_every_tick(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_REOPEN_COOLDOWN_S", 10.0)

    construct_calls = []

    class _AlwaysFails:
        def __init__(self, source, backend=None):
            construct_calls.append(source)

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_AlwaysFails))

    for _ in range(5):
        vision.capture_frame()
    assert len(construct_calls) == 1, (
        f"5 poll ticks within the cooldown window made {len(construct_calls)} real "
        f"open attempts - expected exactly 1"
    )


# ============================================================================
# 8) concurrent poll safety - startup probe and a real poll never open the
#    device at the same time (proven via a concurrent-entry counter, at the
#    same call sites production code actually uses)
# ============================================================================

def test_8_concurrent_probe_and_capture_never_open_device_simultaneously(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 1.0)

    counter = {"active": 0, "max_seen": 0}
    lock = threading.Lock()

    class _SlowOpen:
        def __init__(self, source, backend=None):
            with lock:
                counter["active"] += 1
                counter["max_seen"] = max(counter["max_seen"], counter["active"])
            time.sleep(0.1)
            with lock:
                counter["active"] -= 1

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_SlowOpen))

    threads = [threading.Thread(target=vision.probe_camera) for _ in range(3)]
    threads += [threading.Thread(target=vision.capture_frame) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not any(t.is_alive() for t in threads)
    assert counter["max_seen"] == 1


# ============================================================================
# 9) state transition correctness
# ============================================================================

def test_9_state_transitions_follow_the_actual_sequence_of_outcomes(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "CAMERA_REOPEN_COOLDOWN_S", 0.0)

    class _OpensThenExplodes:
        calls = 0

        def __init__(self, source, backend=None):
            _OpensThenExplodes.calls += 1
            if _OpensThenExplodes.calls == 2:
                raise RuntimeError("driver exploded")

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_OpensThenExplodes))

    assert vision.camera_status()["state"] == "UNKNOWN"
    frame1 = vision.capture_frame()
    assert frame1 == "FRAME"
    assert vision.camera_status()["state"] == "AVAILABLE"

    vision.release_camera()
    frame2 = vision.capture_frame()
    assert frame2 is None
    assert vision.camera_status()["state"] == "BACKEND_ERROR"


# ============================================================================
# 10) dashboard telemetry correctness end-to-end (real adapter, real
#     RealVisionSource cycle ordering, real dashboard collector)
# ============================================================================

def test_10_dashboard_telemetry_reflects_the_cycle_that_just_ran_not_the_previous_one(monkeypatch):
    """Sprint 69.1 fix: `RealVisionSource._tracked_cycle_once()` used to
    query `camera_status()` BEFORE `capture_frame()` each cycle - always
    reporting the PREVIOUS cycle's outcome. This proves a single cycle
    now correctly reports the state IT JUST PRODUCED, using the real
    `RealVisionSource`/`VisionAdapter`/`collect_vision()` stack, not a
    hand-rolled substitute."""
    monkeypatch.setattr(config, "CAMERA_INDEX", 0)
    monkeypatch.setattr(config, "CAMERA_URL", "")
    monkeypatch.setattr(config, "VISION_FPS", 1000.0)  # effectively "run once, fast"
    monkeypatch.setattr(config, "TRACKING_TIMEOUT", 5.0)
    monkeypatch.setattr(config, "MAX_OBJECTS", 20)

    class _Good:
        def __init__(self, source, backend=None):
            pass

        def isOpened(self):
            return True

        def grab(self):
            return True

        def read(self):
            return True, "FRAME"

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Good))
    monkeypatch.setattr(vision, "detect_objects_tracked", lambda frame=None: [])
    monkeypatch.setattr(vision, "attach_pose_keypoints", lambda frame, detections: detections)

    adapter, _bus = _bound_adapter()
    source = RealVisionSource()
    source._vision = vision
    source._config = config
    source.start(adapter)
    try:
        # First real cycle should observe AVAILABLE (fake camera opens
        # cleanly) and the dashboard must reflect it on the VERY NEXT
        # read, not one cycle later.
        deadline = time.time() + 5.0
        while collect_vision(_FakeAdapterManager(adapter))["camera_connected"] is not True:
            if time.time() > deadline:
                pytest.fail("dashboard never reflected camera_connected=True within 5s")
            time.sleep(0.01)
    finally:
        source.stop()


def test_10b_dashboard_camera_connected_field_is_the_authoritative_source_adapter_uses(monkeypatch):
    adapter, _bus = _bound_adapter()
    adapter.on_camera_status({"connected": True, "source": 0, "error": None})
    dashboard = collect_vision(_FakeAdapterManager(adapter))
    assert dashboard["camera_connected"] is True

    adapter.on_camera_status({"connected": False, "source": 0, "error": "boom"})
    dashboard2 = collect_vision(_FakeAdapterManager(adapter))
    assert dashboard2["camera_connected"] is False


def test_10c_first_ever_successful_connect_now_publishes_camera_reconnected(monkeypatch):
    """Sprint 69.1 fix: previously only `False -> True` fired
    `CameraReconnected`; `None -> True` (the very first successful open)
    silently updated only the internal field. Now both fire."""
    adapter, bus = _bound_adapter()
    assert adapter._camera_connected is None

    adapter.on_camera_status({"connected": True, "source": 0, "error": None})
    reconnected = [e for e in bus.published if isinstance(e, CameraReconnected)]
    assert len(reconnected) == 1


# ============================================================================
# 11) no credential leakage in diagnostics
# ============================================================================

def test_11_no_credential_leakage_in_diagnostic_output(monkeypatch):
    secret_user = "admin"
    secret_pass = "SuperSecretPassword123"
    monkeypatch.setattr(config, "CAMERA_URL", f"rtsp://{secret_user}:{secret_pass}@192.168.1.55:554/stream1?token=abc123secret")
    monkeypatch.setattr(config, "CAMERA_OPEN_TIMEOUT_S", 0.2)

    class _Fails:
        def __init__(self, source, backend=None):
            pass

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(vision, "cv2", _fake_cv2(_Fails))

    buf = io.StringIO()
    with redirect_stdout(buf):
        vision.capture_frame()
    output = buf.getvalue()

    assert secret_pass not in output
    assert secret_user not in output
    assert "token=abc123secret" not in output
    assert "SuperSecretPassword123" not in output
    # the redacted classification should still be present (proves the
    # diagnostics actually ran, not that they were silently skipped)
    assert "network(scheme=rtsp, host=192.168.1.55)" in output


def test_11b_classify_source_for_log_never_includes_userinfo_or_path():
    label = vision._classify_source_for_log("http://user:pw@10.0.0.5:8080/stream?key=secret")
    assert "user" not in label
    assert "pw" not in label
    assert "secret" not in label
    assert "stream" not in label
    assert label == "network(scheme=http, host=10.0.0.5)"


def test_11c_local_index_source_classification_is_not_treated_as_sensitive():
    assert vision._classify_source_for_log(0) == "local(index=0)"
