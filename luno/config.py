"""
Semua konstanta konfigurasi Luno, diambil dari .env (lewat os.getenv).

Modul ini SENGAJA tidak punya logic lain selain baca .env dan validasi dasar —
tujuannya supaya semua modul lain (ha_client, devices, engine, stt, tts, dst)
bisa import konstanta dari sini tanpa risiko circular import, dan supaya kalau
mau ganti default suatu setting, cukup edit di SATU tempat ini.
"""

import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  CORE / API KEYS
# ─────────────────────────────────────────────

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Kosongin buat OpenAI resmi (default). Isi kalau mau pakai provider LAIN yang API-nya
# kompatibel format OpenAI, contoh:
#   DeepSeek:   https://api.deepseek.com
#   OpenRouter: https://openrouter.ai/api/v1
# Ganti OPENAI_API_KEY di atas jadi API key provider itu juga, dan OPENAI_MODEL (di
# bawah) jadi nama model punya provider itu (mis. "deepseek-chat" buat DeepSeek).
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
HA_URL = os.getenv("HA_URL", "http://localhost:8123")
HA_TOKEN = os.getenv("HA_TOKEN")

# BUG FIX: `HA_WS_URL` used to default to "ws://localhost:8123/api/websocket"
# UNCONDITIONALLY whenever it wasn't set explicitly - completely ignoring
# `HA_URL` above. Someone who only set `HA_URL` (e.g. to their real HA
# server's LAN address) still had the actual WebSocket client silently
# connect to localhost instead - it would even report "connected" if
# something else happened to be listening on localhost:8123, with zero
# error, making it look like Home Assistant was working while every
# command was actually being sent to the wrong server (or nothing at
# all). Now `HA_WS_URL`, if not explicitly set, is DERIVED from `HA_URL`
# (http->ws, https->wss, "/api/websocket" appended) - only falling back
# to the hardcoded localhost default when `HA_URL` itself is also unset.
# An explicit `HA_WS_URL` in .env still always wins, unchanged.
def _derive_ha_ws_url(ha_url: str) -> str:
    base = (ha_url or "").strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not (base.startswith("ws://") or base.startswith("wss://")):
        base = "ws://" + base
    return base + "/api/websocket"


_HA_WS_URL_ENV = os.getenv("HA_WS_URL")
if _HA_WS_URL_ENV:
    HA_WS_URL = _HA_WS_URL_ENV
elif os.getenv("HA_URL"):
    HA_WS_URL = _derive_ha_ws_url(HA_URL)
else:
    HA_WS_URL = "ws://localhost:8123/api/websocket"

# API key buat fitur web search (info real-time: berita, cuaca, harga, dll).
# Daftar gratis di https://tavily.com buat dapetin key-nya. Kalau dikosongin,
# fitur search_web otomatis nonaktif (Luno tetap jalan normal tanpa itu).
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ─────────────────────────────────────────────
#  LLM MANAGER (Multi-LLM Provider System sprint)
# ─────────────────────────────────────────────
# `luno.adapters.llm_manager.LLMManagerAdapter` and every provider under
# `luno.adapters.llm.*` read these DIRECTLY via `os.getenv()` (same
# "packages read their own env independently" convention
# `OpenRouterConfig.from_env()`/`RealFishAudioConfig.from_env()` already
# established) - the constants below are re-exported from here purely
# for discoverability/documentation (this file's own stated purpose:
# "satu tempat" to see every setting), not because anything imports them
# from `luno.config` at runtime. See `luno/adapters/llm/config.py`'s own
# docstring for the full per-provider env var list (`OPENROUTER_*`/
# `OPENAI_*`/`GEMINI_*`/`ANTHROPIC_*`/`LOCAL_*`) and
# `LLMManagerConfig.from_env()` for `LLM_PROVIDER`/`LLM_PROVIDER_PRIORITY`/
# `ENABLE_FALLBACK`/`ENABLE_STREAMING`/`MAX_RETRIES`/`TIMEOUT`.

#: which of the five providers is active by default - "openrouter",
#: "openai", "gemini", "anthropic", or "local". Never required to be
#: set (defaults to "openrouter", zero behavior change from before this
#: sprint) and never validated here - "only validate the active
#: provider" is `LLMManagerAdapter._do_start()`'s job, not this module's.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()

#: global default model override, tried before each provider's own
#: `{PREFIX}_MODEL` env var - usually left unset (per-provider model
#: selection is the normal path; see `luno/adapters/llm/config.py`).
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "").strip() or None

#: Reused as-is for TWO independent things: (1) the "gemini" chat LLM
#: provider above (`luno/adapters/llm/gemini_provider.py`), and (2) the
#: Gemini 2.0 Flash VISION backend below (`luno/vision_provider.py`,
#: replacing the old local MiniCPM-V/Ollama pipeline - see the VISION
#: section further down this file). Same Google Generative Language API,
#: same key works for both `generateContent` calls - reading it here
#: once and letting both features share it is the "existing Luno
#: configuration mechanism" the vision migration was asked to reuse
#: rather than inventing a second, parallel config path for the same
#: credential.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
#: base URL of a locally-hosted OpenAI-compatible server (LM Studio,
#: Ollama's `/v1` endpoint, vLLM, OpenWebUI, ...) - `LOCAL_MODEL` is
#: whatever model name that server has loaded.
LOCAL_API_BASE = os.getenv("LOCAL_API_BASE", "")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "")

# ─────────────────────────────────────────────
#  DEVICE CONFIG FILES & DEFAULT LIGHT
# ─────────────────────────────────────────────

RGB_LIGHT_ENTITY = os.getenv("RGB_LIGHT_ENTITY", "light.wled")
RGB_LIGHT_NAME = os.getenv("RGB_LIGHT_NAME", "RGB Strip")

# Folder tempat semua file konfigurasi (lights/switches/scripts/memory) disimpan,
# supaya tidak berantakan di root folder project. Dibuat otomatis kalau belum ada.
DATA_DIR = os.getenv("DATA_DIR", "config")
os.makedirs(DATA_DIR, exist_ok=True)

LIGHTS_CONFIG_FILE = os.getenv("LIGHTS_CONFIG_FILE", os.path.join(DATA_DIR, "lights.config.json"))
SWITCHES_CONFIG_FILE = os.getenv("SWITCHES_CONFIG_FILE", os.path.join(DATA_DIR, "switches.config.json"))
SCRIPTS_CONFIG_FILE = os.getenv("SCRIPTS_CONFIG_FILE", os.path.join(DATA_DIR, "scripts.config.json"))
# Implicit/environmental intent inference ("hawanya panas nih" -> propose
# turning the AC on) - see luno/environment_intent.py. Missing file =
# feature simply inactive, same convention as SWITCHES_CONFIG_FILE above.
ENV_TRIGGERS_CONFIG_FILE = os.getenv("ENV_TRIGGERS_CONFIG_FILE", os.path.join(DATA_DIR, "environment_triggers.json"))

