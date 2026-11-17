"""Seed historical clear state without invoking a clear-continuation runtime."""

import json
from typing import Any

from gobby.sessions.handoff import (
    HANDOFF_PULL_PENDING_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    HandoffAttemptState,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import HandoffPayload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager


def seed_legacy_clear_handoff(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    handoff: HandoffPayload,
    terminal_context: dict[str, Any] | None = None,
    chat_context: dict[str, Any] | None = None,
) -> HandoffAttemptState:
    """Persist the obsolete marker shape for retirement and historical-read tests."""
    state = stage_handoff_attempt(db, session_id, attempt_id=attempt_id, handoff=handoff)
    variables = SessionVariableManager(db).get_variables(session_id)
    pending = {**variables[PENDING_HANDOFF_VARIABLE], "clear_session": True}
    clear_marker = {
        **pending,
        "consumed_by": None,
        "terminal_context": terminal_context,
        "chat_context": chat_context,
    }
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_variables SET variables = (variables - %s) || %s::jsonb "
            "WHERE session_id = %s",
            (
                HANDOFF_PULL_PENDING_VARIABLE,
                json.dumps({PENDING_HANDOFF_VARIABLE: pending, "clear_attempt": clear_marker}),
                session_id,
            ),
        )
    SessionManager(db).update_status(session_id, "awaiting_handoff")
    return state
