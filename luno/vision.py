"""
"Mata" Luno — kamera + 2 model computer vision yang beda tugas, sesuai diskusi
sebelumnya (cascade: 1 model cepat/murah buat filter terus-menerus, 1 model
"pintar" cuma dipanggil pas beneran dibutuhkan):

- **YOLO** (`ultralytics`, jalan di CPU, ringan, LOKAL): deteksi objek/orang
  cepat (label + bounding box dari kelas COCO standar). Dipakai buat (a) hint
  konteks murah yang ditempel ke prompt vision, dan (b) `start_watch()`
  opsional yang jalan di background nge-track "ada orang di depan kamera
  nggak" tanpa manggil model vision-language sama sekali. YOLO SENDIRI nggak
  "paham" isi gambar — cuma tau label kelas tetap, nggak bisa jawab
  pertanyaan bebas. ONE resident model instance - `_get_yolo()`/
  `_get_yolo_tracking()` share the exact same singleton (see
  `_get_yolo_tracking()`'s own docstring for the RAM bug this fixed).

- **Gemini 2.0 Flash** (via `luno.vision_provider.GeminiVisionProvider`,
  Google's hosted Generative Language API) — model vision-language yang
  BENERAN bisa jawab pertanyaan natural language soal isi gambar ("ini aku
  pegang apa", "coba jelasin ruangan ini"). Runs REMOTELY, nothing resident
  in local RAM/VRAM for this. Aug 2026 migration: this used to be a LOCAL
  MiniCPM-V model served by Ollama - see `_query_vision_provider()`'s own
  docstring for why/how that changed. Called STRICTLY ON-DEMAND, ONE call
  per `ask_vision()` question (tool `lihat_kamera`/the vision-intent
  classifier, see `luno/vision_intent.py`) - never on a timer, never per
  frame. There is deliberately no "ambient continuous vision" mode anymore
  (see `start_vision_watch()`'s own docstring for what that used to be and
  why it was removed, not just repointed at Gemini).

Tambahan: `start_monitor()`/`stop_monitor()` buka JENDELA live-preview kamera
(+ kotak deteksi YOLO digambar langsung di atas videonya) di thread terpisah,
jalan BARENGAN sama Luno - murni buat kamu bisa LIAT sendiri apa yang kamera
tangkep sambil ngobrol, nggak nyambung ke `ask_vision()`/`start_watch()`
sama sekali (independen, boleh nyala salah satu/semua/nggak ada sama sekali).

SETUP (WAJIB sebelum CAMERA_VISION_ENABLED=true di .env kepake beneran):
1. `pip install -r requirements.txt` (nambah opencv-python + ultralytics).
2. Set `GEMINI_API_KEY` di .env (daftar/ambil key di
   https://aistudio.google.com/apikey - gratis buat pemakaian ringan).
   Opsional: `GEMINI_VISION_MODEL` kalau mau ganti model (default
   "gemini-2.0-flash"). Tanpa `GEMINI_API_KEY`, `ask_vision()` balikin
   error yang jelas ("GEMINI_API_KEY belum di-set...") - Luno TETAP jalan
   normal, cuma fitur "lihat kamera" ini yang nggak aktif.
3. Set `CAMERA_VISION_ENABLED=true` di .env (lihat config.py buat opsi
   lain: CAMERA_INDEX kalau webcam bukan device default, dst).
4. YOLO model (`yolo11n.pt`) otomatis ke-download sendiri dari Ultralytics
   pas pertama kali dipanggil (~6MB, jauh lebih kecil dari model lokal apa
   pun yang sebelumnya dipakai buat vision-language).

CATATAN JUJUR: modul ini nggak bisa ditest end-to-end dari sini (nggak ada
webcam/API key Gemini beneran di lingkungan development) — logic-nya udah
diverifikasi jalan (import, error handling pas kamera/Gemini nggak bisa
diakses, lihat `tests/test_vision_provider.py`), tapi kualitas jawaban
Gemini ATAU akurasi deteksi YOLO di kamera kamu sendiri cuma bisa dicek
langsung pas kamu jalanin.
"""

import enum
import platform
import re
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

import cv2

from . import config
from . import vision_memory
from .vision_memory.utils import DEFAULT_OBJECT_LABELS
from .vision_provider import GeminiVisionProvider, OpenAIVisionProvider, VisionProviderError
from .vision_tracking import ObjectTracker, RawDetection

_camera = None
_camera_lock = threading.Lock()


# ─────────────────────────────────────────────
#  Sprint 69 (Camera Device / OpenCV Stability Fix) — camera state machine
# ─────────────────────────────────────────────

class CameraState(enum.Enum):
    """Explicit camera outcome states (Sprint 69 brief's own list) —
    replaces the previous implicit "isOpened() True/False" binary with
    something a caller/diagnostic can actually reason about and log
    without spamming. Honest limitation, documented once here rather
    than repeated at every call site: OpenCV's own return values do
    NOT reliably distinguish "no camera at this index" from "camera
    claimed by another process" across platforms/backends — this module
    makes its best-effort distinction (see `_capture_frame()`'s own
    comments) but BUSY vs UNAVAILABLE should be read as "best guess",
    not a guaranteed diagnosis."""
    UNKNOWN = "UNKNOWN"          # never attempted yet
    AVAILABLE = "AVAILABLE"      # opened AND a frame/probe read succeeded
    UNAVAILABLE = "UNAVAILABLE"  # no candidate backend could open it at all
    BUSY = "BUSY"                # opened, but read() failed right after — best-effort guess: something else may hold the device
    BACKEND_ERROR = "BACKEND_ERROR"  # a candidate backend raised an exception opening (driver/backend problem, not "no camera")


#: Camera outcome states that mean "we just tried and it did not work" —
#: `_capture_frame()` uses this set to decide whether the reopen-cooldown
#: (`config.CAMERA_REOPEN_COOLDOWN_S`) applies.
_CAMERA_FAILURE_STATES = (CameraState.UNAVAILABLE, CameraState.BUSY, CameraState.BACKEND_ERROR)

_camera_state = CameraState.UNKNOWN
_camera_state_reason = None
#: `time.time()`-based deadline; 0.0 means no cooldown is currently active.
_camera_cooldown_until = 0.0

_yolo_model = None
_yolo_lock = threading.Lock()

# P0.8.4 - CONFIRMED ROOT CAUSE (via direct source inspection of the real,
# exact-version-matching `torch==2.13.0`/`ultralytics==8.4.123` packages,
# not execution - torch could not be imported in the sandbox used for this
# investigation; see docs/change_impact/camera_automation_p0_8_4.md
# Section 2 for the full, execution-free evidence chain) of the
# `AttributeError: 'Conv' object has no attribute 'bn'` failure reported
# against the real Tapo C212 stream in P0.8.3:
#
# `_get_yolo()` (used by `detect_objects()`, this file's `_monitor_loop()`,
# and - via `_get_yolo_tracking()`, which is just an alias for this same
# function - `detect_objects_tracked()`) returns ONE shared
# `ultralytics.YOLO` singleton (`_yolo_model`). `start_watch()` runs
# `detect_objects()` on its own background thread (`_watch_thread`) while
# `RealVisionSource`'s tracked-cycle loop runs `detect_objects_tracked()`
# concurrently on a SEPARATE thread (`_cycle_thread`) - both against that
# same shared model object, and (before this P0.8.4 fix) with DIFFERENT
# `device=` kwargs: `detect_objects()` passed none at all, while
# `detect_objects_tracked()` always passed `device=_device_arg()`.
#
# `ultralytics.engine.model.Model.predict()` caches its internal
# `self.predictor` and only rebuilds it when `self.predictor.args.device
# != args.get("device", self.predictor.args.device)` - a check that is
# NEITHER thread-safe NOR stable when calls alternate between "device
# omitted" and "device explicit" against the SAME model object. Every time
# that comparison flipped, ultralytics rebuilt `self.predictor` and re-ran
# `PyTorchBackend.load_model()`, which calls `.fuse()` again on the
# ALREADY-FUSED, SHARED underlying `nn.Module` (confirmed in
# `ultralytics/nn/backends/pytorch.py`). `BaseModel.fuse()` (`ultralytics/
# nn/tasks.py`) only guards each individual `delattr(m, "bn")` with
# `hasattr(m, "bn")` - it is not atomic against a concurrently-running
# `Conv.forward()` on another thread that is mid-way through `self.act(
# self.bn(self.conv(x)))` on that exact module instance. `_yolo_lock`
# above has always guarded the LAZY CONSTRUCTION of `_yolo_model` (the
# `with _yolo_lock:` blocks inside `_get_yolo()`/`_get_yolo_tracking()`/
# `_get_yolo_pose()`) but never the actual inference call - so this race
# was open on every cycle both background threads happened to overlap,
# consistent with the real machine reporting the failure on every cycle
# rather than intermittently.
#
# Fix (this file, P0.8.4): every call site that shares `_yolo_model` now
# (a) always passes the SAME explicit `device=_device_arg()` (so
# `Model.predict()`'s device-mismatch branch can only ever fire once, on
# the very first call, across every caller - never again after that), and
# (b) wraps the actual `model(frame, ...)` call itself in `_yolo_lock`
# (not just construction), so two threads can never run inference against
# the shared singleton at the same instant even during that first call.
# This is an API-usage/concurrency fix in Luno's OWN code - no ultralytics
# package, torch package, or `.pt` checkpoint file was modified, and the
# model/checkpoint themselves were never the problem (P0.8.3 already
# proved, via `pickletools` disassembly, that both `.pt` files on disk are
# ordinary, un-fused, non-stale checkpoints with genuine `bn` weights).
# `_get_yolo_pose()`/`attach_pose_keypoints()` were left untouched: that
# model is a SEPARATE singleton (`_yolo_pose_model`) only ever called from
# inside the tracked-cycle thread itself (never from `_watch_thread`), so
# it was never exposed to this particular race.

# P0.6.2-FIX (Section 13 - "distinguish no detection from detector
# failure"): `detect_objects_tracked()` below has always had a
# `except Exception: return []` contract (Sprint 8, unchanged) - the
# same "never raises, non-fatal" contract `detect_objects()` has always
# had. That is still correct for callers that only want "what's in
# frame right now" and must never crash a polling loop over it. But it
# means a genuine detector failure (e.g. a stale/mismatched YOLO
# checkpoint raising `'Conv' object has no attribute 'bn'` - see
# `_yolo_checkpoint_hint()` below) was, before this fix, INDISTINGUISHABLE
# from "the model ran fine and legitimately found nothing" - both
# produced an empty list. This one small, additive, module-level cache
# closes that gap WITHOUT changing `detect_objects_tracked()`'s existing
# return-contract (still `[]`, never raises) - a caller that wants to
# know "did the last cycle actually fail" can now check
# `last_tracked_detection_error()`; every existing caller that never
# calls that new getter sees zero behavior change.
_last_tracked_detection_error: Optional[str] = None

