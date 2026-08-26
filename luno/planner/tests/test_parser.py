"""
test_parser.py
================

`IntentParser` (see `luno/planner/parser.py`) previously had NO dedicated
test coverage at all - only exercised indirectly through Planner-level
integration tests. This file closes that gap, and specifically guards a
real, reported bug: turn_on/turn_off - the single most common smart-home
command - only recognized English phrasing ("turn on"/"turn off"/the
"trun on"/"trun off" typo), even though every OTHER verb this parser
supports (open/run/and) already had Indonesian tolerance. An
Indonesian-speaking user's "matikan lampu" ("turn off the light") fell
all the way through to `tool="unknown"` - never even reaching
`RealHomeAssistantHandler`'s verification loop, so no lifecycle event,
no Dashboard row, nothing - just silently misclassified as ordinary
conversation.

Run:
    python3 -m pytest luno/planner/tests/test_parser.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.planner.parser import IntentParser  # noqa: E402


def _single(text):
    steps = IntentParser.parse(text)
    assert len(steps) == 1, f"expected exactly one step for {text!r}, got {steps}"
    return steps[0]


# -- Regression: pre-existing English/typo phrasing must still work ----------

def test_turn_on_english():
    step = _single("turn on the bedroom light")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "bedroom_light")


def test_turn_off_english():
    step = _single("turn off the desk lamp")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_off", "desk_lamp")


def test_trun_typo_tolerated():
    on_step = _single("trun on rgb strip")
    off_step = _single("trun off rgb strip")
    assert on_step.action == "turn_on" and on_step.target == "rgb_strip"
    assert off_step.action == "turn_off" and off_step.target == "rgb_strip"


def test_multi_clause_example_from_module_docstring_still_parses():
    steps = IntentParser.parse("open Chrome, turn on the bedroom light, turn off the desk lamp, then play Spotify.")
    assert [(s.tool, s.action) for s in steps] == [
        ("windows", "open_app"),
        ("home_assistant", "turn_on"),
        ("home_assistant", "turn_off"),
        ("spotify", "play"),
    ]


# -- Fix: Indonesian turn_on/turn_off phrasing --------------------------------

def test_turn_off_indonesian_matikan():
    step = _single("matikan lampu kamar")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_off", "lampu_kamar")


def test_turn_off_indonesian_colloquial_matiin():
    step = _single("matiin lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_off", "lampu")


def test_turn_on_indonesian_nyalakan():
    step = _single("nyalakan lampu ruang tamu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu_ruang_tamu")


def test_turn_on_indonesian_colloquial_nyalain():
    step = _single("nyalain lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu")


def test_turn_on_indonesian_hidupkan():
    step = _single("hidupkan lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu")


def test_turn_on_indonesian_colloquial_hidupin():
    step = _single("hidupin lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu")


def test_turn_on_indonesian_aktifkan():
    step = _single("aktifkan lampu kamar")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu_kamar")


def test_turn_on_indonesian_colloquial_aktifin():
    step = _single("aktifin lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_on", "lampu")


def test_turn_off_indonesian_nonaktifkan():
    step = _single("nonaktifkan lampu kamar")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_off", "lampu_kamar")


def test_turn_off_indonesian_colloquial_nonaktifin():
    step = _single("nonaktifin lampu")
    assert (step.tool, step.action, step.target) == ("home_assistant", "turn_off", "lampu")


def test_indonesian_command_with_polite_prefix_still_recognized():
    """"tolong ..." ("please ...") prefixed onto an otherwise-recognized
    Indonesian command must not push it back into "unknown" - mirrors
    the English parser's own tolerance for incidental extra words
    around a recognized verb."""
    step = _single("tolong matikan lampu")
    assert step.tool == "home_assistant" and step.action == "turn_off"


# -- Genuinely unrecognized phrasing must still fall through honestly --------

def test_unrelated_sentence_falls_back_to_unknown():
    step = _single("bagaimana cuaca hari ini")
    assert (step.tool, step.action) == ("unknown", "unknown")


# -- Browser open target resolution (Browser/computer-use sprint) ------------
# "buka X" where X is URL-shaped or a configured monitoring dashboard name
# routes to the browser instead of `windows.open_app` - see
# `_resolve_browser_open_target()` in parser.py.

def test_open_ordinary_app_name_still_routes_to_windows():
    """The overwhelming common case ("buka spotify"/"buka chrome") must
    be completely unaffected by this addition."""
    step = _single("buka spotify")
    assert (step.tool, step.action, step.target) == ("windows", "open_app", "spotify")


def test_open_registered_app_with_trailing_words_uses_just_the_app_name(monkeypatch):
    """Reported gap: "buka chrome cari foto f35" used to slugify the
    WHOLE remainder ("chrome_cari_foto_f35") and look THAT up as a
    single app name - never registered (app names are single words),
    so `windows.open_app` always failed and told the LLM "belum
    terdaftar" even though a real image search (matched separately, on
    the whole utterance, by `classify_image_search_intent` in
    main_runtime_demo.py) had already opened successfully - a
    confusing, contradictory pair of outcomes for one request. Now: if
    the FIRST WORD alone matches a registered app but the full
    remainder doesn't, open just that app."""
    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "APPS", {"chrome": {"path": "C:\\chrome.exe", "args": []}})

    step = _single("buka chrome cari foto f35")
    assert (step.tool, step.action, step.target) == ("windows", "open_app", "chrome")


