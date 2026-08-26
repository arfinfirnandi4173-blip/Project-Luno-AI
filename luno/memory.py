"""
Manajemen memory Luno — ada 2 lapis yang SENGAJA dipisah:

1. SHORT-TERM (`conversation_history`): riwayat beberapa giliran percakapan
   terakhir, cuma hidup di RAM. Hilang total tiap kali proses Luno di-restart.
   Diatur lewat MEMORY_TURNS di .env — ini yang bikin Luno "nyambung" dalam
   satu sesi ngobrol (tau konteks kalimat sebelumnya).

2. LONG-TERM (`long_term_memory.json`): fakta/preferensi yang secara EKSPLISIT
   diminta user untuk diingat (mis. "inget ya, aku alergi kacang"). Disimpan ke
   file JSON supaya TETAP ADA walau Luno di-restart atau komputer dimatikan —
   ini yang bikin Luno kerasa "kenal" penggunanya dari waktu ke waktu, bukan
   cuma dalam 1 sesi. Beda total dari short-term: harus diminta jelas, tidak
   otomatis menyimpan tiap obrolan (supaya tidak penuh sampah/noise).
"""

import os
import re
import json
import shutil
import tempfile
import uuid
from collections import deque
from datetime import datetime, timezone

from . import config
from . import persistence

# ─────────────────────────────────────────────
#  SHORT-TERM MEMORY (riwayat percakapan, hilang saat restart)
# ─────────────────────────────────────────────

conversation_history = deque(maxlen=config.MEMORY_TURNS * 2)

# SEMUA pesan sesi berjalan ini, TIDAK di-trim (beda dari conversation_history di atas
# yang dibatasi MEMORY_TURNS). Dipakai KHUSUS buat bahan ringkasan sesi — lihat
# summarize_and_archive_session() di bagian bawah file ini.
session_log = []


def get_history():
    """List message {'role', 'content'} siap disisipkan ke messages[] buat GPT."""
    return list(conversation_history)


def remember_turn(user_text, reply):
    conversation_history.append({"role": "user", "content": user_text})
    conversation_history.append({"role": "assistant", "content": reply})
    session_log.append({"role": "user", "content": user_text})
    session_log.append({"role": "assistant", "content": reply})


def clear_short_term():
    conversation_history.clear()


# ─────────────────────────────────────────────
#  LONG-TERM MEMORY (fakta permanen, tersimpan di file JSON)
# ─────────────────────────────────────────────
#
# Manual Memory Management sprint: this IS the "manual memory" the sprint
# brief asks for - this module's own docstring already defines long-term
# memory as "fakta/preferensi yang secara EKSPLISIT diminta user untuk
# diingat", which is word-for-word the sprint's own definition of Manual
# Memory. The audit for this sprint found no separate system to build -
# extending this one (additively: new optional fields, new functions,
# nothing existing removed/renamed/changed in shape) is what the sprint's
# own "prefer extending existing long-term memory before creating a
# dedicated store" instruction calls for. `detect_remember_command()`/
# `detect_forget_fact_command()`/`is_recall_command()` below already
# implement explicit-intent save/forget/recall; this sprint adds explicit
# UPDATE and delete-by-id/topic on top, a lightweight deterministic
# `category` classification, and a `MemorySource` for the existing
# `MemoryRetriever` (see `make_manual_memory_source()` near the bottom of
# this file) so relevance-scored recall ("cari memory tentang PC-ku") goes
# through the SAME retrieval pipeline `episodic_memory.py`'s
# `make_episodic_experience_source()` already uses, rather than a second
# retrieval mechanism.

#: Bumped only if the on-disk entry SHAPE ever changes incompatibly - same
#: convention as `episodic_memory.EPISODIC_SCHEMA_VERSION`. Existing
#: entries (from before this sprint) simply lack this key entirely - see
#: `_classify_memory_category`/`list_memories()`, both tolerant of that.
#: Memory Intelligence & Importance Engine sprint: bumped 1 -> 2 for the
#: new `importance`/`history` fields (matches the sprint brief's own
#: illustrative JSON, which explicitly shows `"schema_version": 2`).
#: Memory Learning & Feedback Loop sprint: bumped 2 -> 3 for the new
#: `usefulness_score`/`positive_feedback_count`/`negative_feedback_count`
#: fields (see that section near `record_memory_usage()` below). Nothing
#: in this module GATES or rejects entries by this number's value (unlike
#: `episodic_memory.py`, which strictly validates it) - the bump is
#: purely informational, so old v1/v2 entries keep loading exactly as
#: before, no migration needed.
#: Memory Evaluation & Self-Calibration sprint: bumped 3 -> 4 for the new
#: `evaluation_score`/`last_evaluated_at`/`retrieval_success_count`/
#: `retrieval_miss_count`/`feedback_event_count`/`correction_count`/
#: `conflict_event_count` fields (see the "MEMORY EVALUATION &
#: SELF-CALIBRATION" section near the end of this file). Same purely
#: informational bump, same no-migration-needed guarantee - a v1/v2/v3
#: entry simply has none of these fields yet and every accessor below
#: defaults safely.
MANUAL_MEMORY_SCHEMA_VERSION = 4

#: Small, controlled set - same reasoning as `episodic_memory.ExperienceCategory`:
#: a freeform LLM-writable category would reopen the "unrestricted authority
#: to persist arbitrary facts" problem. Each has a concrete consumer: shown
#: in `list_memories()`/dashboard-style output and included in the
#: `[MANUAL MEMORY]` retrieval label so the LLM sees what KIND of fact it's
#: looking at, not just raw text.
MANUAL_MEMORY_CATEGORIES = (
    "preference", "personal_fact", "technical_fact",
    "instruction", "project_context", "other",
)

_CATEGORY_KEYWORDS = {
    "preference": (
        "suka", "favorit", "kesukaan", "like", "love", "prefer", "favorite", "favourite",
    ),
    "technical_fact": (
        "gpu", "cpu", "ram", "rtx", "gtx", "ssd", "hdd", "motherboard", "processor",
        "windows", "linux", "macos", "ubuntu", "debian", "fedora", "ip", "port",
        "server", "router", "spek", "spec", "home assistant", "esp32", "sensor",
    ),
    "instruction": (
        "selalu", "jangan pernah", "tolong selalu", "always", "never", "must", "harus",
    ),
    "project_context": (
        "project", "proyek", "kerjaan", "pekerjaan", "repo", "repository",
    ),
}


def _classify_memory_category(text: str) -> str:
    """Deterministic, keyword-based - same style as
    `episodic_memory._classify_category`, deliberately NOT an LLM call (a
    manual memory's category must be as auditable/reproducible as its
    content). Falls back to "other" rather than guessing - a wrong-but-
    confident category would be worse than an honest "other"."""
    lowered = (text or "").lower()
    for category in ("technical_fact", "instruction", "project_context", "preference"):
        if any(kw in lowered for kw in _CATEGORY_KEYWORDS[category]):
            return category
    return "other"


def classify_query_context_category(text):
    """Memory Decision Quality & Adaptive Retrieval sprint - public
    wrapper reusing the SAME deterministic classifier every manual
    memory's own `category` field is already computed from
    (`_classify_memory_category()`), applied here to the CURRENT QUERY
    text instead of a memory's stored text. This is deliberately NOT a
    second tokenizer/classifier - it is the existing, auditable,
    keyword-based category taxonomy (`MANUAL_MEMORY_CATEGORIES`) reused
    as the one deterministic "what kind of context is this turn" signal
    available in this codebase, satisfying the sprint's own "prefer
    deterministic features already present in the repository; no LLM
    classification, no embeddings, no second tokenizer" constraint.
    Always returns one of `MANUAL_MEMORY_CATEGORIES` (never `None`/empty
    - falls back to "other" exactly like `_classify_memory_category()`
    itself), so it is always a valid key into a memory's
    `context_evidence` bucket."""
    return _classify_memory_category(text)


# ─────────────────────────────────────────────
#  Memory Intelligence & Importance Engine sprint - deterministic
#  importance (0-4) + lifecycle (active/stale/archived). Deliberately no
#  LLM call, no embeddings, no vector store (Step 19's "do not
#  overengineer" instruction) - same "small, explicit, per-signal rules,
#  easy to reason about and tune by hand" philosophy
#  `luno/vision_memory/importance.py` already established for its own,
#  structurally similar 1-5 event-importance scale.
#
#  0 trivial    - transient state, no lasting value ("aku lagi makan").
#  1 temporary  - useful soon, likely obsolete soon ("besok servis PC").
#  2 useful     - reasonably useful for future context (tool/software use).
#  3 important  - likely relevant across many future conversations
#                 (ongoing projects/identity-adjacent facts).
#  4 core       - very stable, high-influence facts, or anything the user
#                 explicitly flags as important/permanent.
#
#  Deliberately NOT length-based (Step 5's own explicit prohibition) -
#  every rule below keys off semantic signal words, never char/token
#  count.
# ─────────────────────────────────────────────

MEMORY_IMPORTANCE_LEVELS = (0, 1, 2, 3, 4)

#: Baseline importance per EXISTING category (reuses `_classify_memory_category`
#: above - Step 6's "gunakan category yang sudah ada, importance adalah
#: metadata TAMBAHAN, bukan pengganti category"). `personal_fact` has no
#: dedicated keyword rule in `_classify_memory_category` yet (a pre-
#: existing gap from the prior sprint, not touched here - see
#: `docs/change_impact/memory_intelligence.md`), so it is listed for
#: completeness/forward-compatibility but is currently unreachable via
#: the classifier; harmless either way since `.get(category, 1)` below
#: falls back safely for any category value.
_CATEGORY_IMPORTANCE_BASE = {
    "technical_fact": 2,
    "preference": 2,
    "personal_fact": 2,
    "instruction": 2,
    "project_context": 3,
    "other": 1,
}

#: Explicit "this matters" signal - ALWAYS wins (Step 5: "explicit ...
#: this is important" is one of the strongest listed semantic signals;
#: Step 14's optional "Memory ini penting"/"jadikan permanen" commands
#: reuse this exact same pattern set for consistency between what makes
#: a memory important AT SAVE TIME and what the explicit follow-up
#: command recognizes).
_EXPLICIT_IMPORTANCE_RE = re.compile(
    r'\b(?:ini\s+(?:sangat\s+)?penting(?:\s+banget)?|penting\s+banget|sangat\s+penting|'
    r'jadikan\s+(?:memory\s+)?(?:ini\s+)?permanen|ingat\s+ini\s+selamanya|'
    r'jangan\s+pernah\s+lupa(?:kan)?\s+ini|'
    r'this\s+is\s+(?:very\s+)?important|remember\s+this\s+forever|make\s+this\s+permanent)\b',
    re.IGNORECASE,
)

#: Transient physical/emotional state - no lasting informational value.
#: Only applied when the category classifier ALSO found nothing more
#: specific (`category == "other"`) - a trivial-sounding phrase that also
#: happens to mention a stable technical/preference/project signal is
#: NOT trivial (e.g. "aku lagi capek ngoding Luno" still mentions the
#: project).
_TRIVIAL_ACTIVITY_RE = re.compile(
    r'\b(?:lagi|baru|habis)\s+(?:makan|minum|mandi|tidur|ngantuk|jalan-?jalan)\b|'
    r'\b(?:capek|lapar|haus|ngantuk|bosan)\b',
    re.IGNORECASE,
)

#: Near-term/expiring wording - caps importance at 1 regardless of
#: category, UNLESS an explicit-importance marker is also present
#: (checked first, see `_classify_memory_importance` below).
_TEMPORARY_WORDING_RE = re.compile(
    r'\b(?:besok|lusa|minggu\s+ini|minggu\s+depan|sebentar\s+lagi|nanti|sementara|'
    r'buat\s+sekarang|tomorrow|next\s+week|this\s+week|for\s+now|temporarily)\b',
    re.IGNORECASE,
)

#: Ongoing (not time-boxed) involvement in a project - bumps
#: `project_context` up toward "important". Distinct from
#: `_TEMPORARY_WORDING_RE` ("minggu ini ngerjain X" is time-boxed and
#: temporary; "sedang membangun X" with no end date is ongoing).
_ONGOING_INVOLVEMENT_RE = re.compile(
    r'\b(?:sedang|lagi)\s+(?:membangun|mengembangkan|mengerjakan|bikin|develop(?:ing)?)\b|'
    r'\bbuilding\b|\bdeveloping\b|\bworking\s+on\b',
    re.IGNORECASE,
)

#: Identity/purpose-defining statements about Luno itself or the user's
#: own long-term goals for it - a general, principled pattern (what ROLE
#: the user wants their assistant to have), not a hardcoded copy of the
#: sprint brief's own example sentence. Inherently near-permanent,
#: foundational data (Luno's own persona/identity already lives in
#: `luno/persona.py` - a statement redefining/reinforcing that IS
#: core-tier by nature, not by coincidence).
_IDENTITY_DEFINING_RE = re.compile(
    r'\bluno\s+(?:jadi|menjadi|sebagai|to\s+be|become)\s+(?:my\s+)?(?:personal\s+)?'
    r'(?:ai\s+)?companion\b|'
    r'\b(?:peran|identitas)\s+(?:utama\s+)?luno\b|'
    r'\bwant\s+luno\s+to\s+be\b',
    re.IGNORECASE,
)


def _classify_memory_importance(text, category):
    """Deterministic, keyword/pattern-based, NEVER length-based (Step 5's
    explicit prohibition - "longer text = more important"/"more tokens =
    higher importance" are both forbidden). Priority order: explicit
    override > trivial > temporary cap > category baseline (+ ongoing-
    involvement/identity-defining bumps)."""
    lowered = (text or "").lower()

    if _EXPLICIT_IMPORTANCE_RE.search(lowered):
        return 4

    if category == "other" and _TRIVIAL_ACTIVITY_RE.search(lowered):
        return 0

    if _TEMPORARY_WORDING_RE.search(lowered):
        return 1

    base = _CATEGORY_IMPORTANCE_BASE.get(category, 1)

    if category == "project_context" and _ONGOING_INVOLVEMENT_RE.search(lowered):
        base = max(base, 3)
    if _IDENTITY_DEFINING_RE.search(lowered):
        base = 4

    return base


def _get_importance(entry):
    """Backward-compatible accessor: a pre-sprint (schema v1) entry
    simply lacks the `importance` key - rather than defaulting to an
    arbitrary flat number, this recomputes a REAL classification from
    the entry's own `text`/`category` on the fly (never persisted back
    by this accessor alone - only a natural `add_memory`/`update_memory`
    call ever writes it to disk)."""
    if not isinstance(entry, dict):
        return 1
    importance = entry.get("importance")
    if isinstance(importance, int) and importance in MEMORY_IMPORTANCE_LEVELS:
        return importance
    text = entry.get("text", "") or ""
    category = entry.get("category") or _classify_memory_category(text)
    return _classify_memory_importance(text, category)


#: Per-importance-level (active_days, stale_days) decay thresholds -
#: Step 8's explicit ordering ("importance 4: sangat lambat decay ...
#: importance 0: jangan masuk long-term memory secara otomatis"). Beyond
#: `stale_days`, a memory becomes "archived" - EXCEPT importance>=4,
#: which never auto-archives (see `compute_lifecycle` below - "old core
#: memory remains protected", Step 17's own required test).
_LIFECYCLE_THRESHOLDS_DAYS = {
    4: (180, 720),
    3: (120, 365),
    2: (60, 180),
    1: (14, 60),
    0: (3, 14),
}

#: Explicit user-authored memories decay slower than LLM-inferred ones -
#: Step 8/9's "user_explicit memories harus mendapat perlakuan lebih kuat
#: daripada inferred memory".
_EXPLICIT_SOURCE_DECAY_MULTIPLIER = 1.5


def compute_lifecycle(entry, now=None):
    """Pure function of `(importance, updated_at, source, now)` - NOT
    stored on the entry, NEVER mutates it, no background job anywhere
    computes or writes this (Step 19: no background agent). Called
    on-demand wherever lifecycle matters (retrieval ranking, tests,
    future dashboard/debug views). Conservative by design (Step 8: decay
    tidak boleh menghapus/mengaburkan memory secara agresif) - "archived"
    only means "excluded from NORMAL/ambient retrieval", the entry is
    still fully intact and still findable via `search_memories()`/
    `list_memories()`/`get_memory()` (Step 7's own "archived: tetap dapat
    dipulihkan jika diperlukan")."""
    if not isinstance(entry, dict):
        return "active"

    # Memory Lifecycle & Maintenance sprint (Step 10) - an explicit
    # maintenance-triggered archive is represented as ONE MORE optional,
    # additive input to this same pure function, not a second lifecycle
    # model and not a persisted lifecycle VALUE: `archived_by_maintenance`
    # is metadata the entry carries (set only by `apply_maintenance_plan()`,
    # never automatically), and this function still recomputes its answer
    # fresh every call - it just short-circuits to "archived" when that
    # flag is present, before doing the age-based computation below.
    if entry.get("archived_by_maintenance"):
        return "archived"

    now = now or datetime.now()
    importance = _get_importance(entry)

    raw_ts = entry.get("updated_at") or entry.get("created_at")
    try:
        updated_at = datetime.fromisoformat(raw_ts) if raw_ts else now
    except (TypeError, ValueError):
        updated_at = now
    if updated_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=updated_at.tzinfo)
    elif updated_at.tzinfo is None and now.tzinfo is not None:
        updated_at = updated_at.replace(tzinfo=now.tzinfo)

    age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)

    active_days, stale_days = _LIFECYCLE_THRESHOLDS_DAYS.get(importance, _LIFECYCLE_THRESHOLDS_DAYS[1])
    if entry.get("source") == "user_explicit":
        active_days *= _EXPLICIT_SOURCE_DECAY_MULTIPLIER
        stale_days *= _EXPLICIT_SOURCE_DECAY_MULTIPLIER

    if age_days <= active_days:
        return "active"
    if age_days <= stale_days:
        return "stale"
    if importance >= 4:
        # Core memories never fully decay out of normal awareness - see
        # docstring above.
        return "stale"
    return "archived"


_memories = []  # list of {"id": str, "text": str, "created_at": iso-str, ...}


# ─────────────────────────────────────────────
#  Memory Recovery & Persistence Hardening sprint - backup + atomic-write
#  layer. Extends this module's EXISTING `_save()`/`_load()` in place -
#  no second persistence engine, no new storage location other than a
#  `backups/` subfolder next to the existing file. Direct response to
#  the incident this sprint recovers from: `_save()` previously did a
#  bare `open(path, "w")` truncate-in-place with no prior backup and no
#  atomicity, so any write (including an ad-hoc, non-isolated script
#  mistakenly calling into this module) could silently and irreversibly
#  destroy the previous content. See `docs/change_impact/memory_recovery.md`.
# ─────────────────────────────────────────────

#: Backups live in `<same directory as long_term_memory.json>/backups/` -
#: co-located with the file they protect, same convention the sprint
#: brief's own example lays out.
_MEMORY_BACKUP_DIR_NAME = "backups"

#: Retention policy (Phase 6) - keep at most this many timestamped
#: backups, oldest pruned first. Never fewer than 1 regardless of this
#: value (see `_prune_memory_backups()` below's own `max(1, ...)` floor) -
#: "never delete the last valid backup" is enforced even if this
#: constant is ever misconfigured to 0.
_MEMORY_BACKUP_RETENTION = 20

# ─────────────────────────────────────────────
#  Long-Term Memory Self-Healing / Recovery Hardening sprint - extends
#  the block above IN PLACE (same file, same functions, no second
#  persistence engine). Adds: (1) a root-shape validation contract
#  reused by BOTH the primary load and the backup scan (previously the
#  primary had NO shape check at all - a syntactically valid but
#  wrong-shaped file, e.g. `{}` instead of `[]`, was silently accepted
#  as-is); (2) a quarantine artifact for a primary file that turns out
#  to be unrecoverable (no valid backup either), so the corrupted bytes
#  are never simply discarded; (3) a small, in-memory-only observability
#  status distinguishing healthy / recovered_from_backup / fresh_after_
#  unrecoverable_corruption, surfaced through the EXISTING
#  `memory_health_report()` (no new dashboard page, no second status
#  model - see `get_persistence_status()` below).
#
#  IMPORTANT ARCHITECTURAL CONSTRAINT (found via inspection, not
#  invented): `_load()` runs unconditionally at MODULE IMPORT time
#  ("load sekali saat modul pertama kali diimpor" - see the bottom of
#  this file) - i.e. potentially BEFORE any test/bootstrap isolation
#  redirect has had a chance to run. `tests/test_sprint64_memory_
#  corruption_forensics.py`'s own `test_B_load_is_read_only_no_write_
#  primitive_in_its_source` (a real, already-passing regression test)
#  asserts `_load()`'s OWN source contains NO write primitive at all
#  (`_atomic_write_json(`, `json.dump(`, `f.write(`, no write-mode
#  `open(`) - this is exactly the property that prevents a repeat of
#  the ORIGINAL incident this whole hardening layer exists because of
#  (`docs/change_impact/memory_recovery.md`: a bare, non-isolated
#  import once silently overwrote production `config/long_term_
#  memory.json`). Therefore `_load()` MUST stay 100% read-only, even
#  for the "unrecoverable corruption" branch - it may only decide (in
#  memory) that a quarantine+fresh-store is NEEDED, never perform the
#  write itself. The actual quarantine-copy + fresh-store persistence
#  is deferred to the next real `_save()` call (`_finalize_pending_
#  quarantine_if_any()` below, invoked from `_save()`, never from
#  `_load()` or module import) - the same funnel every other mutation
#  in this module already goes through, so this adds no new write path.
# ─────────────────────────────────────────────

#: Quarantine artifacts live in `<same directory as long_term_memory.json>/
#: quarantine/` - a SIBLING of the existing `backups/` directory (same
#: co-located convention), deliberately a SEPARATE directory rather than
#: reusing `backups/` itself so a quarantined (KNOWN-corrupt) file can
#: never accidentally be picked up by `_list_memory_backups()`'s own
#: `long_term_memory.*.json` glob and mistaken for a legitimate,
#: restorable backup candidate.
_MEMORY_QUARANTINE_DIR_NAME = "quarantine"

#: In-memory-only (never persisted inside `config/long_term_memory.json`
#: itself, never a second on-disk state model) - what the MOST RECENT
#: `_load()` call actually did. One of "healthy" (primary loaded fine,
#: or no primary file existed yet - both are a normal, non-corrupted
#: state), "recovered_from_backup" (primary was invalid; a valid backup
#: was used instead), or "fresh_after_unrecoverable_corruption" (primary
#: was invalid AND no backup was usable either; an empty in-memory store
#: was created). Read via `get_persistence_status()` below - never
#: mutated by anything outside this module.
_persistence_status = {"status": "healthy", "detail": None}

#: Set by `_load()` ONLY when the primary file existed, was invalid, and
#: no backup could recover it - the absolute path of the corrupted
#: primary file, still sitting untouched on disk at that moment,
#: awaiting quarantine by the NEXT `_save()` call. `None` whenever there
#: is nothing pending. Always reset to `None` at the very start of every
#: `_load()` call (see `_load()` itself) so a stale value from an
#: earlier, unrelated `_load()` (e.g. a different isolated test's own
#: tmp_path, in a prior test that never itself called `_save()`) can
#: never leak into a later, unrelated `_save()` call - `_finalize_
#: pending_quarantine_if_any()` below additionally double-checks the
#: pending path's directory still matches the CURRENT `config.
#: LONG_TERM_MEMORY_FILE`'s directory before acting, as defense in depth
#: against exactly that cross-test staleness class of bug.
_pending_quarantine_path = None


def _validate_memory_data(data):
    """The ONE shape-validation contract this module uses for BOTH the
    primary file and every backup candidate (Section "avoid duplicated
    validation logic" - previously `_load_latest_valid_backup()` had its
    own inline `isinstance(data, list)` check and the primary had NO
    check at all; both now call this same function).

    Deliberately ROOT-SHAPE ONLY (must be a `list`) - NOT per-entry
    field validation. This is a considered choice, not an oversight:
    `tests/test_manual_memory.py::test_partial_malformed_entries_are_
    skipped_not_crashed` is an existing, already-passing, intentional
    test proving a hand-edited PRIMARY file containing a mix of one
    well-formed entry and one malformed entry (missing `text`, or not
    even a dict) must keep the well-formed entry and simply skip the
    malformed one at the point of use (`search_memories()`/`make_
    manual_memory_source()` already do this, via their own existing
    `isinstance(m, dict) and m.get("text")`-style filters) - NOT treat
    the whole file as corrupted and discard the good entry too.
    Rejecting a file merely because SOME entry is individually
    malformed would silently throw away that still-recoverable good
    entry, which is exactly the "never silently discard a valid entry"
    principle this sprint's own brief asks for - just applied to mean
    "don't let one bad entry evict a good sibling entry," not "reject
    the whole list." A completely wrong ROOT type (a dict, a string, an
    int, `None`, ...) is a different, unambiguous failure category -
    downstream code universally assumes `for m in _memories` iteration
    and `m["text"]`/`m.get(...)` access, which a non-list root breaks
    for EVERY entry at once, not just a malformed one - that case
    legitimately warrants backup recovery."""
    return isinstance(data, list)


def get_persistence_status():
    """Public, read-only accessor - a COPY of `_persistence_status`
    (never the live dict) so a caller can never mutate this module's
    own bookkeeping by holding a reference to it. Used by `memory_
    health_report()` below (the existing, already dashboard-surfaced
    mechanism - see `luno/dashboard/collectors.py::collect_memory_
    health()`) and directly by tests."""
    return dict(_persistence_status)


def _memory_backup_dir():
    return os.path.join(os.path.dirname(os.path.abspath(config.LONG_TERM_MEMORY_FILE)), _MEMORY_BACKUP_DIR_NAME)


def _memory_quarantine_dir():
    return os.path.join(os.path.dirname(os.path.abspath(config.LONG_TERM_MEMORY_FILE)), _MEMORY_QUARANTINE_DIR_NAME)


def _memory_quarantine_filename(now=None):
    now = now or datetime.now()
    # Same microsecond-resolution convention as `_memory_backup_filename()`
    # - collisions are already astronomically unlikely, but `_finalize_
    # pending_quarantine_if_any()` below still defends against one
    # explicitly (Section "quarantine does not overwrite previous
    # quarantine artifacts").
    return f"long_term_memory.corrupt.{now.strftime('%Y%m%dT%H%M%S%f')}.json"


def _memory_backup_filename(now=None):
    now = now or datetime.now()
    # Microsecond-resolution suffix so two backups within the same
    # second (e.g. a fast test loop, or two saves in one turn) never
    # collide and silently overwrite one another.
    return f"long_term_memory.{now.strftime('%Y%m%dT%H%M%S%f')}.json"


def _backup_current_memory_file():
    """Copies the CURRENT on-disk file (if any) into `backups/` BEFORE
    any write touches it - Phase 6's "backups must happen BEFORE
    destructive mutation." A COPY, never a move/rename - the original
    stays exactly where it is until `_atomic_write_json()` swaps it
    afterward. Best-effort and non-raising: a backup failure is logged,
    never allowed to block Luno's ability to remember something new (the
    alternative - refusing to save a new memory because the backup
    subsystem had a problem - would itself be a worse outcome).

    Sprint 67 (Mutation Audit Trail): a successful backup gets a
    STANDARD-category `luno.mutation_audit` record - the same forensic
    coverage `luno.persistence.atomic_write_json()`'s own backup step
    gets for every OTHER writable store, applied here to this file's
    own separate (pre-existing, not-yet-migrated-to-`persistence.py`)
    backup implementation."""
    src = config.LONG_TERM_MEMORY_FILE
    if not os.path.exists(src):
        return None
    backup_dir = _memory_backup_dir()
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, _memory_backup_filename())
        shutil.copyfile(src, dest)
        from . import mutation_audit
        mutation_audit.record_backup_created(
            backup_path=dest, source_path=src,
            source_component="memory", source_operation="_backup_current_memory_file",
        )
        return dest
    except Exception as ex:
        print(f"[Memory] ✗ Failed to create pre-write backup of {src}: {ex}")
        return None


def _list_memory_backups():
    backup_dir = _memory_backup_dir()
    try:
        return sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith("long_term_memory.") and f.endswith(".json")
        )
    except Exception:
        return []


def _prune_memory_backups():
    """Retention policy (Phase 6) - keep at most `_MEMORY_BACKUP_RETENTION`
    timestamped backups (oldest deleted first), but NEVER delete the
    last remaining one.

    Sprint 67: each deletion gets a lightweight, TEMP-category
    `luno.mutation_audit` record - see `luno.persistence.prune_backups()`'s
    identical addition for the other six stores."""
    from . import mutation_audit
    backup_dir = _memory_backup_dir()
    entries = _list_memory_backups()
    keep = max(1, _MEMORY_BACKUP_RETENTION)
    excess = len(entries) - keep
    for name in entries[:max(0, excess)]:
        backup_path = os.path.join(backup_dir, name)
        try:
            os.remove(backup_path)
            mutation_audit.record_backup_pruned(
                backup_path=backup_path,
                source_component="memory", source_operation="_prune_memory_backups",
            )
        except Exception as ex:
            print(f"[Memory] ✗ Failed to prune old backup {name}: {ex}")


