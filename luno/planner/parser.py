"""
parser.py
=========

Turns a raw user request string into an ordered list of `ParsedStep`s -
the text-to-structured-data boundary, same role
`vision_memory.utils.parse_description_heuristic` plays for camera
descriptions.

HONEST LIMITATION: `IntentParser` is keyword/regex clause-splitting, NOT
real NLU. It is good enough to reliably turn the spec's own example -
"open Chrome, turn on the bedroom light, turn off the desk lamp, then
play Spotify." - into the exact 4-task breakdown the spec shows, and a
handful of similar patterns (browser navigation, Home Assistant on/off/
set, Spotify playback) - but it WILL misparse phrasing outside that
vocabulary, falling back to `tool="unknown"` rather than guessing wrong
(caught by `validator.py` once a real tool registry is attached).

Every step this parser produces is marked SEQUENTIAL (each depends on the
one before it) - see `planner.py`'s `_steps_to_tasks()`, which is where
that dependency chain actually gets built. This is a deliberate,
documented choice: the spec's own multi-step example is drawn as a
strict top-to-bottom chain (arrows between every task) even though
turning on a light plainly doesn't NEED Chrome to be open first -
inferring TRUE independence from prose reliably is exactly the kind of
judgment call a keyword parser shouldn't be trusted with. Real parallel
execution is fully supported by the engine underneath (see
`dependency.py`/`scheduler.py` and `test_planner.py`'s parallel-execution
test, which builds an independent-tasks plan directly through the Task/
Plan API rather than through this parser) - `ParsedStep.depends_on_previous`
exists as the hook a smarter parser (e.g. LLM-backed structured output,
matching the pipeline's "OpenRouter" stage) could flip to `False` for
steps it's confident are independent, without anything downstream
needing to change.

`Planner` accepts any `Callable[[str], List[ParsedStep]]` as `parse_fn`
(defaulting to `IntentParser.parse`), so swapping in that smarter parser
later is a one-line change at the call site - never a change to this
file's contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ParsedStep:
    tool: str
    action: str
    target: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on_previous: bool = True
    label: str = ""


_VOCATIVE_RE = re.compile(r"^\s*(?:hey[,\s]+)?luno[,:]?\s*", re.IGNORECASE)
# "and" tolerated as a clause separator alongside ","/"then" - a real,
# reported gap: "open chrome and trun on rgb strip" used to parse as ONE
# clause (no comma, no "then"), and since _TURN_ON_RE is checked before
# _OPEN_RE below, that single clause matched "trun on" first and
# silently swallowed "open chrome" - only the turn_on half ever ran.
# Splitting on bare "and" too means each half gets its own clause/verb,
# exactly like the spec's own comma-separated multi-command example.
# _LEADING_CONJUNCTION_RE below already anticipated "and" showing up at
# the start of a clause (Oxford-comma lists, e.g. "A, B, and C") - this
# just also treats a bare "A and B" (no comma at all) the same way. A
# stray empty clause between an adjacent ", and" is harmless: parse()'s
# own `if not clause: continue` already drops it. Indonesian "dan"
# ("and") tolerated alongside it - same bilingual-tolerance precedent as
# "buka"/"trun" elsewhere in this module.
_CLAUSE_SPLIT_RE = re.compile(r",|\band\b|\bdan\b|\bthen\b", re.IGNORECASE)
_LEADING_CONJUNCTION_RE = re.compile(r"^\s*(?:(?:and|dan)\s+)?", re.IGNORECASE)

_SET_TO_RE = re.compile(r"\b(?:set|ubah|ganti|atur)\b(.*?)\b(?:to|ke|jadi)\b(.*)", re.IGNORECASE)
# "ubah"/"ganti"/"atur" ("change"/"adjust") and "ke"/"jadi" ("to"/"into")
# tolerated bilingually alongside "set"/"to" - same precedent as "buka"
# ("open") tolerance elsewhere in this module. Purely additive: every
# existing "set X to Y" phrasing (e.g. "set the thermostat to 24") still
# matches exactly as before, this only ALSO matches the Indonesian-verb
# variants ("ubah kecerahan rgb strip jadi 80", "atur warna rgb strip ke
# merah") reported as missing.

# REPORTED GAP: "set RGB strip warna sama brightness" - `_SET_TO_RE` above
# always produced action="set_value", which no `home_assistant` tool
# handler (mock OR real) has ever supported (`_SUPPORTED_ACTIONS` in
# `luno/tool_manager/builtin/home_assistant.py` only ever listed
# turn_on/turn_off/toggle/run_script/set_temperature) - EVERY "set X to Y"
# command whose target wasn't a thermostat/climate entity was silently
# broken before this fix, not just color/brightness. `_classify_color_set`/
# `_classify_brightness_set` below reclassify the SAME `_SET_TO_RE` match
# into the two new, now-actually-implemented actions (see
# `real_home_assistant.py`'s new `set_color`/`set_brightness` handling) -
# checked BEFORE falling back to the generic `set_value` branch, which
# stays exactly as before for everything else (temperature, ...).
_COLOR_WORD_RE = re.compile(r"\b(?:colou?r|warna)(?:nya)?\b", re.IGNORECASE)
_BRIGHTNESS_WORD_RE = re.compile(r"\b(?:brightness|kecerahan|terang(?:nya)?)\b", re.IGNORECASE)

#: Same 10-color palette `luno.devices.WLEDController.set_color_name()`
#: already uses (kept in sync BY CONVENTION, not import - `luno.planner`
#: stays dependency-free from the rest of `luno`, same convention
#: `_resolve_browser_open_target()`'s own local-import comment documents),
#: plus common Indonesian names for the same colors so "merah"/"biru"/etc.
#: work exactly as naturally as "red"/"blue".
_COLOR_NAMES: Dict[str, Tuple[int, int, int]] = {
    "red": (255, 0, 0), "merah": (255, 0, 0),
    "green": (0, 255, 0), "hijau": (0, 255, 0),
    "blue": (0, 0, 255), "biru": (0, 0, 255),
    "yellow": (255, 255, 0), "kuning": (255, 255, 0),
    "cyan": (0, 255, 255), "toska": (0, 255, 255),
    "magenta": (255, 0, 255), "fuschia": (255, 0, 255),
    "white": (255, 255, 255), "putih": (255, 255, 255),
    "orange": (255, 165, 0), "oranye": (255, 165, 0), "jingga": (255, 165, 0),
    "purple": (128, 0, 128), "ungu": (128, 0, 128),
    "pink": (255, 192, 203),
}
# "trun" tolerated alongside "turn(s)" - a real, recurring typo/transposition
# (observed twice from live usage: "trun on"/"trun off") that would otherwise
# silently fall through to the "unknown" tool and read as plain conversation
# instead of a smart-home command. Purely additive - every existing "turn
# on"/"turn off" phrasing still matches exactly as before.
#
# Indonesian "nyalakan"/"nyalain"/"hidupkan"/"hidupin" (turn on) and
# "matikan"/"matiin" (turn off) - a real, reported gap: every OTHER verb
# in this parser already got bilingual tolerance ("buka" for open,
# "jalankan" for run, "dan" for and - see their own comments below/above),
# but turn_on/turn_off, the single most common smart-home command, was
# English-only. An Indonesian-speaking user's "matikan lampu" fell all
# the way through to `tool="unknown"` and was read as plain conversation
# instead of a Home Assistant command - never even reaching
# `RealHomeAssistantHandler`'s verification loop, let alone HA itself.
# Same "single capturing group at the end, shared by every alternative"
# shape as the English-only version above, so `_clause_to_step`'s
# `match.group(1)` still works unchanged regardless of which
# language/verb matched.
#
# "aktifkan"/"aktifin" ("activate") and "nonaktifkan"/"nonaktifin"
# ("deactivate") - another real gap in the same family as the block
# above: a perfectly natural Indonesian way to say turn_on/turn_off
# that fell through to "unknown" because it wasn't one of the specific
# verbs already listed.
_TURN_ON_RE = re.compile(r"\b(?:(?:turns?|trun)\s+on|nyalakan|nyalain|hidupkan|hidupin|aktifkan|aktifin)\b(.*)", re.IGNORECASE)
_TURN_OFF_RE = re.compile(r"\b(?:(?:turns?|trun)\s+off|matikan|matiin|nonaktifkan|nonaktifin)\b(.*)", re.IGNORECASE)
_NAVIGATE_RE = re.compile(r"\bnavigates?\s+to\b(.*)", re.IGNORECASE)
_TYPE_RE = re.compile(r"\btype\b(.*)", re.IGNORECASE)
_PRESS_RE = re.compile(r"\bpress(?:es)?\b(.*)", re.IGNORECASE)
_PLAY_RE = re.compile(r"\bplays?\b(.*)", re.IGNORECASE)
_LOOK_RE = re.compile(r"\b(?:look at|looks at|see|watch)\b(.*)", re.IGNORECASE)
# "buka" (Indonesian for "open") tolerated alongside "open(s)" - same
# reasoning as the "trun" tolerance above: this project's own users mix
# Indonesian and English mid-sentence (see e.g. luno/devices.py's own
# "aliases" feature, built for exactly that), so an Indonesian-only
# phrasing of an otherwise well-supported command shouldn't silently
# fall through to "unknown".
_OPEN_RE = re.compile(r"\b(?:opens?|buka)\b(.*)", re.IGNORECASE)

# "buka github.com" / "open portainer" (a configured monitoring
# dashboard - see `luno/browser/config.py::MonitorTarget`) - an
# ADDITIVE narrowing of the plain "open X" case right above: most "open
# X" phrases mean "launch app X" (`tool="windows"`, unchanged - see
# `_classify_open_target` below), but a URL-shaped `X` or a KNOWN
# dashboard name means the user wants that opened in the BROWSER
# instead, never guessed - only a real URL shape or an actually-
# configured target name ever redirects here, same "don't guess, use a
# real registry lookup" discipline `_RUN_SCRIPT_RE`'s own
# `_is_known_script()` gate already established for "run X".
_URL_SHAPE_RE = re.compile(r"^(?:https?://)?(?:www\.)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?$", re.IGNORECASE)
# "run X" / "start X" / "activate X" / Indonesian "jalankan X" - Planner
# had NO pattern at all for running a Home Assistant script (see
# `config/scripts.config.json`) before this; every one of these fell
# through to "unknown" and was never a smart-home command at all. Reuses
# the "home_assistant" tool (same as turn_on/turn_off) with action
# "run_script" - `RealHomeAssistantHandler.execute()` already handles
# that action (resolving the target against `luno.devices.SCRIPTS`),
# it just never had a way to be REACHED via plain text before now.
_RUN_SCRIPT_RE = re.compile(r"\b(?:runs?|starts?|activates?|jalankan)\b(.*)", re.IGNORECASE)

# Pan/tilt camera control (e.g. "geser kamera ke kanan", "pan the camera
# left", "tilt camera up", "kalibrasi kamera") - new "camera_ptz" tool
# (see luno/tool_manager/builtin/real_camera_ptz.py). Deliberately NOT a
# single regex with word-order assumptions (unlike every pattern above,
# which anchors on a verb followed immediately by its target) - Indonesian
# phrasing here varies more in word order ("geser kamera ke kanan" vs
# "kamera geser ke kanan" are both natural), so `_classify_camera_ptz()`
# below instead just checks for CO-OCCURRENCE of a move verb + the word
# "camera"/"kamera" + a direction word anywhere in the same clause -
# still conservative (all three must be present) so it doesn't
# accidentally fire on an unrelated sentence that happens to contain one
# of these words alone (e.g. "kenapa kamera nggak nyala" has "kamera" but
# no move verb or direction word, so it correctly falls through).
_CAMERA_WORDS = ("camera", "kamera")
# Includes informal "-in" suffixed variants ("arahin", "geserin", "putarin",
# "gerakin") alongside the formal "-kan" forms - casual spoken/typed
# Indonesian uses these interchangeably (e.g. "arahin kamera ke tengah" is
# just as natural as "arahkan kamera ke tengah") and previously only the
# formal form was recognized, silently falling through to "unknown" -> the
# LLM then improvised an unhelpful/inaccurate answer instead of actually
# moving the camera.
_CAMERA_MOVE_VERBS = (
    "pan", "tilt", "move", "point",
    "geser", "geserkan", "geserin",
    "putar", "putarkan", "putarin",
    "arahkan", "arahin",
    "hadapkan", "hadapin",
    "gerakkan", "gerakin",
)
# Unambiguous on their own - safe to trigger "center" the moment they
# co-occur with "kamera"/"camera", no move verb required.
_CAMERA_CENTER_WORDS = ("center", "recenter", "calibrate", "kalibrasi", "tengahkan")
# "tengah" (bare "center", the noun) is a much more common/generic word
# in Indonesian ("middle") than "tengahkan" - "kamera ada di tengah meja"
# (the camera is in the middle of the table) has both "kamera" and
# "tengah" but is NOT a command. So "tengah" is only treated as a center
# command when a move verb is ALSO present ("arahkan kamera ke tengah",
# "arahin kamera ke tengah") - that combination previously fell through
# to `goto_preset` with target="tengah" (only ever worked if a preset
# happened to already be saved under that exact name) instead of the
# camera's real native recenter/calibrate command - see
# `real_camera_ptz.py::_center()`.
_CAMERA_CENTER_WORDS_NEEDS_VERB = ("tengah",)
_CAMERA_DIRECTIONS = {
    "pan_left": ("left", "kiri"),
    "pan_right": ("right", "kanan"),
    "tilt_up": ("up", "atas"),
    "tilt_down": ("down", "bawah"),
}

# Named-target aiming ("arahkan kamera ke pintu", "point the camera at the
# door") - a move verb + "camera"/"kamera" is already required by
# `_classify_camera_ptz` above; once that's confirmed AND none of the
# fixed _CAMERA_DIRECTIONS words matched (checked FIRST, so "arahkan
# kamera ke bawah" still means tilt_down, never a preset literally named
# "bawah"), whatever follows "ke"/"ka"/"to"/"at" is treated as a PRESET
# NAME - see luno/tool_manager/builtin/real_camera_ptz.py's own
# "Named-target aiming" section for why a preset (saved once, recalled
# by name) is the only mechanism that can actually work here (the camera
# has no absolute position readback, so there's no way to compute
# "point at the door" without ever having pointed there before).
# Built FROM `_CAMERA_MOVE_VERBS` (not a second hand-maintained copy of
# the verb list) - the two used to drift out of sync (a verb added to one
# but not the other silently broke goto_preset target extraction for that
# verb even though `_classify_camera_ptz`'s own verb check passed).
_CAMERA_TARGET_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in _CAMERA_MOVE_VERBS) + r")\b.*?"
    r"\b(?:ke|ka|to|at)\b\s*(.+)",
    re.IGNORECASE,
)

# "simpan posisi kamera sebagai pintu" / "simpan posisi ini sebagai
# pintu" / "save this position as the door" / "save camera position as
# door" -> save_preset. Requires a save-verb, "posisi"/"position", and
# "sebagai"/"as" to all appear (in that order) - same conservative
# "every signal must co-occur" approach as the rest of this module.
_SAVE_PRESET_RE = re.compile(
    r"\b(?:simpan|save)\b.*\b(?:posisi|position)\b.*\b(?:sebagai|as)\b\s*(.+)",
    re.IGNORECASE,
)


# Sprint 71 (Camera Patrol) - "mulai patroli kamera"/"mulai patroli rumah"/
# "start camera patrol"/"stop patroli kamera"/"status patroli kamera" - a
# NEW tool ("camera_patrol", see luno/tool_manager/builtin/camera_patrol.py)
# built entirely ON TOP OF the existing camera_ptz foundation above (this
# module never gains a second PTZ classifier - camera_patrol only ever
# starts/stops/queries the ALREADY-classified goto_preset/center calls a
# CameraPatrolModule issues internally). Anchored on the word "patrol"/
# "patroli" (a word this project has no other use for) co-occurring with a
# start/stop/status verb - same "every signal must co-occur" conservative
# approach `_classify_camera_ptz` above already established, so ordinary
# conversation that happens to mention "patrol" in an unrelated sense
# (rare, but e.g. "cerita tentang polisi patroli") without an actual verb
# is never misparsed as a command.
_PATROL_WORD_RE = re.compile(r"\bpatrol(?:i)?\b", re.IGNORECASE)
_PATROL_STOP_VERBS = ("stop", "berhenti", "hentikan")
_PATROL_STATUS_WORDS = ("status",)
_PATROL_START_VERBS = ("mulai", "jalankan", "start")
#: Everything after the word "patrol"/"patroli" - used to extract an
#: optional route NAME for "start" ("mulai patroli rumah" -> "rumah").
#: "kamera"/"camera" alone is treated as a generic reference to the
#: camera itself (not a route name) and stripped before slugifying - see
#: _classify_camera_patrol()'s own final step.
_PATROL_TARGET_RE = re.compile(r"\bpatrol(?:i)?\b\s*(.*)$", re.IGNORECASE)
_PATROL_GENERIC_CAMERA_WORD_RE = re.compile(r"\b(?:kamera|camera)\b", re.IGNORECASE)


def _classify_camera_patrol(lower_clause: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns `(action, target)` for the `camera_patrol` tool - `action`
    is one of "start"/"stop"/"status", `target` is only ever non-None for
    "start" (a route name, or `None` meaning "use the default route" -
    see `CameraPatrolModule._resolve_route()`). Returns `(None, None)` if
    this clause isn't a recognized patrol command at all."""
    if not _PATROL_WORD_RE.search(lower_clause):
        return None, None

    if any(_contains_word(lower_clause, w) for w in _PATROL_STATUS_WORDS):
        return "status", None

    if any(_contains_word(lower_clause, w) for w in _PATROL_STOP_VERBS):
        return "stop", None

    if any(_contains_word(lower_clause, w) for w in _PATROL_START_VERBS):
        match = _PATROL_TARGET_RE.search(lower_clause)
        target = None
        if match:
            remainder = _PATROL_GENERIC_CAMERA_WORD_RE.sub("", match.group(1)).strip()
            target = _slugify(remainder) if remainder else None
        return "start", target

    return None, None


