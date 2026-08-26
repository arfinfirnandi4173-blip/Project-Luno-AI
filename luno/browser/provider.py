"""
provider.py (luno.browser)
=============================

`BrowserProvider` - the seam between `real_browser.py`/`research.py`/
`monitoring.py`/`computer_use.py` and whichever actual browser
automation library drives the page. Same role `luno/vision_provider.py`'s
`VisionProvider` plays for "what's in this image" - nothing outside this
module ever imports Playwright directly or touches a Playwright
`Page`/`Browser` object; every method here takes/returns plain Python
types (str/bytes/dict/list), so a caller (or a test) can substitute any
object with matching methods with zero Playwright dependency.

`PlaywrightBrowserProvider` is a LAZY SINGLETON per this project's
existing provider convention (construction is cheap/config-only;
`_ensure_started()` is what actually launches a browser process, on
first real use, not at import/construction time) - mirrors
`GeminiVisionProvider`'s lazy `_get_session()`. One persistent browser
context is reused across calls (spec section 14/22: "prefer a
controlled persistent browser session," "do not create a new browser
process for every single request") - `close()` is the only thing that
tears it down.

Playwright itself (`pip install playwright && playwright install
chromium`) is an OPTIONAL dependency, imported lazily inside
`_ensure_started()` - importing this module, or even constructing
`PlaywrightBrowserProvider`, never requires it to be installed; only
actually calling a method that needs the browser running does. Absence
raises `BrowserProviderNotConfiguredError`, same "not configured, not a
crash" contract `VisionProviderNotConfiguredError` uses.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# -- errors ---------------------------------------------------------------------

class BrowserProviderError(Exception):
    """Base class for every failure a `BrowserProvider` method can
    raise - callers that don't care about the specific reason can catch
    just this, same "callers can catch the base or the specific
    subclass" contract `VisionProviderError` establishes."""


class BrowserProviderNotConfiguredError(BrowserProviderError):
    """Playwright isn't installed, or `BROWSER_ENABLED` is off - never
    even attempted."""


class BrowserProviderTimeoutError(BrowserProviderError):
    """The action was attempted but didn't complete within the
    configured timeout (navigation, waiting for a selector, ...)."""


class BrowserProviderNavigationError(BrowserProviderError):
    """A page failed to load (DNS failure, connection refused, TLS
    error, HTTP error page, ...)."""


class BrowserProviderSelectorError(BrowserProviderError):
    """A `click`/`type_text` target (CSS selector or coordinates)
    couldn't be resolved to a real, interactable element."""


class BrowserProviderDomainBlockedError(BrowserProviderError):
    """`BROWSER_ALLOWED_DOMAINS` is configured and this URL's host isn't
    on it - raised by `real_browser.py` (which owns the allowlist
    check), never by the provider itself (the provider has no opinion on
    policy, only on execution)."""


@dataclass(frozen=True)
class Target:
    """A click/type target - EITHER a semantic selector (preferred,
    spec section 9: "prefer semantic selectors when possible") OR
    absolute page coordinates (fallback for vision-guided computer-use
    when no reliable selector exists). Exactly one should be set."""
    selector: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None

    @property
    def is_coordinate(self) -> bool:
        return self.selector is None and self.x is not None and self.y is not None


# -- protocol ---------------------------------------------------------------------

@runtime_checkable
class BrowserProvider(Protocol):
    def open_url(self, url: str) -> None: ...
    def go_back(self) -> None: ...
    def go_forward(self) -> None: ...
    def reload(self) -> None: ...
    def get_page_title(self) -> str: ...
    def get_current_url(self) -> str: ...
    def get_page_text(self) -> str: ...
    def get_links(self) -> List[Dict[str, str]]: ...
    def screenshot(self, max_edge: Optional[int] = None) -> bytes: ...
    def click(self, target: Target) -> None: ...
    def type_text(self, target: Target, text: str) -> None: ...
    def press_key(self, key: str) -> None: ...
    def scroll(self, direction: str = "down", amount: int = 3) -> None: ...
    def wait_for(self, selector: str, timeout_s: Optional[float] = None) -> bool: ...
    def download(self, url: str, destination_path: str) -> str: ...
    def new_tab(self) -> None: ...
    def close_tab(self) -> None: ...
    def close(self) -> None: ...


