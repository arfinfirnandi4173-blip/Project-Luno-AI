"""
real_browser.py
================

`RealBrowserHandler` - the ONLY place structured browser actions ever
turn into real `BrowserProvider` calls. This is the mandatory
architectural boundary spec section 27 asks for:

    LLM -> Browser Tool -> BrowserProvider -> Playwright/browser

never `LLM -> arbitrary Python/shell -> computer`. `execute()` only ever
dispatches one of a closed, enumerated set of actions (`_SUPPORTED_
ACTIONS`) to a matching, equally-fixed `BrowserProvider` method - there
is no code path here that runs a string the LLM produced as a command.

Permission enforcement happens HERE too, as the last line of defense,
even though the intended primary gate is the confirm-first flow one
layer up (`PermissionManager`/`main_runtime_demo.py`'s wiring - mirrors
`_handle_environmental_intent()`'s two-turn state machine): a
HIGH_RISK action is refused unconditionally, no matter how it arrived
here; a SENSITIVE action is refused UNLESS `tool_call.parameters
["confirmed"]` is `True` - a flag only the confirm-first orchestration
layer is meant to ever set, never the LLM directly (nothing here parses
that flag out of free-text; it has to be set programmatically).

Every `ToolResult.message` is passed through `security.redact_secrets()`
before being returned - so even if a page's title/text happens to
contain something secret-shaped, it never round-trips into the LLM
prompt verbatim (spec section 11/20).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult

_SUPPORTED_ACTIONS = [
    "open", "navigate", "search", "refresh", "close",
    "read", "screenshot", "click", "type", "scroll", "download", "inspect",
    "go_back", "go_forward", "press_key",
]
_ACTIONS_REQUIRING_TARGET = {"navigate", "click", "type", "download"}


class RealBrowserHandler(ToolHandler):
    name = "browser"
    default_timeout_s = 20.0
    max_timeout_s = 45.0

    def __init__(self, provider: Any, allowed_domains: Optional[List[str]] = None) -> None:
        """`provider` - anything duck-typed as `luno.browser.provider.
        BrowserProvider` (kept as `Any` so this module never hard-
        imports Playwright, same convention `RealCameraPTZHandler`
        uses for `pytapo.Tapo`). `allowed_domains` defaults to
        `BrowserConfig.from_env().allowed_domains` if not given.

        Sprint 66 (Tool Boundary Hardening): also validates the
        CONFIGURED download directory here, at construction time, via
        `luno.browser.security.validate_download_directory()` - fails
        closed by raising if it's unsafe (overlaps the source tree or a
        critical project file - see that function's own docstring for
        the exact invariant, and Sprint 65's Finding SPRINT65-002 for
        why this exists). Raising here needs no new bootstrap plumbing:
        `luno/bootstrap/adapters.py::_register_real_browser_handler()`
        already wraps this constructor in a try/except that falls back
        to "stay mocked" on ANY registration failure - reused as-is.
        This is the STARTUP layer only; `_dispatch()`'s own `"download"`
        branch re-validates on every call too (config is reloadable
        without a restart per this package's own convention - see
        `browser/config.py`'s docstring - so the startup check alone
        would miss a later reconfiguration)."""
        self._provider = provider
        if allowed_domains is None:
            from luno.browser.config import BrowserConfig
            allowed_domains = list(BrowserConfig.from_env().allowed_domains)
        self._allowed_domains = allowed_domains

        from luno.browser.config import BrowserConfig
        from luno.browser.security import validate_download_directory
        configured_download_dir = BrowserConfig.from_env().download_dir
        ok, reason = validate_download_directory(configured_download_dir)
        if not ok:
            raise ValueError(
                f"refusing to register the real browser tool handler: BROWSER_DOWNLOAD_DIR "
                f"is unsafe ({reason}). Configured path: {configured_download_dir!r}. "
                f"Expected boundary: outside the luno/ source package and disjoint from every "
                f"config/*.json, .env, and root-level launcher file."
            )

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        if tool_call.action in _ACTIONS_REQUIRING_TARGET and not (tool_call.target or tool_call.parameters.get("url") or tool_call.parameters.get("selector")):
            return f"Action '{tool_call.action}' needs a target (URL/selector)"
        if tool_call.action == "type" and not tool_call.parameters.get("text"):
            return "Action 'type' needs parameters.text"
        return None

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        from luno.browser.permissions import PermissionLevel, classify_action_risk
        from luno.browser.security import is_domain_allowed, redact_secrets

        action = tool_call.action
        target = tool_call.target or tool_call.parameters.get("url") or tool_call.parameters.get("selector")
        params = tool_call.parameters or {}
        start = time.time()

        level = classify_action_risk(action, params, target)
        if level == PermissionLevel.HIGH_RISK:
            return ToolResult.fail(
                self.name, action,
                "That's a high-risk action (financial/destructive/security-credential class) - "
                "I don't perform this autonomously. You'll need to do it yourself.",
                error_type="HighRiskBlocked", retryable=False,
            )
        if level == PermissionLevel.SENSITIVE and not params.get("confirmed"):
            return ToolResult.fail(
                self.name, action,
                f"'{action}' needs your confirmation before I do it - I haven't performed it yet.",
                error_type="ConfirmationRequired", retryable=False,
            )

        url_actions = {"navigate", "open", "download"}
        if action in url_actions and target:
            allowed, reason = is_domain_allowed(str(target), self._allowed_domains)
            if not allowed:
                return ToolResult.fail(
                    self.name, action, f"That domain isn't allowed ({reason}).",
                    error_type="DomainBlocked", retryable=False,
                )

        try:
            result = self._dispatch(action, target, params)
        except Exception as ex:
            from luno.browser.provider import (
                BrowserProviderNavigationError, BrowserProviderNotConfiguredError,
                BrowserProviderSelectorError, BrowserProviderTimeoutError,
            )
            duration_ms = (time.time() - start) * 1000
            if isinstance(ex, BrowserProviderTimeoutError):
                return ToolResult.fail(self.name, action, redact_secrets(f"Timed out: {ex}"), error_type="BrowserTimeout", retryable=True, execution_time_ms=duration_ms)
            if isinstance(ex, BrowserProviderNavigationError):
                return ToolResult.fail(self.name, action, redact_secrets(f"Couldn't load the page: {ex}"), error_type="BrowserNavigationError", retryable=True, execution_time_ms=duration_ms)
            if isinstance(ex, BrowserProviderSelectorError):
                return ToolResult.fail(self.name, action, redact_secrets(f"Couldn't find that on the page: {ex}"), error_type="BrowserSelectorError", retryable=False, execution_time_ms=duration_ms)
            if isinstance(ex, BrowserProviderNotConfiguredError):
                return ToolResult.fail(self.name, action, "Browser isn't available right now.", error_type="BrowserNotConfigured", retryable=False, execution_time_ms=duration_ms)
            return ToolResult.fail(self.name, action, redact_secrets(f"Browser action failed: {ex}"), error_type="BrowserError", retryable=True, execution_time_ms=duration_ms)

        duration_ms = (time.time() - start) * 1000
        message, data = result
        return ToolResult.ok(self.name, action, redact_secrets(message), data=data, execution_time_ms=duration_ms)

    def _dispatch(self, action: str, target: Optional[str], params: Dict[str, Any]):
        p = self._provider
        if action == "open":
            url = target or "about:blank"
            p.open_url(url)
            return f"Opened {url}", {"url": url}
        if action == "navigate":
            p.open_url(target)
            return f"Navigated to {target}", {"url": target}
        if action == "search":
            query = params.get("query") or target
            search_url = f"https://duckduckgo.com/?q={_url_quote(query)}"
            p.open_url(search_url)
            return f"Searched for '{query}'", {"query": query, "url": search_url}
        if action == "refresh":
            p.reload()
            return "Page refreshed", {}
        if action == "go_back":
            p.go_back()
            return "Went back", {}
        if action == "go_forward":
            p.go_forward()
            return "Went forward", {}
        if action == "close":
            p.close()
            return "Browser closed", {}
        if action == "read":
            text = p.get_page_text()
            title = p.get_page_title()
            url = p.get_current_url()
            return f"Read page '{title}'", {"text": text, "title": title, "url": url}
        if action == "screenshot":
            image = p.screenshot()
            return "Screenshot captured", {"image_bytes": len(image or b""), "image": image}
        if action == "click":
            from luno.browser.provider import Target
            t = self._make_target(target, params)
            p.click(t)
            return f"Clicked {target or params.get('selector')}", {}
        if action == "type":
            from luno.browser.provider import Target
            t = self._make_target(target, params)
            p.type_text(t, params.get("text", ""))
            return f"Typed into {target or params.get('selector')}", {}
        if action == "press_key":
            key = params.get("key") or target or "Enter"
            p.press_key(key)
            return f"Pressed {key}", {}
        if action == "scroll":
            p.scroll(params.get("direction", "down"), int(params.get("amount", 3)))
            return f"Scrolled {params.get('direction', 'down')}", {}
        if action == "download":
            url = target or params.get("url")
            from luno.browser.security import validate_download_directory, validate_download_path
            from luno.browser.config import BrowserConfig
            from luno import mutation_audit
            cfg = BrowserConfig.from_env()
            # Sprint 66: defense-in-depth re-check of the DIRECTORY
            # itself, on every call - not just once at __init__ time.
            # `BrowserConfig.from_env()` is deliberately re-read fresh
            # per call (this package's own "reloadable without a
            # restart" convention), so a configuration change made after
            # startup would otherwise bypass the constructor-time check
            # entirely.
            dir_ok, dir_reason = validate_download_directory(cfg.download_dir)
            if not dir_ok:
                raise ValueError(f"download rejected: BROWSER_DOWNLOAD_DIR is unsafe ({dir_reason})")
            destination = params.get("filename") or _filename_from_url(url)
            ok, resolved = validate_download_path(destination, cfg.download_dir)
            if not ok:
                raise ValueError(f"download rejected: {resolved}")

            # Sprint 67 (Mutation Audit Trail, Phase 8): a correlation ID
            # local to this single dispatch call, so a future forensic
            # read of the audit trail can tell "one download attempt"
            # apart from another - the smallest safe correlation
            # mechanism this project needs (Phase 9 - no second tracing
            # system, no change to ToolCall/ToolManager's own shape).
            correlation_id = uuid.uuid4().hex[:16]
            before = mutation_audit.snapshot(resolved, mutation_audit.PathCategory.STANDARD)
            download_success = False
            try:
                path = p.download(url, resolved)
                download_success = True
            finally:
                after = mutation_audit.snapshot(resolved, mutation_audit.PathCategory.STANDARD)
                mutation_audit.record_mutation(
                    operation="download", path=resolved, category=mutation_audit.PathCategory.STANDARD,
                    source_component="browser", source_operation="_dispatch:download",
                    tool_name="browser", action_name="download", correlation_id=correlation_id,
                    before=before, after=after, success=download_success,
                )
            return f"Downloaded to {path}", {"url": url, "path": path}
        if action == "inspect":
            title = p.get_page_title()
            url = p.get_current_url()
            return f"Current page: '{title}'", {"title": title, "url": url}
        raise ValueError(f"Unhandled action '{action}'")

    @staticmethod
    def _make_target(target: Optional[str], params: Dict[str, Any]):
        from luno.browser.provider import Target
        coords = params.get("coordinates")
        if coords and isinstance(coords, (list, tuple)) and len(coords) == 2:
            return Target(x=float(coords[0]), y=float(coords[1]))
        selector = params.get("selector") or target
        return Target(selector=selector)


def _url_quote(text: Optional[str]) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text or "")


def _filename_from_url(url: Optional[str]) -> str:
    from urllib.parse import urlparse
    import os
    name = os.path.basename(urlparse(url or "").path) or "download"
    return name
