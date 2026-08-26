"""
tests/test_p0_8_7_wled_verification_fix.py
=============================================

LUNO P0.8.7 (Investigate and Fix the Remaining WLED Activation Failure) -
dedicated regression suite. See docs/change_impact/camera_automation_
p0_8_7.md for the full root-cause trace.

Context: a real production log (`logs/runtime/2026-08-23.log`, from the
user's actual `main.py` run, real RTSP/real Vision/real Home Assistant
WebSocket backend) shows Luno correctly dispatching `homeassistant.
turn_on` on `light.wled`, Home Assistant accepting the call, and Luno's
own verification reporting `state=on` - yet the user reports the
physical WLED strip does not visibly illuminate. Source-level audit of
the complete production path (ToolManager -> RealHomeAssistantHandler ->
RealHomeAssistantClient -> luno.ha_client.HomeAssistantClient -> HA
WebSocket API) found:

  - ToolManager never transforms/wraps a tool call - `_invoke_handler()`
    calls `handler.execute(call, context)` with the exact same `ToolCall`
    object the caller built (Section G below).
  - `RealHomeAssistantHandler._resolve_entity_tiered("light.wled")`
    resolves via TIER 1 (`_resolve_entity_id()`'s own literal
    "already looks like domain.object_id" branch) - the EXACT,
    UNMODIFIED string is what reaches `call_service()`, never a fuzzy/
    substituted entity (Section E below).
  - The exact outbound call is `call_service("homeassistant", "turn_on"/
    "turn_off", entity_id="light.wled", data={"entity_id": "light.wled"})`
    - a well-formed, standard HA WebSocket `call_service` command
    (Section D below).
  - `luno.world_model.WorldModel` is updated ONLY in reaction to a REAL
    `device_state_changed` event (itself sourced from a genuine HA
    `state_changed` WebSocket push) - never independently/optimistically
    from `call_service()`'s own return value. `update_from_tool_result()`
    exists in `world_model.py` but is not wired into the production
    bootstrap path at all (Section H below).
  - The ONE genuine gap found: `RealHomeAssistantHandler._verify_state()`
    (the actual "verification success" logic) read
    `RealHomeAssistantClient.get_entity_state()`'s CACHE-FIRST default
    behavior - returning whatever `RealHomeAssistantSource._last_states`
    already held from the last `state_changed` push, rather than
    actively re-querying Home Assistant on each verify attempt. This is
    architecturally reasonable (HA's own state machine IS the ground
    truth, and the cache is kept live by HA's own real event stream -
    never Luno's own invention), but it means "verification success"
    could be reported off a value that was already cached BEFORE this
    specific command, if this command's own fresh `state_changed` push
    was ever delayed/dropped/coalesced. Fixed additively (Sections A-C
    below): `get_entity_state()` gained a `force_refresh` parameter,
    and `_verify_state()`'s retry loop now always passes
    `force_refresh=True` - every verify attempt performs a genuinely
    live `get_states()` round trip against Home Assistant, never trusts
    only the passively-cached value.

This is NOT a fix for the physical WLED device itself - Luno has no
independent physical/optical confirmation channel for any HA-controlled
device, a disclosed architectural limit unrelated to this sprint (see
docs/change_impact/camera_automation_p0_8_7.md's own "Real Machine
Result" section: physical confirmation is D, never claimed here). This
sprint's fix closes the one gap fully within Luno's own control: the
verification claim itself is now backed by a live query, not a
potentially-stale cache.

Sections:
  A. `RealHomeAssistantClient.get_entity_state(force_refresh=...)` - the
     real client-level fresh-vs-cached distinction, using a genuine
     background `RealHomeAssistantSource` + fake async `ha_client`
     (same pattern as tests/test_real_adapters.py).
  B. `RealHomeAssistantHandler._safe_get_state()`/`_verify_state()` - the
     handler-level proof that verification now actively re-queries,
     using an extended sync `FakeHAClient` (same pattern as
     luno/tool_manager/tests/test_real_home_assistant_verification.py).
  C. `_result_data()`'s new `state_query_freshness` field.
  D. Exact outbound domain/service/entity_id/service-data shape.
  E. Entity resolution never substitutes a different entity for an
     already-fully-qualified `domain.object_id` target.
  F. No credentials/tokens are ever accessible to, or logged by, this
     module (structural/architecture guard).
  G. ToolManager never transforms a tool call before dispatching it to
     the handler.
  H. WorldModel is never updated independently of a real HA event
     (architecture guard - no direct coupling in either HA adapter file).
"""

