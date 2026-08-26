"""
test_sprint70_tapo_live_recovery.py
======================================

Tests for "SPRINT 70 - TAPO C212 LIVE AUTHENTICATION & AUTO-RECOVERY".

Scope, per the brief's own Phase 7: at least categories A-O below, using
ONLY fake/mock camera responses for deterministic regression coverage -
never a real password/host/`pytapo` import. Where LIVE verification
would be possible (a real camera, real credentials), it is recorded
SEPARATELY in `docs/change_impact/tapo_c212_live_recovery.md`'s own
"LIVE VERIFICATION" section, never faked here or there - this sandbox
has zero TAPO_* configuration (see that document), so every test below
is a fake-client regression test, not a live result.

Run:
    python3 -m pytest tests/test_sprint70_tapo_live_recovery.py
"""

from __future__ import annotations

import ast
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.tool_manager.builtin.camera_ptz import MockCameraPTZHandler  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import (  # noqa: E402
    PTZConnectionState,
    RealCameraPTZHandler,
    TapoErrorClass,
    _redact_credentials,
    classify_tapo_exception,
)
from luno.tool_manager.models import ToolCall  # noqa: E402

_FAKE_USER = "luno_camera_account"  # placeholder shape, not a real credential
_FAKE_PASS = "hunter2"              # classic placeholder password, not a real credential

# Evidence-sourced exception text (same markers as Sprint 69's own
# classify_tapo_exception() - see that module's citation comment).
_AUTH_FAILED_EXC = Exception("Invalid authentication data")
_SESSION_EXPIRED_EXC = Exception("Error: Invalid stok value, Response: {}")
_RATE_LIMITED_EXC = Exception("Temporary Suspension: Try again in 60 seconds")
_HOST_UNREACHABLE_EXC = Exception("Max retries exceeded with url: /")
_UNKNOWN_EXC = Exception("some never-seen-before message")


class _ScriptedTapoClient:
    """Fake `pytapo.Tapo` stand-in whose calls raise/succeed per a
    scripted sequence - each tracked method call consumes the next
    scripted outcome (`None` = succeed, an `Exception` instance =
    raise), falling back to "succeed" once the script is exhausted.
    Lets a test simulate exactly one client seeing exactly one failure
    (or an unlimited-failure client, for boundedness tests)."""

    def __init__(self, outcomes=None, always_raise=None):
        self._outcomes = list(outcomes) if outcomes is not None else []
        self._always_raise = always_raise  # if set, EVERY call raises a fresh instance of this
        self.calls = []

    def _next(self):
        if self._always_raise is not None:
            raise type(self._always_raise)(str(self._always_raise))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if outcome is not None:
                raise outcome
        return True

    def moveMotor(self, x, y):
        self.calls.append(("moveMotor", x, y))
        return self._next()

    def calibrateMotor(self):
        self.calls.append(("calibrateMotor",))
        return self._next()

    def getPresets(self):
        self.calls.append(("getPresets",))
        self._next()
        return {}

    def savePreset(self, name):
        self.calls.append(("savePreset", name))
        return self._next()

    def setPreset(self, preset_id):
        self.calls.append(("setPreset", preset_id))
        return self._next()


# -- A: valid authentication ---------------------------------------------------------------

def test_A_valid_authentication_succeeds_and_state_is_connected():
    client = _ScriptedTapoClient()
    handler = RealCameraPTZHandler(client)
    assert handler.connection_state() == PTZConnectionState.CONNECTED  # constructed client implies successful auth
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert result.success
    assert handler.connection_state() == PTZConnectionState.CONNECTED


# -- B: invalid credentials ------------------------------------------------------------------

