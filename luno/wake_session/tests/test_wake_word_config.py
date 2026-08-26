"""
test_wake_word_config.py
===========================

Regression suite for the "Wake Word Configuration Loading" bug fix.

Root cause: `WakeSessionConfig.from_env()` only ever read `WAKE_WORDS`
(plural, comma-separated - this package's OWN invented env var), and
fell back to an invented `["luno", "hey luno", "hi luno"]` default that
had no grounding anywhere else in the project. It never read `WAKE_WORD`
(singular) - the project's actual pre-existing, established env var,
already read by `luno/config.py` (default `"alexa"`) and consumed by
`luno/main.py`'s real acoustic wake-word pipeline. A deployment that
only ever configured (or relied on the default of) `WAKE_WORD` saw the
new event-driven runtime silently substitute a completely different,
disconnected wake word list the moment it started using
`SessionManagerModule` - exactly the reported symptom (Runtime log
shows `wake_words=['luno', 'hey luno', 'hi luno']` even though the
project is configured for `alexa`).

Audit findings (see also this fix's own project-wide search, matching
the task's own instructions):
    - No `config.json`/`config.yaml`/`settings.json`/`.env.local` file
      defining wake words exists anywhere in this project - the two
      REAL sources are both environment variables: `WAKE_WORDS`
      (plural, this package) and `WAKE_WORD` (singular, legacy -
      `luno/config.py` / `luno/main.py`).
    - `OPENWAKEWORD_MODEL` (set in `.env`) is a THIRD, unrelated env var
      - an openWakeWord acoustic MODEL name (e.g. "alexa", "hey_google"),
      not a text phrase list - and is not read by any code path in this
      project today. Left untouched (out of scope - wiring it up would
      touch the separate acoustic wake-word pipeline in `luno/main.py`,
      not this bug).

Fix: `WakeSessionConfig._resolve_wake_words()` (see `models.py`)
implements one explicit precedence order: `WAKE_WORDS` env var >
`WAKE_WORD` env var (legacy) > built-in default (mirrors
`luno/config.py`'s own `"alexa"` default, not an invented one). The
resolved `wake_words_source`/`wake_words_conflict_warning` are recorded
on the config itself and logged by `SessionManagerModule` at
construction time AND on every `/reload` - never silent about where the
value came from.

Covers the task's own numbered regression list:
    1. Wake words loaded from .env (WAKE_WORDS)
    2. Wake words loaded from the legacy WAKE_WORD source (this
       project's real second source - no config file exists to load
       from, see audit notes above)
    3. Runtime reload updates wake words
    4. Conflicting configuration sources
    5. Missing configuration falls back to defaults
    6. Custom wake word ("alexa") survives reload (no restart needed)
    7. Multiple wake words
    8. Empty wake word configuration
    9. Invalid configuration
    10. Startup log shows configuration source

Run:
    python3 -m luno.wake_session.tests.test_wake_word_config
"""

from __future__ import annotations

import io
import os
import sys
import time
from contextlib import redirect_stdout
from typing import Callable, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.wake_session.manager import SessionManagerModule  # noqa: E402
from luno.wake_session.models import WakeSessionConfig  # noqa: E402

_ENV_KEYS = ("WAKE_WORDS", "WAKE_WORD")


class _EnvSandbox:
    """Saves/restores exactly the env vars this suite touches - so tests
    never leak configuration into each other or into the rest of the
    process, matching the save/restore pattern already used by
    `test_wake_session.py`'s own `test_config_from_env_reads_barge_in_
    word_lists_as_fallback`."""

    def __enter__(self) -> "_EnvSandbox":
        self._old = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def set(**kwargs: str) -> None:
        for k, v in kwargs.items():
            os.environ[k] = v


def _silent(fn, *a, **kw) -> Tuple[object, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# 1 - wake words loaded from .env (WAKE_WORDS, this package's own knob)
# ============================================================================

@scenario
def test_1_wake_words_loaded_from_env_WAKE_WORDS():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="alexa, hey alexa")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["alexa", "hey alexa"]
        assert "WAKE_WORDS" in cfg.wake_words_source
        assert cfg.wake_words_conflict_warning is None


# ============================================================================
# 2 - wake words loaded from the legacy WAKE_WORD source (this project's
#     real "second source" - no config.json/yaml exists anywhere to load
#     wake words from, confirmed by this fix's own project-wide audit)
# ============================================================================

@scenario
def test_2_wake_words_loaded_from_legacy_WAKE_WORD_when_WAKE_WORDS_unset():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORD="alexa")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["alexa"]
        assert "WAKE_WORD" in cfg.wake_words_source
        assert "legacy" in cfg.wake_words_source.lower()
        assert cfg.wake_words_conflict_warning is None


# ============================================================================
# 3 - Runtime reload updates wake words, no restart required
# ============================================================================

@scenario
def test_3_runtime_reload_updates_wake_words_without_restart():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORD="alexa")
        module, startup_log = _silent(SessionManagerModule)
        assert module.config.wake_words == ["alexa"]
        assert "Wake words loaded from:" in startup_log
        assert "alexa" in startup_log.lower()

        # change configuration WITHOUT restarting/reconstructing the module
        os.environ["WAKE_WORD"] = "luno"
        _, reload_log = _silent(module.reload)

        assert module.config.wake_words == ["luno"]
        assert module.session.config.wake_words == ["luno"], "ConversationSession must also see the new config"
        assert "Wake words loaded from:" in reload_log
        assert "luno" in reload_log.lower()


