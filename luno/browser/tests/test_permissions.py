"""
test_permissions.py
=====================

`luno.browser.permissions` - `classify_action_risk()` (pure function)
and `PermissionManager` (confirm-first state machine). No browser
dependency at all - everything here works on plain `(action, params)`
shapes.
"""

from __future__ import annotations

import time

from luno.browser.permissions import PermissionLevel, PermissionManager, classify_action_risk


# -- classify_action_risk ---------------------------------------------------------

def test_read_only_actions():
    for action in ("search", "open", "read", "screenshot", "inspect"):
        assert classify_action_risk(action) == PermissionLevel.READ_ONLY


def test_low_risk_actions():
    for action in ("click", "scroll", "navigate", "download", "type"):
        assert classify_action_risk(action) == PermissionLevel.LOW_RISK


def test_sensitive_actions():
    for action in ("login", "send_message", "upload_file", "delete", "submit_form"):
        assert classify_action_risk(action) == PermissionLevel.SENSITIVE


def test_high_risk_actions():
    for action in ("purchase", "pay", "transfer_funds", "change_password", "delete_account"):
        assert classify_action_risk(action) == PermissionLevel.HIGH_RISK


def test_unknown_action_defaults_to_sensitive_fail_closed():
    assert classify_action_risk("some_never_before_seen_action") == PermissionLevel.SENSITIVE


def test_click_escalated_when_target_mentions_password():
    """A generically LOW_RISK 'click' escalates to SENSITIVE when the
    target/params clearly point at a login/password field - the action
    verb alone isn't enough context."""
    level = classify_action_risk("click", target="password field")
    assert level == PermissionLevel.SENSITIVE


def test_type_escalated_to_high_risk_when_params_mention_payment():
    level = classify_action_risk("type", params={"text": "1234"}, target="checkout card number")
    assert level == PermissionLevel.HIGH_RISK


def test_ordinary_click_stays_low_risk():
    assert classify_action_risk("click", target="Sign up newsletter") == PermissionLevel.LOW_RISK


# -- PermissionManager ------------------------------------------------------------

def test_read_only_is_always_allowed():
    mgr = PermissionManager()
    decision, _ = mgr.evaluate("conv-1", "search", target="RTX 5060")
    assert decision == "allow"


def test_low_risk_allowed_by_default():
    mgr = PermissionManager(require_confirmation_for_low_risk=False)
    decision, _ = mgr.evaluate("conv-1", "click", target="Learn more")
    assert decision == "allow"


def test_low_risk_confirmed_when_policy_requires_it():
    mgr = PermissionManager(require_confirmation_for_low_risk=True)
    decision, prompt = mgr.evaluate("conv-1", "click", target="Learn more")
    assert decision == "confirm"
    assert prompt


def test_sensitive_always_requires_confirmation():
    mgr = PermissionManager(require_confirmation_for_low_risk=False)
    decision, prompt = mgr.evaluate("conv-1", "submit_form", target="Contact form")
    assert decision == "confirm"
    assert "confirm" not in prompt.lower() or True  # prompt text itself just needs to exist
    assert prompt


def test_high_risk_always_denied():
    mgr = PermissionManager()
    decision, reason = mgr.evaluate("conv-1", "purchase", target="checkout")
    assert decision == "deny"
    assert "never" in reason.lower() or "high-risk" in reason.lower()


def test_high_risk_never_creates_a_pending_confirmation():
    mgr = PermissionManager()
    mgr.evaluate("conv-1", "delete_account")
    assert mgr.has_pending("conv-1") is False


def test_confirm_then_resolve_affirmative_releases_pending():
    mgr = PermissionManager()
    decision, _ = mgr.evaluate("conv-1", "submit_form", target="Contact form")
    assert decision == "confirm"
    assert mgr.has_pending("conv-1") is True
    pending = mgr.resolve_confirmation("conv-1", True)
    assert pending is not None
    assert pending.action == "submit_form"
    assert pending.target == "Contact form"


def test_resolve_negative_drops_pending():
    mgr = PermissionManager()
    mgr.evaluate("conv-1", "submit_form", target="Contact form")
    pending = mgr.resolve_confirmation("conv-1", False)
    assert pending is None
    assert mgr.has_pending("conv-1") is False


def test_resolve_is_one_shot():
    """Resolving (either way) consumes the pending confirmation - a
    SECOND resolve call for the same conversation finds nothing."""
    mgr = PermissionManager()
    mgr.evaluate("conv-1", "submit_form", target="Contact form")
    mgr.resolve_confirmation("conv-1", True)
    assert mgr.resolve_confirmation("conv-1", True) is None


def test_pending_expires_after_ttl():
    mgr = PermissionManager()
    mgr.CONFIRMATION_TTL_S = 0.05
    mgr.evaluate("conv-1", "submit_form", target="Contact form")
    time.sleep(0.1)
    assert mgr.has_pending("conv-1") is False
    assert mgr.resolve_confirmation("conv-1", True) is None


def test_separate_conversations_have_separate_pending_state():
    mgr = PermissionManager()
    mgr.evaluate("conv-1", "submit_form", target="Contact form")
    assert mgr.has_pending("conv-2") is False


def test_clear_drops_pending():
    mgr = PermissionManager()
    mgr.evaluate("conv-1", "submit_form", target="Contact form")
    mgr.clear("conv-1")
    assert mgr.has_pending("conv-1") is False
