"""
test_camera_ptz_bootstrap.py
==============================

`_register_real_camera_ptz_handler` (luno/bootstrap/adapters.py) - the
one place `CAMERA_PTZ_BACKEND=real` + `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` turn into a real `pytapo`-backed tool handler override,
mirroring the exact same "opt-in, fail-closed to mock, never blocks
startup" convention already covered for Home Assistant/Windows. `pytapo`
itself is stubbed via `sys.modules` here (a real network-calling
constructor has no place in a unit test, and this project's own sandbox
doesn't have a live Tapo camera to test against anyway) - this proves
the WIRING is correct, independent of whatever `pytapo` version happens
to be installed.

Run:
    python3 -m pytest tests/test_camera_ptz_bootstrap.py
"""

from __future__ import annotations

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
from luno.tool_manager.manager import ToolManager  # noqa: E402


class _FakeToolManagerModule:
    def __init__(self):
        self.manager = ToolManager()
        self.manager.registry.register("camera_ptz", MockCameraPTZHandler())


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


def _current_handler_class_name(tool_manager_module):
    handler = tool_manager_module.manager.registry.get("camera_ptz")
    return type(handler).__name__


def test_backend_not_real_leaves_mock_in_place():
    tmm = _FakeToolManagerModule()
    _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_MOCK))
    assert _current_handler_class_name(tmm) == "MockCameraPTZHandler"


def test_real_backend_without_credentials_stays_mocked():
    saved = _set_env(TAPO_HOST=None, TAPO_USERNAME=None, TAPO_PASSWORD=None)
    try:
        import luno.config as legacy_config
        saved_attrs = (legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
        legacy_config.TAPO_HOST = ""
        legacy_config.TAPO_USERNAME = ""
        legacy_config.TAPO_PASSWORD = ""
        try:
            tmm = _FakeToolManagerModule()
            _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
            assert _current_handler_class_name(tmm) == "MockCameraPTZHandler"
        finally:
            legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = saved_attrs
    finally:
        _restore_env(saved)


def test_real_backend_with_credentials_registers_real_handler():
    """Stubs `pytapo` via `sys.modules` so this exercises the real wiring
    path without needing network access or a real camera - a fake `Tapo`
    class that just records its constructor args."""
    import luno.config as legacy_config
    saved_attrs = (legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    legacy_config.TAPO_HOST = "192.168.1.52"
    legacy_config.TAPO_USERNAME = "luno_camera_account"
    legacy_config.TAPO_PASSWORD = "hunter2"

    fake_pytapo_module = types.ModuleType("pytapo")

    class _FakeTapo:
        def __init__(self, host, user, password):
            self.host, self.user, self.password = host, user, password

        def moveMotor(self, x, y):
            pass

        def calibrateMotor(self):
            pass

    fake_pytapo_module.Tapo = _FakeTapo
    saved_module = sys.modules.get("pytapo")
    sys.modules["pytapo"] = fake_pytapo_module
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))
        handler = tmm.manager.registry.get("camera_ptz")
        assert type(handler).__name__ == "RealCameraPTZHandler"
        assert handler._client.host == "192.168.1.52"
        assert handler._client.user == "luno_camera_account"
    finally:
        legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = saved_attrs
        if saved_module is not None:
            sys.modules["pytapo"] = saved_module
        else:
            sys.modules.pop("pytapo", None)


def test_pytapo_construction_failure_stays_mocked_never_raises():
    """A bad host/credential (or pytapo simply not installed) must never
    crash startup - falls back to the mock, exactly like every other
    real-handler override in this file."""
    import luno.config as legacy_config
    saved_attrs = (legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    legacy_config.TAPO_HOST = "192.168.1.52"
    legacy_config.TAPO_USERNAME = "luno_camera_account"
    legacy_config.TAPO_PASSWORD = "hunter2"

    fake_pytapo_module = types.ModuleType("pytapo")

    class _ExplodingTapo:
        def __init__(self, host, user, password):
            raise RuntimeError("simulated: invalid authentication data")

    fake_pytapo_module.Tapo = _ExplodingTapo
    saved_module = sys.modules.get("pytapo")
    sys.modules["pytapo"] = fake_pytapo_module
    try:
        tmm = _FakeToolManagerModule()
        _register_real_camera_ptz_handler({"tool_manager_module": tmm}, _launcher_config(BACKEND_REAL))  # must not raise
        assert _current_handler_class_name(tmm) == "MockCameraPTZHandler"
    finally:
        legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = saved_attrs
        if saved_module is not None:
            sys.modules["pytapo"] = saved_module
        else:
            sys.modules.pop("pytapo", None)


def test_no_tool_manager_module_is_a_safe_noop():
    import luno.config as legacy_config
    saved_attrs = (legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    legacy_config.TAPO_HOST = "192.168.1.52"
    legacy_config.TAPO_USERNAME = "luno_camera_account"
    legacy_config.TAPO_PASSWORD = "hunter2"
    try:
        _register_real_camera_ptz_handler({}, _launcher_config(BACKEND_REAL))  # must not raise
    finally:
        legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD = saved_attrs
