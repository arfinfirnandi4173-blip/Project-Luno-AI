# Dashboard Turn-State Recovery Fix — Part 2 / TTS-Path

**STATUS: code written, reasoned through against the actual source,
syntax-checked (`python3 -m py_compile`) — NOT executed. See "Not yet
done" at the bottom of this document before treating this as verified.**

## Why this exists

A real production report (screenshot-documented) said the Dashboard was
STILL getting stuck permanently at `Thinking` after the original
Dashboard Turn-State Recovery fix (see `dashboard_turn_state_recovery.md`
next to this file) had already landed:

- Luno wakes up successfully.
- "Hi, Vinn." gets a reply.
- Every message after that ("how are you?", "hello", "cek", "ck", ...)
  is immediately refused with `"Luno is busy right now (state=thinking)
  - try again in a moment"`.
- The dashboard never recovers on its own.
- The rest of the backend (scheduled vision polling, etc.) keeps running
  normally — this is a single stuck subsystem, not a crashed process.

The instruction that prompted this investigation was explicit: **do not
assume the original fix already solved this** — verify against the
actual current code, not the prior sprint's own claims.

## Root cause / motivation

`SessionManagerModule` (`luno/wake_session/manager.py`) leaves `THINKING`
through exactly two routes, and only two:

1. `speech_playback_started`/`speak_request` → `SPEAKING`, then
   `speech_playback_finished`/`speech_playback_cancelled` →
   `WAITING_USER`/`IDLE` (`_handle_playback_done()`, gated
   `if self.session.state == ConversationState.SPEAKING`).
2. `llm_error`/`llm_cancelled` → `WAITING_USER`/`IDLE`
   (`_handle_llm_failure()`, gated
   `if self.session.state == ConversationState.THINKING`). This is the
   route the ORIGINAL Dashboard Turn-State Recovery fix reuses for a
   planner-side exception.

A turn whose LLM call already succeeded — `assistant_response` was
already published, the Chat panel already shows the correct reply, so
route 2 never fires — but whose TTS synthesis then fails on its very
first chunk (the Fish Audio TTS server is unreachable, or otherwise
fails before any audio plays) falls into **neither** route:

- `FishAudioAdapter._play()`/`_play_stream()` (whichever the configured
  client takes — streaming is the default path, since
  `luno.config.ENABLE_LLM_TTS_STREAMING` defaults on) correctly publishes
  `speech_playback_cancelled` once every chunk has failed
  (`if not any_chunk_played: ... self.publish(SpeechPlaybackCancelled(...))`).
- But `speech_playback_started` is only ever published from inside
  `_on_playback_start()`, which only runs once real audio has actually
  begun for the first chunk. If the very first chunk fails, that
  callback never runs, so `speech_playback_started` is never published.
- `_handle_playback_started()`/`_handle_speak_request()` — the only two
  places `SessionManagerModule` transitions `THINKING → SPEAKING` — are
  therefore never reached.
- `self.session.state` is still `THINKING` when `speech_playback_
  cancelled` arrives.
- `_handle_playback_done()`'s guard, `if self.session.state ==
  ConversationState.SPEAKING`, does not match `THINKING`, so the event is
  silently dropped. No branch. No log line calling this out as a
  problem. No exception anywhere in the process.
- `THINKING` has no timeout anywhere in this codebase (documented in
  `luno/wake_session/manager.py`'s own module docstring, citing a PRIOR,
  already-fixed instance of this exact bug class).

Result: `THINKING` is stuck forever. The dashboard's own busy-guard,
`send_chat_message()` (`luno/dashboard/controls.py`), reads
`session_manager.status_snapshot().get("state")`, sees `"thinking"`, and
returns `"Luno is busy right now (state=thinking) - try again in a
moment"` for every subsequent message — exactly the reported symptom.

**This is NOT a hypothetical constructed for this writeup.** The
project's own pre-existing test file, `tests/test_dashboard_turn_state_
recovery.py`, already contains this admission in `_new_console()`'s own
docstring (verbatim, present before this session touched anything):

> "Mocks BOTH adapters, not just OpenRouter - the real `FishAudioAdapter`
> client defaults to a real `http://127.0.0.1:9880` TTS server that
> doesn't exist in this sandbox, and a connection failure there prevents
> `speech_playback_started` from ever firing. Since `SessionManagerModule`'s
> THINKING -> SPEAKING transition for a streamed reply is keyed on THAT
> event ... an unmocked Fish Audio client makes even an entirely NORMAL
> turn look like a stuck-THINKING bug - a sandbox/environment artifact,
> not a defect in the fix under test."

That prior session correctly diagnosed the mechanism, then mocked it
away to make its own tests pass, rather than recognizing it as a real
architectural gap that would reproduce identically the moment the REAL
Fish Audio TTS server is unreachable in production — which is exactly
what the new bug report describes.

### A second, structural instance of the same bug class

The original fix's own writeup names the general pattern precisely: "an
unsupervised daemon thread with no outer try/except" — and fixed it for
`PlannerBridgeModule._handle_utterance()`. The exact same pattern still
existed, unfixed, one layer down:

