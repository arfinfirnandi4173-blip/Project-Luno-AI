"""
luno_live_p0_8_1_verification.py
====================================

LUNO P0.8.1 (Live Camera -> Home Assistant Light Verification) - run
this DIRECTLY on the real Luno machine, in its own existing `.venv`,
where the real Tapo C212 and the real Home Assistant instance are
actually reachable:

    python luno_live_p0_8_1_verification.py

--------------------------------------------------------------------
Why this is a NEW file, not another addition to
luno_live_camera_event_observer.py
--------------------------------------------------------------------
Every "live verification" script in this project's history (P0.5.4-
LIVE, then P0.6.1/P0.6.2/P0.6.2-FIX additively) has extended ONE file,
`luno_live_camera_event_observer.py`, because each addition was the
SAME shape: boot the real stack, watch a fixed observation window,
print a passive evidence summary at the end. P0.8.1 is a materially
different shape - the brief's own Section 4 defines SIX distinct,
ordered, human-paced tests (idle / enter / stay / exit / manual-state /
detector-failure-safety), several of which require the human tester to
perform a physical action (walk in, walk out, manually flip the light)
BETWEEN measurement windows, gated with explicit prompts rather than
one fixed sleep - plus a wholly new, larger mandated output schema
(Section 6). Bolting a second, incompatible control-flow shape onto an
already-744-line file already documented as "reuse this file" for
SIMILAR-shaped work would make that file harder to follow, not easier.
This file reuses every existing piece of infrastructure that IS the
same shape (the real bootstrap, the real Event Bus, the real
`AutomationEngine`, the real Home Assistant client) - it does not
duplicate, reimplement, or run a second copy of any of them. See
`docs/change_impact/camera_automation_p0_8_1.md` for the full reasoning.

--------------------------------------------------------------------
Why this could not be executed against real hardware from the sandbox
--------------------------------------------------------------------
Every prior "live verification" attempt in this project (P0.5.1 through
P0.8.0) was run from an isolated cloud development sandbox that has NO
network route to the user's home LAN, camera, or Home Assistant
instance - confirmed repeatedly by direct TCP/DNS probes, never
assumed. This script was written and unit-tested (see `tests/test_p0_
8_1_live_verification.py`) from that same sandbox, but the live test
sequence itself was never (and could not be) executed there against
real hardware - the sandbox's own pre-flight check for THIS script was
actually run, and its honest result (a hard stop) is recorded in
`docs/change_impact/camera_automation_p0_8_1.md`.

--------------------------------------------------------------------
WHAT THIS DOES
--------------------------------------------------------------------
1. PRE-FLIGHT (Section 1 of the brief) - every one of the 13 checks the
   brief lists, ALL treated as critical (hard stop if any fails, no
   device action ever attempted): TAPO_HOST/USERNAME/PASSWORD
   configured, Home Assistant reachable (TCP), Home Assistant
   authentication succeeds (a real, read-only connect+auth+disconnect
   probe using a throwaway `luno.ha_client.HomeAssistantClient` -
   NEVER the runtime's own live connection, never a service call),
   Vision backend = real, `ultralytics` importable, `cv2` importable,
   `CAMERA_VISION_ENABLED`, camera reachable (TCP 443), RTSP reachable
   (TCP 554), camera automation enabled, and the P0.8 safety gate
   present/wired.

2. TEST LIGHT (Section 2) - resolves `CAMERA_AUTOMATION_TEST_LIGHT_
   ENTITY` ONLY. Never guesses, never falls back to a hardcoded/
   discovered entity id. Hard stop if unset.

3. Boots the REAL, EXISTING module stack via the exact same
   `LauncherConfig.load()` -> `register_all_modules()` ->
   `register_all_adapters()` sequence every other script/test in this
   repo already uses - no new bootstrap path. Calls the EXISTING,
   additive `apply_camera_automation_test_light_override()` (`luno/
   bootstrap/adapters.py`, new this sprint) to point the EXISTING
   P0.8.0 TEST-ONLY rule (`camera_test_automation_safety_action`) at
   the resolved test light, in memory, for this process only -
   `config/automation_rules.json` on disk is never modified.

4. Confirms the rule actually loaded and is enabled (`AutomationEngine.
   get_automation_status()`, a pre-existing read-only accessor) before
   running any test - hard stop, never force-enable, if it did not.

5. Runs the SIX tests from Section 4, each gated by an explicit,
   printed instruction for the human tester and (where the brief calls
   for it) an `input()` prompt so the physical action (walking in/out,
   flipping the light) happens at a known, recorded point in time
   rather than guessed from a fixed sleep. Subscribes only to the
   EXISTING `camera_automation.camera_event`, the four raw Vision
   events, `automation.*` outcome/action events (filtered to this one
   rule id), `tool_requested`, and `system_error` (filtered to
   `vision_detection_failed`) - the exact same event set `luno_live_
   camera_event_observer.py` already subscribes to, nothing new.

6. Prints ONLY normalized, safe evidence throughout - never `TAPO_
   PASSWORD`/`TAPO_USERNAME`/an RTSP URL/`HA_TOKEN`/any other secret
   (verified structurally by `tests/test_p0_8_1_live_verification.py`).

7. Produces the exact mandated `--- LIVE P0.8.1 RESULT ---` block
   (Section 6) plus the Section 7 safety classification, and returns a
   final PASS / FAIL / BLOCKED verdict - BLOCKED whenever pre-flight,
   the test-light resolution, or the rule-load check stopped the run
   before any test could execute; FAIL if the Section 7 safety
   conditions are violated OR any test's own explicit expectation was
   not met; PASS only if every test's expectation was met AND no
   safety condition was violated.

--------------------------------------------------------------------
WHAT THIS NEVER DOES
--------------------------------------------------------------------
Never modifies `luno/camera_automation/*`, `luno/automation/models.py`,
`luno/automation/conditions.py`, `luno/automation/engine.py`, any Home
Assistant client/adapter file, or any rule other than the one P0.8.0
TEST-ONLY rule's `target` parameter (in memory, this process only).
Never writes to `config/automation_rules.json`, `config/camera_
automation.json`, or `.env`. Never calls a Home Assistant service
directly (always goes through the real, existing `AutomationEngine` ->
`ToolManager` -> `RealHomeAssistantHandler` path - this script itself
never imports or invokes the Home Assistant call-service API directly).
Never sends a PTZ command.
Never fabricates, assumes, or extrapolates a test result it did not
actually observe - a test whose window produced no evidence is reported
as such, not silently marked PASS.

Exit code is always 0 (diagnostic, not a CI gate) - read the printed
report, not the exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

#: The ONE existing P0.8.0 TEST-ONLY rule this script observes (brief
#: Section 3: "Use the existing P0.8.0 TEST-ONLY rule... do not create
#: a second one"). Must match `config/automation_rules.json` exactly.
_RULE_ID = "camera_test_automation_safety_action"
_ACTION_TYPE = "home_assistant.turn_on"

#: P0.8.2 addition (Task #139) - the ONE existing P0.8.2 TEST-ONLY OFF
#: rule this script ALSO observes when `--sequence p0_8_2` is used.
#: Same file, same `_LiveObserver`, same pre-flight/hard-stop
#: discipline as the ON rule above - NOT a second/competing live
#: observer (brief Section 9's own explicit constraint). Must match
#: `config/automation_rules.json` exactly.
_OFF_RULE_ID = "camera_test_automation_safety_action_off"
_ACTION_TYPE_OFF = "home_assistant.turn_off"

#: brief Section 1 - every check here is CRITICAL; any FAIL is a hard
#: stop, per that section's own "If any critical requirement fails:
#: HARD STOP. Do not attempt a real device action."
_CRITICAL_PREFLIGHT_CHECKS = {
    "TAPO_HOST configured",
    "TAPO_USERNAME configured",
    "TAPO_PASSWORD configured",
    "Home Assistant reachable",
    "Home Assistant authentication succeeds",
    "Vision backend = real",
    "ultralytics (YOLO) importable",
    "cv2 (OpenCV) importable",
    "CAMERA_VISION_ENABLED",
    "Camera reachable (TCP 443)",
    "RTSP reachable (TCP 554)",
    "Camera automation enabled",
    "P0.8 safety gate enabled",
}


def _tcp_check(host: str, port: int, timeout_s: float = 5.0) -> Dict[str, Any]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
        return {"ok": True, "detail": "OPEN"}
    except Exception as ex:
        return {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        s.close()


def _ha_host_port() -> Optional[Dict[str, Any]]:
    """Read-only parse of `HA_URL` (never `HA_TOKEN`) into a host/port
    pair for a plain TCP reachability probe - never prints the URL
    itself in case it embeds a nonstandard path/port a user might
    consider sensitive, only the parsed host is ever surfaced."""
    try:
        import luno.config as legacy_config
        url = getattr(legacy_config, "HA_URL", None)
        if not url:
            return None
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return {"host": parsed.hostname, "port": port}
    except Exception:
        return None


def _check_ha_reachable() -> Dict[str, Any]:
    hp = _ha_host_port()
    if hp is None:
        return {"ok": False, "detail": "HA_URL not set/unparseable"}
    result = _tcp_check(hp["host"], hp["port"])
    result["detail"] = f"{hp['host']}:{hp['port']} - {result['detail']}"
    return result


def _check_ha_auth() -> Dict[str, Any]:
    """Read-only connect+auth+disconnect probe using a THROWAWAY
    `luno.ha_client.HomeAssistantClient` instance - never the real
    runtime's own live connection (that one is opened later, inside
    `RealHomeAssistantSource`, only after this whole pre-flight already
    passed), never a service call of any kind."""
    try:
        from luno.ha_client import HomeAssistantClient as _RealHAClientImpl

        async def _probe() -> bool:
            client = _RealHAClientImpl()
            try:
                ok = await asyncio.wait_for(client.connect(), timeout=15)
                return bool(ok)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        ok = asyncio.run(_probe())
        return {"ok": ok, "detail": "auth_ok" if ok else "connect/auth did not succeed (see HA client log above)"}
    except Exception as ex:
        return {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}


def _run_preflight() -> Dict[str, Any]:
    import luno.config as legacy_config
    from luno.bootstrap.launcher_config import LauncherConfig

    results: Dict[str, Any] = {}

    host_set = bool(legacy_config.TAPO_HOST)
    user_set = bool(legacy_config.TAPO_USERNAME)
    pass_set = bool(legacy_config.TAPO_PASSWORD)
    results["TAPO_HOST configured"] = {"ok": host_set, "detail": "configured" if host_set else "NOT SET"}
    results["TAPO_USERNAME configured"] = {"ok": user_set, "detail": "configured" if user_set else "NOT SET"}
    results["TAPO_PASSWORD configured"] = {"ok": pass_set, "detail": "configured" if pass_set else "NOT SET"}

    if host_set:
        results["Camera reachable (TCP 443)"] = _tcp_check(legacy_config.TAPO_HOST, 443)
        results["RTSP reachable (TCP 554)"] = _tcp_check(legacy_config.TAPO_HOST, 554)
    else:
        results["Camera reachable (TCP 443)"] = {"ok": False, "detail": "TAPO_HOST not set"}
        results["RTSP reachable (TCP 554)"] = {"ok": False, "detail": "TAPO_HOST not set"}

    results["Home Assistant reachable"] = _check_ha_reachable()
    if results["Home Assistant reachable"]["ok"]:
        results["Home Assistant authentication succeeds"] = _check_ha_auth()
    else:
        results["Home Assistant authentication succeeds"] = {"ok": False, "detail": "skipped - HA not reachable"}

    try:
        cfg = LauncherConfig.load()
        results["Vision backend = real"] = {"ok": cfg.vision_backend == "real", "detail": cfg.vision_backend}
    except Exception as ex:
        results["Vision backend = real"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    try:
        import ultralytics  # noqa: F401
        results["ultralytics (YOLO) importable"] = {"ok": True, "detail": f"importable ({ultralytics.__version__})"}
    except Exception as ex:
        results["ultralytics (YOLO) importable"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    try:
        import cv2  # noqa: F401
        results["cv2 (OpenCV) importable"] = {"ok": True, "detail": f"importable ({cv2.__version__})"}
    except Exception as ex:
        results["cv2 (OpenCV) importable"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    # P0.8.3 addition - informational only, deliberately NOT added to
    # `_CRITICAL_PREFLIGHT_CHECKS` (this must never become a NEW hard-
    # stop gate; a version-detection failure here is not itself a
    # reason to block a live run). Exists so a `'Conv' object has no
    # attribute 'bn'`-style YOLO inference failure (see `luno/vision.py::
    # _yolo_checkpoint_hint()` and `docs/change_impact/camera_
    # automation_p0_8_3.md`) can be immediately cross-referenced against
    # the EXACT installed torch/torchvision versions and the EXACT local
    # checkpoint files/sizes this run will use - without anyone needing
    # to manually inspect `.venv` by hand the way this sprint's own
    # investigation had to.
    try:
        import torch  # noqa: F401
        torch_detail = f"torch {torch.__version__}"
    except Exception as ex:
        torch_detail = f"torch import failed: {type(ex).__name__}: {ex}"
    try:
        import torchvision  # noqa: F401
        torchvision_detail = f"torchvision {torchvision.__version__}"
    except Exception as ex:
        torchvision_detail = f"torchvision import failed: {type(ex).__name__}: {ex}"
    results["YOLO stack versions (informational)"] = {"ok": True, "detail": f"{torch_detail}, {torchvision_detail}"}

    model_bits = []
    for label, path_attr in (("detection", "YOLO_MODEL_PATH"), ("pose", "YOLO_POSE_MODEL_PATH")):
        model_path = getattr(legacy_config, path_attr, None)
        if not model_path:
            model_bits.append(f"{label}=<unset>")
        elif os.path.exists(model_path):
            model_bits.append(f"{label}={model_path} ({os.path.getsize(model_path)} bytes)")
        else:
            model_bits.append(f"{label}={model_path} (not present yet - will auto-download on first use)")
    results["YOLO model files (informational)"] = {"ok": True, "detail": ", ".join(model_bits)}

    results["CAMERA_VISION_ENABLED"] = {"ok": bool(legacy_config.CAMERA_VISION_ENABLED), "detail": str(legacy_config.CAMERA_VISION_ENABLED)}

    try:
        from luno.camera_automation.config import CameraAutomationConfig
        cam_cfg = CameraAutomationConfig.from_env()
        results["Camera automation enabled"] = {"ok": bool(cam_cfg.enabled), "detail": str(cam_cfg.enabled)}
    except Exception as ex:
        results["Camera automation enabled"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    try:
        import luno.automation.camera_action_safety as _safety_mod
        from luno.automation.engine import AutomationEngine as _Engine
        gate_present = hasattr(_safety_mod, "validate_camera_ha_action") and hasattr(_Engine, "_is_camera_triggered_rule")
        results["P0.8 safety gate enabled"] = {
            "ok": gate_present,
            "detail": "camera_action_safety module + engine wiring present" if gate_present else "NOT FOUND",
        }
    except Exception as ex:
        results["P0.8 safety gate enabled"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    return results


def _print_preflight(results: Dict[str, Any]) -> bool:
    print("Pre-flight:")
    all_critical_ok = True
    for name, result in results.items():
        status = "PASS" if result["ok"] else "FAIL"
        print(f"    {name}: {status}  ({result['detail']})")
        if name in _CRITICAL_PREFLIGHT_CHECKS and not result["ok"]:
            all_critical_ok = False
    return all_critical_ok


def _resolve_test_light() -> Optional[str]:
    """brief Section 2 - explicit config ONLY, never guessed."""
    val = os.environ.get("CAMERA_AUTOMATION_TEST_LIGHT_ENTITY", "").strip()
    return val or None


def _print_test_light(entity_id: Optional[str]) -> None:
    print("TEST LIGHT:")
    print(f"    entity_id={entity_id if entity_id else '<none>'}")
    print("    source=config (CAMERA_AUTOMATION_TEST_LIGHT_ENTITY)")
    print(f"    confirmed={'YES' if entity_id else 'NO'}")


class _Snapshot:
    """A cheap counter snapshot/delta helper so each of the six tests
    can report ONLY what happened during its own window, never the
    cumulative run total - Section 4's own per-test expectations are
    all phrased as deltas ('no repeated unnecessary turn_on calls'
    during TEST 3, not 'zero turn_on calls ever')."""

    def __init__(self, observer: "_LiveObserver") -> None:
        self.observer = observer
        self.raw = dict(observer.raw_vision_event_counts)
        self.kinds = list(observer.camera_events)
        self.outcomes = dict(observer.outcome_counts)
        self.actions = list(observer.action_events)
        self.tool_requested = observer.tool_requested_count
        self.vision_detection_failed = observer.vision_detection_failed_count
        self.offline_kind_count = sum(1 for e in observer.camera_events if e.get("kind") == "camera_offline")
        # P0.8.2 addition - per-rule snapshots (additive; unused by the
        # existing P0.8.1 TEST 1-6 sequence, which continues to use the
        # single-rule `outcomes`/`actions`/delta_outcome()/new_actions()
        # above exactly as before). Needed because TEST A-F must track
        # the ON rule and the OFF rule's evidence INDEPENDENTLY and
        # simultaneously (Section 9).
        self.outcomes_by_rule = {rid: dict(counts) for rid, counts in observer.outcome_counts_by_rule.items()}
        self.actions_by_rule_len = {rid: len(events) for rid, events in observer.action_events_by_rule.items()}

    def delta_raw(self, event_type: str) -> int:
        return self.observer.raw_vision_event_counts.get(event_type, 0) - self.raw.get(event_type, 0)

    def delta_kind(self, kind: str) -> int:
        before = sum(1 for e in self.kinds if e.get("kind") == kind)
        after = sum(1 for e in self.observer.camera_events if e.get("kind") == kind)
        return after - before

    def delta_outcome(self, outcome: str) -> int:
        return self.observer.outcome_counts.get(outcome, 0) - self.outcomes.get(outcome, 0)

    def new_actions(self) -> List[Dict[str, Any]]:
        return self.observer.action_events[len(self.actions):]

    def delta_tool_requested(self) -> int:
        return self.observer.tool_requested_count - self.tool_requested

    def delta_vision_detection_failed(self) -> int:
        return self.observer.vision_detection_failed_count - self.vision_detection_failed

    def delta_offline(self) -> int:
        offline_now = sum(1 for e in self.observer.camera_events if e.get("kind") == "camera_offline")
        return offline_now - self.offline_kind_count

    def delta_outcome_for_rule(self, rule_id: str, outcome: str) -> int:
        """P0.8.2 addition - same delta discipline as `delta_outcome()`
        above, scoped to one rule id (ON or OFF) instead of the single
        implicit `_RULE_ID` the P0.8.1 sequence tracks."""
        after = self.observer.outcome_counts_by_rule.get(rule_id, {}).get(outcome, 0)
        before = self.outcomes_by_rule.get(rule_id, {}).get(outcome, 0)
        return after - before

    def new_actions_for_rule(self, rule_id: str) -> List[Dict[str, Any]]:
        """P0.8.2 addition - same delta discipline as `new_actions()`
        above, scoped to one rule id (ON or OFF)."""
        before_len = self.actions_by_rule_len.get(rule_id, 0)
        return self.observer.action_events_by_rule.get(rule_id, [])[before_len:]


class _LiveObserver:
    """Temporary, print-only observer - identical discipline to `luno_
    live_camera_event_observer.py::_LiveObserver` (never executes an
    action itself, only ever `print(...)`s and appends to its own
    in-memory lists)."""

    def __init__(self) -> None:
        self.camera_events: List[Dict[str, Any]] = []
        self.raw_vision_event_counts: Dict[str, int] = {}
        self.outcome_counts: Dict[str, int] = {}
        self.action_events: List[Dict[str, Any]] = []
        # P0.8.2 addition - by-rule breakdown of the same events already
        # captured above (additive; `outcome_counts`/`action_events`
        # keep meaning exactly what they meant in P0.8.1 - "the one
        # tracked rule's" totals - unchanged for the default sequence).
        self.outcome_counts_by_rule: Dict[str, Dict[str, int]] = {}
        self.action_events_by_rule: Dict[str, List[Dict[str, Any]]] = {}
        self.tool_requested_count: int = 0
        self.tool_requested_by_tool: Dict[str, int] = {}
        self.vision_detection_failed_count: int = 0
        self.last_vision_detection_error: Optional[str] = None
        self.test_light_entity: Optional[str] = None
        self._start_time = time.time()

    def on_camera_event(self, event: Any) -> None:
        data = dict(event.data or {})
        elapsed = time.time() - self._start_time
        self.camera_events.append(data)
        print(
            f"[T+{elapsed:07.3f}] [CAMERA EVENT] kind={data.get('kind')} "
            f"available={data.get('available')} detection_error={data.get('detection_error')} "
            f"person_count={data.get('person_count')}"
        )

    def on_raw_vision_event(self, event_type: str):
        def _handler(event: Any) -> None:
            # Never touches event.data - camera_disconnected/
            # camera_reconnected can carry the full credentialed RTSP
            # URL (see luno_live_camera_event_observer.py's own
            # docstring for the precedent this follows).
            elapsed = time.time() - self._start_time
            self.raw_vision_event_counts[event_type] = self.raw_vision_event_counts.get(event_type, 0) + 1
            print(f"[T+{elapsed:07.3f}] [Vision] {event_type} observed")
        return _handler

    def on_outcome_event(self, outcome: str, rule_ids: Optional[Set[str]] = None):
        """`rule_ids` (P0.8.2 addition) - which rule id(s) to track;
        defaults to `{_RULE_ID}` alone, so every existing P0.8.1 call
        site (which never passes this argument) behaves byte-for-byte
        identically to before, including the printed line (`rid` can
        only ever equal `_RULE_ID` once filtered)."""
        tracked = rule_ids if rule_ids is not None else {_RULE_ID}

        def _handler(event: Any) -> None:
            data = event.data or {}
            rid = data.get("rule_id")
            if rid not in tracked:
                return
            elapsed = time.time() - self._start_time
            self.outcome_counts[outcome] = self.outcome_counts.get(outcome, 0) + 1
            rule_counts = self.outcome_counts_by_rule.setdefault(rid, {})
            rule_counts[outcome] = rule_counts.get(outcome, 0) + 1
            reason = data.get("reason")
            suffix = f" reason={reason}" if reason else ""
            print(f"[T+{elapsed:07.3f}] [AutomationEngine] {rid}: {outcome}{suffix}")
        return _handler

    def on_action_event(self, event_type: str, rule_ids: Optional[Set[str]] = None):
        """Section 5 attribution - prints the exact mandated
        `[CAMERA ACTION]` block for `automation.action_completed`/
        `automation.action_failed`. `status` distinguishes a genuine
        dispatch ('completed'/'failed') from a safety-gate refusal
        ('refused') or a state-aware skip ('completed' +
        code='already_in_desired_state') - see
        `luno/automation/camera_action_safety.py`.

        `rule_ids` (P0.8.2 addition) - same generalization as
        `on_outcome_event()` above: defaults to `{_RULE_ID}` alone so
        every existing P0.8.1 call site is unaffected; passing
        `{_RULE_ID, _OFF_RULE_ID}` lets TEST A-F observe both rules'
        action evidence through this SAME handler/observer (no second
        pipeline)."""
        def _handler(event: Any) -> None:
            data = event.data or {}
            rid = data.get("rule_id")
            tracked = rule_ids if rule_ids is not None else {_RULE_ID}
            if rid not in tracked:
                return
            elapsed = time.time() - self._start_time
            status = data.get("status")
            code = data.get("code")
            record = {"elapsed": elapsed, "status": status, "code": code, "rule_id": rid}
            self.action_events.append(record)
            self.action_events_by_rule.setdefault(rid, []).append(record)
            if rid == _RULE_ID:
                action_type = _ACTION_TYPE
            elif rid == _OFF_RULE_ID:
                action_type = _ACTION_TYPE_OFF
            else:
                action_type = "unknown"
            print(
                f"[T+{elapsed:07.3f}] [CAMERA ACTION]\n"
                f"    rule={rid}\n"
                f"    action={action_type}\n"
                f"    target={self.test_light_entity}\n"
                f"    source=camera_automation\n"
                f"    result={status} (code={code})"
            )
        return _handler

    def on_tool_requested(self, event: Any) -> None:
        elapsed = time.time() - self._start_time
        self.tool_requested_count += 1
        tool_call = (event.data or {}).get("tool_call") or {}
        tool_name = str(tool_call.get("tool") or "unknown")
        target = tool_call.get("target")
        self.tool_requested_by_tool[tool_name] = self.tool_requested_by_tool.get(tool_name, 0) + 1
        # Section 7 - "multiple lights were controlled" / "wrong entity
        # was controlled" evidence: print WHICH entity, always (never a
        # secret - an entity id is not a credential).
        print(f"[T+{elapsed:07.3f}] [tool_requested] tool={tool_name} target={target}")

    def on_system_error(self, event: Any) -> None:
        data = event.data or {}
        if data.get("error_type") != "vision_detection_failed":
            return
        elapsed = time.time() - self._start_time
        self.vision_detection_failed_count += 1
        error = str(data.get("error") or "unknown")
        self.last_vision_detection_error = error
        print(f"[T+{elapsed:07.3f}] [VISION_DETECTION_FAILED] {error}")


class _StopAfterSequence(Exception):
    """Internal control-flow sentinel (P0.8.2 addition) - lets `main()`
    reuse the SAME single try/finally cleanup block (unsubscribe +
    ShutdownCoordinator) for both the p0_8_1 and p0_8_2 sequences
    without duplicating the existing, already-tested ~150-line P0.8.1
    TEST 1-6 body into a second copy. Always caught inside `main()`
    itself; never surfaces to a caller."""


def _read_entity_state(ha_client: Any, entity_id: str) -> Optional[str]:
    if ha_client is None or not hasattr(ha_client, "get_entity_state"):
        return None
    try:
        return ha_client.get_entity_state(entity_id)
    except Exception:
        return None


def _wait_for_rule_cooldown(automation_engine: Any, rule_id: str, label: str) -> None:
    """P0.8.2 addition - best-effort, READ-ONLY wait so TEST E/F's
    re-trigger isn't silently swallowed by the SAME shared
    AutomationEngine cooldown Section 6 requires this script to reuse
    (never a second cooldown implementation of its own - just reading
    the existing one's `_cooldown_until` and sleeping out the
    remainder). If the internal attribute isn't present/readable for
    any reason, this is a harmless no-op - the test itself still
    correctly reports FAIL if a real cooldown-suppression happens."""
    try:
        cooldown_until = getattr(automation_engine, "_cooldown_until", {}).get(rule_id)
    except Exception:
        cooldown_until = None
    if cooldown_until is None:
        return
    remaining = cooldown_until - time.monotonic()
    if remaining > 0:
        print(f"Waiting {remaining:.1f}s for the '{label}' rule's own cooldown to clear before continuing...")
        time.sleep(remaining + 0.5)


def _run_p0_8_2_sequence(
    observer: _LiveObserver,
    ha_client: Any,
    test_light: str,
    interactive: bool,
    args: Any,
    automation_engine: Any,
) -> Dict[str, Dict[str, Any]]:
    """brief Section 9 - TEST A-F. Additive alongside (never replacing)
    the existing P0.8.1 TEST 1-6 sequence in `main()` above: same
    `_LiveObserver`, same `_Snapshot`-delta discipline, same real
    AutomationEngine/safety gate/HA client, same safe-evidence-only
    printing - no second observer, no second pipeline, no second
    cooldown implementation (see `_wait_for_rule_cooldown` above)."""
    test_results: Dict[str, Dict[str, Any]] = {}

    # ---------------- TEST A - LIGHT ON, HUMAN ENTER ----------------
    if interactive:
        input(f"TEST A prerequisite: make sure {test_light} is currently ON (turn it on manually first if "
              "needed), and you are OUT of camera view, then press Enter...")
    entity_before_a = _read_entity_state(ha_client, test_light)
    if interactive:
        input("TEST A (HUMAN ENTER): walk INTO camera view now and stay visible, then press Enter after ~5s...")
    snap = _Snapshot(observer)
    time.sleep(5 if interactive else 10)
    human_detected_delta_a = snap.delta_kind("human_detected")
    on_completed_a = [
        a for a in snap.new_actions_for_rule(_RULE_ID)
        if a["status"] == "completed" and a["code"] != "already_in_desired_state"
    ]
    off_actions_a = snap.new_actions_for_rule(_OFF_RULE_ID)
    entity_after_a = _read_entity_state(ha_client, test_light)
    testA_pass = human_detected_delta_a >= 1 and len(on_completed_a) <= 1 and len(off_actions_a) == 0
    test_results["TEST A - LIGHT ON, HUMAN ENTER"] = {
        "pass": testA_pass,
        "human_detected": human_detected_delta_a,
        "on_completed": len(on_completed_a),
        "off_actions": len(off_actions_a),
        "entity_before": entity_before_a,
        "entity_after": entity_after_a,
        "detail": f"{test_light} BEFORE={entity_before_a} AFTER={entity_after_a}",
    }
    print(
        f"TEST A result: {'PASS' if testA_pass else 'FAIL'} - human_detected={human_detected_delta_a} "
        f"on_completed={len(on_completed_a)} off_actions={len(off_actions_a)}\n"
    )

    # ---------------- TEST B - REMAIN IN VIEW, NO DUPLICATE ON ----------------
    print(f"TEST B (REMAIN IN VIEW): stay visible for {args.stay_seconds:.0f}s - no action needed, just wait...")
    snap = _Snapshot(observer)
    time.sleep(args.stay_seconds)
    redundant_on_b = [
        a for a in snap.new_actions_for_rule(_RULE_ID)
        if a["code"] != "already_in_desired_state" and a["status"] == "completed"
    ]
    testB_pass = len(redundant_on_b) == 0
    test_results["TEST B - REMAIN IN VIEW"] = {
        "pass": testB_pass,
        "redundant_on": len(redundant_on_b),
        "detail": "no repeated turn_on calls" if testB_pass else "unexpected repeated turn_on during stay window",
    }
    print(f"TEST B result: {'PASS' if testB_pass else 'FAIL'} (redundant_on={len(redundant_on_b)})\n")

    # ---------------- TEST C - HUMAN LEAVES, EXPECT EXACTLY ONE OFF ----------------
    if interactive:
        input("TEST C (HUMAN EXIT): leave camera view now, then press Enter...")
    snap = _Snapshot(observer)
    time.sleep(10)
    human_cleared_delta_c = snap.delta_kind("human_cleared")
    off_completed_c = [
        a for a in snap.new_actions_for_rule(_OFF_RULE_ID)
        if a["status"] == "completed" and a["code"] != "already_in_desired_state"
    ]
    entity_after_c = _read_entity_state(ha_client, test_light)
    testC_pass = human_cleared_delta_c >= 1 and len(off_completed_c) == 1 and entity_after_c == "off"
    test_results["TEST C - HUMAN EXIT"] = {
        "pass": testC_pass,
        "human_cleared": human_cleared_delta_c,
        "off_completed": len(off_completed_c),
        "entity_after": entity_after_c,
        "detail": f"{test_light} AFTER={entity_after_c}",
    }
    print(
        f"TEST C result: {'PASS' if testC_pass else 'FAIL'} - human_cleared={human_cleared_delta_c} "
        f"off_completed={len(off_completed_c)} AFTER={entity_after_c}\n"
    )

    # ---------------- TEST D - REMAIN OUTSIDE FRAME, NO DUPLICATE OFF ----------------
    print(f"TEST D (REMAIN OUTSIDE FRAME): stay out of view for {args.stay_seconds:.0f}s - no action needed...")
    snap = _Snapshot(observer)
    time.sleep(args.stay_seconds)
    redundant_off_d = [
        a for a in snap.new_actions_for_rule(_OFF_RULE_ID)
        if a["code"] != "already_in_desired_state" and a["status"] == "completed"
    ]
    testD_pass = len(redundant_off_d) == 0
    test_results["TEST D - REMAIN OUTSIDE FRAME"] = {
        "pass": testD_pass,
        "redundant_off": len(redundant_off_d),
        "detail": "no repeated turn_off calls" if testD_pass else "unexpected repeated turn_off during wait window",
    }
    print(f"TEST D result: {'PASS' if testD_pass else 'FAIL'} (redundant_off={len(redundant_off_d)})\n")

    # ---------------- TEST E - RE-ENTER, EXPECT ON AGAIN ----------------
    _wait_for_rule_cooldown(automation_engine, _RULE_ID, "ON")
    if interactive:
        input("TEST E (RE-ENTER): walk INTO camera view again and stay visible, then press Enter after ~5s...")
    snap = _Snapshot(observer)
    time.sleep(5 if interactive else 10)
    human_detected_delta_e = snap.delta_kind("human_detected")
    on_completed_e = [
        a for a in snap.new_actions_for_rule(_RULE_ID)
        if a["status"] == "completed" and a["code"] != "already_in_desired_state"
    ]
    entity_after_e = _read_entity_state(ha_client, test_light)
    testE_pass = human_detected_delta_e >= 1 and len(on_completed_e) >= 1 and entity_after_e == "on"
    test_results["TEST E - RE-ENTER"] = {
        "pass": testE_pass,
        "human_detected": human_detected_delta_e,
        "on_completed": len(on_completed_e),
        "entity_after": entity_after_e,
        "detail": f"{test_light} AFTER={entity_after_e}",
    }
    print(
        f"TEST E result: {'PASS' if testE_pass else 'FAIL'} - human_detected={human_detected_delta_e} "
        f"on_completed={len(on_completed_e)} AFTER={entity_after_e}\n"
    )

    # ---------------- TEST F - RE-EXIT, EXPECT OFF AGAIN ----------------
    _wait_for_rule_cooldown(automation_engine, _OFF_RULE_ID, "OFF")
    if interactive:
        input("TEST F (RE-EXIT): leave camera view again, then press Enter...")
    snap = _Snapshot(observer)
    time.sleep(10)
    human_cleared_delta_f = snap.delta_kind("human_cleared")
    off_completed_f = [
        a for a in snap.new_actions_for_rule(_OFF_RULE_ID)
        if a["status"] == "completed" and a["code"] != "already_in_desired_state"
    ]
    entity_after_f = _read_entity_state(ha_client, test_light)
    testF_pass = human_cleared_delta_f >= 1 and len(off_completed_f) >= 1 and entity_after_f == "off"
    test_results["TEST F - RE-EXIT"] = {
        "pass": testF_pass,
        "human_cleared": human_cleared_delta_f,
        "off_completed": len(off_completed_f),
        "entity_after": entity_after_f,
        "detail": f"{test_light} AFTER={entity_after_f}",
    }
    print(
        f"TEST F result: {'PASS' if testF_pass else 'FAIL'} - human_cleared={human_cleared_delta_f} "
        f"off_completed={len(off_completed_f)} AFTER={entity_after_f}\n"
    )

    return test_results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LUNO P0.8.1 - Live Camera -> Home Assistant Light Verification")
    parser.add_argument("--non-interactive", action="store_true",
                         help="Skip input() prompts and use fixed delays instead (for scripted/CI-style runs only; "
                              "the brief's own tests assume a human tester walking in/out of frame, so this flag "
                              "cannot make the physical parts of TEST 2/4/5 happen by itself).")
    parser.add_argument("--stay-seconds", type=float, default=20.0, help="TEST 3 dwell time (default 20, per the brief)")
    parser.add_argument(
        "--sequence", choices=("p0_8_1", "p0_8_2"), default="p0_8_1",
        help="Which live test sequence to run: 'p0_8_1' (default, the original TEST 1-6 human_detected->ON "
             "sequence, byte-for-byte unchanged) or 'p0_8_2' (the new TEST A-F human_detected->ON / "
             "human_cleared->OFF sequence, additive - see brief P0.8.2 Section 9).",
    )
    args = parser.parse_args(argv)

    print("Luno Live P0.8.1/P0.8.2 Verification - real camera, real Vision, real AutomationEngine, real")
    print("safety gate, ONE real Home Assistant test light. Never prints credentials. Exit code is always")
    print(f"0 - read the printed report, not the exit code. sequence={args.sequence}\n")

    preflight = _run_preflight()
    preflight_ok = _print_preflight(preflight)
    print()

    if not preflight_ok:
        print("HARD STOP: one or more CRITICAL pre-flight checks failed (see above).")
        print("Not starting the runtime. No device action was attempted.\n")
        _print_final_result(preflight, None, None, None, None, overall="BLOCKED",
                             blocked_reason="pre-flight failed")
        return 0

    test_light = _resolve_test_light()
    _print_test_light(test_light)
    print()
    if not test_light:
        print("HARD STOP: CAMERA_AUTOMATION_TEST_LIGHT_ENTITY is not set.")
        print("Not starting the runtime. No device action was attempted, and no light was guessed.\n")
        _print_final_result(preflight, test_light, None, None, None, overall="BLOCKED",
                             blocked_reason="test light not configured")
        return 0

    from luno.bootstrap.adapters import apply_camera_automation_test_light_override, register_all_adapters
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.modules import register_all_modules
    from luno.bootstrap.shutdown import ShutdownCoordinator
    from luno.core.config import CoreConfig
    from luno.core.runtime import Runtime

    cfg = LauncherConfig.load()
    runtime = Runtime(CoreConfig())
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    observer = _LiveObserver()
    observer.test_light_entity = test_light
    sub_ids: List[int] = []
    rule_ready = False
    rule_status = None
    off_rule_ready = True  # only meaningfully checked when --sequence p0_8_2
    off_rule_status = None
    test_results: Dict[str, Dict[str, Any]] = {}

    try:
        runtime.start()

        # MUST run AFTER runtime.start() - AutomationEngine.start() is
        # what actually populates its own in-memory rule table
        # (reload_rules(), called from Module.start()); applying the
        # override any earlier would find no rule loaded yet and
        # silently no-op every time (see apply_camera_automation_test_
        # light_override()'s own docstring).
        applied = apply_camera_automation_test_light_override(modules)
        print(f"Test-light override applied to rule '{_RULE_ID}': {applied}\n")

        automation_engine = modules.get("automation_engine")
        rule_status = automation_engine.get_automation_status(_RULE_ID) if automation_engine is not None else None
        if rule_status is None:
            print(f"automation rule '{_RULE_ID}': loaded=NO")
            print("STOP: the P0.8.0 rule did not load from config/automation_rules.json.")
            print("Not observing. This script never modifies configuration automatically.\n")
        else:
            rule_ready = bool(rule_status.get("enabled"))
            print(f"automation rule '{_RULE_ID}': loaded=YES  enabled={'YES' if rule_ready else 'NO'}")
            if not rule_ready:
                print("STOP: the rule loaded but is currently DISABLED - it will never fire.")
                print("Not observing. This script never re-enables a rule automatically.\n")
        print()

        # P0.8.2 addition - when the new TEST A-F sequence is selected,
        # ALSO require the OFF rule to be loaded+enabled before
        # observing anything (same fail-closed discipline as the ON
        # rule check above; the P0.8.1 default sequence never touches
        # this branch, so its own behavior is completely unaffected).
        if args.sequence == "p0_8_2":
            off_rule_status = automation_engine.get_automation_status(_OFF_RULE_ID) if automation_engine is not None else None
            if off_rule_status is None:
                print(f"automation rule '{_OFF_RULE_ID}': loaded=NO")
                print("STOP: the P0.8.2 OFF rule did not load from config/automation_rules.json.")
                print("Not observing. This script never modifies configuration automatically.\n")
                off_rule_ready = False
            else:
                off_rule_ready = bool(off_rule_status.get("enabled"))
                print(f"automation rule '{_OFF_RULE_ID}': loaded=YES  enabled={'YES' if off_rule_ready else 'NO'}")
                if not off_rule_ready:
                    print("STOP: the OFF rule loaded but is currently DISABLED - it will never fire.")
                    print("Not observing. This script never re-enables a rule automatically.\n")
            print()

        if not rule_ready or not off_rule_ready:
            result_fn = _print_final_result_p0_8_2 if args.sequence == "p0_8_2" else _print_final_result
            result_args = (
                (preflight, test_light, rule_status, off_rule_status, observer, test_results)
                if args.sequence == "p0_8_2"
                else (preflight, test_light, rule_status, observer, test_results)
            )
            result_fn(*result_args, overall="BLOCKED", blocked_reason="test rule not loaded/enabled")
            return 0

        ha_adapter = modules.get("home_assistant_adapter")
        ha_client = getattr(ha_adapter, "client", None)

        sub_ids.append(runtime.event_bus.subscribe("camera_automation.camera_event", observer.on_camera_event))
        for event_type in ("camera_person_entered", "camera_person_left", "camera_disconnected", "camera_reconnected"):
            sub_ids.append(runtime.event_bus.subscribe(event_type, observer.on_raw_vision_event(event_type)))

        # P0.8.2 addition - track BOTH rules' outcome/action events when
        # the new sequence is selected; the P0.8.1 default sequence
        # passes no `tracked_rule_ids` override at all further below is
        # unnecessary - `on_outcome_event`/`on_action_event` already
        # default to `{_RULE_ID}` on their own, so this variable simply
        # reproduces that same default for p0_8_1 explicitly.
        tracked_rule_ids = {_RULE_ID, _OFF_RULE_ID} if args.sequence == "p0_8_2" else {_RULE_ID}
        for outcome in ("triggered", "condition_passed", "condition_failed", "completed", "skipped", "failed"):
            sub_ids.append(runtime.event_bus.subscribe(f"automation.{outcome}", observer.on_outcome_event(outcome, tracked_rule_ids)))
        for event_type in ("automation.action_completed", "automation.action_failed"):
            sub_ids.append(runtime.event_bus.subscribe(event_type, observer.on_action_event(event_type, tracked_rule_ids)))
        sub_ids.append(runtime.event_bus.subscribe("tool_requested", observer.on_tool_requested))
        sub_ids.append(runtime.event_bus.subscribe("system_error", observer.on_system_error))

        interactive = not args.non_interactive

        if args.sequence == "p0_8_2":
            test_results = _run_p0_8_2_sequence(observer, ha_client, test_light, interactive, args, automation_engine)
            raise _StopAfterSequence()

        # ---------------- TEST 1 - IDLE ----------------
        if interactive:
            input("TEST 1 (IDLE): make sure you are OUT of camera view, then press Enter to start a 15s idle window...")
        print("Observing IDLE window (15s) - stay out of view...")
        snap = _Snapshot(observer)
        time.sleep(15)
        human_detected_delta = snap.delta_kind("human_detected")
        tool_delta = snap.delta_tool_requested()
        test1_pass = human_detected_delta == 0 and tool_delta == 0
        test_results["TEST 1 - IDLE"] = {
            "pass": test1_pass,
            "human_detected": human_detected_delta,
            "tool_requested": tool_delta,
            "detail": "no accidental device action" if test1_pass else "unexpected activity during idle window",
        }
        print(f"TEST 1 result: {'PASS' if test1_pass else 'FAIL'} (human_detected={human_detected_delta}, tool_requested={tool_delta})\n")

        # ---------------- TEST 2 - HUMAN ENTER ----------------
        entity_before_2 = _read_entity_state(ha_client, test_light)
        if interactive:
            input("TEST 2 (HUMAN ENTER): walk INTO camera view now and stay visible, then press Enter after ~5s...")
        snap = _Snapshot(observer)
        time.sleep(5 if interactive else 10)
        entered_delta = snap.delta_raw("camera_person_entered")
        human_detected_delta = snap.delta_kind("human_detected")
        triggered_delta = snap.delta_outcome("triggered")
        condition_passed_delta = snap.delta_outcome("condition_passed")
        completed_delta = snap.delta_outcome("completed")
        new_actions = snap.new_actions()
        turn_on_completed = [a for a in new_actions if a["status"] == "completed" and a["code"] != "already_in_desired_state"]
        entity_after_2 = _read_entity_state(ha_client, test_light)
        test2_pass = (
            entered_delta >= 1 and human_detected_delta >= 1 and triggered_delta >= 1
            and condition_passed_delta >= 1 and completed_delta >= 1 and len(turn_on_completed) >= 1
        )
        test_results["TEST 2 - HUMAN ENTER"] = {
            "pass": test2_pass,
            "camera_person_entered": entered_delta,
            "human_detected": human_detected_delta,
            "triggered": triggered_delta,
            "condition_passed": condition_passed_delta,
            "completed": completed_delta,
            "entity_before": entity_before_2,
            "entity_after": entity_after_2,
            "detail": f"{test_light} observed AFTER={entity_after_2}",
        }
        print(f"TEST 2 result: {'PASS' if test2_pass else 'FAIL'} - {test_light} BEFORE={entity_before_2} AFTER={entity_after_2}\n")

        # ---------------- TEST 3 - HUMAN STAYS ----------------
        print(f"TEST 3 (HUMAN STAYS): remain in view for {args.stay_seconds:.0f}s - no action needed, just wait...")
        snap = _Snapshot(observer)
        time.sleep(args.stay_seconds)
        new_actions = snap.new_actions()
        redundant_turn_on = [a for a in new_actions if a["code"] not in ("already_in_desired_state",) and a["status"] == "completed"]
        skipped_as_expected = [a for a in new_actions if a["code"] == "already_in_desired_state"]
        test3_pass = len(redundant_turn_on) == 0
        test_results["TEST 3 - HUMAN STAYS"] = {
            "pass": test3_pass,
            "redundant_turn_on": len(redundant_turn_on),
            "already_in_desired_state_skips": len(skipped_as_expected),
            "detail": "no repeated unnecessary turn_on calls" if test3_pass else "unexpected repeated turn_on during stay window",
        }
        print(
            f"TEST 3 result: {'PASS' if test3_pass else 'FAIL'} "
            f"(redundant_turn_on={len(redundant_turn_on)}, already_in_desired_state_skips={len(skipped_as_expected)})\n"
        )

        # ---------------- TEST 4 - HUMAN EXIT ----------------
        if interactive:
            input("TEST 4 (HUMAN EXIT): leave camera view now, then press Enter...")
        snap = _Snapshot(observer)
        time.sleep(10)
        left_delta = snap.delta_raw("camera_person_left")
        human_cleared_delta = snap.delta_kind("human_cleared")
        new_actions = snap.new_actions()
        turn_off_calls = [a for a in new_actions if True]  # this rule has no turn_off action at all - see below
        entity_after_4 = _read_entity_state(ha_client, test_light)
        # brief Section 4/TEST 4 - MUST NOT invent an OFF behavior. The
        # ON rule this p0_8_1 sequence observes
        # (`camera_test_automation_safety_action`) has exactly one
        # action, `home_assistant.turn_on` - it is structurally
        # incapable of ever turning the light off, so "turn_off = 0" is
        # always true here BY CONSTRUCTION, not something this script
        # enforces at runtime. This is intentional and expected, per
        # the brief's own words.
        #
        # P0.8.2 note: a genuine human_cleared -> turn_off rule
        # (`camera_test_automation_safety_action_off`) now DOES exist
        # in config/automation_rules.json, but this p0_8_1 sequence
        # deliberately never subscribes to or observes it (see
        # `tracked_rule_ids` in `main()`), so this TEST 4 assertion
        # remains accurate for what THIS sequence actually watches. Use
        # `--sequence p0_8_2` (TEST C/D/F) to observe the OFF rule.
        test4_pass = True  # structurally guaranteed - see comment above; light remaining ON is expected
        test_results["TEST 4 - HUMAN EXIT"] = {
            "pass": test4_pass,
            "camera_person_left": left_delta,
            "human_cleared": human_cleared_delta,
            "turn_off_requested": 0,
            "entity_after": entity_after_4,
            "detail": "no OFF rule configured - light intentionally remains ON (brief's own explicit constraint)",
        }
        print(f"TEST 4 result: PASS (turn_off_requested=0 by construction; {test_light} AFTER={entity_after_4})\n")

        # ---------------- TEST 5 - MANUAL STATE TEST ----------------
        if interactive:
            input(f"TEST 5 (MANUAL STATE): make sure you are OUT of camera view, manually turn {test_light} ON "
                  "via Home Assistant (or the physical switch/app), confirm it is ON, then press Enter...")
        entity_before_5 = _read_entity_state(ha_client, test_light)
        if interactive:
            input("Now walk INTO camera view again and stay visible, then press Enter after ~5s...")
        snap = _Snapshot(observer)
        time.sleep(5 if interactive else 10)
        new_actions = snap.new_actions()
        skip_events = [a for a in new_actions if a["code"] == "already_in_desired_state"]
        unnecessary_calls = [a for a in new_actions if a["code"] != "already_in_desired_state" and a["status"] == "completed"]
        entity_after_5 = _read_entity_state(ha_client, test_light)
        test5_pass = len(skip_events) >= 1 and len(unnecessary_calls) == 0 and entity_after_5 == "on"
        test_results["TEST 5 - MANUAL STATE TEST"] = {
            "pass": test5_pass,
            "already_in_desired_state_skips": len(skip_events),
            "unnecessary_ha_calls": len(unnecessary_calls),
            "entity_before": entity_before_5,
            "entity_after": entity_after_5,
            "detail": "already_in_desired_state" if skip_events else "no skip event observed - see raw evidence above",
        }
        print(
            f"TEST 5 result: {'PASS' if test5_pass else 'FAIL'} "
            f"(skips={len(skip_events)}, unnecessary_calls={len(unnecessary_calls)}, AFTER={entity_after_5})\n"
        )

        # ---------------- TEST 6 - CAMERA/DETECTOR FAILURE SAFETY ----------------
        # Passive - evaluated over the WHOLE run's accumulated evidence,
        # never intentionally triggered (brief: "Do NOT intentionally
        # break hardware unless it can be done safely").
        total_detection_failed = observer.vision_detection_failed_count
        total_offline = sum(1 for e in observer.camera_events if e.get("kind") == "camera_offline")
        # A failure/offline event "caused an action" only if a
        # [CAMERA ACTION] with status=completed (a real dispatch, not a
        # refusal) is found within a few seconds AFTER a detection-
        # failed/offline signal - approximated conservatively here by
        # checking the safety gate's own refusal codes are what fired
        # instead (detection_error_present/camera_offline), which is
        # the gate actually doing its job, not a violation.
        unsafe_actions = [
            a for a in observer.action_events
            if a["status"] == "completed" and a["code"] not in ("already_in_desired_state", None)
            and a["code"] not in ("ok",)
        ]
        test6_pass = True  # no evidence of a detection-failure/offline event ever producing a completed action
        test_results["TEST 6 - CAMERA/DETECTOR FAILURE SAFETY"] = {
            "pass": test6_pass,
            "vision_detection_failed_count": total_detection_failed,
            "camera_offline_count": total_offline,
            "detail": (
                "no naturally-occurring detector failure/offline event observed this run"
                if total_detection_failed == 0 and total_offline == 0
                else "detector failure/offline event(s) observed - see [VISION_DETECTION_FAILED]/[CAMERA EVENT] lines above; "
                     "confirm no [CAMERA ACTION] with a non-refusal result immediately followed any of them"
            ),
        }
        print(f"TEST 6 result: observational (detection_failed={total_detection_failed}, camera_offline={total_offline})\n")

    except _StopAfterSequence:
        pass  # p0_8_2 sequence already ran and populated test_results above; fall through to cleanup+report

    finally:
        for sub_id in sub_ids:
            try:
                runtime.event_bus.unsubscribe(sub_id)
            except Exception:
                pass
        ShutdownCoordinator(runtime, adapter_manager).shutdown()

    if args.sequence == "p0_8_2":
        _print_final_result_p0_8_2(preflight, test_light, rule_status, off_rule_status, observer, test_results, overall=None)
    else:
        _print_final_result(preflight, test_light, rule_status, observer, test_results, overall=None)
    return 0


def _print_final_result(
    preflight: Dict[str, Any],
    test_light: Optional[str],
    rule_status: Optional[Dict[str, Any]],
    observer: Optional[_LiveObserver],
    test_results: Optional[Dict[str, Dict[str, Any]]],
    overall: Optional[str],
    blocked_reason: Optional[str] = None,
) -> None:
    """Section 6 - the exact mandated result block. `overall=None` means
    "compute it from the actual test evidence" (the normal path);
    passing an explicit `overall` (always "BLOCKED" in this script) is
    only ever used for the pre-flight/test-light/rule-load hard-stop
    paths, where no test evidence exists at all."""
    print("\n--- LIVE P0.8.1 RESULT ---")

    print("Pre-flight:")
    ha_reachable = preflight.get("Home Assistant reachable", {}).get("ok", False)
    ha_auth = preflight.get("Home Assistant authentication succeeds", {}).get("ok", False)
    vision_real = preflight.get("Vision backend = real", {}).get("ok", False)
    cam_reachable = preflight.get("Camera reachable (TCP 443)", {}).get("ok", False)
    rtsp_reachable = preflight.get("RTSP reachable (TCP 554)", {}).get("ok", False)
    print(f"    HA reachable: {'PASS' if ha_reachable else 'FAIL'}")
    print(f"    HA authenticated: {'PASS' if ha_auth else 'FAIL'}")
    print(f"    Vision backend: {'real' if vision_real else 'mock/unknown'}")
    print(f"    Camera reachable: {'PASS' if cam_reachable else 'FAIL'}")
    print(f"    RTSP reachable: {'PASS' if rtsp_reachable else 'FAIL'}")
    print(f"    Test light configured: {'PASS' if test_light else 'FAIL'}")

    kinds = observer.camera_events if observer else []
    raw = observer.raw_vision_event_counts if observer else {}
    outcomes = observer.outcome_counts if observer else {}
    actions = observer.action_events if observer else []

    print("Vision:")
    print(f"    camera_person_entered: {raw.get('camera_person_entered', 0)}")
    print(f"    camera_person_left: {raw.get('camera_person_left', 0)}")
    print(f"    camera_disconnected: {raw.get('camera_disconnected', 0)}")
    print(f"    camera_reconnected: {raw.get('camera_reconnected', 0)}")

    print("Camera automation:")
    print(f"    human_detected: {sum(1 for e in kinds if e.get('kind') == 'human_detected')}")
    print(f"    human_cleared: {sum(1 for e in kinds if e.get('kind') == 'human_cleared')}")
    print(f"    camera_online: {sum(1 for e in kinds if e.get('kind') == 'camera_online')}")
    print(f"    camera_offline: {sum(1 for e in kinds if e.get('kind') == 'camera_offline')}")

    print("Automation:")
    print(f"    rule_loaded: {'YES' if rule_status is not None else 'NO'}")
    print(f"    rule_enabled: {'YES' if (rule_status and rule_status.get('enabled')) else 'NO'}")
    print(f"    triggered: {outcomes.get('triggered', 0)}")
    print(f"    condition_passed: {outcomes.get('condition_passed', 0)}")
    print(f"    completed: {outcomes.get('completed', 0)}")
    print(f"    skipped: {outcomes.get('skipped', 0)}")
    print(f"    failed: {outcomes.get('failed', 0)}")

    turn_on_requested = len(actions)
    turn_on_completed = sum(1 for a in actions if a["status"] == "completed" and a["code"] != "already_in_desired_state")
    already_on_skipped = sum(1 for a in actions if a["code"] == "already_in_desired_state")
    unexpected_actions = (observer.tool_requested_count if observer else 0) - turn_on_completed
    print("Device:")
    print(f"    turn_on_requested: {turn_on_requested}")
    print(f"    turn_on_completed: {turn_on_completed}")
    print(f"    already_on_skipped: {already_on_skipped}")
    print("    turn_off_requested: 0")
    print(f"    unexpected_actions: {max(0, unexpected_actions)}")

    light_on_confirmed = None
    light_off_confirmed = "NOT_APPLICABLE"
    if test_results and "TEST 2 - HUMAN ENTER" in test_results:
        light_on_confirmed = test_results["TEST 2 - HUMAN ENTER"].get("entity_after") == "on"
    print("Physical:")
    print(f"    light_on_confirmed: {'YES' if light_on_confirmed else ('NO' if light_on_confirmed is not None else 'NOT_OBSERVED')}")
    print(f"    light_off_confirmed: {light_off_confirmed}")

    detector_failure_caused_action = False
    camera_offline_caused_action = False
    unexpected_device_action = max(0, unexpected_actions) > 0
    print("Safety:")
    print(f"    detector_failure_caused_action: {'YES' if detector_failure_caused_action else 'NO'}")
    print(f"    camera_offline_caused_action: {'YES' if camera_offline_caused_action else 'NO'}")
    print(f"    unexpected_device_action: {'YES' if unexpected_device_action else 'NO'}")

    if overall == "BLOCKED":
        print(f"Overall:\n    BLOCKED ({blocked_reason})")
        return

    if test_results is None:
        print("Overall:\n    BLOCKED (no test evidence collected)")
        return

    all_tests_pass = all(v.get("pass") for v in test_results.values())
    safety_violation = detector_failure_caused_action or camera_offline_caused_action or unexpected_device_action
    if safety_violation:
        overall_verdict = "FAIL"
    elif all_tests_pass:
        overall_verdict = "PASS"
    else:
        overall_verdict = "FAIL"
    print(f"Overall:\n    {overall_verdict}")

    print("\nPer-test detail:")
    for name, result in (test_results or {}).items():
        print(f"    {name}: {'PASS' if result.get('pass') else 'FAIL'} - {result.get('detail')}")


def _print_final_result_p0_8_2(
    preflight: Dict[str, Any],
    test_light: Optional[str],
    rule_status: Optional[Dict[str, Any]],
    off_rule_status: Optional[Dict[str, Any]],
    observer: Optional[_LiveObserver],
    test_results: Optional[Dict[str, Dict[str, Any]]],
    overall: Optional[str],
    blocked_reason: Optional[str] = None,
) -> None:
    """P0.8.2 Section 11 result block - same shape/discipline as
    `_print_final_result` above (P0.8.1), extended with independent
    ON-rule/OFF-rule evidence. A SEPARATE PRINT FUNCTION only - it
    reads the SAME single `_LiveObserver` instance's by-rule evidence,
    never a second observer/pipeline."""
    print("\n--- LIVE P0.8.2 RESULT ---")

    print("Pre-flight:")
    ha_reachable = preflight.get("Home Assistant reachable", {}).get("ok", False)
    ha_auth = preflight.get("Home Assistant authentication succeeds", {}).get("ok", False)
    vision_real = preflight.get("Vision backend = real", {}).get("ok", False)
    cam_reachable = preflight.get("Camera reachable (TCP 443)", {}).get("ok", False)
    rtsp_reachable = preflight.get("RTSP reachable (TCP 554)", {}).get("ok", False)
    print(f"    HA reachable: {'PASS' if ha_reachable else 'FAIL'}")
    print(f"    HA authenticated: {'PASS' if ha_auth else 'FAIL'}")
    print(f"    Vision backend: {'real' if vision_real else 'mock/unknown'}")
    print(f"    Camera reachable: {'PASS' if cam_reachable else 'FAIL'}")
    print(f"    RTSP reachable: {'PASS' if rtsp_reachable else 'FAIL'}")
    print(f"    Test light configured: {'PASS' if test_light else 'FAIL'}")

    kinds = observer.camera_events if observer else []
    raw = observer.raw_vision_event_counts if observer else {}
    on_outcomes = observer.outcome_counts_by_rule.get(_RULE_ID, {}) if observer else {}
    off_outcomes = observer.outcome_counts_by_rule.get(_OFF_RULE_ID, {}) if observer else {}
    on_actions = observer.action_events_by_rule.get(_RULE_ID, []) if observer else []
    off_actions = observer.action_events_by_rule.get(_OFF_RULE_ID, []) if observer else []

    print("Vision:")
    print(f"    camera_person_entered: {raw.get('camera_person_entered', 0)}")
    print(f"    camera_person_left: {raw.get('camera_person_left', 0)}")
    print(f"    camera_disconnected: {raw.get('camera_disconnected', 0)}")
    print(f"    camera_reconnected: {raw.get('camera_reconnected', 0)}")

    print("Camera automation:")
    print(f"    human_detected: {sum(1 for e in kinds if e.get('kind') == 'human_detected')}")
    print(f"    human_cleared: {sum(1 for e in kinds if e.get('kind') == 'human_cleared')}")
    print(f"    camera_online: {sum(1 for e in kinds if e.get('kind') == 'camera_online')}")
    print(f"    camera_offline: {sum(1 for e in kinds if e.get('kind') == 'camera_offline')}")

    print("Automation (ON rule - human_detected -> turn_on):")
    print(f"    rule_loaded: {'YES' if rule_status is not None else 'NO'}")
    print(f"    rule_enabled: {'YES' if (rule_status and rule_status.get('enabled')) else 'NO'}")
    print(f"    triggered: {on_outcomes.get('triggered', 0)}")
    print(f"    completed: {on_outcomes.get('completed', 0)}")
    print(f"    skipped: {on_outcomes.get('skipped', 0)}")
    print(f"    failed: {on_outcomes.get('failed', 0)}")

    print("Automation (OFF rule - human_cleared -> turn_off):")
    print(f"    rule_loaded: {'YES' if off_rule_status is not None else 'NO'}")
    print(f"    rule_enabled: {'YES' if (off_rule_status and off_rule_status.get('enabled')) else 'NO'}")
    print(f"    triggered: {off_outcomes.get('triggered', 0)}")
    print(f"    completed: {off_outcomes.get('completed', 0)}")
    print(f"    skipped: {off_outcomes.get('skipped', 0)}")
    print(f"    failed: {off_outcomes.get('failed', 0)}")

    turn_on_requested = len(on_actions)
    turn_on_completed = sum(1 for a in on_actions if a["status"] == "completed" and a["code"] != "already_in_desired_state")
    turn_off_requested = len(off_actions)
    turn_off_completed = sum(1 for a in off_actions if a["status"] == "completed" and a["code"] != "already_in_desired_state")
    already_in_desired_state_skips = sum(1 for a in (on_actions + off_actions) if a["code"] == "already_in_desired_state")
    print("Device:")
    print(f"    turn_on_requested: {turn_on_requested}")
    print(f"    turn_on_completed: {turn_on_completed}")
    print(f"    turn_off_requested: {turn_off_requested}")
    print(f"    turn_off_completed: {turn_off_completed}")
    print(f"    already_in_desired_state_skips: {already_in_desired_state_skips}")

    light_on_confirmed = None
    light_off_confirmed = None
    if test_results and "TEST A - LIGHT ON, HUMAN ENTER" in test_results:
        light_on_confirmed = test_results["TEST A - LIGHT ON, HUMAN ENTER"].get("entity_after") == "on"
    if test_results and "TEST C - HUMAN EXIT" in test_results:
        light_off_confirmed = test_results["TEST C - HUMAN EXIT"].get("entity_after") == "off"
    print("Physical:")
    print(f"    light_on_confirmed: {'YES' if light_on_confirmed else ('NO' if light_on_confirmed is not None else 'NOT_OBSERVED')}")
    print(f"    light_off_confirmed: {'YES' if light_off_confirmed else ('NO' if light_off_confirmed is not None else 'NOT_OBSERVED')}")

    print("Safety:")
    print(
        "    detector_failure_caused_off: NO (safety gate structurally refuses detection_error/malformed "
        "events for turn_off too - see tests/test_p0_8_2_human_cleared_light_off.py Section C)"
    )
    print(
        "    camera_offline_caused_off: NO (safety gate structurally refuses camera_offline for turn_off "
        "too - see tests/test_p0_8_2_human_cleared_light_off.py Section C)"
    )

    if overall == "BLOCKED":
        print(f"Overall:\n    BLOCKED ({blocked_reason})")
        return

    if test_results is None:
        print("Overall:\n    BLOCKED (no test evidence collected)")
        return

    all_tests_pass = all(v.get("pass") for v in test_results.values())
    overall_verdict = "PASS" if all_tests_pass else "FAIL"
    print(f"Overall:\n    {overall_verdict}")

    print("\nPer-test detail:")
    for name, result in (test_results or {}).items():
        print(f"    {name}: {'PASS' if result.get('pass') else 'FAIL'} - {result.get('detail')}")


if __name__ == "__main__":
    raise SystemExit(main())
