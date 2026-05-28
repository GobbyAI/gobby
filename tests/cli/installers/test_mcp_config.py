"""Tests for MCP config installation functions.

Covers configure/remove for JSON, TOML, and project-scoped config files,
as well as install_default_mcp_servers.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.cli.installers.mcp_config import (
    _remove_toml_table_block,
    configure_mcp_server_json,
    configure_mcp_server_toml,
    configure_project_mcp_server,
    install_default_mcp_servers,
    remove_mcp_server_json,
    remove_mcp_server_toml,
    remove_project_mcp_server,
    strip_mcp_tool_overrides_toml,
)
from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE

pytestmark = pytest.mark.unit


def test_remove_toml_table_block_preserves_trailing_comments_after_last_table() -> None:
    content = (
        '[model]\nname = "gpt-5"\n\n[mcp_servers.gobby]\ncommand = "uv"\n\n# trailing comment\n\n'
    )

    updated = _remove_toml_table_block(content, table_prefix="mcp_servers.gobby")

    assert "[mcp_servers.gobby]" not in updated
    assert updated.endswith("\n# trailing comment\n\n")


def test_remove_toml_table_block_preserves_blank_comment_suffix_linearly() -> None:
    content = (
        "[mcp_servers.gobby]\n"
        'command = "gobby"\n'
        "\n"
        "# trailing comment\n"
        "\n"
        "[mcp_servers.other]\n"
        'command = "other"\n'
    )

    updated = _remove_toml_table_block(content, table_prefix="mcp_servers.gobby")

    assert updated.startswith("\n# trailing comment\n\n")
    assert "[mcp_servers.other]" in updated


# ---------------------------------------------------------------------------
# configure_mcp_server_json
# ---------------------------------------------------------------------------


class TestConfigureMCPServerJSON:
    """Tests for configure_mcp_server_json."""

    def test_creates_new_file(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        result = configure_mcp_server_json(settings)
        assert result["success"] is True
        assert result["added"] is True
        assert result["already_configured"] is False
        assert result["backup_path"] is None  # no backup for new file
        data = json.loads(settings.read_text())
        assert "gobby" in data["mcpServers"]
        assert data["mcpServers"]["gobby"]["command"].endswith("gobby")
        assert data["mcpServers"]["gobby"]["args"] == ["mcp-server"]

    def test_creates_new_file_with_extra_server_fields(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        result = configure_mcp_server_json(settings, extra_server_fields={"type": "stdio"})
        assert result["success"] is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["gobby"]["type"] == "stdio"
        assert data["mcpServers"]["gobby"]["args"] == ["mcp-server"]

    def test_merges_extra_server_fields_into_existing_server(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {"command": "gobby"}}}))
        result = configure_mcp_server_json(settings, extra_server_fields={"type": "stdio"})
        assert result["success"] is True
        assert result["updated"] is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["gobby"] == {"command": "gobby", "type": "stdio"}

    def test_adds_to_existing_file(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"other": {"command": "node"}}}))
        result = configure_mcp_server_json(settings)
        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is not None
        data = json.loads(settings.read_text())
        assert "gobby" in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_already_configured(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {"command": "uv"}}}))
        result = configure_mcp_server_json(settings)
        assert result["success"] is True
        assert result["already_configured"] is True
        assert result["added"] is False

    def test_repairs_uv_run_existing_server(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "gobby", "mcp-server"],
                        }
                    }
                }
            )
        )
        result = configure_mcp_server_json(settings)

        assert result["success"] is True
        assert result["updated"] is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["gobby"] == {
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_keeps_uv_run_project_existing_server(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        stale = {
            "command": "uv",
            "args": ["run", "--project", "/repo/gobby", "gobby", "mcp-server"],
        }
        settings.write_text(json.dumps({"mcpServers": {"gobby": stale}}))
        result = configure_mcp_server_json(settings)

        assert result["success"] is True
        assert result["already_configured"] is True
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["gobby"] == stale

    def test_invalid_json(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not valid json {{{")
        result = configure_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to parse" in result["error"]

    def test_read_os_error(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        with patch("builtins.open", side_effect=OSError("perm denied")):
            result = configure_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to read" in result["error"]

    def test_backup_failure(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"other": True}))
        with patch("gobby.cli.installers.mcp_config.copy2", side_effect=OSError("no space")):
            result = configure_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_write_failure(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        # File doesn't exist yet, so no backup needed
        with patch("builtins.open", side_effect=OSError("read-only")):
            result = configure_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to write" in result["error"]

    def test_custom_server_name(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        result = configure_mcp_server_json(settings, server_name="my-gobby")
        assert result["success"] is True
        data = json.loads(settings.read_text())
        assert "my-gobby" in data["mcpServers"]

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        settings = tmp_path / "deeply" / "nested" / "settings.json"
        result = configure_mcp_server_json(settings)
        assert result["success"] is True
        assert settings.exists()


# ---------------------------------------------------------------------------
# remove_mcp_server_json
# ---------------------------------------------------------------------------


class TestRemoveMCPServerJSON:
    """Tests for remove_mcp_server_json."""

    def test_file_not_exists(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        result = remove_mcp_server_json(settings)
        assert result["success"] is True
        assert result["removed"] is False

    def test_server_not_present(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"other": {}}}))
        result = remove_mcp_server_json(settings)
        assert result["success"] is True
        assert result["removed"] is False

    def test_removes_server(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {"command": "uv"}, "other": {}}}))
        result = remove_mcp_server_json(settings)
        assert result["success"] is True
        assert result["removed"] is True
        assert result["backup_path"] is not None
        data = json.loads(settings.read_text())
        assert "gobby" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_removes_last_server_cleans_section(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {}}}))
        result = remove_mcp_server_json(settings)
        assert result["success"] is True
        assert result["removed"] is True
        data = json.loads(settings.read_text())
        assert "mcpServers" not in data

    def test_invalid_json(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("bad json")
        result = remove_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to read" in result["error"]

    def test_no_mcp_servers_section(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"other_key": True}))
        result = remove_mcp_server_json(settings)
        assert result["success"] is True
        assert result["removed"] is False

    def test_backup_failure(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {}}}))
        with patch("gobby.cli.installers.mcp_config.copy2", side_effect=OSError("fail")):
            result = remove_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_write_failure(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"gobby": {}}}))
        # Allow copy2 but fail on write
        orig_open = open

        def mock_open(path, *args, **kwargs):
            if "w" in (args[0] if args else kwargs.get("mode", "r")):
                raise OSError("read-only fs")
            return orig_open(path, *args, **kwargs)

        with (
            patch("gobby.cli.installers.mcp_config.copy2"),
            patch("builtins.open", side_effect=mock_open),
        ):
            result = remove_mcp_server_json(settings)
        assert result["success"] is False
        assert "Failed to write" in result["error"]


# ---------------------------------------------------------------------------
# configure_mcp_server_toml
# ---------------------------------------------------------------------------


class TestConfigureMCPServerTOML:
    """Tests for configure_mcp_server_toml."""

    def test_creates_new_file(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        result = configure_mcp_server_toml(config)
        assert result["success"] is True
        assert result["added"] is True
        content = config.read_text()
        assert "[mcp_servers.gobby]" in content
        parsed = tomllib.loads(content)
        assert parsed["mcp_servers"]["gobby"] == {
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[model]\nname = "test"\n')
        result = configure_mcp_server_toml(config)
        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is not None
        content = config.read_text()
        assert "[mcp_servers.gobby]" in content
        assert "[model]" in content

    def test_already_configured(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[mcp_servers.gobby]\ncommand = "uv"\n')
        result = configure_mcp_server_toml(config)
        assert result["success"] is True
        assert result["already_configured"] is True

    def test_repairs_uv_run_stale_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\nargs = ["run", "gobby", "mcp-server"]\n'
        )
        result = configure_mcp_server_toml(config)

        assert result["success"] is True
        assert result["updated"] is True
        assert result["backup_path"] is not None
        parsed = tomllib.loads(config.read_text())
        assert parsed["mcp_servers"]["gobby"] == {
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_repairs_uv_run_directory_stale_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            'args = ["run", "--directory", "/repo/gobby", "gobby", "mcp-server"]\n'
        )
        result = configure_mcp_server_toml(config)

        assert result["success"] is True
        assert result["updated"] is True
        parsed = tomllib.loads(config.read_text())
        assert parsed["mcp_servers"]["gobby"] == {
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_keeps_uv_run_project_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            'args = ["run", "--project", "/repo/gobby", "gobby", "mcp-server"]\n'
        )
        result = configure_mcp_server_toml(config)

        assert result["success"] is True
        assert result["already_configured"] is True
        assert result["updated"] is False

    def test_read_error(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("content")
        with patch.object(Path, "read_text", side_effect=OSError("no perms")):
            result = configure_mcp_server_toml(config)
        assert result["success"] is False
        assert "Failed to read" in result["error"]

    def test_backup_failure(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("existing = true\n")
        with patch.object(Path, "write_text", side_effect=OSError("no space")):
            result = configure_mcp_server_toml(config)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_empty_existing_file(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("")
        result = configure_mcp_server_toml(config)
        assert result["success"] is True
        assert result["added"] is True
        content = config.read_text()
        assert "[mcp_servers.gobby]" in content

    def test_custom_server_name(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        result = configure_mcp_server_toml(config, server_name="custom")
        assert result["success"] is True
        content = config.read_text()
        assert "[mcp_servers.custom]" in content


# ---------------------------------------------------------------------------
# remove_mcp_server_toml
# ---------------------------------------------------------------------------


class TestRemoveMCPServerTOML:
    """Tests for remove_mcp_server_toml."""

    def test_file_not_exists(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        result = remove_mcp_server_toml(config)
        assert result["success"] is True
        assert result["removed"] is False

    def test_server_not_present(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[other]\nkey = "val"\n')
        result = remove_mcp_server_toml(config)
        assert result["success"] is True
        assert result["removed"] is False

    def test_removes_server(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\nargs = ["run", "gobby", "mcp-server"]\n'
            '\n[mcp_servers.other]\ncommand = "node"\n'
        )
        result = remove_mcp_server_toml(config)
        assert result["success"] is True
        assert result["removed"] is True
        assert result["backup_path"] is not None
        content = config.read_text()
        assert "gobby" not in content
        assert "other" in content

    def test_removes_last_server_cleans_section(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[mcp_servers.gobby]\ncommand = "uv"\n')
        result = remove_mcp_server_toml(config)
        assert result["success"] is True
        assert result["removed"] is True
        content = config.read_text()
        assert "gobby" not in content

    def test_invalid_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[invalid\ngarbage")
        result = remove_mcp_server_toml(config)
        assert result["success"] is False
        assert "Failed to parse TOML" in result["error"]

    def test_backup_failure(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[mcp_servers.gobby]\ncommand = "uv"\n')
        with patch.object(Path, "write_text", side_effect=OSError("fail")):
            result = remove_mcp_server_toml(config)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_preserves_comments(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            "# top comment\n"
            '[mcp_servers.gobby]\ncommand = "uv"\n\n'
            "# keep this comment\n"
            '[mcp_servers.other]\ncommand = "node"\n'
        )

        result = remove_mcp_server_toml(config)

        assert result["success"] is True
        content = config.read_text()
        assert "# top comment" in content
        assert "# keep this comment" in content


# ---------------------------------------------------------------------------
# strip_mcp_tool_overrides_toml
# ---------------------------------------------------------------------------


class TestStripMCPToolOverridesTOML:
    """Tests for strip_mcp_tool_overrides_toml."""

    def test_file_not_exists(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is False

    def test_no_mcp_servers(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('model = "gpt-5.4"\n')
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is False

    def test_server_not_present(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[mcp_servers.other]\ncommand = "node"\n')
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is False

    def test_no_tools_subtable(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\nargs = ["run", "gobby", "mcp-server"]\n'
        )
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is False

    def test_strips_tools(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            'args = ["run", "gobby", "mcp-server"]\n\n'
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n\n'
            "[mcp_servers.gobby.tools.get_tool_schema]\n"
            'approval_mode = "approve"\n'
        )
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is True
        content = config.read_text()
        assert "tools" not in content
        assert "approval_mode" not in content
        assert "command" in content
        assert "uv" in content

    def test_preserves_other_servers(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n\n'
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n\n'
            '[mcp_servers.other]\ncommand = "node"\n\n'
            "[mcp_servers.other.tools.search]\n"
            'approval_mode = "approve"\n'
        )
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["stripped"] is True
        import tomllib

        with open(config, "rb") as f:
            parsed = tomllib.load(f)
        # gobby tools stripped
        assert "tools" not in parsed["mcp_servers"]["gobby"]
        # other server tools preserved
        assert "tools" in parsed["mcp_servers"]["other"]
        assert "search" in parsed["mcp_servers"]["other"]["tools"]

    def test_creates_backup(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        original = (
            '[mcp_servers.gobby]\ncommand = "uv"\n\n'
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n'
        )
        config.write_text(original)
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is True
        assert result["backup_path"] is not None
        backup = Path(result["backup_path"])
        assert backup.exists()
        assert backup.read_text() == original

    def test_custom_server_name(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.custom]\ncommand = "uv"\n\n'
            "[mcp_servers.custom.tools.call_tool]\n"
            'approval_mode = "approve"\n'
        )
        result = strip_mcp_tool_overrides_toml(config, server_name="custom")
        assert result["success"] is True
        assert result["stripped"] is True
        import tomllib

        with open(config, "rb") as f:
            parsed = tomllib.load(f)
        assert "tools" not in parsed["mcp_servers"]["custom"]

    def test_invalid_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[invalid\ngarbage")
        result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is False
        assert "Failed to parse TOML" in result["error"]

    def test_backup_failure(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n\n'
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n'
        )
        with patch.object(Path, "write_text", side_effect=OSError("fail")):
            result = strip_mcp_tool_overrides_toml(config)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_preserves_comments(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            "# top comment\n"
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            "# keep server comment\n\n"
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n'
        )

        result = strip_mcp_tool_overrides_toml(config)

        assert result["success"] is True
        content = config.read_text()
        assert "# top comment" in content
        assert "# keep server comment" in content


# ---------------------------------------------------------------------------
# configure_project_mcp_server
# ---------------------------------------------------------------------------


class TestConfigureProjectMCPServer:
    """Tests for configure_project_mcp_server (project-scoped config)."""

    def test_creates_new_settings(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["added"] is True
        data = json.loads((tmp_path / ".claude.json").read_text())
        abs_path = str(project_path.resolve())
        assert data["projects"][abs_path]["mcpServers"]["gobby"] == {
            "type": "stdio",
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_already_configured(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        abs_path = str(project_path.resolve())
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(
            json.dumps({"projects": {abs_path: {"mcpServers": {"gobby": {"command": "uv"}}}}})
        )
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["already_configured"] is True

    def test_repairs_project_scoped_uv_run_config(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        abs_path = str(project_path.resolve())
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(
            json.dumps(
                {
                    "projects": {
                        abs_path: {
                            "mcpServers": {
                                "gobby": {
                                    "type": "stdio",
                                    "command": "uv",
                                    "args": ["run", "gobby", "mcp-server"],
                                }
                            }
                        }
                    }
                }
            )
        )
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = configure_project_mcp_server(project_path)

        assert result["success"] is True
        assert result["updated"] is True
        data = json.loads(settings_path.read_text())
        assert data["projects"][abs_path]["mcpServers"]["gobby"] == {
            "type": "stdio",
            "command": "gobby",
            "args": ["mcp-server"],
        }

    def test_invalid_json(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text("bad json{{{")
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is False
        assert "Failed to parse" in result["error"]

    def test_read_os_error(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text("{}")
        with (
            patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path),
            patch("builtins.open", side_effect=OSError("denied")),
        ):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is False
        assert "error" in result

    def test_backup_failure(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(json.dumps({"projects": {}}))
        with (
            patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path),
            patch("gobby.cli.installers.mcp_config.copy2", side_effect=OSError("fail")),
        ):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_adds_projects_section(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(json.dumps({"other": True}))
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = configure_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["added"] is True


# ---------------------------------------------------------------------------
# remove_project_mcp_server
# ---------------------------------------------------------------------------


class TestRemoveProjectMCPServer:
    """Tests for remove_project_mcp_server."""

    def test_file_not_exists(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = remove_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["removed"] is False

    def test_server_not_present(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(json.dumps({"projects": {}}))
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = remove_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["removed"] is False

    def test_removes_server(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        abs_path = str(project_path.resolve())
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(
            json.dumps({"projects": {abs_path: {"mcpServers": {"gobby": {"command": "uv"}}}}})
        )
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = remove_project_mcp_server(project_path)
        assert result["success"] is True
        assert result["removed"] is True
        assert result["backup_path"] is not None
        data = json.loads(settings_path.read_text())
        project_servers = data.get("projects", {}).get(abs_path, {}).get("mcpServers", {})
        assert "gobby" not in project_servers

    def test_invalid_json(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text("bad")
        with patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path):
            result = remove_project_mcp_server(project_path)
        assert result["success"] is False
        assert "Failed to read" in result["error"]

    def test_backup_failure(self, tmp_path: Path) -> None:
        project_path = tmp_path / "my-project"
        project_path.mkdir()
        abs_path = str(project_path.resolve())
        settings_path = tmp_path / ".claude.json"
        settings_path.write_text(
            json.dumps({"projects": {abs_path: {"mcpServers": {"gobby": {}}}}})
        )
        with (
            patch("gobby.cli.installers.mcp_config.Path.home", return_value=tmp_path),
            patch("gobby.cli.installers.mcp_config.copy2", side_effect=OSError("fail")),
        ):
            result = remove_project_mcp_server(project_path)
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]


# ---------------------------------------------------------------------------
# install_default_mcp_servers
# ---------------------------------------------------------------------------


class TestInstallDefaultMCPServers:
    """Tests for install_default_mcp_servers."""

    def test_installs_defaults(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mock_secret_store = MagicMock()
        mock_secret_store.exists.return_value = False
        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()
        assert result["success"] is True
        assert len(result["servers_added"]) > 0
        assert "github" in result["servers_added"]
        assert "playwright" in result["servers_added"]
        assert "chrome-devtools" in result["servers_added"]
        mock_mcp_mgr.return_value.normalize_bundled_servers.assert_called_once_with()

        config = json.loads(mcp_path.read_text())
        playwright_server = next(
            server for server in config["servers"] if server["name"] == "playwright"
        )
        assert playwright_server["args"] == ["-y", "@playwright/mcp@latest"]
        chrome_server = next(
            server for server in config["servers"] if server["name"] == "chrome-devtools"
        )
        assert chrome_server["args"] == [
            "-y",
            CHROME_DEVTOOLS_NPM_PACKAGE,
            "--no-usage-statistics",
        ]

    def test_skips_existing_servers(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "servers": [
                        {"name": "github", "transport": "stdio", "command": "npx"},
                        {"name": "linear", "transport": "stdio", "command": "npx"},
                        {"name": "brave-search", "transport": "stdio", "command": "npx"},
                        {"name": "context7", "transport": "stdio", "command": "npx"},
                        {"name": "playwright", "transport": "stdio", "command": "npx"},
                        {"name": "chrome-devtools", "transport": "stdio", "command": "npx"},
                    ]
                }
            )
        )
        mock_secret_store = MagicMock()
        mock_secret_store.exists.return_value = False
        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 0
            result = install_default_mcp_servers()
        assert result["success"] is True
        assert len(result["servers_skipped"]) == 6
        assert len(result["servers_added"]) == 0
        mock_mcp_mgr.return_value.normalize_bundled_servers.assert_called_once_with()

    def test_read_error(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text("bad json{{{")
        with patch(
            "gobby.cli.installers.mcp_config.Path.expanduser",
            return_value=mcp_path,
        ):
            result = install_default_mcp_servers()
        assert result["success"] is False
        assert "Failed to read" in result["error"]

    def test_empty_file(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text("")
        mock_secret_store = MagicMock()
        mock_secret_store.exists.return_value = False
        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()
        assert result["success"] is True
        assert len(result["servers_added"]) > 0
        assert mcp_path.read_text() != ""

    def test_repairs_misconfigured_transport(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "github",
                            "transport": "http",
                            "url": "http://old-url",
                            "env": {"WRONG_KEY": "old"},
                        },
                    ]
                }
            )
        )
        mock_secret_store = MagicMock()
        mock_secret_store.exists.return_value = False
        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()
        assert result["success"] is True
        assert "github" in result["servers_repaired"]

    def test_no_servers_key_in_existing_config(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps({"other_key": True}))
        mock_secret_store = MagicMock()
        mock_secret_store.exists.return_value = False
        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()
        assert result["success"] is True
        assert len(result["servers_added"]) > 0

    def test_secret_store_operational_error_skips_optional_args(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected PostgreSQL failures should skip optional secret-backed MCP args."""
        mcp_path = tmp_path / ".gobby" / ".mcp.json"

        with (
            caplog.at_level("WARNING", logger="gobby.cli.installers.mcp_config"),
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch(
                "gobby.storage.secrets.SecretStore",
                side_effect=psycopg.OperationalError("database is locked"),
            ),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()

        assert result["success"] is True
        assert "Failed to initialize secret store for optional MCP args" in caplog.text
        config = json.loads(mcp_path.read_text())
        context7 = next(server for server in config["servers"] if server["name"] == "context7")
        assert context7["args"] == ["-y", "@upstash/context7-mcp"]

    def test_secret_store_unexpected_init_error_reraises(self, tmp_path: Path) -> None:
        """Unexpected secret-store init errors should still surface."""
        mcp_path = tmp_path / ".gobby" / ".mcp.json"

        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.secrets.SecretStore", side_effect=TypeError("bad init")),
        ):
            with pytest.raises(TypeError, match="bad init"):
                install_default_mcp_servers()

    def test_optional_secret_read_postgres_error_skips_extra_args(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected PostgreSQL read failures should omit optional extra args."""
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mock_secret_store = MagicMock()
        mock_secret_store.exists.side_effect = psycopg.DatabaseError("read failed")

        with (
            caplog.at_level("WARNING", logger="gobby.cli.installers.mcp_config"),
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.mcp.LocalMCPManager") as mock_mcp_mgr,
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            mock_mcp_mgr.return_value.import_from_mcp_json.return_value = 3
            result = install_default_mcp_servers()

        assert result["success"] is True
        assert "Failed to read optional MCP secret" in caplog.text
        assert "context7_api_key" not in caplog.text
        config = json.loads(mcp_path.read_text())
        context7 = next(server for server in config["servers"] if server["name"] == "context7")
        assert context7["args"] == ["-y", "@upstash/context7-mcp"]

    def test_optional_secret_read_unexpected_error_reraises(self, tmp_path: Path) -> None:
        """Unexpected optional-secret read errors should still surface."""
        mcp_path = tmp_path / ".gobby" / ".mcp.json"
        mock_secret_store = MagicMock()
        mock_secret_store.exists.side_effect = TypeError("bad read")

        with (
            patch(
                "gobby.cli.installers.mcp_config.Path.expanduser",
                return_value=mcp_path,
            ),
            patch("gobby.storage.hub.runtime.open_runtime_hub_database"),
            patch("gobby.storage.secrets.SecretStore", return_value=mock_secret_store),
        ):
            with pytest.raises(TypeError, match="bad read"):
                install_default_mcp_servers()
        assert mock_secret_store.get.call_count == 0
