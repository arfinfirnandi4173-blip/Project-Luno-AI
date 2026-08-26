"""
test_web_search_router.py
============================

`WebSearchRouter` - Tavily wired in as a knowledge-retrieval step only.
Uses injected fake `search_fn`/`deep_search_fn`/`is_configured_fn` -
never touches the real `luno.web_search` module (no network, no
TAVILY_API_KEY needed).
"""

from __future__ import annotations

from luno.routing.models import ComplexityLevel, Intent
from luno.routing.web_search_router import WebSearchRouter


def _router(configured=True, search_result=None, deep_result=None):
    calls = {"search": [], "deep_search": []}

    def _search(query, max_results=5):
        calls["search"].append(query)
        return search_result if search_result is not None else {"answer": f"answer for {query}", "results": []}

    def _deep_search(queries, max_results_per_query=3):
        calls["deep_search"].append(list(queries))
        return deep_result if deep_result is not None else {"searches": [{"query": q, "answer": f"a-{q}", "results": []} for q in queries]}

    router = WebSearchRouter(search_fn=_search, deep_search_fn=_deep_search, is_configured_fn=lambda: configured)
    return router, calls


def test_not_available_when_not_configured():
    router, _ = _router(configured=False)
    assert router.is_available() is False
    assert router.search("weather today", [Intent.SEARCH_WEB], ComplexityLevel.LOW) is None


def test_simple_query_uses_search_web():
    router, calls = _router()
    result = router.search("what's the weather today", [Intent.SEARCH_WEB], ComplexityLevel.LOW)
    assert result["mode"] == "search_web"
    assert calls["search"] == ["what's the weather today"]
    assert calls["deep_search"] == []


def test_reasoning_intent_with_multiple_clauses_uses_deep_search():
    router, calls = _router()
    text = "compare python and javascript, and explain which is faster for web servers"
    result = router.search(text, [Intent.REASONING], ComplexityLevel.HIGH)
    assert result["mode"] == "deep_search"
    assert len(calls["deep_search"][0]) >= 2


def test_single_clause_reasoning_still_uses_search_web():
    router, calls = _router()
    result = router.search("why is the sky blue", [Intent.REASONING], ComplexityLevel.MEDIUM)
    assert result["mode"] == "search_web"


def test_empty_text_returns_none():
    router, calls = _router()
    result = router.search("   ", [Intent.SEARCH_WEB], ComplexityLevel.LOW)
    assert result is None


def test_search_error_is_captured_not_raised():
    def _boom(query, max_results=5):
        raise RuntimeError("network down")

    router = WebSearchRouter(search_fn=_boom, deep_search_fn=lambda *a, **k: {}, is_configured_fn=lambda: True)
    result = router.search("something", [Intent.SEARCH_WEB], ComplexityLevel.LOW)
    assert result["error"] == "network down"


def test_format_context_none_result():
    assert WebSearchRouter.format_context(None) is None


def test_format_context_error_result():
    ctx = WebSearchRouter.format_context({"error": "boom"})
    assert "failed" in ctx.lower()
    assert "boom" in ctx


def test_format_context_search_web_result():
    ctx = WebSearchRouter.format_context({
        "mode": "search_web", "answer": "It's sunny",
        "results": [{"title": "Weather Today", "snippet": "Sunny, 25C", "url": "http://x"}],
    })
    assert "It's sunny" in ctx
    assert "Weather Today" in ctx
    assert "Tavily" in ctx


def test_format_context_deep_search_result():
    ctx = WebSearchRouter.format_context({
        "mode": "deep_search",
        "searches": [
            {"query": "python speed", "answer": "fast", "results": []},
            {"query": "js speed", "answer": "also fast", "results": []},
        ],
    })
    assert "python speed" in ctx
    assert "js speed" in ctx


def test_format_context_no_usable_results():
    ctx = WebSearchRouter.format_context({"mode": "search_web", "answer": None, "results": []})
    assert "no usable results" in ctx.lower()


def test_never_answers_directly_context_always_labelled_informational():
    """Spec: 'Never answer directly from Tavily' - format_context must
    always frame results as context to be synthesized, not a ready
    answer."""
    ctx = WebSearchRouter.format_context({"mode": "search_web", "answer": "42", "results": []})
    assert "context" in ctx.lower() or "cite" in ctx.lower()
