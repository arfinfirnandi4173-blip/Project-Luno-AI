"""
adapters.py
===========

`register_all_adapters(runtime, launcher_config)` - the ONLY place
`main.py` needs to call for "Registering adapters...". Builds ONE
`AdapterManager` bound to the Runtime already built by `bootstrap.modules`,
and registers all seven adapters the spec lists (Whisper, OpenRouter,
Fish Audio/GPT-SoVITS, Vision, Unity, Home Assistant, Scheduler) with a
single, uniform pattern per adapter:

    adapter_manager.register(SomeAdapter(client=_pick_client(...)), AdapterConfig(name="..."))

"Future adapters should only require: `adapter_manager.register(...)`.
Nothing else." - true here exactly as it already was in
`luno.adapters.manager`'s own design; this file doesn't add any new
mechanism, it just calls that one existing method seven times.

Real-vs-mock selection is uniform and config-driven for every adapter
(see `luno/bootstrap/launcher_config.py`): OpenRouter/Fish Audio keep
their own PRE-EXISTING switches (`OPENROUTER_API_KEY` presence /
`FISH_AUDIO_BACKEND`, already shipped and tested before Sprint 6);
Whisper/Vision/Unity/Home Assistant gain the identical pattern via the
new `WHISPER_BACKEND`/`VISION_BACKEND`/`UNITY_BACKEND`/
`HOME_ASSISTANT_BACKEND` env vars, defaulting to `"mock"` (zero external
dependency, zero behavior change for anyone who hasn't opted in).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from luno.adapters.home_assistant import HomeAssistantAdapter
from luno.adapters.models import AdapterConfig, DEFAULT_ADAPTER_EVENT_MAPPING, EventMapping
from luno.adapters.llm_manager import LLMManagerAdapter
from luno.adapters.fish_audio import FishAudioAdapter, MockFishAudioClient
from luno.adapters.manager import AdapterManager
from luno.adapters.scheduler import SchedulerAdapter
from luno.adapters.unity import UnityAdapter
from luno.adapters.vision import VisionAdapter
from luno.adapters.whisper import WhisperAdapter
from luno.core.utils import log

from .launcher_config import BACKEND_REAL

if TYPE_CHECKING:
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig


def _default_fish_audio_client(audio_capture_store: Any = None) -> Any:
    """`audio_capture_store`, when given (real backend only - see
    `luno/dashboard/audio_bridge.py`), taps `RealFishAudioClient`'s
    already-injectable `play_audio_fn` so the Dashboard's Chat panel can
    play Luno's actual synthesized voice in the browser - server-side
    playback itself is completely unchanged (see that module's own
    docstring for exactly why this is safe/additive).

    Fish Audio CLOUD API (`TTS_ENGINE=fish_audio_api` or
    `FISH_AUDIO_BACKEND=fish_audio_api`) addition: TTS is explicitly an
    OPTIONAL subsystem (spec requirement) - `FISH_AUDIO_API_KEY` missing
    must never crash Luno or half-configure a real client that would
    just fail on every single utterance forever. Validated ONCE, right
    here at startup (not per-utterance): if the cloud engine is
    requested but no key is set, this logs a single clear warning and
    falls back to `MockFishAudioClient` - Luno starts and runs exactly
    as if TTS had never been configured at all. gptsovits/f5tts are
    completely unaffected by any of this - same behavior as before this
    engine existed."""
    backend = (os.getenv("FISH_AUDIO_BACKEND", "mock") or "mock").strip().lower()
    from luno.adapters.fish_audio_real import RealFishAudioConfig
    cfg = RealFishAudioConfig.from_env()
    wants_real = backend in ("real", "gptsovits", "f5tts", "fish_audio_api") or cfg.engine == "fish_audio_api"
    if not wants_real:
        return MockFishAudioClient(playback_delay_s=0.05)

    if cfg.engine == "fish_audio_api" and not cfg.fish_audio_api_key:
        log(
            "Fish Audio API engine requested (TTS_ENGINE/FISH_AUDIO_BACKEND=fish_audio_api) but "
            "FISH_AUDIO_API_KEY is not set - TTS disabled, falling back to mock (text responses "
            "are completely unaffected)",
            "bootstrap",
        )
        return MockFishAudioClient(playback_delay_s=0.05)

    from luno.adapters.fish_audio_real import FishAudioApiCircuitBreaker, RealFishAudioClient, _default_play_audio
    play_audio_fn = None
    if audio_capture_store is not None:
        from luno.dashboard.audio_bridge import wrap_play_audio_fn
        play_audio_fn = wrap_play_audio_fn(audio_capture_store, _default_play_audio)
    synthesize_fn = None
    if cfg.engine == "fish_audio_api":
        breaker = FishAudioApiCircuitBreaker(failure_threshold=cfg.failure_threshold, cooldown_s=cfg.cooldown_s)
        synthesize_fn = breaker.call
    return RealFishAudioClient(cfg, synthesize_fn=synthesize_fn, play_audio_fn=play_audio_fn)


def register_all_adapters(runtime: "Runtime", launcher_config: "LauncherConfig") -> Dict[str, Any]:
    # Same LOCAL override `RuntimeDemoConsole` already applies: Fish
    # Audio must only ever trigger off "speak_request" (the normalized,
    # session/barge-in-aware text), never the raw "assistant_response" -
    # routing both would make Fish Audio speak every reply twice. This is
    # a per-deployment customization of the mapping (exactly what
    # `EventMapping`/`DEFAULT_ADAPTER_EVENT_MAPPING`'s own docstring says
    # it's FOR - "avoid hardcoded routing"), not a change to the
    # package's own default.
    adapter_mapping = dict(DEFAULT_ADAPTER_EVENT_MAPPING)
    adapter_mapping.pop("assistant_response", None)

    adapter_manager = AdapterManager(
        runtime.module_manager, runtime.coordinator, runtime.event_bus,
        lifecycle=runtime.lifecycle, health_monitor=runtime.health_monitor,
        event_mapping=EventMapping.from_dict(adapter_mapping),
    )

    # -- LLM Manager (Multi-LLM Provider System sprint) ----------------------
    # `LLMManagerAdapter` replaces the single-provider `OpenRouterAdapter`
    # here (that class itself, and every one of its own tests, is
    # untouched - see `luno/adapters/llm_manager.py`'s own docstring for
    # why both continue to exist, and why this still registers under the
    # module id "openrouter"). `LLM_PROVIDER` (default "openrouter")
    # selects which of the five providers is active; every other
    # configured one stands by for automatic fallback - see that module.
    #
    # Fallback default model when OPENROUTER_MODEL isn't set in .env -
    # only seeds the env var if it's genuinely unset (never overwrites an
    # explicit one), same "never overwrite a real env var" rule
    # `LauncherConfig`'s own env-seeding already follows. Kept as a LIVE,
    # current model rather than pinned once and forgotten - OpenRouter
    # (and upstream providers) sunset old model slugs over time, and a
    # hardcoded fallback pointing at a retired model fails with "No
    # endpoints found for <model>" at request time, not at startup. If
    # this ever happens again: set OPENROUTER_MODEL explicitly in .env
    # (see https://openrouter.ai/models for the current catalog).
    os.environ.setdefault("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    openrouter_adapter = LLMManagerAdapter()
    adapter_manager.register(openrouter_adapter, AdapterConfig(name="openrouter"))

    # -- Fish Audio / GPT-SoVITS (TTS) ---------------------------------------
    # `audio_capture_store`/`audio_correlator` back the Dashboard's Chat
    # panel voice output (Sprint 7 follow-up) - see
    # `luno/dashboard/audio_bridge.py`'s own docstring. Harmless when
    # unused (mock backend, or the dashboard disabled): the store simply
    # never receives anything to capture.
    from luno.dashboard.audio_bridge import AudioCaptureStore, AudioRequestCorrelator
    audio_capture_store = AudioCaptureStore()
    audio_correlator = AudioRequestCorrelator(audio_capture_store, runtime.event_bus)

    fish_audio_adapter = FishAudioAdapter(client=_default_fish_audio_client(audio_capture_store))
    adapter_manager.register(fish_audio_adapter, AdapterConfig(name="fish_audio"))

    # -- Whisper (STT) --------------------------------------------------------
    whisper_source = None
    if launcher_config.whisper_backend == BACKEND_REAL:
        try:
            from luno.adapters.real_whisper import RealWhisperSource
            whisper_source = RealWhisperSource()
        except Exception as ex:
            log(f"real Whisper backend requested but unavailable ({ex}) - falling back to mock", "bootstrap")
    whisper_adapter = WhisperAdapter(source=whisper_source)
    adapter_manager.register(whisper_adapter, AdapterConfig(name="whisper"))

    # -- Vision -----------------------------------------------------------------
    vision_source = None
    if launcher_config.vision_backend == BACKEND_REAL:
        try:
            from luno.adapters.real_vision import RealVisionSource
            vision_source = RealVisionSource()
        except Exception as ex:
            log(f"real Vision backend requested but unavailable ({ex}) - falling back to mock", "bootstrap")
    vision_adapter = VisionAdapter(source=vision_source)
    adapter_manager.register(vision_adapter, AdapterConfig(name="vision"))

    # -- Unity/VNyan --------------------------------------------------------------
    unity_client = None
    if launcher_config.unity_backend == BACKEND_REAL:
        try:
            from luno.adapters.real_unity import RealUnityClient
            unity_client = RealUnityClient()
        except Exception as ex:
            log(f"real Unity backend requested but unavailable ({ex}) - falling back to mock", "bootstrap")
    unity_adapter = UnityAdapter(client=unity_client)
    adapter_manager.register(unity_adapter, AdapterConfig(name="unity"))

    # -- Home Assistant -----------------------------------------------------------
    ha_source = None
    ha_client = None
    if launcher_config.home_assistant_backend == BACKEND_REAL:
        try:
            from luno.adapters.real_home_assistant import RealHomeAssistantClient, RealHomeAssistantSource
            ha_source = RealHomeAssistantSource()
            ha_client = RealHomeAssistantClient(ha_source)
        except Exception as ex:
            log(f"real Home Assistant backend requested but unavailable ({ex}) - falling back to mock", "bootstrap")
            ha_source = None
            ha_client = None
    home_assistant_adapter = HomeAssistantAdapter(source=ha_source, client=ha_client)
    adapter_manager.register(home_assistant_adapter, AdapterConfig(name="home_assistant"))

    # -- Scheduler (bridges Core's own Scheduler ticks to events) -----------------
    scheduler_adapter = SchedulerAdapter(core_scheduler=runtime.scheduler)
    adapter_manager.register(scheduler_adapter, AdapterConfig(name="scheduler_adapter"))

    return {
        "adapter_manager": adapter_manager,
        "openrouter_adapter": openrouter_adapter,
        "fish_audio_adapter": fish_audio_adapter,
        "whisper_adapter": whisper_adapter,
        "vision_adapter": vision_adapter,
        "unity_adapter": unity_adapter,
        "home_assistant_adapter": home_assistant_adapter,
        "scheduler_adapter": scheduler_adapter,
        "audio_capture_store": audio_capture_store,
        "audio_correlator": audio_correlator,
    }


def register_real_tool_handlers(modules: Dict[str, Any], adapters: Dict[str, Any], launcher_config: "LauncherConfig") -> None:
    """Called once from `main.py`, AFTER both `register_all_modules()`
    and `register_all_adapters()` have run (ordering matters: the real
    Home Assistant connection - `home_assistant_adapter.client` - is
    only built inside `register_all_adapters()`, and `ToolManagerBridgeModule`
    - inside `modules["tool_manager_module"]` - is only built inside
    `register_all_modules()`, which currently runs first; this function
    is deliberately its own small step rather than a parameter threaded
    through either one, so neither has to know about the other).

    BUG this closes (originally just for Home Assistant, now also for
    Windows/app-launching): a backend env var like `HOME_ASSISTANT_BACKEND`
    or `WINDOWS_BACKEND` only ever affected the ADAPTER layer (inbound
    state-listening for HA; nothing at all for Windows, which has no
    adapter). Every command a user actually SPEAKS goes through a
    completely different path (Planner -> `ToolRequested` ->
    `ToolManagerBridgeModule` -> `luno.tool_manager`'s OWN registry),
    which `luno/tool_manager/builtin/__init__.py::register_all()`
    unconditionally fills with mocks - so commands were reporting success
    without ever reaching real hardware/apps, regardless of these env
    vars. Fixes it by registering the matching real handler as an
    override, exactly the extension point `builtin/__init__.py`'s own
    docstring describes. Each section below no-ops independently (leaves
    that one tool's mock in place) whenever its backend isn't "real", or
    the real thing it depends on isn't available - never raises, never
    blocks startup."""
    _register_real_home_assistant_handler(modules, adapters, launcher_config)
    _register_real_windows_handler(modules, launcher_config)
    _register_real_camera_ptz_handler(modules, launcher_config)
    _register_real_browser_handler(modules, launcher_config)


_VERIFICATION_STAGE_TO_EVENT_NAME = {
    "started": "ActionVerificationStarted",
    "retry": "ActionVerificationRetry",
    "verified": "ActionVerified",
    "failed": "ActionVerificationFailed",
    "timeout": "ActionVerificationTimeout",
    # Sprint 52 - Robust HA Command & Entity Resolution: same
    # `on_verification_event(stage, payload)` hook, one new stage. See
    # `RealHomeAssistantHandler._emit_resolution()`'s own docstring for
    # exactly when "resolution" fires (fuzzy/ambiguous only).
    "resolution": "EntityResolutionDecision",
}


def _make_verification_event_publisher(tool_manager_module: Any):
    """Verified Smart Home Execution sprint - the ONE place that turns
    `RealHomeAssistantHandler`'s optional `on_verification_event(stage,
    payload)` hook into real Event Bus publishes. Closes over
    `tool_manager_module` (not a raw `event_bus` reference) and re-reads
    `tool_manager_module._event_bus` on every call, same defensive habit
    `ToolManagerBridgeModule.on_event` itself already follows (`bind_event_bus`
    runs once, right after construction, in `register_all_modules` - by
    the time this is ever called that's long done, but there is no
    reason to assume otherwise here). Never raises: a bad event/serialization
    problem must never be able to break the actual verified execution
    path this hook rides on top of - see `RealHomeAssistantHandler._emit`'s
    own matching try/except on the other side of this callback."""
    from luno.adapters import events as adapter_events

    def _publish(stage: str, payload: Dict[str, Any]) -> None:
        event_bus = getattr(tool_manager_module, "_event_bus", None)
        if event_bus is None:
            return
        event_cls_name = _VERIFICATION_STAGE_TO_EVENT_NAME.get(stage)
        if event_cls_name is None:
            return
        event_cls = getattr(adapter_events, event_cls_name)
        try:
            event_bus.publish(event_cls(data=payload))
        except Exception as ex:
            log(f"failed to publish {event_cls_name}: {ex}", "bootstrap")

    return _publish


def _register_real_home_assistant_handler(modules: Dict[str, Any], adapters: Dict[str, Any], launcher_config: "LauncherConfig") -> None:
    if launcher_config.home_assistant_backend != BACKEND_REAL:
        return

    tool_manager_module = modules.get("tool_manager_module")
    ha_adapter = adapters.get("home_assistant_adapter")
    if tool_manager_module is None or ha_adapter is None:
        return

    from luno.adapters.home_assistant import MockHomeAssistantClient
    ha_client = getattr(ha_adapter, "client", None)
    if ha_client is None or isinstance(ha_client, MockHomeAssistantClient):
        log("HOME_ASSISTANT_BACKEND=real requested but the adapter fell back to mock "
            "(HA unreachable at startup?) - smart home tool calls stay mocked for now", "bootstrap")
        return

    try:
        from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler
        on_verification_event = _make_verification_event_publisher(tool_manager_module)
        tool_manager_module.manager.registry.register(
            "home_assistant", RealHomeAssistantHandler(ha_client, on_verification_event=on_verification_event),
        )
        log("Real Home Assistant tool handler registered - smart home commands now reach the real HA instance", "bootstrap")
    except Exception as ex:
        log(f"failed to register real Home Assistant tool handler (staying mocked): {ex}", "bootstrap")

    # World Model Sprint Bagian 2 - one-time startup sync. Deliberately
    # here (not inside the try/except above): a World Model sync
    # failure must never be able to prevent/undo the real tool handler
    # registration that already succeeded above it.
    planner_module = modules.get("planner_module")
    world_model = getattr(planner_module, "world_model", None)
    get_all_states = getattr(ha_client, "get_all_states", None)
    if world_model is not None and callable(get_all_states):
        try:
            count = world_model.sync_from_states(get_all_states())
            log(f"World Model startup sync: {count} entities loaded from Home Assistant", "bootstrap")
        except Exception as ex:
            log(f"World Model startup sync failed (state will catch up via state_changed instead): {ex}", "bootstrap")


def _register_real_windows_handler(modules: Dict[str, Any], launcher_config: "LauncherConfig") -> None:
    """Unlike Home Assistant, there's no adapter/network connection to
    check readiness of here - `luno.desktop_control.open_app()` is a
    plain local, allowlist-only (`config/apps.json`) function call, so
    `WINDOWS_BACKEND=real` is the only gate."""
    if launcher_config.windows_backend != BACKEND_REAL:
        return

    tool_manager_module = modules.get("tool_manager_module")
    if tool_manager_module is None:
        return

    try:
        from luno.tool_manager.builtin.real_windows import RealWindowsHandler
        tool_manager_module.manager.registry.register("windows", RealWindowsHandler())
        log("Real Windows tool handler registered - 'open <app>' now launches real, "
            "allowlisted (config/apps.json) applications", "bootstrap")
    except Exception as ex:
        log(f"failed to register real Windows tool handler (staying mocked): {ex}", "bootstrap")


def _register_real_camera_ptz_handler(modules: Dict[str, Any], launcher_config: "LauncherConfig") -> None:
    """Pan/tilt IP camera control (e.g. TP-Link Tapo C212) via `pytapo`.
    Like Windows (and unlike Home Assistant), there's no long-lived
    adapter/connection to check readiness of - `pytapo.Tapo(...)` just
    holds credentials and makes one HTTP request per command, so
    `CAMERA_PTZ_BACKEND=real` plus `TAPO_HOST`/`TAPO_USERNAME`/
    `TAPO_PASSWORD` all being set is the only gate. `pytapo` itself is an
    optional dependency (`pip install pytapo`) - its absence, a bad host/
    credential, or any other construction failure all fall through to
    the same safe "stay mocked" outcome as every other real-handler
    override in this file, never blocking startup."""
    if launcher_config.camera_ptz_backend != BACKEND_REAL:
        return

    from luno import config as legacy_config
    if not (legacy_config.TAPO_HOST and legacy_config.TAPO_USERNAME and legacy_config.TAPO_PASSWORD):
        log("CAMERA_PTZ_BACKEND=real requested but TAPO_HOST/TAPO_USERNAME/TAPO_PASSWORD "
            "aren't all set - camera pan/tilt commands stay mocked for now", "bootstrap")
        return

    tool_manager_module = modules.get("tool_manager_module")
    if tool_manager_module is None:
        return

    try:
        from pytapo import Tapo
        from luno.tool_manager.builtin.real_camera_ptz import RealCameraPTZHandler
        tapo_client = Tapo(legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
        # Sprint 70 (Tapo C212 Live Auth & Auto-Recovery): also hand the
        # handler a way to rebuild ITS OWN client for a bounded, single
        # reconnect+retry on a recoverable failure (expired session,
        # transient network blip) - see `RealCameraPTZHandler._invoke()`'s
        # own docstring. This closure captures nothing new: the exact same
        # 3 values already read above, for the exact same one client this
        # function has always constructed - NOT a second connection
        # system, and NOT a new place credentials are stored (nothing is
        # written to disk; this is a plain in-memory callable, gone the
        # moment the process exits, same lifetime as `tapo_client` itself).
        client_factory = lambda: Tapo(legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)  # noqa: E731
        tool_manager_module.manager.registry.register(
            "camera_ptz", RealCameraPTZHandler(tapo_client, client_factory=client_factory)
        )
        log("Real camera pan/tilt tool handler registered - 'pan/tilt the camera' commands "
            "now reach the real Tapo camera", "bootstrap")
    except Exception as ex:
        # Tapo C212 Authentication sprint: `pytapo.Tapo(...)` performs REAL,
        # synchronous authentication at construction time (confirmed by
        # reading its source) - so a failure here almost always IS a
        # genuine connection/auth-classifiable event, not just "some
        # exception". Classify it (evidence-based, see
        # `real_camera_ptz.classify_tapo_exception`'s own module comment)
        # so this one easily-missed startup log line actually says WHY,
        # instead of a bare exception repr - without changing the
        # fall-back-to-mock control flow at all (that stays exactly as
        # every other real-handler override in this file behaves, and
        # stays covered by this file's own existing bootstrap tests).
        try:
            from luno.tool_manager.builtin.real_camera_ptz import classify_tapo_exception, _redact_credentials
            classified = classify_tapo_exception(ex)
            detail = _redact_credentials(str(ex))
            log(f"failed to register real camera pan/tilt tool handler (staying mocked) - "
                f"classified as {classified.category}: {detail}", "bootstrap")
        except Exception:
            # Classification itself must never be able to turn a handled,
            # already-safe fallback into a startup crash - fall back to
            # the original, unclassified log line if anything above goes
            # wrong (e.g. `real_camera_ptz` failing to import at all).
            log(f"failed to register real camera pan/tilt tool handler (staying mocked): {ex}", "bootstrap")


def _register_real_browser_handler(modules: Dict[str, Any], launcher_config: "LauncherConfig") -> None:
    """Browser/computer-use - gated on `BROWSER_ENABLED=true`
    (`launcher_config.browser_backend`, see `launcher_config.py`'s own
    comment on reusing that env var directly). Like camera_ptz, there's
    no long-lived adapter to wait on - `PlaywrightBrowserProvider`
    itself launches the real browser process lazily, on first actual
    use (see that class's own docstring), so registering the handler
    here is cheap and never blocks startup even if Playwright ends up
    unusable later. Playwright is an OPTIONAL dependency (`pip install
    playwright && playwright install chromium`) - its absence falls
    through to the same safe "stay mocked" outcome as every other real-
    handler override in this file."""
    if launcher_config.browser_backend != BACKEND_REAL:
        return

    tool_manager_module = modules.get("tool_manager_module")
    if tool_manager_module is None:
        return

    try:
        from luno.browser.provider import get_browser_provider
        from luno.tool_manager.builtin.real_browser import RealBrowserHandler
        provider = get_browser_provider()
        tool_manager_module.manager.registry.register("browser", RealBrowserHandler(provider))
        log("Real browser tool handler registered - browser/computer-use commands "
            "now reach a real browser (lazily launched on first use)", "bootstrap")
    except Exception as ex:
        log(f"failed to register real browser tool handler (staying mocked): {ex}", "bootstrap")


def register_device_intent_classifier(modules: Dict[str, Any], adapters: Dict[str, Any]) -> None:
    """Called once from `main.py`, same ordering reason as
    `register_real_tool_handlers()` above (needs `openrouter_adapter`,
    only built inside `register_all_adapters()`, which runs after
    `PlannerBridgeModule` is already constructed).

    Wires `PlannerBridgeModule.device_intent_client`/`device_intent_model`
    (see that class's `_classify_device_intent()` for the full feature)
    to the SAME `LLMManagerAdapter.client`/`.default_model` the real
    conversational replies already use - no second API key, no second
    config. Opt-in by construction, not by a separate env var: this only
    ever does anything useful when the active LLM provider is a REAL one
    (`.is_mock_active_provider` False - see `llm_manager.py`) - on a mock
    provider, the canned reply would never produce a parseable
    "ACTION=... DEVICE=..." line anyway, so there's nothing to gain by
    wiring it in, and this function no-ops rather than doing so."""
    planner_module = modules.get("planner_module")
    openrouter_adapter = adapters.get("openrouter_adapter")
    if planner_module is None or openrouter_adapter is None:
        return

    client = getattr(openrouter_adapter, "client", None)
    if client is None or getattr(openrouter_adapter, "is_mock_active_provider", True):
        return  # mock mode - classifier would have nothing real to call

    planner_module.device_intent_client = client
    planner_module.device_intent_model = openrouter_adapter.default_model
    log("Device intent classifier wired to the active LLM provider client - "
        "typos/paraphrases IntentParser can't classify get one extra AI-assisted pass", "bootstrap")


def register_session_summary_client(modules: Dict[str, Any], adapters: Dict[str, Any]) -> None:
    """Same opt-in-by-construction pattern and same reason for existing
    post-hoc (not inside register_all_modules()/register_all_adapters())
    as `register_device_intent_classifier()` right above - reuses the
    identical real `OpenRouterAdapter.client`/`.default_model`, no second
    API key or config.

    Wires `PlannerBridgeModule.session_summary_client`/
    `session_summary_model` - see that class's `_on_conversation_ended()`
    (automatic, fires when a wake-word conversation ends) and
    `_handle_manual_summarize_command()` (manual "rangkum obrolan ini")
    for the two places this actually gets used. Both call
    `luno.memory.summarize_and_archive_session()`, which now accepts this
    client shape directly (see that function's own docstring for the
    dual legacy-openai/new-pipeline duck typing). No-ops when the active
    LLM provider is a mock, for the same reason as the device intent
    classifier: a mock reply could never produce a real summary anyway."""
    planner_module = modules.get("planner_module")
    openrouter_adapter = adapters.get("openrouter_adapter")
    if planner_module is None or openrouter_adapter is None:
        return

    client = getattr(openrouter_adapter, "client", None)
    if client is None or getattr(openrouter_adapter, "is_mock_active_provider", True):
        return  # mock mode - nothing real to summarize with

    planner_module.session_summary_client = client
    planner_module.session_summary_model = openrouter_adapter.default_model
    log("Session summary client wired to the active LLM provider client - "
        "conversations now get archived to config/session_summaries.json when a wake session ends", "bootstrap")


def register_vision_context_reader(modules: Dict[str, Any], adapters: Dict[str, Any]) -> None:
    """P0.7 (Vision Context -> Automation Context) - same post-hoc
    "needs output from BOTH register_all_modules() and register_all_
    adapters()" pattern as `register_device_intent_classifier()`/
    `register_session_summary_client()` above, for the same reason:
    `vision_camera_event_bridge` is constructed inside `register_all_
    modules()`, but `adapter_manager` (whose `status_all()` is the ONLY
    source `VisionContext` is built from - see `luno/camera_automation/
    vision_context.py`'s own docstring) does not exist until `register_
    all_adapters()` runs afterward.

    Wires `VisionCameraEventBridge.vision_status_reader` to a tiny lambda
    that calls the SAME public `adapter_manager.status_all().get(
    "vision")` snapshot `luno/dashboard/collectors.py::collect_vision()`
    already reads - no new adapter-manager-wide coupling, no import of
    `luno.vision`/YOLO/RTSP code anywhere in this function. Always safe
    to call (never opt-in-gated on a real backend, unlike the LLM-client
    wiring functions above) - the reader itself degrades to an empty
    dict if `adapter_manager.status_all()` ever raises, and `build_
    vision_context()` degrades THAT to its own honest "unavailable"
    default; there is no failure mode here that could crash startup or
    produce a fabricated value."""
    bridge = modules.get("vision_camera_event_bridge")
    adapter_manager = adapters.get("adapter_manager")
    if bridge is None or adapter_manager is None:
        return

    def _read_vision_status() -> Dict[str, Any]:
        try:
            return adapter_manager.status_all().get("vision") or {}
        except Exception:
            return {}

    bridge.vision_status_reader = _read_vision_status
    log("Vision Context reader wired - camera_automation.camera_event now additively carries "
        "human_present/person_count/detected_objects/available/detection_error from the SAME "
        "Vision status snapshot the dashboard already reads", "bootstrap")


def register_camera_action_ha_state_reader(modules: Dict[str, Any], adapters: Dict[str, Any]) -> None:
    """P0.8.0 (Camera Automation -> Home Assistant Action Safety
    Pipeline) - same post-hoc "needs output from BOTH register_all_
    modules() and register_all_adapters()" pattern as `register_vision_
    context_reader()`/`register_device_intent_classifier()` above:
    `automation_engine` is constructed inside `register_all_modules()`,
    but `home_assistant_adapter.client` (the real `RealHomeAssistantClient`
    - the ONLY thing this function ever reads from) does not exist until
    `register_all_adapters()` runs afterward.

    Wires `AutomationEngine.ha_state_reader` to a tiny closure over the
    SAME real `RealHomeAssistantClient.get_entity_state()` the EXISTING
    `RealHomeAssistantHandler._safe_get_state()` already calls for its
    own "already ON/OFF -> skip the redundant service call" shortcut
    (`luno/tool_manager/builtin/real_home_assistant.py::_execute_on_
    off()`) - no new Home Assistant client, no new WebSocket/REST call,
    ever originates from this function or from `camera_action_safety.py`
    itself (P0.8.0 brief Section 5's own explicit constraint).

    No-ops harmlessly (leaves `AutomationEngine.ha_state_reader` at its
    `None` default) whenever: `automation_engine`/`home_assistant_
    adapter` aren't both present, the HA backend isn't `real`, the real
    backend failed to connect and fell back to `MockHomeAssistantClient`
    (which has no `get_entity_state` at all - `callable(...)` below is
    `False` for it, by construction, not a special case), or anything
    else about the client's shape is unexpected. `camera_action_safety.
    validate_camera_ha_action()`'s own "already in desired state" check
    is entirely OPTIONAL or - per the brief's own Section 5 wording,
    "use the existing state-reading mechanism IF ALREADY AVAILABLE" -
    every one of these no-op cases is a legitimate, non-error "not
    available right now", never a failure to report."""
    engine = modules.get("automation_engine")
    ha_adapter = adapters.get("home_assistant_adapter")
    if engine is None or ha_adapter is None:
        return

    ha_client = getattr(ha_adapter, "client", None)
    get_state = getattr(ha_client, "get_entity_state", None) if ha_client is not None else None
    if not callable(get_state):
        return

    def _read_ha_state(entity_id: str) -> Optional[str]:
        return get_state(entity_id)

    engine.ha_state_reader = _read_ha_state
    log("Camera Action Safety Gate: HA state reader wired - camera-triggered turn_on/turn_off "
        "actions now skip a redundant Home Assistant call when the target entity is already in "
        "the requested state", "bootstrap")


#: P0.8.1/P0.8.2 - the ONLY TEST-ONLY rules this override is scoped to.
#: Matches `config/automation_rules.json`'s own rule ids exactly - no
#: other rule id is ever touched by this function (P0.8.1 brief Section
#: 3: "Use the existing P0.8.0 TEST-ONLY rule... Do not modify unrelated
#: automation rules" - P0.8.2 extends the SAME override to its own new
#: sibling OFF rule rather than introducing a second override
#: mechanism; both rules exist purely to prove the live ON/OFF chain
#: against ONE explicitly configured test light, never a production
#: entity).
_LIVE_TEST_RULE_IDS = frozenset({
    "camera_test_automation_safety_action",       # P0.8.0/P0.8.1 - human_detected -> turn_on
    "camera_test_automation_safety_action_off",   # P0.8.2 - human_cleared -> turn_off
})


def apply_camera_automation_test_light_override(modules: Dict[str, Any]) -> Optional[str]:
    """P0.8.1 (Live Camera -> Home Assistant Light Verification),
    extended by P0.8.2 (Camera Human Cleared -> Safe Light Off) -
    resolves the ONE explicitly configured live-test light
    (`CAMERA_AUTOMATION_TEST_LIGHT_ENTITY`, per the P0.8.1 brief's own
    Section 2: "Use exactly ONE explicitly configured test light...
    never automatically select a random light") and, ONLY when that
    environment variable is actually set, overrides the target of
    EVERY rule in `_LIVE_TEST_RULE_IDS` (currently: the P0.8.0/P0.8.1
    ON rule and the P0.8.2 OFF rule - both TEST-ONLY, both otherwise
    targeting the shipped, non-real placeholder `light.test_camera_
    automation`) to the SAME real entity id the user configured, so a
    live walk-test turns the SAME physical light on and off, never two
    different ones.

    Deliberately scoped as narrowly as possible, per the P0.8.1 brief's
    own "IMPORTANT" preamble ("Do NOT modify production automation
    behavior beyond the specific live-test rule") - a constraint P0.8.2
    inherits and does not relax:

    - No-ops entirely (returns `None`, changes nothing) when `CAMERA_
      AUTOMATION_TEST_LIGHT_ENTITY` is unset - the default, every
      sandbox test, and every prior sprint's own regression run all see
      byte-for-byte the same rules/targets as shipped. This is why this
      function is safe to call unconditionally from `main.py`: it is
      inert unless the user has explicitly opted in.
    - Only ever touches the rule ids in `_LIVE_TEST_RULE_IDS` - the
      pre-existing `camera_human_detected_log`/`camera_human_detected_
      test_action`/`camera_multiple_people_log` rules are never
      inspected or modified by this function.
    - Only ever touches each of those rules' `home_assistant.turn_on`/
      `turn_off` action's `parameters["target"]` - each rule's id, name,
      enabled flag, trigger, conditions, and `cooldown_seconds` are all
      left exactly as shipped (in particular: the ON and OFF rules keep
      their own INDEPENDENT `cooldown_seconds`/`_cooldown_until` entries
      - `AutomationEngine`'s cooldown dict is keyed by `rule.id`, so
      overriding both rules' targets here has no effect whatsoever on
      that independence).
    - Mutates each loaded `AutomationAction.parameters` dict IN PLACE
      (legal - `AutomationAction` is a frozen dataclass, but `parameters`
      itself is an ordinary mutable `dict`; only reassigning the frozen
      dataclass's own field would be illegal, and this function never
      does that). This is purely an in-memory, this-process-only change
      - `config/automation_rules.json` on disk is never written to (no
      `_persist_rules()` call), so nothing here can accidentally
      persist a real household entity id into version control or the
      shipped test-only placeholders for every other sprint's own runs.

    Returns the resolved entity id if it was applied to at least one
    rule, or `None` if nothing was applied (env var unset, engine
    missing, or neither rule is loaded/has a `home_assistant.*` action
    to override - all treated as harmless no-ops, never raised)."""
    entity_id = os.environ.get("CAMERA_AUTOMATION_TEST_LIGHT_ENTITY", "").strip()
    if not entity_id:
        return None

    engine = modules.get("automation_engine")
    if engine is None:
        return None

    rules_dict = engine._rules if hasattr(engine, "_rules") else {}
    any_applied = False
    for rule_id in sorted(_LIVE_TEST_RULE_IDS):
        rule = rules_dict.get(rule_id)
        if rule is None:
            log(
                f"P0.8.1/P0.8.2 test-light override: CAMERA_AUTOMATION_TEST_LIGHT_ENTITY is set, but "
                f"rule '{rule_id}' is not loaded - nothing overridden for it.", "bootstrap",
            )
            continue
        applied_here = False
        for action in rule.actions:
            if action.type in ("home_assistant.turn_on", "home_assistant.turn_off"):
                action.parameters["target"] = entity_id
                applied_here = True
        if applied_here:
            any_applied = True
            log(
                f"P0.8.1/P0.8.2 test-light override APPLIED (in-memory only, config/automation_rules."
                f"json on disk unchanged): rule '{rule_id}' now targets '{entity_id}' for this process "
                "only.", "bootstrap",
            )
        else:
            log(
                f"P0.8.1/P0.8.2 test-light override: rule '{rule_id}' loaded but has no home_assistant."
                "turn_on/turn_off action to override - nothing overridden for it.", "bootstrap",
            )

    return entity_id if any_applied else None


def register_intent_classifier(modules: Dict[str, Any], adapters: Dict[str, Any]) -> None:
    """Efficient LLM Classifier sprint - same opt-in-by-construction
    pattern and same ordering reason as `register_device_intent_
    classifier()`/`register_session_summary_client()` right above (needs
    `openrouter_adapter`, only built inside `register_all_adapters()`,
    which runs after `PlannerBridgeModule` is already constructed).

    Wires `planner_module.decision_engine.classifier_client` to the SAME
    `LLMManagerAdapter.client` the real conversational replies and the
    device-intent classifier already use - no second API key, no second
    config. `DecisionEngine.decide()` still only ever CALLS this when
    `CLASSIFIER_ENABLED=true` (env, `RoutingConfig.classifier_enabled` -
    see `luno/routing/config.py`) AND an utterance is genuinely
    ambiguous (see that method's own "ambiguous gate" comment) - wiring
    the client here does not, by itself, turn the feature on; both this
    AND the env flag must be true. No-ops in mock mode for the same
    reason the other two classifiers above do: a mock reply could never
    produce a real classification anyway."""
    planner_module = modules.get("planner_module")
    openrouter_adapter = adapters.get("openrouter_adapter")
    if planner_module is None or openrouter_adapter is None:
        return

    client = getattr(openrouter_adapter, "client", None)
    if client is None or getattr(openrouter_adapter, "is_mock_active_provider", True):
        return  # mock mode - classifier would have nothing real to call

    decision_engine = getattr(planner_module, "decision_engine", None)
    if decision_engine is None:
        return
    decision_engine.set_classifier_client(client)
    log("Efficient LLM Classifier wired to the active LLM provider client - ambiguous utterances "
        "(no deterministic rule matched) get one optional GPT-5.4-nano classification pass when "
        "CLASSIFIER_ENABLED=true", "bootstrap")