# Habit-learning store ("pulang kerja sore -> biasanya nyalain AC + lampu
# meja") - see luno/proactive/habit_memory.py. Missing file = feature
# simply inactive/starts fresh, same convention as every file above.
HABIT_MEMORY_FILE = os.getenv("HABIT_MEMORY_FILE", os.path.join(DATA_DIR, "habit_memory.json"))

# Relationship Engine Foundation sprint (see luno/relationship_engine.py) -
# compact, DERIVED relationship state (familiarity/trust/closeness/
# interaction counters) - deliberately its own file, separate from
# LONG_TERM_MEMORY_FILE above (facts about the user) and HABIT_MEMORY_FILE
# above (learned device routines) - same "missing file = feature simply
# inactive/starts fresh" convention as every file above.
RELATIONSHIP_STATE_FILE = os.getenv("RELATIONSHIP_STATE_FILE", os.path.join(DATA_DIR, "relationship_state.json"))

# Persistent Adaptive Response Depth Preference sprint (see
# luno/response_depth_preference.py's `PersistedDepthPreference`/`DepthPreferenceStore`) -
# a tiny, bounded, cross-session BEHAVIORAL PREFERENCE ("Vinn tends to
# prefer shorter/more detailed replies"), deliberately its own file -
# NEVER folded into LONG_TERM_MEMORY_FILE/VERIFIED_FACTS_FILE/
# EPISODIC_MEMORY_FILE/RELATIONSHIP_STATE_FILE above, since this is not a
# memory/truth/relationship-trust signal of any kind, just a small,
# decaying nudge on top of the existing, unrelated Response Depth Policy.
# Missing file = feature simply starts neutral (bias=0), same convention
# as every file above.
RESPONSE_DEPTH_PREFERENCE_FILE = os.getenv(
    "RESPONSE_DEPTH_PREFERENCE_FILE", os.path.join(DATA_DIR, "response_depth_preference.json"),
)

# Shared Experience / Episodic Memory sprint (see luno/episodic_memory.py) -
# structured records of MEANINGFUL shared events (technical problems solved
# together, HA devices configured, project milestones, explicitly
# user-declared important moments) - deliberately its own file, separate
# from LONG_TERM_MEMORY_FILE above (plain facts about the user, no
# category/outcome/provenance), SESSION_SUMMARIES_FILE above (a whole-session
# LLM recap with no meaningfulness filter or dedup), and
# RELATIONSHIP_STATE_FILE above (a derived score, not a record of what
# actually happened) - same "missing file = feature simply inactive/starts
# fresh" convention as every file above.
EPISODIC_MEMORY_FILE = os.getenv("EPISODIC_MEMORY_FILE", os.path.join(DATA_DIR, "episodic_memory.json"))

# Memory Guard sprint (see luno/memory_guard.py) - facts about the world
# verified straight from a ToolResult ("the light IS on", not "the LLM
# said the light is on") - deliberately its own file, separate from every
# other *_FILE above (a verified DEVICE fact is neither a long-term fact
# about the USER, an episodic EVENT, a relationship score, nor a session
# recap). Same "missing file = feature simply starts fresh" convention as
# every file above. Added by the Verified Facts & Vision Memory Test
# Isolation sprint - `VerifiedFactStore`'s default was previously computed
# inline as `os.path.join(config.DATA_DIR, "verified_facts.json")`
# (identical value to this constant's own default), with no dedicated
# override - this constant changes nothing about production behavior,
# it only gives tests a safe, independent path to redirect.
VERIFIED_FACTS_FILE = os.getenv("VERIFIED_FACTS_FILE", os.path.join(DATA_DIR, "verified_facts.json"))

# Bounded growth cap (section 16/17 of that sprint's brief: "bounded memory
# growth", "simple append + retrieval + deduplication is preferable to
# premature memory compression" for THIS sprint) - oldest entries are
# dropped (FIFO) once this many are stored; no compression/consolidation.
EPISODIC_MEMORY_MAX_ENTRIES = int(os.getenv("EPISODIC_MEMORY_MAX_ENTRIES", "500"))

DEFAULT_FADE_TRANSITION = float(os.getenv("DEFAULT_FADE_TRANSITION", "5"))

# Batas maksimum kecerahan lampu (0-255). Semua perintah brightness/fade akan di-clamp ke nilai ini.
MAX_BRIGHTNESS = int(os.getenv("MAX_BRIGHTNESS", "255"))
MAX_BRIGHTNESS = max(1, min(255, MAX_BRIGHTNESS))

# ─────────────────────────────────────────────
#  TTS (GPT-SoVITS / F5-TTS)
# ─────────────────────────────────────────────

# TTS_ENGINE memilih backend TTS yang dipakai speak() di main.py:
#   "gptsovits" (default, tetap berjalan seperti sebelumnya)
#   "f5tts"     (server baru di f5tts_server/server.py — lihat
#                f5tts_server/SETUP_F5TTS.md untuk cara setup & menjalankannya
#                sebelum menyalakan opsi ini)
TTS_ENGINE = os.getenv("TTS_ENGINE", "gptsovits").strip().lower()

GPTSOVITS_HOST = os.getenv("GPTSOVITS_HOST", "http://127.0.0.1:9880")
F5TTS_HOST = os.getenv("F5TTS_HOST", "http://127.0.0.1:8880")

# REFERENCE_AUDIO/REFERENCE_TEXT dipakai KEDUA engine (GPT-SoVITS & F5-TTS) —
# ini adalah audio contoh suara + transkrip persisnya untuk voice cloning.
REFERENCE_AUDIO = os.getenv("REFERENCE_AUDIO", r"E:\Luno Evo\character_files\main_sample.wav")
REFERENCE_TEXT = os.getenv("REFERENCE_TEXT", "This is a sample voice")

# ─────────────────────────────────────────────
#  STT (Wake word + bahasa)
# ─────────────────────────────────────────────

WAKE_WORD = os.getenv("WAKE_WORD", "alexa").lower()

# Bahasa untuk speech-to-text, TERPISAH dari LUNO_LANGUAGE (yang cuma ngatur bahasa
# balasan GPT). Ini yang menentukan seberapa akurat suara kamu dikenali jadi teks.
# Contoh: "id-ID" untuk Indonesia, "en-US" untuk Inggris.
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-US")

# Index microphone (PyAudio device index) buat speech_recognition.Microphone().
# Kosong (default) = pakai default input device Windows apa adanya - sama seperti
# sebelum ini ada, zero behavior change kalau nggak di-set. Isi manual kalau health
# check "Microphone" gagal dengan error semacam "[Errno -9996] Invalid device info"
# (PortAudio nggak nemu default device yang valid) - jalankan
# `python list_microphones.py` di root project buat lihat semua device + index-nya,
# lalu isi angka index yang mau dipakai di sini.
_MIC_DEVICE_INDEX_RAW = os.getenv("MIC_DEVICE_INDEX", "").strip()
try:
    MIC_DEVICE_INDEX = int(_MIC_DEVICE_INDEX_RAW) if _MIC_DEVICE_INDEX_RAW else None
