"""
test_intent.py
================

`luno.browser.intent` - the four deterministic classifiers, plus
regression guards proving they don't false-positive on vision/camera-
PTZ/home-assistant/memory/normal-conversation phrasing (spec section 19's
explicit requirement).
"""

from __future__ import annotations

from luno.browser.intent import (
    classify_computer_use_intent, classify_image_search_intent, classify_monitoring_intent,
    classify_research_intent,
)


# -- research intent --------------------------------------------------------------

def test_research_strong_verb_carikan():
    assert classify_research_intent("Carikan harga RTX 5060 Ti 16GB.") is not None


def test_research_cari_with_documentation_marker():
    assert classify_research_intent("Cari dokumentasi resmi API ini") is not None


def test_research_cari_with_penyebab_marker():
    assert classify_research_intent("Cari penyebab error ini") is not None


def test_research_bandingkan_with_produk_marker():
    assert classify_research_intent("Bandingkan dua produk ini") is not None


def test_research_english_search_with_marker():
    assert classify_research_intent("search for the price of this GPU") is not None


def test_research_returns_verbatim_text():
    text = "Carikan harga RTX 5060 Ti 16GB."
    assert classify_research_intent(text) == text


def test_research_no_match_on_plain_cari_without_marker():
    """Bare "cari" with no info-seeking marker is too weak alone (e.g.
    "aku lagi cari kunci" - looking for keys, nothing to research)."""
    assert classify_research_intent("aku lagi cari kunci motor") is None


def test_research_empty_text():
    assert classify_research_intent("") is None
    assert classify_research_intent(None) is None


def test_research_cek_harga_reported_gap():
    """Reported gap: "Cek Harga RTX3060 Di browser" fell through to
    plain conversation entirely (neither "cek" nor "check" was in
    `_RESEARCH_VERBS` at all) - the LLM would have guessed a price from
    stale training data instead of actually searching."""
    assert classify_research_intent("Cek Harga RTX3060 Di browser") is not None


def test_research_cek_with_documentation_marker():
    assert classify_research_intent("cek dokumentasi resmi API ini") is not None


def test_research_check_english_with_price_marker():
    assert classify_research_intent("check the price of this GPU") is not None


def test_research_explicit_browser_word_is_itself_a_marker():
    assert classify_research_intent("cek RTX3060 di browser") is not None


def test_research_no_match_on_plain_cek_without_marker():
    """"cek" alone still needs an info marker, same conservatism as
    "cari" - "cek dulu ya" (just checking in) has no marker at all."""
    assert classify_research_intent("cek dulu ya") is None


def test_research_cek_server_still_does_not_trigger_research():
    """"cek server"/"cek dashboard"-style monitoring phrases must stay
    exclusively `classify_monitoring_intent`'s territory - `_INFO_
    MARKERS` and `_MONITOR_NOUNS` are disjoint on purpose."""
    assert classify_research_intent("cek server") is None
    assert classify_research_intent("cek Portainer") is None


def test_research_cek_device_check_still_does_not_trigger_research():
    """"cek lampu kamar" (a Home Assistant device check) must not be
    swept into research just because "cek" is now a research verb."""
    assert classify_research_intent("cek lampu kamar udah nyala belum") is None


# -- image search intent -------------------------------------------------------------

def test_image_search_cari_gambar():
    assert classify_image_search_intent("cari gambar kucing lucu") == "kucing lucu"


def test_image_search_carikan_foto():
    assert classify_image_search_intent("carikan foto rumah minimalis") == "rumah minimalis"


def test_image_search_english():
    assert classify_image_search_intent("search images of golden retriever puppies") == "golden retriever puppies"


def test_image_search_no_match_without_image_word():
    assert classify_image_search_intent("cari tahu soal kucing") is None


def test_image_search_no_match_without_search_verb():
    assert classify_image_search_intent("gambarnya bagus banget") is None


def test_image_search_empty_text():
    assert classify_image_search_intent("") is None
    assert classify_image_search_intent(None) is None


# -- monitoring intent --------------------------------------------------------------

def test_monitoring_cek_server():
    assert classify_monitoring_intent("cek server") is True


def test_monitoring_check_my_server():
    assert classify_monitoring_intent("check my server") is True


