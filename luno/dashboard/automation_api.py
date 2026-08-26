"""
automation_api.py
====================

P0.12 (Automation API & CRUD) - the Dashboard-facing HTTP surface for
managing `AutomationEngine` rule definitions (list/get/create/update/
delete/enable/disable/run/validate), consumed directly by `server.py`'s
`_dispatch_get`/`_dispatch_post` (the `/api/automations*` route family)
and, in a future sprint (P0.13, explicitly NOT started by this one), by
the Dashboard's own automation editor UI.

Same architectural role `collectors.py`/`controls.py` already play for
every other dashboard panel (see this package's own `__init__.py`
docstring) - a THIN translation layer, never a second source of truth:

    HTTP request -> THIS FILE -> AutomationEngine (existing, unmodified
                                  in its own core dispatch/execution
                                  logic) -> ToolManager -> Home Assistant

Every function here either reads via `AutomationEngine.list_rules()`/
`get_rule()`/`get_status()`/`get_automation_status()` or calls one of
`AutomationEngine.create_rule()`/`update_rule()`/`delete_rule()`/
`enable_automation()`/`disable_automation()`/`run_automation()` - all
five of the mutating calls are EXISTING (four pre-existing, two new-
this-sprint) engine methods that already do their own validation,
locking, and persistence. This file adds NO new device-control path, NO
new persistence mechanism, and NO second `AutomationEngine` - it only
translates between the engine's own `{"ok": bool, "code": str,
"message": str, ...}` internal contract (unchanged, still used by
`AutomationToolHandler`/the voice-command path/every existing test) and
the JSON response CONTRACT the P0.12 brief specifies for the new HTTP
endpoints (`{"success": bool, ...}` / `{"valid": bool, "errors": [...]}`)
- a presentation-layer concern, kept OUT of the engine itself, the same
separation `collectors.py`/`controls.py` already established for every
other panel.

Validation errors are never leaked as Python tracebacks - every
`AutomationRuleError` raised by the existing `models.py::rule_from_dict()`/
`validate_rule()` (never a second, parallel validator) is caught and
translated into ONE structured `{"field": ..., "code": ..., "message":
...}` entry (see `_error_entry()` - honest about being a best-effort
classification of a single, fail-fast error message, not a rewrite of
the underlying validator into a multi-error-collecting one, which
`validate_rule()` was never designed to be and this sprint was not asked
to change).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..automation.models import (
    ACTION_TYPES,
    AutomationRuleError,
    CONDITION_TYPES,
    MAX_COOLDOWN_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_DESCRIPTION_LENGTH,
    SEQUENCE_STEP_TYPES,
    TRIGGER_TYPES,
    rule_from_dict,
    validate_rule,
)

#: P0.13 (Automation Dashboard) - the schema endpoint's own "known
#: event name" suggestions. These mirror (never import - see module
#: docstring's "no new cross-package coupling" note below) the real
#: constants already defined elsewhere: `CAMERA_EVENT_TYPE` in
#: `luno/camera_automation/module.py`, and `ROOM_OCCUPIED_EVENT_TYPE`/
#: `ROOM_VACANT_EVENT_TYPE`/`OCCUPANCY_CHANGED_EVENT_TYPE` in
#: `luno/vision_occupancy.py`. Purely a UI autocomplete HINT - the
#: server-side validator (`models.py::validate_trigger()`) never
#: restricts an event trigger's `event_name` to this list; a user may
#: still type any custom event name and it will validate exactly the
#: same as before this sprint.
_KNOWN_EVENT_NAME_HINTS = frozenset({
    "camera_automation.camera_event",
    "room_occupied",
    "room_vacant",
    "occupancy_changed",
})

#: P0.13 - mirrors `CAMERA_EVENT_KINDS` in `luno/camera_automation/
#: cameras.py` (`motion_detected`/`motion_cleared`/`human_detected`/
#: `human_cleared`/`camera_online`/`camera_offline`) PLUS the two P0.8.6
#: kinds `VisionCameraEventBridge` (`luno/camera_automation/vision_
#: bridge.py`) additionally publishes (`human_confirmed`/`human_
#: unconfirmed`) - a UI hint for the `event.kind` condition target only,
#: same "suggestion, not a restriction" caveat as above.
_KNOWN_CAMERA_EVENT_KIND_HINTS = frozenset({
    "motion_detected", "motion_cleared",
    "human_detected", "human_cleared",
    "camera_online", "camera_offline",
    "human_confirmed", "human_unconfirmed",
})

#: P0.13 - mirrors the `occupancy.*` `state_readers` keys registered in
#: `luno/bootstrap/modules.py` (`occupancy.state`/`occupancy.person_
#: count`/`occupancy.presence_duration_seconds`/`occupancy.occupancy_
#: age_seconds`/`occupancy.last_transition`), plus the `event.*` fields
#: actually used by the real shipped `config/automation_rules.json`
#: (`event.kind`/`event.available`/`event.detection_error`/`event.
#: person_count`) - both are read via the EXISTING `event.<field>`/
#: `state_readers` condition-resolution mechanism in `luno/automation/
#: conditions.py`, never a new one. A UI hint only; any other
#: `state_readers`-registered or `event.<field>` target still validates
#: exactly as before this sprint.
_KNOWN_CONDITION_TARGET_HINTS = frozenset({
    "occupancy.state", "occupancy.person_count", "occupancy.presence_duration_seconds",
    "occupancy.occupancy_age_seconds", "occupancy.last_transition",
    "event.kind", "event.available", "event.detection_error", "event.person_count",
})


def _known_devices() -> List[Dict[str, str]]:
    """P0.13 Phase 5 - the ONE minimal, additive API surface the brief's
    own device/action picker required: reads the SAME, already-loaded
    `luno.devices.LIGHTS`/`SWITCHES` registries the voice/text command
    path already resolves entity names from (`luno/devices.py`'s own
    module docstring - loaded once at import from `config/lights.
    config.json`/`config/switches.config.json`), never a live Home
    Assistant call and never a fabricated discovery mechanism. Imported
    lazily (inside the function, not at module top) purely to keep this
    dashboard-layer module's top-level import list unchanged for every
    other function - `luno.devices` itself has no dashboard dependency,
    so this is not a real circular-import risk, just a minimal-footprint
    choice."""
    from .. import devices as _devices

    out: List[Dict[str, str]] = []
    for name, cfg in _devices.LIGHTS.items():
        entity_id = cfg.get("entity_id") if isinstance(cfg, dict) else None
        if entity_id:
            out.append({"name": name, "entity_id": str(entity_id), "domain": "light"})
    for name, entity_id in _devices.SWITCHES.items():
        if entity_id:
            out.append({"name": name, "entity_id": str(entity_id), "domain": "switch"})
    # P0.14 - `luno.devices.SCRIPTS` (same, already-loaded registry - see
    # this function's own P0.13 docstring above) added so the schema
    # endpoint's flat device list also covers `home_assistant.run_script`/
    # the new `entity_id` picker fields, not just `turn_on`/`turn_off`'s
    # `target` field.
    for name, cfg in _devices.SCRIPTS.items():
        entity_id = cfg.get("entity_id") if isinstance(cfg, dict) else cfg
        if entity_id:
            out.append({"name": name, "entity_id": str(entity_id), "domain": "script"})

    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for d in out:
        if d["entity_id"] in seen:
            continue
        seen.add(d["entity_id"])
        deduped.append(d)
    return sorted(deduped, key=lambda d: (d["domain"], d["name"]))


#: P0.14 Section 10 - `GET /api/automations/devices`'s own honest
#: "we genuinely have no local registry for these" list. Confirmed by
#: inspection (Phase 0 of this sprint), not assumed: `luno.devices` only
#: exposes `LIGHTS`/`SWITCHES`/`SCRIPTS` (backed by real `config/*.
#: config.json` files), and `RealHomeAssistantClient` has no
#: `get_states()`-equivalent anywhere in this project to enumerate ANY
#: other domain live. Populating these with fabricated entries would
#: violate the brief's own "Do not invent fake devices" instruction.
_DEVICE_CATEGORIES_WITHOUT_LOCAL_REGISTRY = ("fans", "climate", "media_players", "sensors", "scenes", "other")


def get_devices(modules: Dict[str, Any], adapter_manager: Any = None) -> Dict[str, Any]:
    """P0.14 Section 10 - `GET /api/automations/devices`. A categorized
    device/entity listing for the visual step builder's entity picker,
    separate from (but reusing the exact same source data as)
    `_known_devices()`/`get_schema()`'s own flat `devices` list above -
    this endpoint exists because Section 10 asks for a category-grouped
    shape (Lights/Switches/Scripts/... ) the flat list doesn't provide.

    Only `lights`/`switches`/`scripts` are ever populated - the SAME,
    already-loaded `luno.devices.LIGHTS`/`SWITCHES`/`SCRIPTS` registries
    (never a live Home Assistant call, never a fabricated discovery
    mechanism - same reasoning `_known_devices()` above already
    documents). Every other category (`fans`/`climate`/`media_players`/
    `sensors`/`scenes`/`other`) is returned as a genuinely empty list -
    this project has no local registry AND no live-discovery mechanism
    for any of them (see `_DEVICE_CATEGORIES_WITHOUT_LOCAL_REGISTRY`'s
    own comment) - never a single fabricated entry to make the picker
    look more complete than it honestly is. `ha_connected` reports
    whether a REAL (not mock) Home Assistant client is currently bound -
    a UI hint only, never gates what a user can type manually."""
    from .. import devices as _devices

    ha_connected = False
    if adapter_manager is not None:
        registry = getattr(adapter_manager, "registry", None)
        adapter = registry.get("home_assistant") if registry is not None else None
        client = getattr(adapter, "client", None)
        # Type-name comparison (not isinstance/import) - deliberately
        # keeps this dashboard-layer module free of any import-time
        # dependency on `luno.adapters.real_home_assistant`, the same
        # "lazy import, minimal footprint" choice `_known_devices()`
        # above already makes for `luno.devices`.
        ha_connected = type(client).__name__ == "RealHomeAssistantClient"

    def _dedupe_sorted(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        seen: set = set()
        out: List[Dict[str, str]] = []
        for item in sorted(items, key=lambda d: d["name"]):
            if item["entity_id"] in seen:
                continue
            seen.add(item["entity_id"])
            out.append(item)
        return out

    lights = [
        {"name": name, "entity_id": str(cfg.get("entity_id"))}
        for name, cfg in _devices.LIGHTS.items()
        if isinstance(cfg, dict) and cfg.get("entity_id")
    ]
    switches = [
        {"name": name, "entity_id": str(entity_id)}
        for name, entity_id in _devices.SWITCHES.items() if entity_id
    ]
    scripts = []
    for name, cfg in _devices.SCRIPTS.items():
        entity_id = cfg.get("entity_id") if isinstance(cfg, dict) else cfg
        if entity_id:
            scripts.append({"name": name, "entity_id": str(entity_id)})

    categories: Dict[str, List[Dict[str, str]]] = {
        "lights": _dedupe_sorted(lights),
        "switches": _dedupe_sorted(switches),
        "scripts": _dedupe_sorted(scripts),
    }
    for empty_category in _DEVICE_CATEGORIES_WITHOUT_LOCAL_REGISTRY:
        categories[empty_category] = []

    return {
        "ha_connected": ha_connected,
        "categories": categories,
        "unavailable_categories": list(_DEVICE_CATEGORIES_WITHOUT_LOCAL_REGISTRY),
        "note": (
            "Lights, switches, and scripts come from this project's own local "
            "device registry (config/lights.config.json, switches.config.json, "
            "scripts.config.json) and are always available. Fans, climate, media "
            "players, sensors, and scenes have no local registry or live "
            "discovery mechanism in this project - enter their entity id "
            "manually (e.g. 'scene.movie_mode')."
        ),
    }


def get_schema(modules: Dict[str, Any]) -> Dict[str, Any]:
    """P0.13 Phase 6/7 - `GET /api/automations/schema`. A pure, read-
    only reflection of `models.py`'s own EXISTING closed allowlists
    (`TRIGGER_TYPES`/`CONDITION_TYPES`/`ACTION_TYPES`/`SEQUENCE_STEP_
    TYPES`) plus the known device registry and a handful of UI-hint
    suggestion lists (see the module-level constants above) - so the
    Dashboard's trigger/condition/action pickers are always built FROM
    the engine's real, current allowlists rather than a second,
    hand-typed copy that could silently drift out of sync. Also unions
    in every `event_name` actually used by a CURRENTLY LOADED rule (via
    `AutomationEngine.list_rules()`, read-only) - a real, live-derived
    suggestion, not a guess. Zero persistence/runtime side effects -
    this function only reads."""
    engine = (modules or {}).get("automation_engine")
    known_event_names = set(_KNOWN_EVENT_NAME_HINTS)
    if engine is not None:
        for rule in engine.list_rules():
            trig = rule.get("trigger") or {}
            if trig.get("type") == "event":
                name = (trig.get("parameters") or {}).get("event_name")
                if name:
                    known_event_names.add(str(name))
    return {
        "trigger_types": sorted(TRIGGER_TYPES),
        "condition_types": sorted(CONDITION_TYPES),
        "action_types": sorted(ACTION_TYPES),
        "sequence_step_types": sorted(SEQUENCE_STEP_TYPES),
        "cooldown_seconds_max": MAX_COOLDOWN_SECONDS,
        "delay_seconds_max": MAX_DELAY_SECONDS,
        "description_max_length": MAX_DESCRIPTION_LENGTH,
        "known_event_names": sorted(known_event_names),
        "known_camera_event_kinds": sorted(_KNOWN_CAMERA_EVENT_KIND_HINTS),
        "known_condition_targets": sorted(_KNOWN_CONDITION_TARGET_HINTS),
        "devices": _known_devices(),
    }

#: P0.12 - a NEW-rule id supplied by an API caller must be safe to use
#: BOTH as a JSON object key (already unconstrained on-disk) and as a
#: literal URL path segment (`/api/automations/{id}/...`) - this is a
#: stricter, API-BOUNDARY-ONLY constraint, deliberately NOT pushed down
#: into `models.py::validate_rule()`'s own `id` check (which stays
#: exactly as permissive as before this sprint, so any existing,
#: hand-authored rule id in `config/automation_rules.json` - none of
#: which were ever required to match this pattern - keeps loading
#: without any migration).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: P0.12 - best-effort classification of a single `AutomationRuleError`
#: message into a `field` name for the API's structured error contract
#: (see module docstring). Checked in order; the FIRST pattern that
#: matches wins, mirroring `validate_rule()`'s own fail-fast, first-
#: error-wins internal order (id -> name -> trigger -> conditions ->
#: actions/sequence -> cooldown -> description) as closely as a
#: message-text classifier reasonably can without parsing the
#: validator's internals.
_FIELD_PATTERNS: List[Any] = [
    (re.compile(r"rule id must not be empty"), "id"),
    (re.compile(r"rule name must not be empty"), "name"),
    (re.compile(r"trigger"), "trigger"),
    (re.compile(r"too many conditions|condition (requires|type)"), "conditions"),
    (re.compile(r"sequence too long"), "sequence"),
    (re.compile(r"not define both 'actions' and 'sequence'|at least one action|too many actions"), "actions"),
    (re.compile(r"action (type|requires|'delay_seconds')"), "actions"),
    (re.compile(r"cooldown_seconds out of range"), "cooldown_seconds"),
    (re.compile(r"description"), "description"),
]

_SEQUENCE_STEP_RE = re.compile(r"sequence step (\d+)")


def _classify_field(message: str) -> str:
    # `sequence step N` is checked FIRST and separately (not via the
    # generic pattern list) because it needs to capture and interpolate
    # the step index into the returned field name (`sequence[N]`), not
    # just match a fixed field name like every other pattern below.
    step_match = _SEQUENCE_STEP_RE.search(message)
    if step_match:
        return f"sequence[{step_match.group(1)}]"
    for pattern, field_name in _FIELD_PATTERNS:
        if pattern.search(message):
            return field_name
    return "rule"


def _error_entry(message: str) -> Dict[str, Any]:
    return {"field": _classify_field(message), "code": "invalid_value", "message": message}


def _validate_new_id(rule_id: str) -> Optional[str]:
    """Returns an error message string if `rule_id` is unsafe to use as
    a new automation id, else `None`. Only applies to a CALLER-SUPPLIED
    id at CREATE time - an auto-generated id (via `AutomationEngine.
    create_rule()`'s own `generate_id()` fallback) never goes through
    this check, and an EXISTING rule's id (update/delete/enable/
    disable/run) is looked up as-is, never re-validated against this
    stricter pattern."""
    if not _ID_RE.match(rule_id):
        return (
            f"invalid automation id {rule_id!r} - must be 1-64 characters, "
            f"letters/digits/underscore/hyphen only"
        )
    return None


# ============================================================================
# Reads
# ============================================================================

def list_automations(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The full DTO (Phase 3's own schema) for every loaded rule, each
    merged with its live runtime `status` sub-object - reuses
    `AutomationEngine.list_rules()` (full definitions, incl. `sequence`/
    `description`/timestamps) AND `get_status()` (running/cooldown/
    last_execution), zipped by id, rather than adding a third,
    duplicate-shaped engine method. A rule with no matching status entry
    (should not happen - both come from the same `self._rules` snapshot,
    but engine internals are never assumed stable across two separate
    calls under concurrent mutation) degrades to `status: None` rather
    than raising."""
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return []
    definitions = engine.list_rules()
    status_by_id = {s["id"]: s for s in engine.get_status()}
    out = []
    for rule in definitions:
        merged = dict(rule)
        status = status_by_id.get(rule["id"])
        merged["status"] = (
            {
                "running": status["running"],
                "cooldown_remaining_s": status["cooldown_remaining_s"],
                "last_execution": status["last_execution"],
            }
            if status is not None else None
        )
        out.append(merged)
    return out


