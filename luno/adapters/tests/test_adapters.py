"""
test_adapters.py
=================

Comprehensive, standalone test suite for the Adapter Layer - every
external system mocked, no network/hardware/API keys required. Run:

    python3 -m luno.adapters.tests.test_adapters
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.adapters.base import BaseAdapter  # noqa: E402
from luno.adapters.events import (  # noqa: E402
    AssistantResponse, AutomationTriggered, DeviceStateChanged, NeedLLMResponse,
    PersonAppeared, PersonDisappeared,
)
from luno.adapters.fish_audio import FishAudioAdapter, MockFishAudioClient  # noqa: E402
from luno.adapters.home_assistant import HomeAssistantAdapter, MockHomeAssistantClient, MockHomeAssistantSource  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.adapters.models import AdapterConfig, EventMapping  # noqa: E402
from luno.adapters.openrouter import MockOpenRouterClient, OpenRouterAdapter  # noqa: E402
from luno.adapters.unity import MockUnityClient, UnityAdapter  # noqa: E402
from luno.adapters.vision import MockVisionSource, VisionAdapter  # noqa: E402
from luno.adapters.whisper import MockWhisperSource, WhisperAdapter  # noqa: E402
from luno.core.events import BehaviorChanged, EmotionChanged, Event, SpeechRecognized  # noqa: E402

Result = Tuple[bool, str]


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class RecordingAdapter(BaseAdapter):
    """Minimal test double implementing BaseAdapter directly - used for
    manager-level scenarios that don't need a real external-system
    adapter (registration, ordering, health, fault recovery, ...)."""

    def __init__(self, name, deps=None, fail_events=False, fail_start=False):
        super().__init__()
        self.name = name
        self.dependencies = deps or []
        self.fail_events = fail_events
        self.fail_start = fail_start
        self.received: List[str] = []
        self.start_count = 0

    def _do_start(self):
        if self.fail_start:
            raise RuntimeError(f"{self.name} refuses to start")
        self.start_count += 1

    def handle_event(self, event):
        if self.fail_events:
            raise RuntimeError("simulated crash")
        self.received.append(event.type)


# ============================================================================
# Scenarios
# ============================================================================

def test_adapter_registration() -> Result:
    mgr = AdapterManager.standalone()
    a = RecordingAdapter("a")
    b = RecordingAdapter("b", deps=["a"])
    mgr.register(a)
    mgr.register(b, AdapterConfig(name="b", dependencies=["a"]))

    ok = (
        sorted(mgr.registry.list_adapters()) == ["a", "b"]
        and mgr.registry.get("a") is a
        and mgr.registry.get_config("b").dependencies == ["a"]
    )
    return ok, f"adapters={mgr.registry.list_adapters()} b_deps={mgr.registry.get_config('b').dependencies}"


def test_start_stop_all() -> Result:
    mgr = AdapterManager.standalone()
    a = RecordingAdapter("a")
    b = RecordingAdapter("b", deps=["a"])
    mgr.register(a)
    mgr.register(b, AdapterConfig(name="b", dependencies=["a"]))

    started = mgr.start_all()
    states_after_start = {n: r.state.value for n, r in mgr.module_manager.all_modules().items()}
    mgr.stop_all()
    states_after_stop = {n: r.state.value for n, r in mgr.module_manager.all_modules().items()}

    ok = (
        started == ["a", "b"]
        and states_after_start == {"a": "running", "b": "running"}
        and states_after_stop == {"a": "stopped", "b": "stopped"}
    )
    return ok, f"started={started} after_start={states_after_start} after_stop={states_after_stop}"


def test_restart() -> Result:
    mgr = AdapterManager.standalone()
    a = RecordingAdapter("a")
    mgr.register(a)
    mgr.start_all()
    before = a.start_count
    mgr.restart("a")
    after = a.start_count
    # still functional post-restart
    mgr.event_bus.start()
    mgr.coordinator.add_route("ping", "a")
    mgr.event_bus.publish(Event(type="ping"))
    ok_functional = _wait_until(lambda: "ping" in a.received)
    mgr.stop_all()

    ok = before == 1 and after == 2 and a._restart_count == 1 and ok_functional
    return ok, f"start_count before={before} after={after} restart_count={a._restart_count} functional_after={ok_functional}"


def test_health_all() -> Result:
    mgr = AdapterManager.standalone()
    good = RecordingAdapter("good")
    bad = RecordingAdapter("bad", fail_events=True)
    bad.MAX_CONSECUTIVE_FAILURES = 100  # keep it FAILED-by-events without self-restarting mid-check
    mgr.register(good)
    mgr.register(bad)
    mgr.start_all()
    mgr.event_bus.start()

    for _ in range(3):
        bad.on_event(Event(type="whatever"))
    _wait_until(lambda: bad._consecutive_failures == 3, timeout_s=2.0)

    health = mgr.health_all()
    ok = health["good"].healthy is True and health["bad"].healthy is False
    mgr.stop_all()
    return ok, f"good_healthy={health['good'].healthy} bad_healthy={health['bad'].healthy} bad_failures={bad._consecutive_failures}"


def test_disabled_adapters() -> Result:
    mgr = AdapterManager.standalone()
    a = RecordingAdapter("a")
    mgr.register(a, AdapterConfig(name="a", enabled=False))

    not_in_mm_before = "a" not in mgr.module_manager.all_modules()
    started = mgr.start_all()  # nothing enabled -> nothing to start
    not_started = started == []

    enabled = mgr.enable("a")
    time.sleep(0.05)
    running_after_enable = mgr.module_manager.all_modules().get("a")
    running_after_enable_ok = running_after_enable is not None and running_after_enable.state.value == "running"

    disabled = mgr.disable("a")
    gone_after_disable = "a" not in mgr.module_manager.all_modules()

    ok = not_in_mm_before and not_started and enabled and running_after_enable_ok and disabled and gone_after_disable
    return ok, (
        f"not_in_mm_before={not_in_mm_before} not_started={not_started} enabled={enabled} "
        f"running_after_enable={running_after_enable_ok} disabled={disabled} gone_after_disable={gone_after_disable}"
    )


def test_event_routing_full_pipeline() -> Result:
    """NeedLLMResponse -> OpenRouterAdapter -> AssistantResponse -> FishAudioAdapter -> SpeechPlaybackFinished,
    wired entirely through the default EventMapping, no direct adapter-to-adapter calls."""
    mgr = AdapterManager.standalone()
    orr = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="Sure thing!"))
    fa = FishAudioAdapter(client=MockFishAudioClient(playback_delay_s=0.02))
    mgr.register(orr)
    mgr.register(fa)
    mgr.start_all()

    finished = []
    mgr.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.data))

    mgr.event_bus.publish(NeedLLMResponse(data={
        "model": "anthropic/claude-3.5-sonnet",
        "messages": [{"role": "user", "content": "tell me a joke"}],
    }))
    ok = _wait_until(lambda: len(finished) == 1, timeout_s=2.0)
    mgr.stop_all()
    return ok, f"finished={finished}"


def test_whisper_event() -> Result:
    mgr = AdapterManager.standalone()
    src = MockWhisperSource()
    w = WhisperAdapter(source=src)
    mgr.register(w)
    mgr.start_all()

    recognized = []
    mgr.event_bus.subscribe("speech_recognized", lambda e: recognized.append(e.data))
    src.simulate_speech("what time is it", 0.95)
    ok = _wait_until(lambda: len(recognized) == 1)
    mgr.stop_all()
    return ok, f"recognized={recognized}"


def test_vision_event() -> Result:
    class FakeVM:
        def update(self, desc):
            return []

    mgr = AdapterManager.standalone()
    src = MockVisionSource()
    v = VisionAdapter(source=src, vision_memory_module=FakeVM())
    mgr.register(v)
    mgr.start_all()

    person_events = []
    mgr.event_bus.subscribe("person_appeared", lambda e: person_events.append(e.type))
    mgr.event_bus.subscribe("person_disappeared", lambda e: person_events.append(e.type))

    src.simulate_detections([{"label": "person", "confidence": 0.9}])
    src.simulate_detections([])
    ok = _wait_until(lambda: person_events == ["person_appeared", "person_disappeared"])
    mgr.stop_all()
    return ok, f"person_events={person_events}"


def test_openrouter_request_no_hardcoded_model() -> Result:
    mgr = AdapterManager.standalone()
    client = MockOpenRouterClient(canned_text="ok")
    orr = OpenRouterAdapter(client=client)  # no default_model configured at all
    mgr.register(orr)
    mgr.start_all()

    responses = []
    mgr.event_bus.subscribe("assistant_response", lambda e: responses.append(e.data))
    failures = []
    mgr.event_bus.subscribe("llm_error", lambda e: failures.append(e.data))

    mgr.event_bus.publish(NeedLLMResponse(data={"model": "openai/gpt-4o", "messages": []}))
    mgr.event_bus.publish(NeedLLMResponse(data={"model": "meta-llama/llama-3-70b", "messages": []}))
    mgr.event_bus.publish(NeedLLMResponse(data={"messages": []}))  # no model at all -> should fail cleanly

    ok = _wait_until(lambda: len(responses) == 2 and len(failures) == 1)
    models_used = sorted(c["model"] for c in client.calls)
    mgr.stop_all()
    return ok and models_used == ["meta-llama/llama-3-70b", "openai/gpt-4o"], f"models_used={models_used} failures={failures}"


def test_fish_audio_playback_and_cancellation() -> Result:
    mgr = AdapterManager.standalone()
    client = MockFishAudioClient(playback_delay_s=0.3)
    fa = FishAudioAdapter(client=client)
    mgr.register(fa)
    mgr.start_all()

    started = []
    cancelled = []
    mgr.event_bus.subscribe("speech_playback_started", lambda e: started.append(e.data))
    mgr.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.data))

    mgr.event_bus.publish(AssistantResponse(data={"text": "a long sentence", "request_id": "r1"}))
    _wait_until(lambda: len(started) == 1, timeout_s=1.0)
    fa.cancel_playback()
    ok = _wait_until(lambda: len(cancelled) == 1, timeout_s=1.0)
    mgr.stop_all()
    return ok, f"started={started} cancelled={cancelled}"


def test_unity_event() -> Result:
    mgr = AdapterManager.standalone()
    client = MockUnityClient()
    u = UnityAdapter(client=client)
    mgr.register(u)

    ready = []
    mgr.event_bus.start()
    mgr.event_bus.subscribe("avatar_ready", lambda e: ready.append(1))
    mgr.start_all()
    ok_ready = _wait_until(lambda: len(ready) == 1)

    # "emotion_changed" -> "unity" is already wired by the default
    # EventMapping (see models.DEFAULT_ADAPTER_EVENT_MAPPING) at
    # registration time - no manual add_route() needed (adding a second
    # one here would double-deliver).
    mgr.event_bus.publish(EmotionChanged(data={"emotion": "excited"}))
    ok_emotion = _wait_until(lambda: client.emotions_set == ["excited"])
    mgr.stop_all()

    ok = ok_ready and ok_emotion
    return ok, f"ready={ready} emotions_set={client.emotions_set}"


def test_home_assistant_event() -> Result:
    mgr = AdapterManager.standalone()
    src = MockHomeAssistantSource()
    client = MockHomeAssistantClient()
    ha = HomeAssistantAdapter(source=src, client=client)
    mgr.register(ha)
    mgr.start_all()

    device_events = []
    automation_events = []
    mgr.event_bus.subscribe("device_state_changed", lambda e: device_events.append(e.data))
    mgr.event_bus.subscribe("automation_triggered", lambda e: automation_events.append(e.data))

    src.simulate_state_change("switch.fan", "off", "on")
    src.simulate_automation("night_mode")
    ok_inbound = _wait_until(lambda: len(device_events) == 1 and len(automation_events) == 1)

    # "tool_requested" -> "home_assistant" is already wired by the
    # default EventMapping - no manual add_route() needed here.
    mgr.event_bus.publish(Event(type="tool_requested", data={"tool": "home_assistant", "action": "turn_off", "target": "switch.fan"}))
    ok_outbound = _wait_until(lambda: len(client.calls) == 1)

    mgr.stop_all()
    ok = ok_inbound and ok_outbound
    return ok, f"device_events={device_events} automation_events={automation_events} calls={client.calls}"


def test_fault_recovery_self_restart() -> Result:
    mgr = AdapterManager.standalone()
    flaky = RecordingAdapter("flaky", fail_events=True)
    flaky.MAX_CONSECUTIVE_FAILURES = 3
    mgr.register(flaky)
    mgr.start_all()
    mgr.event_bus.start()
    mgr.coordinator.add_route("trigger", "flaky")

    system_errors = []
    mgr.event_bus.subscribe("system_error", lambda e: system_errors.append(e.data))

    for _ in range(3):
        mgr.event_bus.publish(Event(type="trigger"))
    ok = _wait_until(lambda: flaky._restart_count == 1 and len(system_errors) == 3, timeout_s=2.0)

    # after self-restart, the module is still RUNNING (never took Runtime down)
    still_running = mgr.module_manager.all_modules()["flaky"].state.value == "running"
    mgr.stop_all()
    return ok and still_running, f"restart_count={flaky._restart_count} system_errors={len(system_errors)} still_running={still_running}"


def test_concurrent_adapters() -> Result:
    mgr = AdapterManager.standalone()
    adapters = [RecordingAdapter(f"adapter_{i}") for i in range(8)]
    for a in adapters:
        mgr.register(a)
    mgr.start_all()
    mgr.event_bus.start()
    for a in adapters:
        mgr.coordinator.add_route("broadcast", a.name)

    def publisher(n):
        for _ in range(n):
            mgr.event_bus.publish(Event(type="broadcast"))

    threads = [threading.Thread(target=publisher, args=(50,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = 5 * 50
    ok = _wait_until(lambda: all(len(a.received) == expected for a in adapters), timeout_s=5.0)
    mgr.stop_all()
    counts = [len(a.received) for a in adapters]
    return ok, f"expected={expected} counts={counts}"


def test_stress_many_events_across_adapters() -> Result:
    mgr = AdapterManager.standalone()
    a = RecordingAdapter("stress_a")
    b = RecordingAdapter("stress_b")
    mgr.register(a)
    mgr.register(b)
    mgr.start_all()
    mgr.event_bus.start()
    mgr.coordinator.add_route("stress_evt", "stress_a")
    mgr.coordinator.add_route("stress_evt", "stress_b")

    N = 1000
    start = time.time()
    for _ in range(N):
        mgr.event_bus.publish(Event(type="stress_evt"))
    ok = _wait_until(lambda: len(a.received) == N and len(b.received) == N, timeout_s=8.0)
    elapsed = time.time() - start
    stats = mgr.event_bus.stats()
    mgr.stop_all()

    ok = ok and stats["dropped"] == 0
    return ok, f"N={N} elapsed={elapsed:.2f}s a={len(a.received)} b={len(b.received)} dropped={stats['dropped']}"


# ============================================================================
# Runner
# ============================================================================

SCENARIOS = [
    test_adapter_registration,
    test_start_stop_all,
    test_restart,
    test_health_all,
    test_disabled_adapters,
    test_event_routing_full_pipeline,
    test_whisper_event,
    test_vision_event,
    test_openrouter_request_no_hardcoded_model,
    test_fish_audio_playback_and_cancellation,
    test_unity_event,
    test_home_assistant_event,
    test_fault_recovery_self_restart,
    test_concurrent_adapters,
    test_stress_many_events_across_adapters,
]


def main() -> int:
    print("\n=== Luno Adapter Layer - Test Suite ===")
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
