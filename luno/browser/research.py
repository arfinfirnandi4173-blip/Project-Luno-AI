"""
research.py (luno.browser)
=============================

`ResearchAgent.research()` - the workflow spec section 3 describes:
search -> open relevant pages -> read content -> prefer authoritative
sources -> hand collected context to the LLM -> (the LLM itself
produces the final answer; this module never does) -> include source
references.

Search itself REUSES this project's existing Tavily integration
(`luno/web_search.py`, already wired as `WebSearchRouter`'s knowledge
source for the automatic/complexity-based routing path - see that
module's own docstring) rather than reinventing a second search
backend. What THIS module adds on top is the part Tavily's snippet-only
results can't do: actually opening the most relevant/authoritative
pages via `BrowserProvider` and reading their real page text, for
requests that need more than a snippet (official documentation, a
specific comparison, a GitHub issue thread, ...).

Deliberately does NOT call an LLM itself - `research()` returns a
`ResearchResult` (raw collected text + sources), and the CALLER
(`main_runtime_demo.py`, mirroring `_handle_vision_intent`'s "pre-fetch
and inject" pattern) is what turns that into a system-prompt note handed
to the existing daily/reasoning LLM. This keeps model routing entirely
where it already lives (`DecisionEngine`) - this module never decides
which model answers, only what CONTEXT that model gets to answer with
(spec section 16's own requirement).

HONEST LIMITATION, matching this project's own "never claim something
was verified if it wasn't actually retrieved" rule (spec section 3):
if `BrowserProvider` is unavailable/fails to open a page, that source is
recorded as "couldn't be opened" rather than silently dropped or (worse)
answered from the Tavily snippet alone while implying a full read
happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

#: Domains preferred when picking which search results to actually open
#: (spec section 3: "prefer authoritative sources... official
#: documentation, official GitHub repositories, vendor documentation,
#: standards/specifications"). Substring-matched against the result
#: URL's host - deliberately a broad net (any "*.readthedocs.io", any
#: "github.com", ...) rather than an exhaustive vendor list.
_AUTHORITATIVE_HINTS: Tuple[str, ...] = (
    "docs.", "documentation", "github.com", "readthedocs.io", "developer.",
    "learn.microsoft.com", "cloud.google.com", "aws.amazon.com",
    ".gov", ".edu", "w3.org", "ietf.org", "wikipedia.org",
)

#: How many pages research() will actually try to OPEN and read (beyond
#: this, only the Tavily snippet is used) - bounded so one research
#: request can't spiral into an unbounded number of navigations.
DEFAULT_MAX_PAGES_TO_OPEN = 3
#: Page text is truncated to this many characters before being folded
#: into the note - a full page can be tens of thousands of characters,
#: far more than any prompt should carry for one source.
_MAX_PAGE_TEXT_CHARS = 2000


@dataclass
class SourceResult:
    title: str
    url: str
    snippet: str = ""
    opened: bool = False
    page_text: str = ""
    open_error: Optional[str] = None


@dataclass
class ResearchResult:
    query: str
    answer: Optional[str] = None
    sources: List[SourceResult] = field(default_factory=list)
    error: Optional[str] = None


class ResearchAgent:
    def __init__(self, browser_provider: Optional[Any] = None, search_fn: Optional[Any] = None, is_search_configured_fn: Optional[Any] = None) -> None:
        if search_fn is None or is_search_configured_fn is None:
            from luno import web_search as _web_search
            search_fn = search_fn or _web_search.search_web
            is_search_configured_fn = is_search_configured_fn or _web_search.is_configured
        self._search_fn = search_fn
        self._is_search_configured_fn = is_search_configured_fn
        self._browser_provider = browser_provider  # may be None - falls back to snippet-only research

    def _rank_by_authority(self, results: List[dict]) -> List[dict]:
        def score(r: dict) -> int:
            url = (r.get("url") or "").lower()
            return 0 if any(hint in url for hint in _AUTHORITATIVE_HINTS) else 1
        return sorted(results, key=score)

    def research(self, query: str, max_pages_to_open: int = DEFAULT_MAX_PAGES_TO_OPEN) -> ResearchResult:
        query = (query or "").strip()
        if not query:
            return ResearchResult(query=query, error="empty query")
        try:
            if not self._is_search_configured_fn():
                return ResearchResult(query=query, error="web search isn't configured (no TAVILY_API_KEY)")
            raw = self._search_fn(query)
        except Exception as ex:
            return ResearchResult(query=query, error=f"search failed: {ex}")

        if raw.get("error"):
            return ResearchResult(query=query, error=raw["error"])

        results = raw.get("results") or []
        ranked = self._rank_by_authority(results)
        sources = [SourceResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("snippet", "")) for r in ranked]

        if self._browser_provider is not None:
            for source in sources[:max_pages_to_open]:
                if not source.url:
                    continue
                try:
                    self._browser_provider.open_url(source.url)
                    text = self._browser_provider.get_page_text()
                    source.page_text = (text or "").strip()[:_MAX_PAGE_TEXT_CHARS]
                    source.opened = True
                except Exception as ex:
                    source.open_error = str(ex)

        return ResearchResult(query=query, answer=raw.get("answer"), sources=sources)

    @staticmethod
    def format_note(result: ResearchResult) -> str:
        """Renders a `ResearchResult` into a system-prompt note - same
        "labelled context block, LLM still has to synthesize/cite it,
        never a directly-asserted fact" shape
        `WebSearchRouter.format_context()` already uses for the
        automatic Tavily path, so both research paths read consistently
        to the model."""
        if result.error:
            return (
                f"Browser Research for \"{result.query}\" failed: {result.error}. "
                "Tell the user honestly you couldn't research this right now, don't guess."
            )
        lines = [f"Browser Research for \"{result.query}\" (cite sources naturally, don't over-claim certainty):"]
        if result.answer:
            lines.append(f"- Summary: {result.answer}")
        for source in result.sources:
            if source.opened and source.page_text:
                lines.append(f"- [OPENED] {source.title} ({source.url}): {source.page_text[:500]}")
            elif source.open_error:
                lines.append(f"- [COULDN'T OPEN] {source.title} ({source.url}) - {source.open_error}; snippet only: {source.snippet}")
            else:
                lines.append(f"- [SNIPPET ONLY] {source.title} ({source.url}): {source.snippet}")
        if len(lines) == 1:
            return f"Browser Research for \"{result.query}\" returned no usable results. Tell the user honestly if you don't know."
        return "\n".join(lines)
