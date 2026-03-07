"""Tests for session API routes.

Exercises src/gobby/servers/routes/sessions.py endpoints and helper functions
using mock-based TestClient approach.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.sessions import (
    _get_commit_count,
    _get_session_stats,
    create_sessions_router,
)

pytestmark = pytest.mark.unit

NOW_ISO = "2026-02-10T12:00:00+00:00"


def _make_session(**overrides) -> MagicMock:
    """Create a mock Session with sensible defaults."""
    defaults = {
        "id": "sess-abc123",
        "external_id": "ext-123",
        "machine_id": "machine-1",
        "source": "Claude Code",
        "project_id": "proj-123",
        "title": "Test Session",
        "status": "active",
        "jsonl_path": "/tmp/test.jsonl",
        "summary_path": None,
        "summary_markdown": None,
        "compact_markdown": None,
        "git_branch": "main",
        "parent_session_id": None,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "seq_num": 42,
    }
    defaults.update(overrides)
    session = MagicMock()
    for key, val in defaults.items():
        setattr(session, key, val)
    session.to_dict.return_value = defaults
    return session


def _make_stop_signal(**overrides) -> MagicMock:
    """Create a mock StopSignal matching what the route code expects."""
    now = datetime.now(UTC)
    defaults = {
        "signal_id": "sig-abc123",
        "session_id": "sess-abc123",
        "reason": "External stop request",
        "source": "http_api",
        "signaled_at": now,
        "acknowledged": False,
        "acknowledged_at": None,
    }
    defaults.update(overrides)
    signal = MagicMock()
    for key, val in defaults.items():
        setattr(signal, key, val)
    return signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_server():
    """Create mock HTTPServer with session-related managers."""
    server = MagicMock()
    server.session_manager = MagicMock()
    server.session_manager.db = MagicMock()
    server.message_manager = AsyncMock()
    server.llm_service = MagicMock()
    server.resolve_project_id = MagicMock(return_value="proj-123")
    return server


@pytest.fixture
def mock_hook_manager():
    """Create a mock HookManager with _stop_registry."""
    hook_manager = MagicMock()
    hook_manager._stop_registry = MagicMock()
    return hook_manager


@pytest.fixture
def client(mock_server, mock_hook_manager):
    """Create TestClient with sessions router and hook_manager on app state."""
    app = FastAPI()
    router = create_sessions_router(mock_server)
    app.include_router(router)
    app.state.hook_manager = mock_hook_manager
    return TestClient(app)


# =============================================================================
# _get_session_stats helper
# =============================================================================


class TestGetSessionStats:
    """Test _get_session_stats helper function."""

    def test_returns_all_stats(self) -> None:
        """All four stat keys are returned with correct values."""
        db = MagicMock()
        session = _make_session()

        # tasks_closed = 3
        # memories_created = 5
        # skills_used = 2
        db.fetchone.side_effect = [(3,), (5,), (2,)]

        with patch("gobby.servers.routes.sessions._get_commit_count", return_value=7):
            stats = _get_session_stats(db, session)

        assert stats["tasks_closed"] == 3
        assert stats["memories_created"] == 5
        assert stats["commit_count"] == 7
        assert stats["skills_used"] == 2

    def test_handles_none_rows(self) -> None:
        """Returns 0 when db.fetchone returns None for each query."""
        db = MagicMock()
        session = _make_session()
        db.fetchone.return_value = None

        with patch("gobby.servers.routes.sessions._get_commit_count", return_value=0):
            stats = _get_session_stats(db, session)

        assert stats["tasks_closed"] == 0
        assert stats["memories_created"] == 0
        assert stats["commit_count"] == 0
        assert stats["skills_used"] == 0

    def test_handles_db_exceptions(self) -> None:
        """Returns 0 for stats when db queries raise exceptions."""
        db = MagicMock()
        session = _make_session()
        db.fetchone.side_effect = Exception("DB error")

        with patch("gobby.servers.routes.sessions._get_commit_count", return_value=0):
            stats = _get_session_stats(db, session)

        assert stats["tasks_closed"] == 0
        assert stats["memories_created"] == 0
        assert stats["skills_used"] == 0


# =============================================================================
# _get_commit_count helper
# =============================================================================


class TestGetCommitCount:
    """Test _get_commit_count helper function."""

    def test_returns_count_with_valid_cwd(self) -> None:
        """Returns commit count when project has repo_path."""
        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at="2026-02-10T10:00:00+00:00",
            updated_at="2026-02-10T12:00:00+00:00",
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch("gobby.servers.routes.sessions.subprocess") as mock_sp:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "5\n"
            mock_sp.run.return_value = mock_result

            count = _get_commit_count(db, session)

        assert count == 5
        mock_sp.run.assert_called_once()
        call_kwargs = mock_sp.run.call_args
        assert call_kwargs.kwargs["cwd"] == "/tmp/repo"

    def test_returns_zero_without_project_id(self) -> None:
        """Returns 0 when session has no project_id."""
        db = MagicMock()
        session = _make_session(project_id=None)

        count = _get_commit_count(db, session)

        assert count == 0

    def test_returns_zero_without_repo_path(self) -> None:
        """Returns 0 when project has no repo_path in DB."""
        db = MagicMock()
        session = _make_session(project_id="proj-1")
        db.fetchone.return_value = None

        count = _get_commit_count(db, session)

        assert count == 0

    def test_returns_zero_when_repo_path_empty(self) -> None:
        """Returns 0 when repo_path row exists but value is empty."""
        db = MagicMock()
        session = _make_session(project_id="proj-1")
        db.fetchone.return_value = ("",)

        count = _get_commit_count(db, session)

        assert count == 0

    def test_returns_zero_on_subprocess_error(self) -> None:
        """Returns 0 when git command fails (nonzero exit)."""
        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at="2026-02-10T10:00:00+00:00",
            updated_at="2026-02-10T12:00:00+00:00",
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch("gobby.servers.routes.sessions.subprocess") as mock_sp:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_sp.run.return_value = mock_result

            count = _get_commit_count(db, session)

        assert count == 0

    def test_returns_zero_on_timeout(self) -> None:
        """Returns 0 when git command times out."""
        import subprocess

        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at="2026-02-10T10:00:00+00:00",
            updated_at="2026-02-10T12:00:00+00:00",
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch(
            "gobby.servers.routes.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            count = _get_commit_count(db, session)

        assert count == 0

    def test_handles_datetime_objects(self) -> None:
        """Handles created_at/updated_at as datetime objects instead of strings."""
        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at=datetime(2026, 2, 10, 10, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 10, 12, 0, 0, tzinfo=UTC),
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch("gobby.servers.routes.sessions.subprocess") as mock_sp:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "3\n"
            mock_sp.run.return_value = mock_result

            count = _get_commit_count(db, session)

        assert count == 3

    def test_handles_none_updated_at(self) -> None:
        """Uses current time when updated_at is None."""
        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at="2026-02-10T10:00:00+00:00",
            updated_at=None,
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch("gobby.servers.routes.sessions.subprocess") as mock_sp:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "1\n"
            mock_sp.run.return_value = mock_result

            count = _get_commit_count(db, session)

        assert count == 1

    def test_handles_db_exception_for_repo_path(self) -> None:
        """Returns 0 when DB query for repo_path throws."""
        db = MagicMock()
        session = _make_session(project_id="proj-1")
        db.fetchone.side_effect = Exception("DB error")

        count = _get_commit_count(db, session)

        assert count == 0

    def test_handles_naive_datetimes(self) -> None:
        """Handles timezone-naive datetime objects correctly."""
        db = MagicMock()
        session = _make_session(
            project_id="proj-1",
            created_at=datetime(2026, 2, 10, 10, 0, 0),  # no tzinfo
            updated_at=datetime(2026, 2, 10, 12, 0, 0),  # no tzinfo
        )
        db.fetchone.return_value = ("/tmp/repo",)

        with patch("gobby.servers.routes.sessions.subprocess") as mock_sp:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "2\n"
            mock_sp.run.return_value = mock_result

            count = _get_commit_count(db, session)

        assert count == 2


# =============================================================================
# POST /sessions/register
# =============================================================================


class TestRegisterSession:
    """Test POST /sessions/register endpoint."""

    def test_register_success(self, client, mock_server) -> None:
        """Register a session returns status and IDs."""
        session = _make_session()
        mock_server.session_manager.register.return_value = session

        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"
        assert data["external_id"] == "ext-123"
        assert data["id"] == "sess-abc123"
        assert data["machine_id"] == "machine-1"

    def test_register_without_machine_id(self, client, mock_server) -> None:
        """Register auto-generates machine_id when not provided."""
        session = _make_session()
        mock_server.session_manager.register.return_value = session

        with patch(
            "gobby.utils.machine_id.get_machine_id",
            return_value="auto-machine",
        ):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "ext-123",
                    "source": "Claude Code",
                },
            )

        assert response.status_code == 200
        assert response.json()["machine_id"] == "auto-machine"

    def test_register_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/register",
            json={"external_id": "ext-123"},
        )

        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_register_extracts_git_branch(self, client, mock_server) -> None:
        """Extracts git_branch from project_path when not provided."""
        session = _make_session()
        mock_server.session_manager.register.return_value = session

        with patch(
            "gobby.utils.git.get_git_metadata",
            return_value={"git_branch": "feature/test"},
        ):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "ext-123",
                    "machine_id": "machine-1",
                    "project_path": "/tmp/project",
                },
            )

        assert response.status_code == 200
        # Verify git_branch was passed to register
        call_kwargs = mock_server.session_manager.register.call_args
        assert call_kwargs.kwargs.get("git_branch") == "feature/test"

    def test_register_value_error(self, client, mock_server) -> None:
        """Returns 400 when resolve_project_id raises ValueError."""
        mock_server.resolve_project_id.side_effect = ValueError("No project found")

        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
            },
        )

        assert response.status_code == 400
        assert "No project found" in response.json()["detail"]

    def test_register_internal_error(self, client, mock_server) -> None:
        """Returns 500 on unexpected internal error."""
        mock_server.session_manager.register.side_effect = RuntimeError("boom")

        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
            },
        )

        assert response.status_code == 500


# =============================================================================
# GET /sessions
# =============================================================================


class TestListSessions:
    """Test GET /sessions endpoint."""

    def test_list_returns_sessions(self, client, mock_server) -> None:
        """GET /sessions returns a list of sessions with counts."""
        sessions = [
            _make_session(id="sess-1", title="Session 1"),
            _make_session(id="sess-2", title="Session 2"),
        ]
        mock_server.session_manager.list.return_value = sessions
        mock_server.message_manager.get_all_counts.return_value = {
            "sess-1": 10,
            "sess-2": 5,
        }

        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["sessions"]) == 2
        assert "response_time_ms" in data

    def test_list_with_filters(self, client, mock_server) -> None:
        """GET /sessions supports query parameter filters."""
        mock_server.session_manager.list.return_value = []
        mock_server.message_manager.get_all_counts.return_value = {}

        response = client.get(
            "/api/sessions",
            params={
                "project_id": "proj-1",
                "status": "active",
                "source": "Claude Code",
                "limit": 50,
            },
        )

        assert response.status_code == 200
        mock_server.session_manager.list.assert_called_once_with(
            project_id="proj-1",
            status="active",
            source="Claude Code",
            limit=50,
            exclude_subagents=False,
        )

    def test_list_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.get("/api/sessions")

        assert response.status_code == 503

    def test_list_empty(self, client, mock_server) -> None:
        """Returns empty list when no sessions exist."""
        mock_server.session_manager.list.return_value = []
        mock_server.message_manager.get_all_counts.return_value = {}

        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["sessions"] == []

    def test_list_handles_message_count_failure(self, client, mock_server) -> None:
        """Still returns sessions when message count fetch fails."""
        sessions = [_make_session()]
        mock_server.session_manager.list.return_value = sessions
        mock_server.message_manager.get_all_counts.side_effect = Exception("boom")

        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        # message_count defaults to 0 when lookup fails
        assert data["sessions"][0]["message_count"] == 0

    def test_list_no_message_manager(self, client, mock_server) -> None:
        """Works when message_manager is None (no enrichment)."""
        sessions = [_make_session()]
        mock_server.session_manager.list.return_value = sessions
        mock_server.message_manager = None

        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1


# =============================================================================
# GET /sessions/{session_id}
# =============================================================================


class TestGetSession:
    """Test GET /sessions/{session_id} endpoint."""

    def test_get_found(self, client, mock_server) -> None:
        """Returns session data when found."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_all_counts.return_value = {"sess-abc123": 15}

        response = client.get("/api/sessions/sess-abc123")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["session"]["id"] == "sess-abc123"
        assert "response_time_ms" in data

    def test_get_not_found(self, client, mock_server) -> None:
        """Returns 404 when session not found."""
        mock_server.session_manager.get.return_value = None

        response = client.get("/api/sessions/nonexistent")

        assert response.status_code == 404
        assert "Session not found" in response.json()["detail"]

    def test_get_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.get("/api/sessions/sess-abc123")

        assert response.status_code == 503

    def test_get_enriches_with_stats(self, client, mock_server) -> None:
        """Session data is enriched with activity stats."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_all_counts.return_value = {}

        with patch(
            "gobby.servers.routes.sessions._get_session_stats",
            return_value={
                "tasks_closed": 3,
                "memories_created": 1,
                "commit_count": 5,
                "skills_used": 2,
            },
        ):
            response = client.get("/api/sessions/sess-abc123")

        assert response.status_code == 200
        session_data = response.json()["session"]
        assert session_data["tasks_closed"] == 3
        assert session_data["memories_created"] == 1
        assert session_data["commit_count"] == 5
        assert session_data["skills_used"] == 2

    def test_get_handles_stats_failure(self, client, mock_server) -> None:
        """Returns session even if stats enrichment fails."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_all_counts.return_value = {}

        with patch(
            "gobby.servers.routes.sessions._get_session_stats",
            side_effect=Exception("stats error"),
        ):
            response = client.get("/api/sessions/sess-abc123")

        assert response.status_code == 200
        assert response.json()["session"]["id"] == "sess-abc123"