# P0.8.5 - TEMPORARY diagnostic-only state (see `detect_objects_tracked()`'s
# own `[VISION PERSON DEBUG]` log line below). Tracks ONLY whether the
# most recently completed tracked cycle saw >=1 person, purely so the log
# line can print a `previous_person_state`/`new_person_state` transition -
# a passive read/write for logging, never consulted by any production
# decision (the real presence/debounce state machines this investigation
# is diagnosing - `VisionAdapter._person_present_debounced`/
# `_known_humans` - are completely untouched by this variable). Safe to
# remove entirely once this investigation's temporary logging is no
# longer needed.
_debug_last_person_state: bool = False

_vision_provider = None
_vision_provider_lock = threading.Lock()

_watch_thread = None
_watch_running = False
_last_detections = []
_last_detection_at = 0.0
_person_present = False

# P0.6.3 (Section 13 - "the existing Vision detector failure handling
# from P0.6.2-FIX must remain [and] the existing Vision error event
# should remain observable"): P0.6.2-FIX only closed this gap for
# `detect_objects_tracked()`/the Sprint 8 tracked-cycle loop that feeds
# the dashboard's rich per-object view. It did NOT cover THIS function
# (`detect_objects()`) - and `detect_objects()` is what actually feeds
# `_watch_loop()` below, which is what `CameraPersonEntered`/
# `CameraPersonLeft` (and therefore Camera Automation's own
# `human_detected`/`human_cleared`) are derived from (see `luno/
# adapters/vision.py::_update_person_presence()`). A detector failure
# here was, before this sprint, exactly as invisible as the
# `detect_objects_tracked()` case P0.6.2-FIX fixed - `except Exception:
# return []` made "the model raised" look identical to "genuinely no
# objects in frame" to every downstream consumer, including the actual
# human-presence state machine Camera Automation depends on. Same
# additive pattern as P0.6.2-FIX's `_last_tracked_detection_error`/
# `last_tracked_detection_error()` - see `detect_objects()`'s own
# except block below.
_last_presence_detection_error: Optional[str] = None

_monitor_thread = None
_monitor_running = False

# Sprint 8: explicit connection-state tracking (separate from the lazy
# reopen-on-next-call behavior `_capture_frame()` already had) - so
# callers (real_vision.py) can tell "camera genuinely went away just
# now" apart from "camera was never opened yet", and publish
# CameraDisconnected/CameraReconnected exactly once per actual state
# change instead of on every failed frame grab.
_camera_connected = None  # None = never attempted yet; True/False after first attempt
_camera_last_error = None


# ─────────────────────────────────────────────
#  Sprint 69.1 (Camera Runtime/Dashboard Disconnect Forensics) — structured
#  diagnostic logging for the camera open/probe path. Added specifically
#  because Sprint 69's own report could not be fully verified against a
#  real camera/Windows machine from this sandbox - these lines make the
#  ACTUAL runtime source/backend/timing/outcome directly visible in the
#  application's own log output, closing that verification gap without
#  requiring code access to the failing machine. Every line goes through
#  `_log_diag()` below, deliberately a small, dependency-free local
#  helper (matches this module's existing plain print()-based "[Vision]"
#  logging convention) rather than importing `luno.adapters.utils.log()`
#  - `luno/adapters/__init__.py`'s own docstring documents that adapters
#  call INTO `luno.vision`, never the reverse; adding a vision.py ->
#  adapters import here would invert that layering for no real benefit
#  (the timestamp format below matches adapters.utils.log()'s own purely
#  so a merged log stream reads consistently, not because of a shared
#  implementation).
# ─────────────────────────────────────────────

