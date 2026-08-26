"""
camera_ptz.py
==============

MOCK pan/tilt camera control handler - same role `home_assistant.py`'s
`MockHomeAssistantHandler` plays for smart home devices: a small,
internally-consistent in-memory stand-in (tracks a simulated pan/tilt
"position" purely so repeated calls behave sensibly in tests/demos), not
talking to any real camera.

FUTURE INTEGRATION: `RealCameraPTZHandler` (see `real_camera_ptz.py`)
subclasses `ToolHandler` the same way, implements the same
`supported_actions()`, and calls the `pytapo` library (TP-Link Tapo
cameras, e.g. C212) inside `execute()` instead of touching `_position`.
Swapping it in is exactly:

    registry.register("camera_ptz", RealCameraPTZHandler(tapo_client))

No Planner or Tool Manager changes required - see the package docstring
in `__init__.py`.

--------------------------------------------------------------------
Named-target aiming ("arahkan kamera ke pintu") - `save_preset`/`goto_preset`
--------------------------------------------------------------------
The camera has no absolute position readback (see `real_camera_ptz.py`'s
own "HONEST LIMITATION" section) - `pan_left`/`pan_right`/`tilt_up`/
`tilt_down` are all RELATIVE deltas. There is therefore no way for Luno
to reliably compute "point at the door" from scratch; the only correct
mechanism is the SAME one Tapo's own app uses: a PRESET, saved once
while the camera happens to already be pointed the right way, recalled
by name later. `save_preset` (target = a name, e.g. "pintu") captures
the CURRENT position under that name; `goto_preset` (target = that same
name) recalls it. This mock stores presets as plain `(pan, tilt)` pairs
in memory; the real handler defers entirely to the camera's own
firmware-side preset storage via `pytapo`'s `savePreset()`/
`setPreset()`/`getPresets()` - see that module's own docstring.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["pan_left", "pan_right", "tilt_up", "tilt_down", "center", "goto_preset", "save_preset"]
#: Actions that require a `target` (a preset name) - every other action
#: acts on the single fixed camera and needs no target at all.
_ACTIONS_REQUIRING_TARGET = {"goto_preset", "save_preset"}

#: Simulated step size in degrees - purely for the mock's own internal
#: "position" bookkeeping, has no bearing on the real handler's
#: TAPO_PAN_STEP_DEGREES/TAPO_TILT_STEP_DEGREES config.
_MOCK_STEP_DEGREES = 15.0


class MockCameraPTZHandler(ToolHandler):
    name = "camera_ptz"
    default_timeout_s = 5.0
    max_timeout_s = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pan = 0.0
        self._tilt = 0.0
        self._presets: Dict[str, Tuple[float, float]] = {}

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action in _ACTIONS_REQUIRING_TARGET and not (tool_call.target or "").strip():
            return f"Action '{tool_call.action}' needs a target (a preset name)"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        action = tool_call.action
        with self._lock:
            if action == "pan_left":
                self._pan -= _MOCK_STEP_DEGREES
                return ToolResult.ok(self.name, action, f"[MOCK] Panned camera left to {self._pan:.0f}°", data={"pan": self._pan, "tilt": self._tilt})

            if action == "pan_right":
                self._pan += _MOCK_STEP_DEGREES
                return ToolResult.ok(self.name, action, f"[MOCK] Panned camera right to {self._pan:.0f}°", data={"pan": self._pan, "tilt": self._tilt})

            if action == "tilt_up":
                self._tilt += _MOCK_STEP_DEGREES
                return ToolResult.ok(self.name, action, f"[MOCK] Tilted camera up to {self._tilt:.0f}°", data={"pan": self._pan, "tilt": self._tilt})

            if action == "tilt_down":
                self._tilt -= _MOCK_STEP_DEGREES
                return ToolResult.ok(self.name, action, f"[MOCK] Tilted camera down to {self._tilt:.0f}°", data={"pan": self._pan, "tilt": self._tilt})

            if action == "center":
                self._pan = 0.0
                self._tilt = 0.0
                return ToolResult.ok(self.name, action, "[MOCK] Camera centered", data={"pan": 0.0, "tilt": 0.0})

            if action == "save_preset":
                name = tool_call.target.strip()
                self._presets[name.lower()] = (self._pan, self._tilt)
                return ToolResult.ok(
                    self.name, action, f"[MOCK] Saved current position as '{name}'",
                    data={"preset": name, "pan": self._pan, "tilt": self._tilt},
                )

            if action == "goto_preset":
                name = tool_call.target.strip()
                saved = self._presets.get(name.lower())
                if saved is None:
                    known = ", ".join(sorted(self._presets)) or "none saved yet"
                    return ToolResult.fail(
                        self.name, action, f"[MOCK] No saved position called '{name}' (known: {known})",
                        error_type="CameraPTZError", retryable=False,
                    )
                self._pan, self._tilt = saved
                return ToolResult.ok(
                    self.name, action, f"[MOCK] Moved to saved position '{name}'",
                    data={"preset": name, "pan": self._pan, "tilt": self._tilt},
                )

        # Unreachable given validate() already restricts action to
        # _SUPPORTED_ACTIONS, kept as a defensive fallback.
        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")