def get_automation(modules: Dict[str, Any], automation_id: str) -> Optional[Dict[str, Any]]:
    """Single-resource GET - `None` means "not found" (the caller,
    `server.py`, turns that into an HTTP 404 - see that file's own
    routing for `/api/automations/{id}`)."""
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return None
    rule = engine.get_rule(automation_id)
    if rule is None:
        return None
    merged = dict(rule)
    status = engine.get_automation_status(automation_id)
    merged["status"] = (
        {
            "running": status["running"],
            "cooldown_remaining_s": status["cooldown_remaining_s"],
            "last_execution": status["last_execution"],
        }
        if status is not None else None
    )
    return merged


# ============================================================================
# Validate-without-saving (Phase 8) - a PURE function, zero engine/
# persistence/runtime interaction of any kind. Deliberately does not even
# accept `modules` - there is nothing here that could touch persistence
# or runtime state even by accident, which is the strongest possible
# proof of Phase 8's own "zero side effects" requirement (test T).
# ============================================================================

def validate_automation(body: Dict[str, Any]) -> Dict[str, Any]:
    rule_id = str(body.get("id") or "validate-preview")
    try:
        rule = rule_from_dict(rule_id, body if isinstance(body, dict) else {})
        validate_rule(rule)
    except AutomationRuleError as ex:
        return {"valid": False, "errors": [_error_entry(str(ex))]}
    except Exception as ex:  # pragma: no cover - defensive, never leak a traceback
        return {"valid": False, "errors": [{"field": "rule", "code": "malformed_payload", "message": str(ex)}]}
    return {"valid": True, "errors": []}