# =============================================================================
# GET /sessions/{session_id}/messages
# =============================================================================


class TestGetMessages:
    """Test GET /sessions/{session_id}/messages endpoint."""

    def test_get_messages_success(self, client, mock_server) -> None:
        """Returns messages and total count."""
        mock_server.message_manager.get_messages.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        mock_server.message_manager.count_messages.return_value = 2

        response = client.get("/api/sessions/sess-abc123/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert len(data["messages"]) == 2
        assert data["total_count"] == 2
        assert "response_time_ms" in data

    def test_get_messages_with_params(self, client, mock_server) -> None:
        """Passes limit, offset, role parameters to message_manager."""
        mock_server.message_manager.get_messages.return_value = []
        mock_server.message_manager.count_messages.return_value = 0

        response = client.get(
            "/api/sessions/sess-abc123/messages",
            params={"limit": 50, "offset": 10, "role": "user"},
        )

        assert response.status_code == 200
        mock_server.message_manager.get_messages.assert_called_once_with(
            session_id="sess-abc123", limit=50, offset=10, role="user"
        )

    def test_get_messages_no_message_manager(self, client, mock_server) -> None:
        """Returns 503 when message_manager is None."""
        mock_server.message_manager = None

        response = client.get("/api/sessions/sess-abc123/messages")

        assert response.status_code == 503
        assert "Message manager not available" in response.json()["detail"]


# =============================================================================
# POST /sessions/find_current
# =============================================================================


class TestFindCurrentSession:
    """Test POST /sessions/find_current endpoint."""

    def test_find_current_found(self, client, mock_server) -> None:
        """Returns session when found by composite key."""
        session = _make_session()
        mock_server.session_manager.find_by_external_id.return_value = session

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == "sess-abc123"

    def test_find_current_not_found(self, client, mock_server) -> None:
        """Returns null session when not found."""
        mock_server.session_manager.find_by_external_id.return_value = None

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "ext-999",
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 200
        assert response.json()["session"] is None

    def test_find_current_missing_required_fields(self, client, mock_server) -> None:
        """Returns 400 when required fields are missing."""
        response = client.post(
            "/api/sessions/find_current",
            json={"external_id": "ext-123"},
        )

        assert response.status_code == 400
        assert "Required fields" in response.json()["detail"]

    def test_find_current_missing_external_id(self, client, mock_server) -> None:
        """Returns 400 when external_id is missing."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 400

    def test_find_current_resolves_cwd_to_project_id(self, client, mock_server) -> None:
        """Resolves cwd to project_id when project_id not provided."""
        session = _make_session()
        mock_server.session_manager.find_by_external_id.return_value = session
        mock_server.resolve_project_id.return_value = "proj-from-cwd"

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
                "source": "Claude Code",
                "cwd": "/tmp/project",
            },
        )

        assert response.status_code == 200
        mock_server.resolve_project_id.assert_called_with(None, "/tmp/project")

    def test_find_current_no_project_id_or_cwd(self, client, mock_server) -> None:
        """Returns 400 when neither project_id nor cwd is provided."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
                "source": "Claude Code",
            },
        )

        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_find_current_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "ext-123",
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 503


