"""
test_computer_use.py
======================

`luno.browser.computer_use.ComputerUseAgent` - the bounded observe/
reason/act/verify loop. `vision_analyze_fn` is faked with a canned
sequence of decision-lines (no real screenshot/vision provider call),
and `browser_provider`/`permission_manager` are faked too - this
exercises the LOOP LOGIC (parsing, permission gating, step limit,
termination), not any real browser or vision backend.
"""

from __future__ import annotations

from luno.browser.computer_use import ComputerUseAgent, _parse_decision
from luno.browser.permissions import PermissionManager


class _FakeBrowserProvider:
    def __init__(self):
        self.screenshot_calls = 0
        self.clicked = []
        self.typed = []
        self.scrolled = []
        self.navigated = []

    def screenshot(self, max_edge=None):
        self.screenshot_calls += 1
        return b"fake-png-bytes"

    def click(self, target):
        self.clicked.append(target)

    def type_text(self, target, text):
        self.typed.append((target, text))

    def scroll(self, direction="down", amount=3):
        self.scrolled.append(direction)

    def open_url(self, url):
        self.navigated.append(url)


def _scripted_vision(responses):
    """Returns a callable that yields each response in order, repeating
    the last one if called more times than provided (defensive - a bug
    in the loop calling one extra time shouldn't crash the test with an
    unhelpful StopIteration)."""
    state = {"i": 0}

    def _fn(image, prompt):
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[i]
    return _fn


# -- _parse_decision --------------------------------------------------------------

def test_parse_decision_done():
    obs, action, detail = _parse_decision("The page shows a success message.\nDONE: task complete")
    assert action == "DONE"
    assert detail == "task complete"


def test_parse_decision_click():
    obs, action, detail = _parse_decision("I see a login form.\nCLICK: Sign In button")
    assert action == "CLICK"
    assert detail == "Sign In button"


def test_parse_decision_no_action_line():
    obs, action, detail = _parse_decision("Just a description, no action format at all")
    assert action is None


# -- ComputerUseAgent.run() --------------------------------------------------------

def test_completes_immediately_when_vision_says_done():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(), vision_analyze_fn=_scripted_vision([
        "Looks fine.\nDONE: avatar is rendering correctly, no error visible",
    ]), max_steps=5)
    result = agent.run("check the avatar for errors")
    assert result.completed is True
    assert "rendering correctly" in result.final_note
    assert len(result.steps) == 1


def test_stuck_halts_the_loop():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(), vision_analyze_fn=_scripted_vision([
        "Nothing obviously actionable.\nSTUCK: no clear error dialog visible",
    ]), max_steps=5)
    result = agent.run("figure out the error")
    assert result.completed is False
    assert "no clear error dialog" in result.final_note


def test_executes_click_then_completes():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(require_confirmation_for_low_risk=False), vision_analyze_fn=_scripted_vision([
        "A dialog with an OK button is showing.\nCLICK: OK",
        "Dialog closed.\nDONE: dismissed the error dialog",
    ]), max_steps=5)
    result = agent.run("dismiss the error")
    assert result.completed is True
    assert len(provider.clicked) == 1
    assert len(result.steps) == 2


def test_max_steps_enforced():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(require_confirmation_for_low_risk=False), vision_analyze_fn=_scripted_vision([
        "Scrolling needed.\nSCROLL: down",
    ]), max_steps=3)
    result = agent.run("find the setting")
    assert result.completed is False
    assert "allowed steps" in result.final_note
    assert len(result.steps) == 3


def test_sensitive_action_halts_and_asks_for_confirmation():
    """A CLICK whose detail mentions a sensitive field must halt the
    loop and surface a confirmation prompt - never silently click
    through a login/payment-shaped element."""
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(), vision_analyze_fn=_scripted_vision([
        "A login form is visible.\nCLICK: password field",
    ]), max_steps=5)
    result = agent.run("log in")
    assert result.completed is False
    assert len(provider.clicked) == 0  # never actually clicked
    assert "confirm" in result.final_note.lower() or result.final_note


def test_high_risk_action_denied_never_executed():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(), vision_analyze_fn=_scripted_vision([
        "A checkout button is visible.\nCLICK: pay now checkout button",
    ]), max_steps=5)
    result = agent.run("buy this")
    assert result.completed is False
    assert len(provider.clicked) == 0


def test_no_vision_provider_configured():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(), vision_analyze_fn=None, max_steps=5)
    result = agent.run("do something")
    assert result.completed is False
    assert "vision" in result.final_note.lower()
    assert provider.screenshot_calls == 0


def test_type_action_parses_text_and_field():
    provider = _FakeBrowserProvider()
    agent = ComputerUseAgent(provider, PermissionManager(require_confirmation_for_low_risk=False), vision_analyze_fn=_scripted_vision([
        "A search box is visible.\nTYPE: hello world INTO: search box",
        "Typed.\nDONE: entered the search query",
    ]), max_steps=5)
    result = agent.run("search for something")
    assert result.completed is True
    assert len(provider.typed) == 1
    _, text = provider.typed[0]
    assert text == "hello world"


def test_screenshot_failure_does_not_crash():
    class _BrokenProvider(_FakeBrowserProvider):
        def screenshot(self, max_edge=None):
            raise RuntimeError("display disconnected")
    agent = ComputerUseAgent(_BrokenProvider(), PermissionManager(), vision_analyze_fn=_scripted_vision(["DONE: x"]), max_steps=5)
    result = agent.run("do something")
    assert result.completed is False
    assert "capture" in result.final_note.lower()