except ValueError:
    MIC_DEVICE_INDEX = None

# ─────────────────────────────────────────────
#  FASTER-WHISPER CONFIG (STT lokal)
# ─────────────────────────────────────────────

# Ukuran model: tiny/base/small/medium/large-v2/large-v3.
# "medium" jauh lebih akurat dari Google STT gratisan (apalagi untuk aksen/kata teknis),
# tapi load pertama kali agak lama karena harus download model (~1.5GB, sekali saja lalu di-cache).
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "medium")

# "cuda" kalau ada GPU NVIDIA + CUDA terpasang (jauh lebih cepat), kalau tidak pakai "cpu".
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

# compute_type ngatur presisi vs kecepatan/RAM. "int8" paling ringan & cocok untuk CPU.
# Kalau WHISPER_DEVICE=cuda, defaultnya otomatis pindah ke "float16" (lebih optimal di GPU).
WHISPER_COMPUTE_TYPE = os.getenv(
    "WHISPER_COMPUTE_TYPE",
    "float16" if WHISPER_DEVICE == "cuda" else "int8",
)

# Bahasa untuk Whisper pakai kode ISO 639-1 ("id", "en", dst) — BEDA format dari STT_LANGUAGE
# punya Google ("id-ID", "en-US"). Default: diambil otomatis dari STT_LANGUAGE (potong bagian
# sebelum tanda "-"). Bisa override manual lewat WHISPER_LANGUAGE=id di .env kalau perlu.
# Set WHISPER_LANGUAGE="" (kosong) supaya Whisper auto-detect bahasa dari audio (sedikit lebih lambat).
WHISPER_LANGUAGE = os.getenv(
    "WHISPER_LANGUAGE",
    STT_LANGUAGE.split("-")[0].lower() if STT_LANGUAGE else "",
)

# Preprocessing audio SEBELUM ditranskrip — membantu akurasi kalau ngomong agak jauh
# dari mic / ruangan berisik. Dua-duanya independen, bisa dimatiin sendiri-sendiri
# kalau ternyata malah bikin hasil transkrip lebih jelek buat setup mic kamu.
STT_NOISE_REDUCE = os.getenv("STT_NOISE_REDUCE", "true").strip().lower() == "true"
STT_NORMALIZE_GAIN = os.getenv("STT_NORMALIZE_GAIN", "true").strip().lower() == "true"

# ─────────────────────────────────────────────
#  MEMORY & BAHASA BALASAN
# ─────────────────────────────────────────────

# Jumlah giliran percakapan terakhir yang diingat Luno (1 giliran = 1 pesan user + 1 balasan).
# Ini memory JANGKA PENDEK — hilang total tiap kali Luno di-restart.
MEMORY_TURNS = int(os.getenv("MEMORY_TURNS", "6"))

# File penyimpanan memory JANGKA PANJANG — fakta/preferensi yang secara eksplisit
# diminta user untuk diingat (mis. "inget ya, aku alergi kacang"). Beda dari
# MEMORY_TURNS di atas, ini TETAP ADA walau Luno di-restart / komputer dimatikan.
LONG_TERM_MEMORY_FILE = os.getenv("LONG_TERM_MEMORY_FILE", os.path.join(DATA_DIR, "long_term_memory.json"))

# File penyimpanan RINGKASAN SESI OBROLAN — beda dari LONG_TERM_MEMORY_FILE (yang isinya
# fakta soal user), ini nyimpen TOPIK yang pernah diobrolin (mis. "kemarin bahas soal
# lubang hitam"), supaya Luno bisa di-recall soal obrolan lama, bukan cuma fakta soal kamu.
SESSION_SUMMARIES_FILE = os.getenv("SESSION_SUMMARIES_FILE", os.path.join(DATA_DIR, "session_summaries.json"))

# File kepribadian Luno — nama, gaya bicara, cara manggil user, dll. Edit file ini buat
# ganti kepribadian TANPA sentuh kode Python sama sekali.
PERSONA_FILE = os.getenv("PERSONA_FILE", os.path.join(DATA_DIR, "persona.json"))

# Emotion Engine (see luno/emotion_engine.py) - two small, additive knobs,
# same "env var with a sane default" convention as everything else in this
# file. Below this confidence, an estimated user emotion is treated as too
# uncertain to influence behavior at all (section 7/16 of the Emotion
# Engine sprint: "never force a confident classification when the evidence
# is weak"). Kept in [0, 1] defensively - a bad .env value should degrade
# to "never confident enough to act on" rather than crash anything.
_EMOTION_LOW_CONFIDENCE_THRESHOLD_RAW = os.getenv("EMOTION_LOW_CONFIDENCE_THRESHOLD", "0.5")
try:
    EMOTION_LOW_CONFIDENCE_THRESHOLD = max(0.0, min(1.0, float(_EMOTION_LOW_CONFIDENCE_THRESHOLD_RAW)))
except ValueError:
    EMOTION_LOW_CONFIDENCE_THRESHOLD = 0.5

# How long (seconds) a detected user emotion is allowed to keep coloring
# Luno's response policy before it decays back to neutral/unknown in the
# absence of fresh evidence (section 11: "emotion should not persist
# forever"). Default 900s (15 minutes) - long enough to survive normal
# turn-to-turn pauses, short enough that "user was annoyed once" can never
# quietly linger for hours.
try:
    EMOTION_DECAY_SECONDS = max(0.0, float(os.getenv("EMOTION_DECAY_SECONDS", "900")))
except ValueError:
    EMOTION_DECAY_SECONDS = 900.0

# File penyimpanan REMINDER/ALARM — "ingetin aku minum obat jam 8 malam", dll.
# Tersimpan lintas restart; reminder yang belum "fired" bakal di-reschedule ulang
# otomatis tiap kali Luno start.
REMINDERS_FILE = os.getenv("REMINDERS_FILE", os.path.join(DATA_DIR, "reminders.json"))

# File mapping nama aplikasi -> path executable, dipakai fitur "buka software" (mis.
# "buka chrome", "buka spotify"). SENGAJA jadi allowlist — GPT cuma bisa buka app yang
# TERDAFTAR di sini, nggak bisa asal jalanin path sembarangan dari luar.
APPS_CONFIG_FILE = os.getenv("APPS_CONFIG_FILE", os.path.join(DATA_DIR, "apps.json"))

# Bahasa balasan Luno:
# - "auto" (default): ikuti bahasa yang dipakai user tiap pesan (Indonesia in → Indonesia out, dst)
# - nilai lain (mis. "english", "indonesian", "japanese"): SELALU balas pakai bahasa itu,
#   berapa pun bahasa perintah yang dikasih user.
LUNO_LANGUAGE = os.getenv("LUNO_LANGUAGE", "auto").strip().lower()