def _refuse_if_pytest_targeting_unisolated_path(path):
    """Phase 7 guard - refuses (raises loudly) a write made while a
    pytest test is running (`PYTEST_CURRENT_TEST` is set - pytest itself
    always sets this, nothing else does) to a path that is NOT under the
    system temp directory. `tests/conftest.py`'s autouse
    `isolate_persistent_state` fixture already redirects
    `config.LONG_TERM_MEMORY_FILE` to a fresh path under pytest's own
    `tmp_path` (itself always under the system temp directory) for EVERY
    test collected under `tests/` - so in a correctly isolated run, this
    check never trips. It exists purely as defense-in-depth for the
    exact failure class this sprint recovers from. Deliberately
    inert outside pytest - `PYTEST_CURRENT_TEST` is never set outside a
    pytest run, so normal production runtime (`main.py`/
    `main_runtime_demo.py`, the dashboard server, an authorized
    migration/recovery script) is completely unaffected."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        resolved = os.path.abspath(path)
        tmp_root = os.path.abspath(tempfile.gettempdir())
    except Exception:
        return
    if resolved.startswith(tmp_root):
        return
    raise RuntimeError(
        f"Refusing to write memory file at {resolved!r} during a pytest run - this "
        f"path is not under the system temp directory ({tmp_root!r}), so it looks "
        f"like a real/production path rather than an isolated test fixture path. "
        f"Isolate it via tests/conftest.py's isolate_persistent_state fixture (or "
        f"monkeypatch config.LONG_TERM_MEMORY_FILE to a tmp_path) instead of writing "
        f"here directly."
    )


def _atomic_write_json(path, data):
    """Write-temp-then-replace (Phase 6's "writes must be atomic") - the
    file at `path` is never observed half-written: either the OLD
    complete content is still there, or the NEW complete content is,
    never a partial/corrupt intermediate state. `os.replace()` (not
    `os.rename()`) is used specifically because it atomically overwrites
    an existing destination on BOTH POSIX and Windows (`os.rename()`
    raises on Windows when the destination already exists). If anything
    raises before the final `os.replace()`, the ORIGINAL file at `path`
    is completely untouched (Phase 6's "failed writes must not destroy
    the current valid state") - only the throwaway `.tmp` file is
    affected, and this function best-effort cleans that up before
    re-raising.

    Sprint 67 (Mutation Audit Trail) - THIS is the dedicated, "major"
    forensic coverage Phase 7 asks for `config/long_term_memory.json`
    specifically: `path` here always resolves CRITICAL (it is always
    either `LONG_TERM_MEMORY_FILE` itself or `SESSION_SUMMARIES_FILE`,
    both `luno.config` `*_FILE` constants), so every call captures a
    before/after SHA-256, fails closed via `mutation_audit.assert_
    audit_subsystem_available()` BEFORE the write begins (Phase 5), and
    records the ACTUAL outcome (success or failure) in a `finally` block
    AFTER `os.replace()` has already succeeded or failed - never before
    (Phase 6). This instruments FUTURE mutations only - the CURRENT,
    already-corrupted `config/long_term_memory.json` on disk is never
    read, rewritten, or otherwise touched by this function; it only
    observes whatever the NEXT successful/failed `_save()` call does.
    `_save()`'s own pre-existing "never raise out of a save" contract is
    preserved: a `AuditSubsystemUnavailableError` raised here propagates
    up to `_save()`'s own catch-all, which logs and swallows it exactly
    like any other save failure - the practical effect is the write
    simply does not happen, which IS the fail-closed behavior Phase 5
    asks for, achieved via the existing error-handling contract rather
    than a new one."""
    from . import mutation_audit
    category = mutation_audit.classify_path(path)
    before = mutation_audit.snapshot(path, category)
    correlation_id = None
    if category == mutation_audit.PathCategory.CRITICAL:
        mutation_audit.assert_audit_subsystem_available()
        # Sprint 68 - see luno/persistence.py::atomic_write_json()'s
        # identical addition and mutation_audit.record_pending_
        # mutation()'s own docstring: makes the Phase 10.D crash window
        # (mutation succeeds, its own completed audit record fails to
        # append) forensically DETECTABLE rather than silent, without a
        # second persistence/transaction system.
        correlation_id = mutation_audit.record_pending_mutation(
            operation="write", path=path, category=category,
            source_component="memory", source_operation="_atomic_write_json",
            before=before,
        )

    success = False
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise
        success = True
    finally:
        after = mutation_audit.snapshot(path, category)
        mutation_audit.record_mutation(
            operation="write", path=path, category=category,
            source_component="memory", source_operation="_atomic_write_json",
            before=before, after=after, success=success,
            correlation_id=correlation_id,
        )


def _load_latest_valid_backup():
    """Phase 8's "restart/reload loads the latest valid state" - if the
    PRIMARY file fails to parse (corrupted), this tries each backup
    newest-first (every successful `_save()` leaves one behind, taken
    BEFORE that save's own write - see `_backup_current_memory_file()`)
    and returns the first one that parses as a JSON list, or `None` if
    none do (or no backups exist) - `_load()` below falls back to an
    empty store only in that last case, never silently discarding
    recoverable content when a valid backup exists."""
    backup_dir = _memory_backup_dir()
    for name in reversed(_list_memory_backups()):
        try:
            with open(os.path.join(backup_dir, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            if _validate_memory_data(data):
                return data, name
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return None, None


def _load():
    """Read-only, by construction and by contract (see the "IMPORTANT
    ARCHITECTURAL CONSTRAINT" note above `_MEMORY_QUARANTINE_DIR_NAME`) -
    this function must never write to disk in ANY branch, since it runs
    unconditionally at module import time, potentially before any
    isolation redirect exists.

    Deterministic recovery sequence (Long-Term Memory Self-Healing /
    Recovery Hardening sprint):
      1. Primary missing entirely -> healthy, empty store (unchanged
         from before this sprint - never creates the file as a side
         effect).
      2. Primary reads and validates (`_validate_memory_data()`) ->
         healthy, loaded as-is (unchanged from before this sprint).
      3. Primary fails to read/parse OR fails validation -> attempt
         backup recovery, newest-first, first VALID one wins
         (`_load_latest_valid_backup()`, unchanged mechanism, now using
         the same shared validation contract as the primary). Recovered
         content is used AS-IS (ids/metadata untouched, never re-ranked
         or rewritten) - status becomes "recovered_from_backup". The
         still-corrupted primary file itself is left completely
         untouched on disk (proven by `tests/test_sprint63_long_term_
         memory_recovery.py::test_M_repeated_recovery_from_backup_is_
         idempotent`) - the NEXT successful `_save()` will naturally
         persist the recovered content back to the primary path via the
         existing, already-hardened atomic-write mechanism; no direct/
         unsafe overwrite is ever performed here.
      4. Primary invalid AND no backup usable either -> status becomes
         "fresh_after_unrecoverable_corruption", `_memories` becomes an
         empty list (unchanged fallback value from before this sprint),
         and `_pending_quarantine_path` records the corrupted primary's
         path so the NEXT `_save()` call can quarantine it (copy, never
         destroy) before persisting the fresh empty store - see
         `_finalize_pending_quarantine_if_any()` below.
    """
    global _memories, _persistence_status, _pending_quarantine_path
    path = config.LONG_TERM_MEMORY_FILE
    # Always reset first - a stale pending-quarantine path from an
    # EARLIER, unrelated `_load()` call (e.g. a different isolated
    # test's own tmp_path) must never survive into this fresh
    # evaluation; this call decides the current pending state from
    # scratch, every time.
    _pending_quarantine_path = None

    if not os.path.exists(path):
        _memories = []
        _persistence_status = {"status": "healthy", "detail": "no primary file - starting from an empty store"}
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as ex:
        print(f"[Memory] ✗ Failed to load {path}: {ex}")
        print("[Memory] ⚠ Primary memory invalid - attempting backup recovery...")
        return _recover_from_backup_or_go_fresh(path)

    if not _validate_memory_data(data):
        print(f"[Memory] ✗ {path} did not match the expected shape (a JSON list) - treating as corrupted")
        print("[Memory] ⚠ Primary memory invalid - attempting backup recovery...")
        return _recover_from_backup_or_go_fresh(path)

    _memories = data
    _persistence_status = {"status": "healthy", "detail": None}
    print(f"[Memory] ✓ Loaded {len(_memories)} long-term memory item(s)")


def _recover_from_backup_or_go_fresh(path):
    """Shared tail of `_load()`'s two "primary is invalid" branches
    (read/parse failure and shape-validation failure) - kept as one
    function so both failure categories go through the EXACT same
    backup-scan/fresh-fallback logic, never two slightly-diverging
    copies of it."""
    global _memories, _persistence_status, _pending_quarantine_path
    recovered, backup_name = _load_latest_valid_backup()
    if recovered is not None:
        _memories = recovered
        _persistence_status = {"status": "recovered_from_backup", "detail": backup_name}
        print(f"[Memory] ⚠ Primary file unreadable - recovered {len(_memories)} item(s) from backup {backup_name!r} instead.")
        return
    print("[Memory] ✗ Primary and all backups unrecoverable - continuing with a fresh, empty long-term memory")
    print("[Memory]   store (the corrupted primary will be quarantined on the next save, never silently overwritten).")
    _memories = []
    _persistence_status = {"status": "fresh_after_unrecoverable_corruption", "detail": "no valid backup available either"}
    _pending_quarantine_path = path


def _finalize_pending_quarantine_if_any():
    """Performs the ACTUAL disk-side quarantine-copy of a corrupted
    primary file `_load()` previously determined was unrecoverable
    (Section "QUARANTINE" of the sprint brief) - deliberately NOT part
    of `_load()` itself (see the read-only constraint documented above
    `_MEMORY_QUARANTINE_DIR_NAME`). Called from `_save()`, the ONE
    existing disk-writing funnel every other mutation already goes
    through - not a new write path.

    A copy, never a move - the quarantine artifact is a SEPARATE file;
    whatever currently sits at the primary path is left exactly as-is
    for `_save()`'s own subsequent, already-hardened backup-then-
    atomic-write to handle normally. Never overwrites an existing
    quarantine artifact (a numeric suffix is appended on the
    astronomically unlikely event of a filename collision). A
    quarantine FAILURE (permissions, disk full, ...) is logged and
    swallowed, never raised - per the brief's own "Luno must not crash
    solely because quarantine failed," the fresh-memory save that
    follows immediately after must still be attempted regardless."""
    global _pending_quarantine_path
    src = _pending_quarantine_path
    if not src:
        return
    # At-most-one-attempt semantics, and defuses the cross-test-
    # staleness class of bug described in `_pending_quarantine_path`'s
    # own docstring: clear immediately, then only proceed if the
    # pending path's directory still matches the CURRENT primary file's
    # directory (a genuine same-session pending quarantine always
    # matches; a stale leftover from an unrelated, earlier `_load()`
    # almost never will).
    _pending_quarantine_path = None
    current_dir = os.path.dirname(os.path.abspath(config.LONG_TERM_MEMORY_FILE))
    if os.path.dirname(os.path.abspath(src)) != current_dir:
        return
    if not os.path.exists(src):
        return

    try:
        quarantine_dir = _memory_quarantine_dir()
        os.makedirs(quarantine_dir, exist_ok=True)
        dest = os.path.join(quarantine_dir, _memory_quarantine_filename())
        suffix = 0
        final_dest = dest
        while os.path.exists(final_dest):
            suffix += 1
            final_dest = f"{dest}.{suffix}"
        shutil.copy2(src, final_dest)
        print(f"[Memory] ⚠ Quarantined corrupted primary file to {final_dest!r} (preserved, never deleted).")
        from . import mutation_audit
        mutation_audit.record_backup_created(
            backup_path=final_dest, source_path=src,
            source_component="memory", source_operation="_finalize_pending_quarantine_if_any",
        )
    except OSError as ex:
        print(f"[Memory] ✗ Failed to quarantine corrupted primary file {src!r}: {ex} - continuing with a fresh memory store anyway.")


def _save():
    """Every mutation in this module funnels through here. As of the
    Memory Recovery & Persistence Hardening sprint: refuses loudly if a
    non-isolated pytest write is detected (Phase 7), takes a timestamped
    backup of whatever is currently on disk BEFORE writing (Phase 6),
    writes the new content atomically (Phase 6), then prunes old backups
    per the retention policy (Phase 6). A failure at any step is caught
    and logged (matching this function's own pre-existing "never raise
    out of a save" contract) rather than propagated - except the Phase 7
    pytest guard, which is DELIBERATELY allowed to raise/fail the test
    loudly, since silently swallowing that one is exactly the failure
    mode Phase 7 exists to prevent.

    Long-Term Memory Self-Healing / Recovery Hardening sprint: also
    finalizes any PENDING quarantine left behind by a previous `_load()`
    call that found the primary unrecoverably corrupted (`_finalize_
    pending_quarantine_if_any()`) - a no-op on every normal save (the
    overwhelming majority of calls), since `_pending_quarantine_path` is
    `None` unless `_load()` itself just set it."""
    _refuse_if_pytest_targeting_unisolated_path(config.LONG_TERM_MEMORY_FILE)
    _finalize_pending_quarantine_if_any()
    try:
        _backup_current_memory_file()
        _atomic_write_json(config.LONG_TERM_MEMORY_FILE, _memories)
        _prune_memory_backups()
    except Exception as ex:
        print(f"[Memory] ✗ Failed to save {config.LONG_TERM_MEMORY_FILE}: {ex}")


_load()  # load sekali saat modul pertama kali diimpor


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


#: Consolidation Jaccard-overlap band (Memory Intelligence sprint, Step
#: 10/11) - "same topic, different wording/value" candidates for
#: automatic UPDATE-with-history, checked ONLY after the pre-existing
#: exact/near-exact substring dedup check above has already found
#: nothing (that check handles near-identical phrasing; this one handles
#: genuinely different wording about the same subject, e.g. "User
#: menggunakan RTX 3060 Ti" vs "Sekarang GPU user adalah RTX 3060 Ti", or
#: a genuine value correction like RTX 3060 Ti -> RTX 4070). Below
#: `_CONSOLIDATION_MIN`: not enough shared vocabulary to be confidently
#: "about the same thing" -> treated as a genuinely new, unrelated fact.
#: Set to 0.45 rather than a lower value based on a REAL regression
#: caught during this sprint's own test run: `tests/test_manual_memory.py`'s
#: pre-existing `test_update_memory_by_topic_ambiguous_does_not_destroy_state`
#: deliberately saves TWO DISTINCT memories ("GPU lamaku RTX 3060 Ti" /
#: "GPU baruku juga RTX 3060 Ti dulunya") specifically so a later
#: topic-based update has two legitimate candidates to be ambiguous
#: about. Those two texts score ~0.428 Jaccard (sharing "gpu"/"rtx"/"ti",
#: differing mainly in a possessive adjective the digit-blind tokenizer
#: can't weight specially) - at a lower floor they were being
#: auto-consolidated by `add_memory()` itself before that test's own
#: `update_memory_by_topic()` call ever ran, silently collapsing two
#: intentionally-separate memories into one and breaking a previously-
#: passing test. Per Step 2's "never break a previously-passing test"
#: rule, the floor was raised to 0.45 (comfortably above that 0.428
#: collision) rather than special-casing the test data - Step 10's own
#: worked examples (a same-fact reword, and a GPU model value
#: correction) both land at 0.75-0.78 Jaccard, safely within this floor.
_CONSOLIDATION_MIN = 0.45

#: Memory Conflict Resolution sprint: raised from an original 0.85 to
#: 0.92 after a real gap was found while testing explicit corrections -
#: "Aku pakai RTX 3070 Ti di laptop." -> "Aku sekarang pakai RTX 3060 Ti
#: di laptop." scores ~0.857 Jaccard (nearly every token shared, only
#: the digit-driven GPU model differs, invisible to the digit-blind
#: tokenizer) yet neither text is a literal substring of the other, so
#: the PRE-EXISTING phase-1 near-duplicate check never catches it either
#: - at the original 0.85 ceiling this pair fell through BOTH mechanisms
#: entirely and was silently treated as two completely unrelated facts,
#: exactly the "silently destroy/ignore contradictory information"
#: outcome this sprint exists to prevent. Not raised all the way to 1.0:
#: `tests/test_memory_intelligence.py::test_retrieval_result_count_is_
#: bounded_by_budget` saves ten "aku suka game nomor {i} banget"
#: memories whose digit-stripped tokens are ALL identical to each other
#: (Jaccard exactly 1.0 pairwise) - these must stay ten separate,
#: unrelated facts, not collapse into one. 0.92 catches the former case
#: (0.857 < 0.92) without reopening the latter (1.0 >= 0.92, still
#: excluded).
_CONSOLIDATION_MAX = 0.92

#: Per-entry history is bounded - Step 6/13's "don't become a growing
#: messy JSON dump" concern applied at the per-entry level too (the
#: STORE's own overall size is a separate, pre-existing characteristic
#: this sprint does not change - see `docs/change_impact/memory_intelligence.md`).
_MAX_MEMORY_HISTORY_ENTRIES = 5

# ─────────────────────────────────────────────
#  MEMORY CONFLICT RESOLUTION sprint - a deterministic classification
#  layer that runs AFTER `_find_conflicting_memory()` (below, UNCHANGED)
#  has already narrowed things down to exactly one same-category,
#  in-Jaccard-band candidate. Answers "what KIND of relationship does
#  this candidate have to the new text" - the prior sprint's
#  `_find_conflicting_memory` only ever answered "is there a candidate
#  at all", then unconditionally merged. See
#  `docs/change_impact/memory_conflict_resolution.md` for the full
#  worked-example trace against every existing consolidation test.
# ─────────────────────────────────────────────

#: A small, fixed list of qualifier/location words - if the new text and
#: the candidate each contain at least one of these, and the SETS found
#: in each text are disjoint (no shared qualifier), the two facts are
#: about different subjects/contexts and must NOT be merged, no matter
#: how much other vocabulary they share. This is what correctly keeps
#: "RTX 3060 Ti untuk PC" and "RTX 3070 Ti untuk server" separate (both
#: share "rtx"/"untuk"/"ti" - digit-blind tokenizer - which alone would
#: otherwise land solidly inside the consolidation band).
_CONTEXT_QUALIFIERS = frozenset({
    "pc", "laptop", "server", "vps", "kantor", "rumah", "utama", "cadangan",
    "backup", "primary", "secondary", "home", "office", "main", "desktop",
})

#: Dual-marker temporal contrast - BOTH an "old" marker and a "new"
#: marker must be present (Section 9's own "Dulu X, sekarang Y" example)
#: - a bare "sekarang" alone (no "dulu"/"used to" counterpart) is treated
#: as a plain CORRECTION signal instead (`_CORRECTION_RE` below), per
#: Section 4 listing "sekarang X" under explicit corrections and Section
#: 2 reserving the TEMPORAL_CHANGE label for the dual "dulu ... sekarang"
#: framing specifically.
_TEMPORAL_OLD_MARKERS = ("dulu", "used to", "no longer", "not anymore", "dulunya")
_TEMPORAL_NEW_MARKERS = ("sekarang", " now", "now,", "now.")

#: Explicit correction language (Section 4) - deliberately a short,
#: maintainable list, not an enormous language parser. "sekarang" alone
#: (without a paired "dulu"/"used to") lands here rather than in the
#: temporal-change bucket above.
_CORRECTION_RE = re.compile(
    r'\bkoreksi\s+memory\b|\byang\s+tadi\s+salah\b|\bbukan\b.{0,40}\btapi\b|'
    r'\bganti\b.{0,40}\bmenjadi\b|\bsekarang\b|'
    r'\bactually\b|\bcorrection\b|\bi\s+no\s+longer\b|\bi\s+use\b.{0,40}\bnow\b',
    re.IGNORECASE,
)


def _has_distinguishing_context(new_tokens, existing_tokens):
    """True if both token sets contain at least one context qualifier
    and the qualifiers found are completely disjoint - evidence the two
    facts are about different subjects (Section 10's "conflict groups
    must not assume every matching keyword represents the same fact")."""
    new_qualifiers = new_tokens & _CONTEXT_QUALIFIERS
    existing_qualifiers = existing_tokens & _CONTEXT_QUALIFIERS
    if not new_qualifiers or not existing_qualifiers:
        return False
    return new_qualifiers.isdisjoint(existing_qualifiers)


def _is_temporal_change(lowered_text):
    has_old = any(m in lowered_text for m in _TEMPORAL_OLD_MARKERS)
    has_new = any(m in lowered_text for m in _TEMPORAL_NEW_MARKERS)
    return has_old and has_new


#: Sprint 41 (Temporal Memory & Timeline Awareness, Phase 2 root cause) -
#: a small, bounded interrogative-shape detector. Live reproduction
#: (Scenario C turn 3: "Sekarang aku pakai GPU apa?") found that a bare
#: "sekarang" - `_CORRECTION_RE`'s own weakest, most generic alternative
#: - matches an ORDINARY QUESTION about the current state exactly as
#: readily as a genuine declarative correction ("Sekarang aku pakai RTX
#: 4070."). Since `update_topic_history()`'s supersession-tagging gate
#: (`is_correction_signal()` below) fires on ANY match, a plain
#: current-state QUESTION that happens to share a token with a PLANNED
#: topic-history entry could wrongly tag that entry "superseded" - not
#: because anything was actually replaced, but because the user merely
#: ASKED about the present. This detector is used ONLY to gate the bare
#: "sekarang" alternative (see `is_correction_signal()` below) - every
#: OTHER correction phrase in `_CORRECTION_RE` (explicit "ganti ...
#: menjadi", "bukan ... tapi", "actually", "correction", dual
#: "dulu ... sekarang") remains an unconditional signal regardless of
#: question shape, since those are inherently declarative-only phrasings
#: with no plausible question reading.
_INTERROGATIVE_RE = re.compile(
    r'\?\s*$|\b(?:apa|apakah|siapa|kapan|kenapa|mengapa|gimana|bagaimana|berapa|'
    r'di\s*mana|dimana|yang\s+mana|what|when|why|how|where|who|which)\b',
    re.IGNORECASE,
)


def _is_interrogative(lowered_text):
    return bool(_INTERROGATIVE_RE.search(lowered_text))


#: Derived (not duplicated) from `_CORRECTION_RE` above - every
#: alternative EXCEPT the bare `\bsekarang\b` one, built by string
#: substitution at import time so the two patterns can never silently
#: drift apart if `_CORRECTION_RE` is ever edited (a maintenance risk a
#: hand-copied second regex would carry). Used only internally by
#: `is_correction_signal()`'s question-exclusion check below.
_CORRECTION_RE_STRONG = re.compile(
    _CORRECTION_RE.pattern.replace(r'\bsekarang\b|', ''), re.IGNORECASE,
)


def is_correction_signal(text):
    """Sprint 40 (Memory Confidence & Conflict Resolution) - a PUBLIC,
    read-only wrapper around the two existing, private, already-tested
    textual signals `_CORRECTION_RE`/`_is_temporal_change()` above (the
    Memory Conflict Resolution sprint's own "explicit correction wording"/
    "dual old+new wording" detectors, built for the PERSISTENT long-term
    memory store's `_classify_conflict()`). Deliberately NOT a new
    detector - `luno.memory_context` (Phase 0 found: ordinary conversation
    turns, i.e. everything that ISN'T an explicit "inget ya ..." command,
    never reach `add_memory()`/`_classify_conflict()` at all - they only
    ever flow through the EPHEMERAL `_active_topic`/`_topic_history`
    bag-of-terms mechanism, Sprint 4/6/38/39, which has zero conflict
    awareness) needs the SAME "does this turn's own wording signal a
    correction/replacement of something already established" question for
    THAT ephemeral layer too, without inventing a second regex/wording
    list. Reused, not duplicated - if `_CORRECTION_RE`/`_TEMPORAL_OLD_
    MARKERS`/`_TEMPORAL_NEW_MARKERS` are ever tuned for the persistent
    store, this wrapper picks up the change automatically.

    Deliberately domain-generic: matches on GRAMMATICAL/DISCOURSE wording
    ("sekarang", "ganti ... menjadi", "bukan ... tapi", "actually", dual
    "dulu ... sekarang") - never on any specific entity/product/device
    name, so this works identically for a GPU, a microcontroller, an
    audio device, an aquascape setup, or a network configuration.

    Sprint 41 (Temporal Memory & Timeline Awareness) - a BARE "sekarang"
    match (no other, stronger correction phrase also present) is now
    additionally gated on the turn NOT being interrogative
    (`_is_interrogative()` above). Only this ephemeral-layer wrapper is
    affected - `_CORRECTION_RE.search()` is still called UNCHANGED,
    directly, by `_classify_conflict()` and `classify_query_intent()`
    elsewhere in this module (both PRE-existing, both unmodified), so
    this fix cannot affect the persistent `manual_memory` layer's own
    conflict detection or the query-intent taxonomy."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if _is_temporal_change(lowered):
        return True
    if _CORRECTION_RE_STRONG.search(lowered):
        return True
    if re.search(r'\bsekarang\b', lowered) and not _is_interrogative(lowered):
        return True
    return False


#: Categories where two DIFFERENT objects of the same shape of statement
#: are normal and non-exclusive - liking a guitar and liking a game are
#: not competing claims the way "my current OS" or "my current GPU" are.
#: Section 2's own NO_CONFLICT example ("Aku suka gitar." / "Aku suka
#: game.") is exactly this: same category, moderate token overlap
#: ("aku"/"suka"), no correction/temporal/subset signal - without this
#: category-aware fallback that pair would otherwise fall all the way
#: through to AMBIGUOUS_CONFLICT, which is wrong for bare preferences
#: (a person can like more than one thing at once; they cannot usually
#: run two different operating systems as their one "current" OS).
_NON_EXCLUSIVE_CATEGORIES = frozenset({"preference"})


def _classify_conflict(new_text, existing_text, category=None):
    """Returns one of:
        "no_conflict"          - different subject/context (or a
                                  non-exclusive category like preference
                                  with no other signal) - do not merge.
        "refinement_forward"   - new text is a superset of old (adds
                                  detail without contradicting) -> merge,
                                  new text becomes current.
        "refinement_backward"  - new text is a SUBSET of old (a terser
                                  restatement) -> do NOT discard old's
                                  extra detail; reinforce old instead.
        "correction"           - explicit correction wording -> merge,
                                  new text becomes current, old -> history.
        "temporal_change"      - dual "dulu ... sekarang" wording -> same
                                  mechanics as correction, distinct reason.
        "ambiguous_conflict"   - same-topic, contradictory, but no
                                  deterministic signal either way -> DO
                                  NOT merge, preserve both, flag both.
    Never uses `importance` or timestamps to decide (Steps 6/7: neither
    importance nor recency is truth) - purely textual signals plus the
    EXISTING category system (Section 6: reuse categories, don't
    duplicate them).

    Order matters: distinguishing-context (strongest "these are
    different things" signal) is checked first, then explicit
    correction/temporal wording (checked BEFORE the subset test
    deliberately - the digit-blind shared tokenizer means "RTX 3070 Ti"
    -> "sekarang RTX 3060 Ti" would otherwise ALSO satisfy the subset
    test, since the differing model numbers vanish as tokens entirely;
    checking correction wording first labels it correctly as a
    correction rather than a coincidental refinement), then the subset
    test, then the category-aware ambiguous fallback."""
    from .memory_retrieval.query import _WORD_RE

    new_tokens = set(w.lower() for w in _WORD_RE.findall(new_text or ""))
    existing_tokens = set(w.lower() for w in _WORD_RE.findall(existing_text or ""))

    if _has_distinguishing_context(new_tokens, existing_tokens):
        return "no_conflict"

    lowered = (new_text or "").lower()
    if _is_temporal_change(lowered):
        return "temporal_change"
    if _CORRECTION_RE.search(lowered):
        return "correction"

    if existing_tokens and existing_tokens.issubset(new_tokens):
        return "refinement_forward"
    if new_tokens and new_tokens.issubset(existing_tokens):
        return "refinement_backward"

    if category in _NON_EXCLUSIVE_CATEGORIES:
        return "no_conflict"
    return "ambiguous_conflict"


def _find_conflicting_memory(new_text, category):
    """Returns an existing entry to UPDATE-with-history, the string
    `"ambiguous"`, or `None` (genuinely new fact - no match). Only
    considers memories in the SAME category (a technical fact about a
    GPU should never get merged into an unrelated preference note) with
    Jaccard token overlap in `[_CONSOLIDATION_MIN, _CONSOLIDATION_MAX)` -
    reuses the SAME tokenizer `update_memory_by_topic()` already uses,
    not a second one. Step 10's explicit safety rule: two or more
    equally-good candidates -> `"ambiguous"` -> caller must NOT guess."""
    from .memory_retrieval.query import _WORD_RE

    new_tokens = set(w.lower() for w in _WORD_RE.findall(new_text or ""))
    if not new_tokens:
        return None

    candidates = []
    for m in _memories:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        if m.get("category") != category:
            continue
        existing_tokens = set(w.lower() for w in _WORD_RE.findall(m["text"]))
        if not existing_tokens:
            continue
        union = new_tokens | existing_tokens
        if not union:
            continue
        jaccard = len(new_tokens & existing_tokens) / len(union)
        if _CONSOLIDATION_MIN <= jaccard < _CONSOLIDATION_MAX:
            candidates.append((jaccard, m))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return "ambiguous"
    return candidates[0][1]


def _reinforce_existing_memory(entry):
    """A repeat/near-duplicate mention of an already-stored fact - Step
    5's "repeated mention" importance signal and Step 10's "duplicate
    memory doesn't multiply", applied together: no new entry, no text
    change, but the existing one gets a small importance bump (capped at
    4) and a freshness refresh (`updated_at`), so a fact the user keeps
    bringing up naturally trends toward higher importance / longer
    active lifecycle instead of decaying like a one-off mention."""
    entry["importance"] = min(4, _get_importance(entry) + 1)
    entry["updated_at"] = _now_iso()
    _save()


def _upgrade_existing_memory(entry, new_text):
    """Memory Conflict Resolution sprint - REFINEMENT via the
    PRE-EXISTING phase-1 near-duplicate check in `add_memory()`: the new
    text is a proper superset (the old text is literally a substring of
    it) - e.g. "Aku pakai Windows." -> "Aku pakai Windows 11 Pro."
    Section 2's own worked example for this case. Unlike plain
    reinforcement (which leaves the stored text untouched), this
    upgrades the entry to the MORE DETAILED wording instead of silently
    discarding the extra detail - while still preserving the old wording
    in `history` (reason="refinement") and still returning through
    `add_memory()`'s existing `None` contract (no new entry is created,
    same as the plain-reinforcement branch)."""
    old_text = entry["text"]
    history = entry.get("history")
    if not isinstance(history, list):
        history = []
    history.append({
        "text": old_text,
        "changed_at": entry.get("updated_at") or entry.get("created_at") or _now_iso(),
        "reason": "refinement",
    })
    entry["history"] = history[-_MAX_MEMORY_HISTORY_ENTRIES:]
    entry["text"] = new_text
    entry["category"] = _classify_memory_category(new_text)
    entry["importance"] = min(4, _get_importance(entry) + 1)
    entry["updated_at"] = _now_iso()
    _save()


def _tag_ambiguous_conflict(new_entry, existing_entry):
    """Memory Conflict Resolution sprint - the AMBIGUOUS_CONFLICT
    outcome: two same-topic, contradictory memories with no
    deterministic signal telling us which one is current. NEVER merges,
    NEVER deletes, NEVER guesses a winner (the sprint's own core
    principle) - both entries are tagged with a shared `conflict_group`
    id (reusing the existing entry's group if it was already flagged
    from an earlier ambiguous conflict, so a 3rd contradictory memory
    joins the SAME group rather than starting a disconnected one) and
    `conflict_status="ambiguous_conflict"`, then both are persisted via
    the same `_save()` every other writer in this module already uses."""
    group_id = existing_entry.get("conflict_group") or uuid.uuid4().hex[:8]
    existing_entry["conflict_status"] = "ambiguous_conflict"
    existing_entry["conflict_group"] = group_id
    new_entry["conflict_status"] = "ambiguous_conflict"
    new_entry["conflict_group"] = group_id
    # Memory Evaluation & Self-Calibration sprint - `conflict_event_count`
    # is evidence for `evaluate_memory()` below (negative evidence: "memory
    # conflict yang unresolved"), NOT a truth judgment about either side -
    # both entries accrue one event each, since both are equally "involved
    # in an unresolved conflict" from this point on. Additive, safe
    # `.get(...)`-default bookkeeping, same pattern every other evidence
    # counter in this file already uses.
    existing_entry["conflict_event_count"] = _get_conflict_event_count(existing_entry) + 1
    new_entry["conflict_event_count"] = _get_conflict_event_count(new_entry) + 1
    _save()


def add_memory(text, source="user_explicit"):
    """Simpan 1 fakta baru ke long-term memory. Return dict entry-nya
    (baru ATAU hasil consolidation/update), atau `None` kalau teks kosong
    ATAU fakta yang mirip persis sudah ada (cegah duplikat, penting buat
    mode auto-remember yang bisa manggil ini tiap giliran obrolan).

    Manual Memory Management sprint: `source` is an optional kwarg
    recording WHO decided this fact should be remembered - "user_explicit"
    for a literal "inget ya, ..." command (`main_runtime_demo.py`'s
    `_handle_explicit_memory_command`, this module's own primary caller),
    "llm_auto" for the OTHER existing caller (`luno/main.py`'s legacy
    `save_memory` tool). Also stamps `updated_at`/`category`/`schema_version` -
    all additive keys; every entry this module already produced
    (`id`/`text`/`created_at`) is completely unchanged in meaning or
    presence.

    Memory Intelligence & Importance Engine sprint (additive, layered on
    top of the above, in this exact order):
      1. Exact/near-exact substring dedup (UNCHANGED, pre-existing,
         checked first) - reinforces the existing entry, returns `None`
         exactly as before (existing tests assert this).
      2. Same-topic-different-wording/value consolidation (NEW) - only
         reached if (1) found nothing. Updates the matched entry (with
         history) instead of creating a contradicting second one. An
         "ambiguous" result (two+ equally-good matches) falls through to
         (3) rather than guessing which one to overwrite - Step 10's
         explicit rule.
      3. Genuinely new fact -> classify importance, create a new entry.

    Memory Conflict Resolution sprint (additive, layered inside step 2
    above): a single same-topic candidate is no longer merged
    unconditionally - `_classify_conflict()` first asks WHAT KIND of
    relationship this is:
      - "no_conflict" (different subject/context)      -> falls through
        to step 3, create as a genuinely separate fact.
      - "refinement_forward"/"correction"/"temporal_change" -> merges via
        `update_memory()` exactly as before, now tagged with a `reason`.
      - "refinement_backward" (new text is a terser restatement of a
        SUBSET of the old text) -> reinforces the OLD entry instead of
        overwriting it with less detail; returns `None` (same contract
        as the exact-dedup branch above - nothing new was learned).
      - "ambiguous_conflict" -> creates the new entry as usual, then
        flags BOTH entries via `_tag_ambiguous_conflict()` - never
        merges, never guesses, never deletes either one.
    """
    text = text.strip().rstrip(".!?")
    if not text:
        return None

    text_lower = text.lower()
    for m in _memories:
        existing_lower = m["text"].lower()
        if text_lower == existing_lower:
            _reinforce_existing_memory(m)
            print(f"[Memory] ~ Skipped (exact duplicate, reinforced): {text}")
            return None
        if existing_lower in text_lower:
            # New text is a proper superset of the existing wording - a
            # REFINEMENT (Section 2's "Aku pakai Windows." -> "Aku pakai
            # Windows 11 Pro." example) - upgrade to the more detailed
            # text instead of discarding it via plain reinforcement.
            _upgrade_existing_memory(m, text)
            print(f"[Memory] ~ Refined (more detail added, reinforced): {m['text']}")
            return None
        if text_lower in existing_lower:
            # New text is a terser restatement of a SUBSET of the
            # existing wording - keep the existing, more detailed text;
            # just reinforce it.
            _reinforce_existing_memory(m)
            print(f"[Memory] ~ Skipped (mirip sudah ada, reinforced): {text}")
            return None

    category = _classify_memory_category(text)

    conflict_target = _find_conflicting_memory(text, category)
    if conflict_target == "ambiguous":
        # Step 10: "Jika ambigu: DO NOT GUESS" - two or more equally-good
        # CANDIDATES (not yet a conflict-type question at all) -> fall
        # through to CREATE a genuinely new entry rather than risk
        # merging into the wrong one. Preserving all candidates untouched
        # is the safe choice. (Distinct from `AMBIGUOUS_CONFLICT` below,
        # which is about a SINGLE candidate whose conflict TYPE is
        # uncertain - two different kinds of "don't guess".)
        conflict_target = None

    # Set below only when the single candidate's conflict TYPE itself is
    # uncertain (`AMBIGUOUS_CONFLICT`) - the new entry still gets
    # created normally further down, then tagged together with this
    # target afterward, once it actually exists.
    pending_ambiguous_target = None

    if conflict_target is not None:
        conflict_type = _classify_conflict(text, conflict_target["text"], category)

        if conflict_type == "no_conflict":
            # Different subject/context (e.g. "PC" vs "server") - not
            # actually a candidate at all; fall through to CREATE.
            pass
        elif conflict_type == "refinement_backward":
            # New text is a terser restatement of a SUBSET of the old
            # text - reinforcing (not overwriting) preserves the old
            # entry's extra detail instead of discarding it.
            _reinforce_existing_memory(conflict_target)
            print(f"[Memory] ~ Skipped (refinement of existing, more detailed memory kept): {text}")
            return None
        elif conflict_type == "ambiguous_conflict":
            # Section 2/11's core case: same topic, contradictory, but no
            # deterministic signal either way. DO NOT merge, DO NOT
            # guess - fall through to CREATE a genuinely separate entry,
            # then flag BOTH once the new one exists (see below).
            pending_ambiguous_target = conflict_target
        else:
            # "refinement_forward" / "correction" / "temporal_change" -
            # all three mechanically merge via update_memory(), only the
            # recorded history `reason` differs.
            old_text = conflict_target["text"]
            updated = update_memory(conflict_target["id"], text, reason=conflict_type)
            if updated:
                print(f"[Memory] ~ Consolidated ({conflict_type}): {old_text!r} -> {text!r}")
                return updated
            # update_memory() only returns None for an empty new_text,
            # which cannot happen here (already validated above) -
            # defensive fallback to CREATE rather than silently losing
            # the fact.

    importance = _classify_memory_importance(text, category)
    now_iso = _now_iso()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "created_at": now_iso,
        "updated_at": now_iso,
        "category": category,
        "importance": importance,
        "history": [],
        "source": source,
        "schema_version": MANUAL_MEMORY_SCHEMA_VERSION,
    }
    _memories.append(entry)

    if pending_ambiguous_target is not None:
        _tag_ambiguous_conflict(entry, pending_ambiguous_target)
        print(f"[Memory] ⚠ Ambiguous conflict detected (both kept): "
              f"{pending_ambiguous_target['text']!r} vs {text!r}")
        return entry

    _save()
    print(f"[Memory] ✓ Remembered: {text}")
    return entry


def remove_memory(query_lower):
    """Hapus fakta yang cocok dengan query (substring dua arah, case-insensitive).
    Return list teks yang berhasil dihapus (kosong kalau tidak ada yang cocok)."""
    global _memories
    removed, kept = [], []
    for m in _memories:
        text_lower = m["text"].lower()
        if query_lower in text_lower or text_lower in query_lower:
            removed.append(m["text"])
        else:
            kept.append(m)
    if removed:
        _memories = kept
        _save()
        print(f"[Memory] ✓ Forgot: {', '.join(removed)}")
    return removed


def clear_all_long_term():
    global _memories
    count = len(_memories)
    _memories = []
    _save()
    print(f"[Memory] ✓ Cleared all {count} long-term memory item(s)")
    return count


def list_memories():
    return list(_memories)


# ─────────────────────────────────────────────
#  MANUAL MEMORY MANAGEMENT sprint - additive operations (get/update/
#  delete-by-id/search) on top of the SAME `_memories` store above. Every
#  function below only ever reads/mutates `_memories` and calls the SAME
#  `_save()` every existing writer already uses - no second persistence
#  path, no second file.
# ─────────────────────────────────────────────


def get_memory(memory_id):
    """Return the entry with this `id`, or `None`. Read-only, no side
    effects - the building block `update_memory()`/`delete_memory_by_id()`
    both use to confirm a real match before mutating anything."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            return dict(m)
    return None


def update_memory(memory_id, new_text, reason=None):
    """Explicit correction of an EXISTING entry, by id - Step 10's
    "update semantics": the user (or `update_memory_by_topic` below, once
    it has resolved a topic query to exactly one id) is IDENTIFYING which
    memory to change, not asking the LLM to guess. Re-classifies
    `category` from the new text (a correction can change what KIND of
    fact this is, e.g. a bare preference note upgraded to a specific
    technical fact) and stamps `updated_at` - `created_at`/`id` never
    change, so the entry's identity and original save time are preserved.
    Returns the updated entry, or `None` if `memory_id` doesn't exist or
    `new_text` is empty (never silently creates a new entry instead - a
    caller that truly wants a new fact should call `add_memory()`).

    Memory Intelligence sprint additions (Step 10/11 - conflict/history):
    if the new text actually differs from the old, the OLD wording is
    appended to a bounded `history` list (newest last, capped at
    `_MAX_MEMORY_HISTORY_ENTRIES`) before being overwritten, so a caller
    can reconstruct "dulu: X / sekarang: Y" instead of losing the prior
    state outright. Importance is recomputed from the new text but never
    allowed to DECREASE below whatever the entry already had - an update
    can only reinforce or raise confidence in a fact, never silently
    demote a previously-important memory just because its new wording
    happens to score lower (Step 8's "jangan menghapus/menurunkan
    proteksi memory eksplisit secara agresif").

    Memory Conflict Resolution sprint addition: `reason` is an OPTIONAL
    string (e.g. `"refinement"`/`"correction"`/`"temporal_change"`) -
    when given, it is stamped onto the history entry this call writes,
    so a later reader (or a "tampilkan konflik memory" command) can
    understand WHY the old wording was superseded, not just that it was.
    Direct callers that aren't going through conflict classification
    (e.g. `update_memory_by_topic()`, an explicit "ubah memory X jadi Y"
    command) simply omit it - the history entry then has no `reason` key
    at all, which every reader already treats as an absent/optional
    field."""
    new_text = (new_text or "").strip().rstrip(".!?")
    if not new_text:
        return None
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            old_text = m.get("text")
            if old_text and old_text != new_text:
                history = m.get("history")
                if not isinstance(history, list):
                    history = []
                history_entry = {
                    "text": old_text,
                    "changed_at": m.get("updated_at") or m.get("created_at") or _now_iso(),
                }
                if reason:
                    history_entry["reason"] = reason
                history.append(history_entry)
                m["history"] = history[-_MAX_MEMORY_HISTORY_ENTRIES:]
                # Memory Evaluation & Self-Calibration sprint - a
                # `reason="correction"` update is genuine negative evidence
                # about the PREVIOUS wording (Step 4's "correction" signal)
                # - counted here, at the one place every correction-shaped
                # update (both the automatic conflict-resolution path in
                # `add_memory()` and the new conversational correction path
                # in `main_runtime_demo.py`) already flows through, rather
                # than duplicated at each caller. Does NOT fire for
                # `reason` values that aren't literally `"correction"`
                # (`"refinement"`/`"temporal_change"`/`"dashboard_edit"`/
                # `None` are NOT corrections - a refinement adds detail, a
                # temporal change is an honest update, a dashboard edit is
                # an administrative fix, none of them mean "the old text
                # was wrong").
                if reason == "correction":
                    m["correction_count"] = _get_correction_count(m) + 1
            m["text"] = new_text
            m["updated_at"] = _now_iso()
            m["category"] = _classify_memory_category(new_text)
            new_importance = _classify_memory_importance(new_text, m["category"])
            m["importance"] = max(new_importance, _get_importance(m))
            _save()
            print(f"[Memory] ✓ Updated {memory_id}: {new_text}")
            return dict(m)
    return None


def update_memory_by_topic(topic_query, new_text):
    """Resolves a TOPIC ("GPU", "PC utamaku") to exactly one existing
    memory before updating it - the safety behavior Step 10 explicitly
    requires: "Do NOT automatically overwrite memories merely because the
    LLM thinks a new statement contradicts an old one... If ambiguity
    exists, prefer a safe response over destructive guessing." Ranks
    existing memories by how many of the topic query's tokens appear in
    their text (reuses `luno.memory_retrieval.query`'s own tokenizer, no
    second tokenizer). Returns `(status, entry_or_None)`:

        ("updated", entry)   - exactly one memory matched best -> updated.
        ("not_found", None)  - nothing in memory matches the topic at all.
        ("ambiguous", None)  - two or more memories tie for the best
                                match - refuses to guess which one the
                                user meant; caller should ask, not pick.
    """
    from .memory_retrieval.query import _WORD_RE  # local import: avoids a
    # module-load-order dependency for the rest of this file, which never
    # otherwise needs memory_retrieval - mirrors episodic_memory.py's own
    # "only import what THIS function needs" discipline.

    topic_tokens = set(w.lower() for w in _WORD_RE.findall(topic_query or ""))
    if not topic_tokens:
        return "not_found", None

    scored = []
    for m in _memories:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        text_tokens = set(w.lower() for w in _WORD_RE.findall(m["text"]))
        overlap = len(topic_tokens & text_tokens)
        if overlap > 0:
            scored.append((overlap, m))

    if not scored:
        return "not_found", None

    best_score = max(s for s, _ in scored)
    best_matches = [m for s, m in scored if s == best_score]
    if len(best_matches) > 1:
        return "ambiguous", None

    updated = update_memory(best_matches[0]["id"], new_text)
    return ("updated", updated) if updated else ("not_found", None)


def delete_memory_by_id(memory_id):
    """Delete exactly one memory by its stable `id` - Step 11's "hapus
    memory nomor 12" case. Returns the removed entry's `text`, or `None`
    if no memory has this id (never removes anything else, never affects
    unrelated memories - a single dict removed from the list by identity,
    same `_save()` every other writer here uses)."""
    global _memories
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            _memories = [x for x in _memories if x is not m]
            _save()
            print(f"[Memory] ✓ Deleted {memory_id}: {m['text']}")
            return m["text"]
    return None


# ─────────────────────────────────────────────
#  MEMORY CONFLICT RESOLUTION sprint (Section 16) - explicit, deterministic
#  conflict inspection/resolution. Deliberately narrow: only "show me the
#  conflicts" (read-only) and "X is the correct one" (topic-anchored,
#  refuses to guess) are implemented. "pakai memory yang terbaru" ("use
#  the newest one") and "hapus memory yang salah" ("delete the wrong
#  one") were considered and deliberately NOT implemented - both would
#  require GUESSING (which one is "newest" in the user's intended sense,
#  or which one is "wrong") without a concrete, explicit target, directly
#  contradicting Section 16's own "ambiguous commands must not guess"
#  rule and Section 17's "the LLM must never be the final authority for
#  silently changing persistent memory". A user who wants a specific
#  side to win can always say which one via `resolve_conflict_by_topic()`
#  below instead.
# ─────────────────────────────────────────────


def list_conflicts():
    """Read-only - every entry currently flagged
    `conflict_status == "ambiguous_conflict"`, grouped by
    `conflict_group` (so the two/more sides of the SAME contradiction
    are returned together). Returns a list of groups (each group a list
    of entry dicts) - never mutates anything, safe to call as often as
    wanted."""
    groups = {}
    for m in _memories:
        if isinstance(m, dict) and m.get("conflict_status") == "ambiguous_conflict":
            groups.setdefault(m.get("conflict_group"), []).append(dict(m))
    return list(groups.values())


def resolve_conflict_by_topic(topic_query):
    """Section 16's "memory X yang benar" ("memory X is the correct
    one") - explicit user resolution of an AMBIGUOUS_CONFLICT. Same
    "identify, don't guess" safety pattern as `update_memory_by_topic()`
    above: ranks every currently-ambiguous entry by how many of the
    topic query's tokens appear in its text; only proceeds if EXACTLY
    ONE entry has the best score. That entry becomes the survivor - its
    conflict siblings (same `conflict_group`) are merged into its
    `history` (reason="user_resolved_conflict", so it's clear a person,
    not a heuristic, made this call) and removed as separate top-level
    entries; the survivor's `conflict_status`/`conflict_group` are
    cleared (it is no longer in an unresolved state). Returns
    `(status, entry_or_None)`:

        ("resolved", entry)  - exactly one ambiguous entry matched the
                                topic best -> it is now the sole,
                                current, non-conflicted entry.
        ("not_found", None)  - nothing currently ambiguous matches the
                                topic at all.
        ("ambiguous", None)  - two or more ambiguous entries tie for the
                                best match - refuses to guess which one
                                the user means is "correct"."""
    global _memories
    from .memory_retrieval.query import _WORD_RE

    topic_tokens = set(w.lower() for w in _WORD_RE.findall(topic_query or ""))
    if not topic_tokens:
        return "not_found", None

    candidates = [m for m in _memories if isinstance(m, dict) and m.get("conflict_status") == "ambiguous_conflict"]
    if not candidates:
        return "not_found", None

    scored = []
    for m in candidates:
        text_tokens = set(w.lower() for w in _WORD_RE.findall(m.get("text", "")))
        overlap = len(topic_tokens & text_tokens)
        if overlap > 0:
            scored.append((overlap, m))
    if not scored:
        return "not_found", None

    best_score = max(s for s, _ in scored)
    best_matches = [m for s, m in scored if s == best_score]
    if len(best_matches) > 1:
        return "ambiguous", None

    survivor = best_matches[0]
    group_id = survivor.get("conflict_group")
    losers = [
        m for m in _memories
        if isinstance(m, dict) and m is not survivor
        and m.get("conflict_group") == group_id
        and m.get("conflict_status") == "ambiguous_conflict"
    ]
    if not losers:
        # Defensive - a conflict_group with only one remaining member is
        # not really an unresolved conflict anymore; nothing to resolve.
        return "not_found", None

    changed_at = _now_iso()
    history = survivor.get("history")
    if not isinstance(history, list):
        history = []
    for loser in losers:
        history.append({"text": loser["text"], "changed_at": changed_at, "reason": "user_resolved_conflict"})
    survivor["history"] = history[-_MAX_MEMORY_HISTORY_ENTRIES:]
    survivor.pop("conflict_status", None)
    survivor.pop("conflict_group", None)
    survivor["updated_at"] = changed_at

    loser_ids = {loser["id"] for loser in losers}
    _memories = [m for m in _memories if not (isinstance(m, dict) and m.get("id") in loser_ids)]
    _save()
    print(f"[Memory] ✓ Conflict resolved: {survivor['text']!r} confirmed correct, "
          f"{len(losers)} superseded side(s) merged into history")
    return "resolved", dict(survivor)


#: Memory Conflict Resolution sprint (Section 9/13) - a small, fixed
#: word list marking a query as asking about a PAST state rather than
#: the current one ("GPU yang DULU pernah aku pakai apa?" vs "GPU ku
#: SEKARANG apa?"). Deliberately reuses the same "short, maintainable
#: word list" discipline as `_CORRECTION_RE`/`_TEMPORAL_OLD_MARKERS`
#: above, not a second temporal parser - in fact the OLD-side markers
#: are literally the same set (`_TEMPORAL_OLD_MARKERS`), plus a couple
#: of query-specific phrasings ("yang lama"/"yang dulu") that make sense
#: as a QUESTION but not as a statement.
#: Sprint 40 (Memory Confidence & Conflict Resolution) - "sebelumnya"
#: ("previously"/"before") added. A very common, generic Indonesian way
#: to ask about a prior state ("yang sebelumnya pakai apa?") that was
#: missing from this list - reproduced live via this sprint's own
#: Scenario C ("Sebelumnya saya pakai X." -> "Sekarang Y." -> "Yang
#: sebelumnya pakai apa?"). Purely a wording addition to the EXISTING
#: marker list, not a new detector.
_HISTORICAL_QUERY_MARKERS = _TEMPORAL_OLD_MARKERS + ("yang lama", "yang dulu", "pernah", "sebelumnya")


def _is_historical_query(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _HISTORICAL_QUERY_MARKERS)


def is_historical_query(text):
    """Memory Context Assembly sprint - public wrapper around the private
    `_is_historical_query()` established by the Memory Conflict Resolution
    sprint. Added because the new `luno.memory_context` module needs the
    SAME historical-query detector `make_manual_memory_source()`/
    `_select_memories_for_prompt()` already use (Step 12: "reuse existing
    historical-query detection", not a second word list) - the check is a
    plain, deterministic delegation, nothing recomputed or reinterpreted."""
    return _is_historical_query(text)


# ─────────────────────────────────────────────
#  TEMPORAL MEMORY & TIMELINE AWARENESS (Sprint 41)
# ─────────────────────────────────────────────
#
# Phase 0 found the ephemeral `_active_topic`/`_topic_history` layer
# already has a two-value temporal axis (`ActiveTopicSnapshot.status`,
# "active"/"superseded", Sprint 40) but nothing for PLANNED ("minggu
# depan aku mau ganti ke X") or COMPLETED ("sudah aku pindah ke X")
# facts - live reproduction (Scenarios C/D/E) confirmed a planned/
# completed/cancelled statement is pushed as an ordinary "active" entry
# indistinguishable from a genuinely current fact, and a cancelled plan
# renders identically to a still-active one. Same discipline as every
# other detector in this module: small, fixed, bounded marker lists -
# no LLM, no embeddings, domain-generic wording only.
#
# PLANNED time-markers reuse `_TEMPORARY_WORDING_RE`'s own future-time
# vocabulary as their base (besok/lusa/minggu ini/minggu depan/sebentar
# lagi/nanti) - not a second future-time word list - plus a couple of
# explicit planning-language additions this sprint's own Scenario C/D/E
# needed ("bulan depan", "rencana").
_PLANNED_TIME_MARKERS = (
    "besok", "lusa", "minggu ini", "minggu depan", "bulan depan",
    "sebentar lagi", "nanti", "tomorrow", "next week", "next month",
)
_PLANNED_INTENT_MARKERS = ("rencana", "berencana", "roadmap", "planning to")

#: A BARE "mau"/"akan"/"bakal" is deliberately NOT by itself a PLANNED
#: trigger - Sprint 39's own established caution (`_TOPIC_OVERLAP_
#: STOPWORDS`'s own comment in `luno.memory_context`) already found
#: "aku mau ..." is common enough phrasing ("aku mau tanya", "aku mau
#: tau") to appear regardless of whether a plan is being stated. Only
#: counts as PLANNED when paired with a change-shaped verb (ganti/
#: pindah/upgrade/beli/ubah/migrasi) in the same clause - the same
#: "verb ... target" combo discipline `_CORRECTION_RE`'s own
#: "ganti ... menjadi" alternative already uses.
_PLANNED_INTENT_VERB_RE = re.compile(
    r'\b(?:mau|akan|bakal)\b.{0,25}\b(?:ganti|pindah|upgrade|beli|ubah|migrasi)\b',
    re.IGNORECASE,
)

#: COMPLETED markers - "this already happened" wording. Deliberately
#: does not include "baru" alone (too generic - "baru beli laptop baru"
#: is ambiguous) - only the fixed "baru saja" phrase.
_COMPLETED_MARKERS = ("sudah", "udah", "telah", "selesai", "baru saja", "already", "just finished")

#: CANCELLED markers - an explicit call-off of a previously stated plan.
#: Reproduced live (Scenario E: "Jadi beli RTX 5070 batal.") - without
#: this, a cancelled plan rendered identically to a still-active one,
#: giving the LLM no signal the purchase was called off.
_CANCELLED_MARKERS = (
    "batal", "dibatalkan", "gak jadi", "nggak jadi", "ga jadi", "tidak jadi",
    "cancel", "cancelled",
)


def classify_temporal_status(text):
    """Sprint 41 - deterministic, bounded classifier for what temporal
    role THIS turn's own statement plays: `"cancelled"`, `"completed"`,
    `"planned"`, or `"none"` (no temporal-state signal - the overwhelming
    majority of ordinary turns, including plain CURRENT statements like
    "ESP32 saya pakai INMP441." or "Sekarang aku pakai RTX 4070." - see
    `update_topic_history()`'s own docstring for why CURRENT itself does
    NOT need a distinct return value here: a `"none"` classification is
    treated as an ordinary rich turn, exactly like every turn before this
    sprint, which is already correctly rendered "active"/current).

    Checked in a fixed precedence order (cancelled > completed > planned)
    since a real turn is expected to carry at most one of these signals -
    if multiple somehow matched, calling off a plan is the most
    consequential, hence highest precedence, followed by completion, then
    a future plan. Never returns anything but one of these four literal
    strings - callers must never guess.

    Deliberately domain-generic: matches on GRAMMATICAL/DISCOURSE wording
    only, never any specific entity/product/device name - identical
    behavior for a GPU, a microcontroller, an audio device, an aquascape
    setup, or a network configuration."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return "none"
    if any(m in lowered for m in _CANCELLED_MARKERS):
        return "cancelled"
    if any(m in lowered for m in _COMPLETED_MARKERS):
        return "completed"
    if any(m in lowered for m in _PLANNED_TIME_MARKERS) or any(m in lowered for m in _PLANNED_INTENT_MARKERS) \
            or _PLANNED_INTENT_VERB_RE.search(lowered):
        return "planned"
    return "none"


def is_historical_statement(text):
    """Sprint 41 - `True` when a STATEMENT clause (not a question) itself
    carries historical/past wording ("Aku dulu pakai GTX 1070.", "Dulunya
    saya pakai ESP8266."). Mirrors the STATEMENT/QUERY split
    `is_historical_query()` already establishes for questions - this is
    its statement-shaped counterpart, reused ONLY by
    `memory_context.update_topic_history()`'s compound-clause split (see
    that function's own docstring) to tag an individual CLAUSE within a
    single multi-fact turn as historical, distinct from
    `classify_temporal_status()` above (which only ever returns
    cancelled/completed/planned/none - CURRENT and HISTORICAL statements
    both fall through as `"none"` there, by design, since a whole-turn
    dispatch has no notion of "this clause is old, that other clause is
    new"). Reuses `_TEMPORAL_OLD_MARKERS` directly - the SAME word list
    `_is_temporal_change()` and `_HISTORICAL_QUERY_MARKERS` already use,
    not a new one."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(m in lowered for m in _TEMPORAL_OLD_MARKERS)


#: CURRENT-state QUERY markers (distinct from `classify_temporal_status()`
#: above, which classifies a STATEMENT's own temporal role - this
#: classifies a QUESTION's temporal intent instead, mirroring
#: `is_historical_query()`'s own existing statement/query split).
_CURRENT_STATE_QUERY_MARKERS = ("sekarang", "saat ini", "currently", "right now")


def is_current_state_query(text):
    """Sprint 41 - `True` only for a QUESTION asking about the CURRENT
    state ("Sekarang aku pakai GPU apa?", "Saat ini board-nya apa?").
    Requires BOTH a current-state marker AND interrogative shape
    (`_is_interrogative()`) - a bare "sekarang" in a declarative
    STATEMENT ("Sekarang aku pakai RTX 4070.") is not a query at all, it
    is the new current fact itself, handled entirely by the existing
    rich-turn REPLACE path, not this function."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(m in lowered for m in _CURRENT_STATE_QUERY_MARKERS) and _is_interrogative(lowered)


#: PLANNED-state QUERY markers - reuses `_PLANNED_INTENT_MARKERS` above
#: (the SAME "rencana"/"berencana" vocabulary a PLANNED statement uses)
#: plus "yang mau"/"akan"/"bakal" as query-shaped phrasings ("GPU yang
#: mau aku beli apa?", "Rencana upgrade ke apa?").
_PLANNED_QUERY_MARKERS = _PLANNED_INTENT_MARKERS + ("yang mau", "akan", "bakal")


def is_planned_query(text):
    """Sprint 41 - `True` only for a QUESTION asking about a PLANNED
    (not-yet-current) state. Same interrogative-shape requirement as
    `is_current_state_query()` above, for the same reason."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(m in lowered for m in _PLANNED_QUERY_MARKERS) and _is_interrogative(lowered)


def search_memories(query_text, limit=5):
    """Bounded, deterministic keyword search over manual memories - Step
    6's required `search_manual_memories(...)` operation. Reuses the SAME
    tokenizer `luno.memory_retrieval.query` already uses (via
    `token_overlap`) rather than a second matching implementation; ranked
    by overlap count (most matching tokens first), ties broken by
    recency (`updated_at`, newest first) so results are reproducible
    across calls. This is also exactly the matching logic
    `make_manual_memory_source()` below uses for retrieval-based recall -
    two entry points, one algorithm, per this sprint's own "do not
    duplicate retrieval logic" instruction.

    Memory Conflict Resolution sprint (Section 13): when `query_text`
    itself carries historical-intent wording (`_is_historical_query()`),
    each entry's `history` list is ALSO searched for token overlap - a
    superseded value ("RTX 3070 Ti", now living only in `history` after
    a correction/temporal merge) remains genuinely findable, never
    silently gone, without needing a second top-level entry to keep it
    alive. Historical matches are distinguished by an added
    `"historical": True` key and `"changed_at"` timestamp on the
    returned dict - `text` is the OLD wording, not the entry's current
    one - so a caller can tell current-state and historical results
    apart without guessing from content alone."""
    from .memory_retrieval.query import _WORD_RE

    query_tokens = [w.lower() for w in _WORD_RE.findall(query_text or "")]
    if not query_tokens:
        return []

    historical_query = _is_historical_query(query_text)

    scored = []
    for m in _memories:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        text_words = set(_WORD_RE.findall(m["text"].lower()))
        overlap = sum(1 for t in query_tokens if t in text_words)
        if overlap > 0:
            scored.append((overlap, m.get("updated_at") or m.get("created_at") or "", dict(m)))

        if historical_query and isinstance(m.get("history"), list):
            for h in m["history"]:
                if not isinstance(h, dict) or not h.get("text"):
                    continue
                hist_words = set(_WORD_RE.findall(h["text"].lower()))
                hist_overlap = sum(1 for t in query_tokens if t in hist_words)
                if hist_overlap > 0:
                    historical_entry = dict(m)
                    historical_entry["text"] = h["text"]
                    historical_entry["historical"] = True
                    historical_entry["changed_at"] = h.get("changed_at")
                    scored.append((hist_overlap, h.get("changed_at") or "", historical_entry))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in scored[:max(0, limit)]]


#: Memory Prompt Intelligence sprint - the SAME score shape
#: `make_manual_memory_source()` already uses for its own retrieval-based
#: ranking (Section 7/13's "reuse existing... don't invent a second
#: relevance algorithm"), reused verbatim here rather than re-derived, so
#: the two memory-note paths agree on what "more important" means.
def _score_memory_for_prompt(entry):
    score = 0.6 + _get_importance(entry) * 0.05
    if entry.get("source") == "user_explicit":
        score += 0.05
    if compute_lifecycle(entry) == "stale":
        score -= 0.15
    # Memory Learning & Feedback Loop sprint (Section 11) - usefulness is
    # ONLY ever a ranking signal AFTER relevance/lifecycle/conflict/
    # importance already applied above, and deliberately weighted far
    # smaller than a single importance point (±0.025 max swing here vs.
    # 0.05 per importance level) so it can only ever break a near-tie
    # among candidates already at the same importance band - it can never
    # flip an importance-level difference, let alone the relevance gate
    # this function's caller already applied before this score is ever
    # computed.
    score += (_get_usefulness(entry) - _DEFAULT_USEFULNESS_SCORE) * 0.05
    return score


def group_ambiguous_conflict_entries(entries):
    """Memory Context Assembly sprint - the grouping-by-`conflict_group`
    logic `_select_memories_for_prompt()` already used inline, factored out
    into its own public function so the new `luno.memory_context` module can
    reuse it verbatim (Step 8/11: "use existing conflict-resolution
    semantics", not a second grouping implementation) instead of
    reimplementing the same `conflict_status == "ambiguous_conflict"` /
    `conflict_group` coercion dance a second time. Pure, read-only, no
    filtering by relevance or lifecycle - callers apply their own gates
    first (both existing callers already excluded `archived` entries before
    calling this).

    Returns `{group_key: [entry, ...]}` for every entry whose
    `conflict_status == "ambiguous_conflict"`; entries without that status
    are simply not included (callers collect those separately)."""
    groups = {}
    for m in entries:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        if m.get("conflict_status") != "ambiguous_conflict":
            continue
        raw_group = m.get("conflict_group")
        # A malformed/hand-edited conflict_group (e.g. a dict) is not
        # hashable - coerce defensively so a corrupted entry can never
        # crash prompt generation (Step 15's "malformed entries must fail
        # safely").
        group_key = raw_group if isinstance(raw_group, (str, int)) else str(raw_group)
        groups.setdefault(group_key, []).append(m)
    return groups


def _select_memories_for_prompt(query_text):
    """Memory Prompt Intelligence sprint - importance/lifecycle/relevance/
    conflict-aware selection for `build_memory_prompt(query_text=...)`.
    Read-only: never mutates `_memories`, never calls `_save()` (prompt
    selection must never be confused with persistence - the stored data
    is the source of truth, this only decides what to SHOW this turn).

    Reuses EXISTING infrastructure only - no second tokenizer, no second
    importance scale, no second budget system:
      - `luno.memory_retrieval.query.analyze_query()`/`token_overlap()` -
        the SAME relevance gate `make_manual_memory_source()` already
        uses.
      - `compute_lifecycle()`/`_get_importance()` - the SAME lifecycle/
        importance functions the Memory Intelligence sprint established.
      - `_is_historical_query()` - the SAME historical-query detector the
        Memory Conflict Resolution sprint established.
      - `MemoryRetrievalConfig.from_env()` - the SAME `MAX_MEMORY_RESULTS`/
        `MAX_MEMORY_TOKENS` env-configurable budget the MemoryRetriever
        pipeline already reads - no new env var.

    Selection order:
      1. No retrieval signal at all in `query_text` -> select nothing,
         matching `MemoryRetriever.retrieve_memories()`'s own "don't even
         query the store" rule for signal-less turns.
      2. `lifecycle() == "archived"` entries are excluded from ordinary
         selection - still fully intact and reachable via
         `search_memories()`/`list_memories()`/`get_memory()` directly,
         this only affects what's ambiently shown this turn.
      3. Relevance (token overlap against the query) is REQUIRED before
         importance/lifecycle ever influence ranking - an irrelevant
         importance=4 memory never enters the candidate pool at all, so
         importance can never override relevance (the sprint's hard
         guarantee).
      4. AMBIGUOUS_CONFLICT entries are grouped by `conflict_group`; if
         ANY member is relevant, the WHOLE group is surfaced together as
         ONE explicitly-hedged note naming both sides - never picked
         apart into a single "winning" fact, never silently resolved.
      5. A historical-shaped query (`_is_historical_query()`) additionally
         searches each entry's bounded `history[]` for relevant superseded
         values, labeled as historical - an ordinary current-state query
         never sees `history` at all, so current values always win for
         current questions by construction.
      6. Remaining candidates are ranked by `_score_memory_for_prompt()`,
         then bounded by `MemoryRetrievalConfig`'s existing budget (same
         rough `len(text)//4` token estimate `MemoryRetriever.
         _estimate_tokens()` already uses)."""
    from .memory_retrieval.query import analyze_query, token_overlap
    from .memory_retrieval.models import MemoryRetrievalConfig

    query = analyze_query(query_text)
    if not query.has_any_signal:
        return []

    historical_query = _is_historical_query(query_text)
    budget = MemoryRetrievalConfig.from_env()

    live_entries = [
        m for m in _memories
        if isinstance(m, dict) and m.get("text") and compute_lifecycle(m) != "archived"
    ]
    conflict_groups = group_ambiguous_conflict_entries(live_entries)
    ordinary = [m for m in live_entries if m.get("conflict_status") != "ambiguous_conflict"]
    hist_candidates = []  # (score, tie_break, text)

    for m in live_entries:
        if historical_query and isinstance(m.get("history"), list):
            for h in m["history"]:
                if not isinstance(h, dict) or not h.get("text"):
                    continue
                if not token_overlap(query.tokens, h["text"]):
                    continue
                hist_candidates.append((
                    _score_memory_for_prompt(m) - 0.5,
                    f"hist:{m.get('id', '')}:{h.get('changed_at', '')}",
                    f"The user previously said (later superseded): {h['text']}",
                ))

    candidates = []
    seen_texts = set()
    for m in ordinary:
        if not token_overlap(query.tokens, m["text"]):
            continue
        norm = m["text"].strip().lower()
        if norm in seen_texts:
            continue
        seen_texts.add(norm)
        candidates.append((_score_memory_for_prompt(m), m.get("id", ""), m["text"]))

    for group_key, members in conflict_groups.items():
        if not any(token_overlap(query.tokens, m["text"]) for m in members):
            continue
        sides = " vs. ".join(f'"{m["text"]}"' for m in members)
        note = (
            f"The user has given conflicting, unresolved information here: {sides}. "
            "Don't present either as certain - ask them which is currently correct if it matters."
        )
        candidates.append((max(_score_memory_for_prompt(m) for m in members), f"conflict:{group_key}", note))

    candidates.extend(hist_candidates)
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)

    selected = []
    used_tokens = 0
    for _, _, text in candidates:
        if len(selected) >= budget.max_results:
            break
        est_tokens = max(1, len(text) // 4)
        if used_tokens + est_tokens > budget.max_tokens:
            break
        selected.append(text)
        used_tokens += est_tokens

    return selected


def build_memory_prompt(query_text=None):
    """Format long-term memory jadi 1 kalimat instruksi buat disisipkan ke system
    prompt GPT. Return string kosong kalau belum ada memory tersimpan.

    Regression & Architecture Guard sprint: `_load()` reads whatever
    valid JSON is in `config/long_term_memory.json` without validating
    its SHAPE (a syntactically-valid but structurally-wrong file, e.g.
    a JSON object instead of a list, used to crash right here with a
    bare `TypeError` the moment this ran - `_load()` itself never
    raised, so the earlier "loader fails safe" guarantee alone wasn't
    enough). Entries that aren't a dict with a "text" key are skipped
    rather than crashing the whole prompt - well-formed data (every
    entry `add_memory()` itself ever produces) is completely unaffected;
    this only changes behavior for a hand-corrupted file.

    Memory Prompt Intelligence sprint: `query_text` is an OPTIONAL kwarg.
    Omitted (every caller before this sprint, plus `luno/main.py`'s
    legacy `build_system_prompt()` and the explicit "recall everything"
    use case): behavior is COMPLETELY UNCHANGED from before this sprint -
    every stored fact, unconditionally, in one dump. This is what keeps
    `tests/test_manual_memory.py::test_recall_everything_full_list_still_works_unchanged`
    (an existing, protected test asserting exactly this "full list,
    unconditional" behavior) passing untouched, along with every
    `tests/test_memory_regression.py` call site.

    When a caller DOES supply the current turn's text (see
    `main_runtime_demo.py`'s `PlannerBridgeModule._handle_utterance()`,
    the one production call site this sprint updates), selection instead
    goes through `_select_memories_for_prompt()` - importance/lifecycle/
    relevance/conflict-aware, bounded by the existing
    `MemoryRetrievalConfig` budget, never a blind dump. See that helper's
    own docstring for the full selection policy. This does NOT replace
    `MemoryRetriever`/`make_manual_memory_source()` (the OTHER memory note
    already built earlier in the same method) - it makes this SEPARATE,
    direct prompt path obey the same intelligence rules that source
    already established, per this sprint's own scope.
    """
    if not query_text:
        facts = [m["text"] for m in _memories if isinstance(m, dict) and "text" in m]
        if not facts:
            return ""
        return (
            f"Things you know about the user from long-term memory (past sessions): {'; '.join(facts)}. "
            "Use this naturally when relevant, don't just recite the list back verbatim."
        )

    facts = _select_memories_for_prompt(query_text)
    if not facts:
        return ""
    # Strip a trailing period before joining - unlike raw `_memories` text
    # (already stripped of trailing punctuation by `add_memory()`), a
    # conflict-group note or historical label built above may end in a
    # full sentence; without this, joining it with the closing sentence
    # below would read "...if it matters.. Use this naturally...".
    facts = [f[:-1] if f.endswith(".") else f for f in facts]
    return (
        f"Things you know about the user from long-term memory, relevant to this conversation: {'; '.join(facts)}. "
        "Use this naturally when relevant, don't just recite the list back verbatim."
    )


# ─────────────────────────────────────────────
#  AUTO-REMEMBER (mode "kayak ChatGPT") — lewat OpenAI function calling
# ─────────────────────────────────────────────
#
# Beda dari detect_remember_command() di atas (yang cuma nangkep kalau user BILANG
# eksplisit "inget ya..."), tool ini dikasih ke GPT supaya DIA SENDIRI yang mutusin
# kapan ada fakta yang layak diinget jangka panjang dari obrolan biasa — persis
# mekanisme fitur Memory bawaan ChatGPT (function/tool call, bukan tebak-tebak teks).

MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "Save a specific, durable FACT ABOUT THE USER (their identity/preferences/life) to "
            "long-term memory — NOT a summary of what just happened in this conversation. Call "
            "this SILENTLY, and ONLY for things like: preferences, allergies, names of people/"
            "pets, routines, ongoing projects, important dates. "
            "GOOD examples: 'User is allergic to peanuts.' / 'User's dog is named Max.' / "
            "'User is building a RAG project for work.' "
            "NEVER call this for: small talk, one-off questions, compliments/flirting exchanges, "
            "device commands, or a narrated recap of the conversation itself (e.g. NEVER 'User "
            "asked if Luno is pretty, then turned on the RGB strip' — that's an event log, not "
            "a fact, and it's not your job to record it here; session summaries handle that "
            "automatically elsewhere). If in doubt whether something is a lasting fact vs. just "
            "part of the conversation, don't call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact to remember, written as one short standalone sentence, e.g. 'User is allergic to peanuts.'",
                }
            },
            "required": ["fact"],
        },
    },
}


# ─────────────────────────────────────────────
#  RINGKASAN SESI OBROLAN (session summary)
# ─────────────────────────────────────────────
#
# BEDA dari long-term memory fakta di atas: ini nyimpen TOPIK/ISI obrolan (mis.
# "kemarin bahas soal cara kerja lubang hitam"), bukan fakta permanen soal user.
# Dipicu otomatis saat Luno ditutup ('exit' / Ctrl+C — lihat main.py bagian akhir),
# atau bisa juga manual kapan aja lewat perintah "rangkum obrolan ini".

_session_summaries = []  # list of {"id", "summary", "turn_count", "ended_at"}


def _load_session_summaries():
    """Persistent State Hardening V2 sprint: now loaded via
    `luno.persistence.safe_load_json()` - same missing/malformed
    fallback to `[]` as before, same log lines as before (kept here,
    domain-side, rather than inside the generic helper, so the exact
    "[Memory] ..." wording this module has always used is unchanged)."""
    global _session_summaries
    existed = os.path.exists(config.SESSION_SUMMARIES_FILE)
    data, source = persistence.safe_load_json(
        config.SESSION_SUMMARIES_FILE, default=[], validate=lambda d: isinstance(d, list),
    )
    _session_summaries = data
    if existed and source == "primary":
        print(f"[Memory] ✓ Loaded {len(_session_summaries)} session summary(ies)")
    elif existed and source == "default":
        print(f"[Memory] ✗ Failed to load {config.SESSION_SUMMARIES_FILE}")


def _save_session_summaries():
    """Persistent State Hardening V2 sprint: now written via
    `luno.persistence.atomic_write_json()` - backup-before-write +
    temp-file + fsync + `os.replace()`, replacing the previous naive
    `open(path,"w")` direct write (the one store in this project that
    had ZERO atomicity before this sprint). Failure is still caught and
    logged, never raised - a persistence failure must never break the
    turn that triggered it, same convention every other store in this
    project follows."""
    try:
        persistence.atomic_write_json(config.SESSION_SUMMARIES_FILE, _session_summaries)
    except Exception as ex:
        print(f"[Memory] ✗ Failed to save {config.SESSION_SUMMARIES_FILE}: {ex}")


_load_session_summaries()


def summarize_and_archive_session(openai_client, model=None):
    """Rangkum session_log (SEMUA obrolan sesi berjalan ini) jadi 1-3 kalimat lewat
    GPT, simpan ke session_summaries.json, lalu kosongkan session_log. Dipanggil
    otomatis saat Luno ditutup, atau manual lewat perintah "rangkum obrolan ini".
    Return teks ringkasannya, atau None kalau belum ada obrolan/gagal.

    `openai_client` accepts EITHER shape, detected via duck typing (no import
    of either concrete client type from this module - see module docstring's
    own "no cross-package imports" convention):
      - legacy: an `openai` SDK client (`.chat.completions.create(...)`
        returning `.choices[0].message.content`) - what `luno/main.py` and
        `legacy_main.py` have always passed here.
      - new pipeline: `luno.adapters.openrouter`'s real client
        (`.chat_completion(model=..., messages=..., max_tokens=...)`
        returning an object with a `.text` attribute) - what
        `main_runtime_demo.py`'s `PlannerBridgeModule` passes when a real
        OpenRouter adapter is wired in (see `register_session_summary_client()`
        in `luno/bootstrap/adapters.py`).
    `model` is only meaningful for the new-pipeline shape (the legacy branch
    keeps its own historical "gpt-4o-mini" default unchanged, since that's an
    OpenAI-specific model id that would never resolve on an OpenRouter/
    Deepseek-style backend)."""
    if len(session_log) < 2:
        return None  # belum ada obrolan berarti buat dirangkum

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session_log)
    prompt = (
        "Summarize the key topics discussed in this conversation in 1-3 short sentences. "
        "Focus on WHAT was discussed (topics, questions, decisions) — not a transcript, "
        "not who-said-what. Write it so it's useful later for recalling 'what did we talk "
        f"about last time'.\n\nConversation:\n{transcript}"
    )
    try:
        if hasattr(openai_client, "chat_completion"):
            response = openai_client.chat_completion(
                model=model or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
            )
            summary = (getattr(response, "text", None) or "").strip()
        else:
            # Sprint 53 - same completion-token-parameter-name
            # compatibility issue as the new-pipeline branch above
            # (see `luno/adapters/openrouter.py::_payload()`'s own
            # Sprint 53 comment), but this legacy raw-`openai`-SDK
            # branch sends its kwargs straight to the API as the wire
            # parameter name, so it needs the same `config.
            # MAX_TOKENS_PARAM` abstraction `luno/main.py`'s legacy
            # OpenAI-SDK call sites already use for exactly this -
            # mirrors that exact `**{config.MAX_TOKENS_PARAM: ...}`
            # pattern instead of hardcoding either literal name here.
            res = openai_client.chat.completions.create(
                model=model or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                **{config.MAX_TOKENS_PARAM: 150},
            )
            summary = (res.choices[0].message.content or "").strip()
    except Exception as ex:
        print(f"[Memory] ✗ Session summary error: {ex}")
        return None

    if not summary:
        return None

    entry = {
        "id": uuid.uuid4().hex[:8],
        "summary": summary,
        "turn_count": len(session_log) // 2,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
    }
    _session_summaries.append(entry)
    _save_session_summaries()
    print(f"[Memory] ✓ Session summary saved: {summary}")
    session_log.clear()
    return summary


def list_session_summaries(limit=None):
    items = list(_session_summaries)
    return items[-limit:] if limit else items


def build_session_summary_prompt(limit=5):
    """Format beberapa ringkasan sesi TERAKHIR jadi teks buat system prompt, supaya
    GPT bisa nyambungin obrolan lama ('kemarin kita bahas X') secara natural.

    Same shape-safety fix as `build_memory_prompt()` above - a
    structurally-wrong (but syntactically valid JSON) entry is skipped
    instead of crashing this function."""
    if not _session_summaries:
        return ""
    recent = _session_summaries[-limit:]
    lines = [
        f"({s['ended_at'][:10]}) {s['summary']}"
        for s in recent
        if isinstance(s, dict) and "ended_at" in s and "summary" in s
    ]
    if not lines:
        return ""
    return f"Summaries of what you discussed with the user in past sessions: {'; '.join(lines)}."


# ─────────────────────────────────────────────
#  DETEKSI PERINTAH MEMORY DARI TEKS ("inget ya...", "lupain kalau...", dst)
# ─────────────────────────────────────────────

# "Bersihkan SEMUANYA" — short-term DAN long-term sekaligus. Sengaja dipisah dari
# _CLEAR_SHORT_TERM_ONLY_PHRASES di bawah: frasa di sini menyebut "semua"/"everything"/
# "memory" secara umum, jadi paling aman diartikan "lupakan semua yang kamu tau",
# bukan cuma riwayat obrolan barusan.
_CLEAR_EVERYTHING_PHRASES = [
    "lupakan semua", "lupakan semua ingatan", "hapus semua memory", "hapus semua ingatan",
    "reset memory", "clear memory", "forget everything", "forget everything you know about me",
    "forget all memory", "wipe your memory",
]

# Cuma bersihkan riwayat OBROLAN saat ini (short-term) — fakta jangka panjang tetap aman.
_CLEAR_SHORT_TERM_ONLY_PHRASES = [
    "lupakan percakapan", "hapus percakapan", "reset percakapan", "lupakan obrolan ini",
    "forget our conversation", "forget this conversation", "clear this conversation",
    "reset this conversation",
]

_RECALL_PHRASES = [
    "apa yang kamu inget tentang aku", "apa yang kamu ingat tentang aku",
    "ingatan kamu tentang aku apa", "kamu inget apa aja tentang aku",
    "coba sebutin yang kamu inget tentang aku",
    "what do you remember about me", "what do you know about me",
]

_SESSION_RECALL_PHRASES = [
    "obrolan sebelumnya ngomongin apa", "kita pernah ngobrolin apa aja",
    "topik obrolan sebelumnya", "riwayat obrolan kita", "sesi sebelumnya ngomongin apa",
    "kita udah pernah bahas apa aja", "obrolan kita kemarin ngomongin apa",
    "what did we talk about before", "what have we discussed before",
    "what did we talk about last time", "past conversations we had",
]

_MANUAL_SUMMARIZE_PHRASES = [
    "rangkum obrolan ini", "ringkas obrolan ini", "rangkum sesi ini",
    "summarize this conversation", "summarize our chat", "summarize this session",
]

_REMEMBER_PATTERNS = [
    re.compile(r'^(?:tolong\s+)?ing(?:at|et)(?:kan|in|lah)?[,\s]+(?:ya[,\s]+)?(?:kalau\s+|bahwa\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^catat(?:kan)?[,\s]+(?:kalau\s+|bahwa\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^(?:please\s+)?remember[,\s]+(?:that\s+)?(.+)$', re.IGNORECASE),
]

# "ingetin AKU ..." / "ingatkan SAYA ..." = minta di-REMINDER (punya objek "aku/saya"
# langsung setelah kata kerjanya), BEDA dari "inget ya, ..." = fakta buat diinget
# jangka panjang (tanpa objek pronomina). Dicek duluan di detect_remember_command supaya
# permintaan reminder ("ingetin aku minum obat jam 8") tidak salah ke-capture sebagai
# fakta — biar jatuhnya ke GPT punya kesempatan manggil set_reminder tool (luno/reminders.py).
_REMINDER_INTENT_RE = re.compile(r'^(?:tolong\s+)?ing(?:at|et)(?:kan|in)\s+(?:aku|saya|gue|gw)\b', re.IGNORECASE)

_FORGET_FACT_PATTERNS = [
    re.compile(r'^lupa(?:kan|in)?[,\s]+(?:kalau\s+|bahwa\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^forget[,\s]+(?:that\s+)?(.+)$', re.IGNORECASE),
]


def is_clear_everything_command(user_lower):
    return any(p in user_lower for p in _CLEAR_EVERYTHING_PHRASES)


def is_clear_short_term_command(user_lower):
    return any(p in user_lower for p in _CLEAR_SHORT_TERM_ONLY_PHRASES)


def is_recall_command(user_lower):
    return any(p in user_lower for p in _RECALL_PHRASES)


def is_session_recall_command(user_lower):
    return any(p in user_lower for p in _SESSION_RECALL_PHRASES)


# ─────────────────────────────────────────────
#  Memory Retrieval & Decision Quality sprint - QUERY-INTENT TAXONOMY.
#
#  Confirmed gap from this sprint's own Phase 0 audit:
#  `classify_query_context_category()` reuses the six MANUAL_MEMORY_CATEGORIES
#  (a taxonomy built to classify STORED MEMORY CONTENT - "technical_fact",
#  "project_context", ...) as a proxy for the CURRENT TURN's intent. It is
#  too coarse for troubleshooting/planning/casual-conversation/continuation-
#  of-topic, which mostly collapse into "other". This function is a
#  SEPARATE, small, deterministic classifier for THAT specific gap - it
#  does not replace, wrap, or change `classify_query_context_category()` in
#  any way (both remain independent signals; nothing about context_evidence
#  changes here).
#
#  Deliberately NOT a second recall/historical detector: explicit recall
#  delegates straight to the EXISTING `is_recall_command()`/
#  `is_session_recall_command()`/`_is_historical_query()` (all defined
#  above) rather than re-implementing that detection. Deliberately NOT an
#  LLM call, embeddings, or a second tokenizer - plain keyword/regex
#  matching against the raw lowered text, same style as
#  `_classify_memory_category()` itself.
#
#  Precedence (checked in this exact order, first match wins - documented
#  per the sprint's own "document precedence clearly" requirement):
#    1. explicit_recall      - reuses existing recall/historical detectors
#    2. correction_update    - reuses the EXISTING `_CORRECTION_RE` (the
#                               same regex `_classify_conflict()` itself
#                               already keys off of)
#    3. continuation_of_topic
#    4. troubleshooting
#    5. planning
#    6. casual_conversation
#    7. other                - safe fallback; identical to "no classification
#                               at all" for every downstream consumer (see
#                               `memory_context._apply_decision_quality_bonus()`,
#                               which treats "other"/`None` as a 0.0-bonus
#                               no-op)
#  An "other" (or ambiguous) result changes NOTHING about existing
#  retrieval/ranking behavior - callers written before this sprint that
#  never call this function at all get the EXACT same behavior as a
#  caller that calls it and gets "other" back.
QUERY_INTENTS = (
    "explicit_recall",
    "correction_update",
    "continuation_of_topic",
    "troubleshooting",
    "planning",
    "casual_conversation",
    "other",
)

#: "lanjut"/"terusin"-style continuation markers - deliberately short and
#: conservative (Phase 1's own "do not over-classify"): a marker here only
#: signals INTENT to continue a topic, it never by itself decides WHICH
#: memory is relevant - that remains entirely the job of the existing
#: relevance/token-overlap machinery.
_CONTINUATION_INTENT_MARKERS = (
    "lanjut", "lanjutkan", "lanjutin", "terusin", "terusan", "terusannya",
    "continue", "keep going", "carry on", "balik ke", "balik lagi ke", "back to",
)

#: Troubleshooting markers - "why is X broken/failing" shaped language.
_TROUBLESHOOTING_INTENT_MARKERS = (
    "error", "gagal", "rusak", "ga jalan", "gak jalan", "nggak jalan",
    "tidak jalan", "not working", "crash", "bug", "masalah", "problem",
    "debug", "troubleshoot", "kenapa error", "kenapa gagal", "why isn't",
    "why doesn't", "why is it not", "kenapa ga bisa", "kenapa nggak bisa",
)

#: Planning markers - "what's next / let's plan" shaped language.
_PLANNING_INTENT_MARKERS = (
    "rencana", "rencanain", "planning", "roadmap", "langkah selanjutnya",
    "next step", "next steps", "strategi", "milestone", "mau bikin apa",
)

#: Casual-conversation markers - deliberately a SMALL, conservative list
#: (Phase 1's "do not over-classify"): only clear small-talk/banter
#: signals, never triggered merely by the ABSENCE of a task-shaped signal
#: (an ordinary factual question with no matching marker anywhere falls
#: through to "other", not "casual_conversation" - see the fallback below).
_CASUAL_CONVERSATION_INTENT_MARKERS = (
    "haha", "wkwk", "lol", "hehe", "gimana kabar", "how are you",
    "lagi apa nih", "santai aja", "just chatting", "ngobrol santai",
)


def _compile_word_boundary_marker_pattern(markers):
    """Word-boundary-safe marker matching - plain `marker in lowered`
    substring checks would wrongly fire on "lanjut" inside "sel-ANJUT-nya"
    ("langkah selanjutnya", one of THIS module's own planning markers) or
    similar false-positive substrings. `\\b` on both ends of each marker
    (markers may be multi-word phrases, e.g. "keep going" - `\\b` still
    anchors correctly at the phrase's own start/end) fixes that without
    introducing a second tokenizer - this is still plain `re`, the same
    library every other deterministic detector in this module already
    uses (`_CORRECTION_RE`, `_EXPLICIT_IMPORTANCE_RE`, ...)."""
    return re.compile(r'\b(?:' + '|'.join(re.escape(m) for m in markers) + r')\b', re.IGNORECASE)


_CONTINUATION_INTENT_RE = _compile_word_boundary_marker_pattern(_CONTINUATION_INTENT_MARKERS)
_TROUBLESHOOTING_INTENT_RE = _compile_word_boundary_marker_pattern(_TROUBLESHOOTING_INTENT_MARKERS)
_PLANNING_INTENT_RE = _compile_word_boundary_marker_pattern(_PLANNING_INTENT_MARKERS)
_CASUAL_CONVERSATION_INTENT_RE = _compile_word_boundary_marker_pattern(_CASUAL_CONVERSATION_INTENT_MARKERS)


def classify_query_intent(text):
    """Memory Retrieval & Decision Quality sprint - deterministic
    query-intent classifier (see the precedence order documented in the
    block comment immediately above). Always returns one of
    `QUERY_INTENTS`, never `None`/empty - unclassifiable/ambiguous input
    safely falls back to `"other"`, the same "no signal -> no behavior
    change" contract `classify_query_context_category()` already
    established for its own fallback."""
    raw = text or ""
    lowered = raw.lower()

    # Reuse EXISTING recall/historical detectors verbatim - never a
    # second recall/historical word list.
    if is_recall_command(lowered) or is_session_recall_command(lowered) or _is_historical_query(raw):
        return "explicit_recall"

    # Reuse the EXISTING correction-language regex (`_classify_conflict()`
    # itself already keys off this same pattern) - never a second
    # correction-language parser.
    if _CORRECTION_RE.search(lowered):
        return "correction_update"

    if _CONTINUATION_INTENT_RE.search(lowered):
        return "continuation_of_topic"

    if _TROUBLESHOOTING_INTENT_RE.search(lowered):
        return "troubleshooting"

    if _PLANNING_INTENT_RE.search(lowered):
        return "planning"

    if _CASUAL_CONVERSATION_INTENT_RE.search(lowered):
        return "casual_conversation"

    return "other"


# ─────────────────────────────────────────────
#  MEMORY CONTINUITY & SHORT FOLLOW-UP REFERENCE RESOLUTION (Sprint 4)
# ─────────────────────────────────────────────
#
# Phase 0's audit (see docs/change_impact/memory_continuity_reference_resolution.md)
# found, with empirical proof (a live probe through the real RuntimeDemoConsole
# event path, not assumption), that NONE of the brief's own 12 example short
# follow-ups ("yang lain?", "other option?", "terus?", "kalau itu gimana?",
# ...) match `_CONTINUATION_INTENT_MARKERS` above - `classify_query_intent()`
# returns "other" for every one of them, so the existing `continuation_of_topic`
# -> `previous_topic_terms` -> `intent_bonus` pipeline never activates for
# exactly the symptom class this sprint targets. `_CONTINUATION_INTENT_MARKERS`
# was designed for a NARROWER, different signal ("please continue" - lanjutkan/
# terusin/keep going) - deliberately NOT touched or widened here (that would
# risk regressing `test_memory_decision_quality.py`'s own contract for that
# signal). This is an ADDITIVE, separate, orthogonal classifier for a
# different linguistic phenomenon: elliptical/anaphoric reference to
# something already discussed ("the other one", "what about that", "terus"
# as a discourse particle meaning "and then/so" - not "continue").
#
# Deliberately reuses the SAME `_compile_word_boundary_marker_pattern()`
# helper `classify_query_intent()` already uses above - no second tokenizer,
# no LLM/embedding classifier, no new regex engine. Precedence order matters
# (first match wins) and is documented at each check below - chosen so the
# brief's own worked examples classify exactly as it specifies:
# "yang lain?" -> ALTERNATIVE_REQUEST, "yang lebih murah?" -> COST_COMPARISON,
# "kalau itu?" -> DIRECT_REFERENCE, "terus?" -> CONTINUATION,
# "kalau tanpa MQTT?" -> NEGATION_OF_CURRENT_OPTION, "ESP32 gimana?" -> COMPARISON.

REFERENCE_TYPES = (
    "repair_reference",
    "negation_of_current_option",
    "cost_comparison",
    "alternative_request",
    "ordinal_reference",
    "attribute_reference",
    "continuation",
    "comparison",
    "direct_reference",
    "unknown",
)

#: every type EXCEPT "unknown" signals "this turn likely refers to something
#: already discussed and has too little standalone semantic signal to be
#: retrieved on its own" - the caller-facing gate
#: (`needs_topic_context()` below) that Phase 4's retrieval expansion and
#: Phase 3's active-topic mechanism key off.
NEEDS_TOPIC_CONTEXT_TYPES = frozenset(t for t in REFERENCE_TYPES if t != "unknown")

#: "tanpa X"/"without X"/"kalau ga/gak/nggak/tidak pakai X" - explicitly
#: rejecting the CURRENT option in favor of some other approach for the
#: SAME underlying goal. Checked FIRST (highest precedence) since "tanpa"
#: is an unambiguous, narrow signal that never legitimately means anything
#: else in this context.
_NEGATION_REFERENCE_RE = re.compile(
    r'\btanpa\s+\w+|\bwithout\s+\w+|\bkalau\s+(?:ga|gak|nggak|tidak)\s+(?:pakai|pake|menggunakan)\b|'
    r'\bif\s+not\s+using\b|\bkalau\s+tidak\s+pakai\b',
    re.IGNORECASE,
)

#: "lebih murah"/"termurah"/"cheaper"/... - asking to compare cost against
#: whatever was just discussed. Checked before ALTERNATIVE_REQUEST since
#: "yang lebih murah?" would otherwise ALSO loosely match "yang ..." framing.
_COST_COMPARISON_RE = re.compile(
    r'\blebih\s+murah\b|\blebih\s+mahal\b|\btermurah\b|\btermahal\b|\bcheaper\b|'
    r'\bmore\s+expensive\b|\bcheapest\b|\bharganya\s+(?:berapa|gimana)\b',
    re.IGNORECASE,
)

#: "yang lain"/"opsi lain"/"pilihan lain"/"cara lain"/"other option"/
#: "another option"/"the other one"/"any other" - explicitly asking for a
#: DIFFERENT option than the one just given.
_ALTERNATIVE_REQUEST_RE = re.compile(
    r'\byang\s+lain\b|\byang\s+lainnya\b|\bopsi\s+lain\b|\bopsi\s+lainnya\b|'
    r'\bpilihan\s+lain\b|\bpilihan\s+lainnya\b|\bcara\s+lain\b|'
    r'\balternatif\s+lain\b|\balternatifnya\b|\bada\s+(?:opsi|pilihan|cara)\s+lain\b|'
    r'\bother\s+option\b|\banother\s+option\b|\bthe\s+other\s+one\b|\banother\s+one\b|'
    r'\bany\s+other\b|\bdifferent\s+option\b|\banything\s+else\b|\bwhat\s+else\b',
    re.IGNORECASE,
)

#: bare "terus" as a discourse particle ("and then?"/"so?"/"go on") -
#: DELIBERATELY separate from `_CONTINUATION_INTENT_MARKERS` above (which
#: only matches "terusin"/"terusan"/"terusannya", never bare "terus") -
#: conflating the two would change `classify_query_intent()`'s own
#: contract, which this sprint must not touch.
_CONTINUATION_REFERENCE_RE = re.compile(
    r'\bterus\b|\band\s+then\b|\bthen\s+what\b|\bwhat\s+next\b|\blalu\b(?!\s+\w)',
    re.IGNORECASE,
)

#: "X gimana?"/"how about X?"/"X vs Y"/"dibanding X" where X names
#: something SPECIFIC (not a bare pronoun) - comparing the just-discussed
#: option against a NAMED alternative already present in the current turn's
#: own text (e.g. "ESP32 gimana?"). Distinguished from DIRECT_REFERENCE
#: below by requiring at least one non-pronoun, non-filler token alongside
#: the comparison marker - reuses the SAME stopword/pronoun list, not a
#: second tokenizer.
#:
#: Sprint 45 (Entity Identity & Semantic Alias Continuity) - added
#: "bagaimana" alongside "gimana". These are not two different words that
#: happen to be related; "gimana" is simply the colloquial contraction of
#: the standard Indonesian question word "bagaimana" ("how") - the exact
#: same lexical item in two registers, already treated as equivalent
#: elsewhere in this very file (the general question-marker regex a few
#: hundred lines up already lists both; `_ATTRIBUTE_RESIDUAL_STOPWORDS`
#: below already lists both). This regex was the one place that missed
#: the pair. Live reproduction (Sprint 45 Scenario G: "ESP32 pakai
#: INMP441." -> "Eh maksudku ESP32-S3." -> "Mic-nya bagaimana?") found
#: this was not a hypothetical: the corrected ESP32-S3/INMP441 topic was
#: available and current, but "Mic-nya bagaimana?" fell all the way to
#: `classify_reference_type() == "unknown"` (no comparison marker
#: matched at all) purely because the user used the formal register
#: instead of "gimana" - the exact same question, asked in more formal
#: Indonesian, silently lost continuity. Not a new alias/synonym
#: mechanism - a one-word omission from an existing pattern, consistent
#: with how this file already treats the pair everywhere else.
_COMPARISON_MARKER_RE = re.compile(
    r'\bgimana\b|\bbagaimana\b|\bhow\s+about\b|\bdibanding(?:kan)?\b|\bversus\b|\bvs\b|\blebih\s+baik\s+mana\b',
    re.IGNORECASE,
)
_PRONOUN_OR_FILLER_TOKENS = frozenset({
    "itu", "ini", "situ", "sini", "that", "this", "it", "the", "a", "an",
    "kalau", "if", "what", "about", "yang", "so",
})

#: "kalau itu?"/"gimana kalau itu?"/"what about that?"/"kalau ini gimana?" -
#: a PURE pronoun/demonstrative reference, no named entity of its own.
_DIRECT_REFERENCE_RE = re.compile(
    r'\bkalau\s+itu\b|\bkalau\s+ini\b|\bitu\s+gimana\b|\bini\s+gimana\b|'
    r'\bwhat\s+about\s+(?:that|this|it)\b|\bhow\s+about\s+(?:that|this|it)\b|'
    r'\babout\s+that\b|\byang\s+tadi\b|\byang\s+barusan\b|\btadi\s+itu\b|'
    # Sprint 38 - bare "yang itu?"/"yang ini?" (one of the brief's own
    # primary target phrases) was previously uncovered - every existing
    # branch above requires "kalau"/"gimana" framing or the word "tadi";
    # a standalone "yang itu?"/"yang ini?" fell all the way through to
    # "unknown" (verified live before this addition).
    r'\byang\s+itu\b|\byang\s+ini\b|'
    # Sprint 38 - "yang <1-3 words> tadi" (e.g. "yang buat mic tadi") - a
    # bounded, non-adjacent generalization of the original "yang tadi"/
    # "tadi itu" patterns above, for the brief's own "yang buat mic tadi"
    # adversarial example. Bounded to at most 3 intervening words so this
    # never matches an unrelated "yang"/"tadi" pair far apart in an
    # otherwise-rich, self-contained sentence.
    r'\byang\b(?:\s+\w+){1,3}\s+tadi\b',
    re.IGNORECASE,
)

#: Memory Retrieval & Decision Quality (re-audit) sprint, Phase 9 - a
#: DIFFERENT bare-pronoun shape than `_DIRECT_REFERENCE_RE` above: "it"/
#: "that"/"this" used as the grammatical SUBJECT or OBJECT of a short
#: question ("which one was it again?", "how does that connect?", "is it
#: still on?", "what was it called?"), rather than inside one of
#: `_DIRECT_REFERENCE_RE`'s own fixed framings ("about that", "kalau itu",
#: "yang tadi"). Phase 0-2's own live reproduction through the real
#: `RuntimeDemoConsole` (turns 6 and 8 of the brief's own 8-turn scenario)
#: found BOTH of these phrasings fall all the way through to "unknown"
#: today - `NEEDS_TOPIC_CONTEXT_TYPES` excludes "unknown" by construction,
#: so `is_short_followup` is `False` for them, and (unlike a genuinely
#: content-bearing turn) they also have no token overlap with anything in
#: `_topic_history` for `select_topic_candidates()` to find - the single-
#: slot `_active_topic` fallback never fires either, so NO memory ever
#: reaches `assemble_context()` for either phrasing (Failure Class B: the
#: candidate is never generated in the first place, not filtered/
#: outranked/budget-cut afterward).
#:
#: Each alternative below is a narrow, specific bigram/trigram (a pronoun
#: immediately adjacent to "was"/"is"/"does"/"did"/"which one"), not a bare
#: "it"/"that"/"this" anywhere in the sentence - this is what keeps a
#: genuinely fresh, self-contained question safe: "How does ESP32 handle
#: low power mode?" contains no "does it/that/this" bigram at all (it's
#: "does ESP32"), so it never matches this pattern and is correctly left
#: alone. Deliberately NO residual-word gate (unlike `_COMPARISON_MARKER_RE`'s
#: own branch) - the phrase specificity itself is already the false-positive
#: guard here; requiring a residual-free sentence would incorrectly reject
#: "How does that connect?" itself ("connect" would count as a residual
#: word despite naming no new entity).
_BARE_PRONOUN_REFERENCE_RE = re.compile(
    r'\bwhich\s+one\b|\bwas\s+it\b|\bis\s+it\b|\bdoes\s+it\b|\bdid\s+it\b|'
    r'\bhow\s+(?:does|do|did)\s+(?:it|that|this)\b|'
    r'\bwhat\s+(?:was|is)\s+it\b',
    re.IGNORECASE,
)


def classify_reference_type(text):
    """Deterministic classifier for short/elliptical follow-up reference
    shapes (Sprint 4, Phase 2). Always returns one of `REFERENCE_TYPES`,
    never `None`/empty. Precedence, documented above each pattern: NEGATION
    -> COST_COMPARISON -> ALTERNATIVE_REQUEST -> CONTINUATION -> COMPARISON
    -> DIRECT_REFERENCE -> BARE_PRONOUN_REFERENCE (Phase 9 re-audit sprint
    addition, same "direct_reference" result, see its own docstring above)
    -> UNKNOWN. Pure function, no I/O, no persistence - mirrors
    `classify_query_intent()`'s own contract exactly."""
    raw = text or ""
    lowered = raw.strip().lower()
    if not lowered:
        return "unknown"

    # Sprint 38 - checked FIRST (highest precedence): an explicit
    # self-correction/rejection is unambiguous and must never be
    # mis-classified as an ordinary DIRECT_REFERENCE just because it
    # also contains "itu"/"ini".
    if _REPAIR_REFERENCE_RE.search(lowered):
        return "repair_reference"
    if _NEGATION_REFERENCE_RE.search(lowered):
        return "negation_of_current_option"
    if _COST_COMPARISON_RE.search(lowered):
        return "cost_comparison"
    if _ALTERNATIVE_REQUEST_RE.search(lowered):
        return "alternative_request"
    # Sprint 38 - checked before CONTINUATION/COMPARISON/DIRECT_REFERENCE:
    # an ordinal marker ("yang kedua", "nomor tiga") is specific and
    # unambiguous, and would otherwise partially overlap DIRECT_REFERENCE's
    # own "yang tadi"-style patterns for phrasings like "yang kedua tadi".
    if _ORDINAL_REFERENCE_RE.search(lowered):
        return "ordinal_reference"
    # Sprint 38 - checked before CONTINUATION/COMPARISON/DIRECT_REFERENCE,
    # after every higher-precedence type has had first refusal (so "yang
    # lain"/"yang lebih murah"/"yang kedua" are never re-classified here -
    # see `_attribute_reference_word()`'s own docstring).
    if _attribute_reference_word(lowered):
        return "attribute_reference"
    if _CONTINUATION_REFERENCE_RE.search(lowered):
        return "continuation"
    if _COMPARISON_MARKER_RE.search(lowered):
        # Requires a residual, non-pronoun/non-filler word token alongside
        # the comparison marker - "kalau itu gimana?" reduces to nothing
        # once "itu"/"kalau" are stripped (-> DIRECT_REFERENCE instead);
        # "ESP32 gimana?" leaves "esp32" behind (-> COMPARISON).
        #
        # Sprint 45 - the marker regex above now also matches "bagaimana"
        # (the standard-register form of "gimana"); this residual filter
        # must exclude it the SAME way it already excludes "gimana"
        # itself, or a bare "Bagaimana?" would wrongly leave "bagaimana"
        # as its own "residual" and misclassify as `comparison` instead
        # of `direct_reference` - an asymmetry a bare "Gimana?" never had.
        words = re.findall(r"[a-z0-9][a-z0-9\-]*", lowered)
        residual = [w for w in words if w not in _PRONOUN_OR_FILLER_TOKENS and w not in ("gimana", "bagaimana")]
        if residual:
            return "comparison"
        return "direct_reference"
    if _DIRECT_REFERENCE_RE.search(lowered):
        return "direct_reference"
    if _BARE_PRONOUN_REFERENCE_RE.search(lowered):
        return "direct_reference"
    return "unknown"


def needs_topic_context(text):
    """`True` when this turn's own text is a short/elliptical reference
    shape that is unlikely to carry enough standalone semantic signal for
    retrieval to find anything relevant on its own (Sprint 4, Phase 4's own
    gating condition for retrieval-query expansion / the active-topic
    candidate). Reuses `classify_reference_type()` - never a second
    classifier."""
    return classify_reference_type(text) in NEEDS_TOPIC_CONTEXT_TYPES


#: Reference types that, BY CONSTRUCTION (see each pattern's own comment
#: above `classify_reference_type()`), carry NO standalone named entity of
#: their own - purely referring back to something already said, with
#: nothing new to anchor to. Deliberately a DIFFERENT, narrower subset than
#: `NEEDS_TOPIC_CONTEXT_TYPES` above: "comparison" ("ESP32 gimana?", "Kalau
#: WLED gimana?") and "negation_of_current_option" ("kalau tanpa MQTT?")
#: both still benefit from retrieval-query EXPANSION (Phase 4 - knowing
#: what's being compared/negated against helps retrieval), but each is
#: REQUIRED by its own regex to carry a real residual word token
#: (`_COMPARISON_MARKER_RE`'s own "requires a residual...word token" check;
#: `_NEGATION_REFERENCE_RE`'s own "tanpa/without \w+" requirement) - i.e.
#: each names something concrete enough ("WLED", "ESP32", "MQTT") to
#: legitimately become the conversation's NEW active topic. A bare "yang
#: lain?"/"terus?"/"kalau itu?"/"yang lebih murah?" has nothing of its own
#: to replace anything with.
#: Sprint 38 - `"ordinal_reference"` added. An ordinal turn ("yang
#: kedua?") carries no standalone entity of its own (it only names a
#: POSITION), exactly the same reasoning as the four original types
#: below - it should PRESERVE the active-topic snapshot (never replace
#: it), while `memory_context.resolve_ordinal_targets()` separately
#: resolves the position itself against the snapshot's own `list_items`.
#: `"repair_reference"`/`"attribute_reference"` are deliberately NOT
#: added here - see `_MERGE_REFERENCE_TYPES`/`is_merge_reference_followup()`
#: above; those two need a THIRD behavior (merge), not preserve.
_PURE_REFERENCE_TYPES = frozenset({"alternative_request", "continuation", "direct_reference", "cost_comparison", "ordinal_reference"})

#: Context-Aware Comparison Topic Preservation sprint - the brief's own
#: explicitly-named generic Indonesian discourse markers that must NEVER,
#: by themselves, count as a "meaningful residual" entity when deciding
#: whether a COMPARISON turn's own topic already overlaps the CURRENT
#: active topic (see `_comparison_residual_terms()`/
#: `is_pure_reference_followup()` below). `"kalau"`/`"yang"`/`"itu"`/
#: `"ini"`/`"about"`/`"that"`/`"this"`/`"it"`/`"the"`/`"a"`/`"an"`/`"if"`/
#: `"so"` are already excluded via `_PRONOUN_OR_FILLER_TOKENS` (reused,
#: not duplicated) and `"gimana"` is already excluded by
#: `classify_reference_type()`'s own comparison-branch check - this set
#: adds ONLY the words the brief itself calls out that are not already
#: covered by either of those: `"bagaimana"` (the fuller synonym of
#: "gimana"), `"tadi"`, `"soal"`. Scoped ENTIRELY to this new preserve-
#: vs-replace decision - `_PRONOUN_OR_FILLER_TOKENS` itself, and therefore
#: `classify_reference_type()`'s own comparison/direct_reference
#: precedence, is completely untouched.
_COMPARISON_PRESERVATION_EXTRA_FILLER = frozenset({"bagaimana", "tadi", "soal"})


def _comparison_residual_terms(text):
    """Extracts the SAME residual words `classify_reference_type()`'s own
    comparison branch already computes (identical regex, identical base
    filter - `_PRONOUN_OR_FILLER_TOKENS` plus the literal `"gimana"`
    check), reused verbatim rather than duplicated, extended only with
    `_COMPARISON_PRESERVATION_EXTRA_FILLER` above. A separate, small
    function (not a second classifier) purely because
    `classify_reference_type()`'s own branch needs its ORIGINAL, narrower
    filter to decide comparison-vs-direct_reference in the first place -
    widening that filter in place would change `classify_reference_type()`
    output itself, which Phase 3 of this sprint requires stay unchanged."""
    lowered = (text or "").strip().lower()
    words = re.findall(r"[a-z0-9][a-z0-9\-]*", lowered)
    return frozenset(
        w for w in words
        if w not in _PRONOUN_OR_FILLER_TOKENS
        and w not in _COMPARISON_PRESERVATION_EXTRA_FILLER
        and w != "gimana"
    )


def _residual_overlaps_active_topic(residual_terms, active_topic_terms):
    """Substring-based overlap (Context-Aware Comparison Topic
    Preservation sprint) - deliberately NOT exact set-equality, and
    deliberately NOT embeddings/semantic similarity (forbidden by this
    sprint's own brief). Two deterministic string-containment checks,
    either direction: this is what lets "INMP441-nya" (residual, an
    Indonesian possessive-suffixed form) match an existing active-topic
    term "inmp441" (the topic never contains suffix variants) WITHOUT a
    dedicated suffix-stripping table, and is the same class of primitive
    (`kw in lowered`) `luno.memory_context._matches_keyword_category()`
    already uses elsewhere in this codebase - not a new matching
    strategy invented for this sprint."""
    if not residual_terms or not active_topic_terms:
        return False
    for residual in residual_terms:
        for topic_term in active_topic_terms:
            if residual in topic_term or topic_term in residual:
                return True
    return False


def is_pure_reference_followup(text, active_topic_terms=None):
    """`True` when `text` is a short follow-up that carries NO standalone
    named entity of its own (Sprint 4, Phase 3/5/6's own "should this turn
    REPLACE or PRESERVE the conversation's active-topic snapshot" question
    - see `memory_context.update_active_topic()`'s own docstring for
    exactly how this is used). Distinct from `needs_topic_context()`
    immediately above (Phase 4's "does this turn benefit from retrieval
    expansion" question, which is ALSO `True` for comparison/negation-type
    turns that this function deliberately excludes, since those carry
    their own real entity and should REPLACE the snapshot, not preserve
    it - see `_PURE_REFERENCE_TYPES`'s own comment for the full reasoning).
    Reuses `classify_reference_type()` - never a second classifier.

    `active_topic_terms` (Context-Aware Comparison Topic Preservation
    sprint, additive, optional, defaults to `None`) - a frozenset of the
    CURRENT active topic's own terms, when the caller has one. Omitting
    it (every existing caller/test before this sprint) is byte-for-byte
    identical to before this sprint: only a COMPARISON-classified turn
    with a non-empty, OVERLAPPING residual (see
    `_comparison_residual_terms()`/`_residual_overlaps_active_topic()`)
    now ALSO returns `True` - narrowing (never widening) the ORIGINAL
    "comparison always replaces" rule. A comparison turn whose residual
    entity is genuinely absent from the current active topic (e.g. "Kalau
    Bluetooth-nya gimana?" against an ESP32/INMP441/mic topic) still
    replaces exactly as before - this only prevents the REPLACE when the
    comparison's own subject is ALREADY part of what's active. Every
    other reference type (negation/cost_comparison/alternative_request/
    continuation/direct_reference/unknown) is completely unaffected -
    this sprint touches ONLY the comparison branch."""
    ref_type = classify_reference_type(text)
    if ref_type in _PURE_REFERENCE_TYPES:
        return True
    if ref_type == "comparison" and active_topic_terms:
        residual = _comparison_residual_terms(text)
        if residual and _residual_overlaps_active_topic(residual, active_topic_terms):
            return True
    return False


# ─────────────────────────────────────────────
#  CONVERSATION REFERENCE RESOLUTION (Sprint 38)
# ─────────────────────────────────────────────
#
# Phase 0's audit found the existing `classify_reference_type()`/
# `is_pure_reference_followup()` pair (Sprint 4 + Context-Aware Comparison
# Topic Preservation, both directly above, both COMPLETELY UNCHANGED by
# this sprint) already correctly resolve "yang lain?"/"terus?"/"kalau
# itu?"/"kalau MQTT gimana?" - a bag-of-terms active topic that a short
# follow-up either PRESERVES or REPLACES. Two concrete gaps remained,
# reproduced live before any code was written:
#
# Gap A - ORDINAL/LIST reference ("yang kedua gimana?", "nomor tiga",
# "yang pertama dibanding yang ketiga"). The existing snapshot only ever
# stores a `frozenset` of topic TERMS (e.g. {"mikrofon", "esp32",
# "inmp441", "max9814", "sph0645"}) - there is no ordering, so "yang
# kedua" has nothing to index into; the best the old mechanism could do
# was re-offer the whole bag, never the one item ("MAX9814") the user
# actually meant.
#
# Gap B - ATTRIBUTE reference ("kalau yang wireless?", "yang lebih
# murah?" without "lebih", "kalau versi Bluetooth?"). None of these match
# any EXISTING `classify_reference_type()` pattern - they fall all the
# way through to "unknown". `"unknown"` is treated as a RICH turn by
# `is_pure_reference_followup()` (correctly, for a genuinely new subject) -
# so `update_active_topic()`/`update_topic_history()` REPLACE the entire
# active-topic snapshot with `{"kalau", "yang", "wireless"}`, losing
# "esp32"/"mikrofon" outright. Reproduced live: a conversation about
# "ESP32 microphone" followed by "Kalau yang wireless?" lost every
# ESP32/mic term from the snapshot by the very next turn.
#
# Neither gap is fixed with a new memory system, a second tokenizer, or
# an LLM judge - both reuse `analyze_query()` (the one tokenizer) and
# plain, deterministic `re` patterns, following this module's own
# existing style exactly (`_compile_word_boundary_marker_pattern()`,
# anchored regex precedence chains).
#
# Three NEW reference types are added to `REFERENCE_TYPES` (below),
# additive only - every EXISTING type's own regex/precedence/output is
# completely unchanged, verified by the full pre-existing
# `test_memory_continuity.py`/`test_memory_topic_retention.py` suites
# continuing to pass unmodified:
#
#   REPAIR_REFERENCE      - "eh maksudku ESP32-S3", "bukan yang itu, yang
#                            satunya" - a conversational self-correction.
#                            Checked FIRST (highest precedence) since its
#                            markers ("maksudku", "bukan itu") are
#                            unambiguous and would otherwise be
#                            mis-classified as DIRECT_REFERENCE.
#   ORDINAL_REFERENCE     - "yang kedua", "nomor tiga", "opsi 2" - refers
#                            to a specific position in something Luno
#                            itself just enumerated.
#   ATTRIBUTE_REFERENCE   - "kalau yang wireless?", "yang murah?", "kalau
#                            versi Bluetooth?" - asks for the active topic
#                            filtered/modified by one new descriptive
#                            word, closing Gap B above.
#
# `is_merge_reference_followup()` (new, mirrors `is_pure_reference_followup()`'s
# own shape) marks REPAIR_REFERENCE and ATTRIBUTE_REFERENCE as needing a
# THIRD update behavior - neither REPLACE (would lose the parent topic,
# Gap B's own bug) nor PRESERVE (would silently drop the new entity/
# correction) but MERGE: union the new turn's terms into the existing
# snapshot's terms, bounded exactly like every other snapshot
# construction. See `memory_context.update_active_topic()`'s own
# docstring for the merge rule itself - this module only classifies,
# never mutates snapshots (same separation of concerns as
# `is_pure_reference_followup()` above).

#: "maksudku"/"maksud saya"/"maksud aku"/"eh maksud..." - an explicit
#: verbal self-correction, and "bukan itu"/"bukan yang itu"/"bukan yang
#: ini" - explicit rejection of the just-offered referent, optionally
#: followed by "yang satu(nya) lagi"/"yang satunya" naming the sibling
#: alternative. Checked with the HIGHEST precedence in
#: `classify_reference_type()` - deliberately narrow, fixed phrases (the
#: same "explicit user intent, not a broad heuristic" discipline
#: `_REMEMBER_PATTERNS`/`_FORGET_FACT_PATTERNS` elsewhere in this module
#: already follow), so an ordinary sentence that happens to contain
#: "bukan" for other reasons ("ini bukan masalah besar") is unaffected -
#: "bukan" alone is never enough; it must be immediately followed by
#: "itu"/"yang itu"/"yang ini".
_REPAIR_REFERENCE_RE = re.compile(
    r'\bmaksudku\b|\bmaksud\s+saya\b|\bmaksud\s+aku\b|\beh\s+maksud\b|'
    r'\bbukan\s+(?:yang\s+)?(?:itu|ini)\b|\byang\s+satu(?:nya)?\s+lagi\b|\byang\s+satunya\b|'
    r'\bi\s+meant\b|\bnot\s+that\s+one\b|\bthe\s+other\s+one\s+i\s+meant\b',
    re.IGNORECASE,
)

#: Word-form Indonesian ordinals used both for classification (below) and
#: index extraction (`parse_ordinal_indices()` in `memory_context.py`,
#: which imports this same mapping - never a second copy of the ordinal
#: vocabulary).
ORDINAL_WORD_MAP = {
    "pertama": 1, "kedua": 2, "ketiga": 3, "keempat": 4, "kelima": 5,
    "keenam": 6, "ketujuh": 7, "kedelapan": 8, "kesembilan": 9, "kesepuluh": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

#: Indonesian CARDINAL number words ("nomor tiga" = "number three", a
#: very natural way to name a list position that does NOT use the
#: ordinal word form "ketiga") - a SEPARATE map from `ORDINAL_WORD_MAP`
#: above because it is only valid immediately after an explicit numbering
#: word ("nomor"/"opsi"/"pilihan"/"item"), never after bare "yang" (
#: "yang tiga" alone is not idiomatic Indonesian for "the third one" and
#: risks false-positives on unrelated numeric content).
CARDINAL_WORD_MAP = {
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
    "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10,
}

_ORDINAL_OR_CARDINAL_WORDS_RE_FRAGMENT = '|'.join(list(ORDINAL_WORD_MAP.keys()) + list(CARDINAL_WORD_MAP.keys()))

#: "yang pertama"/"yang kedua"/"nomor tiga"/"nomor 2"/"opsi 2"/"pilihan
#: ketiga"/"item 2"/"ke-2" - refers to a POSITION in something already
#: enumerated (a numbered/bulleted list in Luno's own previous reply).
#: Detection-only (does this turn reference a position at all?) - actual
#: index extraction is `memory_context.parse_ordinal_indices()`, kept
#: separate so this module (pure classification, no snapshot/list
#: awareness) stays independently testable, matching this file's
#: existing `classify_reference_type()`/`needs_topic_context()` split.
_ORDINAL_REFERENCE_RE = re.compile(
    r'\byang\s+(?:' + '|'.join(ORDINAL_WORD_MAP.keys()) + r')\b|'
    r'\bnomor\s+(?:\d{1,2}|' + _ORDINAL_OR_CARDINAL_WORDS_RE_FRAGMENT + r')\b|'
    r'\bno\.?\s*\d{1,2}\b|'
    r'\bopsi\s+(?:ke-?)?(?:\d{1,2}|' + _ORDINAL_OR_CARDINAL_WORDS_RE_FRAGMENT + r')\b|'
    r'\bpilihan\s+(?:ke-?)?(?:\d{1,2}|' + _ORDINAL_OR_CARDINAL_WORDS_RE_FRAGMENT + r')\b|'
    r'\bitem\s+(?:ke-?)?(?:\d{1,2}|' + _ORDINAL_OR_CARDINAL_WORDS_RE_FRAGMENT + r')\b|'
    r'\bke-\d{1,2}\b',
    re.IGNORECASE,
)

#: Words that must NEVER be treated as a genuine descriptive attribute
#: when they follow "yang"/"kalau yang" - the SAME pronoun/filler
#: vocabulary `_PRONOUN_OR_FILLER_TOKENS` already excludes for the
#: COMPARISON/DIRECT_REFERENCE branches above (reused, not duplicated),
#: extended with the words this sprint's own new types already claim
#: with HIGHER precedence (alternative/ordinal/repair words) so those
#: turns are never double-classified.
_ATTRIBUTE_EXCLUDED_WORDS = (
    _PRONOUN_OR_FILLER_TOKENS
    | frozenset({
        "lain", "lainnya", "tadi", "barusan", "satu", "satunya", "mana",
        # Prepositions/connectors that can appear immediately after "yang"
        # ("yang buat X", "yang untuk X") without themselves naming any
        # attribute - excluded from ever being the candidate WORD itself
        # (as opposed to `_ATTRIBUTE_RESIDUAL_STOPWORDS` below, which
        # excludes them only from the RESIDUAL check).
        "buat", "untuk", "dari", "dengan", "tentang", "soal",
    })
    | frozenset(ORDINAL_WORD_MAP.keys())
)

#: Common Indonesian connective/question-boilerplate words - NOT excluded
#: from being the attribute word itself (they're checked against the
#: CANDIDATE word too, via `_ATTRIBUTE_EXCLUDED_WORDS` above already
#: covering "kalau"/"yang"/etc.), but excluded from the ELLIPTICAL-
#: FRAGMENT residual check below: a turn like "Modul Bluetooth apa yang
#: bagus buat ESP8266?" is a full, rich, self-contained question that
#: merely happens to contain a "yang <word>" clause - it must NOT be
#: mis-classified as an elliptical ATTRIBUTE_REFERENCE fragment. The
#: residual check only tolerates BOILERPLATE words like these alongside
#: the candidate attribute word; any OTHER real content word ("modul",
#: "bluetooth", "esp8266") disqualifies the match. Reuses the same class
#: of lexical resource `memory_context._TOPIC_OVERLAP_STOPWORDS` already
#: establishes for an analogous purpose downstream - kept as its own copy
#: here (not imported) only because `memory_context` imports THIS module,
#: never the reverse (no circular import).
_ATTRIBUTE_RESIDUAL_STOPWORDS = frozenset({
    "kalau", "versi", "dengan", "untuk", "buat", "dong", "sih", "aja", "ya",
    "dan", "atau", "apa", "apakah", "gimana", "bagaimana", "ada", "bisa",
    "punya", "nya", "yg", "juga", "deh", "kok", "dah", "udah", "lagi", "sama",
    "bagian",
    # Sprint 39 (Phase 2/3, MISSING CONTEXT) - "terus" as a leading
    # discourse particle ("so, ...?"/"then, ...?"), not the standalone
    # CONTINUATION marker `_CONTINUATION_REFERENCE_RE` matches elsewhere.
    # Live E2E reproduction (Scenario A, turn 4: "Terus yang paling
    # murah?") found this word alone in the residual check disqualified
    # an otherwise-clear attribute reference, falling through to bare
    # CONTINUATION (a pure PRESERVE) and silently discarding the turn's
    # real content ("paling murah") instead of MERGING it in. Safe to
    # exempt here for the SAME reason "dong"/"sih"/"aja" already are: a
    # genuinely rich, self-contained sentence that happens to start with
    # "terus" ("Terus gimana cara pasang yang bagus buat outdoor?") still
    # has OTHER real residual words ("gimana", "cara", "pasang",
    # "outdoor") that correctly disqualify it regardless.
    "terus",
    # Sprint 39 - "lebih"/"paling" (comparative/superlative markers) -
    # `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` now SKIPS these when locating
    # the candidate span (see that regex's own Sprint 39 comment), but
    # they still appear as separate tokens in the full-sentence residual
    # check below; without this they'd disqualify their own match ("yang
    # lebih bagus?" -> candidate "bagus", but "lebih" left over as a
    # false "extra content word"). They're structurally part of the SAME
    # elliptical phrase as the candidate word, not independent content.
    "lebih", "paling",
})

#: "kalau yang <word>?"/"yang <word>?"/"kalau versi <word>?" - a request
#: to filter/modify the active topic by ONE new descriptive word ("yang
#: wireless", "yang murah", "kalau versi Bluetooth"). Requires the word
#: immediately after "yang"/"versi" to be a real, non-filler token (the
#: `_attribute_reference_word()` helper below does the actual filtering -
#: this regex only locates the candidate span).
#:
#: Sprint 39 (Phase 2/3, MISSING CONTEXT) - `(?:lebih\s+|paling\s+)?`
#: added, same "optional skip" shape `(?:bagian\s+)?` already uses. Live
#: reproduction + the brief's own Phase 8 adversarial phrases ("yang
#: lebih bagus", "yang lebih kecil") found the candidate span was
#: grabbing the COMPARATIVE/SUPERLATIVE MARKER itself ("lebih"/"paling")
#: as the word, which `_attribute_reference_word()` then correctly
#: rejected (a bare marker carries no descriptive content on its own),
#: leaving the REAL word ("bagus"/"kecil"/"murah"/"mahal") never even
#: considered - "yang lebih bagus?"/"yang paling murah?" fell all the way
#: through to `unknown` instead of `attribute_reference`. Skipping the
#: marker the same way "bagian" is already skipped lets the real word be
#: captured instead. Does not change "yang lebih murah?"/"yang lebih
#: mahal?" (still classified `cost_comparison` at higher precedence,
#: checked before this ever runs - see `classify_reference_type()`).
_ATTRIBUTE_REFERENCE_CANDIDATE_RE = re.compile(
    r'\b(?:kalau\s+)?yang\s+(?:bagian\s+)?(?:lebih\s+|paling\s+)?([a-z][a-z0-9\-]*)\b|\bkalau\s+versi\s+([a-z][a-z0-9\-]*)\b',
    re.IGNORECASE,
)


def _attribute_reference_word(lowered_text):
    """Returns the residual descriptive word for an ATTRIBUTE_REFERENCE
    turn, or `None` if no candidate span matched or the only candidate(s)
    found are pronouns/fillers/words already claimed by a
    higher-precedence type (see `_ATTRIBUTE_EXCLUDED_WORDS`). Checked by
    `classify_reference_type()` only AFTER negation/cost_comparison/
    alternative_request/ordinal have already had first refusal - by the
    time this runs, "yang lain"/"yang lebih murah"/"yang kedua" have
    already been classified something else, so this only ever fires for
    a genuinely new descriptive word."""
    all_words = None  # computed lazily, at most once, only if a candidate span is found
    for m in _ATTRIBUTE_REFERENCE_CANDIDATE_RE.finditer(lowered_text):
        word = (m.group(1) or m.group(2) or "").strip()
        if not word or word in _ATTRIBUTE_EXCLUDED_WORDS or word in ("gimana", "bagaimana"):
            continue
        # Elliptical-fragment guard (see `_ATTRIBUTE_RESIDUAL_STOPWORDS`'s
        # own comment above for the full reasoning) - only fires when the
        # sentence has NO other substantial content beyond the candidate
        # word and ordinary connective/question boilerplate.
        if all_words is None:
            all_words = re.findall(r"[a-z0-9][a-z0-9\-]*", lowered_text)
        residual = [
            w for w in all_words
            if w != word and w not in _ATTRIBUTE_EXCLUDED_WORDS and w not in _ATTRIBUTE_RESIDUAL_STOPWORDS
        ]
        if residual:
            continue
        return word
    return None


#: Reference types whose own turn text carries a NEW entity/word that
#: must be MERGED into (never simply replace, never simply preserve) the
#: existing active-topic snapshot - see this section's own module
#: comment above for the full reasoning (Gap A/B).
_MERGE_REFERENCE_TYPES = frozenset({"repair_reference", "attribute_reference"})


def is_merge_reference_followup(text):
    """`True` when `text` is a REPAIR_REFERENCE or ATTRIBUTE_REFERENCE
    turn (Sprint 38) - the caller-facing gate
    `memory_context.update_active_topic()`/`update_topic_history()`'s new
    `is_merge` parameter is keyed off. Mirrors
    `is_pure_reference_followup()`'s exact shape/contract (always a bool,
    reuses `classify_reference_type()`, never a second classifier) for a
    DIFFERENT decision - merge, not preserve-vs-replace."""
    return classify_reference_type(text) in _MERGE_REFERENCE_TYPES


def is_manual_summarize_command(user_lower):
    return any(p in user_lower for p in _MANUAL_SUMMARIZE_PHRASES)


def detect_remember_command(user_text):
    """Return teks fakta yang mau diingat, atau None kalau bukan perintah 'remember'
    (termasuk kalau ternyata ini permintaan REMINDER — lihat _REMINDER_INTENT_RE)."""
    stripped = user_text.strip()
    if _REMINDER_INTENT_RE.match(stripped):
        return None  # "ingetin aku ..." = minta di-reminder, bukan fakta buat diinget
    for pattern in _REMEMBER_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


def detect_forget_fact_command(user_text):
    """Return query teks yang mau dilupakan dari long-term memory, atau None.
    Dicek SETELAH is_clear_everything_command/is_clear_short_term_command oleh
    pemanggil, supaya frasa umum ('lupakan semua') tidak salah ke-capture di sini
    sebagai 'nama fakta yang mau dihapus'."""
    stripped = user_text.strip()
    for pattern in _FORGET_FACT_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


# ─────────────────────────────────────────────
#  MANUAL MEMORY MANAGEMENT sprint - additional explicit-intent detectors
#  (update / delete-by-id / delete-by-topic). Same style/discipline as
#  every pattern list above: anchored at the START of the message
#  (`^...$`), a fixed small set of trigger verbs, never a broad "sounds
#  like it might be about memory" heuristic - "EXPLICIT USER INTENT ->
#  ACT, ORDINARY CONVERSATION -> DO NOT ACT" applies here exactly as it
#  already does to _REMEMBER_PATTERNS/_FORGET_FACT_PATTERNS above.
# ─────────────────────────────────────────────

# "ubah/ganti/koreksi/update memory (tentang|soal) <topic> jadi/ke/= <new text>"
# - deliberately requires the literal word "memory" right after the verb
# (unlike _REMEMBER_PATTERNS, which accepts a bare "inget ya..." with no
# such marker) - Step 10 explicitly wants update treated MORE cautiously
# than a plain save, since it destructively replaces existing content.
_UPDATE_MEMORY_PATTERNS = [
    re.compile(
        r'^(?:tolong\s+)?(?:ubah|update|ganti|koreksi)\s+memory\s+(?:tentang\s+|soal\s+)?(.+?)\s+'
        r'(?:jadi|menjadi|ke|=)\s+(.+)$', re.IGNORECASE,
    ),
    re.compile(
        r'^(?:please\s+)?update\s+(?:the\s+)?memory\s+(?:about\s+)?(.+?)\s+to\s+(.+)$',
        re.IGNORECASE,
    ),
]

_DELETE_MEMORY_BY_ID_PATTERNS = [
    re.compile(r'^(?:tolong\s+)?hapus\s+memory\s+(?:nomor|nomer|number|#)\s*([a-zA-Z0-9]+)$', re.IGNORECASE),
    re.compile(r'^(?:please\s+)?delete\s+memory\s+(?:number|#)\s*([a-zA-Z0-9]+)$', re.IGNORECASE),
]

# "hapus/delete memory (tentang|soal|about) <topic>" - topic-based delete,
# a SEPARATE trigger verb ("hapus"/"delete") from _FORGET_FACT_PATTERNS'
# ("lupa.../forget...") above - both remain independently usable, neither
# is modified, per this sprint's "prefer additive changes" rule.
_DELETE_MEMORY_BY_TOPIC_PATTERNS = [
    re.compile(r'^(?:tolong\s+)?hapus\s+memory\s+(?:tentang\s+|soal\s+)?(.+)$', re.IGNORECASE),
    re.compile(r'^(?:please\s+)?delete\s+memory\s+(?:about\s+)?(.+)$', re.IGNORECASE),
]


def detect_update_memory_command(user_text):
    """Return `(topic_query, new_text)` if `user_text` is an explicit
    memory-update command, else `None`. Checked BEFORE the delete-by-topic
    detector by the caller (`main_runtime_demo.py`) - "ubah memory GPU jadi
    RTX 5070" must never be mistaken for a delete."""
    stripped = user_text.strip()
    for pattern in _UPDATE_MEMORY_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None


def detect_delete_memory_by_id_command(user_text):
    """Return the memory `id` string if `user_text` explicitly asks to
    delete a memory BY ID ("hapus memory nomor 12"), else `None`. Checked
    before `detect_delete_memory_by_topic_command` by the caller, since
    "hapus memory nomor 12" would otherwise also match the by-topic
    pattern's `(.+)` group (capturing "nomor 12" as if it were a topic)."""
    stripped = user_text.strip()
    for pattern in _DELETE_MEMORY_BY_ID_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


def detect_delete_memory_by_topic_command(user_text):
    """Return the topic query if `user_text` explicitly asks to delete a
    memory BY TOPIC ("hapus memory tentang GPU lamaku"), else `None`."""
    stripped = user_text.strip()
    for pattern in _DELETE_MEMORY_BY_TOPIC_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


# ─────────────────────────────────────────────
#  MEMORY INTELLIGENCE sprint - optional Step 14 commands: "Memory ini
#  penting." / "Jadikan memory ini permanen." / "Jangan simpan ini." /
#  "Lupakan memory ini." These are deliberately narrow, ANCHORED patterns
#  (whole-message match, not substring) so they never fire on ordinary
#  conversation that happens to contain the word "ini"/"this". Both
#  target "the most-recently touched entry in `_memories`" - the safest
#  deterministic reading of an otherwise session-less "ini"/"this"
#  reference, since no session-level "last mentioned memory" tracker
#  exists (and building one is out of scope/risk for this sprint - see
#  docs/change_impact/memory_intelligence.md). Checked BEFORE
#  `detect_delete_memory_by_topic_command` by the caller, since "hapus
#  memory ini" would otherwise also match that pattern's catch-all
#  `(.+)` group with topic_query="ini" and go looking for a literal
#  substring match instead of doing the more useful "forget the last
#  one" behavior this section implements.
# ─────────────────────────────────────────────

_MARK_IMPORTANT_RE = re.compile(
    r'^(?:tolong\s+)?(?:memory\s+)?ini\s+(?:sangat\s+)?penting(?:\s+banget)?\.?$|'
    r'^jadikan\s+(?:memory\s+)?ini\s+permanen\.?$|'
    r'^ingat\s+ini\s+selamanya\.?$|'
    r'^this\s+is\s+(?:very\s+)?important\.?$|'
    r'^make\s+this\s+(?:memory\s+)?permanent\.?$',
    re.IGNORECASE,
)

_FORGET_LAST_MEMORY_RE = re.compile(
    r'^(?:tolong\s+)?jangan\s+simpan\s+ini\.?$|'
    r'^(?:tolong\s+)?lupakan\s+memory\s+ini\.?$|'
    r'^(?:tolong\s+)?hapus\s+memory\s+ini\.?$|'
    r"^(?:please\s+)?don'?t\s+save\s+this\.?$|"
    r'^(?:please\s+)?forget\s+this\s+memory\.?$',
    re.IGNORECASE,
)


def detect_mark_important_command(user_text):
    """Whole-message match only ("Memory ini penting." / "Jadikan ini
    permanen.") - returns True/False, not a captured topic, since there
    is nothing to parse out: the target is always "the last touched
    memory" (see `mark_last_memory_important()`)."""
    return bool(_MARK_IMPORTANT_RE.match(user_text.strip()))


def detect_forget_last_memory_command(user_text):
    """Whole-message match only ("Jangan simpan ini." / "Lupakan memory
    ini.") - same "last touched memory" target as
    `detect_mark_important_command` above, via `forget_last_memory()`."""
    return bool(_FORGET_LAST_MEMORY_RE.match(user_text.strip()))


def _most_recently_touched_memory():
    """Returns the entry with the latest `updated_at`/`created_at`
    timestamp, or `None` if `_memories` is empty. ISO-8601 timestamps
    (this module's own stamping convention, unchanged) sort correctly as
    plain strings, so no parsing is needed.

    Ties are broken by LIST POSITION (later == more recent) rather than
    left to `max()`'s default "keep the first maximal element" behavior -
    `_now_iso()` only has 1-second resolution, so two memories
    saved/touched within the same second (routine in tests, and possible
    in real fast back-to-back turns) would otherwise tie and silently
    resolve to the WRONG (earlier) entry."""
    if not _memories:
        return None
    best = None
    best_key = None
    for index, m in enumerate(_memories):
        ts = m.get("updated_at") or m.get("created_at") or ""
        key = (ts, index)
        if best_key is None or key > best_key:
            best_key = key
            best = m
    return best


def mark_last_memory_important():
    """Step 14 - force-promotes the most-recently-touched memory to
    importance=4 (core) and upgrades its `source` to "user_explicit" (an
    explicit user action taken right now legitimately justifies the
    upgrade - this is never automatic/inferred, so it does not violate
    Step 9's "never downgrade explicit to inferred" rule, which only
    protects against the REVERSE direction). Returns the updated entry,
    or `None` if there is no memory yet to mark."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    entry["importance"] = 4
    entry["source"] = "user_explicit"
    entry["updated_at"] = _now_iso()
    _save()
    print(f"[Memory] ✓ Marked important/permanent: {entry['text']}")
    return dict(entry)


def forget_last_memory():
    """Step 14 - deletes the most-recently-touched memory outright
    (reuses `delete_memory_by_id()`, not a second deletion mechanism).
    Returns the removed text, or `None` if there was nothing to
    remove."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    return delete_memory_by_id(entry["id"])


# ─────────────────────────────────────────────
#  MEMORY LEARNING & FEEDBACK LOOP sprint (Section 19) - explicit,
#  deterministic user commands. Same "most-recently-touched memory" target
#  convention `detect_mark_important_command`/`detect_forget_last_memory_command`
#  above already established (no session-level "last mentioned memory"
#  tracker exists inside THIS module - that concept lives one layer up, in
#  `main_runtime_demo.py`'s session-scoped feedback target, see Section 13
#  below and that file's own `_session_feedback_target`). These four are
#  for an EXPLICIT, anchored "memory ini ..." command; the conversational
#  ("iya benar" / "itu salah", no literal word "memory") variants are the
#  separate `detect_positive_memory_feedback`/`detect_negative_memory_feedback`/
#  `detect_memory_feedback_correction` functions further below, which
#  require a caller-supplied target since they carry no target information
#  of their own.
# ─────────────────────────────────────────────

_MARK_USEFUL_RE = re.compile(
    r'^(?:tolong\s+)?memory\s+ini\s+berguna\.?$|'
    r'^(?:please\s+)?this\s+memory\s+is\s+useful\.?$',
    re.IGNORECASE,
)

_MARK_NOT_USEFUL_RE = re.compile(
    r'^(?:tolong\s+)?memory\s+ini\s+(?:tidak|nggak|gak)\s+berguna\.?$|'
    r'^(?:please\s+)?this\s+memory\s+is\s+not\s+useful\.?$',
    re.IGNORECASE,
)

_MARK_MEMORY_CORRECT_RE = re.compile(
    r'^(?:tolong\s+)?memory\s+ini\s+benar\.?$|'
    r'^(?:please\s+)?this\s+memory\s+is\s+correct\.?$',
    re.IGNORECASE,
)

_MARK_MEMORY_INCORRECT_RE = re.compile(
    r'^(?:tolong\s+)?memory\s+ini\s+salah\.?$|'
    r'^(?:please\s+)?this\s+memory\s+is\s+(?:incorrect|wrong)\.?$',
    re.IGNORECASE,
)


def detect_mark_memory_useful_command(user_text):
    return bool(_MARK_USEFUL_RE.match(user_text.strip()))


def detect_mark_memory_not_useful_command(user_text):
    return bool(_MARK_NOT_USEFUL_RE.match(user_text.strip()))


def detect_mark_memory_correct_command(user_text):
    return bool(_MARK_MEMORY_CORRECT_RE.match(user_text.strip()))


def detect_mark_memory_incorrect_command(user_text):
    return bool(_MARK_MEMORY_INCORRECT_RE.match(user_text.strip()))


def mark_last_memory_useful():
    """"Memory ini berguna." - applies POSITIVE feedback to the
    most-recently-touched memory (same deterministic target-selection
    helper `mark_last_memory_important()`/`forget_last_memory()` already
    use). Returns the updated entry, or `None` if there's nothing to act
    on."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    return apply_positive_feedback(entry["id"], reason="explicit_useful_command")


def mark_last_memory_not_useful():
    """"Memory ini tidak berguna." - applies NEGATIVE feedback to the
    most-recently-touched memory. Never deletes it - see
    `apply_negative_feedback()`'s own docstring."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    return apply_negative_feedback(entry["id"], reason="explicit_not_useful_command")


def mark_last_memory_correct():
    """"Memory ini benar." - a confirmation-shaped command, same
    treatment as `mark_last_memory_useful()` (positive feedback) since
    confirming correctness is evidence the memory is working as
    intended, exactly like `apply_positive_feedback()`'s other callers."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    return apply_positive_feedback(entry["id"], reason="explicit_correct_command")


def mark_last_memory_incorrect():
    """"Memory ini salah." - a dispute-shaped command, same treatment as
    `mark_last_memory_not_useful()` (negative feedback). Does NOT delete
    or rewrite the memory - if the user also supplies a new value, that
    goes through the separate correction path (Section 8), never through
    this function."""
    entry = _most_recently_touched_memory()
    if entry is None:
        return None
    return apply_negative_feedback(entry["id"], reason="explicit_incorrect_command")


# ─────────────────────────────────────────────
#  MEMORY LEARNING & FEEDBACK LOOP sprint (Sections 6/7/8) - deterministic
#  CONVERSATIONAL feedback detection. Unlike the explicit "memory ini ..."
#  commands above, these carry no target of their own - a bare "iya
#  benar"/"itu salah" says nothing about WHICH memory. The caller
#  (`main_runtime_demo.py`'s `_handle_memory_feedback_command()`) is
#  responsible for resolving an unambiguous target (its own session-scoped
#  `_session_feedback_target`, Section 13) BEFORE calling
#  `apply_positive_feedback()`/`apply_negative_feedback()`/`update_memory()`
#  with it - if no such target exists, the caller does nothing (Section
#  6/7's explicit "jika target ambiguous: jangan modify memory"). This
#  module deliberately has NO session/conversation concept of its own
#  (same boundary `_most_recently_touched_memory()`'s own docstring
#  already draws) - these functions only ever detect/parse TEXT, never
#  resolve a target.
#
#  Deliberately narrow, anchored patterns (same "small, explicit,
#  fixed trigger-phrase set" discipline as every other detector in this
#  file) - broad/ambiguous phrasing is intentionally left unmatched
#  (returns `False`/`None`, falls through to ordinary conversation)
#  rather than risking a false-positive feedback event on a turn that was
#  never about memory at all. In particular, bare "iya"/"ya"/"tidak"/
#  "ya"/"tidak usah" (already meaningful to
#  `_handle_browser_confirmation`/`_handle_environmental_intent`/
#  `ConfirmationHandler.resolve_reply` in `main_runtime_demo.py`) are
#  NEVER matched here - only the longer, more specific example phrases
#  the sprint brief itself lists, which those other confirmation flows do
#  not use.
# ─────────────────────────────────────────────

_POSITIVE_MEMORY_FEEDBACK_RE = re.compile(
    r'^iya\s+benar\.?$|'
    r'^betul\.?$|'
    r'^benar\s+itu\.?$|'
    r'^ingat\s+itu\.?$|'
    r'^nah\s+itu\.?$|'
    r'^ya,?\s+yang\s+itu\.?$|'
    r"^that'?s\s+(?:right|correct)\.?$",
    re.IGNORECASE,
)

_NEGATIVE_MEMORY_FEEDBACK_RE = re.compile(
    r'^itu\s+salah\.?$|'
    r'^bukan\s+begitu\.?$|'
    r'^(?:nggak|ngga|gak)\s+benar\.?$|'
    r'^udah\s+(?:nggak|ngga|gak)\s+berlaku\.?$|'
    r'^itu\s+sudah\s+berubah\.?$|'
    r"^that'?s\s+(?:wrong|incorrect)\.?$",
    re.IGNORECASE,
)

#: Correction feedback (Section 8) - the ONE case where a conversational
#: reply carries a new value along with the dispute. Deliberately narrow
#: (only the sprint brief's own exact example shape + its direct English
#: equivalent) - a broad "actually X"/"now X" pattern would false-positive
#: on ordinary conversation far too often (Section 20's "no guessed
#: conflict resolution" applies just as much to over-eager DETECTION as to
#: resolution itself). Checked BEFORE `_NEGATIVE_MEMORY_FEEDBACK_RE` by the
#: caller (a correction is a more specific case of "that's wrong" that
#: also supplies a replacement value).
_CORRECTION_MEMORY_FEEDBACK_RE = re.compile(
    r'^yang\s+tadi\s+salah,?\s+sekarang\s+(.+)$|'
    r'^itu\s+salah,?\s+(?:yang\s+benar\s+adalah|seharusnya)\s+(.+)$|'
    r"^that'?s\s+wrong,?\s+(?:it'?s\s+actually|the\s+correct\s+one\s+is)\s+(.+)$",
    re.IGNORECASE,
)


def detect_positive_memory_feedback(user_text):
    """Whole-message match only - True/False, no target (see module-level
    note above)."""
    return bool(_POSITIVE_MEMORY_FEEDBACK_RE.match((user_text or "").strip()))


def detect_negative_memory_feedback(user_text):
    return bool(_NEGATIVE_MEMORY_FEEDBACK_RE.match((user_text or "").strip()))


def detect_memory_feedback_correction(user_text):
    """Returns the captured replacement text (stripped), or `None` if
    `user_text` isn't one of the narrow correction-feedback shapes."""
    stripped = (user_text or "").strip()
    m = _CORRECTION_MEMORY_FEEDBACK_RE.match(stripped)
    if not m:
        return None
    new_text = next((g for g in m.groups() if g), None)
    if not new_text:
        return None
    new_text = new_text.strip().rstrip(".!?").strip()
    return new_text or None


# ─────────────────────────────────────────────
#  MEMORY CONFLICT RESOLUTION sprint - Section 16 command detectors.
#  Same discipline as every detector above: anchored, a small fixed
#  trigger-phrase set, EXPLICIT USER INTENT -> ACT / ORDINARY
#  CONVERSATION -> DO NOT ACT.
# ─────────────────────────────────────────────

_SHOW_CONFLICTS_RE = re.compile(
    r'^(?:tolong\s+)?tampilkan\s+konflik\s+memory(?:\s+tentang\s+.+)?\.?$|'
    r'^memory\s+mana\s+yang\s+bentrok(?:\s+tentang\s+.+)?\??$|'
    r'^(?:please\s+)?show\s+(?:memory\s+)?conflicts\.?$|'
    r'^(?:please\s+)?show\s+(?:me\s+)?(?:the\s+)?conflicting\s+memories\.?$',
    re.IGNORECASE,
)

# "memory (tentang/soal) <topic> yang benar" / "<topic> is correct" -
# requires the literal word "memory" (like `_UPDATE_MEMORY_PATTERNS`
# above), since "X yang benar" alone is common enough ordinary phrasing
# that it would otherwise false-positive on unrelated conversation.
_RESOLVE_CONFLICT_PATTERNS = [
    re.compile(r'^(?:tolong\s+)?memory\s+(?:tentang\s+|soal\s+)?(.+?)\s+yang\s+benar\.?$', re.IGNORECASE),
    re.compile(r'^(?:please\s+)?memory\s+(?:about\s+)?(.+?)\s+is\s+(?:the\s+)?correct(?:\s+one)?\.?$', re.IGNORECASE),
]


def detect_show_conflicts_command(user_text):
    """Whole-message match ("Tampilkan konflik memory." / "Memory mana
    yang bentrok?") - returns True/False, the caller shows ALL current
    `list_conflicts()` groups (an optional per-topic "tentang GPU" tail
    is accepted by the pattern but not separately filtered on -
    deliberately kept simple, per this sprint's own "do not add commands
    merely for feature count" instruction)."""
    return bool(_SHOW_CONFLICTS_RE.match(user_text.strip()))


def detect_resolve_conflict_command(user_text):
    """Return the topic query if `user_text` is an explicit "memory X
    yang benar" resolution command, else `None`."""
    stripped = user_text.strip()
    for pattern in _RESOLVE_CONFLICT_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip()
    return None


# ─────────────────────────────────────────────
#  RETRIEVAL (registers as one more luno.memory_retrieval MemorySource) -
#  same factory shape as `episodic_memory.make_episodic_experience_source()`
#  and every built-in factory in `luno.memory_retrieval.sources`: a zero-
#  arg provider callable in, a closure out. Deliberately does NOT reuse the
#  MemoryRetriever source name "long_term_memory" - that name is already
#  registered in `main_runtime_demo.py` for Vision Memory's OWN internal
#  "long_term_memory" (learned habits/patterns), a completely different
#  system that happens to share a similar name (see
#  `luno.memory_retrieval.sources.make_long_term_memory_source`'s own
#  docstring) - this one is registered as "manual_memory" instead.
# ─────────────────────────────────────────────


def make_manual_memory_source(get_memories):
    """`get_memories` is a zero-arg callable returning the current list of
    manual-memory entries (application wiring binds `list_memories` in
    `main_runtime_demo.py`). Returns a `MemorySource` closure: no query
    signal or empty store -> `[]` immediately, no store access at all
    (same "Vision Memory should not be queried unnecessarily" discipline
    every other source already follows). Uses the SAME token-overlap
    matching `search_memories()` above uses - not a second algorithm.

    Memory Conflict Resolution sprint (Section 9/13): when the CURRENT
    utterance itself carries historical-intent wording
    (`_is_historical_query()` - "dulu"/"pernah"/"yang lama"/"previously"),
    each entry's `history` is ALSO searched for token overlap, so a
    superseded value stays genuinely reachable through the real
    production prompt path, not just via `search_memories()`. An
    ordinary current-state question ("GPU ku sekarang apa?") never
    triggers this branch and behaves exactly as before - the entry's
    CURRENT text is the only thing anything can ever match, so a current
    query can never accidentally surface a stale, superseded value
    (Section 13's own "current query should prioritize current value",
    satisfied by construction: there is nothing else in the ambient pool
    to compete with it unless the query is explicitly asking about the
    past)."""
    from .memory_retrieval.models import RelevantMemory
    from .memory_retrieval.query import token_overlap

    def _source(query, retrieval_config):
        if not query.has_any_signal:
            return []
        try:
            entries = get_memories()
        except Exception:
            return []
        if not entries:
            return []

        historical_query = _is_historical_query(getattr(query, "raw_text", "") or getattr(query, "normalized", ""))

        results = []
        for m in entries:
            if not isinstance(m, dict) or not m.get("text"):
                continue
            # Memory Intelligence sprint (Step 7/13): "archived" entries
            # are deliberately excluded from ambient retrieval - they are
            # still fully recoverable via search_memories()/list_memories()/
            # get_memory() directly, just not surfaced unprompted into the
            # LLM's context budget. This check runs BEFORE the relevance
            # gate below so an archived memory never even becomes a
            # candidate, matching Step 7's "not used in normal retrieval."
            if compute_lifecycle(m) == "archived":
                continue

            if historical_query and isinstance(m.get("history"), list):
                # Historical branch: match against OLD wording, not the
                # entry's current text - a distinct, still-plain-English
                # label (never raw field names/internal metadata in the
                # LLM-facing text, per Section 14) tells the LLM this is
                # superseded information, not the current state.
                for h in m["history"]:
                    if not isinstance(h, dict) or not h.get("text"):
                        continue
                    if not token_overlap(query.tokens, h["text"]):
                        continue
                    old_text = h["text"] if h["text"].endswith((".", "!", "?")) else h["text"] + "."
                    category = m.get("category", "other")
                    hist_text = (
                        f"[MANUAL MEMORY - {category}, historical] The user previously said "
                        f"(later superseded): {old_text}"
                    )
                    hist_timestamp = None
                    raw_hist_ts = h.get("changed_at")
                    if raw_hist_ts:
                        try:
                            hist_timestamp = datetime.fromisoformat(raw_hist_ts)
                            if hist_timestamp.tzinfo is None:
                                hist_timestamp = hist_timestamp.replace(tzinfo=timezone.utc)
                        except (TypeError, ValueError):
                            hist_timestamp = None
                    results.append(RelevantMemory(
                        text=hist_text, source="manual_memory", score=0.45,
                        timestamp=hist_timestamp, raw=m,
                    ))

            if not token_overlap(query.tokens, m["text"]):
                continue

            text = m["text"] if m["text"].endswith((".", "!", "?")) else m["text"] + "."
            category = m.get("category", "other")
            text = f"[MANUAL MEMORY - {category}] The user explicitly asked you to remember: {text}"

            timestamp = None
            raw_ts = m.get("updated_at") or m.get("created_at")
            if raw_ts:
                try:
                    timestamp = datetime.fromisoformat(raw_ts)
                    # `created_at`/`updated_at` are stamped via
                    # `datetime.now().isoformat(...)` (naive, no tzinfo -
                    # this module's own existing convention, unchanged by
                    # this sprint). `MemoryRetriever._apply_recency_and_
                    # staleness()` computes `utcnow() - mem.timestamp`
                    # against a TIMEZONE-AWARE `now` - subtracting a naive
                    # datetime from an aware one raises `TypeError`, so a
                    # naive parse result is treated as UTC (same "naive
                    # input is assumed to already be UTC" rule
                    # `luno.vision_memory.utils.ensure_aware()` uses).
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    timestamp = None

            # Same 0.5 base score as make_long_term_memory_source/
            # make_episodic_experience_source - explicit user-authored
            # content earns a small bonus over those (0.6 vs 0.5) since
            # it is, BY DEFINITION, something the user directly asked to
            # be remembered (Step 14: stronger relevance priority than
            # automatically-inferred memories) - but this is a ranking
            # nudge among OTHER memories only. It never competes with
            # verified facts/current tool results at all: those are
            # injected through a completely separate note
            # (`self.memory_guard`/`world_model`, never through
            # `self.memory_retriever`), so a stale manual memory can
            # never outrank a fresh verified reading - there is no shared
            # ranking pool between the two.
            #
            # Memory Intelligence sprint (Step 12): importance/explicitness/
            # staleness only ever adjust ranking AMONG candidates that
            # already passed the token_overlap relevance gate above - an
            # irrelevant importance=4 memory is filtered out before this
            # point is ever reached, so it structurally cannot "pollute" an
            # unrelated query no matter how high its score would be.
            score = 0.6
            score += _get_importance(m) * 0.05
            if m.get("source") == "user_explicit":
                score += 0.05
            if compute_lifecycle(m) == "stale":
                score -= 0.15
            # Memory Learning & Feedback Loop sprint (Section 11) - same
            # small, bounded usefulness nudge `_score_memory_for_prompt()`
            # applies (kept in sync deliberately - these two functions
            # already share the exact same base formula per this file's
            # own precedent). Applied only AFTER relevance (the
            # token_overlap gate above already ran), lifecycle, and
            # importance - and weighted small enough (±0.025 max) that it
            # can never outrank an importance-level difference (0.05/level)
            # or the staleness penalty (0.15).
            score += (_get_usefulness(m) - _DEFAULT_USEFULNESS_SCORE) * 0.05

            results.append(RelevantMemory(
                text=text, source="manual_memory", score=score,
                timestamp=timestamp, raw=m,
            ))
        return results

    return _source


# ─────────────────────────────────────────────
#  MEMORY LIFECYCLE & MAINTENANCE ENGINE sprint - deterministic usage
#  tracking, maintenance analysis (planning), and explicit execution, all
#  built ON TOP OF the existing `_memories` store, `importance`,
#  `compute_lifecycle()`, `conflict_status`/`conflict_group`, and the
#  existing Jaccard/`_classify_conflict()` infrastructure. No second
#  store, no second tokenizer, no second lifecycle model - see
#  `docs/change_impact/memory_maintenance.md` for the full architecture
#  audit that confirmed this.
#
#  Core principle (this sprint's own "most important rule"): maintenance
#  makes Luno smarter about WHAT TO ANALYZE, never more aggressive about
#  WHAT TO DELETE. Nothing in this section ever calls `del`/list-removal
#  on a memory except `apply_maintenance_plan()`'s "consolidate" action -
#  and even then, the removed entry's text is preserved in the survivor's
#  `history`, never lost. "archive" NEVER removes anything - it only sets
#  metadata that makes `compute_lifecycle()` report "archived" (excluded
#  from ambient retrieval, still fully intact and directly recoverable).
# ─────────────────────────────────────────────

#: Step 4 - a generous, purely defensive ceiling ("bound metadata
#: growth") - a single scalar int has no realistic growth problem the way
#: `history[]` did, but the sprint brief asks for an explicit bound
#: anyway, so one is set here rather than left implicit.
_MAX_RETRIEVAL_COUNT = 999999

#: Step 5 - how many GENUINE retrieval events (each one real conversation
#: turn's call to `record_memory_usage()`, never a same-call burst - see
#: that function's own docstring) it takes before frequency MAY
#: contribute one importance point. Deliberately NOT 1 - a single lucky
#: relevant turn must not be treated as "proven repeatedly useful."
_REINFORCEMENT_RETRIEVAL_THRESHOLD = 5

#: Step 5's explicit ceiling - frequency-driven reinforcement (both the
#: live, automatic kind in `record_memory_usage()` AND the planner-
#: recommended kind applied via `apply_maintenance_plan()`) can NEVER
#: raise importance to 4 by itself. Only an explicit signal
#: (`_EXPLICIT_IMPORTANCE_RE` at save time, or `mark_last_memory_important()`)
#: can reach 4 - "explicit user-marked important memories remain highest
#: priority" is satisfied by this cap alone: nothing frequency-driven can
#: ever catch up to or exceed an explicit core memory.
_FREQUENCY_REINFORCEMENT_CEILING = 3

#: Step 10 - "consolidate only when confidence is above a strict
#: threshold." Chosen above the near-duplicate Jaccard band's own typical
#: scores (0.45-0.92) so only genuinely strong candidates (exact
#: duplicates at 0.95, or clear refinement pairs scored near the top of
#: that range) are EXECUTED automatically by `apply_maintenance_plan()` -
#: weaker candidates still appear in the plan (for visibility in the
#: health report/dry-run) but execution skips them.
_CONSOLIDATION_APPLY_THRESHOLD = 0.75


def _get_retrieval_count(entry):
    """Backward-compatible accessor - same shape as `_get_importance()`:
    a pre-sprint entry simply lacks this key, defaults to 0 (never
    retrieved), never crashes on a malformed/negative/non-int value."""
    if not isinstance(entry, dict):
        return 0
    value = entry.get("retrieval_count")
    return value if isinstance(value, int) and value >= 0 else 0


def _is_protected_from_archival(entry):
    """Step 13 - the minimum protected set this sprint can determine
    from data actually present on an `_memories` entry:
      - importance == 4 (explicit user-marked-important memories already
        route here via `_classify_memory_importance`'s explicit-marker
        branch or `mark_last_memory_important()` - there is no SEPARATE
        "permanent" flag anywhere else in this codebase to check, so
        importance=4 IS the existing representation of "explicitly
        marked as permanent/core").
      - `conflict_status == "ambiguous_conflict"` (a memory currently
        involved in an unresolved conflict) - Step 8/13's own explicit
        requirement; the conflict must be resolved by a person via
        `resolve_conflict_by_topic()`, never silently archived/removed by
        maintenance.
    "Protected verified facts" (Step 13's third category) never appears
    here at all - `VerifiedFactStore` facts are never represented as
    `_memories` entries, so they are structurally unreachable by this
    module's maintenance logic, protected by construction rather than by
    a check (see `docs/change_impact/memory_maintenance.md`)."""
    if not isinstance(entry, dict):
        return False
    if _get_importance(entry) >= 4:
        return True
    if entry.get("conflict_status") == "ambiguous_conflict":
        return True
    return False


# ─────────────────────────────────────────────
#  MEMORY LEARNING & FEEDBACK LOOP sprint - deterministic, bounded
#  usefulness scoring + positive/negative/correction feedback, built ON
#  TOP OF the usage tracking established by the Memory Lifecycle &
#  Maintenance sprint directly below (`record_memory_usage()`,
#  `retrieval_count`/`last_retrieved_at`). No second usage-tracking
#  system: "usage" (the memory was surfaced/selected) IS
#  `retrieval_count`/`last_retrieved_at`, already wired into
#  `record_memory_usage()` and already scoped to genuine, relevance-
#  gated, budget-surviving retrievals only (see that function's own
#  docstring). This section adds a DIFFERENT, deliberately separate
#  concept - USEFULNESS (was this memory actually good, based on
#  evidence), never conflated with usage (was this memory merely shown).
#
#  Deterministic, bounded, explainable:
#    - `usefulness_score` is a float in [0.0, 1.0], default 0.5 (neutral -
#      "no evidence yet" is not the same as "known to be bad"). Never
#      derived from text length/token count (explicitly forbidden by this
#      sprint's own brief) - only from explicit feedback events and a
#      small, capped usage-driven nudge.
#    - `positive_feedback_count`/`negative_feedback_count` are plain
#      non-negative counters - separate from usefulness_score itself, so
#      the RAW EVIDENCE (how many times a human said "yes"/"no" about
#      this memory) is always inspectable independent of the derived
#      score (Section 18's explainability requirement).
#    - Every mutation here is bounded (min/max clamped) and driven by an
#      explicit, named reason - never a silent, unbounded increment.
#    - Usefulness NEVER touches `importance` directly (Section 10's "usefulness
#      tidak boleh langsung menggantikan importance") - `apply_positive_feedback()`/
#      `apply_negative_feedback()` below only ever write
#      `usefulness_score`/`*_feedback_count`, never `importance`. The only
#      existing importance-reinforcement path remains the pre-existing,
#      capped, frequency-driven one inside `record_memory_usage()` below
#      (bounded at `_FREQUENCY_REINFORCEMENT_CEILING`, unchanged, unrelated
#      to feedback) and the pre-existing explicit-signal path
#      (`_classify_memory_importance`'s marker regex / `mark_last_memory_important()`).
#    - Usefulness NEVER overwrites `text` and never deletes an entry -
#      Section 20's explicit "positive feedback -> overwrite memory" /
#      "negative feedback -> delete memory" are both structurally
#      impossible here: neither function below touches `text` or removes
#      an entry from `_memories`.
# ─────────────────────────────────────────────

#: Bounds - Section 9's "usefulness_score harus bounded".
MEMORY_USEFULNESS_MIN = 0.0
MEMORY_USEFULNESS_MAX = 1.0

#: Neutral default for an entry with no feedback/usage evidence yet - NOT
#: 0.0 (which would read as "known to be bad" for every pre-existing
#: entry the moment this sprint ships, including every entry this
#: codebase already relies on).
_DEFAULT_USEFULNESS_SCORE = 0.5

#: Per-event deltas - deliberately small and symmetric, so a SINGLE
#: feedback event nudges the score meaningfully but can never alone swing
#: it from one extreme to the other (several consistent events are needed
#: to reach the bounds - "bounded reinforcement", Section 10).
_USEFULNESS_POSITIVE_FEEDBACK_DELTA = 0.15
_USEFULNESS_NEGATIVE_FEEDBACK_DELTA = 0.15

#: Section 9's "successful retrieval/use -> sedikit naik" - a tiny nudge
#: folded into `record_memory_usage()` below, every genuine retrieval
#: event. Deliberately much smaller than a single explicit feedback delta
#: (0.02 vs 0.15) - frequency of USE is much weaker evidence of quality
#: than an explicit human judgment, so it must never be able to out-race
#: real feedback.
_USEFULNESS_USAGE_DELTA = 0.02

#: Section 9's "repeated irrelevant retrieval -> jangan otomatis
#: dihukum" is satisfied by construction (there is no penalty branch at
#: all here) - this ceiling instead satisfies the SYMMETRIC risk: usage
#: volume ALONE (with zero explicit feedback ever given) must never be
#: able to manufacture a "highly useful" score - mirrors the existing,
#: proven `_FREQUENCY_REINFORCEMENT_CEILING` pattern for importance
#: (frequency alone caps below the maximum; only explicit signal reaches
#: the top).
_USEFULNESS_USAGE_NUDGE_CEILING = 0.7

#: Section 16's maintenance-integration threshold - "usefulness tinggi
#: boleh menjadi sinyal reinforcement" for a stale memory that would
#: otherwise be recommended for archival on usage/age alone. Deliberately
#: only ever used to make maintenance MORE conservative (reinforce/keep
#: instead of archive), never to trigger any additional archiving -
#: Section 16/20's "usefulness tinggi -> core memory" is explicitly NOT
#: implemented (this only affects the archive/reinforce choice, never
#: importance).
_USEFULNESS_PROTECTS_FROM_ARCHIVAL = 0.75


def _get_usefulness(entry):
    """Backward-compatible accessor - same shape as `_get_importance()`/
    `_get_retrieval_count()`: a pre-sprint entry (or a hand-edited/
    malformed value) simply lacks/has an invalid `usefulness_score`,
    defaults to the neutral `_DEFAULT_USEFULNESS_SCORE` rather than
    crashing or guessing an extreme."""
    if not isinstance(entry, dict):
        return _DEFAULT_USEFULNESS_SCORE
    value = entry.get("usefulness_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and MEMORY_USEFULNESS_MIN <= value <= MEMORY_USEFULNESS_MAX:
        return float(value)
    return _DEFAULT_USEFULNESS_SCORE


def _get_positive_feedback_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("positive_feedback_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_negative_feedback_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("negative_feedback_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def get_memory_usefulness(entry):
    """Public, read-only wrapper around `_get_usefulness()` - same
    reasoning as `get_memory_importance()`/`get_memory_retrieval_count()`
    above (a caller outside this module, e.g. the dashboard, should never
    need to reach into a private internal)."""
    return _get_usefulness(entry)


def get_memory_positive_feedback_count(entry):
    return _get_positive_feedback_count(entry)


def get_memory_negative_feedback_count(entry):
    return _get_negative_feedback_count(entry)


def get_memory_usefulness_explanation(entry):
    """Public, read-only wrapper around `_explain_usefulness()` below -
    same reasoning as every other `get_memory_*` public accessor in this
    section (a caller outside this module, e.g. the Memory Dashboard's
    detail view, should never need to reach into a private internal)."""
    return _explain_usefulness(entry)


def _explain_usefulness(entry):
    """Section 18's explainability requirement - a short, human-readable
    breakdown of the CURRENT score's evidence, computed on demand from
    already-stored, already-bounded counters (never a raw per-event log -
    "Gunakan metadata bounded", Section 18's own instruction). Purely
    read-only, safe to call from a dashboard detail view or a test."""
    positive = _get_positive_feedback_count(entry)
    negative = _get_negative_feedback_count(entry)
    retrieval_count = _get_retrieval_count(entry)
    score = _get_usefulness(entry)
    lines = [f"Usefulness: {score:.2f}", f"Baseline (neutral, no evidence): {_DEFAULT_USEFULNESS_SCORE:.2f}"]
    if positive:
        lines.append(f"+ positive feedback x {positive}")
    if negative:
        lines.append(f"- negative feedback x {negative}")
    if retrieval_count:
        lines.append(f"+ usage-based nudge (retrieved {retrieval_count} time(s), capped at {_USEFULNESS_USAGE_NUDGE_CEILING:.2f})")
    return "\n".join(lines)


def apply_positive_feedback(memory_id, reason="user_confirmed"):
    """Deterministic, bounded positive feedback (Section 6). Only ever
    called with a memory_id the CALLER has already resolved to a single,
    unambiguous target (see `main_runtime_demo.py`'s session feedback
    target / `mark_last_memory_useful()` below) - this function itself
    does no guessing, it just applies the mutation to the id it's given.

    Increments `positive_feedback_count` and raises `usefulness_score` by
    a bounded delta (never below/above `[MEMORY_USEFULNESS_MIN,
    MEMORY_USEFULNESS_MAX]`). Never touches `importance`, `text`, or
    `history` - Section 20's "positive feedback -> overwrite memory" is
    structurally impossible here. Returns the updated entry, or `None` if
    `memory_id` doesn't exist."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            m["positive_feedback_count"] = _get_positive_feedback_count(m) + 1
            new_score = min(_get_usefulness(m) + _USEFULNESS_POSITIVE_FEEDBACK_DELTA, MEMORY_USEFULNESS_MAX)
            m["usefulness_score"] = round(new_score, 4)
            _save()
            print(f"[Memory] ✓ Positive feedback ({reason}) recorded for {memory_id}: usefulness={m['usefulness_score']}")
            return dict(m)
    return None


def apply_negative_feedback(memory_id, reason="user_disputed"):
    """Deterministic, bounded negative feedback (Section 7) - the mirror
    of `apply_positive_feedback()` above. Increments
    `negative_feedback_count` and lowers `usefulness_score` by a bounded
    delta. Never deletes the entry, never touches `text`/`history`/
    `importance` - a negative signal alone NEVER removes or rewrites a
    memory (Section 20's "negative feedback -> delete memory" is
    structurally impossible here); an actual content correction only
    ever happens through the EXISTING `update_memory()` path (see
    `main_runtime_demo.py`'s correction-feedback handling, Section 8),
    which this function does not call and has no way to trigger. Returns
    the updated entry, or `None` if `memory_id` doesn't exist."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            m["negative_feedback_count"] = _get_negative_feedback_count(m) + 1
            new_score = max(_get_usefulness(m) - _USEFULNESS_NEGATIVE_FEEDBACK_DELTA, MEMORY_USEFULNESS_MIN)
            m["usefulness_score"] = round(new_score, 4)
            _save()
            print(f"[Memory] ✓ Negative feedback ({reason}) recorded for {memory_id}: usefulness={m['usefulness_score']}")
            return dict(m)
    return None


# ─────────────────────────────────────────────
#  Step 4/5 - usage tracking + conservative reinforcement
# ─────────────────────────────────────────────

def record_memory_usage(relevant_memories, now=None):
    """Increments `retrieval_count`/`last_retrieved_at` for every
    manual-memory entry that ACTUALLY appears in a final, already-
    relevance-gated-and-budget-limited retrieval result this turn -
    never for a memory that merely exists in `_memories`, never for a
    candidate a source considered but that lost to the budget/ranking
    before reaching the caller (Step 4's own "merely existing in the
    database does not count as usage").

    `relevant_memories` accepts the list `MemoryRetriever.retrieve_memories()`
    returns (`RelevantMemory` objects - only ones whose `.source ==
    "manual_memory"` are ever touched; vision/episodic/planner-state
    results in the same combined list are silently ignored, never
    mistaken for a manual entry) OR a plain list of memory-id strings
    (for direct/test use) - duck-typed so callers don't need an extra
    conversion step.

    Deliberately NOT wired into `build_memory_prompt(query_text=...)`/
    `_select_memories_for_prompt()` - the Memory Prompt Intelligence
    sprint's own tests (`test_prompt_generation_never_calls_save`,
    `test_prompt_generation_does_not_mutate_entries`) already prove and
    protect that path as strictly read-only; extending usage-tracking
    there would break an existing, passing test, which this sprint's own
    continuity rule forbids. Usage tracking is scoped to the
    `MemoryRetriever`-based retrieval path only (`main_runtime_demo.py`'s
    `self.memory_retriever.retrieve_memories(text)` call site) - this
    codebase's own "Smart Memory Injection" (Sprint 5) naming for THE
    retrieval mechanism, which is the more faithful reading of Step 4's
    "actually participates in a successful retrieval result" anyway.

    Conservative reinforcement (Step 5) happens here too, not as a
    separate call: every `_REINFORCEMENT_RETRIEVAL_THRESHOLD`-th (5th)
    GENUINE retrieval (i.e. once every 5 separate real turns this memory
    was actually surfaced in) may raise importance by exactly +1, capped
    at `_FREQUENCY_REINFORCEMENT_CEILING` (3) - frequency alone can never
    reach 4 (Step 5's own explicit prohibition), and an entry already at
    importance>=3 is left untouched by this path entirely (no-op),
    matching "explicit user-marked important memories remain highest
    priority." This mirrors the ALREADY-existing, pre-sprint precedent
    of `_reinforce_existing_memory()` auto-bumping importance on an
    exact-duplicate `add_memory()` hit with no explicit command needed -
    ordinary usage bookkeeping, not "maintenance" in Step 12's
    explicit-command-gated sense (see this function's own change-impact
    write-up for the full reasoning)."""
    if not relevant_memories:
        return []

    now_iso = (now or datetime.now()).isoformat(timespec="seconds")
    ids = set()
    for item in relevant_memories:
        if isinstance(item, str):
            ids.add(item)
            continue
        source = getattr(item, "source", None)
        raw = getattr(item, "raw", None)
        if source == "manual_memory" and isinstance(raw, dict) and raw.get("id"):
            ids.add(raw["id"])

    if not ids:
        return []

    updated = []
    for m in _memories:
        if not isinstance(m, dict) or m.get("id") not in ids:
            continue
        count = _get_retrieval_count(m) + 1
        m["retrieval_count"] = min(count, _MAX_RETRIEVAL_COUNT)
        m["last_retrieved_at"] = now_iso
        if count % _REINFORCEMENT_RETRIEVAL_THRESHOLD == 0:
            current_importance = _get_importance(m)
            if current_importance < _FREQUENCY_REINFORCEMENT_CEILING:
                m["importance"] = current_importance + 1
        # Memory Learning & Feedback Loop sprint (Section 9) - a small,
        # capped usefulness nudge for genuine usage, folded into the SAME
        # usage-tracking event rather than a second pass over
        # `relevant_memories` (one retrieval event -> at most one usage
        # record -> at most one usefulness nudge, never double-counted).
        # Bounded below `_USEFULNESS_USAGE_NUDGE_CEILING` so usage volume
        # alone can never manufacture a "highly useful" score - only
        # explicit feedback (`apply_positive_feedback()`) can cross that
        # ceiling.
        current_usefulness = _get_usefulness(m)
        if current_usefulness < _USEFULNESS_USAGE_NUDGE_CEILING:
            m["usefulness_score"] = round(min(current_usefulness + _USEFULNESS_USAGE_DELTA, _USEFULNESS_USAGE_NUDGE_CEILING), 4)
        updated.append(m)

    if updated:
        _save()
    return [dict(m) for m in updated]


# ─────────────────────────────────────────────
#  Step 7 - obsolete/temporary wording (maintenance-specific, checked
#  independent of age - "never use age alone", Step 7's own explicit
#  instruction).
# ─────────────────────────────────────────────

#: Reuses `_TEMPORARY_WORDING_RE`'s existing pattern text verbatim (not
#: retyped, not a second tokenizer) plus a few maintenance-specific
#: additions from Step 7's own example list that the save-time regex
#: doesn't cover. Deliberately a SEPARATE compiled regex rather than
#: extending `_TEMPORARY_WORDING_RE` in place - extending that one would
#: silently change SAVE-TIME importance classification (it caps
#: importance at 1) for every future memory containing a newly-added
#: word like "currently" - an unrelated behavior change out of this
#: sprint's scope.
_OBSOLETE_WORDING_RE = re.compile(
    _TEMPORARY_WORDING_RE.pattern + r'|' +
    r'\b(?:currently|untuk\s+sekarang|lagi\s+coba(?:-coba)?|coba-coba|'
    r'untuk\s+sementara|temporary)\b',
    re.IGNORECASE,
)


# ─────────────────────────────────────────────
#  Steps 6/8/9 - the maintenance planner (analysis only, never mutates).
# ─────────────────────────────────────────────

def _plan_action_for_entry(entry, now=None):
    """One memory's BASE recommendation, before the pairwise redundancy
    sweep below can upgrade it. Conservative by construction: the
    fallback for anything not explicitly matched is always "keep"."""
    memory_id = entry.get("id")
    if _is_protected_from_archival(entry):
        if entry.get("conflict_status") == "ambiguous_conflict":
            return {
                "memory_id": memory_id, "action": "review",
                "reason": "part of an unresolved conflict - preserve both sides, ask the user which is correct",
                "confidence": 1.0,
            }
        return {
            "memory_id": memory_id, "action": "keep",
            "reason": "protected core memory (importance=4)", "confidence": 1.0,
        }

    importance = _get_importance(entry)
    lifecycle = compute_lifecycle(entry, now=now)
    text = entry.get("text", "") or ""
    is_obsolete_wording = bool(_OBSOLETE_WORDING_RE.search(text))
    retrieval_count = _get_retrieval_count(entry)

    if lifecycle == "archived":
        return {
            "memory_id": memory_id, "action": "keep",
            "reason": "already archived - no further action needed", "confidence": 1.0,
        }

    if is_obsolete_wording and importance <= 1:
        return {
            "memory_id": memory_id, "action": "archive",
            "reason": "contains temporary/obsolete wording and low importance", "confidence": 0.8,
        }

    if lifecycle == "stale":
        if retrieval_count >= _REINFORCEMENT_RETRIEVAL_THRESHOLD and importance < _FREQUENCY_REINFORCEMENT_CEILING:
            return {
                "memory_id": memory_id, "action": "reinforce",
                "reason": f"stale by age but retrieved {retrieval_count} times - still useful", "confidence": 0.7,
            }
        # Memory Learning & Feedback Loop sprint (Section 16) - usefulness
        # is a SECOND, independent signal from usage/retrieval_count alone:
        # a memory with real positive-feedback evidence (usefulness_score
        # at/above `_USEFULNESS_PROTECTS_FROM_ARCHIVAL`) should not be
        # archived just because its raw retrieval_count happens to be low
        # (e.g. it was confirmed useful via an explicit feedback command,
        # or via `search_memories()`/the dashboard, paths that don't
        # increment `retrieval_count`). This ONLY ever makes maintenance
        # MORE conservative (reinforce instead of archive) - it never adds
        # a new way to archive something, matching Section 16's own
        # "jangan archive hanya karena usage rendah" / "maintenance harus
        # tetap conservative" requirement.
        usefulness = _get_usefulness(entry)
        if usefulness >= _USEFULNESS_PROTECTS_FROM_ARCHIVAL and importance < _FREQUENCY_REINFORCEMENT_CEILING:
            return {
                "memory_id": memory_id, "action": "reinforce",
                "reason": f"stale by age but usefulness is high ({usefulness:.2f}) - still valuable", "confidence": 0.65,
            }
        # Memory Evaluation & Self-Calibration sprint (Step 10) - a THIRD,
        # independent, advisory signal, consulted only after usage/
        # usefulness above have already had their chance to protect this
        # entry. Same one-way rule as usefulness: `evaluate_memory()`'s
        # own recommendation can only make this planner's decision MORE
        # conservative here (upgrade the default "archive" to "reinforce"
        # or "review"), never invent a new way to archive/delete
        # something that usage/usefulness/obsolete-wording didn't already
        # flag - `evaluate_memory()` itself never mutates, and this
        # integration point never lets its advisory output escalate past
        # what the base planner already decided on its own.
        evaluation = evaluate_memory(entry, now=now)
        if evaluation["recommendation"] == "reinforce" and importance < _FREQUENCY_REINFORCEMENT_CEILING:
            return {
                "memory_id": memory_id, "action": "reinforce",
                "reason": f"stale by age but evaluation evidence is strong (score={evaluation['score']:.2f}) - still valuable",
                "confidence": round(min(0.7, 0.5 + evaluation["confidence"] * 0.3), 2),
            }
        if evaluation["recommendation"] == "review":
            return {
                "memory_id": memory_id, "action": "review",
                "reason": f"stale, evaluation confidence too low to decide (confidence={evaluation['confidence']:.2f}) - needs human review",
                "confidence": round(evaluation["confidence"], 2),
            }
        return {
            "memory_id": memory_id, "action": "archive",
            "reason": "stale and rarely or never retrieved", "confidence": 0.6,
        }

    # active
    if retrieval_count >= _REINFORCEMENT_RETRIEVAL_THRESHOLD and importance < _FREQUENCY_REINFORCEMENT_CEILING:
        return {
            "memory_id": memory_id, "action": "reinforce",
            "reason": f"frequently retrieved ({retrieval_count} times) - reinforcing importance", "confidence": 0.7,
        }

    return {
        "memory_id": memory_id, "action": "keep",
        "reason": "currently active, no maintenance needed", "confidence": 1.0,
    }


def analyze_memory_maintenance(now=None):
    """Step 9's deterministic maintenance planner - ANALYSIS ONLY, never
    mutates `_memories`, never calls `_save()`. Same `_memories` state +
    same injected `now` always produces the same plan (Step 9's own
    determinism requirement - verified by a dedicated test that this
    function is pure with respect to its own inputs).

    Two passes:
      1. `_plan_action_for_entry()` - a per-memory base recommendation
         (keep/reinforce/archive/review), from data on that ONE entry
         alone (protection, lifecycle, obsolete wording, usage).
      2. A bounded pairwise redundancy sweep (Step 6) over active/stale
         entries - reuses the EXISTING `_CONSOLIDATION_MIN`/
         `_CONSOLIDATION_MAX` Jaccard band and `_classify_conflict()`
         waterfall (no second tokenizer, no second threshold set) to
         find exact duplicates, near-duplicates/refinements, and
         correction/temporal/ambiguous pairs that weren't already merged
         by the live `add_memory()` pipeline (a possible, if unusual,
         state - e.g. from a hand-edited or older-format file). A
         protected entry's base recommendation is NEVER overridden by
         this pass (defense in depth, mirrors `apply_maintenance_plan()`'s
         own protection check)."""
    from .memory_retrieval.query import _WORD_RE

    entries = [m for m in _memories if isinstance(m, dict) and m.get("id") and m.get("text")]
    plan_by_id = {}
    for m in entries:
        plan_by_id[m["id"]] = _plan_action_for_entry(m, now=now)

    # Bounded pairwise sweep - Step 15's "maintenance must remain cheap"
    # is satisfied by this ONLY ever running when explicitly requested
    # (dry-run/health-report/run-maintenance commands), never on ordinary
    # conversation turns.
    considered = [m for m in entries if compute_lifecycle(m, now=now) != "archived"]
    for i in range(len(considered)):
        a = considered[i]
        if _is_protected_from_archival(a):
            continue
        a_tokens = set(w.lower() for w in _WORD_RE.findall(a["text"]))
        if not a_tokens:
            continue
        for j in range(i + 1, len(considered)):
            b = considered[j]
            if _is_protected_from_archival(b):
                continue
            if a.get("category") != b.get("category"):
                continue
            # Two entries already tied together in the SAME unresolved
            # conflict group are already correctly represented by their
            # own "review" recommendation from pass 1 - nothing more for
            # this sweep to add.
            if (a.get("conflict_status") == "ambiguous_conflict" and b.get("conflict_status") == "ambiguous_conflict"
                    and a.get("conflict_group") == b.get("conflict_group")):
                continue

            b_tokens = set(w.lower() for w in _WORD_RE.findall(b["text"]))
            if not b_tokens:
                continue

            norm_a = a["text"].strip().lower()
            norm_b = b["text"].strip().lower()
            if norm_a == norm_b:
                jaccard = 1.0
                exact = True
            else:
                union = a_tokens | b_tokens
                jaccard = len(a_tokens & b_tokens) / len(union) if union else 0.0
                exact = False

            if not exact and not (_CONSOLIDATION_MIN <= jaccard < _CONSOLIDATION_MAX):
                continue

            conflict_type = _classify_conflict(b["text"], a["text"], a.get("category"))

            if exact or conflict_type in ("refinement_forward", "refinement_backward"):
                # Deterministic survivor choice: higher importance wins;
                # ties broken by earlier created_at, then by id (stable,
                # reproducible regardless of dict/list ordering quirks).
                a_key = (_get_importance(a), a.get("created_at") or "", a.get("id") or "")
                b_key = (_get_importance(b), b.get("created_at") or "", b.get("id") or "")
                survivor, loser = (a, b) if a_key >= b_key else (b, a)
                confidence = 0.95 if exact else round(min(0.9, 0.5 + jaccard * 0.4), 2)
                plan_by_id[loser["id"]] = {
                    "memory_id": loser["id"], "action": "consolidate",
                    "reason": f"{'exact duplicate' if exact else 'near-duplicate/refinement'} of memory {survivor['id']}",
                    "confidence": confidence, "consolidate_with": survivor["id"],
                }
            elif conflict_type in ("correction", "temporal_change", "ambiguous_conflict"):
                reason = (
                    "possible unresolved correction/temporal pair - review before merging"
                    if conflict_type != "ambiguous_conflict" else
                    "ambiguous overlap with another memory - conflict unclear"
                )
                for entry in (a, b):
                    plan_by_id[entry["id"]] = {
                        "memory_id": entry["id"], "action": "review",
                        "reason": reason, "confidence": 0.5 if conflict_type != "ambiguous_conflict" else 0.4,
                    }

    # Deterministic output order - by list position in `_memories`, not
    # dict-iteration order (which is insertion-order-stable in modern
    # Python but this makes the guarantee explicit rather than incidental).
    return [plan_by_id[m["id"]] for m in entries]


# ─────────────────────────────────────────────
#  Step 10 - explicit execution layer.
# ─────────────────────────────────────────────

def apply_maintenance_plan(plan_entries):
    """Step 10 - the ONLY function in this section that mutates
    `_memories`. Never called automatically - only from an explicit
    "jalankan maintenance memory"/"rapikan memory" command (Step 12).

    Rules (Step 10's own list, applied literally):
      - "keep"/"review" -> no mutation.
      - "reinforce" -> the SAME conservative +1/cap-3 rule usage-driven
        reinforcement already uses (see `record_memory_usage()`).
      - "archive" -> sets `archived_by_maintenance`/`archived_at` only -
        NEVER deletes. Refuses (defense in depth) if the target is
        currently protected, even if the plan incorrectly says
        otherwise - a stale/hand-edited plan can never bypass protection.
      - "consolidate" -> only applied when `confidence` is at or above
        `_CONSOLIDATION_APPLY_THRESHOLD` AND a valid `consolidate_with`
        survivor is named; the loser's text is preserved in the
        survivor's `history` (reason="maintenance_consolidation") before
        being removed as a separate top-level entry - reuses the exact
        merge pattern `resolve_conflict_by_topic()` already established,
        not a second consolidation mechanism.

    Returns a list of `{"memory_id", "action", "status", ...}` result
    dicts - one per input plan entry - so a caller can report exactly
    what happened (or didn't) for each one, honestly."""
    global _memories
    results = []
    changed = False

    for item in plan_entries:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        memory_id = item.get("memory_id")
        live = next((m for m in _memories if isinstance(m, dict) and m.get("id") == memory_id), None)
        if live is None:
            results.append({"memory_id": memory_id, "action": action, "status": "not_found"})
            continue

        if action in ("keep", "review"):
            results.append({"memory_id": memory_id, "action": action, "status": "no_op"})
            continue

        if action == "archive":
            if _is_protected_from_archival(live):
                results.append({"memory_id": memory_id, "action": action, "status": "blocked_protected"})
                continue
            live["archived_by_maintenance"] = True
            live["archived_at"] = _now_iso()
            changed = True
            results.append({"memory_id": memory_id, "action": action, "status": "applied"})
            continue

        if action == "reinforce":
            current_importance = _get_importance(live)
            if current_importance < _FREQUENCY_REINFORCEMENT_CEILING:
                live["importance"] = current_importance + 1
                changed = True
                results.append({"memory_id": memory_id, "action": action, "status": "applied"})
            else:
                results.append({"memory_id": memory_id, "action": action, "status": "no_op_already_high"})
            continue

        if action == "consolidate":
            confidence = item.get("confidence") or 0
            target_id = item.get("consolidate_with")
            if confidence < _CONSOLIDATION_APPLY_THRESHOLD or not target_id:
                results.append({"memory_id": memory_id, "action": action, "status": "skipped_low_confidence"})
                continue
            survivor = next((m for m in _memories if isinstance(m, dict) and m.get("id") == target_id), None)
            if survivor is None:
                results.append({"memory_id": memory_id, "action": action, "status": "target_not_found"})
                continue
            history = survivor.get("history")
            if not isinstance(history, list):
                history = []
            history.append({
                "text": live["text"],
                "changed_at": _now_iso(),
                "reason": "maintenance_consolidation",
            })
            survivor["history"] = history[-_MAX_MEMORY_HISTORY_ENTRIES:]
            _memories = [m for m in _memories if not (isinstance(m, dict) and m.get("id") == memory_id)]
            changed = True
            results.append({"memory_id": memory_id, "action": action, "status": "applied", "merged_into": target_id})
            continue

        results.append({"memory_id": memory_id, "action": action, "status": "unknown_action"})

    if changed:
        _save()
    return results


# ─────────────────────────────────────────────
#  Step 11 - dry-run preview (read-only text rendering of the plan).
# ─────────────────────────────────────────────

_MAINTENANCE_ACTION_HEADERS = {
    "keep": "KEEP",
    "reinforce": "REINFORCE",
    "archive": "ARCHIVE",
    "consolidate": "CONSOLIDATE",
    "review": "REVIEW",
}


def preview_maintenance_text(now=None):
    """Step 11 - a human-readable dry-run preview, grouped by action,
    matching the sprint brief's own example format. Calls
    `analyze_memory_maintenance()` ONLY - never `apply_maintenance_plan()`.
    Guaranteed not to mutate persistent state (the planner it calls is
    itself analysis-only)."""
    plan = analyze_memory_maintenance(now=now)
    if not plan:
        return "Memory Maintenance Preview:\n(no long-term memories to analyze)"

    by_id = {m["id"]: m for m in _memories if isinstance(m, dict) and m.get("id")}
    grouped = {}
    for item in plan:
        grouped.setdefault(item["action"], []).append(item)

    lines = ["Memory Maintenance Preview:"]
    for action in ("keep", "reinforce", "archive", "consolidate", "review"):
        items = grouped.get(action)
        if not items:
            continue
        lines.append("")
        lines.append(_MAINTENANCE_ACTION_HEADERS.get(action, action.upper()))
        for item in items:
            entry = by_id.get(item["memory_id"])
            text = entry["text"] if entry else "(memory not found)"
            lines.append(f'- "{text}"')
            lines.append(f'  reason: {item["reason"]}')
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Step 14 - deterministic, read-only health report.
# ─────────────────────────────────────────────

def memory_health_report(now=None):
    """Step 14 - a deterministic snapshot dict, entirely read-only
    (reuses `analyze_memory_maintenance()` for the duplicate/conflict/
    review counts rather than re-deriving that logic a second time)."""
    entries = [m for m in _memories if isinstance(m, dict) and m.get("text")]
    plan = analyze_memory_maintenance(now=now)

    lifecycle_counts = {"active": 0, "stale": 0, "archived": 0}
    importance_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    usage = {"never_retrieved": 0, "rarely_retrieved": 0, "frequently_retrieved": 0}
    # Memory Learning & Feedback Loop sprint (Section 17/18) - additive
    # dashboard/health signal, computed the same read-only way as every
    # other bucket in this report (never a new health-logic engine).
    usefulness_buckets = {"low": 0, "medium": 0, "high": 0}
    total_positive_feedback = 0
    total_negative_feedback = 0
    protected = 0
    conflict_groups = set()

    for m in entries:
        lifecycle = compute_lifecycle(m, now=now)
        lifecycle_counts[lifecycle] = lifecycle_counts.get(lifecycle, 0) + 1
        importance_counts[_get_importance(m)] = importance_counts.get(_get_importance(m), 0) + 1
        rc = _get_retrieval_count(m)
        if rc == 0:
            usage["never_retrieved"] += 1
        elif rc < _REINFORCEMENT_RETRIEVAL_THRESHOLD:
            usage["rarely_retrieved"] += 1
        else:
            usage["frequently_retrieved"] += 1
        usefulness = _get_usefulness(m)
        if usefulness < 0.35:
            usefulness_buckets["low"] += 1
        elif usefulness < _USEFULNESS_PROTECTS_FROM_ARCHIVAL:
            usefulness_buckets["medium"] += 1
        else:
            usefulness_buckets["high"] += 1
        total_positive_feedback += _get_positive_feedback_count(m)
        total_negative_feedback += _get_negative_feedback_count(m)
        if _is_protected_from_archival(m):
            protected += 1
        if m.get("conflict_status") == "ambiguous_conflict" and m.get("conflict_group"):
            raw_group = m.get("conflict_group")
            # Same defensive coercion as `_select_memories_for_prompt()`/
            # `_tag_ambiguous_conflict()` - a malformed, unhashable
            # `conflict_group` (e.g. a hand-edited dict) must never crash
            # report generation.
            group_key = raw_group if isinstance(raw_group, (str, int)) else str(raw_group)
            conflict_groups.add(group_key)

    return {
        "total": len(entries),
        "lifecycle": lifecycle_counts,
        "importance": importance_counts,
        "usage": usage,
        "usefulness": usefulness_buckets,
        "total_positive_feedback": total_positive_feedback,
        "total_negative_feedback": total_negative_feedback,
        "potential_duplicates": sum(1 for p in plan if p["action"] == "consolidate"),
        "potential_conflicts": len(conflict_groups),
        "review_required": sum(1 for p in plan if p["action"] == "review"),
        "protected_core_memories": protected,
        # Long-Term Memory Self-Healing / Recovery Hardening sprint - the
        # smallest possible observability addition: passthrough of the
        # in-memory-only persistence status `_load()` most recently
        # computed (never recomputed here, same "don't re-derive health
        # logic" discipline as every other field in this report). Never
        # persisted inside `config/long_term_memory.json` itself; never a
        # second status model - `luno/dashboard/collectors.py::collect_
        # memory_health()` already returns this whole dict verbatim, so
        # this key reaches the dashboard with zero dashboard-side changes.
        "persistence_status": get_persistence_status(),
    }


def format_memory_health_report(report):
    """Step 14's own illustrative text layout - pure formatting, takes an
    already-computed `memory_health_report()` dict (never recomputes)."""
    lifecycle = report["lifecycle"]
    importance = report["importance"]
    usage = report["usage"]
    lines = [
        "Memory Health",
        f"Total: {report['total']}",
        f"Active: {lifecycle.get('active', 0)}",
        f"Stale: {lifecycle.get('stale', 0)}",
        f"Archived: {lifecycle.get('archived', 0)}",
        "",
        "Importance:",
    ]
    for level in (0, 1, 2, 3, 4):
        lines.append(f"{level}: {importance.get(level, 0)}")
    lines += [
        "",
        "Usage:",
        f"Never retrieved: {usage.get('never_retrieved', 0)}",
        f"Rarely retrieved: {usage.get('rarely_retrieved', 0)}",
        f"Frequently retrieved: {usage.get('frequently_retrieved', 0)}",
        "",
        "Usefulness:",
        f"Low: {report.get('usefulness', {}).get('low', 0)}",
        f"Medium: {report.get('usefulness', {}).get('medium', 0)}",
        f"High: {report.get('usefulness', {}).get('high', 0)}",
        f"Total positive feedback: {report.get('total_positive_feedback', 0)}",
        f"Total negative feedback: {report.get('total_negative_feedback', 0)}",
        "",
        f"Potential duplicates: {report['potential_duplicates']}",
        f"Potential conflicts: {report['potential_conflicts']}",
        f"Review required: {report['review_required']}",
        f"Protected core memories: {report['protected_core_memories']}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Step 12 - deterministic manual command detectors. Same discipline as
#  every previous sprint's command detectors in this file: anchored,
#  small, fixed trigger-phrase sets, EXPLICIT USER INTENT -> ACT /
#  ORDINARY CONVERSATION -> DO NOT ACT. Exactly the 8 example commands
#  the brief itself lists - no extras added "for feature count" (same
#  scope discipline the Memory Conflict Resolution sprint's own Section
#  16 established as precedent).
# ─────────────────────────────────────────────

_MEMORY_HEALTH_RE = re.compile(
    r'^(?:tolong\s+)?cek\s+kesehatan\s+memory\.?$|'
    r'^(?:please\s+)?(?:check\s+)?memory\s+health\.?$',
    re.IGNORECASE,
)

#: "analisa memory" and "cek memory yang sudah basi" are treated as
#: synonyms of "preview maintenance memory" - all three produce the same
#: analysis-only dry-run output; the brief does not describe a different
#: output shape for any of them, so distinguishing them further would add
#: command surface without adding real behavior.
_MEMORY_MAINTENANCE_PREVIEW_RE = re.compile(
    r'^(?:tolong\s+)?analisa\s+memory\.?$|'
    r'^(?:tolong\s+)?cek\s+memory\s+yang\s+sudah\s+basi\.?$|'
    r'^(?:tolong\s+)?preview\s+maintenance\s+memory\.?$|'
    r'^(?:please\s+)?(?:preview\s+)?analyze\s+memory\.?$|'
    r'^(?:please\s+)?preview\s+memory\s+maintenance\.?$',
    re.IGNORECASE,
)

#: "rapikan memory" and "jalankan maintenance memory" both EXECUTE the
#: plan (Step 10) - same reasoning as above.
_MEMORY_MAINTENANCE_RUN_RE = re.compile(
    r'^(?:tolong\s+)?rapikan\s+memory\.?$|'
    r'^(?:tolong\s+)?jalankan\s+maintenance\s+memory\.?$|'
    r'^(?:please\s+)?(?:run|tidy\s+up)\s+memory(?:\s+maintenance)?\.?$',
    re.IGNORECASE,
)

_ARCHIVE_MEMORY_BY_ID_RE = re.compile(
    r'^(?:tolong\s+)?arsipkan\s+memory\s+(?:nomor|nomer|number|#)\s*([a-zA-Z0-9]+)\.?$|'
    r'^(?:please\s+)?archive\s+memory\s+(?:number|#)\s*([a-zA-Z0-9]+)\.?$',
    re.IGNORECASE,
)

_UNARCHIVE_LAST_MEMORY_RE = re.compile(
    r'^(?:tolong\s+)?jangan\s+arsipkan\s+memory\s+ini\.?$|'
    r"^(?:please\s+)?don'?t\s+archive\s+this\s+memory\.?$",
    re.IGNORECASE,
)


def detect_memory_health_command(user_text):
    return bool(_MEMORY_HEALTH_RE.match(user_text.strip()))


def detect_memory_maintenance_preview_command(user_text):
    return bool(_MEMORY_MAINTENANCE_PREVIEW_RE.match(user_text.strip()))


def detect_memory_maintenance_run_command(user_text):
    return bool(_MEMORY_MAINTENANCE_RUN_RE.match(user_text.strip()))


def detect_archive_memory_by_id_command(user_text):
    """Return the target memory id, or `None`."""
    m = _ARCHIVE_MEMORY_BY_ID_RE.match(user_text.strip())
    if not m:
        return None
    return next(g for g in m.groups() if g)


def detect_unarchive_last_memory_command(user_text):
    return bool(_UNARCHIVE_LAST_MEMORY_RE.match(user_text.strip()))


def archive_memory_by_id(memory_id):
    """Explicit, single-target archive (Step 12's "arsipkan memory nomor
    12") - reuses the SAME protection check `apply_maintenance_plan()`
    uses, so a protected memory refuses here too rather than silently
    archiving it just because a specific id was named. Returns
    `("archived"|"protected"|"not_found", entry_or_None)`."""
    live = next((m for m in _memories if isinstance(m, dict) and m.get("id") == memory_id), None)
    if live is None:
        return "not_found", None
    if _is_protected_from_archival(live):
        return "protected", dict(live)
    live["archived_by_maintenance"] = True
    live["archived_at"] = _now_iso()
    _save()
    return "archived", dict(live)


def unarchive_last_memory():
    """Step 12's "jangan arsipkan memory ini" - un-archives the most-
    recently-touched memory (same deterministic target-selection helper
    `mark_last_memory_important()`/`forget_last_memory()` already use for
    an otherwise session-less "ini"/"this" reference). Returns the
    updated entry, or `None` if there's nothing to act on / it wasn't
    archived in the first place."""
    entry = _most_recently_touched_memory()
    if entry is None or not entry.get("archived_by_maintenance"):
        return None
    entry["archived_by_maintenance"] = False
    entry.pop("archived_at", None)
    _save()
    return dict(entry)


# ─────────────────────────────────────────────
#  MEMORY DASHBOARD & OBSERVABILITY sprint - thin, additive, ID-TARGETED
#  counterparts to three operations above that only ever resolved their
#  target via `_most_recently_touched_memory()` (a "the thing we just
#  talked about" heuristic that makes sense for a spoken/typed command
#  with no other way to say "this one", but is meaningless for a
#  dashboard where the user has already clicked a specific row with a
#  real `id`). Each function below performs the EXACT SAME mutation its
#  last-touched sibling already does - zero new business logic, just a
#  different, explicit way to pick the target - mirroring the pattern
#  `archive_memory_by_id()` already established as the id-targeted
#  counterpart to `unarchive_last_memory()`'s own last-touched sibling.
#  See docs/change_impact/memory_dashboard.md's "Gap found" section for
#  the full reasoning.
# ─────────────────────────────────────────────

def mark_memory_important_by_id(memory_id):
    """Id-targeted counterpart to `mark_last_memory_important()` - same
    three-field mutation (`importance=4`, `source="user_explicit"`,
    `updated_at` stamped), looked up by `id` instead of "most recently
    touched". Returns the updated entry, or `None` if `memory_id`
    doesn't exist."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            m["importance"] = 4
            m["source"] = "user_explicit"
            m["updated_at"] = _now_iso()
            _save()
            print(f"[Memory] ✓ Marked important/permanent: {m['text']}")
            return dict(m)
    return None


def unarchive_memory_by_id(memory_id):
    """Id-targeted counterpart to `unarchive_last_memory()` - same
    two-field mutation (`archived_by_maintenance` cleared, `archived_at`
    popped), looked up by `id` instead of "most recently touched".
    Returns the updated entry, or `None` if `memory_id` doesn't exist or
    wasn't archived in the first place."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            if not m.get("archived_by_maintenance"):
                return None
            m["archived_by_maintenance"] = False
            m.pop("archived_at", None)
            _save()
            return dict(m)
    return None


def is_memory_protected(memory_id):
    """Public, read-only wrapper around the existing private
    `_is_protected_from_archival()` - lets a caller outside this module
    (the Memory Dashboard's Protected Memory badge, Phase 10) ask "is
    this memory protected from automatic archival" without duplicating
    the protection rule a second time and without reaching into a
    private (`_`-prefixed) internal. Returns `False` for an unknown
    `memory_id` (nothing to protect)."""
    entry = get_memory(memory_id)
    if entry is None:
        return False
    return _is_protected_from_archival(entry)


def get_memory_importance(entry):
    """Public, read-only wrapper around the existing private
    `_get_importance()` - same signature (accepts a raw memory dict, the
    shape `list_memories()`/`get_memory()`/`search_memories()` already
    return), same backward-compatible on-the-fly recomputation for a
    pre-sprint entry missing the `importance` key. Exists so a caller
    outside this module (the Memory Dashboard's importance filter/badge)
    never needs to reach into a private internal or re-derive the same
    classification a second time."""
    return _get_importance(entry)


def get_memory_retrieval_count(entry):
    """Public, read-only wrapper around the existing private
    `_get_retrieval_count()` - same signature, same backward-compatible
    zero-default for an entry that predates usage tracking. Exists for
    the same reason as `get_memory_importance()` above."""
    return _get_retrieval_count(entry)


# ═════════════════════════════════════════════════════════════════════
#  MEMORY EVALUATION & SELF-CALIBRATION sprint
#
#  MOST IMPORTANT RULE, restated so it stays visible at the top of the
#  section that could most easily violate it: an evaluation score is
#  NOT truth. Everything below measures how USEFUL/RELEVANT/CONFIRMED a
#  memory has been, from evidence already sitting on the entry - never
#  whether its content is factually correct. `evaluate_memory()` and
#  `calibrate_memory()` never write a "this is true" signal anywhere,
#  there is no `truth_score` field, and nothing in this file ever reads
#  `evaluation_score` as a substitute for `importance`,
#  `usefulness_score`, or a Verified Fact. That distinction is enforced
#  structurally in every subsection below, not just asserted in prose:
#    - Step 5's `evaluate_memory()` computes `score` from RAW EVIDENCE
#      COUNTERS ONLY (feedback/correction/conflict/retrieval-outcome
#      counts) - it never reads `importance` or `usefulness_score` to
#      produce the score itself, so neither can "launder" itself into a
#      truth claim through this new field.
#    - `calibrate_memory()` (Step 8) is the ONLY function that persists
#      `evaluation_score`, and it writes NOTHING else - not `text`, not
#      `history`, not `importance`, not `conflict_group`, not `source`,
#      not a `lifecycle` field (which this codebase never persists for
#      ANY entry - see `compute_lifecycle()` above).
#    - Retrieval ranking (`memory_context.py`) already places usefulness
#      strictly after relevance/importance in `ContextItem._rank_key()`;
#      this sprint does not touch that tuple or add evaluation to it -
#      evaluation is explicitly NOT a retrieval-ranking signal (Step 6
#      only ever ADDS observational metadata about what ranking already
#      decided, it never feeds back into ranking itself).
#    - Verified Facts (`memory_guard.py`/`VerifiedFactStore`) and
#      Episodic Memory (`episodic_memory.py`) are never imported,
#      referenced, or mutated anywhere in this section - proven by a
#      dedicated `inspect.getsource()` isolation test in
#      `tests/test_memory_evaluation.py`, the same technique the Memory
#      Learning & Feedback Loop sprint's own isolation test already
#      established as this codebase's precedent.
# ═════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────
#  Step 3 - additive evaluation-evidence schema. Every field below is
#  OPTIONAL on an entry (absent on every pre-sprint-4 memory) and every
#  accessor below returns a safe, neutral default rather than raising -
#  identical backward-compatibility shape to `_get_importance()`/
#  `_get_usefulness()`/`_get_retrieval_count()` above. `evaluation_score`
#  is the only one of these that is ever WRITTEN outside this section
#  (by `calibrate_memory()`) - the rest are simple, additive counters
#  bumped at the one existing call site each already flows through
#  (`_tag_ambiguous_conflict()` for `conflict_event_count`,
#  `update_memory()` for `correction_count`, `record_context_selection()`
#  below for `retrieval_success_count`/`retrieval_miss_count`, and the
#  feedback/correction handlers below for `feedback_event_count`).
# ─────────────────────────────────────────────

#: Bounds - same shape as `MEMORY_USEFULNESS_MIN/MAX` above (Step 3's
#: own explicit "evaluation_score harus dibatasi 0.0-1.0").
MEMORY_EVALUATION_MIN = 0.0
MEMORY_EVALUATION_MAX = 1.0

#: Neutral default for a memory with no evaluation evidence yet (or one
#: that predates this sprint and has never been calibrated) - same
#: "neutral, not 0.0" reasoning as `_DEFAULT_USEFULNESS_SCORE` above: a
#: freshly-created memory has not yet proven itself useless, it simply
#: hasn't been observed enough to say either way.
_DEFAULT_EVALUATION_SCORE = 0.5

#: Per-signal score deltas (Step 4/5) - deliberately small, bounded, and
#: distinct per evidence TYPE (never a single shared constant) so no one
#: signal alone can swing the score from one extreme to the other. Mirrors
#: the Memory Learning sprint's own `_USEFULNESS_*_DELTA` precedent.
_EVAL_POSITIVE_FEEDBACK_DELTA = 0.12
_EVAL_NEGATIVE_FEEDBACK_DELTA = 0.12
_EVAL_CORRECTION_DELTA = 0.18  # a correction is stronger negative evidence than a bare "no"
_EVAL_REINFORCEMENT_DELTA = 0.05  # each explicit reinforcement (Step 4's "memory direinforce")
_EVAL_SUCCESSFUL_RETRIEVAL_DELTA = 0.015  # per actually-used context selection - small, usage-shaped
_EVAL_RETRIEVAL_MISS_DELTA = 0.0  # Step 4: "retrieved repeatedly tapi tidak pernah positive" is
                                   # handled as a SEPARATE, capped penalty below, not a per-miss
                                   # delta (a single miss proves nothing on its own).
_EVAL_UNCONFIRMED_REPEAT_USE_PENALTY = 0.10  # Step 4's "diambil berkali-kali tapi tidak pernah
                                              # mendapat feedback positif" - a single, bounded,
                                              # ONE-TIME penalty (not per-retrieval) once a memory
                                              # crosses a repeat-use threshold with zero positive
                                              # signal ever recorded.
_EVAL_UNCONFIRMED_REPEAT_USE_THRESHOLD = 5  # same threshold `_REINFORCEMENT_RETRIEVAL_THRESHOLD`
                                             # already uses elsewhere in this file - reused, not
                                             # redefined, so "repeatedly" means the same thing in
                                             # both places.
_EVAL_CONFLICT_DELTA = 0.10  # unresolved conflict - negative evidence, Step 4's own list
_EVAL_OBSOLETE_WORDING_DELTA = 0.08  # matches the SAME `_OBSOLETE_WORDING_RE` maintenance already uses
_EVAL_STALE_TIME_PENALTY = 0.05  # "kehilangan relevansi seiring waktu" - small, age-based, never
                                  # the dominant term (dominant terms are always explicit evidence)
_EVAL_HISTORICAL_SURVIVAL_BONUS = 0.05  # Step 5's "historical memory tetap bisa dapat evaluation
                                         # score tinggi" - a memory that has survived one or more
                                         # corrections/updates (has `history`) and accumulated NO
                                         # further negative evidence since is treated as having
                                         # earned a small amount of extra trust, not penalized for
                                         # having a past.

#: Step 4's explicit "usage alone does not prove truth" ceiling - mirrors
#: `_USEFULNESS_USAGE_NUDGE_CEILING` above: successful-retrieval deltas
#: alone (with zero explicit feedback ever recorded) can raise the score,
#: but never past this ceiling. Only explicit feedback/reinforcement can
#: cross it.
_EVAL_USAGE_ONLY_CEILING = 0.75

#: Step 5's confidence model (Step 9: "evaluation confidence" is a
#: SEPARATE concept from truth, and is never persisted - always
#: recomputed fresh, same treatment as `compute_lifecycle()`). Confidence
#: grows with the TOTAL VOLUME of evidence observed (more observations =
#: more confident the score reflects something real), independent of
#: whether that evidence was positive or negative.
_EVAL_CONFIDENCE_PER_EVIDENCE_UNIT = 0.08
_EVAL_CONFIDENCE_MAX_FROM_EVIDENCE = 0.95  # never claim full (1.0) confidence from bounded, finite
                                            # evidence - a small margin of humility always remains


def _get_retrieval_success_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("retrieval_success_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_retrieval_miss_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("retrieval_miss_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_feedback_event_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("feedback_event_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_correction_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("correction_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_conflict_event_count(entry):
    if not isinstance(entry, dict):
        return 0
    value = entry.get("conflict_event_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _get_evaluation_score(entry):
    """Backward-compatible accessor - same shape as `_get_usefulness()`:
    a pre-sprint-4 entry (or a hand-edited/malformed value) simply lacks
    a persisted `evaluation_score` yet and defaults to the neutral
    `_DEFAULT_EVALUATION_SCORE` rather than crashing or guessing. This is
    the ONLY evaluation accessor that reads a PERSISTED value - every
    other evaluation output (`confidence`, `strengths`, `weaknesses`,
    `recommendation`) is always recomputed fresh by `evaluate_memory()`,
    never read back from storage."""
    if not isinstance(entry, dict):
        return _DEFAULT_EVALUATION_SCORE
    value = entry.get("evaluation_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and MEMORY_EVALUATION_MIN <= value <= MEMORY_EVALUATION_MAX:
        return float(value)
    return _DEFAULT_EVALUATION_SCORE


def _get_last_evaluated_at(entry):
    if not isinstance(entry, dict):
        return None
    value = entry.get("last_evaluated_at")
    return value if isinstance(value, str) and value else None


def get_memory_evaluation_score(entry):
    """Public, read-only wrapper - the LAST CALIBRATED score (may be
    stale relative to evidence recorded since the last `calibrate_memory()`
    call; call `evaluate_memory()` directly for an always-fresh number)."""
    return _get_evaluation_score(entry)


def get_memory_last_evaluated_at(entry):
    return _get_last_evaluated_at(entry)


def get_memory_evidence_counts(entry):
    """Public, read-only snapshot of every raw evidence counter this
    section adds - the Memory Dashboard's "Evidence count" column (Step
    11) reads this rather than reaching into six separate private
    accessors itself."""
    return {
        "positive_feedback_count": _get_positive_feedback_count(entry),
        "negative_feedback_count": _get_negative_feedback_count(entry),
        "correction_count": _get_correction_count(entry),
        "conflict_event_count": _get_conflict_event_count(entry),
        "retrieval_success_count": _get_retrieval_success_count(entry),
        "retrieval_miss_count": _get_retrieval_miss_count(entry),
        "feedback_event_count": _get_feedback_event_count(entry),
    }


def get_memory_outcome_summary(memory_id):
    """Memory Outcome Telemetry & Closed-Loop Learning sprint (Step 14) -
    a single, bounded, read-only snapshot of everything this and the
    prior evaluation sprint know about one memory's evidence trail.
    Purely a re-shaped combination of already-existing public accessors
    (`get_memory_evidence_counts()` for the raw counters,
    `evaluate_memory()` for the live score/confidence) - computes
    nothing new, never mutates, never raises for an unknown id (returns
    `None` instead, same "honest about not finding it" convention as
    `get_memory()`).

    Deliberately does NOT expose `text`/`history`/any conversational
    transcript - Step 14's own explicit "tidak boleh membuka raw
    transcript" - only the bounded evidence counters and derived
    evaluation numbers, which is also all this function's callers (the
    dashboard's outcome panel, tests) actually need."""
    entry = get_memory(memory_id)
    if entry is None:
        return None
    evaluation = evaluate_memory(entry)
    counts = get_memory_evidence_counts(entry)
    return {
        "memory_id": memory_id,
        "retrieval_success_count": counts["retrieval_success_count"],
        "retrieval_miss_count": counts["retrieval_miss_count"],
        "feedback_event_count": counts["feedback_event_count"],
        "correction_count": counts["correction_count"],
        "evaluation_score": evaluation["score"],
        "evaluation_confidence": evaluation["confidence"],
        "last_evaluated_at": get_memory_last_evaluated_at(entry),
    }


# ─────────────────────────────────────────────
#  Step 4/5 - the deterministic, pure evaluator. Reads ONLY raw evidence
#  counters + already-existing, already-pure helpers
#  (`compute_lifecycle()`, `_OBSOLETE_WORDING_RE`, `conflict_status`) -
#  deliberately NEVER reads `importance` or `usefulness_score` to compute
#  `score` itself (see the section banner above). Never mutates `entry`,
#  never calls `_save()`, never appears in any write path.
# ─────────────────────────────────────────────

_EVALUATION_RECOMMENDATIONS = ("keep", "reinforce", "review", "deprioritize", "archive_candidate")

#: Public alias - lets a caller outside this module (the Memory
#: Dashboard's overview tally) enumerate the closed recommendation
#: vocabulary without reaching into a private, underscore-prefixed
#: internal (same "public wrapper for anything a dashboard needs"
#: discipline every `get_memory_*`/`MEMORY_*` public name in this file
#: already follows).
MEMORY_EVALUATION_RECOMMENDATIONS = _EVALUATION_RECOMMENDATIONS


def evaluate_memory(entry, now=None):
    """Step 5 - a pure function of `(entry, now)`. Same inputs always
    produce the same output (Step 6's determinism requirement, verified
    by a dedicated repeatability test). Returns
    `{"score", "confidence", "strengths", "weaknesses", "recommendation"}`.

    Guarantees this function upholds by construction (each one traceable
    to a specific line below, not just documented in prose):
      - `importance` is never read here -> it cannot "directly become"
        the evaluation score (Step 5's explicit prohibition).
      - `usefulness_score` is never read here either -> usefulness alone
        cannot make a memory "considered true" through this path (it can
        only ever have already nudged `usefulness_score` itself, a
        completely separate field).
      - Negative feedback/correction/conflict each contribute a bounded,
        capped DELTA, never an automatic floor/ceiling override -> none
        of them "automatically mean" the memory is wrong on their own
        (a single negative event moves the score by a small, recoverable
        amount, exactly like `apply_negative_feedback()`'s own delta).
      - A memory with `history` (has survived a correction/update) is not
        penalized for that alone - `_EVAL_HISTORICAL_SURVIVAL_BONUS`
        only adds, and only when no negative evidence has followed the
        most recent history entry - Step 5's "historical memory tetap
        bisa dapat evaluation score tinggi"."""
    if not isinstance(entry, dict):
        return {
            "score": _DEFAULT_EVALUATION_SCORE, "confidence": 0.0,
            "strengths": [], "weaknesses": [], "recommendation": "keep",
        }

    positive = _get_positive_feedback_count(entry)
    negative = _get_negative_feedback_count(entry)
    corrections = _get_correction_count(entry)
    conflicts = _get_conflict_event_count(entry)
    success = _get_retrieval_success_count(entry)
    misses = _get_retrieval_miss_count(entry)
    retrieval_count = _get_retrieval_count(entry)
    lifecycle = compute_lifecycle(entry, now=now)
    text = entry.get("text", "") or ""
    is_obsolete_wording = bool(_OBSOLETE_WORDING_RE.search(text))
    has_history = isinstance(entry.get("history"), list) and len(entry["history"]) > 0
    is_unresolved_conflict = entry.get("conflict_status") == "ambiguous_conflict"

    score = _DEFAULT_EVALUATION_SCORE
    strengths = []
    weaknesses = []
    evidence_units = 0

    # Positive evidence (Step 4's list, in order).
    if positive:
        score += _EVAL_POSITIVE_FEEDBACK_DELTA * positive
        strengths.append(f"{positive} positive confirmation(s)")
        evidence_units += positive
    if success:
        # Bounded usage-only contribution - see `_EVAL_USAGE_ONLY_CEILING`.
        usage_only_component = min(score + _EVAL_SUCCESSFUL_RETRIEVAL_DELTA * success, _EVAL_USAGE_ONLY_CEILING)
        if positive or negative or corrections:
            score += _EVAL_SUCCESSFUL_RETRIEVAL_DELTA * success
        else:
            score = usage_only_component
        strengths.append(f"{success} relevant retrieval(s) actually used in context")
        evidence_units += success
    if lifecycle != "archived" and (positive or success):
        strengths.append("remains relevant after time has passed")
    if has_history and negative == 0 and corrections == 0:
        score += _EVAL_HISTORICAL_SURVIVAL_BONUS
        strengths.append("has an update history with no negative evidence since")

    # Negative evidence (Step 4's list, in order).
    if negative:
        score -= _EVAL_NEGATIVE_FEEDBACK_DELTA * negative
        weaknesses.append(f"{negative} negative feedback event(s)")
        evidence_units += negative
    if corrections:
        score -= _EVAL_CORRECTION_DELTA * corrections
        weaknesses.append(f"{corrections} correction(s)")
        evidence_units += corrections
    if success == 0 and misses >= _EVAL_UNCONFIRMED_REPEAT_USE_THRESHOLD and positive == 0:
        # Step 4: "memory yang diambil berkali-kali tapi tidak pernah
        # mendapat feedback positif" - a single, bounded, one-time penalty,
        # not compounded per additional miss beyond the threshold.
        score -= _EVAL_UNCONFIRMED_REPEAT_USE_PENALTY
        weaknesses.append(f"retrieved {misses} time(s) but never confirmed useful")
        evidence_units += 1
    elif retrieval_count == 0 and success == 0 and misses == 0 and positive == 0 and negative == 0:
        # No usage evidence at all yet - genuinely neutral, not a weakness.
        pass
    if conflicts and is_unresolved_conflict:
        score -= _EVAL_CONFLICT_DELTA
        weaknesses.append(f"currently part of an unresolved conflict ({conflicts} event(s))")
        evidence_units += conflicts
    if is_obsolete_wording:
        score -= _EVAL_OBSOLETE_WORDING_DELTA
        weaknesses.append("contains temporary/obsolete wording")
        evidence_units += 1
    if lifecycle == "stale" and not (positive or success):
        score -= _EVAL_STALE_TIME_PENALTY
        weaknesses.append("stale by age with no recent confirming evidence")
        evidence_units += 1

    score = round(max(MEMORY_EVALUATION_MIN, min(MEMORY_EVALUATION_MAX, score)), 4)

    # Step 9 - confidence is evidence VOLUME, not evidence direction, and
    # is always recomputed fresh (never persisted) - see the section
    # banner and `_EVAL_CONFIDENCE_PER_EVIDENCE_UNIT` above.
    confidence = round(min(_EVAL_CONFIDENCE_MAX_FROM_EVIDENCE, evidence_units * _EVAL_CONFIDENCE_PER_EVIDENCE_UNIT), 4)

    # Recommendation (Step 5) - advisory-only vocabulary, deliberately
    # SEPARATE from the maintenance planner's executable action set (see
    # the section banner). `is_unresolved_conflict` always wins first -
    # Step 10's "evaluation tidak boleh membuat conflict yang belum
    # selesai otomatis hilang" - an unresolved conflict is always at
    # least "review", regardless of score.
    # "Real" (interaction-shaped) evidence, as opposed to passive/
    # environmental signals (age-based staleness, obsolete wording) that
    # also feed `evidence_units`/`score` above but were never actually
    # OBSERVED from a human or a genuine retrieval outcome - only the
    # former should be able to trigger the "review" recommendation below,
    # otherwise every merely-stale-with-zero-interaction memory would
    # misleadingly read as "ambiguous, needs review" (it isn't ambiguous,
    # it simply has no evidence yet, which `evidence_units == 0` already
    # handles as "keep" a few lines up - staleness alone must not
    # re-trigger a different branch here).
    has_interaction_evidence = bool(positive or negative or corrections or success or (conflicts and is_unresolved_conflict))
    if is_unresolved_conflict:
        recommendation = "review"
    elif evidence_units == 0:
        # No evidence at all yet - nothing to calibrate on, stay put.
        recommendation = "keep"
    elif has_interaction_evidence and confidence < 0.25 and 0.4 <= score <= 0.6:
        # High usefulness-shaped signal but not enough evidence volume to
        # trust it yet (Step 10's "high usefulness tapi confidence rendah
        # -> review").
        recommendation = "review"
    elif score >= 0.75 and confidence >= 0.25:
        recommendation = "reinforce"
    elif score <= 0.25 and (is_obsolete_wording or lifecycle == "archived"):
        recommendation = "archive_candidate"
    elif score <= 0.35:
        recommendation = "deprioritize"
    else:
        recommendation = "keep"

    return {
        "score": score, "confidence": confidence,
        "strengths": strengths, "weaknesses": weaknesses,
        "recommendation": recommendation,
    }


def get_memory_evaluation_explanation(entry, now=None):
    """Public, read-only wrapper around `_explain_evaluation()` below -
    same reasoning as every other `get_memory_*` explanation wrapper in
    this file (the Memory Dashboard's detail view, Step 12, should never
    need to reach into a private internal)."""
    return _explain_evaluation(entry, now=now)


def _explain_evaluation(entry, now=None):
    """Step 12 - renders `evaluate_memory()`'s output in the sprint
    brief's own illustrative format. Purely read-only (calls
    `evaluate_memory()`, which itself never mutates). Deliberately avoids
    any language implying the system knows absolute truth (Step 12's
    explicit instruction) - "Evaluation"/"Confidence"/"Recommendation",
    never "Truth" or "Verified"."""
    result = evaluate_memory(entry, now=now)
    lines = [
        f"Evaluation: {result['score']:.2f}",
        f"Confidence: {result['confidence']:.2f}",
        "",
        "Positive evidence:",
    ]
    if result["strengths"]:
        lines.extend(f"+ {s}" for s in result["strengths"])
    else:
        lines.append("+ (none recorded yet)")
    lines += ["", "Negative evidence:"]
    if result["weaknesses"]:
        lines.extend(f"- {w}" for w in result["weaknesses"])
    else:
        lines.append("- (none recorded yet)")
    lines += ["", "Recommendation:", result["recommendation"].upper().replace("_", " ")]
    return "\n".join(lines)


def get_memory_selection_explanation(entry, now=None):
    """Memory Outcome Telemetry & Closed-Loop Learning sprint (Step 16) -
    Public, read-only "why would/wouldn't this memory get selected"
    explanation, built entirely from already-PERSISTED, bounded signals
    (`importance`/`usefulness_score`/`evaluation_score`/`lifecycle`/
    `retrieval_success_count`/`retrieval_miss_count`) - never from a
    live, per-turn `MemoryTurnTrace` (those are transient, in-process,
    and scoped to `main_runtime_demo.py`'s own `PlannerBridgeModule`
    instance for the duration of one conversation; the Memory Dashboard
    is a separate, stateless HTTP read path with no "current query" of
    its own, and persisting a query-by-query replay log to bridge that
    gap would violate this sprint's own "jangan menyimpan full
    conversation transcript" / "bounded" constraints). This is therefore
    a STANDING explanation of the memory's own selection-worthiness
    signals ("if queried right now, here is this memory's evidence
    profile"), not a literal replay of one specific past turn - see
    `docs/change_impact/memory_outcome_telemetry.md` for the full
    scoping rationale. Deliberately avoids any language implying the
    system knows absolute truth or made an unexplainable judgment call -
    never "AI decided this."."""
    if not isinstance(entry, dict):
        return "No selection evidence available."
    importance = _get_importance(entry)
    usefulness = _get_usefulness(entry)
    evaluation = evaluate_memory(entry, now=now)
    lifecycle = compute_lifecycle(entry, now=now)
    protected = _is_protected_from_archival(entry)
    success = _get_retrieval_success_count(entry)
    miss = _get_retrieval_miss_count(entry)

    lines = ["Selection-worthiness signals (standing, not tied to one specific past query):"]
    lines.append(f"Importance: {importance}/4" + (" (protected)" if protected else ""))
    lines.append(f"Usefulness: {usefulness:.2f}")
    lines.append(f"Evaluation: {evaluation['score']:.2f} (confidence {evaluation['confidence']:.2f})")
    lines.append(f"Lifecycle: {lifecycle}")
    if lifecycle == "archived":
        lines.append("Currently EXCLUDED from ambient retrieval - archived memories are not offered as candidates.")

    lines.append("")
    if success or miss:
        lines.append(
            f"Selection history: selected into context {success} time(s); "
            f"was a relevant candidate but lost to ranking/budget {miss} time(s)."
        )
        if miss and not success:
            lines.append("Candidate matched a query's topic before, but relevance/budget ranked other content higher every time.")
    else:
        lines.append("This memory has not yet been a candidate for any real query.")

    # Memory Decision Quality & Adaptive Retrieval sprint - per-category
    # evidence, so this standing explanation can also answer "is this
    # memory consistently useful in SOME kinds of conversation but not
    # others" - never claims truth, only reports the bounded evidence
    # trail (Step 8's own "Evidence suggests ..." language, never "this
    # memory IS useful in context X").
    context_evidence = get_memory_context_evidence(entry)
    lines.append("")
    if context_evidence:
        lines.append("Context-specific evidence (evidence, not proof - by query category):")
        for category, counts in context_evidence.items():
            score = get_context_evidence_score(entry, category)
            lines.append(
                f"  {category}: {counts['positive']} positive / {counts['negative']} negative "
                f"outcome(s) -> context score {score:.2f}"
            )
    else:
        lines.append("No context-specific evidence recorded yet for any query category.")

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Step 6 - retrieval outcome tracking: RETRIEVED (surfaced by the
#  relevance-gated retriever - already represented by the existing
#  `retrieval_count`/`record_memory_usage()`) vs ACTUALLY USED (made it
#  into the final, ranked, budget-limited context `assemble_context()`
#  returns). This is a DIFFERENT pipeline stage than `record_memory_usage()`
#  already tracks - that function fires from `MemoryRetriever.retrieve_memories()`'s
#  result, upstream of `memory_context.assemble_context()`'s own separate
#  ranking/budget cut. Operates ONLY on plain id lists (duck-typed, never
#  a `ContextItem` instance) so `memory_context.py`'s existing one-way
#  import of THIS module is never inverted - see the module banner in
#  `memory_context.py`.
# ─────────────────────────────────────────────

def _bump_retrieval_success(entry):
    """Shared, bounded incrementer - the ONE place `retrieval_success_count`
    is ever written, used by both `record_context_selection()` (a memory
    was actually selected into this turn's context) and
    `record_outcome_evidence()` below (a later conversational outcome was
    positive). Both are legitimate "this memory's retrieval/use was a
    good idea" evidence, composed onto the same counter rather than
    duplicated into two - see that field's own schema comment for the
    full reasoning. Bounded the same way `retrieval_count` already is."""
    entry["retrieval_success_count"] = min(_get_retrieval_success_count(entry) + 1, _MAX_RETRIEVAL_COUNT)


def _bump_retrieval_miss(entry):
    """Mirror of `_bump_retrieval_success()` above for
    `retrieval_miss_count`."""
    entry["retrieval_miss_count"] = min(_get_retrieval_miss_count(entry) + 1, _MAX_RETRIEVAL_COUNT)


def get_conflict_group_member_ids(conflict_group):
    """Memory Outcome Telemetry & Closed-Loop Learning sprint - read-only
    helper resolving a `conflict_group` key (the same string
    `_tag_ambiguous_conflict()`/`ContextItem.conflict_group` already
    carry) to the real, underlying manual-memory ids currently in that
    group. Exists specifically so a caller building turn-scoped selection
    evidence can correctly attribute a selected conflict-group's joint
    note (`memory_context._manual_memory_conflict_items()` renders it
    under a SYNTHETIC `memory_id=f"conflict:{group_key}"` - not a real
    `_memories` entry id) back to each REAL member id, exactly once each
    - never recording evidence against the synthetic id itself (which
    does not exist in `_memories` and would silently no-op in
    `record_context_selection()`/`record_outcome_evidence()` anyway, but
    would also mean the group's real members never received their due
    evidence credit - Step 5's own "no double counting, but also no
    UNDER-counting" applies equally to a genuinely selected conflict
    note). Returns `[]` for an unknown/empty `conflict_group`."""
    if not conflict_group:
        return []
    return [
        m["id"] for m in _memories
        if isinstance(m, dict) and m.get("id")
        and m.get("conflict_status") == "ambiguous_conflict"
        and str(m.get("conflict_group")) == str(conflict_group)
    ]


def record_context_selection(candidate_ids, selected_ids, now=None):
    """`candidate_ids`: every manual-memory id the retriever surfaced
    this turn (before `assemble_context()`'s ranking/budget cut).
    `selected_ids`: the subset that actually made it into the final
    assembled context. Both plain iterables of id strings - the CALLER
    (`main_runtime_demo.py`, via `luno.memory_turn_trace.build_turn_trace()`)
    is responsible for extracting these from `relevant_memories_early`/
    `assembled_context.items` respectively (with any conflict-group
    synthetic id already resolved to its real member ids via
    `get_conflict_group_member_ids()` above - this function itself has no
    special-case handling for a `"conflict:..."`-shaped id, it just quietly
    ignores anything that doesn't match a real `_memories` entry), so this
    module never needs to know either caller-side shape.

    A candidate that is also selected earns one `retrieval_success_count`.
    A candidate that is NOT selected (lost to ranking/budget) earns one
    `retrieval_miss_count` - Step 6's own "retrieved tapi tidak benar-benar
    dipakai" distinction. An id that isn't a real manual-memory entry is
    silently ignored (never raises). Returns the list of updated entries."""
    candidate_ids = set(candidate_ids or [])
    selected_ids = set(selected_ids or [])
    if not candidate_ids:
        return []

    updated = []
    for m in _memories:
        if not isinstance(m, dict) or m.get("id") not in candidate_ids:
            continue
        if m["id"] in selected_ids:
            _bump_retrieval_success(m)
        else:
            _bump_retrieval_miss(m)
        updated.append(m)

    if updated:
        _save()
    return [dict(m) for m in updated]


# ─────────────────────────────────────────────
#  Step 7 - context outcome classification. Deterministic, keyword/regex
#  based - reuses the EXISTING feedback/correction detectors above
#  verbatim rather than a second detection pass (no LLM judge, per this
#  step's own explicit instruction). `unknown` is always the fallback -
#  silence is NEVER treated as positive (Step 7's own explicit rule).
# ─────────────────────────────────────────────

_CONTEXT_OUTCOMES = ("positive", "negative", "neutral", "correction", "unknown")

#: A short, closed, exact-match list of bare acknowledgements - Step 7's
#: "neutral" bucket. Deliberately EXACT-match only (not a substring/regex
#: over free text) so this can never misfire on an ordinary sentence that
#: happens to contain one of these words - "neutral" means the user said
#: only this and nothing evaluative, not "the message contains 'ok'
#: somewhere".
_NEUTRAL_ACK_RE = re.compile(
    r'^(?:ok|oke|okay|baik|noted|sip|siap|got\s*it|alright|understood)\.?$',
    re.IGNORECASE,
)


def classify_context_outcome(user_text=None, memory_was_updated=False):
    """Step 7 - returns exactly one of `_CONTEXT_OUTCOMES`. Order matters
    and, as of the Memory Outcome Telemetry & Closed-Loop Learning
    sprint, follows that sprint's own explicit priority list literally:

        1. explicit correction   (`memory_was_updated` / a captured
           replacement value)
        2. explicit negative feedback
        3. explicit positive feedback
        4. "clear contextual confirmation" - deliberately NOT a second,
           broader heuristic layered on top of #3: this codebase has
           exactly one deterministic "the user is confirming something"
           detector (`detect_positive_memory_feedback()`), and adding a
           second, fuzzier one that tries to infer confirmation from
           ordinary conversational continuation is precisely what that
           sprint's own "jangan menginfer positive outcome hanya karena
           user melanjutkan percakapan" / "no LLM judge" rules forbid.
           Priority levels 3 and 4 therefore collapse onto the same
           check here by design, not by oversight (see
           `docs/change_impact/memory_outcome_telemetry.md`).
        5. neutral (a short, closed, exact-match acknowledgement list)
        6. unknown (the default - silence, or anything not matching any
           of the above, is NEVER treated as positive)

    An actual memory content change (`memory_was_updated=True`, i.e.
    `update_memory()` was just called with `reason="correction"`) always
    wins as `"correction"` regardless of the accompanying text - the
    strongest, most concrete signal available. Otherwise falls through
    the same deterministic detectors the feedback-handling commands
    already use - negative is now checked BEFORE positive (previously
    the reverse; the two regex sets are fully anchored and mutually
    exclusive today, so this reordering does not change any existing
    classification, it only makes the priority explicit and future-
    proof against a later regex addition that might overlap).
    `user_text=None`/empty -> `"unknown"` (no signal at all, e.g. a
    scheduled/background turn with no user utterance - silence is never
    positive)."""
    if memory_was_updated:
        return "correction"
    if not user_text or not user_text.strip():
        return "unknown"
    if detect_memory_feedback_correction(user_text):
        return "correction"
    if (detect_negative_memory_feedback(user_text)
            or detect_mark_memory_not_useful_command(user_text)
            or detect_mark_memory_incorrect_command(user_text)):
        return "negative"
    if (detect_positive_memory_feedback(user_text)
            or detect_mark_memory_useful_command(user_text)
            or detect_mark_memory_correct_command(user_text)):
        return "positive"
    if _NEUTRAL_ACK_RE.match(user_text.strip()):
        return "neutral"
    return "unknown"


#: Memory Outcome Telemetry sprint (Step 7) - the closed evidence-mapping
#: table, literally: `positive` -> `retrieval_success_count`, `negative`
#: -> `retrieval_miss_count`. `correction` is deliberately absent here -
#: its evidence (`correction_count`) is already, and ONLY, incremented by
#: `update_memory(reason="correction")` itself (see that function's own
#: comment), never duplicated here. `neutral`/`unknown` map to nothing -
#: "no evidence mutation" is enforced by simply having no entry for them.
_OUTCOME_EVIDENCE_BUMPERS = {
    "positive": _bump_retrieval_success,
    "negative": _bump_retrieval_miss,
}


def record_outcome_evidence(memory_id, outcome, context_category=None):
    """Step 7's evidence-mapping half of the closed loop -
    `classify_context_outcome()` decides WHAT happened, this function
    decides what EVIDENCE that implies for one already-identified target
    memory. Deliberately narrow: only mutates `retrieval_success_count`/
    `retrieval_miss_count` (via the same bounded, shared incrementers
    `record_context_selection()` uses) for `"positive"`/`"negative"`
    respectively - `"correction"`/`"neutral"`/`"unknown"`/any unrecognized
    string are all no-ops, returning `None` without touching anything
    (Step 7's explicit "unknown -> no evidence mutation", "neutral -> no
    strong evidence change", and correction's evidence already being
    `update_memory()`'s own exclusive responsibility).

    Deliberately does NOT call `apply_positive_feedback()`/
    `apply_negative_feedback()` itself - those remain the CALLER's
    responsibility (already wired at every feedback call site in
    `main_runtime_demo.py`/`luno/dashboard/controls.py`), so a caller
    that wants the full Memory Learning sprint feedback behavior
    (`positive_feedback_count`/`usefulness_score`) PLUS this sprint's
    retrieval-evidence half calls both functions, explicitly, side by
    side - no hidden double mutation from a single call.

    `memory_id` must already be a resolved, unambiguous target (this
    function does no guessing - same contract as `apply_positive_feedback()`/
    `apply_negative_feedback()`). Returns the updated entry, or `None` if
    `outcome` isn't `"positive"`/`"negative"` or `memory_id` doesn't
    exist.

    `context_category` (Memory Decision Quality & Adaptive Retrieval
    sprint, additive, optional, defaults to `None` - every EXISTING
    caller that doesn't pass it behaves byte-for-byte as before this
    sprint): when given and one of `MANUAL_MEMORY_CATEGORIES`, this
    function ALSO records the same positive/negative evidence into that
    one category's bucket in `context_evidence` (via
    `_bump_context_evidence()`) - the caller is responsible for
    resolving which category the triggering query belonged to (see
    `classify_query_context_category()`), this function does no
    inference of its own, it only routes an already-classified category
    to the right bounded bucket."""
    bumper = _OUTCOME_EVIDENCE_BUMPERS.get(outcome)
    if bumper is None:
        return None
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            bumper(m)
            if context_category:
                _bump_context_evidence(m, context_category, outcome)
            _save()
            return dict(m)
    return None


# ─────────────────────────────────────────────
#  Memory Decision Quality & Adaptive Retrieval sprint - CONTEXT-SENSITIVE
#  memory value. NOT a second memory store, retrieval engine, tokenizer,
#  or importance system (see this sprint's own hard constraints) - built
#  entirely on top of `classify_query_context_category()` (a reuse of the
#  existing, deterministic `_classify_memory_category()` classifier
#  applied to the CURRENT query instead of a memory's stored text) and
#  `record_outcome_evidence()`'s own existing outcome-classification call
#  sites above.
#
#  Why a new field is genuinely necessary (Step 3's own "justify before
#  adding" instruction): NONE of the existing signals
#  (`retrieval_success_count`/`retrieval_miss_count`/`positive_feedback_count`/
#  `negative_feedback_count`/`usefulness_score`/`evaluation_score`) carry
#  any notion of WHICH KIND of query the evidence came from - they are
#  all single, global scalars. A memory that is reliably useful when
#  asked about ESP32 configuration but never confirmed useful (or
#  actively disputed) when the conversation is about guitar practice is
#  structurally invisible to every existing signal: they would show
#  identical global numbers in both cases. `context_evidence` is the
#  smallest possible additive extension that makes that distinction
#  representable - a bounded, per-(existing-)category evidence table,
#  never a new free-form "quality" score (deliberately NOT persisted as
#  a single number - only the raw, bounded positive/negative counters are
#  stored; `get_context_evidence_score()` below derives a number fresh on
#  every call, the SAME "persist evidence, derive score on demand"
#  discipline `evaluate_memory()` already established, so this can never
#  become a second, competing `evaluation_score`).
#
#  Bounded by construction: `context_evidence` can only ever have keys
#  from the closed `MANUAL_MEMORY_CATEGORIES` tuple (6 values) - never a
#  free-text/per-query key - so it can never grow past 6 top-level
#  entries x 2 counters each, no matter how many turns/queries a memory
#  lives through (Step 7/hard constraint #16's "bounded telemetry").
# ─────────────────────────────────────────────

#: Symmetric, bounded per-event deltas - same magnitude and reasoning as
#: `_USEFULNESS_POSITIVE_FEEDBACK_DELTA`/`_USEFULNESS_NEGATIVE_FEEDBACK_DELTA`
#: above: small enough that a SINGLE event can nudge but never alone
#: swing a context's score from one extreme to the other (Step 7's "no
#: single interaction can dominate").
_CONTEXT_EVIDENCE_POSITIVE_DELTA = 0.12
_CONTEXT_EVIDENCE_NEGATIVE_DELTA = 0.12


def _get_context_evidence_counts(entry, category):
    """Backward-compatible, bounded accessor - a pre-sprint entry (or one
    with no evidence yet for `category`) simply has no such key, defaults
    to `{"positive": 0, "negative": 0}` rather than crashing or guessing.
    `category` must be one of `MANUAL_MEMORY_CATEGORIES` - anything else
    (a stale/hand-edited/unknown value) is treated as "no evidence"
    rather than silently creating a new, unbounded key."""
    if not isinstance(entry, dict) or category not in MANUAL_MEMORY_CATEGORIES:
        return {"positive": 0, "negative": 0}
    bucket = entry.get("context_evidence")
    if not isinstance(bucket, dict):
        return {"positive": 0, "negative": 0}
    counts = bucket.get(category)
    if not isinstance(counts, dict):
        return {"positive": 0, "negative": 0}
    pos = counts.get("positive", 0)
    neg = counts.get("negative", 0)
    pos = pos if isinstance(pos, int) and not isinstance(pos, bool) and pos >= 0 else 0
    neg = neg if isinstance(neg, int) and not isinstance(neg, bool) and neg >= 0 else 0
    return {"positive": pos, "negative": neg}


def get_memory_context_evidence(entry):
    """Public, read-only, bounded snapshot of every category this memory
    has RECORDED evidence for (never all 6 - only ones with at least one
    positive or negative event, keeping the dashboard/explanation output
    short). Never mutates, never raises."""
    if not isinstance(entry, dict):
        return {}
    out = {}
    for category in MANUAL_MEMORY_CATEGORIES:
        counts = _get_context_evidence_counts(entry, category)
        if counts["positive"] or counts["negative"]:
            out[category] = counts
    return out


def _bump_context_evidence(entry, category, outcome):
    """Bounded, per-category evidence incrementer - the context-scoped
    sibling of `_bump_retrieval_success()`/`_bump_retrieval_miss()`
    above. `category` must already be one of `MANUAL_MEMORY_CATEGORIES`
    and `outcome` must be `"positive"`/`"negative"` - anything else is a
    silent no-op (mirrors `_OUTCOME_EVIDENCE_BUMPERS`'s own closed
    table; correction/neutral/unknown never reach here at all - see
    `record_outcome_evidence()`'s own call site, which only forwards
    when its own bumper lookup already succeeded). Each counter is
    capped at `_MAX_RETRIEVAL_COUNT`, same bound every other evidence
    counter in this file already uses."""
    if category not in MANUAL_MEMORY_CATEGORIES or outcome not in ("positive", "negative"):
        return
    bucket = entry.get("context_evidence")
    if not isinstance(bucket, dict):
        bucket = {}
        entry["context_evidence"] = bucket
    counts = bucket.get(category)
    if not isinstance(counts, dict):
        counts = {"positive": 0, "negative": 0}
        bucket[category] = counts
    key = "positive" if outcome == "positive" else "negative"
    current = counts.get(key, 0)
    current = current if isinstance(current, int) and not isinstance(current, bool) and current >= 0 else 0
    counts[key] = min(current + 1, _MAX_RETRIEVAL_COUNT)


def get_context_evidence_score(entry, category):
    """Deterministic, pure, ALWAYS recomputed fresh from the bounded
    `context_evidence` counters - never itself persisted (Step 3's own
    "do not create a generic 'memory quality' field that duplicates
    evaluation_score" is satisfied by construction: there is no stored
    score here, only stored counters, exactly like `evaluate_memory()`'s
    own score is never trusted from storage alone). Returns a neutral
    0.5 when `category` is falsy/unknown/not yet evidenced for this
    entry - "no evidence in this context" is deliberately NOT the same
    as "known to be bad in this context" (same neutral-default
    philosophy `_get_usefulness()` already established). Symmetric,
    bounded per-event deltas mean a single event can nudge but never
    alone swing the score from one extreme to the other - same
    reasoning as `apply_positive_feedback()`/`apply_negative_feedback()`.
    Given the same stored state, always returns the same number (Step 7's
    "given the same stored state and same query/context, ranking must be
    reproducible")."""
    if not isinstance(entry, dict) or category not in MANUAL_MEMORY_CATEGORIES:
        return _DEFAULT_USEFULNESS_SCORE
    counts = _get_context_evidence_counts(entry, category)
    score = _DEFAULT_USEFULNESS_SCORE
    score += _CONTEXT_EVIDENCE_POSITIVE_DELTA * counts["positive"]
    score -= _CONTEXT_EVIDENCE_NEGATIVE_DELTA * counts["negative"]
    return round(max(MEMORY_USEFULNESS_MIN, min(MEMORY_USEFULNESS_MAX, score)), 4)


def get_memory_context_specialization_summary(memory_id):
    """Memory Decision Quality & Adaptive Retrieval sprint - bounded,
    read-only summary of one memory's per-category evidence and derived
    (never persisted) context score, for the Memory Dashboard's Context
    Specialization panel. Returns `None` for an unknown id (same "honest
    about not finding it" convention as `get_memory()`/
    `get_memory_outcome_summary()`)."""
    entry = get_memory(memory_id)
    if entry is None:
        return None
    evidence = get_memory_context_evidence(entry)
    return {
        "memory_id": memory_id,
        "categories": {
            category: {
                "positive": counts["positive"],
                "negative": counts["negative"],
                "context_score": get_context_evidence_score(entry, category),
            }
            for category, counts in evidence.items()
        },
    }


def list_context_specialized_memories(category=None, order="top", limit=20):
    """Memory Decision Quality & Adaptive Retrieval sprint - bounded,
    paginated scan (Step 9's own "no full-memory dump on ordinary
    refresh") across manual-memory entries that have RECORDED context
    evidence, for the dashboard's "consistently useful in this context" /
    "consistently poor in this context" panels. `category=None` scans
    evidence across every category; `category=<one of
    MANUAL_MEMORY_CATEGORIES>` filters to just that one.
    `order="top"` sorts by context score descending (most consistently
    useful first); `order="bottom"` sorts ascending (most consistently
    poor first). `limit` bounds the returned result size. Read-only,
    never mutates, never exposes anything beyond `memory_id`/`text`/
    `category`/the bounded counters/the derived score."""
    rows = []
    for m in _memories:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        evidence = get_memory_context_evidence(m)
        if not evidence:
            continue
        categories = [category] if (category is not None and category in evidence) else (
            list(evidence.keys()) if category is None else []
        )
        for cat in categories:
            counts = evidence[cat]
            rows.append({
                "memory_id": m["id"],
                "text": m.get("text", ""),
                "category": cat,
                "positive": counts["positive"],
                "negative": counts["negative"],
                "context_score": get_context_evidence_score(m, cat),
            })
    reverse = order != "bottom"
    rows.sort(key=lambda r: r["context_score"], reverse=reverse)
    return rows[: max(0, limit)]


# ─────────────────────────────────────────────
#  Step 8 - self-calibration. The ONLY function in this whole section
#  that writes to `_memories`/`_save()`. Writes EXACTLY two fields -
#  `evaluation_score`, `last_evaluated_at` - and nothing else, ever (Step
#  8's explicit "jangan pernah mengubah text, history, importance,
#  conflict_group, source, lifecycle"). A memory that repeatedly earns a
#  high score through this path becomes a more-trusted RETRIEVAL
#  CANDIDATE over time (via the maintenance integration below and the
#  dashboard's own sorting) - it never becomes, or is treated as, a
#  Verified Fact (Step 8's own explicit distinction; `VerifiedFactStore`
#  is never referenced anywhere in this module).
# ─────────────────────────────────────────────

def calibrate_memory(memory_id, now=None):
    """Runs `evaluate_memory()` fresh against the CURRENT live entry and
    persists only its `score` (as `evaluation_score`) plus a
    `last_evaluated_at` timestamp. Never called automatically/on a
    schedule (Step 19 across both sprints: no background scheduler) -
    only from an explicit feedback event (see `main_runtime_demo.py`'s
    feedback handler) or an explicit dashboard/test call. Returns the
    updated entry, or `None` if `memory_id` doesn't exist."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            result = evaluate_memory(m, now=now)
            m["evaluation_score"] = result["score"]
            m["last_evaluated_at"] = _now_iso()
            _save()
            print(f"[Memory] ✓ Calibrated {memory_id}: evaluation={result['score']} confidence={result['confidence']}")
            return dict(m)
    return None


def record_feedback_event(memory_id):
    """Small, additive helper - increments `feedback_event_count` (Step
    3's schema field) for a memory that just received ANY feedback event
    (positive, negative, or correction). Deliberately separate from
    `apply_positive_feedback()`/`apply_negative_feedback()`/
    `update_memory()` themselves (none of which this sprint modifies
    beyond the single `correction_count` line already added to
    `update_memory()`) - callers invoke this ALONGSIDE the existing
    mutator, not instead of it, so a caller that forgets to call this
    still gets fully correct feedback/correction behavior, just without
    the (purely observational) event count incrementing. Returns the
    updated entry, or `None` if `memory_id` doesn't exist."""
    for m in _memories:
        if isinstance(m, dict) and m.get("id") == memory_id:
            m["feedback_event_count"] = _get_feedback_event_count(m) + 1
            _save()
            return dict(m)
    return None