# Sprint 72 (Automation Engine Dasar) Phase 14 - "Aktifkan otomasi mode
# malam" / "Matikan otomasi mode malam" / "status otomasi". Deliberately
# requires the word "otomasi"/"otomatisasi"/"automation" to co-occur (the
# SAME conservative "all signals must co-occur" approach _PATROL_WORD_RE/
# _classify_llm_mode already use) - "aktifkan"/"matikan"/"nyalakan" are
# heavily overloaded elsewhere in this parser for plain Home Assistant
# commands ("nyalakan lampu"), so a bare "aktifkan mode malam" (no anchor
# word) is deliberately NOT classified as an automation command; this is
# a documented, conservative scope boundary (see docs/change_impact/
# automation_engine.md), not a gap - a real deployment names its
# automation rule to match what users actually say, exactly the same
# precedent camera_patrol's own route-name resolution already
# established (no fuzzy alias table - Phase 14's own "Tidak boleh
# melakukan fuzzy execution terhadap automation yang mirip").
_AUTOMATION_WORD_RE = re.compile(r"\botomasi\b|\botomatisasi\b|\bautomation\b", re.IGNORECASE)
_AUTOMATION_STATUS_WORDS = ("status",)
_AUTOMATION_DISABLE_VERBS = ("matikan", "nonaktifkan", "disable")
_AUTOMATION_ENABLE_ONLY_VERBS = ("enable",)
_AUTOMATION_RUN_VERBS = ("aktifkan", "jalankan", "mulai", "nyalakan", "run")
#: Everything after the anchor word - used to extract the target
#: automation id/name ("aktifkan otomasi mode malam" -> "mode malam" ->
#: slugified to "mode_malam"), same `_PATROL_TARGET_RE` idiom.
_AUTOMATION_TARGET_RE = re.compile(r"\botomasi\b|\botomatisasi\b|\bautomation\b", re.IGNORECASE)


