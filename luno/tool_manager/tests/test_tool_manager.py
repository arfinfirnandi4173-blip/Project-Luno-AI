"""
Test manual buat luno/tool_manager/ — jalanin dari root project:

    python luno/tool_manager/tests/test_tool_manager.py

atau:

    python -m luno.tool_manager.tests.test_tool_manager

SEMUA sintetis (nggak butuh Home Assistant/Windows/Spotify/browser/Unity
beneran) — pakai builtin mock handlers + `DummyHandler` yang emang
didesain buat ditest (lihat builtin/dummy.py).
"""

import os
import sys
import threading
import time

# Bootstrap sys.path so this file works whether run directly
# (`python luno/tool_manager/tests/test_tool_manager.py`) or as a module
# (`python -m luno.tool_manager.tests.test_tool_manager`) - direct
# execution only puts THIS file's own directory on sys.path by default,
# which is 3 levels too deep to `import luno`.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.tool_manager import ToolCall, ToolManager, ToolRegistry  # noqa: E402
from luno.tool_manager.builtin import (  # noqa: E402
    DummyHandler,
    MockHomeAssistantHandler,
    MockSpotifyHandler,
    MockUnityHandler,
    MockWindowsHandler,
    register_all,
)
from luno.tool_manager.handler import ToolHandler  # noqa: E402
from luno.tool_manager.result import ResultStatus  # noqa: E402

PASS = "✓"
FAIL = "✗"


def _header(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _new_manager():
    manager = ToolManager()
    register_all(manager.registry)
    return manager


# ---------------------------------------------------------------------------

def test_registry_register_unregister_get_list():
    registry = ToolRegistry()
    handler = DummyHandler()
    registry.register("dummy", handler)
    got = registry.get("dummy") is handler
    listed = registry.list_tools() == ["dummy"]
    has = registry.has("dummy") and not registry.has("nope")
    removed = registry.unregister("dummy")
    gone = registry.get("dummy") is None and registry.list_tools() == []
    ok = got and listed and has and removed and gone
    return ok, f"got={got} listed={listed} has={has} removed={removed} gone={gone}"


def test_unknown_tool():
    manager = _new_manager()
    result = manager.execute({"tool": "nonexistent_tool", "action": "x"})
    manager.shutdown()
    ok = not result.success and result.error_type == "unknown_tool" and result.status == ResultStatus.FAILED
    return ok, f"result={result.to_dict()}"


def test_unknown_action():
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "teleport", "target": "x"})
    manager.shutdown()
    ok = not result.success and result.error_type == "unknown_action"
    return ok, f"result={result.to_dict()}"


def test_validation_failure():
    manager = _new_manager()
    # turn_on with NO target - MockHomeAssistantHandler.validate() requires one
    result = manager.execute({"tool": "home_assistant", "action": "turn_on"})
    manager.shutdown()
    ok = not result.success and result.error_type == "validation_error"
    return ok, f"result={result.to_dict()}"


def test_parallel_execution():
    manager = _new_manager()
    n = 5
    counter = [0]
    lock = threading.Lock()
    starts = []

    def track(execution_id, status, attempt):
        pass

    handles = []
    for i in range(n):
        handles.append(manager.execute_async({
            "tool": "dummy", "action": "simulate", "parameters": {"mode": "success", "delay_s": 0.2},
        }))
    start = time.time()
    results = [h.result(timeout=3.0) for h in handles]
    elapsed = time.time() - start
    manager.shutdown()

    all_ok = all(r.success for r in results)
    # if truly parallel, n tasks each taking 0.2s should finish in well
    # under n * 0.2s (which would indicate serial execution)
    ran_in_parallel = elapsed < (n * 0.2 * 0.6)
    ok = all_ok and ran_in_parallel
    return ok, f"all_ok={all_ok} elapsed={elapsed:.2f}s (serial would be ~{n*0.2:.2f}s) ran_in_parallel={ran_in_parallel}"


def test_timeout():
    manager = _new_manager()
    start = time.time()
    # hang_s is short (not the 999s default) on purpose: ToolManager can
    # only STOP WAITING on a timed-out call, not kill the underlying
    # thread (see manager.py's module docstring) - a short hang_s here
    # lets that abandoned background thread finish on its own shortly
    # after this test, instead of holding the whole test process open.
    result = manager.execute(
        {"tool": "dummy", "action": "simulate", "parameters": {"mode": "timeout", "hang_s": 2.0}},
        timeout_s=0.3,
    )
    elapsed = time.time() - start
    manager.shutdown()
    ok = (not result.success and result.status == ResultStatus.TIMEOUT and result.error_type == "timeout"
          and result.retryable and elapsed < 2.0)
    return ok, f"result={result.to_dict()} elapsed={elapsed:.2f}s"


def test_cancellation():
    manager = _new_manager()
    handle = manager.execute_async({
        "tool": "dummy", "action": "simulate", "parameters": {"mode": "success", "delay_s": 1.0},
    })
    time.sleep(0.1)
    cancelled = manager.cancel(handle.execution_id)
    result = handle.result(timeout=3.0)
    manager.shutdown()
    ok = cancelled and not result.success and result.status == ResultStatus.CANCELLED
    return ok, f"cancel_accepted={cancelled} result={result.to_dict()}"


