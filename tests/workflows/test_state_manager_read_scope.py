"""Session variables are read once per hook event (#23721)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from gobby.storage.hub.read_scope import hub_read_scope
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._contested_expiry import (
    read_session_variables,
    read_session_variables_row,
)
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

VARIABLES_SELECT = "SELECT sv.session_id IS NOT NULL AS stored"
PROJECT_ID_SELECT = "SELECT project_id FROM sessions"


@pytest.fixture
def session_id(session_manager: SessionManager, sample_project: dict[str, Any]) -> str:
    return session_manager.register(
        external_id="variables-scope-session",
        machine_id=get_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    ).id


@pytest.fixture
def variables(session_manager: SessionManager) -> SessionVariableManager:
    return SessionVariableManager(session_manager.db)


@pytest.fixture
def reads(session_manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record variables-blob and project-id SELECTs issued outside transactions."""
    seen: list[str] = []
    fetchone: Callable[..., Any] = session_manager.db.fetchone

    def counting_fetchone(sql: str, params: Any = ()) -> Any:
        if sql.startswith((VARIABLES_SELECT, PROJECT_ID_SELECT)):
            seen.append(sql)
        return fetchone(sql, params)

    monkeypatch.setattr(session_manager.db, "fetchone", counting_fetchone)
    return seen


def test_scope_reads_variables_once(
    variables: SessionVariableManager, session_id: str, reads: list[str]
) -> None:
    variables.merge_variables(session_id, {"probe": 1})
    reads.clear()

    with hub_read_scope():
        first = variables.get_variables(session_id)
        second = variables.get_variables(session_id)
        stored = read_session_variables(variables.db, session_id)

    # One statement serves the blob and the project for defaults (#23359).
    assert [sql.startswith(VARIABLES_SELECT) for sql in reads].count(True) == 1
    assert [sql.startswith(PROJECT_ID_SELECT) for sql in reads].count(True) == 0
    assert first["probe"] == second["probe"] == 1
    assert stored is not None and stored["probe"] == 1


def test_variable_write_refreshes_scoped_blob(
    variables: SessionVariableManager, session_id: str, reads: list[str]
) -> None:
    variables.merge_variables(session_id, {"probe": 1})

    with hub_read_scope():
        variables.get_variables(session_id)
        variables.merge_variables(session_id, {"probe": 2})
        refreshed = variables.get_variables(session_id)

    assert refreshed["probe"] == 2


def test_scoped_blob_copies_are_independent(
    variables: SessionVariableManager, session_id: str, reads: list[str]
) -> None:
    variables.merge_variables(session_id, {"probe": {"nested": 1}})

    with hub_read_scope():
        variables.get_variables(session_id)["probe"]["nested"] = 99
        again = variables.get_variables(session_id)

    assert again["probe"]["nested"] == 1


def test_shared_row_carries_project_without_stored_variables(
    variables: SessionVariableManager, session_id: str, sample_project: dict[str, Any]
) -> None:
    with hub_read_scope():
        row = read_session_variables_row(variables.db, session_id)
        stored = read_session_variables(variables.db, session_id)

    assert row["stored"] is False
    assert str(row["project_id"]) == sample_project["id"]
    assert stored is None
