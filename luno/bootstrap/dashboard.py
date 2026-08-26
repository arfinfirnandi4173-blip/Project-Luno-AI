"""
dashboard.py
=============

`register_dashboard(...)` - the one call `main.py` makes for Sprint 7's
"Registering dashboard..." step, constructing (but not yet starting -
see `main.py`) a `DashboardServer` from the SAME already-running
`Runtime`/`AdapterManager`/module set `ProductionConsole` was built
from. Mirrors that exact construction pattern deliberately: the
dashboard is a second, HTTP-based read/control surface over the SAME
Runtime, not a second Runtime.

`DASHBOARD_ENABLED=false` fully disables it (returns `None`) - a
deployment that wants ZERO extra listening sockets keeps working
exactly as Sprint 6 left it, no dashboard thread ever starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from luno.core.utils import log

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig
    from .shutdown import ShutdownCoordinator
    from .supervisor import Supervisor


def register_dashboard(
    runtime: "Runtime",
    adapter_manager: "AdapterManager",
    modules: Dict[str, Any],
    launcher_config: "LauncherConfig",
    shutdown_coordinator: Optional["ShutdownCoordinator"] = None,
    supervisor: Optional["Supervisor"] = None,
    audio_capture_store: Optional[Any] = None,
) -> Optional[Any]:
    if not launcher_config.dashboard_enabled:
        log("Dashboard disabled by config (DASHBOARD_ENABLED=false)", "dashboard")
        return None

    from luno.dashboard import DashboardServer

    return DashboardServer(
        runtime=runtime,
        adapter_manager=adapter_manager,
        modules=modules,
        launcher_config=launcher_config,
        shutdown_coordinator=shutdown_coordinator,
        supervisor=supervisor,
        audio_capture_store=audio_capture_store,
        host=launcher_config.dashboard_host,
        port=launcher_config.dashboard_port,
    )
