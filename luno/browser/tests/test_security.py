"""
test_security.py
==================

`luno.browser.security` - domain allowlist, credential redaction,
download path validation. Pure functions, no browser/network needed.
"""

from __future__ import annotations

from luno.browser.security import (
    extract_domain, is_domain_allowed, redact_secrets, validate_download_path,
)


# -- extract_domain / is_domain_allowed ---------------------------------------

def test_extract_domain_with_scheme():
    assert extract_domain("https://github.com/foo/bar") == "github.com"


def test_extract_domain_without_scheme():
    assert extract_domain("github.com/foo") == "github.com"


def test_extract_domain_empty():
    assert extract_domain("") == ""


def test_empty_allowlist_allows_everything():
    ok, reason = is_domain_allowed("https://anything.example", [])
    assert ok is True
    assert "no allowlist" in reason


def test_allowlist_allows_exact_domain():
    ok, _ = is_domain_allowed("https://github.com/foo", ["github.com"])
    assert ok is True


def test_allowlist_allows_subdomain():
    ok, _ = is_domain_allowed("https://docs.github.com/foo", ["github.com"])
    assert ok is True


def test_allowlist_rejects_unlisted_domain():
    ok, reason = is_domain_allowed("https://evil.com", ["github.com"])
    assert ok is False
    assert "github.com" in reason


def test_allowlist_rejects_lookalike_domain_attack():
    """"github.com.evil.com" must NOT pass just because it contains
    "github.com" as a substring - a naive `in` check would let this
    through."""
    ok, _ = is_domain_allowed("https://github.com.evil.com", ["github.com"])
    assert ok is False


def test_allowlist_rejects_unparseable_url():
    ok, reason = is_domain_allowed("not a url at all $$$", ["github.com"])
    assert ok is False


# -- redact_secrets -------------------------------------------------------------

def test_redact_secrets_none_and_empty():
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""


def test_redact_secrets_api_key_query_param():
    text = "fetched https://api.example.com/data?api_key=sk-abc123secret"
    result = redact_secrets(text)
    assert "sk-abc123secret" not in result
    assert "REDACTED" in result


def test_redact_secrets_password_field():
    text = "form field password=hunter2 was filled"
    result = redact_secrets(text)
    assert "hunter2" not in result


def test_redact_secrets_bearer_token():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
    result = redact_secrets(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in result


def test_redact_secrets_userinfo_url():
    text = "downloaded from https://user:supersecret@example.com/file"
    result = redact_secrets(text)
    assert "supersecret" not in result


def test_redact_secrets_leaves_normal_text_alone():
    text = "The page title was 'Home Assistant Dashboard'."
    assert redact_secrets(text) == text


# -- validate_download_path ------------------------------------------------------

def test_download_path_accepts_plain_filename():
    ok, resolved = validate_download_path("report.pdf", "/tmp/luno_downloads")
    assert ok is True
    assert resolved.endswith("report.pdf")
    assert "/tmp/luno_downloads" in resolved


def test_download_path_rejects_traversal():
    ok, reason = validate_download_path("../../etc/passwd", "/tmp/luno_downloads")
    assert ok is False
    assert "outside" in reason


def test_download_path_rejects_absolute_path_elsewhere():
    ok, reason = validate_download_path("/etc/passwd", "/tmp/luno_downloads")
    assert ok is False


def test_download_path_rejects_empty_destination():
    ok, reason = validate_download_path("", "/tmp/luno_downloads")
    assert ok is False


def test_download_path_rejects_no_download_dir_configured():
    ok, reason = validate_download_path("file.txt", "")
    assert ok is False
