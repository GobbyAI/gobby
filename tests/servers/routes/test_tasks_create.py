"""HTTP creation surface contracts for Phase 5 task types."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.config import DaemonConfig
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_post_simple_fix(temp_db, sample_project) -> None:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=SessionManager(temp_db),
        task_manager=LocalTaskManager(temp_db),
    )

    with patch.object(server, "resolve_project_id", return_value=sample_project["id"]):
        response = TestClient(server.app).post(
            "/api/tasks",
            json={"title": "Small fix", "task_type": "simple_fix"},
        )

    assert response.status_code == 201
    assert response.json()["task_type"] == "simple_fix"


def test_post_review_anchor(temp_db, sample_project) -> None:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=SessionManager(temp_db),
        task_manager=LocalTaskManager(temp_db),
    )

    with patch.object(server, "resolve_project_id", return_value=sample_project["id"]):
        response = TestClient(server.app).post(
            "/api/tasks",
            json={"title": "Round anchor", "task_type": "review_anchor"},
        )

    assert response.status_code == 201
    assert response.json()["task_type"] == "review_anchor"
