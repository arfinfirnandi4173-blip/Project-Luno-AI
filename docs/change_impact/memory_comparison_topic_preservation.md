# Context-Aware Comparison Topic Preservation

## Root cause

`is_pure_reference_followup(text)` decided REPLACE-vs-PRESERVE for the
active-topic snapshot purely from `classify_reference_type(text)`'s
category. `"comparison"` was never in `_PURE_REFERENCE_TYPES`, so ANY
comparison-shaped follow-up (e.g. "Kalau mikrofonnya gimana?") replaced
the active topic snapshot, even when its residual term ("mikrofon") was
already part of the current, richer active topic (ESP32/INMP441/voice
assistant). Found and proven via the prior read-only audit - see
`docs/change_impact/memory_e2e_audit.md`.

## Fix

Additive only. `is_pure_reference_followup()` gained an optional
`active_topic_terms` parameter (default `None`, fully backward
compatible). When `classify_reference_type()` returns `"comparison"`, the
function now extracts the comparison's residual terms (reusing the same
regex/stopword-filtering style as the existing branch in
`classify_reference_type()`, plus a small additional filler set for
Indonesian words like "bagaimana"/"tadi"/"soal") and checks for substring
overlap against `active_topic_terms`. Overlap -> treat as pure reference
(preserve). No overlap, or no `active_topic_terms` supplied -> unchanged
replace behavior.

Call site: `main_runtime_demo.py::PlannerBridgeModule._on_assistant_response()`
now fetches `existing_snapshot` before classifying and passes
`existing_snapshot.terms` in, so the SAME `is_followup` value continues to
drive both `update_active_topic()` and `update_topic_history()`, unchanged
downstream.

## Exact production files/functions changed

- `luno/memory.py`: added `_COMPARISON_PRESERVATION_EXTRA_FILLER`,
  `_comparison_residual_terms()`, `_residual_overlaps_active_topic()`;
  extended `is_pure_reference_followup(text, active_topic_terms=None)`.
- `main_runtime_demo.py`: reordered `_on_assistant_response()` to compute
  `existing_snapshot` before the `is_followup` classification call.

Nothing else touched. `classify_reference_type()` itself, ranking, budget,
rendering, retrieval architecture, TTS, streaming, and persistence formats
are byte-for-byte unchanged.

## Before / after

| Scenario | Before | After |
|---|---|---|
| "Kalau mikrofonnya gimana?" against ESP32/INMP441/mikrofon active topic | replaces (bug) | preserves |
| "Kalau INMP441-nya gimana?" against same topic | replaces (bug) | preserves |
| "Kalau Bluetooth-nya gimana?" against same topic | replaces (correct) | replaces (unchanged) |
| "Kalau caranya gimana?" (no meaningful residual) | replaces (correct) | replaces (unchanged) |

## Known limitation

The overlap check is purely lexical (substring match on tokens already
present in `active_topic_terms`, which itself is built from user text +
assistant reply text via the existing `extract_topic_terms_from_turn()`).
It does not semantically bridge a word like "mikrofon" to "ESP32/INMP441"
unless that literal vocabulary was already present in the topic snapshot
(from either the user's own words or a prior assistant reply mentioning
it). This is by design - no embeddings or LLM judge were introduced - but
means a comparison whose residual term never appeared anywhere in the
conversation will still (correctly, conservatively) replace rather than
preserve.

See `ARCHITECTURE_GUARD.md` §35 and
`docs/testing/regression_baseline.md` (2026-08-13 entries) for full test
count and regression results.
