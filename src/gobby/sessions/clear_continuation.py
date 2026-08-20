"""Attempt lifecycle, successor resolution, and seeding for clear_self."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard

from gobby.hooks.context_limits import handoff_summary_inject_budget_for
from gobby.sessions.compact_continuation import (
    _format_timestamp,
    _load_variables,
    _parse_timestamp,
    _row_variables,
    schedule_compact_self_continuation,
)
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.storage.hub.protocol import HubDatabase, SessionLineageMutation, SessionVariableMutation
from gobby.storage.session_models import Session
from gobby.storage.sessions._lineage_guard import sanitize_parent_session_id
from gobby.utils.datetime import utc_now
from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN, strip_injected_context
from gobby.workflows.state_manager import SessionVariableManager

__all__ = [
    "CLEAR_ATTEMPT_VARIABLE",
    "CLEAR_HANDOFF_TTL_SECONDS",
    "ClearContinuationResolution",
    "build_clear_self_continue_prompt",
    "clear_failed_attempt",
    "resolve_clear_continuation",
    "schedule_clear_self_continuation",
    "seed_clear_handoff_variables",
    "stage_clear_attempt",
    "take_clear_handoff_marker",
]

logger = logging.getLogger(__name__)

CLEAR_HANDOFF_TTL_SECONDS = 600
CLEAR_ATTEMPT_VARIABLE = "clear_attempt"
MAX_CLEAR_CONTINUATION_CANDIDATES = 250

_SUMMARY_FIELDS = (
    "summary_markdown",
    "summary_path",
    "summary_revision_id",
    "summary_source_context_hash",
    "summary_digest_turn_count",
    "summary_generation_mode",
    "summary_generated_at",
)


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
    terminal_context: dict[str, Any] | None,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Write the one-shot clear-attempt marker on the predecessor row.

    Returns the prior summary state captured for failure restoration.
    ``handoff_ready`` status is never set.
    """
    prior = _capture_summary_state(db, session_id)
    marker = {
        "attempt_id": attempt_id,
        "created_at": _format_timestamp(datetime.now(UTC)),
        "terminal_context": terminal_context,
        "chat": _chat_payload(chat_context),
        "consumed_by": None,
    }
    now = utc_now().isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        variables[CLEAR_ATTEMPT_VARIABLE] = marker
        payload = json.dumps(variables)
        if row:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (payload, now, session_id),
            )
        else:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, payload, now),
            )
    return prior


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
             ORDER BY s.created_at DESC, s.id DESC
             LIMIT %s
            """,
            (source, CLEAR_ATTEMPT_VARIABLE, MAX_CLEAR_CONTINUATION_CANDIDATES),
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
    prior_summary_state: dict[str, Any],
) -> bool:
    """Compare-and-clear an unconsumed marker and restore prior summary state."""
    try:
        with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            variables = _load_variables(_row_variables(row))
            marker = _marker_from_variables(variables)
            if not _unconsumed_attempt(marker, attempt_id):
                return False
            variables.pop(CLEAR_ATTEMPT_VARIABLE, None)
            now = utc_now()
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (json.dumps(variables), now.isoformat(), session_id),
            )
            conn.execute(
                """
                UPDATE sessions
                   SET summary_markdown = %s,
                       summary_path = %s,
                       summary_revision_id = %s,
                       summary_source_context_hash = %s,
                       summary_digest_turn_count = %s,
                       summary_generation_mode = %s,
                       summary_generated_at = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (
                    prior_summary_state.get("summary_markdown"),
                    prior_summary_state.get("summary_path"),
                    prior_summary_state.get("summary_revision_id"),
                    prior_summary_state.get("summary_source_context_hash"),
                    prior_summary_state.get("summary_digest_turn_count"),
                    prior_summary_state.get("summary_generation_mode"),
                    prior_summary_state.get("summary_generated_at"),
                    now,
                    session_id,
                ),
            )
            return True
    except Exception:
        logger.warning(
            "Failed clearing clear_self attempt %s for session %s",
            attempt_id,
            session_id,
            exc_info=True,
        )
        return False


def seed_clear_handoff_variables(
    session_manager: Any,
    successor_session_id: str,
    predecessor: Any,
) -> None:
    """Seed successor injection variables after a successful atomic take."""
    summary = strip_injected_context(getattr(predecessor, "summary_markdown", None) or "")
    SessionVariableManager(session_manager.db).merge_variables(
        successor_session_id,
        {
            "handoff_summary_injectable": _bound_clear_handoff_summary(summary, predecessor),
            "clear_handoff_inject_pending": True,
        },
    )


def build_clear_self_continue_prompt(*, predecessor_ref: str) -> str:
    """Continuation prompt sent to the successor terminal after /clear lands."""
    return (
        "Continue where you last left off. The previous turn called "
        "`gobby-sessions:clear_self`. If startup context contains "
        f"`{INJECTED_CONTEXT_BEGIN}`, use that injected context directly and continue. "
        "Only if the injected context is missing or incomplete, call "
        f'`gobby-sessions.get_handoff_context(session_id="{predecessor_ref}")` '
        "and continue."
    )


def schedule_clear_self_continuation(
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
    return schedule_compact_self_continuation(session, prompt, **kwargs)


def _capture_summary_state(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        """
        SELECT summary_markdown, summary_path, summary_revision_id,
               summary_source_context_hash, summary_digest_turn_count,
               summary_generation_mode, summary_generated_at
          FROM sessions
         WHERE id = %s
        """,
        (session_id,),
    )
    if row is None:
        return {}
    prior: dict[str, Any] = {}
    for field in _SUMMARY_FIELDS:
        value = row[field]
        prior[field] = value.isoformat() if isinstance(value, datetime) else value
    return prior


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


def _bound_clear_handoff_summary(summary: str, session: Any) -> str:
    budget = handoff_summary_inject_budget_for(getattr(session, "source", None))
    if len(summary) <= budget:
        return summary
    seq_num = getattr(session, "seq_num", None)
    ref = f"#{seq_num}" if seq_num else (getattr(session, "id", "") or "")
    ref_clause = f' with session ref "{ref}"' if ref else ""
    breadcrumb = (
        f"Clear handoff is {len(summary)} chars and exceeds the inline "
        "handoff budget. Call get_handoff_context (gobby-sessions)"
        f"{ref_clause} to load the full handoff."
    )
    if len(breadcrumb) <= budget:
        return breadcrumb
    return breadcrumb[:budget]
