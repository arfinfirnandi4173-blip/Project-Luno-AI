"""
camera_automation
==================

LUNO P0 (Camera Automation / Safe Integration & Non-Regression
Protocol) - lets an allowlisted set of Home Assistant camera/motion
entities feed the ALREADY-EXISTING Sprint 72 `AutomationEngine`
(TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN) without any new
automation framework, any change to the existing Event Bus, or any
change to the existing Home Assistant integration.

This package contains NO Home Assistant client code, NO PTZ/camera
implementation, and NO second automation engine - see `module.py`'s own
module docstring for the full "reuse, don't rewrite" architecture.

Public API:

    from luno.camera_automation import CameraAutomationConfig, CameraAutomationModule

    config = CameraAutomationConfig.from_env()   # enabled=False by default
    module = CameraAutomationModule(config=config)
    module.bind_event_bus(event_bus)
    module.start()   # subscribes to the EXISTING "device_state_changed"
                      # event only if config.enabled is True

--------------------------------------------------------------------
P0.5 (Real Camera Integration) addition
--------------------------------------------------------------------
`cameras.py` adds a generic `CameraProfile -> CameraEvent` normalization
layer (the brief's own "Camera Integration Adapter") - config-driven
per-camera Home Assistant entity-role mapping (`config/
camera_automation.json`), classifying motion/human/availability state
changes into `motion_detected`/`motion_cleared`/`human_detected`/
`human_cleared`/`camera_online`/`camera_offline`, published as a new
`camera_automation.camera_event` Event Bus event. This is purely
ADDITIVE alongside P0's own flat-allowlist `camera_automation.
state_changed` relay (`entities`/`CAMERA_AUTOMATION_ENTITIES`) - both
paths coexist unchanged; see `module.py`'s own docstring for the full
architecture.

--------------------------------------------------------------------
P0.5.3 (Vision Event -> Camera Automation Bridge) addition
--------------------------------------------------------------------
`vision_bridge.py` adds `VisionCameraEventBridge` - a THIRD, independent
input path into this SAME `CameraAutomationModule` (via its new
`ingest_external_camera_event()` method), fed by the ALREADY-EXISTING
`luno.adapters.vision.VisionAdapter`'s `CameraPersonEntered`/
`CameraPersonLeft`/`CameraDisconnected`/`CameraReconnected` Event Bus
events - discovered, not built, by P0.5.2's own audit. No new computer
vision, no changes to `VisionAdapter`/the YOLO/OpenCV/RTSP pipeline. See
`vision_bridge.py`'s own module docstring for the full mapping and
`module.py`'s own docstring for why `ingest_external_camera_event()`
reuses the EXACT SAME dedupe/cooldown the HA-sourced path already uses.
"""

from __future__ import annotations

from .cameras import (
    CAMERA_EVENT_KINDS,
    CameraEvent,
    CameraProfile,
    build_entity_role_index,
    classify_state_change,
    load_camera_profiles,
)
from .config import CameraAutomationConfig
from .module import CAMERA_EVENT_TYPE, OUTPUT_EVENT_TYPE, CameraAutomationModule
from .vision_bridge import VisionCameraEventBridge

__all__ = [
    "CameraAutomationConfig", "CameraAutomationModule", "OUTPUT_EVENT_TYPE", "CAMERA_EVENT_TYPE",
    "CameraProfile", "CameraEvent", "CAMERA_EVENT_KINDS", "classify_state_change", "load_camera_profiles",
    "build_entity_role_index", "VisionCameraEventBridge",
]
