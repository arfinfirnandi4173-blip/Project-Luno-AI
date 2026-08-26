"""
tests/test_dashboard_turn_state_recovery.py
=============================================

Dashboard Turn-State Recovery fix - dedicated regression suite.

ROOT CAUSE (see `docs/change_impact/dashboard_turn_state_recovery.md` for
the full writeup): `PlannerBridgeModule._handle_utterance()`
(`main_runtime_demo.py`) runs on a freshly-spawned, unsupervised
`luno-planner-turn` daemon thread (`on_event()`'s own
`threading.Thread(...).start()`). That ~1700-line method already wraps
many of its own individually risky steps in their own `try/except` ("a
bug here must never break a turn"), but large stretches between those
blocks - `self.planner.create_plan()` and the final
`self._event_bus.publish(NeedLLMResponse(...))` call among them - were
NOT individually guarded. `SessionManagerModule` already transitions the
session to `THINKING` before this thread even starts
(`_forward_to_conversation()`, `luno/wake_session/manager.py`), and
THINKING has no timeout anywhere in this codebase (that file's own
docstring: "a permanent post-reply deadlock"). Its only existing
recovery path, `SessionManagerModule._handle_llm_failure()`, is keyed
exclusively on `llm_error`/`llm_cancelled` - both published only from
inside the OpenRouter adapter's own already-guarded `_run_request()`. An
exception escaping `_handle_utterance()` before it ever reached
`NeedLLMResponse` (proven live via a `ConnectionAbortedError` injected
into `self.planner.create_plan()` - the exact WinError 10053 shape from
the reported bug) therefore left THINKING stuck forever, and the
Dashboard's own busy-guard in `send_chat_message()`
(`luno/dashboard/controls.py`) permanently rejected every further
ordinary command with "Luno is busy right now (state=thinking)".

THE FIX: `PlannerBridgeModule._run_utterance_turn_safely()` (new,
`main_runtime_demo.py`) is now the thread's actual target instead of
`_handle_utterance()` directly. It wraps the call in `try/except` and,
on any escaped exception, publishes the SAME `llm_error` event a real
OpenRouter failure already publishes - reusing the EXISTING routes to
`session_manager` (clears THINKING) and `barge_in` (clears its own
`thinking` flag) rather than adding any new event type, route, or state
machine. Separately, `luno/dashboard/server.py` gained
`_is_expected_client_disconnect()` - `WinError 10038` ("not a socket",
the OTHER Windows error named in the bug report) does not subclass
`ConnectionError` and was previously logged/attempted-to-respond-to as
if it were a genuine failure; it is now classified alongside the four
`ConnectionError` subclasses as ordinary client-disconnect noise, while
every OTHER `OSError` (a real bug) is still logged. `luno/bootstrap/
shutdown.py` was investigated and left UNCHANGED - `dashboard.stop()`
already wraps its own socket teardown in `try/except Exception`, so a
`WinError 10038` there was already caught, logged once, and non-fatal;
it was never the root cause of the live (non-shutdown) stuck-Thinking
bug.

Same self-contained-helpers house style as `tests/
test_runtime_observability.py`/`tests/test_memory_voice_observability.py`
(this project's own established "duplicate the small helper set per test
file" convention, not a cross-file import).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Callable, List

import pytest
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.core import Event  # noqa: E402
from luno.dashboard import controls as dash_controls  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard.server import _is_expected_client_disconnect  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_turn_state_recovery", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_turn_state_recovery"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, canned_text="Oke, dimengerti.", **kwargs):
    """Mocks BOTH adapters, not just OpenRouter - the real
    `FishAudioAdapter` client defaults to a real `http://127.0.0.1:9880`
    TTS server that doesn't exist in this sandbox, and a connection
    failure there prevents `speech_playback_started` from ever firing.
    Since `SessionManagerModule`'s THINKING -> SPEAKING transition for a
    streamed reply is keyed on THAT event (see `luno/wake_session/
    manager.py`'s own docstring), an unmocked Fish Audio client makes
    even an entirely NORMAL turn look like a stuck-THINKING bug - a
    sandbox/environment artifact, not a defect in the fix under test.
    Matches the same `MockFishAudioClient` convention already used by
    `tests/test_memory_voice_observability.py`'s own E2E tests."""
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    kwargs.setdefault("fish_audio_client", MockFishAudioClient(playback_delay_s=0.01))
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0), **kwargs)


