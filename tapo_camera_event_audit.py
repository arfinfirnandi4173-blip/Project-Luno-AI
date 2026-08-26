"""
tapo_camera_event_audit.py
============================

LUNO P0.5.2 (Tapo C212 Event Source Audit) - a small, STRICTLY READ-ONLY
diagnostic/probe for the EXISTING `pytapo` Tapo C212 integration, run
directly on the machine where `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` are actually configured:

    python tapo_camera_event_audit.py [--duration 30]

This is the P0.5.2 brief's own "read-only camera probe" (its Section 5),
built on top of the SAME `pytapo.Tapo(...)` construction pattern
`luno/bootstrap/adapters.py::_register_real_camera_ptz_handler` and
`tapo_ptz_diagnostic.py` (Sprint 70) already use - no second credential
mechanism, no second connection system. Answers one question only:
"where can Luno reliably obtain real Tapo C212 events, via the EXISTING
pytapo path?" - it does NOT decide an integration, does NOT touch
`config/camera_automation.json`, and does NOT wire anything into
`CameraAutomationModule` (P0.5.2 brief Section 14).

--------------------------------------------------------------------
WHAT IT DOES (all strictly read-only - see P0.5.2 brief Section 5)
--------------------------------------------------------------------
1. Constructs ONE real `pytapo.Tapo(host, user, password)` client - the
   same call `luno/bootstrap/adapters.py` already makes; this itself
   performs the library's own real, synchronous authentication (see
   `real_camera_ptz.py`'s own module docstring).
2. Calls a small, fixed set of READ-ONLY `pytapo` methods, confirmed by
   direct source inspection of the installed `pytapo` 3.4.18 package to
   only ever GET/query, never SET/control:
     - `getBasicInfo()`      - device info; a successful call is itself
                                evidence the camera is reachable/online.
     - `getMotionDetection()`- the camera's OWN firmware motion-
                                detection CONFIG (enabled/sensitivity) -
                                NOT a live event.
     - `getPersonDetection()`- the camera's OWN firmware AI person-
                                detection CONFIG (enabled/sensitivity) -
                                NOT a live event. Distinct capability
                                from motion (Section 8's own required
                                distinction).
     - `getAlertEventType()` - the list of alarm/notification types the
                                camera supports and whether each is
                                currently enabled (motion/person/pet/
                                vehicle/tamper/bark/meow/baby_cry/
                                glass_break/package/linecrossing, per
                                the installed library's own source -
                                exact set may vary by firmware).
     - `getEvents(start, end)` - queries the camera's OWN recorded
                                detection/playback log
                                (`searchDetectionList`) for a time
                                window - a genuine, evidence-based
                                POLLING event source (not push/
                                websocket - the installed library has
                                no such API, confirmed by source
                                inspection, see Section 4 in the
                                change-impact doc).
3. Time-limited observation window (`--duration`, default 30s, Section
   11): calls `getEvents()` once at the START of the window (a lookback
   query, so it also captures anything already-recorded before this
   script ran) and once again at the END, and reports the DIFFERENCE -
   any event whose `start_time` falls strictly after the first query's
   own request time is a NEW event observed DURING this run (never
   deliberately triggered - Section 10).
4. Classifies every capability using the closed set required by Section
   12: CONFIRMED / AVAILABLE-BUT-NOT-OBSERVED / NOT-AVAILABLE / UNKNOWN.

--------------------------------------------------------------------
WHAT IT NEVER DOES
--------------------------------------------------------------------
Never calls any `set*`/`play*`/`start*`/`stop*`/PTZ method
(`setMotionDetection`, `setPersonDetection`, `setAlarm`, `playAlarm`,
`startManualAlarm`, `stopManualAlarm`, `moveMotor`, `calibrateMotor`,
`savePreset`, `setPreset`, ...). Never restarts/reconfigures the camera.
Never prints `TAPO_PASSWORD`/`TAPO_USERNAME`/any session token - every
exception is passed through the EXISTING, already-tested
`real_camera_ptz._redact_credentials()` before being printed. Never
writes to `config/camera_automation.json` or any other file - stdout
only, for the operator to review and copy from BY HAND (Section 13/14).

Exit code is always 0 (this is a diagnostic, not a pass/fail check).
"""