def _log_diag(message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [Vision] {message}")


def _elapsed_ms(start: float) -> float:
    return round((time.time() - start) * 1000.0, 1)


def _classify_source_for_log(source):
    """Classifies `source` for diagnostic logging WITHOUT ever exposing
    credentials or a complete authenticated URL (Sprint 69.1 brief's own
    explicit constraint). A local int device index is logged verbatim
    (not sensitive). A string (`CAMERA_URL`, possibly Tapo-auto-derived
    - see `config.py`'s own `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD`
    auto-derivation) is parsed and reduced to scheme+host ONLY - userinfo
    (`user:pass@`) is always dropped by `urlsplit().hostname`/`.scheme`
    never reading it, and path/query are always dropped entirely (a
    stream key/token could live there for some camera vendors) - the
    caller only ever sees e.g. "network(scheme=rtsp, host=192.168.1.20)",
    never the credentialed URL itself."""
    if isinstance(source, str):
        try:
            parts = urlsplit(source)
            host = parts.hostname or "unknown"
            return f"network(scheme={parts.scheme or 'unknown'}, host={host})"
        except Exception:
            return "network(unparseable)"
    return f"local(index={source})"


#: Reverse lookup (int value -> readable name) for the small, fixed set of
#: `cv2.CAP_*` backend flags this module ever explicitly requests or is
#: likely to see reported back by `cap.getBackendName()`-adjacent APIs -
#: built lazily (once) since `getattr(cv2, name, None)` is a real,
#: non-free lookup and this is only needed for logging, never for control
#: flow (`_local_backend_candidates()` remains the actual source of truth
#: for what gets requested).
_BACKEND_NAME_CANDIDATES = (
    "CAP_DSHOW", "CAP_MSMF", "CAP_V4L2", "CAP_V4L2_ANY", "CAP_AVFOUNDATION",
    "CAP_FFMPEG", "CAP_ANY", "CAP_OBSENSOR", "CAP_GSTREAMER",
)
_backend_id_to_name = None


def _sanitize_error_text(text, source):
    """Sprint 69.1: best-effort redaction of a raised exception's OWN
    message before it is ever stored in `_camera_last_error`/
    `_camera_state_reason` or logged. `_classify_source_for_log()`
    guards every diagnostic line THIS module writes, but a third-party
    exception (e.g. an OpenCV error raised while opening a `CAMERA_URL`
    string source) can legitimately embed its failing argument - the
    raw, credentialed source - verbatim in its own `str(ex)` text. This
    is deliberately best-effort (arbitrary free-text from a dependency
    can't be perfectly sanitized) - not a guarantee, but real coverage
    for the one place a raw credentialed string could otherwise slip
    into a stored error/reason/logged line through someone else's
    exception message rather than this module's own formatting."""
    if not text or not isinstance(source, str):
        return text
    sanitized = text.replace(source, _classify_source_for_log(source))
    try:
        parts = urlsplit(source)
        if parts.username:
            sanitized = sanitized.replace(parts.username, "***")
        if parts.password:
            sanitized = sanitized.replace(parts.password, "***")
    except Exception:
        pass
    return sanitized


def _backend_label(backend):
    """Human-readable label for a `cv2.CAP_*` int constant (or `None`,
    meaning "no explicit backend - OpenCV's own CAP_ANY auto-probe
    chooses"), for diagnostic logging only."""
    global _backend_id_to_name
    if backend is None:
        return "CAP_ANY(implicit, no explicit backend requested)"
    if _backend_id_to_name is None:
        _backend_id_to_name = {}
        for name in _BACKEND_NAME_CANDIDATES:
            value = getattr(cv2, name, None)
            if value is not None:
                _backend_id_to_name[value] = name
    return _backend_id_to_name.get(backend, f"backend#{backend}")


def is_configured():
    return config.CAMERA_VISION_ENABLED


# ─────────────────────────────────────────────
#  KAMERA
# ─────────────────────────────────────────────

def camera_source():
    """The ONE place that decides what `cv2.VideoCapture(...)` opens -
    an IP camera URL (`CAMERA_URL`) if one is configured, otherwise the
    USB/integrated webcam device index (`CAMERA_INDEX`, default 0).
    `cv2.VideoCapture` accepts either an int device index or a URL string
    identically, so no other code path needs to branch on which kind of
    source this is - every caller (`_capture_frame()`, the health check
    in `luno/bootstrap/health.py`, tests) should call this rather than
    reading `config.CAMERA_INDEX`/`config.CAMERA_URL` directly, so a
    camera source is NEVER hardcoded anywhere else in the codebase."""
    return config.CAMERA_URL if config.CAMERA_URL else config.CAMERA_INDEX


def _local_backend_candidates():
    """Sprint 69: ordered list of explicit `cv2.CAP_*` backend flags to
    try for a LOCAL (int device-index) camera source. Built from the
    actual reported failure evidence, not speculation: `cv2.VideoCapture
    (index)` with no backend argument lets OpenCV's own CAP_ANY
    auto-probe choose a backend, and the Sprint 69 bug report's own log
    showed that auto-probe reaching BOTH `CAP_FFMPEG` (triggering a
    ~30-second internal "opencv_ffmpeg_interrupt_callback Stream
    timeout") and `CAP_OBSENSOR` ("Camera index out of range") before
    ever reaching a backend meant for a local USB/integrated webcam.
    FFMPEG is the CORRECT backend for a network stream (RTSP/HTTP via
    `CAMERA_URL`) - see `camera_source()`'s own docstring and
    `_open_camera_with_discovery()` below, which deliberately leaves
    string sources alone. The bug is only that CAP_ANY can ALSO reach
    FFMPEG/obsensor for a plain int index, with no way to opt out short
    of naming a backend explicitly.

    This returns candidates, never forces exactly one: Windows tries
    `CAP_DSHOW` first (the long-standing DirectShow backend most USB
    webcam drivers target), then `CAP_MSMF` (Media Foundation, present
    since Windows 8 - sometimes the only backend a newer webcam driver
    actually supports) - each tried in BOUNDED time by
    `_open_capture_bounded()`, so a bad first candidate costs at most one
    timeout window, never an unbounded wait, and never an unbounded
    NUMBER of attempts either (this list is always short and finite).
    Linux gets `CAP_V4L2` (Video4Linux2 - confirmed against this
    project's own sandbox: `cv2.VideoCapture(0)` with no explicit
    backend already reaches V4L2 first there). macOS gets
    `CAP_AVFOUNDATION`. An unrecognized platform, or one where none of
    its own candidate flags actually exist in the installed `cv2` build,
    falls back to `[None]` (i.e. let `cv2`'s own CAP_ANY choose - the
    pre-Sprint-69 behavior) rather than guessing at a flag name `cv2`
    doesn't have."""
    system = platform.system()
    if system == "Windows":
        names = ("CAP_DSHOW", "CAP_MSMF")
    elif system == "Linux":
        names = ("CAP_V4L2",)
    elif system == "Darwin":
        names = ("CAP_AVFOUNDATION",)
    else:
        names = ()
    candidates = [getattr(cv2, name, None) for name in names]
    candidates = [c for c in candidates if c is not None]
    return candidates or [None]


def _open_capture_bounded(source, backend, timeout_s):
    """Sprint 69: opens `cv2.VideoCapture(source)` (or `cv2.VideoCapture
    (source, backend)` when `backend` is not `None`) on a background
    daemon thread, and waits at most `timeout_s` for it.
    `cv2.VideoCapture`'s constructor has no timeout parameter of its
    own - this is the only way to bound it (the same technique `luno/
    bootstrap/health.py`'s Camera startup check already used before
    Sprint 69, generalized here into the ONE shared implementation both
    that check and `_capture_frame()` now call, instead of two separate
    ad hoc copies - see Sprint 69's own change-impact doc for why that
    consolidation matters for the concurrency guarantee below).

    Returns `(cap, timed_out, error)`:
    - success: `(cap, False, None)` - `cap.isOpened()` is True.
    - opened but never reported ready, or `isOpened()` False: `(None,
      False, None)` - the `cap` this got is released HERE before
      returning, so no handle ever leaks back to the caller in this
      case either.
    - the constructor itself raised: `(None, False, str(exception))`.
    - the constructor did not return within `timeout_s`: `(None, True,
      None)` - the background thread is deliberately NOT killed (Python
      cannot forcibly kill a thread) but is left running; if/when it
      eventually finishes, it releases whatever `cv2.VideoCapture` it
      got before exiting, so a slow-but-eventually-successful open never
      leaks a device handle just because the caller already gave up
      waiting on it.

    Sprint 69.1: every attempt now logs its start and outcome (source
    classification - never a raw credentialed URL, requested backend,
    elapsed time, outcome) via `_log_diag()` - this is the exact
    evidence the Sprint 69.1 brief asks for ("selected backend, open
    start/end time, success/failure reason") and is the single choke
    point every camera open in this module passes through, so one set of
    log lines here covers every caller. On success, also compares the
    ACTUALLY-opened backend (`cap.getBackendName()`, when the installed
    OpenCV build exposes it) against the one explicitly requested - if
    OpenCV silently opened via a DIFFERENT backend than requested (a
    real, documented possibility when the requested backend exists as
    an enum constant but was not compiled into the installed OpenCV
    build), this is logged as a warning rather than silently trusted,
    since that exact mismatch would explain a local open still ending up
    on `CAP_FFMPEG`/`CAP_ANY` despite this module explicitly requesting
    e.g. `CAP_DSHOW`."""
    source_label = _classify_source_for_log(source)
    backend_label = _backend_label(backend)
    t_start = time.time()
    _log_diag(f"camera open attempt: source={source_label} backend={backend_label} timeout_s={timeout_s}")

    result = {}

    def _open():
        try:
            cap = cv2.VideoCapture(source) if backend is None else cv2.VideoCapture(source, backend)
            result["cap"] = cap
        except Exception as ex:
            # Sprint 69.1: sanitize BEFORE storing - a raised OpenCV
            # exception can legitimately embed the raw, credentialed
            # source verbatim in its own message text (see
            # `_sanitize_error_text()`'s own docstring).
            result["error"] = _sanitize_error_text(str(ex), source)

    thread = threading.Thread(target=_open, daemon=True, name="luno-camera-open")
    thread.start()
    thread.join(timeout=timeout_s)

    if thread.is_alive():
        _log_diag(
            f"camera open result: source={source_label} backend={backend_label} "
            f"outcome=TIMEOUT elapsed_ms={_elapsed_ms(t_start)} "
            f"(background open still running - will be released whenever it eventually completes)"
        )

        def _release_when_done():
            thread.join()
            cap = result.get("cap")
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                _log_diag(
                    f"camera open (late arrival): source={source_label} backend={backend_label} "
                    f"eventually completed after the caller already timed out - released"
                )
        threading.Thread(target=_release_when_done, daemon=True, name="luno-camera-open-cleanup").start()
        return None, True, None

    if "error" in result:
        _log_diag(
            f"camera open result: source={source_label} backend={backend_label} "
            f"outcome=BACKEND_ERROR elapsed_ms={_elapsed_ms(t_start)} error={result['error']!r}"
        )
        return None, False, result["error"]

    cap = result.get("cap")
    if cap is None or not cap.isOpened():
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        _log_diag(
            f"camera open result: source={source_label} backend={backend_label} "
            f"outcome=NOT_OPENED elapsed_ms={_elapsed_ms(t_start)}"
        )
        return None, False, None

    actual_backend_name = None
    try:
        if hasattr(cap, "getBackendName"):
            actual_backend_name = cap.getBackendName()
    except Exception:
        pass
    mismatch_note = ""
    if actual_backend_name and backend is not None:
        requested_name = _backend_label(backend)
        # `requested_name` is our own readable label (e.g. "CAP_DSHOW");
        # OpenCV's own `getBackendName()` uses its own strings (e.g.
        # "DSHOW") - compare loosely (substring, case-insensitive) rather
        # than requiring an exact match, since the two naming schemes are
        # related but not guaranteed identical.
        if actual_backend_name.upper() not in requested_name.upper():
            mismatch_note = (
                f" ⚠ WARNING: requested {requested_name} but OpenCV actually opened via "
                f"backend={actual_backend_name!r} - the explicit backend request may have been "
                f"silently ignored (e.g. not compiled into this OpenCV build)"
            )
    _log_diag(
        f"camera open result: source={source_label} backend={backend_label} "
        f"outcome=SUCCESS elapsed_ms={_elapsed_ms(t_start)} "
        f"actual_backend={actual_backend_name!r}{mismatch_note}"
    )
    return cap, False, None


def _open_camera_with_discovery(source, per_candidate_timeout_s=None):
    """Sprint 69: tries each backend candidate (`_local_backend_
    candidates()`, only for an int device-index `source`) in order via
    `_open_capture_bounded()`, stopping at the first success. A STRING
    `source` (an RTSP/HTTP URL from `CAMERA_URL`) gets backend selection
    left COMPLETELY ALONE (`[None]`, OpenCV's own CAP_ANY choice) -
    FFMPEG is the right backend for a network stream, and this fix is
    scoped to the local-webcam-only bug the bug report's log actually
    shows (item 13 of the brief: never touch unrelated vision
    behavior).

    Returns `(cap_or_None, CameraState, reason)`. If every candidate
    failed: `BACKEND_ERROR` if at least one candidate raised an
    exception (points at a genuine driver/backend problem, not "no
    camera"), else `UNAVAILABLE` (every candidate returned cleanly but
    never opened, or timed out - OpenCV does not reliably distinguish
    "no camera at this index" from "camera busy/claimed elsewhere"
    across platforms/backends, so this module does not claim to either -
    see `CameraState`'s own docstring)."""
    timeout_s = per_candidate_timeout_s if per_candidate_timeout_s is not None else config.CAMERA_OPEN_TIMEOUT_S
    backends = [None] if isinstance(source, str) else _local_backend_candidates()
    _log_diag(
        f"camera discovery starting: source={_classify_source_for_log(source)} "
        f"candidate_backends={[_backend_label(b) for b in backends]} timeout_s={timeout_s}"
    )

    saw_error = None
    saw_timeout = False
    for backend in backends:
        cap, timed_out, error = _open_capture_bounded(source, backend, timeout_s)
        if cap is not None:
            return cap, CameraState.AVAILABLE, None
        if error is not None:
            saw_error = error
        if timed_out:
            saw_timeout = True

    if saw_error is not None:
        return None, CameraState.BACKEND_ERROR, saw_error
    if saw_timeout:
        return None, CameraState.UNAVAILABLE, (
            f"no candidate backend responded within {timeout_s}s per attempt "
            "(camera driver may be stuck, or the device is claimed by another app)"
        )
    return None, CameraState.UNAVAILABLE, f"could not open camera source {_classify_source_for_log(source)}"


def _set_camera_state(new_state, reason):
    """Sprint 69.1: single choke point for updating `_camera_state`/
    `_camera_state_reason`, logging the transition (only when the state
    actually CHANGES - a poll ticking at 2/s while stuck e.g. BUSY must
    not spam one line per tick) via `_log_diag()`. Callers must already
    hold `_camera_lock` (every existing call site does - this function
    does not lock itself, it only centralizes the assignment+logging
    that `_capture_frame()` previously repeated at each of its 4 own
    call sites inline). This directly answers the Sprint 69.1 brief's
    "camera state transition" and "correlation between the vision poll
    and camera state" diagnostic requirements - every real transition is
    now a single, grep-able log line, not something that has to be
    inferred from `camera_status()` snapshots taken at different times."""
    global _camera_state, _camera_state_reason
    if new_state != _camera_state:
        _log_diag(f"camera state transition: {_camera_state.value} -> {new_state.value} (reason={reason!r})")
    _camera_state = new_state
    _camera_state_reason = reason


def _capture_frame():
    """Ambil 1 frame BGR (format native OpenCV) dari kamera, atau None kalau
    gagal (kamera nggak ada/lagi dipakai app lain/dst). Buka device kamera
    sekali aja lalu dipakai ulang (buka device tiap kali cukup lambat -
    ratusan ms - jadi sayang kalau dilakuin tiap panggilan).

    Seluruh open+grab+read dikunci di `_camera_lock` YANG SAMA buat semua
    caller - ada beberapa hal yang manggil ini BARENGAN dari thread berbeda
    (watch loop, monitor window, tool `lihat_kamera` on-demand), dan
    `cv2.VideoCapture` dari 1 device nggak aman dibaca dari 2 thread
    sekaligus tanpa ini - bisa ke-interleave/frame korup.

    Sprint 8: also updates `_camera_connected`/`_camera_last_error` (see
    `camera_status()`) - reopening the device here on the next call after
    a failure IS the "automatic reconnect" the spec asks for; this just
    makes that transition OBSERVABLE instead of silent.

    Sprint 69 (Camera Device / OpenCV Stability Fix): the actual open
    now goes through `_open_camera_with_discovery()` - bounded time per
    backend candidate, explicit local-camera backend selection instead
    of CAP_ANY's own auto-probe (see that function's docstring for the
    evidence). It ALSO now respects a reopen cooldown
    (`config.CAMERA_REOPEN_COOLDOWN_S`) after a failed attempt: if the
    camera is already known UNAVAILABLE/BUSY/BACKEND_ERROR and the
    cooldown hasn't elapsed yet, this returns `None` immediately without
    touching `cv2` at all. Without this, a background poll loop calling
    `capture_frame()` several times a second (e.g. `RealVisionSource.
    _tracked_cycle_loop()` at the default `VISION_FPS`) would re-attempt
    a known-broken open on EVERY tick, potentially re-triggering a slow
    backend timeout every single time - exactly the repeated-stall
    pattern the Sprint 69 bug report's log showed, not a one-time
    startup hang (that part was already fixed - see `luno/bootstrap/
    health.py`'s own history)."""
    global _camera, _camera_connected, _camera_last_error
    global _camera_state, _camera_state_reason, _camera_cooldown_until
    with _camera_lock:
        if _camera is None or not _camera.isOpened():
            now = time.time()
            if _camera_state in _CAMERA_FAILURE_STATES and now < _camera_cooldown_until:
                _log_diag(
                    f"camera open SKIPPED (cooldown active, {round(_camera_cooldown_until - now, 1)}s "
                    f"remaining): state={_camera_state.value} reason={_camera_state_reason!r} - "
                    f"not touching cv2 at all this tick"
                )
                return None
            _camera = None
            cap, state, reason = _open_camera_with_discovery(camera_source())
            _set_camera_state(state, reason)
            if cap is None:
                _camera_connected = False
                _camera_last_error = reason or f"could not open camera source {_classify_source_for_log(camera_source())}"
                _camera_cooldown_until = now + config.CAMERA_REOPEN_COOLDOWN_S
                return None
            _camera = cap
        if not _camera.isOpened():
            _camera_connected = False
            _camera_last_error = f"could not open camera source {_classify_source_for_log(camera_source())}"
            _set_camera_state(CameraState.UNAVAILABLE, _camera_last_error)
            _camera_cooldown_until = time.time() + config.CAMERA_REOPEN_COOLDOWN_S
            return None
        # Buang beberapa frame basi dulu - banyak webcam nge-buffer beberapa
        # frame lama, jadi read() pertama abis lama nganggur bisa ngasih
        # gambar yang udah nggak up-to-date beberapa ratus ms/detik.
        for _ in range(2):
            _camera.grab()
        ok, frame = _camera.read()
        if not ok or frame is None:
            _camera_connected = False
            _camera_last_error = "camera.read() returned no frame"
            # Opened successfully, then read() failed right after - the
            # closest OpenCV-observable signal to "busy/claimed by
            # another app" this module can produce without OS-specific
            # device-handle introspection (see `CameraState`'s own
            # honesty note). Release + drop the stale handle so the NEXT
            # attempt (after cooldown) starts from a clean reopen rather
            # than repeatedly calling `.read()` on a capture object that
            # already proved broken.
            _set_camera_state(CameraState.BUSY, "camera opened but read() returned no frame")
            _camera_cooldown_until = time.time() + config.CAMERA_REOPEN_COOLDOWN_S
            try:
                _camera.release()
            except Exception:
                pass
            _camera = None
            return None
        _camera_connected = True
        _camera_last_error = None
        _set_camera_state(CameraState.AVAILABLE, None)
        _camera_cooldown_until = 0.0
        return frame


def capture_frame():
    """Public wrapper around `_capture_frame()` - the one supported way
    for OTHER modules (`luno/adapters/real_vision.py`) to grab a single
    frame without reaching into this module's private function. Same
    contract: one BGR frame, or `None` on any camera failure."""
    return _capture_frame()


def camera_status():
    """`{"connected": bool|None, "source": <index or URL>, "error": str|None,
    "state": <CameraState value>, "state_reason": str|None,
    "cooldown_remaining_s": float}` - `connected` is `None` only before
    the very first capture attempt ever made (nothing to report yet,
    distinct from a real failure). Read-only, never opens/closes the
    device itself - purely reports what the last real `_capture_frame()`
    (or `probe_camera()`) attempt observed. `state`/`state_reason`/
    `cooldown_remaining_s` are Sprint 69 additions - existing keys
    (`connected`/`source`/`error`) are unchanged so no existing caller
    needs to change."""
    with _camera_lock:
        remaining = max(0.0, _camera_cooldown_until - time.time()) if _camera_cooldown_until else 0.0
        return {
            "connected": _camera_connected,
            "source": camera_source(),
            "error": _camera_last_error,
            "state": _camera_state.value,
            "state_reason": _camera_state_reason,
            "cooldown_remaining_s": round(remaining, 1),
        }


def release_camera():
    """Lepas device kamera eksplisit (mis. kalau mau dipakai app lain
    sementara). Aman dipanggil kapan pun - `_capture_frame()` bakal buka
    ulang otomatis pas dibutuhkan lagi."""
    global _camera
    with _camera_lock:
        if _camera is not None:
            _camera.release()
            _camera = None


def probe_camera(timeout_s=None):
    """Sprint 69: one-shot camera probe - opens the configured
    `camera_source()` via the same bounded, backend-candidate-aware path
    `_capture_frame()` uses, checks `isOpened()`, and ALWAYS releases
    before returning. Does NOT touch or replace the persistent `_camera`
    singleton `_capture_frame()` owns (this is a diagnostic check, not a
    real capture), and is guarded by the SAME `_camera_lock` every other
    camera operation uses - this function can never open the device
    concurrently with a real `capture_frame()`/`_capture_frame()` call
    happening on another thread, and vice versa (Sprint 69 brief item 9:
    startup probe and scheduled polling must never open the same device
    at once). Used by `luno/bootstrap/health.py`'s startup Camera check
    (which, before Sprint 69, ran its own separate, uncoordinated
    `cv2.VideoCapture(CAMERA_INDEX)` - ignoring `CAMERA_URL`, ignoring
    this lock, and never getting the backend-candidate fix either) and
    by `discover_cameras()` below.

    Returns `(ok, CameraState, reason)`. Does NOT update the module's
    persistent `_camera_state`/`_camera_connected` fields
    (`camera_status()` still reflects the last real `_capture_frame()`
    outcome, not this diagnostic one) - a health check or diagnostic run
    should never overwrite what the actual running application last
    observed."""
    with _camera_lock:
        cap, state, reason = _open_camera_with_discovery(camera_source(), timeout_s)
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            return True, state, reason
        return False, state, reason


#: Sprint 69 `discover_cameras()`'s own default candidate-index range -
#: small and explicit (never an unbounded scan). Most consumer machines
#: have at most 1-2 cameras; 5 leaves comfortable headroom without
#: risking a long diagnostic run (worst case: 5 indices x up to 2
#: Windows backend candidates x `CAMERA_OPEN_TIMEOUT_S` each).
DISCOVER_CAMERAS_DEFAULT_MAX_INDEX = 5


def discover_cameras(max_index=None, timeout_s=None):
    """Sprint 69 item 12: read-only-in-the-sense-that-matters camera
    discovery/diagnostic - NEVER touches `config.CAMERA_INDEX`/
    `config.CAMERA_URL`/any `config/*.json` file, NEVER changes which
    source the running application actually uses (`camera_source()`'s
    own return value is completely unaffected by calling this - this
    module has exactly one writer of `config.CAMERA_INDEX`/
    `config.CAMERA_URL`, and it is not this function). It does
    physically open/close camera devices transiently, which is
    unavoidable for real discovery, but never leaves one open: probes
    device indices `0..max_index-1` (bounded, small default range - see
    `DISCOVER_CAMERAS_DEFAULT_MAX_INDEX` - never scans indefinitely) ONE
    AT A TIME, each fully released before moving to the next index, all
    under the SAME `_camera_lock` every other camera operation uses (so
    this can never run concurrently with a real `capture_frame()` call
    either).

    Returns a list of dicts, one per index, in index order:
    `{"index": int, "backend_used": str|None, "state": <CameraState
    value>, "reason": str|None, "open_time_ms": float,
    "read_ok": bool|None, "read_time_ms": float|None,
    "resolution": [w, h]|None, "fps": float|None}`."""
    max_index = max_index if max_index is not None else DISCOVER_CAMERAS_DEFAULT_MAX_INDEX
    results = []
    for idx in range(max_index):
        entry = {
            "index": idx, "backend_used": None, "state": None, "reason": None,
            "open_time_ms": None, "read_ok": None, "read_time_ms": None,
            "resolution": None, "fps": None,
        }
        with _camera_lock:
            t0 = time.time()
            cap, state, reason = _open_camera_with_discovery(idx, timeout_s)
            entry["open_time_ms"] = round((time.time() - t0) * 1000, 1)
            entry["state"] = state.value
            entry["reason"] = reason
            if cap is not None:
                try:
                    if hasattr(cap, "getBackendName"):
                        entry["backend_used"] = cap.getBackendName()
                except Exception:
                    pass
                try:
                    t1 = time.time()
                    ok, frame = cap.read()
                    entry["read_time_ms"] = round((time.time() - t1) * 1000, 1)
                    entry["read_ok"] = bool(ok and frame is not None)
                    if entry["read_ok"] and hasattr(frame, "shape"):
                        h, w = frame.shape[:2]
                        entry["resolution"] = [w, h]
                except Exception as ex:
                    entry["read_ok"] = False
                    entry["reason"] = f"post-open read error: {ex}"
                try:
                    if hasattr(cap, "get"):
                        fps = cap.get(cv2.CAP_PROP_FPS)
                        if fps:
                            entry["fps"] = round(float(fps), 2)
                except Exception:
                    pass
                try:
                    cap.release()
                except Exception:
                    pass
        results.append(entry)
    return results


# ─────────────────────────────────────────────
#  YOLO — deteksi cepat/murah (BUKAN yang jawab pertanyaan)
# ─────────────────────────────────────────────

#: Matches the exact failure signature `_yolo_checkpoint_hint()` looks
#: for - `'Conv' object has no attribute 'bn'` and the same on
#: `ConvTranspose`. Matched against the exception's STRING MESSAGE, not
#: (only) its `.name` attribute - see that function's own docstring
#: (P0.8.3) for why the message text is the reliable signal here.
_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE = re.compile(
    r"'(?:Conv|ConvTranspose)' object has no attribute 'bn'"
)


def _yolo_checkpoint_hint(ex: Exception) -> str:
    """Detects the specific "stale/mismatched YOLO checkpoint" failure
    signature (`AttributeError: 'Conv' object has no attribute 'bn'`,
    and the same on `ConvTranspose`) and returns an actionable hint to
    append to the log line - empty string for anything else, so every
    OTHER kind of detection failure (no webcam, bad frame, ...) is
    completely unaffected.

    Root cause, confirmed against `ultralytics/nn/tasks.py`'s own
    `BaseModel.fuse()`: the first time a loaded model actually runs,
    ultralytics permanently `delattr`s `.bn` off every `Conv`/
    `ConvTranspose` layer and rebinds that layer's `.forward` to
    `.forward_fuse` (which never touches `.bn`) - a one-way, in-place
    optimization. If the LOCAL `.pt` checkpoint file on disk predates
    the currently installed `ultralytics` package (downloaded once,
    `ultralytics` upgraded since), the reconstructed module graph can
    disagree with what today's `Conv.forward()` expects before fuse()
    even runs, and this exact `AttributeError` is the result - a
    stale/mismatched cached checkpoint file, not a webcam/config
    problem (which is why this is safe to keep treating as non-fatal
    and skipping, same as any other detection failure).

    P0.8.3 fix - confirmed bug found in the ORIGINAL matching condition:
    it only ever checked `getattr(ex, "name", None) == "bn"`. `.name` is
    populated by Python automatically ONLY for AttributeErrors the
    interpreter itself raises via the default, implicit attribute-lookup
    path. `Conv`/`ConvTranspose` are `torch.nn.Module` subclasses, and
    `self.bn` failing after fuse() is instead raised by `torch.nn.
    modules.module.Module.__getattr__`'s own explicit, hand-written
    `raise AttributeError(f"'{type(self).__name__}' object has no
    attribute '{name}'")` - a plain, message-only construction that never
    sets `.name` (confirmed by direct inspection of the actually-
    installed `torch` package's own source, `torch/nn/modules/module.py`
    around `Module.__getattr__`). The ORIGINAL condition therefore never
    matched this failure in real use against real PyTorch/ultralytics -
    it only ever matched a hand-crafted test double that manually set
    `.name` itself (see `tests/test_p0_8_3_yolo_checkpoint_diagnostics.
    py` for a regression test built from `Module.__getattr__`'s own,
    real raise pattern, not a `.name`-carrying stand-in). Fixed by
    additionally matching the exception's own string message against
    `_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE` above - `.name` is still
    checked first (kept, not removed, in case a future torch release
    ever does pass `name=` through `Module.__getattr__`), the message
    match is the new, primary, reliable path."""
    if not isinstance(ex, AttributeError):
        return ""
    if getattr(ex, "name", None) != "bn" and not _YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE.search(str(ex)):
        return ""
    return (
        f" -> looks like a stale/mismatched local model checkpoint: delete "
        f"{config.YOLO_MODEL_PATH} and {config.YOLO_POSE_MODEL_PATH} so they "
        f"re-download fresh, and run 'pip install -U ultralytics'"
    )


def _get_yolo():
    global _yolo_model
    with _yolo_lock:
        if _yolo_model is None:
            from ultralytics import YOLO  # import lambat (~1-2s) - baru dipanggil kalau beneran kepake
            _yolo_model = YOLO(config.YOLO_MODEL_PATH)
        return _yolo_model


def detect_objects(frame=None):
    """Return list nama kelas objek yang kedeteksi (unik, urut abjad) di atas
    threshold `YOLO_CONFIDENCE`. Ambil frame baru dari kamera kalau `frame`
    nggak dikasih. Return [] (BUKAN error) kalau kamera/model gagal - dipakai
    cuma sebagai hint tambahan, jadi kegagalan di sini nggak boleh sampai
    nge-block `ask_vision()`."""
    global _last_presence_detection_error
    try:
        if frame is None:
            frame = _capture_frame()
        if frame is None:
            # No frame to even try on (camera not open this cycle) is
            # NOT a detector failure - see `detect_objects_tracked()`'s
            # own identical comment (P0.6.2-FIX). Clears any stale error
            # from a previous cycle rather than leaving a now-irrelevant
            # old failure flag set forever.
            _last_presence_detection_error = None
            return []
        model = _get_yolo()
        # P0.8.4 fix - see this file's own P0.8.4 note near `_yolo_lock`'s
        # declaration for why `device=_device_arg()` and this lock are both
        # required here, not just in `detect_objects_tracked()`.
        with _yolo_lock:
            results = model(frame, verbose=False, conf=config.YOLO_CONFIDENCE, device=_device_arg())
        names = set()
        for r in results:
            for cls_id in r.boxes.cls.tolist():
                names.add(model.names[int(cls_id)])
        # The model call above completed without raising - this cycle's
        # detector genuinely ran, so any previously-recorded failure no
        # longer applies (P0.6.3 Section 13).
        _last_presence_detection_error = None
        return sorted(names)
    except Exception as ex:
        hint = _yolo_checkpoint_hint(ex)
        print(f"[Vision] ⚠ YOLO detect gagal (non-fatal, dilewatin): {ex}{hint}")
        # P0.6.3 Section 13 - record WHY this cycle produced an empty
        # result, without changing the `[]`/never-raises contract itself
        # (every existing caller - `_watch_loop()`, `ask_vision()`'s hint
        # text - is unaffected). See `last_presence_detection_error()`
        # below, the SAME pattern P0.6.2-FIX established for
        # `detect_objects_tracked()`/`last_tracked_detection_error()`.
        _last_presence_detection_error = f"{type(ex).__name__}: {ex}{hint}"
        return []


def last_presence_detection_error() -> Optional[str]:
    """P0.6.3 (Section 13) - `None` if the most recently completed
    `detect_objects()` cycle ran the model without raising (whether or
    not it found anything - an empty scene is not a failure). Otherwise,
    a short, sanitized description of why that cycle's detector call
    itself failed. This is what lets `RealVisionSource._poll_once()`
    (the presence-watch loop that `CameraPersonEntered`/
    `CameraPersonLeft` - and therefore Camera Automation's own
    `human_detected`/`human_cleared` - are derived from) tell "no one is
    in frame right now" apart from "the detector is broken right now",
    the same distinction P0.6.2-FIX already made available for the
    separate Sprint 8 tracked-cycle loop via `last_tracked_detection_
    error()`."""
    return _last_presence_detection_error


# ─────────────────────────────────────────────
#  Sprint 8 - structured, tracked detections (bbox + confidence) + human
#  pose estimation. Additive: `detect_objects()` above is UNCHANGED and
#  still used by `ask_vision()`'s hint text and the plain presence-watch;
#  everything below is a NEW, separate path feeding the real Vision
#  Adapter's tracked-object/human-state events (see
#  `luno/adapters/real_vision.py`).
# ─────────────────────────────────────────────

_yolo_pose_model = None

# Best-effort COCO class name -> this project's own object vocabulary
# (see `luno/vision_memory/utils.py`'s `DEFAULT_OBJECT_LABELS`) - purely
# cosmetic renaming so events/descriptions read the way the rest of this
# codebase already talks about objects ("phone" not "cell phone", "table"
# not "dining table"). Anything not in this map is passed through with
# its original COCO name unchanged (never dropped) - COCO's 80 classes
# don't include "face", "hand", "door", or "window" at all (no bounding-
# box detector trained on those ships with plain YOLOv8/11); that is an
# honest limitation of a lightweight COCO-based detector, not something
# faked here - see this module's own docstring "CATATAN JUJUR" precedent.
_COCO_LABEL_ALIASES = {
    "cell phone": "phone",
    "dining table": "table",
    "tv": "television",
    "potted plant": "plant",
}


def _normalize_label(coco_name):
    return _COCO_LABEL_ALIASES.get(coco_name, coco_name)


def _device_arg():
    """Ultralytics device kwarg: "0" (first CUDA GPU) if USE_GPU and CUDA
    is actually available, else "cpu" - never crashes if CUDA isn't
    present even when USE_GPU=true (honest degrade, logged once by the
    caller if it wants), matching this whole file's "never let YOLO
    failures become fatal" convention."""
    if not config.USE_GPU:
        return "cpu"
    try:
        import torch
        return "0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _get_yolo_tracking():
    """RAM fix: this used to lazy-load a SECOND, fully independent
    `YOLO(config.YOLO_MODEL_PATH)` instance, separate from `_get_yolo()`
    above, even though both always point at the exact same model file
    (there is only one `YOLO_MODEL_PATH` setting - nothing in this
    codebase ever gives the tracked-cycle loop a different path). With
    `VISION_BACKEND=real`, BOTH `start_watch()` (uses `_get_yolo()`) and
    the Sprint 8 tracked-cycle loop (used `_get_yolo_tracking()`) run
    continuously in the background, so that meant two full PyTorch/
    Ultralytics model instances resident in RAM at once for zero benefit
    - a real, measurable, always-on waste, not a one-off spike.

    Now simply delegates to `_get_yolo()` so both loops share ONE loaded
    model (same file, same weights - `conf=`/`device=` are still passed
    per-call by each caller, so behavior is unchanged, only the number of
    resident model copies drops from 2 to 1). If `_yolo_tracking_model`
    genuinely needs to point at a DIFFERENT file than `_get_yolo()` in the
    future, reintroduce a separate cache keyed off a new dedicated config
    value at that point - don't restore the blind duplication."""
    return _get_yolo()


def _get_yolo_pose():
    global _yolo_pose_model
    with _yolo_lock:
        if _yolo_pose_model is None:
            from ultralytics import YOLO
            _yolo_pose_model = YOLO(config.YOLO_POSE_MODEL_PATH)
        return _yolo_pose_model


def detect_objects_tracked(frame=None):
    """Structured detections for THIS frame - `List[RawDetection]`
    (label, confidence, bbox), confidence-filtered by
    `config.CONFIDENCE_THRESHOLD` and capped at `config.MAX_OBJECTS`
    (highest-confidence kept if more than that many things are in frame -
    an honest "too many objects" degrade rather than an unbounded list).
    Returns `[]` (never raises) on any camera/model failure - same
    "non-fatal, just means nothing to report this cycle" contract as
    `detect_objects()` above."""
    global _last_tracked_detection_error, _debug_last_person_state
    try:
        if frame is None:
            frame = _capture_frame()
        if frame is None:
            # Honest distinction (P0.6.2-FIX Section 13): no frame to
            # even try on (camera not open/no image this cycle) is NOT
            # a detector failure - clear any stale error from a
            # previous cycle so a transient camera hiccup doesn't keep
            # reporting a now-irrelevant old detector error forever.
            _last_tracked_detection_error = None
            return []
        model = _get_yolo_tracking()
        # P0.8.4 fix - see the P0.8.4 note near `_yolo_lock`'s declaration.
        with _yolo_lock:
            results = model(frame, verbose=False, conf=config.CONFIDENCE_THRESHOLD, device=_device_arg())
        detections = []
        raw_box_count = 0
        for r in results:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            # Use the SAME `.tolist()` conversion already required for
            # iteration below rather than a bare `len(boxes.cls)` - a
            # real ultralytics `Boxes.cls` is a torch Tensor (len()-able),
            # but test doubles (and possibly other backends) only
            # implement `.tolist()`, not `__len__`. Counting off the
            # already-materialized list keeps this diagnostic-only code
            # from imposing a NEW requirement on `boxes.cls` beyond what
            # the existing parsing loop already depends on.
            cls_list = boxes.cls.tolist()
            raw_box_count += len(cls_list)
            for box, conf, cls_id in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), cls_list):
                label = _normalize_label(model.names[int(cls_id)])
                detections.append(RawDetection(label=label, confidence=float(conf), bbox=tuple(box)))
        detections.sort(key=lambda d: d.confidence, reverse=True)

        # P0.8.5 - TEMPORARY diagnostic logging (see docs/change_impact/
        # camera_automation_p0_8_5.md Section 3): prints immediately after
        # raw YOLO results are parsed into `detections` above, so the
        # exact person_count this cycle's OWN detector run produced is
        # visible before it goes anywhere near the (separate, previously
        # under-suspicion) presence-watch loop / VisionCameraEventBridge /
        # the rules engine downstream. Never logs credentials or image/frame data -
        # only counts, confidences, and a plain True/False presence
        # state. `person_confidences` is capped to avoid an unbounded
        # line if many people are ever in frame at once.
        person_confidences = [round(d.confidence, 3) for d in detections if d.label == "person"]
        person_count_this_cycle = len(person_confidences)
        new_person_state = person_count_this_cycle > 0
        print(
            f"[VISION PERSON DEBUG] raw_boxes={raw_box_count} "
            f"person_boxes={person_count_this_cycle} "
            f"person_confidences={person_confidences[:10]} "
            f"person_count={person_count_this_cycle} "
            f"previous_person_state={_debug_last_person_state} "
            f"new_person_state={new_person_state}"
        )
        _debug_last_person_state = new_person_state

        # Model call + result parsing above completed without raising -
        # this cycle's detector genuinely ran (whether or not it found
        # anything), so any previously-recorded failure no longer
        # applies.
        _last_tracked_detection_error = None
        return detections[: config.MAX_OBJECTS] if config.MAX_OBJECTS > 0 else detections
    except Exception as ex:
        hint = _yolo_checkpoint_hint(ex)
        print(f"[Vision] ⚠ Tracked object detection gagal (non-fatal, dilewatin): {ex}{hint}")
        # P0.6.2-FIX Section 13 - record WHY this cycle produced an empty
        # list, without changing the `[]`/never-raises contract itself
        # (every existing caller of this function is unaffected). See
        # `last_tracked_detection_error()` below.
        _last_tracked_detection_error = f"{type(ex).__name__}: {ex}{hint}"
        return []


