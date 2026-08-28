"""Discovery/query mixin for session storage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session
from gobby.storage.sql_dialect import newer_than_now_expr
from gobby.terminal_ownership import (
    TERMINAL_OWNER_STATUSES,
    TerminalIdentity,
    is_interactive_terminal_claim,
    terminal_session_creation_order,
    terminal_session_identity,
)

from ._discovery_helpers import (
    handoff_candidate_matches,
    normalize_context_parent_pid,
    parse_terminal_context_value,
    terminal_session_match_score,
    unique_best_match,
)
from ._identity_reconciliation import AmbiguousSessionIdentityError

MAX_TERMINAL_SESSION_CANDIDATES = 250
MAX_HANDOFF_PARENT_CANDIDATES = 8

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def find_by_terminal_identity(self, identity: TerminalIdentity) -> list[Session]: ...


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
              AND status = ANY(%s)
            ORDER BY created_at, id
            """,
            (machine_id, pane, list(TERMINAL_OWNER_STATUSES)),
        )
        return [
            session
            for row in rows
            if terminal_session_identity(session := Session.from_row(row)) == identity
        ]

    def find_live_interactive_pane_owner(
        self: _ManagerState,
        terminal_context: dict[str, Any] | None,
        machine_id: str | None,
    ) -> Session | None:
        """Return the oldest live interactive owner of this tmux identity."""
        if not isinstance(terminal_context, dict) or not machine_id:
            return None
        identity = terminal_session_identity(
            SimpleNamespace(machine_id=machine_id, terminal_context=terminal_context)
        )
        if identity is None:
            return None
        live: list[Session] = [
            session
            for session in self.find_by_terminal_identity(identity)
            if session.status in {"active", "paused"} and is_interactive_terminal_claim(session)
        ]
        if not live:
            return None
        return min(live, key=terminal_session_creation_order)

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
            source: CLI source (claude, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Session if found, None otherwise.
        """
        storage_project_id = project_id or PERSONAL_PROJECT_ID
        query = """
            SELECT * FROM sessions
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
            source: CLI source (claude, qwen, etc.)

        Returns:
            Most recently updated matching session, or None.
        """
        row = self.db.fetchone(
            """
            SELECT * FROM sessions
            WHERE external_id = %s AND source = %s AND session_type = %s
              AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (external_id, source, session_type),
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
            source: CLI source (claude, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Most recently updated matching session, or None.
        """
        query = """
            SELECT * FROM sessions
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
            SELECT * FROM sessions
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

        requested_context = parse_terminal_context_value(terminal_context)
        if not requested_context:
            return candidates[0] if len(candidates) == 1 else None

        return next(
            (
                candidate
                for candidate in candidates
                if handoff_candidate_matches(candidate, requested_context)
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
        normalized_parent_pid = normalize_context_parent_pid(parent_pid)
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
