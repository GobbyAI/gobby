"""Attempt lifecycle and successor resolution for clear-session handoffs."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
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
    HANDOFF_PULL_PENDING_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    HandoffAttemptState,
    _store_variables,
    restore_handoff_attempt,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.handoff_records import (
    HandoffPayload,
    delete_undelivered_handoff,
    insert_delivery_receipt,
    insert_handoff_record,
)
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
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER, TERMINAL_SESSION_STATUSES
from gobby.storage.sessions._lineage_guard import sanitize_parent_session_id
from gobby.utils.datetime import utc_now

__all__ = [
    "CLEAR_ATTEMPT_VARIABLE",
    "CLEAR_HANDOFF_TTL_SECONDS",
    "AWAITING_HANDOFF_STATUS",
    "ClearContinuationResolution",
    "clear_failed_attempt",
    "commit_web_chat_clear_successor",
    "mark_clear_command_sent",
    "pending_clear_attempt",
    "refresh_clear_attempt_content",
    "resolve_clear_continuation",
    "schedule_handoff_continuation",
    "stage_clear_attempt",
    "take_clear_handoff_marker",
]

logger = logging.getLogger(__name__)

CLEAR_HANDOFF_TTL_SECONDS = 600
CLEAR_ATTEMPT_VARIABLE = "clear_attempt"
# Status a clear predecessor holds from staging until its successor binds; the
# startup context-reuse expiry and SessionEnd never touch rows in this status.
AWAITING_HANDOFF_STATUS = "awaiting_handoff"
MAX_CLEAR_CONTINUATION_CANDIDATES = 250


@dataclass
class ClearContinuationResolution:
    """Successor-side resolution of a pending clear handoff.

    ``supersedes`` names a same-pane successor that bound the marker but never
    pulled the handoff (an operator ran ``/clear`` again); the new session takes
    the marker, parent, and claims over from it.
    """

    predecessor: Session | None = None
    attempt_id: str | None = None
    degrade_reason: str | None = None
    supersedes: str | None = None


def stage_clear_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    handoff: HandoffPayload,
    terminal_context: dict[str, Any] | None,
    chat_context: dict[str, Any] | None,
) -> HandoffAttemptState:
    """Write the one-shot clear-attempt marker and move the row to ``awaiting_handoff``.

    ``command_sent_at`` is stamped by :func:`mark_clear_command_sent` once ``/clear``
    is on the pane; until then a failed delivery restores the row.
    """
    marker = {
        "attempt_id": attempt_id,
        "created_at": _format_timestamp(datetime.now(UTC)),
        "terminal_context": terminal_context,
        "chat": _chat_payload(chat_context),
        "consumed_by": None,
        "command_sent_at": None,
    }
    return stage_handoff_attempt(
        db,
        session_id,
        attempt_id=attempt_id,
        handoff=handoff,
        clear_session=True,
        additional_markers={CLEAR_ATTEMPT_VARIABLE: marker},
        transition_status=AWAITING_HANDOFF_STATUS,
    )


def mark_clear_command_sent(db: HubDatabase, session_id: str, *, attempt_id: str) -> bool:
    """Stamp ``command_sent_at`` on the unconsumed attempt after ``/clear`` is delivered."""
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            variables = _load_variables(_row_variables(row))
            marker = _marker_from_variables(variables)
            if not _unconsumed_attempt(marker, attempt_id):
                return False
            marker["command_sent_at"] = _format_timestamp(datetime.now(UTC))
            variables[CLEAR_ATTEMPT_VARIABLE] = marker
            _store_variables(conn, session_id, variables, exists=row is not None)
            return True
    except Exception:
        logger.warning(
            "Failed marking clear command sent for attempt %s on session %s",
            attempt_id,
            session_id,
            exc_info=True,
        )
        return False


def pending_clear_attempt(db: HubDatabase, session_id: str) -> dict[str, Any] | None:
    """Return the delivered, unconsumed, unexpired clear attempt for ``session_id``."""
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    marker = _marker_from_variables(_load_variables(_row_variables(row)))
    if (
        marker is None
        or marker.get("consumed_by")
        or not marker.get("command_sent_at")
        or _marker_expired(marker)
        or not isinstance(marker.get("attempt_id"), str)
    ):
        return None
    return marker


def refresh_clear_attempt_content(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    handoff: HandoffPayload,
) -> bool:
    """Rewrite the handoff content of a still-pending attempt without re-staging it."""
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            variables = _load_variables(_row_variables(row))
            marker = _marker_from_variables(variables)
            if not _unconsumed_attempt(marker, attempt_id):
                return False
            old_handoff_id = marker.get("handoff_record_id")
            if not isinstance(old_handoff_id, str) or not old_handoff_id:
                return False
            delivered = conn.execute(
                "SELECT 1 FROM session_handoff_deliveries WHERE handoff_id = %s",
                (old_handoff_id,),
            ).fetchone()
            if delivered is not None:
                return False
            handoff_record_id, _authored_at = insert_handoff_record(conn, session_id, handoff)
            for name in (CLEAR_ATTEMPT_VARIABLE, PENDING_HANDOFF_VARIABLE):
                candidate = variables.get(name)
                if not isinstance(candidate, dict) or candidate.get("attempt_id") != attempt_id:
                    continue
                candidate = dict(candidate)
                candidate["handoff_record_id"] = handoff_record_id
                variables[name] = candidate
            conn.execute(
                "UPDATE sessions SET handoff_markdown = %s, updated_at = %s WHERE id = %s",
                (handoff.rendered_markdown, utc_now(), session_id),
            )
            _store_variables(conn, session_id, variables, exists=row is not None)
            if not delete_undelivered_handoff(conn, old_handoff_id, session_id):
                raise RuntimeError("staged handoff became delivered while refresh held its marker")
            return True
    except Exception:
        logger.warning(
            "Failed refreshing clear attempt %s content for session %s",
            attempt_id,
            session_id,
            exc_info=True,
        )
        return False


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
            SELECT s.*, p.name AS project_name, sv.variables AS session_variables
              FROM sessions s
              LEFT JOIN projects p ON p.id = s.project_id
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
    takeover = _resolve_bound_unpulled_successor(
        db,
        source=source,
        project_id=project_id,
        machine_id=machine_id,
        terminal_context=terminal_context,
    )
    if takeover is not None:
        return takeover
    if saw_identity_mismatch:
        return ClearContinuationResolution(degrade_reason="identity_mismatch")
    if saw_expired:
        return ClearContinuationResolution(degrade_reason="expired")
    if saw_cross_project:
        return ClearContinuationResolution(degrade_reason="cross_project")
    if saw_cross_machine:
        return ClearContinuationResolution(degrade_reason="cross_machine")
    return ClearContinuationResolution()


def _resolve_bound_unpulled_successor(
    db: HubDatabase,
    *,
    source: str,
    project_id: str,
    machine_id: str,
    terminal_context: dict[str, Any] | None,
) -> ClearContinuationResolution | None:
    """Find a same-pane successor that bound a marker but never pulled the handoff.

    An operator who runs ``/clear`` by hand before ``get_handoff`` starts a third
    session on the same pane; without this the handoff and claims would expire
    with the middle row.
    """
    try:
        rows = db.fetchall(
            """
            SELECT child.*, p.name AS project_name, parent_vars.variables AS parent_variables
              FROM sessions child
              LEFT JOIN projects p ON p.id = child.project_id
              JOIN session_variables child_vars ON child_vars.session_id = child.id
              JOIN sessions parent ON parent.id = child.parent_session_id
              JOIN session_variables parent_vars ON parent_vars.session_id = parent.id
             WHERE child.source = %s
               AND child.project_id = %s
               AND child.machine_id = %s
               AND child.session_type = 'terminal'
               AND child.status = ANY(%s)
               AND child_vars.variables ? %s
               AND jsonb_typeof(parent_vars.variables -> %s) = 'object'
               AND (parent_vars.variables -> %s ->> 'consumed_by') = child.id::text
             ORDER BY child.updated_at DESC, child.id DESC
             LIMIT %s
            """,
            (
                source,
                project_id,
                machine_id,
                list(LIVE_SESSION_STATUS_ORDER),
                HANDOFF_PULL_PENDING_VARIABLE,
                CLEAR_ATTEMPT_VARIABLE,
                CLEAR_ATTEMPT_VARIABLE,
                MAX_CLEAR_CONTINUATION_CANDIDATES,
            ),
        )
    except Exception:
        logger.warning("Failed resolving clear successor takeover", exc_info=True)
        return None

    matches: list[tuple[Session, str, str]] = []
    for row in rows:
        stale = Session.from_row(row)
        if not terminal_process_contexts_match(stale.terminal_context, terminal_context):
            continue
        marker = _marker_from_variables(_load_variables(row["parent_variables"]))
        if marker is None or _marker_expired(marker):
            continue
        attempt_id = marker.get("attempt_id")
        parent_id = stale.parent_session_id
        if not isinstance(attempt_id, str) or not attempt_id or not parent_id:
            continue
        matches.append((stale, attempt_id, parent_id))
    if len(matches) > 1:
        return ClearContinuationResolution(degrade_reason="ambiguous")
    if not matches:
        return None
    stale, attempt_id, parent_id = matches[0]
    try:
        parent_row = db.fetchone(
            "SELECT *, (SELECT name FROM projects WHERE projects.id = sessions.project_id) AS project_name FROM sessions WHERE id = %s",
            (parent_id,),
        )
    except Exception:
        logger.warning("Failed loading clear predecessor %s for takeover", parent_id, exc_info=True)
        return None
    if parent_row is None:
        return None
    return ClearContinuationResolution(
        predecessor=Session.from_row(parent_row),
        attempt_id=attempt_id,
        supersedes=stale.id,
    )


def take_clear_handoff_marker(
    db: HubDatabase,
    predecessor_id: str,
    *,
    attempt_id: str,
    successor_id: str,
    supersede_successor_id: str | None = None,
) -> bool:
    """Atomically consume the marker and write successor parentage.

    With ``supersede_successor_id`` the marker may already be consumed by that
    stale successor; it is re-pointed at ``successor_id`` and the stale row's
    agent runs move along with it.
    """
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
            if supersede_successor_id is not None:
                if (
                    marker is None
                    or marker.get("attempt_id") != attempt_id
                    or marker.get("consumed_by") != supersede_successor_id
                ):
                    return False
            elif not _unconsumed_attempt(marker, attempt_id):
                return False
            handoff_record_id = marker.get("handoff_record_id")
            if not isinstance(handoff_record_id, str) or not handoff_record_id:
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
            if supersede_successor_id is None:
                insert_delivery_receipt(
                    conn,
                    handoff_id=handoff_record_id,
                    attempt_id=attempt_id,
                    boundary_kind="clear",
                    continuation_session_id=successor_id,
                )
                conn.execute(
                    "UPDATE sessions SET status = 'expired', updated_at = %s WHERE id = %s",
                    (now, predecessor_id),
                )
            conn.execute(
                "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = %s",
                (successor_id, predecessor_id),
            )
            if supersede_successor_id is not None:
                conn.execute(
                    "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = %s",
                    (successor_id, supersede_successor_id),
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


def resolve_clear_successor(db: HubDatabase, session_id: str) -> str | None:
    """Follow a terminal session's consumed clear marker to its live successor."""
    current_id = session_id
    try:
        for hop_count in range(6):
            row = db.fetchone(
                """
                SELECT s.status, sv.variables
                  FROM sessions s
              LEFT JOIN projects p ON p.id = s.project_id
                  LEFT JOIN session_variables sv ON sv.session_id = s.id
                 WHERE s.id = %s
                """,
                (current_id,),
            )
            if row is None:
                return None
            if row["status"] not in TERMINAL_SESSION_STATUSES:
                return current_id
            if hop_count == 5:
                return None
            marker = _marker_from_variables(_load_variables(row["variables"]))
            consumed_by = marker.get("consumed_by") if marker is not None else None
            if not isinstance(consumed_by, str) or not consumed_by or consumed_by == current_id:
                return None
            current_id = consumed_by
    except Exception:
        logger.warning(
            "Failed resolving clear successor for session %s",
            session_id,
            exc_info=True,
        )
    return None


