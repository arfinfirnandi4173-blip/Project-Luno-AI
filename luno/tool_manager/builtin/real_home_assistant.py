"""
real_home_assistant.py
========================

`RealHomeAssistantHandler` - the "future" handler `home_assistant.py`'s
own docstring already sketches out ("a real handler would subclass
ToolHandler the same way ... call luno.ha_client.HomeAssistantClient
inside execute() ... swapping it in is exactly:
`registry.register("home_assistant", RealHomeAssistantHandler())`, no
Planner or Tool Manager changes required"). This IS that handler.

Two things it deliberately reuses rather than reimplements:

  1. The actual HA connection: `luno.adapters.real_home_assistant.
     RealHomeAssistantClient` - already a synchronous
     `call_service(domain, service, entity_id, data) -> dict` facade
     bridged (via `asyncio.run_coroutine_threadsafe`) onto whichever
     background asyncio loop `RealHomeAssistantSource` is already
     running for `HomeAssistantAdapter`'s own inbound state listening.
     Reusing it means ONE live HA WebSocket connection total, not a
     second one opened per tool call. Sprint "Reliability" also reuses
     that same source's `get_entity_state()` (added alongside this
     sprint) for the verification step below.

  2. Device-name resolution: `luno.devices.LIGHTS` / `SWITCHES` /
     `SCRIPTS` - the SAME name/alias -> entity_id registries the legacy
     `luno/main.py` assistant already reads from `config/lights.config.
     json` / `switches.config.json` / `scripts.config.json` (falling
     back to `.env`'s `RGB_LIGHT_NAME`/`RGB_LIGHT_ENTITY` for lights
     when no JSON file exists - see `devices.py`'s own docstring).
     `luno.planner.parser.IntentParser` slugifies whatever the user
     said ("the RGB strip" -> "rgb_strip") into `ToolCall.target`
     BEFORE this handler ever sees it, so resolution here normalizes
     both sides (lowercase, non-alnum -> "_") and compares.

Opt-in only, same convention as the adapter-layer real HA client this
reuses: constructed and registered by `luno/bootstrap/modules.py` ONLY
when `HOME_ASSISTANT_BACKEND=real` AND a `RealHomeAssistantSource` was
actually built - `luno/tool_manager/builtin/__init__.py::register_all()`
keeps registering `MockHomeAssistantHandler` by default, exactly as
before, for every other case (mock backend, or real backend that failed
to import/construct).

--------------------------------------------------------------------
Reliability Sprint - verified execution (golden rule: never claim an
action succeeded unless it's been objectively verified)
--------------------------------------------------------------------
For `turn_on`/`turn_off`/`toggle`, `execute()` no longer trusts "HA
accepted the service call" as success. After the call is accepted it
re-reads the entity's REAL state (via `RealHomeAssistantClient.
get_entity_state()`) with a configurable retry/delay/timeout window,
and only reports success once the actual state matches what was
requested - see `_execute_on_off()`. `ToolResult.data` always carries
the full execution-result model the sprint asks for (request_id,
entity_id, requested_action, expected_state, actual_state, success,
verification_attempts, elapsed_time_ms, failure_reason) so a caller
that wants the raw facts (dashboard, LLM context) has them, while
`ToolResult.message` itself is already one of the sprint's exact
honest phrasings - so even a caller that only ever reads `.message`
can't accidentally produce a misleading confirmation.

`run_script`/`set_temperature` are unaffected: a script's "success" has
never had a simple binary target state to verify against and climate
attribute verification is a materially bigger feature, so both are left
exactly as they were - out of scope for this pass (see the accompanying
audit notes).

Config (`VERIFY_CONFIG_ENV_VARS` below), all read fresh on every
`execute()` call via `_VerifyConfig.from_env()` - so `VERIFY_DEVICE_STATE`
etc. are reloadable without a restart with zero extra plumbing, matching
the launcher's own config precedent (env / .env / config-file, see
`luno/bootstrap/launcher_config.py`) without importing across that
boundary (this package stays independent - see this docstring's own
"Opt-in only" note and `tool_manager`'s package-level independence rule).

--------------------------------------------------------------------
Verified Smart Home Execution sprint - lifecycle event visibility
--------------------------------------------------------------------
Extends (does NOT replace) the Reliability Sprint verify-loop above with
an OPTIONAL `on_verification_event(stage, payload)` callback, passed in
at construction time (default `None` - zero behavior change for every
existing caller/test that doesn't pass one). When given, `_execute_on_off`/
`_verify_state` call it at exactly the points a caller would want an
audit trail of: "started" (service call accepted, verify-loop about to
begin), "retry" (one read didn't match yet, another attempt remains),
and exactly one terminal stage - "verified", "failed" (retries
exhausted, HA answered but state never matched, including "reported
unavailable"), or "timeout" (the wall-clock budget ran out before every
retry could even be attempted). No event fires for the "already in the
requested state" shortcut's zero-work case beyond an immediate
"verified", and none fire at all when `VERIFY_DEVICE_STATE=false`
(there is nothing to honestly report on that axis - trusting the
service call is not verification).

This module still has zero import-time dependency on the Event Bus -
`luno/bootstrap/adapters.py::_register_real_home_assistant_handler` is
the ONE place that builds a real callback (closing over the already-
bound `ToolManagerBridgeModule._event_bus`) and passes it in, exactly
the same "opt-in, one clear integration point" convention already used
for the Adapter Layer's `RealHomeAssistantClient` itself.

--------------------------------------------------------------------
Sprint 52 - Robust Home Assistant Command & Entity Resolution
--------------------------------------------------------------------
Problem: `_resolve_entity_id()` (unchanged below) only ever does an
EXACT normalized name/alias lookup against `luno.devices.LIGHTS`/
`SWITCHES`/`SCRIPTS`. A typo'd/misheard target ("rg strip", "rbg
strip", "matikan rgb strp", "rgbstrip") never matches, falls straight
to `_unknown_device_result()`, and the user has to repeat themselves -
even though the EXISTING `difflib`-based `_suggest_similar_devices()`
already proves the right device was findable, it was only ever wired
up to phrase a "did you mean X?" question, never to act on it.

Fix: `_resolve_entity_tiered()` (new) wraps `_resolve_entity_id()`
(still the single source of truth for tier 1/2/3 - untouched, called
as-is, never reimplemented) and, ONLY when it finds nothing, adds a
bounded tier 4 - deterministic `difflib.SequenceMatcher` similarity
scored against every DISTINCT known device (`_score_candidates()`,
scores grouped by entity_id so two aliases of the SAME device can
never look like two competing candidates - see that function's own
docstring), gated by `_VerifyConfig.fuzzy_min_confidence`/
`fuzzy_min_margin` (env `FUZZY_ENTITY_MIN_CONFIDENCE`/
`FUZZY_ENTITY_MIN_MARGIN`, same "reloadable via env, no restart"
convention as `VERIFY_DEVICE_STATE` etc.). Auto-resolves ONLY when
exactly one distinct device clears both bars; two or more distinct
devices within the margin of each other is tier 5, "ambiguous" -
`executable=False`, no guessing, no different code path than today's
`_unknown_device_result()` (which already asks "which one did you
mean?" when `_suggest_similar_devices()` itself returns 2+ matches -
this sprint reuses that exact message, doesn't reinvent it). No
embeddings, no vector search, no second ranking system, no LLM judge,
no network call - `_score_candidates()` is pure in-process string
comparison over ~6-device-sized registries, same as the pre-existing
suggestion feature it sits next to.

`execute()`'s ONLY change: the two lines that used to call
`self._resolve_entity_id(target)` directly now call
`self._resolve_entity_tiered(target)` instead and read `.executable`/
`.resolved_entity` off the result - for every target that already
resolved via tier 1/2/3 (the overwhelming majority of real traffic,
and every existing test), `.executable`/`.resolved_entity` are
IDENTICAL to what `_resolve_entity_id()` alone already returned - the
fuzzy tier's scoring code doesn't even run unless tier 1/2/3 found
nothing. Every other branch of `execute()` (on/off verify loop,
run_script, set_temperature, set_color, set_brightness,
`_unknown_device_result()`'s own messages) is completely unmodified.

A genuine, narrow bug found and fixed as part of building tier 3
("alias") consistently across all three registries: `_lookup_script()`
never checked `cfg["aliases"]` at all (unlike `_lookup_light()`, which
always has) - `config/scripts.config.json`'s own `"gaming mode":
{"aliases": ["mode gaming"]}` entry was silently unreachable by its
alias. `_all_known_device_names()` (feeds the pre-existing suggestion
feature) had the same gap. Both fixed by the same 3-line pattern
`_lookup_light()` already used - a strict capability fix, not a
behavior change for anything that was working before.

Observability: every "fuzzy" (auto-resolved without an exact match) or
"ambiguous" (refused - two+ distinct devices both plausible) resolution
- the two outcomes this sprint actually introduces - is reported through
the SAME `on_verification_event(stage, payload)` hook this module
already has (`_emit_resolution()`, new, calls the existing `self._emit()`
- no new hook, no new isolation/try-except discipline needed) with a new
stage name, `"resolution"`. Exact/alias/literal matches and fully
unknown targets stay silent on purpose - see `_emit_resolution()`'s own
docstring for why (short version: matches the module's existing "no
event for nothing new to report" rule, and is what keeps the
pre-existing `test_events_unknown_device_emits_nothing` passing
unchanged). `luno/bootstrap/adapters.py`'s
`_VERIFICATION_STAGE_TO_EVENT_NAME` gets one new entry
(`"resolution": "EntityResolutionDecision"`) and `luno/adapters/
events.py` gets one new `Event` subclass, `EntityResolutionDecision` -
same pattern as `ActionVerificationStarted` et al., not a new
observability system (extends Sprint 50's event model, which already
established "each event backed by a real call site, opt-in via the
existing hook, never raw user text").

See `docs/change_impact/sprint52_ha_entity_resolution.md` for the full
writeup (root cause, tiers, safety gate math, tests, regression).
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..context import ExecutionContext
from ..handler import ToolHandler
from ..models import ToolCall
from ..result import ToolResult
from ..utils import generate_id, log
from .home_assistant import _SUPPORTED_ACTIONS

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: Same 10-color palette (plus Indonesian synonyms) as
#: `luno.planner.parser._COLOR_NAMES` - kept in sync BY CONVENTION, not
#: import (this package talks to `luno.adapters`/`luno.devices`, the
#: Planner package stays dependency-free from the rest of `luno` - see
#: that dict's own comment). `_classify_color_set()` already restricts
#: `tool_call.parameters["color"]` to exactly one of these keys before a
#: "set_color" ToolCall is ever produced, so a KeyError here would mean
#: the two dicts drifted out of sync, not a real user input problem.
_COLOR_RGB = {
    "red": (255, 0, 0), "merah": (255, 0, 0),
    "green": (0, 255, 0), "hijau": (0, 255, 0),
    "blue": (0, 0, 255), "biru": (0, 0, 255),
    "yellow": (255, 255, 0), "kuning": (255, 255, 0),
    "cyan": (0, 255, 255), "toska": (0, 255, 255),
    "magenta": (255, 0, 255), "fuschia": (255, 0, 255),
    "white": (255, 255, 255), "putih": (255, 255, 255),
    "orange": (255, 165, 0), "oranye": (255, 165, 0), "jingga": (255, 165, 0),
    "purple": (128, 0, 128), "ungu": (128, 0, 128),
    "pink": (255, 192, 203),
}

#: HA state strings that mean "not a real on/off/whatever value" - never
#: reported as if it were the requested state.
_UNAVAILABLE_STATES = (None, "unavailable", "unknown")

_EXPECTED_STATE_FOR_ACTION = {"turn_on": "on", "turn_off": "off"}

#: RGB color/brightness verification fix - reported: Vinn said "set rgb
#: strip to blue" and Luno replied "Done", but the physical strip never
#: changed at all. Root cause: unlike `_execute_on_off()`'s full
#: verify-loop, `set_color`/`set_brightness` used to just trust the HA
#: service call being ACCEPTED (`result.get("success")`) as proof the
#: light actually changed - which only proves HA didn't reject the
#: request, not that the entity's real attributes moved (an active
#: WLED effect/preset can silently keep overriding a plain `rgb_color`
#: call, for example). These tolerances are deliberately generous
#: (color-mode round-tripping - rgb -> the light's native mode and back
#: - is lossy) - the goal is catching "nothing happened at all", not
#: nitpicking a few points of gamma/whitepoint drift.
_COLOR_MATCH_TOLERANCE = 60  # sum of |delta| across all 3 channels
_BRIGHTNESS_MATCH_TOLERANCE = 20  # out of 0-255 (~8%)


def _normalize(name: str) -> str:
    """Same normalization on both sides of the name->entity_id lookup:
    lowercase, any run of non-alphanumeric characters collapses to a
    single "_", leading/trailing "_" stripped. Mirrors
    `luno.planner.parser._slugify()` closely enough that a target it
    already slugified ("rgb_strip") and a config key written naturally
    ("RGB Strip", "rgb strip") both normalize to the same string."""
    return _NON_ALNUM_RE.sub("_", (name or "").strip().lower()).strip("_")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class _VerifyConfig:
    """Read fresh (not cached) on every `execute()` call - see module
    docstring's "reloadable without a restart" note."""
    verify_device_state: bool = True
    verify_retries: int = 3
    verify_delay_ms: int = 300
    verify_timeout_ms: int = 3000
    entity_similarity_threshold: float = 0.6
    auto_suggest_similar: bool = True
    #: Sprint 52 - bounded fuzzy entity resolution safety gate. A
    #: candidate must score AT LEAST this well (0.0-1.0, `difflib.
    #: SequenceMatcher.ratio()`) against the best-matching name/alias of
    #: some DISTINCT device before it is even considered for
    #: auto-execution - deliberately stricter than
    #: `entity_similarity_threshold` (0.6) above, which only ever gates
    #: a "did you mean...?" SUGGESTION, never an actual action. Chosen
    #: (see `docs/change_impact/sprint52_ha_entity_resolution.md` for the
    #: worked numbers) so that real single/double-character typos and
    #: word-order-preserving spacing variants on the project's actual
    #: registered devices score 0.85-0.97, comfortably above this bar,
    #: while a bare, genuinely-too-little-information fragment ("rgb"
    #: alone) scores well below it.
    fuzzy_min_confidence: float = 0.78
    #: The winning candidate's score must beat the NEXT-BEST *distinct*
    #: device's score by at least this much, or the match is refused as
    #: ambiguous even if it individually cleared `fuzzy_min_confidence` -
    #: see `_resolve_entity_tiered()`'s own docstring for the exact
    #: "candidates in contention" definition this guards.
    fuzzy_min_margin: float = 0.15

    @classmethod
    def from_env(cls) -> "_VerifyConfig":
        return cls(
            verify_device_state=_bool_env("VERIFY_DEVICE_STATE", True),
            verify_retries=max(0, _int_env("VERIFY_RETRIES", 3)),
            verify_delay_ms=max(0, _int_env("VERIFY_DELAY_MS", 300)),
            verify_timeout_ms=max(0, _int_env("VERIFY_TIMEOUT_MS", 3000)),
            entity_similarity_threshold=_float_env("ENTITY_SIMILARITY_THRESHOLD", 0.6),
            auto_suggest_similar=_bool_env("AUTO_SUGGEST_SIMILAR", True),
            fuzzy_min_confidence=_float_env("FUZZY_ENTITY_MIN_CONFIDENCE", 0.78),
            fuzzy_min_margin=_float_env("FUZZY_ENTITY_MIN_MARGIN", 0.15),
        )


