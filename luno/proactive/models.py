"""
models.py
=========

Sprint 10 - Proactive Intelligence & Autonomous Goal Planner.

Every data type shared across this package, plus `ProactiveConfig` (env-
var only, reloadable - same convention `wake_session.models.
WakeSessionConfig`/`barge_in.models.BargeInConfig` already established).

Nothing here talks to the Event Bus, Vision Memory, or the Planner -
pure data, same "models.py never does I/O" rule as every other package
in this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalType(str, Enum):
    """Coarse category - drives which rule in `goal_generator.py`
    produced it and which speech/action template applies. `OTHER` is the
    safe fallback for anything not covered by a named rule."""
    WELCOME = "welcome"
    COMFORT = "comfort"
    ENERGY_SAVING = "energy_saving"
    SAFETY = "safety"
    HEALTH_REMINDER = "health_reminder"
    FORGOTTEN_APPLIANCE = "forgotten_appliance"
    ASSISTANCE_OFFER = "assistance_offer"
    NIGHT_ROUTINE = "night_routine"
    OTHER = "other"


class GoalStatus(str, Enum):
    """Lifecycle a `Goal` moves through - what the Dashboard's Goals
    panel groups by (Active/Queued/Completed/Rejected + the
    confirmation-pending bucket the spec's "manual approval" feature
    needs)."""
    PENDING = "pending"                          # generated, not yet policy-evaluated
    QUEUED = "queued"                             # policy said yes, but conversation/planner busy
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # medium confidence - needs a human yes/no
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"                         # discarded by policy (unsafe/low confidence/cooldown)
    EXPIRED = "expired"                           # queued too long / context changed / plan failed validation


class PolicyAction(str, Enum):
    """What the Policy Engine decided to do with a given `Goal` this
    cycle - see `policy_engine.PolicyEngine.evaluate()`."""
    AUTO_EXECUTE = "auto_execute"
    ASK_CONFIRMATION = "ask_confirmation"
    QUEUE = "queue"
    DISCARD = "discard"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class HumanContext:
    """One tracked human, trimmed down from `vision_memory.models.
    TrackedHuman` to just what goal-generation rules actually reason
    over - kept here (not a live `TrackedHuman` reference) so the whole
    `ContextSummary` stays a plain, serializable snapshot (Dashboard-
    friendly, test-friendly, no accidental live-object aliasing)."""
    id: str
    identity: Optional[str]
    activity: str
    seconds_in_current_activity: float
    seconds_since_last_seen: float


@dataclass
class ObjectContext:
    id: str
    label: str
    location: Optional[str]
    status: str


@dataclass
class ContextSummary:
    """What the Context Evaluator produces every cycle - the ONLY input
    `GoalGenerator.generate()` sees. Deliberately flat/plain (no live
    object references) so it is cheap to build, cheap to log, and
    trivial to hand-construct in tests without touching Vision Memory,
    the Planner, or any adapter."""
    generated_at: datetime
    hour_of_day: int
    time_bucket: str  # "morning" | "afternoon" | "evening" | "night"

    user_present: bool
    humans: List[HumanContext] = field(default_factory=list)
    objects: List[ObjectContext] = field(default_factory=list)
    light_on: Optional[bool] = None
    door_closed: Optional[bool] = None

    recent_vision_event_descriptions: List[str] = field(default_factory=list)
    long_term_facts: List[str] = field(default_factory=list)
    session_summary_count: int = 0

    session_state: str = "unknown"
    session_busy: bool = False       # conversation actively open (not just awake) - see context_evaluator
    barge_in_busy: bool = False      # thinking/speaking/speech_pending right now
    conversation_active: bool = False  # session_busy OR barge_in_busy - the one flag Policy actually gates on

    planner_active_task_count: int = 0
    last_tool_name: Optional[str] = None
    last_tool_target: Optional[str] = None
    last_tool_action: Optional[str] = None
    last_tool_success: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "hour_of_day": self.hour_of_day,
            "time_bucket": self.time_bucket,
            "user_present": self.user_present,
            "humans": [h.__dict__ for h in self.humans],
            "objects": [o.__dict__ for o in self.objects],
            "light_on": self.light_on,
            "door_closed": self.door_closed,
            "recent_vision_event_descriptions": list(self.recent_vision_event_descriptions),
            "long_term_facts": list(self.long_term_facts),
            "session_summary_count": self.session_summary_count,
            "session_state": self.session_state,
            "session_busy": self.session_busy,
            "barge_in_busy": self.barge_in_busy,
            "conversation_active": self.conversation_active,
            "planner_active_task_count": self.planner_active_task_count,
            "last_tool_name": self.last_tool_name,
            "last_tool_target": self.last_tool_target,
            "last_tool_action": self.last_tool_action,
            "last_tool_success": self.last_tool_success,
        }


@dataclass
class Goal:
    """A candidate goal produced by `GoalGenerator` - "a description
    only, never executes actions directly" per the spec. `action_text`,
    if set, is a plain natural-language phrase handed to the REAL
    `Planner.create_plan()` unchanged (e.g. "turn on the bedroom light
    and heat the water") - never a hand-built `ToolCall`, so the exact
    same parsing/validation a real spoken command goes through applies
    here too (no duplicated Planner logic). `speech_text`, if set, is
    spoken via a direct `speak_request` publish (no LLM) once the goal
    is approved/executed - see `manager.py`."""
    id: str
    type: GoalType
    description: str
    reasoning: str
    created_at: datetime

    action_text: Optional[str] = None
    speech_text: Optional[str] = None
    needs_llm: bool = False
    llm_reason: Optional[str] = None

    confidence: float = 0.0          # 0-100
    cooldown_key: str = ""           # e.g. "welcome_user", "energy_saving:ac_unit"
    triggers: List[str] = field(default_factory=list)  # e.g. ["human_entered", "time_evening"]
    context_snapshot: Dict[str, Any] = field(default_factory=dict)

    status: GoalStatus = GoalStatus.PENDING
    policy: Optional["PolicyResult"] = None
    result: Optional[str] = None
    plan_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "description": self.description,
            "reasoning": self.reasoning,
            "created_at": self.created_at.isoformat(),
            "action_text": self.action_text,
            "speech_text": self.speech_text,
            "needs_llm": self.needs_llm,
            "llm_reason": self.llm_reason,
            "confidence": round(self.confidence, 1),
            "cooldown_key": self.cooldown_key,
            "triggers": list(self.triggers),
            "status": self.status.value,
            "policy": self.policy.to_dict() if self.policy else None,
            "result": self.result,
            "plan_id": self.plan_id,
        }


@dataclass
class PolicyResult:
    """What `PolicyEngine.evaluate()` returns for one `Goal` - every
    field the spec's "Explainability" section asks be recorded."""
    action: PolicyAction
    priority: float
    risk: RiskLevel
    confidence: float
    requires_confirmation: bool
    reasoning: str
    decided_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "priority": round(self.priority, 1),
            "risk": self.risk.value,
            "confidence": round(self.confidence, 1),
            "requires_confirmation": self.requires_confirmation,
            "reasoning": self.reasoning,
            "decided_at": self.decided_at.isoformat(),
        }


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ProactiveConfig:
    """Every tunable this sprint calls out, env-var only, `from_env()`
    is the only supported way to build a non-default one - mirrors
    `WakeSessionConfig.from_env()`/`BargeInConfig.from_env()` exactly."""

    proactive_enabled: bool = True
    #: How often the background tick evaluates context + generates
    #: candidate goals, in seconds.
    evaluation_interval_s: float = 30.0
    #: Minimum time between two EXECUTIONS of the same `cooldown_key`.
    cooldown_s: float = 1800.0
    #: >= this confidence (0-100), a SAFE goal executes immediately.
    auto_execution_threshold: float = 95.0
    #: >= this confidence (and below auto_execution_threshold), ask
    #: first instead of executing. Below this: discard, keep observing.
    confirmation_threshold: float = 60.0
    #: How many goals may be QUEUED/EXECUTING/AWAITING_CONFIRMATION at
    #: once before new AUTO_EXECUTE candidates get demoted to QUEUE.
    max_concurrent_goals: int = 3
    #: "Maximum executions per day" per cooldown_key (spec's own Goal
    #: Cooldown requirement, independent of the plain time-based cooldown
    #: above - a goal could technically clear the time cooldown many
    #: times a day without this second cap).
    max_executions_per_day: int = 6
    #: Per-rule opt-out for `goal_generator._rule_welcome_user()` (the
    #: "Welcome back! I turned the lights on for you." goal - confidence
    #: 97.0 clears the default `auto_execution_threshold` of 95.0, so it
    #: speaks AND flips the lights with no confirmation prompt). Separate
    #: from `proactive_enabled` on purpose - a user who wants the OTHER
    #: rules (health reminder, energy saving, night routine, forgotten
    #: appliances, coffee offer) but not this one specific greeting
    #: shouldn't have to disable proactive behavior entirely.
    welcome_rule_enabled: bool = True

    @classmethod
    def from_env(cls) -> "ProactiveConfig":
        return cls(
            proactive_enabled=_bool("PROACTIVE_ENABLED", True),
            evaluation_interval_s=_float("GOAL_EVALUATION_INTERVAL", 30.0),
            cooldown_s=_float("GOAL_COOLDOWN", 1800.0),
            auto_execution_threshold=_float("AUTO_EXECUTION_THRESHOLD", 95.0),
            confirmation_threshold=_float("CONFIRMATION_THRESHOLD", 60.0),
            max_concurrent_goals=_int("MAX_CONCURRENT_GOALS", 3),
            max_executions_per_day=_int("GOAL_MAX_EXECUTIONS_PER_DAY", 6),
            welcome_rule_enabled=_bool("PROACTIVE_WELCOME_ENABLED", True),
        )
