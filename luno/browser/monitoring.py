"""
monitoring.py (luno.browser)
===============================

`MonitoringService` - spec section 5/6: check configured dashboards
(`MonitorTarget`s, see `config.py`), collect real system/service state,
detect abnormal conditions, and emit debounced Event Bus events - never
inventing a value, never reporting a target healthy that couldn't
actually be reached.

Three independent data sources, each honestly labelled about what it
actually checked:

  1. LOCAL system metrics (CPU/RAM/disk) via `psutil`, for the machine
     Luno itself runs on - the most reliable, structured, testable
     source this module has (spec's own example reply, "RAM sedang
     13.2 GB," matches something `psutil` can state as an exact,
     verified number, not a screen-read guess).
  2. Dashboard REACHABILITY (HTTP GET/HEAD, short timeout) for every
     configured target - answers "is it up at all" without needing to
     understand the page's content. A `home_assistant` target reuses
     the existing `HA_URL`/`HA_TOKEN` the project already has (spec's
     own "preserve existing Home Assistant integration" instruction -
     this does NOT reimplement HA state reading, just a liveness ping).
  3. Dashboard VISUAL inspection (`inspect_dashboard_visually()`) -
     OPTIONAL, on-demand, screenshot + the EXISTING Gemini/OpenAI vision
     provider (spec section 7's pipeline) for the content a plain HTTP
     check can't see (e.g. reading Portainer's actual container list off
     the rendered page). Deliberately NOT part of the debounced
     threshold-event path below - vision output is prose, not a
     reliable structured number to threshold/debounce against; it's
     for the free-text summary a user asked for ("check my server"),
     not for firing `server_cpu_high` automatically.

`DebounceTracker` implements spec section 6's exact requirement: a
condition must be seen continuously TRUE for `debounce_s` before the
matching event fires ONCE (edge-triggered) - it must flip back to
false and re-trigger before firing again, never re-firing every poll
cycle while the condition stays true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import MonitorTarget


@dataclass
class TargetStatus:
    target: MonitorTarget
    reachable: bool
    detail: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


class DebounceTracker:
    """Edge-triggered: `observe(key, condition_true)` returns `True`
    (fire the event) exactly once per continuous TRUE streak that has
    lasted at least `debounce_s` - never again for that same streak,
    and only re-armed once `condition_true` goes `False` at least once
    in between."""

    def __init__(self, debounce_s: float = 30.0) -> None:
        self._debounce_s = debounce_s
        self._since: Dict[str, float] = {}
        self._fired: Dict[str, bool] = {}

    def observe(self, key: str, condition_true: bool, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if not condition_true:
            self._since.pop(key, None)
            self._fired.pop(key, None)
            return False
        if key not in self._since:
            self._since[key] = now
        elapsed = now - self._since[key]
        if elapsed >= self._debounce_s and not self._fired.get(key):
            self._fired[key] = True
            return True
        return False


class MonitoringService:
    def __init__(
        self, event_bus: Optional[Any] = None, browser_provider: Optional[Any] = None,
        debounce_s: float = 30.0, http_get_fn: Optional[Any] = None,
        cpu_threshold_pct: float = 90.0, memory_threshold_pct: float = 90.0, disk_threshold_pct: float = 90.0,
    ) -> None:
        self._event_bus = event_bus
        self._browser_provider = browser_provider
        self._debounce = DebounceTracker(debounce_s)
        self._http_get_fn = http_get_fn
        self._cpu_threshold_pct = cpu_threshold_pct
        self._memory_threshold_pct = memory_threshold_pct
        self._disk_threshold_pct = disk_threshold_pct

    # -- local system metrics -----------------------------------------------------

    def local_metrics(self) -> Dict[str, Any]:
        """Real, verified numbers via `psutil` - `{}` (never fake
        numbers) if `psutil` isn't installed."""
        try:
            import psutil
        except ImportError:
            return {}
        try:
            vm = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            return {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": vm.percent,
                "memory_used_gb": round(vm.used / (1024 ** 3), 2),
                "memory_total_gb": round(vm.total / (1024 ** 3), 2),
                "disk_percent": disk.percent,
            }
        except Exception:
            return {}

    # -- dashboard reachability ----------------------------------------------------

    def _get(self, url: str, timeout_s: float = 5.0) -> Any:
        if self._http_get_fn is not None:
            return self._http_get_fn(url, timeout=timeout_s)
        import requests
        return requests.get(url, timeout=timeout_s)

    def check_target(self, target: MonitorTarget) -> TargetStatus:
        if not target.enabled:
            return TargetStatus(target=target, reachable=False, detail="disabled")
        if target.type == "home_assistant":
            return self._check_home_assistant(target)
        try:
            resp = self._get(target.url)
            status_code = getattr(resp, "status_code", None)
            reachable = status_code is not None and 200 <= status_code < 500
            return TargetStatus(target=target, reachable=reachable, detail=f"HTTP {status_code}")
        except Exception as ex:
            return TargetStatus(target=target, reachable=False, detail=f"unreachable: {ex}")

    def _check_home_assistant(self, target: MonitorTarget) -> TargetStatus:
        from luno import config as legacy_config
        headers = {"Authorization": f"Bearer {legacy_config.HA_TOKEN}"} if legacy_config.HA_TOKEN else {}
        try:
            import requests
            resp = requests.get(f"{target.url.rstrip('/')}/api/", headers=headers, timeout=5.0)
            reachable = resp.status_code == 200
            return TargetStatus(target=target, reachable=reachable, detail=f"HTTP {resp.status_code}")
        except Exception as ex:
            return TargetStatus(target=target, reachable=False, detail=f"unreachable: {ex}")

    # -- optional visual inspection (spec section 7 pipeline) ----------------------

    def inspect_dashboard_visually(self, target: MonitorTarget, vision_analyze_fn: Optional[Any] = None) -> Optional[str]:
        """Screenshot -> (existing) vision provider -> free-text
        description of what's actually on the dashboard. `None` if no
        browser provider is available, or on any failure - never
        fabricates a description. `vision_analyze_fn(image_bytes,
        prompt) -> str` defaults to `luno.vision._get_vision_provider()
        .analyze_image` (the SAME on-demand Gemini/OpenAI provider
        vision questions already use - no separate/parallel vision
        model)."""
        if self._browser_provider is None:
            return None
        try:
            self._browser_provider.open_url(target.url)
            image = self._browser_provider.screenshot()
        except Exception:
            return None
        if vision_analyze_fn is None:
            try:
                import luno.vision as vision_module
                provider = vision_module._get_vision_provider()
                vision_analyze_fn = provider.analyze_image
            except Exception:
                return None
        try:
            return vision_analyze_fn(image, f"This is a screenshot of the '{target.name}' dashboard. Briefly summarize what's visible - status, key metrics, anything that looks abnormal.")
        except Exception:
            return None

    # -- full sweep + debounced event emission --------------------------------------

    def check_all(self, targets: List[MonitorTarget]) -> List[TargetStatus]:
        statuses = [self.check_target(t) for t in targets]
        metrics = self.local_metrics()
        self._emit_events(statuses, metrics)
        return statuses

    def _publish(self, event_cls_name: str, data: Dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        from luno.core.events import (
            DockerContainerDown, HomeAssistantUnavailable, MonitoringTargetUnreachable,
            ServerCpuHigh, ServerDiskHigh, ServerMemoryHigh, ServerServiceDown,
        )
        registry = {
            "server_cpu_high": ServerCpuHigh, "server_memory_high": ServerMemoryHigh,
            "server_disk_high": ServerDiskHigh, "server_service_down": ServerServiceDown,
            "docker_container_down": DockerContainerDown,
            "home_assistant_unavailable": HomeAssistantUnavailable,
            "monitoring_target_unreachable": MonitoringTargetUnreachable,
        }
        cls = registry.get(event_cls_name)
        if cls is None:
            return
        self._event_bus.publish(cls(data=data))

    def _emit_events(self, statuses: List[TargetStatus], metrics: Dict[str, Any]) -> None:
        now = time.time()
        if metrics.get("cpu_percent") is not None:
            if self._debounce.observe("cpu_high", metrics["cpu_percent"] >= self._cpu_threshold_pct, now):
                self._publish("server_cpu_high", {"cpu_percent": metrics["cpu_percent"], "threshold": self._cpu_threshold_pct})
        if metrics.get("memory_percent") is not None:
            if self._debounce.observe("memory_high", metrics["memory_percent"] >= self._memory_threshold_pct, now):
                self._publish("server_memory_high", {"memory_percent": metrics["memory_percent"], "threshold": self._memory_threshold_pct})
        if metrics.get("disk_percent") is not None:
            if self._debounce.observe("disk_high", metrics["disk_percent"] >= self._disk_threshold_pct, now):
                self._publish("server_disk_high", {"disk_percent": metrics["disk_percent"], "threshold": self._disk_threshold_pct})

        for status in statuses:
            key = f"target_unreachable:{status.target.name}"
            if self._debounce.observe(key, not status.reachable, now):
                if status.target.type == "home_assistant":
                    self._publish("home_assistant_unavailable", {"target": status.target.name, "detail": status.detail})
                else:
                    self._publish("monitoring_target_unreachable", {"target": status.target.name, "detail": status.detail})

    @staticmethod
    def format_note(statuses: List[TargetStatus], metrics: Dict[str, Any]) -> str:
        """Renders a monitoring sweep into a system-prompt note - same
        "labelled, LLM still phrases the final reply" shape as
        `research.py`'s `format_note`. Never says a target is healthy
        unless `status.reachable` is actually `True`."""
        lines = ["Server/Dashboard Monitoring (just checked, use as verified fact - do not invent numbers not listed here):"]
        if metrics:
            bits = []
            if "cpu_percent" in metrics:
                bits.append(f"CPU {metrics['cpu_percent']}%")
            if "memory_used_gb" in metrics:
                bits.append(f"RAM {metrics['memory_used_gb']}/{metrics.get('memory_total_gb', '?')} GB ({metrics.get('memory_percent', '?')}%)")
            if "disk_percent" in metrics:
                bits.append(f"Disk {metrics['disk_percent']}%")
            if bits:
                lines.append("- Local system: " + ", ".join(bits))
        else:
            lines.append("- Local system metrics unavailable (psutil not installed)")
        for status in statuses:
            state = "reachable" if status.reachable else "UNREACHABLE"
            lines.append(f"- {status.target.name} ({status.target.type}): {state} - {status.detail}")
        return "\n".join(lines)
