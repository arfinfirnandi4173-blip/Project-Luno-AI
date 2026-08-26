"""
ha_camera_discovery.py
========================

LUNO P0.5 / P0.5.1 (Real Camera Integration / Real Tapo C212 Entity
Discovery) - a small, STRICTLY READ-ONLY discovery script, run directly
on the machine where `HA_URL`/`HA_TOKEN` are actually configured:

    python ha_camera_discovery.py

This is the P0.5/P0.5.1 briefs' own "live discovery" step, which could
NOT be performed from the sandbox that built these sprints: this
sandbox has real `HA_URL`/`HA_TOKEN` values in `.env` (so the connection
is genuinely configured), but attempting the EXACT SAME connection this
script makes - using the project's own existing `luno.ha_client.
HomeAssistantClient` (read-only `connect()`/`get_states()`/registry
queries, the same class `luno/bootstrap/adapters.py`'s real HA backend
already uses - no second HA client, no new communication logic, no
modification to `ha_client.py` at all) - fails with `proxy rejected
connection: HTTP 403`, this sandbox's own outbound network policy, not
a Luno defect. See `docs/change_impact/camera_automation_p0_5_1.md`'s
own "HA DISCOVERY: BLOCKED" section for the full, honest record of that
attempt.

Running this script ON THE USER'S OWN MACHINE (where the sandbox's
network restriction does not apply) is the missing step.

--------------------------------------------------------------------
WHAT IT DOES (all strictly read-only - see P0.5.1 brief Section 4)
--------------------------------------------------------------------
1. Connects via the EXISTING `HomeAssistantClient` (unchanged).
2. Starts `listen_and_dispatch()` as a background task BEFORE calling
   any request/response command - this project's own `luno/
   ha_listener.py` already documents why: `pending_responses` is only
   ever filled in by the message loop `listen_and_dispatch()` runs, not
   by the request call itself. (P0.5's own first version of this script
   had a latent bug here - it called `get_states()` without ever
   starting that background listener, which would have made EVERY
   registry/state call silently time out even on a successful
   connection. Fixed this sprint - see Known Limitations in the P0.5.1
   change-impact doc for the honest note that this fix was never
   exercised against a live server, since the connection itself remains
   blocked in this sandbox.)
3. Calls `get_states()` (a single snapshot read - existing method).
4. Calls two read-only, standard Home Assistant websocket commands -
   `config/entity_registry/list` and `config/device_registry/list` -
   via a small local helper (`_send_and_wait`) that reuses the SAME
   connected client's own `ws`/`msg_id`/`pending_responses`/`call_lock`
   attributes `get_states()` itself already uses internally, generalized
   to any read-only command type. This is NOT a second HA client and
   does NOT modify `luno/ha_client.py` - it is composition over the
   existing public connection, mirroring `get_states()`'s own internal
   shape one level more generically. If the connected HA user account
   lacks admin rights, these commands may fail; that failure is handled
   gracefully (reported as "NOT AVAILABLE", never crashes, never treated
   as "camera not found" - see Section 14 of the P0.5.1 brief).
5. Cross-references entity -> device -> integration (via entity_registry
   and device_registry) to classify camera/motion/human/availability
   entities by REAL relationship, not name-guessing (P0.5.1 brief
   Section 5/7) - falling back to a broad keyword-only match (explicitly
   labeled UNCONFIRMED) only if registry data is unavailable.
6. Checks whether the existing `pytapo` PTZ integration's own
   `TAPO_HOST` (read from `luno.config`, unchanged) matches any
   discovered camera device's own HA `connections` list (a real,
   HA-documented per-device network-address field) - the strongest
   available evidence for "is this the same physical camera", never
   claimed CONFIRMED without it (Section 12).

--------------------------------------------------------------------
WHAT IT NEVER DOES
--------------------------------------------------------------------
Never calls `call_service()` (no light, switch, camera, or automation
is ever triggered - PTZ is never moved, no snapshot is ever taken).
Never reloads an integration or restarts HA. Never prints `HA_TOKEN` or
any other secret. Never writes to `config/camera_automation.json` or
any other file - discovery and configuration are deliberately kept
separate (Section 13); this script only prints a human-readable report
and a machine-readable JSON blob to stdout, for the operator to review
and copy from BY HAND.

Exit code is always 0 (this is a diagnostic, not a pass/fail check).
"""

from __future__ import annotations

import asyncio
import json as json_module
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from luno.config import HA_TOKEN, HA_WS_URL, TAPO_HOST, TAPO_USERNAME  # noqa: E402
from luno.ha_client import HomeAssistantClient  # noqa: E402

