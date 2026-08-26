import os
import re
import json
import wave
import io
import requests
import tempfile
import threading
import asyncio
import time
import socket
import uuid
import functools
import http.server
from datetime import datetime
from openai import OpenAI
import sounddevice as sd
import soundfile as sf
import speech_recognition as sr
from faster_whisper import WhisperModel

from luno import config
from luno.ha_client import HomeAssistantClient
from luno import devices
from luno import ha_listener
from luno import memory
from luno import persona
from luno import web_search
from luno import reminders
from luno import desktop_control
from luno import avatar_bridge
from luno import avatar_dispatch
from luno import vnyan_idle
from luno import expressions
from luno import audio_preprocess
from luno import pending_actions
from luno import tts_text

config.validate()

# ─────────────────────────────────────────────
#  INIT
# ─────────────────────────────────────────────

_client_kwargs = {"api_key": config.OPENAI_API_KEY}
if config.OPENAI_BASE_URL:
    _client_kwargs["base_url"] = config.OPENAI_BASE_URL
    print(f"[Config] ✓ Pakai API endpoint custom: {config.OPENAI_BASE_URL}\n")
client = OpenAI(**_client_kwargs)
ha_client = HomeAssistantClient()

# ─────────────────────────────────────────────
#  SYSTEM PROMPT
# ─────────────────────────────────────────────

def build_system_prompt():
    now = datetime.now()
    light_names = ", ".join(devices.LIGHTS.keys()) if devices.LIGHTS else None
    switch_names = ", ".join(devices.SWITCHES.keys()) if devices.SWITCHES else None
    script_names = ", ".join(devices.SCRIPTS.keys()) if devices.SCRIPTS else None

    parts = [persona.build_persona_prompt()]

    if light_names:
        parts.append(f"You can control these RGB lights: {light_names}.")
    if switch_names:
        parts.append(f"You can also control these switches/plugs: {switch_names}.")
    if script_names:
        parts.append(f"You can also run these scenes/scripts by name: {script_names}.")
    if not light_names and not switch_names and not script_names:
        parts.append("No smart home devices are configured yet.")

    if light_names or switch_names:
        parts.append(
            "You can ALSO control ALL lights/switches at once when the user says things like "
            "'all lights', 'semua lampu', 'everything off', 'semua perangkat' — this works and "
            "controls every configured device of that type simultaneously, it is not a limitation."
        )
        parts.append(
            "You CAN also report whether a specific light/switch is currently on or off if the "
            "user asks (e.g. 'is the RGB strip on?', 'status main lamp gimana?') — this is fully "
            "supported, never claim you can't check status."
        )
        parts.append(
            "You CAN also schedule an on/off action (or a script) to happen after a delay, e.g. "
            "'turn off the RGB strip in 10 minutes' or 'jalankan gaming mode dalam 1 jam' — this "
            "is fully supported, never claim you can't set timers/schedules."
        )

    parts.append(
        "Only claim you can't control a device if it's genuinely outside this list "
        "(e.g. speakers, TVs, thermostats aren't supported)."
    )

    memory_prompt = memory.build_memory_prompt()
    if memory_prompt:
        parts.append(memory_prompt)

    session_prompt = memory.build_session_summary_prompt()
    if session_prompt:
        parts.append(session_prompt)

    parts.append(
        "You have a save_memory tool. Use it silently, without announcing it, ONLY for durable "
        "facts about the user (preferences, allergies, routines, ongoing projects) — never to "
        "log/summarize what just happened in the conversation itself (that's handled elsewhere "
        "automatically). When in doubt, don't call it."
    )

    if web_search.is_configured():
        parts.append(
            "You also have search_web (single quick query) and deep_search (break a complex "
            "topic into 2-5 sub-queries yourself for deeper research) tools for current/"
            "real-time information (news, weather, prices, recent events) — use search_web for "
            "simple one-off facts and deep_search for questions needing comparison or synthesis "
            "across multiple angles, instead of guessing."
        )

    parts.append(
        f"Current date/time: {now.strftime('%A, %Y-%m-%d %H:%M:%S')}. You also have a "
        "set_reminder tool — use it whenever the user asks to be reminded/notified/woken up "
        "about something at a future time, resolving relative time expressions against the "
        "current date/time above."
    )

    registered_apps = ", ".join(desktop_control.APPS.keys()) if desktop_control.APPS else None
    apps_note = f" Registered apps you can open with open_app: {registered_apps}." if registered_apps else ""
    parts.append(
        "You also have open_app (open a registered desktop program), search_browser (open the "
        "user's browser with a Google search, for when THEY want to browse it themselves), and "
        "play_music (play a song/artist) tools for controlling the computer Luno runs on."
        f"{apps_note} If the user names one of those registered apps (even as a single word, e.g. "
        "'buka steam'), call open_app immediately — don't second-guess it as an unrelated word."
    )

    # Instruksi bahasa SENGAJA ditaruh PALING TERAKHIR (bukan di tengah) dan diulang
    # dengan tegas — supaya nggak kalah "berat" sama blok kepribadian di awal prompt
    # yang mungkin berisi banyak teks Bahasa Indonesia (background/traits/motto/contoh
    # kalimat karakter). Tanpa penekanan ini, model kecil kadang kebawa "gravitasi"
    # bahasa dari teks kepribadian yang panjang itu, walau ada instruksi override.
    if config.LUNO_LANGUAGE == "auto":
        parts.append(
            "IMPORTANT, FINAL INSTRUCTION — language: reply in the SAME language the user just "
            "used in their message (Indonesian or English — match them, don't default to "
            "English). This overrides the language of any character/background text above. Keep "
            "it casual and brief. No emojis."
        )
    else:
        parts.append(
            f"IMPORTANT, FINAL INSTRUCTION — language: your reply MUST be written in "
            f"{config.LUNO_LANGUAGE}, no matter what language the user's message is in, and even "
            f"though your character background/traits/example lines above may be written in a "
            f"different language — those are personality flavor only, NOT a language instruction. "
            f"Translate the spirit/tone, not the literal words. Keep it casual and brief. No emojis."
        )

    return " ".join(parts)


# ─────────────────────────────────────────────
#  LUNO BRAIN
# ─────────────────────────────────────────────

def generate_script_feedback(ran_scripts, user_text):
    """Minta GPT bikin kalimat konfirmasi singkat & variatif untuk script yang baru
    dijalankan, supaya tidak monoton ngomong kalimat yang sama tiap kali dipanggil.
    Dipanggil dari thread utama (bukan thread HA), jadi aman biarpun blocking."""
    script_lines = []
    for s in ran_scripts:
        if s["description"]:
            script_lines.append(f'- "{s["name"]}": {s["description"]}')
        else:
            script_lines.append(f'- "{s["name"]}"')

    if config.LUNO_LANGUAGE == "auto":
        language_instruction = (
            "Reply in the SAME language as this user message (match it, don't default to English): "
            f"\"{user_text}\""
        )
    else:
        language_instruction = f"Reply in {config.LUNO_LANGUAGE}, no matter what language the user's message is in."

    flavor_hint = persona.build_persona_flavor_hint()

    prompt = (
        "You just successfully triggered the following smart home script(s):\n"
        + "\n".join(script_lines)
        + "\n\nWrite ONE short spoken confirmation (1-2 sentences, no emojis) telling the "
        "user it's done, in your own natural voice/personality. Vary your wording naturally "
        f"each time — don't always use the same phrase like 'activated'. {flavor_hint} {language_instruction}"
    )

    try:
        res = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            temperature=1.0,  # sedikit lebih acak biar variasi kalimatnya kerasa
            messages=[{"role": "user", "content": prompt}],
            **{config.MAX_TOKENS_PARAM: 60},
        )
        text = (res.choices[0].message.content or "").strip()
        return text if text else f"{', '.join(s['name'].title() for s in ran_scripts)} done."
    except Exception as ex:
        print(f"[Luno] ✗ Script feedback generation error: {ex}\n")
        return f"{', '.join(s['name'].title() for s in ran_scripts)} activated."


