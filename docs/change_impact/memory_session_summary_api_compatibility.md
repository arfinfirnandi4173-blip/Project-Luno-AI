# Sprint 53 (user-numbered) — Memory Session Summary API Compatibility Fix

Filed under `ARCHITECTURE_GUARD.md` section **54**. Numbering note:
this is the user's own "Sprint 53" — a separate, later takeover
session's narrowly-scoped bug-fix request, unrelated to any of the
numbered intelligence sprints (43-50) or the two Dashboard Turn-State
Recovery fixes. See `docs/project_handover.md` §2/§19e/§20e for the
handover-document side of this same change.

## CORRECTION (added by Sprint 54 — read this first)

Sprint 54's own reconnaissance discovered that this document's "affected
call chain" claim below has a gap: `register_session_summary_client()`'s
`openrouter_adapter` parameter is, in the real production bootstrap
wiring (`luno/bootstrap/adapters.py` line 137), an `LLMManagerAdapter`
instance — **not** `luno.adapters.openrouter.OpenRouterAdapter`, despite
the variable/dict-key name. `luno.adapters.openrouter.OpenRouterAdapter`
is not constructed or imported anywhere in production bootstrap code —
it is orphaned, kept alive only by its own test file. The fix THIS
document describes (to `luno/adapters/openrouter.py`) is code-correct,
genuinely tested, and harmless, but it did **not** fix the originally
reported production bug — the real production path routes through
`LLMManagerAdapter.chat_once()` to a provider client under
`luno/adapters/llm/`, specifically `OpenAICompatibleClient._payload()`
in `luno/adapters/llm/base.py` (for the default `LLM_PROVIDER=openrouter`,
that's `luno.adapters.llm.openrouter_provider.OpenRouterProvider`).
Sprint 54 (`ARCHITECTURE_GUARD.md` §55,
`docs/change_impact/llm_max_completion_tokens_compatibility.md`) fixed
that actual path. See that document's own "IMPORTANT CORRECTION TO
SPRINT 53'S DOCUMENTED ROOT CAUSE" section for the full call-chain
trace and reasoning. Every fact below about `luno/adapters/openrouter.py`
itself — root cause, fix, tests, regression — remains accurate for
THAT file; only the "this is what runs in production for Session
Summary" framing needs this correction.

## Reported bug (exact text)

```
[Memory] ✗ Session summary error: Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.
```

## Root cause (confirmed by reading the actual current code, not assumed from the log)

`luno.memory.summarize_and_archive_session(openai_client, model=None)`
(`luno/memory.py`, function starts at line 2010) is the Session Summary
feature — it runs when a wake-word conversation ends
(`PlannerBridgeModule._on_conversation_ended()`) or on the manual
"rangkum obrolan ini" command (`_handle_manual_summarize_command()`),
both in `main_runtime_demo.py`. It asks the LLM to compress
`session_log` into 1-3 sentences, appends the result to
`_session_summaries`, saves `config/session_summaries.json`, then
clears `session_log` — but ONLY on success (a genuine failure leaves
both untouched, isolated inside a single `try/except Exception as ex:
print(f"[Memory] ✗ Session summary error: {ex}"); return None`).

In production, `summarize_and_archive_session()`'s `openai_client`
argument is wired by `register_session_summary_client()`
(`luno/bootstrap/adapters.py`, line 454) to
`openrouter_adapter.client` — the real
`luno.adapters.openrouter.RequestsOpenRouterClient` (a "new pipeline"
client, detected via `hasattr(openai_client, "chat_completion")`; the
function's OTHER duck-typed branch, a raw legacy `openai` SDK client
via `.chat.completions.create(...)`, is dormant in production today,
per that same wiring — but is reachable, e.g. from `luno/main.py`'s
older entry point, and is fixed identically below for consistency).

`RequestsOpenRouterClient._payload()` (`luno/adapters/openrouter.py`,
the ONE method every `chat_completion()`/`stream_chat_completion()`
call goes through to build its HTTP request body) unconditionally
wrote the completion-length JSON key as the **literal string
`"max_tokens"`**, regardless of which model was configured:

```python
tokens = max_tokens if max_tokens is not None else cfg.max_tokens
if tokens is not None:
    body["max_tokens"] = tokens
```

Meanwhile, `summarize_and_archive_session()`'s new-pipeline branch
hardcoded `max_tokens=150` as a literal Python keyword on every call —
the ONLY caller anywhere in the codebase that ever passes a **non-None**
`max_tokens` into this client. Every other caller was checked and
confirmed to never populate it:

- `main_runtime_demo.py`'s `NeedLLMResponse` publisher for ordinary
  chat (line 4982, `llm_request_data` dict, confirmed by direct read)
  never includes a `max_tokens` key at all.
- `RequestsOpenRouterClient.chat_completion()`'s own `event.get(
  "max_tokens")` (`OpenRouterAdapter._handle_need_llm_response()`,
  line 761) is therefore always `None` for normal chat, falling back to
  `cfg.max_tokens` — which comes from `OPENROUTER_MAX_TOKENS`, an env
  var with **no default value** (`OpenRouterConfig.from_env()`:
  `max_tokens=_int("OPENROUTER_MAX_TOKENS", None)`).

So for every request EXCEPT Session Summary, `tokens` was `None` and
the completion-length key was never added to the request body at all
— the model just used its own default output length. This is exactly
why "normal chat already works" with the currently configured model:
it was never sending the incompatible parameter in the first place.
Session Summary was the only path that did, which is exactly why the
reported error is scoped to Session Summary and not general chat.

Separately confirmed: normal conversational chat does **not** even go
through `luno.adapters.openrouter.OpenRouterAdapter` in production —
`register_intent_classifier()`'s own docstring (`luno/bootstrap/
adapters.py`, line ~493) states real conversational replies use `luno.
adapters.llm_manager.LLMManagerAdapter.client` instead, a SEPARATE
multi-provider abstraction. `OpenRouterAdapter` is used only for
Session Summary and the (also Sprint-51-era) device intent classifier
(`register_device_intent_classifier()`, same file, same
`openrouter_adapter.client` source) — meaning this fix's blast radius
is smaller than "the whole LLM stack": it only ever affected these two
call sites, and only Session Summary ever passed a non-None
`max_tokens` before this fix.

The project already has a working abstraction for exactly this
model-parameter-naming incompatibility: `luno.config.MAX_TOKENS_PARAM`
(`luno/config.py` line 348) —

```python
MAX_TOKENS_PARAM = os.getenv("MAX_TOKENS_PARAM", "max_completion_tokens").strip()
if MAX_TOKENS_PARAM not in ("max_tokens", "max_completion_tokens"):
    print(f"[Config] ⚠ MAX_TOKENS_PARAM='{MAX_TOKENS_PARAM}' tidak dikenal, pakai 'max_completion_tokens'")
    MAX_TOKENS_PARAM = "max_completion_tokens"
