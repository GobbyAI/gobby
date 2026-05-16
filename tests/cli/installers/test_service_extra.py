"""Extra tests for OS-level service coverage."""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.service import (
    _launchctl_bootout,
    _linux_restart,
    _linux_start,
    _linux_stop,
    _macos_start,
    _macos_stop,
    disable_service_linux,
    disable_service_macos,
    enable_service_linux,
    service_restart,
    service_start,
    service_stop,
)

pytestmark = pytest.mark.unit


class TestLinuxEnableDisableRestart:
    @patch("gobby.cli.installers.service_linux.subprocess.run")
    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_enable_linux(self, mock_unit_path, mock_run, tmp_path: Path) -> None:
        unit_file = tmp_path / "gobby-daemon.service"
        unit_file.write_text("dummy")
        mock_unit_path.return_value = unit_file

        mock_run.return_value = MagicMock(returncode=0)
        res = enable_service_linux()
        assert res["success"] is True

    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_enable_linux_not_installed(self, mock_unit_path, tmp_path: Path) -> None:
        mock_unit_path.return_value = tmp_path / "missing"
        res = enable_service_linux()
        assert res["success"] is False

    @patch("gobby.cli.installers.service_linux.subprocess.run")
    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_disable_linux(self, mock_unit_path, mock_run, tmp_path: Path) -> None:
        unit_file = tmp_path / "gobby-daemon.service"
        unit_file.write_text("dummy")
        mock_unit_path.return_value = unit_file

        mock_run.return_value = MagicMock(returncode=0)
        res = disable_service_linux()
        assert res["success"] is True

    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_disable_linux_not_installed(self, mock_unit_path, tmp_path: Path) -> None:
        mock_unit_path.return_value = tmp_path / "missing"
        res = disable_service_linux()
        assert res["success"] is False

    @patch("gobby.cli.installers.service_linux.subprocess.run")
    def test_linux_restart(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        res = _linux_restart()
        assert res["success"] is True


class TestMacOSDisable:
    @patch("gobby.cli.installers.service._launchctl_bootout")
    @patch("gobby.cli.installers.service._plist_path")
    def test_disable_macos(self, mock_plist, mock_bootout, tmp_path: Path) -> None:
        plist = tmp_path / "test.plist"
        plist.write_text("dummy")
        mock_plist.return_value = plist
        mock_bootout.return_value = {"success": True, "platform": "macos"}

        res = disable_service_macos()
        assert res["success"] is True
        mock_bootout.assert_called_once()

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_disable_macos_reports_real_bootout_failure(
        self,
        mock_plist,
        mock_run,
        tmp_path: Path,
    ) -> None:
        plist = tmp_path / "test.plist"
        plist.write_text("dummy")
        mock_plist.return_value = plist
        mock_run.return_value = MagicMock(
            returncode=5,
            stderr="Input/output error",
            stdout="",
        )

        res = disable_service_macos()

        assert res["success"] is False
        assert res["error"] == "launchctl bootout failed: Input/output error"

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_disable_macos_treats_already_unloaded_as_success(
        self,
        mock_plist,
        mock_run,
        tmp_path: Path,
    ) -> None:
        plist = tmp_path / "test.plist"
        plist.write_text("dummy")
        mock_plist.return_value = plist
        mock_run.return_value = MagicMock(
            returncode=3,
            stderr="Boot-out failed: 3: No such process",
            stdout="",
        )

        res = disable_service_macos()

        assert res["success"] is True

    @patch("gobby.cli.installers.service._plist_path")
    def test_disable_macos_not_installed(self, mock_plist, tmp_path: Path) -> None:
        mock_plist.return_value = tmp_path / "missing"
        res = disable_service_macos()
        assert res["success"] is False

    @patch("gobby.cli.installers.service.subprocess.run")
    def test_launchctl_bootout_timeout_respects_quiet(
        self,
        mock_run: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="launchctl", timeout=30)

        with caplog.at_level(logging.WARNING):
            quiet_result = _launchctl_bootout(quiet=True)

        assert quiet_result["success"] is False
        assert quiet_result["error"] == "launchctl bootout timed out"
        assert "launchctl bootout timed out" not in caplog.text

        with caplog.at_level(logging.WARNING):
            loud_result = _launchctl_bootout(quiet=False)

        assert loud_result["success"] is False
        assert "launchctl bootout timed out" in caplog.text


class TestDirectStartStopCommands:
    @patch("gobby.cli.installers.service.enable_service_macos")
    def test_macos_start(self, mock_enable) -> None:
        mock_enable.return_value = {"success": True}
        assert _macos_start() == {"success": True}

    @patch("gobby.cli.installers.service.disable_service_macos")
    def test_macos_stop(self, mock_disable) -> None:
        mock_disable.return_value = {"success": True}
        assert _macos_stop() == {"success": True}

    @patch("gobby.cli.installers.service_linux.enable_service_linux")
    def test_linux_start(self, mock_enable) -> None:
        mock_enable.return_value = {"success": True}
        assert _linux_start() == {"success": True}

    @patch("gobby.cli.installers.service_linux.disable_service_linux")
    def test_linux_stop(self, mock_disable) -> None:
        mock_disable.return_value = {"success": True}
        assert _linux_stop() == {"success": True}


class TestServiceDispatchHelpers:
    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service_windows._windows_start")
    @patch("gobby.cli.installers.service._macos_start")
    @patch("gobby.cli.installers.service._linux_start")
    def test_service_start(self, mock_ls, mock_ms, mock_ws, mock_sys) -> None:
        mock_ms.return_value = {"success": True, "p": "mac"}
        mock_ls.return_value = {"success": True, "p": "linux"}
        mock_ws.return_value = {"success": True, "p": "win"}

        mock_sys.platform = "darwin"
        assert service_start()["p"] == "mac"

        mock_sys.platform = "linux"
        assert service_start()["p"] == "linux"

        mock_sys.platform = "win32"
        assert service_start()["p"] == "win"

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service_windows._windows_stop")
    @patch("gobby.cli.installers.service._macos_stop")
    @patch("gobby.cli.installers.service._linux_stop")
    def test_service_stop(self, mock_ls, mock_ms, mock_ws, mock_sys) -> None:
        mock_ms.return_value = {"success": True, "p": "mac"}
        mock_ls.return_value = {"success": True, "p": "linux"}
        mock_ws.return_value = {"success": True, "p": "win"}

        mock_sys.platform = "darwin"
        assert service_stop()["p"] == "mac"

        mock_sys.platform = "linux"
        assert service_stop()["p"] == "linux"

        mock_sys.platform = "win32"
        assert service_stop()["p"] == "win"

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service_windows._windows_restart")
    @patch("gobby.cli.installers.service._macos_restart")
    @patch("gobby.cli.installers.service._linux_restart")
    @patch("gobby.runner_maintenance.write_shutdown_source")
    def test_service_restart(
        self,
        mock_write_shutdown,
        mock_lr,
        mock_mr,
        mock_wr,
        mock_sys,
    ) -> None:
        mock_mr.return_value = {"success": True, "p": "mac"}
        mock_lr.return_value = {"success": True, "p": "linux"}
        mock_wr.return_value = {"success": True, "p": "win"}

        mock_sys.platform = "darwin"
        assert service_restart()["p"] == "mac"

        mock_sys.platform = "linux"
        assert service_restart(shutdown_source="http_restart")["p"] == "linux"

        mock_sys.platform = "win32"
        assert service_restart()["p"] == "win"
        assert mock_write_shutdown.call_count == 3
        assert mock_write_shutdown.call_args_list[0].args == ("service_restart",)
        assert mock_write_shutdown.call_args_list[0].kwargs == {"intent": "restart"}
        assert mock_write_shutdown.call_args_list[1].args == ("http_restart",)
        assert mock_write_shutdown.call_args_list[1].kwargs == {"intent": "restart"}
        assert mock_write_shutdown.call_args_list[2].args == ("service_restart",)
        assert mock_write_shutdown.call_args_list[2].kwargs == {"intent": "restart"}

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service._linux_stop")
    @patch("gobby.runner_maintenance.write_shutdown_source", side_effect=OSError("readonly"))
    def test_service_stop_continues_when_shutdown_source_write_fails(
        self,
        _mock_write_shutdown,
        mock_linux_stop,
        mock_sys,
    ) -> None:
        mock_sys.platform = "linux"
        mock_linux_stop.return_value = {"success": True, "p": "linux"}

        assert service_stop()["p"] == "linux"

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service._linux_restart")
    @patch("gobby.runner_maintenance.write_shutdown_source", side_effect=OSError("readonly"))
    def test_service_restart_continues_when_shutdown_source_write_fails(
        self,
        _mock_write_shutdown,
        mock_linux_restart,
        mock_sys,
    ) -> None:
        mock_sys.platform = "linux"
        mock_linux_restart.return_value = {"success": True, "p": "linux"}

        assert service_restart()["p"] == "linux"
