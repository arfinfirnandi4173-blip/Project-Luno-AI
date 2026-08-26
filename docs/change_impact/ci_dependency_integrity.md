# Change Impact Analysis — CI Dependency Integrity

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md`.

```
FEATURE:
CI Dependency Integrity

WHY:
A truly clean, from-scratch virtualenv built from exactly
`.github/workflows/regression.yml`'s own install line cannot pass the
`luno/` FAST suite it claims to run - 23 of the 25 failures are not
pre-existing-flaky, they are a real, reproducible dependency gap
(confirmed by rebuilding the clean venv fresh for this sprint - see
"Baseline" in the sprint report). CI must accurately reproduce the
environment the test suite it runs actually needs, or it is not a
trustworthy regression guard.

FILES TO CHANGE:
- .github/workflows/regression.yml (add the one missing package to the
  existing pip install line - requirements.txt itself already correctly
  declares it, see ROOT CAUSE below, so requirements.txt needs no change)

DIRECTLY AFFECTED SUBSYSTEMS:
- CI installation step only. No production source file is touched.

INDIRECTLY AFFECTED SUBSYSTEMS:
- luno/adapters/tests/test_fish_audio_api.py (23 tests) goes from
  failing-in-a-clean-venv to passing-in-a-clean-venv - a test-visibility
  fix, not a behavior change to the adapter itself.

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- None. This sprint does not touch any contract - `luno/adapters/
  fish_audio_real.py`'s existing soft-import/graceful-degradation
  behavior (`try: import ormsgpack ... except ImportError: _ormsgpack =
  None`, engine auto-disables to Mock TTS if absent) is UNCHANGED. The
  fix only ensures CI's install step matches what CI's own already-
  existing test scope (all of `luno/`, including this file) requires to
  actually exercise that engine's real code path.

ROOT CAUSE (found during audit, before any change was made):
`ormsgpack` is NOT missing from the project's dependency declaration -
it is already correctly declared in `requirements.txt` (`ormsgpack>=1.5.0`,
under its own clearly-labeled "Fish Audio CLOUD TTS API (opsional)"
section, with an accurate comment describing exactly the graceful-
degradation behavior confirmed by inspection). The actual gap is that
`.github/workflows/regression.yml` was NEVER designed to `pip install -r
requirements.txt` (that file also bundles heavy, hardware-dependent
packages - faster-whisper, sounddevice+PortAudio, opencv-python,
ultralytics, playwright - the FAST suite intentionally avoids); instead
it hand-installs a minimal "Core" set (python-dotenv, requests,
websockets, openai, pytest). Whoever hand-picked that minimal set
(the prior Regression & Architecture Guard sprint) reasoned from
`requirements.txt`'s own "Core" section header and never cross-checked
it against every test file `luno/ -q` actually collects - `ormsgpack`
sits in its own separate, correctly-labeled "optional" section, not
"Core", so it was never included in the hand-picked list, even though
`luno/adapters/tests/test_fish_audio_api.py` (part of `luno/`, which the
workflow DOES fully run) directly imports and exercises the real
msgpack-encoding code path in `luno/adapters/fish_audio_real.py`.

DEPENDENCY CLASSIFICATION (see sprint brief §5/§6):
- Import location: luno/adapters/fish_audio_real.py, line 73 (`import
  ormsgpack as _ormsgpack`) - the ONLY file in the entire repository
  that imports it (confirmed via `grep -rl ormsgpack`).
- Import time: SOFT/guarded (`try/except ImportError`) - never required
  at module import time, never breaks `import luno.adapters` or any
  other module. Confirmed empirically: the clean-venv run failed exactly
  the 23 tests that exercise the real synthesis code path, not a
  package-wide collection error.
- Direct or transitive: DIRECT (imported by name, not pulled in as a
  sub-dependency of another package).
- Production or test-only: BOTH. Production: needed only if a real
  deployment sets `TTS_ENGINE=fish_audio_api` (one of several TTS
  engines - gptsovits/f5tts/Mock are unaffected either way, per
  requirements.txt's own comment). Test: `test_fish_audio_api.py`
  exercises the REAL adapter's real msgpack encoding directly (it does
  not mock `ormsgpack` itself), so for those specific 23 tests to run
  meaningfully at all, the real package must be present - this is not a
  case of "a test imports something it doesn't really need."
- Already declared: YES, in requirements.txt, with a sound, already-
  unpinned-lower-bound-only constraint (`>=1.5.0`) consistent with every
  other line in that file (no upper bounds anywhere in the file).
- Version compatibility: this sandbox's already-installed 1.12.2
  satisfies `>=1.5.0` with no known incompatibility; the fix reuses
  requirements.txt's own existing constraint verbatim rather than
  inventing a new one.

EXPECTED REGRESSION RISKS:
- Low. Adding one small, pure-Python-C-extension package (no native
  system library, no GPU, no network access needed to install or run)
  to a CI install line that already installs 5 packages. `pip check`
  will be re-verified clean after the change.

TESTS TO RUN:
- Fresh venv: python -m pytest luno/adapters/tests/test_fish_audio_api.py -q
- Fresh venv: python -m pytest tests/test_emotion_engine.py -q
- Fresh venv: python -m pytest tests/test_runtime_demo.py::<2 named Emotion Engine node IDs> -q
- Fresh venv: python -m pytest luno/ -q (full FAST suite)
- python -m pip check (both before and after)
- Reproducibility: destroy the venv, build a second one from zero, repeat all of the above

NEW TESTS REQUIRED:
- None - this sprint does not add functionality, it makes an existing,
  already-written test file (test_fish_audio_api.py) actually
  executable in the environment CI claims to provide. No test is
  weakened, skipped, or modified.

ROLLBACK PLAN:
Revert the one added line in `.github/workflows/regression.yml`
(remove `ormsgpack>=1.5.0` from the pip install step). No other file
changes anything reversible-sensitive - requirements.txt is unchanged.
```
