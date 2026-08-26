"""
test_provider.py
==================

`luno.browser.provider` - error taxonomy, the lazy-singleton accessor,
and (since Playwright is NOT installed in this environment - confirmed
by `pip show playwright` returning nothing) a REAL exercise of
`BrowserProviderNotConfiguredError`'s "absence, not a crash" contract,
same "prove the fallback for real" spirit as this project's other
optional-dependency tests.
"""

from __future__ import annotations

import pytest

from luno.browser.provider import (
    BrowserProviderNotConfiguredError, PlaywrightBrowserProvider, Target,
    get_browser_provider, get_visible_browser_provider, reset_browser_provider,
    reset_visible_browser_provider,
)


def test_target_selector_is_not_coordinate():
    t = Target(selector="#submit")
    assert t.is_coordinate is False


def test_target_coordinates_is_coordinate():
    t = Target(x=10.0, y=20.0)
    assert t.is_coordinate is True


def test_target_neither_set_is_not_coordinate():
    t = Target()
    assert t.is_coordinate is False


def test_playwright_not_installed_raises_not_configured():
    """Playwright isn't installed in this environment - constructing
    `PlaywrightBrowserProvider` must succeed (lazy - see class
    docstring), but the first real method call must raise
    `BrowserProviderNotConfiguredError`, never a raw `ImportError`
    leaking out or a silent no-op."""
    try:
        import playwright  # noqa: F401
        pytest.skip("playwright IS installed in this environment - this test only applies to its absence")
    except ImportError:
        pass
    provider = PlaywrightBrowserProvider(headless=True, profile_dir=None, default_timeout_s=5.0, navigation_timeout_s=5.0, screenshot_max_edge=800)
    with pytest.raises(BrowserProviderNotConfiguredError):
        provider.open_url("https://example.com")


def test_close_is_idempotent_even_when_never_started():
    provider = PlaywrightBrowserProvider(headless=True, profile_dir=None, default_timeout_s=5.0, navigation_timeout_s=5.0, screenshot_max_edge=800)
    provider.close()  # must not raise
    provider.close()  # calling twice must also not raise


def test_get_browser_provider_returns_singleton():
    reset_browser_provider()
    try:
        a = get_browser_provider()
        b = get_browser_provider()
        assert a is b
    finally:
        reset_browser_provider()


def test_reset_browser_provider_creates_a_fresh_instance():
    reset_browser_provider()
    try:
        a = get_browser_provider()
        reset_browser_provider()
        b = get_browser_provider()
        assert a is not b
    finally:
        reset_browser_provider()


# -- visible (headed) provider - dedicated singleton, always headless=False --------

def test_visible_provider_is_always_headed_even_if_general_config_is_headless(monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "true")  # general config says headless
    reset_visible_browser_provider()
    try:
        provider = get_visible_browser_provider()
        assert provider._headless is False  # the VISIBLE singleton ignores that
    finally:
        reset_visible_browser_provider()


def test_visible_provider_is_a_separate_singleton_from_the_general_one():
    reset_browser_provider()
    reset_visible_browser_provider()
    try:
        general = get_browser_provider()
        visible = get_visible_browser_provider()
        assert general is not visible
    finally:
        reset_browser_provider()
        reset_visible_browser_provider()


def test_visible_provider_singleton_behavior():
    reset_visible_browser_provider()
    try:
        a = get_visible_browser_provider()
        b = get_visible_browser_provider()
        assert a is b
    finally:
        reset_visible_browser_provider()
