"""Host PID preservation across restart vs drain on full stop (plan 3.1.8)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.runner_lifecycle_processes import _preserved_agent_terminal_pids
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
from tests.terminals.host_fakes import FakeControlClient

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_restart_preserves_host_stop_drains_it(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.config.terminals import TerminalConfig
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    pending = terminals.create_pending(
        terminal_id=tid,
        project_id=sample_project["id"],
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
    tmux_pending = terminals.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="tmux",
        ownership="gobby",
        spawn_key="gobby-tmux-row",
        machine_id=require_machine_id(),
    )

    client = FakeControlClient(host_epoch=epoch, host_pid=4242)
    write_pidfile(tmp_path, 4242)

    async def connect() -> FakeControlClient:
        return client

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        connector=connect,
        spawner=lambda: None,
        pid_identity=lambda _pid: True,
    )
    await host.start()

    runner = MagicMock()
    runner.terminal_manager = terminals
    runner.terminal_host_manager = host
    runner.agent_runner = MagicMock(run_storage=None)
    runner.db_executor = None

    preserved = await _preserved_agent_terminal_pids(cast(Any, runner))
    assert preserved is not None
    assert 4242 in preserved

    await host.stop(preserve_host=True)
    assert client.shutdown_calls == []

    await host.stop(preserve_host=False)
    assert client.shutdown_calls

    # Migrated tmux branch reads pending|live rows, not run.tmux_session_name.
    assert tmux_pending.session_name is None
    assert tmux_pending.spawn_key == "gobby-tmux-row"
