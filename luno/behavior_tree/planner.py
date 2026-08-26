"""
planner.py
==========

Bridges "Conversation Behavior" to the LLM, implementing the spec's
central split of responsibility:

    The Behavior Tree chooses actions. The LLM chooses wording.

`Planner` gathers the context a reply needs (conversation state, Vision
Memory summary, room/Home Assistant state, current emotion) from the
Blackboard, then hands off to an injected `generate_reply` handler for the
actual "send to OpenRouter, get wording back" step - it does NOT build a
prompt string or call any LLM client itself.

Why delegate instead of implementing the OpenRouter call here: main.py's
`Luno_Brain()` ALREADY implements exactly this (system prompt assembly,
tool-calling loop, OpenRouter request) end to end and is the single place
that should own it. Re-implementing a second prompt-building/tool-calling
path here would duplicate that logic and let the two drift apart. In real
wiring, `Handlers.generate_reply` (see actions.py) IS `Luno_Brain` (or a
thin wrapper around it) - `Planner` just decides WHEN to call it and WHAT
extra context to make available, matching the spec's split exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from .blackboard import Blackboard

if TYPE_CHECKING:
    from .actions import Handlers


class Planner:
    """Stateless - every method takes the Blackboard/handlers it needs
    explicitly rather than holding its own references, so it's easy to
    call from tests without constructing a whole tree."""

    @staticmethod
    def gather_context(bb: Blackboard) -> Dict[str, Any]:
        """Everything "Conversation Behavior" is supposed to gather per the
        spec (conversation history, Vision Memory, long-term memory, Home
        Assistant state), pulled off the Blackboard into one plain dict.
        NOTE: full conversation HISTORY itself already lives in
        `luno.memory` (see `memory.get_history()`) and Home Assistant
        service/state access already lives in `luno.ha_client` - both are
        available to whatever `generate_reply` handler is wired in without
        needing to pass through here too. This dict is the ADDITIONAL
        context specific to the Behavior Tree's own state (vision summary,
        emotion, room state) that `generate_reply` wouldn't otherwise see."""
        return {
            "vision_world_state": bb.vision.world_state_summary,
            "vision_recent_events": bb.vision.recent_events_summary,
            "vision_long_term_memory": bb.vision.long_term_summary,
            "room_state": {
                "light_on": bb.room.light_on,
                "door_closed": bb.room.door_closed,
                "temperature": bb.room.temperature,
                "dark": bb.room.dark,
            },
            "emotion": bb.emotion,
            "turn_count": bb.conversation.turn_count,
        }

    @staticmethod
    def handle_user_text(bb: Blackboard, handlers: "Handlers", user_text: str) -> str:
        """Runs the actual generation. Raises `RuntimeError` if no
        `generate_reply` handler was configured - callers (see
        `actions.run_conversation`) are expected to catch this the same
        way they catch any other handler failure (log, transition to
        ERROR_RECOVERY, never crash the tick loop)."""
        if handlers.generate_reply is None:
            raise RuntimeError(
                "Planner.handle_user_text: no generate_reply handler configured - "
                "see actions.Handlers.generate_reply."
            )
        context = Planner.gather_context(bb)
        return handlers.generate_reply(user_text, context)
