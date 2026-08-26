"""
computer_use.py (luno.browser)
=================================

`ComputerUseAgent.run()` - the bounded observe -> reason -> act -> verify
loop (spec section 8):

    screenshot -> vision provider -> decide ONE next action -> permission
    check -> execute via BrowserProvider -> screenshot again (next loop
    iteration's "observe" IS the previous action's verification) -> repeat

Bounded by `max_steps` (`BROWSER_MAX_STEPS`) - hits the limit ->
"I couldn't safely complete the operation within the allowed steps.",
never loops forever (spec section 8's own requirement).

"Reasoning" here is NOT a second, separate LLM call into this project's
chat/routing pipeline - `main_runtime_demo.py` explicitly has no live
function-calling loop (`NeedLLMResponse` carries no `tools` list; see
that module's own comment). Instead, each step reuses the EXISTING
on-demand vision provider (`luno.vision._get_vision_provider()` -
Gemini/OpenAI, whichever `VISION_PROVIDER` selects) with a prompt asking
it to both DESCRIBE what it sees and DECIDE the next action, in one
fixed, parseable text format (see `_ACTION_LINE_RE`). This is exactly
the "screenshot -> vision provider -> visual understanding -> reasoning
-> action" pipeline spec section 7 draws, using the SAME provider vision
questions already use - no new/parallel/permanent local VLM (spec
section 7's explicit prohibition).

Every proposed action is run through `PermissionManager.evaluate()`
before execution - a `"confirm"`/`"deny"` verdict HALTS the loop
immediately (never silently skipped and retried with a different
action) and is surfaced as the loop's final note, matching spec's own
"Luno: halaman sudah siap. Saya perlu menekan Submit. Lanjutkan?"
example.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_ACTION_LINE_RE = re.compile(
    r"(?im)^\s*(DONE|STUCK|CLICK|TYPE|SCROLL|NAVIGATE)\s*:\s*(.+?)\s*$"
)
_TYPE_INTO_RE = re.compile(r"(?i)^(.*?)\s+INTO\s*:\s*(.+)$")

_DECISION_PROMPT_TEMPLATE = (
    "You are directing a browser/computer-use agent. Task: \"{task}\"\n"
    "Look at this screenshot and respond with:\n"
    "1. One short sentence describing what's currently visible/happening.\n"
    "2. Exactly ONE action line, in ONE of these exact formats (nothing else on that line):\n"
    "   DONE: <short summary of what was accomplished, or why it's already satisfied>\n"
    "   STUCK: <short reason you cannot safely proceed>\n"
    "   CLICK: <short visible text/label of the element to click>\n"
    "   TYPE: <text to type> INTO: <short visible label of the field>\n"
    "   SCROLL: <up|down|left|right>\n"
    "   NAVIGATE: <a full URL>\n"
    "Prefer DONE as soon as the task is visibly satisfied. Prefer STUCK rather than guessing "
    "if nothing safe/obvious to do is visible."
)


@dataclass
class StepRecord:
    step_number: int
    observation: str
    action_type: str
    action_detail: str
    executed: bool
    result: str = ""


@dataclass
class ComputerUseResult:
    task: str
    completed: bool
    steps: List[StepRecord] = field(default_factory=list)
    final_note: str = ""


#: Sentinel distinguishing "caller didn't pass `vision_analyze_fn` at
#: all" (auto-resolve the real, existing on-demand vision provider) from
#: "caller explicitly passed `None`" (vision is deliberately unavailable -
#: used by tests, and by any caller that already knows vision isn't
#: configured). Using plain `None` as the default would conflate the two
#: and silently auto-resolve a REAL provider even when a caller
#: explicitly opted out.
_UNSET = object()


class ComputerUseAgent:
    def __init__(
        self, browser_provider: Any, permission_manager: Any,
        vision_analyze_fn: Any = _UNSET, max_steps: int = 10,
    ) -> None:
        self._provider = browser_provider
        self._permissions = permission_manager
        self._max_steps = max_steps
        if vision_analyze_fn is _UNSET:
            try:
                import luno.vision as vision_module
                vision_analyze_fn = vision_module._get_vision_provider().analyze_image
            except Exception:
                vision_analyze_fn = None
        self._vision_analyze_fn = vision_analyze_fn

    def run(self, task: str, conversation_key: str = "_default_") -> ComputerUseResult:
        steps: List[StepRecord] = []
        if self._vision_analyze_fn is None:
            return ComputerUseResult(
                task=task, completed=False, steps=steps,
                final_note="Vision isn't configured, so I can't safely see what's on screen to do this.",
            )

        for step_number in range(1, self._max_steps + 1):
            try:
                screenshot = self._provider.screenshot()
            except Exception as ex:
                return ComputerUseResult(task=task, completed=False, steps=steps, final_note=f"Couldn't capture the screen: {ex}")

            try:
                raw = self._vision_analyze_fn(screenshot, _DECISION_PROMPT_TEMPLATE.format(task=task))
            except Exception as ex:
                return ComputerUseResult(task=task, completed=False, steps=steps, final_note=f"Vision analysis failed: {ex}")

            observation, action_type, action_detail = _parse_decision(raw)

            if action_type == "DONE":
                steps.append(StepRecord(step_number, observation, action_type, action_detail, executed=False, result="task considered complete"))
                return ComputerUseResult(task=task, completed=True, steps=steps, final_note=action_detail or "Done.")
            if action_type == "STUCK" or action_type is None:
                reason = action_detail or "couldn't determine a safe next action from the screen"
                steps.append(StepRecord(step_number, observation, action_type or "STUCK", action_detail, executed=False, result=reason))
                return ComputerUseResult(task=task, completed=False, steps=steps, final_note=f"Stopped: {reason}")

            decision, prompt_or_reason = self._permissions.evaluate(conversation_key, _ACTION_TO_TOOL_ACTION.get(action_type, action_type.lower()), {}, action_detail)
            if decision == "deny":
                steps.append(StepRecord(step_number, observation, action_type, action_detail, executed=False, result=f"denied: {prompt_or_reason}"))
                return ComputerUseResult(task=task, completed=False, steps=steps, final_note=prompt_or_reason or "That action isn't allowed autonomously.")
            if decision == "confirm":
                steps.append(StepRecord(step_number, observation, action_type, action_detail, executed=False, result=f"needs confirmation: {prompt_or_reason}"))
                return ComputerUseResult(task=task, completed=False, steps=steps, final_note=prompt_or_reason or "I need your confirmation before continuing.")

            result_text = self._execute(action_type, action_detail)
            steps.append(StepRecord(step_number, observation, action_type, action_detail, executed=True, result=result_text))

        return ComputerUseResult(
            task=task, completed=False, steps=steps,
            final_note="I couldn't safely complete the operation within the allowed steps.",
        )

    def _execute(self, action_type: str, action_detail: str) -> str:
        from .provider import Target
        try:
            if action_type == "CLICK":
                self._provider.click(Target(selector=f"text={action_detail}"))
                return f"clicked '{action_detail}'"
            if action_type == "TYPE":
                match = _TYPE_INTO_RE.match(action_detail)
                if not match:
                    return f"couldn't parse TYPE action: {action_detail!r}"
                text, field_label = match.group(1).strip(), match.group(2).strip()
                self._provider.type_text(Target(selector=f"text={field_label}"), text)
                return f"typed into '{field_label}'"
            if action_type == "SCROLL":
                self._provider.scroll(action_detail.strip().lower())
                return f"scrolled {action_detail}"
            if action_type == "NAVIGATE":
                self._provider.open_url(action_detail.strip())
                return f"navigated to {action_detail}"
        except Exception as ex:
            return f"action failed: {ex}"
        return "unhandled action"


_ACTION_TO_TOOL_ACTION = {"CLICK": "click", "TYPE": "type", "SCROLL": "scroll", "NAVIGATE": "navigate"}


def _parse_decision(raw: str) -> "tuple[str, Optional[str], str]":
    text = raw or ""
    match = _ACTION_LINE_RE.search(text)
    if not match:
        return text.strip()[:300], None, ""
    action_type = match.group(1).upper()
    action_detail = match.group(2).strip()
    observation = text[: match.start()].strip()[:300] or text.strip()[:300]
    return observation, action_type, action_detail
