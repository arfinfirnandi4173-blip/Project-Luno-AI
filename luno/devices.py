"""
Semua hal yang berkaitan dengan "device apa saja yang bisa dikontrol Luno":
- Controller (WLED/Switch/Script)
- Loader config dari lights.config.json / switches.config.json / scripts.config.json
- Registry runtime (instance controller yang sudah terhubung ke HA)
- Resolusi nama: mencari device mana yang dimaksud dari teks perintah user

Modul lain (engine.py nanti) HARUS mengakses state di sini lewat atribut modul
(mis. `devices.wled_lights`), BUKAN `from luno.devices import wled_lights` —
karena registry ini di-reset ulang tiap kali HA (re)connect, dan import langsung
akan "membeku" ke snapshot lama. Lihat build_devices() di bawah.
"""

import os
import re
import json
import asyncio

from .config import (
    MAX_BRIGHTNESS,
    DEFAULT_FADE_TRANSITION,
    RGB_LIGHT_NAME,
    RGB_LIGHT_ENTITY,
    LIGHTS_CONFIG_FILE,
    SWITCHES_CONFIG_FILE,
    SCRIPTS_CONFIG_FILE,
)

# ─────────────────────────────────────────────
#  WLED CONTROLLER
# ─────────────────────────────────────────────


class WLEDController:
    def __init__(self, ha_client, entity_id, name, max_brightness=None, default_transition=None):
        self.ha_client = ha_client
        self.entity_id = entity_id
        self.name = name
        # Limit & durasi fade per-lampu. Kalau tidak diset di config, fallback ke nilai global (.env).
        self.max_brightness = max(1, min(255, int(max_brightness))) if max_brightness else MAX_BRIGHTNESS
        self.default_transition = float(default_transition) if default_transition is not None else DEFAULT_FADE_TRANSITION

    async def turn_on(self, brightness=None, transition=None):
        """Nyalakan lampu — selalu fade perlahan dari gelap ke kecerahan target."""
        target = self.max_brightness if brightness is None else max(1, min(self.max_brightness, int(brightness)))
        fade_time = self.default_transition if transition is None else max(0, float(transition))
        return await self.fade_on(target, fade_time)

    async def turn_off(self):
        print(f"[WLED] ✓ {self.name}: Turning OFF")
        return await self.ha_client.call_service("light", "turn_off", self.entity_id)

    async def fade_on(self, target_brightness, transition_seconds=None):
        """Nyalakan lampu perlahan (fade-in) dari gelap ke kecerahan tertentu.
        Hanya menyasar entity_id milik controller ini sendiri — tidak pernah lampu lain."""
        transition_seconds = self.default_transition if transition_seconds is None else max(0, float(transition_seconds))
        target_brightness = max(1, min(self.max_brightness, int(target_brightness)))

        print(f"[WLED] ✓ {self.name}: Fade ON → brightness {target_brightness} over {transition_seconds}s (limit {self.max_brightness})")

        # Pastikan mulai dari kondisi hampir gelap dulu
        await self.ha_client.call_service(
            "light", "turn_on", self.entity_id,
            {"brightness": 1, "transition": 0}
        )
        await asyncio.sleep(0.3)

        # Lalu transisi naik ke target selama durasi yang ditentukan — hanya untuk self.entity_id
        return await self.ha_client.call_service(
            "light", "turn_on", self.entity_id,
            {"brightness": target_brightness, "transition": transition_seconds}
        )

    async def set_brightness(self, brightness):
        brightness = max(0, min(self.max_brightness, int(brightness)))
        print(f"[WLED] ✓ {self.name}: Brightness → {brightness} (limit {self.max_brightness})")
        return await self.ha_client.call_service(
            "light", "turn_on", self.entity_id,
            {"brightness": brightness}
        )

    async def set_color(self, rgb):
        r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
        print(f"[WLED] ✓ {self.name}: Color → RGB({r}, {g}, {b})")
        return await self.ha_client.call_service(
            "light", "turn_on", self.entity_id,
            {"rgb_color": [r, g, b]}
        )

    async def set_color_name(self, color_name):
        colors = {
            "red": (255, 0, 0),
            "green": (0, 255, 0),
            "blue": (0, 0, 255),
            "yellow": (255, 255, 0),
            "cyan": (0, 255, 255),
            "magenta": (255, 0, 255),
            "white": (255, 255, 255),
            "orange": (255, 165, 0),
            "purple": (128, 0, 128),
            "pink": (255, 192, 203),
        }

        if color_name.lower() in colors:
            return await self.set_color(colors[color_name.lower()])
        return False


