"""Explicit compact activity reconciliation for durable terminal sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.storage.hub.protocol import Cursor, HubDatabase
from gobby.storage.session_lifecycle import session_has_retained_references
from gobby.storage.session_models import Session
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class SessionActivityManager(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...


class _SessionChangeNotifier(Protocol):
    def _notify_session_change(self, event: str, session_id: str) -> None: ...


@dataclass(frozen=True)
class SessionActivityResolution:
    """Result of preferring an explicitly resolved compact caller."""

    session: Session | None = None
    deleted_ghost_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error: str | None = None
    conflicting_session_ids: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return self.session is not None and self.error_code is None

    def error_result(self) -> dict[str, Any]:
        return {
            "error": self.error or "Compact session activity reconciliation failed.",
            "error_code": self.error_code or "compact_identity_reconciliation_failed",
            "conflicting_session_ids": list(self.conflicting_session_ids),
        }


def reconcile_compact_session_activity(
    manager: SessionActivityManager,
    session_id: str,
) -> SessionActivityResolution:
    """Reactivate an explicit compact caller and guardedly remove empty ghosts."""
    current = manager.get(session_id)
    if current is None:
        return SessionActivityResolution(
            error_code="session_not_found",
            error=f"Compact session {session_id} was not found.",
        )
    if current.status == "deleted":
        return SessionActivityResolution(
            error_code="session_deleted",
            error=f"Compact session {session_id} is deleted.",
        )
    if current.session_type != "terminal" or not current.terminal_context:
        return _activate_without_competitors(manager, current)

    now = utc_now()
    deleted_ids: list[str] = []
    conflicts: list[str] = []
    with manager.db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM sessions
            WHERE machine_id = %s
              AND session_type = 'terminal'
              AND id != %s
              AND status != 'deleted'
              AND (created_at, id) > (%s, %s)
            ORDER BY created_at, id
            FOR UPDATE
            """,
            (current.machine_id, current.id, current.created_at, current.id),
        ).fetchall()
        competitors = [
            candidate
            for row in rows
            if terminal_process_contexts_match(
                (candidate := Session.from_row(row)).terminal_context,
                current.terminal_context,
            )
        ]
        ghosts: list[Session] = []
        for competitor in competitors:
            if _is_empty_compact_ghost(manager.db, competitor):
                ghosts.append(competitor)
            elif not _is_ended_terminal_sibling(competitor):
                conflicts.append(competitor.id)

        if conflicts:
            return SessionActivityResolution(
                error_code="compact_identity_conflict",
                error=(
                    "Compact session identity conflicts with populated or retained terminal rows."
                ),
                conflicting_session_ids=tuple(conflicts),
            )

        updated = conn.execute(
            """
            UPDATE sessions
            SET status = 'active',
                transcript_processed = FALSE,
                updated_at = %s,
                last_activity = %s
            WHERE id = %s
              AND status != 'deleted'
            """,
            (now, now, current.id),
        )
        if not _updated_once(updated):
            return SessionActivityResolution(
                error_code="session_deleted",
                error=f"Compact session {current.id} is deleted.",
            )
        for ghost in ghosts:
            conn.execute("DELETE FROM sessions WHERE id = %s", (ghost.id,))
            deleted_ids.append(ghost.id)

    _notify_session_change(manager, "session_updated", current.id)
    for deleted_id in deleted_ids:
        _notify_session_change(manager, "session_deleted", deleted_id)
        logger.info(
            "Deleted empty compact ghost session %s after restoring explicit owner %s",
            deleted_id,
            current.id,
            extra={
                "event": "compact_identity_ghost_deleted",
                "session_id": current.id,
                "ghost_session_id": deleted_id,
            },
        )
    return SessionActivityResolution(
        session=manager.get(current.id),
        deleted_ghost_ids=tuple(deleted_ids),
    )


def _activate_without_competitors(
    manager: SessionActivityManager,
    current: Session,
) -> SessionActivityResolution:
    now = utc_now()
    updated = manager.db.execute(
        """
        UPDATE sessions
        SET status = 'active',
            transcript_processed = FALSE,
            updated_at = %s,
            last_activity = %s
        WHERE id = %s
          AND status != 'deleted'
        """,
        (now, now, current.id),
    )
    if not _updated_once(updated):
        return SessionActivityResolution(
            error_code="session_deleted",
            error=f"Compact session {current.id} is deleted.",
        )
    _notify_session_change(manager, "session_updated", current.id)
    return SessionActivityResolution(session=manager.get(current.id))


def _is_ended_terminal_sibling(session: Session) -> bool:
    """Later same-pane rows that already finished must not block compact_self."""
    return session.status in {"handoff_ready", "expired"}


def _is_empty_compact_ghost(db: HubDatabase, session: Session) -> bool:
    if _has_durable_activity(session):
        return False
    if session.transcript_path and Path(session.transcript_path).is_file():
        return False
    return not session_has_retained_references(db, session.id)


def _has_durable_activity(session: Session) -> bool:
    numeric_activity = (
        session.message_count,
        session.turn_count,
        session.tool_call_count,
        session.usage_input_tokens,
        session.usage_output_tokens,
        session.usage_cache_creation_tokens,
        session.usage_cache_read_tokens,
    )
    retained_content = (
        session.summary_markdown,
        session.digest_markdown,
        session.last_turn_markdown,
        session.last_assistant_content,
        session.original_prompt,
        session.workflow_name,
        session.agent_run_id,
    )
    return session.had_edits or any(numeric_activity) or any(retained_content)


def _notify_session_change(
    manager: SessionActivityManager,
    event: str,
    session_id: str,
) -> None:
    try:
        cast(_SessionChangeNotifier, manager)._notify_session_change(event, session_id)
    except Exception:
        logger.warning(
            "Session change notification failed for %s (%s)",
            session_id,
            event,
            exc_info=True,
        )


def _updated_once(cursor: Cursor) -> bool:
    return cursor.rowcount == 1