def _build_reply_for_result(result, user_text):
    """Bikin teks balasan deterministik untuk 1 hasil dari process_smart_commands
    (tipe: 'scripts' / 'timer' / 'status'). Dipakai di Luno_Brain, juga dipanggil
    berulang untuk tiap sub-hasil kalau tipe-nya 'multi' (beberapa klausa sekaligus)."""
    result_type = result.get("type")

    indo = _should_reply_indo(user_text.lower())

    if result_type == "scripts":
        ran_scripts = result["ran"]
        # Kalau ada script yang punya "feedback" tetap di config, pakai apa adanya.
        # Sisanya (yang tidak diisi) digabung jadi satu permintaan ke GPT sekaligus,
        # supaya hasilnya tetap 1 kalimat ringkas walau beberapa script jalan bareng.
        fixed = [s["feedback"] for s in ran_scripts if s["feedback"]]
        needs_gpt = [s for s in ran_scripts if not s["feedback"]]
        parts = list(fixed)
        if needs_gpt:
            parts.append(generate_script_feedback(needs_gpt, user_text))

        # Sebut app yang otomatis dibuka (dari "open_apps" di scripts.config.json) —
        # deterministik, TIDAK lewat GPT, biar selalu akurat sama yang beneran kebuka.
        all_opened = [app for s in ran_scripts for app in s.get("opened_apps", [])]
        if all_opened:
            apps_str = ", ".join(all_opened)
            parts.append(f"Juga membuka {apps_str}." if indo else f"Also opening {apps_str}.")

        if result.get("ask_open_app"):
            parts.append("Mau main apa?" if indo else "What do you want to play?")

        return " ".join(parts)

    if result_type == "timer":
        label = result["label"]
        desc = result["action_desc"]
        return f"Oke, aku {desc} dalam {label}." if indo else f"Got it — I'll {desc} in {label}."

    if result_type == "device_action":
        action = result["action"]
        names = ", ".join(result["names"]) if result["names"] else ("lampu" if indo else "the light")
        value = result.get("value")
        if action == "on":
            return f"Oke, {names} udah dinyalain." if indo else f"Done, turned on {names}."
        if action == "off":
            return f"Oke, {names} udah dimatiin." if indo else f"Done, turned off {names}."
        if action == "fade":
            return f"Oke, {names} nyala perlahan." if indo else f"Done, fading {names} on."
        if action == "brightness":
            pct = round((value / 255) * 100) if value else None
            return (f"Oke, kecerahan {names} diatur ke {pct}%." if indo else f"Done, set {names} brightness to {pct}%.") \
                if pct is not None else (f"Oke, kecerahan {names} udah diatur." if indo else f"Done, adjusted {names} brightness.")
        if action == "color":
            return f"Oke, warna {names} diganti jadi {value}." if indo else f"Done, changed {names} color to {value}."
        return f"Oke, {names} udah diatur." if indo else f"Done, {names} adjusted."

    if result_type == "status":
        parts = []
        for d in result["devices"]:
            name = d["name"].title()
            state = d["state"]
            if indo:
                state_word = {"on": "nyala", "off": "mati"}.get(state, state)
                parts.append(f"{name} {state_word}")
            else:
                state_word = {"on": "on", "off": "off"}.get(state, state)
                parts.append(f"{name} is {state_word}")
        return (", ".join(parts) + ".") if parts else None

    if result_type == "light_not_found":
        name = result["name"]
        known = ", ".join(n.title() for n in devices.wled_lights.keys()) if devices.wled_lights else ""
        if indo:
            msg = f"Hmm, aku nggak nemu lampu bernama '{name}'."
            if known:
                msg += f" Lampu yang ada: {known}."
            return msg
        else:
            msg = f"Hmm, I couldn't find a light called '{name}'."
            if known:
                msg += f" Lights I know: {known}."
            return msg

    return None


def Luno_Brain(user_text):
    user_lower = user_text.lower()
    indo = _should_reply_indo(user_lower)

    # ── Jawaban atas pertanyaan yang lagi ditunggu (mis. "mau main apa?") ──
    # Dicek PALING AWAL, tapi kalau jawabannya nggak cocok apa-apa DAN bukan
    # pembatalan eksplisit, pending-nya di-drop diam-diam lalu lanjut ke pemrosesan
    # normal di bawah — supaya command lain yang nggak nyambung sama pertanyaan
    # tadi tetap kejalanin, nggak ke-"telen" gara-gara ada pending yang nyangkut.
    pending = pending_actions.get_pending()
    if pending and pending.get("type") == "open_app":
        cancel_phrases = ["gajadi", "ga jadi", "batal", "nevermind", "never mind", "skip", "cancel"]
        if any(p in user_lower for p in cancel_phrases):
            pending_actions.clear_pending()
            reply = "Oke, gapapa." if indo else "Okay, no worries."
            print(f"[Luno] {reply}\n")
            return reply

        # Word-boundary match (BUKAN substring longgar) — "steam" harus muncul sebagai
        # KATA UTUH di kalimat user, bukan cuma nempel di tengah kata lain. Ini penting
        # justru buat ngurangin false-positive kalau pending ini kelewat lama nyangkut
        # (walau sekarang udah ada expiry juga di pending_actions.get_pending()).
        matched_app = next(
            (name for name in desktop_control.APPS if re.search(rf'\b{re.escape(name)}\b', user_lower)),
            None,
        )
        pending_actions.clear_pending()
        if matched_app:
            _ok, msg = desktop_control.open_app(matched_app)
            print(f"[Luno] {msg}\n")
            return msg
        # Nggak match & bukan pembatalan -> lanjut ke pemrosesan normal di bawah
        # (siapa tau ini command lain, bukan jawaban buat pertanyaan tadi).

    # ── Command deterministik seputar memory — dicek duluan, gak perlu panggil GPT ──

    if memory.is_clear_everything_command(user_lower):
        memory.clear_short_term()
        n = memory.clear_all_long_term()
        reply = (f"Oke, semua yang aku inget udah aku hapus (termasuk {n} fakta jangka panjang)." if indo
                 else f"Got it — I've wiped everything, including {n} long-term memor{'y' if n == 1 else 'ies'}.")
        print(f"[Luno] {reply}\n")
        return reply

    if memory.is_clear_short_term_command(user_lower):
        memory.clear_short_term()
        reply = "Oke, aku lupain percakapan sebelumnya." if indo \
            else "Got it, I've cleared our conversation history."
        print(f"[Luno] {reply}\n")
        return reply

    if memory.is_recall_command(user_lower):
        facts = memory.list_memories()
        if not facts:
            reply = "Belum ada yang aku inget jangka panjang soal kamu nih." if indo \
                else "I don't have anything saved in long-term memory about you yet."
        else:
            listed = "; ".join(m["text"] for m in facts)
            reply = f"Ini yang aku inget: {listed}." if indo else f"Here's what I remember: {listed}."
        print(f"[Luno] {reply}\n")
        return reply

    if memory.is_session_recall_command(user_lower):
        summaries = memory.list_session_summaries(limit=5)
        if not summaries:
            reply = "Belum ada riwayat obrolan sesi sebelumnya yang kesimpen nih." if indo \
                else "I don't have any past session summaries saved yet."
        else:
            listed = "; ".join(f"({s['ended_at'][:10]}) {s['summary']}" for s in summaries)
            reply = f"Ini topik obrolan kita sebelumnya: {listed}" if indo \
                else f"Here's what we discussed in past sessions: {listed}"
        print(f"[Luno] {reply}\n")
        return reply

    if memory.is_manual_summarize_command(user_lower):
        summary = memory.summarize_and_archive_session(client)
        if summary:
            reply = f"Sip, udah aku rangkum: {summary}" if indo else f"Got it, summarized: {summary}"
        else:
            reply = "Belum ada obrolan buat dirangkum nih." if indo else "There's nothing to summarize yet."
        print(f"[Luno] {reply}\n")
        return reply

    if reminders.is_list_command(user_lower):
        pending = reminders.list_pending()
        if not pending:
            reply = "Nggak ada reminder yang masih pending nih." if indo else "You don't have any pending reminders."
        else:
            listed = "; ".join(f"{r['message']} ({r['trigger_at'][:16].replace('T', ' ')})" for r in pending)
            reply = f"Ini reminder kamu yang masih pending: {listed}" if indo \
                else f"Here are your pending reminders: {listed}"
        print(f"[Luno] {reply}\n")
        return reply

    cancel_query = reminders.detect_cancel_command(user_text)
    if cancel_query:
        removed = reminders.remove_reminder(cancel_query.lower())
        if removed:
            names = "; ".join(r["message"] for r in removed)
            reply = f"Oke, reminder '{names}' udah aku batalin." if indo else f"Got it, cancelled: {names}."
        else:
            reply = f"Hmm, aku nggak nemu reminder yang cocok sama '{cancel_query}'." if indo \
                else f"Hmm, I couldn't find a reminder matching '{cancel_query}'."
        print(f"[Luno] {reply}\n")
        return reply

    forget_query = memory.detect_forget_fact_command(user_text)
    if forget_query:
        removed = memory.remove_memory(forget_query.lower())
        if removed:
            reply = f"Oke, aku udah lupain: {'; '.join(removed)}." if indo \
                else f"Got it, I've forgotten: {'; '.join(removed)}."
        else:
            reply = f"Hmm, aku nggak nemu ingatan yang cocok sama '{forget_query}'." if indo \
                else f"Hmm, I couldn't find a memory matching '{forget_query}'."
        print(f"[Luno] {reply}\n")
        return reply

    fact_to_remember = memory.detect_remember_command(user_text)
    if fact_to_remember:
        memory.add_memory(fact_to_remember)
        reply = f"Sip, aku inget: {fact_to_remember}." if indo else f"Got it, I'll remember: {fact_to_remember}."
        print(f"[Luno] {reply}\n")
        return reply

    # ── Obrolan/perintah normal ──

    messages = [
        {"role": "system", "content": build_system_prompt()},
        *memory.get_history(),
    ]

    # Lapis pengaman KEDUA buat instruksi bahasa (selain yang udah ada di akhir
    # system prompt) — ditaruh SEPERSIS MUNGKIN sebelum pesan user, karena model
    # kecil kadang "kebawa" ikutan bahasa pesan user walau udah ada override di
    # system prompt, ESPECIALLY kalau user nulis full kalimat dalam bahasa lain.
    if config.LUNO_LANGUAGE != "auto":
        messages.append({
            "role": "system",
            "content": (
                f"Reminder: reply in {config.LUNO_LANGUAGE} ONLY for the next message, "
                "even if it's written in a different language. Do not switch languages "
                "to match the user."
            ),
        })

    messages.append({"role": "user", "content": user_text})

    tools = [memory.MEMORY_TOOL, reminders.REMINDER_TOOL] + desktop_control.TOOLS
    if web_search.is_configured():
        tools.append(web_search.WEB_SEARCH_TOOL)
        tools.append(web_search.DEEP_SEARCH_TOOL)

    try:
        res = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            **{config.MAX_TOKENS_PARAM: config.CHAT_MAX_TOKENS},
        )
        msg = res.choices[0].message
        reply = (msg.content or "").strip()

        if msg.tool_calls:
            # GPT mutusin sendiri perlu manggil tool (save_memory dan/atau search_web) —
            # eksekusi tiap tool call, lalu (kalau belum ada teks balasan) minta GPT nulis
            # balasan final setelahnya, sekarang dengan hasil tool itu sebagai konteks.
            # PENTING: append sebagai dict biasa (bukan objek message mentah), format standar
            # yang diharapkan OpenAI messages[] untuk merepresentasikan tool_calls.
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                if tc.function.name == "save_memory":
                    try:
                        fact = json.loads(tc.function.arguments).get("fact", "").strip()
                    except Exception:
                        fact = ""
                    # Manual Memory Management sprint: this tool call is the
                    # LLM deciding on its own, mid-conversation, that
                    # something is worth remembering (per this tool's own
                    # "Call this SILENTLY" docstring) - NOT a literal
                    # "inget ya..." from the user. `source="llm_auto"` keeps
                    # that provenance honest instead of mislabeling it as
                    # `add_memory()`'s new "user_explicit" default (used by
                    # `main_runtime_demo.py`'s `detect_remember_command()`
                    # path, a genuinely different, literal user command).
                    saved = memory.add_memory(fact, source="llm_auto") if fact else None
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "saved" if saved else "skipped",
                    })
                elif tc.function.name == "search_web":
                    try:
                        query = json.loads(tc.function.arguments).get("query", "").strip()
                    except Exception:
                        query = ""
                    result = web_search.search_web(query)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                elif tc.function.name == "deep_search":
                    try:
                        queries = json.loads(tc.function.arguments).get("queries", [])
                    except Exception:
                        queries = []
                    result = web_search.deep_search(queries)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                elif tc.function.name == "set_reminder":
                    try:
                        args = json.loads(tc.function.arguments)
                        r_message = args.get("message", "").strip()
                        r_trigger_at = args.get("trigger_at", "").strip()
                    except Exception:
                        r_message, r_trigger_at = "", ""
                    entry = reminders.add_reminder(r_message, r_trigger_at) if r_message and r_trigger_at else None
                    if entry:
                        _schedule_reminder(entry)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"scheduled for {entry['trigger_at']}" if entry else "failed (invalid message/time)",
                    })
                elif tc.function.name == "open_app":
                    try:
                        app_name = json.loads(tc.function.arguments).get("name", "").strip()
                    except Exception:
                        app_name = ""
                    _ok, msg = desktop_control.open_app(app_name)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": msg})
                elif tc.function.name == "search_browser":
                    try:
                        query = json.loads(tc.function.arguments).get("query", "").strip()
                    except Exception:
                        query = ""
                    _ok, msg = desktop_control.search_browser(query)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": msg})
                elif tc.function.name == "play_music":
                    try:
                        query = json.loads(tc.function.arguments).get("query", "").strip()
                    except Exception:
                        query = ""
                    _ok, msg = desktop_control.play_music(query)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": msg})
                else:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "unknown tool"})

            if not reply:
                follow_up = client.chat.completions.create(
                    model=config.OPENAI_MODEL,
                    messages=messages,
                    **{config.MAX_TOKENS_PARAM: config.CHAT_MAX_TOKENS},
                )
                reply = (follow_up.choices[0].message.content or "").strip()
    except Exception as ex:
        reply = f"Error: {ex}"

    if ha_listener.ha_loop and ha_client.connected:
        try:
            future = asyncio.run_coroutine_threadsafe(process_smart_commands(user_text), ha_listener.ha_loop)
            command_result = future.result(timeout=30)  # tunggu sampai semua perintah selesai dieksekusi

            if command_result:
                if command_result.get("type") == "multi":
                    parts = [_build_reply_for_result(r, user_text) for r in command_result["results"]]
                    parts = [p for p in parts if p]
                else:
                    text = _build_reply_for_result(command_result, user_text)
                    parts = [text] if text else []

                if parts:
                    reply = " ".join(parts)
        except Exception as ex:
            print(f"[Luno] ✗ Smart command error: {ex}\n")
    else:
        print("[Luno] ✗ HA belum terkoneksi, perintah device dilewati\n")

    print(f"[Luno] {reply}\n")

    memory.remember_turn(user_text, reply)

    return reply


