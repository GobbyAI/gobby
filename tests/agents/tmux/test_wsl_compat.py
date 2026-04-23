"""Unit tests for WSL compatibility utilities."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl

pytestmark = pytest.mark.unit


class TestNeedsWsl:
    """Tests for needs_wsl() platform detection."""

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Windows")
    def test_windows(self, _mock: object) -> None:
        assert needs_wsl() is True

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Darwin")
    def test_macos(self, _mock: object) -> None:
        assert needs_wsl() is False

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Linux")
    def test_linux(self, _mock: object) -> None:
        assert needs_wsl() is False


class TestConvertWindowsPathToWsl:
    """Tests for convert_windows_path_to_wsl()."""

    def test_c_drive_backslash(self) -> None:
        assert convert_windows_path_to_wsl("C:\\Users\\foo") == "/mnt/c/Users/foo"

    def test_d_drive_forward_slash(self) -> None:
        assert convert_windows_path_to_wsl("D:/Projects/bar") == "/mnt/d/Projects/bar"

    def test_lowercase_drive(self) -> None:
        assert convert_windows_path_to_wsl("e:\\data") == "/mnt/e/data"

    def test_root_of_drive(self) -> None:
        assert convert_windows_path_to_wsl("C:\\") == "/mnt/c/"

    def test_already_unix(self) -> None:
        assert convert_windows_path_to_wsl("/already/unix") == "/already/unix"

    def test_relative_path(self) -> None:
        assert convert_windows_path_to_wsl("relative/path") == "relative/path"

    def test_mixed_separators(self) -> None:
        assert convert_windows_path_to_wsl("C:\\Users/foo\\bar") == "/mnt/c/Users/foo/bar"


class TestSessionManagerWslIntegration:
    """Tests for TmuxSessionManager WSL support."""

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Windows")
    def test_base_args_with_wsl(self, _mock: object) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        mgr = TmuxSessionManager()
        args = mgr._base_args()
        assert args[0] == "wsl"
        assert "tmux" in args
        assert "-L" in args

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Windows")
    def test_base_args_with_wsl_distribution(self, _mock: object) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionManager
        from gobby.config.tmux import TmuxConfig

        config = TmuxConfig(wsl_distribution="Ubuntu")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args[:3] == ["wsl", "-d", "Ubuntu"]

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Darwin")
    def test_base_args_without_wsl(self, _mock: object) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        mgr = TmuxSessionManager()
        args = mgr._base_args()
        assert args[0] == "tmux"

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Windows")
    @patch("shutil.which", return_value="/usr/bin/wsl")
    @patch("subprocess.run")
    def test_is_available_windows_with_wsl(
        self, mock_run: object, _which: object, _sys: object
    ) -> None:
        from unittest.mock import MagicMock

        from gobby.agents.tmux.session_manager import TmuxSessionManager

        result = MagicMock()
        result.returncode = 0
        assert isinstance(mock_run, MagicMock)
        mock_run.return_value = result

        mgr = TmuxSessionManager()
        assert mgr.is_available() is True

    @patch("gobby.agents.tmux.wsl_compat.platform.system", return_value="Windows")
    @patch("shutil.which", return_value=None)
    def test_is_available_windows_without_wsl(self, _which: object, _sys: object) -> None:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        mgr = TmuxSessionManager()
        assert mgr.is_available() is False