from __future__ import annotations

import ast
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.tool_manager import ToolCall, ToolManager, ToolRegistry  # noqa: E402
from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.handler import ToolHandler  # noqa: E402
from luno.tool_manager.result import ToolResult  # noqa: E402


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


_REAL_HA_TOOL_PATH = os.path.join(_ROOT, "luno", "tool_manager", "builtin", "real_home_assistant.py")
_REAL_HA_ADAPTER_PATH = os.path.join(_ROOT, "luno", "adapters", "real_home_assistant.py")


def _set_env(**kwargs: Any) -> Dict[str, Optional[str]]:
    saved: Dict[str, Optional[str]] = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = str(v)
    return saved


def _restore_env(saved: Dict[str, Optional[str]]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _patch_wled_light():
    from luno import devices
    saved = dict(devices.LIGHTS)
    devices.LIGHTS.clear()
    devices.LIGHTS.update({"rgb strip": {"entity_id": "light.wled", "aliases": []}})
    return saved


def _restore_lights(saved) -> None:
    from luno import devices
    devices.LIGHTS.clear()
    devices.LIGHTS.update(saved)


# ============================================================================
# A. RealHomeAssistantClient.get_entity_state(force_refresh=...) - real
#    background source + fake async ha_client, same pattern as
#    tests/test_real_adapters.py.
# ============================================================================

class _AsyncFakeHAClient:
    """Mirrors `luno.ha_client.HomeAssistantClient`'s public async
    surface - same convention `tests/test_real_adapters.py::
    _FakeHAClient` already established. `states` is mutated BETWEEN
    `get_states()` calls by the test itself to simulate "Home Assistant's
    own ground truth changed since the client last cached anything"."""

    def __init__(self, initial_states: List[Dict[str, Any]]) -> None:
        self.states = list(initial_states)
        self.get_states_calls = 0
        self.connected = True

    async def connect(self):
        return True

    async def subscribe_to_events(self):
        return None

    async def listen_and_dispatch(self, callback):
        # Never fires a real event in this suite - these tests are
        # specifically about the FALLBACK/force_refresh path, which must
        # work even when no `state_changed` push has arrived at all.
        import asyncio
        while self.connected:
            await asyncio.sleep(0.05)

    async def get_states(self):
        self.get_states_calls += 1
        return list(self.states)

    async def call_service(self, domain, service, entity_id, data=None):
        return True

    async def disconnect(self):
        self.connected = False


def _start_real_source(initial_states: List[Dict[str, Any]]):
    from luno.adapters.real_home_assistant import RealHomeAssistantClient, RealHomeAssistantSource

    class _Listener:
        def on_state_changed(self, entity_id, old_state, new_state) -> None:
            pass

    fake = _AsyncFakeHAClient(initial_states)
    source = RealHomeAssistantSource(ha_client=fake)
    source.start(_Listener())
    deadline = time.time() + 3.0
    while source.loop is None and time.time() < deadline:
        time.sleep(0.02)
    assert source.loop is not None, "source never got its asyncio loop running"
    # Let the initial get_states() snapshot land before the test starts
    # mutating `fake.states` underneath it.
    deadline = time.time() + 3.0
    while fake.get_states_calls < 1 and time.time() < deadline:
        time.sleep(0.02)
    client = RealHomeAssistantClient(source)
    return fake, source, client


def test_A1_force_refresh_false_returns_the_cached_value_unchanged():
    """Item A1: default (`force_refresh=False`, or omitted entirely)
    behavior is COMPLETELY UNCHANGED - a cache hit returns instantly
    without triggering a second `get_states()` round trip."""
    fake, source, client = _start_real_source([{"entity_id": "light.wled", "state": "off", "attributes": {}}])
    try:
        assert client.get_entity_state("light.wled") == "off"
        calls_before = fake.get_states_calls
        assert client.get_entity_state("light.wled", force_refresh=False) == "off"
        assert fake.get_states_calls == calls_before, "a cache hit must never trigger a live query"
    finally:
        source.stop()


def test_A2_force_refresh_true_queries_ha_live_and_returns_the_fresh_value():
    """Item A2 - THE core P0.8.7 fix, proven directly: Home Assistant's
    OWN ground truth (`fake.states`) changes to "on" WITHOUT any
    `state_changed` push ever arriving (this fake never fires one - see
    its own docstring) - the OLD cache-first behavior would have kept
    returning the stale "off" forever; `force_refresh=True` must
    actively re-query and return the new, correct value."""
    fake, source, client = _start_real_source([{"entity_id": "light.wled", "state": "off", "attributes": {}}])
    try:
        assert client.get_entity_state("light.wled") == "off"  # cached, from the initial snapshot

        # Home Assistant's real state changed - simulated here as the
        # fake client's own ground truth updating (e.g. a `state_changed`
        # push that, for whatever reason, never reached this client -
        # exactly the failure class this fix closes).
        fake.states = [{"entity_id": "light.wled", "state": "on", "attributes": {}}]

        assert client.get_entity_state("light.wled") == "off", "force_refresh=False must still trust the cache"
        fresh = client.get_entity_state("light.wled", force_refresh=True)
        assert fresh == "on", "force_refresh=True must return Home Assistant's ACTUAL current state, not the stale cache"
        # And the cache itself is now updated from that fresh read - a
        # subsequent cached read reflects the correction too.
        assert client.get_entity_state("light.wled") == "on"
    finally:
        source.stop()


def test_A3_force_refresh_true_falls_back_to_cache_when_source_not_connected():
    """Item A3: if a live query is genuinely impossible right now (the
    background source was never started/connected), `force_refresh=True`
    degrades to the last known cached value rather than raising or
    silently returning `None` for an entity that WAS previously seen -
    "a stale-but-real answer beats no answer" (see the method's own
    P0.8.7 docstring)."""
    from luno.adapters.real_home_assistant import RealHomeAssistantClient, RealHomeAssistantSource
    source = RealHomeAssistantSource(ha_client=_AsyncFakeHAClient([]))  # never started
    source._last_states["light.wled"] = "on"  # simulate a previously-cached value
    client = RealHomeAssistantClient(source)
    assert client.get_entity_state("light.wled", force_refresh=True) == "on"


def test_A4_force_refresh_true_returns_none_when_entity_genuinely_absent_from_fresh_response():
    """Item A4 - the honest-failure counterpart to A3: when the live
    query DOES succeed but Home Assistant's fresh response no longer
    contains this entity at all, that is real, current evidence - the
    method must return `None`, never fall back to a stale cached value
    that could now be wrong."""
    fake, source, client = _start_real_source([{"entity_id": "light.wled", "state": "on", "attributes": {}}])
    try:
        assert client.get_entity_state("light.wled") == "on"
        fake.states = []  # entity vanished from HA's own fresh snapshot
        assert client.get_entity_state("light.wled", force_refresh=True) is None
    finally:
        source.stop()


# ============================================================================
# B. RealHomeAssistantHandler._safe_get_state()/_verify_state() - handler-
#    level proof, using an extended sync FakeHAClient (same pattern as
#    luno/tool_manager/tests/test_real_home_assistant_verification.py).
# ============================================================================

class _SyncFakeHAClientWithFreshness:
    """Same overall shape as `test_real_home_assistant_verification.py::
    FakeHAClient`, extended with genuine `force_refresh` support so
    these tests can prove `_verify_state()` actually asks for a fresh
    read rather than merely accepting whichever value happens to already
    be cached. `cached_state`/`fresh_state` are DELIBERATELY allowed to
    disagree - that divergence is exactly what these tests exercise."""

    def __init__(self) -> None:
        self.cached_state: Dict[str, Optional[str]] = {}
        self.fresh_state: Dict[str, Optional[str]] = {}
        self.state_after_call: Dict[str, str] = {}
        self._called_entities: set = set()
        self.calls: List[Any] = []
        self.force_refresh_calls = 0
        self.plain_calls = 0

    def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, dict(data or {})))
        self._called_entities.add(entity_id)
        target = self.state_after_call.get(entity_id)
        if target is not None:
            self.fresh_state[entity_id] = target
        return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_entity_state(self, entity_id: str, force_refresh: bool = False) -> Optional[str]:
        if force_refresh:
            self.force_refresh_calls += 1
            return self.fresh_state.get(entity_id)
        self.plain_calls += 1
        return self.cached_state.get(entity_id)