_CLAUSE_SPLIT_RE = re.compile(r'\s*(?:,|;|\bdan\b|\band\b|\bjuga\b|\blalu\b|\bkemudian\b)\s*', re.IGNORECASE)

# ─── STATUS QUERY ("apakah lampu kamar nyala?", "status main lamp gimana?") ───
_STATUS_QUERY_MARKERS = [
    "apakah", "apa status", "status", "keadaan", "kondisi", "gimana", "bagaimana",
    "cek ", "check ", "is it", "are the",
]
# Kata kerja PERINTAH yang jelas (bukan sekadar kata sifat nyala/mati) — kalau salah
# satu ini ada, anggap ini perintah aksi, BUKAN pertanyaan status, walau ada kata tanya.
_STRONG_ACTION_VERBS = [
    "nyalakan", "matikan", "hidupkan", "matiin", "nyalain", "turn on", "turn off",
    "warna", "color", "brightness", "terang", "bright", "dim", "perlahan", "fade", "transisi",
]


def _is_status_query(user_lower):
    has_marker = any(w in user_lower for w in _STATUS_QUERY_MARKERS) or user_lower.rstrip().endswith("?")
    has_strong_action = any(w in user_lower for w in _STRONG_ACTION_VERBS)
    return has_marker and not has_strong_action


def _looks_indonesian(text_lower):
    """Heuristik ringan buat nebak bahasa suatu teks — dipakai untuk balasan
    deterministik (status/timer) yang tidak lewat GPT, supaya tetap terasa
    natural mengikuti bahasa perintah aslinya."""
    indo_markers = [
        "nyalakan", "nyalain", "matikan", "matiin", "hidupkan", "hidupin", "padamkan",
        "lampu", "dalam", "menit", "jam", "lagi", "saklar", "gajadi", "ga jadi", "batal",
        "detik", "apakah", "gimana", "bagaimana", "kondisi", "keadaan", "status", "semua",
    ]
    return any(w in text_lower for w in indo_markers)


def _should_reply_indo(user_lower):
    """Tentuin bahasa balasan DETERMINISTIK (bukan yang lewat GPT — itu diatur
    langsung di build_system_prompt). SELALU cek config.LUNO_LANGUAGE dulu: kalau
    di-set selain 'auto', itu yang menang mutlak apa pun bahasa kalimat user (mis.
    LUNO_LANGUAGE=english supaya nggak kedengeran aneh kalau voice pack TTS-nya
    cuma bisa Inggris). Cuma fallback ke deteksi otomatis dari kalimat kalau
    LUNO_LANGUAGE='auto'. SEMUA tempat yang butuh tau bahasa balasan HARUS lewat
    fungsi ini, jangan panggil _looks_indonesian() langsung, biar konsisten."""
    if config.LUNO_LANGUAGE == "auto":
        return _looks_indonesian(user_lower)
    return config.LUNO_LANGUAGE in ("indonesian", "id", "bahasa indonesia")


# ─── TIMER / SCHEDULING ("matikan lampu kamar dalam 30 menit") ───
_DELAY_PATTERN_RE = re.compile(
    r'(\d+)\s*(detik|second|seconds|sec|menit|minute|minutes|min|jam|hour|hours|hr)\b'
)
_background_tasks = set()  # simpan referensi task terjadwal biar gak ke-garbage-collect


def _parse_delay_seconds(user_lower):
    """Cari pola waktu tunda ('30 menit', '2 jam', '10 detik lagi'). Return int detik
    atau None kalau tidak ada pola valid."""
    m = _DELAY_PATTERN_RE.search(user_lower)
    if not m:
        return None

    value = int(m.group(1))
    unit = m.group(2)

    if unit in ("detik", "second", "seconds", "sec"):
        # "X detik" dipakai juga oleh durasi FADE — supaya tidak ketuker, timer detik
        # hanya valid kalau ada indikator jeda eksplisit di kalimatnya.
        if not re.search(r'\b(lagi|dalam|in|from now)\b', user_lower):
            return None
        return value
    if unit in ("menit", "minute", "minutes", "min"):
        return value * 60
    return value * 3600  # jam/hour/hours/hr


def _format_duration(seconds, indo):
    if seconds >= 3600 and seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} jam" if indo else f"{n} hour" + ("s" if n != 1 else "")
    if seconds >= 60 and seconds % 60 == 0:
        n = seconds // 60
        return f"{n} menit" if indo else f"{n} minute" + ("s" if n != 1 else "")
    return f"{seconds} detik" if indo else f"{seconds} second" + ("s" if seconds != 1 else "")


def _schedule_action(delay_seconds, targets, method_name, action_label):
    """Jadwalkan pemanggilan target.<method_name>() setelah delay_seconds, TANPA
    nge-block caller (fire-and-forget). Aman dipanggil dari dalam coroutine yang
    sedang jalan di ha_listener.ha_loop — asyncio.create_task() otomatis nempel ke loop yang
    lagi aktif menjalankannya."""
    async def _runner():
        await asyncio.sleep(delay_seconds)
        try:
            for t in targets:
                await getattr(t, method_name)()
            print(f"[Timer] ✓ Executed: {action_label}\n")
            if ha_listener.ha_loop:
                # Jalankan TTS di thread terpisah (run_in_executor) supaya TIDAK
                # nge-block ha_listener.ha_loop selama proses TTS + playback audio berjalan.
                ha_listener.ha_loop.run_in_executor(None, speak, action_label)
        except Exception as ex:
            print(f"[Timer] ✗ Failed: {action_label} — {ex}\n")

    task = asyncio.create_task(_runner())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _schedule_reminder(entry):
    """Jadwalkan pengiriman 1 reminder (fire-and-forget) ke ha_listener.ha_loop. Aman
    dipanggil dari thread MANA PUN (beda dari _schedule_action di atas yang cuma aman
    dipanggil dari DALAM coroutine yang sudah jalan di loop itu) — run_coroutine_threadsafe
    menangani cross-thread scheduling-nya sendiri. Dipakai baik untuk reminder yang baru
    dibuat (dari Luno_Brain, jalan di main thread) maupun saat re-schedule ulang di startup."""
    if not ha_listener.ha_loop:
        print(f"[Reminders] ✗ Loop belum siap, '{entry['message']}' gagal dijadwalkan\n")
        return

    trigger_dt = datetime.fromisoformat(entry["trigger_at"])
    delay = max(0, (trigger_dt - datetime.now()).total_seconds())

    async def _runner():
        await asyncio.sleep(delay)
        reminders.mark_fired(entry["id"])
        print(f"[Reminders] 🔔 {entry['message']}\n")
        prefix = "Pengingat: " if _should_reply_indo(entry["message"].lower()) else "Reminder: "
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, speak, prefix + entry["message"])

    asyncio.run_coroutine_threadsafe(_runner(), ha_listener.ha_loop)


