"""Shared constants and bootstrap helpers for session storage."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING

from gobby.storage.hub.protocol import HubDatabase, SystemSessionBootstrap
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.session_models import Session

logger = logging.getLogger("gobby.storage.sessions")

ALLOWED_SESSION_STATUSES = frozenset(
    {
        "active",
        "paused",
        "handoff_ready",
        "completed",
        "cancelled",
        "closed",
        "expired",
        "deleted",
    }
)
LIVE_SESSION_STATUSES = frozenset({"active", "paused", "handoff_ready"})
TERMINAL_SESSION_STATUSES = frozenset({"expired", "deleted"})


def validate_session_status_transition(current_status: str | None, new_status: str) -> None:
    """Validate a session status value and prevent direct terminal-state revival."""
    if new_status not in ALLOWED_SESSION_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_SESSION_STATUSES))
        raise ValueError(f"Invalid session status {new_status!r}. Must be one of: {allowed}")
    if current_status in TERMINAL_SESSION_STATUSES and new_status != current_status:
        raise ValueError(
            f"Cannot transition terminal session status from {current_status!r} to {new_status!r}"
        )


def get_logger() -> logging.Logger:
    """Resolve package logger through the public import path for patch compatibility."""
    import gobby.storage.sessions as sessions_module

    return sessions_module.logger


SYSTEM_SESSION_PROJECT_ID = PERSONAL_PROJECT_ID
SYSTEM_SESSION_SOURCE = "system"
SYSTEM_SESSION_TITLE = "_system"
SESSION_REVIVAL_HORIZON_HOURS = 24


def system_session_id(machine_id: str | None = None) -> str:
    """Return the deterministic system-session UUID for one machine."""
    normalized_machine_id = str(uuid.UUID(machine_id or require_machine_id()))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby://system-session/{normalized_machine_id}"))


def system_session_external_id(machine_id: str | None = None) -> str:
    """Return the machine-scoped external identity for a system session."""
    normalized_machine_id = str(uuid.UUID(machine_id or require_machine_id()))
    return f"system:{normalized_machine_id}"


def past_terminal_revival_horizon(session: Session) -> bool:
    """Report whether an expired terminal session is too old to revive."""
    return (
        session.session_type == "terminal"
        and session.status == "expired"
        and session.updated_at < utc_now() - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS)
    )


def is_contestable_terminal_expiry(session: Session) -> bool:
    """Report whether this session's expiry may still be reversed by pane ownership.

    SessionStart expires every terminal session sharing a reused terminal context
    before anything validates who owns the pane, so an expired status here can
    simply be wrong. ``revive_expired_terminal_session`` settles the contest
    afterwards and routinely reverses it. Until it does, treat the owner as live:
    a session in this state is working, not dead.

    Only a session carrying a tmux pane can be in that contest, and only until the
    revival horizon passes -- which is what keeps an ordinary expiry sweepable.
    ``release_task_claim`` enforces the same rule in SQL so its release stays a
    single compare-and-set.
    """
    terminal_context = session.terminal_context
    pane = terminal_context.get("tmux_pane") if isinstance(terminal_context, Mapping) else None
    return (
        session.session_type == "terminal"
        and session.status == "expired"
        and isinstance(pane, str)
        and bool(pane.strip())
        and not past_terminal_revival_horizon(session)
    )


def ensure_system_session(db: HubDatabase) -> None:
    """Ensure the bootstrapped root session for cron/pipeline work exists."""
    machine_id = require_machine_id()
    session_id = system_session_id(machine_id)
    external_id = system_session_external_id(machine_id)
    had_existing_sessions = False
    with db.transaction_immediate(SystemSessionBootstrap()):
        existing = db.fetchone(
            "SELECT id, machine_id, external_id FROM sessions WHERE id = %s", (session_id,)
        )
        if existing is not None:
            owner_machine_id = str(existing["machine_id"])
            if owner_machine_id != machine_id:
                raise MachineOwnershipMismatchError(
                    resource_kind="system session",
                    resource_id=session_id,
                    owner_machine_id=owner_machine_id,
                    current_machine_id=machine_id,
                )
            if str(existing["external_id"]) != external_id:
                raise RuntimeError(f"System session {session_id} has an invalid external identity")
            return
        had_existing_sessions = db.fetchone("SELECT 1 FROM sessions LIMIT 1") is not None

        project = db.fetchone("SELECT id FROM projects WHERE id = %s", (SYSTEM_SESSION_PROJECT_ID,))
        if project is None:
            raise RuntimeError(
                "Missing required _personal project for system session bootstrap "
                f"({SYSTEM_SESSION_PROJECT_ID})"
            )

        db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'active', 0)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                session_id,
                external_id,
                machine_id,
                SYSTEM_SESSION_SOURCE,
                SYSTEM_SESSION_PROJECT_ID,
                SYSTEM_SESSION_TITLE,
            ),
        )

        recreated = db.fetchone(
            "SELECT id, machine_id, external_id FROM sessions WHERE id = %s", (session_id,)
        )
        if recreated is None:
            raise RuntimeError(f"Failed to recreate missing system session {session_id}")
        owner_machine_id = str(recreated["machine_id"])
        if owner_machine_id != machine_id:
            raise MachineOwnershipMismatchError(
                resource_kind="system session",
                resource_id=session_id,
                owner_machine_id=owner_machine_id,
                current_machine_id=machine_id,
            )
        if str(recreated["external_id"]) != external_id:
            raise RuntimeError(f"System session {session_id} has an invalid external identity")

    log = get_logger()
    if had_existing_sessions:
        log.warning("Recreated missing system session %s", session_id)
    else:
        log.info("Created system session %s", session_id)
