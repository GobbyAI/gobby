"""Restart reap uses the shutdown-start agent listing and names what a skipped reap leaves running."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.runner_background_services as background_services
import gobby.runner_lifecycle_processes as runner_lifecycle_processes
import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
from gobby.runner import GobbyRunner
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


def _runner() -> SimpleNamespace:
    return SimpleNamespace(
        agent_runner=SimpleNamespace(run_storage=object()),
        db_executor=None,
        terminal_host_manager=None,
        http_server=SimpleNamespace(services=SimpleNamespace(shutdown_in_progress=False)),
    )


@pytest.fixture
def quiet_shutdown_tail(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(background_services, "shutdown_background_services", MagicMock())
    monkeypatch.setattr(
        runner_lifecycle_shutdown, "shutdown_cli_text_generation_calls", AsyncMock()
    )
    yield


@pytest.mark.asyncio
async def test_reap_keeps_the_shutdown_start_listing_when_the_hub_stalls_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch, quiet_shutdown_tail: None
) -> None:
    """A listing taken at shutdown start still gates the reap after the hub stalls."""
    release = threading.Event()
    runner = _runner()
    services = runner.http_server.services

    started = threading.Event()

    def listing(_runner: object, *, include_fenced: bool) -> list[SimpleNamespace]:
        if services.shutdown_in_progress:
            release.wait(timeout=30)  # the hub stalls once the deadline path starts
            return []
        started.set()
        return [SimpleNamespace(pid=4242)]

    monkeypatch.setattr(runner_lifecycle_processes, "_list_active_agent_runs_once", listing)
    runner_lifecycle_processes.start_preserved_agent_listing(cast(GobbyRunner, runner))
    await asyncio.to_thread(started.wait, 10)  # the shutdown-start listing has run
    services.shutdown_in_progress = True

    reaped: list[set[int] | None] = []

    async def reap(**kwargs: Any) -> None:
        reaped.append(kwargs["preserved_agent_pids"])

    try:
        await runner_lifecycle_shutdown._shutdown_child_processes(
            cast(GobbyRunner, runner),
            shutdown_intent=ShutdownIntent.RESTART,
            reap_remaining_child_processes=reap,
        )
    finally:
        release.set()

    assert reaped == [{4242}]


@pytest.mark.asyncio
async def test_skipped_reap_names_each_child_left_running(
    monkeypatch: pytest.MonkeyPatch,
    quiet_shutdown_tail: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both listings fail, the reap is skipped, logs each child PID, and kills nothing."""

    def failing_listing(_runner: object, *, include_fenced: bool) -> list[SimpleNamespace]:
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(runner_lifecycle_processes, "_list_active_agent_runs_once", failing_listing)
    runner = _runner()
    runner_lifecycle_processes.start_preserved_agent_listing(cast(GobbyRunner, runner))

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    reap = AsyncMock()
    try:
        with caplog.at_level(logging.WARNING, logger="gobby.runner_lifecycle"):
            await runner_lifecycle_shutdown._shutdown_child_processes(
                cast(GobbyRunner, runner),
                shutdown_intent=ShutdownIntent.RESTART,
                reap_remaining_child_processes=reap,
            )
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)

    reap.assert_not_awaited()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(child.pid) in message and "left unreaped" in message for message in warnings)
