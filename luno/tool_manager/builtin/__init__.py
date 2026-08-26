"""
builtin
=======

Every placeholder/mock handler shipped with this package, plus
`register_all()` - a convenience for wiring all of them into a
`ToolRegistry` at once (mainly useful for quick-start/demo/test code;
real deployments will likely register a mix of these mocks and real
handlers one at a time as each is actually implemented).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .browser import MockBrowserHandler
from .camera_ptz import MockCameraPTZHandler
from .dummy import DummyHandler
from .home_assistant import MockHomeAssistantHandler
from .llm_mode import LLMModeHandler
from .spotify import MockSpotifyHandler
from .unity import MockUnityHandler
from .vision import MockVisionHandler
from .windows import MockWindowsHandler

__all__ = [
    "MockHomeAssistantHandler", "MockWindowsHandler", "MockBrowserHandler",
    "MockVisionHandler", "MockSpotifyHandler", "MockUnityHandler", "MockCameraPTZHandler",
    "LLMModeHandler", "DummyHandler", "register_all",
]


def register_all(registry: ToolRegistry) -> None:
    """Registers every builtin mock handler under its spec-given tool
    name, plus `"dummy"` for tests. Real integration will typically call
    `registry.register("home_assistant", RealHandler())` per tool
    instead, one at a time, as each real implementation lands - this
    function is a convenience, not a required entry point.

    `"llm_mode"` is the one exception to the mock/real pattern - it has
    no external hardware/network dependency to fake (see
    `llm_mode.py`'s own docstring), so `LLMModeHandler` is registered
    here directly, real from the start, same as every other tool would
    be once its real implementation lands."""
    registry.register("home_assistant", MockHomeAssistantHandler())
    registry.register("windows", MockWindowsHandler())
    registry.register("browser", MockBrowserHandler())
    registry.register("vision", MockVisionHandler())
    registry.register("spotify", MockSpotifyHandler())
    registry.register("unity", MockUnityHandler())
    registry.register("camera_ptz", MockCameraPTZHandler())
    registry.register("llm_mode", LLMModeHandler())
    registry.register("dummy", DummyHandler())
