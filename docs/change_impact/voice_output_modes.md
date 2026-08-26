# Voice Output Mode (ALL / SHORT)

## 1. Root cause / motivation

Luno's voice pipeline always compressed longer replies down to a
budgeted, priority-selected subset of sentences (`build_dual_response()`,
Chat/Voice Dual Output sprint onward). There was no way for the user to
ask Luno to read a full response aloud instead - `explicit=True` on a
DETAILED-depth turn already skipped priority-selection for that ONE
turn, but there was no sticky, explicit, always-available "read
everything" mode, and no runtime command to switch it. This sprint adds
exactly that: a two-value `VOICE_OUTPUT_MODE` (`"ALL"`/`"SHORT"`),
sticky per conversation, toggleable at runtime via a direct API call or
a small set of explicit spoken commands.

## 2. Architecture

```
RAW LLM RESPONSE
        |
        v
  VOICE OUTPUT MODE (new, per-conversation, in PlannerBridgeModule)
        |
   +----+----+
   v         v
  ALL       SHORT
   |         |
   |    existing voice compression
   |    (_dedupe / _select_by_priority / _repair_orphans / budget)
   |         |
   +----+----+
        v
  existing semantic chunking (_group_sentences_into_chunk_pairs)
        |
        v
  existing streaming/legacy TTS dispatch (unchanged)
        |
        v
  playback (unchanged)
```

Mode resolution/storage lives in `PlannerBridgeModule` (mirrors the
existing `_depth_preference` per-conversation dict exactly). It is
threaded to the voice pipeline through the SAME `response_depth_assigned`
event `depth`/`explicit` already use - one more field,
`voice_output_mode` - consumed identically by the non-streaming
(`BehaviorTreeModule._speak()`) and streaming (`StreamingSpeechCoordinator.
_on_finished()`) paths. `build_dual_response()` (the ONE existing
selection authority both paths already share) is the ONLY place the
mode actually changes behavior.

## 3. ALL behavior

`build_dual_response(..., voice_output_mode="ALL")` skips BOTH
`_dedupe()` and `_select_by_priority()` - `selected` is the complete,
unmodified sentence list the splitter found, still run through the
existing mandatory `normalize_for_speech()` cleaning (markdown/code/
links stripped, numbers spoken naturally - TTS legibility, not
compression). No sentence, bullet, or numbered list item is ever
dropped. `voice_adapted` is `False`. `chat_text` is untouched, as
always. Streaming, chunking, cancellation, pause/resume, and safety
handling are all completely unaffected - ALL only ever changes WHICH
sentences are selected, never anything downstream of that selection.

## 4. SHORT behavior

Byte-identical to this function's pre-existing behavior. The
`skip_compression`/budget/`_select_by_priority()`/`_repair_orphans()`
code path was not touched - just moved under an `else` branch next to
the new ALL branch. Confirmed via `tests/test_voice_output_modes.py`'s
own `test_a_default_mode_omitted_equals_short_byte_identical` and the
several `with_mode == without_mode` assertions throughout.

## 5. Runtime switching

`PlannerBridgeModule.get_voice_output_mode(conversation_id)` /
`.set_voice_output_mode(conversation_id, mode)` - a bounded,
per-conversation dict (capped at 50 entries, popped on
`conversation_ended`, never persisted to disk). A mode-switch command
detected in `_handle_utterance()` (`match_voice_output_mode_command()` -
a small fixed bilingual phrase list, not a new classifier) calls
`set_voice_output_mode()` for the conversation and forces THIS turn's
own `ResponsePolicy` to `depth="short", explicit=True` (reusing the
existing `explicit_short_instruction` shape) so its own confirmation is
never itself read as a long reply. The mode change takes effect starting
the NEXT turn - `_handle_utterance()` reads the OLD value into a local
variable before any command in the same turn can change it.

## 6. Tests

`tests/test_voice_output_modes.py` - 42 tests, all passing:
- Section 1 (9 tests): `luno.voice_output_mode` enum/validation/command
  matching.
