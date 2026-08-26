"""
test_memory_session_summary_api_compatibility.py
===================================================

Sprint 53 - Memory Session Summary API Compatibility Fix.

Root cause (see `docs/change_impact/memory_session_summary_api_compatibility.md`
for the full writeup): `luno.memory.summarize_and_archive_session()` is the
Session Summary feature - it runs when a wake-word conversation ends (or on
the manual "rangkum obrolan ini" command) and asks the LLM to compress
`session_log` into 1-3 sentences. In production it is wired (see
`luno/bootstrap/adapters.py::register_session_summary_client()`) to the real
`luno.adapters.openrouter.OpenRouterAdapter.client` - a `RequestsOpenRouterClient`
- and is THE ONLY caller anywhere in the codebase that ever passes a non-None
`max_tokens` value into that client's `chat_completion()` (150, hardcoded).
Every other caller (`main_runtime_demo.py`'s `NeedLLMResponse` publisher for
normal chat) never sets `max_tokens` on the event, and `OPENROUTER_MAX_TOKENS`
has no default value, so `RequestsOpenRouterClient._payload()`'s `tokens`
variable was `None` for every OTHER caller - the completion-length JSON key
was simply never added to the request body anywhere except this one path.

`RequestsOpenRouterClient._payload()` (`luno/adapters/openrouter.py`)
unconditionally wrote that JSON key as the literal string `"max_tokens"`,
regardless of which model was configured - even though the project already
has a working abstraction for exactly this incompatibility,
`luno.config.MAX_TOKENS_PARAM` (defaults to `"max_completion_tokens"`,
already used correctly by `luno/main.py`'s legacy OpenAI-SDK call sites via
`**{config.MAX_TOKENS_PARAM: ...}`). This file tests the fix: `_payload()`
now writes `body[config.MAX_TOKENS_PARAM]` instead of the hardcoded literal,
and `summarize_and_archive_session()`'s legacy raw-openai-SDK duck-typed
branch (dormant in production, but reachable - see that function's own
docstring) now uses the identical `**{config.MAX_TOKENS_PARAM: 150}` pattern
`luno/main.py` already established, instead of its own separate hardcoded
`max_tokens=150` literal.

Scope note: this file tests ONLY the Session Summary code path and the one
shared request-payload method it goes through
(`RequestsOpenRouterClient._payload()`). It deliberately does NOT touch (and
does not test) `luno.adapters.llm_manager`/`luno.adapters.llm.base.
OpenAICompatibleClient` - the SEPARATE multi-provider stack that powers
normal conversational chat (see `luno/adapters/llm/openrouter_provider.py`'s
own module docstring for why these two "OpenRouter" implementations are
deliberately distinct and never call each other). That second stack's
`_payload()` (`luno/adapters/llm/base.py` lines ~344-358) has the textually
identical hardcoded-`"max_tokens"` pattern this sprint fixed here - it is a
plausible SEPARATE latent bug, out of this sprint's explicit scope
(untouched, unfixed, documented as a Sprint 54+ candidate in
`docs/change_impact/memory_session_summary_api_compatibility.md` and
`ARCHITECTURE_GUARD.md`, per this sprint's own "do not fix opportunistically"
rule) - not exercised or asserted on anywhere in this file.

Every test here relies on `tests/conftest.py`'s autouse `isolate_persistent_state`
fixture, which already redirects `config.SESSION_SUMMARIES_FILE` to a
`tmp_path`-derived location before this file's tests even start - no test
here ever touches Vinn's real `config/session_summaries.json`. `luno.memory`'s
OTHER module-level globals used by this feature (`session_log`,
`_session_summaries`) are NOT covered by that fixture (deliberately, per its
own docstring), so every test below saves/restores them itself, mirroring
`tests/test_memory_persistence_hardening.py`'s established
`_save_state()`/`_restore_state()` convention.

Run:
    pytest -q tests/test_memory_session_summary_api_compatibility.py
"""

from __future__ import annotations

import os

import luno.memory as memory_module
from luno import config
from luno.adapters.openrouter import (
    OpenRouterConfig,
    RequestsOpenRouterClient,
)

# ============================================================================
# Local fakes - self-contained, no cross-package import from
# `luno/adapters/tests/test_openrouter_adapter.py` (different test package,
# and this file only needs a small slice of that file's fake surface).
# ============================================================================

