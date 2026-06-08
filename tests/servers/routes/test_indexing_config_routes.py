from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


@pytest.fixture
def client(temp_db: HubDatabase) -> TestClient:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        task_manager=LocalTaskManager(temp_db),
    )
    return TestClient(server.app)


def test_config_values_expose_indexing_default(client: TestClient) -> None:
    response = client.get("/api/config/values")

    assert response.status_code == 200
    assert response.json()["values"]["indexing"]["respect_gitignore"] is True


def test_config_values_round_trip_indexing_respect_gitignore(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    response = client.put(
        "/api/config/values",
        json={"values": {"indexing": {"respect_gitignore": False}}},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert ConfigStore(temp_db).get("indexing.respect_gitignore") is False

    values = client.get("/api/config/values").json()["values"]
    assert values["indexing"]["respect_gitignore"] is False
