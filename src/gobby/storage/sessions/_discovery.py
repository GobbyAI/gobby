"""Discovery/query mixin for session storage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session
from gobby.storage.sql_dialect import newer_than_now_expr

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


_TERMINAL_CONTEXT_FILTER_FIELDS = (
    "tmux_pane",
    "tmux_socket_path",
    "tmux_session",
    "tty",
    "term_session_id",
)


def _parse_terminal_context_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_context_parent_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _non_empty_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _terminal_context_match_score(
    requested_context: dict[str, Any],
    stored_context: dict[str, Any],
) -> int | None:
    score = 0
    for field_name in _TERMINAL_CONTEXT_FILTER_FIELDS:
        requested_value = _non_empty_text(requested_context.get(field_name))
        stored_value = _non_empty_text(stored_context.get(field_name))
        if not requested_value or not stored_value:
            continue
        if requested_value != stored_value:
            return None
        score += 1
    return score


class _ManagerState(Protocol):
    db: HubDatabase


class _DiscoveryMixin:
    def find_by_external_id(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> Session | None:
        """
        Find session by external_id, machine_id, project_id, and source.

        This is the primary lookup for reconnecting to an existing session
        after daemon restart. The external_id (e.g., Claude Code's session ID)
        is stable within a session.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            project_id: Project identifier
            source: CLI source (claude, gemini, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Session if found, None otherwise.
        """
        query = """
            SELECT * FROM sessions
            WHERE external_id = %s
              AND machine_id = %s
              AND ((project_id = %s) OR (project_id IS NULL AND %s::text IS NULL))
              AND source = %s
        """
        params: list[str | None] = [external_id, machine_id, project_id, project_id, source]
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_active_by_external_id(
        self: _ManagerState,
        external_id: str,
        source: str,
    ) -> Session | None:
        """Find an active session by external_id and source (relaxed lookup).

        Unlike find_by_external_id, this does not require machine_id or project_id,
        making it suitable for the statusline handler which only knows the session_id.

        Args:
            external_id: External session identifier (e.g., Claude Code session ID)
            source: CLI source (claude, gemini, etc.)

        Returns:
            Most recently updated matching session, or None.
        """
        row = self.db.fetchone(
            """
            SELECT * FROM sessions
            WHERE external_id = %s AND source = %s AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (external_id, source),
        )
        return Session.from_row(row) if row else None

    def find_by_external_id_any_project(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        source: str,
        session_type: str | None = None,
    ) -> Session | None:
        """Find session by external_id, machine_id, source — ignoring project_id.

        Fallback lookup for daemon restart recovery when the caller may not
        know the correct project_id.  Returns the most recently updated match.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            source: CLI source (claude, gemini, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Most recently updated matching session, or None.
        """
        query = """
            SELECT * FROM sessions
            WHERE external_id = %s
              AND machine_id = %s
              AND source = %s
        """
        params: list[str | None] = [external_id, machine_id, source]
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_by_external_id_all_sources(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> list[Session]:
        """Find all sessions sharing an external_id across sources within one project."""
        query = """
            SELECT * FROM sessions
            WHERE external_id = %s
              AND machine_id = %s
              AND ((project_id = %s) OR (project_id IS NULL AND %s::text IS NULL))
        """
        params: list[str | None] = [external_id, machine_id, project_id, project_id]
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        query += " ORDER BY created_at ASC, id ASC"

        rows = self.db.fetchall(query, tuple(params))
        return [Session.from_row(row) for row in rows]

    def find_parent(
        self: _ManagerState,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "handoff_ready",
        max_age_minutes: int = 10,
    ) -> Session | None:
        """
        Find most recent parent session with specific status.

        Args:
            machine_id: Machine identifier
            project_id: Project identifier
            source: Optional source identifier to filter by
            status: Status to filter by (default: handoff_ready)
            max_age_minutes: Only match sessions updated within this many minutes.
                Legitimate handoffs happen within seconds; stale sessions should
                not be matched. Default 10 minutes.

        Returns:
            Session object or None
        """
        updated_recent_sql = newer_than_now_expr(self.db, "updated_at", "%s", "minute")
        query = (
            "SELECT * FROM sessions WHERE machine_id = %s AND status = %s AND project_id = %s"
            f" AND {updated_recent_sql}"
        )
        params: list[Any] = [machine_id, status, project_id, max_age_minutes]

        if source:
            query += " AND source = %s"
            params.append(source)

        query += " ORDER BY updated_at DESC LIMIT 1"

        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_active_by_terminal_context(
        self: _ManagerState,
        project_id: str,
        parent_pid: Any,
        terminal_context: dict[str, Any] | str | None = None,
    ) -> Session | None:
        """Find the unique active session matching project and terminal identity."""
        normalized_parent_pid = _normalize_context_parent_pid(parent_pid)
        if not project_id or normalized_parent_pid is None:
            return None

        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE project_id = %s
            AND status = %s
            AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            """,
            (project_id, "active"),
        )

        requested_context = _parse_terminal_context_value(terminal_context)
        matches: list[tuple[int, Session]] = []
        for row in rows:
            session = Session.from_row(row)
            stored_context = session.terminal_context or {}
            stored_parent_pid = _normalize_context_parent_pid(stored_context.get("parent_pid"))
            if stored_parent_pid != normalized_parent_pid:
                continue
            match_score = _terminal_context_match_score(requested_context, stored_context)
            if match_score is None:
                continue

            matches.append((match_score, session))

        if not matches:
            return None

        best_score = max(score for score, _session in matches)
        best_matches = [session for score, session in matches if score == best_score]
        return best_matches[0] if len(best_matches) == 1 else None

    def find_children(self: _ManagerState, parent_session_id: str) -> list[Session]:
        """
        Find all child sessions of a parent.

        Args:
            parent_session_id: The parent session ID.

        Returns:
            List of child Session objects.
        """
        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE parent_session_id = %s
            ORDER BY created_at ASC
            """,
            (parent_session_id,),
        )
        return [Session.from_row(row) for row in rows]

    def is_ancestor(self: _ManagerState, ancestor_id: str, descendant_id: str) -> bool:
        """Check if ancestor_id is in the parent chain of descendant_id.

        Walks the parent_session_id chain from descendant upward.
        A session is NOT considered its own ancestor.

        Args:
            ancestor_id: Potential ancestor session ID
            descendant_id: Potential descendant session ID

        Returns:
            True if ancestor_id is found in the parent chain
        """
        current_id = descendant_id
        seen: set[str] = set()
        while True:
            row = self.db.fetchone(
                "SELECT parent_session_id FROM sessions WHERE id = %s",
                (current_id,),
            )
            if not row or row["parent_session_id"] is None:
                return False
            parent_id = row["parent_session_id"]
            if parent_id == ancestor_id:
                return True
            if parent_id in seen:
                return False
            seen.add(parent_id)
            current_id = parent_id