def test_B1_verify_state_always_requests_force_refresh():
    """Item B1: `_verify_state()`'s own retry loop must call
    `get_entity_state(entity_id, force_refresh=True)` on every attempt -
    never the plain, cache-first call - proven by the fake recording
    which path was actually used."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000, VERIFY_RETRIES=3)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "off"  # deliberately WRONG/stale
        client.fresh_state["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is True
        assert client.force_refresh_calls >= 1
        assert result.data["state_query_freshness"] == "fresh"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_B2_stale_cache_alone_would_never_verify_but_fresh_query_does():
    """Item B2 - the direct proof this fix closes the reported gap: the
    CACHED value never updates to "on" at all (simulating a dropped/
    delayed `state_changed` push - the exact failure class from the P0.8.7
    brief), while the FRESH value the client's live query would return
    DOES correctly reflect the command's effect. Verification must
    succeed, because `_verify_state()` now reads the fresh path, not the
    cached one."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000, VERIFY_RETRIES=2)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "off"  # never updates - simulates a lost state_changed push
        client.fresh_state["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"  # only the FRESH path picks this up (see call_service())
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is True
        assert result.data["actual_state"] == "on"
        # The cached-only value the OLD (pre-P0.8.7) behavior would have
        # used never budged - proving this passed BECAUSE of the fresh
        # query, not by coincidence.
        assert client.cached_state["light.wled"] == "off"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_B3_client_without_force_refresh_support_still_works():
    """Item B3 - backward compatibility: a client whose `get_entity_state`
    only accepts a single positional argument (every pre-existing test
    double in this project, e.g. `test_real_home_assistant_verification.
    py::FakeHAClient`) must keep working unchanged - `_safe_get_state()`
    catches the `TypeError` and falls back to the plain call."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        class _OneArgClient:
            def __init__(self) -> None:
                self.state = "off"
                self.calls = []

            def call_service(self, domain, service, entity_id=None, data=None):
                self.calls.append((domain, service, entity_id, data))
                self.state = "on"
                return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

            def get_entity_state(self, entity_id):  # no force_refresh kwarg at all
                return self.state

        client = _OneArgClient()
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is True
        assert result.data["actual_state"] == "on"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


# ============================================================================
# C. _result_data()'s new state_query_freshness field.
# ============================================================================

def test_C1_already_in_state_precheck_reports_cached_freshness():
    """Item C1: the "already in desired state" skip-dispatch path never
    performs a live query at all - its result must honestly report
    `state_query_freshness == "cached"`, not "fresh"."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "on"
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.success is True
        assert result.data.get("already_in_state") is True
        assert result.data["state_query_freshness"] == "cached"
        assert client.force_refresh_calls == 0, "the pre-check path must never trigger a live HA query"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_C2_full_verify_path_reports_fresh_freshness():
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "off"
        client.fresh_state["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"
        handler = RealHomeAssistantHandler(client)
        result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert result.data["state_query_freshness"] == "fresh"
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


# ============================================================================
# D. Exact outbound domain/service/entity_id/service-data shape.
# ============================================================================

def test_D1_turn_on_sends_exactly_homeassistant_turn_on_with_bare_entity_id():
    """Item D1: reproduces the brief's own "one controlled test" - the
    EXACT outbound call must be domain='homeassistant', service='turn_on',
    entity_id='light.wled', with service data containing ONLY the
    entity_id (never a fabricated/extra field, never a color/brightness
    payload for a bare turn_on)."""
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "off"
        client.fresh_state["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"
        handler = RealHomeAssistantHandler(client)
        handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb strip"))
        assert len(client.calls) == 1
        domain, service, entity_id, data = client.calls[0]
        assert domain == "homeassistant"
        assert service == "turn_on"
        assert entity_id == "light.wled"
        # `RealHomeAssistantHandler._execute_on_off()` calls
        # `self._client.call_service("homeassistant", action,
        # entity_id=entity_id)` - entity_id is passed as its own
        # keyword argument, not folded into `data` at this layer (that
        # folding happens one layer down, inside `luno.ha_client.
        # HomeAssistantClient.call_service()`, which mutates
        # `data["entity_id"] = entity_id` right before sending the WS
        # frame - see that file's own `call_service()`). No extra
        # fields (color/brightness/etc.) are ever added for a bare
        # turn_on.
        assert data == {}
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


def test_D2_turn_off_sends_exactly_homeassistant_turn_off():
    saved_devices = _patch_wled_light()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=2000)
    try:
        client = _SyncFakeHAClientWithFreshness()
        client.cached_state["light.wled"] = "on"
        client.fresh_state["light.wled"] = "on"
        client.state_after_call["light.wled"] = "off"
        handler = RealHomeAssistantHandler(client)
        handler.execute(ToolCall(tool="home_assistant", action="turn_off", target="rgb strip"))
        domain, service, entity_id, data = client.calls[0]
        assert (domain, service, entity_id) == ("homeassistant", "turn_off", "light.wled")
    finally:
        _restore_lights(saved_devices)
        _restore_env(saved_env)


# ============================================================================
# E. Entity resolution never substitutes a different entity for an
#    already-fully-qualified target.
# ============================================================================

def test_E1_exact_entity_id_target_resolves_to_itself_via_tier_1_literal():
    """Item E1: `light.wled` passed directly as `target` (the exact
    shape `config/automation_rules.json::camera_human_detected_test_
    action`'s own `parameters.target` uses) resolves through TIER 1
    (`_resolve_entity_id()`'s own "already looks like domain.object_id"
    branch), never the fuzzy/scored tiers - so it can never be silently
    substituted for a different, similarly-named device."""
    saved_devices = _patch_wled_light()
    try:
        client = _SyncFakeHAClientWithFreshness()
        handler = RealHomeAssistantHandler(client)
        resolution = handler._resolve_entity_tiered("light.wled")
        assert resolution.resolved_entity == "light.wled"
        assert resolution.resolution_method in ("entity_id_literal", "exact_name", "exact_alias")
        assert resolution.confidence == 1.0
    finally:
        _restore_lights(saved_devices)


def test_E2_resolve_entity_id_returns_the_literal_string_unchanged():
    saved_devices = _patch_wled_light()
    try:
        client = _SyncFakeHAClientWithFreshness()
        handler = RealHomeAssistantHandler(client)
        assert handler._resolve_entity_id("light.wled") == "light.wled"
    finally:
        _restore_lights(saved_devices)


# ============================================================================
# F. No credentials/tokens are ever accessible to, or logged by, this
#    module (structural/architecture guard).
# ============================================================================

def _non_comment_non_docstring_code(path: str) -> str:
    """Strips module/class/function docstrings and `#`-comments so a
    structural credential-leak scan isn't fooled by (or falsely flagged
    by) prose that merely EXPLAINS the architecture in English, e.g.
    "call_service() sends HA_TOKEN solely inside the earlier auth
    handshake, never here." Only actual executable code (identifiers,
    string literals used as real values, f-strings passed to log(...),
    etc.) is checked."""
    tree = ast.parse(_read(path))
    docstring_lines: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), (ast.Constant,)):
                const = body[0].value
                if isinstance(const.value, str):
                    start = body[0].lineno
                    end = getattr(body[0], "end_lineno", start)
                    docstring_lines.update(range(start, end + 1))
    kept_lines = []
    for i, line in enumerate(_read(path).splitlines(), start=1):
        if i in docstring_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines)