# =============================================================================
# POST /sessions/find_parent
# =============================================================================


class TestFindParentSession:
    """Test POST /sessions/find_parent endpoint."""

    def test_find_parent_found(self, client, mock_server) -> None:
        """Returns parent session when found."""
        session = _make_session(id="parent-sess")
        mock_server.session_manager.find_parent.return_value = session

        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == "parent-sess"

    def test_find_parent_not_found(self, client, mock_server) -> None:
        """Returns null session when no parent found."""
        mock_server.session_manager.find_parent.return_value = None

        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "machine-1",
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 200
        assert response.json()["session"] is None

    def test_find_parent_missing_source(self, client, mock_server) -> None:
        """Returns 400 when source is missing."""
        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "machine-1",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_find_parent_no_project_id_or_cwd(self, client, mock_server) -> None:
        """Returns 400 when neither project_id nor cwd is provided."""
        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "machine-1",
                "source": "Claude Code",
            },
        )

        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_find_parent_resolves_cwd(self, client, mock_server) -> None:
        """Resolves cwd to project_id when project_id not provided."""
        session = _make_session()
        mock_server.session_manager.find_parent.return_value = session
        mock_server.resolve_project_id.return_value = "proj-from-cwd"

        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "machine-1",
                "source": "Claude Code",
                "cwd": "/tmp/project",
            },
        )

        assert response.status_code == 200
        mock_server.resolve_project_id.assert_called_with(None, "/tmp/project")

    def test_find_parent_auto_machine_id(self, client, mock_server) -> None:
        """Auto-generates machine_id when not provided."""
        session = _make_session()
        mock_server.session_manager.find_parent.return_value = session

        with patch(
            "gobby.utils.machine_id.get_machine_id",
            return_value="auto-machine",
        ):
            response = client.post(
                "/api/sessions/find_parent",
                json={
                    "source": "Claude Code",
                    "project_id": "proj-123",
                },
            )

        assert response.status_code == 200
        mock_server.session_manager.find_parent.assert_called_once_with(
            machine_id="auto-machine",
            source="Claude Code",
            project_id="proj-123",
        )

    def test_find_parent_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/find_parent",
            json={
                "source": "Claude Code",
                "project_id": "proj-123",
            },
        )

        assert response.status_code == 503


