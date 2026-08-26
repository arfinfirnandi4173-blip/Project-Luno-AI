# Luno Evo — Project Context

Luno adalah asisten AI suara berbasis desktop (voice companion / VTuber assistant) yang bisa diajak ngobrol, mengontrol smart home (Home Assistant), melihat lewat kamera (vision), mengingat percakapan jangka panjang, mengatur reminder, membuka aplikasi, mencari info di web, dan tampil sebagai avatar 3D (VNyan atau web VRM) yang lipsync mengikuti suaranya sendiri (TTS dengan voice cloning, via GPT-SoVITS atau F5-TTS).

> **KOREKSI (Regression & Architecture Guard sprint):** paragraf di bawah ini
> tadinya bilang `main.py` adalah skrip lama monolitik dan `main_runtime_demo.py`
> "belum menggantikan `main.py` sepenuhnya" — itu sudah TIDAK akurat lagi.
> Sejak migrasi, `main.py` (root) sendiri sudah jadi launcher tipis yang
> merakit `luno/bootstrap/*` (lihat docstring `main.py` sendiri: *"the ONE
> official production entry point"*). Skrip lama prosedural yang dulu ada di
> `main.py` sudah dipindah utuh ke `legacy_main.py` (lihat docstring
> `main.py` sendiri) — file itu sendiri sedang tidak ada di checkout ini,
> dicatat sebagai known issue di `ARCHITECTURE_GUARD.md` §15, bukan dihapus/
> diganti sengaja. `main_runtime_demo.py` juga bukan cuma console dev —
> file itu satu-satunya tempat 4 class Module Bridge (`PlannerBridgeModule`,
> `ToolManagerBridgeModule`, `BehaviorTreeModule`, `VisionMemoryModule`)
> diimplementasikan, dan `luno/bootstrap/modules.py` meng-import class-class
> itu dari sana untuk DIPAKAI OLEH `main.py` produksi juga (lewat
> `register_all_modules()`), bukan cuma dipakai `main_runtime_demo.py`
> sendiri. Detail lengkap: `ARCHITECTURE_GUARD.md` §2.

Repo ini berisi **dua implementasi berdampingan**:

1. **`main.py`** (root) — **entry point produksi resmi saat ini**: launcher tipis yang merakit `luno/bootstrap/*` (adapters asli, module registration, health check, dashboard). Event-driven, bukan monolitik.
2. **`legacy_main.py`** — skrip produksi LAMA (prosedural/monolitik, satu file besar, terhubung langsung ke hardware asli, bukan event-driven) — dipertahankan apa adanya sebagai referensi historis, sudah tidak lagi yang dijalankan `python main.py`. Tidak ada di checkout ini saat ini (lihat `ARCHITECTURE_GUARD.md` §15).
3. **`main_runtime_demo.py`** — console developer (device mock secara default) SEKALIGUS satu-satunya tempat 4 class Module Bridge event-driven di atas diimplementasikan, dipakai bersama oleh `main.py` (produksi, adapter asli) maupun dirinya sendiri (device mock) lewat `luno/bootstrap/modules.py`.

Kedua dunia ini **sengaja dipisah**: `legacy_main.py` mengimpor modul-modul lama langsung dari `luno/` (`luno/config.py`, `luno/ha_client.py`, `luno/vision.py`, dst — file-file lepas, bukan bagian dari package event-driven), sedangkan `luno/core`, `luno/adapters`, `luno/behavior_tree`, `luno/planner`, `luno/tool_manager`, `luno/vision_memory`, `luno/wake_session`, `luno/barge_in`, `luno/text_normalizer` adalah package-package baru yang saling terhubung lewat Event Bus, bukan pemanggilan fungsi langsung — dan `main.py` produksi berjalan sepenuhnya di dunia kedua ini sekarang.

---

## 1. Arsitektur Event-Driven (`luno/core` + turunannya)

### Prinsip inti

- **Tidak ada modul yang memanggil modul lain secara langsung.** Semua komunikasi lewat `Event` yang dipublish ke `EventBus`, lalu di-dispatch ke module yang subscribe pola event tersebut (diatur oleh `Coordinator`, bukan hardcoded di kode).
- **`ModuleManager`** yang membuat dan mengelola lifecycle setiap module (start/stop/restart), termasuk dependency order antar module. Module tidak pernah saling membuat instance satu sama lain.
- **Fault isolation**: satu module gagal start tidak menggagalkan `Runtime.start()` — tercermin di `status()`/`health()`.
- **`Runtime`** adalah entry point tunggal yang merakit `EventBus`, `Dispatcher`, `Scheduler`, `HeartbeatMonitor`, `ModuleManager`, `Coordinator`, `LifecycleManager`, `HealthMonitor`. API publiknya: `start()`, `stop()`, `restart()`, `reload()`, `health()`, `status()`.

### Isi `luno/core/`

| File | Peran |
|---|---|
| `event_bus.py` | Publish/subscribe event, antrian, wildcard subscribe (`"*"`) |
| `dispatcher.py` | Thread pool yang benar-benar mengeksekusi handler event |
| `coordinator.py` | Tabel routing (event pattern → nama module), `add_route()` |
| `module_manager.py` | Registrasi module + resolusi dependency |
| `lifecycle.py` | Start/stop/restart per module dengan isolasi fault |
| `scheduler.py` / `heartbeat.py` | Tick loop periodik + heartbeat kesehatan sistem |
| `health.py` | Agregasi status kesehatan semua module |
| `events.py` | ~24 tipe `Event` inti (SystemStarted, SpeechRecognized, HomeAssistantEvent, dst) |
| `context_builder.py` | Membangun konteks percakapan untuk LLM |

Setiap package fungsional lain (`adapters`, `behavior_tree`, `planner`, dst) punya `tests/` sendiri dengan pola runner mandiri `[PASS]/[FAIL]` (list `SCENARIOS` + decorator `@scenario` + `main()`), dijalankan lewat `python3 -m luno.<package>.tests.test_<package>`. Beberapa test level-console pakai `pytest` (`tests/test_runtime_demo.py`, `tests/test_wake_session_console.py`) atau memuat `main_runtime_demo.py` sebagai module lewat `importlib` (`tests/test_barge_in_console.py`, `tests/test_wake_barge_in_integration.py`, `tests/test_real_fish_audio_console.py`).

---

## 2. Package-package fungsional

### `luno/adapters/` — Lapisan Adaptor (batas ke dunia luar)

Satu-satunya lapisan yang boleh bicara ke sistem eksternal (API, hardware, network). Tidak ada logic AI/planning di sini — hanya translasi Event ↔ API eksternal.

- **`whisper.py`** — speech-to-text (mock + interface `WhisperSource`)
- **`vision.py`** — kamera/vision (mock + interface)
- **`openrouter.py`** — LLM lewat OpenRouter/OpenAI-compatible API. `MockOpenRouterClient` (tanpa network) dan `RequestsOpenRouterClient` (asli, streaming SSE, retry/backoff)
- **`fish_audio.py`** — TTS playback lifecycle: `SpeakRequest → SpeechPlaybackStarted → SpeechPlaybackFinished` (atau `Cancelled`). Pause/resume, executor sendiri (tidak memblokir Event Bus)
- **`fish_audio_real.py`** — implementasi **asli** GPT-SoVITS/F5-TTS (`RealFishAudioClient`), lihat bagian 4 di bawah
- **`unity.py`**, **`home_assistant.py`**, **`scheduler.py`** — adaptor lain (avatar Unity, smart home, scheduled job)
- **`manager.py`** — `AdapterManager`, fasad tipis di atas `ModuleManager`/`Coordinator` khusus adaptor
- **`models.py`** — `EventMapping`/`RouteRule`: tabel routing event→adaptor yang **konfigurabel** (`DEFAULT_ADAPTER_EVENT_MAPPING`), bukan hardcoded `if/else`

~29 tipe event didefinisikan di `events.py` (LLMStarted/LLMChunk/LLMFinished, SpeechPlaybackStarted/Finished/Cancelled/Paused/Resumed, dst).

### `luno/behavior_tree/` — Pengambil keputusan "apa yang harus dilakukan sekarang"

State machine prioritas: Emergency → Critical HA event → Direct user speech (Listening) → Tool execution → Conversation continuation → Watching (visual pasif) → Proactive → Idle. Setiap aksi dijalankan async di executor sendiri (`_dispatch()`), tidak pernah memblokir tick loop.

### `luno/planner/` — "Bagaimana" mengeksekusi sebuah permintaan

Mengubah permintaan user jadi satu/lebih `ToolCall` terstruktur, dependency-ordered, dengan retry/rollback/cancel/pause-resume. Tidak pernah bicara langsung ke hardware — hanya menghasilkan `ToolCall` generik seperti `{"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"}`.

### `luno/tool_manager/` — Lapisan eksekusi universal

Menerima `ToolCall` generik (dari Planner, tapi **tidak** mengimpor Planner — hanya bergantung pada bentuk data), mencari handler yang tepat lewat `registry.py`, menjalankannya dengan timeout/retry/cancel, selalu mengembalikan `ToolResult` terstruktur. Tidak pernah generate bahasa atau memanggil LLM.

### `luno/vision_memory/` — Kesadaran visual persisten

Duduk antara model vision (MiniCPM-V) dan LLM: mengubah stream deskripsi per-frame jadi world-state yang selalu ter-update, log perubahan yang **bermakna saja** (di-skor 1–5, dedup), dan kebiasaan yang dipelajari pelan-pelan. LLM hanya pernah melihat ringkasan (`get_world_state()`/`get_recent_events()`), bukan raw frame-by-frame. Disimpan di SQLite (`config/vision_memory.sqlite3`).

### `luno/wake_session/` — Wake word + manajemen sesi percakapan (Sprint 2)

State: `SLEEPING → AWAKENING → LISTENING/THINKING/SPEAKING/WAITING_USER → (timeout) → SLEEPING`. Kata bangun (default: "luno", "hey luno", "hi luno") hanya wajib diucapkan saat `SLEEPING`; begitu sesi terbuka, semua ucapan berikutnya diproses langsung tanpa mengulang kata bangun, sampai `session_timeout_s` habis tanpa aktivitas.

### `luno/barge_in/` — Interupsi saat Luno sedang bicara/berpikir (Sprint 3)

4 mode: **FREE** (langsung berhenti + "Okay."/"Sure."), **SOFT** (hanya suara berhenti, task tetap jalan di background — mis. saat mematikan lampu), **CONFIRM** (tanya "Do you want to cancel the operation?", tunggu ya/tidak), **CRITICAL** (saat ada emergency aktif — hanya pause, tidak pernah cancel diam-diam). Whisper tetap mendengarkan penuh selagi Luno bicara (full-duplex, tidak di-pause).

### `luno/text_normalizer/` — Normalisasi teks sebelum dibacakan TTS

Mengubah angka/singkatan/simbol jadi bentuk yang enak dibacakan suara, mendukung Bahasa Indonesia (`numbers_id.py`) dan Inggris (`numbers_en.py`).

---

## 3. `main_runtime_demo.py` — Runtime Console Developer

Entry point pengembangan (**bukan** entry point produksi — itu `main.py`) yang merakit **semua** subsystem di atas lewat Event Bus asli, tanpa hardware nyata (mic/kamera/ESP32/Unity semua di-mock secara default). Tujuannya: developer bisa lihat seluruh pipeline event-driven jalan end-to-end sebelum integrasi hardware asli dipasang.

Alur satu giliran percakapan lengkap:

```
"Luno, buka chrome"
  → SpeechRecognized                         (console publish - simulasi Whisper)
  → SessionManagerModule (Sleeping → cocok kata bangun?)
  → WakeWordDetected / ConversationStarted / "Yes?" (ack diucapkan lewat Fish Audio)
  → "conversation_speech" (Listening, sudah bangun)
  → BehaviorTreeModule → "user_utterance"
  → PlannerBridgeModule → "speaking_mode_assigned" (klasifikasi FREE/SOFT/CONFIRM/CRITICAL)
  → ToolRequested → ToolManagerBridgeModule → ToolFinished
  → NeedLLMResponse → OpenRouterAdapter → LLMChunk*(streaming) → LLMFinished → AssistantResponse
  → BehaviorTreeModule._speak() (normalize_for_speech(), lalu SpeakRequest)
  → FishAudioAdapter → SpeechPlaybackFinished → WaitingUser
  → (tidak ada ucapan baru dalam session_timeout_s) → Sleeping
```

Sekaligus, kapan pun setelah Luno mulai bicara: "stop"/"cancel"/"pause"/"wait"/"hold on"/"enough"/"batal"/"sudah"/dst → `BargeInModule` memutuskan mode interupsi apa yang berlaku.

Jalankan: `python3 main_runtime_demo.py`. Perintah console penting: `/status`, `/health`, `/events`, `/session`, `/bargein`, `/wake`, `/sleep`, `/emergency`, `/debug on|off`, `/help`.

---

## 4. Perbaikan besar yang sudah dikerjakan di sesi-sesi sebelumnya

### Sprint 2 — Wake word + session management
Menambahkan gerbang kata-bangun sebelum ucapan diteruskan ke Behavior Tree, plus state machine sesi percakapan dengan timeout otomatis kembali ke Sleeping.

### Sprint 3 — Barge-in / percakapan yang bisa diinterupsi
Menambahkan `luno/barge_in` (4 mode interupsi) dan `luno/text_normalizer`, plus pause/resume pada Fish Audio adapter.

### Bug fix: integrasi Wake Session + Barge-In
Ditemukan celah timing nyata: `BargeInModule` membersihkan flag `thinking` begitu LLM selesai, tapi flag `speaking` baru menyala setelah `speech_playback_started` — ada jeda (bisa berdetik-detik lawan TTS asli) di mana interupsi suara diabaikan begitu saja. Diperbaiki dengan jendela toleransi `_speech_pending_deadline`. Ditemukan juga bug kedua: `SessionManagerModule` bisa salah meneruskan kata interupsi ("stop") sebagai giliran percakapan baru alih-alih interupsi — diperbaiki dengan pengecekan prioritas interupsi sebelum pencocokan kata bangun.

### Bug fix: adaptor GPT-SoVITS/F5-TTS asli
Setelah mock TTS diganti API asli, status Runtime tidak lagi mencerminkan kondisi bicara yang sebenarnya (Wake Session tetap `Sleeping`/`Talking=False` walau Luno sedang bersuara), sehingga Barge-In tidak pernah aktif. Akar masalah: `SpeechPlaybackStarted` dipublish **sebelum** sintesis audio selesai, bukan tepat sebelum audio benar-benar mulai diputar. Diperbaiki dengan:
1. **`RealFishAudioClient`** (`luno/adapters/fish_audio_real.py`) — implementasi asli GPT-SoVITS/F5-TTS yang genuinely: sintesis (HTTP) berjalan di background thread yang bisa dibatalkan di tengah jalan, `on_playback_start` dipanggil **hanya** tepat sebelum audio benar-benar mulai (bukan saat sintesis dimulai), pause/resume asli lewat `sounddevice`.
2. **Celah kedua**: interupsi FREE-mode yang terjadi **selagi masih sintesis** (belum ada suara sama sekali) tidak pernah memberi tahu Fish Audio untuk berhenti, karena `BargeInModule` hanya mengirim `stop_playback` saat `speaking=True`. Diperbaiki di lapisan adaptor saja (tanpa mengubah `barge_in/manager.py`): `FishAudioAdapter` sekarang juga mendengarkan `llm_cancelled` dan menghentikan sintesis/pemutaran yang sedang berjalan jika `request_id`-nya cocok dengan yang sedang ditangani.

Diaktifkan lewat env var `FISH_AUDIO_BACKEND=real` (atau `gptsovits`/`f5tts`) — default tetap mock, demo tetap jalan tanpa dependency eksternal.

---

## 5. Cara menjalankan & mode operasi

### Menjalankan
- **Console pengembangan (mock, tanpa hardware)**: `python3 main_runtime_demo.py`
- **Produksi (hardware asli)**: `python3 main.py` (butuh `.env` terisi, lihat bawah)

### Konfigurasi (`luno/config.py`, dibaca dari `.env`)

| Kategori | Variabel penting |
|---|---|
| LLM | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (kosong = OpenAI resmi; isi untuk DeepSeek/OpenRouter/dll), `OPENAI_MODEL` (default `gpt-4o`), `CHAT_MAX_TOKENS` |
| Home Assistant | `HA_URL`, `HA_TOKEN`, `HA_WS_URL` |
| Web search | `TAVILY_API_KEY` (opsional — fitur nonaktif kalau kosong) |
| TTS | `TTS_ENGINE` (`gptsovits`/`f5tts`), `GPTSOVITS_HOST`, `F5TTS_HOST`, `REFERENCE_AUDIO`, `REFERENCE_TEXT` (voice cloning) |
| STT | `WAKE_WORD`, `STT_LANGUAGE`, `WHISPER_MODEL_SIZE`, `WHISPER_DEVICE` (`cpu`/`cuda`), `STT_NOISE_REDUCE` |
| Bahasa balasan | `LUNO_LANGUAGE` (`auto` = ikut bahasa user, atau paksa satu bahasa) |
| Audio output | `AUDIO_OUTPUT_MODE` (`desktop`/`cast`), `CAST_ENTITY_ID` |
| Avatar | `AVATAR_BACKEND` (`web`/`vnyan`/`vnyan_engine`/`none`), `VNYAN_OSC_HOST`, `VNYAN_OSC_PORT`, `VNYAN_IDLE_MOTION` |
| Vision | `CAMERA_VISION_ENABLED` (default mati), `YOLO_MODEL_PATH`, `OLLAMA_HOST`, `OLLAMA_VISION_MODEL` (MiniCPM-V lokal lewat Ollama) |
| Memory | `MEMORY_TURNS`, `LONG_TERM_MEMORY_FILE`, `SESSION_SUMMARIES_FILE`, `PERSONA_FILE` |

File konfigurasi runtime (bukan `.env`) disimpan di `config/`: `persona.json`, `lights.config.json`, `switches.config.json`, `scripts.config.json`, `apps.json`, `reminders.json`, `long_term_memory.json`, `session_summaries.json`, `vision_memory.sqlite3`.

Karakter suara (voice cloning) disimpan di `character_files/` (mis. `Kirara`, `main_sample.wav`).

### Dependency utama (`requirements.txt`)
`openai`, `requests`, `websockets`, `python-dotenv` (core) · `faster-whisper`, `SpeechRecognition`, `sounddevice`, `soundfile`, `noisereduce` (STT/audio) · `openwakeword`, `onnxruntime` (wake word) · `python-osc` (VNyan) · `opencv-python`, `ultralytics` (YOLO), Ollama terpisah untuk MiniCPM-V (vision).

Server TTS terpisah (`f5tts_server/`) — FastAPI, endpoint `POST /tts` mengembalikan WAV mentah, drop-in pengganti GPT-SoVITS untuk backend F5-TTS (lihat `f5tts_server/SETUP_F5TTS.md`).

---

## 6. Status pengujian

Setiap package event-driven punya suite regresinya sendiri. Total saat ini: **~236+ pemeriksaan otomatis** lolos di seluruh package (`core`, `adapters` termasuk `fish_audio_real`, `wake_session`, `barge_in`, `text_normalizer`, `tool_manager`) ditambah 5 suite integrasi level-console di `tests/` (`test_runtime_demo.py`, `test_wake_session_console.py`, `test_barge_in_console.py`, `test_wake_barge_in_integration.py`, `test_real_fish_audio_console.py`).

Konvensi pengujian: setiap file test punya list `SCENARIOS` + decorator `@scenario`, fungsi `main()` mencetak `[PASS]`/`[FAIL]` per skenario lalu ringkasan `N/M scenarios passed`, dijalankan langsung (`python3 path/to/test_file.py`) atau lewat `python3 -m luno.<pkg>.tests.test_<pkg>`. Test level-console memuat `main_runtime_demo.py` lewat `importlib.util.spec_from_file_location` agar bisa mengetes `RuntimeDemoConsole` yang sesungguhnya tanpa mengubahnya jadi package importable biasa.

---

## 7. Aturan arsitektur yang harus selalu dijaga

- **Jangan pernah memanggil modul lain secara langsung** — selalu lewat Event Bus (publish/subscribe), kecuali memang butuh jawaban sinkron (pakai pola `wait_for_event`).
- **Jangan buat routing event hardcoded** di dalam kode module — gunakan `Coordinator.add_route()` atau `EventMapping`/`DEFAULT_ADAPTER_EVENT_MAPPING` yang konfigurabel.
- **`luno/wake_session` dan `luno/barge_in` sengaja tidak saling mengimpor** — kalau ada logic yang mirip dibutuhkan di keduanya, duplikasi kecil yang disengaja lebih baik daripada coupling lintas package.
- **Lapisan adaptor (`luno/adapters`) adalah satu-satunya tempat yang boleh bicara ke API/hardware eksternal.** Package lain (`behavior_tree`, `planner`, `tool_manager`, `vision_memory`) tidak boleh tahu apakah yang di baliknya itu mock atau layanan asli.
- **Setiap adaptor punya mock default** supaya seluruh sistem tetap bisa dijalankan dan diuji tanpa API key/hardware apa pun.
