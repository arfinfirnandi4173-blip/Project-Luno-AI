"""
tests/test_ha_camera_discovery.py
====================================

LUNO P0.5.1 (Real Tapo C212 Entity Discovery) - the smallest possible
test file for `ha_camera_discovery.py`'s own pure classification logic
(`_build_report`), per that sprint's own Section 16 ("If a test is
needed for a clear bug in the discovery script, add the smallest
possible test. Do NOT add broad infrastructure tests during this
sprint.").

This does NOT test live Home Assistant connectivity (no network in this
environment - see the script's own module docstring) - only the pure,
deterministic classification function, which is exactly what this
sprint's own real bug (see below) lived in adjacent code, and exactly
what a future change to the classification rules could regress.

Historical note (the "clear bug" this sprint fixed): the P0.5 version of
this script called `client.get_states()` without ever starting
`client.listen_and_dispatch()` as a background task first - per `luno/
ha_listener.py`'s own documented ordering requirement, `pending_
responses` is only ever filled in by that background listener, so
`get_states()` (and this sprint's own new registry calls) would have
silently timed out on every call, even against a fully reachable HA
server. Fixed in `main()`'s own `_run()` this sprint. This specific
bug cannot be exercised by a unit test without a real or mocked
websocket server - out of scope for "smallest possible test" - so it is
documented here rather than tested directly.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ha_camera_discovery import _build_report, _device_is_tapo, _matches_keywords  # noqa: E402


def _tapo_fixture():
    states = [
        {"entity_id": "camera.front_door", "state": "idle", "attributes": {"friendly_name": "Front Door Camera"}},
        {"entity_id": "binary_sensor.front_door_motion", "state": "off", "attributes": {"friendly_name": "Motion", "device_class": "motion"}},
        {"entity_id": "binary_sensor.front_door_person", "state": "off", "attributes": {"friendly_name": "Person", "device_class": "occupancy"}},
        {"entity_id": "binary_sensor.front_door_connectivity", "state": "on", "attributes": {"friendly_name": "Connectivity", "device_class": "connectivity"}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
    ]
    entity_registry = [
        {"entity_id": "camera.front_door", "device_id": "dev1", "platform": "tapo"},
        {"entity_id": "binary_sensor.front_door_motion", "device_id": "dev1", "platform": "tapo"},
        {"entity_id": "binary_sensor.front_door_person", "device_id": "dev1", "platform": "tapo"},
        {"entity_id": "binary_sensor.front_door_connectivity", "device_id": "dev1", "platform": "tapo"},
        {"entity_id": "light.kitchen", "device_id": "dev2", "platform": "hue"},
    ]
    device_registry = [
        {"id": "dev1", "manufacturer": "TP-Link", "model": "Tapo C212", "name": "Front Door Camera", "connections": [["ip", "192.168.1.4"]]},
        {"id": "dev2", "manufacturer": "Philips", "model": "Hue", "name": "Kitchen Light"},
    ]
    return states, entity_registry, device_registry


def test_01_full_registry_classifies_all_four_roles_confirmed():
    states, entity_registry, device_registry = _tapo_fixture()
    result = _build_report(states, entity_registry, device_registry)
    assert result["camera"]["found"] and result["camera"]["confirmed_via_device_registry"]
    assert result["camera"]["entity_id"] == "camera.front_door"
    assert result["camera"]["manufacturer"] == "TP-Link"
    assert result["motion"]["found"] and result["motion"]["confirmed_via_device_registry"]
    assert result["human"]["found"] and result["human"]["confirmed_via_device_registry"]
    assert result["availability"]["found"] and result["availability"]["confirmed_via_device_registry"]


def test_02_pytapo_relationship_confirmed_only_with_connection_evidence():
    states, entity_registry, device_registry = _tapo_fixture()
    result = _build_report(states, entity_registry, device_registry)
    assert result["pytapo"]["same_physical_camera"] is True
    assert "connections list" in result["pytapo"]["evidence"]


def test_03_unrelated_light_never_classified_as_camera_role():
    states, entity_registry, device_registry = _tapo_fixture()
    result = _build_report(states, entity_registry, device_registry)
    for block in (result["camera"], result["motion"], result["human"], result["availability"]):
        assert block.get("entity_id") != "light.kitchen"


def test_04_no_registry_falls_back_to_unconfirmed_keyword_match():
    states = [{"entity_id": "camera.random", "state": "idle", "attributes": {"friendly_name": "Some Camera"}}]
    result = _build_report(states, None, None)
    assert result["registry_available"] is False
    assert result["camera"]["found"] is True
    assert result["camera"]["confirmed_via_device_registry"] is False


def test_05_human_never_conflated_with_motion_when_absent():
    """The P0.5.1 brief's own critical distinction (Section 10) - a
    device with ONLY a motion sensor must report human as NOT FOUND,
    never silently reuse the motion entity."""
    states = [
        {"entity_id": "camera.front_door", "state": "idle", "attributes": {}},
        {"entity_id": "binary_sensor.front_door_motion", "state": "off", "attributes": {"device_class": "motion"}},
    ]
    entity_registry = [
        {"entity_id": "camera.front_door", "device_id": "dev1", "platform": "tapo"},
        {"entity_id": "binary_sensor.front_door_motion", "device_id": "dev1", "platform": "tapo"},
    ]
    device_registry = [{"id": "dev1", "manufacturer": "TP-Link", "model": "Tapo C212"}]
    result = _build_report(states, entity_registry, device_registry)
    assert result["motion"]["found"] is True
    assert result["human"]["found"] is False
    assert result["availability"]["found"] is False


def test_06_no_camera_device_no_pytapo_same_physical_camera_claim():
    result = _build_report([], [], [])
    assert result["camera"]["found"] is False
    assert result["pytapo"]["same_physical_camera"] is None  # never guessed


def test_07_matches_keywords_and_device_is_tapo_helpers():
    assert _matches_keywords("camera.tapo_c212", "") is True
    assert _matches_keywords("light.kitchen", "Kitchen Light") is False
    assert _device_is_tapo({"manufacturer": "TP-Link", "model": "Tapo C212"}) is True
    assert _device_is_tapo({"manufacturer": "Philips", "model": "Hue"}) is False


def test_08_never_opens_any_file_for_writing():
    """Section 13 - discovery must never mutate production configuration
    (or any other file). Static proof: the script's source contains no
    `open(...)` call using a write/append mode anywhere at all - it only
    prints to stdout."""
    import inspect
    import re

    import ha_camera_discovery as mod

    source = inspect.getsource(mod)
    write_mode_opens = re.findall(r"""open\([^)]*['"]\s*[wa]\+?['"]""", source)
    assert not write_mode_opens, f"found file-write call(s): {write_mode_opens}"