def test_open_unregistered_app_with_trailing_words_still_slugifies_whole_phrase(monkeypatch):
    """The fallback (heuristic doesn't apply) case - if NEITHER the
    full remainder NOR its first word is a registered app, behavior is
    unchanged from before this fix (slugify the whole remainder, even
    though it will predictably fail to resolve - that failure is
    still the most honest result when nothing matches at all)."""
    import luno.desktop_control as desktop_control
    monkeypatch.setattr(desktop_control, "APPS", {})

    step = _single("buka chrome cari foto f35")
    assert (step.tool, step.action, step.target) == ("windows", "open_app", "chrome_cari_foto_f35")


def test_open_url_shaped_target_routes_to_browser_navigate():
    step = _single("buka github.com")
    assert (step.tool, step.action, step.target) == ("browser", "navigate", "https://github.com")


def test_open_full_url_with_scheme_routes_to_browser_navigate():
    step = _single("open https://example.com/path")
    assert (step.tool, step.action, step.target) == ("browser", "navigate", "https://example.com/path")


def test_open_bare_word_browser_routes_to_real_chrome_via_open_app():
    """Changed behavior (reported gap): "buka browser" used to return
    tool=browser, action="open" - which opened Playwright's BUNDLED
    Chromium (not the user's real chrome.exe, no profile/logins). Now
    routes through the exact same `windows.open_app` mechanism as any
    other app name, hardcoded to "chrome" since "browser" itself isn't
    a real registered app - checked BEFORE the URL-shape/monitor-target
    resolution below."""
    step = _single("buka browser")
    assert (step.tool, step.action, step.target) == ("windows", "open_app", "chrome")


# -- Pan/tilt camera control (Tapo C212 integration) -------------------------

def test_camera_pan_right_indonesian():
    step = _single("geser kamera ke kanan")
    assert (step.tool, step.action, step.depends_on_previous) == ("camera_ptz", "pan_right", False)


def test_camera_pan_left_indonesian_putar():
    step = _single("putar kamera ke kiri")
    assert (step.tool, step.action) == ("camera_ptz", "pan_left")


def test_camera_tilt_up_english():
    step = _single("tilt the camera up")
    assert (step.tool, step.action) == ("camera_ptz", "tilt_up")


def test_camera_pan_left_english():
    step = _single("pan camera left")
    assert (step.tool, step.action) == ("camera_ptz", "pan_left")


def test_camera_tilt_down_indonesian_arahkan():
    step = _single("arahkan kamera ke bawah")
    assert (step.tool, step.action) == ("camera_ptz", "tilt_down")


def test_camera_center_indonesian():
    step = _single("kalibrasi kamera")
    assert (step.tool, step.action) == ("camera_ptz", "center")


def test_camera_center_english():
    step = _single("center the camera")
    assert (step.tool, step.action) == ("camera_ptz", "center")


def test_camera_goto_preset_indonesian():
    step = _single("arahkan kamera ke pintu")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "pintu")


def test_camera_goto_preset_english_at():
    step = _single("point the camera at the door")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "door")