# ─────────────────────────────────────────────
#  SWITCH CONTROLLER
# ─────────────────────────────────────────────


class SwitchController:
    def __init__(self, ha_client, entity_id, name):
        self.ha_client = ha_client
        self.entity_id = entity_id
        self.name = name

    async def turn_on(self):
        print(f"[Switch] ✓ {self.name}: Turning ON")
        return await self.ha_client.call_service("switch", "turn_on", self.entity_id)

    async def turn_off(self):
        print(f"[Switch] ✓ {self.name}: Turning OFF")
        return await self.ha_client.call_service("switch", "turn_off", self.entity_id)


# ─────────────────────────────────────────────
#  SCRIPT CONTROLLER (jalankan HA script by name, mis. "Gaming Mode")
# ─────────────────────────────────────────────


class ScriptController:
    def __init__(self, ha_client, entity_id, name, feedback=None, description=None, open_apps=None, ask_open_app=False):
        self.ha_client = ha_client
        self.entity_id = entity_id
        self.name = name
        # feedback: kalau diisi di config, ini kalimat TETAP (dipakai apa adanya, tidak lewat GPT).
        #           Cocok untuk script penting yang butuh wording pasti/konsisten.
        # description: konteks singkat soal script ini (mis. "dims lights and turns on TV"),
        #           dipakai GPT untuk bikin kalimat konfirmasi yang variatif tiap kali dipanggil.
        # open_apps: list nama app (harus terdaftar di config/apps.json) yang OTOMATIS dibuka
        #           di PC begitu script ini jalan — mis. script "gaming mode" buka Steam sekalian.
        # ask_open_app: True -> alih-alih auto-buka app tetap, Luno NANYA dulu "mau main apa?"
        #           dan buka app sesuai jawaban giliran berikutnya. Diabaikan kalau open_apps
        #           udah diisi (nggak masuk akal nanya DAN auto-buka sekaligus).
        self.feedback = feedback
        self.description = description
        self.open_apps = open_apps or []
        self.ask_open_app = ask_open_app

    async def run(self):
        print(f"[Script] ✓ {self.name}: Running")
        # HA convention: script.turn_on men-trigger script apa pun, tidak perlu service khusus per-script.
        return await self.ha_client.call_service("script", "turn_on", self.entity_id)


# ─────────────────────────────────────────────
#  LIGHTS CONFIG (multi-lampu RGB)
# ─────────────────────────────────────────────