class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class _ModelAwareFakeSession:
    """Simulates a real OpenAI-family provider's actual rejection
    behavior: any request body containing a literal `"max_tokens"` key
    is rejected with the EXACT error text from the original bug report
    (proving the OLD hardcoded-`"max_tokens"` behavior would fail
    against a real endpoint); a body containing
    `config.MAX_TOKENS_PARAM`'s configured key succeeds. This is what
    lets `test_before_fix_behavior_would_have_failed_this_exact_error`
    below reproduce the reported error text precisely without a live
    network call, and what lets every "after fix" test below prove a
    genuine success against the same simulated rejection rule - neither
    direction mocks the failure away."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if "max_tokens" in json:
            return _FakeResponse(400, {
                "error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                      "with this model. Use 'max_completion_tokens' instead."}
            })
        return _FakeResponse(200, {
            "model": json.get("model"),
            "choices": [{"message": {"content": "Talked about the weekend trip and dinner plans."},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 42},
        })


class _FakeNewPipelineClient:
    """Duck-types the new-pipeline shape `summarize_and_archive_session()`
    detects via `hasattr(obj, "chat_completion")` - a thin wrapper around
    a real `RequestsOpenRouterClient` so tests exercise the ACTUAL fixed
    `_payload()` code, not a re-implementation of it."""

    def __init__(self, session):
        self._client = RequestsOpenRouterClient(
            OpenRouterConfig(api_key="sk-test"), session=session, sleep_fn=lambda s: None,
        )

    def chat_completion(self, model, messages, max_tokens=None, **kwargs):
        return self._client.chat_completion(model=model, messages=messages, max_tokens=max_tokens, **kwargs)


class _FakeLegacyOpenAIClient:
    """Duck-types the legacy shape (`.chat.completions.create(**kwargs)`,
    no `chat_completion` attribute) - records exactly the kwargs it
    received so tests can assert on the wire parameter NAME, matching
    what the raw `openai` SDK would forward as-is to the API."""

    class _Completions:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            if "max_tokens" in kwargs:
                raise RuntimeError(
                    "Unsupported parameter: 'max_tokens' is not supported with this "
                    "model. Use 'max_completion_tokens' instead."
                )

            class _Msg:
                content = "Talked about the weekend trip and dinner plans."

            class _Choice:
                message = _Msg()

            class _Res:
                choices = [_Choice()]

            return _Res()

    class _Chat:
        def __init__(self, outer):
            self.completions = outer._Completions(outer)

    def __init__(self):
        self.calls = []
        self.chat = self._Chat(self)


def _save_state():
    return list(memory_module.session_log), list(memory_module._session_summaries)


def _restore_state(saved):
    memory_module.session_log[:] = saved[0]
    memory_module._session_summaries[:] = saved[1]


def _seed_session_log():
    memory_module.session_log.clear()
    memory_module._session_summaries.clear()
    memory_module.session_log.extend([
        {"role": "user", "content": "What should I make for dinner this weekend?"},
        {"role": "assistant", "content": "How about a pasta bake, easy and crowd-pleasing."},
    ])


# ============================================================================
# 1. Summary uses the correct completion-token parameter (the project's
#    existing `config.MAX_TOKENS_PARAM` abstraction, default
#    "max_completion_tokens") - new-pipeline branch.
# ============================================================================

def test_summary_request_uses_configured_completion_token_param():
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        summary = memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")
        assert summary is not None, "summary must succeed once the correct param name is sent"
        assert len(session.calls) == 1
        body = session.calls[0]["json"]
        assert config.MAX_TOKENS_PARAM in body, f"expected '{config.MAX_TOKENS_PARAM}' key in body, got {list(body.keys())}"
        assert body[config.MAX_TOKENS_PARAM] == 150
        assert "max_tokens" not in body, "the old, incompatible key must not also be sent"
    finally:
        _restore_state(saved)


# ============================================================================
# 2. The failing `max_tokens` request is no longer generated for the
#    affected model/provider - explicit negative assertion, and proof the
#    behavior is genuinely config-driven (bounded provider-aware), not a
#    second hardcoded literal replacing the first.
# ============================================================================

def test_max_tokens_key_never_generated_with_default_config():
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")
        body = session.calls[0]["json"]
        assert "max_tokens" not in body

        # Config-driven, not a second hardcoded literal: pointing the
        # SAME abstraction at the older key name changes the wire
        # output accordingly (this project's `config.MAX_TOKENS_PARAM`
        # already supports both values - see `luno/config.py`).
    finally:
        _restore_state(saved)


def test_completion_token_param_is_config_driven_not_hardcoded(monkeypatch):
    saved = _save_state()
    try:
        _seed_session_log()
        monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_tokens", raising=False)
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        summary = memory_module.summarize_and_archive_session(client, model="openai/gpt-4")
        # With the OLDER key configured, this simulated provider (which
        # only rejects a literal "max_tokens" key) now fails - proving
        # `_payload()` genuinely reads `config.MAX_TOKENS_PARAM` at
        # request time rather than being hardcoded to either literal.
        assert summary is None
        assert "max_tokens" in session.calls[0]["json"]
    finally:
        _restore_state(saved)


# ============================================================================
# 3. Summary request succeeds end-to-end with a mocked compatible provider.
# ============================================================================

def test_summary_succeeds_end_to_end_and_is_archived():
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        summary = memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")
        assert summary == "Talked about the weekend trip and dinner plans."
        assert len(memory_module._session_summaries) == 1
        assert memory_module._session_summaries[0]["summary"] == summary
        assert memory_module.session_log == [], "session_log must be cleared only on SUCCESS"
    finally:
        _restore_state(saved)


# ============================================================================
# 4. Existing normal LLM/chat path remains unchanged: a caller that never
#    supplies `max_tokens` (exactly how `main_runtime_demo.py`'s
#    `NeedLLMResponse` publisher for ordinary chat behaves - see that
#    file's `PlannerBridgeModule._on_llm_ready...` publish site) still gets
#    a body with NEITHER key at all, byte-for-byte identical to before
#    this sprint's fix.
# ============================================================================

def test_normal_chat_style_call_without_max_tokens_is_unaffected():
    session = _ModelAwareFakeSession()
    cfg = OpenRouterConfig(api_key="sk-test")
    client = RequestsOpenRouterClient(cfg, session=session, sleep_fn=lambda s: None)
    resp = client.chat_completion(model="openai/gpt-5-mini", messages=[{"role": "user", "content": "hi"}])
    assert resp.text == "Talked about the weekend trip and dinner plans."
    body = session.calls[0]["json"]
    assert "max_tokens" not in body
    assert config.MAX_TOKENS_PARAM not in body, (
        "normal chat (no explicit max_tokens, no OPENROUTER_MAX_TOKENS default) must keep "
        "omitting the completion-length key entirely, exactly as before this sprint"
    )


# ============================================================================
# 5. Summary failure remains isolated from main memory state - a genuine
#    provider failure must not raise, must not clear session_log, must not
#    append a summary entry, and must return None (pre-existing contract,
#    unchanged by this sprint).
# ============================================================================

def test_genuine_failure_is_isolated_from_memory_state(capsys):
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()  # rejects literal "max_tokens"
        client = _FakeNewPipelineClient(session)
        # Force the OLD, incompatible wire behavior to prove the ORIGINAL
        # bug report's failure mode stays safely isolated even if a
        # future misconfiguration reintroduces it.
        import luno.config as cfg_module
        old_val = cfg_module.MAX_TOKENS_PARAM
        cfg_module.MAX_TOKENS_PARAM = "max_tokens"
        try:
            summary = memory_module.summarize_and_archive_session(client, model="openai/gpt-4")
        finally:
            cfg_module.MAX_TOKENS_PARAM = old_val

        assert summary is None
        assert memory_module._session_summaries == [], "no summary entry may be appended on failure"
        assert len(memory_module.session_log) == 2, "session_log must NOT be cleared on failure"
        out = capsys.readouterr().out
        assert "[Memory] ✗ Session summary error:" in out
    finally:
        _restore_state(saved)


# ============================================================================
# 6. Existing provider/model configuration is respected - the model this
#    sprint's fix passes through is whatever the caller configured
#    (`register_session_summary_client()`'s `openrouter_adapter.default_model`
#    in production), never silently overridden.
# ============================================================================

def test_configured_model_is_respected_not_overridden():
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        memory_module.summarize_and_archive_session(client, model="anthropic/claude-sonnet-4.5")
        assert session.calls[0]["json"]["model"] == "anthropic/claude-sonnet-4.5"
    finally:
        _restore_state(saved)


# ============================================================================
# 7. No duplicate request is generated - exactly one HTTP call per summary.
# ============================================================================

def test_exactly_one_request_per_summary_call():
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")
        assert len(session.calls) == 1
    finally:
        _restore_state(saved)


# ============================================================================
# 8. No persistent config file is modified other than the (already
#    test-isolated, per `conftest.py`) session summaries file itself.
# ============================================================================

def test_no_persistent_state_file_touched_besides_session_summaries(tmp_path):
    saved = _save_state()
    try:
        _seed_session_log()
        session = _ModelAwareFakeSession()
        client = _FakeNewPipelineClient(session)
        memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")

        assert os.path.exists(config.SESSION_SUMMARIES_FILE), (
            "the isolated (tmp_path-redirected, per conftest.py) session summaries file "
            "must have been written"
        )
        assert str(tmp_path) in config.SESSION_SUMMARIES_FILE, (
            "sanity check that conftest.py's autouse redirect is actually in effect for this test"
        )
        # Nothing else this feature could plausibly touch should exist under
        # the same isolated tmp_path - a real bug here would mean this
        # sprint's fix accidentally started writing memory/long-term-memory
        # state, which is explicitly out of scope.
        other_files = [p for p in tmp_path.glob("*.json") if str(p) != config.SESSION_SUMMARIES_FILE]
        assert other_files == [], f"unexpected persistent-state files written: {other_files}"
    finally:
        _restore_state(saved)


# ============================================================================
# 9. Existing error logging still works when the provider genuinely fails
#    for an UNRELATED reason (never masked by this sprint's fix).
# ============================================================================

def test_error_logging_preserved_for_unrelated_provider_failure(capsys):
    saved = _save_state()
    try:
        _seed_session_log()

        class _AlwaysBoomSession:
            def post(self, *args, **kwargs):
                raise RuntimeError("simulated: upstream connection reset")

        client = _FakeNewPipelineClient(_AlwaysBoomSession())
        summary = memory_module.summarize_and_archive_session(client, model="openai/gpt-5-mini")
        assert summary is None
        out = capsys.readouterr().out
        assert "[Memory] ✗ Session summary error:" in out
        assert "connection reset" in out or "network error" in out
    finally:
        _restore_state(saved)


# ============================================================================
# Legacy duck-typed branch (`.chat.completions.create(...)`) - dormant in
# production today (see module docstring / `summarize_and_archive_session()`'s
# own docstring), but reachable and fixed identically for consistency with
# the SAME existing `config.MAX_TOKENS_PARAM` abstraction `luno/main.py`
# already uses for this exact scenario.
# ============================================================================

def test_legacy_branch_uses_configured_completion_token_param():
    saved = _save_state()
    try:
        _seed_session_log()
        legacy_client = _FakeLegacyOpenAIClient()
        summary = memory_module.summarize_and_archive_session(legacy_client, model="gpt-5-mini")
        assert summary == "Talked about the weekend trip and dinner plans."
        assert len(legacy_client.calls) == 1
        kwargs = legacy_client.calls[0]
        assert config.MAX_TOKENS_PARAM in kwargs
        assert kwargs[config.MAX_TOKENS_PARAM] == 150
        assert "max_tokens" not in kwargs
    finally:
        _restore_state(saved)


def test_legacy_branch_error_isolated_and_logged(capsys):
    saved = _save_state()
    try:
        _seed_session_log()
        legacy_client = _FakeLegacyOpenAIClient()
        import luno.config as cfg_module
        old_val = cfg_module.MAX_TOKENS_PARAM
        cfg_module.MAX_TOKENS_PARAM = "max_tokens"  # simulate the ORIGINAL hardcoded bug
        try:
            summary = memory_module.summarize_and_archive_session(legacy_client, model="gpt-5-mini")
        finally:
            cfg_module.MAX_TOKENS_PARAM = old_val
        assert summary is None
        assert memory_module._session_summaries == []
        assert len(memory_module.session_log) == 2
        out = capsys.readouterr().out
        assert "Unsupported parameter: 'max_tokens'" in out
    finally:
        _restore_state(saved)


# ============================================================================
# Before/after reproduction of the EXACT reported bug - not live, but a
# realistic simulation of the real provider's own rejection rule (see
# `_ModelAwareFakeSession`'s docstring). Confirms this sprint's fix is what
# changed the outcome, not a coincidence of the test double.
# ============================================================================

def test_before_fix_behavior_would_have_reproduced_the_exact_reported_error(monkeypatch, capsys):
    """Simulates the pre-Sprint-53 wire behavior (the hardcoded literal
    `"max_tokens"` key `_payload()` used to always send) by pointing
    `config.MAX_TOKENS_PARAM` at that same literal - NOT by re-injecting
    the old code. Proves the exact error string from the bug report
    reproduces under that condition, and that the current (fixed) default
    configuration does not reproduce it."""
    saved = _save_state()
    try:
        _seed_session_log()

        # BEFORE (simulated): old hardcoded key name.
        monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_tokens", raising=False)
        session_before = _ModelAwareFakeSession()
        client_before = _FakeNewPipelineClient(session_before)
        summary_before = memory_module.summarize_and_archive_session(client_before, model="openai/gpt-5-mini")
        assert summary_before is None
        out_before = capsys.readouterr().out
        assert ("[Memory] ✗ Session summary error: Unsupported parameter: 'max_tokens' "
                "is not supported with this model. Use 'max_completion_tokens' instead.") in out_before

        _seed_session_log()  # reset for the AFTER half of this test

        # AFTER (actual current code path): correct key name.
        monkeypatch.setattr(config, "MAX_TOKENS_PARAM", "max_completion_tokens", raising=False)
        session_after = _ModelAwareFakeSession()
        client_after = _FakeNewPipelineClient(session_after)
        summary_after = memory_module.summarize_and_archive_session(client_after, model="openai/gpt-5-mini")
        assert summary_after == "Talked about the weekend trip and dinner plans."
    finally:
        _restore_state(saved)