@dataclass(frozen=True)
class EntityResolutionResult:
    """Sprint 52 - the structured record of how (or whether) a spoken/
    typed `target` string was turned into a real HA `entity_id`. See
    this module's own "Sprint 52" docstring section for the full
    picture; `resolution_method` is one of:

      "exact"             - `target` normalized to exactly one
                             registered device NAME (tiers 1-2 -
                             `_resolve_entity_id()`, unchanged, found it
                             via `_lookup_light/switch/script()`)
      "alias"              - same underlying lookup as "exact", but the
                             match was against a registered ALIAS, not
                             the primary name (distinguished here only
                             for observability - not a separate code
                             path; see `_classify_exact_match()`)
      "entity_id_literal"  - `target` already looked like a raw
                             "domain.object_id" and was used as-is
                             (pre-existing passthrough, untouched)
      "fuzzy"              - NEW (tier 4): no exact/alias/literal match,
                             but bounded `difflib` similarity cleared
                             both `fuzzy_min_confidence` and
                             `fuzzy_min_margin` against every OTHER
                             distinct device, with exactly one device
                             left "in contention"
      "ambiguous"          - NEW (tier 5): two or more DISTINCT devices
                             scored within `fuzzy_min_margin` of each
                             other - refused, `executable=False`
      "unknown"            - nothing cleared `fuzzy_min_confidence` at
                             all - refused, `executable=False`

    `confidence` is 1.0 for exact/alias/entity_id_literal, the winning
    `difflib` ratio (0.0-1.0) for fuzzy/ambiguous/unknown.
    `candidate_count` is the number of DISTINCT entity_ids "in
    contention" (see `_resolve_entity_tiered()`) for fuzzy/ambiguous/
    unknown - always 1 for exact/alias/entity_id_literal, 0 only for a
    completely empty candidate pool (no devices configured at all).
    `ambiguity` is True only for the "ambiguous" method. `executable`
    is what `execute()` actually branches on."""
    raw_target: str
    normalized_target: str
    resolved_entity: Optional[str]
    resolution_method: str
    confidence: float
    candidate_count: int
    ambiguity: bool
    executable: bool


