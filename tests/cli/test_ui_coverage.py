"""Tests for cli/ui.py — targeting uncovered lines."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.ui import (
    _ensure_npm_deps_installed,
    _get_ui_pid,
    ui,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _get_ui_pid
# ---------------------------------------------------------------------------
class TestGetUiPid:
    @patch("gobby.cli.ui.get_gobby_home")
    def test_no_pid_file(self, mock_home: MagicMock, tmp_path: Path) -> None:
        mock_home.return_value = tmp_path
        assert _get_ui_pid() is None

    @patch("gobby.cli.ui.get_gobby_home")
    @patch("gobby.cli.ui.os.kill")
    def test_valid_pid(self, mock_kill: MagicMock, mock_home: MagicMock, tmp_path: Path) -> None:
        pid_file = tmp_path / "ui.pid"
        pid_file.write_text("12345")
        mock_home.return_value = tmp_path
        mock_kill.return_value = None  # process exists
        assert _get_ui_pid() == 12345

    @patch("gobby.cli.ui.get_gobby_home")
    @patch("gobby.cli.ui.os.kill")
    def test_stale_pid(self, mock_kill: MagicMock, mock_home: MagicMock, tmp_path: Path) -> None:
        pid_file = tmp_path / "ui.pid"
        pid_file.write_text("99999")
        mock_home.return_value = tmp_path
        mock_kill.side_effect = ProcessLookupError
        assert _get_ui_pid() is None

    @patch("gobby.cli.ui.get_gobby_home")
    def test_invalid_pid_content(self, mock_home: MagicMock, tmp_path: Path) -> None:
        pid_file = tmp_path / "ui.pid"
        pid_file.write_text("not-a-number")
        mock_home.return_value = tmp_path
        assert _get_ui_pid() is None


# ---------------------------------------------------------------------------
# _ensure_npm_deps_installed
# ---------------------------------------------------------------------------
class TestEnsureNpmDepsInstalled:
    def test_node_modules_exists(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        assert _ensure_npm_deps_installed(tmp_path) is True

    @patch("gobby.cli.ui.subprocess.run")
    def test_npm_install_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert _ensure_npm_deps_installed(tmp_path) is True

    @patch("gobby.cli.ui.subprocess.run")
    def test_npm_install_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        assert _ensure_npm_deps_installed(tmp_path) is False

    @patch("gobby.cli.ui.subprocess.run")
    def test_npm_install_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="npm", timeout=120)
        assert _ensure_npm_deps_installed(tmp_path) is False

    @patch("gobby.cli.ui.subprocess.run")
    def test_npm_not_found(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = FileNotFoundError
        assert _ensure_npm_deps_installed(tmp_path) is False

    @patch("gobby.cli.ui.subprocess.run")
    def test_npm_oserror(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = OSError("bad")
        assert _ensure_npm_deps_installed(tmp_path) is False


# ---------------------------------------------------------------------------
# ui start
# ---------------------------------------------------------------------------
class TestUiStart:
    def test_ui_not_enabled(self, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = False
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0
        assert "not enabled" in result.output.lower()

    @patch("gobby.cli.ui._get_ui_pid", return_value=999)
    def test_already_running_dev_mode(self, _mock_pid: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui.spawn_ui_server", return_value=1234)
    @patch("gobby.cli.ui.find_web_dir", return_value=Path("/fake/web"))
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    def test_start_dev_success(
        self,
        _mock_pid: MagicMock,
        _mock_web: MagicMock,
        _mock_spawn: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        config.ui.host = "localhost"
        config.ui.port = 60889
        config.logging.client = "~/.gobby/logs/gobby.log"
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "1234" in result.output

    @patch("gobby.cli.ui.find_web_dir", return_value=None)
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    def test_start_dev_no_web_dir(
        self, _mock_pid: MagicMock, _mock_web: MagicMock, runner: CliRunner
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui.spawn_ui_server", return_value=None)
    @patch("gobby.cli.ui.find_web_dir", return_value=Path("/fake/web"))
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    def test_start_dev_spawn_fails(
        self,
        _mock_pid: MagicMock,
        _mock_web: MagicMock,
        _mock_spawn: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        config.ui.host = "localhost"
        config.ui.port = 60889
        config.logging.client = "~/.gobby/logs/gobby.log"
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0

    def test_start_production_mode(self, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "production"
        result = runner.invoke(ui, ["start"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Production mode" in result.output


# ---------------------------------------------------------------------------
# ui stop
# ---------------------------------------------------------------------------
class TestUiStop:
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    def test_not_running(self, _mock: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["stop"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    @patch("gobby.cli.ui.stop_ui_server", return_value=True)
    @patch("gobby.cli.ui._get_ui_pid", return_value=123)
    def test_stop_success(self, _pid: MagicMock, _stop: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["stop"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    @patch("gobby.cli.ui.stop_ui_server", return_value=False)
    @patch("gobby.cli.ui._get_ui_pid", return_value=123)
    def test_stop_failure(self, _pid: MagicMock, _stop: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["stop"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ui restart
# ---------------------------------------------------------------------------
class TestUiRestart:
    @patch("gobby.cli.ui.spawn_ui_server", return_value=9999)
    @patch("gobby.cli.ui.find_web_dir", return_value=Path("/fake/web"))
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    @patch("gobby.cli.ui.stop_ui_server")
    def test_restart(
        self,
        _stop: MagicMock,
        _pid: MagicMock,
        _web: MagicMock,
        _spawn: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        config.ui.host = "localhost"
        config.ui.port = 60889
        config.logging.client = "~/.gobby/logs/gobby.log"
        result = runner.invoke(ui, ["restart"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.ui.spawn_ui_server", return_value=None)
    @patch("gobby.cli.ui.find_web_dir", return_value=Path("/fake/web"))
    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    @patch("gobby.cli.ui.stop_ui_server")
    def test_restart_failure(
        self,
        _stop: MagicMock,
        _pid: MagicMock,
        _web: MagicMock,
        _spawn: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        result = runner.invoke(ui, ["restart"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0
        assert "Failed to" in result.output

    @patch("gobby.cli.ui.spawn_ui_server", return_value=9999)
    @patch("gobby.cli.ui.find_web_dir", return_value=Path("/fake/web"))
    @patch("gobby.cli.ui._get_ui_pid", return_value=123)
    @patch("gobby.cli.ui.stop_ui_server", return_value=False)
    def test_restart_stop_failure(
        self,
        _stop: MagicMock,
        _pid: MagicMock,
        _web: MagicMock,
        _spawn: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        result = runner.invoke(ui, ["restart"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ui status
# ---------------------------------------------------------------------------
class TestUiStatus:
    def test_status_disabled(self, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = False
        result = runner.invoke(ui, ["status"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Disabled" in result.output

    @patch("gobby.cli.ui._get_ui_pid", return_value=5555)
    def test_status_dev_running(self, _pid: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        config.ui.host = "localhost"
        config.ui.port = 60889
        result = runner.invoke(ui, ["status"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "5555" in result.output
        assert "Running" in result.output

    @patch("gobby.cli.ui._get_ui_pid", return_value=None)
    def test_status_dev_stopped(self, _pid: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "dev"
        result = runner.invoke(ui, ["status"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_status_production(self, runner: CliRunner) -> None:
        config = MagicMock()
        config.ui.enabled = True
        config.ui.mode = "production"
        config.daemon_port = 60888
        result = runner.invoke(ui, ["status"], obj={"config": config}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Served by daemon" in result.output


# ---------------------------------------------------------------------------
# ui dev
# ---------------------------------------------------------------------------
class TestUiDev:
    @patch("gobby.cli.ui.subprocess.run")
    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_dev_success(
        self,
        mock_dir: MagicMock,
        _npm: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        (tmp_path / "package.json").write_text("{}")
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(ui, ["dev"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.ui.WEB_UI_DIR", new=Path("/nonexistent/path"))
    def test_dev_no_web_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["dev"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_dev_no_package_json(
        self, mock_dir: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        mock_dir.exists.return_value = True
        result = runner.invoke(ui, ["dev"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui.subprocess.run")
    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_dev_keyboard_interrupt(
        self,
        mock_dir: MagicMock,
        _npm: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        (tmp_path / "package.json").write_text("{}")
        mock_run.side_effect = KeyboardInterrupt
        result = runner.invoke(ui, ["dev"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.ui.subprocess.run")
    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_dev_called_process_error(
        self,
        mock_dir: MagicMock,
        _npm: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        (tmp_path / "package.json").write_text("{}")
        mock_run.side_effect = subprocess.CalledProcessError(returncode=2, cmd="npm")
        result = runner.invoke(ui, ["dev"], catch_exceptions=False)
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# ui build
# ---------------------------------------------------------------------------
class TestUiBuild:
    @patch("gobby.cli.ui.WEB_UI_DIR", new=Path("/nonexistent"))
    def test_build_no_web_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["build"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui.subprocess.run")
    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_build_success(
        self,
        mock_dir: MagicMock,
        _npm: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(ui, ["build"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Build complete" in result.output

    @patch("gobby.cli.ui.subprocess.run")
    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_build_failure(
        self,
        mock_dir: MagicMock,
        _npm: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        mock_run.return_value = MagicMock(returncode=1)
        result = runner.invoke(ui, ["build"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=False)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_build_npm_deps_fail(
        self, mock_dir: MagicMock, _npm: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_dir.exists.return_value = True
        mock_dir.__truediv__ = lambda self, key: tmp_path / key
        result = runner.invoke(ui, ["build"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ui install-deps
# ---------------------------------------------------------------------------
class TestUiInstallDeps:
    @patch("gobby.cli.ui.WEB_UI_DIR", new=Path("/nonexistent"))
    def test_install_deps_no_web_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(ui, ["install-deps"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=True)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_install_deps_success(
        self, mock_dir: MagicMock, _npm: MagicMock, runner: CliRunner
    ) -> None:
        mock_dir.exists.return_value = True
        result = runner.invoke(ui, ["install-deps"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "installed" in result.output.lower()

    @patch("gobby.cli.ui._ensure_npm_deps_installed", return_value=False)
    @patch("gobby.cli.ui.WEB_UI_DIR")
    def test_install_deps_failure(
        self, mock_dir: MagicMock, _npm: MagicMock, runner: CliRunner
    ) -> None:
        mock_dir.exists.return_value = True
        result = runner.invoke(ui, ["install-deps"], catch_exceptions=False)
        assert result.exit_code != 0