# =============================================================================
# POST /sessions/update_status
# =============================================================================


class TestUpdateSessionStatus:
    """Test POST /sessions/update_status endpoint."""

    def test_update_status_success(self, client, mock_server) -> None:
        """Updates session status successfully."""
        session = _make_session(status="archived")
        mock_server.session_manager.update_status.return_value = session

        response = client.post(
            "/api/sessions/update_status",
            json={"session_id": "sess-abc123", "status": "archived"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["status"] == "archived"

    def test_update_status_not_found(self, client, mock_server) -> None:
        """Returns 404 when session not found."""
        mock_server.session_manager.update_status.return_value = None

        response = client.post(
            "/api/sessions/update_status",
            json={"session_id": "nonexistent", "status": "archived"},
        )

        assert response.status_code == 404
        assert "Session not found" in response.json()["detail"]

    def test_update_status_missing_fields(self, client, mock_server) -> None:
        """Returns 400 when required fields are missing."""
        response = client.post(
            "/api/sessions/update_status",
            json={"session_id": "sess-abc123"},
        )

        assert response.status_code == 400
        assert "Required fields" in response.json()["detail"]

    def test_update_status_missing_session_id(self, client, mock_server) -> None:
        """Returns 400 when session_id is missing."""
        response = client.post(
            "/api/sessions/update_status",
            json={"status": "archived"},
        )

        assert response.status_code == 400

    def test_update_status_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/update_status",
            json={"session_id": "sess-abc123", "status": "archived"},
        )

        assert response.status_code == 503


# =============================================================================
# POST /sessions/update_summary
# =============================================================================


class TestUpdateSessionSummary:
    """Test POST /sessions/update_summary endpoint."""

    def test_update_summary_success(self, client, mock_server) -> None:
        """Updates session summary path successfully."""
        session = _make_session(summary_path="/tmp/summary.md")
        mock_server.session_manager.update_summary.return_value = session

        response = client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": "sess-abc123",
                "summary_path": "/tmp/summary.md",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["summary_path"] == "/tmp/summary.md"

    def test_update_summary_not_found(self, client, mock_server) -> None:
        """Returns 404 when session not found."""
        mock_server.session_manager.update_summary.return_value = None

        response = client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": "nonexistent",
                "summary_path": "/tmp/summary.md",
            },
        )

        assert response.status_code == 404

    def test_update_summary_missing_fields(self, client, mock_server) -> None:
        """Returns 400 when required fields are missing."""
        response = client.post(
            "/api/sessions/update_summary",
            json={"session_id": "sess-abc123"},
        )

        assert response.status_code == 400
        assert "Required fields" in response.json()["detail"]

    def test_update_summary_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": "sess-abc123",
                "summary_path": "/tmp/summary.md",
            },
        )

        assert response.status_code == 503


