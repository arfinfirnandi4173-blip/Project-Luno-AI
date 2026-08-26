"""
config.py (luno.browser)
=========================

`BrowserConfig.from_env()` - read fresh from `os.getenv()` on every call,
same "reloadable without a restart" convention
`real_camera_ptz.py::_PTZConfig.from_env()` / `real_home_assistant.py`'s
`_VerifyConfig.from_env()` already established for this project's "real"
tool handlers, rather than reading once at import time the way the
legacy `luno/config.py` module does. Env vars are documented here, not
duplicated into `luno/config.py` (this package owns its own config
surface end to end, same as `real_camera_ptz.py`'s `TAPO_PAN_STEP_DEGREES`
etc. do today).

`MonitorTarget`/`load_monitor_targets()` is the SAME "missing file = the
feature is just inactive" convention `luno.environment_intent.
load_environment_triggers()` already uses for
`config/environment_triggers.json` - a JSON file, not a second env-var
list, because a monitoring target list is naturally an array of records,
not flat key=value pairs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


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


def _csv_env(name: str) -> Tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool = False
    headless: bool = True
    profile_dir: str = ""
    default_timeout_s: float = 20.0
    navigation_timeout_s: float = 30.0
    max_steps: int = 10
    allowed_domains: Tuple[str, ...] = ()
    require_confirmation: bool = True
    monitor_interval_s: float = 30.0
    download_dir: str = ""
    screenshot_max_edge: int = 1280
    monitor_targets_file: str = ""

    @classmethod
    def from_env(cls) -> "BrowserConfig":
        data_dir = os.getenv("DATA_DIR", "config")
        return cls(
            enabled=_bool_env("BROWSER_ENABLED", False),
            headless=_bool_env("BROWSER_HEADLESS", True),
            profile_dir=os.getenv("BROWSER_PROFILE_DIR", "").strip(),
            default_timeout_s=_float_env("BROWSER_DEFAULT_TIMEOUT_S", 20.0),
            navigation_timeout_s=_float_env("BROWSER_NAVIGATION_TIMEOUT_S", 30.0),
            max_steps=_int_env("BROWSER_MAX_STEPS", 10),
            allowed_domains=_csv_env("BROWSER_ALLOWED_DOMAINS"),
            require_confirmation=_bool_env("BROWSER_REQUIRE_CONFIRMATION", True),
            monitor_interval_s=_float_env("BROWSER_MONITOR_INTERVAL_S", 30.0),
            download_dir=os.getenv("BROWSER_DOWNLOAD_DIR", "").strip() or os.path.join(data_dir, "browser_downloads"),
            screenshot_max_edge=_int_env("BROWSER_SCREENSHOT_MAX_EDGE", 1280),
            monitor_targets_file=os.getenv("BROWSER_MONITOR_TARGETS_FILE", "").strip()
            or os.path.join(data_dir, "browser_monitor_targets.json"),
        )


# -- monitoring targets --------------------------------------------------------

_VALID_TARGET_TYPES = ("home_assistant", "portainer", "grafana", "docker", "generic")


@dataclass(frozen=True)
class MonitorTarget:
    name: str
    url: str
    type: str = "generic"
    enabled: bool = True
    read_only: bool = True


def load_monitor_targets(path: Optional[str] = None) -> List[MonitorTarget]:
    """Reads `BROWSER_MONITOR_TARGETS_FILE`
    (`config/browser_monitor_targets.json` by default). Missing file /
    malformed JSON / a malformed individual entry -> that entry (or the
    whole file) is silently skipped, never raises - identical failure
    mode to `environment_intent.load_environment_triggers()`."""
    file_path = path or BrowserConfig.from_env().monitor_targets_file
    targets: List[MonitorTarget] = []
    if not file_path or not os.path.exists(file_path):
        return targets
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as ex:
        print(f"[BrowserMonitor] ✗ Failed to load {file_path}: {ex}")
        return targets

    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        url = (entry.get("url") or "").strip()
        if not name or not url:
            print(f"[BrowserMonitor] ✗ Skip entry missing 'name'/'url': {entry}")
            continue
        target_type = (entry.get("type") or "generic").strip().lower()
        if target_type not in _VALID_TARGET_TYPES:
            target_type = "generic"
        targets.append(MonitorTarget(
            name=name, url=url, type=target_type,
            enabled=bool(entry.get("enabled", True)),
            read_only=bool(entry.get("read_only", True)),
        ))
    return targets