def load_lights_config():
    """
    Load daftar lampu RGB dari file JSON.

    Mendukung 2 format per-entry, boleh dicampur bebas dalam satu file:

    1) Format singkat (lama, tetap didukung) — cuma entity_id:
       "lampu dapur": "light.wled_dapur"

    2) Format lengkap (baru) — bisa atur limit kecerahan & fade per lampu,
       tanpa perlu sentuh kode sama sekali saat menambah lampu baru:
       "lampu kamar": {
           "entity_id": "light.wled_kamar",
           "max_brightness": 180,       // opsional, default: MAX_BRIGHTNESS global (.env)
           "fade_transition": 8,        // opsional (detik), default: DEFAULT_FADE_TRANSITION global (.env)
           "aliases": ["kamar", "bedroom light"],  // opsional: nama panggilan tambahan
           "area": "kamar"              // opsional (Sprint 60) — lihat di bawah
       }

    "aliases" penting kalau kamu suka campur bahasa — mis. key utama "main lamp"
    (Inggris) tapi kamu sering bilang "lampu utama" (Indonesia): tambahkan
    "aliases": ["lampu utama"] supaya keduanya dikenali untuk lampu yang sama.

    "area" (Sprint 60 — Structured Room/Area Schema Foundation): field STRING
    opsional yang menandai lampu ini "ada di ruangan/area mana" (mis. "kamar",
    dan nantinya "dapur"/"ruang tamu"/dst kalau project ini pernah punya lebih
    dari satu ruangan terkonfigurasi). Sepenuhnya ADDITIVE dan BACKWARD
    COMPATIBLE — device lama tanpa "area" tetap valid persis seperti sebelum
    Sprint 60 ada (lihat validasi di bawah). TIDAK memaksa satu lampu pun untuk
    langsung diberi area; TIDAK mengubah bagaimana entity_id/alias/domain
    di-resolve sama sekali (resolver Sprint 52 tidak pernah membaca field ini).
    Dibaca lewat `get_device_area()`/`get_devices_by_area()` di bawah, dan
    dipakai (kalau ADA datanya) sebagai source-of-truth opsional oleh Sprint
    59's single-room group expansion di `main_runtime_demo.py` — kalau TIDAK
    ada satupun lampu yang punya "area" (registry belum di-migrasi), perilaku
    lama Sprint 59 (anggap seluruh registry = "kamar") tetap dipakai apa
    adanya, tanpa perubahan apa pun.

    Validasi "area" (tidak pernah membuat load gagal / registry rusak):
      - tidak ada field "area" sama sekali  -> valid, disimpan sebagai None
      - "area": ""  (string kosong/whitespace saja) -> diperlakukan SAMA
        seperti tidak diisi (None) — konsisten dengan bagaimana "aliases"
        kosong sudah difilter di atas
      - "area": "Kamar " (spasi/kapital campur) -> dirapikan jadi "kamar"
        (strip + lowercase), SAMA seperti bagaimana key/alias lain di file
        ini sudah dirapikan, supaya pencocokan nanti case-insensitive
      - "area": 123 / ["kamar"] / dst (bukan string) -> DIABAIKAN dengan
        peringatan di konsol (disimpan sebagai None), device itu SENDIRI
        tetap diregister seperti biasa — field opsional yang salah tipe
        tidak boleh menggagalkan device yang sebenarnya valid

    Nama key (kiri) adalah nama panggilan yang dikenali dari perintah suara/teks.
    Kalau file tidak ditemukan/kosong, fallback ke satu lampu default dari .env
    (RGB_LIGHT_ENTITY / RGB_LIGHT_NAME) — supaya tetap kompatibel dengan setup lama.
    """
    lights = {}
    if os.path.exists(LIGHTS_CONFIG_FILE):
        try:
            with open(LIGHTS_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for name, cfg in raw.items():
                key = name.strip().lower()
                if isinstance(cfg, dict):
                    entity_id = cfg.get("entity_id")
                    if not entity_id:
                        print(f"[Lights] ✗ Skip '{name}': 'entity_id' wajib diisi")
                        continue
                    aliases = [a.strip().lower() for a in cfg.get("aliases", []) if a.strip()]
                    area = _normalize_optional_area(cfg.get("area"), name)
                    lights[key] = {
                        "entity_id": entity_id,
                        "max_brightness": cfg.get("max_brightness"),
                        "fade_transition": cfg.get("fade_transition"),
                        "aliases": aliases,
                        "area": area,
                    }
                else:
                    # Format singkat: cuma string entity_id
                    lights[key] = {
                        "entity_id": cfg,
                        "max_brightness": None,
                        "fade_transition": None,
                        "aliases": [],
                        "area": None,
                    }
            print(f"[Lights] ✓ Loaded {len(lights)} light(s) from {LIGHTS_CONFIG_FILE}")
        except Exception as ex:
            print(f"[Lights] ✗ Failed to load {LIGHTS_CONFIG_FILE}: {ex}")

    if not lights:
        lights[RGB_LIGHT_NAME.strip().lower()] = {
            "entity_id": RGB_LIGHT_ENTITY,
            "max_brightness": None,
            "fade_transition": None,
            "aliases": [],
            "area": None,
        }
        print(f"[Lights] Using single default light: {RGB_LIGHT_NAME} ({RGB_LIGHT_ENTITY})")

    return lights


def _normalize_optional_area(raw_area, device_name):
    """Sprint 60 — validate/normalize the optional `"area"` field for a
    single `lights.config.json` entry. Never raises, never fails the whole
    device: an invalid `area` is logged and treated as absent (`None`),
    exactly like a device with no `"area"` key at all. See
    `load_lights_config()`'s own docstring for the full validation table."""
    if raw_area is None:
        return None
    if not isinstance(raw_area, str):
        print(
            f"[Lights] ⚠ '{device_name}': 'area' harus berupa string, diabaikan "
            f"(dapat: {type(raw_area).__name__})"
        )
        return None
    normalized = raw_area.strip().lower()
    return normalized or None


# ─────────────────────────────────────────────
#  SWITCHES CONFIG
# ─────────────────────────────────────────────


def load_switches_config():
    """
    Load daftar saklar dari file JSON, contoh isi switches.config.json:
    {
        "colokan tv": "switch.colokan_tv",
        "saklar taman": "switch.saklar_taman"
    }
    Nama key (kiri) adalah nama panggilan yang dikenali dari perintah suara/teks.
    Beda dengan lampu, saklar tidak punya default — kalau file tidak ada, fitur
    saklar cukup tidak aktif (tidak error), tinggal buat file-nya kapan saja.
    """
    switches = {}
    if os.path.exists(SWITCHES_CONFIG_FILE):
        try:
            with open(SWITCHES_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for name, entity_id in raw.items():
                switches[name.strip().lower()] = entity_id
            print(f"[Switches] ✓ Loaded {len(switches)} switch(es) from {SWITCHES_CONFIG_FILE}")
        except Exception as ex:
            print(f"[Switches] ✗ Failed to load {SWITCHES_CONFIG_FILE}: {ex}")

    return switches


# ─────────────────────────────────────────────
#  SCRIPTS CONFIG (jalankan HA script by name, mis. "Gaming Mode")
# ─────────────────────────────────────────────


def load_scripts_config():
    """
    Load daftar script HA dari file JSON, contoh isi scripts.config.json.

    Mendukung 2 format per-entry, boleh dicampur bebas:

    1) Format singkat — cuma entity_id. Feedback suara akan di-generate oleh
       GPT setiap kali dijalankan (variatif, tidak monoton kalimat yang sama):
       "movie night": "script.movie_night"

    2) Format lengkap — 2 opsi feedback, plus opsional "open_apps" ATAU "ask_open_app":
       "gaming mode": {
           "entity_id": "script.gaming_mode",
           "description": "dims the lights red/purple and turns on the PC RGB",
           "open_apps": ["steam"]
       }
       "gaming mode tanya dulu": {
           "entity_id": "script.gaming_mode",
           "ask_open_app": true
       }
       "emergency stop": {
           "entity_id": "script.emergency_stop",
           "feedback": "Emergency stop activated. All devices are off."
       }

    "open_apps": list nama app (HARUS sudah terdaftar di config/apps.json) yang
    otomatis dibuka di PC begitu script ini jalan — mis. "gaming mode" sekalian
    buka Steam. Opsional, boleh diisi lebih dari satu nama sekaligus.

    "ask_open_app": true -> alih-alih auto-buka app TETAP, Luno nanya dulu "mau
    main apa?" dan buka app sesuai jawaban giliran berikutnya. Diabaikan kalau
    "open_apps" udah diisi (pilih salah satu, bukan dua-duanya).

    Nama key (kiri) adalah nama panggilan yang dikenali dari perintah suara/teks.
    Tidak ada default — kalau file tidak ada, fitur script cukup tidak aktif.
    """
    scripts = {}
    if os.path.exists(SCRIPTS_CONFIG_FILE):
        try:
            with open(SCRIPTS_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for name, cfg in raw.items():
                key = name.strip().lower()
                if isinstance(cfg, dict):
                    entity_id = cfg.get("entity_id")
                    if not entity_id:
                        print(f"[Scripts] ✗ Skip '{name}': 'entity_id' wajib diisi")
                        continue
                    scripts[key] = {
                        "entity_id": entity_id,
                        "feedback": cfg.get("feedback"),
                        "description": cfg.get("description"),
                        "open_apps": cfg.get("open_apps", []),
                        "ask_open_app": cfg.get("ask_open_app", False),
                    }
                else:
                    scripts[key] = {"entity_id": cfg, "feedback": None, "description": None, "open_apps": [], "ask_open_app": False}
            print(f"[Scripts] ✓ Loaded {len(scripts)} script(s) from {SCRIPTS_CONFIG_FILE}")
        except Exception as ex:
            print(f"[Scripts] ✗ Failed to load {SCRIPTS_CONFIG_FILE}: {ex}")

    return scripts


# ─────────────────────────────────────────────
#  STATE (loaded configs + runtime registries)
# ─────────────────────────────────────────────

# Config mentah dari file JSON (nama -> dict/entity_id), di-load sekali saat modul diimpor.
LIGHTS = load_lights_config()
DEFAULT_LIGHT_NAME = next(iter(LIGHTS))
SWITCHES = load_switches_config()
SCRIPTS = load_scripts_config()

# Registry runtime: nama (lowercase) -> instance controller yang SUDAH terhubung ke HA.
# Diisi oleh build_devices() setelah koneksi HA berhasil. PENTING: modul lain harus
# akses ini lewat `devices.wled_lights` (bukan `from luno.devices import wled_lights`)
# karena build_devices() mengisi ulang isinya tiap kali dipanggil.
wled_lights = {}     # name (lowercase) -> WLEDController
switch_devices = {}  # name (lowercase) -> SwitchController
script_devices = {}  # name (lowercase) -> ScriptController
device_states = {}   # entity_id -> {"state": "on"/"off", "attributes": {...}}, untuk status query
wled = None          # controller lampu default, untuk kompatibilitas/fallback

_devices_built = False  # guard supaya controller cuma dibangun sekali walau HA reconnect berkali-kali


def build_devices(ha_client):
    """
    Bangun semua controller (WLED/Switch/Script) dari LIGHTS/SWITCHES/SCRIPTS config,
    terhubung ke ha_client yang diberikan. Aman dipanggil berkali-kali (idempotent) —
    dipanggil ulang tiap kali HA reconnect, tapi cuma benar-benar membangun sekali,
    karena ha_client itu singleton yang connected-state-nya sudah otomatis ke-refresh
    sendiri tiap reconnect (controller lama masih valid, tidak perlu dibuat ulang).
    """
    global wled, _devices_built

    if _devices_built:
        return

    wled_lights.clear()
    for name, cfg in LIGHTS.items():
        controller = WLEDController(
            ha_client,
            cfg["entity_id"],
            name,
            max_brightness=cfg.get("max_brightness"),
            default_transition=cfg.get("fade_transition"),
        )
        wled_lights[name] = controller
        for alias in cfg.get("aliases", []):
            wled_lights[alias] = controller  # alias menunjuk ke controller fisik yang sama
    wled = wled_lights[DEFAULT_LIGHT_NAME]
    print(f"[WLED] ✓ Ready — {len(wled_lights)} name(s): {', '.join(wled_lights.keys())}\n")

    switch_devices.clear()
    switch_devices.update({
        name: SwitchController(ha_client, entity_id, name)
        for name, entity_id in SWITCHES.items()
    })
    if switch_devices:
        print(f"[Switch] ✓ Ready — {len(switch_devices)} switch(es): {', '.join(switch_devices.keys())}\n")

    script_devices.clear()
    script_devices.update({
        name: ScriptController(
            ha_client, cfg["entity_id"], name,
            feedback=cfg.get("feedback"),
            description=cfg.get("description"),
            open_apps=cfg.get("open_apps"),
            ask_open_app=cfg.get("ask_open_app", False),
        )
        for name, cfg in SCRIPTS.items()
    })
    if script_devices:
        print(f"[Script] ✓ Ready — {len(script_devices)} script(es): {', '.join(script_devices.keys())}\n")

    _devices_built = True


async def handle_ha_event(event_data):
    """Callback yang dipanggil tiap ada event state_changed dari HA (lewat
    ha_client.listen_and_dispatch). Update device_states supaya status-query
    (mis. 'apakah lampu utama nyala?') selalu akurat real-time."""
    try:
        new_state = event_data.get("new_state", {})
        entity_id = new_state.get("entity_id", "unknown")

        if not any(x in entity_id for x in ["light", "switch"]):
            return

        state = new_state.get("state", "unknown")
        device_states[entity_id] = {
            "state": state,
            "attributes": new_state.get("attributes", {}),
        }
        print(f"[Event] {entity_id}: {state}")
    except Exception:
        pass


async def refresh_all_device_states(ha_client):
    """Ambil snapshot state semua lampu/saklar yang terdaftar, dipanggil sekali
    tiap kali (re)connect ke HA supaya status-query langsung akurat sejak awal."""
    tracked_entity_ids = {c.entity_id for c in wled_lights.values()} | {c.entity_id for c in switch_devices.values()}
    if not tracked_entity_ids:
        return

    all_states = await ha_client.get_states()
    count = 0
    for s in all_states:
        entity_id = s.get("entity_id")
        if entity_id in tracked_entity_ids:
            device_states[entity_id] = {"state": s.get("state", "unknown"), "attributes": s.get("attributes", {})}
            count += 1
    print(f"[HA] ✓ Loaded initial state for {count} device(s)\n")


# ─────────────────────────────────────────────
#  AREA/ROOM METADATA (Sprint 60 — Structured Room/Area Schema Foundation)
# ─────────────────────────────────────────────
#
# Two small, pure, read-only helpers over the already-loaded `LIGHTS`
# registry (`config/lights.config.json`'s optional `"area"` field — see
# `load_lights_config()`'s own docstring). NOT a second resolver, NOT a
# second registry, NOT a persistent-state store of their own: they only
# ever read the exact same `LIGHTS` dict every other lookup in this
# module already reads, the same way `_lookup_light()` in
# `luno/tool_manager/builtin/real_home_assistant.py` does. No HA call,
# no network, no LLM/embedding — a plain in-process dict lookup, safe to
# call as often as needed (`main_runtime_demo.py`'s own Sprint 60
# integration calls these once per single-room group command, well under
# the 5ms performance target — see `tests/test_sprint60_area_schema.py`).


def get_device_area(name_or_alias):
    """Sprint 60 — the (already-normalized, lowercased) `"area"` this
    light is tagged with in `config/lights.config.json`, looked up by its
    canonical name OR any of its aliases (case-insensitive, same matching
    convention `resolve_target_lights()` already uses below). Returns
    `None` if the name/alias isn't a known light at all, OR if it's a
    known light with no `"area"` set (both are "no answer", deliberately
    not distinguished — callers that need to tell the two apart already
    have `LIGHTS` itself for that). Never raises on bad input (`None`/
    non-string), never touches HA."""
    if not name_or_alias or not isinstance(name_or_alias, str):
        return None
    wanted = name_or_alias.strip().lower()
    if not wanted:
        return None
    for key, cfg in LIGHTS.items():
        if not isinstance(cfg, dict):
            continue
        # Normalize the KEY too (not just `wanted`) rather than assuming
        # it's already lowercased - true whenever `LIGHTS` came from
        # `load_lights_config()` (production), but not guaranteed for a
        # registry a test/caller patched directly with mixed-case keys.
        key_norm = key.strip().lower() if isinstance(key, str) else str(key)
        aliases_norm = [a.strip().lower() for a in (cfg.get("aliases") or []) if isinstance(a, str)]
        if key_norm == wanted or wanted in aliases_norm:
            return cfg.get("area")
    return None


def get_devices_by_area(area):
    """Sprint 60 — canonical `LIGHTS` names (NOT aliases — the same keys
    `LIGHTS.items()` itself iterates, matching how `main_runtime_demo.
    py`'s Sprint 58/59 group-expansion loop already enumerates lights)
    whose `"area"` field matches `area`, case-insensitively. Returns `[]`
    — never raises, never guesses — for: an unknown/never-seen area name,
    an empty/whitespace-only string, `None`, or a non-string `area`
    argument. Deliberately does NOT fall back to "every light" when no
    light anywhere has `"area"` set at all — that decision (whether to
    treat an unmigrated registry as backward-compatible with Sprint 59's
    original behavior) belongs to the CALLER, which can already tell
    "zero light matched this specific area" apart from "zero light has
    ANY area set" by checking `LIGHTS` itself if it needs to (see
    `main_runtime_demo.py`'s own Sprint 60 integration for exactly that
    distinction)."""
    if not area or not isinstance(area, str):
        return []
    wanted = area.strip().lower()
    if not wanted:
        return []
    result = []
    for key, cfg in LIGHTS.items():
        if not isinstance(cfg, dict):
            continue
        raw_area = cfg.get("area")
        if isinstance(raw_area, str) and raw_area.strip().lower() == wanted:
            result.append(key)
    return result


# ─────────────────────────────────────────────
#  RESOLUSI NAMA DEVICE DARI TEKS PERINTAH
# ─────────────────────────────────────────────

ALL_DEVICE_PHRASES = [
    "all device", "all devices", "semua perangkat", "semua device",
    "everything off", "everything on", "turn everything off", "turn everything on",
]


def _dedupe_controllers(controllers):
    seen = set()
    unique = []
    for ctrl in controllers:
        if id(ctrl) not in seen:
            seen.add(id(ctrl))
            unique.append(ctrl)
    return unique


def resolve_target_lights(user_lower):
    """
    Cari lampu mana yang dimaksud user berdasarkan nama panggilan di lights.config.json.
    - Sebut 'semua lampu' / 'all lights' / 'all device(s)' -> kontrol semua lampu sekaligus.
    - Sebut nama spesifik (mis. 'lampu kamar') -> hanya lampu yang cocok.
    - Tidak sebut nama apa pun -> fallback ke lampu default (kompatibel dengan setup 1 lampu).
    """
    if not wled_lights:
        return []

    select_all_phrases = ["semua lampu", "semua rgb", "all light", "all the light", "all rgb"] + ALL_DEVICE_PHRASES
    if any(p in user_lower for p in select_all_phrases):
        return _dedupe_controllers(wled_lights.values())

    matched = [ctrl for name, ctrl in wled_lights.items() if name in user_lower]
    if matched:
        return _dedupe_controllers(matched)

    return [wled] if wled else []


# Kata-kata generik/filler yang dibuang saat menebak "nama lampu" yang disebut user,
# supaya kata kerja/basa-basi tidak ikut kebaca sebagai bagian dari nama device.
_LIGHT_NAME_FILLERS = {
    "nyalakan", "nyalain", "matikan", "matiin", "hidupkan", "hidupin", "padamkan",
    "turn", "on", "off", "the", "please", "tolong", "dong", "ya", "yah", "nih", "deh",
    "sekarang", "dulu", "aja", "juga", "itu", "ini", "nya", "semua", "all", "light",
    "lights", "lampu", "lampunya", "rgb", "wled", "gak", "ga", "coba", "bisa",
    "bisakah", "could", "you", "would", "and", "dan", "buat", "untuk", "di", "in",
}

# Pola untuk menangkap kandidat nama lampu dari kalimat, misal:
# "lampu utama" -> "utama", "main light" -> "main", "nyalain lampu kamar tidur" -> "kamar tidur"
_NAMED_LIGHT_PATTERNS = [
    re.compile(r"\blampu(?:nya)?\s+([a-z][a-z\s]{0,30})"),
    re.compile(r"\b([a-z][a-z\s]{0,30}?)\s+lights?\b"),
]


def _extract_named_light_candidate(user_lower):
    """Coba tebak nama lampu spesifik yang disebut user (mis. 'lampu utama' -> 'utama').
    Return None kalau user cuma menyebut kata generik ('lampu' / 'the light' / 'semua lampu')
    tanpa nama tambahan yang jelas, supaya ucapan biasa tidak salah dianggap menyebut nama
    device yang tidak dikenal."""
    for pattern in _NAMED_LIGHT_PATTERNS:
        for m in pattern.finditer(user_lower):
            words = [w for w in m.group(1).split() if w not in _LIGHT_NAME_FILLERS]
            if words:
                return " ".join(words[:3])
    return None


# Kata kerja/aksi yang menandakan ini benar-benar PERINTAH ke lampu (bukan cuma obrolan
# yang kebetulan menyebut kata "lampu"). Dipakai supaya deteksi "nama lampu tidak ketemu"
# di bawah tidak salah nyala untuk kalimat santai seperti "lampu di kamar itu bagus ya".
_LIGHT_ACTION_WORDS = [
    "on", "off", "turn", "nyalakan", "nyalain", "nyala", "hidup", "hidupkan", "hidupin",
    "matikan", "matiin", "mati", "padamkan", "brightness", "terang", "bright", "dim",
    "color", "warna", "fade", "transisi", "perlahan", "pelan",
]


def light_name_not_found(user_lower):
    """Return kandidat nama (string) kalau user sepertinya menyebut PERINTAH ke lampu
    dengan nama spesifik, tapi nama itu TIDAK cocok dengan lampu manapun yang terdaftar
    di lights.config.json. Return None kalau tidak ada masalah (nama cocok, atau user
    cuma sebut generik/'semua lampu', atau ini bukan perintah lampu sama sekali).

    Ini krusial supaya Luno berperilaku seperti Google Home/Google Assistant: kalau kamu
    bilang "nyalakan lampu utama" tapi tidak ada device bernama itu, Luno akan bilang
    "device tidak ditemukan" — BUKAN diam-diam menyalakan lampu lain yang salah (mis.
    RGB Strip) seperti yang terjadi sebelumnya karena fallback otomatis ke lampu default.
    """
    if not wled_lights:
        return None
    if not any(w in user_lower for w in _LIGHT_ACTION_WORDS):
        return None  # bukan perintah lampu, jangan ganggu obrolan biasa

    select_all_phrases = ["semua lampu", "semua rgb", "all light", "all the light", "all rgb"] + ALL_DEVICE_PHRASES
    if any(p in user_lower for p in select_all_phrases):
        return None

    if any(name in user_lower for name in wled_lights.keys()):
        return None  # ada nama terdaftar yang cocok -> bukan kasus "tidak ketemu"

    return _extract_named_light_candidate(user_lower)


def resolve_explicit_lights(user_lower):
    """Sama seperti resolve_target_lights, TAPI tanpa fallback ke lampu default kalau
    tidak ada nama yang disebut. Dipakai untuk status-query: kita cuma mau tahu status
    kalau user benar-benar menyebut nama lampu/'semua lampu', bukan asal tebak default —
    supaya pertanyaan umum yang kebetulan mengandung kata tanya tidak salah dianggap
    nanya status lampu default."""
    if not wled_lights:
        return []
    select_all_phrases = ["semua lampu", "semua rgb", "all light", "all the light", "all rgb"] + ALL_DEVICE_PHRASES
    if any(p in user_lower for p in select_all_phrases):
        return _dedupe_controllers(wled_lights.values())
    return _dedupe_controllers([ctrl for name, ctrl in wled_lights.items() if name in user_lower])


def resolve_target_switches(user_lower):
    """
    Cari saklar mana yang dimaksud user berdasarkan nama panggilan di switches.config.json.
    - Sebut 'semua saklar' / 'all switches' / 'all device(s)' -> kontrol semua saklar sekaligus.
    - Sebut nama spesifik (mis. 'colokan tv') -> hanya saklar yang cocok.
    - Tidak sebut nama tapi cuma ada 1 saklar terdaftar -> pakai saklar itu (default).
    - Tidak sebut nama & ada lebih dari 1 saklar -> tidak dieksekusi (hindari salah sasaran).
    """
    if not switch_devices:
        return []

    select_all_phrases = ["semua saklar", "semua switch", "all switch"] + ALL_DEVICE_PHRASES
    if any(p in user_lower for p in select_all_phrases):
        return list(switch_devices.values())

    matched = [ctrl for name, ctrl in switch_devices.items() if name in user_lower]
    if matched:
        return matched

    generic_switch_words = ["saklar", "switch", "colokan", "stopkontak", "plug"]
    if any(w in user_lower for w in generic_switch_words) and len(switch_devices) == 1:
        return list(switch_devices.values())

    return []


def resolve_explicit_switches(user_lower):
    """Sama seperti resolve_target_switches, TAPI tanpa fallback ke saklar tunggal
    kalau tidak ada nama yang disebut. Dipakai untuk status-query (lihat alasan di
    resolve_explicit_lights)."""
    if not switch_devices:
        return []
    select_all_phrases = ["semua saklar", "semua switch", "all switch"] + ALL_DEVICE_PHRASES
    if any(p in user_lower for p in select_all_phrases):
        return list(switch_devices.values())
    return [ctrl for name, ctrl in switch_devices.items() if name in user_lower]