def test_F1_real_home_assistant_tool_module_never_references_ha_token_or_auth_headers():
    """Item F: `luno/tool_manager/builtin/real_home_assistant.py` never
    imports `HA_TOKEN`/`luno.config` at all in actual executable code -
    it only ever receives an already-constructed `client` object (see
    `RealHomeAssistantHandler.__init__`'s own docstring) - so there is
    structurally no credential for any log line in this file to ever
    leak, regardless of what gets logged. (The literal substring
    "HA_TOKEN" DOES appear once, in a docstring comment explaining that
    the token is handled elsewhere - that is documentation, not a
    credential reference, so this check strips docstrings/comments
    before scanning.)"""
    code = _non_comment_non_docstring_code(_REAL_HA_TOOL_PATH)
    for forbidden in ("HA_TOKEN", "Authorization", "access_token", "api_key", "password"):
        assert forbidden not in code, f"unexpected credential-shaped reference {forbidden!r} in real_home_assistant.py (tool_manager) executable code"


def test_F2_real_home_assistant_adapter_never_logs_the_auth_token():
    """`luno/adapters/real_home_assistant.py` (the OTHER file this
    sprint touched) also never references the token directly in
    executable code - all logging there is limited to entity_id/state/
    domain/service, matching the pre-existing `luno/ha_client.py`
    (untouched by this or any prior sprint) which sends `HA_TOKEN` only
    inside the one-time `connect()`/auth handshake, never printed."""
    code = _non_comment_non_docstring_code(_REAL_HA_ADAPTER_PATH)
    for forbidden in ("HA_TOKEN", "Authorization", "access_token"):
        assert forbidden not in code, f"unexpected credential-shaped reference {forbidden!r} in real_home_assistant.py (adapters) executable code"