def test_camera_goto_preset_english_to():
    step = _single("point camera to monitor")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "monitor")


def test_camera_goto_preset_hadapkan():
    step = _single("hadapkan kamera ke pintu")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "pintu")


def test_camera_direction_words_still_win_over_preset_parsing():
    """Regression guard: "arahkan kamera ke kanan/kiri/atas/bawah" must
    still mean a relative pan/tilt, never a preset literally named
    "kanan"/"bawah" - direction words are checked BEFORE the named-
    target fallback in `_classify_camera_ptz`."""
    assert _single("arahkan kamera ke kanan").action == "pan_right"
    assert _single("arahkan kamera ke kiri").action == "pan_left"
    assert _single("arahkan kamera ke atas").action == "tilt_up"
    assert _single("arahkan kamera ke bawah").action == "tilt_down"


def test_camera_save_preset_indonesian():
    step = _single("simpan posisi kamera sebagai pintu")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "save_preset", "pintu")


def test_camera_save_preset_indonesian_ini():
    step = _single("simpan posisi ini sebagai monitor")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "save_preset", "monitor")


def test_camera_save_preset_english():
    step = _single("save this position as door")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "save_preset", "door")


def test_camera_save_preset_english_with_camera_word():
    step = _single("save camera position as the monitor")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "save_preset", "monitor")


# -- Informal "-in" verb variants + "tengah" center fix ----------------------
# (bug report: "arahin kamera ke tangah" fell through to "unknown" ->
# Luno fabricated a false "I can't physically move the camera" claim.)

def test_camera_informal_arahin_recognized():
    step = _single("arahin kamera ke kiri")
    assert (step.tool, step.action) == ("camera_ptz", "pan_left")


def test_camera_informal_geserin_recognized():
    step = _single("geserin kamera ke kanan")
    assert (step.tool, step.action) == ("camera_ptz", "pan_right")


def test_camera_informal_gerakin_goto_preset():
    step = _single("gerakin kamera ke pintu")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "pintu")


def test_camera_typo_target_fails_honestly_not_unknown():
    """"arahin kamera ke tangah" (typo for "tengah") still resolves to a
    real camera_ptz command (goto_preset target="tangah") rather than
    falling through to "unknown" - the real handler then fails honestly
    ("no preset named tangah") instead of the LLM fabricating a false
    "I can't move the camera" capability disclaimer with zero tool
    grounding."""
    step = _single("arahin kamera ke tangah")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "goto_preset", "tangah")


def test_camera_bare_tengah_with_verb_is_native_center_not_preset():
    """"arahkan kamera ke tengah" must use the camera's real native
    recenter/calibrate action, not a `goto_preset` lookup for a preset
    literally named "tengah" that only coincidentally may or may not
    exist."""
    step = _single("arahkan kamera ke tengah")
    assert (step.tool, step.action, step.target) == ("camera_ptz", "center", None)


def test_camera_informal_arahin_tengah_also_centers():
    step = _single("arahin kamera ke tengah")
    assert (step.tool, step.action) == ("camera_ptz", "center")


def test_camera_bare_tengah_without_verb_does_not_false_positive():
    """"tengah" alone is a common, generic Indonesian word ("middle") -
    without an accompanying move verb, "kamera ada di tengah meja" (the
    camera is in the middle of the table) must NOT be misread as a
    center/calibrate command."""
    step = _single("kamera ada di tengah meja")
    assert (step.tool, step.action) == ("unknown", "unknown")


def test_camera_goto_preset_is_independent_of_other_clauses():
    steps = IntentParser.parse("matikan lampu kamar dan arahkan kamera ke pintu")
    assert [(s.tool, s.action, s.target, s.depends_on_previous) for s in steps] == [
        ("home_assistant", "turn_off", "lampu_kamar", False),
        ("camera_ptz", "goto_preset", "pintu", False),
    ]


def test_camera_word_alone_without_move_verb_falls_back_to_unknown():
    """Regression guard: "kamera" appearing in an unrelated sentence (no
    move verb, no direction word) must not be misparsed as a PTZ
    command."""
    step = _single("kenapa kamera nggak nyala")
    assert (step.tool, step.action) == ("unknown", "unknown")


