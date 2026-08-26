"""
luno_live_camera_event_observer.py
=====================================

LUNO P0.5.4-LIVE (Real Camera Proof-of-Life), extended by P0.6.1 (Live
Camera -> Automation Log-Only Verification) - run this DIRECTLY on the
real Luno machine, in its own existing `.venv`, where `TAPO_HOST`/the
Tapo C212's RTSP stream are actually reachable (this is NOT runnable
from an isolated development sandbox - see the "why this script exists"
note below):

    python luno_live_camera_event_observer.py [--duration 120]

--------------------------------------------------------------------
Why this script exists (read this before running)
--------------------------------------------------------------------
Every prior "live verification" attempt in this project (P0.5.1
through P0.5.4) was run from an isolated cloud development sandbox that
has NO network route to the user's home LAN, camera, or Home Assistant
instance - confirmed repeatedly by direct TCP/DNS probes, never
"assumed". That sandbox can read/write files in this repository, but
its own shell process cannot reach `TAPO_HOST` no matter what code runs
- this is a property of WHERE the shell executes, not of this
repository's code. This script is the deliverable that lets a human
run the actual live test on the machine that CAN reach the camera - it
was written and unit-tested from the sandbox, but was never (and could
not be) executed there against real hardware. Its own correctness for
non-hardware-dependent logic (argument parsing, sanitization, output
formatting) can still be verified without a camera - see `tests/
test_luno_live_camera_event_observer.py`.

--------------------------------------------------------------------
WHAT THIS DOES (all read-only / observation-only)
--------------------------------------------------------------------
1. PRE-FLIGHT (Section 3) - read-only checks, never prints credential
   values:
     - TAPO_HOST/TAPO_USERNAME/TAPO_PASSWORD configured (boolean only).
     - TCP reachability to TAPO_HOST on port 554 (RTSP) and 443.
     - `ultralytics` importable (the real YOLO pipeline's own
       dependency - see `luno/vision.py::_get_yolo()`).
     - `cv2` (OpenCV) importable.
   Reports PASS/FAIL per check, nothing else.

2. BOOTS THE REAL, EXISTING MODULE STACK - via the exact same
   `register_all_modules()`/`register_all_adapters()` every test file
   in this repo already uses (see `tests/test_p0_camera_automation.py`'s
   own `_build_stack()` for the precedent this mirrors). No new
   bootstrap path, no changes to any bootstrap file.

3. Sets `CAMERA_AUTOMATION_ENABLED=true` and
   `CAMERA_AUTOMATION_COOLDOWN_S=0` in THIS PROCESS's own `os.environ`
   ONLY, before constructing anything - never writes to `.env`, never
   touches `config/camera_automation.json`. This is exactly the
   existing, documented way to opt into Camera Automation (see
   `luno/camera_automation/config.py`'s own module docstring) - not a
   new mechanism.

4. Subscribes a TEMPORARY, PRINT-ONLY observer to the EXISTING
   `camera_automation.camera_event` event (the bridge's own normalized
   output - P0.5.3) and, for cross-checking the full trace Section 14
   asks for, also to the four raw Vision events
   (`camera_person_entered`/`camera_person_left`/`camera_disconnected`/
   `camera_reconnected`) - for those four, ONLY the event type and a
   timestamp are ever printed, NEVER `event.data` (which, for
   `camera_disconnected`/`camera_reconnected`, can contain
   `luno.vision.camera_status()["source"]` - the full, credentialed RTSP
   URL, see `luno/adapters/vision.py::on_camera_status()`). The
   `camera_automation.camera_event` payload itself is always safe to
   print in full - `CameraEvent.to_dict()` never contains a credential
   or URL (see `luno/camera_automation/cameras.py`).

5. Runs for `--duration` seconds (default 120, override with e.g.
   `--duration 300` for a longer walk-test), then cleanly shuts down via
   the EXISTING `ShutdownCoordinator` - unsubscribes the observer, stops
   every module/adapter this script itself started. Ctrl+C also
   triggers the same clean shutdown.

--------------------------------------------------------------------
P0.6.1 addition - AutomationEngine rule-match + device-action evidence
--------------------------------------------------------------------
P0.6 connected `camera_automation.camera_event` to the EXISTING
`AutomationEngine` (Sprint 72) via one log-only rule,
`camera_human_detected_log`. This script's own P0.5.4-LIVE observer had
no visibility into that layer at all - it only ever watched Vision/
CameraAutomation events, never whether `AutomationEngine` actually
matched or executed a rule, and never whether a device action was ever
requested. That visibility gap is exactly what P0.6.1's own Sections
7/9/10/12 require evidence for, so it is added HERE, in the SAME
script (not a second observer implementation, per that sprint's own
explicit instruction to reuse this file):

6. Before observing, calls the EXISTING, already-running `automation_
   engine.get_automation_status("camera_human_detected_log")` (a
   read-only public accessor - Sprint 72's own API, not new) and prints
   `loaded=YES/NO` / `enabled=YES/NO`. If the rule did not load, this
   script prints a clear STOP message and skips the observation loop
   entirely (Section 7: "STOP. Do not modify configuration
   automatically.") - it never edits `config/automation_rules.json` to
   force this to pass.

7. Subscribes (same temporary, print-only observer, same
   unsubscribe-on-exit discipline as every other subscription here) to
   `automation.triggered`/`automation.condition_passed`/`automation.
   completed`/`automation.skipped`/`automation.failed`, counting each
   ONLY for `rule_id == "camera_human_detected_log"` (every other rule
   id, if any is ever added later, is ignored by this script). These
   events are Sprint 72's own metadata-only payloads
   (`execution_id`/`rule_id`/`correlation_id`/`reason` - never a
   credential, frame, or raw exception) - always safe to print in full.

8. Subscribes to `tool_requested` and counts EVERY occurrence during the
   observation window, as the device-action safety proof Section 12
   asks for. Honest limitation, documented rather than glossed over:
   `automation.log` (this rule's only action) is structurally
   incapable of publishing `tool_requested` at all (`luno/automation/
   engine.py::_dispatch_internal_action()` never calls `_dispatch_tool_
   call()`), so THIS rule can never be the source of one - but a
   `tool_requested` observed during the window could originate from
   something else entirely unrelated to camera automation (e.g. a
   manual voice command given during the test). This script cannot
   attribute a `tool_requested` event back to a specific rule execution
   from the outside without deeper engine instrumentation this sprint
   was not asked to add - a nonzero count here means "cross-reference
   the console log around that timestamp," not "this rule caused a
   device action" (which is already structurally impossible for it).

9. Prints a final evidence block distinguishing THREE separate counts,
   exactly as Section 10 requires them to never be assumed equal:
   Vision raw event counts, `camera_automation.camera_event` kind
   counts, and AutomationEngine rule triggered/matched/executed/
   skipped/failed counts for `camera_human_detected_log` specifically -
   plus the `tool_requested` total as the device-action safety count.

--------------------------------------------------------------------
P0.6.2-FIX addition - runtime version evidence + detector-failure signal
--------------------------------------------------------------------
A real run of this script (the first in this project's history) showed
RTSP/camera-open succeeding but tracked-object detection failing with
`'Conv' object has no attribute 'bn'`. Investigation (see `docs/
change_impact/camera_automation_p0_6_2_fix.md` for the full audit) found
this script already uses the IDENTICAL production Vision runtime
main.py does (`LauncherConfig.load()` -> `register_all_adapters()` ->
`VisionAdapter` -> `RealVisionSource` -> `luno.vision.detect_objects_
tracked()`/`_get_yolo_tracking()`/`_get_yolo()` - there is no second,
duplicate Vision implementation anywhere in this file) - so "runtime
parity" was, in that sense, already true. What WAS missing:

10. This script now prints the ACTUAL runtime versions in use (Section
    6) - Python/executable, `ultralytics.__version__`, `torch.__version__`
    + CUDA availability, OpenCV version, and the resolved model paths -
    right after pre-flight, since this exact error signature is a known,
    already-documented-in-code (`luno/vision.py::_yolo_checkpoint_hint()`)
    symptom of a stale/mismatched local `.pt` checkpoint vs. the
    currently installed `ultralytics` package, and this sandbox has no
    way to read those values off the real machine itself.

11. `luno/vision.py::detect_objects_tracked()` already had an
    `except Exception: return []` "never raises" contract (Sprint 8,
    unchanged) - which meant a genuine detector failure was
    indistinguishable from "the model ran fine and legitimately saw
    nothing" (both produced an empty list, and downstream tracking/
    human-state code has no way to tell them apart). This script now
    subscribes to the new, additive `system_error` event
    `RealVisionSource` publishes specifically for this case
    (`error_type == "vision_detection_failed"`) and reports a distinct
    `VISION_DETECTION_FAILED` line with the sanitized error - never
    silently re-labelled as `human_cleared`/"no detection" (Section 13).

Camera automation logic, the AutomationEngine rules
(`camera_human_detected_log`/`camera_human_detected_test_action`), and
the Vision event *semantics* (`camera_person_entered`/`_left`/etc.) are
completely UNCHANGED by this addition - see that change-impact doc's
own "Diff Audit" section for the exact file list.

--------------------------------------------------------------------
WHAT THIS NEVER DOES
--------------------------------------------------------------------
Never modifies any file under `luno/`. Never modifies
`config/camera_automation.json`, `config/automation_rules.json`, `.env`,
or any other config file. Never calls a Home Assistant service
(`turn_on`/`turn_off`/`toggle`/scripts/automations/locks/alarms). Never
sends a PTZ command (`moveMotor`/`calibrateMotor`/`savePreset`/
`setPreset`). Never prints `TAPO_PASSWORD`/`TAPO_USERNAME`/an RTSP
URL/an HA token/any other secret.

Exit code is always 0 (this is a diagnostic, not a pass/fail check) -
read the printed report, not the exit code.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


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


def _run_preflight() -> Dict[str, Any]:
    import luno.config as legacy_config

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

    try:
        import ultralytics  # noqa: F401
        results["ultralytics (YOLO) importable"] = {"ok": True, "detail": "importable"}
    except Exception as ex:
        results["ultralytics (YOLO) importable"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    try:
        import cv2  # noqa: F401
        results["cv2 (OpenCV) importable"] = {"ok": True, "detail": f"importable ({cv2.__version__})"}
    except Exception as ex:
        results["cv2 (OpenCV) importable"] = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"}

    results["CAMERA_VISION_ENABLED"] = {"ok": legacy_config.CAMERA_VISION_ENABLED, "detail": str(legacy_config.CAMERA_VISION_ENABLED)}

    return results


def _print_runtime_versions() -> None:
    """P0.6.2-FIX Section 6 - print, never assume, the exact runtime this
    process is actually using. This is diagnostic-only output (never
    read by any code path, never affects behavior) - its only purpose is
    so a human comparing "why did main.py detect a person before but
    this script can't" has the ACTUAL versions in front of them, since
    this sandbox cannot run this script against the user's real
    environment to find out for them (see module docstring's "why this
    script exists"). Every value is read defensively - an import/attr
    failure here is reported as 'unknown', never raised."""
    import sys as _sys

    print("Runtime versions (Section 6):")
    print(f"    Python: {_sys.version.split()[0]}  ({_sys.executable})")

    try:
        import ultralytics
        print(f"    ultralytics: {getattr(ultralytics, '__version__', 'unknown')}")
    except Exception as ex:
        print(f"    ultralytics: NOT IMPORTABLE ({type(ex).__name__}: {ex})")

    try:
        import torch
        print(f"    torch: {getattr(torch, '__version__', 'unknown')}")
        try:
            cuda_available = bool(torch.cuda.is_available())
            print(f"    torch.cuda.is_available(): {cuda_available}")
            if cuda_available:
                print(f"    CUDA version (torch.version.cuda): {getattr(torch.version, 'cuda', 'unknown')}")
        except Exception as ex:
            print(f"    torch.cuda.is_available(): could not check ({type(ex).__name__}: {ex})")
    except Exception as ex:
        print(f"    torch: NOT IMPORTABLE ({type(ex).__name__}: {ex})")

    try:
        import cv2
        print(f"    OpenCV (cv2): {cv2.__version__}")
    except Exception as ex:
        print(f"    OpenCV (cv2): NOT IMPORTABLE ({type(ex).__name__}: {ex})")

    try:
        import luno.config as legacy_config
        print(f"    YOLO_MODEL_PATH: {legacy_config.YOLO_MODEL_PATH}")
        print(f"    YOLO_POSE_MODEL_PATH: {legacy_config.YOLO_POSE_MODEL_PATH}")
        print(f"    USE_GPU: {getattr(legacy_config, 'USE_GPU', 'unknown')}")
        print(f"    CONFIDENCE_THRESHOLD: {getattr(legacy_config, 'CONFIDENCE_THRESHOLD', 'unknown')}")
    except Exception as ex:
        print(f"    (could not read luno.config model settings: {type(ex).__name__}: {ex})")

    import os as _os
    print(f"    cwd: {_os.getcwd()}")
    print()


def _print_preflight(results: Dict[str, Any]) -> bool:
    print("Pre-flight:")
    all_critical_ok = True
    critical = {"TAPO_HOST configured", "TAPO_USERNAME configured", "TAPO_PASSWORD configured", "Camera reachable (TCP 443)", "RTSP reachable (TCP 554)", "ultralytics (YOLO) importable", "cv2 (OpenCV) importable"}
    for name, result in results.items():
        status = "PASS" if result["ok"] else "FAIL"
        print(f"    {name}: {status}  ({result['detail']})")
        if name in critical and not result["ok"]:
            all_critical_ok = False
    return all_critical_ok


#: P0.6.1 - the one rule this observer tracks AutomationEngine outcomes
#: for. Matches `config/automation_rules.json`'s own rule id exactly -
#: any other rule id is ignored by this script (Section 3: "use the
#: existing rule, do not create a second one").
_TRACKED_RULE_ID = "camera_human_detected_log"

#: P0.6.2 - the ONE new real-device rule this sprint adds, and the ONE
#: real, pre-existing, low-risk entity it targets. Both come straight
#: from `config/automation_rules.json`/`.env`'s own `RGB_LIGHT_ENTITY`
#: (see `luno/config.py`/`luno/devices.py`) - never invented. Kept here
#: only so this script can print/verify state for the same entity the
#: shipped rule already hardcodes; this script itself never chooses a
#: target - it only observes the one the rule config already names.
_TRACKED_HA_RULE_ID = "camera_human_detected_test_action"
_HA_TEST_ENTITY = "light.wled"


class _LiveObserver:
    """The temporary, print-only observer (Section 4). Never executes
    an action - `on_camera_event`/`on_raw_vision_event`/`on_automation_
    event`/`on_tool_requested` only ever `print(...)`. Kept as a small
    class purely so `main()` can hold one `events` list to summarize at
    the end - no state feeds back into any production module."""

    def __init__(self) -> None:
        self.camera_events: List[Dict[str, Any]] = []
        self.raw_vision_event_counts: Dict[str, int] = {}
        #: P0.6.1 - AutomationEngine outcome counts for `_TRACKED_RULE_ID`
        #: only (see module docstring's "P0.6.1 addition" section).
        self.automation_event_counts: Dict[str, int] = {}
        #: P0.6.1 - device-action safety proof (Section 12). See module
        #: docstring for the honest attribution limitation.
        self.tool_requested_count: int = 0
        #: P0.6.2 - AutomationEngine outcome counts for the NEW real-
        #: device rule (`_TRACKED_HA_RULE_ID`) only - kept entirely
        #: separate from `automation_event_counts` above so the P0.6.1
        #: log-only rule's own counting is untouched (Section 5: "the
        #: log-only rule must remain unchanged").
        self.ha_action_event_counts: Dict[str, int] = {}
        #: P0.6.2 Section 19 - device-action count broken down by which
        #: tool was requested, so "light.turn_on-equivalent HA calls"
        #: can be distinguished from "PTZ" and "other" - never inferred
        #: from a log message alone.
        self.tool_requested_by_tool: Dict[str, int] = {}
        #: P0.6.2-FIX Section 13 - count + last message for the NEW,
        #: additive `system_error` signal `RealVisionSource` publishes
        #: when `luno.vision.detect_objects_tracked()`'s own detector
        #: call fails (e.g. the `'Conv' object has no attribute 'bn'`
        #: stale-checkpoint signature - see `_yolo_checkpoint_hint()` in
        #: `luno/vision.py`). Kept entirely separate from
        #: `raw_vision_event_counts`/`camera_events` above - this is
        #: explicitly NOT a "no human detected" signal (Section 13: "do
        #: not report human_cleared... when inference itself failed" -
        #: this script never conflates the two; see `on_system_error`
        #: below and the final SUMMARY block for how they stay separate).
        self.vision_detection_failed_count: int = 0
        self.last_vision_detection_error: Optional[str] = None
        self._start_time = time.time()

    def on_camera_event(self, event: Any) -> None:
        data = dict(event.data or {})
        elapsed = time.time() - self._start_time
        self.camera_events.append(data)
        print(
            f"[T+{elapsed:07.3f}] [CAMERA EVENT]\n"
            f"    kind={data.get('kind')}\n"
            f"    camera_id={data.get('camera_id')}\n"
            f"    entity_id={data.get('entity_id')}\n"
            f"    confidence={data.get('confidence')}\n"
            f"    source={data.get('source')}\n"
            f"    timestamp={data.get('timestamp')}"
        )

    def on_raw_vision_event(self, event_type: str):
        def _handler(event: Any) -> None:
            # Deliberately NEVER touches event.data here - for
            # camera_disconnected/camera_reconnected specifically,
            # VisionAdapter's own data={"source": ...} can be the full,
            # credentialed RTSP URL (see module docstring). Only the
            # fact that this event type fired, and when, is ever
            # printed - sufficient to show the full Vision -> Bridge
            # trace Section 14 asks for without any leak risk.
            elapsed = time.time() - self._start_time
            self.raw_vision_event_counts[event_type] = self.raw_vision_event_counts.get(event_type, 0) + 1
            print(f"[T+{elapsed:07.3f}] [Vision] {event_type} observed (source/error omitted - see camera_automation.camera_event above for the safe, normalized version)")
        return _handler

    def on_automation_event(self, outcome: str):
        """P0.6.1 - `outcome` is one of `triggered`/`condition_passed`/
        `completed`/`skipped`/`failed` (the Sprint 72 event-type suffix
        after `automation.`). Only counts/prints for `_TRACKED_RULE_ID`
        - any other rule_id is silently ignored (there is only ever
        supposed to be the one rule this sprint line ships)."""
        def _handler(event: Any) -> None:
            data = event.data or {}
            if data.get("rule_id") != _TRACKED_RULE_ID:
                return
            elapsed = time.time() - self._start_time
            self.automation_event_counts[outcome] = self.automation_event_counts.get(outcome, 0) + 1
            reason = data.get("reason")
            suffix = f" reason={reason}" if reason else ""
            print(f"[T+{elapsed:07.3f}] [AutomationEngine] {_TRACKED_RULE_ID}: {outcome}{suffix}")
        return _handler

    def on_ha_action_event(self, outcome: str):
        """P0.6.2 - same idea as `on_automation_event`, but for the NEW
        real-device rule (`_TRACKED_HA_RULE_ID`) only, and additionally
        prints the exact Section 13 log line for `completed`/`failed`
        (rule/event/kind/action/target/result). Safe to hardcode
        `kind=human_detected`/`action=home_assistant.turn_on`/
        `target=<entity>` in the printed line - this rule has exactly
        one condition and one action, so a `completed`/`failed` for
        THIS rule id can only ever mean that combination; nothing is
        read from the event payload itself for those fields, and
        `event.data` here is otherwise only ever used for `rule_id`/
        `reason` (Sprint 72's own metadata-only fields - never a
        credential)."""
        def _handler(event: Any) -> None:
            data = event.data or {}
            if data.get("rule_id") != _TRACKED_HA_RULE_ID:
                return
            elapsed = time.time() - self._start_time
            self.ha_action_event_counts[outcome] = self.ha_action_event_counts.get(outcome, 0) + 1
            if outcome in ("completed", "failed"):
                reason = data.get("reason")
                result = "success" if outcome == "completed" else "failed"
                line = (
                    f"[T+{elapsed:07.3f}] [AUTOMATION.LOG] rule={_TRACKED_HA_RULE_ID} "
                    f"event=camera_automation.camera_event kind=human_detected "
                    f"action=home_assistant.turn_on target={_HA_TEST_ENTITY} result={result}"
                )
                if reason:
                    line += f" reason={reason}"
                print(line)
            else:
                reason = data.get("reason")
                suffix = f" reason={reason}" if reason else ""
                print(f"[T+{elapsed:07.3f}] [AutomationEngine] {_TRACKED_HA_RULE_ID}: {outcome}{suffix}")
        return _handler

    def on_tool_requested(self, event: Any) -> None:
        """P0.6.1/P0.6.2 - device-action safety proof (Section 12/19).
        Never prints the full `event.data` (a real `tool_requested`
        payload's nested `tool_call` can carry an HA entity id/PTZ
        target - not a secret, but not this script's job to echo in
        full either); only the tool name (to classify HA vs PTZ vs
        other, per Section 19) and a count. See module docstring's
        honest attribution limitation."""
        elapsed = time.time() - self._start_time
        self.tool_requested_count += 1
        tool_call = (event.data or {}).get("tool_call") or {}
        tool_name = str(tool_call.get("tool") or "unknown")
        self.tool_requested_by_tool[tool_name] = self.tool_requested_by_tool.get(tool_name, 0) + 1
        print(f"[T+{elapsed:07.3f}] [tool_requested] tool={tool_name} (count only - see module docstring's attribution limitation)")

    def on_system_error(self, event: Any) -> None:
        """P0.6.2-FIX Section 13 - filters the generic `system_error`
        event (published by several unrelated subsystems - supervisor,
        lifecycle, any `BaseAdapter` - see `luno/core/events.py`'s own
        `SystemError` docstring) down to ONLY the new, additive
        `error_type == 'vision_detection_failed'` signal
        `RealVisionSource` publishes (see `luno/adapters/real_vision.py`).
        Any other `system_error` (a different adapter, a module crash
        unrelated to Vision detection) is silently ignored by this
        method - this script's job is camera/vision evidence, not a
        general error monitor."""
        data = event.data or {}
        if data.get("error_type") != "vision_detection_failed":
            return
        elapsed = time.time() - self._start_time
        self.vision_detection_failed_count += 1
        error = str(data.get("error") or "unknown")
        self.last_vision_detection_error = error
        print(f"[T+{elapsed:07.3f}] [VISION_DETECTION_FAILED] {error}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LUNO P0.5.4-LIVE - Real Camera Proof-of-Life observer (read-only)")
    parser.add_argument("--duration", type=float, default=120.0, help="How long to observe, in seconds (default 120)")
    args = parser.parse_args(argv)

    print("Luno Live Camera Event Observer (P0.5.4-LIVE) - print-only, never executes actions "
          "ITSELF, never calls HA services ITSELF, never moves PTZ, never writes any file.\n")
    print(
        "IMPORTANT (P0.6.2): this script boots the REAL runtime, and "
        "config/automation_rules.json now ships a SECOND, real-device rule "
        f"('{_TRACKED_HA_RULE_ID}') that calls Home Assistant "
        f"'homeassistant.turn_on' on '{_HA_TEST_ENTITY}' (RGB Strip) whenever a "
        "real human_detected camera event matches - this is the runtime's OWN "
        "existing automation behavior, not something this observer script "
        "does. If you do not want that real device action to occur, disable "
        f"'{_TRACKED_HA_RULE_ID}' in config/automation_rules.json before "
        "running this script. Remember to restore the light to its prior "
        "state after testing (Section 20) - see the SUMMARY at the end.\n"
    )

    preflight = _run_preflight()
    ok = _print_preflight(preflight)
    print()
    _print_runtime_versions()

    if not ok:
        print("HARD STOP: one or more critical pre-flight checks failed (see above).")
        print("Not starting the runtime - per this sprint's own instruction, do not modify")
        print("networking/configuration/dependencies to force this to pass. Fix the underlying")
        print("issue (camera power/network/venv) and re-run this script.")
        return 0

    # Section 4/9 - opt into Camera Automation for THIS PROCESS ONLY,
    # via the exact existing, documented mechanism
    # (CameraAutomationConfig.from_env()) - never writes .env, never
    # touches config/camera_automation.json.
    os.environ["CAMERA_AUTOMATION_ENABLED"] = "true"
    os.environ.setdefault("CAMERA_AUTOMATION_COOLDOWN_S", "0")

    from luno.bootstrap.adapters import register_all_adapters
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.modules import register_all_modules
    from luno.bootstrap.shutdown import ShutdownCoordinator
    from luno.core.config import CoreConfig
    from luno.core.runtime import Runtime

    # P0.5.4-FIX root cause: main.py (line 66) resolves its config via
    # LauncherConfig.load() - the ONLY code path that calls load_dotenv()
    # and re-derives vision_backend from the now-populated environment
    # (VISION_BACKEND=real in .env). A bare LauncherConfig() constructor
    # never reads .env at all and silently keeps the hardcoded dataclass
    # default vision_backend="mock" - which is exactly why this script
    # was previously running against MockVisionSource (no RTSP, no YOLO,
    # zero real camera events) even though .env has VISION_BACKEND=real.
    # Using .load() here attaches this script to the SAME real Vision
    # lifecycle main.py already uses successfully - no other change is
    # needed.
    cfg = LauncherConfig.load()
    runtime = Runtime(CoreConfig())
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module = modules["camera_automation_module"]
    print(f"camera_automation.enabled: {cam_module.is_enabled()}")
    print(f"vision backend: {cfg.vision_backend}\n")
    if cfg.vision_backend != "real":
        print(
            "WARNING: vision backend resolved to "
            f"'{cfg.vision_backend}', not 'real' - this run will use "
            "MockVisionSource, not the real camera/YOLO pipeline. Check "
            "VISION_BACKEND in .env.\n"
        )

    observer = _LiveObserver()
    sub_ids = []
    rule_ready = False
    ha_rule_ready = False
    rule_status = None
    ha_rule_status = None
    entity_before = None
    entity_after = None
    try:
        runtime.start()

        # P0.6.1/P0.6.2 Section 7/17 - confirm BOTH rules loaded/enabled
        # BEFORE waiting for any camera event: the pre-existing log-only
        # rule (must remain enabled, Section 17 #9) and the new
        # real-device rule (Section 17 #8). Uses the engine's own
        # public, read-only status accessor (Sprint 72's own API) -
        # never reaches into a private attribute, never edits
        # config/automation_rules.json to force this to pass.
        automation_engine = modules.get("automation_engine")
        rule_status = automation_engine.get_automation_status(_TRACKED_RULE_ID) if automation_engine is not None else None
        ha_rule_status = automation_engine.get_automation_status(_TRACKED_HA_RULE_ID) if automation_engine is not None else None
        if rule_status is None:
            print(f"automation rule '{_TRACKED_RULE_ID}': loaded=NO")
            print("STOP: the P0.6 rule did not load from config/automation_rules.json.")
            print("Not observing - per this sprint's own instruction, this script never")
            print("modifies automation configuration automatically. Check that file and")
            print("re-run.\n")
        else:
            rule_ready = bool(rule_status.get("enabled"))
            print(f"automation rule '{_TRACKED_RULE_ID}': loaded=YES  enabled={'YES' if rule_ready else 'NO'}")
            if not rule_ready:
                print("STOP: the rule loaded but is currently DISABLED - it will never fire.")
                print("Not observing. This script never re-enables a rule automatically.\n")

        if ha_rule_status is None:
            print(f"automation rule '{_TRACKED_HA_RULE_ID}': loaded=NO")
            print("NOTE: the P0.6.2 real-device rule did not load. No real Home Assistant")
            print("action will occur this run (only the log-only rule, if it is ready).\n")
        else:
            ha_rule_ready = bool(ha_rule_status.get("enabled"))
            print(f"automation rule '{_TRACKED_HA_RULE_ID}': loaded=YES  enabled={'YES' if ha_rule_ready else 'NO'}")
            if not ha_rule_ready:
                print("NOTE: the real-device rule loaded but is DISABLED - no real HA action")
                print("will occur this run.\n")
        print()

        # P0.6.2 Section 17 #5/#6 - read the target entity's CURRENT
        # state before observing, if the bound HA client supports it
        # (best-effort, read-only; the MockHomeAssistantClient used when
        # HOME_ASSISTANT_BACKEND is not "real" has no such method at
        # all, so this is always guarded).
        entity_before = None
        ha_adapter = modules.get("home_assistant_adapter")
        ha_client = getattr(ha_adapter, "client", None)
        if ha_rule_ready and ha_client is not None and hasattr(ha_client, "get_entity_state"):
            try:
                entity_before = ha_client.get_entity_state(_HA_TEST_ENTITY)
                print(f"{_HA_TEST_ENTITY} state BEFORE: {entity_before}\n")
            except Exception as ex:
                print(f"{_HA_TEST_ENTITY} state BEFORE: could not read ({type(ex).__name__})\n")
        elif ha_rule_ready:
            print(f"{_HA_TEST_ENTITY} state BEFORE: not available (HOME_ASSISTANT_BACKEND is not 'real')\n")

        sub_ids.append(runtime.event_bus.subscribe("camera_automation.camera_event", observer.on_camera_event))
        for event_type in ("camera_person_entered", "camera_person_left", "camera_disconnected", "camera_reconnected"):
            sub_ids.append(runtime.event_bus.subscribe(event_type, observer.on_raw_vision_event(event_type)))
        # P0.6.1 - AutomationEngine outcome + device-action evidence.
        for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
            sub_ids.append(runtime.event_bus.subscribe(f"automation.{outcome}", observer.on_automation_event(outcome)))
        # P0.6.2 - same, for the new real-device rule.
        for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
            sub_ids.append(runtime.event_bus.subscribe(f"automation.{outcome}", observer.on_ha_action_event(outcome)))
        sub_ids.append(runtime.event_bus.subscribe("tool_requested", observer.on_tool_requested))
        # P0.6.2-FIX Section 13 - the new, additive detector-failure
        # signal (see `on_system_error` above).
        sub_ids.append(runtime.event_bus.subscribe("system_error", observer.on_system_error))

        if not rule_ready and not ha_rule_ready:
            print("Skipping the observation window (neither rule is loaded/enabled - see above).\n")
        else:
            print(f"Observing for {args.duration:.0f}s - walk in front of the camera now. Ctrl+C to stop early.\n")
            deadline = time.time() + args.duration
            try:
                while time.time() < deadline:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("\nStopped early by user.")

        # P0.6.2 Section 19 - read the target entity's state AFTER the
        # observation window, still inside the running runtime (must
        # happen before ShutdownCoordinator stops the HA adapter below).
        entity_after = entity_before
        if ha_rule_ready and ha_client is not None and hasattr(ha_client, "get_entity_state"):
            try:
                entity_after = ha_client.get_entity_state(_HA_TEST_ENTITY)
            except Exception:
                entity_after = None

    finally:
        # Section 15 - cleanup: unsubscribe the temporary observer,
        # cleanly stop everything this script itself started. Never
        # leaves a permanent debug subscriber behind.
        for sub_id in sub_ids:
            try:
                runtime.event_bus.unsubscribe(sub_id)
            except Exception:
                pass
        ShutdownCoordinator(runtime, adapter_manager).shutdown()

    # P0.6.1 Section 9/10 - the required evidence block, keeping every
    # layer's counts SEPARATE (Vision raw / camera_automation kind /
    # AutomationEngine outcome / device-action) rather than assuming any
    # two of them are equal.
    kinds = [e.get("kind") for e in observer.camera_events]
    print("\n--- LIVE P0.6.1 RESULT ---")
    print("Vision detector health (P0.6.2-FIX Section 13 - kept separate from")
    print("'no human in frame' below; a failure here means the detector itself")
    print("could not run this cycle, NOT that it looked and found no one):")
    print(f"    VISION_DETECTION_FAILED count: {observer.vision_detection_failed_count}")
    if observer.vision_detection_failed_count > 0:
        print(f"    last error: {observer.last_vision_detection_error}")
        if observer.raw_vision_event_counts.get("camera_person_left", 0) > 0:
            print(
                "    CAUTION: camera_person_left/human_cleared ALSO fired during this "
                "window - cross-check timestamps above. A detector failure right after "
                "a person was tracked can look like 'they left' when the real cause is "
                "that detection stopped working, not that the room emptied."
            )
    print("Vision (raw, count only):")
    for event_type in ("camera_person_entered", "camera_person_left", "camera_disconnected", "camera_reconnected"):
        print(f"    {event_type}: {observer.raw_vision_event_counts.get(event_type, 0)}")
    print("camera_automation.camera_event:")
    for kind in ("human_detected", "human_cleared", "camera_online", "camera_offline"):
        print(f"    {kind}: {kinds.count(kind)}")
    print(f"automation rule ('{_TRACKED_RULE_ID}'):")
    print(f"    loaded: {'YES' if rule_status is not None else 'NO'}")
    print(f"    enabled: {'YES' if rule_ready else 'NO'}")
    for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
        print(f"    {outcome}: {observer.automation_event_counts.get(outcome, 0)}")
    print(f"automation rule ('{_TRACKED_HA_RULE_ID}'):")
    print(f"    loaded: {'YES' if ha_rule_status is not None else 'NO'}")
    print(f"    enabled: {'YES' if ha_rule_ready else 'NO'}")
    for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
        print(f"    {outcome}: {observer.ha_action_event_counts.get(outcome, 0)}")
    print("device actions:")
    print(f"    tool_requested total (any tool): {observer.tool_requested_count}")
    for tool_name, count in observer.tool_requested_by_tool.items():
        label = tool_name
        if tool_name == "home_assistant":
            label = "home_assistant (light.turn_on-equivalent HA service calls)"
        elif tool_name in ("camera_ptz", "camera_patrol"):
            label = f"{tool_name} (PTZ actions)"
        print(f"    {label}: {count}")
    print("    See module docstring for the per-rule attribution limitation - a nonzero")
    print("    tool_requested count cannot be proven to have come from a specific rule")
    print("    from outside the engine, though automation.log itself remains structurally")
    print("    incapable of ever producing one (see the log-only rule above).")
    print(f"{_HA_TEST_ENTITY} state:")
    print(f"    BEFORE: {entity_before}")
    print(f"    AFTER:  {entity_after}")
    if ha_rule_ready and entity_before is not None and entity_after is not None and entity_before != entity_after:
        print(
            f"    NOTE (Section 20): state changed during this run. If this was not "
            f"intentional, restore '{_HA_TEST_ENTITY}' to '{entity_before}' now via the "
            "existing, safe HA mechanism (e.g. the Luno voice/text light command, or "
            "the Home Assistant UI directly) - this script does not do this for you."
        )

    if not observer.camera_events and not observer.raw_vision_event_counts:
        print("\nNo event of any kind was observed during this window.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
