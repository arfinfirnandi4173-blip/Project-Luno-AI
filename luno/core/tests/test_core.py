"""
test_core.py
============

Comprehensive, standalone test suite for the Core Integration Layer -
no camera, microphone, Home Assistant, OpenRouter, or Unity required
(every fixture is a plain fake `Module`/callable). Run directly:

    python3 luno/core/tests/test_core.py

or as a module from the project root:

    python3 -m luno.core.tests.test_core
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from typing import List, Tuple

# -- sys.path bootstrap (works both via direct execution and `-m`) --------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.core import (  # noqa: E402
    CoreConfig, Coordinator, Dispatcher, EventBus, HealthMonitor,
    HeartbeatMonitor, LifecycleManager, Module, ModuleManager, Runtime,
)
from luno.core.context_builder import ContextBuilder  # noqa: E402
from luno.core.events import Event, Heartbeat, SpeechRecognized, SystemError  # noqa: E402
from luno.core.exceptions import DependencyCycleError, ModuleStartError  # noqa: E402
from luno.core.scheduler import Scheduler  # noqa: E402

Result = Tuple[bool, str]


def _header(name: str) -> None:
    print(f"\n=== {name} ===")


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class RecordingModule(Module):
    def __init__(self, name, deps=None, fail_start=False, on_start=None, on_stop=None):
        self.name = name
        self.dependencies = deps or []
        self.fail_start = fail_start
        self.on_start = on_start
        self.on_stop = on_stop
        self.events: List[Event] = []
        self.started = False

    def start(self):
        if self.fail_start:
            raise RuntimeError(f"{self.name} refuses to start")
        self.started = True
        if self.on_start:
            self.on_start(self.name)

    def stop(self):
        self.started = False
        if self.on_stop:
            self.on_stop(self.name)

    def on_event(self, event):
        self.events.append(event)


# ============================================================================
# Scenarios
# ============================================================================

def test_startup_dependency_order() -> Result:
    order_seen: List[str] = []
    mm = ModuleManager()
    a = RecordingModule("vision", on_start=order_seen.append)
    b = RecordingModule("vision_memory", deps=["vision"], on_start=order_seen.append)
    c = RecordingModule("behavior_tree", deps=["vision_memory"], on_start=order_seen.append)
    mm.register(a)
    mm.register(b)
    mm.register(c)

    bus = EventBus(); bus.start()
    lc = LifecycleManager(mm, event_bus=bus)
    started = lc.startup()
    bus.stop(wait=True)

    ok = (
        started == ["vision", "vision_memory", "behavior_tree"]
        and order_seen == ["vision", "vision_memory", "behavior_tree"]
    )
    return ok, f"started={started} order_seen={order_seen}"


def test_shutdown_reverse_order() -> Result:
    order_seen: List[str] = []
    mm = ModuleManager()
    a = RecordingModule("a")
    b = RecordingModule("b", deps=["a"])
    c = RecordingModule("c", deps=["b"])
    mm.register(a); mm.register(b); mm.register(c)
    a.on_stop = order_seen.append
    b.on_stop = order_seen.append
    c.on_stop = order_seen.append

    lc = LifecycleManager(mm)
    lc.startup()
    lc.shutdown()

    return order_seen == ["c", "b", "a"], f"stop order={order_seen}"


def test_start_out_of_order_blocked_and_cycle_detected() -> Result:
    mm = ModuleManager()
    a = RecordingModule("a")
    b = RecordingModule("b", deps=["a"])
    mm.register(a); mm.register(b)
    blocked = False
    try:
        mm.start("b")
    except ModuleStartError:
        blocked = True

    mm2 = ModuleManager()
    x = RecordingModule("x", deps=["y"])
    y = RecordingModule("y", deps=["x"])
    mm2.register(x); mm2.register(y)
    cycle_detected = False
    try:
        mm2.dependency_order()
    except DependencyCycleError:
        cycle_detected = True

    ok = blocked and cycle_detected
    return ok, f"out_of_order_blocked={blocked} cycle_detected={cycle_detected}"


def test_health_report_reflects_failed_module() -> Result:
    mm = ModuleManager()
    bus = EventBus(); bus.start()
    health = HealthMonitor(mm, bus)
    lc = LifecycleManager(mm, event_bus=bus, health_monitor=health)

    good = RecordingModule("good")
    bad = RecordingModule("bad", fail_start=True)
    mm.register(good); mm.register(bad)
    lc.startup()

    report = health.report()
    errors = health.last_errors()
    bus.stop(wait=True)

    ok = (
        report.healthy is False
        and report.modules["good"].healthy is True
        and report.modules["bad"].healthy is False
        and any("bad" in e for e in errors)
    )
    return ok, f"healthy={report.healthy} issues={report.issues} errors={errors}"


def test_fault_recovery_restart_failed() -> Result:
    attempts = {"n": 0}

    class FlakyModule(Module):
        name = "flaky"
        def start(self):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("not yet")
        def stop(self):
            pass

    mm = ModuleManager()
    flaky = FlakyModule()
    mm.register(flaky)
    lc = LifecycleManager(mm)

    started = lc.startup()
    first_state = mm.all_modules()["flaky"].state.value
    restarted = lc.restart_failed()
    second_state = mm.all_modules()["flaky"].state.value

    ok = started == [] and first_state == "failed" and restarted == ["flaky"] and second_state == "running"
    return ok, f"first_state={first_state} restarted={restarted} second_state={second_state} attempts={attempts['n']}"


def test_event_routing_fanout_via_coordinator() -> Result:
    mm = ModuleManager()
    bus = EventBus(); bus.start()
    coord = Coordinator(bus, mm)

    bt = RecordingModule("behavior_tree")
    ctx = RecordingModule("context_feed")
    mm.register(bt); mm.register(ctx)
    mm.start("behavior_tree"); mm.start("context_feed")

    coord.add_route("speech_recognized", "behavior_tree")
    coord.add_route("speech_recognized", "context_feed")
    coord.add_route("tool_finished", "context_feed")

    bus.publish(SpeechRecognized(data={"text": "turn on the light"}))
    bus.publish(Event(type="tool_finished", data={"tool": "home_assistant"}))
    ok = _wait_until(lambda: len(bt.events) == 1 and len(ctx.events) == 2)

    coord.teardown()
    bus.stop(wait=True)
    return ok, f"bt.events={[e.type for e in bt.events]} ctx.events={[e.type for e in ctx.events]}"


def test_subscriber_priority_ordering() -> Result:
    bus = EventBus(); bus.start()
    order: List[str] = []
    bus.subscribe("ping", lambda e: order.append("low"), priority=0)
    bus.subscribe("ping", lambda e: order.append("high"), priority=100)
    bus.subscribe("ping", lambda e: order.append("mid"), priority=50)
    bus.publish(Event(type="ping"))
    ok = _wait_until(lambda: len(order) == 3)
    bus.stop(wait=True)
    return ok and order == ["high", "mid", "low"], f"order={order}"


def test_wildcard_and_once_subscriptions() -> Result:
    bus = EventBus(); bus.start()
    wildcard_hits: List[str] = []
    prefix_hits: List[str] = []
    once_hits: List[str] = []

    bus.subscribe("*", lambda e: wildcard_hits.append(e.type))
    bus.subscribe("tool_*", lambda e: prefix_hits.append(e.type))
    bus.subscribe("greeting", lambda e: once_hits.append(e.type), once=True)

    bus.publish(Event(type="tool_started"))
    bus.publish(Event(type="tool_finished"))
    bus.publish(Event(type="vision_updated"))
    bus.publish(Event(type="greeting"))
    bus.publish(Event(type="greeting"))

    ok = _wait_until(lambda: len(wildcard_hits) == 5 and len(once_hits) == 1)
    sub_count_after = bus.subscriber_count()
    bus.stop(wait=True)

    ok = ok and prefix_hits == ["tool_started", "tool_finished"] and sub_count_after == 2
    return ok, f"wildcard={wildcard_hits} prefix={prefix_hits} once={once_hits} subs_left={sub_count_after}"


def test_subscriber_self_healing() -> Result:
    """Reliability fix: a subscriber that fails 5x in a row used to be
    unsubscribed permanently (silent, unrecoverable). It's now marked
    'degraded' and throttled with backoff instead - still subscribed,
    and it recovers automatically once its handler stops raising."""
    bus = EventBus(); bus.start()
    call_count = {"n": 0}
    should_fail = {"v": True}

    def flaky(e):
        call_count["n"] += 1
        if should_fail["v"]:
            raise ValueError("transient")

    sub_id = bus.subscribe("boom", flaky)
    for _ in range(6):
        bus.publish(Event(type="boom"))

    became_degraded = _wait_until(lambda: bus.degraded_subscribers() != [])
    still_subscribed = bus.subscriber_count() == 1  # NOT auto-removed
    calls_after_degrade = call_count["n"]

    # Further publishes while degraded should be throttled (skipped),
    # not keep calling the still-failing handler on every event.
    bus.publish(Event(type="boom"))
    bus.publish(Event(type="boom"))
    throttled = call_count["n"] == calls_after_degrade

    # Once the underlying cause is gone, the next delivery after the
    # backoff window should succeed and clear the degraded state again.
    should_fail["v"] = False
    time.sleep(1.1)  # initial backoff window (~1s)
    bus.publish(Event(type="boom"))
    recovered = _wait_until(lambda: bus.degraded_subscribers() == [])

    can_still_unsubscribe = bus.unsubscribe(sub_id)  # explicit unsubscribe still works
    bus.stop(wait=True)
    ok = became_degraded and still_subscribed and throttled and recovered and can_still_unsubscribe
    return ok, (
        f"became_degraded={became_degraded} still_subscribed={still_subscribed} "
        f"throttled={throttled} recovered={recovered} can_still_unsubscribe={can_still_unsubscribe}"
    )


def test_async_dispatch_does_not_block_sync_delivery() -> Result:
    dispatcher = Dispatcher(max_workers=4)
    dispatcher.start()
    bus = EventBus(dispatcher=dispatcher)
    bus.start()

    slow_started = threading.Event()
    release_slow = threading.Event()
    sync_order: List[str] = []

    def slow_async_handler(e):
        slow_started.set()
        release_slow.wait(timeout=2.0)
        sync_order.append("slow_finished")

    def fast_sync_handler(e):
        sync_order.append("fast")

    bus.subscribe("slow_evt", slow_async_handler, async_mode=True)
    bus.publish(Event(type="slow_evt"))
    slow_started.wait(timeout=2.0)  # the slow async handler is now blocked mid-flight

    bus.subscribe("fast_evt", fast_sync_handler)
    bus.publish(Event(type="fast_evt"))
    fast_delivered_promptly = _wait_until(lambda: "fast" in sync_order, timeout_s=1.0)

    release_slow.set()
    _wait_until(lambda: "slow_finished" in sync_order, timeout_s=2.0)

    dispatcher.stop(wait=False)
    bus.stop(wait=True)
    ok = fast_delivered_promptly and sync_order[0] == "fast"
    return ok, f"sync_order={sync_order} fast_delivered_promptly={fast_delivered_promptly}"


def test_scheduler_periodic_once_and_predicate_jobs() -> Result:
    dispatcher = Dispatcher(max_workers=4)
    dispatcher.start()
    sched = Scheduler(dispatcher, tick_interval_s=0.05)
    sched.start()

    periodic_count = {"n": 0}
    once_fired = []
    predicate_fired = []

    sched.schedule_periodic("tick", lambda: periodic_count.__setitem__("n", periodic_count["n"] + 1), interval_s=0.12)
    sched.schedule_once("once", lambda: once_fired.append(1), delay_s=0.05)
    # `fn` (no args) records the fire; `predicate` (takes `now`) decides
    # whether it's due - these are two separate callables per the
    # Scheduler contract, not the same lambda.
    sched.schedule_predicate("always", lambda: predicate_fired.append(1), predicate=lambda now: True)

    time.sleep(0.8)
    sched.stop()
    dispatcher.stop(wait=False)

    ok = periodic_count["n"] >= 4 and once_fired == [1] and len(predicate_fired) == 1
    return ok, f"periodic={periodic_count['n']} once={once_fired} predicate_fires={len(predicate_fired)}"


def test_heartbeat_reports_and_publishes() -> Result:
    mm = ModuleManager()
    mm.register(RecordingModule("m1"))
    mm.start("m1")
    bus = EventBus(); bus.start()
    received = []
    bus.subscribe("heartbeat", lambda e: received.append(e))

    hb = HeartbeatMonitor(bus, mm, interval_s=100.0, gauge_provider=lambda: {"running_tools": 3, "active_plans": 2})
    stats = hb.beat_now()
    ok = _wait_until(lambda: len(received) == 1)
    bus.stop(wait=True)

    ok = (
        ok
        and stats.active_modules == 1
        and stats.running_tools == 3
        and stats.active_plans == 2
        and received[0].data["running_tools"] == 3
    )
    return ok, f"stats={stats} event_data={received[0].data if received else None}"


def test_stress_1000_events_per_second() -> Result:
    bus = EventBus(max_queue=5000)
    bus.start()
    received = {"n": 0}
    lock = threading.Lock()

    def handler(e):
        with lock:
            received["n"] += 1

    bus.subscribe("stress", handler)

    N = 1200
    start = time.time()
    for _ in range(N):
        bus.publish(Event(type="stress"))
    publish_elapsed = time.time() - start

    ok = _wait_until(lambda: received["n"] == N, timeout_s=5.0)
    total_elapsed = time.time() - start
    stats = bus.stats()
    bus.stop(wait=True)

    rate = N / total_elapsed if total_elapsed > 0 else float("inf")
    ok = ok and stats["dropped"] == 0 and rate >= 1000
    return ok, f"published {N} events, publish_took={publish_elapsed:.3f}s total_took={total_elapsed:.3f}s rate={rate:.0f}/s dropped={stats['dropped']}"


def test_concurrent_publishers() -> Result:
    bus = EventBus(max_queue=20000)
    bus.start()
    received = {"n": 0}
    lock = threading.Lock()
    bus.subscribe("concurrent", lambda e: (lock.acquire(), received.__setitem__("n", received["n"] + 1), lock.release()))

    NUM_THREADS = 10
    PER_THREAD = 200

    def publisher():
        for _ in range(PER_THREAD):
            bus.publish(Event(type="concurrent"))

    threads = [threading.Thread(target=publisher) for _ in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = NUM_THREADS * PER_THREAD
    ok = _wait_until(lambda: received["n"] == expected, timeout_s=5.0)
    stats = bus.stats()
    bus.stop(wait=True)
    return ok and stats["dropped"] == 0, f"expected={expected} received={received['n']} dropped={stats['dropped']}"


def test_concurrent_subscribers() -> Result:
    bus = EventBus()
    bus.start()
    NUM_SUBS = 50
    counters = [0] * NUM_SUBS
    lock = threading.Lock()

    def make_handler(i):
        def h(e):
            with lock:
                counters[i] += 1
        return h

    sub_ids = [bus.subscribe("broadcast", make_handler(i)) for i in range(NUM_SUBS)]

    def unsub_some():
        for sid in sub_ids[:10]:
            bus.unsubscribe(sid)

    unsub_thread = threading.Thread(target=unsub_some)
    unsub_thread.start()
    for _ in range(20):
        bus.publish(Event(type="broadcast"))
    unsub_thread.join()

    time.sleep(0.3)
    remaining = bus.subscriber_count()
    bus.stop(wait=True)
    # no crash + roughly the right ballpark of delivery is success; exact
    # counts for the unsubscribed-mid-flight ones are inherently racy
    ok = remaining == NUM_SUBS - 10 and all(c > 0 for i, c in enumerate(counters) if i >= 10)
    return ok, f"remaining_subs={remaining} min_count_after_10={min(counters[10:])} max_count={max(counters)}"


def test_graceful_shutdown_idempotent() -> Result:
    mm_events: List[str] = []
    a = RecordingModule("a", on_start=lambda n: mm_events.append(f"start:{n}"), on_stop=lambda n: mm_events.append(f"stop:{n}"))

    rt = Runtime(CoreConfig(heartbeat_interval_s=100, scheduler_tick_s=1.0))
    rt.register_module(a)
    rt.start()
    rt.stop()
    rt.stop()  # idempotent - must not raise or double-stop the module
    status_after = rt.status()

    ok = mm_events == ["start:a", "stop:a"] and status_after["running"] is False
    return ok, f"mm_events={mm_events} status={status_after}"


def test_config_from_dict_env_and_reload() -> Result:
    cfg = CoreConfig.from_dict({"heartbeat_interval_s": 5.0, "unknown_field": "kept_in_extra"})
    ok1 = cfg.heartbeat_interval_s == 5.0 and cfg.extra.get("unknown_field") == "kept_in_extra"

    os.environ["LUNO_CORE_HEARTBEAT_INTERVAL_S"] = "42.0"
    env_cfg = CoreConfig.from_env()
    ok2 = env_cfg.heartbeat_interval_s == 42.0
    del os.environ["LUNO_CORE_HEARTBEAT_INTERVAL_S"]

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "core_config.json")
        with open(path, "w") as f:
            json.dump({"heartbeat_interval_s": 7.0, "dispatcher_max_workers": 3}, f)
        file_cfg = CoreConfig.from_json(path)
        ok3 = file_cfg.heartbeat_interval_s == 7.0 and file_cfg.dispatcher_max_workers == 3

        with open(path, "w") as f:
            json.dump({"heartbeat_interval_s": 99.0, "dispatcher_max_workers": 3}, f)
        reloaded = file_cfg.reload()
        ok4 = reloaded.heartbeat_interval_s == 99.0

    ok = ok1 and ok2 and ok3 and ok4
    return ok, f"dict_ok={ok1} env_ok={ok2} json_ok={ok3} reload_ok={ok4}"


def test_context_builder_defaults_and_error_isolation() -> Result:
    cb = ContextBuilder()
    empty_ctx = cb.build()
    ok1 = (
        empty_ctx.conversation_memory == [] and empty_ctx.vision_memory == {}
        and empty_ctx.current_emotion == "neutral" and empty_ctx.current_activity == "unknown"
    )

    cb.register_provider("current_emotion", lambda: "focused")
    cb.register_provider("planner_state", lambda: {"active_plan": "make_coffee"})

    def broken_provider():
        raise RuntimeError("vision offline")

    cb.register_provider("vision_memory", broken_provider)
    ctx = cb.build()
    ok2 = ctx.current_emotion == "focused" and ctx.planner_state == {"active_plan": "make_coffee"} and ctx.vision_memory == {}

    d = ctx.to_dict()
    ok3 = "current_time" in d and isinstance(d["current_time"], str)

    ok = ok1 and ok2 and ok3
    return ok, f"defaults_ok={ok1} overrides_and_error_isolation_ok={ok2} to_dict_ok={ok3}"


def test_full_runtime_pipeline_example() -> Result:
    """End-to-end smoke test mirroring the spec's own pipeline sketch:
    SpeechRecognized -> Behavior Tree -> Planner -> Tool Manager ->
    ToolFinished -> Context Builder feed, all through Runtime's Event
    Bus and Coordinator, using fake stand-ins for the AI-logic packages
    (which this layer never imports)."""
    pipeline_log: List[str] = []

    class FakeBehaviorTree(Module):
        name = "behavior_tree"
        def start(self): pass
        def stop(self): pass
        def on_event(self, event):
            if event.type == "speech_recognized":
                pipeline_log.append("behavior_tree_saw_speech")
                event_bus_ref.publish(Event(type="tool_requested", data={"tool": "home_assistant"}, source="behavior_tree"))

    class FakeToolManager(Module):
        name = "tool_manager"
        def start(self): pass
        def stop(self): pass
        def on_event(self, event):
            if event.type == "tool_requested":
                pipeline_log.append("tool_manager_executed")
                event_bus_ref.publish(Event(type="tool_finished", data={"result": "ok"}, source="tool_manager"))

    class ContextFeed(Module):
        name = "context_feed"
        def start(self): pass
        def stop(self): pass
        def on_event(self, event):
            if event.type == "tool_finished":
                pipeline_log.append("context_builder_notified")

    rt = Runtime(CoreConfig(heartbeat_interval_s=100, scheduler_tick_s=1.0))
    event_bus_ref = rt.event_bus
    rt.register_module(FakeBehaviorTree())
    rt.register_module(FakeToolManager())
    rt.register_module(ContextFeed())
    rt.add_route("speech_recognized", "behavior_tree")
    rt.add_route("tool_requested", "tool_manager")
    rt.add_route("tool_finished", "context_feed")
    rt.start()

    rt.event_bus.publish(SpeechRecognized(data={"text": "turn on the bedroom light"}))
    ok = _wait_until(lambda: pipeline_log == ["behavior_tree_saw_speech", "tool_manager_executed", "context_builder_notified"], timeout_s=2.0)
    rt.stop()
    return ok, f"pipeline_log={pipeline_log}"


# ============================================================================
# Runner
# ============================================================================

SCENARIOS = [
    test_startup_dependency_order,
    test_shutdown_reverse_order,
    test_start_out_of_order_blocked_and_cycle_detected,
    test_health_report_reflects_failed_module,
    test_fault_recovery_restart_failed,
    test_event_routing_fanout_via_coordinator,
    test_subscriber_priority_ordering,
    test_wildcard_and_once_subscriptions,
    test_subscriber_self_healing,
    test_async_dispatch_does_not_block_sync_delivery,
    test_scheduler_periodic_once_and_predicate_jobs,
    test_heartbeat_reports_and_publishes,
    test_stress_1000_events_per_second,
    test_concurrent_publishers,
    test_concurrent_subscribers,
    test_graceful_shutdown_idempotent,
    test_config_from_dict_env_and_reload,
    test_context_builder_defaults_and_error_isolation,
    test_full_runtime_pipeline_example,
]


def main() -> int:
    _header("Luno Core Integration Layer - Test Suite")
    results = []
    for fn in SCENARIOS:
        name = fn.__name__
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"raised {type(ex).__name__}: {ex}"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} - {detail}")
        results.append((name, ok))

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{total} scenarios passed.")
    if passed == total:
        print("Semua skenario lolos.")
        return 0
    print("Beberapa skenario gagal:")
    for name, ok in results:
        if not ok:
            print(f"  - {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
