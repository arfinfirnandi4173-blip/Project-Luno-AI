# Change Impact Analysis: Memory Dashboard & Observability

**Written BEFORE implementation**, per this sprint's own audit-first
requirement (Phase 1). Updated after implementation only if something
material changed from what's described here.

## Baseline (verified directly against the repository, not assumed)

- `luno/` fast suite: 806 passed / 808 total (2 known-flaky Barge-in
  tests: `test_confirm_mode_interrupt_then_no_resumes`,
  `test_stress_many_ordinary_utterances_then_one_real_interrupt` -
  unchanged root cause, re-confirmed).
- Named 14-file memory/relationship/emotion/personality/runtime batch:
  557 passed / 557.
- Dashboard-related suite (`tests/test_dashboard.py` +
  `test_llm_dashboard.py` + `test_routing_dashboard.py` +
  `test_verification_dashboard.py`): 63 passed (1 benign
  `PytestUnhandledThreadExceptionWarning` from an SSE test hitting a
  real outbound network timeout unrelated to dashboard code - not a
  failure, not new).
- `tests/test_production_launcher.py`: 23 passed / 24 (1 known
  environment-specific failure:
  `test_07_health_checks_all_pass_in_default_mock_configuration`).
- All numbers match the immediately-preceding Memory Lifecycle &
  Maintenance sprint's own final regression exactly - confirmed by
  actually running the suites, not by trusting the prior report.
- 10 persistent state files (see Phase 16 list) SHA256+mtime hashed:
  byte-identical to the values recorded at the end of the Memory
  Lifecycle & Maintenance sprint's own final sweep, `episodic_memory.json`
  still absent. No drift between that sprint's end and this sprint's
  start.

## Architecture Audit

### The dashboard is NOT a framework of our choosing - it already exists

`luno/dashboard/` (package) + `luno/bootstrap/dashboard.py` (the
`register_dashboard()` construction point `main.py` calls) is a
complete, already-running Sprint-7-era system:

- **`server.py`** - `DashboardServer`, a stdlib
  `http.server.ThreadingHTTPServer`-based HTTP + Server-Sent-Events
  transport (deliberately zero new hard dependencies - no Flask/
  FastAPI/aiohttp/uvicorn anywhere in this project). Routes are a flat
  `if/elif path == "/api/..."` chain in `_dispatch_get()` (GET) and a
  `_run_control()` dispatch table (POST), both string-exact-match.
- **`collectors.py`** (801 lines before this sprint) - pure, read-only
  "Runtime/module state -> JSON-safe dict" functions, one per dashboard
  view (status, modules, adapters, llm, conversation, planner,
  tool_manager, verification, vision_memory, vision, goals,
  memory_retrieval, routing, context, health, configuration,
  statistics). Every function's own docstring states it reads the same
  already-public accessor the terminal `ProductionConsole` uses - never
  a second, independently-derived view.
- **`controls.py`** (422 lines before this sprint) - every dashboard
  button, each a thin call-through to an existing public method or an
  `Event` published onto the same Event Bus a real spoken
  interrupt/wake word would use. Returns
  `{"ok": bool, "message": str, ...}`.
- **`static/index.html`** (1472 lines) - single-file HTML/CSS/JS,
  vanilla JS (no build step, no framework), a sidebar of
  `data-panel`-tagged buttons grouped into collapsible categories, one
  `<div id="panel-X" class="panel">` per view, an `onPanelShown(name)`
  loader dispatch table, a 3-second `setInterval` poll of whichever
  panel is currently active, and a small helper set (`api()` for
  fetch+JSON, `esc()`, `badge()`, `card()`).
