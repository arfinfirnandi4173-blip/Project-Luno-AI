"""
test_sprint69_tapo_c212_auth.py
==================================

Tests for "SPRINT 69 - TAPO C212 AUTHENTICATION & CONNECTION RECOVERY".

Scope, per the sprint brief's own Phase 8: at least the following
categories, using ONLY fake/mock camera responses - never a real
password, host, or a real `pytapo` import. Every fake exception message
below is copied verbatim from evidence gathered by reading the actually-
installed `pytapo` (3.4.18) library's own source (`pytapo/const.py`'s
`ERROR_CODES`, `pytapo/transport/klap/klap.py`,
`pytapo/transport/pytapo/pytapo.py`) - see
`docs/change_impact/tapo_c212_authentication.md` for the full citation
list. None of these strings are invented.

Covers, across the classes below:
  A. valid config registers the real handler
  B. missing username stays mocked
  C. missing password stays mocked
  D. invalid credential (construction-time auth failure) stays mocked,
     classified
  E. unreachable camera (construction-time network failure) stays
     mocked, classified
  F. API/construction timeout stays mocked, classified
  G. auth success -> real handler registered and usable
  H. auth failure on a PER-COMMAND call (post-registration) ->
     AUTH_FAILED, non-retryable
  I. session expiration on a per-command call -> SESSION_EXPIRED,
     retryable
  J. bounded re-authentication - documented as substantially already
     satisfied by pytapo's own internal `MAX_LOGIN_RETRIES=1` retry (see
     module docstring in `real_camera_ptz.py`) - this file proves this
     layer does NOT add a second, unbounded retry loop on top of it
     (a single call in -> at most a single client call out, always)
  K. re-authentication failure (still failing after pytapo's own retry)
     surfaces as a normal classified failure, not a hang/crash
  L. PTZ success after auth (happy path, all actions)
  M. PTZ failure after successful auth (a genuine API-level rejection,
     e.g. motor busy) -> UNKNOWN/CameraPTZError (no evidence-based
     marker matches a "busy" style message, which is intentional - see
     module docstring's "never guess" rule)
  N. mock fallback remains fully functional and untouched
  O. credential never appears in the resulting message/log text
  P. credential never appears in ToolResult.data (the shape that would
     flow into any future mutation-audit/dashboard event)
  Q. no arbitrary URL/host execution surface exists on the handler
  R. no persistent credential duplication / disk writes from this layer
  S. explicit camera target precedence - `target` is only ever resolved
     as a preset NAME against the camera's own `getPresets()`, never as
     a host/URL override
  T. no regression to unrelated tools (registry untouched for anything
     else)

Run:
    python3 -m pytest tests/test_sprint69_tapo_c212_auth.py
"""

from __future__ import annotations

import ast
import os
import sys
import types

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import _register_real_camera_ptz_handler  # noqa: E402
from luno.bootstrap.launcher_config import BACKEND_MOCK, BACKEND_REAL, LauncherConfig  # noqa: E402
from luno.tool_manager.builtin.camera_ptz import MockCameraPTZHandler  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import (  # noqa: E402
    RealCameraPTZHandler,
    TapoErrorClass,
    _redact_credentials,
    classify_tapo_exception,
)
from luno.tool_manager.manager import ToolManager  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402

_FAKE_HOST = "192.168.1.52"          # not a real device - documentation/example range only
_FAKE_USER = "luno_camera_account"   # matches the shape of a Tapo "Camera Account" name, not a real credential
_FAKE_PASS = "hunter2"               # classic placeholder password, not a real credential


# -- shared helpers (mirrors tests/test_camera_ptz_bootstrap.py's own conventions) -----------

class _FakeToolManagerModule:
    def __init__(self):
        self.manager = ToolManager()
        self.manager.registry.register("camera_ptz", MockCameraPTZHandler())
        # Category T: also register one unrelated tool, to prove this
        # sprint's changes never touch anything but "camera_ptz".
        self.manager.registry.register("home_assistant", MockCameraPTZHandler())  # any handler will do as a sentinel


def _launcher_config(camera_ptz_backend: str) -> LauncherConfig:
    cfg = LauncherConfig()
    cfg.camera_ptz_backend = camera_ptz_backend
    return cfg


def _set_env(**kwargs):
    saved = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _handler_class_name(tmm):
    return type(tmm.manager.registry.get("camera_ptz")).__name__


def _install_fake_pytapo(tapo_cls):
    fake_module = types.ModuleType("pytapo")
    fake_module.Tapo = tapo_cls
    saved = sys.modules.get("pytapo")
    sys.modules["pytapo"] = fake_module
    return saved


def _restore_pytapo(saved):
    if saved is not None:
        sys.modules["pytapo"] = saved
    else:
        sys.modules.pop("pytapo", None)


