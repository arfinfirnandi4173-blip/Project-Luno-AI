"""
permissions.py (luno.browser)
================================

The mandatory permission system (spec section 10). Two independent
pieces:

  1. `classify_action_risk()` - a pure, deterministic function mapping
     `(action, params)` to one of the four `PermissionLevel`s below. No
     state, no I/O - a lookup table plus a few keyword checks against
     `params` (e.g. a `type` action typing into a field literally named
     "password" is escalated even though "type" is normally Level 1).

  2. `PermissionManager` - the confirm-first STATE MACHINE for anything
     above Level 0. Deliberately the SAME two-turn shape
     `main_runtime_demo.py::PlannerBridgeModule._handle_environmental_
     intent()` already uses for "hawanya panas nih" -> propose -> wait
     for explicit yes on the NEXT turn -> only then act: a pending
     action is recorded (never executed yet), the caller surfaces the
     confirmation question, and only an explicit affirmative reply on
     the user's very next turn releases it. Level 3 (high risk) is
     never released by this state machine at all - `execute_after_
     confirmation()` refuses unconditionally for Level 3, per spec
     ("Always require explicit user confirmation immediately before
     execution" - interpreted here as "never autonomously, no matter
     how it's phrased," matching the spec's own Level 3 examples all
     being irreversible/financial).

This module has ZERO dependency on Playwright/`BrowserProvider` - it
only ever reasons about `(action, params)` shapes, so it's exercised
directly in tests with plain dicts, no browser at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Optional, Tuple


class PermissionLevel(IntEnum):
    READ_ONLY = 0
    LOW_RISK = 1
    SENSITIVE = 2
    HIGH_RISK = 3


#: Level 0 - automatically allowed, no state changed on the page/site.
_READ_ONLY_ACTIONS = {
    "search", "open", "read", "screenshot", "inspect", "get_page_text",
    "get_links", "get_page_title", "get_current_url", "go_back",
    "go_forward", "reload", "wait_for", "research", "monitor",
}
#: Level 1 - allowed under policy (see `BROWSER_REQUIRE_CONFIRMATION`),
#: reversible/low-consequence page interaction.
_LOW_RISK_ACTIONS = {"click", "scroll", "navigate", "download", "type", "press_key", "open_tab", "close_tab"}
#: Level 2 - ALWAYS confirmed first, regardless of `BROWSER_REQUIRE_
#: CONFIRMATION` (that flag only ever loosens Level 1, never Level 2/3).
_SENSITIVE_ACTIONS = {"login", "send_message", "upload_file", "change_setting", "delete", "submit_form"}
#: Level 3 - never performed autonomously, full stop.
_HIGH_RISK_ACTIONS = {
    "purchase", "pay", "transfer_funds", "change_password",
    "delete_account", "change_security_credentials", "destructive_system_operation",
}

#: A `type`/`click` action whose target/params mention one of these is
#: escalated a level even though the action name alone would be Level 1 -
#: "type into the password field" or "click Submit on a payment form"
#: must not slip through as ordinary low-risk interaction just because
#: the ACTION verb is generic.
_SENSITIVE_FIELD_HINTS = ("password", "login", "sign in", "signin", "credit card", "card number", "cvv", "otp", "2fa")
_HIGH_RISK_FIELD_HINTS = ("pay", "purchase", "checkout", "transfer", "bank", "wire", "delete account", "close account")


def _text_mentions_any(value: Any, hints: Tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(h in text for h in hints)


def classify_action_risk(action: str, params: Optional[Dict[str, Any]] = None, target: Optional[str] = None) -> PermissionLevel:
    """Deterministic - same `(action, params, target)` always yields the
    same level, no hidden state. Unknown actions default to `SENSITIVE`
    (fail closed: something this module has never heard of is treated
    as needing confirmation, never silently allowed)."""
    action = (action or "").strip().lower()
    params = params or {}
    haystack_bits = [target] + [str(v) for v in params.values()]
    combined = " ".join(b for b in haystack_bits if b)

    if action in _HIGH_RISK_ACTIONS or _text_mentions_any(combined, _HIGH_RISK_FIELD_HINTS):
        return PermissionLevel.HIGH_RISK
    if action in _SENSITIVE_ACTIONS or _text_mentions_any(combined, _SENSITIVE_FIELD_HINTS):
        return PermissionLevel.SENSITIVE
    if action in _LOW_RISK_ACTIONS:
        return PermissionLevel.LOW_RISK
    if action in _READ_ONLY_ACTIONS:
        return PermissionLevel.READ_ONLY
    return PermissionLevel.SENSITIVE


@dataclass
class PendingConfirmation:
    action: str
    params: Dict[str, Any]
    target: Optional[str]
    level: PermissionLevel
    prompt: str
    expires_at: float


class PermissionManager:
    """Per-conversation confirm-first gate for Level 2/3 actions. Never
    imports `BrowserProvider` - `evaluate()` only ever returns a
    decision, the CALLER (e.g. `real_browser.py`/`computer_use.py`)
    is what actually executes or refuses."""

    #: Same 120s default `PlannerBridgeModule._ENV_CONFIRMATION_TTL_S`
    #: uses - a pending confirmation nobody answered eventually expires
    #: rather than lingering forever.
    CONFIRMATION_TTL_S = 120.0

    def __init__(self, require_confirmation_for_low_risk: bool = False) -> None:
        self._require_confirmation_for_low_risk = require_confirmation_for_low_risk
        self._pending: Dict[str, PendingConfirmation] = {}

    def evaluate(
        self, conversation_key: str, action: str, params: Optional[Dict[str, Any]] = None,
        target: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Returns `(decision, prompt)`:
          - `("allow", None)`       - execute immediately.
          - `("confirm", prompt)`   - do NOT execute; surface `prompt`
                                      to the user and wait for their next
                                      reply (see `resolve_confirmation`).
          - `("deny", reason)`      - never execute this, full stop
                                      (Level 3 - `prompt` here is a
                                      human-readable refusal reason, not
                                      a question)."""
        level = classify_action_risk(action, params, target)
        if level == PermissionLevel.HIGH_RISK:
            return "deny", (
                f"'{action}' is a high-risk action (financial/destructive/security-credential class) - "
                "Luno never performs this autonomously. Tell the user this requires them to do it "
                "themselves, and confirm explicitly with them immediately before ANY such step if they "
                "insist on proceeding manually."
            )
        if level == PermissionLevel.SENSITIVE:
            prompt = f"I need to {action.replace('_', ' ')}"
            if target:
                prompt += f" ({target})"
            prompt += " - should I go ahead?"
            self._pending[conversation_key] = PendingConfirmation(
                action=action, params=dict(params or {}), target=target, level=level,
                prompt=prompt, expires_at=time.time() + self.CONFIRMATION_TTL_S,
            )
            return "confirm", prompt
        if level == PermissionLevel.LOW_RISK and self._require_confirmation_for_low_risk:
            prompt = f"I need to {action.replace('_', ' ')}"
            if target:
                prompt += f" ({target})"
            prompt += " - go ahead?"
            self._pending[conversation_key] = PendingConfirmation(
                action=action, params=dict(params or {}), target=target, level=level,
                prompt=prompt, expires_at=time.time() + self.CONFIRMATION_TTL_S,
            )
            return "confirm", prompt
        return "allow", None

    def resolve_confirmation(self, conversation_key: str, affirmative: Optional[bool]) -> Optional[PendingConfirmation]:
        """Pops and returns the pending confirmation if `affirmative` is
        `True` and it hasn't expired; otherwise pops it (if present) and
        returns `None` - one-shot either way, same "consumed regardless
        of outcome" rule `_handle_environmental_intent()` uses, so a
        stale pending action can never be accidentally released by a
        much-later unrelated "yes"."""
        pending = self._pending.pop(conversation_key, None)
        if pending is None:
            return None
        if time.time() > pending.expires_at:
            return None
        if affirmative is True:
            return pending
        return None

    def has_pending(self, conversation_key: str) -> bool:
        pending = self._pending.get(conversation_key)
        if pending is None:
            return False
        if time.time() > pending.expires_at:
            self._pending.pop(conversation_key, None)
            return False
        return True

    def clear(self, conversation_key: str) -> None:
        self._pending.pop(conversation_key, None)