- Section 2 (19 tests): `build_dual_response()` ALL vs SHORT - short/
  long replies, numbered/bulleted lists, multi-paragraph, warnings,
  conditions, invalid mode fallback, empty/null response, dedup bypass,
  `voice_adapted` correctness.
- Section 3 (2 tests): chat-text integrity in both modes.
- Section 4 (3 tests): TTS chunk coverage/ordering.
- Section 5 (9 tests): E2E through the real `RuntimeDemoConsole` -
  default mode, direct API toggle, spoken-command toggle with
  next-turn-only semantics, repeated consecutive toggles, chat
  integrity, streaming-stays-active, cancellation during ALL playback,
  memory/topic-state isolation, cross-conversation non-leak, status
  visibility.
- Section 6 (1 test): first-audio latency comparison.

## 7. Latency measurements

Measured (not assumed) via a standalone probe through the real
`RuntimeDemoConsole` + mocked LLM/TTS backends, 5 repetitions per mode,
same ~6-sentence reply:

| | first-audio latency (mean) | first-audio range | total latency (mean) |
|---|---|---|---|
| SHORT | 0.35s | 0.27s - 0.52s | 0.69s |
| ALL | 0.29s | 0.29s - 0.30s | 0.68s |

No first-audio latency regression for ALL - both are dominated by the
same always-safe lead-sentence dispatch mechanism, unmodified by this
sprint. Total speech DURATION naturally differs with more content
spoken in ALL mode for a genuinely long reply; that was never what was
being measured or bounded here.

## 8. Regression results

Full `tests/` tree (83 files) run in 4 chunks (`pytest -n 2`), plus the
new suite run standalone. Zero new regressions. Observed failures, all
independently re-confirmed as pre-existing/environmental (not caused by
this sprint):
- 4 timing-flakes under parallel (`-n 2`) load
  (`test_streaming_e2e.py::test_D_...`, `test_emotion_engine.py::test_stale_emotion_...`,
  two `test_llm_tts_streaming_production.py` first-audio-timing tests) -
  all pass reliably when run in isolation.
- `tests/test_main_bargein.py` / `tests/test_root_main_bargein.py` -
  pre-existing `ModuleNotFoundError: No module named 'faster_whisper'`
  (missing optional dependency in this sandbox, unrelated to any code
  touched this sprint).
- `tests/test_mic_device_index.py`, `tests/test_production_launcher.py::test_07_...`,
  `tests/test_real_adapters.py::test_real_whisper_source_...` (x2),
  `tests/test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_...` -
  pre-existing environment-configuration artifacts (e.g. a real
  `MIC_DEVICE_INDEX` environment value already set in this sandbox,
  confirmed by inspecting the actual assertion failure) - fail
  identically whether or not this sprint's files are even collected.

## 9. Persistent-state verification

SHA256 of all 15 `config/*.json` files confirmed byte-identical before
and after the full sprint (implementation + full test suite runs).
`config/vision_memory.sqlite3`'s mtime is unaffected (unrelated to any
turn/voice-mode logic - vision-memory events are never simulated by
these tests). The voice output mode itself is NEVER persisted to disk -
`PlannerBridgeModule._voice_output_mode` is a plain in-memory dict,
popped on conversation end, with no writer path anywhere in this
sprint's code.

## 10. Known limitations

ALL mode still runs text through `normalize_for_speech()` (markdown/
code/link stripping, number-to-words conversion) - this is unavoidable
TTS legibility processing, not a content-selection decision, and applies
identically to every existing mode; "ALL" means "no sentence is ever
dropped", not "byte-identical to the raw markdown text is spoken
verbatim". The command-phrase list (Phase 5) is intentionally small and
fixed - an utterance that expresses the same intent in different words
(e.g. "please read the whole thing") will not switch modes; this is a
deliberate scope boundary matching the brief's own "jangan membuat
classifier besar baru" instruction, not an oversight.
