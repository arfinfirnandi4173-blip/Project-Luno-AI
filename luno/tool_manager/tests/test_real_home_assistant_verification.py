"""
Test manual buat Reliability Sprint - verified execution di
`RealHomeAssistantHandler` (luno/tool_manager/builtin/real_home_assistant.py).
Jalanin dari root project:

    python luno/tool_manager/tests/test_real_home_assistant_verification.py

Semua sintetis - pakai `FakeHAClient` (implementasi minimal
`call_service()`/`get_entity_state()` sendiri, BUKAN Home Assistant
beneran) supaya tiap skenario (retry sukses, timeout, device
unavailable, entity tidak ketemu, dst) bisa disimulasikan deterministik
tanpa jaringan/HA server.
"""

import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402

PASS = "✓"
FAIL = "✗"


class FakeHAClient:
    """Minimal fake standing in for `RealHomeAssistantClient`:
    `states` is the ground truth the "verification" reads from,
    `state_after_call` maps entity_id -> the state it flips to once
    `call_service()` is invoked (simulating HA actually acting on the
    command), optionally after `settle_after_reads` verification reads
    (simulating a slow device) - `None` means it never settles (device
    stays unresponsive)."""

    def __init__(self):
        self.states = {}
        self.state_after_call = {}
        self.settle_after_reads = {}
        self._reads_since_call = {}
        self._called_entities = set()
        self.call_service_result = None  # override to force a failed service call
        self.calls = []
        # RGB color/brightness verification fix - entity_id -> attributes
        # dict `get_entity_attributes()` reads back. Unset (or entity
        # missing/empty) => `_verify_light_attribute()` treats it as "no
        # attributes available", same "skip verification, trust the
        # service call" behavior every pre-existing test above already
        # relies on.
        self.attributes = {}
        self.attribute_reads = 0

    def get_entity_attributes(self, entity_id):
        self.attribute_reads += 1
        return self.attributes.get(entity_id)

    def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        if self.call_service_result is not None:
            return self.call_service_result
        self._called_entities.add(entity_id)
        self._reads_since_call[entity_id] = 0
        return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_entity_state(self, entity_id):
        target = self.state_after_call.get(entity_id)
        if target is not None and entity_id in self._called_entities:
            settle_after = self.settle_after_reads.get(entity_id, 0)
            reads = self._reads_since_call.get(entity_id, 0)
            self._reads_since_call[entity_id] = reads + 1
            if reads >= settle_after:
                self.states[entity_id] = target
        return self.states.get(entity_id)


def _make_handler(client, monkeypatch_devices=None, on_verification_event=None):
    handler = RealHomeAssistantHandler(client, on_verification_event=on_verification_event)
    return handler


class _EventRecorder:
    """Verified Smart Home Execution sprint - a tiny stand-in for the
    real `on_verification_event(stage, payload)` hook `RealHomeAssistantHandler`
    now accepts (see `luno.bootstrap.adapters._make_verification_event_publisher`
    for the real Event Bus wiring). Records `(stage, payload)` in call
    order so tests can assert the exact lifecycle sequence
    (started -> retry* -> verified/failed/timeout) without needing a
    real Runtime/EventBus."""

    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, dict(payload)))

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


def _patch_devices(lights=None, switches=None, scripts=None):
    """`_resolve_entity_id` reads `luno.devices.LIGHTS/SWITCHES/SCRIPTS`
    fresh (import inside the lookup helpers) - monkeypatching that
    module's globals is the same "swap the registry, restore after"
    approach the legacy `luno/main.py` test suite already uses for
    `devices.py`."""
    from luno import devices
    saved = (dict(devices.LIGHTS), dict(devices.SWITCHES), dict(devices.SCRIPTS))
    devices.LIGHTS.clear()
    devices.LIGHTS.update(lights or {})
    devices.SWITCHES.clear()
    devices.SWITCHES.update(switches or {})
    devices.SCRIPTS.clear()
    devices.SCRIPTS.update(scripts or {})
    return saved


def _restore_devices(saved):
    from luno import devices
    devices.LIGHTS.clear()
    devices.LIGHTS.update(saved[0])
    devices.SWITCHES.clear()
    devices.SWITCHES.update(saved[1])
    devices.SCRIPTS.clear()
    devices.SCRIPTS.update(saved[2])


