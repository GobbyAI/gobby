"""HTTP task list response shape drops legacy task-state fields."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.config import DaemonConfig
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_no_legacy_fields(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    manager.create_task(project_id=sample_project["id"], title="Shape")
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=SessionManager(temp_db),
        task_manager=manager,
    )

    with patch.object(server, "resolve_project_id", return_value=sample_project["id"]):
        body = TestClient(server.app).get("/api/tasks").json()

    task = body["tasks"][0]
    assert "status" not in task
    assert "lifecycle" not in task
    assert "lifecycle_stage" not in task
