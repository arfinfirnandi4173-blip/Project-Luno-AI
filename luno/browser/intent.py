"""
intent.py (luno.browser)
===========================

Deterministic (non-LLM) classifiers deciding when a plain utterance
means "do browser research" / "check my server" / "computer-use this
app" - same architecture and same conservatism as `luno/vision_intent.py`
(regex/keyword co-occurrence, never a single common word alone) and
`luno/environment_intent.py`. Spec section 19's own worry - "do not
classify every mention of 'web'/'browser'/'lihat' as a browser command"
- is handled the same way `vision_intent.py` avoids "lihat status lampu"
false-triggering: every rule below requires at least two independent
signals to co-occur, and deliberately never fires on a bare mention of
"browser"/"internet"/"website" alone.

Three independent classifiers, deliberately NOT merged into one
function - a false positive in one must never block the other two from
being checked (see `main_runtime_demo.py`'s wiring: monitoring is
checked first since "cek server" could otherwise be swallowed by a
looser research rule), and each is independently unit-testable.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


def _contains_word(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _contains_any(text: str, phrases) -> bool:
    return any(_contains_word(text, p) for p in phrases)


# -- research intent ("cari harga RTX 5060 Ti", "search for X") --------------

_RESEARCH_VERBS: Tuple[str, ...] = (
    "cari", "carikan", "cariin", "googling", "search", "browsing",
    "bandingkan", "compare",
    # "cek"/"check" ("cek harga RTX3060") - a real, reported gap: this
    # is just as natural a way to ask for a web lookup as "cari", but
    # was missing entirely, so "cek harga X" fell through to plain
    # conversation (the LLM guessing from stale training data) instead
    # of triggering an actual search. Kept WEAK (needs an
    # `_INFO_MARKERS` co-occurrence, same as "cari") rather than
    # strong, specifically so it never collides with
    # `classify_monitoring_intent`'s own "cek server"/"cek dashboard"
    # meaning - the two verb sets overlap on purpose, but
    # `_INFO_MARKERS` and `_MONITOR_NOUNS` below are disjoint, so "cek
    # server" still only ever matches monitoring, never research.
    "cek", "check", "checking",
)
#: These verbs alone are common in unrelated sentences ("cari tahu" as a
#: figure of speech, "compare" in a totally different context) - require
#: co-occurrence with an INFO-SEEKING marker word too, same "two signals"
#: conservatism as the rest of this module. "browser" itself counts as a
#: marker too - "cek harga RTX3060 di browser" is about as explicit an
#: instruction as a user can give.
_INFO_MARKERS: Tuple[str, ...] = (
    "harga", "price", "dokumentasi", "documentation", "docs",
    "penyebab", "cause", "error", "info", "informasi", "review",
    "spesifikasi", "spec", "produk", "product", "berita", "news",
    "artikel", "article", "situs", "website", "di internet", "online",
    "browser",
)


def classify_research_intent(text: str) -> Optional[str]:
    """Returns the research query (verbatim `text`, stripped) or `None`.
    Requires a research VERB - unambiguous enough alone in most cases,
    but still gated further by requiring an info-seeking marker too
    (spec section 19's own "don't classify every 'search' mention"
    caution) UNLESS the verb itself is one of the strong/explicit ones
    ("carikan"/"googling"/"browsing" are essentially never used for
    anything else in casual Indonesian/English)."""
    if not text:
        return None
    lower = text.lower().strip()
    if not lower:
        return None
    strong_verbs = ("carikan", "cariin", "googling", "browsing")
    if _contains_any(lower, strong_verbs):
        return text.strip()
    if _contains_any(lower, _RESEARCH_VERBS) and _contains_any(lower, _INFO_MARKERS):
        return text.strip()
    return None


# -- image search intent ("cari gambar kucing lucu") -------------------------
# Deliberately its own classifier, separate from `classify_research_
# intent` above - a plain research request gets read/synthesized by the
# LLM from text `research.py` collects; an IMAGE search is instead about
# Vinn wanting to actually LOOK at something himself, so it opens a
# real, VISIBLE browser window (see `main_runtime_demo.py::
# _handle_image_search_intent`, `provider.py::get_visible_browser_
# provider`) rather than being read and summarized.

_IMAGE_SEARCH_VERBS: Tuple[str, ...] = ("cari", "carikan", "cariin", "search", "tunjukkan", "tunjukin", "lihatkan")
_IMAGE_WORDS: Tuple[str, ...] = ("gambar", "foto", "image", "images", "photo", "photos", "picture", "pictures")


def classify_image_search_intent(text: str) -> Optional[str]:
    """Requires an image WORD co-occurring with a search/show verb -
    "cari gambar kucing lucu" matches; "gambarnya bagus banget" (no
    search verb) or "cari tahu soal kucing" (no image word) correctly
    do not. Returns a cleaned-up query (the image word and everything
    before the search verb stripped out) - "cari gambar kucing lucu" ->
    "kucing lucu"."""
    if not text:
        return None
    lower = text.lower().strip()
    if not lower:
        return None
    if not (_contains_any(lower, _IMAGE_SEARCH_VERBS) and _contains_any(lower, _IMAGE_WORDS)):
        return None
    match = re.search(
        r"(?:" + "|".join(re.escape(v) for v in _IMAGE_SEARCH_VERBS) + r")\b\s*"
        r"(?:" + "|".join(re.escape(w) for w in _IMAGE_WORDS) + r")\b\s*(?:dari|of|nya)?\s*(.*)",
        lower,
    )
    query = match.group(1).strip(" .,!?") if match else ""
    return query or text.strip()


# -- monitoring intent ("cek server", "lihat Portainer") ---------------------

_MONITOR_VERBS: Tuple[str, ...] = ("cek", "check", "lihat", "liat", "periksa", "inspect")
_MONITOR_NOUNS: Tuple[str, ...] = (
    "server", "dashboard", "portainer", "grafana", "docker", "container",
    "kontainer", "monitoring", "cpu", "ram", "disk", "gpu",
)


def classify_monitoring_intent(text: str) -> bool:
    """Requires a monitor VERB + a monitor NOUN co-occurring - "cek
    lampu" (a Home Assistant device check) has the verb but not a
    monitoring noun, so it correctly does NOT match here (stays with
    the existing home_assistant/device-intent path)."""
    if not text:
        return False
    lower = text.lower().strip()
    return _contains_any(lower, _MONITOR_VERBS) and _contains_any(lower, _MONITOR_NOUNS)


# -- computer-use intent ("buka Unity dan lihat kenapa avatar saya error") ---

_OPEN_VERBS: Tuple[str, ...] = ("buka", "open", "jalankan")
_DIAGNOSTIC_WORDS: Tuple[str, ...] = (
    "kenapa", "why", "error", "masalah", "problem", "salah", "rusak",
    "gagal", "fail", "failed", "bug", "issue",
)


def classify_computer_use_intent(text: str) -> Optional[str]:
    """Requires BOTH an open/launch verb AND a diagnostic word in the
    same utterance - "buka spotify" (no diagnostic word) correctly stays
    a plain app-open command; "kenapa kamera nggak nyala" (diagnostic
    word, no open verb) correctly stays plain conversation. Returns the
    verbatim utterance as the task description for `ComputerUseAgent`."""
    if not text:
        return None
    lower = text.lower().strip()
    if _contains_any(lower, _OPEN_VERBS) and _contains_any(lower, _DIAGNOSTIC_WORDS):
        return text.strip()
    return None