async def process_smart_commands(user_text):
    """Process smart home commands.
    Return: dict {"type": "scripts", "ran": [...]} kalau ada script yang dijalankan
    (data mentah, belum jadi kalimat — diproses jadi teks di Luno_Brain/thread utama),
    selain itu None.

    Kalimat dipecah dulu jadi beberapa klausa (dipisah "dan"/"and"/koma/dst) dan
    diproses SATU PER SATU secara independen. Ini penting supaya perintah seperti
    "nyalakan lampu A dan matikan lampu B" tidak salah — tanpa ini, seluruh kalimat
    dianggap 1 niat tunggal ("nyalakan") yang diterapkan ke semua nama yang ke-match,
    padahal maksudnya beda aksi untuk target yang beda."""
    if not devices.wled_lights and not devices.switch_devices and not devices.script_devices:
        return None

    clauses = [c for c in _CLAUSE_SPLIT_RE.split(user_text) if c.strip()]
    if len(clauses) <= 1:
        return await _process_clause(user_text)

    results = []
    for clause in clauses:
        result = await _process_clause(clause)
        if result:
            results.append(result)

    if not results:
        return None
    if len(results) == 1:
        return results[0]
    return {"type": "multi", "results": results}


async def _process_clause(user_text):
    """Proses SATU klausa perintah (hasil pecahan dari process_smart_commands)."""
    user_lower = user_text.lower()

    if not devices.wled_lights and not devices.switch_devices and not devices.script_devices:
        return None

    # STATUS QUERY — "apakah lampu kamar nyala?", "status main lamp gimana?"
    # Ditaruh SEBELUM devices.resolve_target_lights (yang fallback ke default) dan pakai
    # resolve_explicit_* (TANPA fallback) supaya pertanyaan generik yang gak nyebut
    # nama device tidak salah dianggap nanya status lampu default.
    explicit_lights = devices.resolve_explicit_lights(user_lower)
    explicit_switches = devices.resolve_explicit_switches(user_lower)
    if (explicit_lights or explicit_switches) and _is_status_query(user_lower):
        devices_info = []
        for ctrl in explicit_lights + explicit_switches:
            info = devices.device_states.get(ctrl.entity_id)
            state = info["state"] if info else "unknown"
            devices_info.append({"name": ctrl.name, "state": state})
        return {"type": "status", "devices": devices_info}

    # devices.SCRIPTS — cukup sebut namanya (mis. "gaming mode") untuk men-trigger,
    # tidak butuh kata kerja tambahan supaya cara pakainya sama seperti
    # memanggil scene/routine di Google Home / Alexa.
    # PENTING: hanya jalankan & kumpulkan data di sini — JANGAN panggil GPT di sini,
    # karena fungsi ini berjalan di event loop thread HA; memanggil GPT (blocking)
    # di sini akan menahan loop itu dan bisa bikin koneksi HA putus/lag.
    if devices.script_devices:
        matched_scripts = [ctrl for name, ctrl in devices.script_devices.items() if name in user_lower]
        if matched_scripts:
            delay_seconds = _parse_delay_seconds(user_lower)
            names = ", ".join(s.name for s in matched_scripts)
            if delay_seconds:
                indo = _should_reply_indo(user_lower)
                label = _format_duration(delay_seconds, indo)
                action_label = f"Menjalankan {names} sekarang." if indo else f"Running {names} now."
                _schedule_action(delay_seconds, matched_scripts, "run", action_label)
                desc = f"menjalankan {names}" if indo else f"run {names}"
                return {"type": "timer", "label": label, "action_desc": desc}

            ran_scripts = []
            ask_open_app_pending = False
            for script in matched_scripts:
                await script.run()
                opened_apps = []
                for app_name in script.open_apps:
                    # open_app() itu operasi sinkron (subprocess/os.startfile) — jalanin di
                    # thread terpisah biar nggak nge-block event loop HA yang lagi dipakai
                    # bareng buat koneksi Home Assistant.
                    ok, _msg = await asyncio.get_running_loop().run_in_executor(
                        None, desktop_control.open_app, app_name
                    )
                    if ok:
                        opened_apps.append(app_name)
                if script.ask_open_app and not script.open_apps:
                    ask_open_app_pending = True
                ran_scripts.append({
                    "name": script.name,
                    "feedback": script.feedback,          # kalimat tetap (kalau diisi di config)
                    "description": script.description,    # konteks untuk GPT (kalau feedback kosong)
                    "opened_apps": opened_apps,
                })

            if ask_open_app_pending:
                pending_actions.set_pending("open_app")

            return {"type": "scripts", "ran": ran_scripts, "ask_open_app": ask_open_app_pending}

    if not devices.wled_lights and not devices.switch_devices:
        return None

    # Cek dulu: apakah ini perintah lampu dengan NAMA SPESIFIK yang tidak terdaftar
    # sama sekali (mis. "nyalakan lampu utama" padahal cuma ada "rgb strip" di config)?
    # Kalau ya, berhenti di sini dan bilang device tidak ditemukan — JANGAN lanjut ke
    # devices.resolve_target_lights() di bawah, karena itu akan diam-diam fallback ke lampu
    # default (biasanya RGB Strip) yang bukan yang dimaksud user.
    missing_light_name = devices.light_name_not_found(user_lower)
    if missing_light_name:
        return {"type": "light_not_found", "name": missing_light_name}

    light_targets = devices.resolve_target_lights(user_lower) if devices.wled_lights else []
    switch_targets = devices.resolve_target_switches(user_lower) if devices.switch_devices else []
    is_all_devices = any(p in user_lower for p in devices.ALL_DEVICE_PHRASES)

    # Kata kunci "ini perintah lampu" — generik + otomatis mengikutkan semua nama
    # lampu yang terdaftar di config, supaya lampu baru dengan nama apa pun
    # (mis. "Main Lamp") tetap terdeteksi tanpa perlu edit kode ini lagi.
    light_word_present = is_all_devices or any(
        w in user_lower for w in ["light", "lampu", "wled", "rgb"]
    ) or any(name in user_lower for name in devices.wled_lights.keys())

    # FADE / TRANSITION ON (khusus lampu — saklar tidak punya brightness)
    fade_keywords = ["perlahan", "pelan-pelan", "pelan", "transisi", "fade", "gradually", "slowly", "smooth"]
    if light_targets and any(w in user_lower for w in fade_keywords) and (
        light_word_present or any(w in user_lower for w in ["on", "nyala", "hidup"])
    ):
        import re

        # Durasi transisi (detik). Kalau user tidak sebutkan, biarkan None
        # supaya masing-masing lampu pakai default_transition miliknya sendiri.
        duration_match = re.search(r'(\d+)\s*(?:detik|second|seconds|sec)\b', user_lower)
        duration = int(duration_match.group(1)) if duration_match else None

        # Target kecerahan: prioritaskan pola persen, lalu angka lain selain durasi
        percent_match = re.search(r'(\d+)\s*%', user_text)
        if percent_match:
            pct = int(percent_match.group(1))
            brightness_target = int((pct / 100) * 255)
        else:
            all_numbers = re.findall(r'\d+', user_text)
            if duration_match:
                dur_str = duration_match.group(1)
                remaining = [n for n in all_numbers if n != dur_str]
                all_numbers = remaining if remaining else all_numbers
            if all_numbers:
                val = int(all_numbers[0])
                brightness_target = int((val / 100) * 255) if val <= 100 else min(255, val)
            else:
                brightness_target = 255  # default full brightness kalau tidak disebutkan

        for light in light_targets:
            await light.fade_on(brightness_target, duration)
        return {"type": "device_action", "action": "fade", "names": [t.name for t in light_targets]}
    
    # ON
    if any(w in user_lower for w in ["on", "turn on", "nyalakan", "hidup"]):
        delay_seconds = _parse_delay_seconds(user_lower)
        on_targets = []
        if light_targets and light_word_present:
            on_targets += light_targets
        if switch_targets:
            on_targets += switch_targets

        if on_targets and delay_seconds:
            indo = _should_reply_indo(user_lower)
            label = _format_duration(delay_seconds, indo)
            names = ", ".join(t.name for t in on_targets)
            action_label = f"{names} sudah dinyalakan." if indo else f"{names} turned on."
            _schedule_action(delay_seconds, on_targets, "turn_on", action_label)
            desc = f"menyalakan {names}" if indo else f"turn on {names}"
            return {"type": "timer", "label": label, "action_desc": desc}
        elif on_targets:
            for t in on_targets:
                await t.turn_on()
            return {"type": "device_action", "action": "on", "names": [t.name for t in on_targets]}

    # OFF
    elif any(w in user_lower for w in ["off", "turn off", "matikan", "mati"]):
        delay_seconds = _parse_delay_seconds(user_lower)
        off_targets = []
        if light_targets and light_word_present:
            off_targets += light_targets
        if switch_targets:
            off_targets += switch_targets

        if off_targets and delay_seconds:
            indo = _should_reply_indo(user_lower)
            label = _format_duration(delay_seconds, indo)
            names = ", ".join(t.name for t in off_targets)
            action_label = f"{names} sudah dimatikan." if indo else f"{names} turned off."
            _schedule_action(delay_seconds, off_targets, "turn_off", action_label)
            desc = f"mematikan {names}" if indo else f"turn off {names}"
            return {"type": "timer", "label": label, "action_desc": desc}
        elif off_targets:
            for t in off_targets:
                await t.turn_off()
            return {"type": "device_action", "action": "off", "names": [t.name for t in off_targets]}
    
    # BRIGHTNESS (khusus lampu)
    elif any(w in user_lower for w in ["brightness", "terang", "bright", "dim"]):
        try:
            import re
            matches = re.findall(r'\d+', user_text)
            if matches:
                brightness_val = int(matches[0])
                if brightness_val <= 100:
                    brightness_val = int((brightness_val / 100) * 255)
                for light in light_targets:
                    await light.set_brightness(brightness_val)
                return {"type": "device_action", "action": "brightness", "names": [t.name for t in light_targets], "value": brightness_val}
        except Exception as e:
            pass
    
    # COLOR (khusus lampu)
    elif any(w in user_lower for w in ["color", "warna", "red", "blue", "green", "yellow", "orange", "pink", "purple", "cyan", "magenta", "white"]):
        for color in ["red", "green", "blue", "yellow", "cyan", "magenta", "white", "orange", "purple", "pink"]:
            if color in user_lower:
                for light in light_targets:
                    await light.set_color_name(color)
                return {"type": "device_action", "action": "color", "names": [t.name for t in light_targets], "value": color}


