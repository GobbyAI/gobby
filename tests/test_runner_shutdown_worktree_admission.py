"""Worktree delete admission closes when daemon shutdown starts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn

from gobby import runner_lifecycle_shutdown
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner
from gobby.worktrees.executor import DestructiveBoundary, WorktreeDeleteExecutor


def _shutdown_runner(executor: WorktreeDeleteExecutor) -> SimpleNamespace:
    return SimpleNamespace(
        bootstrap_config=BootstrapConfig(),
        agent_runner=None,
        http_server=SimpleNamespace(
            services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
            _terminate_streamable_http_sessions=AsyncMock(),
        ),
        wake_dispatcher=SimpleNamespace(
            reconcile_restart_active_sessions=AsyncMock(return_value=[])
        ),
        lifecycle_manager=SimpleNamespace(stop=AsyncMock()),
        agent_lifecycle_monitor=None,
        cron_scheduler=None,
        message_processor=None,
        communications_manager=None,
        config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
        startup_config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
        memory_manager=None,
        vector_store=None,
        mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
        database=SimpleNamespace(close=MagicMock()),
        worktree_delete_executor=executor,
    )


async def _completed_server() -> None:
    return None


@pytest.mark.asyncio
async def test_worktree_delete_admission_closes_when_shutdown_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown start refuses new deletes and revokes queued ones; an active delete runs on."""
    executor = WorktreeDeleteExecutor(max_workers=1)
    observed: dict[str, str] = {}
    release = asyncio.Event()
    active_started = asyncio.Event()

    async def active_operation(_boundary: DestructiveBoundary) -> str:
        active_started.set()
        await release.wait()
        return "active"

    async def queued_operation(_boundary: DestructiveBoundary) -> str:
        observed["queued_ran"] = "yes"
        return "queued"

    async def graceful_probe(*_args: object, **_kwargs: object) -> None:
        async def late_operation(_boundary: DestructiveBoundary) -> str:
            observed["late_ran"] = "yes"
            return "late"

        try:
            await executor.run_delete(late_operation)
        except RuntimeError as exc:
            observed["refused"] = str(exc)
        # Settle the active delete inside shutdown, so the executor join returns at once.
        release.set()
        assert await active == "active"

    monkeypatch.setattr(
        runner_lifecycle_shutdown, "_run_graceful_shutdown_sequence", graceful_probe
    )
    active = asyncio.create_task(executor.run_delete(active_operation))
    await active_started.wait()
    queued = asyncio.create_task(executor.run_delete(queued_operation))
    await asyncio.sleep(0)  # yields once so the queued delete registers behind the active one
    try:
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            cast(GobbyRunner, _shutdown_runner(executor)),
            cast(uvicorn.Server, SimpleNamespace(should_exit=False)),
            asyncio.create_task(_completed_server()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )
        assert queued.cancelled()
    finally:
        release.set()
        await active

    assert observed == {"refused": "WorktreeDeleteExecutor is shut down"}
