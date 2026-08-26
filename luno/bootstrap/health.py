"""
health.py
=========

`run_startup_health_checks(runtime, adapter_manager, launcher_config)` -
the spec's "Running health checks..." step, covering every component it
lists: Configuration, SQLite database, Microphone, Camera, OpenRouter,
GPT-SoVITS endpoint, Home Assistant, Unity, Vision Memory, Memory
Retrieval.

Ground rule (spec, verbatim): "If a component fails: Log the error.
Disable only that subsystem. Continue running whenever possible. Do NOT
terminate Runtime unless Core cannot start." Core (`Runtime`,
`EventBus`, `ModuleManager`, `Coordinator`) has no external dependency
at all, so nothing in this file can ever legitimately raise/abort - a
check either passes, or is downgraded to a warning, or (for a small set
of genuinely OPTIONAL peripherals - Vision, Unity, Home Assistant)
actually disables that one adapter via `adapter_manager.disable(name)`
so a real-but-unreachable backend doesn't sit there logging repeated
connection failures. OpenRouter/Fish Audio/Whisper are never
auto-disabled here even when their real backend looks unreachable -
they ARE the conversation loop; the existing mock-fallback (already
chosen at registration time in `bootstrap.adapters` if construction
itself failed) plus each adapter's own built-in self-restart-after-N-
failures (`BaseAdapter`, see `luno/adapters/base.py`) is the right
response to a merely-flaky-at-this-instant endpoint, not disabling the
whole subsystem.

Every check here is read-only / best-effort and has a short, explicit
timeout - a health check must never hang startup indefinitely waiting
on a dead network endpoint.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlparse

from luno.core.utils import log

from .launcher_config import BACKEND_REAL

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig

_SOCKET_TIMEOUT_S = 2.0


@dataclass
class HealthCheckResult:
    name: str
    ok: bool
    message: str = ""
    action: str = "none"  # "none" | "warned" | "disabled"


@dataclass
class HealthCheckReport:
    results: List[HealthCheckResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def core_ok(self) -> bool:
        """Core itself never fails a check in this file (see module
        docstring) - kept as an explicit property so callers never need
        to guess which result index "is Core"."""
        return True


def _tcp_reachable(host: str, port: int, timeout_s: float = _SOCKET_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _http_reachable(url: str, timeout_s: float = _SOCKET_TIMEOUT_S) -> bool:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _tcp_reachable(host, port, timeout_s)


def _check_configuration() -> HealthCheckResult:
    try:
        import luno.config as legacy_config
    except Exception as ex:
        return HealthCheckResult("Configuration", False, f"luno.config failed to import: {ex}", "warned")
    missing = []
    if not getattr(legacy_config, "OPENAI_API_KEY", None):
        missing.append("OPENAI_API_KEY")
    if not getattr(legacy_config, "HA_TOKEN", None):
        missing.append("HA_TOKEN")
    if missing:
        return HealthCheckResult("Configuration", False, f"not set: {', '.join(missing)} (some features will run in mock/degraded mode)", "warned")
    return HealthCheckResult("Configuration", True, "loaded")


def _check_sqlite() -> HealthCheckResult:
    try:
        from luno import vision_memory as vm
        state = vm.get_world_state()
        return HealthCheckResult("SQLite database", True, f"reachable ({len(state.objects)} object(s) tracked)")
    except Exception as ex:
        return HealthCheckResult("SQLite database", False, f"{ex}", "warned")


def _check_microphone(launcher_config: "LauncherConfig") -> HealthCheckResult:
    if launcher_config.whisper_backend != BACKEND_REAL:
        return HealthCheckResult("Microphone", True, "not required (Whisper backend=mock)")
    try:
        import luno.config as legacy_config
        import speech_recognition as sr

        # MIC_DEVICE_INDEX set explicitly (see luno/config.py's own
        # docstring on it, and `list_microphones.py` at the project
        # root) - validate THAT specific device instead of PyAudio's own
        # default-device lookup, which is exactly what fails with
        # "[Errno -9996] Invalid device info" on some Windows setups.
        configured_index = getattr(legacy_config, "MIC_DEVICE_INDEX", None)
        if configured_index is not None:
            names = sr.Microphone.list_microphone_names()
            if configured_index < 0 or configured_index >= len(names):
                return HealthCheckResult(
                    "Microphone", False,
                    f"MIC_DEVICE_INDEX={configured_index} is out of range (found {len(names)} device(s) - "
                    "run `python list_microphones.py` to see valid indices)",
                    "warned",
                )
            return HealthCheckResult("Microphone", True, f"using MIC_DEVICE_INDEX={configured_index} ('{names[configured_index]}')")

        names = sr.Microphone.list_microphone_names()
        if not names:
            return HealthCheckResult("Microphone", False, "no input devices found - falling back to mock Whisper source", "warned")
        return HealthCheckResult("Microphone", True, f"{len(names)} input device(s) found")
    except Exception as ex:
        return HealthCheckResult(
            "Microphone", False,
            f"speech_recognition unavailable: {ex} - if this is \"[Errno -9996] Invalid device info\", "
            "run `python list_microphones.py` and set MIC_DEVICE_INDEX in .env to a specific device",
            "warned",
        )


_CAMERA_OPEN_TIMEOUT_S = 5.0


def _check_camera(launcher_config: "LauncherConfig") -> HealthCheckResult:
    """Bug fix: `cv2.VideoCapture(index)` has NO built-in timeout - on
    Windows especially (MSMF/DirectShow backend device enumeration), it
    can block for a very long time or indefinitely if no camera responds,
    the device is claimed by another process, a USB webcam is asleep, or
    Windows' camera privacy toggle silently blocks access. That directly
    violated this module's own documented promise ("a health check must
    never hang startup indefinitely") - `python main.py` would print
    "Running health checks..." and then simply never continue, with
    nothing to indicate WHY (results are only printed after the full
    list finishes - see `run_startup_health_checks()`).

    Fix: run the actual camera open/probe on a background daemon thread
    and only wait up to `_CAMERA_OPEN_TIMEOUT_S` for it - if it doesn't
    finish in time, report a warning and move on (matches the "warned"/
    degrade-don't-block behavior every other check here already uses for
    an unreachable peripheral). The daemon thread itself may keep
    running/holding the device in the background if the OS call truly
    never returns, but the thing it was blocking - Runtime startup - is
    no longer stuck on it, which is the actual promise this module
    makes.

    Sprint 69 (Camera Device / OpenCV Stability Fix): the actual probe
    now goes through `luno.vision.probe_camera()` instead of a separate,
    ad hoc `cv2.VideoCapture(legacy_config.CAMERA_INDEX)` call. That
    separate call had two real problems beyond the timeout above: it
    read `CAMERA_INDEX` directly instead of `vision.camera_source()`
    (silently ignoring `CAMERA_URL` if one was configured - the module
    docstring's own "camera_source() is the ONE place" rule was already
    being violated here), and it opened the device with NO explicit
    backend and NO shared lock, meaning it could run fully concurrently
    with a real `capture_frame()` call from `RealVisionSource`'s own
    poll loops trying to open the SAME device at the SAME time - the
    Sprint 69 brief's own item 9 requirement. `probe_camera()` fixes
    both: it reads the real configured source, is guarded by the same
    `_camera_lock` every other camera operation uses, and gets the same
    bounded, backend-candidate-aware open Sprint 69 added to
    `_capture_frame()` (see that function's own docstring for the
    evidence behind explicit local-camera backend selection). This
    thread+join wrapper is kept as a defensive outer bound - `probe_
    camera()` is already internally bounded, but this stays as the
    last-resort guarantee startup can never hang no matter what."""
    if launcher_config.vision_backend != BACKEND_REAL:
        return HealthCheckResult("Camera", True, "not required (Vision backend=mock)")
    try:
        import luno.config as legacy_config
        if not legacy_config.CAMERA_VISION_ENABLED:
            return HealthCheckResult("Camera", False, "CAMERA_VISION_ENABLED is false", "warned")

        import threading

        import luno.vision as vision_module

        box: Dict[str, Any] = {}

        def _run_probe() -> None:
            try:
                ok, state, reason = vision_module.probe_camera()
                box["ok"] = ok
                box["state"] = state.value
                box["reason"] = reason
            except Exception as ex:  # pragma: no cover - defensive, mirrors outer except
                box["error"] = str(ex)

        thread = threading.Thread(target=_run_probe, daemon=True, name="luno-camera-healthcheck")
        thread.start()
        thread.join(timeout=_CAMERA_OPEN_TIMEOUT_S)

        if thread.is_alive():
            return HealthCheckResult(
                "Camera", False,
                f"device did not respond within {_CAMERA_OPEN_TIMEOUT_S}s "
                "(camera driver may be stuck/claimed by another app) - continuing without blocking startup",
                "warned",
            )
        if "error" in box:
            return HealthCheckResult("Camera", False, box["error"], "warned")
        if not box.get("ok"):
            reason = box.get("reason") or "could not be opened"
            return HealthCheckResult("Camera", False, f"camera ({box.get('state')}): {reason}", "warned")
        return HealthCheckResult("Camera", True, f"camera opened ({box.get('state')})")
    except Exception as ex:
        return HealthCheckResult("Camera", False, f"{ex}", "warned")


def _check_openrouter() -> HealthCheckResult:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return HealthCheckResult("OpenRouter", True, "no API key set - running in mock mode")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if _http_reachable(base_url):
        return HealthCheckResult("OpenRouter", True, f"{base_url} reachable")
    return HealthCheckResult("OpenRouter", False, f"{base_url} not reachable - requests will fail until connectivity returns", "warned")


def _check_gpt_sovits() -> HealthCheckResult:
    backend = (os.getenv("FISH_AUDIO_BACKEND", "mock") or "mock").strip().lower()
    engine = (os.getenv("TTS_ENGINE", "") or "").strip().lower()
    if not engine:
        engine = "fish_audio_api" if backend == "fish_audio_api" else "gptsovits"

    if backend not in ("real", "gptsovits", "f5tts", "fish_audio_api") and engine != "fish_audio_api":
        return HealthCheckResult("GPT-SoVITS endpoint", True, "not required (Fish Audio backend=mock)")

    if engine == "fish_audio_api":
        # Cloud API - config-presence check (mirrors `_check_openrouter()`'s
        # own shape exactly) rather than a local host reachability probe.
        # Missing key is NOT a failure here - `_default_fish_audio_client()`
        # already degrades to mock cleanly in that case (spec requirement:
        # never crash on missing config), so this is reported the same
        # honest, non-alarming way `_check_openrouter()` reports "no API
        # key set - running in mock mode".
        api_key = os.getenv("FISH_AUDIO_API_KEY")
        if not api_key:
            return HealthCheckResult("Fish Audio API", True, "no API key set - falling back to mock TTS")
        base_url = (os.getenv("FISH_AUDIO_API_BASE_URL") or "").strip() or "https://api.fish.audio"
        if _http_reachable(base_url):
            return HealthCheckResult("Fish Audio API", True, f"{base_url} reachable")
        return HealthCheckResult("Fish Audio API", False, f"{base_url} not reachable - TTS requests will fail until connectivity returns (text responses unaffected)", "warned")

    host = os.getenv("F5TTS_HOST") if engine == "f5tts" else os.getenv("GPTSOVITS_HOST")
    if not host:
        return HealthCheckResult("GPT-SoVITS endpoint", False, f"{'F5TTS_HOST' if engine == 'f5tts' else 'GPTSOVITS_HOST'} not set", "warned")
    if _http_reachable(host):
        return HealthCheckResult("GPT-SoVITS endpoint", True, f"{host} reachable")
    return HealthCheckResult("GPT-SoVITS endpoint", False, f"{host} not reachable - TTS requests will fail until it comes up", "warned")


def _check_home_assistant(launcher_config: "LauncherConfig", adapter_manager: "AdapterManager") -> HealthCheckResult:
    try:
        import luno.config as legacy_config
        configured = bool(legacy_config.HA_TOKEN and legacy_config.HA_URL)
    except Exception:
        configured = False
    if launcher_config.home_assistant_backend != BACKEND_REAL:
        return HealthCheckResult("Home Assistant", True, f"mock backend (configured={configured})")
    if not configured:
        adapter_manager.disable("home_assistant")
        return HealthCheckResult("Home Assistant", False, "real backend requested but HA_URL/HA_TOKEN missing - disabled", "disabled")
    # Live reachability is verified by RealHomeAssistantSource's own
    # reconnect-with-backoff loop once the adapter starts (see
    # real_home_assistant.py) - a synchronous websocket handshake here
    # would duplicate that and block startup on a slow/dead endpoint.
    return HealthCheckResult("Home Assistant", True, f"real backend, configured (live connection verified after startup)")


def _check_unity(launcher_config: "LauncherConfig", adapter_manager: "AdapterManager") -> HealthCheckResult:
    unity_adapter = adapter_manager.registry.get("unity")
    if unity_adapter is None:
        return HealthCheckResult("Unity", False, "adapter not registered", "warned")
    try:
        reachable = bool(unity_adapter.client.ping())
    except Exception as ex:
        reachable = False
        log(f"unity ping() raised: {ex}", "bootstrap")
    if reachable:
        return HealthCheckResult("Unity", True, "ping ok")
    if launcher_config.unity_backend == BACKEND_REAL:
        adapter_manager.disable("unity")
        return HealthCheckResult("Unity", False, "endpoint unreachable - disabled", "disabled")
    return HealthCheckResult("Unity", True, "mock backend (ping always ok)")


def _check_vision_memory() -> HealthCheckResult:
    try:
        from luno import vision_memory as vm
        state = vm.get_world_state()
        return HealthCheckResult("Vision Memory", True, f"{len(state.objects)} object(s), {len(state.humans)} human(s) tracked")
    except Exception as ex:
        return HealthCheckResult("Vision Memory", False, f"{ex}", "warned")


def _check_memory_retrieval(runtime: "Runtime") -> HealthCheckResult:
    record = runtime.module_manager.all_modules().get("memory_retrieval")
    if record is None:
        return HealthCheckResult("Memory Retrieval", False, "module not registered", "warned")
    status = record.module.health()
    return HealthCheckResult("Memory Retrieval", status.healthy, status.message or "ok")


def run_startup_health_checks(
    runtime: "Runtime", adapter_manager: "AdapterManager", launcher_config: "LauncherConfig", *, verbose: bool = True,
) -> HealthCheckReport:
    """Bug fix: this used to build `checks = [_check_configuration(), ...]`
    as a single list LITERAL - Python evaluates every element of a list
    literal immediately, left to right, before the `for` loop below even
    starts. That meant if any one check hung (see `_check_camera()`'s own
    fix above for a real example), every check after it in the list never
    even got CALLED yet - and since results were only ever printed via
    `print_health_check_report()` AFTER this whole function returns, the
    console showed nothing at all except "Running health checks..." with
    no way to tell which check was actually stuck.

    Each check now runs one at a time in an explicit sequence, printed
    (when `verbose=True`, the default - `main.py`'s own real usage) and
    logged immediately as it completes - so a future hang in any single
    check is now visible on the console exactly where it stalls, instead
    of a silent, undiagnosable freeze."""
    report = HealthCheckReport()
    check_calls: List[Any] = [
        lambda: _check_configuration(),
        lambda: _check_sqlite(),
        lambda: _check_microphone(launcher_config),
        lambda: _check_camera(launcher_config),
        lambda: _check_openrouter(),
        lambda: _check_gpt_sovits(),
        lambda: _check_home_assistant(launcher_config, adapter_manager),
        lambda: _check_unity(launcher_config, adapter_manager),
        lambda: _check_vision_memory(),
        lambda: _check_memory_retrieval(runtime),
    ]
    for check_call in check_calls:
        result = check_call()
        report.results.append(result)
        if verbose:
            _print_result(result)
        if not result.ok:
            log(f"health check '{result.name}' failed: {result.message} (action={result.action})", "bootstrap")
    return report


def _print_result(result: HealthCheckResult) -> None:
    mark = "✓" if result.ok else "✗"
    suffix = f" - {result.message}" if result.message else ""
    print(f"{mark} {result.name}{suffix}")


def print_health_check_report(report: HealthCheckReport) -> None:
    """Kept for any caller that already has a computed `HealthCheckReport`
    and wants to (re-)print it in one go (e.g. a future summary view).
    `run_startup_health_checks(..., verbose=True)` - the default, and
    what `main.py` actually uses - already prints each result live as it
    completes (see that function's own docstring for why that changed),
    so `main.py` no longer calls this a second time."""
    for result in report.results:
        _print_result(result)
