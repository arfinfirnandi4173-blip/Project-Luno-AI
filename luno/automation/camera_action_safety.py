"""
camera_action_safety.py
========================

LUNO P0.8.0 (Camera Automation -> Home Assistant Action Safety Pipeline).

A small, pure, isolated validation function - `validate_camera_ha_
action()` - plus one small result dataclass (`SafetyCheckResult`). This
module contains NO Event Bus subscription, NO Home Assistant client, NO
HTTP/WebSocket call of any kind, and NO camera/vision code. It is called
from exactly one place (`luno/automation/engine.py::_dispatch_home_
assistant_action()`), and ONLY when the rule being executed was
triggered by the camera automation event (`camera_automation.
camera_event`) - every other automation rule (time-based, manual,
non-camera event-based) is completely unaffected by this module's
existence.

--------------------------------------------------------------------
Why this exists (P0.8.0 brief Section 2)
--------------------------------------------------------------------
Before P0.8.0, `_dispatch_home_assistant_action()` forwarded every
`home_assistant.turn_on`/`turn_off` action straight to the existing
`tool_requested` -> `ToolManagerBridgeModule` -> `ToolManager` round trip
(the same path a manual voice command and Sprint 71's own
`CameraPatrolModule` already use) with no camera-specific validation at
all. That is fine for a human-issued voice command (a person is right
there deciding whether to say it), but is not yet safe for a
camera-triggered action that fires autonomously off a computer-vision
detection - a stale/duplicate event, a detector failure that looks like
"no error", or a malformed `CameraEvent` must never be able to flip a
real light. This module is the one, small, additive gate that sits
between "AutomationEngine decided to run a camera rule's HA action" and
"the existing dispatcher actually sends it."

--------------------------------------------------------------------
Reused, not reinvented (P0.8.0 brief Section 1/4/5/12)
--------------------------------------------------------------------
- Cooldown/duplicate protection: this module has NO cooldown logic of
  its own. `AutomationEngine._trigger()`'s existing `_cooldown_until`
  check (Sprint 72, Phase 8) already refuses a rule re-trigger while its
  `cooldown_seconds` window is open - three `human_detected` events in a
  row already only run the rule once per cooldown window, with zero
  changes needed here.
- State-aware "already in the desired state -> skip": this module never
  opens its own connection to Home Assistant to find out. It accepts an
  OPTIONAL `ha_state_reader: Callable[[str], Optional[str]]` argument -
  when the caller (`AutomationEngine`) has one wired (from the EXISTING
  `RealHomeAssistantClient.get_entity_state()`, the same reused,
  already-established real client `RealHomeAssistantHandler._safe_get_
  state()` already calls for its own "already ON" shortcut - see that
  module's own `_execute_on_off()`), this function calls it; when it is
  `None` (mock backend, or not yet wired), the state-aware check is
  simply skipped - proceeding to allow, never blocking on a check that
  was never available in the first place. No new HA API client, no
  direct WebSocket/REST call, ever originates from this file.
- Entity/action allowlisting: `home_assistant.turn_on`/`turn_off` are
  already the only two Home Assistant action types this project's
  automation domain model (`models.py::ACTION_TYPES`) exposes to ANY
  rule - this module's own `CAMERA_HA_ACTION_TYPES` constant is the same
  two-member set, re-checked here defensively (defense in depth, same
  precedent `engine.py::_dispatch_action()`'s own `ACTION_TYPES`
  re-check already established) rather than trusted blindly.

--------------------------------------------------------------------
Fail-closed discipline (P0.8.0 brief Section 7)
--------------------------------------------------------------------
Every check below is a simple, independent, ordered gate - the FIRST
one that fails wins and the action is refused; nothing here ever infers
a "safe" default when information is missing or malformed. In
particular (Section 3D's own explicit warning, echoing the same
principle P0.6.2-FIX/P0.6.3/P0.7 already established one layer down in
the Vision pipeline): a `detection_error` or an offline/unavailable
camera is NEVER interpreted as "no human present" - this module does
not even look at `human_present`/`person_count` to decide that; it
simply refuses the action outright and lets the NEXT genuinely healthy
camera event (if any) drive automation again.

A wired `ha_state_reader` that raises, or that this function otherwise
cannot make sense of, is ALSO treated as a fail-closed condition (P0.8.0
brief Section 7 item 7: "HA state lookup failure ... must result in NO
device action") - NOT the same as the reader being entirely absent
(which is a legitimate "this optimization isn't available right now,
proceed without it" case, per Section 5's own "if already available"
qualifier).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

#: P0.8.0 brief Section 3A - "Initially allow only: home_assistant.
#: turn_on, home_assistant.turn_off. Do not add support for arbitrary
#: services." Identical to the two HA members of `models.ACTION_TYPES`
#: today - kept as this module's own small, independent constant
#: (rather than importing `models.ACTION_TYPES` and filtering it) so a
#: future sprint that adds a new HA action type to the general
#: automation engine does NOT silently also grant it to camera-triggered
#: actions without an explicit, separate decision to extend this set.
CAMERA_HA_ACTION_TYPES = frozenset({"home_assistant.turn_on", "home_assistant.turn_off"})

#: P0.8.0 brief Section 3B - a conservative, real-world Home Assistant
#: entity id shape: lowercase ASCII domain, a literal '.', lowercase
#: ASCII/digits/underscore object id. Deliberately stricter than
#: `models.py::validate_action()`'s own pre-existing "any non-empty
#: string, not '*'" check (that check runs once, at rule LOAD time, for
#: every HA action type this project has ever had; this one runs at
#: EXECUTION time, specifically for autonomously-triggered camera
#: actions, so it can afford to be the stricter of the two without
#: breaking any existing rule - every entity id already used by any
#: existing rule/config in this project, e.g. "light.wled", already
#: matches this pattern).
_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

#: Camera-event `kind` values that unambiguously mean "the camera itself
#: is not usable right now" - Section 3D/7's own "camera explicitly
#: offline" case. `human_cleared`/`human_detected` are NOT included here
#: on purpose (a `human_cleared` event is a NORMAL, healthy signal that
#: should be free to drive a `turn_off` rule - it is not itself a
#: failure condition).
_CAMERA_OFFLINE_KINDS = frozenset({"camera_offline"})

_ACTION_TO_DESIRED_STATE = {"home_assistant.turn_on": "on", "home_assistant.turn_off": "off"}


@dataclass(frozen=True)
class SafetyCheckResult:
    """`allowed=False` -> the caller must refuse the action outright
    (`code`/`message` explain why - both safe to log/publish, never a
    credential or raw exception object). `allowed=True, skip_dispatch=
    True` -> the action is safe to consider "done" WITHOUT actually
    calling the existing HA dispatcher (Section 5's "already in the
    desired state" shortcut) - the caller should record this as a
    completed, no-op action, never as a refusal (nothing was unsafe
    about it, it was simply unnecessary). `allowed=True, skip_dispatch=
    False` -> proceed to the existing, unmodified HA dispatch path
    exactly as before this sprint."""
    allowed: bool
    code: str
    message: str
    skip_dispatch: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "message": self.message, "skip_dispatch": self.skip_dispatch}


def _refuse(code: str, message: str) -> SafetyCheckResult:
    return SafetyCheckResult(allowed=False, code=code, message=message)


def validate_camera_ha_action(
    action_type: str,
    target: Any,
    event_data: Optional[Dict[str, Any]],
    ha_state_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> SafetyCheckResult:
    """Pure (aside from the single optional `ha_state_reader` call) and
    synchronous - safe to call directly from `AutomationEngine`'s own
    execution thread with no locking of its own needed (it mutates
    nothing).

    `action_type`/`target` - the `AutomationAction` about to be
    dispatched (`action.type`/`action.parameters.get("target")`).
    `event_data` - the triggering `camera_automation.camera_event`'s own
    `.data` dict (the SAME dict `event.<field>` conditions already read -
    P0.6/P0.7), or `None` if this execution somehow has no originating
    event at all (Section 3C/7's own "missing event context" case).
    `ha_state_reader` - see module docstring; `None` by default."""

    # -- A. action type allowlist (Section 3A) -------------------------------
    if action_type not in CAMERA_HA_ACTION_TYPES:
        return _refuse("unsupported_action_type", f"action type {action_type!r} is not allowed for a camera-triggered Home Assistant action")

    # -- B. target / entity id validation (Section 3B) -----------------------
    if target is None:
        return _refuse("invalid_target", "target is missing (null)")
    if isinstance(target, (list, tuple, set)):
        # Section 3B - "multiple targets unless the existing architecture
        # explicitly supports them." It does not (models.py::validate_
        # action() has always required a single string target for every
        # HA action type this project has ever had) - refused, not
        # silently narrowed to "the first one."
        return _refuse("invalid_target", "multiple targets are not supported for a camera-triggered Home Assistant action")
    if not isinstance(target, str) or not target.strip():
        return _refuse("invalid_target", "target is empty or not a string")
    target = target.strip()
    if target == "*":
        return _refuse("invalid_target", "target must not be a wildcard ('*')")
    if not _ENTITY_ID_RE.match(target):
        return _refuse("invalid_target", f"target {target!r} is not a well-formed entity id (expected 'domain.object_id')")

    # -- C. camera event validity (Section 3C) --------------------------------
    if event_data is None or not isinstance(event_data, dict):
        return _refuse("missing_event_context", "no camera event context is available for this execution")
    kind = event_data.get("kind")
    if not kind or not isinstance(kind, str):
        return _refuse("malformed_camera_event", "camera event is missing a valid 'kind'")

    # -- D. vision-state safety (Section 3D) - fail closed, never infer -------
    #    "human_cleared" from a failure/offline signal.
    detection_error = event_data.get("detection_error")
    if detection_error:
        return _refuse("detection_error_present", f"vision detection_error is set ({detection_error!r}) - refusing to infer any state from it")
    if kind in _CAMERA_OFFLINE_KINDS:
        return _refuse("camera_offline", "camera event kind indicates the camera is offline")
    available = event_data.get("available")
    if available is False:
        return _refuse("camera_unavailable", "camera event reports available=false")

    # -- E. state-aware "already in the desired state" skip (Section 5) ------
    #    Optional - only attempted when the caller wired a reader AND this
    #    action type has a known desired on/off state.
    desired_state = _ACTION_TO_DESIRED_STATE.get(action_type)
    if ha_state_reader is not None and desired_state is not None:
        try:
            current_state = ha_state_reader(target)
        except Exception as ex:
            # Section 7 item 7 - a state lookup that was actually
            # ATTEMPTED and failed is a fail-closed condition, distinct
            # from `ha_state_reader is None` (the check simply being
            # unavailable) above.
            return _refuse("ha_state_lookup_failed", f"HA state lookup for {target!r} raised: {ex}")
        if current_state == desired_state:
            return SafetyCheckResult(
                allowed=True, code="already_in_desired_state",
                message=f"{target} is already {desired_state} - skipping the redundant Home Assistant call",
                skip_dispatch=True,
            )

    return SafetyCheckResult(allowed=True, code="ok", message="camera action safety gate passed")
