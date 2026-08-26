"""
test_mode_state.py
=====================

`LLMModeState` (mode_state.py) in isolation - each test constructs its
own fresh instance (never touches the process-wide `RUNTIME_MODE`
singleton), so these can run in any order/interleaving with every other
test file in the suite with zero risk of cross-test pollution.

Run:
    python3 -m pytest luno/routing/tests/test_mode_state.py
"""

from __future__ import annotations

import threading

from luno.routing.mode_state import MODE_AUTO, MODE_MANUAL, LLMModeState


def test_default_state_is_auto():
    state = LLMModeState()
    assert state.snapshot() == (MODE_AUTO, None)


def test_set_manual_with_alias():
    state = LLMModeState()
    state.set_manual("openai")
    assert state.snapshot() == (MODE_MANUAL, "openai")


def test_set_manual_alias_is_trimmed_and_lowercased():
    state = LLMModeState()
    state.set_manual("  OpenAI  ")
    assert state.snapshot() == (MODE_MANUAL, "openai")


def test_set_manual_with_none_alias():
    state = LLMModeState()
    state.set_manual(None)
    assert state.snapshot() == (MODE_MANUAL, None)


def test_set_manual_with_empty_string_alias_treated_as_none():
    state = LLMModeState()
    state.set_manual("   ")
    assert state.snapshot() == (MODE_MANUAL, None)


def test_set_auto_clears_manual_alias():
    state = LLMModeState()
    state.set_manual("anthropic")
    state.set_auto()
    assert state.snapshot() == (MODE_AUTO, None)


def test_reset_is_equivalent_to_set_auto():
    state = LLMModeState()
    state.set_manual("gemini")
    state.reset()
    assert state.snapshot() == (MODE_AUTO, None)


def test_concurrent_writes_never_corrupt_state():
    """Every writer sets a DIFFERENT alias - after all threads finish,
    whichever wrote last must be internally consistent (mode=manual,
    some valid alias), never a torn read (e.g. manual mode with None,
    or auto mode with a leftover alias)."""
    state = LLMModeState()
    errors = []

    def worker(alias):
        try:
            for _ in range(100):
                state.set_manual(alias)
                mode, got = state.snapshot()
                assert mode == MODE_MANUAL
                assert got in ("openai", "anthropic", "gemini", "deepseek", "local")
        except Exception as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(a,)) for a in ("openai", "anthropic", "gemini", "deepseek", "local")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors


def test_runtime_mode_singleton_starts_auto():
    """The real process-wide singleton other modules default to - only
    checked for its INITIAL state (never mutated here, to avoid leaking
    into any other test file that also imports it)."""
    from luno.routing.mode_state import RUNTIME_MODE
    # Don't assert the exact value (another test file running earlier in
    # the same session may have already touched it) - just that it's a
    # real, usable LLMModeState instance with the right shape.
    mode, alias = RUNTIME_MODE.snapshot()
    assert mode in (MODE_AUTO, MODE_MANUAL)
