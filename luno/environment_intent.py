"""
environment_intent.py
========================

Implicit/environmental smart-home intent inference - "hawanya panas
nih" ("it's kind of hot") -> infer the user might want the AC turned
on, WITHOUT them ever saying an explicit device command.

This is deliberately a SEPARATE, narrower mechanism from
`main_runtime_demo.py`'s `PlannerBridgeModule._classify_device_intent()`
(the "AI-assisted device intent" LLM classifier, which only tolerates
typos/paraphrases of an otherwise-EXPLICIT "turn on/off" request).
Inferring intent from an ambient remark is inherently less certain than
correcting a typo, so this module NEVER decides anything gets executed
by itself - it only ever proposes a cue (see `classify_environmental_cue`
below). `PlannerBridgeModule._handle_environmental_intent()` is what
turns a proposal into a real command, and ONLY after the user explicitly
confirms on their very next turn - see that method's own docstring for
the full two-turn state machine ("ask, then act", never "act, then
hope it was right").

Cue -> device mapping is entirely configurable (see
`ENV_TRIGGERS_CONFIG_FILE`, defaults to `config/environment_triggers.json`).
Each `device`/`devices` entry must match a name/alias already registered
in `luno.devices.LIGHTS` / `SWITCHES` / `SCRIPTS` (the SAME registries
`IntentParser`/`RealHomeAssistantHandler` already resolve names
against) - this module does zero entity-id resolution itself, it only
ever builds a canonical "turn on/off/run <device>" phrase (see
`build_confirmation_command`) that `IntentParser.parse()` already knows
how to turn into a real `ToolCall`, reusing 100% of the existing
verified-execution pipeline once the user says yes. If the config file
doesn't exist, or a cue's trigger isn't configured, that cue is
silently inert - same "no file = feature just isn't active" convention
`luno.devices.load_switches_config()` already uses.

Cue classification is deliberately REGEX/KEYWORD-based, not an LLM call
(unlike `_classify_device_intent`) - keeps it instant, free, and fully
deterministic (easy to unit test exhaustively), and the false-positive
risk inherent to inferring intent from an ambient remark is exactly why
the caller ALWAYS confirms before acting instead of this module trying
to be clever about it - a keyword false-positive costs one harmless
extra conversational turn ("mau aku nyalain AC?" / "eh, enggak kok"),
never an unwanted device action.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import ENV_TRIGGERS_CONFIG_FILE


def _contains_word(text: str, phrase: str) -> bool:
    """Word-boundary match, also correct for multi-word phrases (`\\b`
    only needs to land on the outer edges) - same helper convention as
    `luno/planner/parser.py`'s own `_contains_word`."""
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _contains_any(text: str, phrases) -> bool:
    return any(_contains_word(text, p) for p in phrases)


# -- cue keyword rules --------------------------------------------------------
# STRONG phrases are unambiguous enough to fire alone (rarely used any
# other way in casual speech). WEAK words are common enough in unrelated
# contexts ("panas" can describe food, weather in general conversation,
# or figuratively describe a tense situation; "gelap"/"dark" shows up in
# plenty of sentences that aren't about the room's lighting) that they
# only count when they co-occur with a personal/exclamatory MARKER word
# in the same clause - the same "co-occurrence, not a single word"
# conservatism `luno/planner/parser.py`'s `_classify_camera_ptz()`/
# `_classify_llm_mode()` already use.
_MARKER_WORDS = (
    "nih", "banget", "deh", "duh", "aduh", "aku", "gue", "saya",
    "i'm", "im", "so", "here",
)

_CUE_RULES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "hot": {
        "strong": ("gerah", "kepanasan", "kegerahan", "sumuk", "sweating", "overheating"),
        "weak": ("panas", "hot"),
    },
    "cold": {
        "strong": ("kedinginan", "menggigil", "freezing", "shivering"),
        "weak": ("dingin", "cold"),
    },
    "dark": {
        "strong": ("gelap gulita", "nggak keliatan", "ga keliatan", "can't see", "cant see"),
        "weak": ("gelap", "dark"),
    },
    "sleepy": {
        "strong": (
            "ngantuk", "mengantuk", "sleepy", "mau tidur", "mau bobo",
            "waktunya tidur", "going to bed", "going to sleep", "time for bed", "time to sleep",
        ),
        "weak": (),
    },
}

