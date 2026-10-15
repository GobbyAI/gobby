"""Prompt route writes reach long-lived Python loaders without a restart."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.prompts.loader import PromptLoader
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import TEST_FRONT_DOOR_IDENTITY, create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def client(hub_db: HubDatabase) -> TestClient:
    sync_bundled_prompts(hub_db)
    server: Any = create_http_server(
        config=DaemonConfig(),
        database=hub_db,
        task_manager=LocalTaskManager(hub_db),
    )
    return TestClient(server.app, headers=TEST_FRONT_DOOR_IDENTITY)


def test_route_override_reaches_a_long_lived_loader(
    client: TestClient,
    hub_db: HubDatabase,
) -> None:
    path = client.get("/api/config/prompts?limit=1").json()["prompts"][0]["path"]
    loader = PromptLoader(db=hub_db)
    bundled = loader.load(path).content

    response = client.put(f"/api/config/prompts/{path}", json={"content": "Overridden body"})

    assert response.status_code == 200
    assert bundled != "Overridden body"
    assert loader.load(path).content == "Overridden body"