def last_tracked_detection_error() -> Optional[str]:
    """P0.6.2-FIX (Section 13) - `None` if the most recently completed
    `detect_objects_tracked()` cycle ran the model without raising
    (whether or not it found any objects - an empty scene is not a
    failure). Otherwise, a short, sanitized (no credentials/frame data,
    just the exception type/message and, when recognized, the existing
    `_yolo_checkpoint_hint()` diagnostic) description of why that cycle's
    detector call itself failed. This is the ONE additive hook a caller
    (`RealVisionSource`, this project's live observer script) can use to
    tell "no one is in frame right now" apart from "the detector is
    broken right now" - see `detect_objects_tracked()`'s own comments for
    why that distinction was not observable at all before this."""
    return _last_tracked_detection_error


def attach_pose_keypoints(frame, detections):
    """For every `label == "person"` entry in `detections`, run the
    (separately lazy-loaded) pose model ONCE on this same frame and
    attach its 17-point keypoint list - only ever called when the
    general detector already found at least one person, so a scene with
    no people never pays for a second model call. Matches people to pose
    detections by bounding-box IoU (both models see the same frame, so
    their person boxes should overlap closely); a person detection that
    the pose model didn't also find simply keeps `keypoints=None` (human
    state estimation then falls back to its own bbox-only heuristics -
    see `vision_human_state.py`), never a hard failure. Returns a NEW
    list (does not mutate the input)."""
    if frame is None or not any(d.label == "person" for d in detections):
        return detections
    try:
        model = _get_yolo_pose()
        results = model(frame, verbose=False, conf=config.CONFIDENCE_THRESHOLD, device=_device_arg())
    except Exception as ex:
        print(f"[Vision] ⚠ Pose estimation gagal (non-fatal, lanjut tanpa keypoints): {ex}{_yolo_checkpoint_hint(ex)}")
        return detections

    pose_boxes = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        keypoints_obj = getattr(r, "keypoints", None)
        if boxes is None or keypoints_obj is None:
            continue
        xyxy = boxes.xyxy.tolist()
        kps = keypoints_obj.data.tolist()  # [[ [x,y,conf], ... 17 pts ], ...]
        for box, kp in zip(xyxy, kps):
            pose_boxes.append((tuple(box), [tuple(point) for point in kp]))

    def _iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    out = []
    used_pose_idx = set()
    for det in detections:
        if det.label != "person":
            out.append(det)
            continue
        best_idx, best_score = None, 0.0
        for idx, (pose_box, _kp) in enumerate(pose_boxes):
            if idx in used_pose_idx:
                continue
            score = _iou(det.bbox, pose_box)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is not None and best_score >= 0.3:
            used_pose_idx.add(best_idx)
            out.append(RawDetection(label=det.label, confidence=det.confidence, bbox=det.bbox, keypoints=pose_boxes[best_idx][1]))
        else:
            out.append(det)
    return out


