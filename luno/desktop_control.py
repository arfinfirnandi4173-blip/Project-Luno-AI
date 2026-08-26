"""
Kontrol PC lokal — buka aplikasi/software, buka browser buat search, dan muter musik.

BEDA TOTAL dari devices.py (yang kontrol perangkat smart home lewat Home Assistant):
modul ini ngontrol KOMPUTER TEMPAT LUNO SENDIRI JALAN (buka Chrome, Spotify, dll di
PC kamu langsung), pakai fungsi bawaan OS (os.startfile / subprocess / webbrowser).

KEAMANAN: open_app() SENGAJA cuma bisa buka aplikasi yang terdaftar di config/apps.json
(allowlist) — GPT tidak pernah diberi akses buat menjalankan path/executable sembarangan
dari luar daftar ini.
"""

import os
import sys
import json
import subprocess
import webbrowser
from urllib.parse import quote

from . import config


def load_apps_config():
    """Load config/apps.json — mapping nama panggilan (lowercase) -> {"path", "args"}.

    Mendukung 2 format per-entry, boleh dicampur bebas:

    1) Format singkat — cuma path, TANPA argumen tambahan:
       "chrome": "C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe"

    2) Format lengkap — kalau butuh argumen command-line tambahan (mis. CapCut butuh
       "--src1"). JANGAN digabung jadi satu string kayak "app.exe --src1" — itu bakal
       dianggap sebagai satu nama file utuh (nggak ketemu). Pisahkan ke "args":
       "capcut": {
           "path": "C:\\\\Users\\\\kamu\\\\AppData\\\\Local\\\\CapCut\\\\Apps\\\\CapCut.exe",
           "args": ["--src1"]
       }

    Kalau file tidak ada/kosong, fitur open_app cukup nggak bisa buka apa-apa (nggak error) —
    search_browser & play_music tetap jalan normal karena keduanya nggak butuh config ini.
    """
    apps = {}
    if os.path.exists(config.APPS_CONFIG_FILE):
        try:
            with open(config.APPS_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for name, cfg in raw.items():
                key = name.strip().lower()
                if isinstance(cfg, dict):
                    apps[key] = {"path": cfg.get("path", ""), "args": cfg.get("args", [])}
                else:
                    apps[key] = {"path": cfg, "args": []}
            print(f"[Desktop] ✓ Loaded {len(apps)} app(s) from {config.APPS_CONFIG_FILE}")
        except Exception as ex:
            print(f"[Desktop] ✗ Failed to load {config.APPS_CONFIG_FILE}: {ex}")
    else:
        print(f"[Desktop] {config.APPS_CONFIG_FILE} not found — open_app nonaktif sampai diisi")
    return apps


APPS = load_apps_config()


def open_app(name):
    """Buka aplikasi terdaftar di APPS (allowlist, dari config/apps.json). Return
    (True, pesan) kalau sukses, (False, pesan) kalau nama nggak terdaftar/gagal buka."""
    name = (name or "").strip().lower()
    entry = APPS.get(name)
    if not entry:
        # SENGAJA nggak nyebutin daftar app yang ADA di sini (dulu ada
        # "Yang sudah ada: steam, chrome, ..." - reported gap: bikin
        # balasan LLM kepanjangan tiap kali gagal buka app). Fakta yang
        # dikirim ke LLM cukup pendek ("app X belum terdaftar") - LLM
        # sendiri yang improvise kalimat balasannya (natural, beda-beda
        # tiap kali dipanggil, bukan template statis) lewat instruksi
        # "in your own natural words" yang udah ada di
        # `build_verified_action_notes()` (main_runtime_demo.py). Kalau
        # nanti ada UI/dashboard yang BENERAN butuh daftar lengkap app
        # terdaftar, baca langsung dari `APPS` di situ - jangan taruh di
        # pesan yang ujung-ujungnya kebaca sama LLM/user.
        return False, f"Aplikasi '{name}' belum terdaftar di config/apps.json."

    path = entry.get("path", "")
    args = entry.get("args") or []
    if not path:
        return False, f"Path buat '{name}' kosong di config/apps.json."

    try:
        if args:
            # Ada argumen tambahan -> WAJIB lewat subprocess.Popen (os.startfile di
            # Windows nggak bisa nerima argumen sama sekali), dan ini jalan cross-platform.
            subprocess.Popen([path, *args])
        elif sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen([path])
        return True, f"Membuka {name}."
    except Exception as ex:
        return False, f"Gagal membuka {name}: {ex}"


def open_url(url):
    """Buka URL apa aja di browser SUNGGUHAN user - chrome.exe langsung
    (lewat entry "chrome" di config/apps.json, path yang sama dipakai
    open_app()) kalau terdaftar, atau fallback ke default OS browser
    lewat webbrowser.open() kalau nggak. SENGAJA bukan Playwright: ini
    generalisasi dari search_browser() di bawah (yang di-hardcode ke
    Google search doang) biar luno/browser's research/image-search
    intent (main_runtime_demo.py) bisa buka URL apa aja (termasuk
    Google Images) lewat mekanisme yang sama persis - chrome.exe asli,
    profile/login/bookmark user ikut kebawa, nggak butuh Playwright
    ATAU 'playwright install chromium' sama sekali buat fitur ini.
    Playwright tetap dipakai, tapi CUMA buat computer-use & monitoring
    dashboard visual inspection (yang emang butuh kontrol/screenshot
    halaman, bukan cuma munculin browser)."""
    url = (url or "").strip()
    if not url:
        return False, "Nggak ada URL yang mau dibuka."
    chrome = APPS.get("chrome")
    if chrome and chrome.get("path"):
        try:
            subprocess.Popen([chrome["path"], url])
            return True, f"Membuka {url} di Chrome."
        except Exception:
            pass  # chrome path terdaftar tapi gagal (mis. path salah) -> fallback ke default browser di bawah
    try:
        webbrowser.open(url)
        return True, f"Membuka {url} di browser."
    except Exception as ex:
        return False, f"Gagal membuka browser: {ex}"


def search_browser(query):
    """Buka browser default OS, search Google buat query tsb. BEDA dari search_web
    (Tavily, luno/web_search.py) — ini beneran munculin jendela browser di layar user
    (dia yang browsing sendiri), bukan info yang dibacain balik lewat GPT."""
    query = (query or "").strip()
    if not query:
        return False, "Nggak ada yang mau dicari."
    try:
        webbrowser.open(f"https://www.google.com/search?q={quote(query)}")
        return True, f"Membuka pencarian '{query}' di browser."
    except Exception as ex:
        return False, f"Gagal membuka browser: {ex}"


#: "buka X" gagal karena X bukan app terdaftar (config/apps.json) - kalau kata
#: platform-nya kedetek di teks yang gagal itu, arahkan ke pencarian SITUS itu
#: langsung (bukan Google umum). Key = kata yang dicari di slug target (sudah
#: di-_slugify pemanggil, jadi selalu lowercase/underscore-separated), value =
#: template URL pencarian situs tsb ({q} = query yang sudah di-quote).
_PLATFORM_SEARCH_URLS = {
    "youtube": ("https://www.youtube.com/results?search_query={q}", "YouTube"),
    "spotify": ("https://open.spotify.com/search/{q}", "Spotify"),
    "netflix": ("https://www.netflix.com/search?q={q}", "Netflix"),
    "instagram": ("https://www.instagram.com/explore/search/keyword/?q={q}", "Instagram"),
    "twitter": ("https://twitter.com/search?q={q}", "Twitter/X"),
    "tiktok": ("https://www.tiktok.com/search?q={q}", "TikTok"),
    "github": ("https://github.com/search?q={q}", "GitHub"),
}
#: kata penghubung yang dibuang dari query pencarian ("channel X di youtube"
#: -> "channel X"), TIDAK termasuk nama platform itu sendiri (dibuang
#: terpisah di bawah, cuma kalau memang match _PLATFORM_SEARCH_URLS).
_QUERY_STOPWORDS = {"di", "in", "on", "at"}


def guess_fallback_search_url(failed_target):
    """`failed_target` = slug (underscore-separated, lowercase - lihat
    `luno.planner.parser._slugify`) dari nama app yang GAGAL dibuka lewat
    `open_app()` (nggak terdaftar di config/apps.json). Dipakai buat
    fallback confirm-first: "app-nya nggak ada, mau dicariin di browser
    aja?" - lihat main_runtime_demo.py's AppNotFound handling.

    Deteksi kata platform (youtube/spotify/dll) di slug itu -> arahkan ke
    pencarian situs itu langsung; kalau nggak ada platform yang match,
    fallback ke Google search biasa buat sisa kata-katanya. Return
    (url, label) - label buat kalimat konfirmasi manusiawi, mis.
    ("https://www.youtube.com/results?search_query=channel+mr+beast",
    "mencari \"channel mr beast\" di YouTube")."""
    words = [w for w in (failed_target or "").split("_") if w]
    platform_word = None
    platform_url_template = None
    platform_label = None
    for w in words:
        hit = _PLATFORM_SEARCH_URLS.get(w)
        if hit:
            platform_word = w
            platform_url_template, platform_label = hit
            break
    query_words = [w for w in words if w not in _QUERY_STOPWORDS and w != platform_word]
    query = " ".join(query_words).strip() or " ".join(words)
    if platform_url_template:
        url = platform_url_template.format(q=quote(query))
        return url, f'mencari "{query}" di {platform_label}'
    url = f"https://www.google.com/search?q={quote(query)}"
    return url, f'mencari "{query}" di Google'


def play_music(query):
    """Muter musik. Kalau 'spotify' terdaftar di config/apps.json, coba buka langsung
    ke situ (lebih enak kalau memang dipakai sehari-hari); kalau tidak/gagal, fallback
    ke pencarian YouTube di browser (universal, nggak butuh app terinstall)."""
    query = (query or "").strip()
    if not query:
        return False, "Nggak ada judul/lagu yang mau diputer."

    if "spotify" in APPS:
        try:
            if sys.platform == "win32":
                os.startfile(f"spotify:search:{quote(query)}")
            else:
                webbrowser.open(f"https://open.spotify.com/search/{quote(query)}")
            return True, f"Memutar '{query}' di Spotify."
        except Exception:
            pass  # fallback ke YouTube di bawah

    try:
        webbrowser.open(f"https://www.youtube.com/results?search_query={quote(query)}")
        return True, f"Memutar '{query}' di YouTube."
    except Exception as ex:
        return False, f"Gagal memutar musik: {ex}"


# ─────────────────────────────────────────────
#  TOOL SCHEMA buat OpenAI function calling
# ─────────────────────────────────────────────

OPEN_APP_TOOL = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": (
            "Open a desktop application on the user's computer by name. Only apps registered "
            "by the user can be opened — if the requested app isn't registered, tell the user "
            "it needs to be added to config/apps.json first. Use this when the user asks to "
            "open/launch a specific program (not a website search — that's search_browser)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The app's name, e.g. 'chrome', 'spotify', 'vscode'."}
            },
            "required": ["name"],
        },
    },
}

SEARCH_BROWSER_TOOL = {
    "type": "function",
    "function": {
        "name": "search_browser",
        "description": (
            "Open the user's default web browser with a Google search for a query, so THEY "
            "can browse the results themselves. Use this when the user explicitly asks to open "
            "the browser / search on the web for something they want to look at themselves — "
            "NOT for questions you should just answer directly (use search_web for that instead)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
        },
    },
}

PLAY_MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": (
            "Play a song, artist, or playlist by opening it in Spotify (if configured) or "
            "YouTube. Use this whenever the user asks to play music."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Song, artist, or playlist to play."}},
            "required": ["query"],
        },
    },
}

TOOLS = [OPEN_APP_TOOL, SEARCH_BROWSER_TOOL, PLAY_MUSIC_TOOL]