def _set_env(**kwargs):
    saved = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_successful_light_on():
    saved_devices = _patch_devices(lights={"living room light": {"entity_id": "light.living_room", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.living_room"] = "off"
        client.state_after_call["light.living_room"] = "on"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="living_room_light"))
        ok = (
            result.success and result.data["actual_state"] == "on" and result.data["expected_state"] == "on"
            and "turned on" in result.message.lower() and result.data["verification_attempts"] >= 1
        )
        return ok, f"success={result.success} message={result.message!r} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_successful_light_off():
    saved_devices = _patch_devices(lights={"desk lamp": {"entity_id": "light.desk", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.desk"] = "on"
        client.state_after_call["light.desk"] = "off"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_off", target="desk_lamp"))
        ok = result.success and result.data["actual_state"] == "off" and "turned off" in result.message.lower()
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_retry_then_success():
    """Sprint's own example: still OFF on first read, ON on the retry."""
    saved_devices = _patch_devices(lights={"hallway light": {"entity_id": "light.hallway", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=3, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.hallway"] = "off"
        client.state_after_call["light.hallway"] = "on"
        client.settle_after_reads["light.hallway"] = 1  # first read still off, second read on
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="hallway_light"))
        ok = result.success and result.data["verification_attempts"] == 2
        return ok, f"success={result.success} attempts={result.data['verification_attempts']}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_retry_exhausted_device_never_responds():
    saved_devices = _patch_devices(lights={"garage light": {"entity_id": "light.garage", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=20, VERIFY_RETRIES=2, VERIFY_TIMEOUT_MS=200)
    try:
        client = FakeHAClient()
        client.states["light.garage"] = "off"
        # no state_after_call entry - the device just never updates
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="garage_light"))
        ok = (not result.success) and "didn't respond" in result.message and result.data["actual_state"] == "off"
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_device_unavailable():
    saved_devices = _patch_devices(lights={"attic light": {"entity_id": "light.attic", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=1, VERIFY_TIMEOUT_MS=500)
    try:
        client = FakeHAClient()
        client.states["light.attic"] = "unavailable"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="attic_light"))
        ok = (not result.success) and "unavailable" in result.message.lower()
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_home_assistant_offline():
    saved_devices = _patch_devices(lights={"kitchen light": {"entity_id": "light.kitchen", "aliases": []}})
    try:
        client = FakeHAClient()
        client.call_service_result = {"success": False, "error": "Home Assistant source is not connected yet"}
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="kitchen_light"))
        ok = (not result.success) and "can't reach home assistant" in result.message.lower()
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)


def test_entity_not_found_no_suggestion():
    saved_devices = _patch_devices(lights={"office light": {"entity_id": "light.office", "aliases": []}})
    saved_env = _set_env(ENTITY_SIMILARITY_THRESHOLD=0.9)  # very strict -> no match
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="totally_unrelated_zzz"))
        ok = (not result.success) and result.error_type == "UnknownDevice" and not result.data.get("suggestions")
        return ok, f"success={result.success} message={result.message!r} suggestions={result.data.get('suggestions')}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_similar_entity_single_suggestion():
    saved_devices = _patch_devices(lights={"office desk light": {"entity_id": "light.office_desk", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="desk_light"))
        ok = (
            not result.success and result.error_type == "UnknownDevice"
            and "did you mean" in result.message.lower()
            and result.data.get("suggestions") == ["office desk light"]
        )
        return ok, f"message={result.message!r} suggestions={result.data.get('suggestions')}"
    finally:
        _restore_devices(saved_devices)


def test_multiple_similar_entities():
    saved_devices = _patch_devices(lights={
        "kitchen light": {"entity_id": "light.kitchen", "aliases": []},
        "kitchen ceiling": {"entity_id": "light.kitchen_ceiling", "aliases": []},
        "kitchen strip": {"entity_id": "light.kitchen_strip", "aliases": []},
    })
    saved_env = _set_env(ENTITY_SIMILARITY_THRESHOLD=0.5)
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="kitchen"))
        ok = (not result.success) and len(result.data.get("suggestions") or []) >= 2 and "which one" in result.message.lower()
        return ok, f"message={result.message!r} suggestions={result.data.get('suggestions')}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_already_on():
    saved_devices = _patch_devices(lights={"bedroom light": {"entity_id": "light.bedroom", "aliases": []}})
    try:
        client = FakeHAClient()
        client.states["light.bedroom"] = "on"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="bedroom_light"))
        ok = result.success and "already on" in result.message.lower() and not client.calls
        return ok, f"message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_already_off():
    saved_devices = _patch_devices(lights={"bedroom light": {"entity_id": "light.bedroom", "aliases": []}})
    try:
        client = FakeHAClient()
        client.states["light.bedroom"] = "off"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_off", target="bedroom_light"))
        ok = result.success and "already off" in result.message.lower() and not client.calls
        return ok, f"message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_verify_disabled_trusts_service_call():
    saved_devices = _patch_devices(lights={"pantry light": {"entity_id": "light.pantry", "aliases": []}})
    saved_env = _set_env(VERIFY_DEVICE_STATE="false")
    try:
        client = FakeHAClient()
        client.states["light.pantry"] = "off"
        # deliberately never settles - would fail verification if it ran
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="pantry_light"))
        ok = result.success and result.data["verification_attempts"] == 0
        return ok, f"success={result.success} attempts={result.data['verification_attempts']}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_no_false_success_messages_on_failure():
    """Golden rule regression guard: whenever success is False, the
    message must never contain a bare confirmation phrase."""
    saved_devices = _patch_devices(lights={"porch light": {"entity_id": "light.porch", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=0, VERIFY_TIMEOUT_MS=100)
    try:
        client = FakeHAClient()
        client.states["light.porch"] = "off"
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="porch_light"))
        forbidden = ("i've turned on", "i've turned it on")
        ok = (not result.success) and not any(p in result.message.lower() for p in forbidden)
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_concurrent_commands_stay_isolated():
    """Two different entities commanded back-to-back on the same
    handler instance (same `self._lock`) must not cross-contaminate
    each other's verification result."""
    saved_devices = _patch_devices(lights={
        "light a": {"entity_id": "light.a", "aliases": []},
        "light b": {"entity_id": "light.b", "aliases": []},
    })
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=1000)
    try:
        client = FakeHAClient()
        client.states = {"light.a": "off", "light.b": "on"}
        client.state_after_call = {"light.a": "on", "light.b": "off"}
        handler = _make_handler(client)
        results = []
        errors = []

        import threading as _threading

        def _run(action, target):
            try:
                results.append(handler.execute(ToolCall(tool="home_assistant", action=action, target=target)))
            except Exception as ex:
                errors.append(ex)

        threads = [
            _threading.Thread(target=_run, args=("turn_on", "light_a")),
            _threading.Thread(target=_run, args=("turn_off", "light_b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        ok = (
            not errors and len(results) == 2
            and all(r.success for r in results)
            and {r.data["entity_id"] for r in results} == {"light.a", "light.b"}
        )
        return ok, f"errors={errors} results={[(r.data['entity_id'], r.data['actual_state']) for r in results]}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


# ---------------------------------------------------------------------------
# Verified Smart Home Execution sprint - verification lifecycle events
# ---------------------------------------------------------------------------
# Extends (does not replace) the scenarios above with coverage for the new
# optional `on_verification_event` hook - same `FakeHAClient`/`_patch_devices`
# fixtures, same real handler, nothing about the verify-loop itself changed.

def test_events_success_sequence():
    saved_devices = _patch_devices(lights={"living room light": {"entity_id": "light.living_room", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.living_room"] = "off"
        client.state_after_call["light.living_room"] = "on"
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="living_room_light"))
        ok = (
            result.success and recorder.stages == ["started", "verified"]
            and recorder.calls[0][1]["expected_state"] == "on"
            and recorder.calls[1][1]["request_id"] == recorder.calls[0][1]["request_id"]
            and recorder.calls[1][1]["actual_state"] == "on"
        )
        return ok, f"stages={recorder.stages} calls={recorder.calls}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_retry_then_success_sequence():
    saved_devices = _patch_devices(lights={"hallway light": {"entity_id": "light.hallway", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=3, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.hallway"] = "off"
        client.state_after_call["light.hallway"] = "on"
        client.settle_after_reads["light.hallway"] = 1  # first read still off, second read on
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="hallway_light"))
        ok = (
            result.success and recorder.stages == ["started", "retry", "verified"]
            and recorder.calls[1][1]["attempt"] == 1
            and recorder.calls[1][1]["actual_state"] == "off"
        )
        return ok, f"stages={recorder.stages} calls={recorder.calls}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_retry_exhausted_emits_failed_not_timeout():
    """Retries run out with plenty of wall-clock budget to spare - this
    is a plain verification failure, not a timeout (see
    `ActionVerificationFailed`/`ActionVerificationTimeout`'s own
    docstrings in `luno/adapters/events.py`)."""
    saved_devices = _patch_devices(lights={"garage light": {"entity_id": "light.garage", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=2, VERIFY_TIMEOUT_MS=5000)
    try:
        client = FakeHAClient()
        client.states["light.garage"] = "off"
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="garage_light"))
        ok = (
            not result.success and recorder.stages == ["started", "retry", "retry", "failed"]
            and recorder.calls[-1][1]["failure_reason"] is not None
        )
        return ok, f"stages={recorder.stages}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_timeout_when_budget_runs_out_before_retries_exhausted():
    """Distinct scenario from the one above: the wall-clock budget is
    the binding constraint, not the retry count - must emit
    ActionVerificationTimeout ('timeout' stage), never 'failed'."""
    saved_devices = _patch_devices(lights={"shed light": {"entity_id": "light.shed", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=50, VERIFY_RETRIES=10, VERIFY_TIMEOUT_MS=120)
    try:
        client = FakeHAClient()
        client.states["light.shed"] = "off"  # never settles
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="shed_light"))
        ok = (
            not result.success and recorder.stages[0] == "started" and recorder.stages[-1] == "timeout"
            and "failed" not in recorder.stages
        )
        return ok, f"stages={recorder.stages} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_device_unavailable_emits_failed():
    saved_devices = _patch_devices(lights={"attic light": {"entity_id": "light.attic", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_RETRIES=1, VERIFY_TIMEOUT_MS=500)
    try:
        client = FakeHAClient()
        client.states["light.attic"] = "unavailable"
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="attic_light"))
        ok = not result.success and recorder.stages[-1] == "failed" and recorder.stages[0] == "started"
        return ok, f"stages={recorder.stages}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_home_assistant_offline_emits_failed_without_started():
    """The service call itself fails - the verify-loop never begins, so
    no 'started'/'retry' should ever fire, only a single 'failed'."""
    saved_devices = _patch_devices(lights={"kitchen light": {"entity_id": "light.kitchen", "aliases": []}})
    try:
        client = FakeHAClient()
        client.call_service_result = {"success": False, "error": "Home Assistant source is not connected yet"}
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="kitchen_light"))
        ok = not result.success and recorder.stages == ["failed"]
        return ok, f"stages={recorder.stages}"
    finally:
        _restore_devices(saved_devices)


def test_events_already_in_state_emits_verified_zero_attempts():
    saved_devices = _patch_devices(lights={"bedroom light": {"entity_id": "light.bedroom", "aliases": []}})
    try:
        client = FakeHAClient()
        client.states["light.bedroom"] = "on"
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="bedroom_light"))
        ok = (
            result.success and recorder.stages == ["verified"]
            and recorder.calls[0][1].get("verification_attempts") == 0
        )
        return ok, f"stages={recorder.stages} calls={recorder.calls}"
    finally:
        _restore_devices(saved_devices)


