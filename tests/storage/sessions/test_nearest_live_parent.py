"""Nearest-live-parent resolution for the agents tree (#25239)."""

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._lineage_discovery import nearest_live_parent_ids
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration

MACHINE_ID = "21000000-0000-4000-8000-000000000004"


@pytest.fixture(autouse=True)
def _enrolled_machine(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    LocalMachineManager(temp_db).upsert_seen(MACHINE_ID, TEST_USER_ID)
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)


def _register(
    sessions: SessionManager, project_id: str, name: str, parent_id: str | None = None
) -> Session:
    return sessions.register(
        external_id=name,
        machine_id=MACHINE_ID,
        source="pipeline",
        project_id=project_id,
        parent_session_id=parent_id,
    )


def test_live_parent_resolves_to_itself(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    live = _register(sessions, str(sample_project["id"]), "live")

    assert nearest_live_parent_ids(temp_db, [live.id]) == {live.id: live.id}


def test_terminal_chain_resolves_to_nearest_live_ancestor(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    project_id = str(sample_project["id"])
    root = _register(sessions, project_id, "root")
    live = _register(sessions, project_id, "live", root.id)
    gone = _register(sessions, project_id, "gone", live.id)
    sessions.update_status(gone.id, "deleted")
    expired = _register(sessions, project_id, "expired", gone.id)
    sessions.update_status(expired.id, "expired")

    resolved = nearest_live_parent_ids(temp_db, [expired.id, gone.id, live.id])

    assert resolved == {expired.id: live.id, gone.id: live.id, live.id: live.id}


def test_terminal_root_resolves_to_none(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    gone = _register(sessions, str(sample_project["id"]), "gone")
    sessions.update_status(gone.id, "deleted")

    assert nearest_live_parent_ids(temp_db, [gone.id]) == {gone.id: None}


def test_missing_parent_row_keeps_its_id(temp_db: HubDatabase) -> None:
    missing = "21000000-0000-4000-8000-0000000000ff"

    assert nearest_live_parent_ids(temp_db, [missing, None]) == {missing: missing}


def test_cycle_through_terminal_rows_terminates_as_none(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    project_id = str(sample_project["id"])
    first = _register(sessions, project_id, "first")
    second = _register(sessions, project_id, "second", first.id)
    sessions.update_status(first.id, "deleted")
    sessions.update_status(second.id, "deleted")
    temp_db.execute(
        "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (second.id, first.id)
    )

    assert nearest_live_parent_ids(temp_db, [second.id]) == {second.id: None}
