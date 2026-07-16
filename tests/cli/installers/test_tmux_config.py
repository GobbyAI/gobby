from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.cli.installers.tmux_config import configure_tmux_clipboard


def _which(command: str) -> str | None:
    return {
        "tmux": "/opt/homebrew/bin/tmux",
        "pbcopy": "/usr/bin/pbcopy",
    }.get(command)


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", side_effect=_which)
@patch("gobby.cli.installers.tmux_config.subprocess.run")
def test_configure_tmux_clipboard_preserves_existing_config(
    mock_run: MagicMock,
    _mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".tmux.conf"
    existing = "set -g mouse on\nset -g history-limit 10000\n"
    config_path.write_text(existing, encoding="utf-8")
    mock_run.return_value = MagicMock(returncode=0)

    result = configure_tmux_clipboard(home=tmp_path)

    content = config_path.read_text(encoding="utf-8")
    assert content.startswith(existing)
    assert "set-option -s set-clipboard off" in content
    assert "set-option -s copy-command 'pbcopy'" in content
    assert result == {
        "success": True,
        "skipped": False,
        "updated": True,
        "live_applied": True,
        "config_path": str(config_path),
        "error": None,
    }
    assert mock_run.call_args_list == [
        call(
            ["/opt/homebrew/bin/tmux", "set-option", "-s", "set-clipboard", "off"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
        call(
            ["/opt/homebrew/bin/tmux", "set-option", "-s", "copy-command", "pbcopy"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", side_effect=_which)
@patch("gobby.cli.installers.tmux_config.subprocess.run")
def test_configure_tmux_clipboard_is_idempotent(
    mock_run: MagicMock,
    _mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    first = configure_tmux_clipboard(home=tmp_path)
    second = configure_tmux_clipboard(home=tmp_path)

    content = (tmp_path / ".tmux.conf").read_text(encoding="utf-8")
    assert content.count("# BEGIN GOBBY MANAGED TMUX CLIPBOARD") == 1
    assert first["updated"] is True
    assert second["updated"] is False


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", side_effect=_which)
@patch("gobby.cli.installers.tmux_config.subprocess.run")
def test_configure_tmux_clipboard_uses_existing_xdg_config(
    mock_run: MagicMock,
    _mock_which: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_home = tmp_path / "xdg"
    xdg_config = xdg_home / "tmux" / "tmux.conf"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("set -g status off\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    mock_run.return_value = MagicMock(returncode=0)

    result = configure_tmux_clipboard(home=tmp_path)

    assert result["config_path"] == str(xdg_config)
    assert "copy-command 'pbcopy'" in xdg_config.read_text(encoding="utf-8")
    assert not (tmp_path / ".tmux.conf").exists()


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", side_effect=_which)
@patch("gobby.cli.installers.tmux_config.subprocess.run")
def test_configure_tmux_clipboard_preserves_config_symlink(
    mock_run: MagicMock,
    _mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "dotfiles" / "tmux.conf"
    target_path.parent.mkdir()
    target_path.write_text("set -g status off\n", encoding="utf-8")
    config_link = tmp_path / ".tmux.conf"
    config_link.symlink_to(target_path)
    mock_run.return_value = MagicMock(returncode=0)

    result = configure_tmux_clipboard(home=tmp_path)

    assert config_link.is_symlink()
    assert "copy-command 'pbcopy'" in target_path.read_text(encoding="utf-8")
    assert result["config_path"] == str(target_path)


@patch("gobby.cli.installers.tmux_config.sys.platform", "linux")
def test_configure_tmux_clipboard_skips_non_macos(tmp_path: Path) -> None:
    result = configure_tmux_clipboard(home=tmp_path)

    assert result["success"] is True
    assert result["skipped"] is True
    assert not (tmp_path / ".tmux.conf").exists()


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", return_value=None)
def test_configure_tmux_clipboard_skips_when_dependencies_are_missing(
    _mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    result = configure_tmux_clipboard(home=tmp_path)

    assert result["success"] is True
    assert result["skipped"] is True
    assert not (tmp_path / ".tmux.conf").exists()


@patch("gobby.cli.installers.tmux_config.sys.platform", "darwin")
@patch("gobby.cli.installers.tmux_config.shutil.which", side_effect=_which)
def test_configure_tmux_clipboard_preserves_non_utf8_config(
    _mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".tmux.conf"
    original = b"set -g mouse on\n\xff\n"
    config_path.write_bytes(original)

    result = configure_tmux_clipboard(home=tmp_path)

    assert result["success"] is False
    assert "UTF-8" in result["error"]
    assert config_path.read_bytes() == original
