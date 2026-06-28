"""Tests for the AGY CLI installer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.adapters.agy_contract import AGY_HOOK_NAMES
from gobby.cli.installers.agy import install_agy, uninstall_agy

pytestmark = pytest.mark.unit


@pytest.fixture
def project_path(temp_dir: Path) -> Path:
    project = temp_dir / "project"
    project.mkdir()
    return project


@pytest.fixture
def agy_env(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.delenv("GOBBY_HOOKS_DIR", raising=False)
    monkeypatch.delenv("GOBBY_AGY_HOOKS_FILE", raising=False)
    monkeypatch.delenv("GOBBY_AGY_MCP_FILE", raising=False)
    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
        patch("gobby.cli.installers.agy.install_shared_content", return_value={"plugins": []}),
        patch("gobby.cli.installers.agy.install_cli_content", return_value={"commands": []}),
    ):
        yield temp_dir


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_install_agy_global_writes_vendor_hooks_and_mcp(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    assert tuple(result["hooks_installed"]) == AGY_HOOK_NAMES
    assert result["trust"]["skipped"] is True
    assert result["trust"]["files_written"] == []
    assert not (agy_env / ".antigravitycli").exists()

    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks = _load_json(hooks_file)["hooks"]
    assert tuple(hooks) == AGY_HOOK_NAMES
    for hook_type in AGY_HOOK_NAMES:
        command = hooks[hook_type][0]["hooks"][0]["command"]
        expected = f"/Users/test/.gobby/bin/ghook --gobby-owned --cli=agy --type={hook_type}"
        assert command == expected

    mcp = _load_json(agy_env / ".gemini" / "config" / "mcp_config.json")
    assert mcp["mcpServers"]["gobby"]["type"] == "stdio"
    assert mcp["mcpServers"]["gobby"]["args"] == ["mcp-server"]


def test_install_agy_rejects_project_mode(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="project")

    assert result["success"] is False
    assert result["error"] == "AGY integration only supports global install mode"
    assert not (agy_env / ".gemini" / "config" / "hooks.json").exists()
    assert not (agy_env / ".gemini" / "config" / "mcp_config.json").exists()
    assert not (project_path / ".gemini" / "config" / "hooks.json").exists()


def test_install_agy_preserves_existing_mcp_servers(
    project_path: Path,
    agy_env: Path,
) -> None:
    mcp_file = agy_env / ".gemini" / "config" / "mcp_config.json"
    mcp_file.parent.mkdir(parents=True)
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"command": "node"},
                    "beta": {"command": "python"},
                }
            }
        )
    )

    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    mcp_servers = _load_json(mcp_file)["mcpServers"]
    assert set(mcp_servers) == {"alpha", "beta", "gobby"}
    assert mcp_servers["gobby"]["type"] == "stdio"


def test_install_agy_preserves_custom_hooks(
    project_path: Path,
    agy_env: Path,
) -> None:
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "custom --hook"}]},
                    ],
                    "Custom": [{"hooks": [{"type": "command", "command": "custom"}]}],
                }
            }
        )
    )

    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    hooks = _load_json(hooks_file)["hooks"]
    assert hooks["PreToolUse"][0]["hooks"][0]["command"] == "custom --hook"
    assert hooks["PreToolUse"][1]["hooks"][0]["command"].endswith("--cli=agy --type=PreToolUse")
    assert "Custom" in hooks


def test_uninstall_agy_removes_only_gobby_entries(
    project_path: Path,
    agy_env: Path,
) -> None:
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "custom"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "ghook --gobby-owned --cli=agy --type=PreToolUse",
                                }
                            ]
                        },
                    ],
                    "Custom": [{"hooks": [{"type": "command", "command": "custom"}]}],
                }
            }
        )
    )
    mcp_file = agy_env / ".gemini" / "config" / "mcp_config.json"
    mcp_file.write_text(
        json.dumps({"mcpServers": {"gobby": {"command": "gobby"}, "other": {"command": "node"}}})
    )

    result = uninstall_agy(project_path, mode="global")

    assert result["success"] is True
    assert result["hooks_removed"] == ["PreToolUse"]
    hooks = _load_json(hooks_file)["hooks"]
    assert hooks["PreToolUse"] == [{"hooks": [{"type": "command", "command": "custom"}]}]
    assert "Custom" in hooks
    assert set(_load_json(mcp_file)["mcpServers"]) == {"other"}