def _state(console) -> str:
    return console.session_manager.status_snapshot().get("state")


def _reached_state_since(console, state_value: str, since: float) -> bool:
    """True if `console.session_manager`'s session transitioned INTO
    `state_value` at or after wall-clock time `since` (`time.time()`
    units), even if the live, currently-polled state has already moved
    on by the time this is checked.

    Sprint 55 stability-gate finding: `_wait_until(lambda: _state(console)
    == "thinking", ...)` polls the CURRENT state on a fixed interval, but
    `_forward_to_conversation()` transitions synchronously to THINKING
    and the mocked LLM+TTS round trip (`chunk_delay_s=0.0`,
    `playback_delay_s=0.01`) can complete the ENTIRE turn - THINKING all
    the way back to WAITING_USER - fast enough, in an already-warmed-up
    interpreter (e.g. this test running after another test in the same
    pytest process has already paid every import/JIT/cache cost), that
    the live poll can genuinely never observe the transient THINKING
    state in between two samples. Reproduced directly outside pytest
    (`test_01` then `test_05` in the same process): the planner's own
    "plan created" log line for the SECOND normal turn appears
    immediately after the SpeechRecognized/ForwardedToPlanner=True log
    line, and by the time the assertion re-checked state it had already
    advanced all the way to `waiting_user` - proof the full cycle
    completed (recovery works, nothing is stuck), not that THINKING was
    ever skipped. This does NOT reproduce with any real LLM/TTS backend
    (genuine network/synthesis latency makes THINKING last many
    milliseconds at minimum) - it is a test-polling-granularity artifact
    of a zero-delay mock, not evidence of the production stuck-at-
    THINKING defect class this file exists to guard against (the
    opposite: recovery is happening, just too fast for a coarse poll to
    catch mid-flight). Reading `ConversationSession.history` instead
    (populated synchronously, inside `transition_to()`'s own lock, at
    the exact moment the transition happens - see `luno/wake_session/
    session.py`) checks the FACT of the transition rather than racing a
    live snapshot against it."""
    history = console.session_manager.session.history
    for entry in history:
        if entry.to_state.value == state_value and entry.at.timestamp() >= since:
            return True
    return False


def _wake(console) -> None:
    console.session_manager.force_wake(reason="test")
    assert _wait_until(lambda: _state(console) in ("listening", "waiting_user", "idle"), 3.0), \
        f"session never left AWAKENING (state={_state(console)!r})"


def _speak(console, text: str, conversation_id=None) -> None:
    data = {"text": text, "confidence": None, "source": "test"}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    console.event_bus.publish(Event(type="speech_recognized", data=data))


def _dashboard_modules(console):
    return {"session_manager": console.session_manager, "barge_in_module": console.barge_in_module}


class _FakeRuntimeForControls:
    """`send_chat_message()` only ever reads `runtime.event_bus` - a real
    `RuntimeDemoConsole.runtime` object works too, but this thin stand-in
    (same pattern `luno/dashboard/controls.py`'s own module docstring
    describes) keeps these tests from depending on any other Runtime
    attribute that isn't actually used by the function under test."""

    def __init__(self, event_bus):
        self.event_bus = event_bus


