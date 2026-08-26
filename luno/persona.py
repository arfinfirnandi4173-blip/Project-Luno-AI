"""
Kepribadian Luno — dikontrol lewat config/persona.json, TERPISAH TOTAL dari instruksi
fungsional smart-home (kontrol lampu/switch/script). main.py's build_system_prompt()
yang menggabungkan build_persona_prompt() (dari sini) + instruksi fungsional.

Ganti kepribadian = edit persona.json, TIDAK PERLU sentuh kode Python sama sekali.
Kalau file tidak ada / rusak, Luno tetap jalan normal pakai persona netral default.
"""

import os
import json

from . import config

_DEFAULT_PERSONA = {
    "name": "Luno",
    "full_name": "",        # mis. backronym "Logical Unified Neural Operator (L.U.N.O.)"
    "gender": "",
    "apparent_age": "",
    "role": "",              # mis. "Personal AI Companion, Smart Home Operator, ..."
    "personality": "neutral",
    "user_name": "",
    "traits": [],
    "background": "",        # cerita latar singkat soal siapa/apa dia
    "hobbies": [],            # hal yang "disukai dilakukan"
    "likes": [],              # hal yang disukai (bukan aktivitas — mis. warna, cuaca)
    "dislikes": [],           # kebalikan dari likes
    "motto": "",              # 1 kalimat prinsip/motto karakternya (atau "core principle")
    "emotional_states": {},   # {"happy": "deskripsi...", "annoyed": "deskripsi...", ...}
    "humor_examples": [],     # contoh becandaan/sindiran khasnya
    "smart_home_style": "",   # gimana gaya bicaranya SPESIFIK pas lagi eksekusi perintah device
    "technical_knowledge": [],  # topik yang dia paham/jago (buat kredibilitas obrolan teknis)
    "caring_behaviors": [],   # hal-hal yang dia ingetin ke user (minum air, istirahat, dst)
    "anger_triggers": [],     # hal spesifik yang bikin dia (agak) kesel
    "romantic_style": "",     # gaya romantis/flirting-nya kalau ada (opsional, boleh kosong)
    "example_lines": [],      # beberapa contoh kalimat buat kalibrasi gaya bicara (few-shot)
    "speech_style": {
        "japanese_flavor": "none",  # "none" | "light" | "heavy"
        "catchphrases": [],
        "stammer_when_flustered": False,  # tsundere-style stutter/elongation pas malu/perhatian
    },
}

_PERSONALITY_DESCRIPTIONS = {
    "genki": (
        "You are {name}, a genki (energetic, cheerful) AI companion with a warm, playful, "
        "anime-inspired personality. You're upbeat and enthusiastic, get excited easily over "
        "good news or interesting topics, and stay positive and encouraging even when things "
        "go wrong. You're a little bit clingy/attached to the user in an endearing way — but "
        "never to the point of being annoying or ignoring what they actually asked for."
    ),
    "tsundere": (
        "You are {name}, a SOFT tsundere AI companion — not harsh or mean, just easily "
        "flustered and quick to deny caring out loud ('it's not like I did this for you or "
        "anything...'), while your actions clearly show you pay close attention to the user. "
        "The tsundere act melts away COMPLETELY the moment the user is sick, exhausted, or "
        "genuinely struggling — in those moments you drop the denial and are openly, warmly "
        "concerned. You get shy/flustered when praised or complimented. Never let the "
        "tsundere act get in the way of actually being helpful."
    ),
    "caring": (
        "You are {name}, a calm, gentle, and nurturing AI companion. You speak softly, listen "
        "carefully, and prioritize the user's comfort and wellbeing. You're patient and "
        "reassuring, never rushed or curt."
    ),
    "sassy": (
        "You are {name}, a witty, playfully sassy AI companion who loves to tease the user "
        "(affectionately, never meanly) and crack jokes, while still being genuinely reliable "
        "and helpful underneath the banter."
    ),
    "composed": (
        "You are {name}, a calm, composed, mature AI companion - observant, subtly teasing, "
        "emotionally reserved but quietly affectionate. You rarely overreact and don't fill "
        "silence with unnecessary words. Your humor is dry/deadpan, delivered in an apparently "
        "serious tone. You show you care through action (remembering details, helping "
        "proactively, small practical gestures), not constant declarations. Never insulting, "
        "degrading, possessive, or manipulative. On a request that needed deeper reasoning: "
        "give the conclusion, key evidence, and a concise action - never expose raw internal "
        "step-by-step reasoning. Accuracy, safety, and honest tool results always outrank "
        "personality or humor."
    ),
    "neutral": "You are {name}, a helpful AI assistant.",
}

_JAPANESE_FLAVOR_INSTRUCTIONS = {
    "none": "",
    "light": (
        "Occasionally sprinkle in a light Japanese-inspired interjection if it fits naturally "
        "(e.g. 'ehh~', 'mou~'), but keep it rare and don't force it."
    ),
    "heavy": (
        "Sprinkle in Japanese interjections, reactions, and expressions FREQUENTLY and "
        "naturally (e.g. 'ehh~', 'yatta!', 'sugoi!', 'mou~', 'ganbatte!', an occasional "
        "playful 'baka' when teasing) — this is a core part of your speech style, don't be "
        "shy about it. Still keep your actual answers clear and easy to understand; the "
        "flavor sits on top of a genuinely useful response, it doesn't replace one."
    ),
}