def clear_failed_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    attempt_state: HandoffAttemptState | None = None,
    marker_updates: Mapping[str, Any] | None = None,
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
            handoff_record_id = marker.get("handoff_record_id")
            if not isinstance(handoff_record_id, str) or not handoff_record_id:
                return False
            prior_status = marker.get("prior_status")
            attempt_state = HandoffAttemptState(
                session_id=session_id,
                attempt_id=attempt_id,
                handoff_record_id=handoff_record_id,
                prior_handoff_markdown=marker.get("prior_handoff_markdown"),
                prior_markers={},
                missing_markers=frozenset({CLEAR_ATTEMPT_VARIABLE, PENDING_HANDOFF_VARIABLE}),
                prior_status=prior_status if isinstance(prior_status, str) else None,
            )
        return restore_handoff_attempt(db, attempt_state, marker_updates=marker_updates)
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
        "SELECT *, (SELECT name FROM projects WHERE projects.id = sessions.project_id) AS project_name FROM sessions WHERE id = %s FOR UPDATE",
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
    handoff_record_id = marker.get("handoff_record_id")
    if not isinstance(handoff_record_id, str) or not handoff_record_id:
        raise _ClearCommitAborted("clear attempt handoff record is missing")

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
    insert_delivery_receipt(
        conn,
        handoff_id=handoff_record_id,
        attempt_id=attempt_id,
        boundary_kind="clear",
        continuation_session_id=successor_id,
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
    conn.execute(
        "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = %s",
        (successor_id, predecessor_id),
    )
    succ_row = conn.execute(
        "SELECT *, (SELECT name FROM projects WHERE projects.id = sessions.project_id) AS project_name FROM sessions WHERE id = %s",
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
