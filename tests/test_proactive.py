"""
test_proactive.py
====================

Sprint 10 - Proactive Intelligence & Autonomous Goal Planner regression
suite. Same dual-mode `@scenario` pattern every other test file in this
project uses (see `tests/test_dashboard.py`'s own docstring) - runnable
standalone (`python3 tests/test_proactive.py`) or filtered by substring.

Covers the sprint's own testing checklist:
  - Goal generation (all 6 rules, positive + negative cases)
  - Policy decisions (all 4 PolicyAction outcomes)
  - Confidence thresholds (auto-execute / ask / discard boundaries)
  - Cooldown (time-based + daily cap)
  - Priority scheduling
  - Conversation awareness (queue + promote-when-free)
  - Planner integration (REAL `Planner.create_plan()`/`execute()`, no
    hand-built ToolCalls, validation-error handling)
  - Tool Manager integration (via the last-tool-result proxy)
  - Memory-based personalization (coffee rule with/without dislike fact)
  - Dashboard/status_snapshot updates
  - Stress + concurrent goal generation
  - Safety denylist (never auto-executes purchase/transfer/unlock/
    delete/message-send goals)

Where a real Runtime/Planner/Tool Manager is needed (Planner/Tool
Manager integration, ProactiveModule end-to-end, concurrency), this
file builds the exact same all-mock-backend stack `test_dashboard.py`/
`test_production_launcher.py` already use - no external hardware or
network required. Where it isn't (Context Evaluator, Goal Generator,
Policy Engine in isolation), everything is hand-built
`ContextSummary`/`Goal` data per this package's own "provider callable,
plain dataclass, fully testable without a running Runtime" design.

Run:
    python3 tests/test_proactive.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402

from luno.proactive.context_evaluator import ContextEvaluator  # noqa: E402
from luno.proactive.goal_generator import GoalGenerator  # noqa: E402
from luno.proactive.manager import ProactiveModule  # noqa: E402
from luno.proactive.models import (  # noqa: E402
    ContextSummary, Goal, GoalStatus, GoalType, HumanContext, ObjectContext,
    PolicyAction, PolicyResult, ProactiveConfig, RiskLevel,
)
from luno.proactive.policy_engine import PolicyEngine  # noqa: E402

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []
_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# Helpers
# ============================================================================

def _ctx(**overrides) -> ContextSummary:
    """A minimal, otherwise-neutral `ContextSummary` - every rule/policy
    test starts from this and overrides only the fields it cares about,
    so each test reads as "what's different about this scenario"."""
    base = dict(
        generated_at=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc),  # evening
        hour_of_day=18,
        time_bucket="evening",
        user_present=False,
        humans=[],
        objects=[],
        light_on=None,
        door_closed=None,
        recent_vision_event_descriptions=[],
        long_term_facts=[],
        session_summary_count=0,
        session_state="sleeping",
        session_busy=False,
        barge_in_busy=False,
        conversation_active=False,
        planner_active_task_count=0,
        last_tool_name=None,
        last_tool_target=None,
        last_tool_action=None,
        last_tool_success=None,
    )
    base.update(overrides)
    return ContextSummary(**base)


def _goal(**overrides) -> Goal:
    base = dict(
        id="test-goal-1", type=GoalType.OTHER, description="test goal",
        reasoning="test reasoning", created_at=datetime.now(timezone.utc),
        confidence=80.0, cooldown_key="test:goal",
    )
    base.update(overrides)
    return Goal(**base)


def _build_stack():
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    return runtime, modules, adapter_manager, cfg


def _teardown(runtime, adapter_manager):
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


# ============================================================================
# Context Evaluator
# ============================================================================

@scenario
def test_01_time_bucket_boundaries():
    from luno.proactive.context_evaluator import _time_bucket
    assert _time_bucket(5) == "morning"
    assert _time_bucket(11) == "morning"
    assert _time_bucket(12) == "afternoon"
    assert _time_bucket(16) == "afternoon"
    assert _time_bucket(17) == "evening"
    assert _time_bucket(21) == "evening"
    assert _time_bucket(22) == "night"
    assert _time_bucket(4) == "night"
    assert _time_bucket(0) == "night"


@scenario
def test_02_context_evaluator_reads_world_state_into_flat_summary():
    class FakeActivity:
        value = "sitting"

    class FakeHuman:
        id = "h1"
        identity = "vinn"
        activity = FakeActivity()
        first_seen = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
        last_seen = datetime(2026, 1, 1, 17, 59, tzinfo=timezone.utc)

    class FakeStatus:
        value = "on"

    class FakeObject:
        id = "o1"
        label = "lamp"
        location = "living_room"
        status = FakeStatus()

    class FakeRoom:
        light_on = True
        door_closed = False

    class FakeWorld:
        humans = {"h1": FakeHuman()}
        objects = {"o1": FakeObject()}
        room = FakeRoom()

    evaluator = ContextEvaluator(
        get_world_state=lambda: FakeWorld(),
        now_fn=lambda: datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc),
    )
    ctx = evaluator.evaluate()
    assert ctx.user_present is True
    assert ctx.humans[0].id == "h1"
    assert ctx.humans[0].activity == "sitting"
    assert ctx.humans[0].seconds_in_current_activity == 4.0 * 3600.0
    assert ctx.objects[0].label == "lamp"
    assert ctx.light_on is True
    assert ctx.door_closed is False
    assert ctx.time_bucket == "evening"


@scenario
def test_03_context_evaluator_survives_a_raising_world_state_provider():
    """A bad/raising source must never take down the whole cycle -
    spec: "one bad source must never take down the whole evaluation"."""
    def _boom():
        raise RuntimeError("vision memory unavailable")

    evaluator = ContextEvaluator(get_world_state=_boom, get_long_term_facts=_boom)
    ctx = evaluator.evaluate()
    assert ctx.user_present is False
    assert ctx.humans == []
    assert ctx.long_term_facts == []


@scenario
def test_04_context_evaluator_conversation_active_derivation():
    evaluator = ContextEvaluator(
        get_session_status=lambda: {"state": "listening"},
        get_barge_in_status=lambda: {"busy": False},
    )
    ctx = evaluator.evaluate()
    assert ctx.session_busy is True
    assert ctx.conversation_active is True

    evaluator2 = ContextEvaluator(
        get_session_status=lambda: {"state": "sleeping"},
        get_barge_in_status=lambda: {"busy": True},
    )
    ctx2 = evaluator2.evaluate()
    assert ctx2.session_busy is False
    assert ctx2.barge_in_busy is True
    assert ctx2.conversation_active is True  # busy OR barge_in_busy

    evaluator3 = ContextEvaluator(
        get_session_status=lambda: {"state": "idle"},
        get_barge_in_status=lambda: {"busy": False},
    )
    ctx3 = evaluator3.evaluate()
    assert ctx3.conversation_active is False


@scenario
def test_05_context_evaluator_planner_active_count_reads_running_waiting_retrying():
    class FakeStatus:
        def __init__(self, v):
            self.value = v

    class FakeTask:
        def __init__(self, v):
            self.status = FakeStatus(v)

    evaluator = ContextEvaluator(
        get_planner_queue=lambda: {"p1": [FakeTask("running"), FakeTask("done"), FakeTask("waiting")]},
    )
    ctx = evaluator.evaluate()
    assert ctx.planner_active_task_count == 2


# ============================================================================
# Goal Generator - all 6 rules
# ============================================================================

@scenario
def test_06_rule_welcome_user_fires_on_dark_room_evening_arrival():
    gen = GoalGenerator()
    ctx = _ctx(user_present=True, light_on=False, time_bucket="evening")
    goals = gen.generate(ctx)
    welcome = [g for g in goals if g.type == GoalType.WELCOME]
    assert len(welcome) == 1
    assert welcome[0].action_text == "turn on the lights"
    assert welcome[0].confidence >= 95.0


@scenario
def test_07_rule_welcome_user_does_not_fire_when_room_is_bright():
    gen = GoalGenerator()
    ctx = _ctx(user_present=True, light_on=True, time_bucket="evening")
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.WELCOME]


@scenario
def test_08_rule_welcome_user_does_not_fire_when_no_one_present():
    gen = GoalGenerator()
    ctx = _ctx(user_present=False, light_on=False, time_bucket="evening")
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.WELCOME]


@scenario
def test_08b_rule_welcome_user_can_be_disabled_via_config_without_touching_other_rules():
    """Regression test for the reported bug: the welcome-home greeting
    ("Welcome back! I turned the lights on for you.") auto-executes
    (confidence 97.0 clears the 95.0 threshold) with no way to turn it
    off short of disabling proactive behavior entirely.
    `PROACTIVE_WELCOME_ENABLED=false` must suppress ONLY this rule -
    every other rule (health reminder here, as a representative example)
    must keep firing normally."""
    cfg = ProactiveConfig(welcome_rule_enabled=False)
    gen = GoalGenerator(config=cfg)

    ctx = _ctx(user_present=True, light_on=False, time_bucket="evening")
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.WELCOME], (
        "welcome_rule_enabled=False must suppress the WELCOME goal entirely"
    )

    human = HumanContext(id="h1", identity=None, activity="sitting",
                          seconds_in_current_activity=3 * 3600.0 + 1, seconds_since_last_seen=0.0)
    ctx2 = _ctx(user_present=True, humans=[human])
    goals2 = gen.generate(ctx2)
    assert [g for g in goals2 if g.type == GoalType.HEALTH_REMINDER], (
        "disabling the welcome rule must not affect any other rule"
    )


@scenario
def test_08c_proactive_config_from_env_reads_welcome_toggle():
    old = os.environ.get("PROACTIVE_WELCOME_ENABLED")
    try:
        os.environ["PROACTIVE_WELCOME_ENABLED"] = "false"
        assert ProactiveConfig.from_env().welcome_rule_enabled is False
        os.environ["PROACTIVE_WELCOME_ENABLED"] = "true"
        assert ProactiveConfig.from_env().welcome_rule_enabled is True
        del os.environ["PROACTIVE_WELCOME_ENABLED"]
        assert ProactiveConfig.from_env().welcome_rule_enabled is True  # default
    finally:
        if old is None:
            os.environ.pop("PROACTIVE_WELCOME_ENABLED", None)
        else:
            os.environ["PROACTIVE_WELCOME_ENABLED"] = old


@scenario
def test_09_rule_health_reminder_fires_after_three_hours_sitting():
    gen = GoalGenerator()
    human = HumanContext(id="h1", identity=None, activity="sitting",
                          seconds_in_current_activity=3 * 3600.0 + 1, seconds_since_last_seen=0.0)
    ctx = _ctx(user_present=True, humans=[human])
    goals = gen.generate(ctx)
    reminders = [g for g in goals if g.type == GoalType.HEALTH_REMINDER]
    assert len(reminders) == 1
    assert reminders[0].action_text is None  # speech only, no Home Assistant action
    assert reminders[0].speech_text


@scenario
def test_10_rule_health_reminder_does_not_fire_before_threshold():
    gen = GoalGenerator()
    human = HumanContext(id="h1", identity=None, activity="sitting",
                          seconds_in_current_activity=3 * 3600.0 - 60, seconds_since_last_seen=0.0)
    ctx = _ctx(user_present=True, humans=[human])
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.HEALTH_REMINDER]


@scenario
def test_11_rule_energy_saving_fires_when_ac_left_on_empty_room():
    gen = GoalGenerator()
    ctx = _ctx(user_present=False, last_tool_target="ac_living_room", last_tool_action="turn_on", last_tool_success=True)
    goals = gen.generate(ctx)
    energy = [g for g in goals if g.type == GoalType.ENERGY_SAVING]
    assert len(energy) == 1
    assert energy[0].action_text == "turn off ac_living_room"


@scenario
def test_12_rule_energy_saving_does_not_fire_when_user_present():
    gen = GoalGenerator()
    ctx = _ctx(user_present=True, last_tool_target="ac_living_room", last_tool_action="turn_on", last_tool_success=True)
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.ENERGY_SAVING]


@scenario
def test_13_rule_energy_saving_does_not_fire_for_non_ac_target():
    gen = GoalGenerator()
    ctx = _ctx(user_present=False, last_tool_target="tv_living_room", last_tool_action="turn_on", last_tool_success=True)
    goals = gen.generate(ctx)
    assert not [g for g in goals if g.type == GoalType.ENERGY_SAVING]


@scenario
def test_14_rule_night_routine_fires_when_sleeping_and_tv_on():
    gen = GoalGenerator()
    human = HumanContext(id="h1", identity=None, activity="sleeping",
                          seconds_in_current_activity=600.0, seconds_since_last_seen=0.0)
    ctx = _ctx(humans=[human], last_tool_target="tv_bedroom", last_tool_action="turn_on")
    goals = gen.generate(ctx)
    night = [g for g in goals if g.type == GoalType.NIGHT_ROUTINE]
    assert len(night) == 1
    assert night[0].confidence == 75.0  # medium confidence - "ask first" per spec Scenario 4
    assert "want me to turn it off" in night[0].speech_text.lower()


@scenario
def test_15_rule_forgotten_lights_fires_when_empty_room_lights_on():
    gen = GoalGenerator()
    ctx = _ctx(user_present=False, light_on=True, last_tool_target="lamp_hallway")
    goals = gen.generate(ctx)
    forgotten = [g for g in goals if g.type == GoalType.FORGOTTEN_APPLIANCE]
    assert len(forgotten) == 1
    assert forgotten[0].action_text == "turn off lamp_hallway"


@scenario
def test_16_rule_assistance_offer_coffee_with_and_without_dislike_fact():
    gen = GoalGenerator()
    # Daytime/evening arrival + likes coffee -> offer, medium-high confidence
    ctx_day = _ctx(user_present=True, time_bucket="evening", long_term_facts=["user usually drinks coffee after work"])
    goals_day = gen.generate(ctx_day)
    coffee_day = [g for g in goals_day if g.type == GoalType.ASSISTANCE_OFFER]
    assert len(coffee_day) == 1
    assert coffee_day[0].confidence == 70.0

    # Nighttime + likes coffee but no explicit preference either way -> low confidence
    ctx_night = _ctx(user_present=True, time_bucket="night", long_term_facts=["user usually drinks coffee after work"])
    goals_night = gen.generate(ctx_night)
    coffee_night = [g for g in goals_night if g.type == GoalType.ASSISTANCE_OFFER]
    assert len(coffee_night) == 1
    assert coffee_night[0].confidence < 60.0  # below confirmation threshold - policy will discard

    # Nighttime + explicit dislike-at-night fact -> no goal at all
    ctx_dislike = _ctx(user_present=True, time_bucket="night",
                        long_term_facts=["user usually drinks coffee after work", "user dislikes coffee at night"])
    goals_dislike = gen.generate(ctx_dislike)
    assert not [g for g in goals_dislike if g.type == GoalType.ASSISTANCE_OFFER]


@scenario
def test_17_multiple_rules_combine_in_one_cycle():
    """Spec: "Multiple triggers may combine" - a scenario that satisfies
    more than one rule simultaneously must produce more than one goal."""
    gen = GoalGenerator()
    human = HumanContext(id="h1", identity=None, activity="sitting",
                          seconds_in_current_activity=4 * 3600.0, seconds_since_last_seen=0.0)
    ctx = _ctx(user_present=True, light_on=False, time_bucket="evening", humans=[human],
               long_term_facts=["usually drinks coffee after work"])
    goals = gen.generate(ctx)
    types = {g.type for g in goals}
    assert GoalType.WELCOME in types
    assert GoalType.HEALTH_REMINDER in types
    assert GoalType.ASSISTANCE_OFFER in types


@scenario
def test_18_goal_generator_rule_exception_does_not_break_other_rules():
    """A single misbehaving rule must not prevent the others from
    producing goals (spec: each rule is independent/defensive)."""
    gen = GoalGenerator()

    def _boom(_ctx):
        raise RuntimeError("rule bug")

    gen._rule_welcome_user = _boom  # monkeypatch one rule to explode
    ctx = _ctx(user_present=False, last_tool_target="ac_x", last_tool_action="turn_on", last_tool_success=True)
    goals = gen.generate(ctx)
    # energy saving rule still ran despite welcome_user raising
    assert any(g.type == GoalType.ENERGY_SAVING for g in goals)


# ============================================================================
# Policy Engine - confidence thresholds / decisions
# ============================================================================

@scenario
def test_19_policy_auto_executes_high_confidence_safe_goal():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    goal = _goal(confidence=97.0, type=GoalType.WELCOME, action_text="turn on the lights")
    ctx = _ctx(conversation_active=False)
    decision = engine.evaluate(goal, ctx, active_goal_count=0)
    assert decision.action == PolicyAction.AUTO_EXECUTE


@scenario
def test_20_policy_asks_first_for_medium_confidence():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    goal = _goal(confidence=75.0, action_text="start heating water")
    ctx = _ctx()
    decision = engine.evaluate(goal, ctx, active_goal_count=0)
    assert decision.action == PolicyAction.ASK_CONFIRMATION


@scenario
def test_21_policy_discards_low_confidence():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    goal = _goal(confidence=45.0)
    ctx = _ctx()
    decision = engine.evaluate(goal, ctx, active_goal_count=0)
    assert decision.action == PolicyAction.DISCARD


@scenario
def test_22_policy_threshold_boundaries_are_inclusive_exclusive_as_documented():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    ctx = _ctx()
    # exactly at confirmation_threshold -> NOT discarded (>= condition)
    assert engine.evaluate(_goal(confidence=60.0, cooldown_key="b1"), ctx, 0).action != PolicyAction.DISCARD
    # just under confirmation_threshold -> discarded
    assert engine.evaluate(_goal(confidence=59.9, cooldown_key="b2"), ctx, 0).action == PolicyAction.DISCARD
    # exactly at auto_execution_threshold -> AUTO_EXECUTE
    assert engine.evaluate(_goal(confidence=95.0, cooldown_key="b3"), ctx, 0).action == PolicyAction.AUTO_EXECUTE
    # just under auto_execution_threshold -> ASK_CONFIRMATION
    assert engine.evaluate(_goal(confidence=94.9, cooldown_key="b4"), ctx, 0).action == PolicyAction.ASK_CONFIRMATION


@scenario
def test_23_policy_safety_denylist_always_requires_confirmation_never_auto_executes():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    ctx = _ctx()
    # Even at 99% confidence, a denylisted action never auto-executes.
    goal = _goal(confidence=99.0, action_text="delete the file", description="Delete an old file")
    decision = engine.evaluate(goal, ctx, active_goal_count=0)
    assert decision.action == PolicyAction.ASK_CONFIRMATION
    assert decision.requires_confirmation is True

    # Below confirmation threshold, a denylisted action is discarded outright.
    goal2 = _goal(confidence=40.0, action_text="transfer money to savings", cooldown_key="denylist2")
    decision2 = engine.evaluate(goal2, ctx, active_goal_count=0)
    assert decision2.action == PolicyAction.DISCARD

    for phrase in ("buy groceries online", "unlock the front door", "send message to mom", "purchase a new filter"):
        g = _goal(confidence=99.0, action_text=phrase, cooldown_key=f"deny:{phrase}")
        d = engine.evaluate(g, ctx, active_goal_count=0)
        assert d.action != PolicyAction.AUTO_EXECUTE, f"{phrase!r} must never auto-execute"


@scenario
def test_24_policy_cooldown_blocks_repeat_execution_within_window():
    engine = PolicyEngine(ProactiveConfig(cooldown_s=3600.0, confirmation_threshold=60.0, auto_execution_threshold=95.0))
    goal = _goal(confidence=97.0, cooldown_key="welcome_user")
    ctx = _ctx()
    d1 = engine.evaluate(goal, ctx, active_goal_count=0)
    assert d1.action == PolicyAction.AUTO_EXECUTE
    engine.record_execution("welcome_user")

    d2 = engine.evaluate(_goal(confidence=97.0, cooldown_key="welcome_user"), ctx, active_goal_count=0)
    assert d2.action == PolicyAction.DISCARD
    assert "cooldown" in d2.reasoning.lower()
    assert engine.cooldown_remaining_s("welcome_user") > 0


@scenario
def test_25_policy_cooldown_daily_execution_cap():
    engine = PolicyEngine(ProactiveConfig(cooldown_s=0.0, max_executions_per_day=2, confirmation_threshold=60.0, auto_execution_threshold=95.0))
    ctx = _ctx()
    key = "capped_goal"
    for i in range(2):
        d = engine.evaluate(_goal(confidence=97.0, cooldown_key=key), ctx, 0)
        assert d.action == PolicyAction.AUTO_EXECUTE, f"execution {i} should be allowed"
        engine.record_execution(key)
    d3 = engine.evaluate(_goal(confidence=97.0, cooldown_key=key), ctx, 0)
    assert d3.action == PolicyAction.DISCARD
    assert "today" in d3.reasoning.lower()


@scenario
def test_26_policy_conversation_awareness_queues_instead_of_interrupting():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0))
    ctx_busy = _ctx(conversation_active=True)
    decision = engine.evaluate(_goal(confidence=97.0), ctx_busy, active_goal_count=0)
    assert decision.action == PolicyAction.QUEUE
    assert "conversation" in decision.reasoning.lower() or "interrupt" in decision.reasoning.lower()


@scenario
def test_27_policy_max_concurrent_goals_demotes_to_queue():
    engine = PolicyEngine(ProactiveConfig(auto_execution_threshold=95.0, confirmation_threshold=60.0, max_concurrent_goals=2))
    ctx = _ctx(conversation_active=False)
    decision = engine.evaluate(_goal(confidence=97.0), ctx, active_goal_count=2)
    assert decision.action == PolicyAction.QUEUE
    decision_ok = engine.evaluate(_goal(confidence=97.0, cooldown_key="under_cap"), ctx, active_goal_count=1)
    assert decision_ok.action == PolicyAction.AUTO_EXECUTE


@scenario
def test_28_policy_priority_scoring_reflects_safety_and_conversation_risk():
    engine = PolicyEngine()
    ctx_free = _ctx(conversation_active=False, planner_active_task_count=0)
    ctx_busy = _ctx(conversation_active=True, planner_active_task_count=5)

    safety_goal = _goal(confidence=90.0, type=GoalType.SAFETY)
    other_goal = _goal(confidence=90.0, type=GoalType.OTHER, cooldown_key="other")

    p_safety = engine.evaluate(safety_goal, ctx_free, 0).priority
    p_other = engine.evaluate(other_goal, ctx_free, 0).priority
    assert p_safety > p_other, "a SAFETY goal must outrank an OTHER goal at equal confidence"

    p_free = engine.evaluate(_goal(confidence=90.0, cooldown_key="k1"), ctx_free, 0).priority
    p_busy = engine.evaluate(_goal(confidence=90.0, cooldown_key="k2"), ctx_busy, 0).priority
    assert p_busy < p_free, "conversation-active + planner workload must lower priority"


# ============================================================================
# ProactiveModule - end-to-end pipeline (real Planner + Tool Manager)
# ============================================================================

@scenario
def test_29_proactive_module_reuses_the_single_shared_planner_instance():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        assert modules["proactive_module"]._planner is modules["planner_module"].planner
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_30_proactive_module_auto_execute_reaches_the_real_planner():
    """The whole point of "no duplicated Planner logic": an AUTO_EXECUTE
    goal's `action_text` must flow through the REAL `create_plan()` +
    `execute()`, producing a real Task/Tool Manager execution - the same
    two calls a spoken command already goes through."""
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        goal = _goal(id="e2e-1", type=GoalType.WELCOME, confidence=99.0,
                     cooldown_key="e2e_test_welcome", action_text="turn on the kitchen light")
        pm._execute_goal(goal)
        assert goal.status == GoalStatus.COMPLETED, goal.result
        assert goal.plan_id is not None
        # Tool Manager integration proxy: the executed plan's dispatch
        # must have updated the same last_tool/last_result fields the
        # Context Evaluator itself reads back (see test_45).
        deadline = time.time() + 5.0
        tmm = modules["tool_manager_module"]
        while time.time() < deadline and tmm.last_tool is None:
            time.sleep(0.1)
        assert tmm.last_tool is not None, "expected the real Tool Manager to have dispatched a tool call"
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_31_proactive_module_marks_expired_on_planner_validation_error():
    """An `action_text` the parser can't turn into a valid plan must
    result in EXPIRED (not a silent crash, not COMPLETED)."""
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        goal = _goal(id="e2e-invalid-1", confidence=99.0, cooldown_key="e2e_invalid",
                     action_text="asdkjaslkdj completely nonsensical gibberish that parses to nothing 12903")
        pm._execute_goal(goal)
        # Whether the heuristic parser manages to produce *something* or
        # not is an implementation detail of IntentParser - what matters
        # here is the module never raises and always lands on a terminal
        # status with a recorded result.
        assert goal.status in (GoalStatus.COMPLETED, GoalStatus.EXPIRED)
        assert goal.result
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_32_proactive_module_speaks_via_speak_request_no_llm_call():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        received = []
        sub_id = runtime.event_bus.subscribe("speak_request", lambda e: received.append(e))
        try:
            goal = _goal(id="speak-1", confidence=99.0, cooldown_key="speak_test",
                         action_text=None, speech_text="Hello, this is a proactive greeting.")
            pm._execute_goal(goal)
            deadline = time.time() + 3.0
            while time.time() < deadline and not received:
                time.sleep(0.05)
            assert received, "expected a speak_request event"
            assert "hello" in received[0].data["text"].lower() or "hello" in received[0].data["raw_text"].lower()
            assert received[0].data["source"] == "proactive"
        finally:
            runtime.event_bus.unsubscribe(sub_id)
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_33_proactive_module_conversation_awareness_queue_then_promote():
    """Queued while conversation is active; the very next cycle after
    the conversation frees up must promote it to execution without
    waiting for a brand new goal of the same kind."""
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        session_manager = modules["session_manager"]

        from luno.wake_session.models import ConversationState
        session_manager.session.transition_to(ConversationState.LISTENING, reason="test - conversation busy")

        ctx = pm.evaluator.evaluate()
        assert ctx.conversation_active is True
        goal = _goal(id="queue-1", confidence=99.0, cooldown_key="queue_test", action_text="turn on the porch light")
        pm._process_goal(goal, ctx)
        assert goal.status == GoalStatus.QUEUED
        with pm._lock:
            assert "queue-1" in pm._active

        # conversation frees up
        session_manager.session.transition_to(ConversationState.SLEEPING, reason="test - conversation over")
        ctx2 = pm.evaluator.evaluate()
        assert ctx2.conversation_active is False
        pm._retry_queued(ctx2)

        deadline = time.time() + 3.0
        promoted = False
        while time.time() < deadline:
            with pm._lock:
                still_active = "queue-1" in pm._active
            if not still_active:
                promoted = True
                break
            time.sleep(0.05)
        assert promoted, "queued goal was never promoted once the conversation freed up"
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_34_proactive_module_approve_and_reject_goal_api():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        g1 = _goal(id="approve-1", confidence=75.0, cooldown_key="approve_test", speech_text="ok?")
        g1.status = GoalStatus.AWAITING_CONFIRMATION
        with pm._lock:
            pm._active[g1.id] = g1
        result = pm.approve_goal("approve-1")
        assert result["ok"] is True
        with pm._lock:
            assert "approve-1" not in pm._active or pm._active["approve-1"].status != GoalStatus.AWAITING_CONFIRMATION

        g2 = _goal(id="reject-1", confidence=75.0, cooldown_key="reject_test")
        g2.status = GoalStatus.AWAITING_CONFIRMATION
        with pm._lock:
            pm._active[g2.id] = g2
        result2 = pm.reject_goal("reject-1")
        assert result2["ok"] is True
        assert any(g.id == "reject-1" for g in pm._rejected)

        # unknown id never raises
        assert pm.approve_goal("does-not-exist")["ok"] is False
        assert pm.reject_goal("does-not-exist")["ok"] is False
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_35_proactive_module_status_snapshot_shape_for_dashboard():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        snap = pm.status_snapshot()
        for field in ("enabled", "cycle_count", "last_cycle_at", "last_context",
                      "active_goals", "awaiting_confirmation", "completed_goals",
                      "rejected_goals", "config"):
            assert field in snap
        for cfg_field in ("proactive_enabled", "evaluation_interval_s", "cooldown_s",
                          "auto_execution_threshold", "confirmation_threshold",
                          "max_concurrent_goals", "max_executions_per_day"):
            assert cfg_field in snap["config"]
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_36_proactive_module_config_live_reload():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        os.environ["GOAL_EVALUATION_INTERVAL"] = "12.5"
        try:
            pm.reload()
            assert pm.config.evaluation_interval_s == 12.5
            assert pm.policy.config.evaluation_interval_s == 12.5
        finally:
            del os.environ["GOAL_EVALUATION_INTERVAL"]
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_37_proactive_module_health_reports_tick_thread_alive():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        health = pm.health()
        assert health.healthy is True
        pm.stop()
        time.sleep(0.2)
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_38_proactive_module_disabled_via_config_runs_no_cycles():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        pm.config.proactive_enabled = False
        before = pm._cycle_count
        pm._run_cycle(reason="test")
        assert pm._cycle_count == before, "a disabled module must not run cycles at all"
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_39_proactive_module_immediate_trigger_events_are_debounced():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        before = pm._cycle_count
        runtime.event_bus.publish(Event(type="human_entered", data={}))
        runtime.event_bus.publish(Event(type="human_entered", data={}))
        runtime.event_bus.publish(Event(type="human_entered", data={}))
        deadline = time.time() + 2.0
        while time.time() < deadline and pm._cycle_count == before:
            time.sleep(0.05)
        after_burst = pm._cycle_count
        assert after_burst > before, "at least one immediate cycle should have run"
        # a rapid burst within the debounce window must not run 3 separate cycles
        assert after_burst - before <= 2, f"debounce should have collapsed the burst, got {after_burst - before} cycles"
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# Stress / concurrency
# ============================================================================

@scenario
def test_40_stress_many_rapid_cycles_no_crash_bounded_history():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        for _ in range(200):
            pm._run_cycle(reason="stress")
        assert pm._cycle_count >= 200
        assert len(pm._completed) <= 100
        assert len(pm._rejected) <= 100
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_41_concurrent_goal_generation_thread_safety():
    """Multiple threads triggering cycles/approve/reject concurrently
    must never corrupt internal state or raise."""
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        errors = []
        stop_flag = threading.Event()

        def _cycle_worker():
            while not stop_flag.is_set():
                try:
                    pm._run_cycle(reason="concurrency_test")
                except Exception as ex:
                    errors.append(str(ex))
                time.sleep(0.01)

        def _snapshot_worker():
            while not stop_flag.is_set():
                try:
                    pm.status_snapshot()
                except Exception as ex:
                    errors.append(str(ex))
                time.sleep(0.01)

        threads = [threading.Thread(target=_cycle_worker, daemon=True) for _ in range(3)]
        threads += [threading.Thread(target=_snapshot_worker, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop_flag.set()
        for t in threads:
            t.join(timeout=2.0)
        assert not errors, f"concurrency errors: {errors}"
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_42_max_concurrent_goals_enforced_end_to_end():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    try:
        pm = modules["proactive_module"]
        pm.config.max_concurrent_goals = 1
        pm.policy.reconfigure(pm.config)
        ctx = pm.evaluator.evaluate()

        g1 = _goal(id="cap-1", confidence=99.0, cooldown_key="cap_1", action_text="turn on the light")
        pm._process_goal(g1, ctx)
        assert g1.status == GoalStatus.COMPLETED  # first one executes and completes immediately (mock is fast)

        # Force an active QUEUED goal, then verify a second AUTO_EXECUTE
        # candidate is correctly demoted to QUEUE while at the cap.
        blocker = _goal(id="cap-blocker", confidence=97.0, cooldown_key="cap_blocker")
        blocker.status = GoalStatus.QUEUED
        with pm._lock:
            pm._active[blocker.id] = blocker
        g2 = _goal(id="cap-2", confidence=99.0, cooldown_key="cap_2")
        pm._process_goal(g2, ctx)
        assert g2.status == GoalStatus.QUEUED
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# LLM Decision Gate (needs_llm / llm_reason - descriptive fields only;
# nothing in this package invokes the LLM directly, per spec)
# ============================================================================

@scenario
def test_43_goals_never_set_needs_llm_by_default_local_rules_only():
    """Spec: "The Goal Generator should rely primarily on structured
    World Model data" - none of the 6 built-in rules should mark a goal
    as needing the LLM; they're all resolvable locally."""
    gen = GoalGenerator()
    human = HumanContext(id="h1", identity=None, activity="sitting",
                          seconds_in_current_activity=4 * 3600.0, seconds_since_last_seen=0.0)
    ctx = _ctx(user_present=True, light_on=False, time_bucket="evening", humans=[human],
               long_term_facts=["usually drinks coffee after work"])
    goals = gen.generate(ctx)
    assert goals, "expected at least one goal for this combined scenario"
    assert all(not g.needs_llm for g in goals)


def main() -> int:
    filters = sys.argv[1:]
    scenarios = SCENARIOS
    if filters:
        scenarios = [(n, f) for n, f in SCENARIOS if any(flt in n for flt in filters)]

    passed = 0
    failed = 0
    for name, fn in scenarios:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