# Model OpenAI yang dipakai buat obrolan/konfirmasi (bukan Whisper — itu diatur
# terpisah lewat WHISPER_MODEL_SIZE di atas). "gpt-4o" lebih nurut instruksi
# (termasuk soal bahasa) dibanding "gpt-4o-mini", tapi lebih mahal per pesan.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Nama parameter buat batas panjang balasan — beda provider/generasi model beda konvensi:
# - OpenAI model BARU (gpt-5.x dst): "max_completion_tokens" (default di sini)
# - OpenAI model LAMA (gpt-4o-mini, gpt-4o) DAN kebanyakan provider lain (DeepSeek,
#   OpenRouter, dst): "max_tokens"
# Kalau pindah provider/model dan kena error "unsupported parameter", ini yang perlu diganti.
MAX_TOKENS_PARAM = os.getenv("MAX_TOKENS_PARAM", "max_completion_tokens").strip()
if MAX_TOKENS_PARAM not in ("max_tokens", "max_completion_tokens"):
    print(f"[Config] ⚠ MAX_TOKENS_PARAM='{MAX_TOKENS_PARAM}' tidak dikenal, pakai 'max_completion_tokens'")
    MAX_TOKENS_PARAM = "max_completion_tokens"

# Batas panjang balasan obrolan bebas (bukan smart-home command), dalam token (~0.75 kata/token).
# Default 500 ≈ 350-400 kata — jauh lebih leluasa buat ngobrol dibanding default lama (150).
# Naikin lagi kalau mau balasan lebih panjang; turunin kalau kerasa kepanjangan pas dibacain TTS.
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "500"))

# ─────────────────────────────────────────────
#  AUDIO OUTPUT
# ─────────────────────────────────────────────

# "desktop" (speaker komputer) atau "cast" (Google Cast / Assistant speaker)
AUDIO_OUTPUT_MODE = os.getenv("AUDIO_OUTPUT_MODE", "desktop").lower()
CAST_ENTITY_ID = os.getenv("CAST_ENTITY_ID", "media_player.google_home")
AUDIO_SERVE_PORT = int(os.getenv("AUDIO_SERVE_PORT", "8990"))
AUDIO_SERVE_DIR = os.path.join(tempfile.gettempdir(), "luno_audio_cast")
os.makedirs(AUDIO_SERVE_DIR, exist_ok=True)

# Port WebSocket buat avatar 3D (avatar.html, Three.js+VRM) nyambung ke Luno.
# Avatar-nya yang connect KE Luno (bukan sebaliknya), jadi bisa buka/tutup tab
# browser kapan aja tanpa perlu restart Luno.
AVATAR_WS_PORT = int(os.getenv("AVATAR_WS_PORT", "8991"))

# Backend avatar yang dipakai:
# - "web": avatar.html, Three.js+VRM, bawaan.
# - "vnyan": software VNyan lewat OSC/VMC — VNyan sendiri yang urus
#   render+lipsync+idle pose+physics, Luno cuma ngirim sinyal ekspresi/status
#   (vnyan_bridge.py) + idle motion OPSIONAL (vnyan_idle.py, lihat
#   VNYAN_IDLE_MOTION di bawah).
# - "vnyan_engine": SAMA tujuan OSC/VMC-nya kayak "vnyan" (masih butuh setup
#   VNyan yang sama - "Receive VMC data" aktif), tapi idle motion-nya diganti
#   `vrm_idle_engine` (vnyan_engine_bridge.py) — animation engine full-body
#   procedural terpisah (breathing, gaze, blink, gesture acak, dst, SELALU
#   jalan, bukan opsional lewat VNYAN_IDLE_MOTION). Defaultnya
#   body_focus="chest_up" (animasi lengan/tangan/kaki dimatikan, avatar cuma
#   gerak dari dada ke atas) — lihat vnyan_engine_bridge.py buat cara ganti ke
#   full body lagi. Kalau mau balik ke idle motion lama, tinggal ganti nilai
#   ini balik ke "vnyan".
# - "none": matiin avatar TOTAL — nggak ada WebSocket server/OSC yang jalan
#   sama sekali, Luno murni jadi asisten teks/suara tanpa visual apa pun.
AVATAR_BACKEND = os.getenv("AVATAR_BACKEND", "web").strip().lower()

# Alamat & port penerima VMC di VNyan — HARUS SAMA PERSIS dengan yang di-set di
# VNyan (Settings -> OSC/VMC -> aktifkan "Receive VMC data", catat port-nya).
# Default 39539 = port standar protokol VMC buat kirim ke aplikasi penerima ("Marionette").
VNYAN_OSC_HOST = os.getenv("VNYAN_OSC_HOST", "127.0.0.1")
VNYAN_OSC_PORT = int(os.getenv("VNYAN_OSC_PORT", "39539"))

# Opsional: nama/index device audio TAMBAHAN buat diputer BARENGAN speaker biasa —
# dipakai kalau mau route audio TTS ke virtual audio cable (mis. VB-Audio Cable)
# biar lipsync bawaan VNyan bisa "denger" suara Luno. Kosongin kalau nggak dipakai
# (mis. kalau avatar_backend="web", audio browser dikirim lewat WebSocket, bukan ini).
SECONDARY_AUDIO_DEVICE = os.getenv("SECONDARY_AUDIO_DEVICE", "").strip()

# Aktifkan loop idle motion parametrik (kirim rotasi kepala/spine/hips terus-menerus
# ke VNyan lewat VMC Bone/Pos) — port dari logic idle animation avatar.html. CUMA
# ngaruh kalau AVATAR_BACKEND=vnyan. Default MATI karena bisa "rebutan" sama idle
# motion bawaan VNyan kalau belum kamu matiin buat bone yang sama — nyalain manual
# setelah siap. Termasuk gesture "lagi ngejelasin" pas ngomong (SPEAKING_GESTURES
# di vnyan_idle.py) — SEMUA lewat VMC bone rotation, TANPA dependency ke Trigger
# API/Node Graph sama sekali (sempat dicoba, tapi ribet setup-nya dan rawan putus).
VNYAN_IDLE_MOTION = os.getenv("VNYAN_IDLE_MOTION", "false").strip().lower() == "true"

# Cuma dipakai mode "cast": margin tambahan (detik) di atas estimasi durasi audio,
# buat jaga-jaga latensi jaringan/startup speaker Cast. Luno NUNGGU (durasi audio +
# margin ini) sebelum lanjut ke langkah berikutnya (mis. sebelum mic otomatis aktif
# lagi buat follow-up) — biar mic nggak nangkep suara Luno sendiri dari speaker.
CAST_PLAYBACK_MARGIN = float(os.getenv("CAST_PLAYBACK_MARGIN", "1.5"))

# Jeda singkat SETELAH audio selesai diputer, di mode APA PUN (desktop ATAU cast),
# sebelum kontrol balik ke pemanggil — buffer tambahan biar nggak kepancing gema/
# delay ruangan pas mic aktif lagi. Naikin kalau masih suka nangkep suara sendiri;
# turunin kalau kerasa jeda antar-giliran obrolannya kelamaan.
POST_SPEECH_COOLDOWN = float(os.getenv("POST_SPEECH_COOLDOWN", "0.3"))

