"""Terminal ownership revival for expired session rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session
from gobby.terminal_ownership import (
    TERMINAL_OWNER_STATUSES,
    resolve_pane_ownership,
    terminal_session_creation_order,
    terminal_session_identity,
)
from gobby.utils.datetime import utc_now

from ._constants import get_logger, past_terminal_revival_horizon

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _RevivalHost(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _TerminalRevivalMixin:
    def revive_expired_terminal_session(self: _RevivalHost, session_id: str) -> Session | None:
        """Reconcile terminal ownership when fresh activity arrives.

        Interactive claims require foreground-process validation when they
        compete. Spawned-only claims retain newest-created ownership.
        """
        current = self.get(session_id)
        if current is None:
            return None
        if current.session_type != "terminal":
            return current
        if current.status not in {*TERMINAL_OWNER_STATUSES, "expired"}:
            return current

        past_horizon = past_terminal_revival_horizon(current)
        identity = terminal_session_identity(current)
        if identity is None:
            if current.status != "expired" or past_horizon:
                return current

            now = utc_now()
            with self.db.transaction():
                self.db.execute(
                    """
                    UPDATE sessions
                    SET status = 'active',
                        transcript_processed = FALSE,
                        updated_at = %s,
                        last_activity = %s
                    WHERE id = %s
                      AND status = 'expired'
                      AND session_type = 'terminal'
                    """,
                    (now, now, session_id),
                )
            updated = self.get(session_id)
            if updated is not None and updated.status == "active":
                self._notify_session_change("session_updated", session_id)
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=now)
                )
            return updated

        machine_id, socket_identity, pane = identity
        now = utc_now()
        status_changes: list[tuple[Session, str]] = []
        owner: Session | None = None
        inconclusive_reason: str | None = None
        claimant_ids: list[str] = []
        validated_ids: list[str] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sessions
                WHERE machine_id IS NOT DISTINCT FROM %s
                  AND session_type = 'terminal'
                  AND BTRIM(terminal_context ->> 'tmux_pane') = %s
                  AND CASE
                      WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket_path'), '')
                          IS NOT NULL
                      THEN 'tmux_socket_path:'
                          || BTRIM(terminal_context ->> 'tmux_socket_path')
                      WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket_name'), '')
                          IS NOT NULL
                      THEN 'tmux_socket_name:'
                          || BTRIM(terminal_context ->> 'tmux_socket_name')
                      WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket'), '') IS NOT NULL
                      THEN 'tmux_socket:' || BTRIM(terminal_context ->> 'tmux_socket')
                      ELSE NULL
                  END = %s
                  AND status != 'deleted'
                ORDER BY created_at, id
                FOR UPDATE
                """,
                (machine_id, pane, socket_identity),
            ).fetchall()
            matching = [
                candidate
                for row in rows
                if terminal_session_identity(candidate := Session.from_row(row)) == identity
            ]
            candidates = [
                candidate
                for candidate in matching
                if candidate.status in TERMINAL_OWNER_STATUSES
                or (candidate.id == session_id and candidate.status == "expired")
            ]
            claimant_ids = [candidate.id for candidate in candidates]

            if len(candidates) == 1:
                candidate = candidates[0]
                if candidate.status != "expired":
                    owner = candidate
                elif not past_horizon:
                    owner = candidate
                elif (
                    candidate.id == session_id
                    and candidate.agent_run_id is None
                    and candidate.agent_depth == 0
                ):
                    decision = resolve_pane_ownership(
                        [candidate],
                        requested_session_id=session_id,
                    )
                    validated_ids = sorted(decision.validated_session_ids)
                    if decision.owner_session_id == session_id:
                        owner = candidate
                    else:
                        inconclusive_reason = decision.reason
            elif len(candidates) > 1:
                interactive_candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.agent_run_id is None and candidate.agent_depth == 0
                ]
                if interactive_candidates:
                    decision = resolve_pane_ownership(
                        interactive_candidates,
                        requested_session_id=session_id,
                    )
                    validated_ids = sorted(decision.validated_session_ids)
                    owner = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate.id == decision.owner_session_id
                        ),
                        None,
                    )
                    if owner is None:
                        inconclusive_reason = decision.reason
                elif not past_horizon:
                    owner = max(candidates, key=terminal_session_creation_order)

            if owner is not None:
                reset_target_transcript = owner.id == session_id and owner.status == "expired"
                for candidate in candidates:
                    desired_status = candidate.status
                    if candidate.id == owner.id and candidate.status == "expired":
                        desired_status = "active"
                    elif candidate.id != owner.id and candidate.status in TERMINAL_OWNER_STATUSES:
                        desired_status = "expired"

                    reset_transcript = reset_target_transcript and candidate.id == session_id
                    if desired_status == candidate.status and not reset_transcript:
                        continue

                    revived = candidate.id == owner.id and desired_status == "active"
                    conn.execute(
                        """
                        UPDATE sessions
                        SET status = %s,
                            transcript_processed = CASE
                                WHEN %s THEN FALSE
                                ELSE transcript_processed
                            END,
                            updated_at = %s,
                            last_activity = CASE
                                WHEN %s THEN %s
                                ELSE last_activity
                            END
                        WHERE id = %s
                        """,
                        (desired_status, reset_transcript, now, revived, now, candidate.id),
                    )
                    if desired_status != candidate.status:
                        status_changes.append((candidate, desired_status))

        if owner is None:
            if inconclusive_reason is not None:
                get_logger().debug(
                    "Terminal ownership reconciliation was inconclusive for %s: %s",
                    session_id,
                    inconclusive_reason,
                    extra={
                        "event": "terminal_session_ownership_inconclusive",
                        "reason": inconclusive_reason,
                        "terminal_identity": identity,
                        "requested_session_id": session_id,
                        "terminal_claimant_session_ids": claimant_ids,
                        "validated_session_ids": validated_ids,
                    },
                )
            return self.get(session_id)

        logger = get_logger()
        if current.status == "expired" and current.id != owner.id:
            logger.debug(
                "Suppressed revival of superseded terminal session %s; owner is %s",
                current.id,
                owner.id,
                extra={
                    "event": "terminal_session_revival_suppressed",
                    "session_id": current.id,
                    "terminal_owner_session_id": owner.id,
                    "machine_id": machine_id,
                    "tmux_socket": socket_identity,
                    "tmux_pane": pane,
                },
            )

        for candidate, desired_status in status_changes:
            event = "session_expired" if desired_status == "expired" else "session_updated"
            self._notify_session_change(event, candidate.id)
            self._notify_status_transition(
                SessionStatusTransition.from_session(
                    candidate,
                    status=desired_status,
                    transitioned_at=now,
                )
            )
            if desired_status == "expired":
                logger.info(
                    "Expired superseded terminal session %s; owner is %s",
                    candidate.id,
                    owner.id,
                    extra={
                        "event": "terminal_session_owner_superseded",
                        "session_id": candidate.id,
                        "terminal_owner_session_id": owner.id,
                        "machine_id": machine_id,
                        "tmux_socket": socket_identity,
                        "tmux_pane": pane,
                    },
                )

        return self.get(session_id)