def test_monitoring_lihat_portainer():
    assert classify_monitoring_intent("lihat Portainer dong") is True


def test_monitoring_cek_docker():
    assert classify_monitoring_intent("cek container docker") is True


def test_monitoring_no_match_without_noun():
    """"cek" alone (no server/dashboard-shaped noun) must not match -
    otherwise this would swallow "cek jadwal saya" etc."""
    assert classify_monitoring_intent("cek jadwal saya besok") is False


def test_monitoring_no_match_on_device_check():
    """"cek lampu" (a Home Assistant device check) must stay with the
    existing device-intent path, not get hijacked into "monitoring"."""
    assert classify_monitoring_intent("cek lampu kamar udah nyala belum") is False


def test_monitoring_empty_text():
    assert classify_monitoring_intent("") is False


# -- computer-use intent --------------------------------------------------------------

def test_computer_use_buka_dan_kenapa():
    task = classify_computer_use_intent("buka Unity dan lihat kenapa avatar saya error")
    assert task is not None


def test_computer_use_open_and_why():
    assert classify_computer_use_intent("open the app and tell me why it's failing") is not None


def test_computer_use_no_match_plain_open():
    """"buka spotify" (no diagnostic word) must stay a plain app-open
    command, not get pulled into the computer-use loop."""
    assert classify_computer_use_intent("buka spotify") is None


def test_computer_use_no_match_diagnostic_without_open():
    """"kenapa kamera nggak nyala" (diagnostic word, no open/launch verb)
    must stay plain conversation, not misfire as computer-use."""
    assert classify_computer_use_intent("kenapa kamera nggak nyala") is None


def test_computer_use_empty_text():
    assert classify_computer_use_intent("") is None


# -- cross-classifier false-positive regressions (spec section 19) ------------------

_UNRELATED_UTTERANCES = (
    "arahkan kamera ke tengah",         # camera_ptz
    "geser kamera ke kanan",            # camera_ptz
    "nyalakan lampu kamar",             # home_assistant
    "matikan rgb strip",                # home_assistant
    "ada apa di kamera",                # vision
    "lihat kamera dong",                # vision
    "inget ya aku alergi kacang",       # memory
    "gimana kabarnya hari ini",         # plain conversation
    "aku lagi sedih nih",               # plain conversation
    "cek lampu kamar udah nyala belum", # home_assistant device check ("cek" is now a research verb too)
    "cek jadwal saya besok",            # plain conversation, "cek" without a real marker
)


def test_camera_and_ha_and_vision_phrases_never_trigger_research():
    for text in _UNRELATED_UTTERANCES:
        assert classify_research_intent(text) is None, f"false positive (research): {text!r}"


def test_camera_and_ha_and_vision_phrases_never_trigger_monitoring():
    for text in _UNRELATED_UTTERANCES:
        assert classify_monitoring_intent(text) is False, f"false positive (monitoring): {text!r}"


def test_camera_and_ha_and_vision_phrases_never_trigger_computer_use():
    for text in _UNRELATED_UTTERANCES:
        assert classify_computer_use_intent(text) is None, f"false positive (computer-use): {text!r}"


def test_camera_and_ha_and_vision_phrases_never_trigger_image_search():
    for text in _UNRELATED_UTTERANCES:
        assert classify_image_search_intent(text) is None, f"false positive (image search): {text!r}"


def test_plain_research_request_never_triggers_image_search():
    """A text-research request ("cari dokumentasi resmi API ini") has no
    image word - must stay with `classify_research_intent`, never get
    hijacked into opening a visible image-search browser window."""
    assert classify_image_search_intent("cari dokumentasi resmi API ini") is None


def test_image_search_request_still_may_also_look_like_research_verb_but_routes_correctly():
    """Both classifiers independently agreeing "carikan" is a search verb
    is fine - what matters is `classify_image_search_intent` only ACTS
    when an image word is also present, and vice versa this doesn't
    prevent `classify_research_intent` from separately matching if the
    caller checks both (main_runtime_demo.py checks image search
    first - see that module's own wiring)."""
    assert classify_image_search_intent("carikan gambar kucing") is not None
    assert classify_research_intent("carikan gambar kucing") is not None  # both fire; caller orders them
