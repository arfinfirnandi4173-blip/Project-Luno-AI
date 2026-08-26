"""
camera_patrol
=============

Sprint 71 (Camera Patrol) - lets Luno run a bounded, deterministic,
stoppable-at-any-time sequence of Tapo PTZ presets ("patrol") on top of
the ALREADY-EXISTING camera PTZ foundation (Sprint 69/70's own
`camera_ptz` tool + `classify_tapo_exception` error classification).

This package deliberately contains NO new PTZ implementation - every
actual camera movement still goes through the exact same
`tool_requested` -> `ToolManagerBridgeModule` -> `ToolManager` ->
`camera_ptz` handler round trip every other PTZ command (manual voice
control, the dashboard, etc.) already uses. See `controller.py`'s own
module docstring for the full architecture.

Public API:

    from luno.camera_patrol import CameraPatrolModule, PatrolRoute, PatrolState

    module = CameraPatrolModule(routes_path="config/camera_patrol_routes.json")
    module.bind_event_bus(event_bus)
    module.start()   # Module lifecycle - cheap, no background thread yet
    ...
    result = module.start_patrol(route_name="rumah")
    status = module.get_status()
    module.stop_patrol()
"""

from __future__ import annotations

from .controller import CameraPatrolModule
from .route import PatrolRoute, PatrolRouteError, validate_route
from .state import PatrolState

__all__ = ["CameraPatrolModule", "PatrolRoute", "PatrolRouteError", "validate_route", "PatrolState"]
