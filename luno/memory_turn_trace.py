"""
memory_turn_trace.py
=====================

Memory Outcome Telemetry & Closed-Loop Learning sprint.

This is NOT another memory store, retrieval engine, or tokenizer. It adds
exactly one thing: a TRANSIENT, per-turn record of which memory/context
candidates were selected into THIS turn's assembled context - built
entirely from data `luno.memory_retrieval` and `luno.memory_context`
already compute, never a second relevance/ranking pass.

`MemoryTurnTrace` exists only for the lifecycle of one conversational
turn. It is NEVER persisted to disk by this module (Step 3's own "jangan
persist seluruh trace secara default") - the caller
(`main_runtime_demo.py`) is free to hold the MOST RECENT trace per
conversation in a small, bounded, in-memory dict (mirroring the existing
`_session_feedback_target` pattern) so the NEXT turn's outcome
classification has something to attribute evidence against, but that is
the caller's own session-scoped bookkeeping, not a feature of this
module or of `luno.memory`'s persistent schema.

Dependency direction: this module imports `luno.memory` (for
`get_conflict_group_member_ids()`) and `luno.memory_context`/
`luno.memory_retrieval` (for the `ContextItem`/`RelevantMemory`/
`AssembledContext` shapes it reads) - the same one-way direction
`memory_context.py` itself already established (conversation code ->
this module -> existing providers). Neither `luno.memory` nor
`luno.memory_context` import this module back.

Candidate / relevant / selected / rendered (Step 4)
-----------------------------------------------------
The sprint brief asks for four distinct states per memory. In THIS
codebase's actual, audited architecture (not guessed), two pairs are
structurally identical today, and this module says so honestly rather
than inventing daylight between them that doesn't exist:

  - "candidate" and "relevant" are the same set: `MemoryRetriever`'s own
    per-source `token_overlap()` relevance gate has ALREADY run, inside
    `retrieve_memories()`, before any `RelevantMemory` is ever returned -
    there is no way for an irrelevant item to reach this module as a
    "candidate" in the first place (see `luno.memory.make_manual_memory_source()`'s
    own `token_overlap` gate).
  - "selected" and "rendered" are the same set: `AssembledContext.render()`
    (`memory_context.render_context_block()`) renders exactly
    `AssembledContext.items` - nothing is selected into `.items` and then
    separately dropped before rendering.

The four names are kept as SEPARATE fields anyway (not collapsed to two)
so a future architecture change (e.g. a relevance sub-threshold inside
`MemoryRetriever`, or a render-time truncation independent of selection)
never needs a schema migration here - this is additive-by-construction,
matching the sprint's own hard constraint #20.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from . import memory as memory_module

#: Hard cap on how many turn traces this module will ever help a caller
#: keep - see `build_turn_trace()`'s own docstring. Not enforced here
#: directly (this module builds ONE trace at a time and holds no state
#: of its own), but documented as the contract callers must honor -
#: `main_runtime_demo.py`'s own `_memory_turn_trace_max` mirrors this.
RECOMMENDED_MAX_TRACES_PER_PROCESS = 50


@dataclass
class MemoryTurnTrace:
    """One turn's transient memory-selection record. Never written to
    disk by this module. Holds only ids/scores/short reason strings -
    never the user's message text or the assistant's response (Step 3's
    "jangan persist seluruh trace" / hard constraint #17's "jangan
    menyimpan full conversation transcript")."""

    turn_id: str
    context_timestamp: str = ""

    #: Manual-memory ids (already conflict-group-resolved - see
    #: `build_turn_trace()`) that reached this turn's candidate pool.
    #: Identical to `relevant_memory_ids` in this architecture today (see
    #: module docstring) - kept as a separate field for forward
    #: compatibility, not because they currently differ.
    candidate_memory_ids: Set[str] = field(default_factory=set)
    relevant_memory_ids: Set[str] = field(default_factory=set)

    #: The subset that survived `assemble_context()`'s own ranking/budget
    #: cut and made it into the final context. Identical to
    #: `rendered_memory_ids` today (see module docstring).
    selected_memory_ids: Set[str] = field(default_factory=set)
    rendered_memory_ids: Set[str] = field(default_factory=set)

    #: Read-only awareness of the OTHER sources `assemble_context()` also
    #: selects from this turn - Verified Facts / Episodic Memory ids are
    #: tracked here for explainability ONLY. Nothing in this sprint ever
    #: writes evidence onto a Verified Fact or an episodic experience -
    #: see `luno.memory_guard`/`luno.episodic_memory`, both untouched.
    selected_verified_fact_ids: Set[str] = field(default_factory=set)
    selected_experience_ids: Set[str] = field(default_factory=set)

    #: memory_id -> short, human-readable reason string (never raw prompt
    #: text, never the user's utterance).
    selection_reasons: Dict[str, str] = field(default_factory=dict)

    #: memory_id -> the relevance score `MemoryRetriever`/`assemble_context()`
    #: already computed for it (never recomputed here).
    retrieval_scores: Dict[str, float] = field(default_factory=dict)

    #: Memory Decision Quality & Adaptive Retrieval sprint - the query
    #: category (`luno.memory.classify_query_context_category()`) this
    #: turn's utterance was classified as, when a query text was given to
    #: `build_turn_trace()` - `""` when not available (e.g. an existing
    #: caller that doesn't pass `query_text`, kept fully backward
    #: compatible). Purely a short, bounded label, never the raw
    #: utterance itself.
    query_category: str = ""

    # ─────────────────────────────────────────────
    # Memory & Voice Observability Dashboard sprint - additive fields
    # only, every one of them defaulted so an existing caller that omits
    # the new `build_turn_trace()` keyword arguments gets a trace
    # byte-for-byte equivalent (on these fields) to before this sprint:
    # empty dict/list/string/`None`. Every value below is copied from
    # data the CALLER already computed this turn (see `_handle_utterance()`
    # in `main_runtime_demo.py`) - this module still never classifies,
    # retrieves, ranks, or tokenizes anything itself.
    # ─────────────────────────────────────────────

    #: Whether `MemoryRetriever.retrieve_memories()` was actually invoked
    #: this turn (vs. skipped/raised) - answers Phase 1's "retrieval
    #: called?" question. `None` when the caller didn't report it.
    retrieval_called: Optional[bool] = None

    #: `luno.memory.classify_query_intent(text)` for this turn, if the
    #: caller computed one (it already does, unconditionally, in
    #: production - see `_handle_utterance()`). `""` when unavailable.
    query_intent: str = ""

    #: `luno.memory.classify_reference_type(text)` (Sprint 4) - answers
    #: "was this turn considered continuation/reference/alternative/etc."
    #: `""` when unavailable.
    reference_type: str = ""

    #: `luno.memory.needs_topic_context(text)`-derived flag (Sprint 4) -
    #: whether this turn was treated as a short/elliptical follow-up.
    #: `None` when unavailable.
    is_short_followup: Optional[bool] = None

    #: Sprint 4's single-slot `_active_topic` snapshot for this
    #: conversation AT THE START of this turn (before this turn's own
    #: update) - sorted terms only, never a raw sentence. Empty list when
    #: there was no active topic.
    active_topic_terms: List[str] = field(default_factory=list)

    #: This conversation's bounded topic HISTORY (Memory Topic Retention
    #: sprint) at the START of this turn - one dict per entry:
    #: `{"terms": [...], "age": int, "referenced": bool, "produced_candidate": bool}`.
    #: `"referenced"` / `"produced_candidate"` both come from whether THIS
    #: entry was among `select_topic_candidates()`'s own already-computed
    #: return value for this turn - never re-derived by matching terms
    #: again here.
    topic_history: List[Dict[str, Any]] = field(default_factory=list)

    #: Per-turn retrieval-funnel stage counts (Phase 2) - copied straight
    #: from `memory_context.assemble_context(funnel=...)`'s own
    #: write-only output dict, plus `"query"`/`"topic_candidates"` which
    #: the caller fills in itself (both are caller-side counts, not
    #: something `assemble_context()` itself would ever know). Keys only
    #: ever present when actually computed this turn - a missing key
    #: means "not measured", never a fabricated zero.
    funnel: Dict[str, int] = field(default_factory=dict)

    # ─────────────────────────────────────────────
    # Sprint 50 (Runtime Observability, Test Logging & Real-World Data
    # Capture) - two more additive, defaulted fields, same "existing
    # caller that omits these kwargs gets a trace unaffected on these
    # fields" contract as the Sprint-32 block above. Both are read
    # straight from `PlannerBridgeModule._handle_utterance()`'s own
    # ALREADY-COMPUTED topic-decision branch (which of the four existing
    # branches - ordinal / topic-history / active-topic / temporal
    # fallback - fired this turn, and what `is_active_topic_relevant_
    # to_query()` returned when it was actually evaluated) - this module
    # still never classifies or re-derives anything itself.
    # ─────────────────────────────────────────────

    #: Which of the four existing topic-resolution branches produced
    #: (or failed to produce) a candidate this turn:
    #: "ORDINAL_RESOLVED" / "MERGE_TOPIC_HISTORY" / "MERGE_ACTIVE_TOPIC" /
    #: "MERGE_TEMPORAL_FALLBACK" / "NO_CANDIDATE". `""` when the caller
    #: didn't report it (pre-Sprint-50 callers, or a turn where this
    #: branch chain never ran at all).
    topic_decision: str = ""

    #: `memory_context.is_active_topic_relevant_to_query()`'s own return
    #: value for THIS turn, captured ONLY when that function was actually
    #: called (Python's own short-circuit `or` means it is skipped for
    #: most `NEEDS_TOPIC_CONTEXT_TYPES` turns - see that call site's own
    #: comment). `None` means "not evaluated this turn" - honestly
    #: distinct from `False` ("evaluated, and said this candidate should
    #: be refused" - Sprint 48/49's own ambiguity gates included).
    ambiguity_check_result: Optional[bool] = None

    @property
    def is_ambiguity_refusal(self) -> bool:
        """True only when the relevance guard was actually evaluated
        AND said no (`ambiguity_check_result is False`) - a plain,
        derived read of the field above, never a second decision. Absence
        of evidence (`None`, guard never ran) is deliberately NOT treated
        as a refusal - matches this architecture's own long-standing "no
        sufficient evidence -> refuse" being a POSITIVE claim, not a
        default."""
        return self.ambiguity_check_result is False

    def not_selected_memory_ids(self) -> Set[str]:
        """Candidates that were relevant but lost to ranking/budget this
        turn - Step 4's memory-B example (`candidate=true, relevant=true,
        selected=false, rendered=false`)."""
        return self.candidate_memory_ids - self.selected_memory_ids

    def all_manual_memory_ids(self) -> Set[str]:
        """Every manual-memory id this trace has ANY opinion about
        (candidate or selected) - used by `record_context_selection()`'s
        caller as the id universe for one bounded write."""
        return self.candidate_memory_ids | self.selected_memory_ids


def _manual_memory_id(rm: Any) -> Optional[str]:
    if getattr(rm, "source", None) != "manual_memory":
        return None
    raw = getattr(rm, "raw", None)
    if isinstance(raw, dict) and raw.get("id"):
        return str(raw["id"])
    return None


def _candidate_reason(rm: Any, query_category: str) -> str:
    """Memory Decision Quality & Adaptive Retrieval sprint - the initial,
    candidate-stage explanation for one `RelevantMemory` (before it's
    known whether it will be selected this turn). Built entirely from
    already-public `luno.memory` accessors applied to the memory's own
    raw stored dict - never recomputes anything, never mutates. Falls
    back to the pre-sprint generic reason for anything that isn't a
    Manual Memory entry (or that predates this sprint's own schema)."""
    raw = getattr(rm, "raw", None)
    base = "matched current query (relevance-gated by MemoryRetriever)"
    if not isinstance(raw, dict) or "importance" not in raw:
        return base
    parts = [
        base,
        f"importance={memory_module._get_importance(raw)}/4",
        f"usefulness={memory_module._get_usefulness(raw):.2f}",
        f"evaluation={memory_module.evaluate_memory(raw)['score']:.2f}",
    ]
    if query_category:
        counts = memory_module._get_context_evidence_counts(raw, query_category)
        if counts["positive"] or counts["negative"]:
            ctx_score = memory_module.get_context_evidence_score(raw, query_category)
            direction = "raised" if ctx_score > 0.5 else ("lowered" if ctx_score < 0.5 else "left neutral")
            parts.append(
                f"context evidence for '{query_category}' queries {direction} its ranking here "
                f"({counts['positive']} positive / {counts['negative']} negative, context score {ctx_score:.2f})"
            )
        else:
            parts.append(f"no context-specific evidence yet for '{query_category}' queries")
    return "; ".join(parts)


def build_turn_trace(turn_id: str, relevant_memories: Optional[List[Any]],
                      assembled_context: Any, now: Optional[datetime] = None,
                      query_text: Optional[str] = None, *,
                      retrieval_called: Optional[bool] = None,
                      query_intent: str = "",
                      reference_type: str = "",
                      is_short_followup: Optional[bool] = None,
                      active_topic_snapshot: Optional[Any] = None,
                      topic_history: Optional[List[Any]] = None,
                      topic_history_candidates: Optional[List[Any]] = None,
                      funnel: Optional[Dict[str, int]] = None,
                      topic_decision: str = "",
                      ambiguity_check_result: Optional[bool] = None) -> MemoryTurnTrace:
    """Builds one `MemoryTurnTrace` from data this turn's real production
    call already computed - `relevant_memories` (the SAME
    `relevant_memories_early` list `record_memory_usage()` already reads,
    passed straight through, no second retrieval) and `assembled_context`
    (the real `AssembledContext` `memory_context.assemble_context()`
    already returned this turn). Never queries anything itself, never
    mutates anything - a pure, read-only transformation.

    `query_text` (Memory Decision Quality & Adaptive Retrieval sprint,
    additive, optional, defaults to `None` - every existing caller that
    doesn't pass it gets `query_category=""` and the pre-sprint generic
    selection reasons, i.e. behaves exactly as before this sprint) lets
    this function classify the turn's own query category
    (`luno.memory.classify_query_context_category()`, the SAME
    deterministic classifier `memory_context.assemble_context()` already
    used internally to rank this turn's items) purely for EXPLANATION
    purposes - it does not change WHICH memories are candidate/selected,
    only what `selection_reasons` says about them (Step 8's
    explainability requirement: relevance, importance, lifecycle, global
    usefulness, evaluation, context-specific evidence, and a plain-
    language "helped/hurt ranking" direction, never a truth claim).

    Conflict-group handling (Step 5's "no double counting, no under-
    counting"): a selected conflict-group joint note
    (`ContextItem.memory_id == f"conflict:{group}"`,
    `ContextItem.conflict_group` set) is resolved here to its REAL member
    ids via `luno.memory.get_conflict_group_member_ids()` - each real
    member id is added to `selected_memory_ids` exactly once, never the
    synthetic `"conflict:..."` string itself (which is not a real
    `_memories` entry and would silently no-op downstream anyway, but
    would also mean the real members never got their due evidence
    credit).

    Historical-wording items (`ContextItem.historical=True`) share the
    SAME underlying `memory_id` as their current-text counterpart (see
    `memory_context._memory_id_for_relevant_memory()`) - since every id
    set here is a plain `set`, a memory appearing twice (once current,
    once historical) is naturally counted once, satisfying Step 5's own
    "historical entry counted correctly, never double-counted" without
    any special-case code.

    All keyword-only parameters below (Memory & Voice Observability
    Dashboard sprint, additive) are OPTIONAL and PASSTHROUGH-ONLY - every
    one is data the caller already computed this turn (see
    `_handle_utterance()` in `main_runtime_demo.py`); this function never
    classifies, retrieves, ranks, or recomputes anything from them, only
    copies them onto the returned trace's matching field (see
    `MemoryTurnTrace`'s own field docs for exactly what each one means).
    Omitting all of them (every caller before this sprint) produces a
    trace identical to before this sprint on every pre-existing field."""
    now = now or datetime.now()
    query_category = memory_module.classify_query_context_category(query_text) if query_text else ""
    trace = MemoryTurnTrace(
        turn_id=turn_id, context_timestamp=now.isoformat(timespec="seconds"),
        query_category=query_category,
        retrieval_called=retrieval_called,
        query_intent=query_intent or "",
        reference_type=reference_type or "",
        is_short_followup=is_short_followup,
        funnel=dict(funnel) if funnel else {},
        topic_decision=topic_decision or "",
        ambiguity_check_result=ambiguity_check_result,
    )
    if active_topic_snapshot is not None and getattr(active_topic_snapshot, "terms", None):
        trace.active_topic_terms = sorted(active_topic_snapshot.terms)
    if topic_history:
        # `topic_history_candidates` (already computed by the caller via
        # `select_topic_candidates()`) tells us which entries were
        # actually referenced/offered as a candidate THIS turn - compared
        # by identity (`is`), never by re-matching terms, since these are
        # the SAME `ActiveTopicSnapshot` objects the caller already holds.
        candidate_ids = {id(e) for e in (topic_history_candidates or [])}
        for entry in topic_history:
            terms = getattr(entry, "terms", None)
            if not terms:
                continue
            was_candidate = id(entry) in candidate_ids
            trace.topic_history.append({
                "terms": sorted(terms),
                "age": getattr(entry, "turns_since_active", None),
                "referenced": was_candidate,
                "produced_candidate": was_candidate,
            })

    for rm in (relevant_memories or []):
        mid = _manual_memory_id(rm)
        if mid is None:
            continue
        trace.candidate_memory_ids.add(mid)
        trace.relevant_memory_ids.add(mid)
        score = getattr(rm, "score", None)
        if isinstance(score, (int, float)):
            trace.retrieval_scores[mid] = float(score)
        trace.selection_reasons.setdefault(mid, _candidate_reason(rm, query_category))

    items = getattr(assembled_context, "items", None) or []
    for item in items:
        source = getattr(item, "source", None)
        memory_id = getattr(item, "memory_id", None)
        if not memory_id:
            continue

        if source == "manual_memory":
            conflict_group = getattr(item, "conflict_group", None)
            if conflict_group or str(memory_id).startswith("conflict:"):
                member_ids = memory_module.get_conflict_group_member_ids(conflict_group)
                for member_id in member_ids:
                    trace.selected_memory_ids.add(member_id)
                    trace.rendered_memory_ids.add(member_id)
                    trace.selection_reasons[member_id] = (
                        f"part of an unresolved conflict group presented jointly (group={conflict_group})"
                    )
                    relevance = getattr(item, "relevance", None)
                    if isinstance(relevance, (int, float)):
                        trace.retrieval_scores.setdefault(member_id, float(relevance))
                continue

            trace.selected_memory_ids.add(str(memory_id))
            trace.rendered_memory_ids.add(str(memory_id))
            relevance = getattr(item, "relevance", None)
            importance = getattr(item, "importance", None)
            lifecycle = getattr(item, "lifecycle", None)
            usefulness = getattr(item, "usefulness", None)
            evaluation = getattr(item, "evaluation", None)
            context_evidence = getattr(item, "context_evidence", None)
            reason_parts = ["SELECTED into context this turn"]
            if isinstance(relevance, (int, float)):
                reason_parts.append(f"relevance={relevance:.2f}")
            if importance is not None:
                reason_parts.append(f"importance={importance}/4")
            if lifecycle is not None:
                reason_parts.append(f"lifecycle={lifecycle}")
            if context_evidence is not None:
                # Memory Decision Quality & Adaptive Retrieval sprint
                # (Step 8) - "whether adaptation helped or hurt ranking",
                # stated plainly rather than as a bare number: this IS
                # the same context_evidence value `_rank_key()` just used
                # to rank this item, so the explanation matches the
                # actual ranking contribution exactly (never a separate,
                # re-derived estimate).
                if context_evidence > 0.5:
                    reason_parts.append(f"context-specific evidence HELPED its ranking here (score={context_evidence:.2f})")
                elif context_evidence < 0.5:
                    reason_parts.append(f"context-specific evidence HURT its ranking here (score={context_evidence:.2f})")
                else:
                    reason_parts.append(f"no context-specific evidence yet (neutral, score={context_evidence:.2f})")
            if usefulness is not None:
                reason_parts.append(f"usefulness={usefulness:.2f}")
            if evaluation is not None:
                reason_parts.append(f"evaluation={evaluation:.2f}")
            trace.selection_reasons[str(memory_id)] = " - ".join(reason_parts)
            if isinstance(relevance, (int, float)):
                trace.retrieval_scores[str(memory_id)] = float(relevance)

        elif source == "verified_facts":
            trace.selected_verified_fact_ids.add(str(memory_id))
        elif source == "episodic_memory":
            trace.selected_experience_ids.add(str(memory_id))

    # Step 8 - make "why NOT selected" unambiguous for every candidate
    # that was relevant but lost to ranking/budget this turn, rather than
    # leaving only the generic candidate-stage reason in place (which,
    # read alone, could be mistaken for "this WAS shown").
    for mid in trace.not_selected_memory_ids():
        existing = trace.selection_reasons.get(mid, "")
        trace.selection_reasons[mid] = (
            existing + " -- NOT selected into this turn's context (outranked or lost to budget)."
        ).strip()

    return trace