# ============================================================================
# 4 - conflicting configuration sources
# ============================================================================

@scenario
def test_4_conflicting_sources_produces_warning_and_uses_higher_priority():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="luno", WAKE_WORD="alexa")
        cfg = WakeSessionConfig.from_env()
        # WAKE_WORDS (higher priority - explicit multi-alias override) wins.
        assert cfg.wake_words == ["luno"]
        assert cfg.wake_words_conflict_warning is not None
        assert "WAKE_WORDS" in cfg.wake_words_conflict_warning
        assert "WAKE_WORD" in cfg.wake_words_conflict_warning
        assert "alexa" in cfg.wake_words_conflict_warning
        assert "luno" in cfg.wake_words_conflict_warning

        # module construction must surface the warning too, not swallow it.
        _, startup_log = _silent(SessionManagerModule)
        assert "WARNING" in startup_log


@scenario
def test_4b_matching_sources_produce_no_conflict_warning():
    """Same resolved value from both env vars is NOT a conflict - only
    genuinely DIFFERENT values should warn."""
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="alexa", WAKE_WORD="alexa")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["alexa"]
        assert cfg.wake_words_conflict_warning is None


# ============================================================================
# 5 - missing configuration falls back to the (correct) default
# ============================================================================

@scenario
def test_5_missing_configuration_falls_back_to_default():
    with _EnvSandbox():
        cfg = WakeSessionConfig.from_env()
        # the fallback mirrors luno/config.py's OWN WAKE_WORD default
        # ("alexa") - not an invented, disconnected default.
        assert cfg.wake_words == ["alexa"]
        assert "built-in default" in cfg.wake_words_source
        assert cfg.wake_words_conflict_warning is None


# ============================================================================
# 6 - the custom wake word survives reload (never silently replaced)
# ============================================================================

@scenario
def test_6_custom_wake_word_alexa_survives_reload():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORD="alexa")
        module, _ = _silent(SessionManagerModule)
        assert module.config.wake_words == ["alexa"]

        # reload repeatedly with the SAME configuration still in place -
        # must never drift toward the built-in/invented defaults.
        for _ in range(5):
            _silent(module.reload)
            assert module.config.wake_words == ["alexa"], "custom wake word was silently replaced by a reload"
        assert module.session.config.wake_words == ["alexa"]


# ============================================================================
# 7 - multiple wake words
# ============================================================================

@scenario
def test_7_multiple_wake_words_all_match():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="alexa, hey alexa, computer")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["alexa", "hey alexa", "computer"]

        from luno.wake_session.matcher import match_wake_word
        for phrase in ("alexa", "hey alexa there", "computer, what time is it"):
            match = match_wake_word(phrase, cfg.wake_words)
            assert match is not None, f"{phrase!r} should have matched one of {cfg.wake_words}"


# ============================================================================
# 8 - empty wake word configuration falls back gracefully
# ============================================================================

@scenario
def test_8_empty_wake_word_configuration_falls_back_to_default():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="", WAKE_WORD="")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words, "empty configuration must never resolve to an empty (unusable) wake word list"
        assert cfg.wake_words == ["alexa"]
        assert "built-in default" in cfg.wake_words_source


@scenario
def test_8b_whitespace_only_wake_words_falls_back_to_default():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORDS="   ")
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["alexa"]


# ============================================================================
# 9 - invalid configuration does not crash, resolves to something sane
# ============================================================================

@scenario
def test_9_invalid_configuration_does_not_crash():
    with _EnvSandbox():
        # garbage separators / stray commas - must not raise, must not
        # produce a list full of empty strings.
        _EnvSandbox.set(WAKE_WORDS=",,,   ,,")
        cfg = WakeSessionConfig.from_env()
        assert all(w.strip() for w in cfg.wake_words), f"invalid config produced blank entries: {cfg.wake_words}"
        assert cfg.wake_words  # falls back rather than being empty

        # also must not crash constructing a whole module with garbage config.
        _silent(SessionManagerModule)


# ============================================================================
# 10 - startup log shows the configuration source, not just the final list
# ============================================================================

@scenario
def test_10_startup_log_shows_configuration_source():
    with _EnvSandbox():
        _EnvSandbox.set(WAKE_WORD="alexa")
        module, startup_log = _silent(SessionManagerModule)
        assert "Wake words loaded from:" in startup_log
        assert "Wake words:" in startup_log
        assert "alexa" in startup_log.lower()
        # never JUST the final list with no indication of where it came from.
        source_line_present = any(
            "Wake words loaded from:" in line for line in startup_log.splitlines()
        )
        assert source_line_present

        # status_snapshot() must expose the same information for the
        # console's own /status, /session, /reload output to surface.
        snap = module.status_snapshot()
        assert snap["config"]["wake_words_source"] == module.config.wake_words_source
        assert snap["config"]["wake_words"] == ["alexa"]


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"[FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"[ERROR] {name}: {ex}")
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
