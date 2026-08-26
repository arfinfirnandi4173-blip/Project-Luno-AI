"""
goal_generator.py
==================

`GoalGenerator` - turns one `ContextSummary` into zero or more candidate
`Goal`s. Rule-based only (spec: "The Goal Generator should rely
primarily on structured World Model data" / "LLM should only be
consulted when reasoning cannot be performed locally") - every rule here
is plain Python reading `ContextSummary` fields, no LLM call.

Goals are DESCRIPTIONS ONLY - nothing in this file ever touches the
Planner, an adapter, or the Event Bus. `PolicyEngine` (next stage) is
what decides whether a goal actually runs; `ProactiveModule` is what
actually runs it.

Each `_rule_*` method is independent and defensive (a bad/missing field
in `ContextSummary` must never raise past this file - `generate()`
wraps every rule call). Multiple rules may fire in the same cycle
("Multiple triggers may combine" - the spec's own wording); it is
`PolicyEngine`'s job, not this file's, to prioritize/limit them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..core.utils import generate_id
from .habit_memory import HabitMemory
from .models import ContextSummary, Goal, GoalType, ProactiveConfig

# How long a human must be continuously "sitting" before the health
# reminder rule fires - the spec's own "continuous sitting for three
# hours" example.
_HEALTH_REMINDER_SITTING_S = 3 * 3600.0

# How long a room must show "no person present" before the energy-saving
# rule considers an appliance left on to be worth flagging - avoids
# firing the instant someone steps out for a few seconds.
_ENERGY_SAVING_EMPTY_ROOM_S = 5 * 60.0

_AC_KEYWORDS = ("ac", "air_conditioner", "aircon", "air conditioner", "pendingin")
_TV_KEYWORDS = ("tv", "television", "televisi")
_LIGHT_KEYWORDS = ("light", "lamp", "lampu")

_COFFEE_LIKE_FACT_MARKERS = ("coffee", "kopi")
_COFFEE_DISLIKE_MARKERS = ("dislikes coffee", "doesn't like coffee", "don't like coffee", "no coffee at night", "gak suka kopi")


def _now(now_fn: Optional[Callable[[], datetime]]) -> datetime:
    return now_fn() if now_fn else datetime.now(timezone.utc)


class GoalGenerator:
    def __init__(
        self, now_fn: Optional[Callable[[], datetime]] = None,
        config: Optional[ProactiveConfig] = None,
        habit_memory: Optional[HabitMemory] = None,
    ) -> None:
        self._now_fn = now_fn
        #: Plain mutable attribute (not read-only-at-construction) so
        #: `ProactiveModule.reload()` can swap it for a freshly-loaded
        #: `ProactiveConfig` the same way it already reassigns its own
        #: `self.config` - see that method's own docstring.
        self.config = config or ProactiveConfig()
        #: Optional (None = habit-learning rule is fully inert, same
        #: "opt-in by construction" convention as this project's
        #: classifier_client/confirmation_handler wiring elsewhere).
        self._habit_memory = habit_memory

    def generate(self, context: ContextSummary) -> List[Goal]:
        goals: List[Goal] = []
        for rule in (
            self._rule_welcome_user,
            self._rule_health_reminder,
            self._rule_energy_saving,
            self._rule_night_routine,
            self._rule_forgotten_lights,
            self._rule_assistance_offer_coffee,
            self._rule_learned_habit,
        ):
            try:
                produced = rule(context)
            except Exception:
                continue
            if not produced:
                continue
            goals.extend(produced if isinstance(produced, list) else [produced])
        return goals

    def _new_goal(self, gtype: GoalType, description: str, reasoning: str, confidence: float,
                  cooldown_key: str, triggers: List[str], context: ContextSummary,
                  action_text: Optional[str] = None, speech_text: Optional[str] = None) -> Goal:
        return Goal(
            id=generate_id("goal"),
            type=gtype,
            description=description,
            reasoning=reasoning,
            created_at=_now(self._now_fn),
            action_text=action_text,
            speech_text=speech_text,
            confidence=confidence,
            cooldown_key=cooldown_key,
            triggers=list(triggers),
            context_snapshot=context.to_dict(),
        )

    # -- Scenario 1: "User arrived home. Room dark. Evening." -----------------

    def _rule_welcome_user(self, context: ContextSummary) -> Optional[Goal]:
        # Deliberately does NOT early-return on `context.conversation_active`
        # - whether this goal executes now, gets queued, or is asked about
        # is entirely `PolicyEngine`'s call (see its own "Conversation
        # Awareness" branch: "Never interrupt... queue instead"). Goal
        # generation only describes what the world looks like.
        if not self.config.welcome_rule_enabled:
            return None
        if not context.user_present:
            return None
        room_dark = context.light_on is False or context.light_on is None
        is_evening_or_night = context.time_bucket in ("evening", "night")
        if not (room_dark and is_evening_or_night):
            return None
        return self._new_goal(
            GoalType.WELCOME,
            description="Welcome the user home and turn on the lights.",
            reasoning=(
                f"user_present=True, light_on={context.light_on!r}, time_bucket={context.time_bucket!r} "
                "(evening/night + dark room => classic arrival scenario)."
            ),
            confidence=97.0,
            cooldown_key="welcome_user",
            triggers=["human_entered", "room_dark", "time_" + context.time_bucket],
            context=context,
            action_text="turn on the lights",
            speech_text="Welcome back! I turned the lights on for you.",
        )

    # -- Scenario 2: "User working. Continuous sitting for three hours." -----

    def _rule_health_reminder(self, context: ContextSummary) -> Optional[Goal]:
        for human in context.humans:
            if human.activity != "sitting":
                continue
            if human.seconds_in_current_activity < _HEALTH_REMINDER_SITTING_S:
                continue
            return self._new_goal(
                GoalType.HEALTH_REMINDER,
                description="Remind the user to take a break.",
                reasoning=(
                    f"human {human.id} has been 'sitting' for "
                    f"{human.seconds_in_current_activity / 3600.0:.1f}h (>= 3h threshold)."
                ),
                confidence=80.0,
                cooldown_key=f"health_reminder:{human.id}",
                triggers=["human_state", "long_sitting"],
                context=context,
                action_text=None,
                speech_text="You've been sitting for a while - maybe take a short break and stretch?",
            )
        return None

    # -- Scenario 3: "No person detected. AC still running." -----------------

    def _rule_energy_saving(self, context: ContextSummary) -> Optional[Goal]:
        if context.user_present:
            return None
        target = (context.last_tool_target or "").lower()
        action = (context.last_tool_action or "").lower()
        if action != "turn_on" or context.last_tool_success is False:
            return None
        if not any(k in target for k in _AC_KEYWORDS):
            return None
        # Best-effort "how long has the room been empty" proxy: none of
        # the currently-tracked humans are present at all, which Vision
        # Memory itself only reports once someone has genuinely left
        # (see HumanLeft/vision_tracking's own timeout) - good enough for
        # this rule without inventing a second, separate empty-room timer.
        return self._new_goal(
            GoalType.ENERGY_SAVING,
            description=f"Turn off the air conditioner ('{context.last_tool_target}') - no one is in the room.",
            reasoning=(
                f"user_present=False, last Home Assistant action was turn_on on "
                f"target={context.last_tool_target!r} (AC-like) - likely left running unattended."
            ),
            confidence=90.0,
            cooldown_key=f"energy_saving:{context.last_tool_target or 'ac'}",
            triggers=["no_person_detected", "appliance_running"],
            context=context,
            action_text=f"turn off {context.last_tool_target}" if context.last_tool_target else "turn off the air conditioner",
            speech_text=None,
        )

    # -- Scenario 4: "User sleeping. TV still on." ---------------------------

    def _rule_night_routine(self, context: ContextSummary) -> Optional[Goal]:
        sleeping = [h for h in context.humans if h.activity == "sleeping"]
        if not sleeping:
            return None
        target = (context.last_tool_target or "").lower()
        action = (context.last_tool_action or "").lower()
        if action != "turn_on" or not any(k in target for k in _TV_KEYWORDS):
            return None
        return self._new_goal(
            GoalType.NIGHT_ROUTINE,
            description=f"Ask before turning off the TV ('{context.last_tool_target}') - user appears to be sleeping.",
            reasoning=(
                f"human activity=sleeping detected, TV-like target {context.last_tool_target!r} still on - "
                "night routine, medium confidence (ask first per spec's own Scenario 4)."
            ),
            confidence=75.0,
            cooldown_key=f"night_routine:{context.last_tool_target or 'tv'}",
            triggers=["human_state_sleeping", "appliance_running"],
            context=context,
            action_text=f"turn off {context.last_tool_target}" if context.last_tool_target else "turn off the tv",
            speech_text="You seem to be asleep and the TV's still on - want me to turn it off?",
        )

    # -- Forgotten appliance: lights left on in an empty room -----------------

    def _rule_forgotten_lights(self, context: ContextSummary) -> Optional[Goal]:
        if context.user_present or context.light_on is not True:
            return None
        target = (context.last_tool_target or "").lower()
        if not any(k in target for k in _LIGHT_KEYWORDS):
            return None
        return self._new_goal(
            GoalType.FORGOTTEN_APPLIANCE,
            description=f"Turn off the lights ('{context.last_tool_target}') - room is empty.",
            reasoning=f"user_present=False but light_on=True (target={context.last_tool_target!r}) - forgotten appliance.",
            confidence=92.0,
            cooldown_key=f"forgotten_appliance:{context.last_tool_target or 'lights'}",
            triggers=["no_person_detected", "light_on"],
            context=context,
            action_text=f"turn off {context.last_tool_target}" if context.last_tool_target else "turn off the lights",
            speech_text=None,
        )

    # -- Memory-based personalization: "usually drinks coffee after work" ----

    def _rule_assistance_offer_coffee(self, context: ContextSummary) -> Optional[Goal]:
        if not context.user_present:
            return None
        facts_lower = [f.lower() for f in context.long_term_facts]
        likes_coffee = any(any(m in f for m in _COFFEE_LIKE_FACT_MARKERS) for f in facts_lower)
        if not likes_coffee:
            return None
        dislikes_at_night = context.time_bucket == "night" and any(
            any(m in f for m in _COFFEE_DISLIKE_MARKERS) for f in facts_lower
        )
        if dislikes_at_night:
            return None
        if context.time_bucket == "night":
            # Even with no explicit "dislikes coffee at night" fact on
            # file, offering coffee at night is low-confidence by
            # default - genuinely ambiguous, not a case local rules can
            # resolve with confidence (see spec's LLM Decision Gate).
            return self._new_goal(
                GoalType.ASSISTANCE_OFFER,
                description="Offer coffee (uncertain - it's nighttime).",
                reasoning="Memory shows the user likes coffee, but it's nighttime and no explicit preference either way is on file.",
                confidence=45.0,
                cooldown_key="assistance_offer:coffee",
                triggers=["memory_preference", "time_night"],
                context=context,
                action_text=None,
                speech_text="Want me to start some coffee?",
            )
        return self._new_goal(
            GoalType.ASSISTANCE_OFFER,
            description="Offer to make coffee - the user usually has some after arriving.",
            reasoning="Memory shows the user usually drinks coffee after work/arrival; user just arrived, not nighttime.",
            confidence=70.0,
            cooldown_key="assistance_offer:coffee",
            triggers=["memory_preference", "human_entered"],
            context=context,
            action_text=None,
            speech_text="Want me to start some coffee?",
        )

    # -- Learned habit: "usually turns on X (and Y) around this time" --------
    # (see luno/proactive/habit_memory.py - a pattern only ever reaches
    # "confirmed" after Vinn has both DONE it repeatedly across several
    # distinct days AND said yes to the one-time voice question asking
    # whether to automate it. This rule never invents anything - it only
    # replays an already-confirmed, already-observed pattern.)

    def _rule_learned_habit(self, context: ContextSummary) -> Optional[Goal]:
        if self._habit_memory is None or not context.user_present:
            return None
        habits = self._habit_memory.active_habits_for(context.time_bucket)
        if not habits:
            return None
        phrases = [f"turn {'on' if action == 'turn_on' else 'off'} {target}" for action, target in habits]
        # "on"/"off" spoken accurately even for a mixed set (e.g. "turn on
        # the AC and turn off the hallway light") - never just assumes
        # everything in the bundle was a turn_on.
        display = ", ".join(
            f"{target.replace('_', ' ')} ({'on' if action == 'turn_on' else 'off'})" for action, target in habits
        )
        return self._new_goal(
            GoalType.OTHER,
            description=f"Run the learned arrival routine ({display}).",
            reasoning=(
                f"user_present=True, time_bucket={context.time_bucket!r} - {len(habits)} previously-CONFIRMED "
                "habit(s) match this arrival (Vinn both did this repeatedly across multiple days and said "
                "yes when asked whether to automate it)."
            ),
            confidence=97.0,
            cooldown_key=f"learned_habit:{context.time_bucket}",
            triggers=["human_entered", "learned_habit", "time_" + context.time_bucket],
            context=context,
            action_text=" and ".join(phrases),
            speech_text=f"Done - {display}, like usual.",
        )
