"""
Web search untuk info real-time (berita, cuaca, harga, event terkini, dll) — hal-hal
yang GPT nggak bakal tau dari training data-nya sendiri (entah karena post-cutoff atau
memang cepat berubah tiap saat).

Pakai Tavily (https://tavily.com) sebagai provider — API search yang memang didesain
buat dipakai AI agent/tool-calling, bukan scraping HTML Google manual yang gampang rapuh.
Perlu TAVILY_API_KEY di .env; kalau kosong, fitur ini otomatis nonaktif (lihat
is_configured() — dipanggil dari main.py sebelum nawarin tool ini ke GPT).
"""

import requests

from . import config

TAVILY_URL = "https://api.tavily.com/search"


def is_configured():
    return bool(config.TAVILY_API_KEY)


def search_web(query, max_results=5):
    """Cari di web lewat Tavily. Return dict {'answer': str|None, 'results': [...]}
    kalau sukses, atau {'error': str} kalau gagal/belum di-setup. Dipanggil dari
    main.py saat GPT manggil tool 'search_web'."""
    if not is_configured():
        return {"error": "Web search belum di-setup. Tambahkan TAVILY_API_KEY di .env (daftar gratis di tavily.com)."}

    query = (query or "").strip()
    if not query:
        return {"error": "Query kosong."}

    try:
        res = requests.post(
            TAVILY_URL,
            json={
                "api_key": config.TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "include_answer": True,
                "max_results": max_results,
            },
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()

        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content", "") or "")[:400],
            }
            for r in data.get("results", [])[:max_results]
        ]

        print(f"[WebSearch] ✓ '{query}' → {len(results)} result(s)")
        return {"answer": data.get("answer"), "results": results}

    except requests.exceptions.RequestException as ex:
        print(f"[WebSearch] ✗ Error: {ex}")
        return {"error": f"Web search gagal: {ex}"}
    except Exception as ex:
        print(f"[WebSearch] ✗ Unexpected error: {ex}")
        return {"error": f"Web search error: {ex}"}


def deep_search(queries, max_results_per_query=3):
    """Riset lebih dalam dari search_web — jalankan BEBERAPA query sekaligus (hasil
    breakdown GPT sendiri dari 1 topik kompleks), lalu gabung semua hasilnya jadi 1
    payload buat disintesis GPT. Dipanggil dari main.py saat GPT manggil tool
    'deep_search' (beda dari 'search_web' yang cuma 1x query, buat pertanyaan simpel)."""
    if not is_configured():
        return {"error": "Web search belum di-setup. Tambahkan TAVILY_API_KEY di .env (daftar gratis di tavily.com)."}

    queries = [q.strip() for q in (queries or []) if q and q.strip()][:5]  # cap 5 biar nggak boros kuota
    if not queries:
        return {"error": "Tidak ada query yang valid."}

    print(f"[WebSearch] ⏳ Deep search — {len(queries)} sub-query: {queries}")
    searches = [
        {"query": q, **search_web(q, max_results=max_results_per_query)}
        for q in queries
    ]
    return {"searches": searches}


# Tool schema buat OpenAI function calling — GPT sendiri yang mecah topik kompleks jadi
# beberapa sub-query (BEDA dari WEB_SEARCH_TOOL di atas yang cuma 1x query buat fakta simpel).
DEEP_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_search",
        "description": (
            "Perform deeper, multi-angle web research on a complex topic or question — break "
            "the topic into 2-5 focused sub-queries YOURSELF and pass them all in. Use this "
            "INSTEAD OF search_web when the user's question needs comparing multiple things, "
            "synthesizing info from different angles, or genuinely researching a topic — not "
            "just a single quick fact. For simple one-off lookups (weather, a single price, "
            "a quick current fact), use search_web instead — it's faster and cheaper."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-5 focused search queries covering different angles of the topic.",
                    "minItems": 2,
                    "maxItems": 5,
                }
            },
            "required": ["queries"],
        },
    },
}


# Tool schema buat OpenAI function calling — GPT sendiri yang mutusin kapan perlu
# manggil ini, bukan Luno yang nebak dari kata kunci di kalimat user.
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "Search the web for current, real-time, or post-training information — news, "
            "weather, prices, schedules, recent events, sports scores, or any fact you're "
            "not confident about or that could have changed recently. Use this instead of "
            "guessing whenever the user asks about something time-sensitive or current."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query — short and specific, like a search engine query.",
                }
            },
            "required": ["query"],
        },
    },
}