class RealHomeAssistantHandler(ToolHandler):
    name = "home_assistant"
    default_timeout_s = 15.0  # matches RealHomeAssistantClient's own _CALL_TIMEOUT_S
    max_timeout_s = 20.0

    def __init__(
        self, client: Any,
        on_verification_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        """`client` - a `luno.adapters.real_home_assistant.
        RealHomeAssistantClient` (or anything with the same sync
        `call_service(domain, service, entity_id, data) -> dict`
        shape, and ideally also `get_entity_state(entity_id) -> str |
        None` for verification - kept as `Any` rather than importing
        that class here so this module has zero import-time dependency
        on the Adapter Layer; `luno/bootstrap/modules.py` is the only
        place that wires the two together). A client without
        `get_entity_state()` still works - verification is skipped and
        the pre-sprint "trust the service call" behavior is used, so
        older test doubles don't break.

        `on_verification_event` - optional `(stage, payload) -> None`
        callback (see module docstring's "Verified Smart Home Execution
        sprint" section); `stage` is one of "started"/"retry"/"verified"/
        "failed"/"timeout", plus "resolution" (Sprint 52 - see
        `_emit_resolution()`). Defaults to `None` - every existing caller
        that doesn't pass one gets exactly the pre-sprint behavior, no
        events, no new failure surface."""
        self._client = client
        self._on_verification_event = on_verification_event
        self._lock = threading.Lock()

    def _emit(self, stage: str, payload: Dict[str, Any]) -> None:
        hook = self._on_verification_event
        if hook is None:
            return
        try:
            hook(stage, payload)
        except Exception as ex:
            # A broken/misbehaving listener must never be able to affect
            # the actual verification result or the ToolResult returned
            # to the caller - same isolation discipline as EventBus's own
            # per-subscriber exception handling.
            log(f"on_verification_event('{stage}') hook raised (ignored): {ex}")

    def supported_actions(self) -> List[str]:
        return list(_SUPPORTED_ACTIONS)

    def validate(self, tool_call: ToolCall) -> Optional[str]:
        error = super().validate(tool_call)
        if error:
            return error
        # P0.14 - `call_service` is a generic passthrough with no single
        # `target` field at all (see this class's own P0.14 docstring
        # section) - validated on domain/service instead, same shape as
        # `MockHomeAssistantHandler.validate()`'s own P0.14 addition.
        if tool_call.action == "call_service":
            domain = tool_call.parameters.get("domain")
            service = tool_call.parameters.get("service")
            if not domain or not isinstance(domain, str):
                return "Action 'call_service' requires parameters.domain"
            if not service or not isinstance(service, str):
                return "Action 'call_service' requires parameters.service"
            return None
        if tool_call.action not in ("run_script", "call_service") and not tool_call.target:
            return f"Action '{tool_call.action}' requires a 'target' (e.g. a device/entity name)"
        if tool_call.action == "set_temperature" and "value" not in tool_call.parameters:
            return "Action 'set_temperature' requires parameters.value"
        if tool_call.action == "set_color" and "color" not in tool_call.parameters and "rgb" not in tool_call.parameters:
            return "Action 'set_color' requires parameters.color or parameters.rgb"
        if tool_call.action == "set_brightness" and "level" not in tool_call.parameters:
            return "Action 'set_brightness' requires parameters.level"
        return None

    def _resolve_entity_id(self, target: str) -> Optional[str]:
        """`target` is already slugified by the Planner's parser (e.g.
        "rgb_strip"). Checks lights, then switches, then scripts -
        first match wins (a device shouldn't reasonably be registered
        under the same name in two of these, but if it is, lights takes
        priority since that's by far the most common voice-command
        target). Falls back to treating `target` itself as a literal
        entity_id if it already looks like one ("domain.object_id") -
        covers a caller that already passes a real entity_id straight
        through instead of a spoken device name."""
        wanted = _normalize(target)

        # Checked as three separate small helpers (below) rather than one
        # long branch, so each config shape's own key/alias handling stays
        # readable - lights, then switches, then scripts, first match wins.
        entity_id = _lookup_light(wanted) or _lookup_switch(wanted) or _lookup_script(wanted)
        if entity_id:
            return entity_id

        if "." in target and " " not in target:
            return target  # already looks like a real entity_id - use as-is

        return None

    def _resolve_entity_tiered(self, target: str) -> EntityResolutionResult:
        """Sprint 52 - wraps `_resolve_entity_id()` (unchanged, called
        as-is - the single source of truth for tiers 1-3) and adds a
        bounded tier 4 (fuzzy) / tier 5 (ambiguous/unknown) ONLY when it
        finds nothing. See this module's own "Sprint 52" docstring
        section for the full picture.

        "Candidates in contention" (used for both the ambiguity check
        and `candidate_count`): every DISTINCT device whose best score
        is within `fuzzy_min_margin` of the single best score - i.e. not
        just the runner-up, but everyone close enough to the winner to
        be a real competing possibility. Exactly one entity in
        contention -> resolve to it (tier 4, "fuzzy"). Two or more ->
        refuse (tier 5, "ambiguous") - this is what makes "rgb comp"
        confidently resolve to 'RGB Computer' (its only real competitor,
        'RGB Strip', scores far below the margin) while two genuinely
        close devices would refuse rather than guess."""
        raw = target
        normalized = _normalize(target)

        exact = self._resolve_entity_id(target)
        if exact:
            if _lookup_light(normalized) or _lookup_switch(normalized) or _lookup_script(normalized):
                method = _classify_exact_match(normalized)
            else:
                method = "entity_id_literal"
            return EntityResolutionResult(raw, normalized, exact, method, 1.0, 1, False, True)

        cfg = _VerifyConfig.from_env()
        scored = _score_candidates(normalized)
        if not scored:
            return EntityResolutionResult(raw, normalized, None, "unknown", 0.0, 0, False, False)

        scored.sort(key=lambda t: t[0], reverse=True)
        top_score = scored[0][0]
        if top_score < cfg.fuzzy_min_confidence:
            return EntityResolutionResult(raw, normalized, None, "unknown", top_score, 0, False, False)

        contenders = [s for s in scored if s[0] >= top_score - cfg.fuzzy_min_margin]
        distinct_entities = {s[1] for s in contenders}
        if len(distinct_entities) > 1:
            return EntityResolutionResult(
                raw, normalized, None, "ambiguous", top_score, len(distinct_entities), True, False,
            )

        top_entity = scored[0][1]
        return EntityResolutionResult(raw, normalized, top_entity, "fuzzy", top_score, 1, False, True)

    def _emit_resolution(self, resolution: Optional[EntityResolutionResult]) -> None:
        """Sprint 52 observability - reuses the existing `self._emit()`
        hook (same isolation/try-except discipline, no new plumbing)
        with a new `"resolution"` stage.

        Deliberately fires ONLY for `"fuzzy"`/`"ambiguous"` - the two
        outcomes this sprint actually introduces (a target was
        auto-resolved WITHOUT an exact match, or was refused because two
        distinct devices were both plausible). `"exact"`/`"alias"`/
        `"entity_id_literal"` (tier 1-3, the overwhelming majority of
        real traffic - unchanged from before this sprint) and
        `"unknown"` (nothing found at all - also unchanged from before
        this sprint, `_unknown_device_result()`'s own message is still
        the only signal) are intentionally silent, matching the existing
        module-wide convention that "no event fires when there is
        nothing new to honestly report" (see `_execute_on_off()`'s own
        "verify disabled" shortcut, same rule). This is also what keeps
        `test_events_unknown_device_emits_nothing` (pre-existing,
        `tests/test_real_home_assistant_verification.py`) passing
        unchanged - a real regression this exact design was checked
        against, not a coincidence. No-ops if no hook was passed at
        construction (the overwhelming majority of tests/callers) or if
        there was nothing to resolve (empty target)."""
        if resolution is None or resolution.resolution_method not in ("fuzzy", "ambiguous"):
            return
        self._emit("resolution", {
            "raw_target": resolution.raw_target,
            "normalized_target": resolution.normalized_target,
            "resolved_entity": resolution.resolved_entity,
            "resolution_method": resolution.resolution_method,
            "confidence": resolution.confidence,
            "candidate_count": resolution.candidate_count,
            "ambiguity": resolution.ambiguity,
            "executable": resolution.executable,
        })

    def execute(self, tool_call: ToolCall, context: Optional[ExecutionContext] = None) -> ToolResult:
        target = tool_call.target or ""
        resolution = self._resolve_entity_tiered(target) if target else None
        if resolution is not None:
            self._emit_resolution(resolution)
        entity_id = resolution.resolved_entity if (resolution is not None and resolution.executable) else None

        if target and entity_id is None:
            return self._unknown_device_result(tool_call, target)

        # Sprint 57 (Contextual Home Assistant References) message-
        # quality fix - Sprint 56's own flagged finding: the guard just
        # above only ever fired when `target` was truthy, so a command
        # with NO target at all (`target` empty/None - e.g. a bare
        # "Matikan." that `_apply_device_context()` had no fresh/
        # compatible remembered device to fill in for) fell straight
        # through into `_execute_on_off`/the set_* branches with
        # `entity_id=None` AND `target=""`, producing the genuinely
        # confusing `"None is currently unavailable."` message
        # (`friendly = target or entity_id` = `None`). `run_script` is
        # deliberately exempt: it has its own legitimate no-target path
        # (`script_entity = entity_id or (target or tool_call.
        # parameters.get("script"))`) that must keep working exactly as
        # before - this fix only changes what happens when NEITHER a
        # target NOR a resolvable device exists for every other action.
        # P0.14 - `call_service` has no `target`/device-registry resolution
        # concept at all (see this class's own P0.14 docstring section) -
        # exempted from the "missing target" refusal the same way
        # `run_script` already was above.
        if entity_id is None and not target and tool_call.action not in ("run_script", "call_service"):
            return self._missing_target_result(tool_call)

        with self._lock:
            if tool_call.action in ("turn_on", "turn_off", "toggle"):
                return self._execute_on_off(tool_call, entity_id, target)

            if tool_call.action == "run_script":
                script_entity = entity_id or (target or tool_call.parameters.get("script"))
                if not script_entity:
                    return ToolResult.fail(self.name, tool_call.action, "run_script requires a target or parameters.script")
                # P0.14 - `variables` (optional) is the ONLY new behavior
                # here: absent, this is byte-for-byte the pre-P0.14 call
                # (generic `homeassistant.turn_on`, which already worked
                # for every voice-command script invocation this project
                # has ever made). Present, the call routes through the
                # script's own domain service instead - real Home
                # Assistant only accepts a `variables` payload on
                # `script.turn_on`, never on the generic `homeassistant`
                # domain's service.
                variables = tool_call.parameters.get("variables")
                if isinstance(variables, dict) and variables:
                    result = self._client.call_service("script", "turn_on", entity_id=script_entity, data={"variables": variables})
                else:
                    result = self._client.call_service("homeassistant", "turn_on", entity_id=script_entity)
                return self._to_tool_result(tool_call, result, f"ran script '{script_entity}'", data_key="script", data_value=script_entity)

            if tool_call.action == "activate_scene":
                scene_entity = entity_id or target
                if not scene_entity:
                    return ToolResult.fail(self.name, tool_call.action, "activate_scene requires a target")
                # A scene is activated in Home Assistant via the generic
                # `homeassistant.turn_on` service targeting the scene
                # entity (the same mechanism `turn_on`/`turn_off` above
                # already use for lights/switches) - no new HA connection
                # or client method, same call_service() facade.
                result = self._client.call_service("homeassistant", "turn_on", entity_id=scene_entity)
                return self._to_tool_result(tool_call, result, f"activated scene '{scene_entity}'", data_key="scene", data_value=scene_entity)

            if tool_call.action == "call_service":
                # P0.14 (Section 4) - a controlled, generic HA service
                # call. `domain`/`service` are already validated (lowercase
                # snake_case only - see `models.py::_HA_DOMAIN_SERVICE_RE`)
                # before a rule using this action type can even be saved;
                # `validate()` above re-checks it defensively for any
                # caller that bypasses rule validation. Multiple entity ids
                # are dispatched as one `call_service()` per entity (the
                # client's own facade only accepts a single `entity_id`
                # string per call - see `RealHomeAssistantClient.
                # call_service()`'s own signature) - never a second HA
                # client, never a raw HTTP request.
                domain = tool_call.parameters.get("domain")
                service = tool_call.parameters.get("service")
                entity_ids = tool_call.parameters.get("entity_id") or []
                if isinstance(entity_ids, str):
                    entity_ids = [entity_ids]
                data = dict(tool_call.parameters.get("data") or {})
                if not entity_ids:
                    result = self._client.call_service(domain, service, entity_id=None, data=data)
                    return self._to_tool_result(tool_call, result, f"called {domain}.{service}", data_key="service", data_value=f"{domain}.{service}")
                results = [self._client.call_service(domain, service, entity_id=eid, data=data) for eid in entity_ids]
                all_ok = all(r.get("success") for r in results)
                if all_ok:
                    return ToolResult.ok(
                        self.name, tool_call.action, f"called {domain}.{service} on {len(entity_ids)} target(s)",
                        data={"domain": domain, "service": service, "entity_id": entity_ids, "data": data},
                    )
                failed = [r for r in results if not r.get("success")]
                reason = failed[0].get("error") or "Home Assistant service call failed" if failed else "unknown error"
                return ToolResult.fail(
                    self.name, tool_call.action, f"called {domain}.{service} but {len(failed)}/{len(entity_ids)} target(s) failed: {reason}",
                    error_type="HomeAssistantError", retryable=True,
                    data={"domain": domain, "service": service, "entity_id": entity_ids, "data": data},
                )

            if tool_call.action == "set_temperature":
                value = tool_call.parameters["value"]
                domain = entity_id.split(".", 1)[0] if entity_id and "." in entity_id else "climate"
                result = self._client.call_service(domain, "set_temperature", entity_id=entity_id, data={"temperature": value})
                return self._to_tool_result(tool_call, result, f"set '{target}' temperature to {value}", data_key="temperature", data_value=value)

            if tool_call.action == "set_color":
                if "rgb" in tool_call.parameters:
                    rgb = tuple(max(0, min(255, int(c))) for c in tool_call.parameters["rgb"])
                    color_label = f"rgb{rgb}"
                    data_key, data_value = "rgb", list(rgb)
                else:
                    color = tool_call.parameters["color"]
                    rgb = _COLOR_RGB.get(color)
                    if rgb is None:
                        return ToolResult.fail(self.name, tool_call.action, f"Unknown color '{color}'")
                    color_label = color
                    data_key, data_value = "color", color
                domain = entity_id.split(".", 1)[0] if entity_id and "." in entity_id else "light"
                result = self._client.call_service(domain, "turn_on", entity_id=entity_id, data={"rgb_color": list(rgb)})
                if not result.get("success"):
                    return self._to_tool_result(tool_call, result, f"set '{target}' color to {color_label}", data_key=data_key, data_value=data_value)

                def _color_checker(attributes: Dict[str, Any], _expected: Tuple[int, int, int] = rgb) -> Tuple[bool, str]:
                    actual = attributes.get("rgb_color")
                    if not actual or len(actual) != 3:
                        return False, "no color reported back"
                    matched = sum(abs(a - b) for a, b in zip(actual, _expected)) <= _COLOR_MATCH_TOLERANCE
                    return matched, f"rgb{tuple(actual)}"

                verified, actual_desc = self._verify_light_attribute(entity_id, _color_checker)
                if verified is False:
                    return ToolResult.fail(
                        self.name, tool_call.action,
                        f"I told Home Assistant to set '{target}' to {color_label}, but the light is still reporting "
                        f"{actual_desc} - it might have an effect/preset active overriding the color, or the entity "
                        "doesn't support this color mode.",
                        error_type="AttributeNotVerified", retryable=True,
                        data={"target": target, data_key: data_value, "actual": actual_desc},
                    )
                return ToolResult.ok(self.name, tool_call.action, f"set '{target}' color to {color_label}",
                                      data={"target": target, data_key: data_value})

            if tool_call.action == "set_brightness":
                level = tool_call.parameters["level"]
                domain = entity_id.split(".", 1)[0] if entity_id and "." in entity_id else "light"
                result = self._client.call_service(domain, "turn_on", entity_id=entity_id, data={"brightness_pct": level})
                if not result.get("success"):
                    return self._to_tool_result(tool_call, result, f"set '{target}' brightness to {level}%", data_key="brightness", data_value=level)

                expected_255 = round(level / 100 * 255)

                def _brightness_checker(attributes: Dict[str, Any], _expected: int = expected_255) -> Tuple[bool, str]:
                    actual = attributes.get("brightness")
                    if actual is None:
                        return False, "no brightness reported back"
                    matched = abs(int(actual) - _expected) <= _BRIGHTNESS_MATCH_TOLERANCE
                    return matched, f"{round(int(actual) / 255 * 100)}%"

                verified, actual_desc = self._verify_light_attribute(entity_id, _brightness_checker)
                if verified is False:
                    return ToolResult.fail(
                        self.name, tool_call.action,
                        f"I told Home Assistant to set '{target}' brightness to {level}%, but the light is still "
                        f"reporting {actual_desc}.",
                        error_type="AttributeNotVerified", retryable=True,
                        data={"target": target, "brightness": level, "actual": actual_desc},
                    )
                return ToolResult.ok(self.name, tool_call.action, f"set '{target}' brightness to {level}%",
                                      data={"target": target, "brightness": level})

        # Unreachable given validate() already restricts action to
        # _SUPPORTED_ACTIONS, kept as a defensive fallback (mirrors
        # MockHomeAssistantHandler's own).
        return ToolResult.fail(self.name, tool_call.action, f"Unhandled action '{tool_call.action}'")

    # -- Reliability Sprint: verified on/off/toggle execution ---------------

    def _execute_on_off(self, tool_call: ToolCall, entity_id: str, target: str) -> ToolResult:
        action = tool_call.action
        friendly = target or entity_id
        request_id = generate_id("ha_verify")
        service = f"homeassistant.{action}"
        cfg = _VerifyConfig.from_env()

        current_state = self._safe_get_state(entity_id)

        expected_state = _EXPECTED_STATE_FOR_ACTION.get(action)
        if expected_state is None and action == "toggle":
            expected_state = _opposite_state(current_state)  # None if unknown starting state

        # "Device already ON"/"already OFF" - no need to call the service
        # (or run the verify loop) if reality already matches the
        # request. Still an honestly VERIFIED fact (we just read it) -
        # reported as "verified" with zero attempts, not skipped silently.
        if expected_state is not None and current_state == expected_state and action != "toggle":
            verb = "on" if expected_state == "on" else "off"
            message = f"{friendly} is already {verb}."
            data = _result_data(request_id, entity_id, action, expected_state, current_state,
                                 True, 0, 0.0, None, already_in_state=True)
            data["service"] = service
            data["message"] = message
            self._emit("verified", data)
            return ToolResult.ok(self.name, action, message, data=data)

        start = time.time()
        # P0.8.7 - diagnostic requirement (objective D of the brief: "one
        # controlled test where Luno executes home_assistant.turn_on and
        # capture: exact outbound HA request domain/service, exact
        # target entity, sanitized service data"). This logs ONLY the
        # domain/service/entity_id - never a token, password, API key, or
        # Authorization header (none of those are ever present in this
        # call's own arguments in the first place - `ha_client.py::
        # call_service()` sends `HA_TOKEN` solely inside the earlier,
        # separate `connect()`/auth handshake, never as part of a
        # `call_service` payload).
        log(f"P0.8.7 A->B: dispatching domain='homeassistant' service='{action}' entity_id='{entity_id}' "
            f"(request_id={request_id})")
        ha_result = self._client.call_service("homeassistant", action, entity_id=entity_id)
        log(f"P0.8.7 B: Home Assistant call_service response - success={ha_result.get('success')} "
            f"domain={ha_result.get('domain')} service={ha_result.get('service')} "
            f"entity_id={ha_result.get('entity_id')} error={ha_result.get('error')} "
            f"(this proves ONLY that HA accepted/ran the service call - not that any physical device changed)")

        if not ha_result.get("success"):
            reason = ha_result.get("error") or "Home Assistant service call failed"
            message = _call_failed_message(friendly, reason)
            data = _result_data(request_id, entity_id, action, expected_state, current_state,
                                 False, 0, _elapsed_s(start), reason)
            data["service"] = service
            data["message"] = message
            self._emit("failed", data)
            return ToolResult.fail(
                self.name, action, message, error_type="HomeAssistantError", retryable=True, data=data,
            )

        if not cfg.verify_device_state or expected_state is None:
            # Verification disabled (VERIFY_DEVICE_STATE=false), or a
            # toggle whose starting state we never knew - fall back to
            # trusting the accepted service call, same as before this
            # sprint. No lifecycle event fires here: there is nothing
            # verified to honestly report.
            data = _result_data(request_id, entity_id, action, expected_state, current_state,
                                 True, 0, _elapsed_s(start), None)
            data["service"] = service
            data["message"] = f"{action.replace('_', ' ')} '{friendly}'"
            return ToolResult.ok(self.name, action, data["message"], data=data)

        self._emit("started", {
            "request_id": request_id, "entity_id": entity_id, "requested_action": action,
            "service": service, "expected_state": expected_state, "current_state": current_state,
            "max_attempts": cfg.verify_retries + 1,
        })

        actual_state, attempts, timed_out = self._verify_state(entity_id, expected_state, cfg, request_id, service)
        elapsed_s = _elapsed_s(start)
        verified = actual_state == expected_state
        # P0.8.7 - the explicit A/B/C/D distinction the brief's own
        # "final verification" requirement demands: (A) Luno accepted
        # the automation/tool call - true simply by having reached this
        # line; (B) HA accepted the command - `ha_result["success"]`,
        # already logged above; (C) HA's OWN state machine now REPORTS
        # this entity as the expected state - `verified` here, from a
        # fresh (force_refresh=True) query, not a cached value; (D)
        # whether the physical device actually changed is NEVER claimed
        # by this line or anywhere else in this codebase - Luno has no
        # independent physical/optical sensing channel for any
        # HA-controlled device (see docs/change_impact/camera_
        # automation_p0_8_7.md).
        log(f"P0.8.7 C: fresh post-command HA state query for '{entity_id}' = '{actual_state}' "
            f"(expected '{expected_state}', {attempts} attempt(s), verified={verified}) - "
            f"D: physical device state is NOT independently confirmed by this or any check in this codebase")

        data = _result_data(
            request_id, entity_id, action, expected_state, actual_state, verified,
            attempts, elapsed_s, None if verified else _failure_reason(actual_state),
            state_query_freshness="fresh",
        )
        data["service"] = service

        if verified:
            verb = "turned on" if expected_state == "on" else "turned off"
            data["message"] = f"I've {verb} {friendly}."
            self._emit("verified", data)
            return ToolResult.ok(self.name, action, data["message"], data=data)

        if actual_state in _UNAVAILABLE_STATES:
            message = f"{friendly} is currently unavailable."
        else:
            verb = "on" if expected_state == "on" else "off"
            message = f"I tried to turn {verb} {friendly}, but it didn't respond."
        data["message"] = message
        self._emit("timeout" if timed_out else "failed", data)
        return ToolResult.fail(self.name, action, message, error_type="VerificationFailed", retryable=True, data=data)

    def _verify_state(
        self, entity_id: str, expected_state: str, cfg: "_VerifyConfig",
        request_id: Optional[str] = None, service: Optional[str] = None,
    ) -> Tuple[Optional[str], int, bool]:
        """Wait -> read -> compare, up to `verify_retries + 1` attempts,
        never running past `verify_timeout_ms` total wall-clock time -
        matches the sprint's own example (wait 300ms, read, still OFF,
        retry, read, now ON, success). Returns `(last_seen_state,
        attempts_made, timed_out)` - `timed_out` is True only when the
        wall-clock budget ran out BEFORE every configured retry could
        even be attempted (distinct from simply exhausting every attempt
        with time to spare, which is a plain verification failure, not a
        timeout). `request_id`/`service` are optional - only needed to
        label the "retry" lifecycle event; omitting them (pre-sprint
        callers) just means no correlation info accompanies that event."""
        deadline = time.time() + (cfg.verify_timeout_ms / 1000.0)
        max_attempts = cfg.verify_retries + 1
        delay_s = cfg.verify_delay_ms / 1000.0
        attempts = 0
        last_state: Optional[str] = None
        timed_out = False

        while attempts < max_attempts:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(delay_s, remaining))
            attempts += 1
            # P0.8.7 - root cause objective E/"final verification must
            # perform a fresh HA state query rather than trusting
            # internal/cached state": before this fix, `_safe_get_state()`
            # here read `RealHomeAssistantClient.get_entity_state()`'s
            # default (cache-first) behavior, which only ever returns
            # whatever `RealHomeAssistantSource._last_states` currently
            # holds - populated ONLY by real HA `state_changed` WS pushes
            # (never by Luno itself, confirmed by source audit; see
            # docs/change_impact/camera_automation_p0_8_7.md). That is
            # architecturally sound (HA's own state machine IS the ground
            # truth), but it means a verification "success" could be
            # reported off a state the client had cached from BEFORE this
            # specific command, if the fresh `state_changed` push for
            # THIS command was ever delayed, dropped, or coalesced -
            # `force_refresh=True` makes every verify attempt actively
            # re-query Home Assistant's `get_states` right now, rather
            # than passively trusting whatever is already sitting in the
            # cache. `_safe_get_state()` degrades gracefully to the old
            # cache-first behavior for any client that doesn't support the
            # `force_refresh` kwarg at all (test doubles, older callers).
            last_state = self._safe_get_state(entity_id, force_refresh=True)
            if last_state == expected_state:
                # P0.8.6 - wording fix (objective F/8 of the P0.8.6
                # brief): this line, and the "success"/"verified" result
                # it leads to, prove ONLY that Home Assistant's OWN
                # reported/cached entity state now matches what we asked
                # for - Luno has no independent, physical (e.g. optical)
                # channel to the device itself, so "HA reports the state
                # changed" is the most this can honestly claim. Never
                # phrased as "physical device confirmed" - see `_result_
                # data()`'s own `verification_scope` field below for the
                # same distinction surfaced in the structured result.
                log(f"verify '{entity_id}' attempt {attempts}: state={last_state} - HA reports state change accepted (not a physical device confirmation)")
                break
            if attempts < max_attempts:
                log(f"verify '{entity_id}' attempt {attempts}: state={last_state} (want '{expected_state}') - retrying")
                self._emit("retry", {
                    "request_id": request_id, "entity_id": entity_id, "service": service,
                    "expected_state": expected_state, "actual_state": last_state,
                    "attempt": attempts, "max_attempts": max_attempts,
                })
            else:
                log(f"verify '{entity_id}' attempt {attempts}: state={last_state} (want '{expected_state}') - verification failed")

        return last_state, attempts, timed_out

    def _safe_get_state(self, entity_id: Optional[str], force_refresh: bool = False) -> Optional[str]:
        """P0.8.7 - `force_refresh` (additive, default `False` - every
        pre-existing caller/behavior is unchanged) asks the client to
        perform a genuinely LIVE query against Home Assistant right now,
        rather than returning whatever it already has cached. Not every
        client necessarily supports the kwarg (test doubles, the mock
        HA handler's own client, or any future minimal client that only
        implements the single-argument shape already documented in
        `RealHomeAssistantHandler.__init__`'s own docstring) - falls back
        to the plain single-argument call on `TypeError` so this stays
        100% backward compatible rather than requiring every caller of
        this project's `get_entity_state()` contract to add the new
        parameter."""
        if not entity_id:
            return None
        getter = getattr(self._client, "get_entity_state", None)
        if getter is None:
            return None
        try:
            if force_refresh:
                try:
                    return getter(entity_id, force_refresh=True)
                except TypeError:
                    return getter(entity_id)
            return getter(entity_id)
        except Exception as ex:
            log(f"get_entity_state('{entity_id}') raised: {ex}")
            return None

    # -- Reliability Sprint: unknown-device -> similar-device suggestion ----

    def _unknown_device_result(self, tool_call: ToolCall, target: str) -> ToolResult:
        cfg = _VerifyConfig.from_env()
        suggestions = _suggest_similar_devices(target, cfg) if cfg.auto_suggest_similar else []

        if len(suggestions) == 1:
            message = f"I couldn't find '{target}'. Did you mean '{suggestions[0]}'?"
        elif len(suggestions) > 1:
            joined = ", ".join(f"'{s}'" for s in suggestions)
            message = f"I found {len(suggestions)} matching devices: {joined}. Which one did you mean?"
        else:
            message = (
                f"I couldn't find '{target}' - check config/lights.config.json, "
                "switches.config.json, scripts.config.json, or .env's RGB_LIGHT_NAME/RGB_LIGHT_ENTITY"
            )

        # Suggestions are ALWAYS surfaced as a question, never executed -
        # this stays a `fail()` result no matter how many/confident the
        # matches are (see sprint spec: "Never execute automatically").
        return ToolResult.fail(
            self.name, tool_call.action, message, error_type="UnknownDevice",
            data={"target": target, "suggestions": suggestions},
        )

    def _missing_target_result(self, tool_call: ToolCall) -> ToolResult:
        """Sprint 57 (Contextual Home Assistant References) - the
        honest, distinct refusal for "no device named at all" (e.g. a
        bare "Matikan." with no fresh/compatible remembered device to
        fill it in), kept SEPARATE from `_unknown_device_result` (which
        means "you named a device but I don't recognize it" and offers
        fuzzy suggestions) - there is no misspelled name here to
        suggest against, so a different message is honest rather than
        printing an empty-string "I couldn't find ''" or falling
        through into execution with a `None` target (the exact bug this
        fix closes - see `execute()`'s own comment at the call site)."""
        return ToolResult.fail(
            self.name, tool_call.action,
            "Which device did you mean? I don't have one to go on right now.",
            error_type="MissingTarget",
            data={"target": None},
        )

    def _to_tool_result(
        self, tool_call: ToolCall, ha_result: Dict[str, Any], ok_message: str,
        data_key: str = "target", data_value: Any = None,
    ) -> ToolResult:
        data = {data_key: data_value if data_value is not None else (tool_call.target or "")}
        if ha_result.get("success"):
            return ToolResult.ok(self.name, tool_call.action, ok_message, data=data)
        error = ha_result.get("error") or "Home Assistant service call failed"
        return ToolResult.fail(self.name, tool_call.action, error, error_type="HomeAssistantError", retryable=True, data=data)

    def _verify_light_attribute(
        self, entity_id: str, checker: Callable[[Dict[str, Any]], Tuple[bool, str]],
    ) -> Tuple[Optional[bool], Optional[str]]:
        """set_color/set_brightness verification - see the
        `_COLOR_MATCH_TOLERANCE`/`_BRIGHTNESS_MATCH_TOLERANCE` comment
        above for why this exists. Lighter than `_execute_on_off()`'s
        full retry loop (a single settle delay + a single read - color/
        brightness settle almost immediately on real lights, and there's
        no one canonical value to retry against the way on/off's binary
        state has). `checker(attributes) -> (matched, actual_description)`.

        Returns `(None, None)` - meaning "couldn't check, trust the
        service call" (today's pre-fix behavior) - whenever: verification
        is disabled (`VERIFY_DEVICE_STATE=false`, same env var the on/off
        verify loop already honors), the client doesn't expose
        `get_entity_attributes` at all (the optional-capability
        convention `get_entity_attributes()`'s own docstring documents -
        `MockHomeAssistantClient`/`FakeHAClient` in tests don't have it,
        so this is a complete no-op for every existing test), or the read
        itself raised/came back empty. Only ever returns `False` when an
        attribute WAS actually read back and it genuinely doesn't match -
        that's the one case worth an honest failure instead of a false
        "Done"."""
        cfg = _VerifyConfig.from_env()
        if not cfg.verify_device_state:
            return None, None
        get_attrs = getattr(self._client, "get_entity_attributes", None)
        if get_attrs is None:
            return None, None
        time.sleep(cfg.verify_delay_ms / 1000.0)
        try:
            attributes = get_attrs(entity_id)
        except Exception as ex:
            log(f"get_entity_attributes('{entity_id}') raised (skipping verification): {ex}")
            return None, None
        if not attributes:
            return None, None
        try:
            matched, actual_desc = checker(attributes)
        except Exception as ex:
            log(f"attribute verification checker raised (skipping verification): {ex}")
            return None, None
        return matched, actual_desc


