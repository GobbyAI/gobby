"""Tests for cli/daemon.py — targeting uncovered lines."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.daemon import (
    _neo4j_start,
    _neo4j_stop,
    get_merge_status,
    spawn_watchdog,
    status,
    stop,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _neo4j_start / _neo4j_stop
# ---------------------------------------------------------------------------
class TestNeo4jStart:
    def test_no_compose_file(self, tmp_path: Path) -> None:
        """No compose file → early return, no error."""
        _neo4j_start(tmp_path)  # Should not raise

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_exists_success(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        cfg = MagicMock()
        cfg.memory.neo4j_auth = "neo4j:password123"
        mock_config.return_value = cfg
        mock_run.return_value = MagicMock(returncode=0)

        _neo4j_start(tmp_path)
        mock_run.assert_called_once()

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_exists_failure(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        cfg = MagicMock()
        cfg.memory.neo4j_auth = None
        mock_config.return_value = cfg
        mock_run.return_value = MagicMock(returncode=1, stderr="err", stdout="")

        _neo4j_start(tmp_path)  # Should not raise

    @patch("gobby.cli.daemon.subprocess.run")
    @patch("gobby.config.app.load_config")
    def test_compose_timeout(
        self, mock_config: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.return_value = MagicMock(memory=MagicMock(neo4j_auth=None))
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=120)
        _neo4j_start(tmp_path)  # Should not raise

    @patch("gobby.config.app.load_config")
    def test_config_error(self, mock_config: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        mock_config.side_effect = RuntimeError("config error")
        # Should still try to run docker compose even on config error
        with patch("gobby.cli.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _neo4j_start(tmp_path)


class TestNeo4jStop:
    def test_no_compose_file(self, tmp_path: Path) -> None:
        _neo4j_stop(tmp_path)  # no-op

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.return_value = MagicMock(returncode=0)
        _neo4j_stop(tmp_path)
        mock_run.assert_called_once()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)
        _neo4j_stop(tmp_path)  # Should not raise

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_exception(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "neo4j" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = FileNotFoundError("docker not found")
        _neo4j_stop(tmp_path)  # Should not raise


# ---------------------------------------------------------------------------
# spawn_watchdog
# ---------------------------------------------------------------------------
class TestSpawnWatchdog:
    @patch("gobby.cli.daemon.subprocess.Popen")
    def test_spawn_success(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        mock_popen.return_value.pid = 9876
        log_file = tmp_path / "watchdog.log"
        pid = spawn_watchdog(60888, False, log_file)
        assert pid == 9876

    @patch("gobby.cli.daemon.subprocess.Popen")
    def test_spawn_verbose(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        mock_popen.return_value.pid = 5555
        log_file = tmp_path / "logs" / "watchdog.log"
        pid = spawn_watchdog(60888, True, log_file)
        assert pid == 5555

    @patch("gobby.cli.daemon.subprocess.Popen", side_effect=OSError("fail"))
    def test_spawn_failure(self, _popen: MagicMock, tmp_path: Path) -> None:
        log_file = tmp_path / "watchdog.log"
        pid = spawn_watchdog(60888, False, log_file)
        assert pid is None


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------
class TestStopCommand:
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_success(self, _stop: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        result = runner.invoke(stop, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.daemon.stop_daemon_util", return_value=False)
    def test_stop_failure(self, _stop: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        result = runner.invoke(stop, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 1

    @patch("gobby.cli.daemon._neo4j_stop")
    @patch("gobby.cli.daemon.get_gobby_home", return_value=Path("/fake"))
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_with_neo4j(
        self, _stop: MagicMock, _home: MagicMock, mock_neo4j: MagicMock, runner: CliRunner
    ) -> None:
        config = MagicMock()
        result = runner.invoke(stop, ["--neo4j"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        mock_neo4j.assert_called_once()


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------
class TestStatusCommand:
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_no_pid_file(
        self, _fmt: MagicMock, mock_home: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_invalid_pid_file(
        self, _fmt: MagicMock, mock_home: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("not-a-number")
        config = MagicMock()
        config.logging.client = str(tmp_path / "gobby.log")
        result = runner.invoke(status, [], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0

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


# ---------------------------------------------------------------------------
# get_merge_status
# ---------------------------------------------------------------------------
class TestGetMergeStatus:
    @patch("gobby.storage.merge_resolutions.MergeResolutionManager")
    @patch("gobby.storage.database.LocalDatabase")
    def test_no_active_resolution(self, _db: MagicMock, mock_mgr_cls: MagicMock) -> None:
        mock_mgr_cls.return_value.get_active_resolution.return_value = None
        result = get_merge_status()
        assert result["active"] is False

    @patch("gobby.storage.merge_resolutions.MergeResolutionManager")
    @patch("gobby.storage.database.LocalDatabase")
    def test_active_resolution(self, _db: MagicMock, mock_mgr_cls: MagicMock) -> None:
        resolution = MagicMock()
        resolution.id = "res-123"
        resolution.source_branch = "feature"
        resolution.target_branch = "main"

        conflict1 = MagicMock(status="pending")
        conflict2 = MagicMock(status="resolved")

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.get_active_resolution.return_value = resolution
        mock_mgr.list_conflicts.return_value = [conflict1, conflict2]

        result = get_merge_status()
        assert result["active"] is True
        assert result["pending_conflicts"] == 1
        assert result["total_conflicts"] == 2

    @patch("gobby.storage.database.LocalDatabase", side_effect=RuntimeError("db error"))
    def test_exception_returns_inactive(self, _db: MagicMock) -> None:
        result = get_merge_status()
        assert result["active"] is False
        assert "error" in result
