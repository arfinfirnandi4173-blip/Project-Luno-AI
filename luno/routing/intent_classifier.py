"""
intent_classifier.py
=====================

`classify_intent(text) -> List[Intent]` - rule-based keyword/regex
classification, bilingual (Indonesian + English, matching this whole
project's own convention - see `luno/planner/parser.py`,
`luno/memory.py`'s command detectors, `luno/memory_retrieval/query.py`).
Deliberately NOT an LLM call: this runs BEFORE any LLM routing decision
is even made, so it has to be free, instant, and fully deterministic -
exactly the same reasoning `PlannerBridgeModule._classify_device_intent()`
already documents for why ITS fallback path is opt-in/last-resort rather
than the default.

Returns every matched `Intent`, ranked highest-signal-first (ties broken
by `_PRIORITY`, a fixed "if this utterance could plausibly be several
things, which one actually drives routing" order - specific/actionable
intents outrank vague conversational ones). `intents[0]` is always what
`RoutingDecision.primary_intent` gets set to. Never raises, never
returns an empty list - falls back to `GENERAL_QUESTION`/`GENERAL_CHAT`.
"""

from __future__ import annotations

import re
from typing import Dict, List, Pattern

from .models import Intent

# ---------------------------------------------------------------------------
# pattern tables (all matched against the LOWERCASED text)
# ---------------------------------------------------------------------------


def _pats(*phrases: str) -> List[Pattern]:
    """Single alphanumeric-token phrases (`"ac"`, `"lampu"`) get real
    `\\b` word-boundary wrapping so they can't false-positive-match
    inside a longer unrelated word (`"practice"`); multi-word phrases
    are matched as a plain escaped substring - the phrase's own spaces
    already provide enough boundary in practice for this heuristic
    router (not safety-critical, unlike device-command parsing)."""
    compiled = []
    for raw in phrases:
        p = raw.strip()
        if not p:
            continue
        if " " not in p and p.isalnum():
            compiled.append(re.compile(r"\b" + re.escape(p) + r"\b"))
        else:
            compiled.append(re.compile(re.escape(p)))
    return compiled


_PATTERNS: Dict[Intent, List[Pattern]] = {
    Intent.DEVICE_CONTROL: _pats(
        "turn on", "turn off", "switch on", "switch off", "power on", "power off",
        "nyalakan", "matikan", "hidupkan", "padamkan", "redupkan", "kunci", "buka pintu",
        "tutup pintu", "unlock", "lock the", "dim the", "mute", "unmute", "play music",
        "putar lagu", "pause music", "stop music", "naikkan volume", "turunkan volume",
        "set brightness", "set the temperature", "atur suhu", "atur kecerahan",
    ),
    Intent.STATUS_QUERY: _pats(
        "apakah lampu", "apakah pintu", "berapa suhu",
        "what's the status", "what is the status", "cek status", "check status",
        "is it on", "is it off", "apakah nyala", "apakah mati", "sudah nyala",
        "sudah mati", "how many lights", "berapa lampu",
    ),
    Intent.WORLD_STATE: _pats(
        "what's on right now", "what is on right now", "apa saja yang nyala",
        "keadaan rumah", "status semua", "overview of my home", "ringkasan rumah",
        "everything that's on", "semua yang nyala",
    ),
    Intent.AUTOMATION: _pats(
        "automation", "otomatis", "routine", "rutinitas", "scene", "skenario",
        "every morning", "setiap pagi", "setiap malam", "every night", "every day",
        "setiap hari", "when i get home", "kalau saya pulang", "jika", "kalau",
    ),
    Intent.SCHEDULING: _pats(
        "remind me", "ingatkan saya", "ingatkan aku", "jadwalkan", "schedule a",
        "set a timer", "set an alarm", "pasang alarm", "pasang timer", "at 7am",
        "wake me up",
    ),
    Intent.SMART_HOME: _pats(
        "smart home", "rumah pintar", "lampu", "light bulb", "kipas angin",
        "colokan", "stopkontak", "smart plug", "saklar", "thermostat",
    ),
    Intent.VISION: _pats(
        "what do you see", "apa yang kamu lihat", "siapa di depan kamera", "camera",
        "kamera", "is anyone", "ada orang", "lihat sekitar", "deteksi objek",
        "who's in the room", "siapa di ruangan",
    ),
    Intent.MEMORY: _pats(
        "do you remember", "apa yang kamu ingat", "remember that", "ingat bahwa",
        "forget that", "lupakan bahwa", "what did i tell you", "apa yang aku bilang",
        "inget ya", "jangan lupa",
    ),
    Intent.SEARCH_WEB: _pats(
        "search the web", "search online", "cari di internet", "cari online",
        "latest news", "berita terbaru", "berita terkini", "current weather",
        "cuaca hari ini", "cuaca sekarang", "stock price", "harga saham",
        "skor pertandingan", "who won", "siapa yang menang", "what happened today",
        "apa yang terjadi hari ini",
    ),
    Intent.REASONING: _pats(
        "why", "explain", "jelaskan", "mengapa", "kenapa", "analyze", "analisa",
        "compare", "bandingkan", "apa bedanya", "pros and cons", "kelebihan dan kekurangan",
        "root cause", "figure out",
    ),
    Intent.PLANNING: _pats(
        "make a plan", "buatkan rencana", "susun rencana", "strategy", "strategi",
        "roadmap", "step by step plan", "rencana langkah",
    ),
    Intent.CODING: _pats(
        "write code", "write a function", "debug this", "fix this bug", "kode ini",
        "syntax error", "compile error", "refactor", "traceback", "stack trace",
        "write a script", "my code", "in my python", "in my javascript",
    ),
    Intent.MULTI_STEP: _pats(
        "first", "then", "after that", "lalu", "setelah itu", "dan kemudian",
        "step by step", "langkah demi langkah",
    ),
}

