"""
test_llm_mode.py
====================

`LLMModeHandler` (luno/tool_manager/builtin/llm_mode.py) - the tool
that lets a voice/text command flip the Intelligent AI Routing Engine's
auto/manual mode LIVE. Every test resets the process-wide
`RUNTIME_MODE` singleton (which this handler necessarily writes to -
that IS the point of the tool) back to auto in a `finally` block, so
these tests can run in any order/interleaving with
`luno/routing/tests/test_decision_engine.py` (which constructs its own
`DecisionEngine` instances defaulting to that same singleton) without
leaking state into it.

Run:
    python3 -m pytest luno/tool_manager/tests/test_llm_mode.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from luno.routing.mode_state import MODE_AUTO, MODE_MANUAL, RUNTIME_MODE  # noqa: E402
from luno.tool_manager.builtin.llm_mode import LLMModeHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_runtime_mode():
    RUNTIME_MODE.reset()
    yield
    RUNTIME_MODE.reset()


def test_supported_actions():
    handler = LLMModeHandler()
    assert set(handler.supported_actions()) == {"set_auto", "set_manual"}


def test_set_manual_with_known_provider_updates_singleton():
    handler = LLMModeHandler()
    result = handler.execute(ToolCall(tool="llm_mode", action="set_manual", target="openai"))
    assert result.success
    assert "openai" in result.message.lower()
    assert result.data == {"provider_alias": "openai"}
    assert RUNTIME_MODE.snapshot() == (MODE_MANUAL, "openai")


def test_set_manual_without_provider_still_switches_mode():
    handler = LLMModeHandler()
    result = handler.execute(ToolCall(tool="llm_mode", action="set_manual"))
    assert result.success
    assert "manual" in result.message.lower()
    assert RUNTIME_MODE.snapshot() == (MODE_MANUAL, None)


def test_set_auto_resets_singleton():
    handler = LLMModeHandler()
    RUNTIME_MODE.set_manual("anthropic")
    result = handler.execute(ToolCall(tool="llm_mode", action="set_auto"))
    assert result.success
    assert "automatic" in result.message.lower()
    assert RUNTIME_MODE.snapshot() == (MODE_AUTO, None)


def test_validate_rejects_unknown_provider():
    handler = LLMModeHandler()
    error = handler.validate(ToolCall(tool="llm_mode", action="set_manual", target="not_a_real_provider"))
    assert error is not None
    assert "not_a_real_provider" in error


def test_validate_accepts_every_known_alias():
    handler = LLMModeHandler()
    for alias in ("openrouter", "openai", "gemini", "anthropic", "local", "deepseek", "gpt", "chatgpt", "claude", "OpenAI"):
        error = handler.validate(ToolCall(tool="llm_mode", action="set_manual", target=alias))
        assert error is None, f"{alias!r} should be a recognized provider alias"


def test_validate_rejects_unsupported_action():
    handler = LLMModeHandler()
    error = handler.validate(ToolCall(tool="llm_mode", action="zoom_in"))
    assert error is not None and "not supported" in error.lower()


def test_execute_is_case_insensitive_for_provider_target():
    handler = LLMModeHandler()
    result = handler.execute(ToolCall(tool="llm_mode", action="set_manual", target="  Anthropic  "))
    assert result.success
    assert RUNTIME_MODE.snapshot() == (MODE_MANUAL, "anthropic")
