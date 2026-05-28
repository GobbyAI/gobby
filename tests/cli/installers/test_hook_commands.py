from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.hook_commands import (
    build_hook_command,
    config_contains_gobby_hook,
    is_gobby_hook_command,
    rewrite_hook_template_commands,
)

pytestmark = pytest.mark.unit


def test_build_hook_command_prefers_local_ghook(temp_dir: Path) -> None:
    ghook_bin = temp_dir / ".gobby" / "bin" / "ghook"
    ghook_bin.parent.mkdir(parents=True)
    ghook_bin.write_text("")
    ghook_bin.chmod(0o755)

    with patch.object(Path, "home", return_value=temp_dir):
        command = build_hook_command("codex", "SessionStart", temp_dir / ".gobby" / "hooks")

    assert "--gobby-owned" in command
    assert str(ghook_bin) in command
    assert "--cli=codex --type=SessionStart" in command


def test_build_hook_command_falls_back_to_bare_ghook(temp_dir: Path, tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="ghook",
        ),
    ):
        command = build_hook_command("gemini", "SessionStart", hooks_dir)

    assert command == "ghook --gobby-owned --cli=gemini --type=SessionStart"


def test_build_stop_hook_command_uses_raw_ghook() -> None:
    command = build_hook_command(
        "codex",
        "Stop",
        Path("/tmp/gobby-hooks"),
        ghook_bin="ghook",
    )

    assert command == "ghook --gobby-owned --cli=codex --type=Stop"


def test_rewrite_hook_template_commands_updates_nested_entries() -> None:
    config = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "legacy"}]},
            ]
        }
    }

    rewrite_hook_template_commands(
        config,
        cli_name="claude",
        hooks_dir=Path("/tmp/hooks"),
        ghook_bin="/Users/test/.gobby/bin/ghook",
    )

    assert (
        config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=claude --type=SessionStart"
    )


def test_gobby_hook_detection_accepts_ghook_marker() -> None:
    assert is_gobby_hook_command("/Users/test/.gobby/bin/ghook --gobby-owned --cli=codex")
    assert config_contains_gobby_hook(
        {"hooks": [{"type": "command", "command": "ghook --gobby-owned --cli=gemini"}]}
    )


def test_gobby_hook_detection_rejects_legacy_dispatcher_commands() -> None:
    assert not is_gobby_hook_command("uv run /tmp/hooks/hook_dispatcher.py --cli=codex")
    assert not config_contains_gobby_hook(
        {"hooks": [{"type": "command", "command": "hook_dispatcher.py --cli=gemini"}]}
    )


def test_gobby_hook_detection_ignores_scalar_metadata_values() -> None:
    assert not config_contains_gobby_hook(
        {"metadata": {"description": "ghook --gobby-owned is just documentation"}}
    )
