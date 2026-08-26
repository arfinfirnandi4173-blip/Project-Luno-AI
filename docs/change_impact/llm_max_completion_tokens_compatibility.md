# Sprint 54 (user-numbered) — LLM Stack API Compatibility & Max Completion Tokens Hardening

Filed under `ARCHITECTURE_GUARD.md` section **55**. This is a takeover
session's continuation of Sprint 53 (Memory Session Summary API
Compatibility Fix, `ARCHITECTURE_GUARD.md` §54,
`docs/change_impact/memory_session_summary_api_compatibility.md`),
narrowly scoped to the latent bug that sprint identified but did not
fix: `luno/adapters/llm/base.py`.

## IMPORTANT CORRECTION TO SPRINT 53'S DOCUMENTED ROOT CAUSE

This sprint's own Phase 0/1 reconnaissance (reading the actual
production bootstrap wiring, not re-trusting Sprint 53's prose)
discovered that **Sprint 53's root-cause tracing had a gap**: it fixed
`luno/adapters/openrouter.py` (`RequestsOpenRouterClient._payload()`)
believing that class was what `register_session_summary_client()`
wires into `luno.memory.summarize_and_archive_session()`. It is not.

`luno/bootstrap/adapters.py` (line 137):

```python
openrouter_adapter = LLMManagerAdapter()
```

