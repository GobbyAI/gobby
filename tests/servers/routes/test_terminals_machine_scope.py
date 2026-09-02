"""Machine-scoping checks for the terminal REST inventory."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.servers.test_terminals_routes import _client
from tests.storage.test_terminals import (
    LOCAL_MACHINE_ID,
    _manager,
    _tmux_key,
    _tmux_locator,
)

pytestmark = pytest.mark.unit

FOREIGN_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_terminal_list_and_get_exclude_foreign_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    local_locator = _tmux_locator(pane_id="%local")
    local = manager.upsert_external(
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        backend="tmux",
        locator=local_locator,
        locator_key=_tmux_key(local_locator),
        session_name="local",
        window_id="@1",
        title="local",
    )
    foreign_locator = _tmux_locator(pane_id="%foreign")
    foreign = manager.upsert_external(
        machine_id=FOREIGN_MACHINE_ID,
        project_id=sample_project["id"],
        backend="tmux",
        locator=foreign_locator,
        locator_key=_tmux_key(foreign_locator),
        session_name="foreign",
        window_id="@2",
        title="foreign",
    )

    with _client(temp_db) as client:
        listing = client.get("/api/terminals", params={"project_id": sample_project["id"]})
        assert listing.status_code == 200
        assert {item["id"] for item in listing.json()["items"]} == {local.id}

        local_detail = client.get(f"/api/terminals/{local.id}")
        assert local_detail.status_code == 200
        assert local_detail.json()["id"] == local.id

        foreign_detail = client.get(f"/api/terminals/{foreign.id}")
        assert foreign_detail.status_code == 404
