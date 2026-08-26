"""
Wake Word + Conversation Session Management
============================================

Sprint 2's addition: Luno stays quietly dormant until a configured wake
word is heard, then keeps the user's attention for a configurable
inactivity window without requiring the wake word again, and returns to
dormant automatically once that window elapses.

Nothing here modifies `core`, `adapters`, `behavior_tree`, `planner`,
`tool_manager`, or `vision_memory` - this is a new, independent,
standalone package (same convention as every other subsystem) that
plugs into the SAME Event Bus every other module already uses:

    models.py    - ConversationState enum + WakeSessionConfig (env-only, reloadable)
    matcher.py    - pure text wake-word matching (no Event Bus, no I/O)
    session.py     - ConversationSession: a pure, standalone state machine
    manager.py      - SessionManagerModule: the Event Bus adapter around the two above

Quick start (standalone - exactly like every other package's own docstring)
------------------------------------------------------------------------------
    from luno.core import Runtime
    from luno.wake_session import SessionManagerModule, CONVERSATION_SPEECH_EVENT

    runtime = Runtime()
    session_manager = SessionManagerModule()
    session_manager.bind_event_bus(runtime.event_bus)
    runtime.register_module(session_manager)

    runtime.add_route("speech_recognized", "session_manager")
    runtime.add_route("wake_word_detected", "session_manager")
    runtime.add_route("assistant_response", "session_manager")
    runtime.add_route("speech_playback_finished", "session_manager")
    runtime.add_route("speech_playback_cancelled", "session_manager")
    runtime.add_route("llm_error", "session_manager")
    runtime.add_route("llm_cancelled", "session_manager")
    runtime.add_route(CONVERSATION_SPEECH_EVENT, "behavior_tree")

See `main_runtime_demo.py` for the full, real wiring (including the one
required change to its existing routing table: `speech_recognized` now
targets `session_manager` instead of `behavior_tree` directly, so
Sleeping-state speech never reaches Behavior Tree/Planner/Tool
Manager/OpenRouter at all).
"""

from .manager import CONVERSATION_SPEECH_EVENT, REQUIRED_ROUTES, SessionManagerModule
from .matcher import WakeMatch, looks_like_interrupt_or_resume, match_wake_word, normalize
from .models import TIMEOUT_ACTIVE_STATES, ConversationState, WakeSessionConfig
from .session import ConversationSession, SessionTransition

__all__ = [
    "SessionManagerModule", "CONVERSATION_SPEECH_EVENT", "REQUIRED_ROUTES",
    "WakeMatch", "match_wake_word", "normalize", "looks_like_interrupt_or_resume",
    "ConversationState", "WakeSessionConfig", "TIMEOUT_ACTIVE_STATES",
    "ConversationSession", "SessionTransition",
]
