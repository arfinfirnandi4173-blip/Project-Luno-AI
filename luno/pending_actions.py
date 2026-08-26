"""
State percakapan super ringan: nunggu jawaban user buat SATU pertanyaan yang Luno
ajuin (mis. "mau main apa?" setelah script gaming mode jalan tanpa "open_apps"
tetap di config).

SENGAJA cuma nyimpen 1 pending action di satu waktu (bukan antrian) — kalau ada
pertanyaan baru sebelum yang lama dijawab, yang lama otomatis ke-replace. Ini
state jangka SANGAT pendek (biasanya cuma 1 giliran), beda dari long-term
memory/session summary yang memang didesain buat nyimpen lama.
"""

_pending = None  # None, atau dict {"type": ..., ...data tambahan}


def set_pending(action_type, **data):
    global _pending
    _pending = {"type": action_type, **data}


def get_pending():
    return _pending


def clear_pending():
    global _pending
    _pending = None
