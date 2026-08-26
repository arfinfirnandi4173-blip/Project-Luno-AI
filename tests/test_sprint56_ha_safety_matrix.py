"""
tests/test_sprint56_ha_safety_matrix.py
========================================

Sprint 56 (Home Assistant + Query Intelligence), Phases 9-11 - takeover
re-verification of Sprint 52's REAL tiered entity resolver
(`RealHomeAssistantHandler._resolve_entity_tiered()` in
`luno/tool_manager/builtin/real_home_assistant.py`) plus ONE new,
genuinely-reproduced test closing the specific "Category L" scenario the
Sprint 56 brief calls out by name: a typo that is textually CLOSER to a
WRONG device than to the one actually meant.

This file does NOT add a second resolver, a second ambiguity policy, or
any HA-specific hardcoding - it exercises the existing, unmodified
Sprint 52 resolver against this checkout's own real configured devices
(read live from `luno.devices`, never invented names), the same
fixture convention `tests/test_sprint52_ha_entity_resolution.py`
already established.

**Category L finding:** a `difflib`-based sweep across dozens of
corrupted spellings of "RGB Strip" and "RGB Computer" (this checkout's
two most textually similar real devices) found no NATURAL typo that
mis-ranks the wrong device as the top scorer - every realistic
corruption still ranks the intended device highest. A deliberately
ADVERSARIAL corruption ("rgb cprip") was needed to produce a genuine
near-tie (RGB Strip 0.67 vs RGB Computer 0.57 - within the 0.15 margin
bar), and `test_L_adversarial_near_tie_refuses_never_misactivates`
below proves the resolver's ambiguity gate correctly refuses rather
than guessing, end-to-end through `execute()`, with zero calls made to
either device's `call_service()`. This is real, live-executed proof of
the sprint's own hard invariant: "a typo must NEVER activate a
different device merely for highest similarity."
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _handler, _patch_real_devices, _restore_devices,
    _set_env, _restore_env,
)

from luno.tool_manager.builtin.real_home_assistant import _score_candidates  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402


def test_L_natural_typo_sweep_never_misranks_the_wrong_device():
    """Every one of these realistic corruptions of 'RGB Strip'/'RGB
    Computer' (this checkout's two most textually similar real devices)
    must still rank the CORRECT device as the top scorer - proving
    ordinary typos/mishearings never naturally drift toward the wrong
    device, before even reaching the ambiguity gate."""
    saved = _patch_real_devices()
    try:
        strip_typos = ["rgb strip", "rgb strp", "rgbstrip", "rg strip", "rgb stip", "gb strip", "rb strip", "rgb setrip"]
        for typo in strip_typos:
            scored = sorted(_score_candidates(typo), key=lambda s: -s[0])
            top_name = scored[0][2]
            assert top_name == "RGB Strip", f"{typo!r} wrongly top-ranked {top_name!r}, not RGB Strip: {scored}"

        computer_typos = ["rgb computer", "rgb komputer", "rgb comp", "rgb komp", "rgb kompute", "rgb komputr"]
        for typo in computer_typos:
            scored = sorted(_score_candidates(typo), key=lambda s: -s[0])
            top_name = scored[0][2]
            assert top_name in ("RGB Computer", "RGB komputer"), (
                f"{typo!r} wrongly top-ranked {top_name!r}, not RGB Computer: {scored}"
            )
    finally:
        _restore_devices(saved)


def test_L_adversarial_near_tie_refuses_never_misactivates():
    """A deliberately adversarial corruption engineered to land within
    the fuzzy margin bar of BOTH 'RGB Strip' and 'RGB Computer' -
    proves the live resolver refuses end-to-end through `execute()`
    rather than guessing either one. Zero `call_service()` calls must
    be made to EITHER device - not just "the right one was chosen", but
    "no device was touched at all" when the evidence is genuinely
    insufficient."""
    saved = _patch_real_devices()
    saved_env = _set_env(FUZZY_ENTITY_MIN_CONFIDENCE=0.5, FUZZY_ENTITY_MIN_MARGIN=0.15)
    try:
        scored = sorted(_score_candidates("rgb cprip"), key=lambda s: -s[0])
        top_two = scored[:2]
        assert {n for _s, _e, n in top_two} == {"RGB Strip", "RGB Computer"}, (
            f"fixture drifted, no longer a genuine near-tie: {scored}"
        )
        assert (top_two[0][0] - top_two[1][0]) < 0.15, (
            f"fixture drifted, no longer within the margin bar: {scored}"
        )

        client = FakeHAClient()
        h = _handler(client)
        result = h.execute(ToolCall(tool="home_assistant", action="turn_off", target="rgb_cprip"))

        assert result.success is False, f"must refuse, not guess: {result.message!r}"
        assert client.calls == [], f"must never touch EITHER device when ambiguous: {client.calls}"
    finally:
        _restore_devices(saved)
        _restore_env(saved_env)


def test_ambiguity_gate_still_never_auto_resolves_two_distinct_contenders():
    """Direct re-verification of Sprint 52's own core safety gate
    (mirrors that sprint's `test_T`, kept here as a Sprint 56 takeover
    cross-check rather than assumed correct from documentation alone -
    per this sprint's own 'source of truth is the code, verify it
    yourself' mandate)."""
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        resolution = h._resolve_entity_tiered("rgb cprip")
        assert resolution.executable is False, "two distinct near-tied contenders must never be executable"
        assert resolution.resolved_entity is None
    finally:
        _restore_devices(saved)


def test_exact_alias_fuzzy_priority_order_is_still_respected():
    """Hard invariant #1: exact > alias > fuzzy. An EXACT name match
    must resolve via the exact tier even when a fuzzy-adjacent device
    also exists in the registry - the fuzzy tier must never even be
    consulted once an earlier tier already found a match."""
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        resolution = h._resolve_entity_tiered("RGB Strip")
        assert resolution.resolution_method in ("exact", "alias")
        assert resolution.resolved_entity == "light.wled"
        assert resolution.confidence == 1.0 or resolution.confidence is None or resolution.confidence >= 0.99
    finally:
        _restore_devices(saved)


def test_no_llm_or_network_call_in_the_resolution_path():
    """Hard invariant: no LLM inference, no network call, for simple
    entity resolution. Checked structurally at the MODULE IMPORT level
    (mirrors Sprint 52's own `test_score_candidates_uses_only_stdlib_
    difflib` convention of inspecting real import/dependency markers,
    not prose) - `real_home_assistant.py` must import no LLM adapter,
    no HTTP client library, and no embedding/vector-search dependency
    at all; the resolution tier is pure, in-process `difflib` string
    scoring."""
    import inspect
    from luno.tool_manager.builtin import real_home_assistant as rha
    module_src = inspect.getsource(rha)
    import_lines = "\n".join(
        line for line in module_src.splitlines()
        if line.strip().startswith(("import ", "from "))
    )
    forbidden_imports = ["openai", "openrouter", "anthropic", "requests", "httpx", "urllib", "socket", "embedding"]
    lowered = import_lines.lower()
    for literal in forbidden_imports:
        assert literal not in lowered, f"module imports must never reference {literal!r}: {import_lines}"
    fn_src = inspect.getsource(rha._score_candidates)
    assert "difflib" in fn_src and "SequenceMatcher" in fn_src, "must be traceable to stdlib difflib alone"


def test_no_domain_specific_hardcoding_in_resolver():
    """Hard invariant: no `if device == "..."`-style hardcoding of any
    specific device/product name inside the resolver's own decision
    logic. Checks for actual EQUALITY/MEMBERSHIP comparisons against a
    literal device name (the actual hardcoding shape the invariant
    forbids), not mere mentions in comments/docstrings - Sprint 52's own
    module docstring and this file's own module docstring both
    legitimately name real devices as illustrative examples, which is
    not the same thing as the resolver's DECISION LOGIC branching on
    one. Re-verifies Sprint 52's own documented discipline still holds
    after Sprint 56's unrelated changes elsewhere in the codebase (this
    sprint never touched `real_home_assistant.py`)."""
    import ast
    import inspect
    import textwrap
    from luno.tool_manager.builtin import real_home_assistant as rha

    src = textwrap.dedent(inspect.getsource(rha.RealHomeAssistantHandler._resolve_entity_tiered))
    tree = ast.parse(src)
    forbidden = {"main lamp", "rgb strip", "rgb computer", "baterai", "aquascape", "gaming mode"}
    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in [node.left] + list(node.comparators):
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    if side.value.strip().lower() in forbidden:
                        offending.append(side.value)
    assert not offending, f"resolver decision logic must stay device-agnostic - found literal comparison(s): {offending}"
