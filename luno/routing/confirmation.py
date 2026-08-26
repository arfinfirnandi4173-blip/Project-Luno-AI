"""
confirmation.py
=================

Efficient LLM Classifier sprint - `ConfirmationHandler`: a generic,
tool-agnostic pending-confirmation store for the "classifier is only
MEDIUM confidence" case (`RoutingDecision.needs_confirmation=True` - see
`decision_engine.py`'s own docstring for exactly when that fires:
`classifier_confirmation_threshold <= confidence < classifier_confidence_
threshold`). Deliberately NOT specific to browser/Home Assistant/any one
tool, and deliberately NOT `luno.browser.permissions.PermissionManager`
reused/extended - that class is browser-action-shaped (action name +
params + risk level); this one only ever holds "here's the ORIGINAL text
the user said, and what category we think it might be" and hands the
text back unchanged on confirmation, so it's naturally reusable for
Home Assistant, browser, or any future tool without needing to know
anything tool-specific itself (see `main_runtime_demo.py`'s wiring for
how a confirmed entry actually gets EXECUTED - this class never executes
anything, it only tracks state and answers yes/no).

Confirmation TEXT is always template/deterministic (`prompt_for()`/
`cancelled_ack()` below) - NEVER an extra LLM call just to phrase a
question, per the spec's own explicit requirement.

Yes/no matching reuses `luno.environment_intent.classify_confirmation_
reply()` - the SAME function `_handle_environmental_intent()`'s own
confirm-first flow and the browser-permissions confirmation reply
handler already use, rather than forking a third word list/matcher
(see that module's own docstring - "batal"/"cancel"/"lakukan" were
added to its word lists specifically to serve this new caller).

Thread safety / Event Bus safety: every method here is a plain in-memory
dict operation under one lock - microseconds, never any I/O, so there is
nothing to time-bound and nothing that could ever block the Event Bus
pump thread (see spec section 13) - this class doesn't even touch the
Event Bus.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..environment_intent import classify_confirmation_reply

_DEFAULT_KEY = "_default_"

#: Human-readable Indonesian labels for the template confirmation prompt -
#: falls back to a de-underscored raw value for any `Intent` not listed
#: here (never raises, never blocks on an unmapped label).
_INTENT_LABELS: Dict[str, str] = {
    "smart_home": "kontrol rumah pintar",
    "vision": "cek kamera",
    "memory": "ingatan sebelumnya",
    "world_state": "status rumah",
    "general_chat": "obrolan biasa",
    "general_question": "pertanyaan umum",
    "search_web": "pencarian di internet",
    "reasoning": "analisa/penjelasan",
    "planning": "penyusunan rencana",
    "coding": "kode/pemrograman",
    "multi_step": "beberapa langkah",
    "scheduling": "penjadwalan",
    "device_control": "kontrol perangkat",
    "status_query": "cek status",
    "automation": "otomasi",
}


@dataclass
class PendingConfirmation:
    request_id: str
    conversation_id: Optional[str]
    original_text: str
    intent: str
    confidence: float
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ConfirmationOutcome:
    #: "confirmed" or "cancelled" - never anything else.
    action: str
    pending: PendingConfirmation


class ConfirmationHandler:
    """One live pending confirmation per conversation at a time - a
    second ambiguous turn arriving before the first is answered
    SUPERSEDES it (same "one entry per live conversation" convention
    `PlannerBridgeModule._pending_env_confirmations` already uses),
    rather than stacking multiple pending questions a user would have no
    clean way to disambiguate between."""

    #: Defensive bound so a long-running process can't grow this dict
    #: unboundedly even if some conversations' confirmations are never
    #: answered and never revisited - same convention as
    #: `PlannerBridgeModule._pending_turns_max`/`_last_device_target_max`.
    _MAX_ENTRIES = 200

    def __init__(self, ttl_s: float = 60.0) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._pending: Dict[str, PendingConfirmation] = {}

    def request_confirmation(
        self, *, request_id: str, conversation_id: Optional[str], text: str, intent: str, confidence: float,
    ) -> PendingConfirmation:
        """Stores (or replaces) the pending confirmation for this
        conversation and returns it - the caller builds its prompt from
        the returned entry via `prompt_for()`."""
        key = conversation_id or _DEFAULT_KEY
        now = time.time()
        entry = PendingConfirmation(
            request_id=request_id, conversation_id=conversation_id, original_text=text,
            intent=intent, confidence=confidence, created_at=now, expires_at=now + self.ttl_s,
        )
        with self._lock:
            if len(self._pending) >= self._MAX_ENTRIES and key not in self._pending:
                self._evict_expired_locked()
            self._pending[key] = entry
        return entry

    def peek(self, conversation_id: Optional[str]) -> Optional[PendingConfirmation]:
        """Read-only, non-consuming lookup - never pops, never resolves
        anything. `None` if there's nothing pending OR it already
        expired (an expired entry is popped here too, same as
        `resolve_reply()`, so a stale entry never lingers just because
        only `peek()` was ever called on it)."""
        key = conversation_id or _DEFAULT_KEY
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                return None
            if time.time() >= entry.expires_at:
                del self._pending[key]
                return None
            return entry

    def resolve_reply(self, conversation_id: Optional[str], text: str) -> Optional[ConfirmationOutcome]:
        """Checks whether `text` answers THIS conversation's pending
        confirmation. Returns `None` (caller MUST fall through and
        process `text` as a brand new, unrelated turn) when:
          - there is no pending entry for this conversation at all
            (a bare "iya"/"oke"/"lakukan" with nothing pending is a
            guaranteed no-op - `classify_confirmation_reply()` is never
            even called in that case, so it can't accidentally match);
          - the pending entry already expired (popped here, BEFORE any
            reply-matching runs - an expired confirmation can never be
            confirmed OR cancelled, it simply no longer exists, same as
            if it had never been asked);
          - `text` isn't a recognizable yes/no answer at all (the entry
            is left in place, still pending, for a later reply to
            answer).

        Cross-conversation isolation is structural, not a rule that has
        to be remembered: the pending store is keyed strictly on
        `conversation_id` (or the shared sentinel ONLY when
        `conversation_id` is `None`), so conversation A's reply can only
        ever look up conversation A's dict entry - it is not possible
        for A to resolve B's pending confirmation because they are
        different dict keys.

        One-shot: on a recognizable yes/no answer, the entry is POPPED
        before this method returns - a second identical "iya" sent
        again afterward finds nothing pending and returns `None`
        (duplicate confirmations are inherently no-ops, not re-executed)."""
        key = conversation_id or _DEFAULT_KEY
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                return None
            if time.time() >= entry.expires_at:
                del self._pending[key]
                return None
            answer = classify_confirmation_reply(text)
            if answer is None:
                return None
            del self._pending[key]
        return ConfirmationOutcome(action="confirmed" if answer else "cancelled", pending=entry)

    def prompt_for(self, pending: PendingConfirmation) -> str:
        """Deterministic, template-based confirmation question - NEVER
        an LLM call (spec's explicit requirement)."""
        label = _INTENT_LABELS.get(pending.intent, pending.intent.replace("_", " "))
        return f"Sepertinya kamu maksudnya soal {label}: \"{pending.original_text}\" - mau aku lanjutkan? (ya/tidak)"

    def cancelled_ack(self) -> str:
        """Deterministic, template-based decline acknowledgment - same
        "no LLM call" rule as `prompt_for()`."""
        return "Oke, dibatalkan."

    # -- housekeeping -----------------------------------------------------

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items() if now >= v.expires_at]
        for k in expired:
            del self._pending[k]

    def snapshot(self) -> Dict[str, int]:
        """Dashboard/test introspection only - counts, never leaks
        `original_text`/`request_id` content."""
        with self._lock:
            return {"pending_count": len(self._pending)}