#: wildcard-shaped status-query phrasings ("is the X on/off", "apakah X
#: nyala/mati") - a plain literal-phrase match can't handle the device
#: name sitting in the middle, so these are real regexes, appended to
#: the literal STATUS_QUERY phrases above rather than replacing them.
_PATTERNS[Intent.STATUS_QUERY].extend([
    re.compile(r"\bis\s+the\s+\w+(\s+\w+){0,2}\s+(on|off)\b"),
    re.compile(r"\bapakah\s+\w+(\s+\w+){0,2}\s+(nyala|mati)\b"),
])

#: tie-break order when multiple intents score equally - specific/
#: actionable intents outrank vague conversational ones.
_PRIORITY: List[Intent] = [
    Intent.DEVICE_CONTROL, Intent.AUTOMATION, Intent.SCHEDULING, Intent.STATUS_QUERY,
    Intent.WORLD_STATE, Intent.SMART_HOME, Intent.CODING, Intent.PLANNING, Intent.REASONING,
    Intent.MULTI_STEP, Intent.SEARCH_WEB, Intent.VISION, Intent.MEMORY,
    Intent.GENERAL_QUESTION, Intent.GENERAL_CHAT,
]

_QUESTION_WORDS = _pats(
    "what", "who", "when", "where", "why", "how", "which",
    "apa", "siapa", "kapan", "dimana", "di mana", "mengapa", "kenapa", "bagaimana", "berapa",
)


def _looks_like_question(lower: str) -> bool:
    stripped = lower.strip()
    if stripped.endswith("?"):
        return True
    return any(p.match(stripped) for p in _QUESTION_WORDS)


def classify_intent(text: str) -> List[Intent]:
    """Never raises, never returns `[]`. `text` may be empty/None
    (treated as `GENERAL_CHAT`, matching `PlannerBridgeModule`'s own
    tolerance for an empty `user_utterance`)."""
    lower = (text or "").lower()
    if not lower.strip():
        return [Intent.GENERAL_CHAT]

    scores: Dict[Intent, int] = {}
    for intent, patterns in _PATTERNS.items():
        count = sum(1 for p in patterns if p.search(lower))
        if count:
            scores[intent] = count

    if not scores:
        return [Intent.GENERAL_QUESTION] if _looks_like_question(lower) else [Intent.GENERAL_CHAT]

    def _rank(item):
        intent, count = item
        priority_idx = _PRIORITY.index(intent) if intent in _PRIORITY else len(_PRIORITY)
        return (-count, priority_idx)

    ordered = sorted(scores.items(), key=_rank)
    return [intent for intent, _ in ordered]
