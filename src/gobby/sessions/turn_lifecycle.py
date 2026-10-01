"""Provider-aware, generation-fenced session turn lifecycle reduction."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from gobby.storage.attention import (
    AttentionState,
    AttentionStateManager,
    session_attention_entry_id,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import TERMINAL_SESSION_STATUSES

logger = logging.getLogger(__name__)

TurnDisposition = Literal[
    "completed",
    "ended_non_user",
    "user_interrupted",
    "unknown",
]
WaitKind = Literal["input", "approval", "handoff"]
WaitResolution = Literal["resumed", "abandoned", "ambiguous"]
WaitState = Literal["open", "resolving"]


class SessionLifecycleStore(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Any | None: ...

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        activity_confirmed: bool = False,
    ) -> bool: ...


@dataclass(frozen=True)
class TurnEvidence:
    """Correlation carried by one provider or managed-surface observation."""

    source: str
    generation: int | None = None
    provider_turn_key: str | None = None
    request_id: str | None = None
    cursor: str | int | None = None


@dataclass(frozen=True)
class OutstandingWait:
    """One exact interaction that protects the current composer."""

    token: str
    kind: WaitKind
    state: WaitState = "open"
    request_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "kind": self.kind,
            "state": self.state,
            "request_id": self.request_id,
        }

    @classmethod
    def from_value(cls, value: object) -> OutstandingWait | None:
        if not isinstance(value, Mapping):
            return None
        token = value.get("token")
        kind = value.get("kind")
        state = value.get("state", "open")
        if not isinstance(token, str) or kind not in {"input", "approval", "handoff"}:
            return None
        if state not in {"open", "resolving"}:
            state = "open"
        request_id = value.get("request_id")
        return cls(
            token=token,
            kind=cast(WaitKind, kind),
            state=cast(WaitState, state),
            request_id=request_id if isinstance(request_id, str) else None,
        )


@dataclass(frozen=True)
class ProviderErrorState:
    """Last failed provider turn and its consecutive recovery count."""

    error_type: str
    message: str
    attempts: int
    retryable: bool
    generation: int

    def to_dict(self) -> dict[str, object]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "attempts": self.attempts,
            "retryable": self.retryable,
            "generation": self.generation,
        }

    @classmethod
    def from_value(cls, value: object) -> ProviderErrorState | None:
        if not isinstance(value, Mapping):
            return None
        error_type = _optional_str(value.get("error_type"))
        message = _optional_str(value.get("message"))
        attempts = value.get("attempts")
        retryable = value.get("retryable")
        generation = value.get("generation")
        if (
            error_type is None
            or message is None
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 1
            or not isinstance(retryable, bool)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            return None
        return cls(error_type, message, attempts, retryable, generation)


@dataclass(frozen=True)
class TurnLifecycleState:
    """Durable correlation for the current local turn generation."""

    generation: int = 0
    provider_turn_key: str | None = None
    waits: tuple[OutstandingWait, ...] = ()
    turn_state: Literal["open", "terminal"] = "terminal"
    prior_status: str = "paused"
    request_ids: tuple[str, ...] = ()
    evidence_source: str | None = None
    cursor: str | int | None = None
    provider_error: ProviderErrorState | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "provider_turn_key": self.provider_turn_key,
            "outstanding_wait_tokens": [wait.to_dict() for wait in self.waits],
            "turn_state": self.turn_state,
            "prior_status": self.prior_status,
            "request_ids": list(self.request_ids),
            "evidence_source": self.evidence_source,
            "cursor": self.cursor,
            "provider_error": self.provider_error.to_dict() if self.provider_error else None,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | None) -> TurnLifecycleState:
        raw = payload.get("turn_lifecycle") if payload else None
        if not isinstance(raw, Mapping):
            return cls()
        generation = raw.get("generation", 0)
        waits_raw = raw.get("outstanding_wait_tokens", [])
        waits = (
            tuple(
                wait for item in waits_raw if (wait := OutstandingWait.from_value(item)) is not None
            )
            if isinstance(waits_raw, list)
            else ()
        )
        request_ids_raw = raw.get("request_ids", [])
        request_ids = (
            tuple(value for value in request_ids_raw if isinstance(value, str))
            if isinstance(request_ids_raw, list)
            else ()
        )
        turn_state = raw.get("turn_state")
        return cls(
            generation=generation if isinstance(generation, int) and generation >= 0 else 0,
            provider_turn_key=_optional_str(raw.get("provider_turn_key")),
            waits=waits,
            turn_state=turn_state if turn_state in {"open", "terminal"} else "terminal",
            prior_status=_optional_str(raw.get("prior_status")) or "paused",
            request_ids=request_ids,
            evidence_source=_optional_str(raw.get("evidence_source")),
            cursor=_cursor(raw.get("cursor")),
            provider_error=ProviderErrorState.from_value(raw.get("provider_error")),
        )


@dataclass(frozen=True)
class TurnLifecycleTransitionResult:
    """Outcome of one lifecycle observation."""

    applied: bool
    session_id: str
    generation: int
    status: str | None
    lifecycle: TurnLifecycleState
    reason: str | None = None


LifecycleMutation = Callable[
    [TurnLifecycleState, str],
    tuple[TurnLifecycleState, str],
]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _cursor(value: object) -> str | int | None:
    return value if isinstance(value, (str, int)) else None


def _status_for_waits(waits: tuple[OutstandingWait, ...]) -> str | None:
    kinds = {wait.kind for wait in waits}
    if "input" in kinds:
        return "awaiting_input"
    if "approval" in kinds:
        return "awaiting_approval"
    if "handoff" in kinds:
        return "awaiting_handoff"
    return None


class TurnLifecycleReducer:
    """Apply current-generation lifecycle evidence inside one hub transaction."""

    def __init__(
        self,
        session_manager: SessionLifecycleStore,
        attention_manager: AttentionStateManager | None = None,
    ) -> None:
        self._sessions = session_manager
        self._db = session_manager.db
        self._attention = attention_manager or AttentionStateManager(self._db)

    def get(self, session_id: str) -> TurnLifecycleState:
        attention = self._attention.get(session_attention_entry_id(session_id))
        return TurnLifecycleState.from_payload(attention.payload if attention else None)

    def begin_turn(
        self,
        session_id: str,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Start a fresh generation and invalidate prior wait or terminal evidence."""

        def mutate(current: TurnLifecycleState, status: str) -> tuple[TurnLifecycleState, str]:
            if (
                current.turn_state == "open"
                and evidence.provider_turn_key is not None
                and evidence.provider_turn_key == current.provider_turn_key
            ):
                return current, status
            request_ids = (evidence.request_id,) if evidence.request_id else ()
            return (
                TurnLifecycleState(
                    generation=current.generation + 1,
                    provider_turn_key=evidence.provider_turn_key,
                    waits=(),
                    turn_state="open",
                    prior_status=status,
                    request_ids=request_ids,
                    evidence_source=evidence.source,
                    cursor=evidence.cursor,
                    provider_error=current.provider_error,
                ),
                "active",
            )

        return self._apply(session_id, evidence, mutate, allow_new_turn=True)

    def enter_wait(
        self,
        session_id: str,
        *,
        kind: WaitKind,
        token: str,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Protect the current turn after an exact interaction becomes visible."""
        if not token:
            raise ValueError("wait token is required")

        def mutate(current: TurnLifecycleState, status: str) -> tuple[TurnLifecycleState, str]:
            if current.turn_state == "terminal" and status != "active":
                return current, status
            existing = next((wait for wait in current.waits if wait.token == token), None)
            if existing is not None:
                return current, _status_for_waits(current.waits) or status
            if token in current.request_ids:
                return current, status
            waits = (*current.waits, OutstandingWait(token, kind, request_id=evidence.request_id))
            prior_status = current.prior_status if current.waits else status
            updated = self._with_evidence(
                current,
                evidence,
                waits=waits,
                prior_status=prior_status,
                turn_state="open",
            )
            if token not in updated.request_ids:
                updated = replace(updated, request_ids=(*updated.request_ids, token))
            return updated, _status_for_waits(waits) or status

        return self._apply(session_id, evidence, mutate)

    def resolve_wait(
        self,
        session_id: str,
        *,
        token: str,
        resolution: WaitResolution,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Resolve one exact wait; ambiguous outcomes stay protected."""
        if not token:
            raise ValueError("wait token is required")

        def mutate(current: TurnLifecycleState, status: str) -> tuple[TurnLifecycleState, str]:
            matching = [wait for wait in current.waits if wait.token == token]
            if not matching:
                return current, status
            if resolution == "ambiguous":
                waits = tuple(
                    OutstandingWait(wait.token, wait.kind, "resolving", wait.request_id)
                    if wait.token == token
                    else wait
                    for wait in current.waits
                )
                return self._with_evidence(current, evidence, waits=waits), (
                    _status_for_waits(waits) or status
                )
            waits = tuple(wait for wait in current.waits if wait.token != token)
            next_status = _status_for_waits(waits)
            if next_status is None:
                next_status = "active" if resolution == "resumed" else "paused"
            return self._with_evidence(current, evidence, waits=waits), next_status

        return self._apply(session_id, evidence, mutate)

    def resumed_work(
        self,
        session_id: str,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Confirm that provider work resumed and clear protected waits."""

        def mutate(current: TurnLifecycleState, _status: str) -> tuple[TurnLifecycleState, str]:
            return self._with_evidence(current, evidence, waits=(), turn_state="open"), "active"

        return self._apply(session_id, evidence, mutate, accept_resolving_provider_key=True)

    def end_turn(
        self,
        session_id: str,
        disposition: TurnDisposition,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Apply terminal evidence; unknown observations retain current state."""

        def mutate(current: TurnLifecycleState, status: str) -> tuple[TurnLifecycleState, str]:
            if disposition == "unknown":
                return current, status
            next_status = "interrupted" if disposition == "user_interrupted" else "paused"
            updated = self._with_evidence(current, evidence, waits=(), turn_state="terminal")
            if disposition == "completed":
                updated = replace(updated, provider_error=None)
            return (
                updated,
                next_status,
            )

        result = self._apply(
            session_id,
            evidence,
            mutate,
            clear_provider_error=disposition == "completed",
        )
        if result.reason in {"stale_generation", "stale_provider_turn"}:
            # A rejected turn end can strand the row active, so it stays visible (#23102).
            logger.info(
                "Rejected %s turn end for session %s: %s (source=%s evidence_generation=%s "
                "generation=%s status=%s)",
                disposition,
                session_id,
                result.reason,
                evidence.source,
                evidence.generation,
                result.generation,
                result.status,
            )
        return result

    def record_provider_failure(
        self,
        session_id: str,
        *,
        error_type: str,
        message: str,
        retryable: bool,
        max_resumes: int,
        evidence: TurnEvidence,
    ) -> TurnLifecycleTransitionResult:
        """Persist a failed turn once per generation and block terminal failures."""

        def mutate(current: TurnLifecycleState, status: str) -> tuple[TurnLifecycleState, str]:
            previous = current.provider_error
            if (
                current.turn_state == "terminal"
                and previous is not None
                and previous.generation == current.generation
            ):
                return current, status
            attempts = (previous.attempts if previous is not None else 0) + 1
            updated = self._with_evidence(current, evidence, waits=(), turn_state="terminal")
            return (
                replace(
                    updated,
                    provider_error=ProviderErrorState(
                        error_type, message, attempts, retryable, current.generation
                    ),
                ),
                "paused",
            )

        return self._apply(
            session_id,
            evidence,
            mutate,
            provider_failure_limit=max_resumes,
        )

    def block_provider_failure(
        self,
        session_id: str,
        *,
        generation: int,
        attempts: int,
    ) -> bool:
        """Show a delivery failure only if its failed turn is still current."""
        entry_id = session_attention_entry_id(session_id)
        with self._db.transaction() as transaction:
            row = transaction.execute(
                "SELECT * FROM attention_states WHERE entry_id = %s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            attention = AttentionState.from_row(row)
            lifecycle = TurnLifecycleState.from_payload(attention.payload)
            failure = lifecycle.provider_error
            if (
                failure is None
                or lifecycle.generation != generation
                or failure.attempts != attempts
                or lifecycle.turn_state != "terminal"
            ):
                return False
            return self._attention.transition(
                entry_id,
                state="blocked",
                session_id=session_id,
                reason="provider_error",
                kind="non_actionable",
                fingerprint=f"provider_error:{generation}:{attempts}",
                payload=self._provider_error_payload(lifecycle, attention.payload),
                expected_attention_id=attention.attention_id,
            ).applied

    @staticmethod
    def _provider_error_payload(
        lifecycle: TurnLifecycleState, existing: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        failure = lifecycle.provider_error
        payload = dict(existing or {})
        payload["turn_lifecycle"] = lifecycle.to_dict()
        if failure is not None:
            payload["error_type"] = failure.error_type
            payload["message"] = failure.message
        return payload

    def _apply(
        self,
        session_id: str,
        evidence: TurnEvidence,
        mutate: LifecycleMutation,
        *,
        allow_new_turn: bool = False,
        accept_resolving_provider_key: bool = False,
        clear_provider_error: bool = False,
        provider_failure_limit: int | None = None,
    ) -> TurnLifecycleTransitionResult:
        entry_id = session_attention_entry_id(session_id)
        with self._db.transaction() as transaction:
            row = transaction.execute(
                "SELECT * FROM attention_states WHERE entry_id = %s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            attention = AttentionState.from_row(row) if row is not None else None
            current = TurnLifecycleState.from_payload(attention.payload if attention else None)
            session = self._sessions.get(session_id)
            if session is None:
                return TurnLifecycleTransitionResult(
                    False, session_id, current.generation, None, current, "session_not_found"
                )
            raw_status = getattr(session, "status", None)
            session_status = raw_status if isinstance(raw_status, str) else "paused"
            # Terminal ownership/explicit resume must reactivate a session first.
            # Delayed hook evidence alone cannot reclaim a superseded session.
            if session_status in TERMINAL_SESSION_STATUSES:
                return TurnLifecycleTransitionResult(
                    False,
                    session_id,
                    current.generation,
                    session_status,
                    current,
                    "session_terminal",
                )
            stale_reason = None if allow_new_turn else self._stale_reason(current, evidence)
            if (
                stale_reason == "stale_provider_turn"
                and accept_resolving_provider_key
                and current.waits
                and all(wait.state == "resolving" for wait in current.waits)
            ):
                stale_reason = None
            if stale_reason:
                logger.debug(
                    "Rejected turn lifecycle evidence for session %s: %s",
                    session_id,
                    stale_reason,
                )
                return TurnLifecycleTransitionResult(
                    False,
                    session_id,
                    current.generation,
                    session_status,
                    current,
                    stale_reason,
                )
            updated, next_status = mutate(current, session_status)
            if updated == current and next_status == session_status:
                return TurnLifecycleTransitionResult(
                    False, session_id, current.generation, session_status, current, "duplicate"
                )
            next_attention_state = attention.state if attention else None
            next_reason = attention.reason if attention else None
            next_kind = attention.kind if attention else None
            next_fingerprint = attention.fingerprint if attention else None
            payload = dict(attention.payload) if attention else {}
            payload["turn_lifecycle"] = updated.to_dict()
            failure = updated.provider_error
            if clear_provider_error and next_reason == "provider_error":
                next_attention_state = None
                next_reason = None
                next_kind = None
            elif (
                provider_failure_limit is not None
                and failure is not None
                and (not failure.retryable or failure.attempts > provider_failure_limit)
            ):
                next_attention_state = "blocked"
                next_reason = "provider_error"
                next_kind = "non_actionable"
                next_fingerprint = f"provider_error:{updated.generation}:{failure.attempts}"
                payload = self._provider_error_payload(updated, payload)
            self._attention.transition(
                entry_id,
                state=next_attention_state,
                run_id=attention.run_id if attention else None,
                session_id=session_id,
                reason=next_reason,
                kind=next_kind,
                fingerprint=next_fingerprint,
                payload=payload,
                expected_attention_id=attention.attention_id if attention else None,
            )
            if next_status != session_status:
                self._sessions.update_session_status(
                    session_id,
                    next_status,
                    activity_confirmed=True,
                )
            return TurnLifecycleTransitionResult(
                True, session_id, updated.generation, next_status, updated
            )

    @staticmethod
    def _stale_reason(current: TurnLifecycleState, evidence: TurnEvidence) -> str | None:
        if evidence.generation is not None and evidence.generation != current.generation:
            return "stale_generation"
        if (
            evidence.provider_turn_key is not None
            and current.provider_turn_key is not None
            and evidence.provider_turn_key != current.provider_turn_key
        ):
            return "stale_provider_turn"
        return None

    @staticmethod
    def _with_evidence(
        current: TurnLifecycleState,
        evidence: TurnEvidence,
        *,
        waits: tuple[OutstandingWait, ...] | None = None,
        turn_state: Literal["open", "terminal"] | None = None,
        prior_status: str | None = None,
    ) -> TurnLifecycleState:
        request_ids = current.request_ids
        if evidence.request_id and evidence.request_id not in request_ids:
            request_ids = (*request_ids, evidence.request_id)
        return TurnLifecycleState(
            generation=current.generation,
            provider_turn_key=current.provider_turn_key or evidence.provider_turn_key,
            waits=current.waits if waits is None else waits,
            turn_state=current.turn_state if turn_state is None else turn_state,
            prior_status=current.prior_status if prior_status is None else prior_status,
            request_ids=request_ids,
            evidence_source=evidence.source,
            cursor=evidence.cursor if evidence.cursor is not None else current.cursor,
            provider_error=current.provider_error,
        )