# ============================================================================
# Mutations (Phases 5-7) - each is a thin translation of an EXISTING
# AutomationEngine method's `{"ok", "code", "message", ...}` result into
# the brief's own `{"success": bool, ...}` HTTP contract.
# ============================================================================

def create_automation(modules: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "errors": [{"field": "rule", "code": "unavailable", "message": "automation engine is not available"}]}
    if not isinstance(body, dict):
        return {"success": False, "errors": [{"field": "rule", "code": "malformed_payload", "message": "request body must be a JSON object"}]}
    supplied_id = body.get("id")
    rule_id: Optional[str] = None
    if supplied_id:
        rule_id = str(supplied_id)
        id_error = _validate_new_id(rule_id)
        if id_error is not None:
            return {"success": False, "errors": [{"field": "id", "code": "invalid_value", "message": id_error}]}
    result = engine.create_rule(body, rule_id=rule_id)
    if not result["ok"]:
        return {"success": False, "errors": [_error_entry(result["message"])]}
    return {"success": True, "automation": result["rule"]}


def update_automation(modules: Dict[str, Any], automation_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "errors": [{"field": "rule", "code": "unavailable", "message": "automation engine is not available"}]}
    if not isinstance(body, dict):
        return {"success": False, "errors": [{"field": "rule", "code": "malformed_payload", "message": "request body must be a JSON object"}]}
    result = engine.update_rule(automation_id, body)
    if not result["ok"]:
        code = "not_found" if result["code"] == "unknown_automation" else "invalid_value"
        return {"success": False, "errors": [{"field": "id" if code == "not_found" else _classify_field(result["message"]), "code": code, "message": result["message"]}]}
    return {"success": True, "automation": result["rule"]}


