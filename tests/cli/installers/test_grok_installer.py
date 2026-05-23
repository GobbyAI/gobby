"""Tests for Grok CLI hook installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _write_grok_template(install_dir: Path) -> None:
    template_dir = install_dir / "grok"
    template_dir.mkdir(parents=True)
    (template_dir / "hooks-template.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                }
            }
        ),
        encoding="utf-8",
    )


def test_install_grok_writes_native_hook_file(temp_dir: Path) -> None:
    from gobby.cli.installers.grok import install_grok

    install_dir = temp_dir / "install"
    _write_grok_template(install_dir)
    project_dir = temp_dir / "project"
    project_dir.mkdir()

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.cli.installers.grok.get_install_dir", return_value=install_dir),
        patch("gobby.cli.installers.grok.install_global_hooks"),
        patch(
            "gobby.cli.installers.grok.install_shared_content",
            return_value={"agents": [], "plugins": []},
        ),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
    ):
        result = install_grok(project_dir)

    hook_file = temp_dir / ".grok" / "hooks" / "gobby.json"
    assert result["success"] is True
    assert result["config_path"] == str(hook_file)
    assert result["hooks_installed"] == ["SessionStart", "PreToolUse", "Stop"]

    config = json.loads(hook_file.read_text(encoding="utf-8"))
    assert (
        config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=grok --type=session_start"
    )
    assert (
        config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=grok --type=pre_tool_use"
    )
    assert config["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("--cli=grok --type=stop")


def test_uninstall_grok_removes_gobby_hook_file(temp_dir: Path) -> None:
    from gobby.cli.installers.grok import uninstall_grok

    hook_file = temp_dir / ".grok" / "hooks" / "gobby.json"
    hook_file.parent.mkdir(parents=True)
    hook_file.write_text(
        json.dumps({"hooks": {"SessionStart": [], "PreToolUse": []}}),
        encoding="utf-8",
    )

    with patch.object(Path, "home", return_value=temp_dir):
        result = uninstall_grok(temp_dir)

    assert result["success"] is True
    assert result["hooks_removed"] == ["SessionStart", "PreToolUse"]
    assert result["files_removed"] == [str(hook_file)]
    assert not hook_file.exists()