from __future__ import annotations

import argparse
import json as json_module
import sys
import time
from typing import Any, Dict, List, Optional

import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import luno.config as legacy_config  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import (  # noqa: E402
    _redact_credentials,
    classify_tapo_exception,
)

#: Closed classification set - Section 12 of the P0.5.2 brief. Plain
#: string constants (matches this project's existing `TapoErrorClass`/
#: `PTZConnectionState` convention in `real_camera_ptz.py` - a free-form
#: string contract, not an Enum type).
CONFIRMED = "CONFIRMED"
AVAILABLE_BUT_NOT_OBSERVED = "AVAILABLE-BUT-NOT-OBSERVED"
NOT_AVAILABLE = "NOT-AVAILABLE"
UNKNOWN = "UNKNOWN"


def _safe_call(client: Any, method_name: str, *args: Any) -> Dict[str, Any]:
    """Calls a single READ-ONLY `pytapo` method by name, catching and
    classifying any failure the same way the production PTZ tool does
    (`classify_tapo_exception`) - never raises. Returns
    `{"ok": bool, "result": Any, "error": Optional[str], "error_class": Optional[str]}`.
    `error` is always passed through `_redact_credentials()` first."""
    try:
        method = getattr(client, method_name, None)
        if method is None:
            return {"ok": False, "result": None, "error": f"pytapo.Tapo has no method {method_name!r}", "error_class": "NOT_IMPLEMENTED"}
        result = method(*args)
        return {"ok": True, "result": result, "error": None, "error_class": None}
    except Exception as ex:  # noqa: BLE001 - deliberately broad, classified below
        classified = classify_tapo_exception(ex)
        return {"ok": False, "result": None, "error": _redact_credentials(str(ex)), "error_class": classified.category}


def _classify_config_capability(probe: Dict[str, Any]) -> str:
    """For a config-style GET (`getMotionDetection`/`getPersonDetection`/
    `getAlertEventType`) - CONFIRMED means the call succeeded AND
    returned a real structure (evidence the capability genuinely exists
    on this device/firmware). A clean failure with an evidence-based
    classification (e.g. the device doesn't support this detection type)
    is NOT-AVAILABLE only if the failure is unambiguous; otherwise
    UNKNOWN (never overstated - Section 12's own instruction)."""
    if probe["ok"] and probe["result"] is not None:
        return CONFIRMED
    if not probe["ok"] and probe["error_class"] in ("AUTH_FAILED", "HOST_UNREACHABLE", "PORT_UNREACHABLE", "DEVICE_OFFLINE", "AUTH_RATE_LIMITED", "SESSION_EXPIRED"):
        # Connection/auth-level failure - tells us nothing about whether
        # the CAPABILITY itself exists, so this is UNKNOWN, not
        # NOT-AVAILABLE (Section 12: "do not overstate results").
        return UNKNOWN
    if not probe["ok"]:
        # An API-level rejection while otherwise connected is the
        # strongest evidence this specific capability is absent.
        return NOT_AVAILABLE
    return UNKNOWN