# TTS Chunking/Streaming sprint — batas panjang (karakter) SATU chunk suara yang
# dikirim ke Fish Audio (lihat luno/response_output.py's build_dual_response()
# `max_chunk_chars` param, dipakai dari BehaviorTreeModule._speak() di
# main_runtime_demo.py). Ini CUMA batas atas/pengaman (dipakai buat mecah kalimat
# yang kepanjangan di titik koma/spasi) — bukan target ukuran; granularitas
# defaultnya tetap 1 kalimat = 1 chunk (baca alasannya di response_output.py) biar
# TTS bisa mulai ngomong secepat mungkin dari chunk pertama. Naikin kalau chunk
# kerasa kependekan/kebanyakan jeda; turunin kalau delay-mulai-ngomong kerasa lama
# untuk kalimat-kalimat yang panjang.
VOICE_CHUNK_MAX_CHARS = int(os.getenv("VOICE_CHUNK_MAX_CHARS", "220"))

# LLM Streaming -> Real-Time Speech Pipeline sprint — awalnya default off
# (opt-in), lihat riwayat di bawah. Kalau True, BehaviorTreeModule mulai
# ngirim SpeechChunk ke Fish Audio SEBELUM seluruh jawaban LLM selesai (lewat
# StreamingSpeechCoordinator di luno/incremental_speech.py, konsumsi event
# llm_chunk yang udah ada/nyata — bukan LLM abstraction baru).
#
# Production-Safe LLM -> TTS Streaming Activation sprint (Phase 10 decision)
# — checked-in default STAYS False. Not because streaming itself is unsafe
# anymore: two real production bugs that made it unsafe were found AND
# fixed this sprint, with empirical proof, not assumptions:
#   1. Streaming lama sama sekali nggak lewat build_dual_response() -
#      artinya SEMUA balasan yang di-stream diomongin utuh, ngelewatin
#      response-depth policy (SHORT/NORMAL/DETAILED) sama sekali. Diperbaiki:
#      cuma kalimat pertama yang boleh mulai duluan (selalu aman - build_
#      dual_response() SELALU nyimpen kalimat index 0), sisanya nunggu
#      build_dual_response() jalan di teks lengkap - otoritas seleksi yang
#      SAMA yang dipakai jalur non-streaming, nggak ada selector kedua.
#   2. SessionManagerModule cuma pindah THINKING->SPEAKING pas "speak_request"
#      - yang TIDAK PERNAH dipublish buat turn yang di-stream penuh (lihat
#      BehaviorTreeModule._speak()). Ini bikin state macet permanen di
#      THINKING (THINKING nggak punya timeout) abis balasan streaming
#      pertama - setiap ucapan berikutnya kebuang diem-diem selamanya.
#      Diperbaiki: SessionManagerModule sekarang juga dengerin
#      "speech_playback_started" (dipublish jalur legacy MAUPUN streaming),
#      lihat luno/wake_session/manager.py's _handle_playback_started().
#   3. BehaviorTreeModule._generate_reply() dulu cuma bangun dari
#      assistant_response/llm_error - kalau barge-in masuk PAS LLM masih
#      generating, thread ini nunggu sampe llm_timeout_s (default 45s)
#      sebelum ucapan berikutnya bisa diproses. Diperbaiki: sekarang juga
#      bangun dari llm_cancelled (lihat _on_cancel() di main_runtime_demo.py).
# Verified via tests/test_llm_tts_streaming_production.py (39 scenarios,
# including real E2E through RuntimeDemoConsole) - the STREAMING PATH ITSELF
# is production-safe. A prior sprint kept the CHECKED-IN default at False for
# a narrower "rollout blast radius" reason only: flipping it changed ambient
# behavior for tests/integrations that implicitly assumed the legacy
# "speak_request marks a turn as spoken" signal as the default (e.g.
# tests/test_adaptive_response_depth.py::test_R, tests/test_barge_in_console.py
# ::test_uninterrupted_turn_produces_exactly_one_history_line - not because
# streaming misbehaves, but because those tests listened for "speak_request"
# specifically and a fully streamed turn correctly never publishes it).
#
# Voice Output Naturalness & First-Audio Latency sprint: measured through the
# real production path (RuntimeDemoConsole, real Event Bus/threading) that
# the DEFAULT (streaming disabled) path still waits for the ENTIRE LLM
# response before any audio is dispatched - median first-audio latency
# ~2.06s vs ~0.61s with streaming enabled (~70% degradation), for zero
# architectural reason: the already-built, already-safe streamed path was
# simply left off. Rather than build a second/duplicate low-latency
# mechanism for the default path (explicitly forbidden by this sprint's own
# brief), this sprint flips the default to True and fixes the two identified
# legacy-path-assumption tests above (mode-agnostic now - they check for
# EITHER "speak_request" OR "speak_stream_chunk", whichever dispatch path
# actually fired, never assuming which one - see each test's own updated
# docstring) plus ran the full regression suite to catch any other such
# call site. Rollback (if ever needed) is the same env var set explicitly to
# "false" in .env, no code change required either way. See
# docs/change_impact/voice_output_naturalness_and_latency.md and
# docs/change_impact/llm_tts_streaming_activation.md for full detail.
ENABLE_LLM_TTS_STREAMING = os.getenv("ENABLE_LLM_TTS_STREAMING", "true").strip().lower() == "true"

# Batas jumlah SpeechChunk yang boleh "pending" (udah di-publish ke Fish Audio
# tapi belum kelar diputer) per request_id saat streaming — backpressure biar
# LLM yang ngetik jauh lebih cepat dari TTS ngomong nggak numpuk job audio
# tanpa batas. Kalau kepenuhan, StreamingSpeechCoordinator NAHAN chunk yang
# udah jadi di buffer teks lokal (bukan bikin job audio baru) sampai ada slot
# kosong lagi — lihat luno/incremental_speech.py.
LLM_TTS_STREAM_MAX_PENDING_CHUNKS = int(os.getenv("LLM_TTS_STREAM_MAX_PENDING_CHUNKS", "4"))

# ─────────────────────────────────────────────
#  VISION (kamera + YOLO lokal + Gemini 2.0 Flash — lihat luno/vision.py)
# ─────────────────────────────────────────────
#
# Aug 2026 migration: visual question-answering ("ada apa di kamera",
# "aku pegang apa") used to go through a LOCAL MiniCPM-V model served by
# Ollama - that's been replaced by Google's Gemini 2.0 Flash Vision API
# (see GEMINI_VISION_MODEL below, and `luno/vision_provider.py`). Two
# reasons: (1) MiniCPM-V sat resident in RAM/VRAM (~5.5GB) the whole time
# Luno ran, whether or not vision was ever actually used that session;
# Gemini runs remotely, so that memory is simply gone. (2) it dropped the
# hard `ollama pull minicpm-v` + local GPU requirement for this feature
# entirely. YOLO is UNCHANGED - still the local, always-on, lightweight
# detector for presence/object hints (see below); only the "actually
# understand and answer a question about the image" step moved off-box.
# The OLLAMA_* settings further down are kept (not deleted) in case
# something else on your machine still points at that Ollama instance,
# but nothing in the vision pipeline reads them anymore.

