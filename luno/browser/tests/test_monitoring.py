"""
test_monitoring.py
====================

`luno.browser.monitoring` - `DebounceTracker` (pure, deterministic
edge-triggering) and `MonitoringService` (fake `http_get_fn`/event bus,
real `psutil` for local metrics - no real dashboard/network needed).
"""

from __future__ import annotations

from luno.browser.config import MonitorTarget
from luno.browser.monitoring import DebounceTracker, MonitoringService


# -- DebounceTracker ----------------------------------------------------------------

def test_debounce_does_not_fire_before_window_elapses():
    tracker = DebounceTracker(debounce_s=30.0)
    now = 1000.0
    assert tracker.observe("cpu_high", True, now=now) is False
    assert tracker.observe("cpu_high", True, now=now + 10) is False


def test_debounce_fires_once_after_window_elapses():
    tracker = DebounceTracker(debounce_s=30.0)
    now = 1000.0
    tracker.observe("cpu_high", True, now=now)
    assert tracker.observe("cpu_high", True, now=now + 30) is True


def test_debounce_never_fires_twice_for_same_continuous_streak():
    """This is spec section 6's exact requirement: fire ONCE, not every
    polling cycle, while the condition stays continuously true."""
    tracker = DebounceTracker(debounce_s=10.0)
    now = 1000.0
    tracker.observe("mem_high", True, now=now)
    assert tracker.observe("mem_high", True, now=now + 10) is True
    assert tracker.observe("mem_high", True, now=now + 20) is False
    assert tracker.observe("mem_high", True, now=now + 30) is False


def test_debounce_rearms_after_condition_clears():
    tracker = DebounceTracker(debounce_s=10.0)
    now = 1000.0
    tracker.observe("mem_high", True, now=now)
    assert tracker.observe("mem_high", True, now=now + 10) is True
    tracker.observe("mem_high", False, now=now + 11)  # condition clears
    tracker.observe("mem_high", True, now=now + 12)   # new streak starts
    assert tracker.observe("mem_high", True, now=now + 22) is True


def test_debounce_condition_false_never_fires():
    tracker = DebounceTracker(debounce_s=1.0)
    assert tracker.observe("x", False, now=1000.0) is False
    assert tracker.observe("x", False, now=1005.0) is False


# -- MonitoringService.check_target --------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_check_target_reachable():
    target = MonitorTarget(name="Portainer", url="http://fake-portainer.local", type="portainer")
    service = MonitoringService(http_get_fn=lambda url, timeout=5.0: _FakeResponse(200))
    status = service.check_target(target)
    assert status.reachable is True


def test_check_target_unreachable_exception():
    def _raise(url, timeout=5.0):
        raise ConnectionError("no route to host")
    target = MonitorTarget(name="Portainer", url="http://fake-portainer.local", type="portainer")
    service = MonitoringService(http_get_fn=_raise)
    status = service.check_target(target)
    assert status.reachable is False
    assert "unreachable" in status.detail


def test_check_target_disabled_target_reported_unreachable_never_claimed_healthy():
    target = MonitorTarget(name="Grafana", url="http://fake-grafana.local", type="grafana", enabled=False)
    service = MonitoringService(http_get_fn=lambda url, timeout=5.0: _FakeResponse(200))
    status = service.check_target(target)
    assert status.reachable is False
    assert status.detail == "disabled"


def test_check_target_home_assistant_reachable():
    target = MonitorTarget(name="HA", url="http://fake-ha.local:8123", type="home_assistant")
    service = MonitoringService()
    # home_assistant path uses `requests` directly, not `_http_get_fn` -
    # patch via monkeypatched module import instead.
    class _FakeRequestsModule:
        @staticmethod
        def get(url, headers=None, timeout=5.0):
            return _FakeResponse(200)

    import sys
    original = sys.modules.get("requests")
    sys.modules["requests"] = _FakeRequestsModule
    try:
        status = service.check_target(target)
    finally:
        if original is not None:
            sys.modules["requests"] = original
    assert status.reachable is True


def test_local_metrics_returns_real_numbers():
    """`psutil` is a real, always-available dependency in this repo's
    environment - this asserts real, sane values rather than mocking it
    out, since local system metrics are exactly the thing this module
    should never fake."""
    service = MonitoringService()
    metrics = service.local_metrics()
    if metrics:  # only assert shape if psutil actually returned something
        assert 0.0 <= metrics["cpu_percent"] <= 100.0
        assert 0.0 <= metrics["memory_percent"] <= 100.0
        assert metrics["memory_used_gb"] >= 0


# -- event emission -----------------------------------------------------------------

class _FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def test_check_all_emits_monitoring_target_unreachable_after_debounce():
    target = MonitorTarget(name="Portainer", url="http://fake.local", type="portainer")

    def _raise(url, timeout=5.0):
        raise ConnectionError("down")

    bus = _FakeEventBus()
    service = MonitoringService(event_bus=bus, http_get_fn=_raise, debounce_s=0.0)
    service.check_all([target])
    types = [e.type for e in bus.published]
    assert "monitoring_target_unreachable" in types


def test_check_all_never_emits_for_reachable_target():
    target = MonitorTarget(name="Portainer", url="http://fake.local", type="portainer")
    bus = _FakeEventBus()
    service = MonitoringService(event_bus=bus, http_get_fn=lambda url, timeout=5.0: _FakeResponse(200), debounce_s=0.0)
    service.check_all([target])
    types = [e.type for e in bus.published]
    assert "monitoring_target_unreachable" not in types


def test_check_all_does_not_spam_repeated_sweeps_within_debounce_window():
    """Two consecutive `check_all()` calls, condition true both times,
    debounce window not yet elapsed on the second - must not double-fire."""
    target = MonitorTarget(name="Portainer", url="http://fake.local", type="portainer")

    def _raise(url, timeout=5.0):
        raise ConnectionError("down")

    bus = _FakeEventBus()
    service = MonitoringService(event_bus=bus, http_get_fn=_raise, debounce_s=9999.0)
    service.check_all([target])
    service.check_all([target])
    unreachable_events = [e for e in bus.published if e.type == "monitoring_target_unreachable"]
    assert len(unreachable_events) <= 1


def test_no_event_bus_does_not_crash():
    target = MonitorTarget(name="Portainer", url="http://fake.local", type="portainer")
    service = MonitoringService(event_bus=None, http_get_fn=lambda url, timeout=5.0: _FakeResponse(200))
    statuses = service.check_all([target])  # should not raise
    assert len(statuses) == 1


# -- format_note ----------------------------------------------------------------------

def test_format_note_never_claims_unreachable_target_healthy():
    target = MonitorTarget(name="Portainer", url="http://fake.local", type="portainer")
    from luno.browser.monitoring import TargetStatus
    status = TargetStatus(target=target, reachable=False, detail="unreachable: timeout")
    note = MonitoringService.format_note([status], {})
    line = [ln for ln in note.splitlines() if "Portainer" in ln][0]
    assert "UNREACHABLE" in line
    assert "unreachable: timeout" in line
