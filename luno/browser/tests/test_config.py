"""
test_config.py
================

`luno.browser.config` - `BrowserConfig.from_env()` and
`load_monitor_targets()`. Uses `monkeypatch.setenv`/a temp JSON file
rather than touching the real `.env`/`config/browser_monitor_targets.json`.
"""

from __future__ import annotations

import json
import os

from luno.browser.config import BrowserConfig, load_monitor_targets


def test_defaults_when_nothing_set(monkeypatch):
    for key in (
        "BROWSER_ENABLED", "BROWSER_HEADLESS", "BROWSER_PROFILE_DIR", "BROWSER_MAX_STEPS",
        "BROWSER_ALLOWED_DOMAINS", "BROWSER_REQUIRE_CONFIRMATION",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = BrowserConfig.from_env()
    assert cfg.enabled is False
    assert cfg.headless is True
    assert cfg.max_steps == 10
    assert cfg.allowed_domains == ()
    assert cfg.require_confirmation is True


def test_enabled_true_parses(monkeypatch):
    monkeypatch.setenv("BROWSER_ENABLED", "true")
    assert BrowserConfig.from_env().enabled is True


def test_allowed_domains_csv_parsing(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "github.com, portainer.local ,grafana.local")
    cfg = BrowserConfig.from_env()
    assert cfg.allowed_domains == ("github.com", "portainer.local", "grafana.local")


def test_max_steps_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BROWSER_MAX_STEPS", "not-a-number")
    assert BrowserConfig.from_env().max_steps == 10


def test_load_monitor_targets_missing_file_returns_empty(tmp_path):
    targets = load_monitor_targets(str(tmp_path / "does_not_exist.json"))
    assert targets == []


def test_load_monitor_targets_valid_file(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([
        {"name": "Portainer", "url": "http://x:9000", "type": "portainer", "enabled": True},
        {"name": "Grafana", "url": "http://x:3000", "type": "grafana", "enabled": False},
    ]))
    targets = load_monitor_targets(str(path))
    assert len(targets) == 2
    assert targets[0].name == "Portainer"
    assert targets[1].enabled is False


def test_load_monitor_targets_skips_entries_missing_url(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([{"name": "Broken"}]))
    targets = load_monitor_targets(str(path))
    assert targets == []


def test_load_monitor_targets_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text("{not valid json")
    targets = load_monitor_targets(str(path))
    assert targets == []


def test_load_monitor_targets_unknown_type_falls_back_to_generic(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([{"name": "X", "url": "http://x", "type": "something_weird"}]))
    targets = load_monitor_targets(str(path))
    assert targets[0].type == "generic"
