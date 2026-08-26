"""
test_desktop_control.py
==========================

`guess_fallback_search_url()` - AppNotFound browser-fallback feature
(reported gap: "buka channel Mr beast di youtube" used to just fail
with "not registered", nothing else). Pure function, no I/O - takes a
slugified failed app-open target and returns (url, human-readable
label), platform-aware (youtube/spotify/etc) with a Google fallback.
"""

from __future__ import annotations

from urllib.parse import unquote_plus

from luno.desktop_control import APPS, guess_fallback_search_url, open_app


def test_youtube_platform_detected_and_di_stripped():
    url, label = guess_fallback_search_url("channel_mr_beast_di_youtube")
    assert url.startswith("https://www.youtube.com/results?search_query=")
    assert "youtube.com" in url
    assert "channel mr beast" in unquote_plus(url)
    assert "YouTube" in label
    assert "channel mr beast" in label


def test_spotify_platform_detected():
    url, label = guess_fallback_search_url("some_song_di_spotify")
    assert url.startswith("https://open.spotify.com/search/")
    assert "some song" in unquote_plus(url)
    assert "Spotify" in label


def test_netflix_instagram_twitter_tiktok_github_all_map_to_their_own_site():
    cases = {
        "stranger_things_di_netflix": "netflix.com",
        "some_account_di_instagram": "instagram.com",
        "some_account_di_twitter": "twitter.com",
        "some_video_di_tiktok": "tiktok.com",
        "some_repo_di_github": "github.com",
    }
    for target, expected_domain in cases.items():
        url, _label = guess_fallback_search_url(target)
        assert expected_domain in url, f"{target} -> {url}"


def test_no_platform_word_falls_back_to_google():
    url, label = guess_fallback_search_url("notepad_plus_plus")
    assert url.startswith("https://www.google.com/search?q=")
    assert "notepad plus plus" in unquote_plus(url)
    assert "Google" in label


def test_platform_word_is_not_a_registered_app_prefix_bug():
    """Regression guard: the platform word itself must never leak into
    the search QUERY (only used to pick which SITE to search) - "beli
    hp murah di youtube" must search "beli hp murah", not
    "beli hp murah youtube"."""
    url, _label = guess_fallback_search_url("beli_hp_murah_di_youtube")
    decoded = unquote_plus(url)
    assert "youtube" not in decoded.split("search_query=")[-1] if "search_query=" in decoded else True
    assert "beli hp murah" in decoded


def test_empty_target_does_not_crash():
    url, label = guess_fallback_search_url("")
    assert isinstance(url, str) and url
    assert isinstance(label, str) and label


def test_bare_platform_word_alone_still_produces_a_sane_url():
    """"buka youtube" (nothing else) - query degrades to the platform
    word itself rather than an empty/crashing query string."""
    url, label = guess_fallback_search_url("youtube")
    assert "youtube.com" in url
    assert isinstance(label, str) and label


# -- open_app() failure message stays short (reported gap) -------------------

def test_open_app_not_registered_message_never_lists_other_registered_apps():
    """Regression guard: the failure message used to enumerate every
    registered app ("Yang sudah ada: steam, chrome, ...") - every reply
    the LLM built from it inherited that length. The message must stay
    short (just the ONE app that failed) - the LLM is trusted to phrase
    the actual reply naturally on its own, no app list needed."""
    ok, message = open_app("definitely_not_a_real_registered_app_xyz")
    assert ok is False
    assert "definitely_not_a_real_registered_app_xyz" in message
    # None of the OTHER currently-registered apps' names should appear
    # in the message - this is the exact regression the old ", ".join
    # (APPS.keys()) behavior would trigger.
    for known_app_name in APPS.keys():
        assert known_app_name not in message.lower(), (
            f"open_app() failure message still leaks registered app {known_app_name!r}: {message!r}"
        )
    assert len(message) < 100, f"message unexpectedly long ({len(message)} chars): {message!r}"
