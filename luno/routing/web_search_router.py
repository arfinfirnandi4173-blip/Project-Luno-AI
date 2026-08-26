"""
web_search_router.py
======================

`WebSearchRouter` - wires `luno/web_search.py`'s existing Tavily
integration in as the Decision Engine's LAST-RESORT knowledge source,
exactly per spec: "Need Internet -> Tavily Search -> Search Results ->
DeepSeek or GPT summarizes. Never answer directly from Tavily." Tavily
is called HERE (by the Decision Engine, before any LLM request is
published) rather than exposed as an LLM-callable tool - the whole point
of this sprint is that the ROUTER decides when to search, not the model
guessing mid-conversation.

This closes a gap flagged in an earlier investigation this session:
`luno/web_search.py` was fully correct but never reachable from the
production `main.py`/`PlannerBridgeModule` runtime (only from
`legacy_main.py`/`luno/main.py`, neither of which
`bootstrap/modules.py` loads). Wiring it in here - as a knowledge-
retrieval step, never as a model-invoked tool - is the natural, in-
scope place to fix that, since this sprint explicitly asks for exactly
this integration.

Results are formatted into a plain system-prompt NOTE (same shape
`PlannerBridgeModule._handle_utterance()` already builds for
persona/memory/verified-action blocks - see `notes.append(...)` there),
NEVER turned into a spoken reply directly - the LLM still has to read
and synthesize it, so a bad/empty search result degrades to "I don't
have current information about that" instead of ever being presented as
a fact `PlannerBridgeModule` itself asserted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .models import REASONING_INTENTS, ComplexityLevel, Intent

_SPLIT_RE = re.compile(r"\bdan\b|\band\b|,")


class WebSearchRouter:
    def __init__(
        self,
        search_fn: Optional[Any] = None,
        deep_search_fn: Optional[Any] = None,
        is_configured_fn: Optional[Any] = None,
    ) -> None:
        if search_fn is None or deep_search_fn is None or is_configured_fn is None:
            from luno import web_search as _web_search
            search_fn = search_fn or _web_search.search_web
            deep_search_fn = deep_search_fn or _web_search.deep_search
            is_configured_fn = is_configured_fn or _web_search.is_configured
        self._search_fn = search_fn
        self._deep_search_fn = deep_search_fn
        self._is_configured_fn = is_configured_fn
        self.last_queries: List[str] = []

    def is_available(self) -> bool:
        try:
            return bool(self._is_configured_fn())
        except Exception:
            return False

    def build_queries(self, text: str, intents: List[Intent]) -> List[str]:
        """One query (the utterance itself, cleaned up) for the common
        case; splits into up to 3 sub-queries only when the utterance
        itself looks like it bundles several distinct asks AND the turn
        is already reasoning/planning-flavored (deep_search is for
        genuine multi-angle research, not every casual "and" in a
        sentence - see `luno/web_search.py::DEEP_SEARCH_TOOL`'s own
        description)."""
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        wants_deep = bool(set(intents) & REASONING_INTENTS)
        if wants_deep:
            parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p.strip() and len(p.strip()) > 3]
            if len(parts) >= 2:
                return parts[:5]
        return [cleaned]

    def search(self, text: str, intents: List[Intent], complexity: ComplexityLevel) -> Optional[Dict[str, Any]]:
        if not self.is_available():
            self.last_queries = []
            return None
        queries = self.build_queries(text, intents)
        self.last_queries = queries
        if not queries:
            return None
        try:
            if len(queries) > 1:
                return {"mode": "deep_search", **self._deep_search_fn(queries)}
            return {"mode": "search_web", **self._search_fn(queries[0])}
        except Exception as ex:
            return {"error": str(ex)}

    @staticmethod
    def format_context(result: Optional[Dict[str, Any]]) -> Optional[str]:
        """Renders `search()`'s raw Tavily payload into a system-prompt
        note. Deliberately verbose/labelled ("via Tavily web search -
        verify, don't over-claim") so the LLM never mistakes this for a
        directly-verified fact the way a `ToolResult`-derived note is -
        internet search results are informational context only."""
        if not result:
            return None
        if result.get("error"):
            return (
                "Web Search (Tavily) was attempted for this turn but failed: "
                f"{result['error']}. Tell the user honestly you couldn't search "
                "right now, don't guess at current information."
            )

        lines = ["Web Search Results (via Tavily - use as context, cite naturally, don't over-claim certainty):"]
        if result.get("mode") == "deep_search":
            for entry in result.get("searches") or []:
                query = entry.get("query", "")
                if entry.get("error"):
                    lines.append(f"- [{query}] search failed: {entry['error']}")
                    continue
                if entry.get("answer"):
                    lines.append(f"- [{query}] {entry['answer']}")
                for r in (entry.get("results") or [])[:3]:
                    lines.append(f"  - {r.get('title', '')}: {r.get('snippet', '')} ({r.get('url', '')})")
        else:
            if result.get("answer"):
                lines.append(f"- {result['answer']}")
            for r in (result.get("results") or [])[:5]:
                lines.append(f"  - {r.get('title', '')}: {r.get('snippet', '')} ({r.get('url', '')})")

        if len(lines) == 1:
            return "Web Search (Tavily) returned no usable results for this query. Tell the user honestly if you don't know."
        return "\n".join(lines)