#: Every cue key this module knows how to classify - `load_environment_
#: triggers()` rejects any config entry whose key isn't in here.
KNOWN_CUES = tuple(_CUE_RULES.keys())


def classify_environmental_cue(text: str) -> Optional[str]:
    """Returns a cue key (one of `KNOWN_CUES`) or `None`. See module
    docstring for why this is keyword-based and why that's an
    acceptable trade-off here (the caller always confirms before
    acting)."""
    if not text:
        return None
    lower = text.lower()
    for cue, rules in _CUE_RULES.items():
        if _contains_any(lower, rules["strong"]):
            return cue
        weak = rules.get("weak", ())
        if weak and _contains_any(lower, weak) and _contains_any(lower, _MARKER_WORDS):
            return cue
    return None


# -- confirmation reply classification (yes/no to the pending question) ------

_NEGATIVE_PHRASES = (
    "nggak usah", "gak usah", "ga usah", "enggak usah", "tidak usah",
    "nggak", "enggak", "gak", "ga", "tidak", "jangan",
    "no", "nope", "don't", "dont",
    # Efficient LLM Classifier sprint (luno/routing/confirmation.py) -
    # its ConfirmationHandler reuses this SAME function for "cancel the
    # pending action" replies rather than forking a second word list;
    # "batal"/"cancel" are the two explicit decline words that sprint
    # requires which weren't already covered above.
    "batal", "cancel",
)
# Deliberately UNAMBIGUOUS words/phrases only - safe to match anywhere in
# the reply (word-boundary), even embedded in a longer sentence.
_AFFIRMATIVE_STRONG_PHRASES = (
    "iya", "yaudah", "ya udah", "boleh", "gas", "silakan", "silahkan",
    "tolong", "please", "yes", "yeah", "yep", "sure", "okay", "oke",
    # Efficient LLM Classifier sprint - see the `_NEGATIVE_PHRASES`
    # comment right above; "lakukan" is the one explicit confirm word
    # that sprint requires which wasn't already covered.
    "lakukan",
)
# "ya"/"ok" alone are too ambiguous to treat as affirmative wherever they
# appear - "ya" in particular is an extremely common Indonesian sentence-
# final softener/tag ("kan"/"deh"-like, e.g. "hmm apa ya", "gitu ya?")
# that says nothing about agreement. These only count as affirmative when
# they're the ENTIRE (trimmed) reply - i.e. the user said literally just
# "ya" or "ok" and nothing else.
_AFFIRMATIVE_EXACT_ONLY = ("ya", "ok")


def classify_confirmation_reply(text: str) -> Optional[bool]:
    """Returns `True` (affirmative), `False` (negative), or `None`
    (neither - this reply should be treated as a fresh, unrelated
    utterance, NOT an answer to the pending confirmation at all).

    Deliberately ASYMMETRIC about false positives: negative phrases are
    matched broadly (word-boundary anywhere in the reply) because a
    wrongly-detected decline just means nothing happens (the same safe
    default as detecting neither) - but affirmative detection is kept
    narrow (see `_AFFIRMATIVE_EXACT_ONLY` above) because a wrongly-
    detected confirmation would actually ACT on something the user never
    agreed to, which is exactly the mistake the whole confirm-first
    design in `environment_intent.py`'s module docstring exists to
    prevent. This is meant for short yes/no-style replies, not a
    general sentiment classifier - see
    `PlannerBridgeModule._handle_environmental_intent()` for how a
    `None` result is handled (the pending confirmation is dropped and
    `text` gets classified fresh as a brand new turn)."""
    if not text:
        return None
    lower = text.strip().lower()
    if _contains_any(lower, _NEGATIVE_PHRASES):
        return False
    if _contains_any(lower, _AFFIRMATIVE_STRONG_PHRASES):
        return True
    if lower in _AFFIRMATIVE_EXACT_ONLY:
        return True
    return None


# -- trigger config (cue -> device action) ------------------------------------

_VALID_ACTIONS = ("turn_on", "turn_off", "run_script")