#: Superset keyword filter used ONLY as a fallback when registry data is
#: unavailable, and for the initial "which entities are worth cross-
#: referencing at all" pass - never used alone to make a final
#: CONFIRMED classification when registry data IS available (P0.5.1
#: brief Section 5: "Do NOT classify an entity as belonging to the Tapo
#: C212 merely because its name contains 'camera' or 'motion'").
_KEYWORDS = ("camera", "tapo", "c212", "motion", "person", "human", "presence", "occupancy", "onvif")

#: Device manufacturer/model substrings that count as real evidence of
#: a Tapo device in device_registry data (case-insensitive).
_TAPO_DEVICE_HINTS = ("tapo", "tp-link", "tplink")

#: device_class values HA itself defines for each role (documented HA
#: convention, not invented) - used only when a device relationship is
#: already known; never used to invent a device relationship.
_MOTION_DEVICE_CLASSES = ("motion",)
_HUMAN_DEVICE_CLASSES = ("occupancy", "presence")
_AVAILABILITY_DEVICE_CLASSES = ("connectivity",)


async def _send_and_wait(client: HomeAssistantClient, msg_type: str, timeout_s: float = 5.0) -> Optional[Any]:
    """Generic read-only request/response helper, reusing the SAME
    connected `client`'s own public attributes `get_states()` itself
    uses internally (`ws`/`msg_id`/`pending_responses`/`call_lock`) -
    zero changes to `luno/ha_client.py`. Returns `result` on success,
    `None` on failure/timeout/unsupported command (never raises)."""
    async with client.call_lock:
        try:
            if not client.connected:
                return None
            msg_id = client.msg_id
            client.msg_id += 1
            client.pending_responses[msg_id] = None
            await client.ws.send(json_module.dumps({"id": msg_id, "type": msg_type}))

            for _ in range(int(timeout_s / 0.1)):
                await asyncio.sleep(0.1)
                if msg_id in client.pending_responses and client.pending_responses[msg_id]:
                    result = client.pending_responses.pop(msg_id)
                    if result.get("success"):
                        return result.get("result")
                    return None

            client.pending_responses.pop(msg_id, None)
            return None
        except Exception:
            return None


def _matches_keywords(entity_id: str, friendly_name: str) -> bool:
    haystack = f"{entity_id} {friendly_name}".lower()
    return any(kw in haystack for kw in _KEYWORDS)


def _device_is_tapo(device: Dict[str, Any]) -> bool:
    text = f"{device.get('manufacturer') or ''} {device.get('model') or ''} {device.get('name') or ''}".lower()
    return any(hint in text for hint in _TAPO_DEVICE_HINTS)