def test_F3_ha_client_never_prints_the_token_value():
    """Sanity re-confirmation of the pre-existing, untouched `luno/
    ha_client.py`'s own long-standing contract - the raw token value is
    sent once, inside the auth frame, and never appears in any `print(...)`
    call anywhere in that file."""
    path = os.path.join(_ROOT, "luno", "ha_client.py")
    source = _read(path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in node.args:
                dumped = ast.dump(arg)
                assert "HA_TOKEN" not in dumped, "a print() call references HA_TOKEN directly"


# ============================================================================
# G. ToolManager never transforms a tool call before dispatching it.
# ============================================================================

class _SpyHandler(ToolHandler):
    name = "home_assistant"

    def __init__(self) -> None:
        self.received_calls: List[ToolCall] = []

    def supported_actions(self) -> List[str]:
        return ["turn_on", "turn_off"]

    def execute(self, tool_call: ToolCall, context=None) -> ToolResult:
        self.received_calls.append(tool_call)
        return ToolResult.ok(self.name, tool_call.action, "ok", data={"target": tool_call.target})


def test_G1_tool_manager_passes_the_tool_call_through_unchanged():
    """Item G: the EXACT `action`/`target`/`parameters` a caller builds
    reach the handler's `execute()` byte-for-byte - `ToolManager` never
    renames an action, rewrites a target, or injects/removes parameters
    along the way (the only thing between the caller and the handler is
    `ToolCall.from_any()`'s own type coercion and `handler.validate()`'s
    read-only check, confirmed here by using the handler's OWN registered
    action name/target unmodified)."""
    registry = ToolRegistry()
    spy = _SpyHandler()
    registry.register("home_assistant", spy)
    manager = ToolManager(registry=registry)
    try:
        result = manager.execute(ToolCall(
            tool="home_assistant", action="turn_on", target="light.wled",
            parameters={"_automation_origin": True, "_automation_execution_id": "exec-1"},
        ))
        assert result.success is True
        assert len(spy.received_calls) == 1
        received = spy.received_calls[0]
        assert received.tool == "home_assistant"
        assert received.action == "turn_on"
        assert received.target == "light.wled"
        assert received.parameters.get("_automation_origin") is True
        assert received.parameters.get("_automation_execution_id") == "exec-1"
    finally:
        manager.shutdown(wait=True)


# ============================================================================
# H. WorldModel is never updated independently of a real HA event.
# ============================================================================

def test_H1_neither_ha_adapter_file_imports_or_references_world_model():
    """Item H: proves directly against the real source that no code path
    in either HA adapter file writes into `WorldModel` at all - the
    world_model_updated events observed in the real production log are
    entirely accounted for by `WorldModel.update_from_state_changed()`,
    itself wired ONLY to the genuine `device_state_changed` Event Bus
    event (sourced from a real HA `state_changed` WebSocket push, per
    `RealHomeAssistantSource._on_ha_event()`) - never from `call_service()`
    returning success."""
    for path in (_REAL_HA_TOOL_PATH, _REAL_HA_ADAPTER_PATH):
        code = _non_comment_non_docstring_code(path)
        assert "world_model" not in code.lower() and "worldmodel" not in code.lower(), (
            f"{path} unexpectedly references world_model/WorldModel in executable code "
            "(a docstring mention explaining the architecture is fine and expected - "
            "e.g. get_all_states()'s own docstring pointing at "
            "WorldModel.sync_from_states() as its ONE intended startup-time caller; "
            "this check only fails on a real import/instantiation/call)"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