def _elapsed_s(start: float) -> float:
    return time.time() - start


def _opposite_state(state: Optional[str]) -> Optional[str]:
    if state == "on":
        return "off"
    if state == "off":
        return "on"
    return None  # unknown/unavailable starting state - can't predict a toggle's target


def _call_failed_message(friendly: str, reason: str) -> str:
    """Distinguishes "Home Assistant itself is unreachable" from a
    generic per-call error, matching the sprint's own example wording
    ("I can't reach Home Assistant right now...")."""
    lowered = reason.lower()
    if "not connected" in lowered or "connect" in lowered:
        return "I can't reach Home Assistant right now. Please check if the server is online."
    return f"I tried to reach {friendly}, but the command failed: {reason}"


def _failure_reason(actual_state: Optional[str]) -> str:
    if actual_state in _UNAVAILABLE_STATES:
        return "Device unavailable"
    return f"Device did not reach the expected state in time (last seen: '{actual_state}')"


def _result_data(
    request_id: str, entity_id: Optional[str], action: str, expected_state: Optional[str],
    actual_state: Optional[str], success: bool, verification_attempts: int, elapsed_s: float,
    failure_reason: Optional[str], already_in_state: bool = False, state_query_freshness: str = "cached",
) -> Dict[str, Any]:
    """The sprint's Execution Result Model, as `ToolResult.data` (no
    `ToolResult` schema change - see module docstring)."""
    data: Dict[str, Any] = {
        "request_id": request_id,
        "entity_id": entity_id,
        "requested_action": action,
        "expected_state": expected_state,
        "actual_state": actual_state,
        "success": success,
        "verification_attempts": verification_attempts,
        "elapsed_time_ms": round(elapsed_s * 1000, 1),
        "failure_reason": failure_reason,
        "target": entity_id,  # kept for callers reading the pre-sprint "target" key
        # P0.8.6 (objective F/8) - honest, explicit statement of what
        # `success`/`actual_state` above actually prove: Home Assistant's
        # OWN reported entity state matched what was requested. This
        # project has no independent physical/optical confirmation
        # channel for any HA-controlled device - a WLED (or any other)
        # integration reporting `state=on` reflects what HA itself
        # believes, not a proof the physical hardware is lit. Additive
        # key - no existing reader of this dict is required to look at
        # it, matching this file's own "no ToolResult schema change"
        # convention (see this function's own docstring).
        "verification_scope": "ha_reported_state",
        # P0.8.7 - "fresh" only for the real post-command verify loop
        # (`_verify_state()`, which now always queries Home Assistant
        # live via `force_refresh=True` - see that method's own P0.8.7
        # comment), "cached" for every other call site (the "already in
        # desired state" pre-check, a failed service call with no state
        # query attempted, or verification disabled entirely) - lets any
        # caller/log reader immediately see whether a given result's
        # `actual_state` came from an active query against HA right now
        # or from whatever was already known beforehand.
        "state_query_freshness": state_query_freshness,
    }
    if already_in_state:
        data["already_in_state"] = True
    return data


