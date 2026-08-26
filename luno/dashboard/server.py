"""
server.py
=========

`DashboardServer` - the Dashboard API's HTTP + Server-Sent-Events
transport, and the one object `main.py` constructs/starts/stops
(exactly the same pattern already used for `ProductionConsole` - see
that class's own docstring: built from an ALREADY-RUNNING `Runtime`/
`AdapterManager`/module set, never constructs its own).

Why stdlib `http.server` instead of a real framework
------------------------------------------------------
This project has, deliberately, added exactly zero new hard
dependencies for every prior sprint's own HTTP-adjacent work (the real
Home Assistant client is a plain `websockets` connection this project
ALREADY required; nothing here has ever pulled in Flask/FastAPI/
aiohttp/uvicorn). The spec explicitly asks for a "lightweight Dashboard
API" that "does not duplicate Runtime state" - `http.server.
ThreadingHTTPServer` plus stdlib `json`/`urllib.parse` is enough to
serve a handful of read-only JSON endpoints, a couple of Server-Sent-
Events streams, and one static HTML page, with zero new dependencies
and a request-handling model (one thread per connection, all daemon)
that matches every other background worker in this codebase. SSE (not
WebSocket) was chosen for the two genuinely live-streaming views (Event
Bus, Logs) specifically because it needs nothing more than a long-lived
plain HTTP response - no second asyncio event loop to bridge (compare
`real_home_assistant.py`, which genuinely needed one because the
library it wraps is asyncio-only; nothing here does), consistent with
"every other package this session is thread-based, not asyncio-based"
(see `core/event_bus.py`'s own docstring for that same rule).

Never calls an adapter directly
--------------------------------
Every read here goes through `collectors.py` (which itself only ever
reads `runtime.*`/`adapter_manager.status_all()`/module public methods
- see that module's docstring) and every mutation goes through
`controls.py` (which only ever calls an existing public method or
publishes an Event) - this file is pure HTTP/JSON plumbing, it contains
no Runtime-reading or Runtime-mutating logic of its own.
"""

from __future__ import annotations

import errno
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from luno.core.utils import log

from . import automation_api, collectors, controls
from .event_log_writer import DEFAULT_LOG_DIR, EventLogWriter
from .events_buffer import EventRingBuffer, StatsAggregator
from .logs_buffer import LogCapture
from .voice_latency import VoiceLatencyRecorder

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.shutdown import ShutdownCoordinator
    from luno.bootstrap.supervisor import Supervisor

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SSE_KEEPALIVE_S = 15.0

#: Dashboard Turn-State Recovery fix (Phase 4) - `WinError 10038`
#: ("An operation was attempted on something that is not a socket") is
#: the OTHER Windows error a dead/aborted client connection produces
#: here, alongside the four `ConnectionError` subclasses already handled
#: below. It happens when a write is attempted on a per-connection
#: socket that Windows has already invalidated after an earlier failed
#: send on the SAME connection (a well-known `socketserver`/
#: `http.server` platform quirk, most commonly hit by the long-lived
#: `/api/events/stream`/`/api/logs/stream` SSE loop's own keepalive
#: write landing just after the client is already gone). Unlike the
#: four `ConnectionError` subclasses, plain `OSError` with this specific
#: `errno`/`winerror` is NOT automatically caught by `except
#: ConnectionError` - per PEP 3151 only `WSAECONNABORTED` (10053),
#: `WSAECONNRESET` (10054), and `WSAECONNREFUSED` (10061) map to a
#: `ConnectionError` subclass; `WSAENOTSOCK` (10038) stays a plain
#: `OSError`. Deliberately narrow (checks the specific errno on BOTH
#: `errno`/`winerror` - `winerror` is Windows-only, `errno` covers the
#: rare case a socket library surfaces the same condition differently on
#: another platform) so a genuinely unexpected `OSError` (a real bug)
#: still gets logged and reported, not silently swallowed.
_ENOTSOCK_ERRNO = 10038