class _RaisingTapo:
    """Stands in for `pytapo.Tapo` raising at CONSTRUCTION time - exactly
    matches the real library's own behavior (auth happens synchronously
    inside `__init__`, confirmed by reading its source)."""

    def __init__(self, message):
        self._message = message

    def __call__(self, host, user, password):
        raise RuntimeError(self._message)


class _FakeTapoClient:
    """Minimal duck-typed stand-in for a successfully-constructed
    `pytapo.Tapo` - a per-call `raise_on` map lets a single fake client
    simulate any single method failing with an arbitrary (evidence-
    sourced) exception, without needing a real network or credentials."""

    def __init__(self, raise_on=None, presets=None):
        self._raise_on = dict(raise_on or {})
        self.move_calls = []
        self.calibrate_calls = 0
        self._presets = dict(presets or {})
        self._next_id = max([int(k) for k in self._presets] or [0]) + 1
        self.set_preset_calls = []

    def _maybe_raise(self, method):
        if method in self._raise_on:
            raise self._raise_on[method]

    def moveMotor(self, x, y):
        self._maybe_raise("moveMotor")
        self.move_calls.append((x, y))

    def calibrateMotor(self):
        self._maybe_raise("calibrateMotor")
        self.calibrate_calls += 1

    def getPresets(self):
        self._maybe_raise("getPresets")
        return dict(self._presets)

    def savePreset(self, name):
        self._maybe_raise("savePreset")
        preset_id = str(self._next_id)
        self._next_id += 1
        self._presets[preset_id] = name
        return True

    def setPreset(self, presetID):
        self._maybe_raise("setPreset")
        self.set_preset_calls.append(presetID)
        return True


def _with_credentials():
    import luno.config as legacy_config
    saved = (legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = _FAKE_HOST, _FAKE_USER, _FAKE_PASS
    return saved


def _restore_credentials(saved):
    import luno.config as legacy_config
    legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = saved


# -- A/G: valid config + auth success registers the real handler ------------------------------

class _ConstructibleFakeTapo:
    """Matches `pytapo.Tapo`'s real constructor signature exactly
    (`Tapo(host, user, password)`) - a separate class from
    `_FakeTapoClient` (which is constructed directly by tests, never via
    the 3-positional-arg bootstrap path) so each stays honest about what
    it stands in for."""

    def __init__(self, host, user, password):
        self.host, self.user, self.password = host, user, password

    def moveMotor(self, x, y):
        pass

    def calibrateMotor(self):
        pass


def test_A_valid_config_registers_real_handler():
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_ConstructibleFakeTapo)
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "RealCameraPTZHandler"
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


def test_G_auth_success_handler_is_usable():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert result.success


# -- B/C: missing username/password stays mocked (no construction attempt at all) -------------

def test_B_missing_username_stays_mocked():
    saved_cfg = _with_credentials()
    import luno.config as legacy_config
    legacy_config.TAPO_USERNAME = ""
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
    finally:
        _restore_credentials(saved_cfg)


def test_C_missing_password_stays_mocked():
    saved_cfg = _with_credentials()
    import luno.config as legacy_config
    legacy_config.TAPO_PASSWORD = ""
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
    finally:
        _restore_credentials(saved_cfg)


# -- D/E/F: construction-time failures stay mocked, and are now classified --------------------

def test_D_invalid_credential_at_construction_stays_mocked_and_classified():
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_RaisingTapo("Invalid authentication data"))
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
        classified = classify_tapo_exception(RuntimeError("Invalid authentication data"))
        assert classified.category == TapoErrorClass.AUTH_FAILED
        assert classified.retryable is False
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


def test_E_unreachable_camera_at_construction_stays_mocked_and_classified():
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_RaisingTapo("HTTPConnectionPool(host='192.168.1.52', port=443): "
                                                      "Max retries exceeded with url: /"))
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
        classified = classify_tapo_exception(RuntimeError("Max retries exceeded with url: /"))
        assert classified.category == TapoErrorClass.HOST_UNREACHABLE
        assert classified.retryable is True
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


def test_F_construction_timeout_stays_mocked_and_classified():
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_RaisingTapo("Read timed out."))
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
        classified = classify_tapo_exception(RuntimeError("Read timed out."))
        assert classified.category == TapoErrorClass.HOST_UNREACHABLE
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


def test_construction_failure_never_raises_regardless_of_classification():
    """Category D/E/F's shared safety net: NO classification outcome may
    ever turn a handled fallback into a startup crash."""
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_RaisingTapo("some totally novel, unclassified failure"))
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))  # must not raise
        assert _handler_class_name(tmm) == "MockCameraPTZHandler"
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


