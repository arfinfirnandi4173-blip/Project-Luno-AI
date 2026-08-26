"""
test_browser_wiring.py
=========================

`PlannerBridgeModule`'s browser integration (`main_runtime_demo.py`):
the pre-fetch-and-inject research/monitoring/computer-use handlers, and
the confirm-first release flow for sensitive browser actions. Same
"real bridge object, fake external dependencies" style as
`tests/test_device_context.py`.

Run:
    python3 -m pytest tests/test_browser_wiring.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main_runtime_demo as demo  # noqa: E402


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


# ============================================================================
# research intent (visible-open, no-answer redesign - Vinn's explicit
# choice: "Cukup bukain aja, gak usah jawab" - and, since Vinn separately
# confirmed via AskUserQuestion that he wants his REAL chrome.exe back
# (not Playwright's bundled Chromium), this now opens via
# `luno.desktop_control.open_url()`, not `luno.browser.provider.
# get_visible_browser_provider()` - and is therefore no longer gated on
# `BROWSER_ENABLED` at all, same as `windows.open_app` never was)
# ============================================================================

def test_research_intent_no_match_returns_none():
    bridge = _bridge()
    assert bridge._handle_browser_research_intent("halo apa kabar", "r1") is None


def test_research_intent_matched_but_browser_open_fails_tells_llm_not_to_guess(monkeypatch):
    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "open_url", lambda url: (False, "boom"))

    bridge = _bridge()
    note = bridge._handle_browser_research_intent("Carikan harga RTX 5060 Ti 16GB", "r1")
    assert note is not None
    assert "boom" in note
    assert "do not answer" in note.lower()


def test_research_intent_opens_the_real_system_browser_not_playwright(monkeypatch):
    """Proves `_handle_browser_research_intent` reaches for
    `luno.desktop_control.open_url()` - real chrome.exe/OS default
    browser - never `luno.browser.provider.get_visible_browser_
    provider()` (Playwright's bundled Chromium). Also proves it works
    with NO `BROWSER_ENABLED` env var at all, since this path no longer
    depends on that flag."""
    monkeypatch.delenv("BROWSER_ENABLED", raising=False)

    opened = []

    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "open_url", lambda url: (opened.append(url), (True, f"Membuka {url}"))[1])

    import luno.browser.provider as provider_module
    def _boom():
        raise AssertionError("must not touch the Playwright provider for research intent")
    monkeypatch.setattr(provider_module, "get_visible_browser_provider", _boom)

    bridge = _bridge()
    note = bridge._handle_browser_research_intent("Carikan harga RTX 5060 Ti 16GB", "r1")
    assert note is not None
    assert len(opened) == 1
    assert "RTX" in opened[0]
    # The whole point: no LLM-guessed answer allowed, just an
    # acknowledgment that it's open.
    assert "do not answer" in note.lower() or "do not state any facts" in note.lower()
    assert "on screen" in note.lower()


# ============================================================================
# image search intent (same real-system-browser mechanism as research
# above, separate classifier)
# ============================================================================

def test_image_search_no_match_returns_none():
    bridge = _bridge()
    assert bridge._handle_image_search_intent("gimana kabarnya", "r1") is None


def test_image_search_open_fails_returns_honest_note(monkeypatch):
    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "open_url", lambda url: (False, "boom"))

    bridge = _bridge()
    note = bridge._handle_image_search_intent("cari gambar kucing lucu", "r1")
    assert note is not None
    assert "boom" in note


def test_image_search_opens_the_real_system_browser_not_playwright(monkeypatch):
    """Same proof as `test_research_intent_opens_the_real_system_
    browser_not_playwright` - image search must also use
    `luno.desktop_control.open_url()`, not the Playwright-driven visible
    provider, and works with no `BROWSER_ENABLED` set at all."""
    monkeypatch.delenv("BROWSER_ENABLED", raising=False)

    opened = []

    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "open_url", lambda url: (opened.append(url), (True, f"Membuka {url}"))[1])

    import luno.browser.provider as provider_module
    def _boom():
        raise AssertionError("must not touch the Playwright provider for image search intent")
    monkeypatch.setattr(provider_module, "get_visible_browser_provider", _boom)

    bridge = _bridge()
    note = bridge._handle_image_search_intent("cari gambar kucing lucu", "r1")
    assert note is not None
    assert "kucing lucu" in note
    assert len(opened) == 1
    assert "kucing" in opened[0] or "tbm=isch" in opened[0]


# ============================================================================
# monitoring intent
# ============================================================================

def test_monitoring_intent_no_match_returns_none():
    bridge = _bridge()
    assert bridge._handle_monitoring_intent("gimana kabarnya", "r1") is None


def test_monitoring_intent_matched_returns_note():
    bridge = _bridge()
    note = bridge._handle_monitoring_intent("cek server", "r1")
    assert note is not None
    assert "Monitoring" in note


def test_monitoring_intent_never_crashes_with_no_targets_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_MONITOR_TARGETS_FILE", str(tmp_path / "missing.json"))
    bridge = _bridge()
    note = bridge._handle_monitoring_intent("cek server", "r1")
    assert note is not None  # local metrics line is always present


# ============================================================================
# computer-use intent
# ============================================================================

def test_computer_use_intent_no_match_returns_none():
    bridge = _bridge()
    assert bridge._handle_computer_use_intent("buka spotify", "r1", "conv-1") is None


def test_computer_use_intent_matched_but_browser_disabled(monkeypatch):
    monkeypatch.setenv("BROWSER_ENABLED", "false")
    bridge = _bridge()
    note = bridge._handle_computer_use_intent("buka Unity dan lihat kenapa avatar saya error", "r1", "conv-1")
    assert note is not None
    assert "isn't enabled" in note or "Computer-use" in note


# ============================================================================
# browser permission confirm-first release (mirrors environment_intent's
# two-turn shape)
# ============================================================================

def test_no_pending_confirmation_returns_none():
    bridge = _bridge()
    assert bridge._handle_browser_confirmation("iya", "r1", "conv-1") is None


def test_declining_a_pending_action_returns_acknowledgment_note():
    bridge = _bridge()
    bridge.browser_permissions.evaluate("conv-1", "submit_form", target="contact form")
    assert bridge.browser_permissions.has_pending("conv-1") is True
    note = bridge._handle_browser_confirmation("nggak usah", "r1", "conv-1")
    assert note is not None
    assert "declined" in note.lower()
    assert bridge.browser_permissions.has_pending("conv-1") is False


def test_unrelated_reply_leaves_pending_state_consumed_and_falls_through():
    """Neither affirmative nor negative - `_handle_environmental_intent`'s
    own documented behavior for this exact shape is "pending is
    consumed either way, `None` returned so `text` is treated as a
    fresh turn" - this proves `_handle_browser_confirmation` follows the
    same rule."""
    bridge = _bridge()
    bridge.browser_permissions.evaluate("conv-1", "submit_form", target="contact form")
    note = bridge._handle_browser_confirmation("apa kabar hari ini", "r1", "conv-1")
    assert note is None
    assert bridge.browser_permissions.has_pending("conv-1") is False


def test_confirming_a_pending_action_executes_it_through_the_real_tool_pipeline():
    """End-to-end through the real `_tool_bridge_handler` -> Event Bus ->
    `ToolManagerBridgeModule` -> `MockBrowserHandler` path - not just
    `PermissionManager` in isolation."""
    import time
    from luno.adapters import MockOpenRouterClient

    client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
    console = demo.RuntimeDemoConsole(openrouter_client=client)
    console.start()
    try:
        bridge = console.planner_module
        # "click" is normally LOW_RISK, but the real bridge's
        # `browser_permissions` is constructed with
        # `require_confirmation_for_low_risk=BROWSER_REQUIRE_CONFIRMATION`
        # (default true in .env) - matches production behavior, and
        # "click" is an action `MockBrowserHandler` actually supports
        # (unlike the SENSITIVE-only action names, which the mock
        # doesn't implement).
        decision, _ = bridge.browser_permissions.evaluate("conv-1", "click", target="Learn more")
        assert decision == "confirm"

        tool_events = []
        console.event_bus.subscribe("tool_finished", lambda e: tool_events.append(e))

        note = bridge._handle_browser_confirmation("iya", "r1", "conv-1")
        assert note is not None
        assert "confirmed" in note.lower()

        deadline = time.time() + 3
        while time.time() < deadline and not tool_events:
            time.sleep(0.02)
        assert len(tool_events) == 1
        assert tool_events[0].data["tool"] == "browser"
        assert bridge.browser_permissions.has_pending("conv-1") is False
    finally:
        console.stop()


def test_browser_permissions_cleared_on_conversation_ended():
    bridge = _bridge()
    bridge.browser_permissions.evaluate("conv-1", "submit_form", target="contact form")
    assert bridge.browser_permissions.has_pending("conv-1") is True
    bridge._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": "conv-1", "reason": "timeout"}))
    assert bridge.browser_permissions.has_pending("conv-1") is False