class DashboardBindError(OSError):
    """Sprint 71 (Dashboard Startup & Access Recovery) - raised by
    `DashboardServer.start()` when the listening socket itself could not
    be bound (port already in use by another process - e.g. a previous
    Luno run that didn't exit cleanly, or a second instance already
    running - or a permission/address error). Subclasses `OSError` (the
    exact type `ThreadingHTTPServer.__init__()`'s own `server_bind()`
    already raises) purely to ATTACH an actionable, host/port-specific
    message - any existing caller written against `except OSError` (the
    stdlib-idiomatic way to catch a bind failure) still catches this
    without any code change; nothing about the underlying error type
    changed, only its message and the fact that `start()` now cleans up
    its own already-installed observability subscriptions before
    re-raising, so a failed `start()` never leaves `DashboardServer` in
    a half-initialized state that a later `stop()` or a caller's own
    retry could trip over."""


def _describe_bind_failure(ex: OSError, host: str, port: int) -> str:
    """Cross-platform bind-failure classification - `errno` is the same
    number on POSIX and Windows for the two conditions that actually
    matter here (`EADDRINUSE`=98 POSIX / `WSAEADDRINUSE`=10048 Windows
    both surface as `errno.EADDRINUSE`=98... except Windows report its
    OWN winerror 10048 with a DIFFERENT `errno` value under some Python/
    OS combinations - so this checks BOTH `errno` and the Windows-only
    `winerror` attribute, never assumes only one platform's shape).
    Never includes anything from `.env`/secrets - only the host/port the
    caller itself already configured (never a credential) and the OS's
    own error text."""
    winerror = getattr(ex, "winerror", None)
    if ex.errno == errno.EADDRINUSE or winerror == 10048:
        return (
            f"port {port} on {host} is already in use - another process (possibly a "
            f"previous Luno run that did not exit cleanly, or a second instance already "
            f"running) is listening there. Stop that process, or set DASHBOARD_PORT in "
            f".env to a different port, then try again."
        )
    if ex.errno == errno.EACCES or winerror == 10013:
        return (
            f"permission denied binding to {host}:{port} - this OS/user account is not "
            f"allowed to listen on that address/port (common for ports below 1024, or a "
            f"restricted network profile on Windows). Try a port above 1024 via "
            f"DASHBOARD_PORT in .env, or run with the appropriate privileges."
        )
    if ex.errno == errno.EADDRNOTAVAIL or winerror == 10049:
        return (
            f"the address {host} is not available on this machine - DASHBOARD_HOST in "
            f".env does not match any local network interface. Use 127.0.0.1 (localhost "
            f"only) or 0.0.0.0 (all interfaces) unless you specifically need a particular "
            f"interface's own IP."
        )
    return f"could not bind {host}:{port} - {ex.strerror or ex}"


def _is_expected_client_disconnect(ex: BaseException) -> bool:
    """True for a dead/aborted client connection Luno should treat as
    ordinary background noise (no log line, no doomed response-write
    attempt) - a `ConnectionError` subclass, or the `WinError 10038`
    "not a socket" quirk described above. False for anything else,
    including every other `OSError` (e.g. a real file-system failure in
    a collector) - those must still be logged, never silently hidden."""
    if isinstance(ex, ConnectionError):
        return True
    if isinstance(ex, OSError):
        return getattr(ex, "winerror", None) == _ENOTSOCK_ERRNO or ex.errno == _ENOTSOCK_ERRNO
    return False


def _read_static_index() -> str:
    index_path = _STATIC_DIR / "index.html"
    try:
        return index_path.read_text(encoding="utf-8")
    except OSError as ex:
        return f"<html><body><h1>Luno Dashboard</h1><p>static/index.html missing: {ex}</p></body></html>"


