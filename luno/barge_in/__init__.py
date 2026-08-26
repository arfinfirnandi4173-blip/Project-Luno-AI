"""
Full Conversational Barge-In (Sprint 3)
=========================================

Luno must always be able to hear the user, even mid-sentence. This
package decides WHAT interrupting actually means for the reply/task
currently in flight, based on which `SpeakingMode` that reply was
classified as - it never itself calls OpenRouter, Fish Audio, Planner,
Behavior Tree, or Tool Manager; every decision is a published Event.

    models.py       - SpeakingMode enum (FREE/SOFT/CONFIRM/CRITICAL) + BargeInConfig
    matcher.py        - pure text matching (interrupt words / resume words / yes-no)
    classifier.py       - pure, rule-based classify_speaking_mode() (no LLM, ever)
    manager.py            - BargeInModule: the Event Bus adapter around the above

Quick start (standalone)
----------------------------
    from luno.core import Runtime
    from luno.barge_in import BargeInModule, REQUIRED_ROUTES

    runtime = Runtime()
    barge_in = BargeInModule()
    barge_in.bind_event_bus(runtime.event_bus)
    runtime.register_module(barge_in)
    for pattern in REQUIRED_ROUTES:
        runtime.add_route(pattern, "barge_in")

Deliberately decoupled from `luno.wake_session` (Sprint 2) - both
subscribe independently to the same raw `speech_recognized` events
(ordinary Event Bus fan-out, the same mechanism `"motion"` already uses
to reach both `behavior_tree` and `vision_memory`); neither package
imports the other, so each stays independently testable with zero
cross-package imports, matching every other package in this project.

See `main_runtime_demo.py` for the full real wiring, including how a
turn's `SpeakingMode` gets assigned (`PlannerBridgeModule` publishes
`speaking_mode_assigned` right before `NeedLLMResponse`, using
`classify_speaking_mode()` against the user's request text and whatever
tool the Planner actually ran).
"""

from .classifier import classify_speaking_mode
from .manager import BargeInModule, REQUIRED_ROUTES
from .matcher import match_confirmation, match_interrupt_word, match_resume_word, normalize
from .models import BargeInConfig, SpeakingMode

__all__ = [
    "BargeInModule", "REQUIRED_ROUTES",
    "SpeakingMode", "BargeInConfig",
    "classify_speaking_mode",
    "match_confirmation", "match_interrupt_word", "match_resume_word", "normalize",
]
