"""
test_mic_device_index.py
===========================

`MIC_DEVICE_INDEX` (see `luno/config.py`) - lets a user pin a specific
PyAudio input device instead of relying on Windows' own "default
recording device" lookup, which can fail outright on some machines
("[Errno -9996] Invalid device info"). Covers: env parsing, wiring into
`RealWhisperSource`'s `sr.Microphone(...)` calls, the updated
`_check_microphone()` health check, and `list_microphones.py`'s helper
script - all against fake `speech_recognition`/`pyaudio` modules (no
real audio hardware needed/used).
"""

from __future__ import annotations

import importlib
import os
import sys
import types

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================================
# luno/config.py - MIC_DEVICE_INDEX parsing
# ============================================================================

def _reload_config(monkeypatch, raw_value):
    if raw_value is None:
        monkeypatch.delenv("MIC_DEVICE_INDEX", raising=False)
    else:
        monkeypatch.setenv("MIC_DEVICE_INDEX", raw_value)
    import luno.config as legacy_config
    importlib.reload(legacy_config)
    return legacy_config


def test_unset_defaults_to_none(monkeypatch):
    cfg = _reload_config(monkeypatch, None)
    assert cfg.MIC_DEVICE_INDEX is None


def test_blank_defaults_to_none(monkeypatch):
    cfg = _reload_config(monkeypatch, "")
    assert cfg.MIC_DEVICE_INDEX is None


def test_valid_integer_parsed(monkeypatch):
    cfg = _reload_config(monkeypatch, "3")
    assert cfg.MIC_DEVICE_INDEX == 3


def test_garbage_value_fails_open_to_none(monkeypatch):
    cfg = _reload_config(monkeypatch, "not-a-number")
    assert cfg.MIC_DEVICE_INDEX is None


def test_reload_leaves_config_importable_for_other_tests(monkeypatch):
    # cleanup: reload once more back to a clean unset state so this
    # module-level mutation of the REAL luno.config module doesn't leak
    # into other test files that import it afterward in the same run.
    _reload_config(monkeypatch, None)


# ============================================================================
# RealWhisperSource - device_index wiring
# ============================================================================

class _FakeMicrophone:
    last_device_index = None

    def __init__(self, device_index=None):
        _FakeMicrophone.last_device_index = device_index

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeRecognizer:
    def adjust_for_ambient_noise(self, source, duration=1.0):
        pass

    dynamic_energy_threshold = True

    def listen(self, source, timeout=None, phrase_time_limit=None):
        return "fake-audio"


def _install_fake_sr(monkeypatch):
    fake_sr = types.ModuleType("speech_recognition")
    fake_sr.Microphone = _FakeMicrophone
    fake_sr.Recognizer = _FakeRecognizer

    class _WaitTimeoutError(Exception):
        pass

    fake_sr.WaitTimeoutError = _WaitTimeoutError
    monkeypatch.setitem(sys.modules, "speech_recognition", fake_sr)
    return fake_sr


def _patch_out_legacy_main_import(monkeypatch):
    """`RealWhisperSource.__init__` also imports the real `legacy_main.py`
    (for `transcribe_audio()`), which itself imports `sounddevice` at
    module level - a real hardware/native-library dependency this test
    has no need to exercise (it's only testing `device_index` wiring)
    and that may not even be installable in a headless sandbox. Swap in
    a trivial stand-in so constructing `RealWhisperSource()` doesn't
    require a working PortAudio install."""
    import luno.adapters.real_whisper as real_whisper_mod
    fake_legacy = types.ModuleType("legacy_main")
    fake_legacy.transcribe_audio = lambda audio: ""
    monkeypatch.setattr(real_whisper_mod, "_import_legacy_main", lambda: fake_legacy)


def test_real_whisper_source_passes_configured_device_index(monkeypatch):
    _install_fake_sr(monkeypatch)
    _patch_out_legacy_main_import(monkeypatch)
    monkeypatch.setenv("MIC_DEVICE_INDEX", "5")
    import luno.config as legacy_config
    importlib.reload(legacy_config)

    from luno.adapters.real_whisper import RealWhisperSource
    source = RealWhisperSource()
    assert source._device_index == 5

    source._calibrate_once()
    assert _FakeMicrophone.last_device_index == 5

    # reset the sentinel, then confirm the LISTEN call site (not just
    # calibration) threads the same device_index through too
    _FakeMicrophone.last_device_index = None
    source._listener = None  # returns early after the fake .listen() call, before touching self._legacy
    source._listen_and_transcribe_once()
    assert _FakeMicrophone.last_device_index == 5

    monkeypatch.delenv("MIC_DEVICE_INDEX", raising=False)
    importlib.reload(legacy_config)


