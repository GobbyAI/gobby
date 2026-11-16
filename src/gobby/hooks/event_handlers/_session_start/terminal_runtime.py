"""Terminal runtime ownership checks for session-start handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg

from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.terminal_binding import cli_shares_shell_tty
from gobby.sessions.tmux_context import parse_terminal_context_value

STALE_TERMINAL_SESSION_SCAN_LIMIT = 200
NativeTerminalBind = tuple[str, dict[str, Any]]


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def discover_and_bind_external_terminal(
    handler: Any,
    *,
    session_id: str | None,
    project_id: str | None,
    terminal_context: dict[str, Any] | None,
) -> NativeTerminalBind | None:
    """Bind the native host row using the hook process identity."""
    terminal_manager = getattr(handler, "terminal_manager", None)
    if terminal_manager is None:
        return None
    return bind_discovered_terminal(
        terminal_manager,
        handler.logger,
        session_id=session_id,
        project_id=project_id,
        terminal_context=terminal_context,
        session_manager=handler._session_manager,
    )


def bind_discovered_terminal(
    terminal_manager: Any,
    logger: Any,
    *,
    session_id: str | None,
    project_id: str | None,
    terminal_context: dict[str, Any] | None,
    session_manager: Any,
) -> NativeTerminalBind | None:
    """Bind a native terminal only after verifying the CLI's actual tty.

    Returns the native row still held by another owner, for a retry once it is freed.
    """
    if not session_id or not project_id or not terminal_context:
        return None

    native_terminal_id = terminal_context.get("gobby_terminal_id")
    if not isinstance(native_terminal_id, str) or not native_terminal_id:
        return None
    cli_pid = terminal_context.get("parent_pid")
    terminal = terminal_manager.get(native_terminal_id)
    if (
        isinstance(cli_pid, bool)
        or not isinstance(cli_pid, int)
        or cli_pid <= 0
        or terminal is None
        or not cli_shares_shell_tty(cli_pid, terminal)
    ):
        logger.info(
            "native terminal %s refused for session %s: CLI and shell tty could not be verified",
            native_terminal_id,
            session_id,
        )
        session_manager.clear_gobby_terminal_id(session_id)
        if terminal is not None and terminal.session_id == session_id:
            terminal_manager.release_session(native_terminal_id, session_id)
        terminal_context.pop("gobby_terminal_id", None)
        return None
    if terminal_manager.bind_session(native_terminal_id, session_id, project_id) is None:
        return project_id, terminal_context
    return None


def retry_native_terminal_bind(
    handler: Any,
    *,
    session_id: str,
    pending_bind: NativeTerminalBind | None,
) -> None:
    """Retry a native bind after stale same-context session owners expire."""
    terminal_manager = getattr(handler, "terminal_manager", None)
    if pending_bind is None or terminal_manager is None:
        return
    project_id, terminal_context = pending_bind
    pending = bind_discovered_terminal(
        terminal_manager,
        handler.logger,
        session_id=session_id,
        project_id=project_id,
        terminal_context=terminal_context,
        session_manager=handler._session_manager,
    )
    if pending is not None:
        handler.logger.info("native terminal not bound to session %s", session_id)


def _terminal_context_reused(
    current: Mapping[str, Any], stored: Mapping[str, Any] | str | None
) -> bool:
    """Recognize a reused pane or tty, including after its CLI process restarts."""
    previous = parse_terminal_context_value(stored)
    if not previous:
        return False
    terminal_id = current.get("gobby_terminal_id")
    if terminal_id and terminal_id == previous.get("gobby_terminal_id"):
        return True
    tty = current.get("tty")
    if tty and tty == previous.get("tty"):
        return True
    return terminal_process_contexts_match(current, previous)


def expire_stale_terminal_sessions_for_context(
    handler: Any,
    *,
    session_id: str | None,
    project_id: str | None,
    terminal_context: dict[str, Any] | None,
) -> None:
    if not session_id or not project_id or not terminal_context or not handler._session_manager:
        return

    db = getattr(handler._session_manager, "db", None)
    if db is None or not hasattr(db, "fetchall"):
        return

    try:
        rows = db.fetchall(
            """
            SELECT id, source, terminal_context FROM sessions
            WHERE project_id = %s
            AND machine_id = (SELECT machine_id FROM sessions WHERE id = %s)
            AND session_type = %s
            AND status IN (%s, %s, %s, %s)
            AND id <> %s
            AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (
                project_id,
                session_id,
                "terminal",
                "active",
                "paused",
                "waiting",
                "idle",
                session_id,
                STALE_TERMINAL_SESSION_SCAN_LIMIT,
            ),
        )
    except psycopg.Error as e:
        handler.logger.warning(
            "Failed to scan stale terminal sessions",
            extra={
                "session_id": session_id,
                "project_id": project_id,
                "error_type": type(e).__name__,
                "error": str(e),
            },
            exc_info=True,
        )
        return

    expired_session_ids: list[str] = []
    for row in rows:
        candidate_id = _row_value(row, "id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        stored_context = _row_value(row, "terminal_context")
        try:
            if not _terminal_context_reused(terminal_context, stored_context):
                continue
        except Exception as e:  # Stale context scans are an owned fail-open boundary.
            handler.logger.warning(
                "Failed to compare stale terminal session context",
                extra={
                    "session_id": session_id,
                    "project_id": project_id,
                    "candidate_session_id": candidate_id,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
                exc_info=True,
            )
            continue
        try:
            expired = handler._session_manager.mark_session_expired(
                candidate_id,
                cause="context_reuse",
            )
        except Exception as e:  # One failed expiry must not block the remaining candidates.
            handler.logger.warning(
                "Failed to expire stale terminal session",
                extra={
                    "session_id": session_id,
                    "project_id": project_id,
                    "candidate_session_id": candidate_id,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
                exc_info=True,
            )
            continue
        if expired:
            expired_session_ids.append(candidate_id)

    if expired_session_ids:
        handler.logger.info(
            "Expired stale terminal sessions for reused terminal context",
            extra={
                "session_id": session_id,
                "expired_count": len(expired_session_ids),
                "expired_session_ids": expired_session_ids[:10],
            },
        )
