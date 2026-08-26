"""
fish_audio_real.py
====================

`RealFishAudioClient` - a real, `FishAudioClient`-conformant integration
for the GPT-SoVITS/F5-TTS servers `luno.main` (the separate, legacy,
non-event-driven script) already knows how to talk to. This is the
FIRST such implementation in the Adapter Layer - before this file,
`FishAudioAdapter` only ever had `MockFishAudioClient` plugged into it.

Nothing here is new architecture: `RealFishAudioClient` implements the
exact same `FishAudioClient` interface (`play`/`stop`/`pause`/`resume`)
`fish_audio.py` already defines, so `FishAudioAdapter`, and everything
above it (Behavior Tree, Wake Session, Planner, Tool Manager, Barge-In,
Runtime Demo), needs zero changes and has zero idea whether a mock or a
real backend is doing the talking - exactly this task's objective.

Two phases, kept structurally separate on purpose (see `play()`):

    1. SYNTHESIS - `POST {host}/tts` (payload shape matches
       `luno.main._request_tts_audio()`'s own two branches - GPT-SoVITS
       vs F5-TTS - exactly, so this is a faithful reimplementation, not
       a guess) - returns raw WAV bytes. Can legitimately take seconds.
    2. PLAYBACK - actually pushing decoded audio out through
       `sounddevice`, blocking until finished.

`on_playback_start` (see `FishAudioClient.play()`'s own docstring) is
called ONLY at the start of phase 2 - this is the whole fix: publishing
`SpeechPlaybackStarted` while synthesis is still an in-flight HTTP
request (phase 1) is what made Wake Session/Barge-In believe Luno was
still Sleeping/Idle while audio was (about to be, but not yet) playing.

Testability (mirrors `luno.adapters.openrouter.RequestsOpenRouterClient`'s
own established pattern exactly): both phases are injectable.
`session` stands in for `requests.Session` (`.post(url, json=..., timeout=...)
-> object with .status_code/.content`) so synthesis can be exercised with
a scripted fake, no network. `play_audio_fn` stands in for the actual
`sounddevice`/`soundfile` call so playback timing/cancellation/pause can
be exercised with a controllable fake, no audio hardware. Production
code paths (`_default_synthesize`/`_default_play_audio`) use `requests`/
`sounddevice`/`soundfile` for real and are never exercised by this
project's own test suite (no server, no audio device in CI/sandbox).
"""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .fish_audio import FishAudioClient, PlaybackCancelled
from .utils import log

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None

try:
    import sounddevice as _sd
except (ImportError, OSError):  # pragma: no cover - PortAudio may be entirely absent
    _sd = None

try:
    import soundfile as _sf
except (ImportError, OSError):  # pragma: no cover
    _sf = None

try:
    import ormsgpack as _ormsgpack
except ImportError:  # pragma: no cover - only required by the fish_audio_api engine
    _ormsgpack = None