def test_camera_ptz_is_independent_of_other_clauses_in_same_sentence():
    """Same fix as the Home Assistant turn_on/turn_off independence -
    a camera pan command in a multi-clause utterance must not be
    structurally chained to (and therefore skippable by) an unrelated
    earlier clause."""
    steps = IntentParser.parse("matikan lampu kamar dan geser kamera ke kanan")
    assert [(s.tool, s.action, s.depends_on_previous) for s in steps] == [
        ("home_assistant", "turn_off", False),
        ("camera_ptz", "pan_right", False),
    ]


# -- LLM auto/manual routing mode (luno/routing/mode_state.py) --------------

def test_llm_mode_set_auto_indonesian():
    step = _single("pakai llm otomatis")
    assert (step.tool, step.action, step.target, step.depends_on_previous) == ("llm_mode", "set_auto", None, False)


def test_llm_mode_set_auto_english():
    step = _single("switch llm to auto")
    assert (step.tool, step.action, step.target) == ("llm_mode", "set_auto", None)


def test_llm_mode_set_manual_no_provider_indonesian():
    step = _single("pakai llm manual")
    assert (step.tool, step.action, step.target) == ("llm_mode", "set_manual", None)


def test_llm_mode_set_manual_with_provider_indonesian():
    step = _single("pakai llm openai")
    assert (step.tool, step.action, step.target) == ("llm_mode", "set_manual", "openai")


def test_llm_mode_set_manual_with_provider_english():
    step = _single("use llm claude")
    assert (step.tool, step.action, step.target) == ("llm_mode", "set_manual", "anthropic")


def test_llm_mode_provider_aliases_resolve_to_canonical_name():
    assert _single("pakai llm gpt").target == "gpt"
    assert _single("pakai llm chatgpt").target == "gpt"
    assert _single("pakai llm claude").target == "anthropic"
    assert _single("pakai llm anthropic").target == "anthropic"
    assert _single("pakai llm deepseek").target == "deepseek"
    assert _single("pakai llm gemini").target == "gemini"
    assert _single("pakai llm local").target == "local"
    assert _single("pakai llm openrouter").target == "openrouter"


def test_llm_mode_without_llm_keyword_does_not_false_positive():
    """A provider name mentioned in ordinary conversation, with no "llm"/
    "ai model" keyword anywhere in the clause, must not be misparsed as
    a mode-switch command."""
    step = _single("is gemini a real word")
    assert (step.tool, step.action) == ("unknown", "unknown")


def test_llm_mode_is_independent_of_other_clauses_in_same_sentence():
    steps = IntentParser.parse("matikan lampu kamar dan pakai llm manual")
    assert [(s.tool, s.action, s.depends_on_previous) for s in steps] == [
        ("home_assistant", "turn_off", False),
        ("llm_mode", "set_manual", False),
    ]


def test_bare_word_mati_without_verb_form_does_not_false_positive():
    """Regression guard for the new Indonesian patterns: they key off
    the actual verb forms ("matikan"/"matiin"), not the bare root "mati"
    ("dead"/"off" as an adjective) - "kenapa lampu itu mati?" ("why is
    that light off?") is a QUESTION, not a command, and must not be
    misparsed as a turn_off action."""
    step = _single("kenapa lampu itu mati")
    assert (step.tool, step.action) == ("unknown", "unknown")


# -- RGB color/brightness fix (reported: "di bagian HA kok ngga bisa set
# -- rgb strip warna sama brightnes?") -- `_SET_TO_RE` used to always
# -- produce action="set_value", which no handler ever supported for
# -- anything but a thermostat. `_classify_color_set`/`_classify_
# -- brightness_set` reclassify the same match into real, now-supported
# -- actions - see home_assistant.py/real_home_assistant.py.

def test_set_color_english():
    step = _single("set rgb strip to red")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"color": "red"})


def test_set_color_indonesian_verb_and_connector():
    step = _single("ubah rgb strip jadi biru")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"color": "biru"})


def test_set_color_indonesian_ganti_ke():
    step = _single("ganti lampu kamar ke merah")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "lampu_kamar", {"color": "merah"})