def _watch_loop():
    global _last_detections, _last_detection_at, _person_present
    print(
        f"[Vision] ✓ Camera watch (YOLO) jalan tiap {config.CAMERA_WATCH_INTERVAL_S}s "
        f"(device index {config.CAMERA_INDEX})\n"
    )
    while _watch_running:
        detections = detect_objects()
        _last_detections = detections
        _last_detection_at = time.time()
        _person_present = "person" in detections
        time.sleep(config.CAMERA_WATCH_INTERVAL_S)


def start_watch():
    """Jalanin YOLO di background tiap `CAMERA_WATCH_INTERVAL_S` detik (BUKAN
    tiap frame - nggak perlu presisi tinggi buat sekadar 'ada orang nggak di
    depan kamera'), nyimpen hasilnya ke `last_detections()`/`person_present()`
    di bawah. Idempotent. OPSIONAL - `ask_vision()` di bawah nggak butuh ini
    jalan, cuma fitur presence-detection tambahan buat dipakai nanti (mis.
    trigger auto-listen pas ada orang di depan kamera)."""
    global _watch_thread, _watch_running
    if _watch_running:
        return
    _watch_running = True
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
    _watch_thread.start()


def stop_watch():
    global _watch_running
    _watch_running = False


def last_detections():
    """(list_label, umur_detik) dari deteksi YOLO background terakhir, atau
    ([], None) kalau `start_watch()` belum pernah dipanggil/belum ada hasil."""
    if _last_detection_at == 0.0:
        return [], None
    return list(_last_detections), time.time() - _last_detection_at


