"""Attempt lifecycle and successor resolution for clear-session handoffs."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard
from uuid import uuid4

from gobby.sessions.compact_continuation import (
    _format_timestamp,
    _load_variables,
    _parse_timestamp,
    _row_variables,
    schedule_handoff_compact_continuation,
)
from gobby.sessions.handoff import (
    PENDING_HANDOFF_VARIABLE,
    FeedbackObservation,
    HandoffAttemptState,
    restore_handoff_attempt,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.title_lifecycle import clear_successor_title
from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionLineageMutation,
    SessionRegistration,
    SessionSeqMutation,
    SessionVariableMutation,
    WebChatSessionBootstrap,
)
from gobby.storage.session_models import Session
from gobby.storage.sessions._lineage_guard import sanitize_parent_session_id
from gobby.utils.datetime import utc_now

__all__ = [
    "CLEAR_ATTEMPT_VARIABLE",
    "CLEAR_HANDOFF_TTL_SECONDS",
    "ClearContinuationResolution",
    "clear_failed_attempt",
    "commit_web_chat_clear_successor",
    "resolve_clear_continuation",
    "schedule_handoff_continuation",
    "stage_clear_attempt",
    "take_clear_handoff_marker",
]

logger = logging.getLogger(__name__)

CLEAR_HANDOFF_TTL_SECONDS = 600
CLEAR_ATTEMPT_VARIABLE = "clear_attempt"
MAX_CLEAR_CONTINUATION_CANDIDATES = 250


@dataclass
class ClearContinuationResolution:
    """Successor-side resolution of a pending clear handoff."""

    predecessor: Session | None = None
    attempt_id: str | None = None
    degrade_reason: str | None = None


def stage_clear_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    handoff_markdown: str,
    observations: Sequence[FeedbackObservation],
    terminal_context: dict[str, Any] | None,
    chat_context: dict[str, Any] | None,
) -> HandoffAttemptState:
    """Write the one-shot clear-attempt marker on the predecessor row.

    ``handoff_ready`` status is never set.
    """
    marker = {
        "attempt_id": attempt_id,
        "created_at": _format_timestamp(datetime.now(UTC)),
        "terminal_context": terminal_context,
        "chat": _chat_payload(chat_context),
        "consumed_by": None,
    }
    return stage_handoff_attempt(
        db,
        session_id,
        attempt_id=attempt_id,
        markdown=handoff_markdown,
        observations=observations,
        clear_session=True,
        additional_markers={CLEAR_ATTEMPT_VARIABLE: marker},
    )


def resolve_clear_continuation(
    db: HubDatabase,
    *,
    source: str,
    project_id: str,
    machine_id: str,
    terminal_context: dict[str, Any] | None,
    predecessor_hint: str | None,
) -> ClearContinuationResolution:
    """Find the predecessor for a SessionStart(source='clear')."""
    try:
        rows = db.fetchall(
            """
            SELECT s.*, sv.variables AS session_variables
              FROM sessions s
              JOIN session_variables sv ON sv.session_id = s.id
             WHERE s.source = %s
               AND s.status <> 'deleted'
               AND jsonb_typeof(sv.variables -> %s) = 'object'
               AND (sv.variables -> %s ->> 'consumed_by') IS NULL
             ORDER BY (sv.variables -> %s ->> 'created_at') DESC, s.id DESC
             LIMIT %s
            """,
            (
                source,
                CLEAR_ATTEMPT_VARIABLE,
                CLEAR_ATTEMPT_VARIABLE,
                CLEAR_ATTEMPT_VARIABLE,
                MAX_CLEAR_CONTINUATION_CANDIDATES,
            ),
        )
    except Exception:
        logger.warning("Failed resolving clear continuation", exc_info=True)
        return ClearContinuationResolution(degrade_reason="exception")

    matches: list[tuple[Session, str]] = []
    saw_expired = False
    saw_cross_project = False
    saw_cross_machine = False
    saw_identity_mismatch = False
    for row in rows:
        session = Session.from_row(row)
        marker = _marker_from_variables(_load_variables(row["session_variables"]))
        if marker is None:
            continue
        if marker.get("consumed_by"):
            continue
        if session.project_id != project_id:
            saw_cross_project = True
            continue
        if session.machine_id != machine_id:
            saw_cross_machine = True
            continue
        if _marker_expired(marker):
            saw_expired = True
            continue
        if not _identity_matches(session, marker, terminal_context, predecessor_hint):
            saw_identity_mismatch = True
            continue
        attempt_id = marker.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            continue
        matches.append((session, attempt_id))

    if len(matches) == 1:
        session, attempt_id = matches[0]
        return ClearContinuationResolution(predecessor=session, attempt_id=attempt_id)
    if len(matches) > 1:
        return ClearContinuationResolution(degrade_reason="ambiguous")
    if saw_identity_mismatch:
        return ClearContinuationResolution(degrade_reason="identity_mismatch")
    if saw_expired:
        return ClearContinuationResolution(degrade_reason="expired")
    if saw_cross_project:
        return ClearContinuationResolution(degrade_reason="cross_project")
    if saw_cross_machine:
        return ClearContinuationResolution(degrade_reason="cross_machine")
    return ClearContinuationResolution()


def take_clear_handoff_marker(
    db: HubDatabase,
    predecessor_id: str,
    *,
    attempt_id: str,
    successor_id: str,
) -> bool:
    """Atomically consume the marker and write successor parentage."""
    try:
        with db.transaction_immediate(SessionLineageMutation()) as conn:
            conn.acquire_additional_lock(SessionVariableMutation(session_id=predecessor_id))
            successor = conn.execute(
                "SELECT id FROM sessions WHERE id = %s FOR UPDATE",
                (successor_id,),
            ).fetchone()
            if successor is None:
                return False
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
                (predecessor_id,),
            ).fetchone()
            if row is None:
                return False
            variables = _load_variables(_row_variables(row))
            marker = _marker_from_variables(variables)
            if not _unconsumed_attempt(marker, attempt_id):
                return False
            sanitized = sanitize_parent_session_id(
                conn,
                child_session_id=successor_id,
                parent_session_id=predecessor_id,
                context="clear handoff take",
            )
            if sanitized is None:
                return False
            taken = dict(marker)
            taken["consumed_by"] = successor_id
            variables[CLEAR_ATTEMPT_VARIABLE] = taken
            now = utc_now()
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (json.dumps(variables), now.isoformat(), predecessor_id),
            )
            conn.execute(
                "UPDATE sessions SET parent_session_id = %s, updated_at = %s WHERE id = %s",
                (sanitized, now, successor_id),
            )
            return True
    except Exception:
        logger.warning(
            "Failed taking clear handoff marker for predecessor %s successor %s",
            predecessor_id,
            successor_id,
            exc_info=True,
        )
        return False


def clear_failed_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    attempt_state: HandoffAttemptState | None = None,
) -> bool:
    """Compare-and-clear an unconsumed marker and restore staged state."""
    try:
        if attempt_state is None:
            row = db.fetchone(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            )
            variables = _load_variables(_row_variables(row))
            marker = _marker_from_variables(variables)
            if not _unconsumed_attempt(marker, attempt_id):
                return False
            feedback_ids = marker.get("feedback_ids")
            attempt_state = HandoffAttemptState(
                session_id=session_id,
                attempt_id=attempt_id,
                prior_handoff_markdown=marker.get("prior_handoff_markdown"),
                prior_markers={},
                missing_markers=frozenset({CLEAR_ATTEMPT_VARIABLE, PENDING_HANDOFF_VARIABLE}),
                feedback_ids=tuple(item for item in feedback_ids or () if isinstance(item, str)),
            )
        return restore_handoff_attempt(db, attempt_state)
    except Exception:
        logger.warning(
            "Failed clearing handoff attempt %s for session %s",
            attempt_id,
            session_id,
            exc_info=True,
        )
        return False


class _ClearCommitAborted(Exception):
    """Abort the web-chat clear commit without leaving partial row changes."""


def commit_web_chat_clear_successor(
    db: HubDatabase,
    predecessor_id: str,
    *,
    attempt_id: str,
) -> Session | None:
    """Expire the predecessor and insert a force-new successor in one transaction."""
    bootstrap_external_id = f"web-chat-bootstrap:{uuid4()}"
    try:
        predecessor_meta = db.fetchone(
            "SELECT machine_id, source, project_id FROM sessions WHERE id = %s",
            (predecessor_id,),
        )
        if predecessor_meta is None:
            return None
        machine_id = str(predecessor_meta["machine_id"])
        source = str(predecessor_meta["source"])
        project_id = str(predecessor_meta["project_id"])
        with db.transaction_immediate(
            WebChatSessionBootstrap(
                external_id=bootstrap_external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                session_type="web_chat",
            )
        ) as conn:
            conn.acquire_additional_lock(
                SessionRegistration(
                    external_id=bootstrap_external_id,
                    source=source,
                    session_type="web_chat",
                )
            )
            conn.acquire_additional_lock(SessionLineageMutation())
            conn.acquire_additional_lock(SessionSeqMutation(project_id=project_id))
            conn.acquire_additional_lock(SessionVariableMutation(session_id=predecessor_id))
            return _commit_web_chat_clear_successor_rows(
                conn,
                predecessor_id=predecessor_id,
                attempt_id=attempt_id,
                bootstrap_external_id=bootstrap_external_id,
                source=source,
                project_id=project_id,
                machine_id=machine_id,
            )
    except _ClearCommitAborted:
        return None
    except Exception:
        logger.warning(
            "Failed committing web-chat clear successor for predecessor %s attempt %s",
            predecessor_id,
            attempt_id,
            exc_info=True,
        )
        return None


def _commit_web_chat_clear_successor_rows(
    conn: Any,
    *,
    predecessor_id: str,
    attempt_id: str,
    bootstrap_external_id: str,
    source: str,
    project_id: str,
    machine_id: str,
) -> Session:
    pred_row = conn.execute(
        "SELECT * FROM sessions WHERE id = %s FOR UPDATE",
        (predecessor_id,),
    ).fetchone()
    if pred_row is None:
        raise _ClearCommitAborted("predecessor missing")
    predecessor = Session.from_row(pred_row)
    if predecessor.status in {"expired", "deleted"}:
        raise _ClearCommitAborted("predecessor is already terminal")

    var_row = conn.execute(
        "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
        (predecessor_id,),
    ).fetchone()
    if var_row is None:
        raise _ClearCommitAborted("clear attempt missing")
    variables = _load_variables(_row_variables(var_row))
    marker = _marker_from_variables(variables)
    if not _unconsumed_attempt(marker, attempt_id):
        raise _ClearCommitAborted("clear attempt is not pending")

    successor_id = str(uuid4())
    sanitized_parent = sanitize_parent_session_id(
        conn,
        child_session_id=successor_id,
        parent_session_id=predecessor_id,
        context="web-chat clear successor",
    )
    if sanitized_parent is None:
        raise _ClearCommitAborted("parentage rejected")

    now = utc_now()
    max_seq_row = conn.execute(
        "SELECT MAX(seq_num) as max_seq FROM sessions WHERE project_id = %s",
        (project_id,),
    ).fetchone()
    next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
    title, title_source = clear_successor_title(conn, predecessor, next_seq_num)
    conn.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, title, title_source,
            transcript_path, git_branch, parent_session_id,
            agent_depth, spawned_by_agent_id, terminal_context,
            workflow_name, session_type, is_local, sandbox_enabled, sandbox_policy_hash,
            status, seq_num,
            had_edits, message_count, turn_count, tool_call_count, last_assistant_content
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            'active', %s, FALSE, 0, 0, 0, NULL
        )
        """,
        (
            successor_id,
            bootstrap_external_id,
            machine_id,
            source,
            project_id,
            title,
            title_source,
            None,
            predecessor.git_branch,
            sanitized_parent,
            predecessor.agent_depth,
            predecessor.spawned_by_agent_id,
            json.dumps(predecessor.terminal_context) if predecessor.terminal_context else None,
            predecessor.workflow_name,
            "web_chat",
            bool(predecessor.is_local),
            predecessor.sandbox_enabled,
            predecessor.sandbox_policy_hash,
            next_seq_num,
        ),
    )
    conn.execute(
        """
        UPDATE sessions
           SET model = COALESCE(%s, model),
               chat_mode = COALESCE(%s, chat_mode),
               updated_at = %s
         WHERE id = %s
        """,
        (predecessor.model, predecessor.chat_mode, now, successor_id),
    )
    conn.execute(
        """
        UPDATE sessions
           SET status = 'expired', updated_at = %s
         WHERE id = %s
        """,
        (now, predecessor_id),
    )
    taken = dict(marker)
    taken["consumed_by"] = successor_id
    variables[CLEAR_ATTEMPT_VARIABLE] = taken
    conn.execute(
        """
        UPDATE session_variables
           SET variables = %s, updated_at = %s
         WHERE session_id = %s
        """,
        (json.dumps(variables), now.isoformat(), predecessor_id),
    )
    conn.execute(
        """
        INSERT INTO session_variables (session_id, variables, updated_at)
        VALUES (%s, %s, %s)
        """,
        (successor_id, json.dumps({}), now.isoformat()),
    )
    succ_row = conn.execute(
        "SELECT * FROM sessions WHERE id = %s",
        (successor_id,),
    ).fetchone()
    if succ_row is None:
        raise _ClearCommitAborted("successor disappeared after insert")
    return Session.from_row(succ_row)


