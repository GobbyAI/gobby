"""Terminal runtime ownership checks for session-start handling."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Any

import psycopg

from gobby.sessions.handoff_identity import terminal_contexts_match

STALE_TERMINAL_SESSION_SCAN_LIMIT = 200
TMUX_COMMAND_TIMEOUT_SECONDS = 1.0
EXACT_PANE_OWNER_COMMAND_SOURCES = {
    "droid": "droid",
    "grok": "grok",
    "qwen": "qwen",
    "agy": "agy",
    "claude": "claude",
}


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def _source_for_pane_command(command: str) -> str | None:
    normalized = command.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("codex"):
        return "codex"
    return EXACT_PANE_OWNER_COMMAND_SOURCES.get(normalized)


def _tmux_pane_current_command(terminal_context: dict[str, Any] | None) -> str | None:
    if not terminal_context:
        return None
    pane_id = terminal_context.get("tmux_pane")
    if not isinstance(pane_id, str) or not pane_id:
        return None

    command = ["tmux"]
    socket_path = terminal_context.get("tmux_socket_path")
    if isinstance(socket_path, str) and socket_path:
        command.extend(["-S", socket_path])
    command.extend(["display-message", "-p", "-t", pane_id, "#{pane_current_command}"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=TMUX_COMMAND_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    pane_command = result.stdout.strip()
    return pane_command or None


def session_start_is_nested_cli_child(
    cli_source: str,
    terminal_context: dict[str, Any] | None,
) -> bool:
    pane_command = _tmux_pane_current_command(terminal_context)
    if pane_command is None:
        return False
    owner_source = _source_for_pane_command(pane_command)
    return owner_source is not None and owner_source != cli_source


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
            AND session_type = %s
            AND status IN (%s, %s)
            AND id <> %s
            AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (
                project_id,
                "terminal",
                "active",
                "paused",
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
            if not terminal_contexts_match(terminal_context, stored_context):
                continue
        except Exception as e:  # noqa: BLE001 - stale scan should fail open
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
        except Exception as e:  # noqa: BLE001 - stale expiry should fail open
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
