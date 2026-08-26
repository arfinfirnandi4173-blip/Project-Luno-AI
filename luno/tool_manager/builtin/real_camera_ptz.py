"""
real_camera_ptz.py
====================

`RealCameraPTZHandler` - controls a real TP-Link Tapo pan/tilt camera
(e.g. C212) via the `pytapo` library (`pip install pytapo`,
https://github.com/JurajNyiri/pytapo). Same "future integration" slot
`camera_ptz.py`'s own docstring sketches out - swapping it in is exactly:

    registry.register("camera_ptz", RealCameraPTZHandler(tapo_client))

No Planner or Tool Manager changes required (see the package docstring
in `__init__.py`). Opt-in only, wired by `luno/bootstrap/adapters.py`
ONLY when `CAMERA_PTZ_BACKEND=real` AND `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` are all set - `luno/tool_manager/builtin/__init__.py::
register_all()` keeps registering `MockCameraPTZHandler` by default,
exactly as before, for every other case.

--------------------------------------------------------------------
HONEST LIMITATION - no verified execution (unlike Home Assistant)
--------------------------------------------------------------------
The Reliability/"Verified Smart Home Execution" pattern this project
uses for `real_home_assistant.py` (call the service, then re-READ the
device's real state to confirm it actually changed) is NOT possible
here: `pytapo.Tapo` has no method that returns the camera's current
pan/tilt angle (no `getCurrentPosition()`-equivalent exists in the
library's own API - confirmed against its source, see `getMotorCapability()`
which only returns the motor's configured RANGE, not its live position).
This handler is therefore deliberately honest about what it can and
cannot claim:

  - `execute()` reports SUCCESS only once `pytapo` has returned without
    raising - i.e. "the camera ACCEPTED the command" - never "the
    camera is now pointed at X degrees" (which cannot be verified).
  - Every success message says "I've sent a command to ..." rather than
    "I've moved the camera to ...", matching this project's own
    "never claim success for something that wasn't actually confirmed"
    rule as closely as the underlying hardware/library allows.
  - A failure (network error, wrong credentials, camera offline) is
    always reported honestly with the real exception text - never
    silently swallowed.

`center` calls `calibrateMotor()`, which per Tapo's own documented
behavior physically re-homes the camera to its default center position -
the one PTZ action this handler can describe with a LITTLE more
confidence ("I've centered the camera"), since the physical endpoint is
a known, fixed reference position rather than an arbitrary relative
delta - but still not independently re-verified by reading anything
back.

--------------------------------------------------------------------
Named-target aiming ("arahkan kamera ke pintu") - `save_preset`/`goto_preset`
--------------------------------------------------------------------
Because there is no absolute position readback (see above), Luno cannot
compute "point at the door" from nothing - it can only recall a position
that was saved once while the camera happened to already be pointed the
right way, exactly like Tapo's own app. This handler defers entirely to
the camera's OWN firmware-side preset storage via `pytapo`:

  - `save_preset` (target = a name, e.g. "pintu") calls `savePreset(name)`
    - the camera saves its CURRENT physical position under that name.
  - `goto_preset` (target = that same name) calls `getPresets()` (returns
    `{presetID: name}`), finds the ID whose name matches (case-
    insensitive), and calls `setPreset(id)`. An unrecognized name fails
    honestly, listing whatever presets DO exist, rather than silently
    moving to the wrong place or doing nothing.

Presets saved this way live ON THE CAMERA (visible/editable in the Tapo
app too, and vice versa - saving one via the app makes it immediately
recallable by voice/text here, since `getPresets()` is read fresh every
call, not cached).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = ["pan_left", "pan_right", "tilt_up", "tilt_down", "center", "goto_preset", "save_preset"]
#: Actions that require a `target` (a preset name) - every other action
#: acts on the single fixed camera and needs no target at all. Note this
#: is a CLOSED list: `target` is only ever interpreted as a preset NAME
#: (looked up against the camera's own `getPresets()`), never as a host,
#: URL, or path fragment - see `classify_tapo_exception()`'s module
#: docstring section below for why that distinction matters for Sprint
#: "Tapo C212 Authentication" Phase 7 (no arbitrary-target host injection).
_ACTIONS_REQUIRING_TARGET = {"goto_preset", "save_preset"}


# ============================================================================
# Sprint "Tapo C212 Authentication & Connection Recovery" - error
# classification layer
# ============================================================================
#
# Added by the Tapo C212 sprint. BEFORE this, every failure from the
# underlying `pytapo` client - network-unreachable, wrong credentials,
# expired session, or a genuine API-level rejection - was caught by one
# broad `except Exception` and reported with the exact same generic
# `error_type="CameraPTZError"`, indistinguishable from one another. The
# brief's Phase 1 forensic trace (see `docs/change_impact/
# tapo_c212_authentication.md`) confirmed this file never itself
# constructs or emits the literal string "disconnect"/"disconnected"
# anywhere - that word is a `luno/dashboard/static/index.html` badge for
# `luno.vision`'s OWN, SEPARATE `CameraState` classification (RTSP/OpenCV
# capture, driven by the same TAPO_* credentials via `config.CAMERA_URL`
# auto-derivation, but a structurally distinct code path from this PTZ
# tool). What THIS file's failures can legitimately surface instead - if
# the underlying HTTP connection to the camera is reset mid-request - is
# Python's own `http.client.RemoteDisconnected` / `urllib3`'s
# `ProtocolError('Connection aborted.', RemoteDisconnected(...))`, which
# DOES contain the substring "disconnect" for a completely different,
# also-legitimate reason (see `_UNREACHABLE_TEXT_MARKERS` below).
#
# `classify_tapo_exception()` maps a raised exception to one of a small,
# closed set of categories, using ONLY evidence gathered by directly
# reading the installed `pytapo` (3.4.18) library's own source - never an
# invented endpoint or protocol:
#
#   - `pytapo/const.py::ERROR_CODES` - the library's own dict of Tapo API
#     error codes to human-readable messages (e.g. -40209 "Invalid login
#     credentials", -40401 "Invalid stok value", -1007 "DEVICE_OFFLINE").
#   - `pytapo/transport/pytapo/const.py::AUTH_ERROR_CODES`/
#     `RETRYABLE_ERROR_CODES` - the library's own auth-vs-retryable split.
#   - `pytapo/transport/klap/klap.py::authenticate()` - raises the exact,
#     stable string "Invalid authentication data" (from `python-kasa`'s
#     `AuthenticationError`) for BOTH KLAP v1 and v2 attempts.
#   - `pytapo/transport/pytapo/pytapo.py` - raises
#     f"Temporary Suspension: Try again in {sec_left} seconds" on the
#     legacy transport's own anti-bruteforce lockout.
#
# Deliberately backward compatible: an exception that matches NONE of
# these known markers keeps the original, pre-sprint `error_type=
# "CameraPTZError"` - this is exactly what happens for e.g. a synthetic
# `RuntimeError("simulated camera offline")` in a test, or any other
# error this classifier has no evidence-based reason to name more
# specifically. Nothing is ever guessed.
class TapoErrorClass:
    """Closed set of classification outcomes (Phase 2's category list).
    Deliberately plain string constants (not an Enum) - this project's
    `ToolResult.error_type` is already a free-form string field (see
    `result.py`), and every OTHER consumer of an error category in this
    codebase (`luno.vision.CameraState`, `ARCHITECTURE_GUARD.md`'s own
    conventions) treats "does this string exactly match" as the contract,
    not "is this a member of a specific Enum type" - keeping this a
    plain string keeps `classify_tapo_exception()` trivially safe to
    call from `luno/bootstrap/adapters.py` too, with zero import-time
    coupling beyond this one module."""

    HOST_UNREACHABLE = "HOST_UNREACHABLE"
    PORT_UNREACHABLE = "PORT_UNREACHABLE"
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    API_REJECTED = "API_REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class _Classification:
    category: str
    error_type: str
    retryable: bool


# Evidence sources for every marker below are cited in the module comment
# above - none of these strings are invented. Matched case-insensitively
# against `str(exception)`.
_AUTH_TEXT_MARKERS = (
    "invalid authentication data",   # klap.py AuthenticationError (KLAP v1 + v2)
    "invalid login credentials",     # ERROR_CODES[-40209]
    "tpap_authentication_failed",    # ERROR_CODES[-40418]
    "authentication failed",         # generic rendering of the above
    "digest_authorize_fail",         # ERROR_CODES[-3]
    "unauthorized",                  # ERROR_CODES[401]
)
_SESSION_EXPIRED_TEXT_MARKERS = (
    "invalid stok value",            # ERROR_CODES[-40401]
    "invalid_nonce",                 # ERROR_CODES[-40413]
    "tpap_session_token_invalid",    # ERROR_CODES[-40421]
)
_RATE_LIMITED_TEXT_MARKERS = (
    "temporary suspension",          # legacy transport's own lockout message
)
_DEVICE_OFFLINE_TEXT_MARKERS = (
    "device_offline",                # ERROR_CODES[-1007]
    "error_device_offline",          # ERROR_CODES[-20002]
)
# Network-layer signals - deliberately checked by EXCEPTION TYPE NAME
# (not `isinstance`, so this module needs zero import of `requests`/
# `socket`/`pytapo` to classify) plus a small set of well-known message
# substrings that those same exception types stably produce.
_UNREACHABLE_EXC_TYPE_NAMES = {
    "ConnectionError", "ConnectionResetError", "gaierror", "timeout",
    "TimeoutError", "ReadTimeout", "ConnectTimeout", "Timeout",
    "RemoteDisconnected", "ProtocolError", "MaxRetryError", "OSError",
}
_PORT_UNREACHABLE_TEXT_MARKERS = ("connection refused",)
_HOST_UNREACHABLE_TEXT_MARKERS = (
    "name or service not known", "no route to host", "network is unreachable",
    "nodename nor servname provided", "timed out", "remote end closed connection",
    "connection aborted", "max retries exceeded",
)


def classify_tapo_exception(ex: BaseException) -> _Classification:
    """Evidence-based classification only - see the module comment above
    for exactly which pytapo source locations each marker comes from.
    Falls back to the pre-sprint generic `CameraPTZError`/UNKNOWN for
    anything unrecognized, so this is purely additive: it can only make
    an already-honest failure message MORE specific, never change
    whether `execute()` reports success or failure."""
    text = str(ex).lower()
    type_name = type(ex).__name__

    if any(m in text for m in _RATE_LIMITED_TEXT_MARKERS):
        return _Classification(TapoErrorClass.AUTH_RATE_LIMITED, "CameraPTZAuthRateLimited", retryable=False)
    if any(m in text for m in _SESSION_EXPIRED_TEXT_MARKERS):
        return _Classification(TapoErrorClass.SESSION_EXPIRED, "CameraPTZSessionExpired", retryable=True)
    if any(m in text for m in _AUTH_TEXT_MARKERS):
        return _Classification(TapoErrorClass.AUTH_FAILED, "CameraPTZAuthFailed", retryable=False)
    if any(m in text for m in _DEVICE_OFFLINE_TEXT_MARKERS):
        return _Classification(TapoErrorClass.DEVICE_OFFLINE, "CameraPTZUnreachable", retryable=True)
    if any(m in text for m in _PORT_UNREACHABLE_TEXT_MARKERS):
        return _Classification(TapoErrorClass.PORT_UNREACHABLE, "CameraPTZUnreachable", retryable=True)
    if type_name in _UNREACHABLE_EXC_TYPE_NAMES or any(m in text for m in _HOST_UNREACHABLE_TEXT_MARKERS):
        return _Classification(TapoErrorClass.HOST_UNREACHABLE, "CameraPTZUnreachable", retryable=True)
    return _Classification(TapoErrorClass.UNKNOWN, "CameraPTZError", retryable=True)


def _redact_credentials(text: str) -> str:
    """Defense-in-depth for Phase 7's "password must never enter logs/
    exceptions/... " requirement. Direct source review of both pytapo
    transports (legacy MD5/SHA256-digest and KLAP) confirmed neither
    ever embeds the raw plaintext password in an exception message (both
    hash it before use; the legacy transport even has its own explicit
    `redactedKwargsData["params"]["password"] = "REDACTED"` for its OWN
    debug logging) - so this is a belt-and-suspenders backstop against a
    future pytapo regression, not evidence of a current leak. Reads the
    configured credential values fresh (never caches them), and redacts
    by VALUE match only - never logs or returns the value itself."""
    try:
        from luno import config as legacy_config
    except Exception:
        return text
    for secret in (getattr(legacy_config, "TAPO_PASSWORD", None), getattr(legacy_config, "TAPO_USERNAME", None)):
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


# ============================================================================
# Sprint "Tapo C212 Live Authentication & Auto-Recovery" - bounded
# in-memory connection state + recovery
# ============================================================================
#
# Added by Sprint 70, on top of the classification layer above.
# EXPLICITLY reuses the existing single-client, single-lock architecture -
# this is NOT a second camera connection system: `RealCameraPTZHandler`
# still owns exactly one `self._client` at a time, still goes through the
# exact same `self._lock`, still calls into `pytapo` the exact same way.
# The only new capability is: when a call fails with a RECOVERABLE
# classification (a session that expired, or a transient network blip -
# see `_RECOVERABLE_CATEGORIES` below), the handler may rebuild its OWN
# `self._client` (via an optional `client_factory` callable, supplied by
# `luno/bootstrap/adapters.py` and capturing nothing beyond the same
# `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` values already used for the
# very first construction) and retry the SAME command EXACTLY ONCE more
# before giving up - never a loop, never unbounded, never for a failure
# class where retrying cannot possibly help (wrong credentials, a rate
# limit lockout, or an unclassified/unknown failure - see Phase 4's own
# explicit per-category policy in `docs/change_impact/
# tapo_c212_live_recovery.md`).
#
# Backward compatible by construction: `client_factory` defaults to
# `None`, and every recovery attempt is gated on it being set - a caller
# that constructs `RealCameraPTZHandler(client)` exactly as before (every
# pre-Sprint-70 test does) gets IDENTICAL behavior to before this sprint:
# one call, one classified failure, no retry.
class PTZConnectionState:
    """Coarse, in-memory-only connection state for the Tapo PTZ client -
    deliberately a SMALLER set than `TapoErrorClass` above (which stays
    the fine-grained, per-exception classification used for `error_type`/
    `data["error_class"]`). This is a per-HANDLER-INSTANCE attribute
    (`self._connection_state`), never a module-level global and never
    persisted to disk - see `connection_state()` below for the read-only
    accessor. Values match the brief's own Phase 3 list exactly."""

    DISCONNECTED = "DISCONNECTED"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTH_FAILED = "AUTH_FAILED"
    DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"


#: Exactly one reconnect+retry, ever, per `_invoke()` call - a plain
#: constant (not a loop bound) because `_invoke()` below contains no
#: loop at all: this is the STRONGEST possible "no infinite loop"
#: guarantee, statically verifiable by `test_R_..._no_loop_construct...`
#: in the Sprint 70 test file (an AST guard, same spirit as Sprint 69's
#: own single-call-site regression check).
_MAX_RECONNECT_ATTEMPTS = 1

#: Categories where a rebuild-the-client-and-retry-once is plausible:
#: an expired session simply needs a fresh authenticated client, and a
#: transient network hiccup may have cleared by the time of the retry.
#: Deliberately EXCLUDES `AUTH_FAILED` (wrong credentials will still be
#: wrong), `AUTH_RATE_LIMITED` (retrying immediately is actively
#: counterproductive - the brief's own "stop retrying and report
#: clearly"), and `UNKNOWN` (an unrecognized failure gets no special
#: handling - the brief's own "preserve safe failure behavior").
_RECOVERABLE_CATEGORIES = frozenset({
    TapoErrorClass.SESSION_EXPIRED,
    TapoErrorClass.DEVICE_OFFLINE,
    TapoErrorClass.PORT_UNREACHABLE,
    TapoErrorClass.HOST_UNREACHABLE,
})


def _category_to_connection_state(category: str) -> str:
    """Maps a `TapoErrorClass` (fine-grained, per-exception) to a
    `PTZConnectionState` (coarse, per-handler). `UNKNOWN` maps to
    `DISCONNECTED` - the safest generic "not confirmed connected"
    bucket, never guessed further than the evidence supports."""
    if category == TapoErrorClass.SESSION_EXPIRED:
        return PTZConnectionState.SESSION_EXPIRED
    if category in (TapoErrorClass.AUTH_FAILED, TapoErrorClass.AUTH_RATE_LIMITED):
        return PTZConnectionState.AUTH_FAILED
    if category in (TapoErrorClass.DEVICE_OFFLINE, TapoErrorClass.PORT_UNREACHABLE, TapoErrorClass.HOST_UNREACHABLE):
        return PTZConnectionState.DEVICE_UNREACHABLE
    return PTZConnectionState.DISCONNECTED


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class _PTZConfig:
    """Read fresh (not cached) on every `execute()` call - same
    reloadable-without-a-restart precedent `real_home_assistant.py`'s own
    `_VerifyConfig.from_env()` already established for this package."""
    pan_step_degrees: float = 15.0
    tilt_step_degrees: float = 15.0
    invert_pan: bool = False
    invert_tilt: bool = False

    @classmethod
    def from_env(cls) -> "_PTZConfig":
        return cls(
            pan_step_degrees=_float_env("TAPO_PAN_STEP_DEGREES", 15.0),
            tilt_step_degrees=_float_env("TAPO_TILT_STEP_DEGREES", 15.0),
            invert_pan=_bool_env("TAPO_INVERT_PAN", False),
            invert_tilt=_bool_env("TAPO_INVERT_TILT", False),
        )


class RealCameraPTZHandler(ToolHandler):
    name = "camera_ptz"
    default_timeout_s = 10.0
    max_timeout_s = 20.0

    def __init__(self, client: Any, client_factory: Optional[Any] = None) -> None:
        """`client` - a `pytapo.Tapo` instance (or anything duck-typed
        with the same sync `moveMotor(x, y)`/`calibrateMotor()` methods -
        kept as `Any` rather than importing `pytapo` here so this module
        has zero import-time dependency on that library; only
        `luno/bootstrap/adapters.py` needs it installed to construct the
        real client).

        `client_factory` (Sprint 70, OPTIONAL) - a zero-arg callable that,
        when called, returns a freshly (re)constructed client - used
        ONLY for the bounded, single-retry recovery described in the
        module comment above. `None` (the default, and what every
        pre-Sprint-70 caller/test still passes) means recovery is simply
        unavailable - behavior is then IDENTICAL to before this sprint."""
        self._client = client
        self._client_factory = client_factory
        self._lock = threading.Lock()
        # Assume an already-constructed client implies its own
        # constructor already authenticated successfully (true for real
        # `pytapo.Tapo` - see the classification-layer module comment on
        # `Tapo.__init__()` performing real, synchronous auth) - a `None`
        # client (never used by any real caller, only a hypothetical)
        # starts DISCONNECTED instead of assuming success.
        self._connection_state = PTZConnectionState.CONNECTED if client is not None else PTZConnectionState.DISCONNECTED

    def connection_state(self) -> str:
        """Read-only accessor for the handler's current, in-memory-only
        `PTZConnectionState` - deliberately NOT wired into the dashboard
        (see Phase 6 of `docs/change_impact/tapo_c212_live_recovery.md`
        for why: `camera_ptz` is a ToolManager TOOL, not a dashboard
        adapter, and there is no existing safe shared abstraction to
        unify it with `luno.vision`'s own, separate `CameraState` without
        fabricating a connectivity claim this layer cannot actually
        back up for the STREAMING path)."""
        return self._connection_state

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
        cfg = _PTZConfig.from_env()
        with self._lock:
            if action == "pan_left":
                return self._move(action, x=-self._signed(cfg.pan_step_degrees, cfg.invert_pan), y=0.0, verb="panned the camera left")
            if action == "pan_right":
                return self._move(action, x=self._signed(cfg.pan_step_degrees, cfg.invert_pan), y=0.0, verb="panned the camera right")
            if action == "tilt_up":
                return self._move(action, x=0.0, y=self._signed(cfg.tilt_step_degrees, cfg.invert_tilt), verb="tilted the camera up")
            if action == "tilt_down":
                return self._move(action, x=0.0, y=-self._signed(cfg.tilt_step_degrees, cfg.invert_tilt), verb="tilted the camera down")
            if action == "center":
                return self._center()
            if action == "save_preset":
                return self._save_preset(tool_call.target.strip())
            if action == "goto_preset":
                return self._goto_preset(tool_call.target.strip())

        # Unreachable given the base class's validate() already restricts
        # action to supported_actions(), kept as a defensive fallback.
        return ToolResult.fail(self.name, action, f"Unhandled action '{action}'")

    @staticmethod
    def _signed(step: float, invert: bool) -> float:
        return -step if invert else step

    def _invoke(self, method_name: str, *args: Any) -> Any:
        """Calls `method_name(*args)` on `self._client`, returning its
        result. Sprint 70's ONLY new runtime behavior: on a RECOVERABLE
        classified failure (see `_RECOVERABLE_CATEGORIES`) with a
        `client_factory` configured, rebuilds `self._client` and retries
        the SAME call EXACTLY ONCE more (`_MAX_RECONNECT_ATTEMPTS = 1` -
        there is no loop here at all, so "at most once" is structural,
        not counted). Every other outcome - success, a non-recoverable
        failure, no factory configured, or a failure that survives the
        retry - propagates exactly as `execute()`'s callers already
        expect (an exception the caller's own `try/except` catches and
        classifies/redacts/reports, unchanged from before this sprint).
        Updates `self._connection_state` at every transition - the
        "connection check -> authenticate/recover if safely possible"
        step Phase 5 asks for happens transparently HERE, inside the
        existing per-action `try/except`, never bypassing it."""
        try:
            result = getattr(self._client, method_name)(*args)
            self._connection_state = PTZConnectionState.CONNECTED
            return result
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            self._connection_state = _category_to_connection_state(classified.category)
            if classified.category not in _RECOVERABLE_CATEGORIES or self._client_factory is None:
                raise
            # Exactly one bounded reconnect + retry (_MAX_RECONNECT_ATTEMPTS) - no loop.
            self._connection_state = PTZConnectionState.AUTHENTICATING
            try:
                new_client = self._client_factory()
            except Exception:
                # The reconnect ITSELF failed - report the ORIGINAL
                # classified failure (what the caller actually asked
                # for), not the reconnect attempt's own exception.
                self._connection_state = _category_to_connection_state(classified.category)
                raise ex
            self._client = new_client
            try:
                result = getattr(self._client, method_name)(*args)
                self._connection_state = PTZConnectionState.CONNECTED
                return result
            except Exception as retry_ex:
                retry_classified = classify_tapo_exception(retry_ex)
                self._connection_state = _category_to_connection_state(retry_classified.category)
                raise retry_ex

    def _move(self, action: str, x: float, y: float, verb: str) -> ToolResult:
        try:
            self._invoke("moveMotor", x, y)
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            return ToolResult.fail(
                self.name, action, f"Couldn't move the camera - {_redact_credentials(str(ex))}",
                error_type=classified.error_type, retryable=classified.retryable,
                data={"pan_delta": x, "tilt_delta": y, "error_class": classified.category,
                      "connection_state": self._connection_state},
            )
        # HONEST: "I've sent a command to ..." not "I've moved the camera
        # to ..." - see module docstring's "no verified execution" section.
        return ToolResult.ok(
            self.name, action, f"I've sent a command to the camera - it should have {verb}.",
            data={"pan_delta": x, "tilt_delta": y},
        )

    def _center(self) -> ToolResult:
        try:
            self._invoke("calibrateMotor")
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            return ToolResult.fail(
                self.name, "center", f"Couldn't center the camera - {_redact_credentials(str(ex))}",
                error_type=classified.error_type, retryable=classified.retryable,
                data={"error_class": classified.category, "connection_state": self._connection_state},
            )
        return ToolResult.ok(self.name, "center", "I've centered the camera.")

    def _save_preset(self, name: str) -> ToolResult:
        try:
            self._invoke("savePreset", name)
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            return ToolResult.fail(
                self.name, "save_preset", f"Couldn't save this position as '{name}' - {_redact_credentials(str(ex))}",
                error_type=classified.error_type, retryable=classified.retryable,
                data={"error_class": classified.category, "connection_state": self._connection_state},
            )
        return ToolResult.ok(
            self.name, "save_preset", f"I've saved the camera's current position as '{name}'.",
            data={"preset": name},
        )

    def _goto_preset(self, name: str) -> ToolResult:
        try:
            presets = self._invoke("getPresets")  # {presetID: name}
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            return ToolResult.fail(
                self.name, "goto_preset", f"Couldn't read the camera's saved positions - {_redact_credentials(str(ex))}",
                error_type=classified.error_type, retryable=classified.retryable,
                data={"error_class": classified.category, "connection_state": self._connection_state},
            )
        wanted = name.strip().lower()
        matched_id = next(
            (preset_id for preset_id, preset_name in (presets or {}).items() if str(preset_name).strip().lower() == wanted),
            None,
        )
        if matched_id is None:
            known = ", ".join(sorted({str(n) for n in (presets or {}).values()})) or "none saved yet"
            return ToolResult.fail(
                self.name, "goto_preset",
                f"I don't have a saved position called '{name}' - saved positions: {known}",
                error_type="CameraPTZError", retryable=False,
            )
        try:
            self._invoke("setPreset", matched_id)
        except Exception as ex:
            classified = classify_tapo_exception(ex)
            return ToolResult.fail(
                self.name, "goto_preset", f"Couldn't move to '{name}' - {_redact_credentials(str(ex))}",
                error_type=classified.error_type, retryable=classified.retryable,
                data={"error_class": classified.category, "connection_state": self._connection_state},
            )
        return ToolResult.ok(
            self.name, "goto_preset", f"I've sent a command to the camera - it should now be pointed at {name}.",
            data={"preset": name, "preset_id": matched_id},
        )