# Master switch — kalau False, vision intent classifier
# (luno/vision_intent.py) nggak pernah nyalain kamera/Gemini sama sekali
# (nggak ada akses kamera diam-diam). Default MATI - nyalain manual
# setelah GEMINI_API_KEY di-set (lihat vision.py's docstring buat
# langkah setup lengkap).
CAMERA_VISION_ENABLED = os.getenv("CAMERA_VISION_ENABLED", "false").strip().lower() == "true"

# Index device webcam buat OpenCV (cv2.VideoCapture) — 0 biasanya kamera
# default/pertama. Ganti kalau kamu punya lebih dari 1 kamera dan yang mau
# dipakai bukan yang pertama.
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))

# Sprint 69 (Camera Device / OpenCV Stability Fix) — how long
# `luno.vision._open_capture_bounded()` waits for ONE `cv2.VideoCapture(...)`
# backend candidate to open before giving up on it and trying the next
# one (or reporting failure) — `cv2.VideoCapture`'s constructor has no
# built-in timeout of its own, so this is the only thing that bounds it.
# Kept well under 5s so that even the worst realistic case (2 Windows
# backend candidates — CAP_DSHOW then CAP_MSMF, see
# `luno.vision._local_backend_candidates()`) still finishes within the
# "<=5s total for the failure case" target the Sprint 69 brief set,
# instead of stacking two full 5s waits back to back.
CAMERA_OPEN_TIMEOUT_S = float(os.getenv("CAMERA_OPEN_TIMEOUT_S", "2.5"))

# Sprint 69 — after a camera open attempt fails (UNAVAILABLE/BUSY/
# BACKEND_ERROR — see `luno.vision.CameraState`), how long
# `luno.vision._capture_frame()` waits before trying to reopen the
# camera again, instead of retrying on every single call. Without this,
# a background poll loop (e.g. `RealVisionSource._tracked_cycle_loop()`,
# which by default calls `capture_frame()` roughly twice a second — see
# `VISION_FPS` below) would re-attempt a known-broken camera open on
# every tick, potentially re-triggering a slow backend timeout every
# single time — exactly the repeated-stall pattern the Sprint 69 bug
# report's log showed.
CAMERA_REOPEN_COOLDOWN_S = float(os.getenv("CAMERA_REOPEN_COOLDOWN_S", "10.0"))

# YOLO: deteksi objek/orang CEPAT & MURAH (CPU cukup, nggak butuh GPU) —
# dipakai sebagai hint konteks buat Gemini dan buat presence-watch
# opsional di background (lihat vision.start_watch()). YOLO SENDIRI nggak
# bisa jawab pertanyaan bebas soal isi gambar, cuma tau label kelas tetap.
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolo11n.pt")  # auto-download pertama kali dipanggil
YOLO_CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.4"))
# Sprint 8: keypoint (pose) variant - SAME Ultralytics auto-download
# convention as YOLO_MODEL_PATH above, only ever loaded lazily and only
# when a person is actually in frame (see vision.py's
# detect_people_with_pose()) - used purely for the human posture/facing/
# hand-raised ESTIMATES in the tracked-detection cycle, never for the
# existing ask_vision()/detect_objects() hint path.
YOLO_POSE_MODEL_PATH = os.getenv("YOLO_POSE_MODEL_PATH", "yolov8n-pose.pt")

# Kalau True, YOLO jalan TERUS di background (interval di bawah) buat nge-
# track kehadiran orang di depan kamera, TERPISAH dari tool 'lihat_kamera'
# (yang cuma dipanggil on-demand). Default MATI - fitur ini fondasi buat
# nanti (mis. auto-listen pas ada orang di depan kamera), belum dipakai
# otomatis oleh main.py di luar nyalain/matiin thread-nya.
CAMERA_WATCH_ENABLED = os.getenv("CAMERA_WATCH_ENABLED", "false").strip().lower() == "true"
CAMERA_WATCH_INTERVAL_S = float(os.getenv("CAMERA_WATCH_INTERVAL_S", "1.0"))

# Kalau True, buka JENDELA live-preview kamera (+ kotak deteksi YOLO di atas
# videonya) pas Luno start — murni buat kamu bisa liat sendiri apa yang
# kamera tangkep sambil ngobrol sama Luno, TERPISAH total dari tool
# 'lihat_kamera'/CAMERA_WATCH_ENABLED di atas (independen, boleh nyala salah
# satu/semua/nggak ada sama sekali). Default MATI karena nge-pop up jendela
# GUI - nggak semua orang mau itu selalu muncul tiap Luno start.
CAMERA_MONITOR_WINDOW_ENABLED = os.getenv("CAMERA_MONITOR_WINDOW_ENABLED", "false").strip().lower() == "true"

# DEPRECATED / no longer read by the vision pipeline (kept only so an
# existing .env with these set doesn't error, and in case something else
# on your machine still points at this Ollama instance for something
# unrelated to vision). `luno/vision.py` used these to call MiniCPM-V
# through a local Ollama server - that call is gone, replaced by
# GeminiVisionProvider below.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "minicpm-v")
OLLAMA_VISION_TIMEOUT_S = float(os.getenv("OLLAMA_VISION_TIMEOUT_S", "120"))

# DEPRECATED - the continuous "ambient scene watch" loop this used to gate
# (1 MiniCPM-V call/second, cached via vision.last_vision_description())
# has been removed outright, not just repointed at Gemini: sending camera
# frames to a remote API on a fixed interval regardless of whether anyone
# asked is exactly the always-on-VLM-polling behavior this migration was
# explicitly asked to NOT reintroduce (Gemini is on-demand only - see
# vision.ask_vision()). `vision.start_vision_watch()` still exists as a
# safe no-op (nothing calls into it anymore) so callers that reference it
# don't need to change. Kept here, unused, for the same reason as
# OLLAMA_VISION_TIMEOUT_S above.
CAMERA_VISION_WATCH_ENABLED = os.getenv("CAMERA_VISION_WATCH_ENABLED", "false").strip().lower() == "true"
CAMERA_VISION_WATCH_INTERVAL_S = float(os.getenv("CAMERA_VISION_WATCH_INTERVAL_S", "1.0"))
CAMERA_VISION_WATCH_TIMEOUT_S = float(os.getenv("CAMERA_VISION_WATCH_TIMEOUT_S", "15"))

# Which VisionProvider (see luno/vision_provider.py) actually answers
# "what's in this image" questions - "gemini" or "openai". Read by
# luno.vision._get_vision_provider(). Default "openai": originally
# Gemini, switched after discovering GEMINI_API_KEY is ALSO used by this
# project's chat-LLM fallback priority (LLM_PROVIDER_PRIORITY) - the two
# features silently competed for the same free-tier per-minute quota,
# so a vision question could hit "429 rate limited" purely because
# ordinary conversation had already been using Gemini for chat. Giving
# vision its own provider (a different vendor entirely, not just a
# different key) removes that coupling outright. Switch back to
# "gemini" any time - both implementations stay maintained, this is a
# config change only.
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "openai").strip().lower()