# ─────────────────────────────────────────────
#  TTS
# ─────────────────────────────────────────────

def get_local_ip():
    """Deteksi IP lokal komputer ini di jaringan (dibutuhkan agar Google Cast bisa fetch audio)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_audio_server():
    """Jalankan HTTP server ringan di background agar file audio bisa diambil oleh Google Cast."""
    try:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=config.AUDIO_SERVE_DIR)
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", config.AUDIO_SERVE_PORT), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"[AudioServer] ✓ Serving {config.AUDIO_SERVE_DIR} on port {config.AUDIO_SERVE_PORT}\n")
        return httpd
    except Exception as ex:
        print(f"[AudioServer] ✗ Failed to start: {ex}\n")
        return None


def _cleanup_old_cast_files(keep_last=5):
    try:
        files = sorted(
            (os.path.join(config.AUDIO_SERVE_DIR, f) for f in os.listdir(config.AUDIO_SERVE_DIR)),
            key=os.path.getmtime,
        )
        for f in files[:-keep_last]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


def cast_audio(audio_bytes, interrupt_event=None):
    """Simpan audio ke folder yang di-serve, lalu suruh speaker Google Cast memutarnya
    lewat HA. SENGAJA nunggu (time.sleep) kira-kira selama durasi audio-nya sebelum
    return — soalnya media_player.play_media cuma MEMICU playback (langsung balik
    begitu speaker mulai muter), TIDAK nunggu sampai speaker selesai ngomong. Tanpa
    nunggu ini, kode lanjut ke langkah berikutnya (mis. mic aktif lagi buat follow-up)
    SEMENTARA speaker Cast masih ngomong — mic bisa nangkep suara Luno sendiri sebagai
    'command' baru (masalah klasik echo/feedback di voice assistant).

    Bug fix (barge-in tidak motong Google Cast): `interrupt_event` OPSIONAL
    (default None, semua caller lama nggak berubah). Cast speaker SAMA SEKALI
    nggak lewat sd.play()/sd.wait() (beda dari play_audio()/_play_on_device()),
    jadi sd.stop() yang dipanggil barge-in NGGAK ADA efeknya ke sini sama
    sekali. Kalau dikasih interrupt_event, nunggu wait_time-nya di-loop per
    0.1 detik (bukan 1 time.sleep(wait_time) penuh) biar bisa ketauan
    diinterupsi di tengah jalan, DAN beneran ngirim service call
    media_player.media_stop ke HA buat bener-bener nyuruh speaker Cast-nya
    berhenti (satu-satunya cara valid buat 'motong' Cast — nggak ada stream
    lokal yang bisa di-stop())."""
    filename = f"luno_{uuid.uuid4().hex}.wav"
    filepath = os.path.join(config.AUDIO_SERVE_DIR, filename)
    try:
        with open(filepath, "wb") as f:
            f.write(audio_bytes)
    except Exception as ex:
        print(f"[Cast] ✗ Failed to save audio: {ex}\n")
        return

    # Estimasi durasi audio dari WAV header-nya sendiri (nggak butuh library tambahan).
    duration = 0.0
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            duration = wf.getnframes() / float(wf.getframerate())
    except Exception:
        pass  # gagal baca durasi -> tetep lanjut, cuma nggak nunggu presisi

    # Kirim SEKALIAN ke device tambahan (VB-Cable dkk) — dulu ini CUMA ada di
    # play_audio() (jalur speaker desktop), padahal cast_audio() adalah jalur yang
    # SAMA SEKALI TERPISAH (audio dikirim ke speaker Cast lewat HA, nggak pernah
    # lewat sd.play() sama sekali) — jadi VB-Cable/VNyan sebelumnya kelewat total
    # kalau kamu pakai AUDIO_OUTPUT_MODE=cast. Sekarang jalan paralel di thread
    # sendiri, nggak nunggu/nge-block proses cast ke speaker Cast-nya.
    secondary_thread = None
    if config.SECONDARY_AUDIO_DEVICE:
        try:
            data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
            device = config.SECONDARY_AUDIO_DEVICE
            try:
                device = int(device)
            except ValueError:
                pass  # bukan angka, biarin sebagai nama device (string)
            secondary_thread = threading.Thread(
                target=_play_on_device, args=(data, samplerate, device, interrupt_event), daemon=True
            )
            secondary_thread.start()
        except Exception as ex:
            print(f"[Cast] ✗ Gagal siapin audio buat secondary device: {ex}\n")

    local_ip = get_local_ip()
    url = f"http://{local_ip}:{config.AUDIO_SERVE_PORT}/{filename}"
    print(f"[Cast] → {config.CAST_ENTITY_ID}: {url}")

    if ha_listener.ha_loop and ha_client.connected:
        try:
            future = asyncio.run_coroutine_threadsafe(
                ha_client.call_service(
                    "media_player", "play_media", config.CAST_ENTITY_ID,
                    {"media_content_id": url, "media_content_type": "music"}
                ),
                ha_listener.ha_loop,
            )
            future.result(timeout=15)
            # Nunggu kira-kira sampai speaker Cast selesai ngomong + margin jaringan,
            # SEBELUM balik ke pemanggil — ini yang mencegah mic aktif lagi terlalu cepat.
            wait_time = duration + config.CAST_PLAYBACK_MARGIN
            print(f"[Cast] ⏳ Menunggu playback selesai (~{wait_time:.1f}s)...\n")

            elapsed = 0.0
            step = 0.1
            interrupted_mid_cast = False
            while elapsed < wait_time:
                if interrupt_event is not None and interrupt_event.is_set():
                    interrupted_mid_cast = True
                    break
                time.sleep(step)
                elapsed += step

            if interrupted_mid_cast:
                try:
                    stop_future = asyncio.run_coroutine_threadsafe(
                        ha_client.call_service("media_player", "media_stop", config.CAST_ENTITY_ID),
                        ha_listener.ha_loop,
                    )
                    stop_future.result(timeout=5)
                    print(f"[Cast] ✓ Interrupted - sent media_stop to {config.CAST_ENTITY_ID}\n")
                except Exception as ex:
                    print(f"[Cast] ✗ Failed to stop Cast playback: {ex}\n")
        except Exception as ex:
            print(f"[Cast] ✗ Error: {ex}\n")

    if secondary_thread:
        secondary_thread.join(timeout=5)
    else:
        print("[Cast] ✗ HA belum terkoneksi, cast dilewati\n")

    _cleanup_old_cast_files()


def _request_tts_audio(speech_text):
    """Panggil backend TTS yang aktif (config.TTS_ENGINE) dan balikin raw WAV
    bytes, atau None kalau gagal/status bukan 200. Semua percabangan
    engine-specific (GPT-SoVITS vs F5-TTS) hidup DI SINI SAJA — speak() sendiri
    nggak perlu tahu bedanya."""
    if config.TTS_ENGINE == "f5tts":
        payload = {
            "ref_audio_path": config.REFERENCE_AUDIO,
            "ref_text": config.REFERENCE_TEXT,
            "gen_text": speech_text,
        }
        try:
            # Timeout lebih panjang dari GPT-SoVITS — inference diffusion F5-TTS
            # lebih lambat, apalagi kalau server jalan di CPU (lihat SETUP_F5TTS.md).
            res = requests.post(f"{config.F5TTS_HOST}/tts", json=payload, timeout=120)
        except Exception:
            return None
    else:
        payload = {
            "ref_audio_path": config.REFERENCE_AUDIO,
            "prompt_text": config.REFERENCE_TEXT,
            "prompt_lang": "auto",
            "text": speech_text,
            "text_lang": "auto",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
        }
        try:
            res = requests.post(f"{config.GPTSOVITS_HOST}/tts", json=payload, timeout=60)
        except Exception:
            return None

    if res.status_code == 200:
        return res.content
    return None


def speak(text, interrupt_event=None):
    """Bug fix (barge-in tidak motong SECONDARY_AUDIO_DEVICE): `interrupt_event`
    itu parameter BARU, OPSIONAL (default None) — semua caller lama (reminder,
    timer terjadwal, process_and_respond() versi non-barge-in, dst) tetap jalan
    identik tanpa perubahan apa pun, cukup nggak nyertain argumen ini. Cuma
    process_and_respond_with_bargein() yang ngirim `state.interrupted` di sini,
    supaya _play_on_device() (device sekunder, lihat komentarnya sendiri di
    bawah) BISA ikut kepotong pas barge-in kejadian — sebelumnya cuma sd.play()
    di device UTAMA yang kepotong (lewat sd.stop() globalnya sounddevice),
    device sekunder (VB-Cable dkk, kalau SECONDARY_AUDIO_DEVICE di-set) TERUS
    lanjut muter penuh karena de-facto punya stream sendiri yang independen."""
    speech_text = tts_text.clean_for_speech(text)
    try:
        audio_bytes = _request_tts_audio(speech_text)
        if audio_bytes:
            # Broadcast ke avatar 3D (kalau ada yang connect) — no-op aman kalau
            # avatar.html belum dibuka sama sekali, nggak nge-block audio biasa.
            # Caption tetap pakai teks ASLI (bukan speech_text) — biar link/dash yang
            # dibuang dari suara tetap kebaca kalau user lagi liat layar avatar.
            try:
                avatar_dispatch.send_speech(text, audio_bytes, expressions.guess_expression(text))
            except Exception:
                pass

            # Sinyal "mulai ngomong" (sekalian mungkin trigger gesture "lagi
            # ngejelasin" di VNyan) — dibungkus try/finally biar "selesai ngomong"
            # TETAP terkirim walau playback-nya sendiri gagal di tengah jalan.
            avatar_dispatch.send_speaking(True)
            try:
                if config.AUDIO_OUTPUT_MODE == "cast":
                    cast_audio(audio_bytes, interrupt_event=interrupt_event)
                else:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp.write(audio_bytes)
                        tmp_path = tmp.name
                    play_audio(tmp_path, interrupt_event=interrupt_event)
                    try:
                        os.remove(tmp_path)
                    except Exception as ex:
                        print(f"[Audio] ✗ Gagal hapus temp file {tmp_path}: {ex}\n")
            finally:
                avatar_dispatch.send_speaking(False)

            # Jeda singkat SETELAH audio selesai (di mode apa pun) — buffer tambahan
            # biar mic (kalau abis ini aktif lagi buat follow-up) nggak kepancing
            # gema/delay ruangan dari suara Luno sendiri barusan.
            time.sleep(config.POST_SPEECH_COOLDOWN)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as ex:
        print(f"[Luno] ✗ speak() gagal: {ex}\n")


def _play_on_device(data, samplerate, device, interrupt_event=None):
    """Muter audio ke 1 device SPESIFIK pakai stream independen sendiri — sengaja
    TIDAK pakai sd.play()/sd.wait() (yang berbagi 1 'current stream' global), biar
    nggak saling potong sama playback device utama yang jalan bebarengan di thread lain.

    Bug fix (barge-in tidak motong SECONDARY_AUDIO_DEVICE): justru KARENA stream
    ini independen dari stream utama, sd.stop() (yang dipanggil barge-in buat
    motong device UTAMA) sama sekali nggak ada efeknya di sini — kalau user
    dengerin lewat device sekunder ini (mis. VB-Cable ke soundcard/monitor),
    suaranya bakal TERUS lanjut muter penuh walau console udah bilang
    "interrupted". `interrupt_event` (kalau dikasih) bikin ini ikut bisa
    berhenti: nulis datanya per-potong kecil (~100ms), bukan sekaligus penuh
    lewat 1 panggilan stream.write(data) yang blocking, biar ada titik buat
    ngecek & berhenti di tengah jalan."""
    try:
        channels = data.shape[1] if data.ndim > 1 else 1
        with sd.OutputStream(samplerate=samplerate, device=device, channels=channels) as stream:
            if interrupt_event is None:
                stream.write(data)
            else:
                chunk_size = max(1, int(samplerate * 0.1))
                for start in range(0, len(data), chunk_size):
                    if interrupt_event.is_set():
                        break
                    stream.write(data[start:start + chunk_size])
    except Exception as ex:
        print(f"[Audio] ✗ Gagal kirim ke secondary device ({device}): {ex}\n")


def play_audio(path, interrupt_event=None):
    try:
        data, samplerate = sf.read(path, dtype='float32')

        # Kalau di-set (biasanya buat kasus VNyan: virtual audio cable biar lipsync
        # bawaan VNyan bisa "denger" suara Luno), putar SEKALIAN ke device tambahan
        # ini di thread terpisah — device bisa nama (string) atau index (int).
        secondary_thread = None
        if config.SECONDARY_AUDIO_DEVICE:
            device = config.SECONDARY_AUDIO_DEVICE
            try:
                device = int(device)
            except ValueError:
                pass  # bukan angka, biarin sebagai nama device (string)
            secondary_thread = threading.Thread(
                target=_play_on_device, args=(data, samplerate, device, interrupt_event), daemon=True
            )
            secondary_thread.start()

        sd.play(data, samplerate)
        sd.wait()

        if secondary_thread:
            secondary_thread.join(timeout=5)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as ex:
        print(f"[Audio] ✗ play_audio() gagal untuk '{path}': {ex}\n")


# ─────────────────────────────────────────────
#  BARGE-IN (dengerin "stop"/"batal" SELAGI Luno mikir/ngomong)
# ─────────────────────────────────────────────
#
# main.py ini jalur produksi LAMA yang tetep dipakai tiap hari (lihat
# main_runtime_demo.py buat arsitektur event-driven yang baru) — jadi fix ini
# SENGAJA ditulis minimal & aditif: nggak ada satu pun baris di Luno_Brain(),
# speak(), play_audio(), atau _request_tts_audio() yang diubah. Yang ditambah
# cuma LAPISAN concurrency di luarnya: sambil Luno "mikir" (nunggu GPT) atau
# "ngomong" (nunggu sd.wait()), 1 thread background tetep dengerin mic lewat
# jalur Whisper yang SAMA (transcribe_audio()) buat nangkep kata interupsi.
#
# Mekanisme cut-off suara pas lagi speaking: sounddevice cuma punya SATU
# "current stream" global per proses (lihat komentar _play_on_device() di
# atas) — jadi sd.stop() yang dipanggil dari thread lain bakal motong
# sd.play()/sd.wait() yang lagi jalan di play_audio(), sama sekali nggak perlu
# ubah play_audio() itu sendiri.
#
# Mekanisme "stop selagi mikir": beda ceritanya — panggilan
# client.chat.completions.create() di dalam Luno_Brain() itu BLOCKING dan
# non-streaming, nggak ada titik pembatalan bersih di tengah jalan (beda dari
# arsitektur baru yang streaming). Jadi bukannya benar-benar membatalkan
# request HTTP-nya, begitu interupsi kedengeran selagi mikir, balasannya
# nanti (begitu GPT-nya kelar) SENGAJA dibuang / nggak diomongin — persis pola
# "suppress stale reply" yang sama yang sudah dipakai di BehaviorTreeModule
# arsitektur baru (luno/barge_in/manager.py).
#
# Daftar kata interupsi SENGAJA nggak nge-import dari luno/barge_in atau
# luno/wake_session (main.py nggak pernah nyentuh package arsitektur baru sama
# sekali) — disalin independen di sini, baca env var yang SAMA
# (BARGE_IN_INTERRUPT_WORDS) kalau di-set, biar 1 deployment cukup 1x
# konfigurasi buat kedua runtime, tanpa cross-import.
_DEFAULT_BARGE_IN_WORDS = [
    "stop", "cancel", "pause", "wait", "hold on", "enough", "that's enough",
    "never mind", "nevermind", "actually",
    "batal", "sudah", "diam dulu", "tunggu", "sebentar",
]


def _barge_in_words():
    raw = os.getenv("BARGE_IN_INTERRUPT_WORDS")
    if raw and raw.strip():
        return [w.strip().lower() for w in raw.split(",") if w.strip()]
    return _DEFAULT_BARGE_IN_WORDS


def _looks_like_interrupt(text):
    """Bug fix: cocokin pakai regex word-boundary (\\b), BUKAN padding spasi
    manual seperti versi awal. Faster-Whisper (transcribe_audio()) HAMPIR
    SELALU nambahin tanda baca ke hasil transkrip (mis. "Stop." bukan
    "stop") — versi awal (f" {w} " in f" {norm} ") gagal total buat kasus
    ini karena nggak ada spasi SETELAH "stop" sebelum titiknya, jadi kata
    yang sebenarnya cocok kehitung nggak cocok. \\b menganggap tanda baca
    SAMA seperti batas kata (non-word char), jadi "Stop.", "Stop!", "stop,"
    dst semua tetap kena."""
    if not text:
        return False
    norm = text.strip().lower()
    if not norm:
        return False
    return any(re.search(r"\b" + re.escape(w) + r"\b", norm) for w in _barge_in_words())


class _BargeInState:
    """Nge-track apakah Luno lagi 'sibuk' (mikir dan/atau ngomong) buat 1
    giliran, plus flag `interrupted` yang di-set thread listener kalau
    kedengeran kata stop. `speaking` SENGAJA di-set True SEBELUM `thinking`
    di-set False (lihat process_and_respond_with_bargein) biar nggak pernah
    ada celah sesaat is_busy()==False di antara dua fase itu — sama persis
    bug 'thinking->speaking gap' yang pernah kejadian & diperbaiki di
    BargeInModule arsitektur baru (luno/barge_in/manager.py)."""

    def __init__(self):
        self.thinking = False
        self.speaking = False
        self.interrupted = threading.Event()
        self._lock = threading.Lock()

    def begin_thinking(self):
        with self._lock:
            self.thinking = True

    def end_thinking(self):
        with self._lock:
            self.thinking = False

    def begin_speaking(self):
        with self._lock:
            self.speaking = True

    def end_speaking(self):
        with self._lock:
            self.speaking = False

    def is_busy(self):
        with self._lock:
            return self.thinking or self.speaking

    def request_interrupt(self):
        self.interrupted.set()
        sd.stop()  # motong sd.play()/sd.wait() yang lagi jalan di play_audio() (no-op aman kalau lagi nggak ada yang muter)


def _barge_in_listener(state):
    """Jalan di thread daemon terpisah SELAMA state.is_busy() (mikir ATAU
    ngomong). Rekam potongan-potongan pendek lewat sr.Microphone() (device
    yang SAMA dipakai _listen_once(), dibuka/ditutup tiap iterasi biar nggak
    'nahan' device terus-terusan) lalu transkrip pakai transcribe_audio() —
    jalur Whisper yang SAMA dipakai buat command biasa. Kalau hasilnya cocok
    kata interupsi, panggil state.request_interrupt() lalu berhenti.

    Bug fix: versi awal manggil recognizer.adjust_for_ambient_noise() ULANG
    tiap iterasi loop — termasuk PAS Luno lagi ngomong keras lewat speaker.
    adjust_for_ambient_noise() ngukur suara TERKINI dan jadiin itu baseline
    "hening", jadi begitu Luno mulai ngomong, threshold-nya ikut kekerek naik
    tinggi banget (nganggep suara Luno = "ambient"), sampai suara user yang
    normal buat bilang "stop" jadi kedengeran nggak cukup lebih keras dari
    baseline yang salah itu -> nggak pernah ke-trigger listen()-nya sama
    sekali. Fix-nya: kalibrasi SEKALI SAJA di awal (kondisi mic masih hening,
    Luno belum mulai ngomong), matiin dynamic_energy_threshold biar recognizer
    juga nggak diam-diam nge-drift naikin threshold sendiri tiap kali abis
    denger suara Luno lewat listen().

    Catatan jujur yang TETAP berlaku walau kalibrasi ini dibenerin: karena
    speaker & mic jalan bebarengan (full-duplex, TANPA echo cancellation —
    sama seperti keterbatasan arsitektur baru), mic ini bisa aja ikut
    ke-rekam & ke-transkrip suara Luno sendiri lewat speaker (buang-buang
    siklus Whisper, tapi harusnya tetap nggak match kata interupsi kalau
    kalimat Luno-nya sendiri nggak mengandung kata itu). Ini trade-off yang
    diterima, bukan bug yang coba diperbaiki fix minimal ini — kalau
    interupsi masih suka kelewat gara-gara ini, coba pelanin volume speaker
    atau jauhin mic dari speaker."""
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = False

    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.3)
        print(f"[BargeIn] 👂 Listening for interrupt words (threshold={recognizer.energy_threshold:.0f})...\n")
    except Exception as ex:
        print(f"[BargeIn] ✗ Could not calibrate mic, interrupt listening disabled for this turn: {ex}\n")
        return

    while state.is_busy():
        try:
            with sr.Microphone() as source:
                try:
                    audio = recognizer.listen(source, timeout=0.6, phrase_time_limit=4)
                except sr.WaitTimeoutError:
                    continue
        except Exception as ex:
            print(f"[BargeIn] ✗ Mic error, retrying: {ex}\n")
            time.sleep(0.2)
            continue

        if not state.is_busy():
            return

        try:
            text = transcribe_audio(audio)
        except Exception as ex:
            print(f"[BargeIn] ✗ Transcribe error: {ex}\n")
            continue

        if not text:
            continue

        if _looks_like_interrupt(text):
            print(f"[BargeIn] ✓ Interrupt detected: {text!r}\n")
            state.request_interrupt()
            return
        else:
            print(f"[BargeIn] · Heard (no match): {text!r}\n")


# ─────────────────────────────────────────────
#  PROCESS INPUT
# ─────────────────────────────────────────────

def process_and_respond(user_input):
    """Return True kalau SETELAH balasan ini Luno lagi nunggu jawaban user (pending
    action, mis. abis nanya 'mau main apa?') — sinyal ke pemanggil (mode_voice/
    mode_wake_word) biar otomatis dengerin lagi TANPA perlu tekan Enter/wake word
    ulang, biar berasa kayak obrolan nyambung, bukan harus 'panggil' Luno tiap giliran."""
    if not user_input or not user_input.strip():
        return False

    # Kasih tau avatar 3D Luno lagi "mikir" (no-op aman kalau avatar.html belum
    # dibuka). Dibungkus try/finally biar sinyal "selesai mikir" TETAP terkirim
    # walau Luno_Brain() error di tengah jalan — avatar nggak nyangkut di pose mikir.
    avatar_dispatch.send_thinking(True)
    try:
        reply = Luno_Brain(user_input.strip())
    finally:
        avatar_dispatch.send_thinking(False)

    speak(reply)
    return pending_actions.get_pending() is not None


def process_and_respond_with_bargein(user_input):
    """Sama persis kontraknya kayak process_and_respond() (return True kalau
    lagi nunggu follow-up) — bedanya di sini mic TETAP aktif dengerin (lewat
    _barge_in_listener() di thread terpisah) SELAMA Luno mikir & ngomong, jadi
    bilang 'stop'/'batal' bisa motong Luno TANPA perlu ulang wake word.

    Ini SATU-SATUNYA hal baru dari fix ini — Luno_Brain()/speak()/play_audio()
    di atas nggak disentuh sama sekali, cuma dipanggil dari sini persis kayak
    sebelumnya."""
    if not user_input or not user_input.strip():
        return False

    state = _BargeInState()
    state.begin_thinking()
    listener_thread = threading.Thread(target=_barge_in_listener, args=(state,), daemon=True, name="luno-bargein-listener")
    listener_thread.start()

    avatar_dispatch.send_thinking(True)
    try:
        reply = Luno_Brain(user_input.strip())
    finally:
        avatar_dispatch.send_thinking(False)

    if state.interrupted.is_set():
        # Kepotong SELAGI MIKIR — client.chat.completions.create() di
        # Luno_Brain() nggak punya titik pembatalan bersih (blocking,
        # non-streaming), jadi balasan yang baru selesai dihitung ini SENGAJA
        # dibuang / nggak pernah diomongin (sama kayak pola "suppress stale
        # reply" di arsitektur baru), bukan diteruskan ke speak().
        state.end_thinking()
        print("[BargeIn] ✓ Interrupted while thinking - reply dropped\n")
        ack = "Oke." if _should_reply_indo(user_input.strip().lower()) else "Okay."
        speak(ack)
        return False

    # Set speaking SEBELUM clear thinking — hindari celah is_busy()==False di
    # antara dua fase (lihat docstring _BargeInState).
    state.begin_speaking()
    state.end_thinking()
    try:
        # interrupt_event=state.interrupted - bug fix: tanpa ini, sd.stop()
        # (dipanggil listener lewat state.request_interrupt()) cuma motong
        # device audio UTAMA. Kalau SECONDARY_AUDIO_DEVICE di-set (VB-Cable
        # dkk), device itu jalan lewat stream sendiri yang independen dan
        # TERUS lanjut muter penuh kalau nggak dikasih tau juga soal
        # interrupt-nya - lihat _play_on_device()'s own docstring.
        speak(reply, interrupt_event=state.interrupted)
    finally:
        state.end_speaking()

    if state.interrupted.is_set():
        # Kepotong SELAGI NGOMONG — sd.stop() (dipanggil listener) udah motong
        # audio-nya barusan lewat speak()/play_audio(). Jangan lanjut ke
        # follow-up chain, biar kontrol balik ke wake-word loop.
        print("[BargeIn] ✓ Interrupted while speaking - playback cut off\n")
        return False

    return pending_actions.get_pending() is not None


def _listen_once(recognizer, timeout=10, phrase_time_limit=15):
    """Rekam 1 giliran ucapan lewat mic & transkrip. Return teksnya, atau None
    kalau timeout/nggak kedengeran/nggak ngerti (supaya pemanggil tau kapan harus
    berhenti, bukan lanjut proses teks kosong)."""
    with sr.Microphone() as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.3)
        try:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        except sr.WaitTimeoutError:
            print("[STT] ✗ No audio detected\n")
            return None

    try:
        print("[STT] Processing (Faster-Whisper)...")
        text = transcribe_audio(audio)
        if not text:
            print("[STT] ✗ Could not understand\n")
            return None
        print(f"[You] {text}\n")
        return text
    except Exception as ex:
        print(f"[STT] ✗ Error: {ex}\n")
        return None


def _voice_turn_with_followup(recognizer, timeout=10, max_followups=3):
    """Rekam + proses 1 giliran percakapan. Kalau abis itu Luno lagi NUNGGU JAWABAN
    (mis. abis nanya 'mau main apa?' setelah script gaming mode jalan), OTOMATIS
    dengerin lagi tanpa perlu Enter/wake word ulang — mic aktif lagi sebentar buat
    nangkep jawabannya, sampai maksimal `max_followups` kali (jaga-jaga biar nggak
    berpotensi dengerin selamanya kalau ada bug yang bikin pending nggak ke-clear)."""
    text = _listen_once(recognizer, timeout=timeout)
    if text is None:
        return
    has_pending = process_and_respond_with_bargein(text)

    followups = 0
    while has_pending and followups < max_followups:
        followups += 1
        print("[Luno] 👂 Listening for your answer...\n")
        text = _listen_once(recognizer, timeout=8)
        if text is None:
            return
        has_pending = process_and_respond_with_bargein(text)


# ─────────────────────────────────────────────
#  MODE 1: TEXT INPUT
# ─────────────────────────────────────────────

def mode_text_input():
    print("\n" + "=" * 50)
    print("  TEXT MODE")
    print("  Examples:")
    print("    'turn on the light'")
    print("    'set brightness to 150'")
    print("    'change to blue'")
    print("    'turn off'")
    print("  'exit' to quit")
    print("=" * 50 + "\n")
    
    while True:
        try:
            user_input = input("[You] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Luno] Goodbye!")
            break
        if user_input.lower() == "exit":
            print("[Luno] Goodbye!")
            break
        if user_input:
            process_and_respond(user_input)


# ─────────────────────────────────────────────
#  SPEECH-TO-TEXT (Faster-Whisper — lokal/offline)
# ─────────────────────────────────────────────
#
# sr.Microphone tetap dipakai buat urusan rekam + deteksi hening (VAD sederhana
# bawaan speech_recognition), cuma bagian "audio → teks"-nya yang diganti dari
# recognizer.recognize_google() (butuh internet, kurang akurat) ke Faster-Whisper
# model "medium" yang jalan lokal dan jauh lebih akurat, termasuk untuk Bahasa Indonesia.

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    """Lazy-load model Whisper sekali saja (thread-safe). Load pertama akan
    men-download model kalau belum ada di cache lokal, jadi bisa makan waktu."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                print(
                    f"[STT] ⏳ Loading Faster-Whisper '{config.WHISPER_MODEL_SIZE}' "
                    f"({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})... (pertama kali bisa lama)\n"
                )
                _whisper_model = WhisperModel(
                    config.WHISPER_MODEL_SIZE,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                )
                print("[STT] ✓ Faster-Whisper siap\n")
    return _whisper_model


