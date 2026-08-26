"""
config.py
=========

`CoreConfig` - Runtime-level tunables (heartbeat interval, scheduler
tick, dispatcher pool size, ...), loadable from JSON, YAML, or
environment variables, with a `reload()` that re-reads from wherever it
originally came from. Deliberately does NOT hold per-module
configuration - each real module owns its own config the same way it
owns its own start-up logic; this is only what `Runtime` itself needs
to wire the engine together.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .exceptions import ConfigError

_ENV_FIELD_MAP = {
    "HEARTBEAT_INTERVAL_S": ("heartbeat_interval_s", float),
    "SCHEDULER_TICK_S": ("scheduler_tick_s", float),
    "DISPATCHER_MAX_WORKERS": ("dispatcher_max_workers", int),
    "EVENT_QUEUE_MAX": ("event_queue_max", int),
    "STALL_THRESHOLD_S": ("stall_threshold_s", float),
}


@dataclass
class CoreConfig:
    heartbeat_interval_s: float = 10.0
    scheduler_tick_s: float = 1.0
    dispatcher_max_workers: int = 8
    event_queue_max: int = 20000
    stall_threshold_s: float = 30.0
    extra: Dict[str, Any] = field(default_factory=dict)
    _source: Optional[Dict[str, str]] = field(default=None, repr=False)  # {"kind": "json"|"yaml"|"env", "path"/"prefix": ...}

    # -- constructors -------------------------------------------------------

    @staticmethod
    def default() -> "CoreConfig":
        return CoreConfig()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CoreConfig":
        known = {f.name for f in dataclasses.fields(cls) if not f.name.startswith("_") and f.name != "extra"}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(extra=extra, **kwargs)

    @classmethod
    def from_json(cls, path: str) -> "CoreConfig":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as ex:
            raise ConfigError(f"Failed to load JSON config '{path}': {ex}") from ex
        cfg = cls.from_dict(data)
        cfg._source = {"kind": "json", "path": path}
        return cfg

    @classmethod
    def from_yaml(cls, path: str) -> "CoreConfig":
        try:
            import yaml  # type: ignore
        except ImportError as ex:
            raise ConfigError("PyYAML is not installed - `pip install pyyaml` to use from_yaml()") from ex
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except OSError as ex:
            raise ConfigError(f"Failed to load YAML config '{path}': {ex}") from ex
        cfg = cls.from_dict(data)
        cfg._source = {"kind": "yaml", "path": path}
        return cfg

    @classmethod
    def from_env(cls, prefix: str = "LUNO_CORE_") -> "CoreConfig":
        data: Dict[str, Any] = {}
        for suffix, (field_name, caster) in _ENV_FIELD_MAP.items():
            raw = os.getenv(prefix + suffix)
            if raw is None:
                continue
            try:
                data[field_name] = caster(raw)
            except ValueError:
                raise ConfigError(f"Env var {prefix}{suffix}='{raw}' is not a valid {caster.__name__}")
        cfg = cls.from_dict(data)
        cfg._source = {"kind": "env", "prefix": prefix}
        return cfg

    # -- reload / export --------------------------------------------------

    def reload(self) -> "CoreConfig":
        """Re-reads config from wherever this instance originally came
        from (file path for json/yaml, environment again for env-loaded
        or default-constructed configs) - used by `Runtime.reload()`."""
        if self._source is None:
            return CoreConfig.from_env()
        kind = self._source["kind"]
        if kind == "json":
            return CoreConfig.from_json(self._source["path"])
        if kind == "yaml":
            return CoreConfig.from_yaml(self._source["path"])
        return CoreConfig.from_env(self._source.get("prefix", "LUNO_CORE_"))

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("_source", None)
        return d