def _build_report(
    connected: bool,
    connect_error: Optional[Dict[str, Any]],
    basic_info: Optional[Dict[str, Any]],
    motion_cfg: Optional[Dict[str, Any]],
    person_cfg: Optional[Dict[str, Any]],
    alert_types: Optional[Dict[str, Any]],
    events_before: Optional[Dict[str, Any]],
    events_after: Optional[Dict[str, Any]],
    observation_window_started_at: Optional[float],
    duration_s: float,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "pytapo_reachable": connected,
        "connect_error": connect_error,
        "capabilities": {},
        "live_observation": {
            "duration_s": duration_s,
            "events_observed": [],
            "events_not_observed": True,
            "errors": [],
        },
        "same_physical_camera_vs_home_assistant": {
            "status": "UNKNOWN",
            "reason": (
                "P0.5.1's own live HA discovery reported the Tapo C212 camera entity as "
                "NOT FOUND in Home Assistant (registry was reachable and searched, "
                "genuinely absent, not merely unreachable) - there is no HA-side camera "
                "device/connections evidence to compare TAPO_HOST against, so this cannot "
                "be resolved to CONFIRMED or UNCONFIRMED, only UNKNOWN (Section 13: do not "
                "claim same-camera merely from an IP address match that was never actually "
                "cross-checked against verified HA device metadata)."
            ),
        },
    }

    if not connected:
        for cap in ("camera_connection", "camera_status", "motion", "human_detection", "events", "availability"):
            result["capabilities"][cap] = {"result": UNKNOWN, "evidence": "pytapo connection was never established this run - see connect_error."}
        return result

    # Camera connection / camera status - a successful client construction
    # AND a successful getBasicInfo() call together are the strongest
    # available evidence pytapo can offer for "the camera is reachable
    # and responding" (Section 9's own "connection success/failure" and
    # "device status" mechanisms).
    result["capabilities"]["camera_connection"] = {
        "result": CONFIRMED,
        "evidence": "pytapo.Tapo(host, user, password) construction succeeded (performs real synchronous authentication).",
    }
    if basic_info and basic_info["ok"]:
        result["capabilities"]["camera_status"] = {
            "result": CONFIRMED,
            "evidence": "getBasicInfo() succeeded - device responded with real device info.",
            "device_info_keys": sorted(list(basic_info["result"].keys())) if isinstance(basic_info["result"], dict) else None,
        }
    else:
        result["capabilities"]["camera_status"] = {
            "result": UNKNOWN,
            "evidence": f"getBasicInfo() failed: {(basic_info or {}).get('error')}",
        }

    # Motion - CONFIG capability only, never a live event (Section 7).
    motion_class = _classify_config_capability(motion_cfg) if motion_cfg else UNKNOWN
    result["capabilities"]["motion"] = {
        "result": motion_class,
        "evidence": (
            "getMotionDetection() returned the camera's own firmware motion-detection "
            "configuration (enabled/sensitivity) - this confirms the CAPABILITY exists and "
            "is configurable, but pytapo has no live push/event-stream API for motion "
            "(confirmed by source inspection of the installed 3.4.18 package: no "
            "socket/subscribe/listen/callback method exists) - real-time motion events, if "
            "needed, would have to come from polling getEvents() instead (see 'events' "
            "below), never from this config call."
            if motion_class == CONFIRMED else f"getMotionDetection() call outcome: {motion_cfg}"
        ),
    }

    # Human/person detection - EXPLICITLY separate from motion (Section 8).
    person_class = _classify_config_capability(person_cfg) if person_cfg else UNKNOWN
    result["capabilities"]["human_detection"] = {
        "result": person_class,
        "evidence": (
            "getPersonDetection() returned the camera's own firmware AI person-detection "
            "configuration (enabled/sensitivity) - a REAL, distinct capability from generic "
            "motion (the C212's firmware itself distinguishes 'person_detection' from "
            "'motion_detection' as two separate config namespaces - confirmed by source "
            "inspection). Like motion, this is a CONFIG read, not a live event; person "
            "detection is NOT available through this interface as a push/event feed. Do "
            "not infer a live human-detected event merely because this capability is "
            "CONFIRMED enabled - see 'events' below for the actual event mechanism."
            if person_class == CONFIRMED else f"getPersonDetection() call outcome: {person_cfg}"
        ),
    }
    if alert_types and alert_types["ok"]:
        result["capabilities"]["human_detection"]["alert_event_types"] = alert_types["result"]

    # Events - the actual, evidence-based, POLL-based mechanism.
    if events_before and events_before["ok"]:
        before_list = events_before["result"] or []
        result["capabilities"]["events"] = {
            "result": CONFIRMED,
            "evidence": (
                f"getEvents() succeeded - queried the camera's own recorded detection log "
                f"(searchDetectionList) and got {len(before_list)} event(s) in the lookback "
                f"window. This is a POLLING mechanism (no push/websocket API exists in the "
                f"installed pytapo library - confirmed by source inspection), but it IS a "
                f"real, evidence-based event source: each returned entry has its own "
                f"start_time/end_time from the camera's own detection log, not invented."
            ),
        }
    else:
        result["capabilities"]["events"] = {
            "result": UNKNOWN if (events_before or {}).get("error_class") in ("AUTH_FAILED", "HOST_UNREACHABLE", "PORT_UNREACHABLE", "DEVICE_OFFLINE") else NOT_AVAILABLE,
            "evidence": f"getEvents() call outcome: {events_before}",
        }

    # Availability - Section 9. Best mechanism identified: connection
    # success/failure (camera_connection above) + getBasicInfo() device
    # status (camera_status above) are the safest, already-evidenced
    # signals; no permanent heartbeat is created this sprint.
    result["capabilities"]["availability"] = {
        "result": result["capabilities"]["camera_status"]["result"],
        "evidence": (
            "Safest identified mechanism: a successful pytapo.Tapo(...) construction plus a "
            "successful getBasicInfo() call together indicate camera_online; a connection- "
            "or auth-classified failure (see classify_tapo_exception categories already used "
            "in production by real_camera_ptz.py) indicates camera_offline. This sprint only "
            "IDENTIFIES this mechanism (Section 9) - it does not implement a permanent "
            "heartbeat/polling service."
        ),
    }

    # Live observation window - diff getEvents() before vs after.
    new_events: List[Dict[str, Any]] = []
    errors: List[str] = []
    if events_before and not events_before["ok"]:
        errors.append(f"initial getEvents() failed: {events_before['error']}")
    if events_after is not None:
        if events_after["ok"]:
            before_starts = {e.get("start_time") for e in ((events_before or {}).get("result") or [])}
            for ev in events_after["result"] or []:
                if ev.get("start_time") not in before_starts:
                    new_events.append(ev)
        else:
            errors.append(f"final getEvents() failed: {events_after['error']}")

    result["live_observation"]["events_observed"] = new_events
    result["live_observation"]["events_not_observed"] = len(new_events) == 0
    result["live_observation"]["errors"] = errors
    result["live_observation"]["window_started_at"] = observation_window_started_at

    return result


