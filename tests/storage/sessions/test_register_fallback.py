"""Regression tests for unified SessionManager register fallback behavior."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

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


def test_register_session_returns_uuid_str_on_storage_failure(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="fallback-session",
            machine_id="machine-1",
            source="claude",
            project_id=project_id,
        )

    assert isinstance(session_id, str)
    assert len(session_id) == 36


def test_register_session_fallback_does_not_persist(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="failed-session",
            machine_id="machine-1",
            source="claude",
            project_id=project_id,
        )

    assert session_mgr.get(session_id) is None


def test_register_session_fallback_does_not_populate_caches(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="failed-session",
            machine_id="machine-1",
            source="claude",
            project_id=project_id,
        )

    assert session_mgr.get_session_id("failed-session", "claude") is None
    assert (
        session_mgr.lookup_session_id(
            external_id="failed-session",
            source="claude",
            machine_id="machine-1",
            project_id=project_id,
        )
        is None
    )
    # Intentional white-box assertion: register_session() owns cache population,
    # and this regression specifically guards that its private metadata cache stays
    # empty on fallback. The coupling is deliberate because that's the bug surface.
    assert session_id not in session_mgr._session_metadata


def test_register_session_happy_path_populates_caches(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session_id = session_mgr.register_session(
        external_id="storage-session",
        machine_id="machine-1",
        source="claude",
        project_id=project_id,
        transcript_path="/tmp/storage-session.jsonl",
        title="Unified Manager Session",
    )

    assert len(session_id) == 36
    assert session_mgr.get_session_id("storage-session", "claude") == session_id
    assert (
        session_mgr.lookup_session_id(
            external_id="storage-session",
            source="claude",
            machine_id="machine-1",
            project_id=project_id,
        )
        == session_id
    )
    assert session_mgr._session_metadata[session_id]["external_id"] == "storage-session"


def test_register_existing_session_backfills_terminal_context(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    created = session_mgr.register(
        external_id="existing-terminal-session",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
    )

    terminal_context = {
        "parent_pid": 12345,
        "tmux_pane": "%7",
        "tmux_socket_path": "/tmp/tmux-501/gobby",
    }

    updated = session_mgr.register(
        external_id="existing-terminal-session",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        terminal_context=terminal_context,
        workflow_name="developer",
    )

    assert updated.id == created.id
    assert updated.terminal_context == terminal_context
    assert updated.workflow_name == "developer"


def test_register_raises_on_storage_failure(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    class FailingConnection:
        def execute(self, sql: str, params: object = ()) -> object:
            if "SELECT MAX(seq_num)" in sql:
                return SimpleNamespace(fetchone=lambda: {"max_seq": None})
            if "INSERT INTO sessions" in sql:
                raise RuntimeError("boom")
            raise AssertionError(f"Unexpected SQL: {sql}")

    @contextmanager
    def transaction_with_insert_failure():
        yield FailingConnection()

    with patch.object(
        session_mgr.db,
        "transaction_immediate",
        side_effect=transaction_with_insert_failure,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            session_mgr.register(
                external_id="raise-session",
                machine_id="machine-1",
                source="claude",
                project_id=project_id,
            )