def transcribe_audio(audio: "sr.AudioData") -> str:
    """Transkrip sr.AudioData (hasil recording sr.Microphone) pakai Faster-Whisper.
    Return string kosong kalau tidak ada ucapan yang terdeteksi (setara UnknownValueError)."""
    model = get_whisper_model()

    tmp_path = None
    try:
        wav_bytes = audio_preprocess.preprocess_audio(audio.get_wav_data())
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name

        segments, _info = model.transcribe(
            tmp_path,
            language=config.WHISPER_LANGUAGE or None,  # None = auto-detect bahasa
            vad_filter=True,   # buang segmen hening/noise biar makin bersih
            beam_size=5,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ─────────────────────────────────────────────
#  MODE 2: VOICE (PRESS ENTER)
# ─────────────────────────────────────────────

def mode_voice():
    """⭐ ALWAYS WORKING - No dependencies"""
    recognizer = sr.Recognizer()
    
    print("\n" + "=" * 50)
    print("  VOICE MODE")
    print("  Instructions:")
    print("    1. Press ENTER to start recording")
    print("    2. Say your command")
    print("    3. Wait for response")
    print("    Type 'exit' to quit")
    print("=" * 50 + "\n")
    
    while True:
        try:
            cmd = input(">> Press ENTER to record (or type 'exit'): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Luno] Goodbye!")
            break
        
        if cmd.lower() == "exit":
            print("[Luno] Goodbye!")
            break
        
        if cmd:
            continue
        
        print("[STT] 🎤 Recording... (speak now)\n")
        _voice_turn_with_followup(recognizer)


# ─────────────────────────────────────────────
#  MODE 3: WAKE WORD ("Alexa")
# ─────────────────────────────────────────────

def mode_wake_word():
    """⭐ Offline local wake word detection using openWakeWord + Google STT for the command"""
    try:
        from openwakeword.model import Model
    except ImportError:
        print("[WakeWord] ✗ Package 'openwakeword' not installed.")
        print("           Run: pip install openwakeword\n")
        return

    SAMPLE_RATE = 16000
    CHUNK_SAMPLES = 1280  # openWakeWord expects 80ms chunks @ 16kHz
    THRESHOLD = 0.5

    print("\n" + "=" * 50)
    print("  WAKE WORD MODE (openWakeWord — offline)")
    print(f"  Wake word: '{config.WAKE_WORD.title()}'")
    print(f"  Say 'stop'/'batal' (etc.) any time while Luno is thinking or")
    print(f"  speaking to interrupt her — no need to repeat the wake word.")
    print("  Press Ctrl+C to quit")
    print("=" * 50 + "\n")

    try:
        oww_model = Model(wakeword_models=[config.WAKE_WORD], inference_framework="onnx")
    except Exception as ex:
        print(f"[WakeWord] ✗ Failed to load model '{config.WAKE_WORD}': {ex}")
        print("           If the .onnx file is missing, run:")
        print(f"           python -c \"import openwakeword.utils; openwakeword.utils.download_models(['{config.WAKE_WORD}'])\"\n")
        return

    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES)
        stream.start()
    except Exception as ex:
        print(f"[WakeWord] ✗ Microphone error: {ex}\n")
        return

    recognizer = sr.Recognizer()
    print(f"[Luno] 👂 Listening for wake word '{config.WAKE_WORD.title()}'...\n")

    try:
        while True:
            audio_chunk, _ = stream.read(CHUNK_SAMPLES)
            audio_chunk = audio_chunk.flatten()

            prediction = oww_model.predict(audio_chunk)
            score = prediction.get(config.WAKE_WORD, 0.0)

            if score < THRESHOLD:
                continue

            print(f"[Luno] ✓ Wake word detected! (score={score:.2f})")
            oww_model.reset()

            # Free the mic before handing it to speech_recognition
            stream.stop()
            print("[Luno] 🎤 Listening for your command...\n")
            try:
                _voice_turn_with_followup(recognizer, timeout=8)
            except Exception as ex:
                print(f"[STT] ✗ Error: {ex}\n")
            finally:
                stream.start()

            print(f"[Luno] 👂 Listening for wake word '{config.WAKE_WORD.title()}'...\n")

    except KeyboardInterrupt:
        print("\n[Luno] Goodbye!")
    except Exception as ex:
        print(f"[WakeWord] ✗ Error: {ex}\n")
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  AUDIO OUTPUT SELECTION
# ─────────────────────────────────────────────

