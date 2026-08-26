"""
real_whisper.py
=================

Real `WhisperSource` implementation for `WhisperAdapter` (see
`whisper.py`) - a continuous microphone-listen-and-transcribe loop
reusing the EXISTING, already-tested `get_whisper_model()`/
`transcribe_audio()` pair from `legacy_main.py` (Faster-Whisper, local/
offline - see that file's own "SPEECH-TO-TEXT" section) - neither
function is duplicated or modified here, only imported and called.

Design choice - no separate wake-word-spotting model here: unlike
`legacy_main.py`'s procedural `mode_wake_word()` (a dedicated
`openwakeword` model that ONLY spots the wake word, then swaps into a
completely different Whisper-based command-listening mode once
triggered), this event-driven architecture already has a purpose-built
wake-word layer: `luno.wake_session.SessionManagerModule` independently
pattern-matches EVERY raw `SpeechRecognized` transcript against the
configured wake word(s) (`match_wake_word()`) and decides whether the
utterance should wake Luno up, extend an active session, or be dropped
while dormant - see that package's own docstring. Running a SECOND,
separate wake-word-spotting model at the Whisper Source layer would
duplicate that decision in two places using two different mechanisms
and risk them disagreeing; instead, this source simply transcribes
EVERYTHING it hears (like the developer console's `simulate_speech()`
already does for typed input) and lets `SessionManagerModule` make the
one, single wake-word decision - exactly the architecture the package's
own "Quick start" docstring describes. This also means barge-in
("stop"/"wait"/... spoken while Luno is talking) works for free: the
mic never stops listening, `BargeInModule` independently fans out off
the same raw stream (see `luno/barge_in/__init__.py`'s own docstring:
"both subscribe independently to the same raw speech_recognized
events").

Ambient noise calibration discipline: calibrated ONCE, up front, with
`recognizer.dynamic_energy_threshold = False` - the exact fix this
project already applied (this session) to `legacy_main.py`'s own
barge-in listener after discovering that repeatedly recalibrating WHILE
Luno is speaking corrupts the energy threshold against Luno's own
voice. Mirrored here deliberately rather than re-deriving it, since
this source runs continuously (including while Luno is speaking, by
design - see above) and would hit the exact same failure mode
otherwise.

Opt-in only: `WHISPER_BACKEND=real` (see
`luno/bootstrap/launcher_config.py`) - default stays `MockWhisperSource`,
zero behavior change unless explicitly enabled.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from .whisper import WhisperListener, WhisperSource
from .utils import log

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LISTEN_TIMEOUT_S = 4.0
_PHRASE_TIME_LIMIT_S = 12.0


def _import_legacy_main() -> ModuleType:
    """Reuses `legacy_main.py`'s already-tested `get_whisper_model()`/
    `transcribe_audio()` - same file-based loading technique every test
    in `tests/` already uses for root-level scripts (see
    `luno/bootstrap/modules.py`'s `_import_demo_module()` for the
    identical pattern, one level up)."""
    if "legacy_main" in sys.modules:
        return sys.modules["legacy_main"]
    try:
        import legacy_main as legacy_mod  # type: ignore[import-not-found]
        return legacy_mod
    except ImportError:
        pass
    legacy_path = _PROJECT_ROOT / "legacy_main.py"
    spec = importlib.util.spec_from_file_location("legacy_main", str(legacy_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load legacy_main.py from {legacy_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["legacy_main"] = module
    spec.loader.exec_module(module)
    return module


class RealWhisperSource(WhisperSource):
    def __init__(self) -> None:
        import speech_recognition as sr  # local import: optional hardware dependency, mirrors luno/vision.py's own pattern

        self._sr = sr
        self._legacy = _import_legacy_main()
        self._recognizer = sr.Recognizer()
        self._listener: Optional[WhisperListener] = None
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._calibrated = False
        # Optional explicit PyAudio input device index (MIC_DEVICE_INDEX
        # in .env) - `None` (default, unset) means "let PyAudio pick its
        # own default input device", exactly the behavior this class had
        # before this option existed. Added because PyAudio's default-
        # device lookup can fail outright on some Windows setups
        # (`[Errno -9996] Invalid device info`) with no way to work
        # around it other than naming a device explicitly - see
        # `list_microphones.py` at the project root for how to find the
        # right index.
        from luno import config as _legacy_config
        self._device_index: Optional[int] = _legacy_config.MIC_DEVICE_INDEX
        if self._device_index is not None:
            log(f"using explicit MIC_DEVICE_INDEX={self._device_index}", "whisper")

    def start(self, listener: WhisperListener) -> None:
        self._listener = listener
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="luno-whisper-real-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        self._listener = None

    def _run(self) -> None:
        try:
            self._calibrate_once()
        except Exception as ex:
            log(f"ambient noise calibration failed (continuing with defaults): {ex}", "whisper")

        while not self._stop_flag.is_set():
            try:
                self._listen_and_transcribe_once()
            except self._sr.WaitTimeoutError:
                continue
            except Exception as ex:
                log(f"listen/transcribe loop error (continuing): {ex}", "whisper")

    def _calibrate_once(self) -> None:
        # Calibrated exactly ONCE, at a presumed-quiet startup moment -
        # never again during the loop below, and with dynamic energy
        # thresholding disabled - see module docstring's "Ambient noise
        # calibration discipline" section for why.
        with self._sr.Microphone(device_index=self._device_index) as source:
            self._recognizer.adjust_for_ambient_noise(source, duration=1.0)
        self._recognizer.dynamic_energy_threshold = False
        self._calibrated = True
        log("ambient noise calibrated", "whisper")

    def _listen_and_transcribe_once(self) -> None:
        with self._sr.Microphone(device_index=self._device_index) as source:
            audio = self._recognizer.listen(source, timeout=_LISTEN_TIMEOUT_S, phrase_time_limit=_PHRASE_TIME_LIMIT_S)

        listener = self._listener
        if listener is None:
            return

        text = self._legacy.transcribe_audio(audio)
        if not text or not text.strip():
            return

        listener.on_speech_started()
        listener.on_speech_recognized(text.strip())
        listener.on_speech_finished()
