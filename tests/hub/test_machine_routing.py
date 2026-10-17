"""Remote checkout metadata must never become local filesystem authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gobby.identity import hash_password
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.users import LocalUserManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def remote_machine(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    temp_db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (machine_id, sample_project["id"], f"/node-only/{machine_id}/checkout"),
    )
    return machine_id


async def test_default_and_explicit_targets(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.hub.machine_routing as module

    client = AsyncMock()
    client.connected_machine_ids.return_value = {remote_machine}
    monkeypatch.setattr(module, "node_command_client", lambda: client)
    local_id = require_machine_id()
    assert module.is_local_machine(None)
    assert module.is_local_machine(local_id)
    assert not module.is_local_machine(remote_machine)
    local = await module.resolve_target_machine(
        temp_db, project_id=sample_project["id"], requested=None, origin_machine_id=local_id
    )
    assert (local.machine_id, local.local, local.checkout_root) == (local_id, True, None)
    client.connected_machine_ids.assert_not_awaited()
    default = await module.resolve_target_machine(
        temp_db, project_id=sample_project["id"], requested=None, origin_machine_id=remote_machine
    )
    assert (default.machine_id, default.local) == (remote_machine, False)
    node = LocalMachineManager(temp_db).get(remote_machine)
    assert node is not None
    explicit = await module.resolve_target_machine(
        temp_db,
        project_id=sample_project["id"],
        requested=str(node.ref),
        origin_machine_id=local_id,
    )
    assert explicit == default


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("checkout", "checkout_unresolved"),
        ("offline", "node_offline"),
        ("owner", "machine_not_owned"),
        ("unknown", "machine_not_found"),
        ("unknown_ref", "machine_not_found"),
        ("invalid", "machine_not_found"),
    ],
)
async def test_ineligible_targets_never_fall_back(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    code: str,
) -> None:
    import gobby.hub.machine_routing as module
    from gobby.hub.node_client import NodeCommandError

    client = AsyncMock()
    client.connected_machine_ids.return_value = set() if case == "offline" else {remote_machine}
    monkeypatch.setattr(module, "node_command_client", lambda: client)
    target = remote_machine
    if case == "checkout":
        temp_db.execute("DELETE FROM project_checkouts WHERE machine_id = %s", (remote_machine,))
    elif case == "owner":
        user = LocalUserManager(temp_db).create(
            name="Other owner", email="other@example.test", password_hash=hash_password("test-only")
        )
        temp_db.execute(
            "UPDATE machines SET owner_user_id = %s WHERE id = %s", (user.id, remote_machine)
        )
    elif case == "unknown":
        target = str(uuid4())
    elif case == "unknown_ref":
        target = "999999"
    elif case == "invalid":
        target = "not-a-machine"
    with pytest.raises(NodeCommandError) as caught:
        await module.resolve_target_machine(
            temp_db,
            project_id=sample_project["id"],
            requested=target,
            origin_machine_id=require_machine_id(),
        )
    assert caught.value.code == code
    assert caught.value.stage == "none"


async def test_remote_checkout_read_keeps_local_guard(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.hub.machine_routing as module

    client = AsyncMock()
    client.connected_machine_ids.return_value = {remote_machine}
    monkeypatch.setattr(module, "node_command_client", lambda: client)
    remote_root = f"/node-only/{remote_machine}/checkout"
    assert not Path(remote_root).exists()
    target = await module.resolve_target_machine(
        temp_db,
        project_id=sample_project["id"],
        requested=remote_machine,
        origin_machine_id=require_machine_id(),
    )
    assert target.checkout_root == remote_root
    assert target.machine_id == remote_machine
    with pytest.raises(MachineOwnershipMismatchError):
        require_root(temp_db, sample_project["id"], remote_machine)


async def test_discovery_failure_does_not_fall_back(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.hub.machine_routing as module
    from gobby.hub.node_client import NodeCommandError

    client = AsyncMock()
    client.connected_machine_ids.side_effect = NodeCommandError(
        "node_routing_unavailable", "down", "none"
    )
    monkeypatch.setattr(module, "node_command_client", lambda: client)
    with pytest.raises(NodeCommandError) as caught:
        await module.resolve_target_machine(
            temp_db,
            project_id=sample_project["id"],
            requested=remote_machine,
            origin_machine_id=require_machine_id(),
        )
    assert caught.value.code == "node_routing_unavailable"
    assert caught.value.stage == "none"