# Gemini 2.0 Flash Vision - one of the two on-demand "actually answer a
# question about the camera image" backends (see luno/vision_provider.py's
# GeminiVisionProvider, called from vision.ask_vision() when
# VISION_PROVIDER=gemini). Auth reuses GEMINI_API_KEY (see its own
# comment above) - NEVER logged/printed anywhere, only ever placed in
# the request's `x-goog-api-key` header.
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.0-flash")
# A single on-demand call to a hosted API - no multi-gigabyte cold-start
# load like the old local MiniCPM-V path had, so this can stay far
# shorter than the old OLLAMA_VISION_TIMEOUT_S (120s) was.
GEMINI_VISION_TIMEOUT_S = float(os.getenv("GEMINI_VISION_TIMEOUT_S", "20"))

# OpenAI Vision - the other on-demand vision backend (see
# luno/vision_provider.py's OpenAIVisionProvider, called when
# VISION_PROVIDER=openai, the current default - see that setting's own
# comment above). Auth reuses OPENAI_API_KEY (already defined near the
# top of this file for the chat-LLM OpenAI provider) - same "don't
# invent a second config mechanism for the same credential" reasoning
# GEMINI_API_KEY's own comment documents, and same "never logged"
# discipline (only ever placed in the request's `Authorization: Bearer`
# header). "gpt-4o-mini" default - vision-capable, and the cost-
# effective tier (mirrors picking Gemini's "flash", not "pro").
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
OPENAI_VISION_TIMEOUT_S = float(os.getenv("OPENAI_VISION_TIMEOUT_S", "20"))

# ─────────────────────────────────────────────
#  Screen vision - "Luno, screenshot terus liat kenapa error" - lihat
#  luno/screen_vision.py. Independent on/off switch dari
#  CAMERA_VISION_ENABLED di atas (dua capture path yang beda - layar
#  desktop lewat Pillow ImageGrab vs webcam/Tapo lewat OpenCV - tapi
#  SAMA-SAMA lewat vision provider yang sama, VISION_PROVIDER/
#  GEMINI_VISION_*/OPENAI_VISION_* di atas dipakai ulang, bukan
#  duplikat config buat credential yang sama).
# ─────────────────────────────────────────────
SCREEN_VISION_ENABLED = os.getenv("SCREEN_VISION_ENABLED", "false").strip().lower() == "true"
# Downscale sebelum upload - alasan/mekanisme sama kayak
# BROWSER_SCREENSHOT_MAX_EDGE (luno/browser/config.py) - screenshot 4K
# penuh nambah token/latency tanpa nambah kemampuan vision provider baca
# dialog error.
SCREEN_VISION_MAX_EDGE = int(os.getenv("SCREEN_VISION_MAX_EDGE", "1280"))

# How many seconds YOLO must go without detecting a person before the
# debounced room-level presence signal (VisionAdapter's CameraPersonLeft -
# see luno/adapters/vision.py) flips back to ABSENT. Presence flips to
# PRESENT (CameraPersonEntered) immediately on the FIRST detection - only
# the "gone" direction is debounced, so a single missed/occluded frame
# doesn't flip-flop the room in and out of "someone's here" every second.
# Same idea as TRACKING_TIMEOUT below, kept as its own knob since this
# gates a simpler, cheaper, always-on signal than the full Sprint 8
# tracked-object pipeline TRACKING_TIMEOUT belongs to.
CAMERA_PERSON_ABSENCE_TIMEOUT_S = float(os.getenv("CAMERA_PERSON_ABSENCE_TIMEOUT_S", "5.0"))

# ─────────────────────────────────────────────
#  Sprint 8 - Real Vision tracking pipeline (luno/vision_tracking.py +
#  luno/vision_human_state.py, wired through luno/adapters/real_vision.py)
# ─────────────────────────────────────────────
#
# Separate from the YOLO/MiniCPM-V knobs above on purpose: those already
# existed (presence-watch + ambient scene description); these are new,
# additive config specifically for the tracked-object + human-pose-
# estimation cycle real_vision.py runs at its own configurable rate. None
# of this changes any existing behavior unless VISION_BACKEND=real.

# IP camera URL (e.g. "rtsp://192.168.1.20:554/stream" or an MJPEG HTTP
# URL) - takes priority over CAMERA_INDEX when set (cv2.VideoCapture
# accepts a URL string exactly like it accepts an int device index).
# Empty (default) = use CAMERA_INDEX, i.e. a USB/integrated webcam.
# NEVER hardcode a camera source anywhere else - always read through
# `luno.vision.camera_source()` (see that module), which is this one
# config decision point.
CAMERA_URL = os.getenv("CAMERA_URL", "").strip()

# ─────────────────────────────────────────────
#  Tapo pan/tilt IP camera (e.g. TP-Link Tapo C212) - vision source AND
#  pan/tilt control, via the `pytapo` library (pip install pytapo).
# ─────────────────────────────────────────────
#
# Credentials are the camera's own "Camera Account" (Tapo app > Settings >
# Advanced settings > Camera account) - NOT your TP-Link cloud login.
# Used for two independent things:
#   1. Auto-deriving CAMERA_URL above (only if you didn't already set one
#      explicitly) so Vision's object/person detection reads frames from
#      this camera instead of a local webcam - no need to repeat the same
#      host/credentials in two separate settings.
#   2. The pan/tilt control tool (see
#      luno/tool_manager/builtin/real_camera_ptz.py, CAMERA_PTZ_BACKEND=real)
#      - "geser kamera ke kiri/kanan/atas/bawah" / "pan/tilt the camera left/
#      right/up/down".
# Leave TAPO_HOST empty (default) to disable both - existing CAMERA_INDEX/
# CAMERA_URL-based webcam behavior is completely unaffected either way.
TAPO_HOST = os.getenv("TAPO_HOST", "").strip()
TAPO_USERNAME = os.getenv("TAPO_USERNAME", "").strip()
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "").strip()
# "stream1" = main/HD stream, "stream2" = sub/SD stream (less bandwidth,
# useful over Wi-Fi/remote access - see TP-Link's own RTSP documentation).
TAPO_STREAM = os.getenv("TAPO_STREAM", "stream1").strip() or "stream1"

if not CAMERA_URL and TAPO_HOST and TAPO_USERNAME and TAPO_PASSWORD:
    CAMERA_URL = f"rtsp://{TAPO_USERNAME}:{TAPO_PASSWORD}@{TAPO_HOST}:554/{TAPO_STREAM}"

