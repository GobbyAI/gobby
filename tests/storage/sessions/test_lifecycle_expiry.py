"""Focused tests for session lifecycle expiry edge cases."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_lifecycle import _build_empty_session_prune_reference_guards
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._field_update import _FieldUpdateMixin

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def project_id(temp_db: HubDatabase) -> str:
    """Create a project and return its ID."""
    return LocalProjectManager(temp_db).create(name="test-project", repo_path="/tmp/test").id


@pytest.fixture
def session_mgr(temp_db: HubDatabase) -> SessionManager:
    """Create the canonical storage SessionManager under test."""
    return SessionManager(temp_db)


class _PostgresColumnCaptureDb:
    dialect = "postgres"

    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def fetchall(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, str]]:
        self.queries.append((query, params))
        if params == ("sessions",):
            return [{"name": "parent_session_id"}]
        return []


def test_empty_session_prune_reference_guards_use_postgres_information_schema() -> None:
    db = _PostgresColumnCaptureDb()

    guards = _build_empty_session_prune_reference_guards(db)  # type: ignore[arg-type]

    assert any("information_schema.columns" in query for query, _params in db.queries)
    assert guards == (
        "NOT EXISTS (SELECT 1 FROM sessions ref WHERE ref.parent_session_id = sessions.id)",
    )


def test_update_status_if_non_terminal_loses_race_to_expiration() -> None:
    manager = MagicMock()
    manager.get.return_value.status = "active"
    manager.db.execute.return_value.rowcount = 0

    updated = _FieldUpdateMixin.update_status_if_non_terminal(manager, "session-1", "paused")

    assert updated is None
    manager.get.assert_called_once_with("session-1")
    manager._notify_session_change.assert_not_called()
    query, params = manager.db.execute.call_args.args
    assert "status != ALL(%s)" in query
    assert params[0] == "paused"
    assert params[2] == "session-1"
    assert set(params[3]) == {"expired", "deleted"}


def test_expire_stale_sessions_keeps_recently_active_untracked_terminal_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-untracked-terminal",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"


def test_expire_stale_sessions_keeps_stale_tmux_terminal_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="tracked-terminal",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context={"tmux_pane": "%1"},
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = NOW() - INTERVAL '25 hours',
            status = 'paused'
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "paused"


def test_expire_stale_sessions_keeps_web_chat_session_with_recent_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-web-chat",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
        session_type="web_chat",
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"
