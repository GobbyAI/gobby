"""Native backend bind and supervisor liveness contracts."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.bootstrap import BootstrapConfig, FrontDoorConfig
from gobby.runner_backend import BackendBind, backend_bind, watch_supervisor
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("supervised", [False, True])
def test_native_backend_uses_private_loopback_ports(
    monkeypatch: pytest.MonkeyPatch, supervised: bool
) -> None:
    monkeypatch.delenv("GOBBY_SUPERVISOR_FD", raising=False)
    if supervised:
        monkeypatch.setenv("GOBBY_SUPERVISOR_FD", "123")
    bootstrap = BootstrapConfig(
        bind_host="0.0.0.0",
        daemon_port=20000,
        websocket_port=20001,
        front_door=FrontDoorConfig(enabled=False),
    )
    assert backend_bind(bootstrap) == (
        BackendBind("127.0.0.1", 20100, 20101)
        if supervised
        else BackendBind("0.0.0.0", 20000, 20001)
    )


@pytest.mark.skipif(os.name != "posix", reason="Unix pipe readiness contract")
async def test_supervisor_eof_writes_stop_and_requests_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("GOBBY_SUPERVISOR_FD", str(read_fd))
    drained = asyncio.Event()
    with patch("gobby.runner_backend.write_shutdown_intent") as marker:
        task = asyncio.create_task(watch_supervisor(drained.set))
        try:
            await asyncio.sleep(0)
            os.write(write_fd, b"alive")
            await asyncio.sleep(0)
            assert not drained.is_set()
            os.close(write_fd)
            write_fd = -1
            await asyncio.wait_for(task, timeout=2)
            assert drained.is_set()
            marker.assert_called_once_with("supervisor_liveness_eof", ShutdownIntent.STOP)
            with pytest.raises(OSError):
                os.fstat(read_fd)
        finally:
            if write_fd != -1:
                os.close(write_fd)
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.skipif(os.name != "posix", reason="Unix pipe cancellation contract")
async def test_watcher_cancellation_closes_pipe_without_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("GOBBY_SUPERVISOR_FD", str(read_fd))
    drained = asyncio.Event()
    with patch("gobby.runner_backend.write_shutdown_intent") as marker:
        task = asyncio.create_task(watch_supervisor(drained.set))
        try:
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not drained.is_set()
            marker.assert_not_called()
            with pytest.raises(OSError):
                os.fstat(read_fd)
        finally:
            os.close(write_fd)