def test_cancellation_before_dispatch_via_retry_backoff():
    """Cancel WHILE a retryable failure is waiting out its backoff delay -
    should stop almost immediately rather than waiting out the full delay."""
    from luno.tool_manager.models import RetryPolicy
    manager = _new_manager()
    handle = manager.execute_async(
        {"tool": "dummy", "action": "simulate", "parameters": {"mode": "failure", "retryable": True}},
        retry_policy=RetryPolicy(max_retries=5, backoff_s=5.0, backoff_multiplier=1.0),
    )
    time.sleep(0.15)  # let the first attempt fail and enter backoff
    start = time.time()
    manager.cancel(handle.execution_id)
    result = handle.result(timeout=3.0)
    cancel_latency = time.time() - start
    manager.shutdown()
    ok = result.status == ResultStatus.CANCELLED and cancel_latency < 1.0
    return ok, f"status={result.status.value} cancel_latency={cancel_latency:.2f}s (backoff was 5s)"


def test_retry_then_succeeds():
    from luno.tool_manager.models import RetryPolicy
    manager = _new_manager()
    counter = [0]
    result = manager.execute(
        {"tool": "dummy", "action": "simulate",
         "parameters": {"mode": "retry", "succeed_on_attempt": 3, "_attempt_counter": counter}},
        retry_policy=RetryPolicy(max_retries=3, backoff_s=0.05, backoff_multiplier=1.0),
    )
    manager.shutdown()
    ok = result.success and counter[0] == 3
    return ok, f"result={result.to_dict()} attempts={counter[0]}"


def test_retry_exhausted_stays_failed():
    from luno.tool_manager.models import RetryPolicy
    manager = _new_manager()
    counter = [0]
    result = manager.execute(
        {"tool": "dummy", "action": "simulate",
         "parameters": {"mode": "retry", "succeed_on_attempt": 10, "_attempt_counter": counter}},
        retry_policy=RetryPolicy(max_retries=2, backoff_s=0.05, backoff_multiplier=1.0),
    )
    manager.shutdown()
    # max_retries=2 -> 3 total attempts, never reaches succeed_on_attempt=10
    ok = not result.success and counter[0] == 3
    return ok, f"result={result.to_dict()} attempts={counter[0]}"


def test_dummy_delays():
    manager = _new_manager()
    start = time.time()
    result = manager.execute({"tool": "dummy", "action": "simulate", "parameters": {"mode": "success", "delay_s": 0.3}})
    elapsed = time.time() - start
    manager.shutdown()
    ok = result.success and 0.25 <= elapsed < 1.0
    return ok, f"elapsed={elapsed:.2f}s (expected ~0.3s)"


def test_multiple_handlers_same_manager():
    manager = _new_manager()
    ha = manager.execute({"tool": "home_assistant", "action": "turn_on", "target": "a"})
    win = manager.execute({"tool": "windows", "action": "launch_app", "target": "chrome"})
    browser = manager.execute({"tool": "browser", "action": "open"})
    vision = manager.execute({"tool": "vision", "action": "look_camera"})
    spotify = manager.execute({"tool": "spotify", "action": "play", "target": "lofi"})
    unity = manager.execute({"tool": "unity", "action": "wave"})
    manager.shutdown()
    results = [ha, win, browser, vision, spotify, unity]
    ok = all(r.success for r in results) and len({r.tool for r in results}) == 6
    return ok, f"tools_ok={[(r.tool, r.success) for r in results]}"


# -- RGB color/brightness fix (`MockHomeAssistantHandler` "set_color"/
# -- "set_brightness") - reported: "di bagian HA kok ngga bisa set rgb
# -- strip warna sama brightnes?" - these two actions never existed on
# -- any handler before this fix; `_SUPPORTED_ACTIONS`/`validate()`/
# -- `execute()` in `luno/tool_manager/builtin/home_assistant.py` gained
# -- them, mirrored in `RealHomeAssistantHandler` (see the separate
# -- Reliability Sprint test file for that one).

def test_mock_ha_set_color_success():
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "set_color", "target": "rgb_strip", "parameters": {"color": "red"}})
    manager.shutdown()
    ok = result.success and result.data["color"] == "red" and result.data["target"] == "rgb_strip"
    return ok, f"result={result.to_dict()}"


def test_mock_ha_set_brightness_success():
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "set_brightness", "target": "rgb_strip", "parameters": {"level": 80}})
    manager.shutdown()
    ok = result.success and result.data["brightness"] == 80 and result.data["target"] == "rgb_strip"
    return ok, f"result={result.to_dict()}"


def test_mock_ha_set_color_missing_parameter_fails_validation():
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "set_color", "target": "rgb_strip"})
    manager.shutdown()
    ok = not result.success and result.error_type == "validation_error"
    return ok, f"result={result.to_dict()}"


