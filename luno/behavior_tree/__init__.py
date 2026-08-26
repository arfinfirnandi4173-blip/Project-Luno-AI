"""
Behavior Tree
=============

Luno's central decision-making system - decides WHAT Luno should do next.
It does NOT generate language; that remains OpenRouter's job (see
`planner.py`'s module docstring). The Behavior Tree chooses actions, the
LLM chooses wording.

    Whisper / Vision Memory / YOLO / Gemini Vision / Home Assistant /
    System Status / Conversation Memory / Long-Term Memory /
    Current Emotion / Activity / Time / Date
                        |
                        v
                  blackboard.py  (shared state)
                        |
              +---------+----------+
              |                    |
        conditions.py         attention.py / emotion.py
        (what's true?)        (update internal state)
              |
        behavior_tree.py  <-- priority-ordered BehaviorNode list
              |
        actions.py  --(Handlers, dependency-injected)--> real Luno I/O
              |
        scheduler.py  (ticks the tree every 100-200ms, async dispatch)

Quick start (standalone - no real hardware/API needed)
--------------------------------------------------------
    from luno.behavior_tree import BehaviorTree, Handlers, Blackboard

    bb = Blackboard()
    handlers = Handlers(speak=print)  # swap in real functions later
    tree = BehaviorTree(blackboard=bb, handlers=handlers)

    bb.user.present = True
    tree.tick()

Running for real (100-200ms loop, async dispatch)
----------------------------------------------------
    from luno.behavior_tree import BehaviorTree, Scheduler, Handlers

    tree = BehaviorTree(handlers=my_real_handlers)
    scheduler = Scheduler(tree, tree.bb, perceive=my_perceive_function,
                           executor=None)  # Scheduler makes its own executor
    scheduler.start()

Architecture (see each file's own docstring for the full story)
-------------------------------------------------------------------
    blackboard.py      shared state (UserState/RoomState/Conversation/Vision/
                        System/Tool status, event queues)
    state_machine.py    LunoState enum + single-active-state enforcement
    conditions.py         pure predicates, one per priority tier
    attention.py            "can we interrupt the user right now?"
    emotion.py                "what's the emotional read on this moment?"
    cooldowns.py                "haven't we already said this recently?"
    planner.py                    bridges Conversation Behavior to the LLM
    actions.py                      one function per Behavior + Handlers DI seam
    behavior_tree.py                   priority-ordered orchestrator (tick())
    scheduler.py                          runs tick() every 100-200ms, async dispatch

This package is standalone and does NOT import anything from `main.py` or
any other Luno module that needs a live camera/microphone/API key -
wiring real Luno functions in happens by constructing a `Handlers` and a
`perceive` callback elsewhere (not part of this module), the same
"standalone first, integrate later" shape `luno/vision_memory/` used.
"""

from .actions import ActionResult, Handlers, RunContext, dispatch_tool
from .attention import AttentionEstimator, AttentionState
from .behavior_tree import BehaviorNode, BehaviorTree
from .blackboard import (
    Blackboard,
    ConversationContext,
    HAEvent,
    HAEventSeverity,
    RoomState,
    SystemStatus,
    ToolStatus,
    UserState,
    VisionContext,
    VisualEvent,
)
from .cooldowns import CooldownManager
from .emotion import Emotion, EmotionEstimator, EmotionSignals
from .planner import Planner
from .scheduler import Scheduler
from .state_machine import LunoState, StateMachine

__all__ = [
    "BehaviorTree", "BehaviorNode",
    "Blackboard", "UserState", "RoomState", "ConversationContext", "VisionContext",
    "SystemStatus", "ToolStatus", "HAEvent", "HAEventSeverity", "VisualEvent",
    "StateMachine", "LunoState",
    "CooldownManager",
    "Emotion", "EmotionEstimator", "EmotionSignals",
    "AttentionState", "AttentionEstimator",
    "Planner",
    "Handlers", "RunContext", "ActionResult", "dispatch_tool",
    "Scheduler",
]
