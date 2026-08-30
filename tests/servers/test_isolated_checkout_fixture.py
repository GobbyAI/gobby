"""Servers fixture uses the isolated-machine checkout helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.utils.project_context import get_project_context


def test_servers_test_project_uses_isolated_machine_helper(
    test_project: dict[str, Any],
    temp_db: HubDatabase,
) -> None:
    assert "repo_path" not in test_project
    row = temp_db.fetchone(
        "SELECT machine_id, root_path FROM project_checkouts WHERE project_id = %s",
        (test_project["id"],),
    )
    assert row is not None
    checkout = LocalProjectCheckoutManager(temp_db).get(str(row["machine_id"]), test_project["id"])
    assert checkout is not None
    marker = get_project_context(Path(checkout.root_path))
    assert marker is not None
    assert marker["id"] == test_project["id"]
    machine = temp_db.fetchone("SELECT 1 FROM machines WHERE id = %s", (checkout.machine_id,))
    assert machine is not None
