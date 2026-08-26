"""
luno.bootstrap
==============

Sprint 6 - Production Launcher. Everything a REAL `python main.py` run
needs beyond what already exists: deterministic configuration loading,
a startup banner, automatic module/adapter registration, a startup
health-check framework, background supervision (adapter restart on
failure), graceful shutdown, and structured lifecycle logging.

This package contains ZERO business logic of its own and never
reimplements anything already built - it is pure wiring/orchestration
glue around the 19 already-existing subsystems (Core Runtime, Event
Bus, Module Manager, Scheduler, Behavior Tree, Planner, Tool Manager,
Context Builder, Memory Retrieval, Vision Memory, Wake Session,
Barge-In, Text Normalizer, OpenRouter/Whisper/Fish Audio/Home
Assistant/Vision/Unity Adapters). Every one of those is imported and
used exactly as it already is - "Do NOT rewrite these packages. Reuse
them exactly as they are."

    version.py          - VERSION/build string for the startup banner
    launcher_config.py    - LauncherConfig: deterministic env > .env > config-file > default precedence
    logging_setup.py        - structured lifecycle logging helper
    banner.py                  - startup banner + runtime status printer
    modules.py                    - register_all_modules(runtime, config)
    adapters.py                      - register_all_adapters(runtime, config)
    health.py                           - run_startup_health_checks(...)
    supervisor.py                          - periodic adapter/module restart-on-failure
    shutdown.py                               - signal handling + graceful shutdown sequence
    console.py                                   - thin developer command console (relay only)

See `main.py` at the project root for how these compose into the single
official production entry point:

    python main.py
"""

from .launcher_config import LauncherConfig
from .version import VERSION, build_string

__all__ = ["LauncherConfig", "VERSION", "build_string"]