def person_present():
    return _person_present


# ─────────────────────────────────────────────
#  Jendela monitor kamera (opsional, YOLO overlay) — buat NGELIAT langsung
#  apa yang kamera tangkep SAMBIL Luno tetap jalan normal, bukan bagian dari
#  pipeline lihat_kamera/watch di atas (dua-duanya jalan independen).
# ─────────────────────────────────────────────

def _monitor_loop():
    global _monitor_running
    window_name = "Luno - Camera Monitor"
    window_shown = False  # baru True abis cv2.imshow() beneran kepanggil sekali
    print(
        f"[Vision] ✓ Jendela monitor kamera kebuka ('{window_name}') — fokusin "
        "jendelanya lalu tekan 'q' buat nutup, atau panggil vision.stop_monitor()."
    )
    try:
        while _monitor_running:
            frame = _capture_frame()
            if frame is None:
                # Kamera lagi nggak bisa diakses (dipakai proses lain, GUI
                # nggak kesupport di sistem ini, dst) - jangan buru-buru
                # mati, coba lagi sebentar lagi.
                time.sleep(0.5)
                continue

            annotated = frame
            try:
                model = _get_yolo()
                # P0.8.4 fix - see the P0.8.4 note near `_yolo_lock`'s declaration.
                with _yolo_lock:
                    results = model(frame, verbose=False, conf=config.YOLO_CONFIDENCE, device=_device_arg())
                # `.plot()` dari ultralytics langsung gambar semua bounding
                # box + label + confidence di atas frame - nggak perlu
                # gambar manual pakai cv2.rectangle/putText satu-satu.
                annotated = results[0].plot()
            except Exception as ex:
                # YOLO gagal itu non-fatal buat jendela ini - tetep tampilin
                # frame mentah tanpa overlay daripada jendelanya ikut mati.
                print(f"[Vision] ⚠ Monitor: YOLO overlay gagal (nampilin frame mentah aja): {ex}")

            try:
                cv2.imshow(window_name, annotated)
                window_shown = True
                # cv2.waitKey WAJIB dipanggil tiap loop biar HighGUI sempet
                # ngerender window-nya (bukan cuma buat baca keyboard) -
                # tanpa ini jendelanya bakal freeze/nggak muncul sama sekali.
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
            except cv2.error as ex:
                # Build OpenCV tanpa dukungan GUI (headless) - nggak bisa
                # nampilin jendela sama sekali. Matiin loop ini dengan pesan
                # jelas daripada spam error tiap frame.
                print(
                    f"[Vision] ✗ Monitor: OpenCV build ini nggak dukung jendela GUI ({ex}). "
                    "Cek 'opencv-python' (BUKAN 'opencv-python-headless') yang keinstall."
                )
                break
    finally:
        if window_shown:
            try:
                cv2.destroyWindow(window_name)
            except cv2.error:
                pass  # udah ketutup duluan/GUI nggak ke-init - aman diabaikan
        _monitor_running = False


