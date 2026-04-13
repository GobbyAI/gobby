import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.admin._usage import register_usage_routes


@pytest.fixture
def test_app():
    app = FastAPI()
    router = APIRouter()
    server_mock = MagicMock()
    server_mock.services.database = MagicMock()

    register_usage_routes(router, server_mock)
    app.include_router(router)

    return app, server_mock


def test_usage_no_filters(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    def fetchall_mock(query, params):
        if "GROUP BY source" in query:
            return [
                {
                    "source": "cli",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 10,
                    "cache_creation_tokens": 5,
                    "session_count": 2,
                }
            ]
        if "GROUP BY model" in query:
            return [
                {
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 10,
                    "cache_creation_tokens": 5,
                    "session_count": 2,
                }
            ]
        # totals
        return [
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 5,
                "session_count": 2,
            }
        ]

    db.fetchall.side_effect = fetchall_mock

    client = TestClient(app)
    response = client.get("/usage")

    assert response.status_code == 200
    data = response.json()

    assert data["totals"]["input_tokens"] == 100
    assert data["by_source"]["cli"]["output_tokens"] == 50
    assert data["by_model"]["gpt-4"]["session_count"] == 2


def test_usage_with_filters_and_fallback(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    def fetchall_mock(query, params):
        if "GROUP BY source" in query:
            return [
                {
                    "source": None,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "session_count": 1,
                }
            ]
        if "GROUP BY model" in query:
            return [
                {
                    "model": None,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "session_count": 1,
                }
            ]
        # totals empty
        return []

    db.fetchall.side_effect = fetchall_mock

    client = TestClient(app)
    response = client.get("/usage?hours=24&project_id=p1")
    assert response.status_code == 200
    data = response.json()
    assert data["by_source"]["unknown"]["input_tokens"] == 10
    assert data["by_model"]["unknown"]["output_tokens"] == 5
    assert data["totals"]["input_tokens"] == 0  # because empty rows returned


def test_usage_exceptions(test_app):
    app, server_mock = test_app
    db = server_mock.services.database

    db.fetchall.side_effect = sqlite3.Error("DB error")

    client = TestClient(app)
    response = client.get("/usage")

    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["input_tokens"] == 0
    assert data["by_source"] == {}
    assert data["by_model"] == {}
