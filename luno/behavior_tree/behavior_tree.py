"""
behavior_tree.py
=================

The orchestrator. Implements the spec's Root Behavior:

    Read all sensors -> Update internal state -> Evaluate priorities ->
    Select one behavior -> Execute it -> Wait -> Repeat.

("Read all sensors"/"Wait"/"Repeat" are `scheduler.Scheduler`'s job -
this module owns "Update internal state" (attention/emotion estimation)
and "Evaluate priorities -> Select -> Execute".)

Priority order and an important, deliberate deviation from the spec's
literal list order
-----------------------------------------------------------------------
The spec lists, top to bottom: Emergency, Critical Home Assistant events,
Direct user speech, Tool execution, Conversation continuation, Visual
events, Idle behaviors, Background maintenance, Sleep.

Read as a strict "first condition that's true wins" evaluation order,
that list has a bug: `idle_default()` (Idle Behavior's condition) is
UNCONDITIONALLY true - it's the fallback for "nothing else is happening".
If Idle is evaluated before Background maintenance and Sleep, as the
spec's prose order suggests, those two would NEVER run - Idle's
always-true condition would win every single tick before evaluation ever
reaches them.

The fix applied here: Background maintenance, Sleep, and a quiet
Error-recovery cleanup step are evaluated BEFORE the generic Idle
fallback, which stays exactly where the spec wants it in every way that
actually matters - it is still the LOWEST-URGENCY behavior (none of these
three can interrupt anything above them, and Idle interrupts nothing
either), it just needs to be tried LAST among the "nothing important is
happening" group so its catch-all condition doesn't starve its more
specific siblings. This is a standard behavior-tree "selector" pattern
(specific conditions before a generic fallback) and does not change how
Emergency through Proactive behave, which follow the spec's list exactly.

Final evaluation order (priority number = evaluation order, lower first):

    0  Emergency
    10 Critical Home Assistant events
    20 Direct user speech
    30 Tool execution
    40 Conversation continuation
    50 Visual events (Watching)
    55 Proactive
    57 Error recovery (quiet cleanup)
    58 Sleep
    59 Background maintenance
    60 Idle (fallback)

Interruption model
-------------------
"Higher priority behaviors interrupt lower priority ones" is enforced via
`StateMachine.is_interruptible()`: while the machine is in a "busy" state
(LISTENING/THINKING/TALKING/EXECUTING_TOOL/ERROR_RECOVERY - see
state_machine.py), only nodes with a STRICTLY LOWER priority NUMBER
(= higher priority) than the currently-active node may even be
considered; everything else is skipped so a slow action already in
flight isn't restarted every tick. See actions.py's module docstring for
the honest limitation on what "interrupt" can actually guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from . import actions, conditions
from .attention import AttentionEstimator
from .blackboard import Blackboard
from .cooldowns import CooldownManager
from .emotion import EmotionEstimator
from .planner import Planner  # re-exported for convenience, see __init__.py
from .state_machine import LunoState, StateMachine


_MAX_PRIORITY = 1_000_000  # sentinel: "no known active priority" - see tick()


@dataclass
class BehaviorNode:
    name: str
    priority: int  # lower = higher priority = evaluated first
    condition: Callable[[Blackboard], bool]
    action: Callable[[Blackboard, "actions.RunContext"], "actions.ActionResult"]


def _build_default_nodes(cooldowns: CooldownManager) -> List[BehaviorNode]:
    """Wires conditions.py + actions.py together into the priority-ordered
    node list described in this module's docstring. A `cooldowns`
    reference is closed over here only for the two conditions that need
    one directly (`proactive`, `background_maintenance`) - everything else
    is a plain `Callable[[Blackboard], bool]`."""
    return [
        BehaviorNode("emergency", 0, conditions.has_emergency, actions.run_emergency),
        BehaviorNode("critical_ha", 10, conditions.has_critical_ha_event, actions.run_critical_ha),
        BehaviorNode("direct_user_speech", 20, conditions.direct_user_speech, actions.run_listening),
        BehaviorNode("tool_execution", 30, conditions.tool_execution_pending, actions.run_tool_execution),
        BehaviorNode("conversation_continuation", 40, conditions.conversation_continuation, actions.run_conversation),
        BehaviorNode("visual_events", 50, conditions.has_visual_event, actions.run_watching),
        BehaviorNode(
            "proactive", 55,
            lambda bb: conditions.proactive_eligible(bb, cooldowns) is not None,
            actions.run_proactive,
        ),
        BehaviorNode(
            "error_recovery", 57,
            lambda bb: bb.system.consecutive_errors > 0,
            actions.run_error_recovery,
        ),
        BehaviorNode("sleep", 58, conditions.should_sleep, actions.run_sleep),
        BehaviorNode(
            "background_maintenance", 59,
            lambda bb: conditions.background_maintenance_due(bb, cooldowns),
            actions.run_background_maintenance,
        ),
        BehaviorNode("idle", 60, conditions.idle_default, actions.run_idle),
    ]


class BehaviorTree:
    def __init__(
        self,
        blackboard: Optional[Blackboard] = None,
        handlers: Optional[actions.Handlers] = None,
        cooldowns: Optional[CooldownManager] = None,
        state_machine: Optional[StateMachine] = None,
        executor=None,
        nodes: Optional[List[BehaviorNode]] = None,
    ) -> None:
        self.bb = blackboard if blackboard is not None else Blackboard()
        self.cooldowns = cooldowns if cooldowns is not None else CooldownManager()
        self.state_machine = state_machine if state_machine is not None else StateMachine()
        self.handlers = handlers if handlers is not None else actions.Handlers()

        # `executor` is normally supplied by `scheduler.Scheduler` (which
        # owns a ThreadPoolExecutor) - a tiny inline synchronous stand-in is
        # used if none is given, so `BehaviorTree` alone is still usable/
        # testable without constructing a full Scheduler (see
        # test_behavior_tree.py, which mostly does exactly that for
        # deterministic single-threaded assertions).
        self.executor = executor if executor is not None else _InlineExecutor()

        self.nodes = nodes if nodes is not None else _build_default_nodes(self.cooldowns)
        self.nodes.sort(key=lambda n: n.priority)

        self._active_priority: Optional[int] = None
        self.last_result: Optional[actions.ActionResult] = None

    def tick(self) -> Optional[actions.ActionResult]:
        """One full cycle of the spec's Root Behavior (minus "read
        sensors"/"wait", which are `Scheduler`'s job). Returns the
        `ActionResult` of whichever node ran, or None if the current busy
        state didn't yield to anything (still in progress)."""
        with self.bb.lock:
            self.bb.current_state = self.state_machine.state.value

        # "Update internal state": attention + emotion estimation, every tick.
        AttentionEstimator.from_blackboard(self.bb)
        EmotionEstimator.from_blackboard(self.bb)

        interruptible = self.state_machine.is_interruptible()
        if interruptible:
            candidates = self.nodes
        else:
            # Busy - only strictly-higher-priority nodes may preempt.
            # `_active_priority` is only set BY this same tick() loop when
            # IT is the one that started the busy action - if something
            # outside the tree put the state machine into a busy state
            # (e.g. `actions.dispatch_tool()` called directly), we have no
            # recorded priority to compare against. Treat that as "unknown,
            # don't block anything" rather than defaulting to a value that
            # would exclude every node (including priority 0) - the correct
            # node (e.g. tool_execution) then gets picked up and its
            # priority recorded for subsequent ticks.
            threshold = self._active_priority if self._active_priority is not None else _MAX_PRIORITY
            candidates = [n for n in self.nodes if n.priority < threshold]

        selected: Optional[BehaviorNode] = None
        for node in candidates:
            if node.condition(self.bb):
                selected = node
                break

        if selected is None:
            # Nothing eligible right now (or the busy action is still the
            # most important thing happening) - leave it running.
            return None

        if not interruptible:
            # A genuine preemption of an in-flight busy action - see the
            # "Interruption is cooperative, not preemptive" note in
            # actions.py's module docstring for what this can/can't guarantee.
            self.bb.interrupt_requested = True

        ctx = actions.RunContext(
            handlers=self.handlers,
            cooldowns=self.cooldowns,
            state_machine=self.state_machine,
            executor=self.executor,
        )
        result = selected.action(self.bb, ctx)
        self.bb.interrupt_requested = False
        self._active_priority = selected.priority
        self.bb.current_behavior = selected.name
        self.last_result = result
        return result


class _InlineExecutor:
    """Minimal `concurrent.futures.Executor`-compatible stand-in that runs
    submitted work IMMEDIATELY on the calling thread, for use only when no
    real executor (from `scheduler.Scheduler`) is supplied. Fine for
    single-threaded tests that want deterministic, synchronous behavior;
    NOT suitable for real use (it would make every dispatched action block
    the tick, defeating the entire point of `actions._dispatch()`) - real
    wiring always goes through `Scheduler`, which supplies a genuine
    `ThreadPoolExecutor`."""

    def submit(self, fn: Callable, *args, **kwargs):
        fn(*args, **kwargs)
        return None
