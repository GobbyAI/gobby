from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.hook_commands import (
    build_hook_command,
    config_contains_gobby_hook,
    is_gobby_hook_command,
    rewrite_hook_template_commands,
    set_gobby_hook_timeouts,
)

pytestmark = pytest.mark.unit


def test_build_hook_command_prefers_local_ghook(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOBBY_NATIVE_BIN_DIR", raising=False)
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
        command = build_hook_command("qwen", "SessionStart", hooks_dir)

    assert command == "ghook --gobby-owned --cli=qwen --type=SessionStart"


def test_build_stop_hook_command_uses_raw_ghook() -> None:
    command = build_hook_command(
        "codex",
        "Stop",
        Path("/tmp/gobby-hooks"),
        ghook_bin="ghook",
    )

    assert command == "ghook --gobby-owned --cli=codex --type=Stop"


def test_rewrite_preserves_kebab_type_under_pascalcase_key() -> None:
    """The rewrite swaps only the ghook prefix and keeps the template ``--type``.

    Claude settings keys are PascalCase but the native ``--type`` token is kebab;
    re-deriving ``--type`` from the PascalCase key is what dropped Claude hooks to
    NOTIFICATION. The kebab token must survive the rewrite.
    """
    config = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "ghook --gobby-owned --cli=claude --type=user-prompt-submit"
                            ),
                        }
                    ]
                },
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
        config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=claude --type=user-prompt-submit"
    )


def test_rewrite_fills_droid_placeholder_from_key() -> None:
    """Bare ``__GOBBY_HOOK_COMMAND__`` placeholders (Droid) build from the hook key."""
    config = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "__GOBBY_HOOK_COMMAND__"}],
                },
            ]
        }
    }

    rewrite_hook_template_commands(
        config,
        cli_name="droid",
        hooks_dir=Path("/tmp/hooks"),
        ghook_bin="/Users/test/.gobby/bin/ghook",
    )

    assert (
        config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=droid --type=PreToolUse"
    )


def test_rewrite_swaps_ghook_prefix_but_keeps_flags() -> None:
    """Only the executable prefix is rewritten; ``--cli``/``--type`` are preserved."""
    config = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": ("old-ghook --gobby-owned --cli=claude --type=pre-tool-use"),
                        }
                    ]
                },
            ]
        }
    }

    rewrite_hook_template_commands(
        config,
        cli_name="claude",
        hooks_dir=Path("/tmp/hooks"),
        ghook_bin="/new/path/ghook",
    )

    assert (
        config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        == "/new/path/ghook --gobby-owned --cli=claude --type=pre-tool-use"
    )


def test_rewrite_leaves_foreign_commands_untouched() -> None:
    """Non-Gobby commands (no ``--gobby-owned`` marker, not a placeholder) are kept."""
    config = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "some-user-script --foo"}]},
            ]
        }
    }

    rewrite_hook_template_commands(
        config,
        cli_name="claude",
        hooks_dir=Path("/tmp/hooks"),
        ghook_bin="/Users/test/.gobby/bin/ghook",
    )

    assert config["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "some-user-script --foo"


def test_gobby_hook_detection_accepts_ghook_marker() -> None:
    assert is_gobby_hook_command("/Users/test/.gobby/bin/ghook --gobby-owned --cli=codex")
    assert config_contains_gobby_hook(
        {"hooks": [{"type": "command", "command": "ghook --gobby-owned --cli=agy"}]}
    )


def test_gobby_hook_detection_rejects_legacy_dispatcher_commands() -> None:
    assert not is_gobby_hook_command("uv run /tmp/hooks/hook_dispatcher.py --cli=codex")
    assert not config_contains_gobby_hook(
        {"hooks": [{"type": "command", "command": "hook_dispatcher.py --cli=qwen"}]}
    )


def test_gobby_hook_detection_ignores_scalar_metadata_values() -> None:
    assert not config_contains_gobby_hook(
        {"metadata": {"description": "ghook --gobby-owned is just documentation"}}
    )


def test_set_gobby_hook_timeouts_preserves_foreign_handlers_and_applies_override() -> None:
    config = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "ghook --gobby-owned --cli=claude --type=session-start",
                            "timeout": 30,
                        },
                        {"type": "command", "command": "user-hook", "timeout": 7},
                    ]
                }
            ],
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "ghook --gobby-owned --cli=claude --type=session-end",
                            "timeout": 30,
                        }
                    ]
                }
            ],
        }
    }

    result = set_gobby_hook_timeouts(
        config,
        timeout=120,
        hook_overrides={"SessionEnd": 60},
    )

    assert result["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 120
    assert result["hooks"]["SessionStart"][0]["hooks"][1]["timeout"] == 7
    assert result["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 60