# -- Playwright implementation ----------------------------------------------------

class PlaywrightBrowserProvider:
    """Structurally typed against `BrowserProvider` above. Every method
    that touches the live page is guarded by `self._lock` - a single
    shared browser context is not thread-safe for concurrent
    navigation/interaction, and this project's tool calls can arrive
    from more than one request thread (see `manager.py`'s own threading
    model)."""

    def __init__(
        self,
        headless: Optional[bool] = None,
        profile_dir: Optional[str] = None,
        default_timeout_s: Optional[float] = None,
        navigation_timeout_s: Optional[float] = None,
        screenshot_max_edge: Optional[int] = None,
    ) -> None:
        if headless is None or profile_dir is None or default_timeout_s is None or navigation_timeout_s is None or screenshot_max_edge is None:
            from .config import BrowserConfig
            cfg = BrowserConfig.from_env()
            headless = headless if headless is not None else cfg.headless
            profile_dir = profile_dir if profile_dir is not None else cfg.profile_dir
            default_timeout_s = default_timeout_s if default_timeout_s is not None else cfg.default_timeout_s
            navigation_timeout_s = navigation_timeout_s if navigation_timeout_s is not None else cfg.navigation_timeout_s
            screenshot_max_edge = screenshot_max_edge if screenshot_max_edge is not None else cfg.screenshot_max_edge

        self._headless = headless
        self._profile_dir = profile_dir or None
        self._default_timeout_s = default_timeout_s
        self._navigation_timeout_s = navigation_timeout_s
        self._screenshot_max_edge = screenshot_max_edge

        self._lock = threading.Lock()
        self._playwright: Any = None
        self._browser_context: Any = None
        self._page: Any = None

    # -- lifecycle --------------------------------------------------------------

    def _ensure_started(self) -> Any:
        """Launches the browser on first use only. A persistent context
        (`profile_dir` set) keeps cookies/local-storage across restarts
        so Vinn doesn't need to log in repeatedly (spec section 14);
        without one, an ephemeral in-memory context is used (still one
        long-lived process across calls within this run, just not
        persisted to disk)."""
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as ex:
            raise BrowserProviderNotConfiguredError(
                "playwright isn't installed - run 'pip install playwright' then "
                "'playwright install chromium' to enable real browser control"
            ) from ex

        self._playwright = sync_playwright().start()
        try:
            if self._profile_dir:
                os.makedirs(self._profile_dir, exist_ok=True)
                self._browser_context = self._playwright.chromium.launch_persistent_context(
                    self._profile_dir, headless=self._headless,
                )
            else:
                browser = self._playwright.chromium.launch(headless=self._headless)
                self._browser_context = browser.new_context()
            self._browser_context.set_default_timeout(self._default_timeout_s * 1000)
            self._page = self._browser_context.pages[0] if self._browser_context.pages else self._browser_context.new_page()
        except Exception:
            self._teardown_playwright()
            raise
        return self._page

    def _teardown_playwright(self) -> None:
        try:
            if self._browser_context is not None:
                self._browser_context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._browser_context = None
        self._playwright = None
        self._page = None

    def close(self) -> None:
        """Idempotent - safe to call even if the browser was never
        started (resource-management requirement, spec section 22:
        "browser process is terminated cleanly on shutdown")."""
        with self._lock:
            self._teardown_playwright()

    # -- navigation ---------------------------------------------------------------

    def open_url(self, url: str) -> None:
        with self._lock:
            page = self._ensure_started()
            try:
                page.goto(url, timeout=self._navigation_timeout_s * 1000)
            except Exception as ex:
                message = str(ex)
                if "Timeout" in message or "timeout" in message:
                    raise BrowserProviderTimeoutError(f"navigating to {url!r} timed out: {ex}") from ex
                raise BrowserProviderNavigationError(f"couldn't open {url!r}: {ex}") from ex

    def go_back(self) -> None:
        with self._lock:
            self._ensure_started().go_back(timeout=self._navigation_timeout_s * 1000)

    def go_forward(self) -> None:
        with self._lock:
            self._ensure_started().go_forward(timeout=self._navigation_timeout_s * 1000)

    def reload(self) -> None:
        with self._lock:
            self._ensure_started().reload(timeout=self._navigation_timeout_s * 1000)

    def get_page_title(self) -> str:
        with self._lock:
            return self._ensure_started().title()

    def get_current_url(self) -> str:
        with self._lock:
            return self._ensure_started().url

    def get_page_text(self) -> str:
        with self._lock:
            page = self._ensure_started()
            try:
                return page.inner_text("body")
            except Exception as ex:
                raise BrowserProviderSelectorError(f"couldn't read page text: {ex}") from ex

    def get_links(self) -> List[Dict[str, str]]:
        with self._lock:
            page = self._ensure_started()
            try:
                raw = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
                )
            except Exception as ex:
                raise BrowserProviderSelectorError(f"couldn't read links: {ex}") from ex
            return [{"text": (r.get("text") or "")[:200], "href": r.get("href") or ""} for r in (raw or [])]

    # -- visual ---------------------------------------------------------------------

    def screenshot(self, max_edge: Optional[int] = None) -> bytes:
        """Full-page PNG, downscaled so its longest edge is at most
        `max_edge` (default: `BROWSER_SCREENSHOT_MAX_EDGE`) - resource-
        management requirement (spec section 22: screenshots/image
        buffers must not accumulate/balloon RAM); downscaling once here,
        before the bytes ever leave this method, is cheaper than every
        caller doing it themselves."""
        with self._lock:
            page = self._ensure_started()
            try:
                raw = page.screenshot(full_page=False, type="png")
            except Exception as ex:
                raise BrowserProviderError(f"screenshot failed: {ex}") from ex
        return _downscale_png(raw, max_edge or self._screenshot_max_edge)

    # -- interaction ------------------------------------------------------------

    def click(self, target: "Target") -> None:
        with self._lock:
            page = self._ensure_started()
            try:
                if target.is_coordinate:
                    page.mouse.click(target.x, target.y)
                else:
                    page.click(target.selector, timeout=self._default_timeout_s * 1000)
            except Exception as ex:
                raise BrowserProviderSelectorError(f"couldn't click {target}: {ex}") from ex

    def type_text(self, target: "Target", text: str) -> None:
        with self._lock:
            page = self._ensure_started()
            try:
                if target.is_coordinate:
                    page.mouse.click(target.x, target.y)
                    page.keyboard.type(text)
                else:
                    page.fill(target.selector, text, timeout=self._default_timeout_s * 1000)
            except Exception as ex:
                raise BrowserProviderSelectorError(f"couldn't type into {target}: {ex}") from ex

    def press_key(self, key: str) -> None:
        with self._lock:
            self._ensure_started().keyboard.press(key)

    def scroll(self, direction: str = "down", amount: int = 3) -> None:
        with self._lock:
            page = self._ensure_started()
            delta = amount * 200
            dx, dy = {
                "down": (0, delta), "up": (0, -delta),
                "left": (-delta, 0), "right": (delta, 0),
            }.get((direction or "down").lower(), (0, delta))
            page.mouse.wheel(dx, dy)

    def wait_for(self, selector: str, timeout_s: Optional[float] = None) -> bool:
        with self._lock:
            page = self._ensure_started()
        try:
            page.wait_for_selector(selector, timeout=(timeout_s or self._default_timeout_s) * 1000)
            return True
        except Exception:
            return False

    def download(self, url: str, destination_path: str) -> str:
        """Caller (`real_browser.py`) is responsible for having already
        validated `destination_path` via `security.validate_download_
        path()` - this method trusts the path it's given and just
        performs the download, never executes the downloaded file (spec
        section 13)."""
        with self._lock:
            page = self._ensure_started()
            try:
                with page.expect_download(timeout=self._navigation_timeout_s * 1000) as dl_info:
                    page.evaluate("url => window.location.href = url", url)
                download = dl_info.value
                download.save_as(destination_path)
            except Exception as ex:
                raise BrowserProviderError(f"download of {url!r} failed: {ex}") from ex
        return destination_path

    def new_tab(self) -> None:
        with self._lock:
            self._ensure_started()  # ensure context exists
            self._page = self._browser_context.new_page()

    def close_tab(self) -> None:
        with self._lock:
            if self._page is None:
                return
            try:
                self._page.close()
            except Exception:
                pass
            remaining = self._browser_context.pages if self._browser_context is not None else []
            self._page = remaining[0] if remaining else (self._browser_context.new_page() if self._browser_context is not None else None)


