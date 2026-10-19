"""Listener and socket inspection survives a process the OS refuses to inspect (#23650)."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import psutil
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.utils_process import _kill_port_holder, kill_all_gobby_daemons
from gobby.cli.utils_ui import _find_gobby_ui_port_holder
from gobby.config.app import DaemonConfig
from gobby.runner_pid_file import ProbeState, SingletonProbe
from gobby.utils.status import RichStatusProbe

pytestmark = pytest.mark.unit


class _SandboxedProcess:
    """A process whose socket table the sandbox refuses, as macOS proc_pidinfo does."""

    pid = 4321

    def __init__(self, cmdline: list[str] | None = None) -> None:
        self._cmdline = cmdline or ["python", "worker.py"]
        self.info = {"name": self._cmdline[0]}
        self.signals: list[int] = []

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def status(self) -> str:
        return str(psutil.STATUS_SLEEPING)

    def cmdline(self) -> list[str]:
        return self._cmdline

    def name(self) -> str:
        return self._cmdline[0]

    def net_connections(self, kind: str = "inet") -> list[object]:
        raise RuntimeError("proc_pidinfo(PROC_PIDLISTFDS) 2/2 syscall failed")


def test_status_reports_an_uninspectable_listener_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("gobby.cli.daemon_lifecycle.unsupported_platform_error", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon_lifecycle.get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(
        "gobby.cli.daemon_lifecycle.probe_daemon_lock",
        MagicMock(return_value=SingletonProbe(ProbeState.DAEMON, pid=12345, role="daemon")),
    )
    monkeypatch.setattr("gobby.cli.daemon_lifecycle._read_pid_file", lambda: 12345)
    monkeypatch.setattr("gobby.cli.daemon_lifecycle._is_process_alive", lambda _pid: True)
    monkeypatch.setattr(
        "gobby.cli.daemon_lifecycle.get_service_status", lambda: {"installed": False}
    )
    monkeypatch.setattr(
        "psutil.process_iter", lambda *_args, **_kwargs: iter([_SandboxedProcess()])
    )
    monkeypatch.setattr(
        "gobby.cli.runtime.CliRuntime.require_config",
        MagicMock(return_value=DaemonConfig(daemon_port=61234)),
    )
    monkeypatch.setattr("gobby.cli.runtime.CliRuntime.require_database", MagicMock())
    monkeypatch.setattr(
        "gobby.cli.daemon_lifecycle.fetch_rich_status",
        AsyncMock(return_value=RichStatusProbe(api_data={"process": {}}, health_confirmed=True)),
    )
    monkeypatch.setattr(
        "gobby.storage.schema_divergence.collect_schema_heads",
        MagicMock(return_value=MagicMock(diverged=False)),
    )
    monkeypatch.setattr("gobby.utils.deps.collect_all_deps", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("gobby.utils.deps.check_config_mismatches", lambda _config: [])

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Running (PID: 12345)" in result.output
    assert "cannot inspect HTTP port 61234" in result.output
    assert "proc_pidinfo" in result.output


def test_kill_all_skips_a_process_whose_sockets_the_os_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.cli.utils_process.is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(
        "gobby.cli.utils_process.load_bootstrap", MagicMock(side_effect=OSError("isolated"))
    )
    monkeypatch.setattr("gobby.runner_maintenance.write_shutdown_source", MagicMock())
    daemon_looking = _SandboxedProcess(cmdline=["python", "-m", "gobby.runner"])
    monkeypatch.setattr("psutil.process_iter", lambda *_args, **_kwargs: iter([daemon_looking]))

    assert kill_all_gobby_daemons() == 0
    assert daemon_looking.signals == []


def test_kill_port_holder_skips_a_process_whose_sockets_the_os_refuses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("gobby.cli.utils_process.is_test_protect_enabled", lambda: False)
    daemon_looking = _SandboxedProcess(cmdline=["python", "-m", "gobby.runner"])
    monkeypatch.setattr("psutil.process_iter", lambda *_args, **_kwargs: iter([daemon_looking]))

    with caplog.at_level(logging.INFO):
        _kill_port_holder(61234)

    assert "Killing orphan process" not in caplog.text


def test_find_gobby_ui_port_holder_skips_a_process_whose_sockets_the_os_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("gobby.cli.utils_ui._is_gobby_ui_process", lambda _proc, **_kw: True)
    monkeypatch.setattr(
        "psutil.process_iter", lambda *_args, **_kwargs: iter([_SandboxedProcess()])
    )

    assert _find_gobby_ui_port_holder(5173, tmp_path) is None
