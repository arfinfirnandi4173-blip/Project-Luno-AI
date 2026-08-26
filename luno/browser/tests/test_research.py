"""
test_research.py
==================

`luno.browser.research.ResearchAgent` - search (fake `search_fn`, no
real Tavily call) -> optionally open pages (fake `browser_provider`, no
real Playwright) -> format a system-prompt note. Every test injects
fakes, matching this project's own "mock/real behind an interface"
testing convention.
"""

from __future__ import annotations

from luno.browser.research import DEFAULT_MAX_PAGES_TO_OPEN, ResearchAgent


class _FakeBrowserProvider:
    def __init__(self, page_text_by_url=None, fail_urls=None):
        self._page_text_by_url = page_text_by_url or {}
        self._fail_urls = fail_urls or set()
        self.opened_urls = []
        self._current_url = None

    def open_url(self, url):
        self.opened_urls.append(url)
        if url in self._fail_urls:
            raise RuntimeError("simulated navigation failure")
        self._current_url = url

    def get_page_text(self):
        return self._page_text_by_url.get(self._current_url, "")


def _search_fn_factory(payload):
    def _search(query):
        return payload
    return _search


def test_empty_query_returns_error():
    agent = ResearchAgent(search_fn=_search_fn_factory({}), is_search_configured_fn=lambda: True)
    result = agent.research("")
    assert result.error is not None


def test_search_not_configured():
    agent = ResearchAgent(search_fn=_search_fn_factory({}), is_search_configured_fn=lambda: False)
    result = agent.research("RTX 5060 Ti price")
    assert result.error is not None
    assert "configured" in result.error


def test_search_error_passthrough():
    agent = ResearchAgent(search_fn=_search_fn_factory({"error": "Tavily is down"}), is_search_configured_fn=lambda: True)
    result = agent.research("something")
    assert result.error == "Tavily is down"


def test_search_only_no_browser_provider():
    payload = {
        "answer": "It costs around $450.",
        "results": [
            {"title": "Store A", "url": "https://store-a.example/rtx", "snippet": "RTX 5060 Ti listing"},
        ],
    }
    agent = ResearchAgent(browser_provider=None, search_fn=_search_fn_factory(payload), is_search_configured_fn=lambda: True)
    result = agent.research("RTX 5060 Ti price")
    assert result.error is None
    assert result.answer == "It costs around $450."
    assert len(result.sources) == 1
    assert result.sources[0].opened is False
    assert result.sources[0].snippet == "RTX 5060 Ti listing"


def test_opens_pages_when_browser_provider_given():
    payload = {
        "answer": None,
        "results": [{"title": "Docs", "url": "https://docs.example.com/api", "snippet": "API docs"}],
    }
    provider = _FakeBrowserProvider(page_text_by_url={"https://docs.example.com/api": "Full API documentation text here."})
    agent = ResearchAgent(browser_provider=provider, search_fn=_search_fn_factory(payload), is_search_configured_fn=lambda: True)
    result = agent.research("API docs")
    assert result.sources[0].opened is True
    assert "Full API documentation" in result.sources[0].page_text
    assert provider.opened_urls == ["https://docs.example.com/api"]


def test_page_open_failure_is_recorded_honestly_not_silently_dropped():
    payload = {"results": [{"title": "Broken", "url": "https://broken.example/page", "snippet": "snippet only"}]}
    provider = _FakeBrowserProvider(fail_urls={"https://broken.example/page"})
    agent = ResearchAgent(browser_provider=provider, search_fn=_search_fn_factory(payload), is_search_configured_fn=lambda: True)
    result = agent.research("broken page test")
    assert result.sources[0].opened is False
    assert result.sources[0].open_error is not None


def test_authoritative_sources_ranked_first():
    payload = {
        "results": [
            {"title": "Random blog", "url": "https://randomblog.example/post", "snippet": "a blog post"},
            {"title": "Official docs", "url": "https://docs.python.org/3/", "snippet": "official docs"},
        ],
    }
    agent = ResearchAgent(search_fn=_search_fn_factory(payload), is_search_configured_fn=lambda: True)
    result = agent.research("python docs")
    assert result.sources[0].url == "https://docs.python.org/3/"


def test_max_pages_to_open_is_respected():
    results = [{"title": f"R{i}", "url": f"https://example.com/{i}", "snippet": "s"} for i in range(5)]
    provider = _FakeBrowserProvider(page_text_by_url={r["url"]: "text" for r in results})
    agent = ResearchAgent(browser_provider=provider, search_fn=_search_fn_factory({"results": results}), is_search_configured_fn=lambda: True)
    agent.research("many results", max_pages_to_open=2)
    assert len(provider.opened_urls) == 2


def test_default_max_pages_constant_is_reasonable():
    assert 1 <= DEFAULT_MAX_PAGES_TO_OPEN <= 5


# -- format_note ------------------------------------------------------------------

def test_format_note_error():
    from luno.browser.research import ResearchResult
    note = ResearchAgent.format_note(ResearchResult(query="x", error="search failed: timeout"))
    assert "failed" in note
    assert "x" in note


def test_format_note_labels_opened_vs_snippet_only():
    from luno.browser.research import ResearchResult, SourceResult
    result = ResearchResult(
        query="test",
        sources=[
            SourceResult(title="A", url="https://a.example", opened=True, page_text="full text"),
            SourceResult(title="B", url="https://b.example", opened=False, snippet="just a snippet"),
        ],
    )
    note = ResearchAgent.format_note(result)
    assert "[OPENED]" in note
    assert "[SNIPPET ONLY]" in note
    assert "full text" in note
    assert "just a snippet" in note


def test_format_note_no_results():
    from luno.browser.research import ResearchResult
    note = ResearchAgent.format_note(ResearchResult(query="nothing found"))
    assert "no usable results" in note