def test_real_whisper_source_defaults_to_none_when_unset(monkeypatch):
    _install_fake_sr(monkeypatch)
    _patch_out_legacy_main_import(monkeypatch)
    monkeypatch.delenv("MIC_DEVICE_INDEX", raising=False)
    import luno.config as legacy_config
    importlib.reload(legacy_config)

    from luno.adapters.real_whisper import RealWhisperSource
    source = RealWhisperSource()
    assert source._device_index is None

    source._calibrate_once()
    assert _FakeMicrophone.last_device_index is None


# ============================================================================
# health.py - _check_microphone() with an explicit MIC_DEVICE_INDEX
# ============================================================================

def test_health_check_validates_configured_index_in_range(monkeypatch):
    from luno.bootstrap.health import _check_microphone
    from luno.bootstrap.launcher_config import BACKEND_REAL, LauncherConfig

    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: ["Built-in Mic", "USB Headset", "Line In"])

    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "MIC_DEVICE_INDEX", 1)

    result = _check_microphone(LauncherConfig(whisper_backend=BACKEND_REAL))
    assert result.ok is True
    assert "USB Headset" in result.message


def test_health_check_flags_out_of_range_configured_index(monkeypatch):
    from luno.bootstrap.health import _check_microphone
    from luno.bootstrap.launcher_config import BACKEND_REAL, LauncherConfig

    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: ["Built-in Mic"])

    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "MIC_DEVICE_INDEX", 99)

    result = _check_microphone(LauncherConfig(whisper_backend=BACKEND_REAL))
    assert result.ok is False
    assert "out of range" in result.message
    assert "list_microphones.py" in result.message


def test_health_check_falls_back_to_default_behavior_when_unset(monkeypatch):
    from luno.bootstrap.health import _check_microphone
    from luno.bootstrap.launcher_config import BACKEND_REAL, LauncherConfig

    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: ["Built-in Mic", "USB Headset"])

    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "MIC_DEVICE_INDEX", None)

    result = _check_microphone(LauncherConfig(whisper_backend=BACKEND_REAL))
    assert result.ok is True
    assert "2 input device" in result.message


def test_health_check_points_to_helper_script_on_enumeration_failure(monkeypatch):
    from luno.bootstrap.health import _check_microphone
    from luno.bootstrap.launcher_config import BACKEND_REAL, LauncherConfig

    fake_sr = _install_fake_sr(monkeypatch)

    def _boom():
        raise OSError("[Errno -9996] Invalid device info")

    fake_sr.Microphone.list_microphone_names = staticmethod(_boom)

    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "MIC_DEVICE_INDEX", None)

    result = _check_microphone(LauncherConfig(whisper_backend=BACKEND_REAL))
    assert result.ok is False
    assert result.action == "warned"
    assert "list_microphones.py" in result.message


def test_health_check_skipped_when_backend_is_mock():
    from luno.bootstrap.health import _check_microphone
    from luno.bootstrap.launcher_config import BACKEND_MOCK, LauncherConfig

    result = _check_microphone(LauncherConfig(whisper_backend=BACKEND_MOCK))
    assert result.ok is True
    assert "not required" in result.message


# ============================================================================
# list_microphones.py
# ============================================================================

def _load_list_microphones_module():
    import importlib.util
    path = os.path.join(_ROOT, "list_microphones.py")
    spec = importlib.util.spec_from_file_location("list_microphones", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_microphones_reports_devices_and_default(monkeypatch, capsys):
    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: ["Built-in Mic", "USB Headset"])

    fake_pyaudio = types.ModuleType("pyaudio")

    class _FakePA:
        def get_default_input_device_info(self):
            return {"index": 1}

        def terminate(self):
            pass

    fake_pyaudio.PyAudio = lambda: _FakePA()
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pyaudio)

    mod = _load_list_microphones_module()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "[0] Built-in Mic" in out
    assert "[1] USB Headset" in out
    assert "current Windows default" in out
    assert "MIC_DEVICE_INDEX=" in out


def test_list_microphones_handles_no_valid_default(monkeypatch, capsys):
    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: ["Built-in Mic"])

    fake_pyaudio = types.ModuleType("pyaudio")

    class _FakePA:
        def get_default_input_device_info(self):
            raise OSError("[Errno -9996] Invalid device info")

        def terminate(self):
            pass

    fake_pyaudio.PyAudio = lambda: _FakePA()
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pyaudio)

    mod = _load_list_microphones_module()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "no valid DEFAULT input device" in out


def test_list_microphones_handles_missing_dependency(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "speech_recognition", None)  # forces ImportError on `import speech_recognition`
    mod = _load_list_microphones_module()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "pip install" in out


def test_list_microphones_handles_zero_devices(monkeypatch, capsys):
    fake_sr = _install_fake_sr(monkeypatch)
    fake_sr.Microphone.list_microphone_names = staticmethod(lambda: [])

    mod = _load_list_microphones_module()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "No input devices found" in out