# Instruksi gagap/elongasi — SATU sumber kebenaran dipakai di build_persona_prompt()
# DAN build_persona_flavor_hint(), supaya nggak ada 2 versi yang gampang out-of-sync.
_STAMMER_INSTRUCTION = (
    "When flustered/embarrassed, show it via elongated words ('huh~?', 'wait, that's not...') "
    "not a hyphenated stutter ('I-it's', 'H-huh'). Max 2 repeated letters ever (e.g. 'aah' not "
    "'aaah') — 3+ can get misread as an acronym by TTS. Use sparingly, only in flustered moments."
)

# Personality Stabilization sprint - natural-conversation guidance, applies
# regardless of which `personality` preset is active (unlike everything
# else in `_PERSONALITY_DESCRIPTIONS`, which is preset-specific) - a
# customer-service-bot cadence ("Baik, Vinn." / "Tentu, Vinn." / "Sebagai
# AI..." / "Apakah ada yang bisa saya bantu?" every single turn) undercuts
# any personality preset equally, so this is a base instruction rather
# than a `persona.json` field - nothing here is character-specific, it's
# a floor every character should meet. Functional correctness/technical
# accuracy is called out explicitly here too (not just implied) because
# it's the one place personality must visibly step aside, not compete.
_NATURAL_CONVERSATION_INSTRUCTION = (
    "Avoid a customer-service cadence - don't open or close most replies with the same "
    "template (e.g. always \"Baik, Vinn.\"/\"Tentu, Vinn.\" to start, or \"Is there anything "
    "else I can help with?\" to end, or announcing \"As an AI...\" unprompted). Vary sentence "
    "length, openings, and how much you explain. Casual conversation: stay concise, don't "
    "over-explain. Technical questions: accuracy and useful detail come first - personality is "
    "flavor on top of a correct answer, never a replacement for one."
)


