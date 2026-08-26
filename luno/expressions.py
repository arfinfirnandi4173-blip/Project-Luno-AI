"""
Tebak ekspresi wajah avatar dari teks balasan Luno — CUMA heuristik kata kunci/tanda
baca, SENGAJA tidak lewat GPT (function calling) lagi, supaya nggak nambah latensi/
round-trip API di SETIAP balasan (beda dari save_memory/set_reminder yang cuma
kepake sesekali, ekspresi ini perlu dihitung tiap kali Luno ngomong).

Kalau nanti mau upgrade jadi lebih akurat (GPT yang nentuin), tinggal ganti isi
guess_expression() jadi manggil tool — pemanggilnya (main.py's speak()) nggak perlu
berubah sama sekali.
"""

import re

# Urutan dicek dari atas ke bawah — yang pertama cocok dipakai.
_PATTERNS = [
    ("laughing", re.compile(r"(wkwk|haha|lol|lmao|:\)|😂|🤣)", re.IGNORECASE)),
    ("surprised", re.compile(r"(wow|whoa|omg|astaga|gila|serius(?:an)?[!?]|!{2,}|\?!)", re.IGNORECASE)),
    ("sad", re.compile(r"(maaf|sori|sorry|sedih|:\(|😢|😭|yah\b)", re.IGNORECASE)),
    ("angry", re.compile(r"(kesel|marah|ugh|geez|damn)", re.IGNORECASE)),
    ("thinking", re.compile(r"(hmm+|coba (aku )?pikir|let me think|kayaknya|mungkin)", re.IGNORECASE)),
    ("happy", re.compile(r"(yatta|sugoi|senang|seneng|asyik|mantap|yay|😊|😄|!)", re.IGNORECASE)),
]


def guess_expression(text):
    """Return nama ekspresi (string) berdasarkan isi teks. Default 'neutral' kalau
    nggak ada pola yang cocok."""
    if not text:
        return "neutral"
    for expression, pattern in _PATTERNS:
        if pattern.search(text):
            return expression
    return "neutral"