def test_set_color_with_explicit_color_word_in_target_is_stripped():
    """"set the color of rgb strip to red" / "set rgb strip color to
    red" must resolve to the same bare target as the plain phrasing -
    the optional "color"/"warna" word in the target is not part of the
    device name."""
    step = _single("set the color of rgb strip to red")
    assert (step.tool, step.action, step.target) == ("home_assistant", "set_color", "rgb_strip")

    step2 = _single("set rgb strip color to red")
    assert (step2.tool, step2.action, step2.target) == ("home_assistant", "set_color", "rgb_strip")


def test_set_brightness_english_requires_brightness_word():
    step = _single("set brightness of rgb strip to 80")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_brightness", "rgb_strip", {"level": 80})


def test_set_brightness_indonesian_kecerahan():
    step = _single("atur kecerahan lampu kamar ke 50")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_brightness", "lampu_kamar", {"level": 50})


def test_set_brightness_indonesian_terang():
    step = _single("atur terangnya rgb strip ke 30")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_brightness", "rgb_strip", {"level": 30})


def test_set_brightness_level_clamped_to_0_100():
    step = _single("set brightness of rgb strip to 150")
    assert step.params == {"level": 100}


def test_set_value_without_color_or_brightness_word_falls_back_unchanged():
    """Regression guard: a plain "set X to Y" whose value ISN'T a known
    color name and whose target has NO brightness word must still fall
    through to the original generic set_value behavior - e.g. "set the
    thermostat to 24" (never a color, never mentions brightness)."""
    step = _single("set the thermostat to 24")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_value", "thermostat", {"value": 24})


def test_set_value_bare_number_without_brightness_word_not_guessed_as_brightness():
    """"set rgb strip to 80" (no "brightness"/"kecerahan"/"terang" word
    anywhere) must NOT be guessed as brightness - stays plain set_value,
    per the "never guess, require an explicit signal" discipline."""
    step = _single("set rgb strip to 80")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_value", "rgb_strip", {"value": 80})


def test_set_color_and_brightness_independent_of_other_clauses():
    steps = IntentParser.parse("matikan lampu kamar dan set rgb strip to red")
    assert [(s.tool, s.action, s.target, s.depends_on_previous) for s in steps] == [
        ("home_assistant", "turn_off", "lampu_kamar", False),
        ("home_assistant", "set_color", "rgb_strip", False),
    ]


# -- Custom numeric RGB combo (reported: "dulu ada kombinasi warnanya di
# -- program" - the fixed 10-name palette alone isn't enough, need to be
# -- able to give raw RGB numbers too). Deliberately space/slash
# -- separated, never comma - see `_RGB_TRIPLET_RE`'s own comment for why
# -- comma can't work (it's already this parser's clause separator).

def test_set_color_custom_rgb_space_separated():
    step = _single("set rgb strip to 120 50 200")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"rgb": [120, 50, 200]})


def test_set_color_custom_rgb_with_leading_rgb_word():
    step = _single("set rgb strip to rgb 10 20 30")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"rgb": [10, 20, 30]})


def test_set_color_custom_rgb_slash_separated():
    step = _single("set rgb strip to 120/50/200")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"rgb": [120, 50, 200]})


def test_set_color_custom_rgb_indonesian():
    step = _single("atur warna rgb strip ke 100 150 200")
    assert (step.tool, step.action, step.target, step.params) == ("home_assistant", "set_color", "rgb_strip", {"rgb": [100, 150, 200]})


def test_set_color_custom_rgb_out_of_range_values_clamped():
    step = _single("set rgb strip to 300 -5 999")
    assert step.params == {"rgb": [255, 0, 255]}


def test_set_color_named_color_still_takes_priority_shape():
    """A plain color name must still produce the {"color": name} shape,
    never {"rgb": [...]} - the two are mutually exclusive per match,
    named-color check happens first."""
    step = _single("set rgb strip to blue")
    assert step.params == {"color": "blue"}


def test_set_color_single_number_not_treated_as_rgb_triplet():
    """Exactly one number (not three) must never be guessed as a partial
    RGB triplet - falls through to plain set_value, same as before this
    feature existed."""
    step = _single("set rgb strip to 80")
    assert (step.tool, step.action, step.params) == ("home_assistant", "set_value", {"value": 80})


def test_set_color_four_numbers_not_treated_as_rgb_triplet():
    step = _single("set rgb strip to 10 20 30 40")
    assert step.tool == "home_assistant" and step.action == "set_value"