def start_monitor():
    """Buka jendela live-preview kamera (+ kotak deteksi YOLO di atasnya) di
    thread terpisah, jalan BARENGAN sama Luno (bukan gantiin apa pun - tool
    `lihat_kamera` dan `start_watch()` tetap jalan independen dari jendela
    ini). Idempotent.

    CATATAN JUJUR: `cv2.imshow` dari background thread biasanya jalan normal
    di Windows, tapi belum bisa aku test visual beneran dari sini (nggak ada
    monitor/GUI di lingkungan development) - kalau jendelanya nggak muncul
    sama sekali atau kerasa nge-freeze, kabarin, kemungkinan besar perlu
    dipindah jadi proses terpisah (skrip sendiri) alih-alih thread dalam
    proses Luno yang sama."""
    global _monitor_thread, _monitor_running
    if _monitor_running:
        return
    _monitor_running = True
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()


def stop_monitor():
    global _monitor_running
    _monitor_running = False


# ─────────────────────────────────────────────
#  Vision-language backend — yang BENERAN jawab pertanyaan soal gambar
#  (Gemini 2.0 Flash ATAU OpenAI gpt-4o-mini, lihat VISION_PROVIDER)
# ─────────────────────────────────────────────

#: `config.VISION_PROVIDER` value -> the `VisionProvider` class that
#: implements it. A future third provider only needs one more entry
#: here (plus the class itself in `luno/vision_provider.py`) - nothing
#: else in this module changes.
_VISION_PROVIDER_CLASSES = {
    "gemini": GeminiVisionProvider,
    "openai": OpenAIVisionProvider,
}


def _get_vision_provider():
    """Lazy singleton, same pattern as `_get_yolo()` above - constructed
    once (reads `VISION_PROVIDER` plus that provider's own config, e.g.
    `OPENAI_API_KEY`/`OPENAI_VISION_MODEL`/`OPENAI_VISION_TIMEOUT_S` or
    `GEMINI_API_KEY`/`GEMINI_VISION_MODEL`/`GEMINI_VISION_TIMEOUT_S`,
    from `luno.config` exactly once, at first use, not at import time),
    reused for every `ask_vision()` call after that. An unrecognized
    `VISION_PROVIDER` value falls back to OpenAI (this module's own
    default) rather than raising - a typo'd .env value should degrade
    to "vision still works, just via the default provider", not break
    the feature outright; the fallback is logged once so a genuine typo
    is still discoverable.

    A test can swap this out entirely via
    `set_vision_provider_for_testing()` below without touching real env
    vars or making real HTTP calls."""
    global _vision_provider
    with _vision_provider_lock:
        if _vision_provider is None:
            provider_name = (config.VISION_PROVIDER or "openai").strip().lower()
            provider_cls = _VISION_PROVIDER_CLASSES.get(provider_name)
            if provider_cls is None:
                print(
                    f"[Vision] ⚠ VISION_PROVIDER={provider_name!r} nggak dikenal "
                    f"(pilihan: {', '.join(_VISION_PROVIDER_CLASSES)}) - fallback ke 'openai'."
                )
                provider_cls = OpenAIVisionProvider
            _vision_provider = provider_cls()
        return _vision_provider


def set_vision_provider_for_testing(provider):
    """Test-only seam - installs `provider` (anything with a matching
    `analyze_image(image: bytes, prompt: str) -> str`) as the singleton
    `_get_vision_provider()` returns, or resets back to lazy-real
    construction when called with `None`. Mirrors the plain-function-
    reassignment monkeypatch style `tests/test_vision_sprint8.py`
    already uses elsewhere in this module - a dedicated setter here
    instead just makes the intent explicit and keeps the lock-protected
    global out of test code."""
    global _vision_provider
    with _vision_provider_lock:
        _vision_provider = provider


def _feed_vision_memory(description):
    """[Vision Memory integration] Setiap deskripsi Gemini yang BERHASIL —
    dari `ask_vision()` (on-demand, satu-satunya caller sekarang - lihat
    modul docstring soal ambient watch yang udah dihapus) — masuk sini
    lewat `_query_vision_provider()` di bawah.

    Fire-and-forget di thread terpisah: `vision_memory.update()` sendiri
    cepat (~20ms diukur), tapi ini sengaja TETAP nggak nunggu hasilnya
    (non-blocking) biar mutlak nggak pernah nge-lag loop vision/audio
    recognition/TTS/avatar Unity yang jalan di thread lain. Kalau gagal
    (mis. SQLite lagi kekunci), cuma di-log — Vision Memory itu lapisan
    memori tambahan, bukan dependency keras pipeline vision utama, jadi
    kegagalannya nggak boleh sampai nge-crash/berhentiin apa pun di atasnya."""

    def _run():
        try:
            vision_memory.update(description)
        except Exception as ex:
            print(f"[VisionMemory] ⚠ update gagal (non-fatal, dilewatin): {ex}")

    threading.Thread(target=_run, daemon=True).start()


#: Longest edge (px) a frame gets resized down to before upload, if
#: bigger - most webcams/IP cameras already shoot well above what a
#: vision-language model needs to read a room/object correctly, so this
#: trades invisible-in-practice detail for real, measurable wins on
#: upload bandwidth, Gemini's own per-request cost (billed partly by
#: image size/token count), and latency. `None`/`<= 0` disables resizing
#: entirely - kept as a plain module constant rather than a `config.py`
#: setting since there's no real reason a deployment would want to tune
#: this differently from the default.
_MAX_UPLOAD_EDGE_PX = 1024
_JPEG_QUALITY = 85