# Pan/tilt step size in degrees per "geser kamera ke kiri/kanan/atas/bawah"
# command (see the parser patterns in luno/planner/parser.py).
# HONEST LIMITATION: pytapo/Tapo's own API has no way to read back the
# camera's actual current angle after a move (no getCurrentPosition()-
# equivalent method exists in the library) - so a pan/tilt command can
# only ever honestly report "command sent to the camera", never a
# VERIFIED new position the way Home Assistant's on/off commands can
# (see real_camera_ptz.py's own docstring).
TAPO_PAN_STEP_DEGREES = float(os.getenv("TAPO_PAN_STEP_DEGREES", "15"))
TAPO_TILT_STEP_DEGREES = float(os.getenv("TAPO_TILT_STEP_DEGREES", "15"))
# Some cameras/mounts wire their pan/tilt motor axes inverted - flip
# these to "true" in .env if "left" visibly moves the camera right (or
# "up" moves it down), no code change needed.
TAPO_INVERT_PAN = os.getenv("TAPO_INVERT_PAN", "false").strip().lower() == "true"
TAPO_INVERT_TILT = os.getenv("TAPO_INVERT_TILT", "false").strip().lower() == "true"

# How often the tracked-object + human-pose cycle runs, in frames per
# second - a cleaner, more directly-named knob than CAMERA_WATCH_INTERVAL_S
# above for this specific loop (that one stays interval-based and
# unchanged, for backward compatibility with the presence-only watch).
VISION_FPS = float(os.getenv("VISION_FPS", "2.0"))

# Minimum detector confidence (0-1) for the TRACKING cycle specifically -
# deliberately its own var rather than reusing YOLO_CONFIDENCE (which
# still governs the older detect_objects()/ask_vision() hint path
# unchanged) so the two can be tuned independently.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.4"))

# Hard cap on how many distinct objects ObjectTracker keeps alive at once
# (see vision_tracking.ObjectTracker's own docstring for the eviction
# policy once this is hit).
MAX_OBJECTS = int(os.getenv("MAX_OBJECTS", "20"))

# Seconds an object/person can go undetected before its track is dropped
# (see ObjectTracker.tracking_timeout_s).
TRACKING_TIMEOUT = float(os.getenv("TRACKING_TIMEOUT", "5.0"))

# Device preference for YOLO inference: "cuda"/GPU when True (falls back
# to CPU automatically inside real_vision.py if no CUDA device is
# actually available - never a hard crash), plain CPU when False.
USE_GPU = os.getenv("USE_GPU", "false").strip().lower() == "true"

# ─────────────────────────────────────────────
#  P0.8.6 - Human-presence AUTOMATION confidence/debounce (separate from
#  CONFIDENCE_THRESHOLD above, which stays the DETECTION-visibility
#  threshold - the debug viewer / detect_objects_tracked()'s own raw
#  output is unaffected by anything below).
# ─────────────────────────────────────────────
#
# Root cause of the P0.8.6 false-positive bug: CONFIDENCE_THRESHOLD (0.4)
# is a DETECTION-visibility threshold ("is this worth returning as a
# RawDetection at all"), not an AUTOMATION-safety threshold ("is this
# confident enough to physically turn a real device on"). A single
# tracked-cycle frame at, e.g., person=0.506 (a real, observed false
# positive - see docs/change_impact/camera_automation_p0_8_6.md Section 2)
# already clears 0.4 and, before this fix, could instantly flip Vision's
# room-level presence signal and fire a physical home_assistant.turn_on.
#
# HUMAN_DETECTION_CONFIDENCE is the minimum single-frame confidence a
# tracked "person" detection must reach to count as an automation
# CANDIDATE at all (see VisionAdapter.on_vision_cycle()'s confirmation
# state machine in luno/adapters/vision.py). Chosen empirically from the
# real confidence samples the P0.8.6 brief provided: 15 of 25 real
# samples (0.462-0.599, including the confirmed 0.506 false positive)
# cluster BELOW 0.60; the remaining 10 (0.605-0.830) cluster AT OR ABOVE
# it - 0.60 sits almost exactly at that natural gap in the real data,
# and matches the brief's own suggested value. Not a magic number - an
# evidence-derived cut between the two observed confidence populations.
HUMAN_DETECTION_CONFIDENCE = float(os.getenv("HUMAN_DETECTION_CONFIDENCE", "0.60"))

# How many CONSECUTIVE tracked cycles (VISION_FPS apart, default 0.5s
# each) must each independently contain a qualifying (>= HUMAN_DETECTION_
# CONFIDENCE) person detection before Vision's automation-facing
# "human_confirmed" signal flips True. At the default VISION_FPS=2.0
# (0.5s/cycle), 3 cycles is ~1.5s of SUSTAINED, repeated high-confidence
# detection - long enough that a single false-positive frame (the
# reported bug) or a person merely passing through the edge of frame for
# one cycle cannot alone trigger a physical action, short enough that a
# real, standing person is still confirmed almost immediately. Matches
# the brief's own suggested "3 consecutive positive cycles" - kept as
# its own env-overridable knob rather than hardcoded, same convention
# every other debounce/timeout constant in this file already follows.
HUMAN_DETECTION_CONFIRM_CYCLES = max(1, int(os.getenv("HUMAN_DETECTION_CONFIRM_CYCLES", "3")))

# ─────────────────────────────────────────────
#  VALIDASI
# ─────────────────────────────────────────────


def validate():
    """Cek konfigurasi wajib. Dipanggil sekali di main.py saat startup.

    Nerima `OPENAI_API_KEY` ATAU salah satu dari kelima provider LLM
    Manager (`OPENROUTER_API_KEY`/`OPENAI_API_KEY` lagi via prefix
    provider/`GEMINI_API_KEY`/`ANTHROPIC_API_KEY`/`LOCAL_API_BASE`) -
    project udah migrasi ke `LLMManagerAdapter` multi-provider (lihat
    `luno/bootstrap/adapters.py`/`luno/adapters/llm_manager.py`, yang
    baca env var-nya masing-masing LANGSUNG dan gak pernah sentuh
    variabel `OPENAI_API_KEY` di modul ini sama sekali). Tanpa cek yang
    dilonggarkan ini, siapa pun yang cuma set (misalnya) `GEMINI_API_KEY`
    (dan gak pernah butuh `OPENAI_API_KEY`/`OPENROUTER_API_KEY` lagi)
    bakal ke-block di sini setiap kali sesuatu meng-import
    `legacy_main.py` sebagai modul (mis. `luno/adapters/real_whisper.py`
    waktu `WHISPER_BACKEND=real`), walau Runtime produksi sendiri gak
    pernah butuh `OPENAI_API_KEY` ataupun provider spesifik apa pun di
    modul ini."""
    has_any_llm_credential = bool(
        OPENAI_API_KEY or os.getenv("OPENROUTER_API_KEY") or GEMINI_API_KEY
        or ANTHROPIC_API_KEY or LOCAL_API_BASE
    )
    if not has_any_llm_credential or not HA_TOKEN:
        print(
            "[ERROR] Set salah satu dari OPENAI_API_KEY/OPENROUTER_API_KEY/GEMINI_API_KEY/"
            "ANTHROPIC_API_KEY/LOCAL_API_BASE, dan HA_TOKEN, di .env"
        )
        raise SystemExit(1)
