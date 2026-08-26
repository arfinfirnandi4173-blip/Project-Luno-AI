"""
whisper.py
==========

`WhisperAdapter` - translates speech-recognition callbacks into
internal Events. Contains NO speech recognition itself ("Do not include
Whisper implementation. Use interfaces and mocks.") - a real
integration provides a `WhisperSource` implementation that calls back
into the adapter (via the `WhisperListener` methods, which the adapter
itself implements) whenever the real Whisper pipeline notices
something; this file only knows how to turn those calls into
`WakeWordDetected` / `SpeechStarted` / `SpeechRecognized` /
`SpeechFinished` events (all reused from `luno.core.events` - no new
event types needed here).

    real Whisper pipeline --calls--> WhisperListener methods (this adapter)
                                              |
                                        Event Bus (publish)

`handle_event()` is intentionally a no-op by default - Whisper is a
pure event SOURCE in this architecture (nothing currently asks it to
DO anything via an internal Event), but the hook is there for a future
need (e.g. a "start_listening"/"mute" control event) without changing
the adapter's shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..core.events import SpeechFinished, SpeechRecognized, SpeechStarted, WakeWordDetected
from .base import BaseAdapter
from .utils import log


class WhisperListener(ABC):
    """What a real Whisper integration calls back into. `WhisperAdapter`
    implements this itself - real code never needs its own listener
    class, just an instance of the adapter."""

    def on_wake_word_detected(self, confidence: Optional[float] = None) -> None: ...
    def on_speech_started(self) -> None: ...
    def on_speech_finished(self) -> None: ...
    def on_speech_recognized(self, text: str, confidence: Optional[float] = None) -> None: ...


class WhisperSource(ABC):
    """The external system interface - a real implementation wraps
    whatever local/streaming Whisper pipeline actually runs and calls
    the given `listener`'s methods as things happen."""

    @abstractmethod
    def start(self, listener: WhisperListener) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class MockWhisperSource(WhisperSource):
    """Does nothing on its own - tests/demo code call
    `simulate_wake_word()`/`simulate_speech(...)` directly to drive the
    adapter, standing in for a real microphone + model."""

    def __init__(self) -> None:
        self.listener: Optional[WhisperListener] = None
        self.running = False

    def start(self, listener: WhisperListener) -> None:
        self.listener = listener
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.listener = None

    def simulate_wake_word(self, confidence: float = 0.95) -> None:
        if self.listener:
            self.listener.on_wake_word_detected(confidence)

    def simulate_speech(self, text: str, confidence: float = 0.9) -> None:
        if not self.listener:
            return
        self.listener.on_speech_started()
        self.listener.on_speech_recognized(text, confidence)
        self.listener.on_speech_finished()


class WhisperAdapter(BaseAdapter, WhisperListener):
    name = "whisper"

    def __init__(self, source: Optional[WhisperSource] = None) -> None:
        BaseAdapter.__init__(self)
        self.source = source or MockWhisperSource()

    def _do_start(self) -> None:
        self.source.start(self)

    def _do_stop(self) -> None:
        self.source.stop()

    # -- WhisperListener: external system -> internal Events ------------------

    def on_wake_word_detected(self, confidence: Optional[float] = None) -> None:
        log(f"wake word detected (confidence={confidence})", self.name)
        self.publish(WakeWordDetected(data={"confidence": confidence}))

    def on_speech_started(self) -> None:
        self.publish(SpeechStarted())

    def on_speech_finished(self) -> None:
        self.publish(SpeechFinished())

    def on_speech_recognized(self, text: str, confidence: Optional[float] = None) -> None:
        log(f"speech recognized: '{text}' (confidence={confidence})", self.name)
        self.publish(SpeechRecognized(data={"text": text, "confidence": confidence}))
