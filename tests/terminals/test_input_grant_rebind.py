"""Preserved native authority is durable and expires after daemon adoption."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.input_grants import sync_host_input_grant
from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry
from tests.terminals.host_fakes import FakeControlClient, FakeListRow

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


class _HostGrantRuntime:
    def __init__(self, client: FakeControlClient) -> None:
        self.client = client

    async def grant_input(self, terminal: Terminal, attachment_id: str) -> None:
        await self.client.grant_input("ht-1", attachment_id)

    async def revoke_input(self, terminal: Terminal, attachment_id: str | None = None) -> None:
        await self.client.revoke_input("ht-1", attachment_id)


def _native_row(terminals: TerminalManager, project_id: str) -> Terminal:
    terminal_id = str(uuid4())
    pending = terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=MACHINE_ID,
    )
    live = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-1"},
        locator_key=native_locator_key("handoff-epoch", "ht-1"),
        host_epoch="handoff-epoch",
    )
    assert live is not None
    return live


def test_handoff_record_survives_storage_reload_and_clear_checks_attachment(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])

    terminals.record_native_input_handoff(row, "old-attachment")

    reloaded = TerminalManager(temp_db)
    records = reloaded.list_native_input_handoffs(MACHINE_ID)
    assert len(records) == 1
    assert records[0].id == row.id
    assert records[0].locator == row.locator
    assert records[0].process is not None
    assert records[0].process["native_input_handoff"] == {
        "attachment_id": "old-attachment",
        "host_epoch": "handoff-epoch",
        "host_terminal_id": "ht-1",
    }
    reloaded.clear_native_input_handoff(row.id, "other-attachment")
    assert len(reloaded.list_native_input_handoffs(MACHINE_ID)) == 1
    reloaded.clear_native_input_handoff(row.id, "old-attachment")
    assert reloaded.list_native_input_handoffs(MACHINE_ID) == []


def test_handoff_record_rejects_changed_native_identity(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    assert not terminals.record_native_input_handoff(
        replace(row, host_epoch="another-epoch"), "old-attachment"
    )
    assert not terminals.record_native_input_handoff(
        replace(row, locator={"host_terminal_id": "another-terminal"}), "old-attachment"
    )
    assert terminals.list_native_input_handoffs(MACHINE_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("record_error", [False, True])
async def test_durable_holder_transition_clears_on_rebind_and_revokes_on_record_error(
    temp_db: HubDatabase, sample_project: dict[str, Any], record_error: bool
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    client = FakeControlClient(host_epoch="handoff-epoch", authed=True)
    runtime = _HostGrantRuntime(client)
    registry = TerminalLeaseRegistry()

    async def follow_holder(change: HolderChange) -> bool | None:
        return await sync_host_input_grant(
            runtime, change.terminal, change.holder, manager=terminals, reason=change.reason
        )

    registry.set_holder_observer(follow_holder)
    attachment = await registry.attach(row.id, "direct", terminal=row)
    result = await registry.take_control(row.id, attachment.attachment_id)
    assert result.host_input_granted is True
    if record_error:
        with patch.object(
            terminals, "record_native_input_handoff", side_effect=OSError("storage unavailable")
        ):
            await registry.finalize(attachment.attachment_id, "daemon_shutdown")
        assert client.input_grants == {}
        assert terminals.list_native_input_handoffs(MACHINE_ID) == []
        return

    await registry.finalize(attachment.attachment_id, "daemon_shutdown")
    assert client.grant_calls == [("ht-1", attachment.attachment_id)]
    assert client.input_grants == {"ht-1": attachment.attachment_id}
    assert len(terminals.list_native_input_handoffs(MACHINE_ID)) == 1

    rebound = await registry.attach(row.id, "direct", terminal=row)
    result = await registry.take_control(row.id, rebound.attachment_id)
    assert result.host_input_granted is True
    assert client.input_grants == {"ht-1": rebound.attachment_id}
    assert terminals.list_native_input_handoffs(MACHINE_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "new-attachment"])
@pytest.mark.parametrize("first_revoke_fails", [False, True])
async def test_reconcile_bounds_unbound_handoff_without_revoking_a_replacement(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    replacement: str | None,
    first_revoke_fails: bool,
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    terminals.record_native_input_handoff(row, "old-attachment")
    client = FakeControlClient(
        host_epoch="handoff-epoch",
        authed=True,
        terminals=[FakeListRow(terminal_id=row.id, spawn_key=row.spawn_key or row.id)],
        input_grants={"ht-1": "old-attachment"},
    )
    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), health_interval_seconds=3600),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
    )
    host._client = client
    host.host_epoch = "handoff-epoch"
    now = 0.0
    host._monotonic = lambda: now

    with patch("gobby.terminals.host_manager.require_machine_id", return_value=MACHINE_ID):
        await host.reconcile()
        assert client.revoke_calls == []
        assert client.input_grants == {"ht-1": "old-attachment"}
        now = 29.0
        await host.reconcile()
        assert client.revoke_calls == []

        if replacement is not None:
            await client.grant_input("ht-1", replacement)
        now = 30.0
        if first_revoke_fails:
            with patch.object(client, "revoke_input", side_effect=ConnectionError("unavailable")):
                await host.reconcile()
            assert len(terminals.list_native_input_handoffs(MACHINE_ID)) == 1
            assert client.revoke_calls == []
            now = 31.0
        await host.reconcile()

    assert client.revoke_calls == [("ht-1", "old-attachment")]
    assert client.input_grants == ({} if replacement is None else {"ht-1": replacement})
    assert terminals.list_native_input_handoffs(MACHINE_ID) == []


@pytest.mark.asyncio
async def test_health_loop_wakes_for_handoff_deadline(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    terminals.record_native_input_handoff(row, "old-attachment")
    client = FakeControlClient(
        host_epoch="handoff-epoch",
        authed=True,
        terminals=[FakeListRow(terminal_id=row.id, spawn_key=row.spawn_key or row.id)],
        input_grants={"ht-1": "old-attachment"},
    )
    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), health_interval_seconds=3600),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
    )
    host._client = client
    host.host_epoch = "handoff-epoch"
    now = 0.0
    delays: list[float] = []
    host._monotonic = lambda: now

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay
        host._stop_requested = True

    host._sleep = sleep
    with patch("gobby.terminals.host_manager.require_machine_id", return_value=MACHINE_ID):
        await host.reconcile()
        await host._health_loop()

    assert delays == [30.0]
    assert client.revoke_calls == [("ht-1", "old-attachment")]
    assert client.input_grants == {}


@pytest.mark.asyncio
async def test_old_epoch_handoff_never_revokes_the_replacement_host(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    terminals.record_native_input_handoff(row, "old-attachment")
    client = FakeControlClient(
        host_epoch="new-epoch", authed=True, input_grants={"ht-1": "new-attachment"}
    )
    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
    )
    host._client = client
    host.host_epoch = "new-epoch"
    with patch("gobby.terminals.host_manager.require_machine_id", return_value=MACHINE_ID):
        await host.reconcile()

    assert client.revoke_calls == []
    assert client.input_grants == {"ht-1": "new-attachment"}
    assert terminals.list_native_input_handoffs(MACHINE_ID) == []


@pytest.mark.asyncio
async def test_inventory_failure_does_not_delay_guarded_expiry(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    terminals = TerminalManager(temp_db)
    row = _native_row(terminals, sample_project["id"])
    terminals.record_native_input_handoff(row, "old-attachment")
    client = FakeControlClient(
        host_epoch="handoff-epoch", authed=True, input_grants={"ht-1": "old-attachment"}
    )
    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
    )
    host._client = client
    host.host_epoch = "handoff-epoch"
    now = 0.0
    host._monotonic = lambda: now
    with (
        patch("gobby.terminals.host_manager.require_machine_id", return_value=MACHINE_ID),
        patch.object(
            client, "list_terminals", side_effect=ConnectionError("inventory unavailable")
        ),
    ):
        await host.reconcile()
        assert client.revoke_calls == []
        now = 30.0
        await host.reconcile()

    assert client.revoke_calls == [("ht-1", "old-attachment")]
    assert client.input_grants == {}
    assert terminals.list_native_input_handoffs(MACHINE_ID) == []