def _all_known_device_names() -> List[str]:
    from luno import devices
    names: List[str] = []
    for key, cfg in devices.LIGHTS.items():
        names.append(key)
        names.extend(cfg.get("aliases") or [])
    names.extend(devices.SWITCHES.keys())
    for key, cfg in devices.SCRIPTS.items():
        names.append(key)
        # Sprint 52 bugfix: scripts can have "aliases" in
        # config/scripts.config.json (e.g. "gaming mode" -> ["mode
        # gaming"]) exactly like lights do, above - this loop simply
        # never read them, so an alias-only utterance was silently
        # unreachable even via the "did you mean...?" suggestion this
        # function feeds. `_lookup_script()` below had the identical gap.
        if isinstance(cfg, dict):
            names.extend(cfg.get("aliases") or [])
    return names


def _suggest_similar_devices(target: str, cfg: "_VerifyConfig") -> List[str]:
    """Fuzzy-matches `target` against every configured light/switch/script
    name and alias (`luno.devices` - the same registries `_resolve_entity_id`
    already reads), using stdlib `difflib` so this stays dependency-free.
    Never returns the raw entity_id, only the human-facing name it was
    registered under - what should actually be read back to the user."""
    wanted = _normalize(target)
    norm_to_display: Dict[str, str] = {}
    for display_name in _all_known_device_names():
        norm_to_display.setdefault(_normalize(display_name), display_name)

    close = difflib.get_close_matches(
        wanted, list(norm_to_display.keys()), n=5, cutoff=cfg.entity_similarity_threshold,
    )
    return [norm_to_display[c] for c in close]


