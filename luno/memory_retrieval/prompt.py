"""
prompt.py
=========

The "Prompt Builder" stage from the spec's desired flow - turns a
`RelevantMemory` list into the exact block the spec's own "Prompt
Construction" example shows:

    Relevant Memory:
    - Cup last seen on the desk. Observed 3 minutes ago.

    User:
    Where is my cup?

This module renders ONLY the "Relevant Memory:" block itself (returning
"" when there is nothing to inject, so a caller can safely always
prepend/insert it without a conditional) - assembling that alongside the
rest of the system prompt and the user's own message is the caller's job
(`main_runtime_demo.py`), matching "Do NOT redesign OpenRouter Adapter" /
"Context Builder should never know how retrieval works internally": this
package hands back plain text, nothing more.
"""

from __future__ import annotations

from typing import List

from .models import RelevantMemory


def build_memory_prompt_block(memories: List[RelevantMemory]) -> str:
    if not memories:
        return ""
    lines = ["Relevant Memory:"]
    for mem in memories:
        lines.append(f"- {mem.text}")
    return "\n".join(lines)
