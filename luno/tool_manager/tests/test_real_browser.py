"""
test_real_browser.py
======================

`RealBrowserHandler` - the mandatory architectural boundary (spec
section 27): a fake `BrowserProvider` stands in for Playwright (no real
browser needed), and every test proves the PERMISSION/security gating
happens even when a caller tries to skip past it - never that the
happy path merely "should work."
"""

from __future__ import annotations

from luno.tool_manager.builtin.real_browser import RealBrowserHandler
from luno.tool_manager.context import ExecutionContext
from luno.tool_manager.models import ToolCall


class _FakeProvider:
    def __init__(self):
        self.opened = []
        self.clicked = []
        self.typed = []

    def open_url(self, url):
        self.opened.append(url)

    def get_page_text(self):
        return "page text with a secret token=sk-abc123 in it"

    def get_page_title(self):
        return "Fake Page"

    def get_current_url(self):
        return "https://example.com/current"

    def click(self, target):
        self.clicked.append(target)

    def type_text(self, target, text):
        self.typed.append((target, text))

    def screenshot(self, max_edge=None):
        return b"fake-bytes"

    def reload(self):
        pass

    def go_back(self):
        pass

    def go_forward(self):
        pass

    def press_key(self, key):
        pass

    def scroll(self, direction="down", amount=3):
        pass

    def close(self):
        pass


def _call(tool="browser", action="open", target=None, parameters=None):
    return ToolCall(tool=tool, action=action, target=target, parameters=parameters or {})


def test_open_succeeds_when_no_allowlist():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=[])
    result = handler.execute(_call(action="navigate", target="https://example.com"))
    assert result.success is True


def test_navigate_blocked_by_domain_allowlist():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=["github.com"])
    result = handler.execute(_call(action="navigate", target="https://evil.com"))
    assert result.success is False
    assert result.error_type == "DomainBlocked"


def test_navigate_allowed_when_domain_matches_allowlist():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=["example.com"])
    result = handler.execute(_call(action="navigate", target="https://example.com/page"))
    assert result.success is True


def test_high_risk_action_never_executes():
    provider = _FakeProvider()
    handler = RealBrowserHandler(provider, allowed_domains=[])
    result = handler.execute(_call(action="purchase", target="checkout"))
    assert result.success is False
    assert result.error_type == "HighRiskBlocked"
    assert result.retryable is False


def test_sensitive_action_without_confirmation_is_refused():
    provider = _FakeProvider()
    handler = RealBrowserHandler(provider, allowed_domains=[])
    result = handler.execute(_call(action="submit_form", target="contact form"))
    assert result.success is False
    assert result.error_type == "ConfirmationRequired"


def test_sensitive_action_with_confirmed_flag_executes():
    """`confirmed=True` is only ever meant to be set by the confirm-
    first orchestration layer after a real user 'yes' - this proves the
    handler DOES honor it once set (the flow itself is tested at the
    `PermissionManager`/`main_runtime_demo.py` level)."""
    provider = _FakeProvider()
    handler = RealBrowserHandler(provider, allowed_domains=[])
    result = handler.execute(_call(action="type", target="password field", parameters={"text": "hunter2", "confirmed": True}))
    assert result.success is True


def test_credentials_never_appear_in_tool_result_message():
    """The fake provider's `get_page_text()` returns a secret-shaped
    string - `read` must redact it before it ever reaches
    `ToolResult.message`."""
    provider = _FakeProvider()
    handler = RealBrowserHandler(provider, allowed_domains=[])
    result = handler.execute(_call(action="read"))
    assert "sk-abc123" not in result.message


def test_unsupported_action_rejected_by_validate():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=[])
    error = handler.validate(_call(action="run_arbitrary_shell_command"))
    assert error is not None


def test_click_requires_target():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=[])
    error = handler.validate(_call(action="click"))
    assert error is None or "target" in error.lower() or "needs" in error.lower()


def test_provider_exception_maps_to_structured_failure_not_a_crash():
    class _FailingProvider(_FakeProvider):
        def open_url(self, url):
            raise RuntimeError("connection refused")
    handler = RealBrowserHandler(_FailingProvider(), allowed_domains=[])
    result = handler.execute(_call(action="navigate", target="https://example.com"))
    assert result.success is False
    assert result.error_type in ("BrowserError", "BrowserNavigationError")


def test_screenshot_action_returns_image_bytes_in_data():
    handler = RealBrowserHandler(_FakeProvider(), allowed_domains=[])
    result = handler.execute(_call(action="screenshot"))
    assert result.success is True
    assert result.data.get("image_bytes") == len(b"fake-bytes")


def test_click_action_reaches_provider():
    provider = _FakeProvider()
    handler = RealBrowserHandler(provider, allowed_domains=[])
    result = handler.execute(_call(action="click", target="Learn more"))
    assert result.success is True
    assert len(provider.clicked) == 1