def _encode_frame_for_upload(frame):
    """BGR OpenCV frame -> JPEG bytes ready for `VisionProvider.
    analyze_image()`. Downscales first if the frame's longer edge
    exceeds `_MAX_UPLOAD_EDGE_PX` (aspect ratio preserved, `INTER_AREA` -
    the recommended OpenCV interpolation for shrinking) - see this
    module's own `_MAX_UPLOAD_EDGE_PX` docstring for why. Returns `None`
    (never raises) if the frame is empty or encoding fails - same
    "non-fatal, caller decides what that means" convention every other
    frame-touching function in this module already follows."""
    if frame is None:
        return None
    try:
        height, width = frame.shape[:2]
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None

    to_encode = frame
    longest_edge = max(width, height)
    if _MAX_UPLOAD_EDGE_PX and longest_edge > _MAX_UPLOAD_EDGE_PX:
        scale = _MAX_UPLOAD_EDGE_PX / float(longest_edge)
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        try:
            to_encode = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
        except Exception as ex:
            print(f"[Vision] ⚠ Resize gagal sebelum upload (lanjut pakai ukuran asli): {ex}")
            to_encode = frame

    ok, buf = cv2.imencode(".jpg", to_encode, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        return None
    # `buf` is an owned numpy array from imencode; `.tobytes()` copies
    # into a plain `bytes` object and `buf`/`to_encode` (if a fresh
    # resized copy) both go out of scope right after this returns -
    # nothing here is retained past one call, so frames never
    # accumulate (see module docstring's "Latest-frame handling"
    # discussion in the migration task this was written for).
    return buf.tobytes()


def _query_vision_provider(prompt, frame, timeout=None):
    """Aug 2026 migration: replaces the old `_query_minicpm()` (local
    MiniCPM-V via Ollama). Same call sites (`ask_vision()` - the ONLY
    caller now that the ambient watch loop is gone, see module
    docstring), same return contract: `{'description': str}` or
    `{'error': str}`, SEMUA pesan Bahasa Indonesia yang jelas - this
    function is the ONE place that translates `VisionProviderError`
    (see `luno/vision_provider.py`) into that long-standing dict shape,
    so `ask_vision()` itself never needs to know a provider abstraction
    exists at all.

    `timeout` overrides the provider's own configured
    `GEMINI_VISION_TIMEOUT_S` for this one call - kept as a parameter
    (unused by the current single caller, but harmless to keep) purely
    for interface stability, same reasoning `_query_minicpm`'s own
    `timeout` param had."""
    image_bytes = _encode_frame_for_upload(frame)
    if image_bytes is None:
        return {"error": "Gagal encode gambar dari kamera."}

    provider = _get_vision_provider()
    original_timeout = None
    if timeout is not None and hasattr(provider, "_timeout_s"):
        original_timeout = provider._timeout_s
        provider._timeout_s = timeout
    try:
        description = provider.analyze_image(image_bytes, prompt)
    except VisionProviderError as ex:
        return {"error": f"Vision (Gemini) gagal: {ex}"}
    except Exception as ex:
        # Never let an unexpected exception from a third-party
        # dependency (network stack quirk, etc.) escape this function -
        # `ask_vision()`'s whole reason to exist is that a vision
        # failure degrades to an honest error message, never a crash.
        return {"error": f"Vision error nggak terduga: {ex}"}
    finally:
        if original_timeout is not None:
            provider._timeout_s = original_timeout

    if not description or not description.strip():
        return {"error": "Gemini ngasih balasan kosong."}

    # [Vision Memory integration] Hasil vision yang beneran berhasil —
    # umpanin ke Vision Memory di sini (lihat _feed_vision_memory di
    # atas), TIDAK di caller (ask_vision) biar konsisten dengan
    # arsitektur lama.
    _feed_vision_memory(description)
    return {"description": description}


# [Vision Memory integration - Task 3] "Where is my X" questions that Vision
# Memory's own cached world state can already answer shouldn't need to wake
# up the camera + Gemini at all - that's the whole point of remembering
# things. Trigger words below cover the common EN/ID phrasings actually used
# in this codebase (Luno replies in whichever language the user used, see
# main.py build_system_prompt's language instruction), and the candidate
# object must be one of Vision Memory's own known labels
# (`vision_memory.utils.DEFAULT_OBJECT_LABELS` - the SAME vocabulary its
# heuristic parser tracks objects under, so a match here is guaranteed to be
# something `query_location()` could actually know about).
_WHERE_TRIGGERS = ("where is", "where's", "where are", "dimana", "di mana", "ada di mana")


def _extract_location_query(question):
    """Best-effort: does `question` look like a "where is my X" question
    Vision Memory might already have a cached answer for? Returns the
    matched object label, or None (which just means "ask Gemini as
    normal", not an error).

    HONEST LIMITATION: plain keyword/substring matching, same trade-off as
    `vision_memory.utils.parse_description_heuristic` - a "where" question
    phrased unusually, or about an object outside the fixed label list,
    simply falls through to the real camera pipeline below rather than
    guessing wrong."""
    q = (question or "").lower()
    if not any(trigger in q for trigger in _WHERE_TRIGGERS):
        return None
    for label in DEFAULT_OBJECT_LABELS:
        if label in q:
            return label
    return None


def ask_vision(question=""):
    """Ambil 1 frame kamera SEKARANG (paling up-to-date - lihat
    `_capture_frame()`'s own "buang frame basi dulu" handling), lampirin
    hint objek dari YOLO (kalau ada), lalu tanyain ke Gemini 2.0 Flash.
    Return dict {'description': str} kalau sukses, {'error': str} kalau
    gagal (kamera nggak ada, GEMINI_API_KEY belum di-set, network/
    timeout, dst) - SEMUA pesan error dalam Bahasa Indonesia yang jelas
    apa yang perlu dibenerin, karena ini bakal balik lagi ke GPT sebagai
    hasil tool call ATAU konteks vision-intent (GPT yang nyampein ke
    user, bukan kamu yang baca log). This is the ONLY caller of
    `_query_vision_provider()` - Gemini is called ONCE per `ask_vision()`
    invocation, never on a timer (see module docstring).

    [Vision Memory integration - Task 3] Kalau pertanyaannya keliatan kayak
    "di mana X" DAN X ada di world state yang lagi dipegang Vision Memory,
    jawab LANGSUNG dari situ - kamera/YOLO/Gemini nggak disentuh sama
    sekali buat kasus ini (lebih cepat, nggak buang kuota API buat
    sesuatu yang udah diketahui). Kalau memori nggak punya jawaban (objek
    nggak lagi ke-track, world state udah expired/ke-clear, dst - Vision
    Memory sendiri yang ngatur itu semua lewat short-term TTL-nya, nggak
    diduplikasi di sini), lanjut normal ke pipeline kamera di bawah."""
    question = (question or "").strip() or "Jelasin apa yang kamu lihat di gambar ini secara singkat dan natural."

    location_label = _extract_location_query(question)
    if location_label:
        try:
            cached_location = vision_memory.query_location(location_label)
        except Exception as ex:
            cached_location = None
            print(f"[VisionMemory] ⚠ query_location gagal (lanjut ke Gemini seperti biasa): {ex}")
        if cached_location:
            answer = f"The {location_label} is {cached_location}."
            print(f"[Vision] ✓ (dari Vision Memory, Gemini dilewatin) '{question[:60]}' → {answer}")
            return {"description": answer}
        # else: objek nggak diketahui/nggak lagi ke-track di memori - lanjut ke bawah, tanya kamera beneran.

    frame = _capture_frame()
    if frame is None:
        return {
            "error": (
                "Kamera nggak bisa diakses. Cek CAMERA_INDEX di .env (device index webcam), "
                "dan pastikan nggak ada app lain yang lagi pegang kamera."
            )
        }

    hint_objects = detect_objects(frame)
    prompt = question
    if hint_objects:
        prompt += f"\n\n(Deteksi objek cepat sebagai konteks tambahan, bisa aja nggak lengkap/salah: {', '.join(hint_objects)}.)"

    result = _query_vision_provider(prompt, frame)
    if "description" in result:
        print(f"[Vision] ✓ '{question[:60]}' → {result['description'][:80]}...")
    return result


# ─────────────────────────────────────────────
#  Ambient vision watch — REMOVED (Aug 2026 migration).
#
#  This used to run a vision-language model (MiniCPM-V) continuously in
#  the background - 1 frame + 1 call every CAMERA_VISION_WATCH_INTERVAL_S
#  seconds (default 1s), caching a short scene description via
#  `last_vision_description()` for main.py to splice into the system
#  prompt as ambient visual awareness, with zero tool call needed.
#
#  That's exactly the "send camera frames to the vision model on a timer,
#  whether or not anyone asked" pattern the Gemini migration was
#  explicitly required NOT to reintroduce (Gemini is on-demand only, see
#  `ask_vision()`) - a remote API call once a second, forever, for as
#  long as Luno runs, would be a real, ongoing cost/latency/bandwidth
#  liability a purely-local model never had. So this was removed
#  outright rather than repointed at Gemini.
#
#  The three functions below are kept as INERT NO-OPS (not deleted) so
#  nothing that already calls them - `luno/adapters/real_vision.py`'s
#  `CAMERA_VISION_WATCH_ENABLED` check, existing tests - needs to change.
#  `CAMERA_VISION_WATCH_ENABLED`/`CAMERA_VISION_WATCH_INTERVAL_S` in
#  config.py are correspondingly unused now (kept, same reasoning).
# ─────────────────────────────────────────────

def start_vision_watch():
    """No-op (see section docstring above). Idempotent, matches the old
    signature exactly - safe to call whether or not
    `CAMERA_VISION_WATCH_ENABLED` is set."""
    return


def stop_vision_watch():
    """No-op (see section docstring above)."""
    return


def last_vision_description():
    """Always `(None, None)` now - nothing ever populates an ambient
    description anymore (see section docstring above). Same "no result
    yet" contract callers already handled before this migration, so
    nothing downstream needs a special case for "this feature doesn't
    exist" vs. "this feature hasn't produced a result yet"."""
    return None, None


# ─────────────────────────────────────────────
#  Vision Memory context builder — ringkasan world_state/recent_events/
#  long_term_memory yang udah PENDEK (target ~500 kata total), dipakai
#  main.py buat nempel konteks visual persisten ke system prompt SEBELUM
#  pesan user (lihat main.py build_system_prompt()). Ini murni lapisan
#  FORMATTING di atas Vision Memory yang udah selesai/tertest - nggak ada
#  logic tracking/scoring/dsb di sini, itu semua tetap punya vision_memory/.
# ─────────────────────────────────────────────

def _trim_to_word_budget(lines, budget_words):
    """Ambil item dari `lines` (urutan tetap dipertahankan) selama total kata
    kumulatifnya belum lewat `budget_words`. Berhenti di item pertama yang
    bakal ngelewatin budget (BUKAN motong kata di tengah kalimat - lebih
    baik dapet kalimat utuh yang lebih dikit daripada kalimat kepotong)."""
    kept = []
    total = 0
    for line in lines:
        words_in_line = len(line.split())
        if kept and total + words_in_line > budget_words:
            break
        kept.append(line)
        total += words_in_line
    return kept, total


def _format_world_state_lines(state):
    """WorldState -> list kalimat pendek ("White cup on the desk.", "Room
    light is on.", dst) - satu kalimat per human/objek/kondisi ruangan yang
    lagi diketahui PRESENT sekarang."""
    lines = []

    for human in state.humans.values():
        name = human.identity or "Someone"
        bit = f"{name} is present"
        activity = human.activity.value.replace("_", " ") if human.activity else ""
        if activity and activity != "unknown":
            bit += f", currently {activity}"
        if human.emotion:
            bit += f", seems {human.emotion}"
        lines.append(bit + ".")

    for obj in state.objects.values():
        if obj.status.value != "present":
            continue
        desc = f"{obj.color} {obj.label}" if obj.color else obj.label
        if obj.location:
            desc += f" {obj.location}"
        lines.append(desc[:1].upper() + desc[1:] + ".")

    if state.room.light_on is not None:
        lines.append("Room light is on." if state.room.light_on else "Room light is off.")
    if state.room.door_closed is not None:
        lines.append("Door is closed." if state.room.door_closed else "Door is open.")

    return lines


def build_vision_context(max_words=500):
    """Vision Context Builder. Narik 3 API baca Vision Memory
    (`get_world_state()`/`get_recent_events()`/`get_long_term_memory()`),
    ubah jadi 3 blok teks PENDEK siap tempel ke system prompt - lihat
    main.py build_system_prompt() buat cara makenya. Total ketiga blok
    dijaga di sekitar `max_words` kata (dibagi ~50% world_state / 30%
    recent_events / 20% long_term_memory - "apa yang bener sekarang" paling
    konsisten kepake, habit paling jarang ada datanya).

    Aman dipanggil kapan pun, termasuk sebelum ada observasi apa pun (world
    state masih kosong) - balikin dict isinya string kosong, BUKAN error.
    Nggak pernah raise - kegagalan Vision Memory (mis. file DB lagi ke-lock)
    cukup bikin konteks visual kosong buat giliran ini, nggak boleh sampai
    gagalin build_system_prompt() punya main.py sama sekali."""
    empty = {"world_state": "", "recent_events": "", "long_term_memory": ""}
    if not is_configured():
        return empty

    try:
        state = vision_memory.get_world_state()
        events = vision_memory.get_recent_events()
        long_term = vision_memory.get_long_term_memory()
    except Exception as ex:
        print(f"[VisionMemory] ⚠ Gagal ambil context buat system prompt (non-fatal, dilewatin): {ex}")
        return empty

    world_lines, world_words = _trim_to_word_budget(_format_world_state_lines(state), int(max_words * 0.5))
    # get_recent_events() balikin urutan TERBARU dulu - trim di urutan itu
    # (biar yang KEPOTONG kalau kepanjangan adalah yang paling lama, bukan
    # yang paling baru), baru dibalik buat ditampilin kronologis.
    event_lines, event_words = _trim_to_word_budget([e.description for e in events], int(max_words * 0.3))
    habit_budget = max(0, max_words - world_words - event_words)
    habit_lines, _ = _trim_to_word_budget([r.statement for r in long_term], habit_budget)

    return {
        "world_state": " ".join(world_lines),
        "recent_events": " ".join(reversed(event_lines)),
        "long_term_memory": " ".join(habit_lines),
    }


# Tool schema buat OpenAI function calling — SAMA pola kayak web_search.py:
# GPT sendiri yang mutusin kapan user butuh Luno "melihat", bukan Luno yang
# nebak dari kata kunci.
VISION_TOOL = {
    "type": "function",
    "function": {
        "name": "lihat_kamera",
        "description": (
            "Look through the webcam and answer a question about what's currently visible - "
            "use this whenever the user asks Luno to look at, describe, identify, or read "
            "something in front of the camera (e.g. 'what am I holding', 'describe the room', "
            "'what's on this page', 'does this outfit look okay'). This actually analyzes a "
            "live camera frame - don't guess or answer from imagination instead of calling this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to look for or answer about the current camera view, phrased as a "
                        "clear question or instruction, e.g. 'what object is the user holding?' "
                        "or 'describe the room briefly'."
                    ),
                }
            },
            "required": ["question"],
        },
    },
}