def choose_audio_output():

    print("\n" + "=" * 70)
    print("  Pilih output suara:")
    print("  1️⃣  DESKTOP SPEAKER   - Suara diputar langsung dari komputer ini")
    print("  2️⃣  GOOGLE CAST       - Suara dikirim ke speaker Google Assistant/Cast")
    print("=" * 70)

    while True:
        choice = input("\nPilihan (1 atau 2): ").strip()
        if choice == "1":
            config.AUDIO_OUTPUT_MODE = "desktop"
            print("[Audio] ✓ Output: Desktop Speaker\n")
            break
        elif choice == "2":
            config.AUDIO_OUTPUT_MODE = "cast"
            print(f"[Audio] ✓ Output: Google Cast → {config.CAST_ENTITY_ID}")
            print("        (ganti target lewat .env: CAST_ENTITY_ID=media_player.xxx)\n")
            start_audio_server()
            break
        else:
            print("⚠️  Masukkan 1 atau 2")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    print("\n" + "=" * 70)
    print("   🌙 LUNO - Smart Home AI Assistant 🌙")
    print("=" * 70)
    
    # Start HA in background
    ha_thread = threading.Thread(target=ha_listener.run_ha_listener, args=(ha_client,), daemon=True)
    ha_thread.start()

    # Start avatar bridge in background — CUMA kalau backend-nya "web" (avatar.html).
    # Backend "vnyan" pakai OSC (client, bukan server), jadi nggak butuh thread ini.
    if config.AVATAR_BACKEND != "vnyan":
        avatar_thread = threading.Thread(target=avatar_bridge.run_avatar_bridge, daemon=True)
        avatar_thread.start()
    else:
        print(f"[Avatar] ✓ Backend VNyan aktif — kirim OSC ke {config.VNYAN_OSC_HOST}:{config.VNYAN_OSC_PORT}\n")
        if config.VNYAN_IDLE_MOTION:
            vnyan_idle.start()

    time.sleep(2)

    # Reminder yang belum "fired" pas Luno terakhir mati perlu dijadwalkan ULANG di
    # sini — task asyncio-nya hilang tiap restart, walau datanya sendiri persist di
    # reminders.json. ha_listener.ha_loop sudah pasti hidup di titik ini (disiapkan
    # sebelum loop koneksi HA-nya sendiri jalan, lihat ha_listener.py).
    pending_reminders = reminders.list_pending()
    if pending_reminders:
        print(f"[Reminders] ✓ Re-scheduling {len(pending_reminders)} pending reminder(s)\n")
        for entry in pending_reminders:
            _schedule_reminder(entry)

    choose_audio_output()
    
    print("\n" + "=" * 70)
    print("  Choose input mode:")
    print("  1️⃣  TEXT INPUT     - Type your commands")
    print("  2️⃣  VOICE INPUT    - Press ENTER to record")
    print(f"  3️⃣  WAKE WORD      - Say '{config.WAKE_WORD.title()}' to activate")
    print("=" * 70)
    
    while True:
        choice = input("\nChoice (1, 2 or 3): ").strip()
        if choice == "1":
            mode_text_input()
            break
        elif choice == "2":
            mode_voice()
            break
        elif choice == "3":
            mode_wake_word()
            break
        else:
            print("⚠️  Please enter 1, 2 or 3")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[Luno] Goodbye!")
    except Exception as ex:
        print(f"\n[Error] {ex}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            memory.summarize_and_archive_session(client)
        except Exception as ex:
            print(f"[Memory] ✗ Gagal merangkum sesi saat exit: {ex}")
        try:
            if ha_client.connected:
                asyncio.run(ha_client.disconnect())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as ex:
            print(f"[HA] ✗ Gagal disconnect saat exit: {ex}")