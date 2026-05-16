"""Tests for OS-level service installation."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.service import (
    LAUNCHD_LABEL,
    LAUNCHD_PLIST_NAME,
    SYSTEMD_UNIT_NAME,
    _build_path,
    _find_project_from_cwd,
    _find_project_root,
    _is_dev_mode,
    _render_template,
    _resolve_install_context,
    disable_service,
    enable_service,
    get_service_status,
    install_service,
    uninstall_service,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestTemplateRendering:
    """Test Jinja2 template rendering produces valid output."""

    def test_render_plist_template(self) -> None:
        """Plist template renders valid XML."""
        content = _render_template(
            "com.gobby.daemon.plist.j2",
            python_executable="/usr/bin/python3",
            working_directory="/Users/test",
            home_dir="/Users/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="",
            verbose=False,
        )
        assert '<?xml version="1.0"' in content
        assert "<plist" in content
        assert f"<string>{LAUNCHD_LABEL}</string>" in content
        assert "<string>/usr/bin/python3</string>" in content
        assert "<key>RunAtLoad</key>" in content
        assert "<key>KeepAlive</key>" in content
        assert "--verbose" not in content

    def test_render_plist_template_verbose(self) -> None:
        """Plist template includes --verbose when requested."""
        content = _render_template(
            "com.gobby.daemon.plist.j2",
            python_executable="/usr/bin/python3",
            working_directory="/Users/test",
            home_dir="/Users/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="",
            verbose=True,
        )
        assert "<string>--verbose</string>" in content

    def test_render_plist_template_with_gobby_home(self) -> None:
        """Plist template includes GOBBY_HOME when set."""
        content = _render_template(
            "com.gobby.daemon.plist.j2",
            python_executable="/usr/bin/python3",
            working_directory="/Users/test",
            home_dir="/Users/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="/custom/gobby/home",
            verbose=False,
        )
        assert "<key>GOBBY_HOME</key>" in content
        assert "<string>/custom/gobby/home</string>" in content

    def test_render_plist_template_without_gobby_home(self) -> None:
        """Plist template omits GOBBY_HOME when empty."""
        content = _render_template(
            "com.gobby.daemon.plist.j2",
            python_executable="/usr/bin/python3",
            working_directory="/Users/test",
            home_dir="/Users/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="",
            verbose=False,
        )
        assert "GOBBY_HOME" not in content

    def test_render_systemd_template(self) -> None:
        """Systemd template renders valid unit file."""
        content = _render_template(
            "gobby-daemon.service.j2",
            python_executable="/usr/bin/python3",
            working_directory="/home/test",
            home_dir="/home/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="",
            verbose=False,
        )
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "ExecStart=/usr/bin/python3 -m gobby.runner" in content
        assert "Restart=on-failure" in content
        assert "WantedBy=default.target" in content
        assert "--verbose" not in content

    def test_render_systemd_template_verbose(self) -> None:
        """Systemd template includes --verbose when requested."""
        content = _render_template(
            "gobby-daemon.service.j2",
            python_executable="/usr/bin/python3",
            working_directory="/home/test",
            home_dir="/home/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="",
            verbose=True,
        )
        assert "--verbose" in content

    def test_render_systemd_template_with_gobby_home(self) -> None:
        """Systemd template includes GOBBY_HOME when set."""
        content = _render_template(
            "gobby-daemon.service.j2",
            python_executable="/usr/bin/python3",
            working_directory="/home/test",
            home_dir="/home/test",
            path_env="/usr/bin:/bin",
            log_file="/tmp/gobby.log",
            error_log_file="/tmp/gobby-error.log",
            gobby_home="/custom/gobby/home",
            verbose=False,
        )
        assert "GOBBY_HOME=/custom/gobby/home" in content


# ---------------------------------------------------------------------------
# Dev mode detection
# ---------------------------------------------------------------------------


class TestDevModeDetection:
    """Test development mode detection logic."""

    def test_is_dev_mode_exe_in_project_venv(self, tmp_path: Path) -> None:
        """Detects dev mode when sys.executable is inside a gobby project .venv."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_exe = venv_bin / "python3"
        fake_exe.touch()

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')

        with patch("gobby.cli.installers.service_common.sys") as mock_sys:
            mock_sys.executable = str(fake_exe)
            assert _is_dev_mode() is True

    def test_is_dev_mode_global_exe_in_project_cwd(self, tmp_path: Path) -> None:
        """Detects dev mode when global CLI is run from a gobby project directory."""
        # sys.executable is a global Python (not in a gobby project)
        global_exe = tmp_path / "global" / "bin" / "python3"
        global_exe.parent.mkdir(parents=True)
        global_exe.touch()

        # But CWD is a gobby project with .venv
        project_dir = tmp_path / "project"
        venv_bin = project_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").touch()
        (project_dir / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        with (
            patch("gobby.cli.installers.service_common.sys") as mock_sys,
            patch("gobby.cli.installers.service_common.Path.cwd", return_value=project_dir),
        ):
            mock_sys.executable = str(global_exe)
            assert _is_dev_mode() is True

    def test_is_dev_mode_without_pyproject(self, tmp_path: Path) -> None:
        """Not dev mode when no pyproject.toml found."""
        fake_exe = tmp_path / "bin" / "python3"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.touch()

        with (
            patch("gobby.cli.installers.service_common.sys") as mock_sys,
            patch("gobby.cli.installers.service_common.Path.cwd", return_value=tmp_path),
        ):
            mock_sys.executable = str(fake_exe)
            assert _is_dev_mode() is False

    def test_is_dev_mode_wrong_project(self, tmp_path: Path) -> None:
        """Not dev mode when pyproject.toml is a different project."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_exe = venv_bin / "python3"
        fake_exe.touch()

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "other-project"\n')

        with (
            patch("gobby.cli.installers.service_common.sys") as mock_sys,
            patch("gobby.cli.installers.service_common.Path.cwd", return_value=tmp_path),
        ):
            mock_sys.executable = str(fake_exe)
            assert _is_dev_mode() is False


class TestFindProjectFromCwd:
    """Test CWD-based project detection."""

    def test_finds_project_in_cwd(self, tmp_path: Path) -> None:
        """Finds gobby project when CWD is the project root."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").touch()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        with patch("gobby.cli.installers.service_common.Path.cwd", return_value=tmp_path):
            assert _find_project_from_cwd() == tmp_path

    def test_finds_project_from_subdirectory(self, tmp_path: Path) -> None:
        """Finds gobby project when CWD is a subdirectory."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").touch()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        subdir = tmp_path / "src" / "gobby"
        subdir.mkdir(parents=True)

        with patch("gobby.cli.installers.service_common.Path.cwd", return_value=subdir):
            assert _find_project_from_cwd() == tmp_path

    def test_returns_none_without_venv(self, tmp_path: Path) -> None:
        """Returns None when project has no .venv."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        with patch("gobby.cli.installers.service_common.Path.cwd", return_value=tmp_path):
            assert _find_project_from_cwd() is None

    def test_returns_none_for_non_gobby_project(self, tmp_path: Path) -> None:
        """Returns None for a different project."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").touch()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')

        with patch("gobby.cli.installers.service_common.Path.cwd", return_value=tmp_path):
            assert _find_project_from_cwd() is None


class TestResolveInstallContext:
    """Test install context resolution."""

    def test_resolve_returns_absolute_python_path(self) -> None:
        """Python executable path is absolute."""
        with patch("gobby.cli.installers.service_common._is_dev_mode", return_value=False):
            ctx = _resolve_install_context()
            assert Path(ctx["python_executable"]).is_absolute()

    def test_resolve_dev_mode_sets_project_root(self, tmp_path: Path) -> None:
        """Dev mode sets working directory to project root."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_exe = venv_bin / "python3"
        fake_exe.touch()

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')

        with (
            patch("gobby.cli.installers.service_common._is_dev_mode", return_value=True),
            patch("gobby.cli.installers.service_common.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            ctx = _resolve_install_context()
            assert ctx["mode"] == "dev"
            assert ctx["working_directory"] == str(tmp_path)

    def test_resolve_dev_mode_via_cwd_uses_venv_python(self, tmp_path: Path) -> None:
        """When global CLI runs from project dir, uses .venv python."""
        # Global python (not in project)
        global_exe = tmp_path / "global" / "bin" / "python3"
        global_exe.parent.mkdir(parents=True)
        global_exe.touch()

        # Project with .venv
        project_dir = tmp_path / "project"
        venv_bin = project_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        venv_python = venv_bin / "python3"
        venv_python.touch()
        (project_dir / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        with (
            patch("gobby.cli.installers.service_common._is_dev_mode", return_value=True),
            patch(
                "gobby.cli.installers.service_common._find_project_from_cwd",
                return_value=project_dir,
            ),
            patch("gobby.cli.installers.service_common.sys") as mock_sys,
        ):
            mock_sys.executable = str(global_exe)
            ctx = _resolve_install_context()
            assert ctx["mode"] == "dev"
            assert ctx["working_directory"] == str(project_dir)
            assert ".venv/bin/python3" in ctx["python_executable"]

    def test_resolve_installed_mode_uses_home(self) -> None:
        """Installed mode sets working directory to $HOME."""
        with patch("gobby.cli.installers.service_common._is_dev_mode", return_value=False):
            ctx = _resolve_install_context()
            assert ctx["mode"] == "installed"
            assert ctx["working_directory"] == str(Path.home())


class TestBuildPath:
    """Test PATH construction."""

    def test_exe_dir_is_first(self) -> None:
        """Executable's bin dir is first in PATH."""
        exe = Path("/some/venv/bin/python3")
        path = _build_path(exe)
        assert path.startswith("/some/venv/bin:")

    def test_deduplicates_exe_dir(self) -> None:
        """Doesn't duplicate exe dir if already in PATH."""
        exe = Path("/some/venv/bin/python3")
        with patch.dict(os.environ, {"PATH": "/some/venv/bin:/usr/bin:/bin"}):
            path = _build_path(exe)
            parts = path.split(":")
            assert parts.count("/some/venv/bin") == 1


class TestFindProjectRoot:
    """Test project root detection."""

    def test_finds_root_from_venv(self, tmp_path: Path) -> None:
        """Finds project root from .venv/bin/python3."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        exe = venv_bin / "python3"
        exe.touch()

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')

        assert _find_project_root(exe) == tmp_path

    def test_falls_back_to_home(self, tmp_path: Path) -> None:
        """Falls back to $HOME when no pyproject.toml found."""
        exe = tmp_path / "bin" / "python3"
        exe.parent.mkdir(parents=True)
        exe.touch()
        result = _find_project_root(exe)
        assert result == Path.home()


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


class TestPlatformDispatch:
    """Test platform dispatch calls correct platform function."""

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service.install_service_macos")
    def test_install_dispatches_darwin(self, mock_install: MagicMock, mock_sys: MagicMock) -> None:
        """install_service dispatches to macOS on darwin."""
        mock_sys.platform = "darwin"
        mock_install.return_value = {"success": True}
        result = install_service()
        mock_install.assert_called_once_with(verbose=False)
        assert result["success"] is True

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service.install_service_linux")
    def test_install_dispatches_linux(self, mock_install: MagicMock, mock_sys: MagicMock) -> None:
        """install_service dispatches to Linux on linux."""
        mock_sys.platform = "linux"
        mock_install.return_value = {"success": True}
        result = install_service()
        mock_install.assert_called_once_with(verbose=False)
        assert result["success"] is True

    @patch("gobby.cli.installers.service.sys")
    def test_install_unsupported_platform(self, mock_sys: MagicMock) -> None:
        """install_service returns error on unsupported platform."""
        mock_sys.platform = "freebsd"
        result = install_service()
        assert result["success"] is False
        assert "Unsupported platform" in result["error"]

    @patch("gobby.cli.installers.service_windows.install_service_windows")
    @patch("gobby.cli.installers.service.sys")
    def test_install_dispatches_win32(self, mock_sys: MagicMock, mock_install: MagicMock) -> None:
        """install_service dispatches to Windows on win32."""
        mock_sys.platform = "win32"
        mock_install.return_value = {"success": True}
        result = install_service()
        mock_install.assert_called_once_with(verbose=False)
        assert result["success"] is True

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service.uninstall_service_macos")
    def test_uninstall_dispatches_darwin(
        self, mock_uninstall: MagicMock, mock_sys: MagicMock
    ) -> None:
        """uninstall_service dispatches to macOS on darwin."""
        mock_sys.platform = "darwin"
        mock_uninstall.return_value = {"success": True}
        result = uninstall_service()
        mock_uninstall.assert_called_once()
        assert result["success"] is True

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service._get_service_status_macos")
    def test_status_dispatches_darwin(self, mock_status: MagicMock, mock_sys: MagicMock) -> None:
        """get_service_status dispatches to macOS on darwin."""
        mock_sys.platform = "darwin"
        mock_status.return_value = {"installed": True, "platform": "macos"}
        result = get_service_status()
        mock_status.assert_called_once()
        assert result["platform"] == "macos"

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service.enable_service_macos")
    def test_enable_dispatches_darwin(self, mock_enable: MagicMock, mock_sys: MagicMock) -> None:
        """enable_service dispatches to macOS on darwin."""
        mock_sys.platform = "darwin"
        mock_enable.return_value = {"success": True}
        result = enable_service()
        mock_enable.assert_called_once()
        assert result["success"] is True

    @patch("gobby.cli.installers.service.sys")
    @patch("gobby.cli.installers.service.disable_service_macos")
    def test_disable_dispatches_darwin(self, mock_disable: MagicMock, mock_sys: MagicMock) -> None:
        """disable_service dispatches to macOS on darwin."""
        mock_sys.platform = "darwin"
        mock_disable.return_value = {"success": True}
        result = disable_service()
        mock_disable.assert_called_once()
        assert result["success"] is True


# ---------------------------------------------------------------------------
# macOS (launchd) tests
# ---------------------------------------------------------------------------


class TestMacOSInstall:
    """Test macOS launchd installation."""

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._resolve_install_context")
    @patch("gobby.cli.installers.service._plist_path")
    def test_install_writes_plist_and_bootstraps(
        self,
        mock_plist_path: MagicMock,
        mock_ctx: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install writes plist file and calls launchctl bootstrap."""
        from gobby.cli.installers.service import install_service_macos

        plist_file = tmp_path / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
        mock_plist_path.return_value = plist_file
        mock_ctx.return_value = {
            "python_executable": "/usr/bin/python3",
            "working_directory": "/Users/test",
            "mode": "installed",
            "home_dir": "/Users/test",
            "path_env": "/usr/bin:/bin",
            "log_file": "/tmp/gobby.log",
            "error_log_file": "/tmp/gobby-error.log",
            "gobby_home": "",
            "verbose": False,
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = install_service_macos()

        assert result["success"] is True
        assert result["platform"] == "macos"
        assert plist_file.exists()
        content = plist_file.read_text()
        assert LAUNCHD_LABEL in content
        assert "<key>RunAtLoad</key>" in content

        # Verify launchctl was called (bootout + bootstrap)
        launchctl_calls = [call for call in mock_run.call_args_list if call[0][0][0] == "launchctl"]
        assert len(launchctl_calls) >= 1  # At least bootstrap

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._resolve_install_context")
    @patch("gobby.cli.installers.service._plist_path")
    def test_install_fails_on_bootstrap_error(
        self,
        mock_plist_path: MagicMock,
        mock_ctx: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install returns failure when launchctl bootstrap fails."""
        from gobby.cli.installers.service import install_service_macos

        plist_file = tmp_path / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
        mock_plist_path.return_value = plist_file
        mock_ctx.return_value = {
            "python_executable": "/usr/bin/python3",
            "working_directory": "/Users/test",
            "mode": "installed",
            "home_dir": "/Users/test",
            "path_env": "/usr/bin:/bin",
            "log_file": "/tmp/gobby.log",
            "error_log_file": "/tmp/gobby-error.log",
            "gobby_home": "",
            "verbose": False,
        }

        # First call (bootout) succeeds, second (bootstrap) fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # bootout
            MagicMock(returncode=1, stderr="some error", stdout=""),  # bootstrap
        ]

        result = install_service_macos()

        assert result["success"] is False
        assert "bootstrap failed" in result["error"]


class TestMacOSEnable:
    """Test macOS launchd enable (bootstrap)."""

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_enable_skips_bootout_when_already_running(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable returns early without bootout when daemon is already running (#10680)."""
        from gobby.cli.installers.service import enable_service_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file

        # Health check shows daemon running with PID
        mock_run.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout="\tpid = 12345\n\tstate = running\n",
        )

        result = enable_service_macos()

        assert result["success"] is True
        assert result.get("already_running") is True
        # Only the print call — no bootout or bootstrap
        launchctl_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "launchctl"]
        assert len(launchctl_calls) == 1
        assert "print" in launchctl_calls[0][0][0][1]

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_enable_bootouts_stale_entry_before_bootstrap(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable calls bootout before bootstrap to clear stale entries."""
        from gobby.cli.installers.service import enable_service_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = enable_service_macos()

        assert result["success"] is True
        # Verify print (health check) + bootout + bootstrap sequence
        calls = mock_run.call_args_list
        launchctl_calls = [c for c in calls if c[0][0][0] == "launchctl"]
        assert len(launchctl_calls) == 3
        assert "print" in launchctl_calls[0][0][0][1]
        assert "bootout" in launchctl_calls[1][0][0][1]
        assert "bootstrap" in launchctl_calls[2][0][0][1]

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_enable_succeeds_after_stale_bootout(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable succeeds even when bootout fails (no stale entry)."""
        from gobby.cli.installers.service import enable_service_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file

        # Health check (loaded but not running), bootout fails (no stale entry), bootstrap succeeds
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # print (loaded, not running)
            MagicMock(returncode=3, stderr="No such process", stdout=""),  # bootout
            MagicMock(returncode=0, stderr="", stdout=""),  # bootstrap
        ]

        result = enable_service_macos()

        assert result["success"] is True
        assert mock_run.call_count == 3

    @patch("gobby.cli.installers.service._plist_path")
    def test_enable_fails_when_not_installed(
        self,
        mock_plist_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Enable returns error when plist doesn't exist."""
        from gobby.cli.installers.service import enable_service_macos

        mock_plist_path.return_value = tmp_path / "nonexistent.plist"

        result = enable_service_macos()

        assert result["success"] is False
        assert "not installed" in result["error"].lower()


class TestMacOSRestart:
    """Test macOS launchd restart."""

    @patch("gobby.cli.installers.service._get_service_status_macos")
    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_restart_bootout_bootstrap(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        mock_status: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Restart does a full bootout + bootstrap cycle."""
        from gobby.cli.installers.service import _macos_restart

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file
        mock_status.return_value = {"enabled": False}

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # bootout
            MagicMock(returncode=0, stderr="", stdout=""),  # bootstrap
        ]

        result = _macos_restart()

        assert result["success"] is True
        assert "bootout" in result["method"]
        assert "bootstrap" in result["method"]

    @patch("gobby.cli.installers.service._get_service_status_macos")
    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_restart_bootstrap_failure(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        mock_status: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Restart reports bootstrap error on failure."""
        from gobby.cli.installers.service import _macos_restart

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file
        mock_status.return_value = {"enabled": False}

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # bootout
            MagicMock(returncode=1, stderr="bootstrap error", stdout=""),  # bootstrap fails
        ]

        result = _macos_restart()

        assert result["success"] is False
        assert "bootstrap" in result["error"]

    @patch("gobby.cli.installers.service.time.sleep")
    @patch("gobby.cli.installers.service._get_service_status_macos")
    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_restart_waits_for_bootout(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        mock_status: MagicMock,
        mock_sleep: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Restart polls until launchd fully unloads the service."""
        from gobby.cli.installers.service import _macos_restart

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file

        # Simulate launchd taking 3 polls to fully unload
        mock_status.side_effect = [
            {"enabled": True},
            {"enabled": True},
            {"enabled": True},
            {"enabled": False},
        ]

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # bootout
            MagicMock(returncode=0, stderr="", stdout=""),  # bootstrap
        ]

        result = _macos_restart()

        assert result["success"] is True
        assert mock_status.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("gobby.cli.installers.service._plist_path")
    def test_restart_fails_when_no_plist(
        self,
        mock_plist_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Restart fails early when plist is missing."""
        from gobby.cli.installers.service import _macos_restart

        mock_plist_path.return_value = tmp_path / "nonexistent.plist"

        result = _macos_restart()

        assert result["success"] is False
        assert "not installed" in result["error"].lower()


class TestMacOSUninstall:
    """Test macOS launchd uninstallation."""

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_uninstall_removes_plist(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Uninstall removes plist file and calls bootout."""
        from gobby.cli.installers.service import uninstall_service_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist_file
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = uninstall_service_macos()

        assert result["success"] is True
        assert not plist_file.exists()

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_uninstall_when_not_installed(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Uninstall succeeds gracefully when not installed."""
        from gobby.cli.installers.service import uninstall_service_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        mock_plist_path.return_value = plist_file
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = uninstall_service_macos()

        assert result["success"] is True


class TestMacOSStatus:
    """Test macOS service status detection."""

    @patch("gobby.cli.installers.service._plist_path")
    def test_status_not_installed(self, mock_plist_path: MagicMock, tmp_path: Path) -> None:
        """Reports not installed when plist doesn't exist."""
        from gobby.cli.installers.service import _get_service_status_macos

        mock_plist_path.return_value = tmp_path / "nonexistent.plist"

        result = _get_service_status_macos()

        assert result["installed"] is False
        assert result["running"] is False

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_status_installed_and_running(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Reports running when launchctl print shows pid."""
        from gobby.cli.installers.service import _get_service_status_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text(
            '<?xml version="1.0"?><plist><dict>'
            "<key>ProgramArguments</key><array>"
            f"<string>{sys.executable}</string>"
            "</array>"
            f"<key>WorkingDirectory</key><string>{tmp_path}</string>"
            "</dict></plist>"
        )
        mock_plist_path.return_value = plist_file

        # Realistic launchctl print output with nested state = active lines
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "com.gobby.daemon = {\n"
                "\tstate = running\n"
                "\tprogram = /usr/bin/python3\n"
                "\tpid = 12345\n"
                "\tsubprocess = {\n"
                "\t\tstate = active\n"
                "\t}\n"
                "\tanother = {\n"
                "\t\tstate = active\n"
                "\t}\n"
                "}\n"
            ),
            stderr="",
        )

        result = _get_service_status_macos()

        assert result["installed"] is True
        assert result["running"] is True
        assert result["pid"] == 12345

    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    def test_status_nested_state_does_not_override(
        self,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Nested `state = active` lines don't override top-level `state = running`."""
        from gobby.cli.installers.service import _get_service_status_macos

        plist_file = tmp_path / LAUNCHD_PLIST_NAME
        plist_file.write_text(
            '<?xml version="1.0"?><plist><dict>'
            "<key>ProgramArguments</key><array>"
            f"<string>{sys.executable}</string>"
            "</array>"
            f"<key>WorkingDirectory</key><string>{tmp_path}</string>"
            "</dict></plist>"
        )
        mock_plist_path.return_value = plist_file

        # state = running first, then nested state = active (the bug scenario)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=("\tstate = running\n\tpid = 99999\n\t\tstate = active\n\t\tstate = active\n"),
            stderr="",
        )

        result = _get_service_status_macos()

        assert result["running"] is True
        assert result["pid"] == 99999


# ---------------------------------------------------------------------------
# Linux (systemd) tests
# ---------------------------------------------------------------------------


class TestLinuxInstall:
    """Test Linux systemd installation."""

    @patch("gobby.cli.installers.service_linux._check_linger", return_value=[])
    @patch("gobby.cli.installers.service_linux.subprocess.run")
    @patch("gobby.cli.installers.service_linux._resolve_install_context")
    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_install_writes_unit_and_enables(
        self,
        mock_unit_path: MagicMock,
        mock_ctx: MagicMock,
        mock_run: MagicMock,
        mock_linger: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Install writes unit file and calls systemctl enable+start."""
        from gobby.cli.installers.service import install_service_linux

        unit_file = tmp_path / "systemd" / "user" / SYSTEMD_UNIT_NAME
        mock_unit_path.return_value = unit_file
        mock_ctx.return_value = {
            "python_executable": "/usr/bin/python3",
            "working_directory": "/home/test",
            "mode": "installed",
            "home_dir": "/home/test",
            "path_env": "/usr/bin:/bin",
            "log_file": "/tmp/gobby.log",
            "error_log_file": "/tmp/gobby-error.log",
            "gobby_home": "",
            "verbose": False,
        }
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = install_service_linux()

        assert result["success"] is True
        assert result["platform"] == "linux"
        assert unit_file.exists()
        content = unit_file.read_text()
        assert "[Unit]" in content
        assert "Restart=on-failure" in content

        # Verify systemctl was called 3 times: daemon-reload, enable, start
        systemctl_calls = [call for call in mock_run.call_args_list if call[0][0][0] == "systemctl"]
        assert len(systemctl_calls) == 3


class TestLinuxUninstall:
    """Test Linux systemd uninstallation."""

    @patch("gobby.cli.installers.service_linux.subprocess.run")
    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_uninstall_removes_unit(
        self,
        mock_unit_path: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Uninstall removes unit file and calls systemctl stop+disable."""
        from gobby.cli.installers.service import uninstall_service_linux

        unit_file = tmp_path / SYSTEMD_UNIT_NAME
        unit_file.write_text("[Unit]\nDescription=test\n")
        mock_unit_path.return_value = unit_file
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = uninstall_service_linux()

        assert result["success"] is True
        assert not unit_file.exists()


class TestLinuxStatus:
    """Test Linux service status detection."""

    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_status_not_installed(self, mock_unit_path: MagicMock, tmp_path: Path) -> None:
        """Reports not installed when unit file doesn't exist."""
        from gobby.cli.installers.service import _get_service_status_linux

        mock_unit_path.return_value = tmp_path / "nonexistent.service"

        result = _get_service_status_linux()

        assert result["installed"] is False
        assert result["running"] is False

    @patch("gobby.cli.installers.service_linux._check_linger", return_value=[])
    @patch("gobby.cli.installers.service_linux.subprocess.run")
    @patch("gobby.cli.installers.service_linux._systemd_unit_path")
    def test_status_installed_and_running(
        self,
        mock_unit_path: MagicMock,
        mock_run: MagicMock,
        mock_linger: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Reports running when systemctl shows active."""
        from gobby.cli.installers.service import _get_service_status_linux

        unit_file = tmp_path / SYSTEMD_UNIT_NAME
        unit_file.write_text("[Unit]\nDescription=test\n")
        mock_unit_path.return_value = unit_file

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if "is-enabled" in cmd:
                return MagicMock(returncode=0, stdout="enabled\n", stderr="")
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active\n", stderr="")
            if "show" in cmd:
                return MagicMock(returncode=0, stdout="MainPID=5678\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run

        result = _get_service_status_linux()

        assert result["installed"] is True
        assert result["enabled"] is True
        assert result["running"] is True
        assert result["pid"] == 5678


# ---------------------------------------------------------------------------
# Result dict structure
# ---------------------------------------------------------------------------


class TestResultDictStructure:
    """Verify result dicts have expected keys."""

    @patch("gobby.cli.installers.service.sys")
    def test_status_result_has_required_keys(self, mock_sys: MagicMock) -> None:
        """Status result always has installed, enabled, running, platform."""
        mock_sys.platform = "win32"
        result = get_service_status()
        assert "installed" in result
        assert "enabled" in result
        assert "running" in result
        assert "platform" in result

    @patch("gobby.cli.installers.service.sys")
    def test_install_failure_has_error_key(self, mock_sys: MagicMock) -> None:
        """Failed install has error key."""
        mock_sys.platform = "win32"
        result = install_service()
        assert result["success"] is False
        assert "error" in result

    @patch("gobby.cli.installers.service.sys")
    def test_uninstall_failure_has_error_key(self, mock_sys: MagicMock) -> None:
        """Failed uninstall has error key."""
        mock_sys.platform = "win32"
        result = uninstall_service()
        assert result["success"] is False
        assert "error" in result
