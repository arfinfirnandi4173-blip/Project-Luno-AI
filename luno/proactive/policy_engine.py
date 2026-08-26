"""
policy_engine.py
=================

`PolicyEngine` - the ONE gate every `Goal` the Goal Generator produces
must pass through before anything executes. Scores priority/risk,
enforces the spec's absolute safety rules (never auto-execute a
purchase/money-transfer/door-unlock/file-delete/message-send - those
ALWAYS require explicit confirmation, no matter how confident the
goal's own number is), enforces cooldown (time-based + a per-day cap),
and defers to Conversation Awareness (never interrupt an active
conversation/critical planner execution - queue instead).

This file makes DECISIONS only - it never touches the Planner, an
adapter, or the Event Bus. `ProactiveModule` (`manager.py`) is the only
thing that acts on a `PolicyResult`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, Optional

from .models import ContextSummary, Goal, PolicyAction, PolicyResult, ProactiveConfig, RiskLevel

#: Absolute safety denylist (spec: "Never: Purchase products, Transfer
#: money, Unlock doors, Delete files, Send messages. Execute dangerous
#: actions automatically. Such actions always require explicit
#: confirmation.") - matched as substrings against the goal's own
#: description/action_text, case-insensitive. This is intentionally a
#: fixed code rule, NOT a config knob - these are safety invariants, not
#: preferences a deployment should be able to loosen via .env.
_SAFETY_DENYLIST = (
    "buy", "purchase", "order ", "checkout",
    "transfer money", "send money", "pay ", "payment",
    "unlock", "unlock door",
    "delete", "erase", "wipe", "format",
    "send message", "send email", "send text",
)

#: Goal types considered inherently protective/low-risk by nature (a
#: SAFETY goal existing at all means something looked unsafe - taking
#: the safe action is the point) - still runs through cooldown/
#: conversation-awareness/concurrency like everything else.
_LOW_RISK_TYPES = ("safety", "welcome", "health_reminder")
_MEDIUM_RISK_TYPES = ("energy_saving", "forgotten_appliance", "night_routine", "comfort")


@dataclass
class _CooldownState:
    last_executed_at: Optional[float] = None  # time.monotonic()
    day: Optional[date] = None
    count_today: int = 0


class PolicyEngine:
    def __init__(self, config: Optional[ProactiveConfig] = None) -> None:
        self.config = config or ProactiveConfig()
        self._cooldowns: Dict[str, _CooldownState] = {}

    def reconfigure(self, config: ProactiveConfig) -> None:
        self.config = config

    # -- public API -----------------------------------------------------------

    def evaluate(self, goal: Goal, context: ContextSummary, active_goal_count: int) -> PolicyResult:
        now = datetime.now(timezone.utc)
        is_unsafe = self._matches_safety_denylist(goal)
        risk = self._assess_risk(goal, is_unsafe)
        priority = self._compute_priority(goal, context, risk)

        cooldown_blocked, cooldown_reason = self._check_cooldown(goal.cooldown_key)
        if cooldown_blocked:
            return PolicyResult(
                action=PolicyAction.DISCARD, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=False, reasoning=cooldown_reason, decided_at=now,
            )

        if is_unsafe:
            if goal.confidence >= self.config.confirmation_threshold:
                return PolicyResult(
                    action=PolicyAction.ASK_CONFIRMATION, priority=priority, risk=risk, confidence=goal.confidence,
                    requires_confirmation=True,
                    reasoning=(
                        f"Goal matches the safety denylist (action_text={goal.action_text!r}) - "
                        "never auto-executed regardless of confidence; asking for explicit confirmation."
                    ),
                    decided_at=now,
                )
            return PolicyResult(
                action=PolicyAction.DISCARD, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=True,
                reasoning="Goal matches the safety denylist and confidence is below the confirmation threshold - discarded outright.",
                decided_at=now,
            )

        if goal.confidence < self.config.confirmation_threshold:
            return PolicyResult(
                action=PolicyAction.DISCARD, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=False,
                reasoning=f"Confidence {goal.confidence:.1f}% is below CONFIRMATION_THRESHOLD ({self.config.confirmation_threshold:.1f}%) - ignored, continuing to observe.",
                decided_at=now,
            )

        if goal.confidence < self.config.auto_execution_threshold:
            return PolicyResult(
                action=PolicyAction.ASK_CONFIRMATION, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=True,
                reasoning=f"Confidence {goal.confidence:.1f}% is between CONFIRMATION_THRESHOLD and AUTO_EXECUTION_THRESHOLD - asking first.",
                decided_at=now,
            )

        # High confidence, safe, not on cooldown - but conversation
        # awareness and concurrency can still demote AUTO_EXECUTE to
        # QUEUE rather than letting it interrupt anything or pile past
        # MAX_CONCURRENT_GOALS.
        if context.conversation_active:
            return PolicyResult(
                action=PolicyAction.QUEUE, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=False,
                reasoning="High confidence and safe, but a conversation/speech is active right now - queued instead of interrupting.",
                decided_at=now,
            )
        if active_goal_count >= self.config.max_concurrent_goals:
            return PolicyResult(
                action=PolicyAction.QUEUE, priority=priority, risk=risk, confidence=goal.confidence,
                requires_confirmation=False,
                reasoning=f"MAX_CONCURRENT_GOALS ({self.config.max_concurrent_goals}) already reached - queued.",
                decided_at=now,
            )
        return PolicyResult(
            action=PolicyAction.AUTO_EXECUTE, priority=priority, risk=risk, confidence=goal.confidence,
            requires_confirmation=False,
            reasoning=f"Confidence {goal.confidence:.1f}% >= AUTO_EXECUTION_THRESHOLD ({self.config.auto_execution_threshold:.1f}%), safe, no conversation in progress - executing.",
            decided_at=now,
        )

    def record_execution(self, cooldown_key: str) -> None:
        """Called by `ProactiveModule` the moment a goal actually
        executes (not merely decided) - this is what the NEXT cycle's
        cooldown check reads."""
        if not cooldown_key:
            return
        state = self._cooldowns.setdefault(cooldown_key, _CooldownState())
        today = date.today()
        if state.day != today:
            state.day = today
            state.count_today = 0
        state.count_today += 1
        state.last_executed_at = time.monotonic()

    def cooldown_remaining_s(self, cooldown_key: str) -> float:
        state = self._cooldowns.get(cooldown_key)
        if state is None or state.last_executed_at is None:
            return 0.0
        elapsed = time.monotonic() - state.last_executed_at
        return max(0.0, self.config.cooldown_s - elapsed)

    # -- internals --------------------------------------------------------------

    def _check_cooldown(self, cooldown_key: str) -> "tuple[bool, str]":
        if not cooldown_key:
            return False, ""
        state = self._cooldowns.get(cooldown_key)
        if state is None:
            return False, ""
        today = date.today()
        if state.day == today and state.count_today >= self.config.max_executions_per_day:
            return True, f"Goal cooldown_key={cooldown_key!r} already executed {state.count_today}x today (max {self.config.max_executions_per_day}) - discarded."
        if state.last_executed_at is not None:
            elapsed = time.monotonic() - state.last_executed_at
            if elapsed < self.config.cooldown_s:
                remaining = self.config.cooldown_s - elapsed
                return True, f"Goal cooldown_key={cooldown_key!r} still on cooldown ({remaining:.0f}s remaining) - discarded."
        return False, ""

    @staticmethod
    def _matches_safety_denylist(goal: Goal) -> bool:
        haystack = f"{goal.description} {goal.action_text or ''} {goal.speech_text or ''}".lower()
        return any(word in haystack for word in _SAFETY_DENYLIST)

    @staticmethod
    def _assess_risk(goal: Goal, is_unsafe: bool) -> RiskLevel:
        if is_unsafe:
            return RiskLevel.HIGH
        type_value = goal.type.value
        if type_value in _LOW_RISK_TYPES:
            return RiskLevel.LOW
        if type_value in _MEDIUM_RISK_TYPES:
            return RiskLevel.MEDIUM
        return RiskLevel.MEDIUM

    @staticmethod
    def _compute_priority(goal: Goal, context: ContextSummary, risk: RiskLevel) -> float:
        """0-100 relative ordering (spec: "Prioritize based on: Safety,
        Urgency, User comfort, Energy efficiency, Conversation
        interruption risk, Planner workload") - used by the Dashboard/
        manager for display and tie-breaking among QUEUED goals, never
        for the AUTO/ASK/DISCARD decision itself (that's confidence +
        risk + cooldown above)."""
        score = goal.confidence
        type_value = goal.type.value
        if type_value == "safety":
            score += 20
        elif type_value in ("energy_saving", "forgotten_appliance"):
            score += 8
        elif type_value == "health_reminder":
            score += 5
        if risk == RiskLevel.HIGH:
            score -= 15
        if context.conversation_active:
            score -= 25
        if context.planner_active_task_count > 0:
            score -= min(15.0, context.planner_active_task_count * 3.0)
        return max(0.0, min(120.0, score))