def _lookup_light(wanted: str) -> Optional[str]:
    from luno import devices
    for key, cfg in devices.LIGHTS.items():
        if _normalize(key) == wanted:
            return cfg.get("entity_id")
        for alias in cfg.get("aliases") or []:
            if _normalize(alias) == wanted:
                return cfg.get("entity_id")
    return None


def _lookup_switch(wanted: str) -> Optional[str]:
    from luno import devices
    for key, entity_id in devices.SWITCHES.items():
        if _normalize(key) == wanted:
            return entity_id
    return None


def _lookup_script(wanted: str) -> Optional[str]:
    from luno import devices
    for key, cfg in devices.SCRIPTS.items():
        if _normalize(key) == wanted:
            return cfg.get("entity_id") if isinstance(cfg, dict) else cfg
        # Sprint 52 bugfix - see `_all_known_device_names()`'s matching
        # comment: scripts can have aliases too, this loop never checked.
        if isinstance(cfg, dict):
            for alias in cfg.get("aliases") or []:
                if _normalize(alias) == wanted:
                    return cfg.get("entity_id")
    return None


def _classify_exact_match(normalized: str) -> str:
    """Sprint 52 - observability-only label: was a successful
    `_resolve_entity_id()` match against a device's primary NAME, or one
    of its ALIASES? Re-walks the same registries `_lookup_light/switch/
    script()` already checked (cheap - a handful of devices) purely to
    tell the two apart for `EntityResolutionResult.resolution_method`;
    never changes which entity_id was already chosen."""
    from luno import devices
    for key in devices.LIGHTS:
        if _normalize(key) == normalized:
            return "exact"
    for key in devices.SWITCHES:
        if _normalize(key) == normalized:
            return "exact"
    for key in devices.SCRIPTS:
        if _normalize(key) == normalized:
            return "exact"
    return "alias"  # _resolve_entity_id() already succeeded, so it must have been an alias


