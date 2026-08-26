"""
luno.dashboard
===============

Sprint 7 - the Web Dashboard. A read-only(-mostly) monitoring and
control surface for an already-running `Runtime`, replacing the
terminal `ProductionConsole` (`/status /health /events /debug ...`)
for daily use with a browser UI.

Architecture (per the spec, verbatim):

    Browser -> Web Dashboard -> Dashboard API -> Production Runtime
               -> Event Bus -> Modules / Adapters

This package IS the "Dashboard API" box. It never runs its own
Runtime, never duplicates Runtime state, and never reaches into an
adapter to make it DO something directly (e.g. it never calls
`fish_audio_adapter.client.play()` itself) - every control it exposes
either (a) calls an existing PUBLIC method already used by
`ProductionConsole`/`main_runtime_demo.py`'s own dev console
(`runtime.reload()`, `session_manager.force_sleep()`,
`adapter_manager.restart(name)`, ...) or (b) publishes an Event onto
the SAME Event Bus a real spoken interrupt/wake word would produce
(`speech_recognized` with an interrupt word, `smoke_detected`, ...) -
see `controls.py`'s own docstring for the full mapping and why each one
is not new business logic.

Modules in this package:

    collectors.py     - pure, read-only "Runtime state -> JSON-safe dict"
                         functions. One function per dashboard view
                         (status, modules, adapters, conversation,
                         planner, tool_manager, vision_memory,
                         memory_retrieval, health, statistics). Every
                         one of these reads the EXACT SAME accessor
                         `luno/bootstrap/console.py`'s own `print_*`
                         methods already use - this package is not a
                         second, independently-derived view of Runtime
                         state, it is the same view, serialized as JSON
                         instead of printed.
    events_buffer.py  - a bounded ring buffer subscribed to the Event
                         Bus (`event_bus.subscribe("*", ...)`) purely
                         for the dashboard's own "Event Bus" live-stream
                         view - the exact same technique
                         `ProductionConsole._wire_listeners()` already
                         uses for its own `/events` command, just also
                         exposed live over SSE.
    logs_buffer.py    - a stdout "tee" capturing every line every
                         package in this project already prints via
                         `luno.core.utils.log()` / `logging_setup.
                         log_lifecycle()` into a searchable, filterable
                         ring buffer - additive only, stdout still
                         receives every line exactly as before.
    controls.py       - the mutation endpoints backing the spec's
                         "Controls" page.
    server.py         - `DashboardServer` - a stdlib
                         `http.server.ThreadingHTTPServer`-based HTTP +
                         SSE API, constructed directly by `main.py`
                         (same pattern as `ProductionConsole` - NOT
                         registered into `ModuleManager`, since the
                         spec's own architecture diagram treats the
                         dashboard as a layer OUTSIDE Runtime's module
                         graph, reading it from the side, not a module
                         Runtime itself supervises).

See `server.py`'s own docstring for why stdlib `http.server` + Server-
Sent Events was chosen over adding a new hard dependency (FastAPI/
Flask/uvicorn) - matches this project's own "zero-dependency-unless-
truly-needed" convention already established for every other package.
"""

from __future__ import annotations

from .server import DashboardServer

__all__ = ["DashboardServer"]