def test_mock_ha_set_color_custom_rgb_success():
    """Custom RGB triplet ("kombinasi warnanya") - params.rgb is an
    equally valid alternative to params.color, not just the fixed
    10-name palette."""
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "set_color", "target": "rgb_strip", "parameters": {"rgb": [120, 50, 200]}})
    manager.shutdown()
    ok = result.success and result.data["rgb"] == [120, 50, 200] and "color" not in result.data
    return ok, f"result={result.to_dict()}"


def test_mock_ha_set_brightness_missing_parameter_fails_validation():
    manager = _new_manager()
    result = manager.execute({"tool": "home_assistant", "action": "set_brightness", "target": "rgb_strip"})
    manager.shutdown()
    ok = not result.success and result.error_type == "validation_error"
    return ok, f"result={result.to_dict()}"


def test_handler_crash_is_caught():
    class CrashingHandler(ToolHandler):
        name = "crasher"
        def supported_actions(self):
            return ["boom"]
        def execute(self, tool_call, context=None):
            raise RuntimeError("intentional crash")

    manager = ToolManager()
    manager.registry.register("crasher", CrashingHandler())
    result = manager.execute({"tool": "crasher", "action": "boom"})
    manager.shutdown()
    ok = not result.success and result.error_type == "handler_crash" and result.retryable
    return ok, f"result={result.to_dict()}"


def test_thread_safety_registry_and_execution():
    manager = _new_manager()
    errors = []
    results = []
    lock = threading.Lock()

    def worker(i):
        try:
            r = manager.execute({"tool": "dummy", "action": "simulate", "parameters": {"mode": "success", "value": i}})
            with lock:
                results.append(r)
        except Exception as ex:
            with lock:
                errors.append(str(ex))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    manager.shutdown()

    ok = not errors and len(results) == 40 and all(r.success for r in results)
    return ok, f"errors={errors[:3]} results_count={len(results)}"


def test_stress_many_executions():
    manager = _new_manager()
    n = 200
    start = time.time()
    handles = [
        manager.execute_async({"tool": "dummy", "action": "simulate", "parameters": {"mode": "success"}})
        for _ in range(n)
    ]
    results = [h.result(timeout=10.0) for h in handles]
    elapsed = time.time() - start
    manager.shutdown()
    ok = len(results) == n and all(r.success for r in results)
    return ok, f"n={n} elapsed={elapsed:.2f}s all_success={all(r.success for r in results)}"


def test_progress_callbacks():
    from luno.tool_manager.models import RetryPolicy
    manager = _new_manager()
    events = []
    lock = threading.Lock()

    def on_progress(execution_id, status, attempt):
        with lock:
            events.append((status, attempt))

    counter = [0]
    handle = manager.execute_async(
        {"tool": "dummy", "action": "simulate",
         "parameters": {"mode": "retry", "succeed_on_attempt": 2, "_attempt_counter": counter}},
        retry_policy=RetryPolicy(max_retries=2, backoff_s=0.05, backoff_multiplier=1.0),
        on_progress=on_progress,
    )
    result = handle.result(timeout=3.0)
    manager.shutdown()
    statuses_seen = {e[0] for e in events}
    ok = result.success and "running" in statuses_seen and "retrying" in statuses_seen
    return ok, f"events={events} result_success={result.success}"


def main():
    scenarios = [
        ("registry_register_unregister_get_list", test_registry_register_unregister_get_list),
        ("unknown_tool", test_unknown_tool),
        ("unknown_action", test_unknown_action),
        ("validation_failure", test_validation_failure),
        ("parallel_execution", test_parallel_execution),
        ("timeout", test_timeout),
        ("cancellation", test_cancellation),
        ("cancellation_before_dispatch_via_retry_backoff", test_cancellation_before_dispatch_via_retry_backoff),
        ("retry_then_succeeds", test_retry_then_succeeds),
        ("retry_exhausted_stays_failed", test_retry_exhausted_stays_failed),
        ("dummy_delays", test_dummy_delays),
        ("multiple_handlers_same_manager", test_multiple_handlers_same_manager),
        ("mock_ha_set_color_success", test_mock_ha_set_color_success),
        ("mock_ha_set_brightness_success", test_mock_ha_set_brightness_success),
        ("mock_ha_set_color_missing_parameter_fails_validation", test_mock_ha_set_color_missing_parameter_fails_validation),
        ("mock_ha_set_color_custom_rgb_success", test_mock_ha_set_color_custom_rgb_success),
        ("mock_ha_set_brightness_missing_parameter_fails_validation", test_mock_ha_set_brightness_missing_parameter_fails_validation),
        ("handler_crash_is_caught", test_handler_crash_is_caught),
        ("thread_safety_registry_and_execution", test_thread_safety_registry_and_execution),
        ("stress_many_executions", test_stress_many_executions),
        ("progress_callbacks", test_progress_callbacks),
    ]

    results = {}
    for name, fn in scenarios:
        _header(name)
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"EXCEPTION: {ex}"
            import traceback
            traceback.print_exc()
        print(f"{PASS if ok else FAIL} {detail}")
        results[name] = ok

    _header("Ringkasan")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL}  {name}")

    all_ok = all(results.values())
    print(f"\n{PASS if all_ok else FAIL} {'Semua skenario lolos.' if all_ok else 'Ada yang gagal - cek detail di atas.'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
