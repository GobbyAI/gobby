"""Discovery/query mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session

from ._constants import LIVE_SESSION_STATUS_ORDER
from ._discovery_helpers import (
    normalize_context_parent_pid,
    parse_terminal_context_value,
    terminal_session_match_score,
    unique_best_match,
)
from ._identity_reconciliation import AmbiguousSessionIdentityError
from ._lineage_discovery import _LineageDiscoveryMixin

MAX_TERMINAL_SESSION_CANDIDATES = 250

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase


class _DiscoveryMixin(_LineageDiscoveryMixin):
    def find_by_external_id(
        self: _ManagerState,
        external_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None:
        """
        Find a session by its canonical provider identity.

        This is the primary lookup for reconnecting to an existing session after daemon
        restart. The external_id (e.g., Claude Code's session ID) is stable within a
        session.

        Args:
            external_id: External session identifier
            project_id: Project identifier
            source: CLI source (claude, codex, droid, grok, agy)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Session if found, None otherwise.
        """
        storage_project_id = project_id or PERSONAL_PROJECT_ID
        query = """
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE external_id = %s
              AND project_id = %s
              AND source = %s
        """
        params: list[str | None] = [external_id, storage_project_id, source]
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        query += " ORDER BY created_at, id LIMIT 2"
        rows = self.db.fetchall(query, tuple(params))
        if len(rows) > 1:
            raise AmbiguousSessionIdentityError(
                "Multiple sessions share canonical identity "
                f"{external_id!r}/{source!r}/{storage_project_id!r}/{session_type!r}"
            )
        return Session.from_row(rows[0]) if rows else None

    def find_active_by_external_id(
        self: _ManagerState,
        external_id: str,
        source: str,
        session_type: str = "terminal",
    ) -> Session | None:
        """Find an active session by external_id and source (relaxed lookup).

        Unlike find_by_external_id, this does not require project_id,
        making it suitable for the statusline handler which only knows the session_id.

        Args:
            external_id: External session identifier (e.g., Claude Code session ID)
            source: CLI source (claude, codex, etc.)

        Returns:
            Most recently updated matching session, or None.
        """
        status_placeholders = ",".join("%s" for _ in LIVE_SESSION_STATUS_ORDER)
        row = self.db.fetchone(
            f"""
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE external_id = %s AND source = %s AND session_type = %s
              AND status IN ({status_placeholders})
            ORDER BY updated_at DESC
            LIMIT 1
            """,  # nosec B608 # placeholders come from a fixed local constant.
            (external_id, source, session_type, *LIVE_SESSION_STATUS_ORDER),
        )
        return Session.from_row(row) if row else None

    def find_by_external_id_any_project(
        self: _ManagerState,
        external_id: str,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None:
        """Find session by external_id and source, ignoring project_id.

        Fallback lookup for daemon restart recovery when the caller may not know the
        correct project_id. Returns the most recently updated match.

        Args:
            external_id: External session identifier
            source: CLI source (claude, codex, droid, grok, agy)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Most recently updated matching session, or None.
        """
        query = """
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE external_id = %s
              AND source = %s
        """
        params: list[str | None] = [external_id, source]
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_by_external_id_all_sources(
        self: _ManagerState,
        external_id: str,
        project_id: str | None,
        session_type: str | None = "terminal",
    ) -> list[Session]:
        """Find all sessions sharing an external_id across sources."""
        query = """
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE external_id = %s
        """
        params: list[str | None] = [external_id]
        if project_id is not None:
            query += " AND project_id = %s"
            params.append(project_id)
        if session_type is not None:
            query += " AND session_type = %s"
            params.append(session_type)
        query += " ORDER BY created_at ASC, id ASC"

        rows = self.db.fetchall(query, tuple(params))
        return [Session.from_row(row) for row in rows]

    def find_active_by_terminal_context(
        self: _ManagerState,
        project_id: str | None,
        parent_pid: Any,
        terminal_context: dict[str, Any] | str | None = None,
    ) -> Session | None:
        """Find the unique active session matching project and terminal identity."""
        normalized_parent_pid = normalize_context_parent_pid(parent_pid)
        normalized_project_id = project_id.strip() if isinstance(project_id, str) else None
        if not normalized_project_id or normalized_parent_pid is None:
            return None

        rows = self.db.fetchall(
            """
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE project_id = %s
            AND status = %s
            AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (normalized_project_id, "active", MAX_TERMINAL_SESSION_CANDIDATES),
        )

        requested_context = parse_terminal_context_value(terminal_context)
        matches: list[tuple[int, Session]] = []
        for row in rows:
            session = Session.from_row(row)
            match_score = terminal_session_match_score(
                session,
                requested_context,
                normalized_parent_pid,
            )
            if match_score is None:
                continue
            matches.append((match_score, session))

        if not matches:
            return None

        return unique_best_match(matches)

    def resolve_current_terminal_session(
        self: _ManagerState,
        project_id: str | None,
        parent_pid: Any,
        terminal_context: dict[str, Any] | str | None,
    ) -> Session | None:
        """Resolve the current terminal session from project-scoped ambient identity."""
        normalized_project_id = project_id.strip() if isinstance(project_id, str) else None
        normalized_parent_pid = normalize_context_parent_pid(parent_pid)
        requested_context = parse_terminal_context_value(terminal_context)
        if not normalized_project_id or (normalized_parent_pid is None and not requested_context):
            return None

        status_placeholders = ",".join("%s" for _ in LIVE_SESSION_STATUS_ORDER)
        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
            WHERE project_id = %s
              AND session_type = %s
              AND status IN ({status_placeholders})
              AND terminal_context IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,  # nosec B608 # placeholders come from a fixed local constant.
            (
                normalized_project_id,
                "terminal",
                *LIVE_SESSION_STATUS_ORDER,
                MAX_TERMINAL_SESSION_CANDIDATES,
            ),
        )

        active_matches: list[tuple[int, Session]] = []
        fallback_matches: list[tuple[int, Session]] = []
        for row in rows:
            session = Session.from_row(row)
            match_score = terminal_session_match_score(
                session,
                requested_context,
                normalized_parent_pid,
            )
            if match_score is None:
                continue
            matches = active_matches if session.status == "active" else fallback_matches
            matches.append((match_score, session))

        if active_matches:
            return unique_best_match(active_matches)
        return unique_best_match(fallback_matches) if fallback_matches else None
