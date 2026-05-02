"""Legacy HTTP task filters are rejected after Phase 5."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.config import DaemonConfig
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_status_filter_400(temp_db, sample_project) -> None:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=SessionManager(temp_db),
        task_manager=LocalTaskManager(temp_db),
    )

    with patch.object(server, "resolve_project_id", return_value=sample_project["id"]):
        response = TestClient(server.app).get("/api/tasks?status=open")

    assert response.status_code == 400