def test_events_verify_disabled_emits_nothing():
    saved_devices = _patch_devices(lights={"pantry light": {"entity_id": "light.pantry", "aliases": []}})
    saved_env = _set_env(VERIFY_DEVICE_STATE="false")
    try:
        client = FakeHAClient()
        client.states["light.pantry"] = "off"
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="pantry_light"))
        ok = result.success and recorder.calls == []
        return ok, f"calls={recorder.calls}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_unknown_device_emits_nothing():
    saved_devices = _patch_devices(lights={"office light": {"entity_id": "light.office", "aliases": []}})
    saved_env = _set_env(ENTITY_SIMILARITY_THRESHOLD=0.9)
    try:
        client = FakeHAClient()
        recorder = _EventRecorder()
        handler = _make_handler(client, on_verification_event=recorder)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="totally_unrelated_zzz"))
        ok = not result.success and recorder.calls == []
        return ok, f"calls={recorder.calls}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_events_hook_exception_never_breaks_execution():
    """A broken listener (e.g. a Dashboard subscriber that raises) must
    never be able to affect the actual verified execution result -
    `_emit()` swallows it and logs, nothing more."""
    saved_devices = _patch_devices(lights={"study light": {"entity_id": "light.study", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.study"] = "off"
        client.state_after_call["light.study"] = "on"

        def _boom(stage, payload):
            raise RuntimeError("dashboard subscriber exploded")

        handler = _make_handler(client, on_verification_event=_boom)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="study_light"))
        ok = result.success and result.data["actual_state"] == "on"
        return ok, f"success={result.success} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


# ---------------------------------------------------------------------------
# RGB color/brightness fix - `RealHomeAssistantHandler`'s new "set_color"/
# "set_brightness" actions. Reported: "di bagian HA kok ngga bisa set rgb
# strip warna sama brightnes?" - root cause was that NEITHER handler ever
# supported anything but turn_on/turn_off/toggle/run_script/set_temperature
# (and set_temperature was itself unreachable from the parser). These two
# actions skip the on/off verify-loop entirely (color/brightness have no
# single canonical "expected state" to poll for) - they call the service
# and trust the result, same as `set_temperature` already does.
# ---------------------------------------------------------------------------

def test_set_color_calls_light_turn_on_with_rgb_and_succeeds():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = (
            result.success and result.data["color"] == "red"
            and client.calls == [("light", "turn_on", "light.rgb_strip", {"rgb_color": [255, 0, 0]})]
        )
        return ok, f"success={result.success} message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_set_color_indonesian_name_resolves_to_same_rgb():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "biru"}))
        ok = result.success and client.calls == [("light", "turn_on", "light.rgb_strip", {"rgb_color": [0, 0, 255]})]
        return ok, f"success={result.success} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_set_brightness_calls_light_turn_on_with_brightness_pct():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_brightness", target="rgb_strip", parameters={"level": 80}))
        ok = (
            result.success and result.data["brightness"] == 80
            and client.calls == [("light", "turn_on", "light.rgb_strip", {"brightness_pct": 80})]
        )
        return ok, f"success={result.success} message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_set_color_unknown_device_fails_honestly():
    saved_devices = _patch_devices(lights={"office light": {"entity_id": "light.office", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="totally_unrelated_zzz", parameters={"color": "red"}))
        ok = (not result.success) and result.error_type == "UnknownDevice" and not client.calls
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)