def delete_automation(modules: Dict[str, Any], automation_id: str) -> Dict[str, Any]:
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "message": "automation engine is not available"}
    result = engine.delete_rule(automation_id)
    return {"success": result["ok"], "message": result["message"]}


def enable_automation(modules: Dict[str, Any], automation_id: str) -> Dict[str, Any]:
    """Reuses `AutomationEngine.enable_automation()` VERBATIM (Phase 6's
    own explicit "do NOT directly edit JSON from the API endpoint, reuse
    the existing enable/disable implementation" instruction) - this
    function is pure response-shape translation, nothing else."""
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "message": "automation engine is not available"}
    result = engine.enable_automation(automation_id)
    if not result["ok"]:
        return {"success": False, "message": result["message"]}
    return {"success": True, "automation": engine.get_rule(automation_id)}


def disable_automation(modules: Dict[str, Any], automation_id: str) -> Dict[str, Any]:
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "message": "automation engine is not available"}
    result = engine.disable_automation(automation_id)
    if not result["ok"]:
        return {"success": False, "message": result["message"]}
    return {"success": True, "automation": engine.get_rule(automation_id)}


def run_automation(modules: Dict[str, Any], automation_id: str) -> Dict[str, Any]:
    """Reuses `AutomationEngine.run_automation()` VERBATIM - the SAME
    manual-trigger entry point `AutomationToolHandler`'s own `run`
    action already calls (Phase 7's own "reuse the SAME execution path,
    do NOT duplicate `_run_execution()`" requirement - there is no
    device-control or execution logic in this function at all, only a
    response-shape translation)."""
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"success": False, "message": "automation engine is not available"}
    result = engine.run_automation(automation_id)
    return {
        "success": result["ok"],
        "automation_id": automation_id,
        "execution_id": result.get("execution_id"),
        "status": "queued" if result["ok"] else "refused",
        "message": result["message"],
    }


