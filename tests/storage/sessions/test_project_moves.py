"""Tests for moving sessions between projects."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _create_source_project(db: HubDatabase, name: str) -> str:
    return LocalProjectManager(db).create(name=name, repo_path=f"/tmp/{name}").id


def test_register_recovery_remints_seq_num_in_destination(
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    source_project_id = _create_source_project(temp_db, "registration-move-source")
    source = session_manager.register(
        external_id="registration-move",
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=source_project_id,
    )
    destination = session_manager.register(
        external_id="destination-existing",
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )

    recovered = session_manager.register(
        external_id="registration-move",
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )

    assert recovered.id == source.id
    assert recovered.project_id == sample_project["id"]
    assert destination.seq_num == 1
    assert recovered.seq_num == 2


def test_update_project_id_remints_seq_num_in_destination(
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    source_project_id = _create_source_project(temp_db, "update-move-source")
    source = session_manager.register(
        external_id="update-move",
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=source_project_id,
    )
    destination = session_manager.register(
        external_id="update-destination-existing",
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )

    updated = session_manager.update(source.id, project_id=sample_project["id"])

    assert updated is not None
    assert updated.project_id == sample_project["id"]
    assert destination.seq_num == 1
    assert updated.seq_num == 2
