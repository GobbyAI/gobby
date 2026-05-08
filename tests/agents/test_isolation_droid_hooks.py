"""Tests for Droid hook fallback copying in isolated agent environments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.agents.isolation import _copy_cli_hooks

pytestmark = pytest.mark.unit


def _hook_commands(configs: list[dict[str, Any]]) -> list[str]:
    return [
        hook["command"]
        for config in configs
        for hook in config.get("hooks", [])
        if isinstance(hook, dict) and isinstance(hook.get("command"), str)
    ]


def _assert_gobby_droid_command(command: str, hook_type: str) -> None:
    assert command.endswith(f" --gobby-owned --cli=droid --type={hook_type}")


@pytest.mark.asyncio
async def test_droid_isolation_hooks_written_from_template(tmp_path: Path) -> None:
    target_path = tmp_path / "worktree"

    await _copy_cli_hooks(
        source_path=str(tmp_path / "source"),
        target_path=str(target_path),
        provider="droid",
    )

    hooks_file = target_path / ".factory" / "hooks" / "hooks.json"
    settings = json.loads(hooks_file.read_text())

    assert tuple(settings["hooks"]) == DROID_PASCAL_HOOK_NAMES
    for hook_type in DROID_PASCAL_HOOK_NAMES:
        commands = _hook_commands(settings["hooks"][hook_type])
        assert len(commands) == 1
        _assert_gobby_droid_command(commands[0], hook_type)


@pytest.mark.asyncio
async def test_droid_isolation_hooks_merge_existing_entries(tmp_path: Path) -> None:
    target_path = tmp_path / "clone"
    hooks_file = target_path / ".factory" / "hooks" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo custom"}],
                        },
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old-ghook --gobby-owned --cli=droid",
                                }
                            ],
                        },
                    ],
                    "CustomEvent": [
                        {"hooks": [{"type": "command", "command": "echo custom-event"}]}
                    ],
                },
                "other": {"preserved": True},
            }
        )
        + "\n"
    )

    await _copy_cli_hooks(
        source_path=str(tmp_path / "source"),
        target_path=str(target_path),
        provider="droid",
    )

    settings = json.loads(hooks_file.read_text())
    pre_tool_use = settings["hooks"]["PreToolUse"]
    commands = _hook_commands(pre_tool_use)

    assert settings["other"] == {"preserved": True}
    assert settings["hooks"]["CustomEvent"] == [
        {"hooks": [{"type": "command", "command": "echo custom-event"}]}
    ]
    assert "echo custom" in commands
    assert "old-ghook --gobby-owned --cli=droid" not in commands
    assert any(
        command.endswith(" --gobby-owned --cli=droid --type=PreToolUse") for command in commands
    )


@pytest.mark.asyncio
async def test_droid_isolation_hooks_are_idempotent(tmp_path: Path) -> None:
    target_path = tmp_path / "worktree"

    await _copy_cli_hooks(
        source_path=str(tmp_path / "source"),
        target_path=str(target_path),
        provider="droid",
    )
    hooks_file = target_path / ".factory" / "hooks" / "hooks.json"
    first_settings = json.loads(hooks_file.read_text())

    await _copy_cli_hooks(
        source_path=str(tmp_path / "source"),
        target_path=str(target_path),
        provider="droid",
    )

    assert json.loads(hooks_file.read_text()) == first_settings