def schedule_handoff_continuation(
    session: Any,
    prompt: str,
    *,
    loop: Any | None = None,
    delay_seconds: float | None = None,
) -> bool:
    """Schedule delivery of the continue prompt to the successor terminal."""
    kwargs: dict[str, Any] = {"loop": loop}
    if delay_seconds is not None:
        kwargs["delay_seconds"] = delay_seconds
    return schedule_handoff_compact_continuation(session, prompt, **kwargs)


def _chat_payload(chat_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(chat_context, dict):
        return None
    payload = {key: chat_context[key] for key in ("model", "mode") if key in chat_context}
    return payload or None


def _marker_from_variables(variables: dict[str, Any]) -> dict[str, Any] | None:
    marker = variables.get(CLEAR_ATTEMPT_VARIABLE)
    return dict(marker) if isinstance(marker, dict) else None


def _unconsumed_attempt(
    marker: dict[str, Any] | None, attempt_id: str
) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(marker, dict)
        and marker.get("attempt_id") == attempt_id
        and not marker.get("consumed_by")
    )


def _marker_expired(marker: dict[str, Any]) -> bool:
    created_at = _parse_timestamp(marker.get("created_at"))
    if created_at is None:
        return True
    age = (datetime.now(UTC) - created_at).total_seconds()
    return age < 0 or age > CLEAR_HANDOFF_TTL_SECONDS


def _identity_matches(
    session: Session,
    marker: dict[str, Any],
    terminal_context: dict[str, Any] | None,
    predecessor_hint: str | None,
) -> bool:
    if predecessor_hint and predecessor_hint == session.id:
        return True
    stored_context = marker.get("terminal_context") or session.terminal_context
    return terminal_process_contexts_match(stored_context, terminal_context)
