"""D5 owner-label bug: the web serializer resolves the owning session to a
friendly ref (never the raw UUID) and the dependency tree carries real tasks.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.config import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(temp_db: HubDatabase) -> str:
    pm = LocalProjectManager(temp_db)
    return pm.create(name="owner-ref", repo_path="/tmp/owner-ref").id


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def session(temp_db: HubDatabase, project_id: str) -> Session:
    return SessionManager(temp_db).register(
        external_id="owner-ref-session",
        machine_id="test-machine",
        source="claude",
        project_id=project_id,
        title="Owner session",
    )


@pytest.fixture
def client(
    temp_db: HubDatabase,
    task_manager: LocalTaskManager,
    project_id: str,
) -> Iterator[TestClient]:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=SessionManager(temp_db),
        task_manager=task_manager,
    )
    with patch.object(server, "resolve_project_id", return_value=project_id):
        yield TestClient(server.app)


def test_get_task_owner_ref_is_friendly_not_uuid(
    client: TestClient,
    task_manager: LocalTaskManager,
    project_id: str,
    session: Session,
) -> None:
    task = task_manager.create_task(project_id=project_id, title="Claimed")
    task_manager.claim_task(task.id, session_id=session.id)

    body = client.get(f"/api/tasks/{task.id}").json()

    ref = body["owner_session_ref"]
    assert ref is not None
    assert ref["session_id"] == session.id
    assert ref["ref"] == session.ref
    assert ref["source"] == "claude"
    assert ref["ref"] != session.id
    assert len(ref["ref"]) < len(session.id)


def test_list_tasks_carries_owner_ref(
    client: TestClient,
    task_manager: LocalTaskManager,
    project_id: str,
    session: Session,
) -> None:
    task = task_manager.create_task(project_id=project_id, title="Listed")
    task_manager.claim_task(task.id, session_id=session.id)

    tasks = client.get("/api/tasks").json()["tasks"]
    listed = next(t for t in tasks if t["id"] == task.id)

    assert listed["owner_session_ref"]["ref"] == session.ref
    assert listed["owner_session_ref"]["source"] == "claude"


def test_unclaimed_task_owner_ref_is_none(
    client: TestClient,
    task_manager: LocalTaskManager,
    project_id: str,
) -> None:
    task = task_manager.create_task(project_id=project_id, title="Unclaimed")

    body = client.get(f"/api/tasks/{task.id}").json()

    assert body["owner_session_ref"] is None


def test_dependency_tree_resolves_actual_tasks(
    client: TestClient,
    task_manager: LocalTaskManager,
    project_id: str,
) -> None:
    blocker = task_manager.create_task(
        project_id=project_id, title="Upstream migration", task_type="task"
    )
    blocked = task_manager.create_task(
        project_id=project_id, title="Downstream work", task_type="feature"
    )
    TaskDependencyManager(task_manager.db).add_dependency(blocked.id, blocker.id, "blocks")

    tree = client.get(f"/api/tasks/{blocked.id}/dependencies?direction=both").json()

    node = tree["blockers"][0]
    assert node["id"] == blocker.id
    assert node["ref"] == f"#{blocker.seq_num}"
    assert node["title"] == "Upstream migration"
    assert node["task_type"] == "task"