```

— already used correctly by `luno/main.py`'s three legacy OpenAI-SDK
call sites (`**{config.MAX_TOKENS_PARAM: ...}`), but never consulted
anywhere in `luno/memory.py` or `luno/adapters/openrouter.py`.

## The fix (smallest safe change, reuses the existing abstraction)

Two files, both directly on the confirmed call chain, no new
abstraction created:

1. **`luno/adapters/openrouter.py`** — `RequestsOpenRouterClient.
   _payload()`: the hardcoded `body["max_tokens"] = tokens` line now
   reads `body[config.MAX_TOKENS_PARAM] = tokens`. One new import
   (`from .. import config`, no circular risk — `luno/config.py` has
   no imports from `luno.adapters`, confirmed by reading it). This is
   the fix for the ACTUAL reported production bug: it changes the one
   shared request-payload method both the new-pipeline Session Summary
   call and (incidentally, correctly) the device intent classifier go
   through. It does **not** touch `luno.adapters.llm_manager`/`luno.
   adapters.llm.*` — the separate stack that powers normal chat (see
   "Explicitly out of scope" below) — so "if normal chat already
   works, do not break that path" is satisfied structurally, not just
   by luck: normal chat's request-building code was never touched.

2. **`luno/memory.py`** — `summarize_and_archive_session()`'s legacy
   raw-`openai`-SDK branch (dormant in production, reachable
   elsewhere): `max_tokens=150` → `**{config.MAX_TOKENS_PARAM: 150}`,
   mirroring `luno/main.py`'s own established pattern exactly (`config`
   was already imported in this file). Defense-in-depth consistency,
   not a second root-cause fix — this branch does not run in the
   production wiring that produced the reported error (confirmed via
   `register_session_summary_client()`), but hardcoding the same
   incompatible literal here, right next to the branch that WAS fixed
   for the identical reason, would leave a latent duplicate of the
   exact bug this sprint exists to close.

The new-pipeline branch's own `max_tokens=150` keyword argument was
**not** changed — that name is the `OpenRouterClient.chat_completion()`
Python interface's own parameter name (an internal calling convention,
unrelated to the wire JSON key), and passing it unchanged is correct;
only `_payload()`'s translation of that Python parameter into an HTTP
JSON key needed to become config-driven.

### Explicitly forbidden approaches, and why they were avoided

- **No blind global replace** of `max_tokens` → `max_completion_tokens`
  anywhere in the repo. Only the two call sites on the confirmed,
  traced call chain were touched.
- **No silently sending both parameters.** `_payload()` sends exactly
  one key, chosen by `config.MAX_TOKENS_PARAM`.
- **No try/except runtime fallback** (`try: max_completion_tokens
  except: max_tokens`). `config.MAX_TOKENS_PARAM` already lets an
  operator declare which family the configured model needs, matching
  this project's existing convention (`luno/main.py`) — no evidence of
  a genuine need for per-request runtime probing was found.
- **No provider/model-specific hardcoding** beyond what already
  existed. `config.MAX_TOKENS_PARAM` is a single global setting (not
  per-model), matching its existing, working use in `luno/main.py` —
  introducing a NEW per-model capability table was considered
  unnecessary scope expansion for a project that (per `luno/config.py`'s
  own comment) already treats this as an operator-configured global.

## Memory safety (Phase 4)

Session Summary failure was ALREADY correctly isolated before this
sprint (the single `try/except` wrapping both branches, `return None`
on any exception, `session_log.clear()` only reached on the success
path after the `try` block) — this sprint changed WHAT gets sent over
the wire, not the isolation/error-handling structure itself. Verified
by test (`test_genuine_failure_is_isolated_from_memory_state`,
`test_legacy_branch_error_isolated_and_logged`): a genuine provider
failure still returns `None`, still leaves `session_log` and
`_session_summaries` untouched, still prints the exact
`[Memory] ✗ Session summary error: ...` line, and no exception escapes.
No change to memory ranking, topic continuity, or any other retrieval
logic — `luno/memory_context.py` and `luno/memory_retrieval/*` (a
semantically different, unrelated "max_tokens" — a memory-retrieval
context-budget cap, not an LLM API request parameter) were read only
to confirm they were NOT part of this call chain, and were not
modified.

## Compatibility model

`config.MAX_TOKENS_PARAM` is a single, global, operator-configured
setting (env var `MAX_TOKENS_PARAM`, default `"max_completion_tokens"`,
validated against exactly `("max_tokens", "max_completion_tokens")`,
falling back to the default with a warning print on anything else).
This sprint extends its reach (previously `luno/main.py` only) to
every request `RequestsOpenRouterClient._payload()` builds, and to
`summarize_and_archive_session()`'s legacy branch. It is bounded and
provider-aware in the sense the project already established: one
operator-set value, applied consistently, not a per-model lookup
table — confirmed genuinely config-driven (not a second hardcoded
literal) by `test_completion_token_param_is_config_driven_not_hardcoded`,
which points the same code at BOTH valid values and observes the wire
body change accordingly.

## Tests — 13 new, ACTUALLY EXECUTED THIS SESSION

`tests/test_memory_session_summary_api_compatibility.py` — **13
passed, 0 failed**, run via `pytest -q tests/
test_memory_session_summary_api_compatibility.py` against a
minimal-but-real dependency chain assembled in this session's cloud
sandbox (see "Execution method" below). Covers all 9 minimum items the
sprint brief required, plus the legacy branch and an explicit
before/after reproduction:

1. `test_summary_request_uses_configured_completion_token_param` —
   summary uses the correct completion-token parameter.
2. `test_max_tokens_key_never_generated_with_default_config` +
   `test_completion_token_param_is_config_driven_not_hardcoded` — the
   failing `max_tokens` key is never generated under default config,
   and the behavior is proven config-driven (not a second hardcoded
   literal) by pointing the same code at the OLDER key name and
   observing the wire body change accordingly.
3. `test_summary_succeeds_end_to_end_and_is_archived` — summary
   request succeeds against a mocked compatible provider, end to end
   (archived to `_session_summaries`, `session_log` cleared).
4. `test_normal_chat_style_call_without_max_tokens_is_unaffected` —
   existing normal-chat-style call (no explicit `max_tokens`) is
   byte-for-byte unaffected — still omits the key entirely.
5. `test_genuine_failure_is_isolated_from_memory_state` — summary
   failure stays isolated from main memory state.
6. `test_configured_model_is_respected_not_overridden` — existing
   provider/model configuration is respected, never silently
   overridden.
7. `test_exactly_one_request_per_summary_call` — no duplicate request
   is generated.
8. `test_no_persistent_state_file_touched_besides_session_summaries` —
   no persistent config file is modified besides the (already
   test-isolated) session summaries file.
9. `test_error_logging_preserved_for_unrelated_provider_failure` —
   existing error logging still works for a genuinely unrelated
   provider failure.
10. `test_legacy_branch_uses_configured_completion_token_param` +
    `test_legacy_branch_error_isolated_and_logged` — the legacy
    raw-openai-SDK branch, fixed identically.
11. `test_before_fix_behavior_would_have_reproduced_the_exact_reported_error`
    — a realistic (not live) BEFORE/AFTER reproduction: a fake HTTP
    session that rejects any request body containing a literal
    `"max_tokens"` key with the EXACT reported error text, and accepts
    a body containing `config.MAX_TOKENS_PARAM`'s configured key.
    Simulating the OLD hardcoded behavior (by pointing
    `config.MAX_TOKENS_PARAM` at the literal `"max_tokens"` — the exact
    value the code used to hardcode, not by re-injecting old code)
    reproduces the reported error string verbatim; the current default
    configuration does not.

### Execution method

A minimal-but-real dependency chain was assembled in this session's
cloud sandbox: `luno/{__init__,config,persistence,memory}.py`, the
full `luno/adapters/` package (`__init__.py` eagerly imports every
adapter — `base`, `events`, `exceptions`, `models`, `manager`,
`registry`, `scheduler`, `openrouter` (this sprint's own edit),
`fish_audio`, `fish_audio_real`, `home_assistant`, `unity`, `vision`,
`whisper`, `llm_manager`, plus the `llm/` sub-package it imports from),
`luno/core/` (all 14 files `luno/core/__init__.py` imports), `luno/
vision_memory/` (imported by `tests/conftest.py`'s isolation fixture),
and `luno/speech_chunk.py` (a transitive dependency of `fish_audio.py`
surfaced during assembly). All staged unmodified from the real
checkout except this sprint's own two edits. `tests/conftest.py`
(unmodified) was staged as-is — its autouse `isolate_persistent_state`
fixture is what redirects `config.SESSION_SUMMARIES_FILE` to a
`tmp_path`-derived location for every test in this file, so no test
here ever touches Vinn's real `config/session_summaries.json`.

## Regression — full results

- **`tests/test_memory_session_summary_api_compatibility.py`** (new):
  **13 passed, 0 failed.**
- **`luno/adapters/tests/test_openrouter_adapter.py`** (pre-existing,
  UNMODIFIED, run via its own documented standalone entry point,
  `python3 -m luno.adapters.tests.test_openrouter_adapter`): **31/31
  scenarios passed** — real proof this sprint's `_payload()` change
  does not regress the file it lives in, including every retry/
  backoff/streaming/cancellation/status-classification test.
- **`luno/adapters/tests/test_llm_manager.py`** (pre-existing,
  UNMODIFIED — the separate multi-provider stack this sprint
  deliberately did NOT touch): **33 passed, 0 failed** via `pytest -q`
  — confirms the untouched stack (which has the textually identical
  latent hardcoded-`"max_tokens"` pattern in `luno/adapters/llm/
  base.py`, see "Known limitation / Sprint 54 candidate" below) is
  unaffected either way, and that this sprint genuinely left it alone.
- **`tests/test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py`** (pre-existing, UNMODIFIED):
  **16 passed, 3 skipped** (skips are pre-existing and
  environment-specific — two reference `recovery/*.json` snapshot
  files not present in this sandbox checkout, unrelated to this
  sprint) — confirms this sprint's `luno/memory.py` edit does not
  regress the file's own existing persistence/regression test
  coverage.
- **Combined single run** (`pytest -q` across all five files above):
  **93 passed, 3 skipped, 0 failed.**

**NOT run this session:** the full ~2900-test repository sweep (only
this feature's real dependency chain was staged, not the whole
checkout — no network access for the rest, e.g. the vision/OpenCV,
Whisper/audio, or Unity stacks' own heavier third-party dependencies);
any test file requiring the real Windows `.venv` or a live network
call. See §16/§22 of `docs/project_handover.md` for the standing
recommendation (carried over from Sprint 52) that a future session
with real device/network access run the full chunked 8-way sweep
before treating ANY sprint filed via this sandbox methodology as part
of the "verified, stable, production baseline" language elsewhere in
that document.

## Live verification — NOT performed, explicitly

**No live call to OpenRouter (or any real LLM provider) was made this
session.** This sandbox has no `OPENROUTER_API_KEY` and, per the
established constraint from Sessions 1-2 of this same takeover
lineage, the device bridge to the real Windows checkout has no network
access. The "before/after reproduction" test above
(`test_before_fix_behavior_would_have_reproduced_the_exact_reported_error`)
is a REALISTIC SIMULATION of the real provider's own documented
rejection rule (a fake HTTP session that returns the exact historical
error JSON shape `{"error": {"message": "..."}}` for a `"max_tokens"`
key, matching `RequestsOpenRouterClient._error_message()`'s own
parsing code, confirmed by direct read) — it is not a live call, and
is not represented as one anywhere in this document or the final
report. **The single most important remaining verification step**
before trusting this fix in production is: with `OPENROUTER_API_KEY`
set to a real key and `OPENROUTER_MODEL` set to whatever model
actually produced the original bug report, trigger a real Session
Summary (end a wake-word conversation, or say "rangkum obrolan ini")
and confirm BOTH that no `[Memory] ✗ Session summary error:` line
appears AND that a real `[Memory] ✓ Session summary saved: ...` line
does, with a real, sensible summary text.

## Performance

`RequestsOpenRouterClient._payload()` (the only method this sprint
changed) measured for real in this sandbox (5,000-call loop, `time.
perf_counter()`): **~0.0006ms/call**, far under the 5ms target — the
change is a single dict-key-name substitution (`config.MAX_TOKENS_PARAM`
is a module-level constant computed once at import time), not a new
computation.

## Persistent state

Verified: `find`-based diff before/after the full test run found zero
`*.json` files created or modified under this sandbox's staged
checkout root, and the assembled `config/` directory (empty,
deliberately) received no writes. Every test's own writes land under
pytest's `tmp_path` (via `tests/conftest.py`'s autouse
`isolate_persistent_state` fixture, unmodified this sprint), never
Vinn's real `config/*.json` files. No config file was legitimately
required to change for this fix (it changes what parameter NAME is
sent over HTTP, not any stored configuration value).

## Observability (Phase 10)

Reviewed, deliberately UNCHANGED. `summarize_and_archive_session()`
has no existing Event Bus integration — it is, and remains,
print-based only (`[Memory] ✓ Session summary saved: ...` /
`[Memory] ✗ Session summary error: {ex}`). Sprint 50's own
observability tap (`docs/project_handover.md` §2/§15) is scoped
specifically to the memory RETRIEVAL/selection pipeline during a live
turn (`memory_reference_classified`/`memory_topic_decision`/
`memory_selection_summary`) — a different pipeline from end-of-session
archival, with no established convention this sprint could correctly
extend. Per the sprint brief's own instruction ("do NOT create a new
event type merely for its own sake — follow existing architecture"),
no new Event Bus event was added. The existing print-based log line
already makes the error diagnosable — it is, verbatim, how this bug
was originally reported — and this fix's own before/after test
confirms that line's exact format is unchanged.

## Known limitation / Sprint 54+ candidate (documented, NOT fixed — out of this sprint's explicit scope)

`luno/adapters/llm/base.py` (`OpenAICompatibleClient._payload()`,
lines ~344-358 — part of the SEPARATE `luno.adapters.llm_manager`
multi-provider stack that powers normal conversational chat, per
`luno/adapters/llm/openrouter_provider.py`'s own module docstring:
"This is a NEW, separate implementation from `luno.adapters.openrouter.
OpenRouterAdapter`... the two `OpenRouterAdapter` classes coexist and
never call each other") has the **textually identical** hardcoded
`body["max_tokens"] = tokens` pattern this sprint fixed in `luno/
adapters/openrouter.py`. This is a plausible SEPARATE latent bug in a
genuinely different code path — normal chat currently appears
unaffected only because (per this sprint's own root-cause tracing) no
caller on that path currently supplies a non-None `max_tokens` either,
the same structural reason Session Summary was the only path
triggering the ORIGINAL bug. This was found while confirming this
sprint's fix would not overlap with, or need to touch, normal chat's
own request-building code — it was **not** fixed here, per the sprint
brief's explicit "if another bug is discovered, document it as a
finding for a separate sprint rather than fixing it opportunistically"
instruction. Recommended as a Sprint 54+ candidate: the same
`config.MAX_TOKENS_PARAM`-based fix, applied to `luno/adapters/llm/
base.py`, with its own dedicated regression run against `luno/adapters/
tests/test_llm_manager.py` (33 pre-existing tests, confirmed passing
unmodified this session) and any of the five individual provider test
files under `luno/adapters/llm/tests/` (not staged or inspected this
session — see `docs/project_handover.md` §22 for the exact
recommendation).

## Files modified

- `luno/adapters/openrouter.py` — `_payload()`'s completion-length JSON
  key is now `config.MAX_TOKENS_PARAM`-driven instead of the hardcoded
  literal `"max_tokens"`; one new import (`from .. import config`).
- `luno/memory.py` — `summarize_and_archive_session()`'s legacy branch
  now sends `**{config.MAX_TOKENS_PARAM: 150}` instead of the hardcoded
  literal `max_tokens=150`.
- `docs/project_handover.md`, `docs/project_handover.json`,
  `ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`
  (this change's own documentation).

## Files created

- `tests/test_memory_session_summary_api_compatibility.py`.
- `docs/change_impact/memory_session_summary_api_compatibility.md`
  (this file).

## Explicitly out of scope (per the sprint brief, untouched)

Home Assistant entity resolution (Sprint 52), Vision/OpenCV/FFmpeg, TTS/
Fish Audio, Dashboard turn-state logic (both Dashboard Turn-State
Recovery fixes), memory ranking/topic continuity/reference resolution
(`luno/memory_context.py`, `luno/memory_retrieval/*` — read only to
confirm their OWN "max_tokens" is an unrelated retrieval-budget concept,
never modified), `luno.adapters.llm_manager`/`luno.adapters.llm.*` (see
"Known limitation" above — read, not modified), and any HA command
semantics. Zero lines changed in any of those files or subsystems.