@dataclass(frozen=True)
class EnvTrigger:
    action: str  # "turn_on" | "turn_off" | "run_script"
    devices: Tuple[str, ...]  # one or more names, matched against luno.devices registries
    ask: str  # confirmation question to pose BEFORE acting
    decline_ack: str = ""  # optional custom "okay, never mind" line


def _normalize_devices(raw) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(d.strip() for d in raw if isinstance(d, str) and d.strip())
    return ()


def load_environment_triggers(path: Optional[str] = None) -> Dict[str, EnvTrigger]:
    """Reads `ENV_TRIGGERS_CONFIG_FILE` (`config/environment_triggers.json`
    by default). Missing file / malformed JSON / an individual entry
    with an unknown cue, bad action, or no device -> that entry (or the
    whole file) is silently skipped, never raises - same "no file = the
    feature is just inactive" convention `luno.devices.
    load_switches_config()` already uses for `SWITCHES`."""
    file_path = path or ENV_TRIGGERS_CONFIG_FILE
    triggers: Dict[str, EnvTrigger] = {}
    if not os.path.exists(file_path):
        return triggers
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as ex:
        print(f"[EnvTriggers] ✗ Failed to load {file_path}: {ex}")
        return triggers

    for cue, cfg in (raw or {}).items():
        cue_key = cue.strip().lower()
        if cue_key not in _CUE_RULES:
            print(f"[EnvTriggers] ✗ Skip unknown cue '{cue}' (known: {', '.join(KNOWN_CUES)})")
            continue
        if not isinstance(cfg, dict):
            print(f"[EnvTriggers] ✗ Skip '{cue}': entry must be an object")
            continue
        action = (cfg.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            print(f"[EnvTriggers] ✗ Skip '{cue}': action must be one of {_VALID_ACTIONS}")
            continue
        devices = _normalize_devices(cfg.get("device") if cfg.get("device") is not None else cfg.get("devices"))
        if not devices:
            print(f"[EnvTriggers] ✗ Skip '{cue}': no 'device' (or 'devices') configured")
            continue
        ask = (cfg.get("ask") or "").strip() or f"Mau aku {action.replace('_', ' ')} {', '.join(devices)}?"
        decline_ack = (cfg.get("decline_ack") or "").strip()
        triggers[cue_key] = EnvTrigger(action=action, devices=devices, ask=ask, decline_ack=decline_ack)

    if triggers:
        print(f"[EnvTriggers] ✓ Loaded {len(triggers)} environmental trigger(s) from {file_path}: {', '.join(triggers)}")
    return triggers


#: Loaded once at import time (mirrors `luno.devices.LIGHTS`/`SWITCHES`).
#: Mutated in place by `reload_environment_triggers()` below rather than
#: reassigned, so a caller that imported this dict directly (rather than
#: reading it off a module attribute) still sees fresh data after a
#: reload - safe here (unlike `luno.devices.wled_lights`'s own "must
#: access via the module attribute" warning) precisely because this
#: object is never swapped out, only cleared+refilled.
ENV_TRIGGERS: Dict[str, EnvTrigger] = load_environment_triggers()


def reload_environment_triggers() -> None:
    """Re-reads the config file into the existing `ENV_TRIGGERS` dict
    (in place) - lets a config edit on disk take effect without a
    restart, mirroring this project's `RoutingConfig.reload_config()`/
    `_VerifyConfig.from_env()`-style "reloadable without a restart"
    precedent elsewhere."""
    ENV_TRIGGERS.clear()
    ENV_TRIGGERS.update(load_environment_triggers())


_ACTION_VERB = {"turn_on": "turn on", "turn_off": "turn off", "run_script": "run"}


def build_confirmation_command(trigger: EnvTrigger) -> str:
    """Turns a trigger's device list into the SAME canonical
    "turn on/off/run <device>" text `IntentParser` already knows how to
    split into independent clauses (comma-joined - see
    `luno/planner/parser.py`'s `_CLAUSE_SPLIT_RE`) and resolve against
    `luno.devices` - reuses the exact device-name resolution
    `RealHomeAssistantHandler` already does, no new resolution logic
    needed here at all."""
    verb = _ACTION_VERB[trigger.action]
    return ", ".join(f"{verb} {device}" for device in trigger.devices)
