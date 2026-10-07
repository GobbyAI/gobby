"""Per-hook-event memo of repeated hub reads (#23721)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from gobby.storage.hub.read_scope import hub_read_scope, written_tables
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit

SESSION_ROW_SELECT = "SELECT * FROM sessions LEFT JOIN"


def _register(session_manager: SessionManager, project_id: str) -> Session:
    return session_manager.register(
        external_id="read-scope-session",
        machine_id=get_machine_id(),
        source="claude",
        project_id=project_id,
    )


@pytest.fixture
def row_reads(session_manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every full session-row SELECT issued through the hub adapter."""
    reads: list[str] = []
    fetchone: Callable[..., Any] = session_manager.db.fetchone

    def counting_fetchone(sql: str, params: Any = ()) -> Any:
        if sql.startswith(SESSION_ROW_SELECT):
            reads.append(sql)
        return fetchone(sql, params)

    monkeypatch.setattr(session_manager.db, "fetchone", counting_fetchone)
    return reads


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM sessions WHERE id = %s", frozenset()),
        ("  select variables FROM session_variables WHERE id = %s FOR UPDATE", frozenset()),
        ("SELECT pg_advisory_xact_lock(hashtext(%s))", frozenset()),
        ("SET LOCAL statement_timeout = 1000", frozenset()),
        ("UPDATE sessions SET title = %s WHERE id = %s", frozenset({"sessions"})),
        (
            "INSERT INTO public.session_variables (session_id) VALUES (%s)",
            frozenset({"session_variables"}),
        ),
        (
            "WITH changed AS (UPDATE machines SET hostname = %s RETURNING id) SELECT * FROM changed",
            frozenset({"machines"}),
        ),
        ("WITH live AS (SELECT id FROM sessions) SELECT * FROM live", frozenset()),
        ("DELETE FROM loop_progress WHERE session_id = %s", None),
        ("WITH gone AS (DELETE FROM sessions RETURNING id) SELECT 1", None),
        ("TRUNCATE sessions", None),
        ("LOCK TABLE sessions", None),
    ],
)
def test_written_tables_classifies_statements(sql: str, expected: frozenset[str] | None) -> None:
    assert written_tables(sql) == expected


def _set_title(session_manager: SessionManager, session_id: str, title: str) -> None:
    with session_manager.db.transaction() as conn:
        conn.execute("UPDATE sessions SET title = %s WHERE id = %s", (title, session_id))


def test_scope_reads_session_row_once(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])
    row_reads.clear()

    with hub_read_scope():
        first = session_manager.get(session.id)
        second = session_manager.get(session.id)

    assert len(row_reads) == 1
    assert first is not None and second is not None
    assert second.id == first.id == session.id


def test_reads_outside_scope_hit_hub(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])
    row_reads.clear()

    with hub_read_scope():
        session_manager.get(session.id)
    session_manager.get(session.id)
    session_manager.get(session.id)

    assert len(row_reads) == 3


def test_write_in_scope_refreshes_row(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])

    with hub_read_scope():
        session_manager.get(session.id)
        _set_title(session_manager, session.id, "after write")
        refreshed = session_manager.get(session.id)

    assert refreshed is not None
    assert refreshed.title == "after write"


def test_write_from_other_thread_refreshes_row(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])

    with hub_read_scope():
        session_manager.get(session.id)
        writer = threading.Thread(
            target=_set_title, args=(session_manager, session.id, "other thread")
        )
        writer.start()
        writer.join()
        refreshed = session_manager.get(session.id)

    assert refreshed is not None
    assert refreshed.title == "other thread"


def test_unrelated_write_keeps_row(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])
    row_reads.clear()

    with hub_read_scope():
        session_manager.get(session.id)
        with session_manager.db.transaction() as conn:
            conn.execute(
                "UPDATE machines SET hostname = hostname WHERE id = %s", (get_machine_id(),)
            )
        session_manager.get(session.id)

    assert len(row_reads) == 1


def test_read_inside_transaction_hits_hub(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])
    row_reads.clear()

    with hub_read_scope():
        session_manager.get(session.id)
        with session_manager.db.transaction():
            session_manager.get(session.id)

    assert len(row_reads) == 2


def test_memo_hands_out_independent_copies(
    session_manager: SessionManager, sample_project: dict[str, Any], row_reads: list[str]
) -> None:
    session = _register(session_manager, sample_project["id"])
    with session_manager.db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET terminal_context = %s WHERE id = %s",
            ('{"tmux_pane": "%1"}', session.id),
        )

    with hub_read_scope():
        first = session_manager.get(session.id)
        assert first is not None and first.terminal_context is not None
        first.terminal_context["tmux_pane"] = "mutated"
        second = session_manager.get(session.id)

    assert second is not None and second.terminal_context is not None
    assert second.terminal_context["tmux_pane"] == "%1"
