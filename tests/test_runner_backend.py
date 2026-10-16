"""Native backend bind and supervisor liveness contracts."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.config.bootstrap import BootstrapConfig, FrontDoorConfig, FrontDoorRouteBackend
from gobby.runner_backend import (
    BackendBind,
    backend_bind,
    require_latched_terminal_route,
    supervisor_write_token,
    watch_supervisor,
)
from gobby.runner_init import StartRefusal
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


def test_windows_keeps_python_public_bind() -> None:
    bootstrap = BootstrapConfig(bind_host="0.0.0.0", daemon_port=20000, websocket_port=20001)
    with patch("gobby.runner_backend.os", SimpleNamespace(name="nt", environ={})):
        assert backend_bind(bootstrap) == BackendBind("0.0.0.0", 20000, 20001)


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


@pytest.mark.parametrize(
    ("latched", "configured", "refused"),
    [
        (None, {"terminal_ws": "native"}, False),
        ("proxy", {}, False),
        ("native", {"terminal_ws": "native"}, False),
        ("compare", {"terminal_ws": "compare"}, False),
        ("proxy", {"terminal_ws": "native"}, True),
        ("native", {}, True),
        ("native", {"terminal_ws": "compare"}, True),
    ],
)
def test_terminal_route_flip_requires_a_gdaemon_restart(
    monkeypatch: pytest.MonkeyPatch,
    latched: str | None,
    configured: dict[str, FrontDoorRouteBackend],
    refused: bool,
) -> None:
    monkeypatch.delenv("GOBBY_TERMINAL_ROUTE", raising=False)
    if latched is not None:
        monkeypatch.setenv("GOBBY_TERMINAL_ROUTE", latched)
    bootstrap = BootstrapConfig(front_door=FrontDoorConfig(routes=configured))
    if refused:
        with pytest.raises(StartRefusal, match="restart gdaemon"):
            require_latched_terminal_route(bootstrap)
    else:
        require_latched_terminal_route(bootstrap)


@pytest.mark.skipif(os.name != "posix", reason="Unix pipe readiness contract")
async def test_write_token_travels_only_over_the_supervisor_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_SUPERVISOR_FD", raising=False)
    await watch_supervisor(lambda: None)
    assert supervisor_write_token() is None

    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("GOBBY_SUPERVISOR_FD", str(read_fd))
    drained = asyncio.Event()
    with patch("gobby.runner_backend.write_shutdown_intent"):
        task = asyncio.create_task(watch_supervisor(drained.set))
        try:
            await asyncio.sleep(0)
            os.write(write_fd, b"start-")
            await asyncio.sleep(0)
            assert supervisor_write_token() is None
            os.write(write_fd, b"token\n")
            await asyncio.sleep(0)
            assert supervisor_write_token() == "start-token"
            assert not any("start-token" in value for value in os.environ.values())
            assert not any("start-token" in arg for arg in sys.argv)
            assert not drained.is_set()
            os.close(write_fd)
            write_fd = -1
            await asyncio.wait_for(task, timeout=2)
            assert drained.is_set()
            assert supervisor_write_token() is None
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