# =============================================================================
# POST /sessions/{session_id}/synthesize-title
# =============================================================================


class TestSynthesizeTitle:
    """Test POST /sessions/{session_id}/synthesize-title endpoint."""

    def test_synthesize_title_success(self, client, mock_server) -> None:
        """Synthesizes and saves a title from conversation messages."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_messages.return_value = [
            {"role": "user", "content": "Help me write a CLI"},
            {"role": "assistant", "content": "Sure, let me help with that CLI tool."},
        ]
        provider = AsyncMock()
        provider.generate_text.return_value = "CLI Tool Development"
        mock_server.llm_service.get_default_provider.return_value = provider
        mock_server.session_manager.update_title.return_value = session

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["title"] == "CLI Tool Development"
        assert "response_time_ms" in data

    def test_synthesize_title_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 503

    def test_synthesize_title_no_llm_service(self, client, mock_server) -> None:
        """Returns 503 when llm_service is None."""
        mock_server.llm_service = None

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 503

    def test_synthesize_title_no_message_manager(self, client, mock_server) -> None:
        """Returns 503 when message_manager is None."""
        mock_server.message_manager = None

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 503

    def test_synthesize_title_session_not_found(self, client, mock_server) -> None:
        """Returns 404 when session does not exist."""
        mock_server.session_manager.get.return_value = None

        response = client.post("/api/sessions/nonexistent/synthesize-title")

        assert response.status_code == 404

    def test_synthesize_title_no_messages(self, client, mock_server) -> None:
        """Returns 422 when session has no messages."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_messages.return_value = []

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 422
        assert "No messages" in response.json()["detail"]

    def test_synthesize_title_only_tool_messages(self, client, mock_server) -> None:
        """Returns 422 when there are only tool messages (no user/assistant)."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_messages.return_value = [
            {"role": "tool", "content": "some tool output"},
        ]

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 422
        assert "No user/assistant messages" in response.json()["detail"]

    def test_synthesize_title_strips_quotes(self, client, mock_server) -> None:
        """LLM output is stripped of surrounding quotes."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_messages.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        provider = AsyncMock()
        provider.generate_text.return_value = '"Greeting Session"'
        mock_server.llm_service.get_default_provider.return_value = provider
        mock_server.session_manager.update_title.return_value = session

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 200
        assert response.json()["title"] == "Greeting Session"

    def test_synthesize_title_empty_llm_output(self, client, mock_server) -> None:
        """Falls back to 'Untitled Session' when LLM returns empty string."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.message_manager.get_messages.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        provider = AsyncMock()
        provider.generate_text.return_value = "  "
        mock_server.llm_service.get_default_provider.return_value = provider
        mock_server.session_manager.update_title.return_value = session

        response = client.post("/api/sessions/sess-abc123/synthesize-title")

        assert response.status_code == 200
        assert response.json()["title"] == "Untitled Session"


# =============================================================================
# POST /sessions/{session_id}/rename
# =============================================================================


class TestRenameSession:
    """Test POST /sessions/{session_id}/rename endpoint."""

    def test_rename_success(self, client, mock_server) -> None:
        """Renames a session title successfully."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.session_manager.update_title.return_value = session

        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": "New Title"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["title"] == "New Title"

    def test_rename_empty_title(self, client, mock_server) -> None:
        """Returns 400 when title is empty."""
        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": ""},
        )

        assert response.status_code == 400
        assert "Title must not be empty" in response.json()["detail"]

    def test_rename_whitespace_title(self, client, mock_server) -> None:
        """Returns 400 when title is only whitespace."""
        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": "   "},
        )

        assert response.status_code == 400

    def test_rename_no_title_field(self, client, mock_server) -> None:
        """Returns 400 when title field is missing."""
        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={},
        )

        assert response.status_code == 400

    def test_rename_session_not_found(self, client, mock_server) -> None:
        """Returns 404 when session does not exist."""
        mock_server.session_manager.get.return_value = None

        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": "New Title"},
        )

        assert response.status_code == 404

    def test_rename_update_title_returns_none(self, client, mock_server) -> None:
        """Returns 404 when update_title returns None (race condition)."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session
        mock_server.session_manager.update_title.return_value = None

        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": "New Title"},
        )

        assert response.status_code == 404

    def test_rename_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post(
            "/api/sessions/sess-abc123/rename",
            json={"title": "New Title"},
        )

        assert response.status_code == 503


# =============================================================================
# POST /sessions/{session_id}/generate-summary
# =============================================================================


class TestGenerateSummary:
    """Test POST /sessions/{session_id}/generate-summary endpoint."""

    def test_generate_summary_success(self, client, mock_server) -> None:
        """Generates AI summary successfully."""
        session = _make_session()
        updated_session = _make_session(summary_markdown="# Summary\nDid stuff.")
        mock_server.session_manager.get.side_effect = [session, updated_session]

        with (
            patch(
                "gobby.sessions.transcripts.get_parser",
                return_value=MagicMock(),
            ),
            patch(
                "gobby.workflows.summary_actions.generate_summary",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
        ):
            response = client.post("/api/sessions/sess-abc123/generate-summary")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["summary_markdown"] == "# Summary\nDid stuff."
        assert "response_time_ms" in data

    def test_generate_summary_no_session_manager(self, client, mock_server) -> None:
        """Returns 503 when session_manager is None."""
        mock_server.session_manager = None

        response = client.post("/api/sessions/sess-abc123/generate-summary")

        assert response.status_code == 503

    def test_generate_summary_no_llm_service(self, client, mock_server) -> None:
        """Returns 503 when llm_service is None."""
        mock_server.llm_service = None

        response = client.post("/api/sessions/sess-abc123/generate-summary")

        assert response.status_code == 503

    def test_generate_summary_session_not_found(self, client, mock_server) -> None:
        """Returns 404 when session does not exist."""
        mock_server.session_manager.get.return_value = None

        response = client.post("/api/sessions/nonexistent/generate-summary")

        assert response.status_code == 404

    def test_generate_summary_with_error(self, client, mock_server) -> None:
        """Returns 422 when generate_summary returns an error."""
        session = _make_session()
        mock_server.session_manager.get.return_value = session

        with (
            patch(
                "gobby.sessions.transcripts.get_parser",
                return_value=MagicMock(),
            ),
            patch(
                "gobby.workflows.summary_actions.generate_summary",
                new_callable=AsyncMock,
                return_value={"error": "No transcript data available"},
            ),
        ):
            response = client.post("/api/sessions/sess-abc123/generate-summary")

        assert response.status_code == 422
        assert "No transcript data" in response.json()["detail"]


# =============================================================================
# POST /sessions/{session_id}/stop - send stop signal
# =============================================================================


class TestStopSession:
    """Test POST /sessions/{session_id}/stop endpoint."""

    def test_stop_signal_success(self, client, mock_hook_manager) -> None:
        """Sends stop signal and returns confirmation."""
        signal = _make_stop_signal()
        mock_hook_manager._stop_registry.signal_stop.return_value = signal

        response = client.post(
            "/api/sessions/sess-abc123/stop",
            json={"reason": "User requested stop", "source": "cli"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stop_signaled"
        assert data["session_id"] == "sess-abc123"
        assert data["signal_id"] == "sig-abc123"
        assert data["reason"] == "External stop request"

    def test_stop_signal_empty_body(self, client, mock_hook_manager) -> None:
        """Uses default reason and source when body is empty."""
        signal = _make_stop_signal()
        mock_hook_manager._stop_registry.signal_stop.return_value = signal

        response = client.post("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        # Defaults: reason="External stop request", source="http_api"
        mock_hook_manager._stop_registry.signal_stop.assert_called_once_with(
            session_id="sess-abc123",
            reason="External stop request",
            source="http_api",
        )

    def test_stop_no_hook_manager(self, client, mock_hook_manager) -> None:
        """Returns 503 when hook_manager is not on app state."""
        # Remove hook_manager from app state
        del client.app.state.hook_manager

        response = client.post("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503
        assert "Hook manager not available" in response.json()["detail"]

    def test_stop_no_stop_registry(self, client, mock_hook_manager) -> None:
        """Returns 503 when _stop_registry is None."""
        mock_hook_manager._stop_registry = None

        response = client.post("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503
        assert "Stop registry not available" in response.json()["detail"]

    def test_stop_no_stop_registry_attr(self, client, mock_hook_manager) -> None:
        """Returns 503 when hook_manager has no _stop_registry attribute."""
        del mock_hook_manager._stop_registry

        response = client.post("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503


# =============================================================================
# GET /sessions/{session_id}/stop - check stop signal
# =============================================================================


class TestGetStopSignal:
    """Test GET /sessions/{session_id}/stop endpoint."""

    def test_get_signal_present(self, client, mock_hook_manager) -> None:
        """Returns signal details when a stop signal exists."""
        signal = _make_stop_signal(acknowledged=False, acknowledged_at=None)
        mock_hook_manager._stop_registry.get_signal.return_value = signal

        response = client.get("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is True
        assert data["session_id"] == "sess-abc123"
        assert data["signal_id"] == "sig-abc123"
        assert data["acknowledged"] is False
        assert data["acknowledged_at"] is None

    def test_get_signal_with_acknowledgment(self, client, mock_hook_manager) -> None:
        """Returns acknowledged signal details."""
        ack_time = datetime(2026, 2, 10, 13, 0, 0, tzinfo=UTC)
        signal = _make_stop_signal(
            acknowledged=True,
            acknowledged_at=ack_time,
        )
        mock_hook_manager._stop_registry.get_signal.return_value = signal

        response = client.get("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is True
        assert data["acknowledged"] is True
        assert data["acknowledged_at"] == ack_time.isoformat()

    def test_get_signal_not_present(self, client, mock_hook_manager) -> None:
        """Returns has_signal=False when no signal exists."""
        mock_hook_manager._stop_registry.get_signal.return_value = None

        response = client.get("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is False
        assert data["session_id"] == "sess-abc123"

    def test_get_signal_no_hook_manager(self, client, mock_hook_manager) -> None:
        """Returns 503 when hook_manager is not on app state."""
        del client.app.state.hook_manager

        response = client.get("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503

    def test_get_signal_no_stop_registry(self, client, mock_hook_manager) -> None:
        """Returns 503 when _stop_registry is None."""
        mock_hook_manager._stop_registry = None

        response = client.get("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503


# =============================================================================
# DELETE /sessions/{session_id}/stop - clear stop signal
# =============================================================================


class TestClearStopSignal:
    """Test DELETE /sessions/{session_id}/stop endpoint."""

    def test_clear_signal_present(self, client, mock_hook_manager) -> None:
        """Clears existing stop signal."""
        mock_hook_manager._stop_registry.clear.return_value = True

        response = client.delete("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cleared"
        assert data["session_id"] == "sess-abc123"
        assert data["was_present"] is True

    def test_clear_signal_not_present(self, client, mock_hook_manager) -> None:
        """Returns no_signal when clearing nonexistent signal."""
        mock_hook_manager._stop_registry.clear.return_value = False

        response = client.delete("/api/sessions/sess-abc123/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_signal"
        assert data["was_present"] is False

    def test_clear_no_hook_manager(self, client, mock_hook_manager) -> None:
        """Returns 503 when hook_manager is not on app state."""
        del client.app.state.hook_manager

        response = client.delete("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503

    def test_clear_no_stop_registry(self, client, mock_hook_manager) -> None:
        """Returns 503 when _stop_registry is None."""
        mock_hook_manager._stop_registry = None

        response = client.delete("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503

    def test_clear_no_stop_registry_attr(self, client, mock_hook_manager) -> None:
        """Returns 503 when hook_manager has no _stop_registry attribute."""
        del mock_hook_manager._stop_registry

        response = client.delete("/api/sessions/sess-abc123/stop")

        assert response.status_code == 503