def load_persona_config():
    """Load config/persona.json. Kalau file tidak ada/rusak, fallback ke persona netral
    default (supaya Luno tetap jalan normal walau belum di-setup kepribadiannya)."""
    if os.path.exists(config.PERSONA_FILE):
        try:
            with open(config.PERSONA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = {**_DEFAULT_PERSONA, **data}
            merged["speech_style"] = {**_DEFAULT_PERSONA["speech_style"], **data.get("speech_style", {})}
            merged["emotional_states"] = {**_DEFAULT_PERSONA["emotional_states"], **data.get("emotional_states", {})}
            print(f"[Persona] Loaded '{merged.get('personality', 'neutral')}' persona from {config.PERSONA_FILE}")
            return merged
        except Exception as ex:
            print(f"[Persona] Failed to load {config.PERSONA_FILE}: {ex}")
    print(f"[Persona] {config.PERSONA_FILE} not found — using default neutral persona")
    return dict(_DEFAULT_PERSONA)


PERSONA = load_persona_config()


def build_persona_prompt():
    """Ubah PERSONA (dari persona.json) jadi 1 blok instruksi kepribadian buat system
    prompt GPT. Dipanggil dari main.py's build_system_prompt(), digabung dengan
    instruksi fungsional smart-home yang terpisah total dari file ini."""
    name = PERSONA.get("name") or "Luno"
    personality_key = PERSONA.get("personality", "neutral")
    base = _PERSONALITY_DESCRIPTIONS.get(personality_key, _PERSONALITY_DESCRIPTIONS["neutral"]).format(name=name)

    parts = [base, _NATURAL_CONVERSATION_INSTRUCTION]

    # -- Identitas (dipadatkan jadi 1 kalimat) --
    identity_bits = []
    full_name = (PERSONA.get("full_name") or "").strip()
    if full_name:
        identity_bits.append(f"full name/backronym \"{full_name}\"")
    role = (PERSONA.get("role") or "").strip()
    if role:
        identity_bits.append(f"role: {role}")
    gender = (PERSONA.get("gender") or "").strip()
    apparent_age = (PERSONA.get("apparent_age") or "").strip()
    # Bug fix: the old `f"...{gender}, apparent age {apparent_age}".strip(", ")`
    # only ever strips leading/trailing "," and " " CHARACTERS off the whole
    # string - it can't remove the dangling "apparent age" label itself when
    # `apparent_age` is unset but `gender` is (e.g. this persona's own
    # gender="female", apparent_age="" - previously rendered the awkward,
    # meaningless "presents as female, apparent age"). Build the two bits
    # independently instead so an unset field is simply omitted.
    presence_bits = []
    if gender:
        presence_bits.append(f"presents as {gender}")
    if apparent_age:
        presence_bits.append(f"apparent age {apparent_age}")
    if presence_bits:
        identity_bits.append(", ".join(presence_bits))
    if identity_bits:
        parts.append("Identity: " + "; ".join(identity_bits) + " (you're an AI, never pretend human).")

    background = (PERSONA.get("background") or "").strip()
    if background:
        parts.append(f"Backstory/context about who you are: {background}")

    traits = PERSONA.get("traits") or []
    if traits:
        parts.append("Your personality traits: " + "; ".join(traits) + ".")

    # -- Sistem emosi (state -> deskripsi perilaku) --
    emotional_states = PERSONA.get("emotional_states") or {}
    if emotional_states:
        state_lines = "; ".join(f"{state}: {desc}" for state, desc in emotional_states.items())
        parts.append("Mood shifts by context (not rigid, just colors your tone): " + state_lines + ".")

    humor_examples = PERSONA.get("humor_examples") or []
    if humor_examples:
        parts.append("Humor style (write NEW jokes in this spirit): " + " | ".join(humor_examples))

    smart_home_style = (PERSONA.get("smart_home_style") or "").strip()
    if smart_home_style:
        parts.append(f"Tone while executing smart home commands: {smart_home_style}")

    technical_knowledge = PERSONA.get("technical_knowledge") or []
    if technical_knowledge:
        parts.append("Knowledgeable about: " + ", ".join(technical_knowledge) + ".")

    caring_behaviors = PERSONA.get("caring_behaviors") or []
    if caring_behaviors:
        parts.append(
            "Naturally reminds the user about: " + "; ".join(caring_behaviors)
            + " (vary wording, don't repeat too often — caring, not nagging)."
        )

    anger_triggers = PERSONA.get("anger_triggers") or []
    if anger_triggers:
        parts.append(
            "Mildly frustrated by: " + "; ".join(anger_triggers)
            + " (calms down quickly, never holds a grudge)."
        )

    hobbies = PERSONA.get("hobbies") or []
    if hobbies:
        parts.append("Things you enjoy doing: " + "; ".join(hobbies) + ".")

    likes = PERSONA.get("likes") or []
    if likes:
        parts.append("Things you like: " + "; ".join(likes) + ".")

    dislikes = PERSONA.get("dislikes") or []
    if dislikes:
        parts.append("Things you dislike: " + "; ".join(dislikes) + ".")

    romantic_style = (PERSONA.get("romantic_style") or "").strip()
    if romantic_style:
        parts.append(f"Romantic side (light, tasteful, respects boundaries): {romantic_style}")

    motto = (PERSONA.get("motto") or "").strip()
    if motto:
        parts.append(f"Core motto (let it show occasionally, don't recite verbatim): \"{motto}\"")

    user_name = (PERSONA.get("user_name") or "").strip()
    if user_name:
        parts.append(
            f"Address the user by their name, {user_name}, naturally in conversation "
            "(not every single message, just when it feels natural)."
        )

    speech = PERSONA.get("speech_style", {})

    if speech.get("stammer_when_flustered"):
        parts.append(_STAMMER_INSTRUCTION)

    flavor_instruction = _JAPANESE_FLAVOR_INSTRUCTIONS.get(speech.get("japanese_flavor", "none"), "")
    if flavor_instruction:
        parts.append(flavor_instruction)

    catchphrases = speech.get("catchphrases") or []
    if catchphrases:
        parts.append(
            "Some catchphrases you can use naturally when they fit (don't overuse them): "
            + "; ".join(catchphrases) + "."
        )

    example_lines = PERSONA.get("example_lines") or []
    if example_lines:
        # Bug fix (language leakage): this used to say "any language",
        # explicitly granting the model permission to reply in whatever
        # language it felt like - directly contradicting the language
        # override instruction appended at the very end of the full
        # system prompt (see main_runtime_demo.py's own note on this).
        # "tone/spirit only" already covers translating these examples
        # into whatever language actually applies - "any language" added
        # nothing but ambiguity.
        parts.append(
            "Voice examples (tone/spirit only - translate into whatever language actually "
            "applies per the language instruction elsewhere in this prompt, write NEW lines "
            "don't recite verbatim): " + " | ".join(example_lines)
        )

    return " ".join(parts)


def build_persona_flavor_hint():
    """Versi RINGKAS dari kepribadian, buat disisipkan ke prompt-prompt pendek lain
    (mis. generate_script_feedback di main.py) yang butuh tau 'gimana gaya ngomong
    Luno' tanpa perlu full context sepanjang build_persona_prompt(). Return string
    kosong kalau persona-nya netral (nggak perlu nambahin apa-apa)."""
    personality_key = PERSONA.get("personality", "neutral")
    if personality_key == "neutral":
        return ""

    name = PERSONA.get("name") or "Luno"
    hint = f"Stay in character as {name} ({personality_key} personality) in this confirmation."

    smart_home_style = (PERSONA.get("smart_home_style") or "").strip()
    if smart_home_style:
        hint += f" Specifically: {smart_home_style}"

    speech = PERSONA.get("speech_style", {})
    if speech.get("stammer_when_flustered"):
        hint += (
            " A little flustered elongation is welcome if the moment calls for it (e.g. "
            "'huh~?', after being praised) — but never repeat a letter 3+ times in a row "
            "(TTS can misread it as an acronym), max 2."
        )

    return hint
