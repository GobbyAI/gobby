"""Tests for Windows Task Scheduler service backend."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.service_windows import (
    WINDOWS_LAUNCHER_NAME,
    WINDOWS_TASK_NAME,
    WINDOWS_TASK_XML_NAME,
    _get_service_status_windows,
    _windows_restart,
    disable_service_windows,
    enable_service_windows,
    install_service_windows,
    uninstall_service_windows,
)

pytestmark = pytest.mark.unit


_INSTALL_CONTEXT = {
    "python_executable": r"C:\Python313\python.exe",
    "working_directory": r"C:\Users\test",
    "mode": "installed",
    "home_dir": r"C:\Users\test",
    "path_env": r"C:\Python313;C:\Windows\system32;C:\Windows",
    "log_file": r"C:\Users\test\.gobby\logs\gobby.log",
    "error_log_file": r"C:\Users\test\.gobby\logs\gobby-error.log",
    "gobby_home": "",
    "verbose": False,
}


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestTemplateRendering:
    """Test Jinja2 template rendering for Windows files."""

    def test_render_task_xml(self) -> None:
        """Task XML template renders valid XML with launcher path."""
        from gobby.cli.installers.service import _render_template

        content = _render_template(
            "gobby-daemon.task.xml.j2",
            launcher_script=r"C:\Users\test\.gobby\gobby-launcher.cmd",
            working_directory=r"C:\Users\test",
        )
        assert "<?xml" in content
        assert "<Task" in content
        assert "<LogonTrigger>" in content
        assert r"C:\Users\test\.gobby\gobby-launcher.cmd" in content
        assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in content

    def test_render_launcher_cmd(self) -> None:
        """Launcher batch script renders with correct paths."""
        from gobby.cli.installers.service import _render_template

        content = _render_template("gobby-launcher.cmd.j2", **_INSTALL_CONTEXT)
        assert "@echo off" in content
        assert r"C:\Python313" in content
        assert "-m gobby.runner" in content
        assert "--verbose" not in content

    def test_render_launcher_cmd_verbose(self) -> None:
        """Launcher batch script includes --verbose when requested."""
        from gobby.cli.installers.service import _render_template

        ctx = {**_INSTALL_CONTEXT, "verbose": True}
        content = _render_template("gobby-launcher.cmd.j2", **ctx)
        assert "--verbose" in content

    def test_render_launcher_cmd_with_gobby_home(self) -> None:
        """Launcher batch script includes GOBBY_HOME when set."""
        from gobby.cli.installers.service import _render_template

        ctx = {**_INSTALL_CONTEXT, "gobby_home": r"D:\gobby"}
        content = _render_template("gobby-launcher.cmd.j2", **ctx)
        assert "GOBBY_HOME" in content
        assert r"D:\gobby" in content

    def test_render_launcher_cmd_does_not_html_escape_text_template(self) -> None:
        """Batch script templates should preserve shell metacharacters."""
        from gobby.cli.installers.service import _render_template

        ctx = {**_INSTALL_CONTEXT, "path_env": r"C:\Tools & More;C:\Windows"}
        content = _render_template("gobby-launcher.cmd.j2", **ctx)
        assert r"C:\Tools & More;C:\Windows" in content
        assert "&amp;" not in content

    def test_render_task_xml_escapes_xml_values(self) -> None:
        """XML templates should escape XML-sensitive characters."""
        from gobby.cli.installers.service import _render_template

        content = _render_template(
            "gobby-daemon.task.xml.j2",
            launcher_script=r"C:\Tools & More\gobby-launcher.cmd",
            working_directory=r"C:\Users\test",
        )
        assert r"C:\Tools &amp; More\gobby-launcher.cmd" in content


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


class TestWindowsInstall:
    """Test Windows Task Scheduler installation."""

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service._resolve_install_context")
    @patch("gobby.cli.installers.service_windows._gobby_home_dir")
    def test_install_writes_files_and_creates_task(
        self,
        mock_home: MagicMock,
        mock_ctx: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install writes XML and launcher, then calls schtasks /create."""
        mock_home.return_value = tmp_path
        mock_ctx.return_value = _INSTALL_CONTEXT.copy()
        mock_schtasks.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = install_service_windows()

        assert result["success"] is True
        assert result["platform"] == "windows"

        # Verify files were written
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        launcher_file = tmp_path / WINDOWS_LAUNCHER_NAME
        assert xml_file.exists()
        assert launcher_file.exists()
        assert "<Task" in xml_file.read_text(encoding="utf-8")
        assert "@echo off" in launcher_file.read_text(encoding="utf-8")

        # Verify schtasks was called (create + run)
        assert mock_schtasks.call_count == 2
        create_call = mock_schtasks.call_args_list[0]
        assert "/create" in create_call[0][0]
        assert WINDOWS_TASK_NAME in create_call[0][0]

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service._resolve_install_context")
    @patch("gobby.cli.installers.service_windows._gobby_home_dir")
    def test_install_fails_on_schtasks_error(
        self,
        mock_home: MagicMock,
        mock_ctx: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install returns failure when schtasks /create fails."""
        mock_home.return_value = tmp_path
        mock_ctx.return_value = _INSTALL_CONTEXT.copy()
        mock_schtasks.return_value = MagicMock(returncode=1, stderr="Access denied", stdout="")

        result = install_service_windows()

        assert result["success"] is False
        assert "schtasks /create failed" in result["error"]

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service._resolve_install_context")
    @patch("gobby.cli.installers.service_windows._gobby_home_dir")
    def test_install_handles_timeout(
        self,
        mock_home: MagicMock,
        mock_ctx: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install handles subprocess timeout gracefully."""
        mock_home.return_value = tmp_path
        mock_ctx.return_value = _INSTALL_CONTEXT.copy()
        mock_schtasks.side_effect = subprocess.TimeoutExpired("schtasks", 30)

        result = install_service_windows()

        assert result["success"] is False
        assert "failed" in result["error"]


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestWindowsUninstall:
    """Test Windows Task Scheduler uninstallation."""

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._gobby_home_dir")
    def test_uninstall_deletes_task_and_files(
        self,
        mock_home: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Uninstall deletes scheduled task and cleans up files."""
        mock_home.return_value = tmp_path
        mock_schtasks.return_value = MagicMock(returncode=0, stderr="", stdout="")

        # Create files to be cleaned up
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        launcher_file = tmp_path / WINDOWS_LAUNCHER_NAME
        xml_file.write_text("<task/>")
        launcher_file.write_text("@echo off")

        result = uninstall_service_windows()

        assert result["success"] is True
        assert result["platform"] == "windows"
        assert not xml_file.exists()
        assert not launcher_file.exists()

        # Verify schtasks /end + /delete were called
        assert mock_schtasks.call_count == 2
        end_call = mock_schtasks.call_args_list[0]
        assert "/end" in end_call[0][0]
        delete_call = mock_schtasks.call_args_list[1]
        assert "/delete" in delete_call[0][0]

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._gobby_home_dir")
    def test_uninstall_succeeds_when_task_not_found(
        self,
        mock_home: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Uninstall succeeds even when task doesn't exist in scheduler."""
        mock_home.return_value = tmp_path
        mock_schtasks.side_effect = [
            MagicMock(returncode=1, stderr="", stdout=""),  # /end fails (not running)
            MagicMock(
                returncode=1,
                stderr="ERROR: The system cannot find the file specified.",
                stdout="",
            ),  # /delete — not found
        ]

        result = uninstall_service_windows()

        assert result["success"] is True
        assert result["platform"] == "windows"
        assert mock_schtasks.call_count == 2


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------


class TestWindowsEnableDisable:
    """Test Windows enable/disable operations."""

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_enable_calls_change_and_run(
        self,
        mock_xml_path: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable calls schtasks /change /enable then /run."""
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        xml_file.write_text("<task/>")
        mock_xml_path.return_value = xml_file
        mock_schtasks.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = enable_service_windows()

        assert result["success"] is True
        assert mock_schtasks.call_count == 2
        change_call = mock_schtasks.call_args_list[0]
        assert "/enable" in change_call[0][0]
        run_call = mock_schtasks.call_args_list[1]
        assert "/run" in run_call[0][0]

    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_enable_fails_when_not_installed(
        self,
        mock_xml_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable returns error when XML doesn't exist."""
        mock_xml_path.return_value = tmp_path / "nonexistent.xml"

        result = enable_service_windows()

        assert result["success"] is False
        assert "not installed" in result["error"].lower()

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_disable_calls_end_and_change(
        self,
        mock_xml_path: MagicMock,
        mock_schtasks: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Disable calls schtasks /end then /change /disable."""
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        xml_file.write_text("<task/>")
        mock_xml_path.return_value = xml_file
        mock_schtasks.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = disable_service_windows()

        assert result["success"] is True
        assert mock_schtasks.call_count == 2
        end_call = mock_schtasks.call_args_list[0]
        assert "/end" in end_call[0][0]
        change_call = mock_schtasks.call_args_list[1]
        assert "/disable" in change_call[0][0]

    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_disable_fails_when_not_installed(
        self,
        mock_xml_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Disable returns error when not installed."""
        mock_xml_path.return_value = tmp_path / "nonexistent.xml"

        result = disable_service_windows()

        assert result["success"] is False
        assert "not installed" in result["error"].lower()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestWindowsStatus:
    """Test Windows status detection."""

    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_status_not_installed(
        self,
        mock_xml_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Status reports not installed when XML doesn't exist."""
        mock_xml_path.return_value = tmp_path / "nonexistent.xml"

        result = _get_service_status_windows()

        assert result["installed"] is False
        assert result["enabled"] is False
        assert result["running"] is False
        assert result["platform"] == "windows"

    @patch("gobby.cli.installers.service_windows._launcher_script_path")
    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_status_running_and_enabled(
        self,
        mock_xml_path: MagicMock,
        mock_schtasks: MagicMock,
        mock_launcher: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Status correctly parses running + enabled state."""
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        xml_file.write_text("<task/>")
        mock_xml_path.return_value = xml_file

        launcher_file = tmp_path / WINDOWS_LAUNCHER_NAME
        launcher_file.write_text(r'"C:\Python313\python.exe" -m gobby.runner')
        mock_launcher.return_value = launcher_file

        mock_schtasks.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout=(
                "HostName:      DESKTOP-TEST\n"
                "TaskName:      \\GobbyDaemon\n"
                "Status:        Running\n"
                "Scheduled Task State: Enabled\n"
                "Last Run Time: 4/2/2026 10:00:00 AM\n"
            ),
        )

        result = _get_service_status_windows()

        assert result["installed"] is True
        assert result["enabled"] is True
        assert result["running"] is True
        assert result["platform"] == "windows"
        assert result["mode"] == "installed"

    @patch("gobby.cli.installers.service_windows._launcher_script_path")
    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_status_disabled_and_not_running(
        self,
        mock_xml_path: MagicMock,
        mock_schtasks: MagicMock,
        mock_launcher: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Status correctly parses disabled + not running state."""
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        xml_file.write_text("<task/>")
        mock_xml_path.return_value = xml_file

        launcher_file = tmp_path / WINDOWS_LAUNCHER_NAME
        launcher_file.write_text(r'"C:\dev\gobby\.venv\Scripts\python.exe" -m gobby.runner')
        mock_launcher.return_value = launcher_file

        mock_schtasks.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout=(
                "HostName:      DESKTOP-TEST\n"
                "TaskName:      \\GobbyDaemon\n"
                "Status:        Ready\n"
                "Scheduled Task State: Disabled\n"
            ),
        )

        result = _get_service_status_windows()

        assert result["installed"] is True
        assert result["enabled"] is False
        assert result["running"] is False
        assert result["mode"] == "dev"

    @patch("gobby.cli.installers.service_windows._launcher_script_path")
    @patch("gobby.cli.installers.service_windows._run_schtasks")
    @patch("gobby.cli.installers.service_windows._task_xml_path")
    def test_status_handles_query_failure(
        self,
        mock_xml_path: MagicMock,
        mock_schtasks: MagicMock,
        mock_launcher: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Status returns safe defaults when schtasks query fails."""
        xml_file = tmp_path / WINDOWS_TASK_XML_NAME
        xml_file.write_text("<task/>")
        mock_xml_path.return_value = xml_file
        mock_launcher.return_value = tmp_path / "nonexistent.cmd"
        mock_schtasks.side_effect = subprocess.TimeoutExpired("schtasks", 10)

        result = _get_service_status_windows()

        assert result["installed"] is True
        assert result["enabled"] is False
        assert result["running"] is False


# ---------------------------------------------------------------------------
# Start / Stop / Restart
# ---------------------------------------------------------------------------


class TestWindowsStartStopRestart:
    """Test Windows start/stop/restart operations."""

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    def test_restart_ends_then_runs(self, mock_schtasks: MagicMock) -> None:
        """Restart calls /end then /run."""
        mock_schtasks.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = _windows_restart()

        assert result["success"] is True
        assert result["method"] == "schtasks restart"
        assert mock_schtasks.call_count == 2
        end_call = mock_schtasks.call_args_list[0]
        assert "/end" in end_call[0][0]
        run_call = mock_schtasks.call_args_list[1]
        assert "/run" in run_call[0][0]

    @patch("gobby.cli.installers.service_windows._run_schtasks")
    def test_restart_fails_on_run_error(self, mock_schtasks: MagicMock) -> None:
        """Restart returns failure when /run fails."""
        mock_schtasks.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # /end
            MagicMock(returncode=1, stderr="Task not found", stdout=""),  # /run
        ]

        result = _windows_restart()

        assert result["success"] is False
        assert "schtasks /run failed" in result["error"]
