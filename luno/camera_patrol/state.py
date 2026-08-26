"""
state.py
========

`PatrolState` - the small, closed set of states a single patrol run can
be in. Deliberately plain string constants, NOT an `Enum` - same
rationale `real_camera_ptz.py::TapoErrorClass`/`PTZConnectionState`
already documented for this exact codebase: every consumer of a state/
category string here treats "does this string exactly match" as the
contract, and keeping it a plain string keeps this module importable
from `luno/tool_manager/builtin/camera_patrol.py`, `luno/dashboard/
collectors.py`, and test code with zero extra coupling.

State machine (Sprint 71 Phase 2, verbatim):

    IDLE -> STARTING -> MOVING -> DWELLING -> MOVING -> ... -> COMPLETED

Terminal/error states: STOPPED, FAILED.

Invariant this module exists to make trivially checkable: once a patrol
run reaches STOPPED, COMPLETED, or FAILED, it is done - nothing may take
it back to MOVING/DWELLING/STARTING. `is_terminal()` below is the single
place that fact is encoded; `CameraPatrolController` never advances a
patrol whose current state is terminal (checked at every loop boundary,
not just once)."""

from __future__ import annotations


class PatrolState:
    IDLE = "idle"
    STARTING = "starting"
    MOVING = "moving"
    DWELLING = "dwelling"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


#: The complete, closed set - used by tests/validation to catch a typo'd
#: state string immediately rather than silently comparing unequal.
ALL_STATES = frozenset({
    PatrolState.IDLE, PatrolState.STARTING, PatrolState.MOVING, PatrolState.DWELLING,
    PatrolState.STOPPED, PatrolState.COMPLETED, PatrolState.FAILED,
})

#: Once in one of these, a patrol run is over - no further preset may
#: execute, no further state transition may occur for that run.
TERMINAL_STATES = frozenset({PatrolState.STOPPED, PatrolState.COMPLETED, PatrolState.FAILED})


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
