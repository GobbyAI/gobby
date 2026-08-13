from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from gobby.config.runtime import ConfigRuntime
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def client(hub_db: HubDatabase) -> Iterator[TestClient]:
    runtime = ConfigRuntime(ConfigRepository(hub_db))
    startup = asyncio.run(runtime.start())
    server = create_http_server(
        config=startup.active,
        database=hub_db,
        task_manager=LocalTaskManager(hub_db),
    )
    server.services.config_runtime = runtime
    test_client = TestClient(server.app)
    yield test_client
    test_client.close()
    asyncio.run(runtime.close())


def test_config_values_expose_indexing_default(client: TestClient) -> None:
    response = client.get("/api/config/values")

    assert response.status_code == 200
    assert response.json()["desired"]["indexing"]["respect_gitignore"] is True
    assert response.json()["desired"]["indexing"]["extra_excludes"] == []


def test_config_values_round_trip_indexing_respect_gitignore(
    client: TestClient,
    hub_db: HubDatabase,
) -> None:
    response = client.patch(
        "/api/config/values",
        json={
            "expected_revision": 0,
            "values": {"indexing": {"respect_gitignore": False}},
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    snapshot = ConfigRepository(hub_db).read(resolve_secrets=False)
    assert snapshot.overrides["indexing.respect_gitignore"] is False

    values = client.get("/api/config/values").json()["desired"]
    assert values["indexing"]["respect_gitignore"] is False


def test_config_values_round_trip_indexing_extra_excludes(
    client: TestClient,
    hub_db: HubDatabase,
) -> None:
    patterns = ["generated", "*.snapshot"]

    response = client.patch(
        "/api/config/values",
        json={
            "expected_revision": 0,
            "values": {"indexing": {"extra_excludes": patterns}},
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    snapshot = ConfigRepository(hub_db).read(resolve_secrets=False)
    assert snapshot.overrides["indexing.extra_excludes"] == patterns

    values = client.get("/api/config/values").json()["desired"]
    assert values["indexing"]["extra_excludes"] == patterns