- `FishAudioAdapter.handle_event()` dispatches actual playback via
  `pool.submit(self._play, event, token)` (or `self._play_stream` for
  the streaming path) on `self._playback_executor`, a `ThreadPoolExecutor`.
  Nothing ever calls `.result()` on the returned `Future` — by design,
  `handle_event()` must stay non-blocking (see its own docstring).
- `BaseAdapter._process_event()` (`luno/adapters/base.py`) DOES wrap
  `handle_event()` itself in a `try/except Exception` that publishes a
  `SystemError` event — but `handle_event()` returns almost immediately
  after `pool.submit(...)`, so that safety net can only catch a
  synchronous bug in the dispatch logic itself, never an exception
  raised later, asynchronously, inside `_play()`/`_play_stream()` on the
  separate `_playback_executor` thread.
- Inside `_play()`/`_play_pipelined()`/`_play_stream()`/`_play_stream_
  pipelined()`, the only exception guard is a narrow, PER-CHUNK
  `except Exception as ex:` around `self.client.play(...)` itself
  (bounded retry, then skip — already correct for a genuine TTS/network
  failure, which is what test_01 below actually exercises). Code
  OUTSIDE that inner guard but still inside the method — token
  cancellation checks, queue reads, the `self.publish(...)` calls
  themselves — had no outer guard at all. Any exception there would
  propagate out of the method entirely, silently killing that
  `_playback_executor` worker thread with **zero** terminal event
  published, leaving `SessionManagerModule` stuck exactly like the
  unguarded planner thread the original fix closed.

