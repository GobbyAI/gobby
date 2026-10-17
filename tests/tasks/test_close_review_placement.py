"""Task-close reviewers open beside the closing session's own workspace pane."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.tasks.agentic_close_review import reviewer_placement
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _caller(db: HubDatabase, project_id: str, pane: str | None = None) -> str:
    sessions = SessionManager(db)
    caller = sessions.register(
        external_id=f"closer-{uuid4()}",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    if pane is not None:
        sessions.update(caller.id, terminal_context={"pane_ref": pane})
    return caller.id


def _pane(db: HubDatabase, project_id: str) -> str:
    LocalMachineManager(db).upsert_seen(MACHINE_ID, TEST_USER_ID, hostname="local")
    workspaces = WorkspaceManager(db)
    workspace = workspaces.create(MACHINE_ID, "lanes")[0]
    pane = str(uuid4())
    workspaces.create_tab(workspace.id, pane_id=pane, project_id=project_id, title="Lane 8")
    return pane


def test_reviewer_splits_right_of_the_callers_pane(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project = str(sample_project["id"])
    pane = _pane(temp_db, project)

    placement = reviewer_placement(temp_db, _caller(temp_db, project, pane), "#42")

    assert placement == {"split": {"pane": pane, "axis": "right", "title": "Close review #42"}}


def test_reviewer_is_unplaced_when_the_caller_has_no_pane(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project = str(sample_project["id"])
    _pane(temp_db, project)

    assert reviewer_placement(temp_db, _caller(temp_db, project), "#42") is None
    assert reviewer_placement(temp_db, None, "#42") is None