def _all_known_device_entities() -> List[Tuple[str, str]]:
    """Sprint 52 - `(display_or_alias_name, entity_id)` for every light/
    switch/script name AND alias - the same registries
    `_all_known_device_names()` already reads for suggestions, just
    paired with the entity_id each name resolves to. `_score_candidates()`
    needs this pairing (not just the bare name list) so it can
    de-duplicate by DISTINCT ENTITY rather than by name/alias string -
    two aliases of the SAME device (e.g. "RGB Computer" and its alias
    "RGB komputer") must never look like two separate competing
    candidates in the ambiguity check."""
    from luno import devices
    pairs: List[Tuple[str, str]] = []
    for key, cfg in devices.LIGHTS.items():
        entity_id = cfg.get("entity_id")
        if not entity_id:
            continue
        pairs.append((key, entity_id))
        for alias in cfg.get("aliases") or []:
            pairs.append((alias, entity_id))
    for key, entity_id in devices.SWITCHES.items():
        if entity_id:
            pairs.append((key, entity_id))
    for key, cfg in devices.SCRIPTS.items():
        entity_id = cfg.get("entity_id") if isinstance(cfg, dict) else cfg
        if not entity_id:
            continue
        pairs.append((key, entity_id))
        if isinstance(cfg, dict):
            for alias in cfg.get("aliases") or []:
                pairs.append((alias, entity_id))
    return pairs


def _score_candidates(normalized_target: str) -> List[Tuple[float, str, str]]:
    """Sprint 52 bounded fuzzy tier - one score per DISTINCT entity_id
    (its single best-matching name/alias wins), computed with stdlib
    `difflib.SequenceMatcher` only - no embeddings, no vector search, no
    LLM judge, no network call (see this module's own "Sprint 52"
    docstring section for why). Returns `(score, entity_id,
    best_display_name)` tuples, unsorted - `_resolve_entity_tiered()`
    sorts and applies the confidence/margin/count safety gate."""
    best: Dict[str, Tuple[float, str]] = {}  # entity_id -> (score, display_name)
    for display_name, entity_id in _all_known_device_entities():
        score = difflib.SequenceMatcher(None, normalized_target, _normalize(display_name)).ratio()
        current = best.get(entity_id)
        if current is None or score > current[0]:
            best[entity_id] = (score, display_name)
    return [(score, entity_id, display_name) for entity_id, (score, display_name) in best.items()]
