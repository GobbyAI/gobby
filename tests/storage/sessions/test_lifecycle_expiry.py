"""Focused tests for session lifecycle expiry edge cases."""

from __future__ import annotations

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(temp_db: LocalDatabase) -> str:
    """Create a project and return its ID."""
    return LocalProjectManager(temp_db).create(name="test-project", repo_path="/tmp/test").id


@pytest.fixture
def session_mgr(temp_db: LocalDatabase) -> SessionManager:
    """Create the canonical storage SessionManager under test."""
    return SessionManager(temp_db)


def test_expire_stale_sessions_expires_old_untracked_terminal_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-untracked-terminal",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = datetime('now', '-25 hours'),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 1
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "expired"


def test_expire_stale_sessions_keeps_tracked_terminal_session_with_recent_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="tracked-terminal",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        terminal_context={"tmux_pane": "%1"},
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = datetime('now', '-25 hours'),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"


def test_expire_stale_sessions_keeps_web_chat_session_with_recent_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-web-chat",
        machine_id="machine-1",
        source="claude",
        project_id=project_id,
        session_type="web_chat",
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = datetime('now', '-25 hours'),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"
