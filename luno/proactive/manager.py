"""
manager.py
==========

`ProactiveModule` - the `Module` (Event-Bus-shaped) wrapper that owns
the whole Context Evaluator -> Goal Generator -> Policy Engine pipeline
and is the ONLY thing in this package allowed to actually DO anything
(create/execute a real Planner plan, publish a `speak_request`). Every
upstream stage (`context_evaluator.py`/`goal_generator.py`/
`policy_engine.py`) only ever produces data - this file is where a
`PolicyAction.AUTO_EXECUTE` decision finally becomes a real
`planner.create_plan()` + `planner.execute()` call, exactly the same
two calls a real spoken command already goes through (see
`main_runtime_demo.py::PlannerBridgeModule._handle_utterance()`) - no
duplicated Planner logic, no direct adapter/Home Assistant access.

Two ways a new evaluation cycle gets triggered:
  1. A background tick every `config.evaluation_interval_s` (the
     spec's own "GOAL_EVALUATION_INTERVAL") - the steady heartbeat that
     covers time-based/memory-based goals with no specific triggering
     event (e.g. "3 hours sitting").
  2. Immediately on a handful of high-signal events (`HumanEntered`,
     `HumanLeft`, `planner_finished`, `wake_word_detected`,
     `conversation_ended`) - so an arrival/departure doesn't have to
     wait out the rest of a 30s tick to be noticed. Both paths run the
     exact same `_run_cycle()`.

Thread-safety note (learned the hard way in Sprint 8's Vision work -
see `luno/adapters/real_vision.py`'s own docstring): `start()` creates a
FRESH `threading.Event()` every call rather than `.clear()`-ing a
shared one, so a `restart()` triggered from any source can never race
an old tick thread that hasn't noticed its own generation ended yet.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import generate_id, log
from .context_evaluator import ContextEvaluator, _time_bucket
from .goal_generator import GoalGenerator
from .habit_memory import HabitMemory
from .models import Goal, GoalStatus, PolicyAction, ProactiveConfig
from .policy_engine import PolicyEngine

#: Events that trigger an immediate out-of-cycle evaluation (debounced -
#: see `_maybe_trigger_immediate_cycle`). Kept small and high-signal on
#: purpose ("Performance: avoid unnecessary [work]") - most context
#: changes are still caught by the steady tick.
_IMMEDIATE_TRIGGER_EVENTS = (
    "human_entered", "human_left", "person_appeared",
    "planner_finished", "wake_word_detected", "conversation_ended",
)

#: Minimum spacing between two immediate-trigger-driven cycles, so a
#: burst of events (e.g. several HumanEntered/HumanLeft in a row) can't
#: run the pipeline more than once per this window.
_IMMEDIATE_TRIGGER_DEBOUNCE_S = 2.0

#: How many entries each history bucket keeps (bounded - same "ring
#: buffer, not unbounded growth" discipline the Dashboard's own
#: `EventRingBuffer`/`LogCapture` already use).
_HISTORY_LIMIT = 100


class ProactiveModule(Module):
    name = "proactive"
    dependencies: List[str] = []

    def __init__(
        self,
        planner: Any,
        config: Optional[ProactiveConfig] = None,
        get_world_state: Optional[Callable[[], Any]] = None,
        get_recent_vision_events: Optional[Callable[[], List[Any]]] = None,
        get_long_term_facts: Optional[Callable[[], List[str]]] = None,
        get_session_summary_count: Optional[Callable[[], int]] = None,
        get_session_status: Optional[Callable[[], Dict[str, Any]]] = None,
        get_barge_in_status: Optional[Callable[[], Dict[str, Any]]] = None,
        get_last_tool_result: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        get_last_tool_name: Optional[Callable[[], Optional[str]]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        habit_memory: Optional[HabitMemory] = None,
        confirmation_handler: Optional[Any] = None,
        get_conversation_id: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.config = config or ProactiveConfig.from_env()
        self._planner = planner
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        self.evaluator = ContextEvaluator(
            get_world_state=get_world_state,
            get_recent_vision_events=get_recent_vision_events,
            get_long_term_facts=get_long_term_facts,
            get_session_summary_count=get_session_summary_count,
            get_session_status=get_session_status,
            get_barge_in_status=get_barge_in_status,
            get_planner_queue=(lambda: self._planner.get_queue()) if planner is not None else None,
            get_last_tool_result=get_last_tool_result,
            get_last_tool_name=get_last_tool_name,
            now_fn=self._now_fn,
        )
        self.generator = GoalGenerator(now_fn=self._now_fn, config=self.config, habit_memory=habit_memory)
        self.policy = PolicyEngine(self.config)

        # Habit-learning wiring (see luno/proactive/habit_memory.py) - all
        # three optional, None = fully inert, same "opt-in by construction"
        # convention as `classifier_client`/`ConfirmationHandler` elsewhere
        # in this project. `confirmation_handler` is the EXACT SAME instance
        # `PlannerBridgeModule` owns (shared by reference from
        # `luno/bootstrap/modules.py`, never a second one) - this module
        # still never talks to `PlannerBridgeModule` directly (see this
        # class's own docstring: Planner reference + Event Bus only), it
        # just also happens to hold a reference to this one shared,
        # tool-agnostic confirmation store. `get_conversation_id` must
        # return the SAME conversation_id real spoken utterances carry
        # (`BehaviorTreeModule.conversation_id` in production - a single,
        # stable id for the whole process, not per-turn) or a later "iya"
        # reply would never resolve this pending entry.
        self.habit_memory = habit_memory
        self._confirmation_handler = confirmation_handler
        self._get_conversation_id = get_conversation_id

        self._event_bus = None
        self._stop_flag: Optional[threading.Event] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_immediate_trigger_at = 0.0
        self._last_cycle_at: Optional[datetime] = None
        self._cycle_count = 0

        self._active: Dict[str, Goal] = {}                # QUEUED/AWAITING_CONFIRMATION/EXECUTING
        self._completed: Deque[Goal] = deque(maxlen=_HISTORY_LIMIT)
        self._rejected: Deque[Goal] = deque(maxlen=_HISTORY_LIMIT)
        self._last_context: Optional[Dict[str, Any]] = None

    # -- Module ABC -------------------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        stop_flag = threading.Event()
        self._stop_flag = stop_flag
        self._tick_thread = threading.Thread(target=self._tick_loop, args=(stop_flag,), daemon=True, name="luno-proactive-tick")
        self._tick_thread.start()

    def stop(self) -> None:
        if self._stop_flag is not None:
            self._stop_flag.set()

    def health(self) -> ModuleHealthStatus:
        if not self.config.proactive_enabled:
            return ModuleHealthStatus(healthy=True, message="proactive intelligence disabled by config")
        alive = self._tick_thread is not None and self._tick_thread.is_alive()
        if not alive:
            return ModuleHealthStatus(healthy=False, message="tick thread not running")
        return ModuleHealthStatus(healthy=True, message=f"{self._cycle_count} evaluation cycle(s) run")

    def reload(self) -> None:
        new_config = ProactiveConfig.from_env()
        self.config = new_config
        self.generator.config = new_config
        self.policy.reconfigure(new_config)
        log(f"config reloaded: enabled={new_config.proactive_enabled} interval={new_config.evaluation_interval_s}s", self.name)

    def on_event(self, event: Event) -> None:
        if event.type in _IMMEDIATE_TRIGGER_EVENTS:
            self._maybe_trigger_immediate_cycle(event.type)
        if self.habit_memory is not None:
            if event.type in ("human_entered", "person_appeared"):
                self._open_habit_arrival_window()
            elif event.type == "tool_finished":
                # `"tool_finished"` (see `luno/core/events.py::ToolFinished`)
                # is published ONLY on a VERIFIED success (see
                # `main_runtime_demo.py::ToolManagerBridgeModule.
                # _process_event()` - a failed/errored call publishes
                # `"tool_failed"` instead, never this type) - so every
                # call reaching `_record_habit_action` below is already
                # know-good, no extra `success` check needed for THAT,
                # only the tool/action filter.
                self._record_habit_action(event)
        if event.type == "proactive_habit_resolved":
            self._on_habit_resolved(event)

    def _open_habit_arrival_window(self) -> None:
        now = self._now_fn()
        try:
            self.habit_memory.open_arrival_window(_time_bucket(now.hour), now)
        except Exception as ex:  # pragma: no cover - defensive, must never break event delivery
            log(f"habit_memory.open_arrival_window raised: {ex}", self.name)

    def _record_habit_action(self, event: Event) -> None:
        data = event.data or {}
        if data.get("tool") != "home_assistant":
            return
        action = data.get("action")
        target = (data.get("data") or {}).get("target")
        try:
            self.habit_memory.record_verified_action(action, target, self._now_fn())
        except Exception as ex:  # pragma: no cover - defensive
            log(f"habit_memory.record_verified_action raised: {ex}", self.name)

    def _on_habit_resolved(self, event: Event) -> None:
        if self.habit_memory is None:
            return
        data = event.data or {}
        outcome = data.get("outcome")
        time_bucket = data.get("time_bucket")
        items = [tuple(it) for it in (data.get("items") or [])]
        if not time_bucket or not items:
            return
        try:
            if outcome == "confirmed":
                self.habit_memory.confirm(items, time_bucket)
                log(f"learned habit CONFIRMED: {items} (time_bucket={time_bucket!r})", self.name)
            elif outcome == "declined":
                self.habit_memory.decline(items, time_bucket)
                log(f"learned habit DECLINED: {items} (time_bucket={time_bucket!r})", self.name)
        except Exception as ex:  # pragma: no cover - defensive
            log(f"habit_memory resolve raised: {ex}", self.name)

    # -- tick loop --------------------------------------------------------------

    def _tick_loop(self, stop_flag: threading.Event) -> None:
        while not stop_flag.is_set():
            try:
                self._run_cycle(reason="scheduled_tick")
            except Exception as ex:  # pragma: no cover - defensive, must never kill the thread
                log(f"evaluation cycle raised: {ex}", self.name)
            stop_flag.wait(max(1.0, self.config.evaluation_interval_s))

    def _maybe_trigger_immediate_cycle(self, event_type: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_immediate_trigger_at < _IMMEDIATE_TRIGGER_DEBOUNCE_S:
                return
            self._last_immediate_trigger_at = now
        try:
            self._run_cycle(reason=f"event:{event_type}")
        except Exception as ex:
            log(f"immediate-trigger evaluation cycle raised: {ex}", self.name)

    # -- the actual pipeline ------------------------------------------------------

    def _run_cycle(self, reason: str) -> None:
        if not self.config.proactive_enabled:
            return

        context = self.evaluator.evaluate()
        with self._lock:
            self._last_context = context.to_dict()
            self._last_cycle_at = context.generated_at
            self._cycle_count += 1

        # Re-check anything already QUEUED first - if the conversation
        # has freed up since it was queued, promote it now rather than
        # waiting for a brand new goal of the same kind to be generated.
        self._retry_queued(context)

        goals = self.generator.generate(context)
        for goal in goals:
            self._process_goal(goal, context)

        self._maybe_ask_habit_proposal(context)

    def _maybe_ask_habit_proposal(self, context: Any) -> None:
        """A pattern that just crossed the promotion threshold (see
        `habit_memory.py`'s own docstring) gets asked about EXACTLY
        once, by voice, deterministic template - never through the
        conversational LLM (no extra LLM call just to phrase a
        question, same rule this whole project already enforces
        everywhere else). Gated on `user_present` (no point asking an
        empty room) and `not conversation_active` (never interrupt an
        ongoing turn - same conversation-awareness principle
        `PolicyEngine` already applies to Goals)."""
        if self.habit_memory is None or self._confirmation_handler is None:
            return
        if not context.user_present or context.conversation_active:
            return
        try:
            proposal = self.habit_memory.pop_pending_proposal(context.time_bucket)
        except Exception as ex:  # pragma: no cover - defensive
            log(f"habit_memory.pop_pending_proposal raised: {ex}", self.name)
            return
        if proposal is None:
            return

        on_items = [t for a, t in proposal.items if a == "turn_on"]
        off_items = [t for a, t in proposal.items if a == "turn_off"]
        parts = []
        if on_items:
            parts.append("menyalakan " + ", ".join(t.replace("_", " ") for t in on_items))
        if off_items:
            parts.append("mematikan " + ", ".join(t.replace("_", " ") for t in off_items))
        display = " dan ".join(parts) if parts else "ini"
        prompt = (
            f"Sepertinya kamu biasa {display} pas jam segini ({proposal.time_bucket}). "
            "Mau aku otomatis lakuin ini ke depannya tiap kali kamu pulang? (ya/tidak)"
        )

        conversation_id = None
        if self._get_conversation_id is not None:
            try:
                conversation_id = self._get_conversation_id()
            except Exception:
                conversation_id = None

        import json as _json
        payload_text = _json.dumps({"time_bucket": proposal.time_bucket, "items": [list(i) for i in proposal.items]})
        pending = self._confirmation_handler.request_confirmation(
            request_id=generate_id("proactive_habit"), conversation_id=conversation_id,
            text=payload_text, intent="proactive_habit", confidence=1.0,
        )

        if self._event_bus is not None:
            try:
                from ..text_normalizer import normalize_for_speech
                speak_text = normalize_for_speech(prompt)
            except Exception:
                speak_text = prompt
            self._event_bus.publish(Event(type="speak_request", data={
                "text": speak_text, "raw_text": prompt,
                "request_id": pending.request_id, "source": "proactive_habit",
            }))
        log(f"asking about learned habit proposal: {proposal.items} (time_bucket={proposal.time_bucket!r})", self.name)

    def _retry_queued(self, context: Any) -> None:
        with self._lock:
            queued_ids = [gid for gid, g in self._active.items() if g.status == GoalStatus.QUEUED]
        for gid in queued_ids:
            with self._lock:
                goal = self._active.get(gid)
            if goal is None:
                continue
            active_count = self._active_count()
            decision = self.policy.evaluate(goal, context, active_count)
            if decision.action == PolicyAction.AUTO_EXECUTE:
                goal.policy = decision
                self._execute_goal(goal)
            # else: stays queued (still busy/still over concurrency cap) - left alone.

    def _process_goal(self, goal: Goal, context: Any) -> None:
        active_count = self._active_count()
        decision = self.policy.evaluate(goal, context, active_count)
        goal.policy = decision
        self._publish("goal_generated", goal)

        if decision.action == PolicyAction.DISCARD:
            goal.status = GoalStatus.REJECTED
            goal.result = decision.reasoning
            with self._lock:
                self._rejected.append(goal)
            self._publish("goal_rejected", goal)
            log(f"goal rejected: {goal.description!r} - {decision.reasoning}", self.name)
            return

        if decision.action == PolicyAction.QUEUE:
            goal.status = GoalStatus.QUEUED
            with self._lock:
                self._active[goal.id] = goal
            self._publish("goal_queued", goal)
            log(f"goal queued: {goal.description!r} - {decision.reasoning}", self.name)
            return

        if decision.action == PolicyAction.ASK_CONFIRMATION:
            goal.status = GoalStatus.AWAITING_CONFIRMATION
            with self._lock:
                self._active[goal.id] = goal
            self._publish("goal_awaiting_confirmation", goal)
            log(f"goal awaiting confirmation: {goal.description!r} - {decision.reasoning}", self.name)
            return

        # AUTO_EXECUTE
        self._execute_goal(goal)

    def _active_count(self) -> int:
        with self._lock:
            return sum(1 for g in self._active.values() if g.status in (GoalStatus.QUEUED, GoalStatus.EXECUTING, GoalStatus.AWAITING_CONFIRMATION))

    # -- execution ----------------------------------------------------------------

    def _execute_goal(self, goal: Goal) -> None:
        goal.status = GoalStatus.EXECUTING
        with self._lock:
            self._active[goal.id] = goal
        self._publish("goal_approved", goal)
        log(f"goal executing: {goal.description!r} (confidence={goal.confidence:.1f})", self.name)

        success = True
        result_parts = []

        if goal.action_text:
            if self._planner is None:
                success = False
                result_parts.append("no Planner available")
            else:
                try:
                    plan = self._planner.create_plan(goal.action_text)
                    goal.plan_id = plan.id
                    if plan.validation_errors:
                        success = False
                        result_parts.append(f"plan validation failed: {plan.validation_errors}")
                    else:
                        self._planner.execute(plan)
                        result_parts.append(f"plan {plan.id} dispatched: {goal.action_text!r}")
                except Exception as ex:
                    success = False
                    result_parts.append(f"planner raised: {ex}")

        if goal.speech_text and success:
            try:
                self._speak(goal)
                result_parts.append("spoke confirmation")
            except Exception as ex:
                result_parts.append(f"speech failed (non-fatal): {ex}")

        goal.status = GoalStatus.COMPLETED if success else GoalStatus.EXPIRED
        goal.result = "; ".join(result_parts) if result_parts else "no action_text/speech_text - nothing to do"
        with self._lock:
            self._active.pop(goal.id, None)
            self._completed.append(goal)
        self.policy.record_execution(goal.cooldown_key)
        self._publish("goal_executed", goal)
        log(f"goal executed: {goal.description!r} success={success} result={goal.result}", self.name)

    def _speak(self, goal: Goal) -> None:
        if self._event_bus is None:
            return
        try:
            from ..text_normalizer import normalize_for_speech
            text = normalize_for_speech(goal.speech_text)
        except Exception:
            text = goal.speech_text
        self._event_bus.publish(Event(type="speak_request", data={
            "text": text, "raw_text": goal.speech_text,
            "request_id": generate_id("proactive"),
            "source": "proactive",
        }))

    def _publish(self, event_type: str, goal: Goal) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish(Event(type=event_type, data={"goal": goal.to_dict()}))

    # -- manual approval (Dashboard "Goals" panel) ---------------------------------

    def approve_goal(self, goal_id: str) -> Dict[str, Any]:
        with self._lock:
            goal = self._active.get(goal_id)
        if goal is None:
            return {"ok": False, "message": f"no active goal with id {goal_id!r}"}
        if goal.status != GoalStatus.AWAITING_CONFIRMATION:
            return {"ok": False, "message": f"goal {goal_id!r} is not awaiting confirmation (status={goal.status.value})"}
        context = self.evaluator.evaluate()
        if context.conversation_active:
            goal.status = GoalStatus.QUEUED
            with self._lock:
                self._active[goal.id] = goal
            self._publish("goal_queued", goal)
            return {"ok": True, "message": "approved, but conversation is active - queued for execution shortly"}
        self._execute_goal(goal)
        return {"ok": True, "message": f"goal {goal_id!r} approved and executed"}

    def reject_goal(self, goal_id: str) -> Dict[str, Any]:
        with self._lock:
            goal = self._active.pop(goal_id, None)
        if goal is None:
            return {"ok": False, "message": f"no active goal with id {goal_id!r}"}
        goal.status = GoalStatus.REJECTED
        goal.result = "manually rejected via Dashboard"
        with self._lock:
            self._rejected.append(goal)
        self._publish("goal_rejected", goal)
        return {"ok": True, "message": f"goal {goal_id!r} rejected"}

    # -- introspection (Dashboard collector) ---------------------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            active = list(self._active.values())
            completed = list(self._completed)
            rejected = list(self._rejected)
            last_context = self._last_context
            last_cycle_at = self._last_cycle_at
            cycle_count = self._cycle_count

        def _with_cooldown(g: Goal) -> Dict[str, Any]:
            d = g.to_dict()
            d["cooldown_remaining_s"] = round(self.policy.cooldown_remaining_s(g.cooldown_key), 1)
            return d

        return {
            "enabled": self.config.proactive_enabled,
            "cycle_count": cycle_count,
            "last_cycle_at": last_cycle_at.isoformat() if last_cycle_at else None,
            "last_context": last_context,
            "active_goals": [_with_cooldown(g) for g in active if g.status in (GoalStatus.QUEUED, GoalStatus.EXECUTING)],
            "awaiting_confirmation": [_with_cooldown(g) for g in active if g.status == GoalStatus.AWAITING_CONFIRMATION],
            "completed_goals": [g.to_dict() for g in reversed(completed)],
            "rejected_goals": [g.to_dict() for g in reversed(rejected)],
            "config": {
                "proactive_enabled": self.config.proactive_enabled,
                "evaluation_interval_s": self.config.evaluation_interval_s,
                "cooldown_s": self.config.cooldown_s,
                "auto_execution_threshold": self.config.auto_execution_threshold,
                "confirmation_threshold": self.config.confirmation_threshold,
                "max_concurrent_goals": self.config.max_concurrent_goals,
                "max_executions_per_day": self.config.max_executions_per_day,
            },
        }
