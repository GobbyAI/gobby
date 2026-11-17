"""Machine-scoping checks for the terminal REST inventory."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.servers.test_terminals_routes import _client
from tests.storage.test_terminals import (
    _EPOCH,
    LOCAL_MACHINE_ID,
    _manager,
    _native_key,
    _native_locator,
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
    local_locator = _native_locator(host_terminal_id="local-host")
    local_pending = manager.create_pending(
        terminal_id="41000000-0000-4000-8000-000000000001",
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key="local-spawn",
    )
    local = manager.promote_to_live(
        local_pending.id,
        locator=local_locator,
        locator_key=_native_key(local_locator),
        host_epoch=_EPOCH,
        title="local",
    )
    assert local is not None
    foreign_locator = _native_locator(host_terminal_id="foreign-host")
    foreign_pending = manager.create_pending(
        terminal_id="41000000-0000-4000-8000-000000000002",
        machine_id=FOREIGN_MACHINE_ID,
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key="foreign-spawn",
    )
    foreign = manager.promote_to_live(
        foreign_pending.id,
        locator=foreign_locator,
        locator_key=_native_key(foreign_locator),
        host_epoch=_EPOCH,
        title="foreign",
    )
    assert foreign is not None

    with _client(temp_db) as client:
        listing = client.get("/api/terminals", params={"project_id": sample_project["id"]})
        assert listing.status_code == 200
        assert {item["id"] for item in listing.json()["items"]} == {local.id}

        local_detail = client.get(f"/api/terminals/{local.id}")
        assert local_detail.status_code == 200
        assert local_detail.json()["id"] == local.id

        foreign_detail = client.get(f"/api/terminals/{foreign.id}")
        assert foreign_detail.status_code == 404
