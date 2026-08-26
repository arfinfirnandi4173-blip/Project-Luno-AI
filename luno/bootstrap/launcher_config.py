"""
launcher_config.py
===================

`LauncherConfig` - the ONE place `main.py` reads "what should this run
look like" from, with a single, deterministic precedence chain:

    1. real process environment variables         (always wins)
    2. `.env` file                                    (fills gaps left by #1)
    3. JSON/YAML config file (`LUNO_CONFIG_FILE`)         (fills gaps left by #1+#2)
    4. hardcoded defaults below                              (final fallback)

Layers 1-3 are resolved by SEEDING `os.environ` (never overwriting a key
that's already set) before any of the 19 existing subsystems' own
`*Config.from_env()` calls run - so the config-file layer is transparent
to every already-existing package (`CoreConfig.from_env()`,
`OpenRouterConfig.from_env()`, `WakeSessionConfig.from_env()`,
`BargeInConfig.from_env()`, `MemoryRetrievalConfig.from_env()`,
`RealFishAudioConfig.from_env()`, `luno.config`'s module-level
`os.getenv()` calls, ...) without touching a single line of any of them.
This is the only way to add "JSON/YAML configuration" support to a
project where 6+ independent packages already each read `os.environ`
directly by design ("read env independently, never import across the
boundary") - reaching into every one of them individually would mean
rewriting packages this sprint explicitly forbids touching.

`LauncherConfig` itself additionally holds the small set of
launcher-level knobs that don't belong to any existing subsystem (which
adapters run in "real" vs "mock" backend mode, the supervisor's poll
interval, the config file path itself) plus a read-only snapshot of the
handful of values the spec's "Runtime Status" section wants printed
(wake words, current LLM/TTS/vision/whisper model, memory retrieval
mode, planner/vision/interrupt enabled flags) - computed by re-reading
each subsystem's OWN already-existing config object, never duplicating
their values as separate hardcoded settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a real requirement, but stay defensive
    def load_dotenv(*_a, **_k) -> bool:  # type: ignore[misc]
        return False

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Backend selector values every adapter-with-a-real-implementation
#: supports - "mock" is always the safe, zero-external-dependency
#: default; every real value requires the matching real dependency/
#: credential to actually be usable (checked by the health-check
#: framework, never assumed here).
BACKEND_MOCK = "mock"
BACKEND_REAL = "real"

_ADAPTER_BACKEND_ENV_VARS = {
    "whisper": "WHISPER_BACKEND",
    "vision": "VISION_BACKEND",
    "unity": "UNITY_BACKEND",
    "home_assistant": "HOME_ASSISTANT_BACKEND",
    "windows": "WINDOWS_BACKEND",
    "camera_ptz": "CAMERA_PTZ_BACKEND",
    # Browser/computer-use - reuses `BROWSER_ENABLED` directly as the
    # backend switch (spec's own naming) rather than a separate
    # `BROWSER_BACKEND` var - `_backend_from_env()` already treats
    # "true"/"1"/"on"/"yes" as REAL, so `BROWSER_ENABLED=true` maps
    # straight onto this project's existing mock/real backend pattern.
    "browser": "BROWSER_ENABLED",
    # openrouter/fish_audio already have their OWN existing switches
    # (`OPENROUTER_API_KEY` presence, `FISH_AUDIO_BACKEND`) - kept as-is,
    # not duplicated here; see `adapters.py`.
}


def _seed_env_from_mapping(values: Dict[str, Any], source: str) -> List[str]:
    """Sets `os.environ[key] = str(value)` for every key NOT already
    present - returns the list of keys actually applied, purely for
    startup logging ("configuration loaded from: ..."). Never overwrites
    a real env var or an already-`.env`-loaded one - see module
    docstring for why order matters here."""
    applied: List[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if key in os.environ:
            continue
        os.environ[key] = str(value)
        applied.append(key)
    return applied


def _load_config_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text) if text.strip() else {}
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency - see CoreConfig.from_yaml's own precedent
        except ImportError as ex:
            raise RuntimeError(
                f"Config file '{path}' is YAML but PyYAML is not installed "
                f"(pip install pyyaml) - use a .json config file instead, or install PyYAML."
            ) from ex
        data = yaml.safe_load(text) or {}
    else:
        raise ValueError(f"Unsupported config file extension '{suffix}' (use .json/.yaml/.yml): {path}")
    if not isinstance(data, dict):
        raise ValueError(f"Config file '{path}' must contain a top-level object/mapping, got {type(data).__name__}")
    # A config file's "env" section maps directly onto os.environ keys
    # (e.g. {"env": {"WAKE_WORD": "luno"}}) - everything else is reserved
    # for future launcher-only settings (not read today, ignored rather
    # than rejected, so old config files never break on a newer launcher).
    env_section = data.get("env")
    return dict(env_section) if isinstance(env_section, dict) else {}


@dataclass
class LauncherConfig:
    #: resolved once, at load time - which env vars actually got applied
    #: from .env / the config file, and from where (startup log +
    #: banner transparency, matches the wake-word-config-source
    #: precedent already established in `luno.wake_session`).
    env_file_loaded: bool = False
    config_file_path: Optional[str] = None
    config_file_keys_applied: List[str] = field(default_factory=list)

    #: adapter backend selection - "mock" (default, zero external deps)
    #: or "real" (wraps the matching existing legacy module - see
    #: `adapters.py`). OpenRouter/Fish Audio keep their OWN pre-existing
    #: switches (`OPENROUTER_API_KEY` presence / `FISH_AUDIO_BACKEND`),
    #: not duplicated here.
    whisper_backend: str = BACKEND_MOCK
    vision_backend: str = BACKEND_MOCK
    unity_backend: str = BACKEND_MOCK
    home_assistant_backend: str = BACKEND_MOCK
    windows_backend: str = BACKEND_MOCK
    #: pan/tilt camera control (e.g. TP-Link Tapo C212 via `pytapo`) -
    #: gated additionally on TAPO_HOST/TAPO_USERNAME/TAPO_PASSWORD all
    #: being set (see `adapters.py::_register_real_camera_ptz_handler`),
    #: same "backend flag alone isn't sufficient" pattern
    #: `home_assistant_backend` already follows (also needs a reachable
    #: connection, not just the flag).
    camera_ptz_backend: str = BACKEND_MOCK
    #: real Playwright-backed browser/computer-use - gated on
    #: `BROWSER_ENABLED=true` (see `adapters.py::
    #: _register_real_browser_handler`); additionally falls back to
    #: mock at registration time if Playwright itself isn't installed,
    #: same "flag alone isn't sufficient" pattern `camera_ptz_backend`
    #: already follows for `pytapo`.
    browser_backend: str = BACKEND_MOCK

    #: supervisor (background adapter/module restart-on-failure) tuning.
    supervisor_enabled: bool = True
    supervisor_interval_s: float = 15.0
    supervisor_max_restart_attempts: int = 3

    #: structured log file (in addition to stdout) - optional.
    log_file: Optional[str] = None
    log_level: str = "INFO"

    #: Sprint 7 - Web Dashboard. Localhost by default (spec: "Dashboard
    #: is localhost by default") - `DASHBOARD_HOST` must be set
    #: explicitly to bind anything else, never inferred.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765

    @classmethod
    def load(cls) -> "LauncherConfig":
        """The one deterministic loader `main.py` calls. Order:
        1) load `.env` (fills real env gaps only - python-dotenv default)
        2) load the JSON/YAML config file, if any, into any remaining gaps
        3) read launcher-only settings (backends, supervisor tuning) from
           env, now that both layers above have had a chance to seed them
        4) hardcoded dataclass defaults for anything still unset
        """
        env_path = PROJECT_ROOT / ".env"
        env_file_loaded = load_dotenv(dotenv_path=str(env_path)) if env_path.exists() else load_dotenv()

        config_file_env = os.getenv("LUNO_CONFIG_FILE")
        config_file_path = Path(config_file_env) if config_file_env else _default_config_file()
        applied: List[str] = []
        if config_file_path is not None and config_file_path.exists():
            file_values = _load_config_file(config_file_path)
            applied = _seed_env_from_mapping(file_values, source=str(config_file_path))

        return cls(
            env_file_loaded=env_file_loaded,
            config_file_path=str(config_file_path) if config_file_path else None,
            config_file_keys_applied=applied,
            whisper_backend=_backend_from_env("whisper"),
            vision_backend=_backend_from_env("vision"),
            unity_backend=_backend_from_env("unity"),
            home_assistant_backend=_backend_from_env("home_assistant"),
            windows_backend=_backend_from_env("windows"),
            camera_ptz_backend=_backend_from_env("camera_ptz"),
            browser_backend=_backend_from_env("browser"),
            supervisor_enabled=_bool_env("LUNO_SUPERVISOR_ENABLED", True),
            supervisor_interval_s=_float_env("LUNO_SUPERVISOR_INTERVAL_S", 15.0),
            supervisor_max_restart_attempts=_int_env("LUNO_SUPERVISOR_MAX_RESTART_ATTEMPTS", 3),
            log_file=os.getenv("LUNO_LOG_FILE"),
            log_level=os.getenv("LUNO_LOG_LEVEL", "INFO"),
            dashboard_enabled=_bool_env("DASHBOARD_ENABLED", True),
            dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int_env("DASHBOARD_PORT", 8765),
        )

    def reload(self) -> "LauncherConfig":
        """Re-resolves from the current environment/config file -
        used by `/reload`. Does NOT re-run `load_dotenv()` a second
        time with different semantics than the first load (python-dotenv
        itself is idempotent/gap-filling only), so this is safe to call
        repeatedly without ever clobbering a real env var a user set
        after startup."""
        return LauncherConfig.load()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "env_file_loaded": self.env_file_loaded,
            "config_file_path": self.config_file_path,
            "config_file_keys_applied": list(self.config_file_keys_applied),
            "whisper_backend": self.whisper_backend,
            "vision_backend": self.vision_backend,
            "unity_backend": self.unity_backend,
            "home_assistant_backend": self.home_assistant_backend,
            "windows_backend": self.windows_backend,
            "camera_ptz_backend": self.camera_ptz_backend,
            "browser_backend": self.browser_backend,
            "supervisor_enabled": self.supervisor_enabled,
            "supervisor_interval_s": self.supervisor_interval_s,
            "supervisor_max_restart_attempts": self.supervisor_max_restart_attempts,
            "log_file": self.log_file,
            "log_level": self.log_level,
            "dashboard_enabled": self.dashboard_enabled,
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
        }


def _default_config_file() -> Optional[Path]:
    for candidate in ("luno.config.json", "luno.config.yaml", "luno.config.yml"):
        p = PROJECT_ROOT / "config" / candidate
        if p.exists():
            return p
    return None


def _backend_from_env(adapter_key: str) -> str:
    env_var = _ADAPTER_BACKEND_ENV_VARS[adapter_key]
    raw = (os.getenv(env_var) or BACKEND_MOCK).strip().lower()
    return BACKEND_REAL if raw in ("real", "true", "1", "on", "yes") else BACKEND_MOCK


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default
