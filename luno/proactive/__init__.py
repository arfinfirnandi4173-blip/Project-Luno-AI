"""
Proactive Intelligence & Autonomous Goal Planner (Sprint 10)
================================================================

Enables Luno to observe context and act WITHOUT waiting for a wake word
or an explicit command - while still flowing every real action through
the exact same Planner/Tool Manager the reactive (wake-word) path
already uses. Nothing in this package talks to Home Assistant, an
adapter, or any hardware directly.

    World state (Vision Memory) + Long-Term Memory + Planner/session
    state + wall-clock time
        -> ContextEvaluator.evaluate()          (context_evaluator.py)
        -> GoalGenerator.generate()              (goal_generator.py)
        -> PolicyEngine.evaluate() per goal       (policy_engine.py)
        -> ProactiveModule executes/queues/asks    (manager.py)
              -> Planner.create_plan()+.execute()     (same Planner
                 every real spoken command already uses)
              -> Event Bus `speak_request`              (same event
                 BehaviorTreeModule/BargeInModule's own ack path uses -
                 no LLM call needed for a templated confirmation)

There is deliberately no separate `world_model` package: this project's
real "World Model" input is `luno.vision_memory.get_world_state()`
(humans/objects/room state) plus Home Assistant's own last-known-command
state (via the Tool Manager bridge) - building a second, parallel state
store on top would duplicate what Vision Memory already owns.

    models.py            - Goal/ContextSummary/PolicyResult/ProactiveConfig
    context_evaluator.py   - ContextEvaluator (World Model -> ContextSummary)
    goal_generator.py        - GoalGenerator (ContextSummary -> List[Goal])
    policy_engine.py            - PolicyEngine (Goal -> PolicyResult)
    manager.py                     - ProactiveModule (the Event-Bus-shaped
                                       Module that owns and drives all four)

See `luno/bootstrap/modules.py` for the real wiring (provider callables
bound to the actual running Vision Memory/Planner/session/barge-in
instances) and `luno/dashboard/collectors.py::collect_goals()` for how
this surfaces in the "Goals" Dashboard panel.
"""

from .context_evaluator import ContextEvaluator
from .goal_generator import GoalGenerator
from .manager import ProactiveModule
from .models import (
    ContextSummary,
    Goal,
    GoalStatus,
    GoalType,
    HumanContext,
    ObjectContext,
    PolicyAction,
    PolicyResult,
    ProactiveConfig,
    RiskLevel,
)
from .policy_engine import PolicyEngine

__all__ = [
    "ContextEvaluator",
    "GoalGenerator",
    "PolicyEngine",
    "ProactiveModule",
    "ContextSummary", "HumanContext", "ObjectContext",
    "Goal", "GoalType", "GoalStatus",
    "PolicyAction", "PolicyResult", "RiskLevel",
    "ProactiveConfig",
]