def test_B_invalid_credentials_is_AUTH_FAILED_never_retried():
    client = _ScriptedTapoClient(outcomes=[_AUTH_FAILED_EXC])
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return _ScriptedTapoClient()

    handler = RealCameraPTZHandler(client, client_factory=factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZAuthFailed"
    assert result.retryable is False
    assert handler.connection_state() == PTZConnectionState.AUTH_FAILED
    assert factory_calls["n"] == 0  # never attempted a reconnect - wrong credentials won't fix themselves


# -- C: expired session ----------------------------------------------------------------------

def test_C_expired_session_triggers_one_reconnect_and_succeeds():
    stale_client = _ScriptedTapoClient(outcomes=[_SESSION_EXPIRED_EXC])
    fresh_client = _ScriptedTapoClient()
    handler = RealCameraPTZHandler(stale_client, client_factory=lambda: fresh_client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    assert result.success
    assert stale_client.calls == [("moveMotor", 15.0, 0.0)]
    assert fresh_client.calls == [("moveMotor", 15.0, 0.0)]
    assert handler.connection_state() == PTZConnectionState.CONNECTED


# -- D: transient connection failure -----------------------------------------------------------

def test_D_transient_network_failure_triggers_one_reconnect_and_succeeds():
    flaky_client = _ScriptedTapoClient(outcomes=[_HOST_UNREACHABLE_EXC])
    fresh_client = _ScriptedTapoClient()
    handler = RealCameraPTZHandler(flaky_client, client_factory=lambda: fresh_client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="tilt_up"))
    assert result.success
    assert handler.connection_state() == PTZConnectionState.CONNECTED


# -- E: permanent unreachable device -----------------------------------------------------------

def test_E_permanently_unreachable_device_fails_honestly_after_one_bounded_retry():
    dead_client = _ScriptedTapoClient(always_raise=_HOST_UNREACHABLE_EXC)
    still_dead_client = _ScriptedTapoClient(always_raise=_HOST_UNREACHABLE_EXC)
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return still_dead_client

    handler = RealCameraPTZHandler(dead_client, client_factory=factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZUnreachable"
    assert result.data["error_class"] == TapoErrorClass.HOST_UNREACHABLE
    assert handler.connection_state() == PTZConnectionState.DEVICE_UNREACHABLE
    assert factory_calls["n"] == 1  # exactly one reconnect attempt, never looped
    assert len(dead_client.calls) == 1
    assert len(still_dead_client.calls) == 1


# -- F: authentication rate limit ----------------------------------------------------------------

def test_F_rate_limited_stops_retrying_and_reports_clearly():
    client = _ScriptedTapoClient(outcomes=[_RATE_LIMITED_EXC])
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return _ScriptedTapoClient()

    handler = RealCameraPTZHandler(client, client_factory=factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="center"))
    assert not result.success
    assert result.error_type == "CameraPTZAuthRateLimited"
    assert result.retryable is False
    assert handler.connection_state() == PTZConnectionState.AUTH_FAILED
    assert factory_calls["n"] == 0


# -- G: unknown exception ----------------------------------------------------------------------

def test_G_unknown_exception_preserves_safe_failure_behavior():
    client = _ScriptedTapoClient(outcomes=[_UNKNOWN_EXC])
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return _ScriptedTapoClient()

    handler = RealCameraPTZHandler(client, client_factory=factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZError"  # exact pre-Sprint-69/70 generic bucket - never guessed
    assert handler.connection_state() == PTZConnectionState.DISCONNECTED
    assert factory_calls["n"] == 0  # unknown failures get no special retry handling


# -- H: successful reconnect (and proves the AUTHENTICATING transition genuinely happens) -------

def test_H_successful_reconnect_passes_through_authenticating_state():
    stale_client = _ScriptedTapoClient(outcomes=[_SESSION_EXPIRED_EXC])
    fresh_client = _ScriptedTapoClient()
    observed_state_during_reconnect = {}

    def factory():
        observed_state_during_reconnect["state"] = handler.connection_state()
        return fresh_client

    handler = RealCameraPTZHandler(stale_client, client_factory=factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert result.success
    assert observed_state_during_reconnect["state"] == PTZConnectionState.AUTHENTICATING
    assert handler.connection_state() == PTZConnectionState.CONNECTED  # settles to CONNECTED after success


# -- I: reconnect exhaustion (the reconnect ITSELF fails) ---------------------------------------

def test_I_reconnect_construction_failure_reports_the_original_error_not_the_reconnect_error():
    stale_client = _ScriptedTapoClient(outcomes=[_SESSION_EXPIRED_EXC])

    def failing_factory():
        raise RuntimeError("could not construct a fresh Tapo client")

    handler = RealCameraPTZHandler(stale_client, client_factory=failing_factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    # Reports the ORIGINAL classified failure (session expired), not the
    # reconnect attempt's own "could not construct" exception - that's
    # what the caller actually asked this tool to do.
    assert result.error_type == "CameraPTZSessionExpired"
    assert "could not construct" not in result.message.lower()
    assert len(stale_client.calls) == 1  # never retried against the (never-built) new client


# -- J: no infinite retry -------------------------------------------------------------------------

def test_J_bounded_to_exactly_two_underlying_calls_even_when_both_clients_always_fail():
    """The strongest possible boundedness proof: even when EVERY client
    (old and new) always fails, a single `execute()` call makes at most
    2 total underlying client calls (1 original + 1 retry) - never 3,
    never a growing count, never a hang."""
    client_a = _ScriptedTapoClient(always_raise=_SESSION_EXPIRED_EXC)
    client_b = _ScriptedTapoClient(always_raise=_SESSION_EXPIRED_EXC)
    handler = RealCameraPTZHandler(client_a, client_factory=lambda: client_b)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    total_calls = len(client_a.calls) + len(client_b.calls)
    assert total_calls == 2


def test_J_invoke_source_contains_no_loop_construct():
    """Static-analysis-style guard (same spirit as Sprint 69's own AST-
    proven single-call-site regression check): `_invoke()`'s own source
    must contain no `while`/`for` - "bounded to one retry" should be
    TRUE BY CONSTRUCTION, not by a counter that could someday be bumped."""
    import inspect
    import textwrap
    from luno.tool_manager.builtin import real_camera_ptz as module

    source = textwrap.dedent(inspect.getsource(module.RealCameraPTZHandler._invoke))
    tree = ast.parse(source)
    loop_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.While, ast.For))]
    assert not loop_nodes, "the bounded-retry method must contain no loop construct"


def test_J_max_reconnect_attempts_constant_is_exactly_one():
    from luno.tool_manager.builtin import real_camera_ptz as module
    assert module._MAX_RECONNECT_ATTEMPTS == 1


# -- K: credential redaction -----------------------------------------------------------------------

def test_K_credential_never_appears_in_failure_message_on_recovery_path(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", _FAKE_USER)
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", _FAKE_PASS)
    leaking_exc = Exception(f"Max retries exceeded - session for {_FAKE_USER}/{_FAKE_PASS} could not be renewed")
    client = _ScriptedTapoClient(outcomes=[leaking_exc])

    def failing_factory():
        raise RuntimeError(f"reconnect failed for {_FAKE_USER}/{_FAKE_PASS}")

    handler = RealCameraPTZHandler(client, client_factory=failing_factory)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert _FAKE_USER not in result.message
    assert _FAKE_PASS not in result.message
    assert _FAKE_USER not in repr(result.data)
    assert _FAKE_PASS not in repr(result.data)


def test_K_connection_state_values_never_resemble_a_credential():
    for value in (PTZConnectionState.DISCONNECTED, PTZConnectionState.AUTHENTICATING,
                  PTZConnectionState.CONNECTED, PTZConnectionState.SESSION_EXPIRED,
                  PTZConnectionState.AUTH_FAILED, PTZConnectionState.DEVICE_UNREACHABLE):
        assert _FAKE_USER not in value and _FAKE_PASS not in value


# -- L: mock backend compatibility -----------------------------------------------------------------

def test_L_mock_backend_entirely_unaffected_by_sprint_70():
    handler = MockCameraPTZHandler()
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    assert result.success
    assert "[MOCK]" in result.message
    # Mock handler has no connection_state()/recovery concept at all -
    # Sprint 70 added nothing to camera_ptz.py.
    assert not hasattr(handler, "connection_state")
    assert not hasattr(handler, "_client_factory")


# -- M: existing PTZ behavior (every action still works through the new _invoke() path) -------------

def test_M_every_ptz_action_still_works_through_the_new_invoke_path():
    client = _ScriptedTapoClient()
    handler = RealCameraPTZHandler(client)
    for action in ("pan_left", "pan_right", "tilt_up", "tilt_down", "center"):
        result = handler.execute(ToolCall(tool="camera_ptz", action=action))
        assert result.success, f"{action} unexpectedly failed"

    save_result = handler.execute(ToolCall(tool="camera_ptz", action="save_preset", target="pintu"))
    assert save_result.success


def test_M_backward_compatible_when_client_factory_omitted():
    """Every pre-Sprint-70 caller (including every pre-existing test)
    constructs `RealCameraPTZHandler(client)` with no factory - behavior
    for those callers must be byte-for-byte identical: one call, one
    classified failure, no retry attempted."""
    client = _ScriptedTapoClient(outcomes=[_SESSION_EXPIRED_EXC])
    handler = RealCameraPTZHandler(client)  # no client_factory - old call signature
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZSessionExpired"
    assert len(client.calls) == 1  # no retry possible without a factory


# -- N: persistent-state immutability ----------------------------------------------------------------

def test_N_module_source_still_has_no_disk_or_db_write_surface():
    module_path = os.path.join(_ROOT, "luno", "tool_manager", "builtin", "real_camera_ptz.py")
    with open(module_path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    forbidden_names = {"open", "shelve", "sqlite3", "pickle", "eval", "exec"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            found.add(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            found.add(node.attr)
    assert not found, f"unexpected dangerous surface introduced: {found}"


def test_N_connection_state_and_client_are_purely_in_memory_instance_attributes():
    """Structural guard: the connection state / client-factory machinery
    must live on `self` (per-handler-instance), never as a module-level
    global - two independently constructed handlers must never share
    state."""
    client_a = _ScriptedTapoClient(outcomes=[_AUTH_FAILED_EXC])
    client_b = _ScriptedTapoClient()
    handler_a = RealCameraPTZHandler(client_a)
    handler_b = RealCameraPTZHandler(client_b)
    handler_a.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert handler_a.connection_state() == PTZConnectionState.AUTH_FAILED
    assert handler_b.connection_state() == PTZConnectionState.CONNECTED  # unaffected by handler_a


def test_N_config_json_files_unchanged_across_a_recovery_scenario():
    import glob
    import hashlib
    config_dir = os.path.join(_ROOT, "config")
    paths = sorted(glob.glob(os.path.join(config_dir, "*.json")))
    before = {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}

    stale_client = _ScriptedTapoClient(outcomes=[_SESSION_EXPIRED_EXC])
    fresh_client = _ScriptedTapoClient()
    handler = RealCameraPTZHandler(stale_client, client_factory=lambda: fresh_client)
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))  # exercises the full recovery path

    after = {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}
    assert before == after


# -- O: dashboard/PTZ status separation ----------------------------------------------------------------

def test_O_dashboard_collectors_do_not_reference_ptz_connection_state():
    """`connection_state()`/`PTZConnectionState` must stay un-wired into
    the dashboard - per Phase 6, unifying them would risk fabricating a
    connectivity claim this tool cannot back up for the SEPARATE
    streaming path. Structural guard against an accidental future
    coupling."""
    collectors_path = os.path.join(_ROOT, "luno", "dashboard", "collectors.py")
    with open(collectors_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "PTZConnectionState" not in source
    assert "connection_state" not in source
    assert "camera_ptz" not in source


def test_O_camera_ptz_still_not_listed_as_an_adapter():
    index_html_path = os.path.join(_ROOT, "luno", "dashboard", "static", "index.html")
    with open(index_html_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "camera_ptz" not in source


def test_O_no_ptz_toolresult_message_ever_says_the_bare_word_disconnect():
    """Sprint 69's own forensic finding was that no USER/LLM-FACING
    message from this file ever says "disconnect" (that word belongs to
    the SEPARATE vision.py/dashboard subsystem - see Phase 2). Sprint
    70's new `PTZConnectionState.DISCONNECTED` is an internal, in-memory
    STATE NAME (matching the brief's own requested Phase 3 vocabulary),
    not a message - it is never interpolated into a `ToolResult.message`
    anywhere. This test proves that distinction holds: every exercised
    failure message stays "disconnect"-free even though the STATE name
    itself legitimately contains the word."""
    client = _ScriptedTapoClient(outcomes=[_UNKNOWN_EXC])  # -> DISCONNECTED state
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert handler.connection_state() == PTZConnectionState.DISCONNECTED
    assert "disconnect" not in result.message.lower()