- Already has a "Memory Retrieval" panel (`/api/memory_retrieval`,
  `collect_memory_retrieval()`) - a DIFFERENT thing from what this
  sprint builds: it's a debug view of `MemoryRetriever.
  retrieve_memories(query_text)` (ranked, cross-source retrieval
  simulation for a hypothetical query), not a browsing/CRUD/maintenance
  surface over the manual-memory store. This sprint adds a NEW,
  separate "Memory" panel; the existing "Memory Retrieval" panel is
  untouched.
- `luno/dashboard/*.py` currently has ZERO references to `luno.memory`
  (confirmed by grep) - this sprint is a genuinely new wiring point,
  purely additive to this package.

### Confirmed: dashboard already reads/writes ONLY through public APIs

Every existing collector/control calls a public method
(`runtime.status()`, `session_manager.force_sleep()`,
`adapter_manager.restart(name)`, ...) or publishes an `Event`. Nothing
in `luno/dashboard/` opens a JSON file directly today. This sprint's
own memory functions must, and will, follow the identical rule -
`luno/memory.py`'s public functions only, never `luno.memory._memories`
or `config.LONG_TERM_MEMORY_FILE` touched directly from dashboard code.

### `luno/memory.py` public surface actually available (exhaustively read this session)

`list_memories()`, `get_memory(id)`, `update_memory(id, text, reason=)`,
`delete_memory_by_id(id)`, `search_memories(query, limit=5)`,
`list_conflicts()`, `compute_lifecycle(entry, now=)`,
`mark_last_memory_important()` (targets the MOST-RECENTLY-TOUCHED
memory, not an arbitrary id), `forget_last_memory()` (same
last-touched targeting), `archive_memory_by_id(id)` (already
id-targeted, already refuses on a protected entry),
`unarchive_last_memory()` (last-touched targeting again),
`record_memory_usage()` (conversational-retrieval-only, out of scope
here), `analyze_memory_maintenance(now=)` (read-only planner),
`apply_maintenance_plan(plan)` (the sole mutating executor),
`preview_maintenance_text()`, `memory_health_report()`,
`format_memory_health_report()`.

**Gap found:** three of the six "safe operations" Phase 7 asks for
(Archive, Unarchive, Delete, Update, Mark important, Forget) have no
ID-TARGETED public entry point today - `mark_last_memory_important()`,
`forget_last_memory()`, and `unarchive_last_memory()` all resolve their
target via `_most_recently_touched_memory()` (a "the thing I just
talked about" heuristic that makes sense for a spoken command with no
other way to say "this one", but is meaningless for a dashboard where
the user has just clicked a specific row with a real `id`). Two of the
six (Update, Delete) and the existing conflict/maintenance operations
already ARE id-targeted (`update_memory`, `delete_memory_by_id`,
`archive_memory_by_id`) and need no new code.

**Resolution (documented here, not discovered mid-implementation):**
add exactly two new thin, additive, id-targeted PUBLIC functions to
`luno/memory.py`, following the EXACT precedent `archive_memory_by_id()`
already set as the id-targeted counterpart to `unarchive_last_memory()`'s
last-touched sibling:

- `mark_memory_important_by_id(memory_id)` - same three-field mutation
  `mark_last_memory_important()` already performs
  (`importance=4`, `source="user_explicit"`, `updated_at` stamped),
  just looked up by `id` via the same pattern `get_memory()`/
  `update_memory()` already use, instead of
  `_most_recently_touched_memory()`. Zero new business logic - it is
  the identical mutation, differently targeted.
- `unarchive_memory_by_id(memory_id)` - same two-field mutation
  `unarchive_last_memory()` already performs (`archived_by_maintenance`
  cleared, `archived_at` popped), id-targeted instead of last-touched.

A third tiny addition is needed for the Protected Memory badge (Phase
10): `is_memory_protected(memory_id)` - a one-line public wrapper
around the EXISTING private `_is_protected_from_archival()` (already
used by `apply_maintenance_plan()`/`archive_memory_by_id()`/the
planner). Without this, the dashboard would have to either duplicate
the protection rule (importance>=4 OR unresolved ambiguous conflict) a
second time, or reach into a private (`_`-prefixed) internal - both
forbidden by this sprint's own rules. Delegating to the existing
private function from one new public one-line wrapper avoids both.

**"Forget" is not separately implemented.** `forget_last_memory()`'s
only distinguishing behavior versus `delete_memory_by_id()` is target
selection (last-touched vs. explicit id) - the dashboard already
supplies an explicit id from the clicked row, so "Forget" and "Delete"
would be the exact same call. Adding a second identically-behaved
control under a different label would be a cosmetic duplication, not a
real feature; the dashboard's "Delete" button covers both.

**"Update" (importance) is intentionally NOT a free-form 0-4 slider.**
No production surface anywhere in this codebase (voice command or
otherwise) lets a user set importance to an arbitrary specific level -
the only existing explicit-importance operation is "mark as
important/permanent" (-> 4, `mark_last_memory_important()`). Adding a
novel "set importance to exactly N" control would be new business
logic this sprint's own rules forbid inventing. The dashboard exposes
exactly what production already supports: "Mark important" (id-targeted,
per above) plus ordinary text "Update" (`update_memory()`, unchanged).

### Verified Facts / Episodic Memory boundary - confirmed structurally isolated, will remain so

`VerifiedFactStore` (`luno/memory_guard.py`) facts are never
represented as `_memories` entries - confirmed again this session (grep
for `_memories`/`build_memory_prompt`/`memory.add_memory` inside
`memory_guard.py`: zero matches). `EpisodicMemoryStore`
(`luno/episodic_memory.py`) is a separate store/file
(`config/episodic_memory.json`), never touches `luno.memory`. This
sprint's dashboard functions only ever call `luno.memory.*` public
functions - structurally unable to reach either store. Per Phase 10,
if a Verified Facts panel is added to the dashboard UI it will be
READ-ONLY and clearly separate (no delete/update button wired to the
manual-memory API against a Verified Fact) - **decision: this sprint
does NOT add a Verified Facts dashboard panel at all** (out of scope -
the brief's Phase 10 rule is a prohibition/boundary, not a request for
a new panel; a read-only Verified Facts view is not among Phases 1-14's
concrete deliverables). This is verified instead by a dedicated
isolation test (Scenario R) proving the memory dashboard API surface
has no code path that can read or mutate `config/verified_facts.json`.
Same reasoning and the same dedicated test (Scenario S) for Episodic
Memory - no Episodic Memory panel, isolation proven by test instead.

### Test infrastructure already sufficient

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture
already monkeypatches `LONG_TERM_MEMORY_FILE` (and 6 other persistent
paths) on `luno.config` for EVERY test under `tests/`, and
`test_dashboard.py`'s own `_build_stack()` calls
`register_all_modules()`/`register_all_adapters()` - the same
real-bootstrap path every other dashboard test already uses, which
means a new dashboard-memory test built the same way automatically
inherits the exact same isolation, with no new fixture needed. No new
persistent file, no new `config.*_FILE` constant, so no new isolation
target is required either.

## Planned Implementation

### `luno/memory.py` (extended again)

Three small additive public functions appended after the existing
`unarchive_last_memory()` (see "Gap found" above):
`mark_memory_important_by_id()`, `unarchive_memory_by_id()`,
`is_memory_protected()`. No existing function's signature or behavior
changes.

### `luno/dashboard/collectors.py` (extended, new "# Memory Dashboard &
Observability" section, same one-function-per-view convention every
existing section already follows)

- `collect_memory_overview()` - total/active/stale/archived (from
  `memory_health_report()`'s `lifecycle`), `importance` histogram (from
  the same report), `categories`/`sources` breakdown (computed by
  counting `list_memories()`'s own `category`/`source` fields - a
  simple tally, not new classification logic), `conflicts`/
  `duplicates`/`review_required`/`protected` (from the same health
  report, passed through, never recomputed), `obsolete` (derived by
  counting `analyze_memory_maintenance()`'s own plan entries whose
  `action == "archive"` and `reason` mentions obsolete/temporary
  wording - distinguishing "obsolete-wording archive candidates" from
  "stale-by-age archive candidates" using the planner's own
  already-public `reason` text, not a second obsolete-detection rule).
- `collect_memory_list(lifecycle=, importance=, category=, source=,
  conflict_status=, search=, limit=, offset=)` - candidate set is
  `search_memories(search, limit=<generous bound>)` when `search` is
  non-empty (reusing the existing tokenizer/ranking exactly, per Phase
  5's explicit instruction), else `list_memories()`; every remaining
  filter is applied ON TOP by simple attribute matching (lifecycle via
  `compute_lifecycle()`, importance via the stored/defaulted value,
  category/source via exact match, conflict_status per the honest
  schema-grounded mapping below); sorted deterministically
  (`updated_at`/`created_at` descending) when not already
  relevance-sorted by search; `limit` clamped to `[1, 200]` (default
  50), `offset` clamped to `>= 0` - NEVER unbounded, satisfying Phase 2
  and Phase 12's explicit requirement. Each returned entry gets one
  computed field added (`lifecycle`) that raw entries don't otherwise
  carry.
- **`conflict_status` filter - schema-honest, not the brief's literal
  6-option list verbatim.** The `_memories` schema only ever persists
  one live `conflict_status` value on an entry:
  `"ambiguous_conflict"` (confirmed by grep - `"correction"`/
  `"temporal_change"`/`"refinement"` are transient classifications
  `_classify_conflict()` returns to DECIDE how to merge/tag at
  save-time, never persisted as a standing status field on an entry).
  Per the brief's own Phase 6 instruction ("Jangan hardcode pilihan
  yang tidak benar-benar didukung schema"), the filter instead supports
  what the schema actually records: `"ambiguous_conflict"` / `"none"`
  (live status) and, separately, `"correction"` / `"temporal_change"` /
  `"refinement"` as a filter over whether the entry's `history[]`
  contains an entry with that `reason` (real, already-persisted data -
  `update_memory()`'s own `reason` stamping) - i.e. "this memory was
  once corrected/superseded/refined", not "this memory currently IS a
  correction". The UI labels this honestly rather than implying a live
  status that doesn't exist.
- `collect_memory_detail(memory_id)` - `get_memory(id)` plus computed
  `lifecycle`, `is_protected` (new `is_memory_protected()`), and, when
  `conflict_status == "ambiguous_conflict"`, the sibling entries from
  the same `conflict_group` (via `list_conflicts()`, filtered to the
  matching group) so the UI can show BOTH sides without a second
  lookup round-trip. `history` is passed through verbatim (already
  bounded to 5, already ordered oldest-first by
  `update_memory()`/`apply_maintenance_plan()`'s own append-only
  convention - never reordered or reinterpreted here).
- `collect_memory_health()` - `memory_health_report()`, unchanged
  passthrough (Phase 2's own "Jangan menghitung ulang health logic").
- `collect_memory_maintenance_preview()` -
  `{"plan": analyze_memory_maintenance(), "text":
  preview_maintenance_text()}`, unchanged passthrough (Phase 2/9's own
  "Jangan membuat planner kedua").
- `collect_memory_conflicts()` - `list_conflicts()`, unchanged
  passthrough, reshaped only by adding each entry's computed
  `lifecycle` (for the "Needs Review" panel's display, Phase 8).

None of these call `record_memory_usage()` - Phase 5's explicit
"Dashboard browsing/search TIDAK boleh dianggap sebagai genuine
retrieval usage" is satisfied structurally: `record_memory_usage()` is
never imported into `luno/dashboard/` at all, so there is no code path
by which a dashboard read could increment `retrieval_count`.

### `luno/dashboard/controls.py` (extended, new "# Memory Dashboard &
Observability" section, same thin-call-through convention)

- `memory_archive(memory_id)` -> `archive_memory_by_id(id)`, maps
  `"archived"/"protected"/"not_found"` to an honest `_ok`/`_fail`.
- `memory_unarchive(memory_id)` -> new `unarchive_memory_by_id(id)`.
- `memory_delete(memory_id, confirm)` -> **requires `confirm is True`**
  (strict identity check, not merely truthy - a client bug or stray
  `"false"` string must not accidentally pass) before calling
  `delete_memory_by_id(id)`; returns `_fail("confirmation required")`
  otherwise. This is the Phase 13 "never trust `confirmed=true` from
  the frontend as the ONLY boundary" requirement read literally: the
  SERVER independently re-validates both that confirmation was given
  AND that the target id actually exists and is deletable (the
  underlying function's own `None`-on-not-found return), rather than
  trusting the frontend's confirm click alone to mean "this is a valid
  operation."
- `memory_update(memory_id, text)` -> `update_memory(id, text,
  reason="dashboard_edit")` (the `reason` string makes a
  dashboard-originated edit distinguishable in `history[]` from a
  conversational one, for free, via the mechanism `update_memory()`
  already supports).
- `memory_mark_important(memory_id)` -> new
  `mark_memory_important_by_id(id)`.
- `memory_apply_maintenance(confirm)` -> requires `confirm is True`;
  ALWAYS recomputes a fresh `analyze_memory_maintenance()` plan at
  apply-time server-side (never trusts a plan blob the client could
  have echoed back, stale or tampered) and passes that fresh plan
  straight to `apply_maintenance_plan()` - satisfying both Phase 9's
  "Preview first, don't apply directly" flow AND Phase 13's "server
  determines validity" rule in one design choice.

No control for "Forget" (subsumed by Delete, see above) and none for a
free-form importance level (subsumed by Mark Important, see above) -
both documented, not silent gaps.

### `luno/dashboard/server.py` (extended)

Six new exact-match GET routes
(`/api/memory/overview|list|health|maintenance/preview|conflicts`) in
`_dispatch_get()`, plus one `startswith("/api/memory/")` catch-all
placed AFTER those six for `/api/memory/<id>` (Python's `elif` chain
means the six specific matches are always checked first - no ordering
bug possible). Six new exact-match POST routes under
`/api/memory/controls/*` in `_run_control()`. No existing route's
behavior changes.

### `luno/dashboard/static/index.html` (extended)

One new sidebar entry (`data-panel="memory"`) in the existing
"Perception & Memory" nav group, one new `<div id="panel-memory">`
following the exact same structure every other panel already uses
(toolbar with filters/search, a results table, a detail
modal/side-panel), registered in the existing `onPanelShown()` loader
table so the existing 3-second poll picks it up like every other
panel - EXCEPT the poll is skipped while a memory detail modal is open
(new small guard flag) so an in-progress edit/read isn't yanked out
from under the user, and it is skipped entirely for the maintenance
preview/apply flow (Phase 12's "jangan menjalankan maintenance planner
pada setiap page refresh" - the preview is fetched once when the
Maintenance tab/section is opened or the user clicks "Preview" again,
never on the ambient 3-second poll).

## Compatibility

Every new collector/control is purely additive - no existing endpoint,
route, panel, or JS function is modified. Old `_memories` entries
(schema v1, missing `importance`/`history`/`conflict_status`/
`retrieval_count`/etc.) are handled by the SAME accessor functions
(`_get_importance`, `compute_lifecycle`, `_get_retrieval_count`) every
prior sprint already made backward-compatible - the dashboard inherits
that for free by only ever calling those functions, never reading raw
dict keys itself for anything that has a public accessor.

## Risks Identified Before Implementation

1. **Path-param routing inside a flat `if/elif` string-match
   dispatcher.** Resolved by placing the `/api/memory/<id>` catch-all
   strictly after all more-specific `/api/memory/...` exact matches in
   the elif chain (see above) - verified by a dedicated test that
   `/api/memory/overview` never gets misrouted to the detail handler
   with `memory_id="overview"`.
2. **Dashboard already bound to `0.0.0.0` in this deployment's actual
   `.env`** (`DASHBOARD_HOST=0.0.0.0`, `DASHBOARD_PORT=8765` - a prior,
   separate sprint's own deliberate change, predating this one; the
   `LauncherConfig` default is still `127.0.0.1`). This sprint adds
   real DELETE/archive/update capability to an HTTP surface that has
   NO authentication anywhere in this codebase (confirmed by audit -
   no auth pattern exists to extend, and Phase 13 explicitly forbids
   inventing a large new auth system). This is a genuine, pre-existing
   risk this sprint does not fully close - documented honestly in the
   final report's Security section, mitigated only by (a) server-side
   confirm validation on every destructive control (never trusting the
   frontend alone), (b) archive-never-deletes and delete-never-touches-
   Verified-Facts/Episodic-Memory, and (c) explicit documentation that
   this deployment is local-network-exposed and should be treated as
   trusted-network-only, not internet-facing.
3. **`search_memories()` has no lifecycle/category/source/conflict
   filtering of its own** - resolved by layering the dashboard's
   additional filters on top of its results in the collector, never
   inside `search_memories()` itself (which stays untouched, still used
   identically by its other existing callers).
4. **Reusing `analyze_memory_maintenance()`'s `reason` text to derive
   the overview's `obsolete` count** is a soft coupling to that
   function's current wording ("contains temporary/obsolete wording and
   low importance" vs. "stale and rarely or never retrieved") rather
   than a structured field - accepted for this sprint (no structured
   `reason_code` exists yet to key on instead) and covered by a test
   that asserts the count is right for a known obsolete-worded fixture,
   so a future wording change to that string would be caught by a
   failing test rather than silently drifting.

## Post-implementation update

Implementation matched this plan closely. Three deviations worth
recording honestly:

1. **Two extra thin public wrappers were needed beyond the three
   planned** (`get_memory_importance()`, `get_memory_retrieval_count()`)
   - the planned three (`mark_memory_important_by_id()`,
   `unarchive_memory_by_id()`, `is_memory_protected()`) covered the
   mutation gaps, but `collect_memory_list()`/`collect_memory_detail()`
   still needed a public way to read an entry's effective importance/
   usage count (for filtering and display) without either reaching into
   the private `_get_importance()`/`_get_retrieval_count()` or
   re-deriving their backward-compatible defaulting logic a second
   time. Both are one-line delegations to the existing private
   functions - zero new business logic, same spirit as the other three.
2. **`collect_memory_list()`'s recency sort needed the same
   `(timestamp, list-position)` tie-break `_most_recently_touched_memory()`
   already uses** - caught by pre-formal-suite smoke testing (this
   sprint's own established discipline): two memories touched within the
   same second (`_now_iso()`'s 1-second resolution) tied under a plain
   `sort(reverse=True)`, and Python's sort stability meant the tie
   silently resolved to whichever was FIRST in `_memories`, not
   necessarily the one actually touched last. Fixed before any test
   suite run by mirroring the existing production tie-break rule
   exactly, rather than inventing a new one.
3. **A one-time, self-caused persistent-state drift during smoke
   testing, not a regression.** An ad-hoc smoke-test script (run
   directly via the shell, predating the formal `tests/
   test_memory_dashboard.py` suite, never part of the test suite or any
   CI path) built a full `register_all_modules()`/`register_all_adapters()`
   stack OUTSIDE pytest's autouse `isolate_persistent_state` fixture and
   only manually redirected `LONG_TERM_MEMORY_FILE` - Vision Memory's
   own path was never isolated in that ad-hoc script, so
   `config/vision_memory.sqlite3-shm`'s bytes changed (SQLite's WAL-mode
   shared-memory bookkeeping file, rewritten by any connection open,
   read or write). The actual data files -
   `config/vision_memory.sqlite3` and `config/vision_memory.sqlite3-wal`
   (where committed content actually lives) - are BYTE-IDENTICAL to the
   pre-sprint baseline, so no real data was affected. Confirmed this is
   NOT a defect in this sprint's own isolation (which the ad-hoc script
   never used) by immediately re-running the full pytest suite (real
   `isolate_persistent_state`-covered tests) with a hash of
   `vision_memory.sqlite3-shm` taken before and after: zero drift.
   Recorded here rather than silently reset, per this sprint's own
   "if there's a change, STOP and investigate" rule - investigated,
   root-caused to a diagnostic script outside the test suite, not to
   production code or to this sprint's test isolation.

See the final sprint report and `ARCHITECTURE_GUARD.md`'s "Memory
Dashboard & Observability" subsection for the as-built description.
