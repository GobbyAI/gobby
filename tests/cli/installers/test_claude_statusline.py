"""Tests for statusLine configuration in Claude Code installer."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.claude import (
    _STATUSLINE_GHOOK_MARKER,
    _configure_statusline,
    _extract_downstream,
    _restore_statusline,
)

pytestmark = pytest.mark.unit


class TestConfigureStatusline:
    """Tests for _configure_statusline."""

    def test_probe_success_emits_ghook_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        ghook_bin = str(tmp_path / "ghook")

        monkeypatch.setattr(
            "gobby.cli.installers.claude.resolve_native_bin",
            lambda name: ghook_bin,
        )
        with patch("gobby.cli.installers.claude.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ghook 0.3.1\n", stderr="")
            _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert cmd == f"{ghook_bin} {_STATUSLINE_GHOOK_MARKER}"
        assert "statusline_handler.py" not in cmd

    def test_sets_statusline_when_none(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        _configure_statusline(settings, hooks_dir)

        assert "statusLine" in settings
        assert settings["statusLine"]["type"] == "command"
        assert "statusline_handler.py" in settings["statusLine"]["command"]
        assert "GOBBY_STATUSLINE_DOWNSTREAM" not in settings["statusLine"]["command"]

    def test_missing_ghook_keeps_python_handler(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert cmd.startswith("python3 ")
        assert "statusline_handler.py" in cmd
        assert _STATUSLINE_GHOOK_MARKER not in cmd

    @pytest.mark.parametrize(
        ("probe_result", "probe_side_effect"),
        [
            (MagicMock(returncode=0, stdout="ghook 0.3.0\n", stderr=""), None),
            (None, OSError("missing binary")),
            (MagicMock(returncode=1, stdout="", stderr="boom"), None),
        ],
    )
    def test_old_or_failed_ghook_probe_keeps_python_handler(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        probe_result: MagicMock | None,
        probe_side_effect: Exception | None,
    ) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        ghook_bin = str(tmp_path / "ghook")
        monkeypatch.setattr(
            "gobby.cli.installers.claude.resolve_native_bin",
            lambda name: ghook_bin,
        )

        with patch("gobby.cli.installers.claude.subprocess.run") as mock_run:
            if probe_side_effect is not None:
                mock_run.side_effect = probe_side_effect
            else:
                mock_run.return_value = probe_result
            _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert cmd.startswith("python3 ")
        assert "statusline_handler.py" in cmd
        assert _STATUSLINE_GHOOK_MARKER not in cmd

    def test_wraps_existing_command(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": "cship --color"}}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert "statusline_handler.py" in cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='cship --color'" in cmd

    def test_idempotent_rewrap(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        # First install with downstream
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": "cship"}}
        _configure_statusline(settings, hooks_dir)

        # Second install (idempotent)
        _configure_statusline(settings, hooks_dir)
        second_cmd = settings["statusLine"]["command"]

        assert "statusline_handler.py" in second_cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='cship'" in second_cmd
        # Paths may differ due to resolve(), but downstream is preserved
        assert "cship" in second_cmd

    @pytest.mark.parametrize(
        "owned_command",
        [
            "GOBBY_STATUSLINE_DOWNSTREAM='cship --color' python3 /path/statusline_handler.py",
            "GOBBY_STATUSLINE_DOWNSTREAM='cship --color' "
            "/path/ghook --gobby-owned --cli=claude --type=statusline",
        ],
    )
    def test_preserves_downstream_from_owned_statusline_markers(
        self, tmp_path: Path, owned_command: str
    ) -> None:
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": owned_command}}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert "statusline_handler.py" in cmd
        assert _extract_downstream(cmd) == "cship --color"

    def test_handles_string_statusline(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {"statusLine": "some-command --flag"}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert "statusline_handler.py" in cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='some-command --flag'" in cmd

    def test_escapes_single_quotes(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {
            "statusLine": {"type": "command", "command": "cmd 'with quotes'"}
        }
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert "statusline_handler.py" in cmd
        # Single quotes should be escaped
        assert "GOBBY_STATUSLINE_DOWNSTREAM=" in cmd

    def test_round_trip_configure_extract(self, tmp_path: Path) -> None:
        """Configure with downstream, then extract — should recover original command."""
        original_downstream = "cship --color --theme=dark"
        settings: dict[str, Any] = {
            "statusLine": {"type": "command", "command": original_downstream}
        }
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "statusline_handler.py").touch()

        _configure_statusline(settings, hooks_dir)
        extracted = _extract_downstream(settings["statusLine"]["command"])
        assert extracted == original_downstream

    def test_round_trip_preserves_single_quotes(self, tmp_path: Path) -> None:
        original_downstream = "cmd 'with quotes'"
        settings: dict[str, Any] = {
            "statusLine": {"type": "command", "command": original_downstream}
        }
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        assert _extract_downstream(settings["statusLine"]["command"]) == original_downstream


class TestExtractDownstream:
    """Tests for _extract_downstream."""

    def test_extracts_simple_command(self) -> None:
        cmd = "GOBBY_STATUSLINE_DOWNSTREAM='cship' python3 /path/to/statusline_handler.py"
        assert _extract_downstream(cmd) == "cship"

    def test_extracts_command_with_flags(self) -> None:
        cmd = "GOBBY_STATUSLINE_DOWNSTREAM='cship --color --theme=dark' python3 /path/handler.py"
        assert _extract_downstream(cmd) == "cship --color --theme=dark"

    def test_returns_none_without_env_var(self) -> None:
        cmd = "python3 /path/to/statusline_handler.py"
        assert _extract_downstream(cmd) is None


class TestRestoreStatusline:
    """Tests for _restore_statusline."""

    @pytest.mark.parametrize(
        "command",
        [
            "GOBBY_STATUSLINE_DOWNSTREAM='cship' python3 /path/statusline_handler.py",
            "GOBBY_STATUSLINE_DOWNSTREAM='cship' "
            "/path/ghook --gobby-owned --cli=claude --type=statusline",
        ],
    )
    def test_restores_downstream(self, command: str) -> None:
        settings: dict[str, Any] = {
            "statusLine": {
                "type": "command",
                "command": command,
            }
        }
        _restore_statusline(settings)
        assert settings["statusLine"] == {"type": "command", "command": "cship"}

    @pytest.mark.parametrize(
        "command",
        [
            "python3 /path/statusline_handler.py",
            "/path/ghook --gobby-owned --cli=claude --type=statusline",
        ],
    )
    def test_removes_when_no_downstream(self, command: str) -> None:
        settings: dict[str, Any] = {
            "statusLine": {
                "type": "command",
                "command": command,
            }
        }
        _restore_statusline(settings)
        assert "statusLine" not in settings

    def test_no_op_for_foreign_statusline(self) -> None:
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": "cship"}}
        _restore_statusline(settings)
        assert settings["statusLine"] == {"type": "command", "command": "cship"}

    def test_no_op_when_missing(self) -> None:
        settings: dict[str, Any] = {}
        _restore_statusline(settings)
        assert "statusLine" not in settings
