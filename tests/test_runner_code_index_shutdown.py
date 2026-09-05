"""Code-index consumers settle before their daemon config endpoint shuts down."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.code_index.models import IndexedFile, IndexedProject
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_breaker import SyncCircuitBreaker
from gobby.code_index.sync_worker import _sync_pass, sync_worker_loop
from gobby.config.code_index import CodeIndexConfig
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_shutdown import shutdown_daemon_services
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("intent", [ShutdownIntent.STOP, ShutdownIntent.RESTART])
async def test_shutdown_drains_code_index_before_config_becomes_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, intent: ShutdownIntent
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def example(): pass\n")
    pending_file = IndexedFile(
        id="pending-file",
        project_id="project",
        file_path=source.name,
        language="python",
        content_hash="version-one",
        symbol_count=1,
        vectors_synced=False,
        graph_synced=True,
    )
    storage = MagicMock(spec=CodeIndexStorage)
    storage.list_indexed_projects.return_value = [
        IndexedProject(id="project", root_path=str(tmp_path), total_files=1, total_symbols=1)
    ]
    storage.get_pending_sync_files.return_value = [pending_file]
    storage.get_file.return_value = pending_file
    services = SimpleNamespace(
        startup_ready=True, shutdown_in_progress=False, http_admission_closed=False
    )
    release_cleanup = asyncio.Event()
    entered = {name: asyncio.Event() for name in ("maintenance", "projection")}
    cancelling = {name: asyncio.Event() for name in entered}
    cleanup_states: list[tuple[str, bool, bool, bool]] = []

    async def block_until_cancelled(name: str) -> None:
        entered[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling[name].set()
            await release_cleanup.wait()
            cleanup_states.append(
                (
                    name,
                    services.startup_ready,
                    services.shutdown_in_progress,
                    services.http_admission_closed,
                )
            )

    async def sync_vector(*_args: object, **_kwargs: object) -> None:
        await block_until_cancelled("projection")

    gateway = MagicMock(spec=GcodeGateway)
    gateway.vector_sync_file = AsyncMock(side_effect=sync_vector)
    context = MagicMock(spec=CodeIndexContext)
    context.gcode_gateway = gateway
    context.daemon_config_breaker = SyncCircuitBreaker(
        name="test", probe_target="daemon config", operation="sync"
    )
    config = CodeIndexConfig(embedding_enabled=True, graph_enabled=False)
    runner = MagicMock(spec=GobbyRunner)
    runner._shutdown_intent = intent
    runner._code_index_shutdown = asyncio.Event()
    runner._sync_worker_shutdown = asyncio.Event()
    runner._code_index_task = asyncio.create_task(block_until_cancelled("maintenance"))
    runner._sync_worker_task = asyncio.create_task(
        sync_worker_loop(storage, context, config, runner._sync_worker_shutdown)
    )
    runner.http_server = SimpleNamespace(
        services=services,
        _hook_manager=None,
        _terminate_streamable_http_sessions=AsyncMock(),
    )
    runner.lifecycle_manager = SimpleNamespace(stop=AsyncMock())
    runner.agent_lifecycle_monitor = None
    runner.cron_scheduler = None
    runner.message_processor = None
    runner.communications_manager = None
    runner.startup_config = SimpleNamespace(ui=SimpleNamespace(enabled=False))
    runner.memory_manager = None
    runner.vector_store = None
    runner.mcp_proxy = SimpleNamespace(disconnect_all=AsyncMock())
    runner.database = MagicMock()
    server = uvicorn.Server(uvicorn.Config(app=MagicMock()))

    async def server_done() -> None:
        return None

    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
    shutdown = asyncio.create_task(
        shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(server_done()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )
    )
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cancelling.values())), 1)
        assert services.startup_ready is True
        assert services.shutdown_in_progress is False
        assert services.http_admission_closed is False
        assert runner._code_index_shutdown.is_set()
        assert runner._sync_worker_shutdown.is_set()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(shutdown, 3)

    assert sorted(cleanup_states) == [
        ("maintenance", True, False, False),
        ("projection", True, False, False),
    ]
    assert runner._code_index_task.cancelled()
    assert runner._sync_worker_task.cancelled()
    assert services.startup_ready is False
    assert services.shutdown_in_progress is True
    assert services.http_admission_closed is True
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_graph_synced.assert_not_called()
    assert pending_file.vectors_synced is False
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    # The next worker can retry the same pending version after restart.
    gateway.vector_sync_file = AsyncMock()
    await _sync_pass(storage, gateway, config, batch_size=1)
    storage.mark_vectors_synced.assert_called_once_with(pending_file.id, pending_file.content_hash)