Both gaps are real, both are provable by direct code inspection, and
both are now closed (see "The fix" below). The first (the state-guard
gap) is the confirmed, concrete explanation for the reported symptom and
requires no exception to escape at all — just an ordinary, already-
correctly-published `speech_playback_cancelled` arriving one state too
early. The second (the missing outer guard) is defense-in-depth against
a DIFFERENT, rarer trigger (a genuinely unanticipated bug in the
adapter's own orchestration code, not a TTS/network failure).

## The fix

Two additive changes, reusing existing plumbing, zero new event type,
route, or state machine:

1. **`luno/wake_session/manager.py`**, `SessionManagerModule.
   _handle_playback_done()` — added:

   ```python
   elif self.session.state == ConversationState.THINKING:
       if self.config.sleep_enabled:
           self.session.transition_to(ConversationState.WAITING_USER, reason="assistant reply finished (tts never started)")
           self.session.touch()
       else:
           self.session.transition_to(ConversationState.IDLE, reason="assistant reply finished (tts never started, always-on mode)")
   ```

   This is byte-for-byte the SAME transition the pre-existing `SPEAKING`
   branch already makes — just also applied when the turn never reached
   `SPEAKING` in the first place. A turn that DOES reach `SPEAKING`
   normally is completely unaffected; that branch is unchanged and still
   the one that matches for it.

2. **`luno/adapters/fish_audio.py`** — `_play()`, `_play_pipelined()`,
   `_play_stream()`, `_play_stream_pipelined()` each gained one new
   `except Exception as ex:` clause, inserted between the existing `try`
   body and the existing `finally:`:

   ```python
   except Exception as ex:
       log(f"SpeechError request_id={request_id} unhandled_exception={ex!r} - publishing SpeechPlaybackCancelled so the turn is never stuck", self.name)
       self.publish(SpeechPlaybackCancelled(data={"request_id": request_id, "error": f"unhandled: {ex}"}))
   ```

   Every normal exit in each method already publishes exactly one
   terminal event and returns immediately, so this branch can only be
   reached by a genuinely unanticipated exception — no double-publish
   risk. Mirrors `PlannerBridgeModule._run_utterance_turn_safely()`'s own
   design from the original fix, applied to this module's equivalent gap.

No other line in either file was changed. `_handle_utterance()`'s own
body, every existing per-chunk `except Exception` in `fish_audio.py`,
and every other module are untouched.

## Tests

`tests/test_dashboard_turn_state_recovery_ttspath.py` — 5 tests, written
in the same house style as (and explicitly cross-referencing) `tests/
test_dashboard_turn_state_recovery.py`:

1. `test_01_e2e_tts_fails_before_playback_started_recovers_from_thinking`
   — the direct live reproduction: `MockFishAudioClient(fail=True)`
   raises inside `client.play()` before `on_playback_start()` is ever
   called (matching the real "TTS server unreachable" shape exactly),
   through a real `RuntimeDemoConsole`/`SessionManagerModule`. Asserts
   the session reaches `THINKING` and then leaves it.
2. `test_02_e2e_send_chat_message_accepts_a_new_command_after_tts_
   failure_recovery` — same scenario, asserted through the actual
   Dashboard `send_chat_message()` busy-guard (the exact function that
   produces the reported error string).
3. `test_03_play_stream_unhandled_exception_still_publishes_terminal_
   event` — unit-level, calls `FishAudioAdapter._play_stream()` directly
   with a token whose `is_cancelled` check raises (a stand-in for a
   genuinely unanticipated internal bug, deliberately NOT a TTS/network
   failure — those are already covered by test_01). Confirms exactly one
   `SpeechPlaybackCancelled` is published instead of the thread dying
   silently, and that bookkeeping (`_in_flight_request_ids`/
   `_chunk_control`) is still cleaned up.
4. `test_04_e2e_normal_turn_still_returns_to_non_busy_state` — baseline
   regression guard: an ordinary successful turn is unaffected by either
   new code path.
5. `test_05_e2e_repeated_tts_failure_then_normal_cycle_stays_usable` —
   3 iterations of the failure scenario in a row, confirming the session
   stays usable throughout, not just on the first occurrence.

## Regression

**Not run this session.** The exact command to run (from the repo root,
matching this project's own established `pytest -q` convention):

```
pytest -q tests/test_dashboard_turn_state_recovery_ttspath.py tests/test_dashboard_turn_state_recovery.py tests/test_dashboard.py tests/test_runtime_demo.py tests/test_wake_barge_in_integration.py tests/test_fish_audio_real.py tests/test_fish_audio_barge_in.py
```

If that passes cleanly, run the full targeted core-plus-observability-
plus-recovery suite listed in `docs/project_handover.md` §21, then the
full repository sweep in 8 chunks per that same section's own
instructions, before updating `docs/testing/regression_baseline.md` and
`docs/project_handover.md`/`.json` §3/§13/`test_baseline` with the real
result.

## Performance

**Not measured this session** (would require actually running the new
`except Exception` clauses under a microbenchmark, which requires the
working Python environment this session did not have). Expected to be
negligible — an `except` clause with no exception raised has near-zero
CPU cost in CPython, matching the original fix's own measured
`_is_expected_client_disconnect()`/wrapper overhead (both ~0.0002ms/
call) — but this is an expectation, not a measurement, and should be
confirmed with the same methodology the original fix used before this
document's own numbers are cited as fact anywhere else.

## Persistent state

**Not verified this session** (no environment to run the SHA256
before/after check the original fix used). Neither change reads or
writes `config/*.json` or any other persisted file — both are pure
in-memory state-machine/event-publish changes — so no impact is
expected, but this should still be confirmed with an actual run before
being stated as fact.

## Known limitations

1. Neither the pre-existing `SPEAKING` branch nor the new `THINKING`
   branch in `_handle_playback_done()` correlates by `request_id` — a
   stale/unrelated `speech_playback_cancelled` could in principle be
   misattributed to a newer turn that has already reached `THINKING`
   again. This is not a new risk introduced by this fix (the `SPEAKING`
   branch has always had the same shape) but is worth a dedicated
   request_id-correlation pass across both branches together if a real
   race is ever observed in practice.
2. The new `FishAudioAdapter` `except Exception` clauses only guard each
   method's own orchestration loop — an exception raised inside
   `self.client.play()` itself is still handled by the pre-existing,
   narrower, already-correct per-chunk guard (bounded retry, then skip).
   This is by design, not a gap.
3. **This entire fix has not been executed.** See "Not yet done" below.

## Invariants preserved

Same list the original fix committed to, still true here: no new state,
no new event type, no new route, `SessionManagerModule`'s existing
`THINKING`/`SPEAKING`/`WAITING_USER`/`IDLE` state machine unchanged in
shape (only which events can transition out of `THINKING` changed),
`_handle_utterance()`'s own body untouched, no change to `luno/
memory.py`, `luno/memory_retrieval/`, ranking, TTS chunking/pipelining
behavior on the success path, or streaming semantics.

## Not yet done

This is the load-bearing section of this document — read it before
treating this fix as complete:

1. **Run the tests.** `pytest -q tests/test_dashboard_turn_state_
   recovery_ttspath.py` at minimum; the fuller command is in "Regression"
   above. Fix anything that fails — the code was reasoned through
   carefully but has never touched a real interpreter running this
   project's actual dependencies.
2. **Run the full targeted + full-sweep regression** per `docs/project_
   handover.md` §21, to catch any interaction with the ORIGINAL Dashboard
   Turn-State Recovery fix's own tests or anything else.
3. **Measure performance and verify persistent state** using the same
   methodology the original fix used (see "Performance"/"Persistent
   state" above).
4. **Update `docs/testing/regression_baseline.md`** with a real,
   timestamped entry once the above is done, and update `docs/project_
   handover.md`/`.json` §3/§13/`test_baseline`/`last_verified`/`status`
   to reflect the real result (they currently, deliberately, describe
   this fix as code-complete-but-unverified — do not silently upgrade
   that language without an actual run backing it up).
5. If the real Fish Audio TTS server IS reachable in the user's actual
   production environment (i.e. the root cause turns out to be something
   OTHER than "TTS server unreachable" - a different first-chunk
   failure mode), the state-guard fix in `_handle_playback_done()` still
   closes the gap generically (it reacts to `speech_playback_cancelled`/
   `speech_playback_finished` arriving before `SPEAKING`, regardless of
   WHY the first chunk failed) - but the actual trigger should still be
   confirmed against the user's own logs if possible, rather than
   assumed.