def _print_human_report(result: Dict[str, Any], duration_s: float) -> None:
    print("TAPO C212 EVENT SOURCE AUDIT (pytapo path)\n")
    print(f"pytapo reachable: {'YES' if result['pytapo_reachable'] else 'NO'}")
    if not result["pytapo_reachable"]:
        print(f"Connect error: {result.get('connect_error')}")
        print("\nNo entity/capability was fabricated - every row below reports UNKNOWN because")
        print("the connection itself never succeeded.")

    print("\nCapabilities:")
    print("| Capability        | Result                     |")
    print("|--------------------|-----------------------------|")
    for key, label in (
        ("camera_connection", "Camera connection"),
        ("camera_status", "Camera status"),
        ("motion", "Motion"),
        ("human_detection", "Human detection"),
        ("events", "Events"),
        ("availability", "Availability"),
    ):
        cap = result["capabilities"].get(key, {})
        print(f"| {label:<18} | {cap.get('result', UNKNOWN):<27} |")

    print("\nEvidence:")
    for key, label in (
        ("camera_connection", "Camera connection"),
        ("camera_status", "Camera status"),
        ("motion", "Motion"),
        ("human_detection", "Human detection"),
        ("events", "Events"),
        ("availability", "Availability"),
    ):
        cap = result["capabilities"].get(key, {})
        print(f"  [{label}] {cap.get('evidence')}")

    print(f"\nLive observation (duration {duration_s}s):")
    if result["live_observation"]["events_not_observed"]:
        print("    No event observed during observation window.")
    else:
        for ev in result["live_observation"]["events_observed"]:
            print(f"    [EVENT] source: pytapo  type: detection  start_time: {ev.get('start_time')}  end_time: {ev.get('end_time')}")
    for err in result["live_observation"]["errors"]:
        print(f"    [ERROR] {err}")

    print("\nSame physical camera vs Home Assistant:")
    print(f"    {result['same_physical_camera_vs_home_assistant']['status']} - {result['same_physical_camera_vs_home_assistant']['reason']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LUNO P0.5.2 - Tapo C212 Event Source Audit (read-only)")
    parser.add_argument("--duration", type=float, default=30.0, help="Observation window in seconds (default 30)")
    args = parser.parse_args(argv)

    print("Luno Tapo Camera Event Audit (P0.5.2) - READ-ONLY, never moves/configures the "
          "camera, never triggers events, never writes a file.\n")

    host_set = bool(legacy_config.TAPO_HOST)
    user_set = bool(legacy_config.TAPO_USERNAME)
    pass_set = bool(legacy_config.TAPO_PASSWORD)
    print(f"TAPO_HOST configured: {host_set}")
    print(f"TAPO_USERNAME configured: {user_set}")
    print(f"TAPO_PASSWORD configured: {pass_set}")
    # Deliberately never prints TAPO_HOST's actual value or the
    # credentials themselves - matches tapo_ptz_diagnostic.py's own
    # precedent exactly.

    if not (host_set and user_set and pass_set):
        print("\nRESULT: NOT_CONFIGURED")
        print("One or more of TAPO_HOST/TAPO_USERNAME/TAPO_PASSWORD is not set via this "
              "project's own configuration mechanism (.env / environment) - nothing to probe.")
        result = _build_report(False, {"error": "NOT_CONFIGURED"}, None, None, None, None, None, None, None, args.duration)
        print("\n--- JSON ---")
        print(json_module.dumps(result, indent=2, default=str))
        return 0

    try:
        from pytapo import Tapo
    except Exception as ex:
        # A distinct, honestly-labeled outcome from "connection failed" -
        # this is a Python import/dependency failure (this sandbox's own
        # installed `kasa`/`cryptography` versions are mutually
        # incompatible with the installed pytapo's own transport layer -
        # a genuine, reproducible environment issue, NOT evidence the
        # camera itself is unreachable or that the capability doesn't
        # exist). Never conflate the two (mirrors P0.5.1's own "HA
        # unreachable is not camera not found" discipline, Section 14).
        print(f"\nRESULT: IMPORT_FAILED")
        print(f"Reason: {_redact_credentials(str(ex))}")
        print("This is a Python dependency/import failure in THIS environment, not evidence")
        print("about the camera or about pytapo's own API capabilities - see the change-impact")
        print("doc's own 'Dependency Capability Audit' section (this sprint's Section 4) for the")
        print("full static-source-based capability findings, which do not require a successful")
        print("import to be evidence-based.")
        result = _build_report(False, {"error_class": "IMPORT_FAILED", "error": _redact_credentials(str(ex))}, None, None, None, None, None, None, None, args.duration)
        print("\n--- JSON ---")
        print(json_module.dumps(result, indent=2, default=str))
        return 0

    try:
        client = Tapo(legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    except Exception as ex:
        classified = classify_tapo_exception(ex)
        print(f"\nRESULT: CONNECT_FAILED ({classified.category})")
        print(f"Reason: {_redact_credentials(str(ex))}")
        result = _build_report(False, {"error_class": classified.category, "error": _redact_credentials(str(ex))}, None, None, None, None, None, None, None, args.duration)
        print("\n--- JSON ---")
        print(json_module.dumps(result, indent=2, default=str))
        return 0

    print("\nRESULT: CONNECTED\n")

    basic_info = _safe_call(client, "getBasicInfo")
    motion_cfg = _safe_call(client, "getMotionDetection")
    person_cfg = _safe_call(client, "getPersonDetection")
    alert_types = _safe_call(client, "getAlertEventType")

    window_started_at = time.time()
    events_before = _safe_call(client, "getEvents")
    print(f"Observing for {args.duration}s (read-only re-poll of getEvents() - never triggers motion)...")
    time.sleep(max(0.0, args.duration))
    events_after = _safe_call(client, "getEvents")

    result = _build_report(
        True, None, basic_info, motion_cfg, person_cfg, alert_types,
        events_before, events_after, window_started_at, args.duration,
    )
    _print_human_report(result, args.duration)
    print("\n--- JSON ---")
    print(json_module.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