def _build_report(
    states: List[Dict[str, Any]],
    entity_registry: Optional[List[Dict[str, Any]]],
    device_registry: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    states_by_entity = {s.get("entity_id"): s for s in states if s.get("entity_id")}

    registry_available = entity_registry is not None and device_registry is not None
    entity_to_device: Dict[str, str] = {}
    entity_to_platform: Dict[str, str] = {}
    if entity_registry:
        for row in entity_registry:
            eid = row.get("entity_id")
            if not eid:
                continue
            if row.get("device_id"):
                entity_to_device[eid] = row["device_id"]
            if row.get("platform"):
                entity_to_platform[eid] = row["platform"]

    devices_by_id: Dict[str, Dict[str, Any]] = {}
    if device_registry:
        for dev in device_registry:
            if dev.get("id"):
                devices_by_id[dev["id"]] = dev

    # Step 1 - find the strongest candidate "camera device" via the
    # entity -> device -> manufacturer/model relationship (Section 5).
    camera_entity_id: Optional[str] = None
    camera_device_id: Optional[str] = None
    camera_confirmed_via_registry = False

    if registry_available:
        for eid, state in states_by_entity.items():
            if not eid.startswith("camera."):
                continue
            device_id = entity_to_device.get(eid)
            device = devices_by_id.get(device_id) if device_id else None
            if device and _device_is_tapo(device):
                camera_entity_id, camera_device_id = eid, device_id
                camera_confirmed_via_registry = True
                break
        if camera_entity_id is None:
            # No device-confirmed Tapo camera - fall back to a
            # keyword-matched camera.* entity, explicitly unconfirmed.
            for eid, state in states_by_entity.items():
                if eid.startswith("camera.") and _matches_keywords(eid, str(state.get("attributes", {}).get("friendly_name", ""))):
                    camera_entity_id = eid
                    camera_device_id = entity_to_device.get(eid)
                    break
    else:
        for eid, state in states_by_entity.items():
            if eid.startswith("camera.") and _matches_keywords(eid, str(state.get("attributes", {}).get("friendly_name", ""))):
                camera_entity_id = eid
                break

    # Step 2 - motion/human/availability, restricted to the SAME device
    # when a camera_device_id is known (the strongest available
    # relationship); otherwise fall back to keyword-only matching
    # across binary_sensor/sensor/event domains, explicitly unconfirmed.
    motion_entity_id = human_entity_id = availability_entity_id = None
    motion_confirmed = human_confirmed = availability_confirmed = False

    def _same_device_candidates(domains: tuple) -> List[str]:
        if not (registry_available and camera_device_id):
            return []
        out = []
        for eid in states_by_entity:
            if entity_to_device.get(eid) == camera_device_id and any(eid.startswith(d + ".") for d in domains):
                out.append(eid)
        return out

    for eid in _same_device_candidates(("binary_sensor",)):
        dc = states_by_entity[eid].get("attributes", {}).get("device_class")
        if dc in _MOTION_DEVICE_CLASSES and motion_entity_id is None:
            motion_entity_id, motion_confirmed = eid, True
        elif dc in _HUMAN_DEVICE_CLASSES and human_entity_id is None:
            human_entity_id, human_confirmed = eid, True
        elif dc in _AVAILABILITY_DEVICE_CLASSES and availability_entity_id is None:
            availability_entity_id, availability_confirmed = eid, True

    if motion_entity_id is None:
        for eid, state in states_by_entity.items():
            if eid.startswith("binary_sensor.") and "motion" in eid.lower():
                motion_entity_id = eid
                break
    if human_entity_id is None:
        for eid, state in states_by_entity.items():
            if any(eid.startswith(d + ".") for d in ("binary_sensor", "sensor", "event")) and any(
                k in eid.lower() for k in ("person", "human")
            ):
                human_entity_id = eid
                break
    if availability_entity_id is None:
        for eid, state in states_by_entity.items():
            if eid.startswith("binary_sensor.") and states_by_entity[eid].get("attributes", {}).get("device_class") in _AVAILABILITY_DEVICE_CLASSES:
                availability_entity_id = eid
                break

    # Step 3 - pytapo relationship (Section 12). CONFIRMED only with a
    # real connections-list match; never guessed.
    pytapo_found = bool(TAPO_HOST and TAPO_USERNAME)
    same_physical_camera: Optional[bool] = None
    pytapo_evidence = None
    if pytapo_found and camera_device_id and devices_by_id.get(camera_device_id):
        connections = devices_by_id[camera_device_id].get("connections") or []
        for conn in connections:
            if len(conn) == 2 and TAPO_HOST and str(conn[1]) == str(TAPO_HOST):
                same_physical_camera = True
                pytapo_evidence = f"device connections list contains ({conn[0]!r}, {conn[1]!r}) matching TAPO_HOST"
                break
        if same_physical_camera is None:
            same_physical_camera = False  # camera device found, but no matching connection evidence

    def _entity_block(eid: Optional[str], confirmed: bool) -> Dict[str, Any]:
        if eid is None:
            return {"found": False, "entity_id": None, "confirmed_via_device_registry": False}
        state = states_by_entity.get(eid, {})
        return {
            "found": True,
            "entity_id": eid,
            "state": state.get("state"),
            "confirmed_via_device_registry": confirmed,
            "attributes": {
                k: v for k, v in (state.get("attributes") or {}).items()
                if k in ("friendly_name", "device_class", "supported_features")
            },
        }

    camera_device = devices_by_id.get(camera_device_id) if camera_device_id else None
    result: Dict[str, Any] = {
        "ha_reachable": True,
        "registry_available": registry_available,
        "camera": {
            **_entity_block(camera_entity_id, camera_confirmed_via_registry),
            "manufacturer": (camera_device or {}).get("manufacturer"),
            "model": (camera_device or {}).get("model"),
            "integration_platform": entity_to_platform.get(camera_entity_id) if camera_entity_id else None,
        },
        "motion": _entity_block(motion_entity_id, motion_confirmed),
        "human": _entity_block(human_entity_id, human_confirmed),
        "availability": _entity_block(availability_entity_id, availability_confirmed),
        "pytapo": {
            "found": pytapo_found,
            "tapo_host_configured": bool(TAPO_HOST),
            "same_physical_camera": same_physical_camera,
            "evidence": pytapo_evidence,
        },
    }
    return result


def _print_human_report(result: Dict[str, Any]) -> None:
    print("TAPO C212 HOME ASSISTANT DISCOVERY\n")
    print("Connection:")
    print(f"    HA reachable: {'YES' if result['ha_reachable'] else 'NO'}")
    print(f"    Entity/device registry available: {'YES' if result['registry_available'] else 'NO (falling back to name-based matching only - all classifications below are UNCONFIRMED)'}")

    def _print_block(title: str, block: Dict[str, Any]) -> None:
        print(f"\n{title}:")
        if not block.get("found"):
            print(f"    {title} entity: NOT FOUND")
            return
        print(f"    Entity ID: {block['entity_id']}")
        print(f"    State: {block.get('state')}")
        print(f"    Confirmed via device registry: {'YES' if block.get('confirmed_via_device_registry') else 'NO (name/keyword match only)'}")
        for k, v in (block.get("attributes") or {}).items():
            print(f"    {k}: {v}")

    _print_block("Camera", result["camera"])
    if result["camera"].get("found"):
        print(f"    Manufacturer: {result['camera'].get('manufacturer')}")
        print(f"    Model: {result['camera'].get('model')}")
        print(f"    Integration platform: {result['camera'].get('integration_platform')}")

    _print_block("Motion", result["motion"])
    _print_block("Human", result["human"])
    _print_block("Availability", result["availability"])
    if not result["availability"].get("found"):
        print("    Dedicated availability entity: NOT FOUND")

    print("\npytapo relationship:")
    print(f"    pytapo camera configured: {'FOUND' if result['pytapo']['found'] else 'NOT FOUND'}")
    print(f"    HA camera: {'FOUND' if result['camera'].get('found') else 'NOT FOUND'}")
    sp = result["pytapo"]["same_physical_camera"]
    print(f"    Same physical camera: {'CONFIRMED' if sp is True else ('UNCONFIRMED' if sp is False else 'UNKNOWN (insufficient data)')}")
    if result["pytapo"].get("evidence"):
        print(f"    Evidence: {result['pytapo']['evidence']}")

    print("\nLimitations:")
    print("    - No entity ID above was fabricated; every value came from a real HA API response this run.")
    print("    - Nothing was written to config/camera_automation.json - copy values across BY HAND after review.")
    if not result["registry_available"]:
        print("    - Registry commands were unavailable (unsupported or insufficient permission) - classification fell back to keyword matching only, which is inherently unconfirmed.")


def _print_blocked(reason: str) -> None:
    print("HA DISCOVERY: BLOCKED\n")
    print("Reason:")
    print(f"    {reason}\n")
    print("No entity IDs were modified or invented.")
    print(json_module.dumps({"ha_reachable": False, "reason": reason}, indent=2))


def main() -> int:
    print("Luno Home Assistant Camera Discovery (P0.5 / P0.5.1) - READ-ONLY, never calls a "
          "service, never triggers a device, never writes a file.\n")

    token_set = bool(HA_TOKEN)
    print(f"HA_WS_URL: {HA_WS_URL}")
    print(f"HA_TOKEN configured: {token_set}")
    # Deliberately never prints HA_TOKEN's actual value.

    if not token_set:
        _print_blocked("HA_TOKEN is not set via this project's own configuration mechanism (.env / environment) - nothing to discover.")
        return 0

    async def _run() -> int:
        client = HomeAssistantClient()
        ok = await client.connect()
        if not ok:
            _print_blocked(
                "connect() returned False - see the [HA] log line above for the underlying error "
                "(most commonly a network/firewall restriction on THIS machine, or an expired/"
                "incorrect HA_TOKEN). This is exactly what failed when this sprint's own "
                "development sandbox attempted the identical call - see docs/change_impact/"
                "camera_automation_p0_5_1.md."
            )
            return 0

        listen_task = asyncio.create_task(client.listen_and_dispatch(lambda data: None))
        try:
            states = await client.get_states()
            entity_registry = await _send_and_wait(client, "config/entity_registry/list")
            device_registry = await _send_and_wait(client, "config/device_registry/list")
        finally:
            listen_task.cancel()
            try:
                await listen_task
            except (asyncio.CancelledError, Exception):
                pass
            await client.disconnect()

        result = _build_report(states or [], entity_registry, device_registry)
        print("\nRESULT: CONNECTED\n")
        _print_human_report(result)
        print("\n--- JSON ---")
        print(json_module.dumps(result, indent=2, default=str))
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