class DashboardServer:
    def __init__(
        self,
        runtime: "Runtime",
        adapter_manager: "AdapterManager",
        modules: Dict[str, Any],
        launcher_config: "LauncherConfig",
        shutdown_coordinator: Optional["ShutdownCoordinator"] = None,
        supervisor: Optional["Supervisor"] = None,
        audio_capture_store: Optional[Any] = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        observability_log_dir: str = DEFAULT_LOG_DIR,
    ) -> None:
        self.runtime = runtime
        self.adapter_manager = adapter_manager
        self.modules = modules
        self.launcher_config = launcher_config
        self.shutdown_coordinator = shutdown_coordinator
        self.supervisor = supervisor
        #: Sprint 50 (Runtime Observability) - where `EventLogWriter`
        #: persists JSONL/text event history to disk. Defaults to a
        #: `logs/` directory relative to wherever the process runs (same
        #: "local-first, no config required" default every other
        #: dashboard component in this package uses); overridable so
        #: tests can point it at a temp directory instead of the real
        #: repository tree.
        self.observability_log_dir = observability_log_dir
        #: `luno/dashboard/audio_bridge.py::AudioCaptureStore` - `None`
        #: when Fish Audio is on the mock backend (nothing to capture)
        #: or the bootstrap layer didn't wire one in; every chat-audio
        #: code path below handles that case explicitly rather than
        #: assuming it's always present.
        self.audio_capture_store = audio_capture_store
        self.host = host
        self.port = port

        self.debug_enabled = False
        self._log_capture = LogCapture()
        self._events_buffer: Optional[EventRingBuffer] = None
        self._stats: Optional[StatsAggregator] = None
        #: Phase 6/7 (voice pipeline observability) - see
        #: `voice_latency.py`'s own docstring; same lifecycle pattern as
        #: `_events_buffer`/`_stats` above (subscribe in `start()`,
        #: unsubscribe in `stop()`).
        self._voice_latency: Optional[VoiceLatencyRecorder] = None
        #: Sprint 50 (Runtime Observability) - same lifecycle pattern as
        #: `_events_buffer`/`_stats`/`_voice_latency` above (subscribe in
        #: `start()`, unsubscribe in `stop()`); the one NEW thing it does
        #: beyond those three is durable on-disk persistence (JSONL +
        #: human-readable text), see `event_log_writer.py`'s own docstring.
        self._event_log_writer: Optional[EventLogWriter] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._index_html = _read_static_index()
        self._started = False

    # -- lifecycle --------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        if self._started:
            return
        self._log_capture.install()
        self._events_buffer = EventRingBuffer(self.runtime.event_bus)
        self._stats = StatsAggregator(self.runtime.event_bus)
        self._voice_latency = VoiceLatencyRecorder(self.runtime.event_bus)
        self._event_log_writer = EventLogWriter(self.runtime.event_bus, log_dir=self.observability_log_dir)
        self._event_log_writer.start()

        server = self  # closure target for the handler class below

        class _Handler(BaseHTTPRequestHandler):
            server_version = "LunoDashboard/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
                pass  # silenced - luno.core.utils.log() already covers structured logging; the default per-request access log would just be noise here

            def do_GET(self) -> None:  # noqa: N802 - stdlib method name
                server._dispatch_get(self)

            def do_POST(self) -> None:  # noqa: N802 - stdlib method name
                server._dispatch_post(self)

        # Sprint 71 (Dashboard Startup & Access Recovery) - bug fix
        # (reproduced live): `ThreadingHTTPServer(...)`'s own constructor
        # performs the real socket bind synchronously and raises a bare
        # `OSError` on failure (port already in use by a stale/previous
        # Luno process being the most common real-world trigger - "Address
        # already in use" on POSIX, "Only one usage of each socket
        # address..." WinError 10048 on Windows, same underlying OS
        # condition). This call used to be unguarded, and `main.py`'s own
        # `dashboard.start()` call site was ALSO unguarded - together,
        # a bind failure crashed the ENTIRE Luno process (voice pipeline,
        # wake word, everything) with a raw traceback, not just the
        # Dashboard. Every observability subscription made just above
        # (`_log_capture.install()`, `EventRingBuffer`/`StatsAggregator`/
        # `VoiceLatencyRecorder`/`EventLogWriter`) is now rolled back
        # before re-raising, so a failed `start()` never leaves this
        # object half-initialized for a caller's own retry (see Phase 6's
        # own "start twice" verification in `tests/
        # test_sprint71_dashboard_startup_recovery.py`).
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as ex:
            self._log_capture.uninstall()
            if self._events_buffer is not None:
                self._events_buffer.unsubscribe(self.runtime.event_bus)
                self._events_buffer = None
            if self._stats is not None:
                self._stats.unsubscribe(self.runtime.event_bus)
                self._stats = None
            if self._voice_latency is not None:
                self._voice_latency.unsubscribe(self.runtime.event_bus)
                self._voice_latency = None
            if self._event_log_writer is not None:
                self._event_log_writer.stop()
                self._event_log_writer = None
            message = _describe_bind_failure(ex, self.host, self.port)
            log(f"Dashboard failed to start: {message}", "dashboard")
            raise DashboardBindError(ex.errno, message) from ex
        self._httpd.daemon_threads = True
        # `port=0` (used by tests wanting a free OS-assigned port, so
        # many can run concurrently without colliding) only resolves to
        # a real port once bound - sync it back so `self.url`/`self.port`
        # reflect what actually got bound, not the "give me anything" 0.
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="luno-dashboard-http")
        self._thread.start()
        self._started = True
        log(f"Dashboard listening at {self.url}", "dashboard")

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._httpd is not None:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception as ex:
            log(f"error stopping dashboard HTTP server: {ex}", "dashboard")
        if self._events_buffer is not None:
            self._events_buffer.unsubscribe(self.runtime.event_bus)
        if self._stats is not None:
            self._stats.unsubscribe(self.runtime.event_bus)
        if self._voice_latency is not None:
            self._voice_latency.unsubscribe(self.runtime.event_bus)
        if self._event_log_writer is not None:
            self._event_log_writer.stop()
        self._log_capture.uninstall()
        self._started = False
        log("Dashboard stopped", "dashboard")

    # -- routing ------------------------------------------------------------

    def _dispatch_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/":
                self._send_html(handler, self._index_html)
            elif path == "/api/ping":
                self._send_json(handler, collectors.collect_ping(self.runtime))
            elif path == "/api/status":
                self._send_json(handler, collectors.collect_status(self.runtime, self.adapter_manager, self.launcher_config))
            elif path == "/api/modules":
                self._send_json(handler, {"modules": collectors.collect_modules(self.runtime)})
            elif path == "/api/adapters":
                self._send_json(handler, {"adapters": collectors.collect_adapters(self.adapter_manager)})
            elif path == "/api/llm":
                self._send_json(handler, collectors.collect_llm_status(self.adapter_manager))
            elif path == "/api/tts":
                self._send_json(handler, collectors.collect_tts_status(self.adapter_manager))
            elif path == "/api/conversation":
                self._send_json(handler, collectors.collect_conversation(self.runtime, self.modules))
            elif path == "/api/planner":
                self._send_json(handler, collectors.collect_planner(self.modules))
            elif path == "/api/tool_manager":
                history = self._events_buffer.tool_history() if self._events_buffer else []
                self._send_json(handler, collectors.collect_tool_manager(self.modules, history))
            elif path == "/api/verification":
                history = self._events_buffer.verification_history() if self._events_buffer else []
                self._send_json(handler, collectors.collect_verification_status(self.modules, history))
            elif path == "/api/vision_memory":
                self._send_json(handler, collectors.collect_vision_memory(query.get("search", ""), int(query.get("limit", "50"))))
            elif path == "/api/vision":
                self._send_json(handler, collectors.collect_vision(self.adapter_manager, self.modules))
            elif path == "/api/automation":
                self._send_json(handler, collectors.collect_automation(self.modules))
            elif path == "/api/automations":
                # P0.12 (Automation API & CRUD) - the new REST-ish
                # `/api/automations*` family, distinct from the
                # pre-existing singular `/api/automation` status panel
                # above (untouched by this sprint). See `automation_api.
                # py`'s own module docstring for the full contract.
                self._send_json(handler, {"automations": automation_api.list_automations(self.modules)})
            elif path == "/api/automations/schema":
                # P0.13 (Automation Dashboard) - checked BEFORE the
                # single-resource catch-all below, otherwise "schema"
                # would be treated as an automation id (same ordering
                # requirement the P0.12 comment above already documents
                # for this route family).
                self._send_json(handler, automation_api.get_schema(self.modules))
            elif path == "/api/automations/devices":
                # P0.14 - same "checked before the single-resource
                # catch-all" ordering requirement as `/schema` immediately
                # above, otherwise "devices" would be treated as an
                # automation id.
                self._send_json(handler, automation_api.get_devices(self.modules, self.adapter_manager))
            elif path.startswith("/api/automations/") and "/" not in path[len("/api/automations/"):]:
                automation_id = path[len("/api/automations/"):]
                found = automation_api.get_automation(self.modules, automation_id)
                if found is None:
                    self._send_json(handler, {"error": "automation not found", "automation_id": automation_id}, status=404)
                else:
                    self._send_json(handler, found)
            elif path == "/api/goals":
                self._send_json(handler, collectors.collect_goals(self.modules))
            elif path == "/api/memory_retrieval":
                self._send_json(handler, collectors.collect_memory_retrieval(self.modules, query.get("query", "")))
            elif path == "/api/memory/overview":
                self._send_json(handler, collectors.collect_memory_overview())
            elif path == "/api/memory/list":
                self._send_json(handler, collectors.collect_memory_list(
                    lifecycle=query.get("lifecycle", ""), importance=query.get("importance", ""),
                    category=query.get("category", ""), source=query.get("source", ""),
                    conflict_status=query.get("conflict_status", ""), search=query.get("search", ""),
                    sort=query.get("sort", ""),
                    limit=query.get("limit"), offset=query.get("offset"),
                ))
            elif path == "/api/memory/health":
                self._send_json(handler, collectors.collect_memory_health())
            elif path == "/api/memory/maintenance/preview":
                self._send_json(handler, collectors.collect_memory_maintenance_preview())
            elif path == "/api/memory/conflicts":
                self._send_json(handler, collectors.collect_memory_conflicts())
            elif path == "/api/memory/context_leaderboard":
                self._send_json(handler, collectors.collect_memory_context_leaderboard(
                    category=query.get("category", ""), order=query.get("order", "top"),
                    limit=query.get("limit"),
                ))
            elif path == "/api/memory/turns":
                self._send_json(handler, collectors.collect_memory_turn_list(self.modules, limit=query.get("limit")))
            elif path == "/api/memory/decision_trace":
                self._send_json(handler, collectors.collect_memory_decision_trace(
                    self.modules, turn_id=query.get("turn_id", ""),
                    conversation_id=query.get("conversation_id", ""),
                    check_memory_id=query.get("check_memory_id", ""),
                ))
            elif path == "/api/memory/retrieval_funnel":
                self._send_json(handler, collectors.collect_retrieval_funnel(
                    self.modules, turn_id=query.get("turn_id", ""), conversation_id=query.get("conversation_id", ""),
                ))
            elif path == "/api/memory/topic_history_timeline":
                self._send_json(handler, collectors.collect_topic_history_timeline(self.modules, conversation_id=query.get("conversation_id", "")))
            elif path == "/api/memory/quality_metrics":
                self._send_json(handler, collectors.collect_memory_quality_metrics(self.modules))
            elif path == "/api/observability/summary":
                self._send_json(handler, collectors.collect_observability_summary(self.modules, conversation_id=query.get("conversation_id", "")))
            elif path == "/api/observability/session_trace":
                self._send_json(handler, collectors.collect_session_trace(self.modules, conversation_id=query.get("conversation_id", ""), limit=query.get("limit")))
            elif path == "/api/voice/pipeline":
                self._send_json(handler, collectors.collect_voice_pipeline(self._voice_latency, self._log_capture, request_id=query.get("request_id", "")))
            elif path == "/api/voice/latency_timeline":
                self._send_json(handler, collectors.collect_voice_latency_timeline(self._voice_latency, limit=query.get("limit")))
            elif path.startswith("/api/memory/"):
                # Path-param catch-all - MUST stay after every specific
                # `/api/memory/...` match above (Python's `elif` chain
                # already guarantees this: a more specific match short-
                # circuits before this branch is ever reached, so
                # `/api/memory/overview` can never be misrouted here with
                # `memory_id="overview"` - see
                # docs/change_impact/memory_dashboard.md risk #1, verified
                # by a dedicated test).
                memory_id = path[len("/api/memory/"):]
                self._send_json(handler, collectors.collect_memory_detail(memory_id))
            elif path == "/api/routing":
                self._send_json(handler, collectors.collect_routing_status(self.modules, self.adapter_manager))
            elif path == "/api/context":
                self._send_json(handler, collectors.collect_context_preview(self.runtime))
            elif path == "/api/health":
                self._send_json(handler, collectors.collect_health(self.runtime, self.adapter_manager))
            elif path == "/api/configuration":
                self._send_json(handler, collectors.collect_configuration(self.launcher_config))
            elif path == "/api/statistics":
                self._send_json(handler, collectors.collect_statistics(self.runtime, self.modules, self._stats))
            elif path == "/api/debug_state":
                self._send_json(handler, {"debug_enabled": self.debug_enabled})
            elif path == "/api/events":
                limit = int(query.get("limit", "100"))
                events = self._events_buffer.snapshot(limit=limit, event_type_filter=query.get("type") or None, search=query.get("search", "")) if self._events_buffer else []
                self._send_json(handler, {"events": events})
            elif path == "/api/events/stream":
                self._stream_sse(handler, self._events_buffer)
            elif path == "/api/logs":
                limit = int(query.get("limit", "200"))
                logs = self._log_capture.snapshot(
                    limit=limit, module=query.get("module", ""), level=query.get("level", ""),
                    search=query.get("search", ""), request_id=query.get("request_id", ""),
                )
                self._send_json(handler, {"logs": logs})
            elif path == "/api/logs/stream":
                self._stream_sse(handler, self._log_capture)
            elif path == "/api/logs/download":
                self._send_text(handler, self._log_capture.full_text(), content_type="text/plain; charset=utf-8", download_name="luno_logs.txt")
            elif path == "/api/chat/audio":
                self._serve_chat_audio(handler, query.get("request_id", ""))
            else:
                self._send_json(handler, {"error": "not found", "path": path}, status=404)
        except ConnectionError:
            # BUG FIX (reported): client disconnected mid-response/mid-
            # stream - not an error worth logging. Was `(BrokenPipeError,
            # ConnectionResetError)`, which missed `ConnectionAbortedError`
            # (WinError 10053, "An established connection was aborted by
            # the software in your host machine") - the variant Windows
            # actually raises when a long-lived `/api/events/stream` or
            # `/api/logs/stream` SSE connection's client (a closed/
            # refreshed dashboard tab) goes away. All four socket-
            # disconnect exceptions (`BrokenPipeError`,
            # `ConnectionAbortedError`, `ConnectionRefusedError`,
            # `ConnectionResetError`) are subclasses of the same built-in
            # `ConnectionError` - catching that base class instead of an
            # incomplete enumeration covers every platform's variant.
            pass
        except OSError as ex:
            # Dashboard Turn-State Recovery fix (Phase 4): the OTHER
            # expected-disconnect shape (`WinError 10038` - see
            # `_is_expected_client_disconnect()`'s own docstring above)
            # doesn't subclass `ConnectionError`, so it fell into the
            # generic `except Exception` below before this fix - a
            # one-line log plus a doomed attempt to write a 500 response
            # back to a socket that is, per the error itself, no longer a
            # socket. Traced live (this bug's own Phase 0/4/5
            # investigation): this write failure is per-CONNECTION (one
            # daemon thread), never corrupts shared runtime/session state,
            # and was already harmless before this fix - this only
            # removes the noise and the doomed write, it does not change
            # any behavior a client can observe.
            if _is_expected_client_disconnect(ex):
                pass
            else:
                log(f"GET {path} raised: {ex}", "dashboard")
                try:
                    self._send_json(handler, {"error": str(ex)}, status=500)
                except Exception:
                    pass
        except Exception as ex:
            log(f"GET {path} raised: {ex}", "dashboard")
            try:
                self._send_json(handler, {"error": str(ex)}, status=500)
            except Exception:
                pass

    def _dispatch_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        body: Dict[str, Any] = {}
        try:
            length = int(handler.headers.get("Content-Length") or 0)
            if length > 0:
                raw = handler.rfile.read(length)
                body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        try:
            # P0.12 (Automation API & CRUD) - checked BEFORE `_run_control()`
            # since the `/api/automations*` family has its own dynamic
            # `{id}`/`{id}/{verb}` path segments that `_run_control()`'s
            # own flat, exact-match `if path == "..."` chain cannot express.
            # `automation_api.dispatch_post()` returns `None` for any path
            # outside this family (falls through to `_run_control()`
            # unaffected - zero routing change for every existing control),
            # never `None` for a MATCHED-but-failing automation route
            # (those return a `{"success": False, ...}` body instead, same
            # "always a small JSON body, never a bare 404 for a route the
            # caller got right but the id wrong" precedent `/api/memory/
            # controls/*` already established).
            result = automation_api.dispatch_post(self.modules, path, body)
            if result is None:
                result = self._run_control(path, body)
            if result is None:
                self._send_json(handler, {"error": "not found", "path": path}, status=404)
            else:
                self._send_json(handler, result)
        except ConnectionError:
            pass  # see the matching comment in `_dispatch_get` above
        except OSError as ex:
            # see the matching `except OSError` in `_dispatch_get` above
            if _is_expected_client_disconnect(ex):
                pass
            else:
                log(f"POST {path} raised: {ex}", "dashboard")
                try:
                    self._send_json(handler, {"ok": False, "message": str(ex)}, status=500)
                except Exception:
                    pass
        except Exception as ex:
            log(f"POST {path} raised: {ex}", "dashboard")
            try:
                self._send_json(handler, {"ok": False, "message": str(ex)}, status=500)
            except Exception:
                pass

    def _run_control(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if path == "/api/controls/reload_configuration":
            return controls.reload_configuration(self.runtime, self.adapter_manager, self.modules, self.launcher_config)
        if path == "/api/controls/restart_runtime":
            return controls.restart_runtime(self.runtime)
        if path == "/api/controls/restart_module":
            return controls.restart_module(self.runtime, body.get("name", ""))
        if path == "/api/controls/restart_adapter":
            return controls.restart_adapter(self.adapter_manager, body.get("name", ""))
        if path == "/api/controls/switch_llm_provider":
            return controls.switch_llm_provider(self.adapter_manager, body.get("provider", ""))
        if path == "/api/controls/sleep_session":
            return controls.sleep_session(self.modules)
        if path == "/api/controls/wake_session":
            return controls.wake_session(self.modules)
        if path == "/api/controls/clear_planner_queue":
            return controls.clear_planner_queue(self.modules)
        if path == "/api/controls/cancel_current_llm":
            return controls.cancel_current_llm(self.runtime, self.modules)
        if path == "/api/controls/stop_speech":
            return controls.stop_speech(self.runtime, self.modules)
        if path == "/api/controls/resume_speech":
            return controls.resume_speech(self.runtime, self.modules)
        if path == "/api/controls/emergency_stop":
            return controls.emergency_stop(self.runtime)
        if path == "/api/controls/emergency_clear":
            return controls.emergency_clear(self.modules)
        if path == "/api/controls/debug":
            self.debug_enabled = bool(body.get("enabled", not self.debug_enabled))
            return {"ok": True, "message": f"debug {'enabled' if self.debug_enabled else 'disabled'}", "debug_enabled": self.debug_enabled}
        if path == "/api/chat/send":
            return controls.send_chat_message(self.runtime, self.modules, body.get("text", ""))
        if path == "/api/controls/browser_mic_utterance":
            return controls.browser_mic_utterance(self.runtime, body.get("text", ""), body.get("confidence"))
        if path == "/api/controls/approve_goal":
            return controls.approve_goal(self.modules, body.get("goal_id", ""))
        if path == "/api/controls/reject_goal":
            return controls.reject_goal(self.modules, body.get("goal_id", ""))
        if path == "/api/memory/controls/archive":
            return controls.memory_archive(body.get("id", ""))
        if path == "/api/memory/controls/unarchive":
            return controls.memory_unarchive(body.get("id", ""))
        if path == "/api/memory/controls/delete":
            return controls.memory_delete(body.get("id", ""), body.get("confirm"))
        if path == "/api/memory/controls/update":
            return controls.memory_update(body.get("id", ""), body.get("text", ""))
        if path == "/api/memory/controls/mark_important":
            return controls.memory_mark_important(body.get("id", ""))
        if path == "/api/memory/controls/apply_maintenance":
            return controls.memory_apply_maintenance(body.get("confirm"))
        if path == "/api/memory/controls/feedback_positive":
            return controls.memory_feedback_positive(body.get("id", ""))
        if path == "/api/memory/controls/feedback_negative":
            return controls.memory_feedback_negative(body.get("id", ""))
        if path == "/api/memory/controls/recalibrate":
            return controls.memory_recalibrate(body.get("id", ""))
        return None

    # -- SSE ------------------------------------------------------------------

    def _stream_sse(self, handler: BaseHTTPRequestHandler, buffer: Any) -> None:
        """Shared SSE loop for both `/api/events/stream` and
        `/api/logs/stream` - both `EventRingBuffer` and `LogCapture`
        expose the same `add_live_subscriber`/`remove_live_subscriber`
        contract, so one implementation serves both. Sends a keepalive
        comment every `_SSE_KEEPALIVE_S` seconds so a dead connection
        (client gone without a clean close) is discovered promptly
        instead of leaking a thread indefinitely."""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        q: "queue.Queue[Any]" = queue.Queue(maxsize=1000)

        def on_record(record: Any) -> None:
            try:
                q.put_nowait(record)
            except queue.Full:
                pass  # a slow/stuck client must never block real event delivery to everyone else

        buffer.add_live_subscriber(on_record)
        try:
            while self._started:
                try:
                    record = q.get(timeout=_SSE_KEEPALIVE_S)
                except queue.Empty:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
                    continue
                payload = json.dumps(record, default=str)
                handler.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                handler.wfile.flush()
        finally:
            buffer.remove_live_subscriber(on_record)

    # -- chat audio -------------------------------------------------------------

    def _serve_chat_audio(self, handler: BaseHTTPRequestHandler, request_id: str) -> None:
        """Backs the Chat panel's voice-output playback - see
        `audio_bridge.py`'s own docstring for the full capture/
        correlation mechanism. Blocks briefly (bounded by
        `AudioCaptureStore.wait_for()`'s own timeout) since the browser
        may ask for this before synthesis has actually finished; a
        client-side spinner/disabled-speaker-icon is expected to cover
        that wait, not a second poll loop."""
        if not request_id:
            self._send_json(handler, {"error": "request_id query parameter is required"}, status=400)
            return
        if self.audio_capture_store is None:
            self._send_json(handler, {
                "error": "no audio available - Fish Audio is on the mock backend "
                         "(FISH_AUDIO_BACKEND=real is required for real voice output)",
            }, status=404)
            return
        wav_bytes = self.audio_capture_store.wait_for(request_id)
        if wav_bytes is None:
            self._send_json(handler, {"error": f"no audio captured for request_id={request_id!r} (timed out or nothing was synthesized)"}, status=404)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/wav")
        handler.send_header("Content-Length", str(len(wav_bytes)))
        handler.end_headers()
        handler.wfile.write(wav_bytes)

    # -- response helpers -------------------------------------------------------

    def _send_json(self, handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_html(self, handler: BaseHTTPRequestHandler, html: str) -> None:
        body = html.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_text(self, handler: BaseHTTPRequestHandler, text: str, content_type: str, download_name: Optional[str] = None) -> None:
        body = text.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        if download_name:
            handler.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