def _downscale_png(raw: bytes, max_edge: int) -> bytes:
    """Best-effort downscale via Pillow if available; returns `raw`
    unchanged if Pillow isn't installed or decoding fails (never raises -
    a full-resolution screenshot is still a valid, if larger, result)."""
    if not raw or max_edge <= 0:
        return raw
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        width, height = img.size
        longest = max(width, height)
        if longest <= max_edge:
            return raw
        scale = max_edge / float(longest)
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return raw


_singleton_lock = threading.Lock()
_singleton: Optional[PlaywrightBrowserProvider] = None


def get_browser_provider() -> PlaywrightBrowserProvider:
    """Lazy singleton accessor - same pattern this project's other
    lazily-constructed shared resources use (e.g. `luno.vision`'s YOLO
    model singleton). Constructing the object is cheap (config read
    only); the actual browser process only launches on first real
    method call (see `_ensure_started()`)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PlaywrightBrowserProvider()
        return _singleton


def reset_browser_provider() -> None:
    """Closes and drops the singleton - used by shutdown handling and by
    tests that need a clean slate between cases."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
        _singleton = None


_visible_singleton_lock = threading.Lock()
_visible_singleton: Optional[PlaywrightBrowserProvider] = None


def get_visible_browser_provider() -> PlaywrightBrowserProvider:
    """A SEPARATE lazy singleton, always headed (`headless=False`),
    regardless of `BROWSER_HEADLESS` - for actions whose entire point is
    that Vinn sees them happen on screen (e.g. "cari gambar kucing lucu"
    - an image search someone asked for specifically to LOOK at, not to
    have Luno read/summarize). Kept deliberately separate from
    `get_browser_provider()`'s general-purpose (usually headless,
    background-automation) singleton so turning THIS one on can never
    accidentally make background monitoring/research pop up a visible
    window too, and vice versa - the two browser processes are fully
    independent."""
    global _visible_singleton
    with _visible_singleton_lock:
        if _visible_singleton is None:
            from .config import BrowserConfig
            cfg = BrowserConfig.from_env()
            _visible_singleton = PlaywrightBrowserProvider(
                headless=False, profile_dir=cfg.profile_dir,
                default_timeout_s=cfg.default_timeout_s, navigation_timeout_s=cfg.navigation_timeout_s,
                screenshot_max_edge=cfg.screenshot_max_edge,
            )
        return _visible_singleton


def reset_visible_browser_provider() -> None:
    global _visible_singleton
    with _visible_singleton_lock:
        if _visible_singleton is not None:
            try:
                _visible_singleton.close()
            except Exception:
                pass
        _visible_singleton = None
