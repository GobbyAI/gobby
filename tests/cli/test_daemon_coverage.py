"""Tests for cli/daemon.py — targeting uncovered lines."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.daemon import (
    _services_start,
    _services_stop,
    status,
    stop,
)
from gobby.config.persistence import DatabasesConfig

pytestmark = pytest.mark.unit


def _service_config(
    *,
    falkordb_password: str | None = None,
    qdrant_url: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        databases=DatabasesConfig(
            falkordb={"requirepass": falkordb_password},
            qdrant={"url": qdrant_url},
        )
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _services_start / _services_stop
# ---------------------------------------------------------------------------
class TestServicesStart:
    def test_no_compose_file(self, tmp_path: Path) -> None:
        """No compose file → early return, no error."""
        _services_start(tmp_path)
        assert not (tmp_path / "services" / "docker-compose.yml").exists()

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_exists_success(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.return_value = _service_config(
            falkordb_password="password123",
            qdrant_url=None,
        )
        mock_run.return_value = MagicMock(returncode=0)

        with patch("gobby.cli.daemon._open_services_config_db", return_value=MagicMock()):
            _services_start(tmp_path)
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None
        cmd = mock_run.call_args.args[0]
        assert "falkordb" in cmd
        assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "password123"

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_exists_failure(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.return_value = _service_config(
            falkordb_password=None,
            qdrant_url="http://localhost:6333",
        )
        mock_run.return_value = MagicMock(returncode=1, stderr="err", stdout="")

        with patch("gobby.cli.daemon._open_services_config_db", return_value=MagicMock()):
            _services_start(tmp_path)
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None
        assert "qdrant" in mock_run.call_args.args[0]
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "services")

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_timeout(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.return_value = _service_config(
            falkordb_password=None,
            qdrant_url="http://localhost:6333",
        )
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=120)
        with patch("gobby.cli.daemon._open_services_config_db", return_value=MagicMock()):
            result = _services_start(tmp_path)
        assert result is None
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None

    @patch("gobby.config.app.load_config")
    def test_config_error(self, mock_config: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.side_effect = RuntimeError("config error")
        # Without resolved service config there are no profiles to start.
        with patch("gobby.cli.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("gobby.cli.daemon._open_services_config_db", return_value=MagicMock()):
                result = _services_start(tmp_path)
            assert result is None
            assert compose.exists()
            mock_run.assert_not_called()


class TestServicesStop:
    def test_no_compose_file(self, tmp_path: Path) -> None:
        _services_stop(tmp_path)
        assert not (tmp_path / "services" / "docker-compose.yml").exists()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.return_value = MagicMock(returncode=0)
        _services_stop(tmp_path)
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)
        result = _services_stop(tmp_path)
        assert result is None
        mock_run.assert_called_once()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_exception(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = FileNotFoundError("docker not found")
        result = _services_stop(tmp_path)
        assert result is None
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------
class TestStopCommand:
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_success(self, _stop: MagicMock, _svc: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        config.daemon_port = 60887
        result = runner.invoke(stop, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0

    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=False)
    def test_stop_failure(self, _stop: MagicMock, _svc: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        result = runner.invoke(stop, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 1

    @patch("gobby.cli.daemon._services_stop")
    @patch("gobby.cli.daemon.get_gobby_home", return_value=Path("/fake"))
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_with_docker(
        self,
        _stop: MagicMock,
        _svc: MagicMock,
        _home: MagicMock,
        mock_services: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.daemon_port = 60887
        result = runner.invoke(stop, ["--docker"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        mock_services.assert_called_once()
        assert mock_services.call_count == 1
        assert mock_services.call_args is not None


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------
class TestStatusCommand:
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.get_service_status", return_value={"running": False})
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_no_pid_file(
        self,
        _fmt: MagicMock,
        _svc: MagicMock,
        mock_home: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Not running" in result.output
        _fmt.assert_called_once()
        _svc.assert_called_once()

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.get_service_status", return_value={"running": False})
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_invalid_pid_file(
        self,
        _fmt: MagicMock,
        _svc: MagicMock,
        mock_home: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("not-a-number")
        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Not running" in result.output
        _fmt.assert_called_once()
        _svc.assert_called_once()

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.asyncio.run", return_value={})
    @patch("gobby.cli.daemon.format_status_message", return_value="Running PID 123")
    @patch("gobby.cli.daemon.format_uptime", return_value="1h 30m")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon.os.kill")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_running(
        self,
        mock_home: MagicMock,
        mock_kill: MagicMock,
        mock_process: MagicMock,
        _uptime: MagicMock,
        _fmt: MagicMock,
        _async: MagicMock,
        _deps: MagicMock,
        _mismatches: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("12345")
        mock_kill.return_value = None
        mock_process.return_value.create_time.return_value = 0.0

        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        config.daemon_port = 60888
        config.websocket.port = 60889
        config.ui.enabled = False
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Running PID 123" in result.output
        assert _fmt.call_args.kwargs["control_plane_error"] == (
            "HTTP control plane unavailable at localhost:60888; "
            "PID exists but /api/admin/status did not respond"
        )

    @patch("gobby.cli.daemon.format_status_message", return_value="Stale PID")
    @patch("gobby.cli.daemon.os.kill", side_effect=ProcessLookupError)
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_stale_pid(
        self,
        mock_home: MagicMock,
        _kill: MagicMock,
        _fmt: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("99999")
        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Stale" in result.output