class TTSSynthesisError(Exception):
    """Raised when the TTS backend returns a non-200 response, an
    unreadable body, or the HTTP request itself fails (connection
    refused, DNS failure, timeout, ...). `FishAudioAdapter._play()`
    already treats any non-`PlaybackCancelled` exception from
    `client.play()` as `SpeakRequest -> SpeechError` (publishes
    `SpeechPlaybackCancelled` with the error message, no
    `SpeechPlaybackStarted` - synthesis never got as far as playback) -
    this is the ENTIRE fail-silent mechanism for TTS in this project:
    `RealFishAudioClient.play()`/the `fish_audio_api` engine below never
    need to catch their own exceptions or invent a parallel "result"
    type, they just raise, and this pre-existing adapter-level catch-all
    already guarantees Luno never crashes/hangs/duplicates a response
    over a TTS failure (text was already dispatched via `AssistantResponse`
    before `FishAudioAdapter` ever sees it - see that module's own
    docstring).

    `status_code`/`retryable` (both optional, default `None`/`False`)
    are ONLY set by the `fish_audio_api` engine's synthesis path below -
    the pre-existing gptsovits/f5tts call sites construct this exception
    exactly as before (positional message only), completely unaffected
    by this extension."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        #: Mirrors this project's own `ToolResult.fail(..., retryable=...)`
        #: convention (see e.g. `luno/tool_manager/builtin/real_browser.py`) -
        #: same name, same meaning: "worth retrying a bounded number of
        #: times," never "guaranteed to succeed on retry."
        self.retryable = retryable


@dataclass
class RealFishAudioConfig:
    """Every knob this integration needs, env-var only - mirrors every
    other package's `*Config.from_env()` convention in this project.
    Deliberately reads the SAME env var names `luno.config`/`luno.main`
    already use (`TTS_ENGINE`, `GPTSOVITS_HOST`, `F5TTS_HOST`,
    `REFERENCE_AUDIO`, `REFERENCE_TEXT`) so an existing deployment's
    `.env` needs no changes - but this dataclass does NOT import
    `luno.config` (that module lives in the separate, legacy `luno.main`
    world) - same "read the same env var independently, never import
    across that boundary" rule already used between `wake_session` and
    `barge_in`."""

    engine: str = "gptsovits"  # or "f5tts" or "fish_audio_api"
    gptsovits_host: str = "http://127.0.0.1:9880"
    f5tts_host: str = "http://127.0.0.1:8880"
    reference_audio: str = ""
    reference_text: str = ""
    #: HTTP request timeout for the synthesis call itself. F5-TTS
    #: inference is meaningfully slower than GPT-SoVITS, especially on
    #: CPU - matches `luno.main._request_tts_audio()`'s own per-engine
    #: timeouts (60s GPT-SoVITS, 120s F5-TTS) exactly.
    timeout_s: float = 60.0
    #: How often `play()` re-checks for a cancellation that arrived
    #: WHILE synthesis is still in flight (see `play()`'s docstring) -
    #: bounds how long a "stop" spoken mid-synthesis takes to actually
    #: return control, independent of the synthesis call's own timeout.
    synthesis_poll_s: float = 0.1

    # -- Fish Audio CLOUD API (engine="fish_audio_api") - a THIRD engine
    # alongside the two self-hosted ones above, added specifically so
    # `FishAudioAdapter`/`RealFishAudioClient`'s existing interface never
    # needs to change: this is just another `_synthesize_fn`, selected
    # the exact same way gptsovits vs f5tts already is. See
    # `_fish_audio_api_synthesize_once()`/`FishAudioApiCircuitBreaker`
    # below and `luno/bootstrap/adapters.py::_default_fish_audio_client()`
    # for the fail-silent-by-default wiring (missing API key -> falls
    # back to `MockFishAudioClient` at startup, never a live crash).
    fish_audio_api_key: str = ""
    fish_audio_api_base_url: str = "https://api.fish.audio"
    #: One of Fish Audio's own `Backends` literal values (verified
    #: against the official SDK's `schemas.py`): "speech-1.5" (the
    #: SDK's own default, used here too), "speech-1.6", "agent-x0",
    #: "s1", "s1-mini", "s2-pro".
    fish_audio_model: str = "speech-1.5"
    #: A saved Fish Audio voice/model id (`reference_id` in their API) -
    #: empty means "use the model's own default voice," matching the
    #: SDK's own `reference_id: str | None = None` default.
    fish_audio_voice_id: str = ""
    fish_audio_output_format: str = "mp3"  # "wav" | "pcm" | "mp3" per their TTSRequest schema
    #: Bounded, non-aggressive retry - ONLY for `TTSSynthesisError`s
    #: marked `retryable=True` (network failures, 408/429/500/502/503/504
    #: - see `_fish_audio_api_synthesize_once()`). 1 means "one retry",
    #: i.e. up to 2 attempts total.
    max_retries: int = 1
    #: Circuit breaker - after this many CONSECUTIVE failures (of any
    #: kind), stop attempting real HTTP calls for `cooldown_s` seconds.
    failure_threshold: int = 3
    cooldown_s: float = 30.0

    @classmethod
    def from_env(cls) -> "RealFishAudioConfig":
        import os

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        engine = (os.getenv("TTS_ENGINE") or "").strip().lower()
        if not engine:
            # `TTS_ENGINE` always wins if both are set - `FISH_AUDIO_
            # BACKEND=fish_audio_api` alone (no separate TTS_ENGINE) is
            # ALSO enough to select the cloud engine, so setting just
            # one env var is enough to opt in either way.
            engine = "fish_audio_api" if (os.getenv("FISH_AUDIO_BACKEND") or "").strip().lower() == "fish_audio_api" else "gptsovits"

        default_timeout = 15.0 if engine == "fish_audio_api" else (120.0 if engine == "f5tts" else 60.0)
        return cls(
            engine=engine,
            gptsovits_host=os.getenv("GPTSOVITS_HOST", "http://127.0.0.1:9880"),
            f5tts_host=os.getenv("F5TTS_HOST", "http://127.0.0.1:8880"),
            reference_audio=os.getenv("REFERENCE_AUDIO", ""),
            reference_text=os.getenv("REFERENCE_TEXT", ""),
            # `FISH_AUDIO_TIMEOUT` (spec name) takes priority; falls back
            # to the pre-existing `FISH_AUDIO_TTS_TIMEOUT_S` var so
            # nobody's existing gptsovits/f5tts `.env` needs to change.
            timeout_s=_float("FISH_AUDIO_TIMEOUT", _float("FISH_AUDIO_TTS_TIMEOUT_S", default_timeout)),
            synthesis_poll_s=_float("FISH_AUDIO_SYNTHESIS_POLL_S", 0.1),
            fish_audio_api_key=os.getenv("FISH_AUDIO_API_KEY", ""),
            fish_audio_api_base_url=(os.getenv("FISH_AUDIO_API_BASE_URL") or "").strip() or "https://api.fish.audio",
            fish_audio_model=(os.getenv("FISH_AUDIO_MODEL") or "").strip() or "speech-1.5",
            fish_audio_voice_id=os.getenv("FISH_AUDIO_VOICE_ID", ""),
            fish_audio_output_format=(os.getenv("FISH_AUDIO_OUTPUT_FORMAT") or "").strip() or "mp3",
            max_retries=_int("FISH_AUDIO_MAX_RETRIES", 1),
            failure_threshold=_int("FISH_AUDIO_FAILURE_THRESHOLD", 3),
            cooldown_s=_float("FISH_AUDIO_COOLDOWN", 30.0),
        )


@dataclass
class _PlaybackControl:
    """Passed to `play_audio_fn` - the injectable/real audio-output
    step. Its `cancel`/`pause` events are per-CALL, never shared at the
    instance level (see `RealFishAudioClient`'s own docstring for why -
    same reasoning `MockFishAudioClient` already documents for the
    identical fix in that class)."""

    cancel: threading.Event
    pause: threading.Event
    on_playback_start: Callable[[], None]


def _default_synthesize(text: str, config: RealFishAudioConfig, session: Any) -> bytes:
    """Real HTTP call - payload shape matches `luno.main._request_tts_audio()`'s
    two branches exactly (GPT-SoVITS vs F5-TTS), so this is a faithful
    reimplementation of the ALREADY-WORKING legacy integration, wired
    into the Event Bus for the first time rather than guessed from
    scratch."""
    if config.engine == "f5tts":
        host = config.f5tts_host
        payload: Dict[str, Any] = {
            "ref_audio_path": config.reference_audio,
            "ref_text": config.reference_text,
            "gen_text": text,
        }
    else:
        host = config.gptsovits_host
        payload = {
            "ref_audio_path": config.reference_audio,
            "prompt_text": config.reference_text,
            "prompt_lang": "auto",
            "text": text,
            "text_lang": "auto",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
        }

    try:
        resp = session.post(f"{host}/tts", json=payload, timeout=config.timeout_s)
    except Exception as ex:
        raise TTSSynthesisError(f"TTS request to {host} failed: {ex}") from ex

    status = getattr(resp, "status_code", None)
    if status != 200:
        detail = None
        try:
            detail = resp.json().get("detail")
        except Exception:
            pass
        raise TTSSynthesisError(f"TTS backend returned status {status}" + (f": {detail}" if detail else ""))

    content = getattr(resp, "content", None)
    if not content:
        raise TTSSynthesisError("TTS backend returned an empty response body")
    return content


# ─────────────────────────────────────────────────────────────────────────
#  Fish Audio CLOUD API (engine="fish_audio_api") - a third, independent
#  synthesis path. `_default_synthesize()` above is completely untouched
#  by any of this (zero risk to the existing gptsovits/f5tts behavior) -
#  `_default_fish_audio_client()` in `luno/bootstrap/adapters.py` selects
#  `FishAudioApiCircuitBreaker.call` as `RealFishAudioClient`'s
#  `synthesize_fn` directly when this engine is chosen, bypassing
#  `_default_synthesize` entirely.
# ─────────────────────────────────────────────────────────────────────────

#: HTTP status codes worth a bounded retry - transient/server-side, not
#: the API rejecting the request as malformed/unauthorized. Matches the
#: spec's own exact list (408/429/500/502/503/504).
_TRANSIENT_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _fish_audio_api_synthesize_once(text: str, config: "RealFishAudioConfig", session: Any) -> bytes:
    """Single, un-retried attempt at Fish Audio's REAL cloud TTS API.

    Verified against the official SDK source (github.com/fishaudio/
    fish-audio-python, `src/fish_audio_sdk/{apis,io,schemas}.py`) - not
    guessed, per this integration's own hard requirement never to
    invent an endpoint/payload/header shape:

        POST https://api.fish.audio/v1/tts
        Authorization: Bearer <FISH_AUDIO_API_KEY>
        Content-Type: application/msgpack
        model: <FISH_AUDIO_MODEL>              (e.g. "speech-1.5" - a
                                                 HEADER, not a body field)
        body: ormsgpack.packb({...})            (a TTSRequest-shaped dict -
                                                 msgpack, NOT JSON)

    Response: raw audio bytes on success (200); on failure, a JSON body
    shaped `{"status": ..., "message": ...}` (the SDK's own `HttpCodeErr`).

    Retry/circuit-breaker policy lives one layer up, in
    `FishAudioApiCircuitBreaker.call()` - this function is a single,
    side-effect-free attempt, so it stays independently testable and
    never retries anything itself."""
    if not (text or "").strip():
        # Spec requirement: never make a network request for empty/
        # whitespace-only text - this is not a "failure," just nothing
        # to do, but it's still surfaced as a (non-retryable)
        # `TTSSynthesisError` so the caller's single "no audio" path
        # handles it exactly like any other synthesis failure - no
        # separate code path needed.
        raise TTSSynthesisError("empty text - nothing to synthesize", retryable=False)
    if not config.fish_audio_api_key:
        raise TTSSynthesisError(
            "FISH_AUDIO_API_KEY is not set - Fish Audio API TTS is disabled", retryable=False,
        )
    if _ormsgpack is None:
        raise TTSSynthesisError(
            "the 'ormsgpack' package is required for the fish_audio_api engine "
            "(pip install ormsgpack)", retryable=False,
        )

    payload: Dict[str, Any] = {
        "text": text,
        "format": config.fish_audio_output_format,
        "normalize": True,
        "latency": "balanced",
    }
    if config.fish_audio_voice_id:
        payload["reference_id"] = config.fish_audio_voice_id

    headers = {
        "Authorization": f"Bearer {config.fish_audio_api_key}",
        "Content-Type": "application/msgpack",
        "model": config.fish_audio_model,
    }

    try:
        resp = session.post(
            f"{config.fish_audio_api_base_url}/v1/tts",
            data=_ormsgpack.packb(payload),
            headers=headers,
            timeout=config.timeout_s,
        )
    except Exception as ex:
        # Connection refused / DNS failure / connection reset / SSL
        # error / read timeout - every one of these is a network-layer
        # failure, not the API rejecting the request, so it is always
        # worth a bounded retry (never includes the API key - `ex`'s
        # own message is a `requests`/socket-layer string, never echoes
        # request headers).
        raise TTSSynthesisError(f"Fish Audio API request failed: {ex}", retryable=True) from ex

    status = getattr(resp, "status_code", None)
    if status != 200:
        message = None
        try:
            body = resp.json()
            message = body.get("message") or body.get("detail")
        except Exception:
            pass
        detail = f": {message}" if message else ""
        raise TTSSynthesisError(
            f"Fish Audio API returned status {status}{detail}",
            status_code=status, retryable=status in _TRANSIENT_HTTP_STATUS,
        )

    content = getattr(resp, "content", None)
    if not content:
        raise TTSSynthesisError("Fish Audio API returned an empty audio response", retryable=False)
    return content


class FishAudioApiCircuitBreaker:
    """Thread-safe retry + circuit-breaker wrapper around
    `_fish_audio_api_synthesize_once()` - used as the injectable
    `synthesize_fn` for `RealFishAudioClient` when `engine ==
    "fish_audio_api"` (see `_default_fish_audio_client()` in
    `luno/bootstrap/adapters.py`). Its public surface (`call`/`status`/
    `reset`) is deliberately the ENTIRE contract anything outside this
    file ever needs - `RealFishAudioClient.tts_status()` below just
    forwards to `status()`.

    Retry: only for `TTSSynthesisError`s marked `retryable=True`
    (network failures, 408/429/500/502/503/504 - see
    `_fish_audio_api_synthesize_once()`) - never for 400/401/403/404 or
    any other "the request itself is wrong" failure, where retrying is
    guaranteed pointless and just adds latency before the inevitable
    silent failure. Bounded by `config.max_retries` (default 1 - one
    retry, i.e. up to 2 attempts total - not aggressive).

    Circuit breaker: after `config.failure_threshold` CONSECUTIVE
    failures (any kind, not just retryable ones), `call()` starts
    raising immediately WITHOUT attempting any real HTTP call, for
    `config.cooldown_s` seconds - so a genuine Fish Audio outage can
    never turn into a request-per-utterance spam against a live,
    daily-use assistant. A single success at any point (including the
    very first attempt right after cooldown expires) fully resets the
    breaker back to healthy.

    `status()` exposes the current state for health/dashboard reporting
    WITHOUT ever making a network call itself (spec requirement) - it
    purely reads already-tracked in-memory counters under the same lock
    `call()` itself uses."""

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_s = max(0.0, cooldown_s)
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._disabled_until: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_success_at: Optional[float] = None

    def call(self, text: str, config: "RealFishAudioConfig", session: Any) -> bytes:
        with self._lock:
            now = time.time()
            if self._disabled_until is not None and now < self._disabled_until:
                remaining = round(self._disabled_until - now, 1)
                raise TTSSynthesisError(
                    f"Fish Audio API temporarily disabled (circuit breaker open after "
                    f"{self._consecutive_failures} consecutive failure(s), retrying in {remaining}s)",
                    retryable=False,
                )

        attempts = max(1, config.max_retries + 1)
        last_exc: Optional[TTSSynthesisError] = None
        for attempt in range(1, attempts + 1):
            try:
                audio = _fish_audio_api_synthesize_once(text, config, session)
            except TTSSynthesisError as ex:
                last_exc = ex
                if ex.retryable and attempt < attempts:
                    log(
                        f"Fish Audio TTS transient failure (attempt {attempt}/{attempts}) - "
                        f"retrying: status={ex.status_code} retry=true fallback=silent",
                        "fish_audio_real",
                    )
                    continue
                break
            else:
                self._record_success()
                return audio

        assert last_exc is not None  # the loop above always sets this before breaking/exhausting attempts
        self._record_failure(str(last_exc))
        raise last_exc

    def _record_success(self) -> None:
        with self._lock:
            was_degraded = self._consecutive_failures >= self._failure_threshold
            self._consecutive_failures = 0
            self._disabled_until = None
            self._last_success_at = time.time()
        if was_degraded:
            log("Fish Audio TTS recovered - circuit breaker closed", "fish_audio_real")

    def _record_failure(self, error: str) -> None:
        with self._lock:
            now = time.time()
            # BUG FIX (caught by this file's own
            # test_circuit_stays_open_if_recovery_attempt_also_fails):
            # `_disabled_until` being non-`None` does NOT mean "currently
            # degraded" - once cooldown has already expired it's just a
            # STALE past timestamp. The old `self._disabled_until is
            # None` check treated that stale value as "already open," so
            # a "recovery attempt" that failed right after cooldown
            # expired never got a FRESH cooldown window at all - the
            # breaker silently looked healthy again immediately. Compare
            # against `now` instead: only an ACTIVELY open breaker
            # counts as "already open."
            was_degraded = self._disabled_until is not None and now < self._disabled_until
            self._consecutive_failures += 1
            self._last_error = error
            should_be_open = self._consecutive_failures >= self._failure_threshold
            if should_be_open:
                self._disabled_until = now + self._cooldown_s
            newly_opened = should_be_open and not was_degraded
        # WARNING, not ERROR - a TTS failure is a non-critical, already-
        # handled failure (spec's own logging requirement); never
        # includes the API key or Authorization header.
        log(
            f"Fish Audio TTS failed; continuing without audio: provider=fish_audio_api "
            f"consecutive_failures={self._consecutive_failures} fallback=silent",
            "fish_audio_real",
        )
        if newly_opened:
            log(
                f"Fish Audio TTS temporarily disabled after {self._consecutive_failures} "
                f"consecutive failure(s) - cooldown {self._cooldown_s}s",
                "fish_audio_real",
            )

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            degraded = self._disabled_until is not None and now < self._disabled_until
            return {
                "enabled": True,
                "healthy": not degraded and self._consecutive_failures == 0,
                "degraded": degraded,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_success_at": self._last_success_at,
                "cooldown_remaining_s": round(self._disabled_until - now, 1) if degraded else None,
            }

    def reset(self) -> None:
        """Test-only convenience - forces the breaker back to a clean,
        healthy state. Mirrors this project's own `reset_*()` singleton-
        reset convention (e.g. `luno.browser.provider.reset_browser_
        provider()`)."""
        with self._lock:
            self._consecutive_failures = 0
            self._disabled_until = None
            self._last_error = None
            self._last_success_at = None


def _default_play_audio(wav_bytes: bytes, control: _PlaybackControl) -> None:
    """Real playback via `sounddevice`/`soundfile` - the same libraries
    (and the same "decode the whole buffer, stream it out via an
    `OutputStream` callback" shape) `luno.main.play_audio()` already
    uses. Genuinely pausable (the stream callback simply stops
    advancing the read position and outputs silence while paused,
    exactly `MockFishAudioClient`'s own "don't lose position" strategy)
    and genuinely, immediately cancellable (the callback raises
    `sd.CallbackStop` the instant `control.cancel` is set, which tears
    the stream down right away rather than draining the rest of the
    buffer first)."""
    if _sd is None or _sf is None:  # pragma: no cover - exercised only with real deps installed
        raise RuntimeError(
            "sounddevice/soundfile are required for real Fish Audio playback "
            "(pip install sounddevice soundfile), or inject play_audio_fn for testing"
        )

    data, samplerate = _sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    total_frames = data.shape[0]
    channels = data.shape[1]
    position = [0]
    done = threading.Event()
    playback_error: List[BaseException] = []

    def _callback(outdata, frames, time_info, status) -> None:  # pragma: no cover - real audio callback
        if control.cancel.is_set():
            outdata.fill(0)
            raise _sd.CallbackStop()
        if control.pause.is_set():
            outdata.fill(0)
            return
        start = position[0]
        end = min(start + frames, total_frames)
        chunk = end - start
        outdata[:chunk] = data[start:end]
        if chunk < frames:
            outdata[chunk:] = 0
            position[0] = total_frames
            raise _sd.CallbackStop()
        position[0] = end

    control.on_playback_start()
    try:
        with _sd.OutputStream(
            samplerate=samplerate, channels=channels, callback=_callback,
            finished_callback=done.set,
        ) as stream:
            while not done.is_set():
                if control.cancel.is_set():
                    stream.abort()
                    break
                done.wait(0.05)
    except Exception as ex:  # pragma: no cover
        playback_error.append(ex)

    if playback_error:
        raise playback_error[0]
    if control.cancel.is_set():
        raise PlaybackCancelled("playback cancelled")


class RealFishAudioClient(FishAudioClient):
    """The real GPT-SoVITS/F5-TTS-backed `FishAudioClient`. Each `play()`
    call tracks its OWN cancel/pause state (a per-call `_PlaybackControl`,
    kept in `self._active` guarded by a lock) rather than sharing one
    pair of flags at the instance level - identical reasoning, and an
    identical fix, to the one already applied to `MockFishAudioClient`
    (see that class's docstring): `FishAudioAdapter`'s two-worker
    `_playback_executor` can genuinely run two `play()` calls
    concurrently (a paused reply + a Barge-In CONFIRM prompt spoken as
    an interjection), and `stop()`/`pause()`/`resume()` - which have no
    `request_id` to target, same as the mock - correctly act on
    whatever is CURRENTLY in flight rather than clobbering a
    different call's state.

    Cancellation is responsive during BOTH phases: `stop()` sets every
    active call's cancel flag; during playback the `sounddevice`
    callback notices it within one audio buffer; during synthesis
    (phase 1, a plain blocking HTTP call with no natural cancellation
    point) the HTTP request itself runs on a short-lived background
    thread while `play()`'s own thread polls the cancel flag every
    `config.synthesis_poll_s` and returns immediately once cancelled,
    WITHOUT waiting for that HTTP call to actually complete - it is
    simply abandoned (still bounded by `config.timeout_s`, still
    running on a daemon thread, so it can never outlive the process or
    block Runtime shutdown; its eventual result, if any, is discarded).
    """

    def __init__(
        self,
        config: Optional[RealFishAudioConfig] = None,
        session: Optional[Any] = None,
        synthesize_fn: Optional[Callable[[str, RealFishAudioConfig, Any], bytes]] = None,
        play_audio_fn: Optional[Callable[[bytes, _PlaybackControl], None]] = None,
    ) -> None:
        if _requests is None and session is None:  # pragma: no cover
            raise RuntimeError("the 'requests' package is required for RealFishAudioClient (or inject a session)")
        self.config = config or RealFishAudioConfig.from_env()
        self._session = session or _requests.Session()
        self._synthesize = synthesize_fn or _default_synthesize
        self._play_audio = play_audio_fn or _default_play_audio
        self._lock = threading.Lock()
        self._active: List[_PlaybackControl] = []
        #: One background thread per in-flight synthesis call, never
        #: more - `play()` itself already runs on `FishAudioAdapter`'s
        #: own dedicated executor, so this is purely to make the HTTP
        #: call interruptible-by-abandonment, not a general worker pool.
        self._synth_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="luno-fishaudio-synthesis")

    def reload_config(self, config: RealFishAudioConfig) -> None:
        with self._lock:
            self.config = config

    def play(self, text: str, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        control = _PlaybackControl(
            cancel=threading.Event(), pause=threading.Event(),
            on_playback_start=on_playback_start or (lambda: None),
        )
        with self._lock:
            self._active.append(control)
        try:
            with self._lock:
                cfg = self.config
            future = self._synth_executor.submit(self._synthesize, text, cfg, self._session)

            wav_bytes: Optional[bytes] = None
            while wav_bytes is None:
                if control.cancel.is_set():
                    log(f"synthesis abandoned - cancelled before playback started (text_len={len(text)})", "fish_audio_real")
                    raise PlaybackCancelled("cancelled during synthesis")
                try:
                    wav_bytes = future.result(timeout=cfg.synthesis_poll_s)
                except FutureTimeoutError:
                    continue
                except PlaybackCancelled:
                    raise
                except Exception as ex:
                    raise TTSSynthesisError(str(ex)) from ex

            if control.cancel.is_set():
                # synthesis finished right as a cancel arrived - never
                # start audio for a call that was already cancelled.
                raise PlaybackCancelled("cancelled just as synthesis finished")

            self._play_audio(wav_bytes, control)
        finally:
            with self._lock:
                if control in self._active:
                    self._active.remove(control)

    def supports_split_synthesis(self) -> bool:
        """TTS Chunk Pipelining sprint - `True`: this client can
        synthesize (`synthesize()`) and play (`play_audio()`) as two
        separate calls, letting `FishAudioAdapter` prefetch the NEXT
        chunk's audio while the CURRENT chunk is still playing. `play()`
        above is left completely untouched (still used by any caller that
        doesn't opt into the pipelined path, e.g. the non-streaming
        `_play()` turn) - `synthesize()`/`play_audio()` below simply
        expose the same two phases `play()` already runs internally
        (`self._synthesize` then `self._play_audio`) as two separate,
        independently callable methods."""
        return True

    def synthesize(self, text: str) -> bytes:
        """The synthesis half of `play()`, standalone. Deliberately a
        PLAIN blocking call (no cancellation polling) - per
        `FishAudioClient.synthesize()`'s own contract, this is only ever
        used for the CURRENT chunk (synchronously, on the adapter's
        playback thread - cancellation is checked before/after by the
        caller) or as a PREFETCH job submitted to `FishAudioAdapter
        ._prefetch_executor`, where a caller that stops waiting on it
        simply abandons/discards whatever it eventually returns - exactly
        `RealFishAudioClient.play()`'s own existing synthesis-abandonment
        policy, just exposed as its own method instead of being bundled
        with playback."""
        with self._lock:
            cfg = self.config
        return self._synthesize(text, cfg, self._session)

    def play_audio(self, audio: bytes, on_playback_start: Optional[Callable[[], None]] = None) -> None:
        """The playback half of `play()`, standalone, given audio bytes
        an earlier `synthesize()` call (on this SAME client instance)
        already produced. SAME `_PlaybackControl`-in-`self._active`
        bookkeeping as `play()` itself, so instance-level `stop()`/
        `pause()`/`resume()` affect an in-flight `play_audio()` call
        exactly as they already affect an in-flight `play()` call."""
        control = _PlaybackControl(
            cancel=threading.Event(), pause=threading.Event(),
            on_playback_start=on_playback_start or (lambda: None),
        )
        with self._lock:
            self._active.append(control)
        try:
            self._play_audio(audio, control)
        finally:
            with self._lock:
                if control in self._active:
                    self._active.remove(control)

    def stop(self) -> None:
        with self._lock:
            for control in self._active:
                control.cancel.set()
                control.pause.clear()

    def pause(self) -> None:
        with self._lock:
            for control in self._active:
                control.pause.set()

    def resume(self) -> None:
        with self._lock:
            for control in self._active:
                control.pause.clear()

    def close(self) -> None:
        """Best-effort cleanup for the synthesis executor - not part of
        `FishAudioClient`'s own interface (which has no lifecycle hooks
        beyond play/stop/pause/resume), called by whoever constructed
        this client if/when they tear it down (mirrors `FishAudioAdapter
        ._do_stop()` already calling `client.stop()` the same way)."""
        self._synth_executor.shutdown(wait=False)

    def tts_status(self) -> Optional[Dict[str, Any]]:
        """Live circuit-breaker health for the `fish_audio_api` engine
        specifically - `None` for gptsovits/f5tts (they have no circuit
        breaker; `play()` always attempts synthesis directly, exactly as
        before this feature existed). Never makes a network request
        itself - `FishAudioApiCircuitBreaker.status()` only reads
        already-tracked in-memory counters, satisfying the spec's own
        "health checks must not themselves call the API" requirement.

        Works by recognizing `self._synthesize` as a bound
        `FishAudioApiCircuitBreaker.call` method (exactly what
        `_default_fish_audio_client()` injects for this engine) rather
        than needing a new constructor parameter - `RealFishAudioClient`
        itself stays completely unaware a circuit breaker exists."""
        breaker = getattr(self._synthesize, "__self__", None)
        if isinstance(breaker, FishAudioApiCircuitBreaker):
            return breaker.status()
        return None
