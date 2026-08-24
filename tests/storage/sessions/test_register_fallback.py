"""Regression tests for unified SessionManager register fallback behavior."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

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


def test_register_session_returns_empty_str_on_storage_failure(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="fallback-session",
            machine_id="20000000-0000-4000-8000-000000000002",
            source="claude",
            project_id=project_id,
        )

    assert session_id == ""


def test_register_session_fallback_does_not_persist(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="failed-session",
            machine_id="20000000-0000-4000-8000-000000000002",
            source="claude",
            project_id=project_id,
        )

    assert session_id == ""
    assert session_mgr.get(session_id) is None


def test_register_session_fallback_does_not_populate_caches(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="failed-session",
            machine_id="20000000-0000-4000-8000-000000000002",
            source="claude",
            project_id=project_id,
        )

    assert session_mgr.get_session_id("failed-session", "claude") is None
    assert (
        session_mgr.lookup_session_id(
            external_id="failed-session",
            source="claude",
            project_id=project_id,
        )
        is None
    )
    assert session_id == ""
    assert session_mgr._session_metadata == {}


def test_register_session_failure_returns_existing_canonical_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    canonical_id = session_mgr.register_session(
        external_id="resumed-codex",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )

    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        session_id = session_mgr.register_session(
            external_id="resumed-codex",
            machine_id="20000000-0000-4000-8000-000000000002",
            source="codex",
            project_id=project_id,
            transcript_path="/tmp/resumed-codex.jsonl",
        )

    assert session_id == canonical_id
    assert session_mgr.get_session_id("resumed-codex", "codex") == canonical_id
    assert session_mgr._session_metadata[canonical_id]["transcript_path"] == (
        "/tmp/resumed-codex.jsonl"
    )
    assert session_mgr._session_metadata[canonical_id]["title"] == "Codex"


def test_register_session_happy_path_populates_caches(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session_id = session_mgr.register_session(
        external_id="storage-session",
        machine_id="20000000-0000-4000-8000-000000000002",
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
            project_id=project_id,
        )
        == session_id
    )
    assert session_mgr._session_metadata[session_id]["external_id"] == "storage-session"


def test_register_session_happy_path_caches_persisted_provisional_title(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session_id = session_mgr.register_session(
        external_id="storage-provisional-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        transcript_path="/tmp/storage-provisional-session.jsonl",
    )

    session = session_mgr.get(session_id)

    assert session is not None
    assert session.title == "Codex"
    assert session.title_source == "provisional"
    assert session_mgr._session_metadata[session_id]["title"] == session.title


def test_register_existing_session_backfills_terminal_context(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    created = session_mgr.register(
        external_id="existing-terminal-session",
        machine_id="20000000-0000-4000-8000-000000000002",
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
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context=terminal_context,
        workflow_name="developer",
    )

    assert updated.id == created.id
    assert updated.terminal_context == terminal_context
    assert updated.workflow_name == "developer"


def test_register_existing_session_merges_cwd_without_losing_tmux_context(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    original_context = {
        "parent_pid": 12345,
        "tmux_pane": "%7",
        "tmux_socket_path": "/tmp/tmux-501/gobby",
    }
    created = session_mgr.register(
        external_id="existing-terminal-session-with-cwd",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context=original_context,
    )

    updated = session_mgr.register(
        external_id="existing-terminal-session-with-cwd",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context={"cwd": "/work/repos/gobby"},
    )

    assert updated.id == created.id
    assert updated.terminal_context == {
        **original_context,
        "cwd": "/work/repos/gobby",
    }


def test_backfill_terminal_context_merges_without_losing_tmux_context(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    created = session_mgr.register(
        external_id="backfill-terminal-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context={"tmux_pane": "%7"},
    )

    updated, tmux_target_added = session_mgr.backfill_terminal_context(
        created.id,
        {"cwd": "/work/repos/gobby"},
    )

    assert updated is not None
    assert updated.terminal_context == {
        "tmux_pane": "%7",
        "cwd": "/work/repos/gobby",
    }
    assert tmux_target_added is False


def test_recover_session_prefers_tmux_context_over_cwd_only_candidate(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    weak = session_mgr.register(
        external_id="recover-terminal-session",
        machine_id=None,
        source="codex",
        project_id=project_id,
        terminal_context={"cwd": "/work/repos/gobby"},
    )
    tmux_capable = session_mgr.register(
        external_id="recover-terminal-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
        terminal_context={"tmux_pane": "%8", "tmux_socket_path": "/tmp/tmux-501/gobby"},
    )

    recovered = session_mgr.recover_session(
        external_id="recover-terminal-session",
        source="codex",
        project_id=project_id,
    )

    assert recovered is not None
    assert recovered.id == tmux_capable.id
    assert recovered.id != weak.id


def test_recover_session_refuses_equal_score_cross_source_candidates(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    first = session_mgr.register(
        external_id="ambiguous-recovery-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    second = session_mgr.register(
        external_id="ambiguous-recovery-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
    )

    recovered = session_mgr.recover_session(
        external_id="ambiguous-recovery-session",
        source="agy",
        project_id=project_id,
    )

    assert first.id != second.id
    assert recovered is None


def test_registration_failure_does_not_recover_web_chat_identity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    web_chat = session_mgr.register(
        external_id="terminal-web-chat-isolation",
        machine_id=None,
        source="codex",
        project_id=project_id,
        session_type="web_chat",
    )

    with patch.object(session_mgr, "register", side_effect=RuntimeError("boom")):
        recovered_id = session_mgr.register_session(
            external_id="terminal-web-chat-isolation",
            machine_id="20000000-0000-4000-8000-000000000002",
            source="codex",
            project_id=project_id,
        )

    assert recovered_id == ""
    assert (
        session_mgr.find_active_by_external_id(
            "terminal-web-chat-isolation",
            "codex",
        )
        is None
    )
    assert session_mgr.get(web_chat.id) == web_chat


def test_register_raises_on_storage_failure(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    original_transaction = session_mgr.db.transaction_immediate

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql: str, params: object = ()) -> object:
            if "INSERT INTO sessions" in sql:
                raise RuntimeError("boom")
            return self._conn.execute(sql, params)

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    @contextmanager
    def transaction_with_insert_failure(lock: object | None = None):
        with original_transaction(lock) as conn:
            yield FailingConnection(conn)

    with patch.object(session_mgr.db, "transaction_immediate", transaction_with_insert_failure):
        with pytest.raises(RuntimeError, match="boom"):
            session_mgr.register(
                external_id="raise-session",
                machine_id="20000000-0000-4000-8000-000000000002",
                source="claude",
                project_id=project_id,
            )


def test_expired_row_is_invisible_to_lookup_but_visible_to_recovery(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    """The asymmetry that sends a same-source event down the recovery path.

    lookup_session_id validates its candidate against a status exclusion, so a
    retired row is invisible to it. recover_session applies no status filter.
    A recovery can therefore be reached with the source matching exactly, which
    is why the caller must report the dimension that actually differed rather
    than assuming a source mismatch.
    """
    session = session_mgr.register(
        external_id="retired-terminal-session",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        terminal_context={"tmux_pane": "%9", "tmux_socket_path": "/tmp/tmux-501/gobby"},
    )
    assert (
        session_mgr.lookup_session_id(
            "retired-terminal-session", source="claude", project_id=project_id
        )
        == session.id
    )

    assert session_mgr.mark_session_expired(session.id)

    assert (
        session_mgr.lookup_session_id(
            "retired-terminal-session", source="claude", project_id=project_id
        )
        is None
    )
    recovered = session_mgr.recover_session(
        external_id="retired-terminal-session",
        source="claude",
        project_id=project_id,
    )
    assert recovered is not None
    assert recovered.id == session.id
    assert recovered.source == "claude"
    assert recovered.status == "expired"
