"""Tests for statusLine configuration in Claude Code installer."""

from pathlib import Path
from typing import Any

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

    def test_sets_statusline_to_resolved_ghook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        ghook_bin = str(tmp_path / "ghook")

        monkeypatch.setattr(
            "gobby.cli.installers.claude.resolve_native_bin_or_default",
            lambda name: ghook_bin,
        )
        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert cmd == f"{ghook_bin} {_STATUSLINE_GHOOK_MARKER}"

    def test_sets_statusline_when_none(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        assert "statusLine" in settings
        assert settings["statusLine"]["type"] == "command"
        assert _STATUSLINE_GHOOK_MARKER in settings["statusLine"]["command"]
        assert "GOBBY_STATUSLINE_DOWNSTREAM" not in settings["statusLine"]["command"]

    def test_missing_ghook_uses_bare_ghook_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings: dict[str, Any] = {}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        monkeypatch.setattr(
            "gobby.cli.installers.claude.resolve_native_bin_or_default",
            lambda name: "ghook",
        )

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert cmd == f"ghook {_STATUSLINE_GHOOK_MARKER}"

    def test_wraps_existing_command(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": "cship --color"}}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert _STATUSLINE_GHOOK_MARKER in cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='cship --color'" in cmd

    def test_idempotent_rewrap(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        # First install with downstream
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": "cship"}}
        _configure_statusline(settings, hooks_dir)

        # Second install (idempotent)
        _configure_statusline(settings, hooks_dir)
        second_cmd = settings["statusLine"]["command"]

        assert _STATUSLINE_GHOOK_MARKER in second_cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='cship'" in second_cmd
        assert "cship" in second_cmd

    @pytest.mark.parametrize(
        "owned_command",
        [
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
        assert _STATUSLINE_GHOOK_MARKER in cmd
        assert _extract_downstream(cmd) == "cship --color"

    def test_handles_string_statusline(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {"statusLine": "some-command --flag"}
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert _STATUSLINE_GHOOK_MARKER in cmd
        assert "GOBBY_STATUSLINE_DOWNSTREAM='some-command --flag'" in cmd

    def test_escapes_single_quotes(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {
            "statusLine": {"type": "command", "command": "cmd 'with quotes'"}
        }
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        _configure_statusline(settings, hooks_dir)

        cmd = settings["statusLine"]["command"]
        assert _STATUSLINE_GHOOK_MARKER in cmd
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
        cmd = "GOBBY_STATUSLINE_DOWNSTREAM='cship' ghook --gobby-owned --type=statusline"
        assert _extract_downstream(cmd) == "cship"

    def test_extracts_command_with_flags(self) -> None:
        cmd = "GOBBY_STATUSLINE_DOWNSTREAM='cship --color --theme=dark' python3 /path/handler.py"
        assert _extract_downstream(cmd) == "cship --color --theme=dark"

    def test_returns_none_without_env_var(self) -> None:
        cmd = "ghook --gobby-owned --type=statusline"
        assert _extract_downstream(cmd) is None


class TestRestoreStatusline:
    """Tests for _restore_statusline."""

    @pytest.mark.parametrize(
        "command",
        [
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
