"""
banner.py
=========

The startup banner + the "Runtime Status" block the spec asks to be
"clearly visible" on every launch: version/build/python/OS, then every
field the spec's "Runtime Status" section lists (loaded modules,
loaded adapters, wake words, current LLM, current TTS, current vision
model, current whisper model, memory retrieval mode, planner/vision/
interrupt enabled, Home Assistant/Unity/database status).

Every value printed here is read from an EXISTING config object
(`WakeSessionConfig`, `BargeInConfig`, `MemoryRetrievalConfig`,
`OpenRouterAdapter`, `RealFishAudioConfig`, `luno.config`) - nothing is
duplicated as a separate hardcoded setting; this module is purely a
read-and-format layer over what `bootstrap.modules`/`bootstrap.adapters`
already built.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import TYPE_CHECKING, Any, Dict, Optional

from .version import CODENAME, VERSION, build_string

if TYPE_CHECKING:
    from luno.core.runtime import Runtime
    from luno.adapters.manager import AdapterManager
    from .launcher_config import LauncherConfig


class _Colors:
    enabled = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GREY = "\033[90m"

    @classmethod
    def wrap(cls, text: str, *codes: str) -> str:
        if not cls.enabled or not codes:
            return text
        return "".join(codes) + text + cls.RESET


def _c(text: str, *codes: str) -> str:
    return _Colors.wrap(text, *codes)


def print_startup_banner() -> None:
    """The first thing printed - before configuration/health checks even
    run, so a crash during startup still shows *something* identifying
    what was being launched."""
    line = "=" * 50
    print(_c(line, _Colors.CYAN, _Colors.BOLD))
    print(_c(f"{CODENAME}", _Colors.BOLD))
    print(f"Version   {VERSION}")
    print(f"Build     {build_string()}")
    print(f"Python    {platform.python_version()}")
    print(f"OS        {platform.system()} {platform.release()}")
    print(_c(line, _Colors.CYAN, _Colors.BOLD))


def print_step(message: str) -> None:
    """One `Loading configuration...` / `Registering modules...`-style
    startup progress line, matching the spec's own example sequence."""
    print(_c(message, _Colors.DIM))


def _mark(ok: bool) -> str:
    return _c("✓", _Colors.GREEN) if ok else _c("✗", _Colors.RED)


def build_runtime_status(
    runtime: "Runtime",
    adapter_manager: "AdapterManager",
    launcher_config: "LauncherConfig",
) -> Dict[str, Any]:
    """Assembles the exact field set the spec's "Runtime Status" section
    asks for, reading each value from whatever already-existing config
    object owns it. Returned as plain data so both `print_runtime_status`
    and the `/status` console command can share one implementation."""
    module_names = sorted(runtime.module_manager.all_modules().keys())
    adapter_names = sorted(adapter_manager.registry.list_adapters())

    wake_words: list = []
    planner_enabled = "planner" in module_names
    vision_enabled = "vision_memory" in module_names
    interrupt_enabled = "barge_in" in module_names
    memory_mode: Optional[str] = None

    session_manager = runtime.module_manager.all_modules().get("session_manager")
    if session_manager is not None:
        wake_words = list(getattr(session_manager.module.config, "wake_words", []))

    planner_record = runtime.module_manager.all_modules().get("planner")
    if planner_record is not None:
        retriever = getattr(planner_record.module, "memory_retriever", None)
        if retriever is not None:
            memory_mode = getattr(retriever.config, "retrieval_mode", None)

    current_llm = None
    openrouter_adapter = adapter_manager.registry.get("openrouter")
    if openrouter_adapter is not None:
        current_llm = getattr(openrouter_adapter, "default_model", None)

    current_tts = "mock"
    fish_adapter = adapter_manager.registry.get("fish_audio")
    if fish_adapter is not None:
        client = getattr(fish_adapter, "client", None)
        current_tts = type(client).__name__ if client is not None else "unknown"

    try:
        import luno.config as legacy_config
        current_whisper = f"{legacy_config.WHISPER_MODEL_SIZE} ({legacy_config.WHISPER_DEVICE})"
        current_vision_model = legacy_config.YOLO_MODEL_PATH
        ha_configured = bool(legacy_config.HA_TOKEN and legacy_config.HA_URL)
        db_path = os.path.join(legacy_config.DATA_DIR, "vision_memory.sqlite3")
    except Exception:
        current_whisper = "unknown"
        current_vision_model = "unknown"
        ha_configured = False
        db_path = "unknown"

    return {
        "version": VERSION,
        "build": build_string(),
        "modules_loaded": module_names,
        "adapters_loaded": adapter_names,
        "wake_words": wake_words,
        "current_llm": current_llm or "unset",
        "current_tts": current_tts,
        "current_vision_model": current_vision_model,
        "current_whisper_model": current_whisper,
        "memory_retrieval_mode": memory_mode or "disabled",
        "planner_enabled": planner_enabled,
        "vision_enabled": vision_enabled,
        "interrupt_enabled": interrupt_enabled,
        "home_assistant_configured": ha_configured,
        "home_assistant_backend": launcher_config.home_assistant_backend,
        "unity_backend": launcher_config.unity_backend,
        "vision_backend": launcher_config.vision_backend,
        "whisper_backend": launcher_config.whisper_backend,
        "database_path": db_path,
        "healthy": runtime.health().healthy,
    }


def print_runtime_status(status: Dict[str, Any]) -> None:
    print()
    print(_c("Runtime Status", _Colors.BOLD))
    print(f"  Version                 {status['version']} ({status['build']})")
    print(f"  Loaded modules          {', '.join(status['modules_loaded']) or '(none)'}")
    print(f"  Loaded adapters         {', '.join(status['adapters_loaded']) or '(none)'}")
    print(f"  Wake words              {', '.join(status['wake_words']) or '(none configured)'}")
    print(f"  Current LLM             {status['current_llm']}")
    print(f"  Current TTS             {status['current_tts']}")
    print(f"  Current Vision model    {status['current_vision_model']}")
    print(f"  Current Whisper model   {status['current_whisper_model']}")
    print(f"  Memory Retrieval mode   {status['memory_retrieval_mode']}")
    print(f"  Planner enabled         {_mark(status['planner_enabled'])}")
    print(f"  Vision enabled          {_mark(status['vision_enabled'])}")
    print(f"  Interrupt enabled       {_mark(status['interrupt_enabled'])}")
    print(
        f"  Home Assistant          {_mark(status['home_assistant_configured'])} "
        f"(backend={status['home_assistant_backend']})"
    )
    print(f"  Unity                   (backend={status['unity_backend']})")
    print(f"  Database                {status['database_path']}")
    print(f"  Overall health          {_mark(status['healthy'])}")
    print()
