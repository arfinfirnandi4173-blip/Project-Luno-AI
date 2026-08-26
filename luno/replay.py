"""
replay.py
==========

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture) - Phase 9/10: takes a captured real-world case
(`luno.test_capture`'s own JSON shape) and re-runs its `"conversation"`
through a FRESH `RuntimeDemoConsole` - the exact same production
`PlannerBridgeModule`/`memory_context` pipeline every prior sprint's own
E2E tests already exercise, never a second/simplified re-implementation
of any classification or ranking logic - then compares the resulting
`MemoryTurnTrace`'s own decision fields against the case's own
(optional, human-annotated) `"expected"` block.

Deliberately NEVER calls a real LLM: `RuntimeDemoConsole` is constructed
with `MockOpenRouterClient` (the same mock every Sprint 44-49 probe
script already uses), fed a small, DELIBERATELY GENERIC reply for every
turn (see `_GENERIC_REPLY` - "Baik, dicatat." never mentions any
domain-specific noun) so the assistant's own reply text can never leak
the "correct" answer into what gets classified - the same anti-leak
discipline this project's own probe scripts have followed since Sprint
46. Replay is therefore fully deterministic: same case in, same result
out, every time, matching this project's own "no LLM/embeddings in the
core intelligence path" invariant even for its own test-support tooling.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .test_capture import DEFAULT_BASE_DIR, list_cases, load_case

_GENERIC_REPLY = "Baik, dicatat."
_WAIT_TIMEOUT_S = 6.0

#: `expected` key aliases (Phase 8's own worked example used "decision"/
#: "required_terms"; `mark_test_case()`'s own `"actual"` block uses
#: "topic_decision"/"active_topic_terms" - both spellings are accepted
#: here so a case can be annotated in whichever the brief's own example
#: suggests without a schema migration).
_KEY_ALIASES = {
    "decision": "topic_decision",
    "required_terms": "active_topic_terms",
}


@dataclass
class ReplayResult:
    case_id: str
    result: str  # "PASS" / "FAIL" / "REVIEW"
    expected: Optional[Dict[str, Any]]
    actual: Dict[str, Any]
    primary_difference: str = ""
    secondary_difference: str = ""
    mismatches: List[str] = field(default_factory=list)


def _run_turn(console: Any, demo_event_cls: Any, text: str, request_id: str, conversation_id: str) -> None:
    """Publishes one `user_utterance` and blocks (bounded, `_WAIT_TIMEOUT_S`
    max) until this turn's `assistant_response` has landed - the same
    wait-for-completion pattern every Sprint 44-49 probe harness already
    used (`/tmp/sprint49/harness.py::run()`), inlined here so replay has
    no dependency on any scratch-space script."""
    done = threading.Event()

    def _on_done(e: Any) -> None:
        if e.get("request_id") == request_id:
            done.set()

    sub_id = console.event_bus.subscribe("assistant_response", _on_done)
    try:
        console.event_bus.publish(demo_event_cls(type="user_utterance", data={
            "text": text, "request_id": request_id, "conversation_id": conversation_id,
        }))
        deadline = time.time() + _WAIT_TIMEOUT_S
        while time.time() < deadline and not done.is_set():
            time.sleep(0.02)
    finally:
        console.event_bus.unsubscribe(sub_id)


def _build_actual(trace: Any) -> Dict[str, Any]:
    if trace is None:
        return {}
    return {
        "reference_type": getattr(trace, "reference_type", ""),
        "is_short_followup": getattr(trace, "is_short_followup", None),
        "topic_decision": getattr(trace, "topic_decision", ""),
        "ambiguity_check_result": getattr(trace, "ambiguity_check_result", None),
        "ambiguity_refusal": getattr(trace, "is_ambiguity_refusal", False),
        "active_topic_terms": list(getattr(trace, "active_topic_terms", [])),
        "candidate_count": len(getattr(trace, "candidate_memory_ids", set())),
        "selected_count": len(getattr(trace, "selected_memory_ids", set())),
    }


def _compare(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    """Returns a list of human-readable mismatch descriptions - empty
    means every expected field matched. `required_terms`/`active_topic_terms`
    is checked as a SUBSET (every expected term must appear in the
    actual set) rather than exact equality, since the actual term set
    naturally carries extra generic vocabulary the annotator didn't
    bother to list - everything else is exact equality."""
    mismatches: List[str] = []
    for raw_key, expected_value in expected.items():
        key = _KEY_ALIASES.get(raw_key, raw_key)
        actual_value = actual.get(key)
        if key == "active_topic_terms":
            expected_terms = set(expected_value or [])
            actual_terms = set(actual_value or [])
            missing = expected_terms - actual_terms
            if missing:
                mismatches.append(f"active_topic_terms missing {sorted(missing)} (actual={sorted(actual_terms)})")
            continue
        if actual_value != expected_value:
            mismatches.append(f"{key}: expected={expected_value!r} actual={actual_value!r}")
    return mismatches


def replay_case(case: Dict[str, Any], log_dir: Optional[str] = None) -> ReplayResult:
    """Replays one case dict (as returned by `test_capture.load_case()`)
    against a fresh, isolated `RuntimeDemoConsole`. `log_dir` (optional)
    turns on `EventLogWriter` for this replay run too, pointed at a
    caller-chosen directory - off by default, matching every other
    opt-in observability surface in this sprint."""
    from main_runtime_demo import Event, RuntimeDemoConsole
    from luno.adapters import MockOpenRouterClient

    case_id = case.get("id", "unknown")
    conversation: List[str] = case.get("conversation") or []
    expected = case.get("expected")

    client = MockOpenRouterClient(canned_text=_GENERIC_REPLY)
    console = RuntimeDemoConsole(
        openrouter_client=client,
        enable_observability_log=bool(log_dir),
        observability_log_dir=log_dir or "logs",
    )
    console.start()
    conv_id = f"replay-{case_id}"
    try:
        for idx, text in enumerate(conversation):
            request_id = f"{case_id}-{idx}"
            console.conversation_log.append(("USER", text))
            _run_turn(console, Event, text, request_id, conv_id)
        trace = console.planner_module._last_turn_trace.get(conv_id)
        actual = _build_actual(trace)

        if not expected:
            result = ReplayResult(case_id=case_id, result="REVIEW", expected=expected, actual=actual,
                                   primary_difference="(no annotated expected behavior yet)")
        else:
            mismatches = _compare(expected, actual)
            if not mismatches:
                result = ReplayResult(case_id=case_id, result="PASS", expected=expected, actual=actual)
            else:
                result = ReplayResult(
                    case_id=case_id, result="FAIL", expected=expected, actual=actual,
                    primary_difference=mismatches[0],
                    secondary_difference=mismatches[1] if len(mismatches) > 1 else "",
                    mismatches=mismatches,
                )

        # Published BEFORE `console.stop()` so the console's own (possibly
        # opted-in) EventLogWriter still sees it - the final step of the
        # sprint brief's own "REAL CONVERSATION -> LOGGED -> ... ->
        # REPLAYED -> BUG REPRODUCED" loop.
        try:
            console.event_bus.publish(Event(type="test_case_replayed", data={
                "case_id": case_id, "result": result.result,
                "primary_difference": result.primary_difference,
            }))
        except Exception:
            pass
        return result
    finally:
        console.stop()


def format_diff(r: ReplayResult) -> str:
    """Phase 10's own worked example format, rendered from whatever
    fields this case's `expected`/`actual` actually contain (never
    hardcoded to the brief's own 3-field example)."""
    lines = [f"CASE: {r.case_id}", ""]
    if r.expected:
        lines.append("EXPECTED:")
        for k, v in r.expected.items():
            lines.append(f"  {k} = {v}")
        lines.append("")
    lines.append("ACTUAL:")
    for k, v in r.actual.items():
        lines.append(f"  {k} = {v}")
    lines.append("")
    lines.append("RESULT:")
    lines.append(f"  {r.result}")
    if r.primary_difference:
        lines.append("")
        lines.append("PRIMARY DIFFERENCE:")
        lines.append(f"  {r.primary_difference}")
    if r.secondary_difference:
        lines.append("")
        lines.append("SECONDARY DIFFERENCE:")
        lines.append(f"  {r.secondary_difference}")
    return "\n".join(lines)


def replay_all(status: str = "approved", base_dir: str = DEFAULT_BASE_DIR, verbose: bool = False) -> List[ReplayResult]:
    """Phase 11's own data-quality gate, from the replay side: ONLY ever
    reads `status="approved"` by default - a `"candidate"`/`"reviewed"`/
    `"rejected"` case is never silently swept into a regression run.
    Returns an empty list (never an error, never a failure) when the
    approved directory has nothing in it yet - an empty real-world
    corpus is a valid, expected state, not a broken one."""
    results: List[ReplayResult] = []
    for case_id in list_cases(status=status, base_dir=base_dir):
        case = load_case(case_id, base_dir=base_dir)
        if case is None:
            continue
        r = replay_case(case)
        results.append(r)
        if verbose:
            print(format_diff(r))
            print()
    return results