# ============================================================================
# server.py routing helpers
# ============================================================================

_LIST_PATH = "/api/automations"
_PREFIX = "/api/automations/"
_VALIDATE_PATH = "/api/automations/validate"


def dispatch_post(modules: Dict[str, Any], path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns `None` if `path` is not part of the `/api/automations*`
    family at all (the caller, `server.py::_dispatch_post`, falls
    through to its own existing `_run_control()`/404 handling in that
    case) - never `None` for a route that WAS matched, even a failing
    one (those return a `{"success": False, ...}` body instead, exactly
    like every other control in this package)."""
    if path == _LIST_PATH:
        return create_automation(modules, body)
    if path == _VALIDATE_PATH:
        return validate_automation(body)
    if not path.startswith(_PREFIX):
        return None
    remainder = path[len(_PREFIX):]
    parts = [p for p in remainder.split("/") if p]
    if len(parts) != 2:
        return None
    automation_id, verb = parts
    if verb == "enable":
        return enable_automation(modules, automation_id)
    if verb == "disable":
        return disable_automation(modules, automation_id)
    if verb == "run":
        return run_automation(modules, automation_id)
    if verb == "update":
        return update_automation(modules, automation_id, body)
    if verb == "delete":
        return delete_automation(modules, automation_id)
    return None