def test_set_color_service_call_failure_reported_honestly():
    """Unlike turn_on/turn_off (`_execute_on_off`'s verify loop), set_color
    goes through the plain `_to_tool_result()` path (same as
    set_temperature) - a failed service call surfaces the raw HA error
    string as-is, not the friendlier "I can't reach Home Assistant..."
    phrasing the verify loop composes."""
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        client.call_service_result = {"success": False, "error": "Home Assistant source is not connected yet"}
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = not result.success and result.error_type == "HomeAssistantError" and "not connected" in result.message.lower()
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)


def test_set_brightness_service_call_failure_reported_honestly():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        client.call_service_result = {"success": False, "error": "Home Assistant source is not connected yet"}
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_brightness", target="rgb_strip", parameters={"level": 50}))
        ok = not result.success and result.error_type == "HomeAssistantError" and "not connected" in result.message.lower()
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)


def test_set_color_unknown_color_name_fails_without_calling_service():
    """`_classify_color_set()` restricts `params.color` to `_COLOR_NAMES`
    keys before a `ToolCall` is ever produced, so this only guards
    against a hand-built ToolCall (e.g. a future caller bypassing the
    parser) - the handler's own `_COLOR_RGB` lookup must fail honestly
    rather than crash or silently send a bogus color."""
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    try:
        client = FakeHAClient()
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "chartreuse"}))
        ok = not result.success and not client.calls
        return ok, f"success={result.success} message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


