"""
config.py
=========

`CameraAutomationConfig` - env-var-only, reloadable, matching the exact
pattern `barge_in.models.BargeInConfig.from_env()` / `wake_session.models.
WakeSessionConfig.from_env()` already establish: every subsystem owns its
own self-contained `Config.from_env()` dataclass, reading its own
`os.getenv(...)` calls directly, never touching the shared
`luno/bootstrap/launcher_config.py`.

Feature flag (P0 brief §10): `enabled` defaults to `False` - camera
automation is OFF unless explicitly turned on, so a fresh checkout/deploy
behaves EXACTLY as before this package existed (§10: "Default behavior
should preserve the current system behavior"). Even when `enabled=True`,
an empty `entities` allowlist means the module still does nothing -
two independent layers of "safe by default".

--------------------------------------------------------------------
P0.5 (Real Camera Integration) addition: `cameras_path`
--------------------------------------------------------------------
`entities`/`CAMERA_AUTOMATION_ENTITIES` (flat allowlist, P0) is
UNCHANGED - existing behavior, existing tests, existing env var, byte-
for-byte identical. P0.5 adds one new, independent field: `cameras_path`
- the path to a new, isolated `config/camera_automation.json` file
holding structured per-camera entity-ROLE mappings (`CameraProfile` in
`cameras.py`), the config-driven mapping the P0.5 brief's own Section 6
calls for. This follows this project's own established convention for
structured mappings (`config/camera_patrol_routes.json`, `config/
environment_triggers.json`) rather than inventing a new configuration
system (P0.5 brief Section 6: "Do not introduce a new configuration
system"). Loaded fresh by `CameraAutomationModule.reload_cameras()` -
never touches `entities`/the flat allowlist path, which keeps working
exactly as it did in P0 whether or not any `CameraProfile` is
configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

#: P0.5 - the new, isolated per-camera entity-role mapping file. A
#: SEPARATE file from every existing `config/*.json` (never shares a
#: schema/key with `camera_patrol_routes.json`, `automation_rules.json`,
#: or anything else) - P0.5 brief §14's own "camera-specific data should
#: have its own namespace/schema" applied one more time.
DEFAULT_CAMERAS_PATH = "config/camera_automation.json"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _split(raw: str) -> List[str]:
    return [w.strip() for w in raw.split(",") if w.strip()]


@dataclass
class CameraAutomationConfig:
    #: §10 feature flag - independently disableable, off by default.
    enabled: bool = False

    #: §14 "camera-specific data should have its own namespace" - this is
    #: an ALLOWLIST of Home Assistant entity_ids this module will react
    #: to (e.g. "binary_sensor.front_door_motion", "camera.driveway").
    #: Empty by default - an operator must explicitly list which entities
    #: are camera-relevant; nothing is inferred/guessed from entity_id
    #: naming, so there is no risk of accidentally reacting to an
    #: unrelated light/switch/script state change.
    #: P0.5 note: this flat allowlist path is UNCHANGED from P0 - still
    #: produces the exact same `camera_automation.state_changed` raw
    #: relay it always did (see module.py). It is independent from, and
    #: can be used with or without, the new `cameras_path` profiles below.
    entities: List[str] = field(default_factory=list)

    #: Per-entity minimum spacing between two `camera_automation.
    #: state_changed` publishes, seconds. Bounds event volume under a
    #: flapping/noisy sensor without needing a second scheduler/thread -
    #: purely a `time.monotonic()` comparison at publish time.
    cooldown_s: float = 10.0

    #: P0.5 - path to the new, isolated per-camera entity-role mapping
    #: file (see module docstring above). Reloadable, same "point a test
    #: at a temp file" seam `AutomationEngine._rules_path` already
    #: established.
    cameras_path: str = DEFAULT_CAMERAS_PATH

    @classmethod
    def from_env(cls) -> "CameraAutomationConfig":
        defaults = cls()
        return cls(
            enabled=_bool_env("CAMERA_AUTOMATION_ENABLED", defaults.enabled),
            entities=_split(os.getenv("CAMERA_AUTOMATION_ENTITIES", "")) or defaults.entities,
            cooldown_s=float(os.getenv("CAMERA_AUTOMATION_COOLDOWN_S", defaults.cooldown_s)),
            cameras_path=os.getenv("CAMERA_AUTOMATION_CAMERAS_PATH", defaults.cameras_path),
        )
