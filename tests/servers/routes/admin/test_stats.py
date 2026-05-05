from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.admin._stats import register_stats_routes


@pytest.fixture
def test_app():
    app = FastAPI()
    router = APIRouter()
    server_mock = MagicMock()
    server_mock.services.database = MagicMock()

    register_stats_routes(router, server_mock)
    app.include_router(router)

    return app, server_mock


def test_stats_no_filters(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    def fetchall_mock(query, params):
        if "FROM tasks" in query and "GROUP BY task_state" in query:
            return [{"task_state": "ready", "cnt": 10}, {"task_state": "in_progress", "cnt": 2}]
        if "FROM tasks t" in query and "is_ready_sql" not in query:
            return [{"cnt": 5}]
        if (
            "SELECT COUNT(*) as cnt FROM tasks" in query
            and "closed_at IS NULL" in query
            and "blocker.closed_at IS NULL" in query
        ):
            if "NOT EXISTS" in query:
                return [{"cnt": 3}]  # ready
            elif "EXISTS" in query:
                return [{"cnt": 2}]  # blocked
        if "SELECT status, COUNT(*) as cnt FROM sessions" in query:
            return [{"status": "active", "cnt": 1}]
        if "SELECT source, status, COUNT(*) as cnt FROM sessions" in query:
            return [{"source": "cli", "status": "active", "cnt": 1}]
        if "SELECT memory_type, COUNT(*) as cnt FROM memories" in query:
            return [{"memory_type": "fact", "cnt": 4}]
        if "FROM metrics_events" in query and "event_type = 'tool_call'" in query:
            if "GROUP BY name" in query:
                return [{"name": "test_tool", "cnt": 5}]
        if "FROM metrics_events" in query and "event_type = 'rule_eval'" in query:
            if "GROUP BY name" in query:
                return [{"name": "rule1", "cnt": 2, "blocks": 0}]
        if (
            "FROM metrics_events" in query
            and "event_type IN ('skill_search', 'skill_invoke')" in query
        ):
            if "GROUP BY name" in query:
                return [{"name": "skill1", "event_type": "skill_invoke", "cnt": 1}]

        # defaults
        if "SELECT COUNT(*) as cnt" in query:
            return [{"cnt": 0}]
        return []

    def fetchone_mock(query, params):
        if "FROM metrics_events" in query and "event_type = 'tool_call'" in query:
            return {"cnt": 5}
        if "FROM metrics_events" in query and "event_type = 'rule_eval'" in query:
            return {"cnt": 2, "blocks": 0}
        if (
            "FROM metrics_events" in query
            and "event_type IN ('skill_search', 'skill_invoke')" in query
        ):
            return {"searches": 0, "invocations": 1}
        return None

    db.fetchall.side_effect = fetchall_mock
    db.fetchone.side_effect = fetchone_mock

    client = TestClient(app)
    response = client.get("/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["tasks"]["ready"] == 10
    assert data["tasks"]["in_progress"] == 2
    assert data["sessions"]["active"] == 1
    assert data["sessions"]["by_source"]["cli"]["active"] == 1
    assert data["memory"]["by_type"]["fact"] == 4
    assert data["metrics"]["tools"]["total_calls"] == 5


def test_stats_with_filters(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    db.fetchall.return_value = []
    db.fetchone.return_value = {"cnt": 0, "blocks": 0, "searches": 0, "invocations": 0}

    client = TestClient(app)
    response = client.get("/stats?hours=24&project_id=p1")
    assert response.status_code == 200

    response = client.get("/stats?days=7")
    assert response.status_code == 200


def test_stats_exceptions(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    db.fetchall.side_effect = Exception("DB error")
    db.fetchone.side_effect = Exception("DB error")

    client = TestClient(app)
    response = client.get("/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["tasks"]["ready"] == 0
    assert data["sessions"]["active"] == 0
    assert data["memory"]["count"] == 0