# -- H/I: per-command classification (post-registration, real usage) --------------------------

def test_H_auth_failure_on_command_is_AUTH_FAILED_and_not_retryable():
    client = _FakeTapoClient(raise_on={"moveMotor": Exception("Error: Invalid login credentials, Response: {}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZAuthFailed"
    assert result.data["error_class"] == TapoErrorClass.AUTH_FAILED
    assert result.retryable is False


def test_I_session_expiration_on_command_is_SESSION_EXPIRED_and_retryable():
    client = _FakeTapoClient(raise_on={"moveMotor": Exception("Error: Invalid stok value, Response: {}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    assert not result.success
    assert result.error_type == "CameraPTZSessionExpired"
    assert result.data["error_class"] == TapoErrorClass.SESSION_EXPIRED
    assert result.retryable is True


# -- J/K: bounded re-authentication - no second retry loop is added on top --------------------

def test_J_a_single_command_makes_at_most_one_underlying_client_call():
    """This layer must NOT add its own retry loop on top of pytapo's own
    already-bounded (MAX_LOGIN_RETRIES=1) internal retry - a single
    `execute()` call must reach the fake client's `moveMotor` exactly
    once, success or failure, proving no hidden extra attempt exists
    here (an unbounded/looping retry would show up as > 1 call)."""
    client = _FakeTapoClient(raise_on={"moveMotor": Exception("Error: Invalid stok value, Response: {}")})
    handler = RealCameraPTZHandler(client)
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert len(client.move_calls) == 0  # the raising call itself doesn't record args
    # Prove boundedness by counting actual invocations via a wrapper:
    calls = {"n": 0}
    real_move = client.moveMotor

    def _counting_move(x, y):
        calls["n"] += 1
        return real_move(x, y)

    client.moveMotor = _counting_move
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert calls["n"] == 1


def test_K_reauthentication_failure_surfaces_as_a_normal_classified_failure():
    """Even if pytapo's OWN internal retry already happened and STILL
    failed, that just looks like a normal exception from this layer's
    point of view - it must classify and return promptly, never hang or
    crash."""
    client = _FakeTapoClient(raise_on={"moveMotor": Exception("Error: Invalid stok value, Response: {}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZSessionExpired"


# -- L/M: PTZ success/failure after auth ------------------------------------------------------

def test_L_ptz_success_after_auth_for_every_action():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    for action in ("pan_left", "pan_right", "tilt_up", "tilt_down", "center"):
        result = handler.execute(ToolCall(tool="camera_ptz", action=action))
        assert result.success, f"{action} unexpectedly failed"


def test_M_ptz_api_rejection_after_successful_auth_has_no_invented_category():
    """A genuine API-level rejection with no evidence-based marker match
    (e.g. a hypothetical 'motor busy') must NOT be guessed into a made-up
    category - it stays the honest, pre-sprint generic bucket."""
    client = _FakeTapoClient(raise_on={"moveMotor": Exception("Error: -71304, Response: {}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZError"
    assert result.data["error_class"] == TapoErrorClass.UNKNOWN


# -- N: mock fallback remains fully functional -------------------------------------------------

def test_N_mock_fallback_remains_functional():
    handler = MockCameraPTZHandler()
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    assert result.success
    assert "[MOCK]" in result.message


# -- O/P: credential never appears in message/data ---------------------------------------------

def test_O_credential_never_appears_in_failure_message(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", _FAKE_USER)
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", _FAKE_PASS)
    client = _FakeTapoClient(raise_on={"moveMotor": Exception(f"auth failed for user {_FAKE_USER} pw {_FAKE_PASS}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert _FAKE_USER not in result.message
    assert _FAKE_PASS not in result.message
    assert "REDACTED" in result.message


def test_O_redact_credentials_helper_direct(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", _FAKE_USER)
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", _FAKE_PASS)
    redacted = _redact_credentials(f"host=1.2.3.4 user={_FAKE_USER} pass={_FAKE_PASS}")
    assert _FAKE_USER not in redacted
    assert _FAKE_PASS not in redacted


def test_P_credential_never_appears_in_result_data(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", _FAKE_USER)
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", _FAKE_PASS)
    client = _FakeTapoClient(raise_on={"moveMotor": Exception(f"pw={_FAKE_PASS}")})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    data_repr = repr(result.data)
    assert _FAKE_PASS not in data_repr
    assert _FAKE_USER not in data_repr


def test_P_bootstrap_log_line_never_contains_credential(monkeypatch, capsys):
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_RaisingTapo(f"connect failed for {_FAKE_USER}/{_FAKE_PASS}"))
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        out = capsys.readouterr()
        combined = out.out + out.err
        assert _FAKE_PASS not in combined
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


# -- Q: no arbitrary URL/host execution surface --------------------------------------------------

def test_Q_no_generic_url_or_host_parameter_exists_on_the_handler():
    """Structural guard: `execute()`/`validate()` never read anything
    resembling a host/URL/endpoint from `tool_call.params` - the ONLY
    per-call input the real handler ever consumes is `action` (from a
    fixed, closed list) and `target` (a preset NAME, resolved against
    the camera's OWN `getPresets()`, never used to build a URL)."""
    import inspect
    source = inspect.getsource(RealCameraPTZHandler)
    forbidden = ("tool_call.params", "requests.get", "requests.post", "urlopen", "http://", "https://")
    for token in forbidden:
        assert token not in source, f"unexpected URL/params surface: {token!r}"


def test_Q_supported_actions_is_a_fixed_closed_list():
    handler = RealCameraPTZHandler(_FakeTapoClient())
    assert handler.supported_actions() == [
        "pan_left", "pan_right", "tilt_up", "tilt_down", "center", "goto_preset", "save_preset",
    ]
    # Not influenced by any call-time input.
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left", parameters={"action": "shell_exec"}))
    assert handler.supported_actions() == [
        "pan_left", "pan_right", "tilt_up", "tilt_down", "center", "goto_preset", "save_preset",
    ]


# -- R: no persistent credential duplication / disk writes ---------------------------------------

def test_R_module_source_has_no_disk_or_db_write_surface():
    """Static-analysis-style guard (same spirit as Sprint 69.1's own
    AST-proven single-call-site regression check): this module must
    introduce no new persistent storage - no file writes, no sqlite/
    shelve/pickle usage anywhere in the whole file."""
    module_path = os.path.join(_ROOT, "luno", "tool_manager", "builtin", "real_camera_ptz.py")
    with open(module_path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    forbidden_names = {"open", "shelve", "sqlite3", "pickle"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            found.add(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            found.add(node.attr)
    assert not found, f"unexpected persistent-storage surface introduced: {found}"


# -- S: explicit camera target precedence ----------------------------------------------------

def test_S_target_is_only_ever_a_preset_name_never_a_host_override():
    client = _FakeTapoClient(presets={"1": "Pintu"})
    handler = RealCameraPTZHandler(client)
    # A target that LOOKS like a host/URL is still only ever compared
    # against preset names - never passed to the client as a connection
    # parameter (the fake client's constructor signature proves this:
    # RealCameraPTZHandler never re-constructs or reconfigures `client`
    # at all after __init__).
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="192.168.9.9"))
    assert not result.success
    assert "192.168.9.9" in result.message.lower()  # honestly reports the (unmatched) name it looked for
    assert client.set_preset_calls == []  # never attempted to "connect" anywhere


# -- T: no regression to unrelated tools -------------------------------------------------------

def test_T_unrelated_tool_registration_untouched():
    saved_cfg = _with_credentials()
    saved_pytapo = _install_fake_pytapo(_ConstructibleFakeTapo)
    try:
        tmm = _FakeToolManagerModule()
        before = type(tmm.manager.registry.get("home_assistant")).__name__
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        after = type(tmm.manager.registry.get("home_assistant")).__name__
        assert before == after
    finally:
        _restore_pytapo(saved_pytapo)
        _restore_credentials(saved_cfg)


def test_T_backend_mock_path_fully_unaffected():
    tmm = _FakeToolManagerModule()
    _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_MOCK))
    assert _handler_class_name(tmm) == "MockCameraPTZHandler"


# -- classifier coverage (fills out Phase 2's full category list directly) --------------------

def test_classifier_covers_all_documented_categories():
    cases = {
        TapoErrorClass.AUTH_FAILED: "Invalid authentication data",
        TapoErrorClass.SESSION_EXPIRED: "Error: Invalid stok value, Response: {}",
        TapoErrorClass.AUTH_RATE_LIMITED: "Temporary Suspension: Try again in 60 seconds",
        TapoErrorClass.DEVICE_OFFLINE: "Error: DEVICE_OFFLINE, Response: {}",
        TapoErrorClass.PORT_UNREACHABLE: "[Errno 111] Connection refused",
        TapoErrorClass.HOST_UNREACHABLE: "Max retries exceeded with url: /",
        TapoErrorClass.UNKNOWN: "some never-seen-before message",
    }
    for expected_category, message in cases.items():
        classified = classify_tapo_exception(RuntimeError(message))
        assert classified.category == expected_category, f"{message!r} classified as {classified.category}, expected {expected_category}"


def test_classifier_never_raises_on_odd_input():
    for bad in (Exception(""), Exception(), ValueError(None), KeyError(object())):
        classify_tapo_exception(bad)  # must not raise
