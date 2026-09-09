"""Daemon shutdown never drains the gterm host unless explicitly asked (#22002)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle_processes as runner_lifecycle_processes
import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.runner import GobbyRunner
from gobby.shutdown_intent import ShutdownIntent, read_shutdown_intent, write_shutdown_intent
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import write_pidfile
from gobby.utils.machine_id import require_machine_id
from tests.terminals.host_fakes import FakeControlClient, FakeListRow

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
HOST_PID = 4242


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _live_native_row(terminals: TerminalManager, project_id: str, epoch: str) -> Any:
    tid = str(uuid.uuid4())
    pending = terminals.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=tid,
        machine_id=require_machine_id(),
    )
    live = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-1"},
        locator_key=native_locator_key(epoch, "ht-1"),
        host_epoch=epoch,
    )
    assert live is not None
    return live


def _adopted_host(
    tmp_path: Path,
    terminals: TerminalManager | None,
    client: FakeControlClient,
    *,
    terminal_config: TerminalConfig | None = None,
) -> TerminalHostManager:
    write_pidfile(tmp_path, client.host_pid)

    async def connect() -> FakeControlClient:
        return client

    def refuse_spawn() -> Any:
        raise AssertionError("a surviving host must never be replaced")

    return TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=terminal_config or TerminalConfig(),
        terminal_manager=terminals,
        connector=connect,
        spawner=refuse_spawn,
        pid_identity=lambda pid: pid == client.host_pid,
    )


async def _run_shutdown_cleanup(
    runner: SimpleNamespace,
    *,
    intent: ShutdownIntent,
) -> list[set[int]]:
    """Drive the real cleanup tail; return the preserved-pid sets handed to the reaper."""
    reap_calls: list[set[int]] = []

    async def reap(**kwargs: object) -> None:
        reap_calls.append(set(cast(set[int], kwargs["preserved_agent_pids"])))

    with (
        patch.object(
            runner_lifecycle_shutdown,
            "_settle_terminal_delivery_barrier",
            AsyncMock(),
        ),
        patch.object(
            runner_lifecycle_shutdown,
            "_shutdown_database_concurrency",
            AsyncMock(),
        ),
    ):
        await runner_lifecycle_shutdown._run_async_shutdown_cleanup(
            cast(GobbyRunner, runner),
            shutdown_intent=intent,
            reap_remaining_child_processes=reap,
            shutdown_telemetry=MagicMock(),
        )
    return reap_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", list(ShutdownIntent))
async def test_stop_preserves_host_by_default_for_every_intent(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    intent: ShutdownIntent,
) -> None:
    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    row = _live_native_row(terminals, sample_project["id"], epoch)
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=HOST_PID,
        terminals=[FakeListRow(terminal_id=row.id, spawn_key=row.spawn_key or row.id)],
    )
    host = _adopted_host(tmp_path, terminals, client)
    await host.start()
    assert host.adopted is True

    runner = SimpleNamespace(
        terminal_manager=terminals,
        terminal_host_manager=host,
        agent_runner=SimpleNamespace(run_storage=None),
        db_executor=None,
        _drain_terminals_on_shutdown=False,
    )
    reap_calls = await _run_shutdown_cleanup(runner, intent=intent)

    assert client.shutdown_calls == [], f"{intent} must not drain the host"
    assert client.kill_calls == []
    assert host.preserved_host_pid() == HOST_PID
    assert reap_calls == [{HOST_PID}], "the reaper must be told to keep the host tree"
    loaded = terminals.get(row.id)
    assert loaded is not None
    assert loaded.state == "live"


@pytest.mark.asyncio
async def test_explicit_opt_in_drains_host(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    _live_native_row(terminals, sample_project["id"], epoch)

    # Per-shutdown opt-in (`gobby stop --terminals` / `shutdown?terminals=true`).
    client = FakeControlClient(host_epoch=epoch, host_pid=HOST_PID)
    host = _adopted_host(tmp_path, terminals, client)
    await host.start()
    runner = SimpleNamespace(
        terminal_manager=terminals,
        terminal_host_manager=host,
        agent_runner=SimpleNamespace(run_storage=None),
        db_executor=None,
        _drain_terminals_on_shutdown=True,
    )
    reap_calls = await _run_shutdown_cleanup(runner, intent=ShutdownIntent.STOP)
    assert client.shutdown_calls == [200]
    assert host.preserved_host_pid() is None
    assert reap_calls == [set()], "a drained host is not held back from the reaper"

    # Config opt-in (`terminals.stop_host_on_shutdown = true`) drains on a plain stop.
    configured = FakeControlClient(host_epoch=epoch, host_pid=HOST_PID)
    host2 = _adopted_host(
        tmp_path,
        terminals,
        configured,
        terminal_config=TerminalConfig(stop_host_on_shutdown=True),
    )
    await host2.start()
    await host2.stop()
    assert configured.shutdown_calls == [200]
    assert host2.host_pid is None

    # Direct opt-in on the supervisor API.
    direct = FakeControlClient(host_epoch=epoch, host_pid=HOST_PID)
    host3 = _adopted_host(tmp_path, terminals, direct)
    await host3.start()
    await host3.stop()
    assert direct.shutdown_calls == []
    await host3.stop(drain_host=True)
    assert direct.shutdown_calls == [200]


def test_shutdown_marker_carries_drain_opt_in(tmp_path: Path) -> None:
    """`service_restart`/`stop` markers read back as preserve unless drain was requested."""
    write_shutdown_intent("service_restart", ShutdownIntent.RESTART, home=tmp_path)
    plain = read_shutdown_intent(home=tmp_path, consume=False)
    assert plain.intent is ShutdownIntent.RESTART
    assert plain.drain_terminals is False

    write_shutdown_intent(
        "cli_stop",
        ShutdownIntent.STOP,
        home=tmp_path,
        details={"drain_terminals": True},
    )
    draining = read_shutdown_intent(home=tmp_path, consume=False)
    assert draining.drain_terminals is True


def test_request_shutdown_records_drain_opt_in() -> None:
    runner = GobbyRunner.__new__(GobbyRunner)
    runner._shutdown_requested = False
    runner._shutdown_intent = ShutdownIntent.STOP
    runner._drain_terminals_on_shutdown = False

    runner.request_shutdown(ShutdownIntent.RESTART)
    assert runner._drain_terminals_on_shutdown is False

    runner.request_shutdown(ShutdownIntent.STOP, drain_terminals=True)
    assert runner._drain_terminals_on_shutdown is True
    # A later plain request never un-asks for the drain.
    runner.request_shutdown(ShutdownIntent.STOP)
    assert runner._drain_terminals_on_shutdown is True


def test_host_preserve_pids_falls_back_to_identity_checked_pidfile(tmp_path: Path) -> None:
    """The reaper keeps the host even after `stop()` cleared `host_pid`."""
    client = FakeControlClient(host_pid=HOST_PID)
    host = _adopted_host(tmp_path, None, client)
    host.host_pid = None
    runner = SimpleNamespace(terminal_host_manager=host)
    assert runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner)) == {HOST_PID}

    write_pidfile(tmp_path, 9999)
    assert runner_lifecycle_processes._host_preserve_pids(cast(GobbyRunner, runner)) == set()
