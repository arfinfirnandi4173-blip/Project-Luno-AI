"""
Reminder/alarm — "ingetin aku minum obat jam 8 malam", "wake me up tomorrow at 6am", dll.

Parsing waktu dari bahasa natural DISERAHKAN KE GPT lewat REMINDER_TOOL (function
calling) — GPT dikasih tau tanggal/jam SEKARANG di system prompt (lihat main.py's
build_system_prompt()), lalu dia yang menghitung 'besok jam 9 pagi'/'30 menit lagi'
jadi datetime ISO yang pasti. Jauh lebih robust daripada regex manual buat semua
variasi bahasa & format waktu.

File ini CUMA data layer (load/save/CRUD reminder ke reminders.json) + tool schema.
EKSEKUSI penjadwalan & notifikasi suara aktualnya ada di main.py (butuh akses ke
speak() dan ha_listener.ha_loop, yang belum dipindah ke modul terpisah).
"""

import os
import re
import json
import uuid
from datetime import datetime

from . import config
from . import persistence

_reminders = []  # list of {"id", "message", "trigger_at" (iso str), "created_at", "fired"}


def _load():
    """Persistent State Hardening V2 sprint: now loaded via
    `luno.persistence.safe_load_json()` - same missing/malformed
    fallback to `[]` as before, same "[Reminders] ..." log lines as
    before (kept domain-side rather than in the generic helper)."""
    global _reminders
    existed = os.path.exists(config.REMINDERS_FILE)
    data, source = persistence.safe_load_json(
        config.REMINDERS_FILE, default=[], validate=lambda d: isinstance(d, list),
    )
    _reminders = data
    if existed and source == "primary":
        print(f"[Reminders] ✓ Loaded {len(_reminders)} reminder(s)")
    elif existed and source == "default":
        print(f"[Reminders] ✗ Failed to load {config.REMINDERS_FILE}")


def _save():
    """Persistent State Hardening V2 sprint: now written via
    `luno.persistence.atomic_write_json()` - backup-before-write +
    temp-file + fsync + `os.replace()`, replacing the previous naive
    direct write (this store had ZERO atomicity before this sprint)."""
    try:
        persistence.atomic_write_json(config.REMINDERS_FILE, _reminders)
    except Exception as ex:
        print(f"[Reminders] ✗ Failed to save {config.REMINDERS_FILE}: {ex}")


_load()


def add_reminder(message, trigger_at_iso):
    """Simpan reminder baru. Return entry dict, atau None kalau message kosong /
    trigger_at_iso tidak valid."""
    message = (message or "").strip()
    if not message:
        return None
    try:
        trigger_dt = datetime.fromisoformat(trigger_at_iso)
    except Exception:
        return None

    entry = {
        "id": uuid.uuid4().hex[:8],
        "message": message,
        "trigger_at": trigger_dt.isoformat(timespec="seconds"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fired": False,
    }
    _reminders.append(entry)
    _save()
    print(f"[Reminders] ✓ Set: '{message}' at {entry['trigger_at']}")
    return entry


def mark_fired(reminder_id):
    for r in _reminders:
        if r["id"] == reminder_id:
            r["fired"] = True
    _save()


def remove_reminder(query_lower):
    """Batalin reminder yang BELUM fired dan cocok (substring dua arah) sama query.
    Return list entry yang dihapus (kosong kalau tidak ada yang cocok)."""
    global _reminders
    removed, kept = [], []
    for r in _reminders:
        if not r["fired"] and (query_lower in r["message"].lower() or r["message"].lower() in query_lower):
            removed.append(r)
        else:
            kept.append(r)
    if removed:
        _reminders = kept
        _save()
        print(f"[Reminders] ✓ Cancelled: {', '.join(r['message'] for r in removed)}")
    return removed


def list_pending():
    """Reminder yang belum fired, diurutkan dari yang paling deket waktunya. Dipakai
    juga saat startup buat re-schedule reminder yang sempat 'nganggur' pas Luno mati."""
    pending = [r for r in _reminders if not r["fired"]]
    return sorted(pending, key=lambda r: r["trigger_at"])


# ─────────────────────────────────────────────
#  DETEKSI PERINTAH REMINDER DARI TEKS (list & cancel — set_reminder lewat GPT tool)
# ─────────────────────────────────────────────

_LIST_PHRASES = [
    "reminder apa aja", "pengingat apa aja", "reminder apa yang masih ada",
    "pengingat apa yang masih ada", "reminder aku apa aja", "list reminder",
    "what reminders do i have", "list my reminders", "show my reminders",
]

_CANCEL_PATTERNS = [
    re.compile(r'^batal(?:in|kan)?\s+(?:reminder|pengingat)\s+(?:soal\s+|buat\s+|tentang\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^hapus\s+(?:reminder|pengingat)\s+(?:soal\s+|buat\s+|tentang\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^cancel\s+(?:the\s+|my\s+)?reminder\s+(?:about\s+|to\s+|for\s+)?(.+)$', re.IGNORECASE),
]


def is_list_command(user_lower):
    return any(p in user_lower for p in _LIST_PHRASES)


def detect_cancel_command(user_text):
    """Return query teks reminder yang mau dibatalin, atau None."""
    stripped = user_text.strip()
    for pattern in _CANCEL_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


# Tool schema buat OpenAI function calling — GPT sendiri yang mutusin kapan user minta
# diingetkan, dan dia yang ngitung waktu pastinya (dikasih tau waktu SEKARANG di system prompt).
REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "Set a reminder to notify the user about something at a specific future date/time. "
            "Call this whenever the user asks to be reminded/notified/woken up about something, "
            "e.g. 'remind me to take medicine at 8pm', 'ingetin aku minum obat jam 8 malam', "
            "'ingetin aku 30 menit lagi buat matiin kompor', 'wake me up tomorrow at 6am'. "
            "You are told the CURRENT date/time in the system prompt — use it to resolve "
            "relative expressions ('tomorrow', 'in 30 minutes', 'tonight', 'jam 8 malam') into "
            "one exact future datetime. Never call this with a time in the past."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Short description of what to remind the user about.",
                },
                "trigger_at": {
                    "type": "string",
                    "description": "The exact future date+time to fire this reminder, ISO 8601 format: YYYY-MM-DDTHH:MM:SS.",
                },
            },
            "required": ["message", "trigger_at"],
        },
    },
}
