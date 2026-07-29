"""Discovery/query mixin for session storage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session
from gobby.storage.sql_dialect import newer_than_now_expr
from gobby.terminal_ownership import TerminalIdentity, terminal_session_identity

MAX_TERMINAL_SESSION_CANDIDATES = 250
MAX_HANDOFF_PARENT_CANDIDATES = 8

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
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
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


def _terminal_session_match_score(
    session: Session,
    requested_context: dict[str, Any],
    parent_pid: int | None,
) -> int | None:
    stored_context = session.terminal_context or {}
    stored_parent_pid = _normalize_context_parent_pid(stored_context.get("parent_pid"))
    pid_match = parent_pid is not None and stored_parent_pid == parent_pid
    match_score = _terminal_context_match_score(requested_context, stored_context)
    if match_score is None or (not pid_match and match_score == 0):
        return None
    return match_score + 100 if pid_match else match_score


def _unique_best_match(matches: list[tuple[int, Session]]) -> Session | None:
    best_score = max(score for score, _session in matches)
    best_matches = [session for score, session in matches if score == best_score]
    return best_matches[0] if len(best_matches) == 1 else None


def _handoff_candidate_matches(session: Session, requested_context: dict[str, Any]) -> bool:
    """Return whether a handoff candidate matches the child's terminal identity."""
    if requested_context.get("gobby_session_id") == session.id:
        return True
    score = _terminal_context_match_score(requested_context, session.terminal_context or {})
    return score is not None and score > 0


class _ManagerState(Protocol):
    db: HubDatabase


class _DiscoveryMixin:
    def find_by_terminal_identity(
        self: _ManagerState,
        identity: TerminalIdentity,
    ) -> list[Session]:
        """Find every session sharing a global machine/socket/pane identity."""
        machine_id, _socket_identity, pane = identity
        rows = self.db.fetchall(
            """
            SELECT *
            FROM sessions
            WHERE machine_id = %s
              AND session_type = 'terminal'
              AND terminal_context ->> 'tmux_pane' = %s
            ORDER BY created_at, id
            """,
            (machine_id, pane),
        )
        return [
            session
            for row in rows
            if terminal_session_identity(session := Session.from_row(row)) == identity
        ]

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

        This is the primary lookup for reconnecting to an existing session after daemon
        restart. The external_id (e.g., Claude Code's session ID) is stable within a
        session.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            project_id: Project identifier
            source: CLI source (claude, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Session if found, None otherwise.
        """
        storage_project_id = project_id or PERSONAL_PROJECT_ID
        query = """
            SELECT * FROM sessions
            WHERE external_id = %s
              AND machine_id = %s
              AND project_id = %s
              AND source = %s
        """
        params: list[str | None] = [external_id, machine_id, storage_project_id, source]
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
            source: CLI source (claude, qwen, etc.)

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

        Fallback lookup for daemon restart recovery when the caller may not know the
        correct project_id. Returns the most recently updated match.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            source: CLI source (claude, qwen, codex, droid)
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
        """Find all sessions sharing an external_id across sources."""
        query = """
            SELECT * FROM sessions
            WHERE external_id = %s
              AND machine_id = %s
        """
        params: list[str | None] = [external_id, machine_id]
        if project_id is not None:
            query += " AND project_id = %s"
            params.append(project_id)
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
        terminal_context: dict[str, Any] | str | None = None,
        candidate_limit: int = 1,
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
            terminal_context: Child terminal identity used to select the matching parent.
            candidate_limit: Number of newest candidates to scan, bounded at eight.

        Returns:
            Session object or None
        """
        updated_recent_sql = newer_than_now_expr(self.db, "updated_at", "%s", "minute")
        # newer_than_now_expr returns a trusted static fragment for the active SQL dialect.
        query = (
            "SELECT * FROM sessions WHERE machine_id = %s AND status = %s AND project_id = %s"  # nosec B608
            f" AND {updated_recent_sql}"
        )
        params: list[Any] = [machine_id, status, project_id, max_age_minutes]

        if source:
            query += " AND source = %s"
            params.append(source)

        bounded_limit = max(1, min(candidate_limit, MAX_HANDOFF_PARENT_CANDIDATES))
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(bounded_limit)

        rows = self.db.fetchall(query, tuple(params))
        candidates = [Session.from_row(row) for row in rows]
        if not candidates:
            return None

        requested_context = _parse_terminal_context_value(terminal_context)
        if not requested_context:
            return candidates[0] if len(candidates) == 1 else None

        return next(
            (
                candidate
                for candidate in candidates
                if _handoff_candidate_matches(candidate, requested_context)
            ),
            None,
        )

    def find_active_by_terminal_context(
        self: _ManagerState,
        project_id: str | None,
        parent_pid: Any,
        terminal_context: dict[str, Any] | str | None = None,
    ) -> Session | None:
        """Find the unique active session matching project and terminal identity."""
        normalized_parent_pid = _normalize_context_parent_pid(parent_pid)
        normalized_project_id = project_id.strip() if isinstance(project_id, str) else None
        if not normalized_project_id or normalized_parent_pid is None:
            return None

        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE project_id = %s
            AND status = %s
            AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (normalized_project_id, "active", MAX_TERMINAL_SESSION_CANDIDATES),
        )

        requested_context = _parse_terminal_context_value(terminal_context)
        matches: list[tuple[int, Session]] = []
        for row in rows:
            session = Session.from_row(row)
            match_score = _terminal_session_match_score(
                session,
                requested_context,
                normalized_parent_pid,
            )
            if match_score is None:
                continue
            matches.append((match_score, session))

        if not matches:
            return None

        return _unique_best_match(matches)

    def resolve_current_terminal_session(
        self: _ManagerState,
        project_id: str | None,
        parent_pid: Any,
        terminal_context: dict[str, Any] | str | None,
    ) -> Session | None:
        """Resolve the current terminal session from project-scoped ambient identity."""
        normalized_project_id = project_id.strip() if isinstance(project_id, str) else None
        normalized_parent_pid = _normalize_context_parent_pid(parent_pid)
        requested_context = _parse_terminal_context_value(terminal_context)
        if not normalized_project_id or (normalized_parent_pid is None and not requested_context):
            return None

        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE project_id = %s
              AND session_type = %s
              AND status IN (%s, %s, %s)
              AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (
                normalized_project_id,
                "terminal",
                "active",
                "paused",
                "handoff_ready",
                MAX_TERMINAL_SESSION_CANDIDATES,
            ),
        )

        active_matches: list[tuple[int, Session]] = []
        fallback_matches: list[tuple[int, Session]] = []
        for row in rows:
            session = Session.from_row(row)
            match_score = _terminal_session_match_score(
                session,
                requested_context,
                normalized_parent_pid,
            )
            if match_score is None:
                continue
            matches = active_matches if session.status == "active" else fallback_matches
            matches.append((match_score, session))

        if active_matches:
            return _unique_best_match(active_matches)
        return _unique_best_match(fallback_matches) if fallback_matches else None

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