def _classify_automation(lower_clause: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns `(action, target)` for the `automation` tool - `action` is
    one of "run"/"enable"/"disable"/"status", `target` is the automation
    id/name (`None` for a "status" with no specific target, meaning "list
    all"). Returns `(None, None)` if this clause isn't a recognized
    automation command at all (the anchor word is absent)."""
    if not _AUTOMATION_WORD_RE.search(lower_clause):
        return None, None

    match = _AUTOMATION_TARGET_RE.search(lower_clause)
    remainder = lower_clause[match.end():].strip() if match else ""
    target = _slugify(remainder) if remainder else None

    if any(_contains_word(lower_clause, w) for w in _AUTOMATION_STATUS_WORDS):
        return "status", target

    if any(_contains_word(lower_clause, w) for w in _AUTOMATION_DISABLE_VERBS):
        return "disable", target

    if any(_contains_word(lower_clause, w) for w in _AUTOMATION_ENABLE_ONLY_VERBS):
        return "enable", target

    if any(_contains_word(lower_clause, w) for w in _AUTOMATION_RUN_VERBS):
        return "run", target

    return None, None


# LLM auto/manual routing mode (see luno/routing/mode_state.py and
# luno/tool_manager/builtin/llm_mode.py) - "pakai llm manual", "pakai
# llm openai", "llm otomatis", "switch llm to auto", "gunakan llm
# claude", etc. Requires the word "llm" (or "ai model"/"model ai")
# somewhere in the clause, same conservative "all signals must
# co-occur" approach as `_classify_camera_ptz` below - this keeps
# ordinary conversation that happens to mention a provider name by
# itself ("is gemini a real word", "buka chatgpt di browser") from
# being misparsed as a mode-switch command.
_LLM_KEYWORD_PHRASES = ("llm", "ai model", "model ai")
_LLM_AUTO_WORDS = ("otomatis", "automatic", "auto")
_LLM_MANUAL_WORDS = ("manual",)
# Word found in the clause -> canonical provider alias passed to
# `resolve_alias()` (luno/routing/provider_selector.py). Kept in sync BY
# CONVENTION (not import - the routing package is deliberately
# dependency-free from the rest of `luno`) with that module's
# `PROVIDER_NAMES`/`_DEEPSEEK_ALIASES`/`_GPT_ALIASES` and
# `luno/tool_manager/builtin/llm_mode.py`'s `_KNOWN_ALIASES`.
_LLM_PROVIDER_WORDS = {
    "openrouter": "openrouter",
    "openai": "openai",
    "gpt": "gpt",
    "chatgpt": "gpt",
    "gemini": "gemini",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "local": "local",
    "deepseek": "deepseek",
}


def _contains_word(clause: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", clause) is not None


def _classify_llm_mode(lower_clause: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns `(action, target)` for the `llm_mode` tool
    ("set_auto"/None or "set_manual"/<provider alias or None>), or
    `(None, None)` if this clause isn't a recognized LLM mode command.
    `lower_clause` is already lowercased by the caller
    (`_clause_to_step`)."""
    has_keyword = any(_contains_word(lower_clause, phrase) for phrase in _LLM_KEYWORD_PHRASES)
    if not has_keyword:
        return None, None
    if any(_contains_word(lower_clause, w) for w in _LLM_AUTO_WORDS):
        return "set_auto", None
    provider = None
    for word, canon in _LLM_PROVIDER_WORDS.items():
        if _contains_word(lower_clause, word):
            provider = canon
            break
    if provider or any(_contains_word(lower_clause, w) for w in _LLM_MANUAL_WORDS):
        return "set_manual", provider
    return None, None


#: "120 50 200" / "rgb 120 50 200" / "120/50/200" - REPORTED GAP: the
#: fixed 10-name `_COLOR_NAMES` palette has no way to express an
#: arbitrary custom color at all (user: "dulu ada kombinasi warnanya di
#: program" - previously could give raw RGB numbers, not just a name
#: from a short list). An optional leading "rgb"/"warna" word is
#: stripped, optional surrounding brackets are stripped, then exactly
#: three 0-255 integers separated by whitespace/slashes - three real
#: numbers IS the signal (same "never guess" discipline as the rest of
#: this module). Deliberately NOT comma-separated ("120,50,200") - a
#: bare "," is already this parser's OWN multi-clause separator (see
#: `_LEADING_CONJUNCTION_RE`/the module docstring's "open Chrome, turn
#: on the bedroom light, ..." example), applied during clause-splitting
#: LONG before `_clause_to_step`/this function ever sees the text - by
#: the time a comma-separated triplet would reach here it's already
#: been torn into three separate (nonsensical) clauses. Space/slash
#: don't have that conflict.
_RGB_PREFIX_RE = re.compile(r"^(?:rgb|warna)\s*", re.IGNORECASE)
_RGB_TRIPLET_RE = re.compile(r"^(-?\d{1,3})\s*[/\s]\s*(-?\d{1,3})\s*[/\s]\s*(-?\d{1,3})$")


def _parse_rgb_triplet(raw_value: str) -> Optional[Tuple[int, int, int]]:
    value = _RGB_PREFIX_RE.sub("", raw_value.strip()).strip()
    value = value.strip("()[] \t")
    match = _RGB_TRIPLET_RE.match(value)
    if not match:
        return None
    r, g, b = (max(0, min(255, int(match.group(i)))) for i in (1, 2, 3))
    return r, g, b


def _classify_color_set(raw_target: str, raw_value: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Returns `(target_slug, color_params)` for a `_SET_TO_RE` match
    whose VALUE is either a recognized color word (`_COLOR_NAMES`) or an
    explicit RGB triplet (`_parse_rgb_triplet`) - "set rgb strip to red"
    -> ("rgb_strip", {"color": "red"}), "set rgb strip to 120,50,200" ->
    ("rgb_strip", {"rgb": [120, 50, 200]}). Either signal being present
    IS the check (never a guess beyond those two explicit forms) - the
    word "color"/"warna" in `raw_target` is optional and, if present,
    stripped out so "set the color of rgb strip to red"/"set rgb strip
    color to red" resolve to the same target as the bare phrasing.
    Returns `None` (caller falls through to the generic set_value
    branch) whenever `raw_value` is neither - e.g. "set the thermostat
    to 24" never reaches this branch since "24" is neither a color name
    nor a 3-number triplet."""
    value_word = raw_value.strip().lower()
    if value_word in _COLOR_NAMES:
        color_params: Dict[str, Any] = {"color": value_word}
    else:
        rgb = _parse_rgb_triplet(raw_value)
        if rgb is None:
            return None
        color_params = {"rgb": list(rgb)}
    target = _COLOR_WORD_RE.sub("", raw_target).strip()
    # "the color OF rgb strip" -> "the  rgb strip" once "color" itself is
    # stripped above - "of" is the only leftover connector word worth
    # removing (same as `_classify_brightness_set`'s own "of" handling),
    # so both "set the color of rgb strip to red" and "set rgb strip
    # color to red" resolve to the identical target, per this function's
    # own docstring.
    target = re.sub(r"\bof\b", "", target, flags=re.IGNORECASE).strip()
    target_slug = _slugify(target) or _slugify(raw_target)
    if not target_slug:
        return None
    return target_slug, color_params


def _classify_brightness_set(raw_target: str, raw_value: str) -> Optional[Tuple[str, int]]:
    """Returns `(target_slug, level_0_to_100)` for a `_SET_TO_RE` match
    whose TARGET explicitly mentions brightness/kecerahan - "set
    brightness of rgb strip to 80" (raw_target=" brightness of rgb
    strip ", raw_value=" 80") -> ("rgb_strip", 80). Requires the
    brightness word to be PRESENT in `raw_target` (never inferred from
    a bare number alone - "set rgb strip to 80" without that word stays
    a plain, if odd, `set_value` rather than being guessed as
    brightness) - same "real signal, never a guess" discipline
    `_resolve_browser_open_target()`'s own docstring documents.
    Returns `None` if there's no brightness word, or the value isn't
    numeric at all."""
    if not _BRIGHTNESS_WORD_RE.search(raw_target):
        return None
    number_match = _NUMBER_RE.search(raw_value)
    if not number_match:
        return None
    level = max(0, min(100, int(float(number_match.group(0)))))
    target = _BRIGHTNESS_WORD_RE.sub("", raw_target).strip()
    # "brightness of rgb strip" / "rgb strip brightness" - "of" is the
    # only leftover connector word worth stripping; anything else left
    # over is presumed to be the actual device name.
    target = re.sub(r"\bof\b", "", target, flags=re.IGNORECASE).strip()
    target_slug = _slugify(target)
    if not target_slug:
        return None
    return target_slug, level


def _classify_camera_ptz(lower_clause: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns `(action, target)` for the `camera_ptz` tool - `action` is
    one of "pan_left"/"pan_right"/"tilt_up"/"tilt_down"/"center"/
    "goto_preset", `target` is only ever non-None for "goto_preset" (a
    preset name). Returns `(None, None)` if this clause isn't a
    recognized camera-movement command at all. `lower_clause` is already
    lowercased by the caller (`_clause_to_step`)."""
    if not any(_contains_word(lower_clause, w) for w in _CAMERA_WORDS):
        return None, None
    if any(_contains_word(lower_clause, w) for w in _CAMERA_CENTER_WORDS):
        return "center", None
    has_move_verb = any(_contains_word(lower_clause, v) for v in _CAMERA_MOVE_VERBS)
    if has_move_verb and any(_contains_word(lower_clause, w) for w in _CAMERA_CENTER_WORDS_NEEDS_VERB):
        return "center", None
    if not has_move_verb:
        return None, None
    for action, direction_words in _CAMERA_DIRECTIONS.items():
        if any(_contains_word(lower_clause, w) for w in direction_words):
            return action, None
    # No fixed direction word matched - try a NAMED target instead
    # ("arahkan kamera ke pintu") before giving up.
    match = _CAMERA_TARGET_RE.search(lower_clause)
    if match:
        target = _slugify(match.group(1))
        if target:
            return "goto_preset", target
    return None, None


def _classify_camera_save_preset(lower_clause: str) -> Optional[str]:
    """Returns a preset name for the `camera_ptz` "save_preset" action
    ("simpan posisi kamera sebagai pintu" -> "pintu"), or `None`. See
    `_SAVE_PRESET_RE`'s own comment for the exact phrasing this
    requires."""
    match = _SAVE_PRESET_RE.search(lower_clause)
    if not match:
        return None
    return _slugify(match.group(1))


def _resolve_browser_open_target(rest: str) -> Optional[str]:
    """Returns a URL to navigate to in the BROWSER, or `None` (meaning
    "not a browser target, handle as a normal app-open" - the caller,
    `_clause_to_step`'s `_OPEN_RE` branch, falls back to
    `tool="windows"` in that case, exactly as before this function
    existed). Two ways to match, both requiring a REAL signal, never a
    guess:

      1. `rest` is itself URL-shaped (`_URL_SHAPE_RE`) - "buka
         github.com" / "open portainer.local:9000" - returned as-is
         (with `https://` prepended if no scheme was given).
      2. `rest` (slugified) matches the `name` of a configured
         `MonitorTarget` (`config/browser_monitor_targets.json` - see
         `luno/browser/config.py`) - "buka portainer" opens that
         target's configured URL. Local import: this package must stay
         free of a hard dependency on `luno.browser` (mirrors this
         module's own `_is_known_script()`/`_is_known_home_assistant_
         device()`-style local-import convention elsewhere in this
         project) - any import/config error here just means "no
         browser target," never a crash.
    """
    candidate = (rest or "").strip()
    if not candidate:
        return None
    if _URL_SHAPE_RE.match(candidate):
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
        return f"https://{candidate}"
    try:
        from luno.browser.config import load_monitor_targets
        wanted = _slugify(candidate)
        for target in load_monitor_targets():
            if _slugify(target.name) == wanted:
                return target.url
    except Exception:
        pass
    return None


_LEADING_ARTICLE_RE = re.compile(r"^\s*(?:the|a|an|to)\s+", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _is_known_script(slugified_target: str) -> bool:
    """`slugified_target` and `luno.devices.SCRIPTS`' own keys both get
    the exact same normalization (`_slugify` here; `.strip().lower()` -
    equivalent for these keys - in `devices.py`), so a plain equality
    check after slugifying both sides is enough. Any import/config
    problem fails closed (treat as "not a known script") rather than
    letting an exception here take down request parsing."""
    try:
        from luno import devices
        return any(_slugify(key) == slugified_target for key in devices.SCRIPTS)
    except Exception:
        return False


def _is_known_app(slugified_target: str) -> bool:
    """Same "slugify both sides, compare, fail closed" shape as
    `_is_known_script()` just above - checks against `luno.
    desktop_control.APPS` (config/apps.json) instead of
    `luno.devices.SCRIPTS`. Used by the `_OPEN_RE` branch below to
    recover from "buka <app> <extra words>"-shaped phrases."""
    try:
        from luno import desktop_control
        return any(_slugify(key) == slugified_target for key in desktop_control.APPS)
    except Exception:
        return False


def _slugify(text: Optional[str]) -> Optional[str]:
    """"the bedroom light" -> "bedroom_light". Returns None for
    empty/whitespace-only input rather than an empty string, so callers
    can tell "no target mentioned" apart from "target is an empty
    string"."""
    if not text:
        return None
    text = _LEADING_ARTICLE_RE.sub("", text.strip())
    text = _NON_ALNUM_RE.sub("_", text.strip().lower()).strip("_")
    return text or None


def _coerce_value(text: str):
    """"24 C" -> 24 (int), "21.5 degrees" -> 21.5 (float), anything with
    no number -> the trimmed string as-is."""
    match = _NUMBER_RE.search(text)
    if not match:
        return text.strip()
    raw = match.group(0)
    return float(raw) if "." in raw else int(raw)


class IntentParser:
    """Stateless - `parse()` is the only entry point."""

    @staticmethod
    def parse(request: str) -> List[ParsedStep]:
        text = _VOCATIVE_RE.sub("", request or "").strip()
        raw_clauses = _CLAUSE_SPLIT_RE.split(text)
        steps: List[ParsedStep] = []
        for raw in raw_clauses:
            clause = _LEADING_CONJUNCTION_RE.sub("", raw.strip()).strip()
            if not clause:
                continue
            steps.append(IntentParser._clause_to_step(clause))
        return steps

    @staticmethod
    def _clause_to_step(clause: str) -> ParsedStep:
        lower = clause.lower()

        # BUG FIX (reported): "nyalakan rgb strip dan matikan fish light"
        # only ever executed the FIRST clause - `depends_on_previous`
        # defaults to True (this dataclass field's own default), so the
        # SECOND clause's task structurally `depends_on` the first task's
        # id (see `planner.py::_steps_to_tasks`), and `PlanRunner.
        # _cascade_skip_blocked` unconditionally SKIPS any task whose
        # dependency didn't COMPLETE - regardless of `continue_on_failure`,
        # which only governs whether OTHER, non-dependent tasks get
        # cancelled, not this structural block. Two Home Assistant device
        # commands in one sentence have no real data dependency on each
        # other (unlike e.g. "navigate to X, then type Y", which
        # genuinely needs the page from step 1 loaded first) - this
        # module's own docstring already flagged this exact case as
        # over-conservative and named `depends_on_previous` as the hook
        # for exactly this fix ("even though turning on a light plainly
        # doesn't NEED Chrome to be open first"). Every home_assistant
        # action below now opts out of the chain, so each one is
        # attempted independently regardless of any other clause's
        # (unrelated) outcome - non-Home-Assistant clauses (browser/
        # windows/spotify) keep the existing sequential default unchanged.
        llm_mode_action, llm_mode_target = _classify_llm_mode(lower)
        if llm_mode_action:
            return ParsedStep(tool="llm_mode", action=llm_mode_action, target=llm_mode_target, label=clause, depends_on_previous=False)

        save_preset_target = _classify_camera_save_preset(lower)
        if save_preset_target:
            return ParsedStep(tool="camera_ptz", action="save_preset", target=save_preset_target, label=clause, depends_on_previous=False)

        camera_patrol_action, camera_patrol_target = _classify_camera_patrol(lower)
        if camera_patrol_action:
            return ParsedStep(tool="camera_patrol", action=camera_patrol_action, target=camera_patrol_target, label=clause, depends_on_previous=False)

        automation_action, automation_target = _classify_automation(lower)
        if automation_action:
            return ParsedStep(tool="automation", action=automation_action, target=automation_target, label=clause, depends_on_previous=False)

        camera_ptz_action, camera_ptz_target = _classify_camera_ptz(lower)
        if camera_ptz_action:
            return ParsedStep(tool="camera_ptz", action=camera_ptz_action, target=camera_ptz_target, label=clause, depends_on_previous=False)

        match = _SET_TO_RE.search(lower)
        if match:
            raw_target, raw_value = match.group(1), match.group(2)

            color_match = _classify_color_set(raw_target, raw_value)
            if color_match:
                color_target, color_params = color_match
                return ParsedStep(
                    tool="home_assistant", action="set_color",
                    target=color_target, params=color_params,
                    label=clause, depends_on_previous=False,
                )

            brightness_match = _classify_brightness_set(raw_target, raw_value)
            if brightness_match:
                brightness_target, brightness_level = brightness_match
                return ParsedStep(
                    tool="home_assistant", action="set_brightness",
                    target=brightness_target, params={"level": brightness_level},
                    label=clause, depends_on_previous=False,
                )

            return ParsedStep(
                tool="home_assistant", action="set_value",
                target=_slugify(raw_target), params={"value": _coerce_value(raw_value)},
                label=clause, depends_on_previous=False,
            )

        match = _TURN_ON_RE.search(lower)
        if match:
            return ParsedStep(tool="home_assistant", action="turn_on", target=_slugify(match.group(1)), label=clause, depends_on_previous=False)

        match = _TURN_OFF_RE.search(lower)
        if match:
            return ParsedStep(tool="home_assistant", action="turn_off", target=_slugify(match.group(1)), label=clause, depends_on_previous=False)

        match = _NAVIGATE_RE.search(lower)
        if match:
            return ParsedStep(tool="browser", action="navigate", target=_slugify(match.group(1)), label=clause)

        match = _TYPE_RE.search(lower)
        if match:
            return ParsedStep(tool="browser", action="type", params={"text": match.group(1).strip()}, label=clause)

        match = _PRESS_RE.search(lower)
        if match:
            return ParsedStep(tool="browser", action="press_key", params={"key": _slugify(match.group(1)) or "enter"}, label=clause)

        match = _PLAY_RE.search(lower)
        if match:
            rest = match.group(1).replace("spotify", "").strip()
            return ParsedStep(tool="spotify", action="play", target=_slugify(rest), label=clause)

        match = _LOOK_RE.search(lower)
        if match:
            return ParsedStep(tool="vision", action="describe", params={"question": clause.strip()}, label=clause)

        match = _OPEN_RE.search(lower)
        if match:
            rest = match.group(1).strip()
            target_slug = _slugify(rest)
            if target_slug == "browser":
                # Bare "buka browser" (no site/query attached) - route
                # straight to the REAL installed Chrome via the exact
                # same allowlisted `windows.open_app` mechanism every
                # other app name already uses (proven, lightweight,
                # zero Playwright dependency) rather than the
                # Playwright-driven `tool="browser", action="open"`
                # this used to return, which opened Playwright's
                # bundled Chromium binary - a DIFFERENT browser from
                # the user's real chrome.exe, with none of their
                # profile/logins/bookmarks, and (reported gap) an
                # unnecessary Playwright install requirement for what
                # is otherwise just "launch the app." "browser" itself
                # isn't a real registered app name, so it's hardcoded
                # to "chrome" here rather than requiring every user to
                # add a "browser": ... alias to config/apps.json - see
                # `luno.desktop_control.open_app`'s own allowlist.
                # ("buka browser dan cari X"-style utterances that DO
                # carry a search query are handled separately and
                # independently by main_runtime_demo.py's classifier-
                # driven research/image-search intents, which run on
                # the whole utterance and also now open real Chrome via
                # `luno.desktop_control.open_url` - not this per-clause
                # parse at all.)
                return ParsedStep(tool="windows", action="open_app", target="chrome", label=clause)
            browser_url = _resolve_browser_open_target(rest)
            if browser_url:
                return ParsedStep(tool="browser", action="navigate", target=browser_url, label=clause)
            # BUG FIX (reported): "buka chrome cari foto f35" used to
            # slugify the WHOLE remainder ("chrome_cari_foto_f35") and
            # look THAT up as a single app name - never registered
            # (config/apps.json only ever has single-word app names),
            # so `windows.open_app` always failed here and told the LLM
            # "belum terdaftar" even though a real image search (matched
            # separately, on the WHOLE utterance, by `classify_image_
            # search_intent`/`classify_research_intent` in
            # main_runtime_demo.py) had already opened successfully -
            # a confusing, contradictory-sounding pair of outcomes for
            # one request. If the FIRST WORD alone matches a registered
            # app but the full remainder doesn't, prefer opening just
            # that app - trailing words are ordinary sentence content
            # (frequently a search query the classifiers above already
            # handle on their own), not part of the app's name.
            if not _is_known_app(target_slug):
                words = rest.split()
                if words:
                    first_word_slug = _slugify(words[0])
                    if first_word_slug and _is_known_app(first_word_slug):
                        return ParsedStep(tool="windows", action="open_app", target=first_word_slug, label=clause)
            return ParsedStep(tool="windows", action="open_app", target=target_slug, label=clause)

        # Checked LAST, deliberately - "run"/"start"/"activate" are much
        # more generic verbs than the tool-specific ones above ("I want to
        # start my day", "let's run through the plan" are ordinary
        # sentences, not commands). Per this module's own "fall back to
        # unknown rather than guess wrong" rule: only actually produce a
        # run_script step when the slugified target matches a REAL
        # configured script name/alias (`luno.devices.SCRIPTS`, already
        # loaded once at import time - an in-memory dict lookup, not I/O,
        # so this doesn't cost this parser its speed/purity guarantees)
        # - any other "run X" phrase still falls through to "unknown"
        # exactly as it did before this pattern existed.
        match = _RUN_SCRIPT_RE.search(lower)
        if match:
            target = _slugify(match.group(1))
            if target and _is_known_script(target):
                return ParsedStep(tool="home_assistant", action="run_script", target=target, label=clause)

        return ParsedStep(tool="unknown", action="unknown", params={"raw_text": clause.strip()}, label=clause)
