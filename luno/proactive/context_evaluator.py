"""
context_evaluator.py
=====================

`ContextEvaluator` - observes the "World Model" (this project's real
equivalent: `vision_memory.get_world_state()`/`get_recent_events()` -
see this package's own `__init__.py` docstring for why there is no
separate `world_model` package to duplicate), Long-Term Memory, current
Planner state, session/barge-in state, and wall-clock time, and reduces
all of it into one flat `ContextSummary`.

Deliberately lightweight (spec: "Context evaluation must be lightweight.
Avoid unnecessary LLM calls."): every input is a already-cached/in-memory
read (Vision Memory's SQLite-backed world state, Planner's in-process
plan dict, session/barge-in status snapshots) - nothing here ever calls
an LLM, makes a network request, or blocks.

Every dependency is injected as a zero-arg callable (the same "provider
callable, not a live object reference" shape `luno.memory_retrieval.
sources.py` already established) so this class is fully testable without
a running Runtime, Vision Memory database, or Planner instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .models import ContextSummary, HumanContext, ObjectContext

_MORNING = range(5, 12)
_AFTERNOON = range(12, 17)
_EVENING = range(17, 22)
# night = everything else (22-23, 0-4)


def _time_bucket(hour: int) -> str:
    if hour in _MORNING:
        return "morning"
    if hour in _AFTERNOON:
        return "afternoon"
    if hour in _EVENING:
        return "evening"
    return "night"


def _noop_dict() -> Dict[str, Any]:
    return {}


def _noop_list() -> List[Any]:
    return []


def _noop_none() -> Optional[Any]:
    return None


class ContextEvaluator:
    def __init__(
        self,
        get_world_state: Optional[Callable[[], Any]] = None,
        get_recent_vision_events: Optional[Callable[[], List[Any]]] = None,
        get_long_term_facts: Optional[Callable[[], List[str]]] = None,
        get_session_summary_count: Optional[Callable[[], int]] = None,
        get_session_status: Optional[Callable[[], Dict[str, Any]]] = None,
        get_barge_in_status: Optional[Callable[[], Dict[str, Any]]] = None,
        get_planner_queue: Optional[Callable[[], Dict[str, List[Any]]]] = None,
        get_last_tool_result: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        get_last_tool_name: Optional[Callable[[], Optional[str]]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._get_world_state = get_world_state
        self._get_recent_vision_events = get_recent_vision_events or _noop_list
        self._get_long_term_facts = get_long_term_facts or _noop_list
        self._get_session_summary_count = get_session_summary_count or (lambda: 0)
        self._get_session_status = get_session_status or _noop_dict
        self._get_barge_in_status = get_barge_in_status or _noop_dict
        self._get_planner_queue = get_planner_queue or _noop_dict
        self._get_last_tool_result = get_last_tool_result or _noop_none
        self._get_last_tool_name = get_last_tool_name or _noop_none
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def evaluate(self) -> ContextSummary:
        now = self._now_fn()

        humans, objects, light_on, door_closed, user_present = self._read_world_state(now)
        recent_events = self._read_recent_events()
        long_term_facts = self._safe_call(self._get_long_term_facts, [])
        session_summary_count = self._safe_call(self._get_session_summary_count, 0)
        session_status = self._safe_call(self._get_session_status, {})
        barge_in_status = self._safe_call(self._get_barge_in_status, {})
        planner_active_count = self._read_planner_active_count()
        last_tool_name, last_tool_target, last_tool_action, last_tool_success = self._read_last_tool()

        session_state = session_status.get("state", "unknown")
        # "Busy" for conversation-awareness purposes means an actual
        # open/in-progress turn - SLEEPING (the common proactive-only
        # case) and IDLE (always-on mode, nothing happening) are NOT
        # busy; every other state (AWAKENING/LISTENING/THINKING/
        # SPEAKING/WAITING_USER) means a human is actively engaged.
        session_busy = session_state not in ("sleeping", "idle", "unknown")
        barge_in_busy = bool(barge_in_status.get("busy"))

        return ContextSummary(
            generated_at=now,
            hour_of_day=now.hour,
            time_bucket=_time_bucket(now.hour),
            user_present=user_present,
            humans=humans,
            objects=objects,
            light_on=light_on,
            door_closed=door_closed,
            recent_vision_event_descriptions=recent_events,
            long_term_facts=long_term_facts,
            session_summary_count=session_summary_count,
            session_state=session_state,
            session_busy=session_busy,
            barge_in_busy=barge_in_busy,
            conversation_active=session_busy or barge_in_busy,
            planner_active_task_count=planner_active_count,
            last_tool_name=last_tool_name,
            last_tool_target=last_tool_target,
            last_tool_action=last_tool_action,
            last_tool_success=last_tool_success,
        )

    # -- individual readers (each defensive - one bad source must never
    #    take down the whole evaluation cycle) ------------------------------

    def _read_world_state(self, now: datetime):
        humans: List[HumanContext] = []
        objects: List[ObjectContext] = []
        light_on: Optional[bool] = None
        door_closed: Optional[bool] = None
        user_present = False
        if self._get_world_state is None:
            return humans, objects, light_on, door_closed, user_present
        try:
            world = self._get_world_state()
        except Exception:
            return humans, objects, light_on, door_closed, user_present
        if world is None:
            return humans, objects, light_on, door_closed, user_present

        try:
            for h in world.humans.values():
                last_seen = getattr(h, "last_seen", None)
                first_seen = getattr(h, "first_seen", None)
                seconds_since = (now - last_seen).total_seconds() if last_seen else 1e9
                # PRESENT if seen recently is a judgment call this
                # project already makes elsewhere for object tracking
                # (see vision_tracking.ObjectTracker's own timeout) -
                # here, "present" just means "in the current world
                # state snapshot at all" (Vision Memory already drops
                # stale entries on its own timeout), so any entry here
                # counts as present.
                humans.append(HumanContext(
                    id=getattr(h, "id", ""),
                    identity=getattr(h, "identity", None),
                    activity=(getattr(h, "activity", None).value if getattr(h, "activity", None) else "unknown"),
                    seconds_in_current_activity=max(0.0, (now - first_seen).total_seconds()) if first_seen else 0.0,
                    seconds_since_last_seen=max(0.0, seconds_since),
                ))
            user_present = len(humans) > 0
        except Exception:
            pass

        try:
            for o in world.objects.values():
                status = getattr(o, "status", None)
                status_value = status.value if hasattr(status, "value") else str(status)
                objects.append(ObjectContext(
                    id=getattr(o, "id", ""),
                    label=getattr(o, "label", ""),
                    location=getattr(o, "location", None),
                    status=status_value,
                ))
        except Exception:
            pass

        try:
            room = world.room
            light_on = getattr(room, "light_on", None)
            door_closed = getattr(room, "door_closed", None)
        except Exception:
            pass

        return humans, objects, light_on, door_closed, user_present

    def _read_recent_events(self) -> List[str]:
        try:
            events = self._get_recent_vision_events()
        except Exception:
            return []
        descriptions = []
        for e in events or []:
            desc = getattr(e, "description", None)
            if desc:
                descriptions.append(desc)
        return descriptions

    def _read_planner_active_count(self) -> int:
        try:
            queue = self._get_planner_queue()
        except Exception:
            return 0
        if not queue:
            return 0
        count = 0
        try:
            for tasks in queue.values():
                for t in tasks:
                    status = getattr(t, "status", None)
                    status_value = status.value if hasattr(status, "value") else str(status)
                    if status_value in ("running", "waiting", "retrying"):
                        count += 1
        except Exception:
            return count
        return count

    def _read_last_tool(self):
        last_tool_name = self._safe_call(self._get_last_tool_name, None)
        last_result = self._safe_call(self._get_last_tool_result, None)
        target = None
        action = None
        success = None
        if isinstance(last_result, dict):
            target = last_result.get("target") or (last_result.get("data") or {}).get("target")
            action = last_result.get("action") or (last_result.get("data") or {}).get("action")
            success = last_result.get("success")
        return last_tool_name, target, action, success

    @staticmethod
    def _safe_call(fn: Callable[[], Any], default: Any) -> Any:
        try:
            result = fn()
        except Exception:
            return default
        return result if result is not None else default
