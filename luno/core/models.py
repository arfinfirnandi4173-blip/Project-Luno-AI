"""
models.py
=========

Small, dependency-free data types shared by the rest of Core: module
lifecycle state, health/heartbeat snapshots. Deliberately does NOT
import `luno.vision_memory`, `luno.behavior_tree`, `luno.planner`, or
`luno.tool_manager` - Core integrates those packages via generic
`Module`/provider interfaces (see `module_manager.py`,
`context_builder.py`), never by hard-importing their types, so this
whole package stays testable with zero hardware/other-package
dependencies, exactly like every package built earlier this session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from .utils import utcnow


class ModuleState(str, Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RESTARTING = "restarting"


@dataclass
class ModuleHealthStatus:
    """What a single module reports about itself. `stalled` is distinct
    from `healthy=False`: a module can be "alive" (its thread hasn't
    died) but stalled (hasn't made progress in a while) - callers that
    care about the difference can check both fields; `HealthMonitor`
    treats `stalled=True` as unhealthy for the purposes of `healthy()`."""
    healthy: bool
    stalled: bool = False
    message: str = ""
    checked_at: datetime = field(default_factory=utcnow)


@dataclass
class HealthReport:
    """Whole-system health snapshot returned by `HealthMonitor.report()`
    and `Runtime.health()`."""
    healthy: bool
    modules: Dict[str, ModuleHealthStatus] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utcnow)


@dataclass
class HeartbeatStats:
    """Payload of a `Heartbeat` event - see `events.py` and
    `heartbeat.py`. `cpu_percent`/`ram_mb` are `None` when `psutil`
    isn't installed (honest degrade, not a crash - see
    `heartbeat.py`)."""
    uptime_s: float
    cpu_percent: Optional[float]
    ram_mb: Optional[float]
    active_modules: int
    running_tools: int
    active_plans: int
    queue_size: int
    event_throughput_per_s: float
    avg_latency_ms: float
    generated_at: datetime = field(default_factory=utcnow)