# ---------------------------------------------------------------------------
# RGB color/brightness verification fix - reported: "set rgb strip to blue"
# said "Done" but the physical light never changed at all. set_color/
# set_brightness now do a single post-call attribute read-back (when the
# client supports `get_entity_attributes()`) instead of blindly trusting
# the HA service call being accepted - see `_verify_light_attribute()`'s
# own docstring for why this is deliberately lighter than the on/off
# verify loop, and why it never turns into a regression for a client
# that doesn't support the read-back at all.
# ---------------------------------------------------------------------------

def test_set_color_custom_rgb_verified_success():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"rgb_color": [120, 50, 200], "state": "on"}
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"rgb": [120, 50, 200]}))
        ok = result.success and result.data["rgb"] == [120, 50, 200] and client.attribute_reads == 1
        return ok, f"success={result.success} data={result.data} reads={client.attribute_reads}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_set_color_verification_catches_light_that_did_not_actually_change():
    """The exact reported bug: HA accepts the service call (success=True)
    but the light's real rgb_color attribute never moved (e.g. a WLED
    effect/preset silently overriding it) - must be an honest failure,
    never a false "Done"."""
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"rgb_color": [0, 0, 0], "state": "on"}  # never actually changed
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = (
            not result.success and result.error_type == "AttributeNotVerified"
            and result.data.get("actual") == "rgb(0, 0, 0)"
            and client.calls  # the service call itself DID go out
        )
        return ok, f"success={result.success} message={result.message!r} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_set_color_verification_tolerates_small_color_mode_drift():
    """A near-match (small gamma/whitepoint rounding from the light's
    native color mode) must still count as verified - only a genuine
    "didn't change" is a failure."""
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"rgb_color": [248, 10, 5], "state": "on"}  # requested (255,0,0), close
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = result.success and result.data["color"] == "red"
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_set_brightness_verified_success():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"brightness": round(80 / 100 * 255), "state": "on"}
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_brightness", target="rgb_strip", parameters={"level": 80}))
        ok = result.success and result.data["brightness"] == 80
        return ok, f"success={result.success} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_set_brightness_verification_catches_no_change():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"brightness": 0, "state": "on"}  # never actually changed
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_brightness", target="rgb_strip", parameters={"level": 80}))
        ok = not result.success and result.error_type == "AttributeNotVerified" and result.data.get("actual") == "0%"
        return ok, f"success={result.success} message={result.message!r} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_verification_skipped_when_client_has_no_attribute_read_capability():
    """Backward-compat guard: a client without `get_entity_attributes`
    at all (e.g. an older adapter, or `MockHomeAssistantClient`) must
    behave EXACTLY like before this fix - trust the service call, no
    attempt at verification, no crash."""
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10)
    try:
        client = FakeHAClient()
        client.get_entity_attributes = None  # simulate a client without this optional capability
        client.attributes["light.rgb_strip"] = {"rgb_color": [0, 0, 0]}  # would fail verification if it ran
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = result.success and result.data["color"] == "red"
        return ok, f"success={result.success} message={result.message!r}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_verification_skipped_when_verify_device_state_disabled():
    saved_devices = _patch_devices(lights={"rgb strip": {"entity_id": "light.rgb_strip", "aliases": []}})
    saved_env = _set_env(VERIFY_DEVICE_STATE="false")
    try:
        client = FakeHAClient()
        client.attributes["light.rgb_strip"] = {"rgb_color": [0, 0, 0]}  # would fail verification if it ran
        handler = _make_handler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="set_color", target="rgb_strip", parameters={"color": "red"}))
        ok = result.success and client.attribute_reads == 0
        return ok, f"success={result.success} reads={client.attribute_reads}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_no_verification_event_hook_is_fully_backward_compatible():
    """Regression guard: every scenario above this section (pre-sprint)
    constructs `RealHomeAssistantHandler(client)` with NO hook at all -
    must behave identically to before this sprint (this is really just
    re-asserting `test_successful_light_on` still holds with the new
    optional parameter defaulted away)."""
    saved_devices = _patch_devices(lights={"den light": {"entity_id": "light.den", "aliases": []}})
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = FakeHAClient()
        client.states["light.den"] = "off"
        client.state_after_call["light.den"] = "on"
        handler = RealHomeAssistantHandler(client)  # no on_verification_event kwarg at all
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="den_light"))
        ok = result.success and result.data["actual_state"] == "on"
        return ok, f"success={result.success} data={result.data}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def main():
    scenarios = [
        ("successful_light_on", test_successful_light_on),
        ("successful_light_off", test_successful_light_off),
        ("retry_then_success", test_retry_then_success),
        ("retry_exhausted_device_never_responds", test_retry_exhausted_device_never_responds),
        ("device_unavailable", test_device_unavailable),
        ("home_assistant_offline", test_home_assistant_offline),
        ("entity_not_found_no_suggestion", test_entity_not_found_no_suggestion),
        ("similar_entity_single_suggestion", test_similar_entity_single_suggestion),
        ("multiple_similar_entities", test_multiple_similar_entities),
        ("already_on", test_already_on),
        ("already_off", test_already_off),
        ("verify_disabled_trusts_service_call", test_verify_disabled_trusts_service_call),
        ("no_false_success_messages_on_failure", test_no_false_success_messages_on_failure),
        ("concurrent_commands_stay_isolated", test_concurrent_commands_stay_isolated),
        # Verified Smart Home Execution sprint - verification lifecycle events
        ("events_success_sequence", test_events_success_sequence),
        ("events_retry_then_success_sequence", test_events_retry_then_success_sequence),
        ("events_retry_exhausted_emits_failed_not_timeout", test_events_retry_exhausted_emits_failed_not_timeout),
        ("events_timeout_when_budget_runs_out_before_retries_exhausted", test_events_timeout_when_budget_runs_out_before_retries_exhausted),
        ("events_device_unavailable_emits_failed", test_events_device_unavailable_emits_failed),
        ("events_home_assistant_offline_emits_failed_without_started", test_events_home_assistant_offline_emits_failed_without_started),
        ("events_already_in_state_emits_verified_zero_attempts", test_events_already_in_state_emits_verified_zero_attempts),
        ("events_verify_disabled_emits_nothing", test_events_verify_disabled_emits_nothing),
        ("events_unknown_device_emits_nothing", test_events_unknown_device_emits_nothing),
        ("events_hook_exception_never_breaks_execution", test_events_hook_exception_never_breaks_execution),
        ("no_verification_event_hook_is_fully_backward_compatible", test_no_verification_event_hook_is_fully_backward_compatible),
        # RGB color/brightness fix - set_color/set_brightness
        ("set_color_calls_light_turn_on_with_rgb_and_succeeds", test_set_color_calls_light_turn_on_with_rgb_and_succeeds),
        ("set_color_indonesian_name_resolves_to_same_rgb", test_set_color_indonesian_name_resolves_to_same_rgb),
        ("set_brightness_calls_light_turn_on_with_brightness_pct", test_set_brightness_calls_light_turn_on_with_brightness_pct),
        ("set_color_unknown_device_fails_honestly", test_set_color_unknown_device_fails_honestly),
        ("set_color_service_call_failure_reported_honestly", test_set_color_service_call_failure_reported_honestly),
        ("set_brightness_service_call_failure_reported_honestly", test_set_brightness_service_call_failure_reported_honestly),
        ("set_color_unknown_color_name_fails_without_calling_service", test_set_color_unknown_color_name_fails_without_calling_service),
        # RGB color/brightness verification fix - attribute read-back
        ("set_color_custom_rgb_verified_success", test_set_color_custom_rgb_verified_success),
        ("set_color_verification_catches_light_that_did_not_actually_change", test_set_color_verification_catches_light_that_did_not_actually_change),
        ("set_color_verification_tolerates_small_color_mode_drift", test_set_color_verification_tolerates_small_color_mode_drift),
        ("set_brightness_verified_success", test_set_brightness_verified_success),
        ("set_brightness_verification_catches_no_change", test_set_brightness_verification_catches_no_change),
        ("verification_skipped_when_client_has_no_attribute_read_capability", test_verification_skipped_when_client_has_no_attribute_read_capability),
        ("verification_skipped_when_verify_device_state_disabled", test_verification_skipped_when_verify_device_state_disabled),
    ]

    results = {}
    for name, fn in scenarios:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"EXCEPTION: {ex}"
            import traceback
            traceback.print_exc()
        print(f"{PASS if ok else FAIL} ({time.time() - start:.2f}s) {detail}")
        results[name] = ok

    print(f"\n{'=' * 60}\nRingkasan\n{'=' * 60}")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL}  {name}")

    all_ok = all(results.values())
    print(f"\n{PASS if all_ok else FAIL} {'Semua skenario lolos.' if all_ok else 'Ada yang gagal - cek detail di atas.'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