— NOT `luno.adapters.openrouter.OpenRouterAdapter()`. The local
variable (and the `adapters` dict key it's stored under,
`"openrouter_adapter"`) keeps its old name for backward compatibility
with every downstream reader of that dict (`register_session_summary_
client()`, `register_device_intent_classifier()`, `register_intent_
classifier()`, none of which needed to change), but the actual
constructed class changed. `luno/adapters/llm_manager.py`'s own module
docstring says this explicitly: *"Replaces `luno.adapters.openrouter.
OpenRouterAdapter` as the module `bootstrap/adapters.py` actually
constructs and registers (that other class, and its own tests, are
untouched — see that module's docstring for why both continue to
exist)."* Confirmed by grep: `luno.adapters.openrouter.OpenRouterAdapter`
is not imported anywhere in `bootstrap/adapters.py`, `main_runtime_demo.py`,
or `luno/main.py` — it is **orphaned in production**, kept alive only
by its own test file (`luno/adapters/tests/test_openrouter_adapter.py`).

The REAL production call chain for Session Summary (and the also-
Sprint-51-era device intent classifier, which shares the identical
wiring pattern) is:

```
summarize_and_archive_session(openai_client=planner_module.session_summary_client, ...)
  -> openai_client.chat_completion(model=..., messages=..., max_tokens=150)
       (openai_client == LLMManagerAdapter.client == a _LegacyClientShim)
  -> _LegacyClientShim.chat_completion(...) -> LLMManagerAdapter.chat_once(...)
  -> client = self._clients[provider_name]   # provider_name from _priority_order();
                                              # LLM_PROVIDER defaults to "openrouter"
  -> client.chat(messages, model=..., max_tokens=150, ...)
       (client == luno.adapters.llm.openrouter_provider.OpenRouterProvider —
        a DIFFERENT, confusingly-similarly-named OpenAICompatibleClient
        subclass, NOT luno.adapters.openrouter.OpenRouterAdapter)
  -> OpenAICompatibleClient.chat() -> OpenAICompatibleClient._payload()
       (luno/adapters/llm/base.py — THIS sprint's target)
```

**Practical consequence:** Sprint 53's fix to `luno/adapters/openrouter.py`
was code-correct, genuinely tested (31/31 of that file's own suite,
unchanged), and harmless — but it did **not** fix the originally
reported production bug, because that class was never on the path the
bug report's runtime actually executes. **This sprint's fix, to
`luno/adapters/llm/base.py`, is the fix that actually resolves the
originally reported `[Memory] ✗ Session summary error: Unsupported
parameter: 'max_tokens'...` bug in production**, assuming the default
`LLM_PROVIDER=openrouter` (or `openai`/`local` — any
`OpenAICompatibleClient` subclass) is what's actually configured.
Sprint 53's own fix is not reverted or considered wrong — it remains a
correct, tested, harmless improvement to a class that may be
reactivated or referenced elsewhere later — but its "AFFECTED PROVIDER/
MODEL/CALL CHAIN" claim in `docs/project_handover.md`/`ARCHITECTURE_
GUARD.md` §54 should be read with this correction in mind. Both
documents are updated by this sprint (see §13/`ARCHITECTURE_GUARD.md`
§55) to carry this correction forward rather than silently leaving the
old claim standing.

## Root cause (this sprint's actual target)

`luno.adapters.llm.base.OpenAICompatibleClient._payload()` — the ONE
shared request-body builder every `OpenAICompatibleClient` subclass
(`OpenRouterProvider`, `OpenAIProvider`, `LocalProvider` — confirmed by
grep: none of the three override `_payload()`, `chat()`, or
`stream_chat()`) uses for BOTH `chat()` (non-streaming) and
`stream_chat()` (streaming) — unconditionally wrote the
completion-length JSON key as the literal string `"max_tokens"`:

```python
tokens = max_tokens if max_tokens is not None else cfg.max_tokens
if tokens is not None:
    body["max_tokens"] = tokens
```

Textually identical to the bug Sprint 53 fixed in the (as established
above, production-orphaned) `luno/adapters/openrouter.py`.

`Gemini` (`gemini_provider.py`) and `Anthropic` (`anthropic_provider.py`)
do **not** subclass `OpenAICompatibleClient` — confirmed by direct
read of both modules' own docstrings and `_body()` methods:

- Gemini writes `generationConfig.maxOutputTokens` — an entirely
  different JSON key, not part of this incompatibility at all.
- Anthropic writes `"max_tokens"` too, but that is Anthropic's own
  **correct, required** field name (confirmed by
  `anthropic_provider.py`'s own module docstring: *"`max_tokens` is
  REQUIRED by this API (unlike OpenAI/Gemini, where it's optional)"*)
  — NOT the same OpenAI-specific parameter-rename issue, and must
  remain unchanged. `luno/adapters/llm/tests/test_providers.py`'s own
  pre-existing `test_anthropic_chat_uses_top_level_system_and_required_
  max_tokens` already asserts `body["max_tokens"] == 1024` — left
  passing, unmodified, by this sprint (see Regression below).

## The fix

One file, one method, mirroring Sprint 53's own established pattern
exactly:

```python
# luno/adapters/llm/base.py
from ... import config
...
tokens = max_tokens if max_tokens is not None else cfg.max_tokens
if tokens is not None:
    body[config.MAX_TOKENS_PARAM] = tokens
```

No circular import risk: `luno/config.py` has no imports from
`luno.adapters` (confirmed by reading it, same check performed for
Sprint 53's own `openrouter.py` fix). Verified live via
`python3 -c "import luno.adapters.llm.base"` in this session's
sandbox.

## Why the existing MAX_TOKENS_PARAM abstraction is correct here

Identical reasoning to Sprint 53: `config.MAX_TOKENS_PARAM`
(`luno/config.py`, defaults `"max_completion_tokens"`) is a single,
global, operator-configured setting already established as the
project's answer to this exact incompatibility, already used correctly
by `luno/main.py` and now by `luno/adapters/openrouter.py` (Sprint 53).
Extending its reach to the SECOND, actually-live request builder is
the smaller, more consistent change than introducing a second
mechanism, a per-provider table, or a runtime try/except fallback — all
explicitly forbidden by this sprint's own brief. Anthropic and Gemini
are correctly left out of this abstraction's reach entirely (their own
`_body()` methods never read `config.MAX_TOKENS_PARAM`) — confirmed by
two dedicated regression tests (see Tests below) that swap
`config.MAX_TOKENS_PARAM` between both valid values and assert neither
provider's own wire behavior changes at all.

## Payload before/after

Before (any `OpenAICompatibleClient` subclass, `max_tokens=150`
requested):
```json
{"model": "m", "messages": [...], "stream": false, "max_tokens": 150}
```

After (default `config.MAX_TOKENS_PARAM = "max_completion_tokens"`):
```json
{"model": "m", "messages": [...], "stream": false, "max_completion_tokens": 150}
```

Requested token COUNT (`150`) is byte-for-byte unchanged — only the
JSON key name changes, exactly per this sprint's own non-negotiable.

## Affected request paths (Phase 4 — all checked, not assumed)

- **A. Non-streaming (`chat()`)** — calls `self._payload(...)`. Fixed.
- **B. Streaming (`stream_chat()`)** — calls the SAME `self._payload(...)`
  (confirmed by direct read, `base.py` lines 387 and 451). Fixed by the
  same one-line change; proven by a dedicated streaming test per
  subclass.
- **C. Tool/function-calling** — confirmed NOT APPLICABLE: no separate
  payload builder exists for tool calls anywhere in this stack.
  `supports_tools()` is a pure capability-reporting flag, never
  consulted by `_payload()`/`chat()`/`stream_chat()` to alter the
  request shape. The only metadata-driven extra field this stack
  currently supports is `reasoning_effort` (OpenAI only, via
  `_extra_payload_fields()`), unrelated to and independently proven
  non-interfering with the token-param fix (see Tests).
- **D. Retry path** — `retry_call()` re-invokes the SAME already-built
  `payload` dict on each attempt (built once, before the retry loop
  starts, in both `chat()` and `stream_chat()`) — never rebuilds it, so
  there is no separate retry-path payload construction to fix.
- **E. Subclass/inherited adapters** — `OpenRouterProvider`,
  `OpenAIProvider`, `LocalProvider` all inherit `_payload()`/`chat()`/
  `stream_chat()` unmodified (confirmed by grep: none of the three
  files define any of those three names) — one fix in the base class
  covers all three subclasses, proven by parametrizing every new test
  across all three real (not mocked) provider classes.

## Tests — 24 new, ACTUALLY EXECUTED THIS SESSION

`tests/test_llm_max_completion_tokens_compatibility.py` — **24 passed,
0 failed**, run via `pytest -q tests/
test_llm_max_completion_tokens_compatibility.py` against a
minimal-but-real dependency chain (this session reused and extended
Sprint 53's own assembled sandbox tree). Every test exercises the
REAL, unmodified `OpenRouterProvider`/`OpenAIProvider`/`LocalProvider`/
`AnthropicProvider`/`GeminiProvider` classes (never a re-implementation
of `_payload()`), via a local, self-contained `_FakeSession`/
`_FakeResponse` double (same pattern `luno/adapters/llm/tests/
test_providers.py` and Sprint 53's own new test file already
establish) — no real network access anywhere.

Covers all 10 minimum items the sprint brief required, plus two
boundary-of-scope regression tests:

1. `test_payload_uses_configured_completion_token_param` (parametrized
   x3) — payload uses `config.MAX_TOKENS_PARAM`.
2. `test_legacy_max_tokens_key_never_generated_with_default_config`
   (x3) — legacy `max_tokens` never sent under default config.
3. Requested token count preserved exactly — asserted inline in test 1
   above (`body[config.MAX_TOKENS_PARAM] == 150`).
4. `test_request_without_token_limit_has_no_token_limit_field` (x3) —
   no explicit limit -> no key sent at all, matching pre-sprint
   behavior for ordinary chat exactly.
5. `test_streaming_payload_uses_configured_completion_token_param`
   (x3) — the streaming path.
6. `test_metadata_with_tool_like_keys_does_not_interfere_with_token_
   param` — tool/function path confirmed not applicable by inspection;
   proven non-interfering with an actual metadata-driven extra field.
7. Subclass/inherited adapter behavior — every test above is
   parametrized across all 3 real `OpenAICompatibleClient` subclasses.
8. `test_completion_token_param_is_config_driven_not_hardcoded` (x3) —
   proven genuinely config-driven (not a second hardcoded literal) by
   pointing the same code at BOTH valid values.
9. `test_sprint53_openrouter_adapter_fix_remains_intact` — Sprint 53's
   own separate fix, in the separate `luno.adapters.openrouter` module
   this sprint deliberately did not touch, still works.
10. `test_before_fix_behavior_would_have_reproduced_the_exact_reported_
    error` (x3) — realistic (not live) before/after reproduction via a
    fake HTTP session that rejects a literal `"max_tokens"` key with
    Sprint 53's own exact reported error text, and accepts
    `config.MAX_TOKENS_PARAM`'s configured key.

Plus, boundary-of-scope regression (2 tests x 2 config values = 4
cases): `test_anthropic_provider_unaffected_by_max_tokens_param_config`,
`test_gemini_provider_unaffected_by_max_tokens_param_config` — prove
neither non-`OpenAICompatibleClient` provider's own wire behavior
changes regardless of what `config.MAX_TOKENS_PARAM` is set to.

## Targeted regression — full results

Combined single `pytest -q` run across the new file plus every
directly relevant pre-existing file (all UNMODIFIED by this sprint):

- `tests/test_llm_max_completion_tokens_compatibility.py` (new) — **24
  passed.**
- `luno/adapters/llm/tests/test_providers.py` (pre-existing — covers
  every `LLMProviderClient` implementation, including the
  Anthropic-required-`max_tokens` regression guard and the
  reasoning-effort tests) — **48 passed.**
- `luno/adapters/tests/test_llm_manager.py` (pre-existing —
  `LLMManagerAdapter` itself, the class this sprint's fix is actually
  reached through in production) — **33 passed.**
- `luno/adapters/tests/test_openrouter_adapter.py` (pre-existing, the
  Sprint-53-fixed, production-orphaned class — run both ways to be
  sure: via its own documented standalone entry point, **31/31
  scenarios passed**; also collected and run under plain `pytest -q`
  as part of the combined run below, **31 passed** there too).
- `tests/test_memory_session_summary_api_compatibility.py` (Sprint 53's
  own new file — the ORIGINAL bug's own regression suite) — **13
  passed.**
- `tests/test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py` (pre-existing) — **16 passed,
  3 skipped** (same pre-existing, environment-specific skips Sprint 53
  already documented — two missing `recovery/*.json` snapshot files,
  unrelated to this sprint).
- **Combined single `pytest -q` run: 165 passed, 3 skipped, 0 failed.**

**NOT run this session:** the full ~2900-test repository sweep (same
standing limitation as Sessions 1-3/Sprints 52-53 — only this feature's
real dependency chain was staged, no network access for the rest of
the checkout's own heavier third-party dependencies, e.g. vision/
OpenCV, Whisper/audio, Unity); any test file requiring the real Windows
`.venv`.

## Live verification — NOT performed, explicitly

No live call to any real LLM provider was made this session, same
constraint as every prior takeover session in this lineage (no API
keys, no network access to the device bridge). The before/after
reproduction tests are realistic SIMULATIONS of each provider's own
documented rejection rule, never represented as live calls anywhere in
this document. The single most important remaining verification step,
identical in spirit to Sprint 53's own: with a real
`OPENROUTER_API_KEY`/`OPENAI_API_KEY` and `LLM_PROVIDER` set to whatever
provider actually produced the original bug report, trigger a real
Session Summary and confirm no `[Memory] ✗ Session summary error:` line
appears.

## Performance

`OpenAICompatibleClient._payload()` (the only method changed) measured
for real in this sandbox: 5,000-call loop, mean **~0.0007ms/call**;
200-call sample, max **~0.0039ms**. Far under the 5ms target — a single
dict-key-name substitution against an already-computed module-level
constant, consistent with Sprint 53's own measurement of the
structurally identical fix (~0.0006ms/call).

## Persistent state

Verified via `find`-based diff before/after the full test run: zero
`config/*.json` files created or modified (the only newer file found
was `docs/project_handover.json` itself, this sprint's own
documentation edit — not persistent runtime state). No config file was
legitimately required to change for this fix.

## Known limitations

- Full ~2900-test repository sweep and live-provider verification not
  performed this session (see above).
- `luno/adapters/llm/anthropic_provider.py` and `gemini_provider.py`
  were read and tested for NON-regression only — their own,
  provider-correct request-building code was not, and should not be,
  modified by this sprint.
- This sprint does not verify whether the real deployment's `.env`
  currently sets any `{PROVIDER}_MAX_TOKENS` value (e.g.
  `OPENROUTER_MAX_TOKENS`, `OPENAI_MAX_TOKENS`) — if one is set, that
  provider's ordinary (non-Session-Summary) chat traffic would ALSO
  have been silently sending the incompatible key before this fix,
  same as Session Summary was. This sprint's fix covers that case
  correctly regardless (the fix is unconditional on `_payload()`, not
  keyed to which caller triggered it) — this note is about SCOPE OF
  IMPACT reporting, not about anything left unfixed.

## Deferred findings (not fixed, per this sprint's own scope discipline)

None discovered beyond the correction to Sprint 53's own documentation
recorded above. No other independent `max_tokens`-shaped bug was found
in this sprint's own search of `luno/adapters/llm/*.py` beyond what is
described here.

## Files modified

- `luno/adapters/llm/base.py` — `OpenAICompatibleClient._payload()`'s
  hardcoded `body["max_tokens"] = tokens` now reads
  `body[config.MAX_TOKENS_PARAM] = tokens`; one new import
  (`from ... import config`).
- `docs/project_handover.md`, `docs/project_handover.json`,
  `ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`,
  `docs/change_impact/memory_session_summary_api_compatibility.md`
  (Sprint 53's own doc, appended with a correction note — see that
  file's own updated content).

## Files created

- `tests/test_llm_max_completion_tokens_compatibility.py`.
- `docs/change_impact/llm_max_completion_tokens_compatibility.md`
  (this file).

## Explicitly out of scope (per the sprint brief, untouched)

LLM routing architecture, `LLMManagerAdapter`'s own fallback/priority/
health logic, memory ranking/persistence, entity identity/semantic
alias continuity, Home Assistant, Vision, Fish Audio/TTS, barge-in,
dashboard, Event Bus semantics, session state machine, tool manager,
streaming semantics (beyond proving the SAME payload fix covers it),
model selection, retry policy, temperature, system prompts,
conversation history, token BUDGETING semantics (the actual numeric
limits — `luno/memory_retrieval/`'s own, unrelated "max_tokens" concept
— untouched, confirmed a different concern per Sprint 53's own
identical scope note), and persistent `config/*.json` state. Zero
lines changed in any of those files or subsystems.