@pytest.fixture
def tmp_log_dir():
    d = tempfile.mkdtemp(prefix="luno_turn_state_recovery_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────
#  1 - normal turn returns to a non-busy state
# ─────────────────────────────────────────────

def test_01_e2e_normal_turn_returns_to_non_busy_state():
    """Baseline (must keep working unchanged by this fix): an ordinary
    turn reaches THINKING and then leaves it on its own, no injected
    failure at all."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        _speak(console, "halo, apa kabar?")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        assert _wait_until(lambda: _state(console) != "thinking", 6.0), \
            f"normal turn never left THINKING (state={_state(console)!r})"
        # THINKING -> SPEAKING (mocked playback, brief) -> WAITING_USER/
        # IDLE/LISTENING is the normal, self-clearing sequence - only
        # THINKING itself has no timeout, so allow a moment for SPEAKING
        # to settle rather than asserting on whatever single instant this
        # poll happened to land on.
        assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 4.0), \
            f"turn left THINKING but never fully settled (state={_state(console)!r})"
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  2 - exception mid-turn returns to a non-busy state (the core fix)
# ─────────────────────────────────────────────

def test_02_e2e_exception_in_create_plan_recovers_instead_of_sticking():
    """The exact live reproduction this bug report's own WinError 10053
    traceback matches: `self.planner.create_plan()` (a call inside
    `_handle_utterance()` NOT individually wrapped in its own
    try/except) raises `ConnectionAbortedError`. Before the fix this
    left `session_manager` at THINKING forever; after the fix it
    recovers via the reused `llm_error` event."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        orig_create_plan = console.planner_module.planner.create_plan
        console.planner_module.planner.create_plan = lambda *a, **kw: (_ for _ in ()).throw(
            ConnectionAbortedError("[WinError 10053] An established connection was aborted by the software in your host machine")
        )
        try:
            _speak(console, "nyalakan lampu ruang tamu")
            assert _wait_until(lambda: _state(console) == "thinking", 6.0), "never reached THINKING"
            assert _wait_until(lambda: _state(console) != "thinking", 5.0), \
                f"session stuck at THINKING after an unhandled exception (state={_state(console)!r})"
            assert _state(console) in ("waiting_user", "idle", "listening")
        finally:
            console.planner_module.planner.create_plan = orig_create_plan
    finally:
        console.stop()


def test_02b_uncaught_thread_exception_no_longer_escapes():
    """Same injection as test_02, checked from the OTHER side: Python's
    default `threading.excepthook` must no longer fire for this turn -
    the wrapper's own `try/except` is what's catching it now, not
    nothing."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    captured: List[threading.ExceptHookArgs] = []
    orig_hook = threading.excepthook
    threading.excepthook = captured.append
    try:
        _wake(console)
        console.planner_module.planner.create_plan = lambda *a, **kw: (_ for _ in ()).throw(ConnectionAbortedError("boom"))
        _speak(console, "test escape hook")
        assert _wait_until(lambda: _state(console) != "listening", 3.0)
        assert _wait_until(lambda: _state(console) not in ("thinking",), 5.0)
        time.sleep(0.2)  # let any stray excepthook call land
        assert captured == [], f"an exception still escaped uncaught: {captured}"
    finally:
        threading.excepthook = orig_hook
        console.stop()


# ─────────────────────────────────────────────
#  3 - client disconnect does not leave the session/Dashboard stuck
# ─────────────────────────────────────────────

def test_03_e2e_dashboard_client_disconnect_mid_sse_does_not_stick():
    """An abrupt client-side socket close mid-`/api/events/stream` (the
    WinError 10053 shape on the DASHBOARD's own HTTP layer, distinct
    from test_02's LLM-side injection) must never affect the backend
    session/turn state, and the server must stay responsive."""
    import socket

    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    from luno.bootstrap.launcher_config import LauncherConfig
    _obs_dir = tempfile.mkdtemp(prefix="luno_turn_state_recovery_dash_")
    dashboard = DashboardServer(
        console.runtime, console.adapter_manager, _dashboard_modules(console), LauncherConfig(),
        host="127.0.0.1", port=0, observability_log_dir=_obs_dir,
    )
    dashboard.start()
    try:
        _wake(console)
        s = socket.create_connection(("127.0.0.1", dashboard.port), timeout=5)
        s.sendall(b"GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        time.sleep(0.3)
        s.close()
        time.sleep(0.3)

        r = requests.get(dashboard.url + "api/health", timeout=5)
        assert r.status_code == 200, "dashboard did not survive an abrupt client disconnect"

        result = dash_controls.send_chat_message(console.runtime, _dashboard_modules(console), "masih di sana?")
        assert result.get("ok") is True, f"chat send blocked after an unrelated client disconnect: {result}"
    finally:
        dashboard.stop()
        console.stop()
        shutil.rmtree(_obs_dir, ignore_errors=True)


# ─────────────────────────────────────────────
#  4 - cancellation returns to a non-busy state
# ─────────────────────────────────────────────

def test_04_e2e_cancellation_returns_to_non_busy_state():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    # A real chunk delay (unlike every other test's chunk_delay_s=0.0) so
    # there is an actual window to cancel a still-in-flight request - a
    # 0-delay mock reply can complete before the assertion below ever
    # gets a chance to look at `_inflight`.
    console = demo.RuntimeDemoConsole(
        openrouter_client=MockOpenRouterClient(canned_text="cerita panjang sekali " * 20, chunk_delay_s=0.2),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.01),
    )
    console.start()
    try:
        _wake(console)
        _speak(console, "ceritakan dongeng yang panjang")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        inflight = _wait_until(lambda: bool(console.openrouter_adapter._inflight), 3.0)
        assert inflight, "no in-flight OpenRouter request to cancel"
        request_id = next(iter(console.openrouter_adapter._inflight.keys()))
        from luno.adapters.events import CancelLLMRequest
        console.event_bus.publish(CancelLLMRequest(data={"request_id": request_id}))
        assert _wait_until(lambda: _state(console) != "thinking", 5.0), \
            f"session stuck at THINKING after cancellation (state={_state(console)!r})"
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  5 / 6 - repeated failure/recovery, next command works every time
# ─────────────────────────────────────────────

def test_05_e2e_repeated_failure_recovery_cycle_stays_usable():
    """normal -> failure -> normal -> failure -> normal, exactly the
    Phase 1 Scenario E sequence the bug report itself specifies. The
    system must remain usable after every single cycle, not just the
    first."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        orig_create_plan = console.planner_module.planner.create_plan

        # Generous timeouts (this test alone drives 5 sequential turns,
        # cumulative load on top of whatever else this pytest process has
        # already accumulated - see main_runtime_demo.py's own Sprint 5
        # comment on daemon-thread/GIL contention across a long-running
        # suite). This is timing headroom for the SANDBOX, not a
        # tolerance for the fix itself - test_02's own tight, single-turn
        # reproduction is what actually pins the behavior down.
        #
        # Sprint 55 stability-gate finding: the "did it actually reach
        # THINKING" check uses `_reached_state_since()` (session
        # transition HISTORY, timestamped synchronously inside
        # `transition_to()`'s own lock), not a live `_wait_until(lambda:
        # _state(console) == "thinking", ...)` poll - a zero-delay mocked
        # LLM+TTS round trip can complete the ENTIRE turn between two
        # poll samples in an already-warmed-up process, which previously
        # made this assertion racy (see `_reached_state_since()`'s own
        # docstring for the full reproduction). The "did it settle
        # afterwards, not get stuck" check is UNCHANGED - a live poll is
        # exactly right there, since a settled state is unambiguous no
        # matter how it was reached.
        def _one_normal_turn(n):
            console.planner_module.planner.create_plan = orig_create_plan
            turn_started = time.time()
            _speak(console, f"halo yang ke-{n}")
            assert _wait_until(
                lambda: _reached_state_since(console, "thinking", turn_started)
                or _state(console) == "thinking",
                12.0,
            ), f"normal turn {n} never reached THINKING (state={_state(console)!r})"
            assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 8.0), \
                f"normal turn {n} never fully settled (state={_state(console)!r})"

        def _one_failing_turn(n):
            console.planner_module.planner.create_plan = lambda *a, **kw: (_ for _ in ()).throw(
                ConnectionAbortedError(f"boom {n}")
            )
            turn_started = time.time()
            _speak(console, f"gagal yang ke-{n}")
            assert _wait_until(
                lambda: _reached_state_since(console, "thinking", turn_started)
                or _state(console) == "thinking",
                12.0,
            ), f"failing turn {n} never reached THINKING (state={_state(console)!r})"
            assert _wait_until(lambda: _state(console) != "thinking", 6.0), \
                f"failing turn {n} left the session stuck (state={_state(console)!r})"

        _one_normal_turn(1)
        _one_failing_turn(1)
        _one_normal_turn(2)
        _one_failing_turn(2)
        _one_normal_turn(3)

        result = dash_controls.send_chat_message(console.runtime, _dashboard_modules(console), "masih bisa kan?")
        assert result.get("ok") is True, f"dashboard chat still blocked after the full recovery cycle: {result}"
    finally:
        console.planner_module.planner.create_plan = orig_create_plan
        console.stop()


def test_06_send_chat_message_accepts_a_new_command_immediately_after_recovery():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        orig_create_plan = console.planner_module.planner.create_plan
        console.planner_module.planner.create_plan = lambda *a, **kw: (_ for _ in ()).throw(OSError(10038, "not a socket"))
        _speak(console, "trigger a failure")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        # Busy-guard must actively reject WHILE stuck (proves the guard
        # itself works, not just that it eventually stops mattering).
        blocked = dash_controls.send_chat_message(console.runtime, _dashboard_modules(console), "coba lagi")
        assert blocked.get("ok") is False and "busy" in blocked.get("message", "")
        assert _wait_until(lambda: _state(console) != "thinking", 5.0)
        console.planner_module.planner.create_plan = orig_create_plan
        result = dash_controls.send_chat_message(console.runtime, _dashboard_modules(console), "coba lagi")
        assert result.get("ok") is True, f"still blocked after recovery: {result}"
    finally:
        console.planner_module.planner.create_plan = orig_create_plan
        console.stop()


# ─────────────────────────────────────────────
#  7 / 8 - unexpected exceptions stay visible; expected disconnects don't spam
# ─────────────────────────────────────────────

def test_07_unexpected_dashboard_oserror_is_still_logged():
    assert _is_expected_client_disconnect(ConnectionAbortedError("x")) is True
    assert _is_expected_client_disconnect(BrokenPipeError("x")) is True
    not_a_socket = OSError("not a socket")
    not_a_socket.errno = 10038
    assert _is_expected_client_disconnect(not_a_socket) is True
    real_bug = OSError("disk full")
    real_bug.errno = 28
    assert _is_expected_client_disconnect(real_bug) is False, \
        "a genuine OSError must never be silently classified as an expected disconnect"
    assert _is_expected_client_disconnect(ValueError("not even an OSError")) is False


def test_08_e2e_expected_disconnect_does_not_log_an_error_line(tmp_log_dir):
    """`_dispatch_get`'s new `except OSError` branch must stay silent
    (no `log(...)` call) for the WinError-10038 shape, exactly like the
    pre-existing `except ConnectionError` branch right above it - proven
    by driving a real request through `_dispatch_get` with a handler
    stub whose `wfile.write` raises that specific OSError."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    from luno.bootstrap.launcher_config import LauncherConfig
    dashboard = DashboardServer(
        console.runtime, console.adapter_manager, _dashboard_modules(console), LauncherConfig(),
        host="127.0.0.1", port=0, observability_log_dir=tmp_log_dir,
    )
    dashboard.start()
    logged: List[str] = []
    from luno.dashboard import server as server_mod
    orig_log = server_mod.log

    def _capture_log(message, component="core"):
        logged.append(message)
        orig_log(message, component)

    server_mod.log = _capture_log
    try:
        class _BrokenWfile:
            def write(self, data):
                ex = OSError("not a socket")
                ex.errno = 10038
                raise ex

            def flush(self):
                pass

        class _FakeHandler:
            path = "/api/ping"
            wfile = _BrokenWfile()

            def send_response(self, *a, **kw):
                pass

            def send_header(self, *a, **kw):
                pass

            def end_headers(self):
                pass

        dashboard._dispatch_get(_FakeHandler())
        assert not any("raised" in m for m in logged), \
            f"an expected WinError-10038 disconnect was logged as an error: {logged}"
    finally:
        server_mod.log = orig_log
        dashboard.stop()
        console.stop()


# ─────────────────────────────────────────────
#  9 - shutdown does not corrupt turn state
# ─────────────────────────────────────────────

def test_09_dashboard_stop_mid_turn_does_not_raise_or_corrupt_session_state():
    """`dashboard.stop()` (called first, per `luno/bootstrap/shutdown.py`'s
    own documented ordering) while a real turn is THINKING must complete
    without raising and without leaving `session_manager`'s state
    machine in an inconsistent place - proves Phase 5's finding
    (shutdown.py's own try/except already handles this) rather than just
    asserting it from reading the code."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    from luno.bootstrap.launcher_config import LauncherConfig
    _obs_dir = tempfile.mkdtemp(prefix="luno_turn_state_recovery_shutdown_")
    dashboard = DashboardServer(
        console.runtime, console.adapter_manager, _dashboard_modules(console), LauncherConfig(),
        host="127.0.0.1", port=0, observability_log_dir=_obs_dir,
    )
    dashboard.start()
    try:
        _wake(console)
        _speak(console, "turn in flight during shutdown")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        dashboard.stop()  # must not raise
        assert _state(console) in ("thinking", "waiting_user", "idle", "listening", "speaking")
    finally:
        console.stop()
        shutil.rmtree(_obs_dir, ignore_errors=True)


# ─────────────────────────────────────────────
#  10 - observability records the terminal outcome
# ─────────────────────────────────────────────

def test_10_e2e_llm_error_event_records_unhandled_exception_source():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    captured: List[Event] = []
    sub = console.event_bus.subscribe("llm_error", captured.append)
    try:
        _wake(console)
        console.planner_module.planner.create_plan = lambda *a, **kw: (_ for _ in ()).throw(
            ConnectionAbortedError("[WinError 10053] test")
        )
        _speak(console, "should publish llm_error")
        assert _wait_until(lambda: len(captured) >= 1, 5.0), "no llm_error event observed"
        data = captured[0].to_dict()["data"]
        assert data["source"] == "planner_bridge_unhandled_exception"
        assert data["error_type"] == "ConnectionAbortedError"
        assert "10053" in data["error"]
        assert data["retryable"] is False
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()


# ─────────────────────────────────────────────
#  11 - no duplicate terminal transition
# ─────────────────────────────────────────────

def test_11_handle_llm_failure_is_idempotent_against_a_redundant_llm_error():
    """`SessionManagerModule._handle_llm_failure()` already guards on
    `if self.session.state != THINKING: return` - this is what makes it
    safe for the new wrapper to publish `llm_error` even in a
    hypothetical future where a second one arrives after the session
    already recovered. Exercised directly here (not just read from
    source) by publishing `llm_error` twice in a row for the same
    already-recovered session and confirming the second one is a
    silent no-op, not a crash or a bad transition."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        assert _state(console) != "thinking"
        before = _state(console)
        console.event_bus.publish(Event(type="llm_error", data={"request_id": "r1", "error": "x", "error_type": "X", "retryable": False}))
        assert _wait_until(lambda: True, 0.3)  # let the bus deliver
        assert _state(console) == before, "a redundant llm_error while not THINKING must be a no-op"
        console.event_bus.publish(Event(type="llm_error", data={"request_id": "r1", "error": "x", "error_type": "X", "retryable": False}))
        time.sleep(0.3)
        assert _state(console) == before
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  12 - concurrent/rapid requests do not corrupt state
# ─────────────────────────────────────────────

def test_12_e2e_rapid_sequential_turns_do_not_corrupt_state():
    """`_handle_speech_recognized()`'s own busy-state gate already
    refuses to forward a new utterance while THINKING/SPEAKING (see
    `luno/wake_session/manager.py`) - this test proves that gate PLUS
    this fix's own recovery compose correctly under a rapid-fire
    sequence, ending in a clean, non-busy, usable state."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        for i in range(5):
            _speak(console, f"pesan cepat {i}")
            time.sleep(0.01)  # fire without waiting for completion - most will be dropped by the busy gate, that's expected
        assert _wait_until(lambda: _state(console) != "thinking", 8.0), \
            f"rapid-fire turns left the session stuck (state={_state(console)!r})"
        # THINKING clearing to SPEAKING (mocked playback, brief) is
        # correct, expected, self-clearing busy-ness - not the "stuck
        # forever" bug this fix addresses. Wait for it to fully settle
        # before asserting the Dashboard accepts a new command, same as
        # test_01/test_05 above.
        assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 5.0), \
            f"rapid-fire turns never fully settled (state={_state(console)!r})"
        result = dash_controls.send_chat_message(console.runtime, _dashboard_modules(console), "masih waras?")
        assert result.get("ok") is True, f"state corrupted after rapid-fire turns: {result}"
    finally:
        console.stop()
