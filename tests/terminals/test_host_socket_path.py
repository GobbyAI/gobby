"""Socket pathname admission on the default Python host lifecycle."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.terminals import TerminalManager
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.host_manager import HostStartTimeoutError, TerminalHostManager
from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
from gobby.terminals.web_spawn import spawn_web_terminal

pytestmark = pytest.mark.unit


def _host(socket_dir: Path) -> TerminalHostManager:
    return TerminalHostManager(
        config=TerminalHostConfig(enabled=True, socket_dir=str(socket_dir)),
        terminal_config=TerminalConfig(),
        spawner=Mock(return_value=SimpleNamespace(pid=0)),
    )


def _paths() -> tuple[Path, Path, int]:
    limit = 107 if sys.platform.startswith("linux") else 103
    directory_length = limit - len("/gterm-control.sock")
    return Path("x" * directory_length), Path("x" * (directory_length - 1) + "é"), limit


def _assert_diagnostic(message: str, socket_dir: Path, limit: int) -> None:
    path = socket_dir / "gterm-control.sock"
    assert str(path) in message
    assert f"{len(os.fsencode(path))} bytes" in message
    assert f"limit {limit} bytes" in message
    assert "terminal_host.socket_dir" in message


@pytest.mark.asyncio
async def test_socket_preflight_accepts_limit_then_rejects_over_limit_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    at_limit, over_limit, limit = _paths()
    client = AsyncMock()
    host = _host(at_limit)
    monkeypatch.setattr(host, "_wait_for_client", AsyncMock(return_value=client))
    connected, _hello, _ping = await host._spawn_candidate()
    assert connected is client

    invalid = _host(over_limit)
    spawn = Mock(return_value=SimpleNamespace(pid=0))
    wait = AsyncMock(return_value=client)
    monkeypatch.setattr(invalid, "_spawner", spawn)
    monkeypatch.setattr(invalid, "_wait_for_client", wait)
    with pytest.raises(HostUnavailableError) as raised:
        await invalid._spawn_candidate()
    _assert_diagnostic(raised.value.detail or "", over_limit, limit)
    spawn.assert_not_called()
    wait.assert_not_awaited()
    assert not over_limit.exists(), "invalid configuration must not write host state"


@pytest.mark.asyncio
async def test_default_host_start_and_web_spawn_report_path_without_ready_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _at_limit, over_limit, limit = _paths()
    host = _host(over_limit)
    wait = AsyncMock(side_effect=HostStartTimeoutError("readiness wait was entered"))
    monkeypatch.setattr(host, "_wait_for_client", wait)
    monkeypatch.setattr(host, "_connector", AsyncMock(side_effect=ConnectionRefusedError))
    monkeypatch.setattr(host, "_arm_health", Mock())
    await host.start()
    wait.assert_not_awaited()
    assert not host.running
    _assert_diagnostic(host.last_error or "", over_limit, limit)

    manager = Mock(spec=TerminalManager)
    runtime = NativeTerminalRuntime(HostManagerControl(host))
    result = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id="test-project",
        session_id=None,
        rows=24,
        cols=80,
        cwd=None,
        command=["/bin/sh"],
    )
    assert not result.success
    assert result.terminal_id is None
    assert result.error == "host_unreachable"
    _assert_diagnostic(result.error_detail or "", over_limit, limit)
    manager.create_pending.assert_not_called()
