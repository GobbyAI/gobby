"""Isolation repair must not rewrite a selected MCP symlink's target."""

import json
from pathlib import Path

import pytest

from gobby.agents.isolation_repair import _patch_mcp_config_for_isolation
from gobby.cli.installers.mcp_config_shared import gmcp_command


@pytest.mark.asyncio
async def test_mcp_repair_replaces_symlink_without_writing_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    target = tmp_path / "outside.json"
    original = '{"mcpServers":{"other":{"command":"other-server"}}}\n'
    target.write_text(original)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    config = isolated / ".mcp.json"
    config.symlink_to(target)

    await _patch_mcp_config_for_isolation(str(tmp_path), str(isolated), "codex")

    assert target.read_text() == original
    assert not config.is_symlink()
    assert json.loads(config.read_text())["mcpServers"] == {
        "other": {"command": "other-server"},
        "gobby": {"command": gmcp_command(), "args": []},
    }
