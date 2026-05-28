"""Comprehensive tests for cli/installers/shared.py module.

Tests cover:
- install_shared_content: Installing workflows and plugins
- install_cli_content: Installing CLI-specific content
- configure_mcp_server_json: Adding MCP server to JSON settings
- remove_mcp_server_json: Removing MCP server from JSON settings
- configure_mcp_server_toml: Adding MCP server to TOML config
- remove_mcp_server_toml: Removing MCP server from TOML config
"""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.ide_config import (
    VSCODE_FAMILY_IDE_NAMES,
    _get_ide_config_dir,
    configure_ide_terminal_title,
    configure_vscode_family_terminal_titles,
)
from gobby.cli.installers.mcp_config import (
    configure_mcp_server_json,
    configure_mcp_server_toml,
    remove_mcp_server_json,
    remove_mcp_server_toml,
)
from gobby.cli.installers.shared import (
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)

pytestmark = pytest.mark.unit


def test_install_global_hooks_installs_validate_settings_only(temp_dir: Path) -> None:
    install_dir = temp_dir / "install"
    shared_hooks = install_dir / "shared" / "hooks"
    shared_hooks.mkdir(parents=True)
    (shared_hooks / "validate_settings.py").write_text("#!/usr/bin/env python3\n")

    hooks_dir = temp_dir / ".gobby" / "hooks"
    with (
        patch("gobby.cli.installers.shared.get_install_dir", return_value=install_dir),
        patch.dict(os.environ, {"GOBBY_HOOKS_DIR": str(hooks_dir)}),
    ):
        installed = install_global_hooks()

    assert installed == ["validate_settings.py"]
    assert not (hooks_dir / "ghook_guard.py").exists()
    assert (hooks_dir / "validate_settings.py").exists()
    assert os.access(hooks_dir / "validate_settings.py", os.X_OK)


def test_install_global_hooks_removes_legacy_ghook_guard(temp_dir: Path) -> None:
    install_dir = temp_dir / "install"
    shared_hooks = install_dir / "shared" / "hooks"
    shared_hooks.mkdir(parents=True)
    (shared_hooks / "validate_settings.py").write_text("#!/usr/bin/env python3\n")

    hooks_dir = temp_dir / ".gobby" / "hooks"
    hooks_dir.mkdir(parents=True)
    legacy_content = b"#!/usr/bin/env python3\n# old guard\n"
    (hooks_dir / "ghook_guard.py").write_bytes(legacy_content)
    legacy_hash = hashlib.sha256(legacy_content).hexdigest()

    with (
        patch("gobby.cli.installers.shared.get_install_dir", return_value=install_dir),
        patch("gobby.cli.installers.shared._LEGACY_GHOOK_GUARD_SHA256", legacy_hash),
        patch.dict(os.environ, {"GOBBY_HOOKS_DIR": str(hooks_dir)}),
    ):
        installed = install_global_hooks()

    assert installed == ["validate_settings.py"]
    assert not (hooks_dir / "ghook_guard.py").exists()


def test_install_global_hooks_preserves_custom_ghook_guard(temp_dir: Path) -> None:
    install_dir = temp_dir / "install"
    shared_hooks = install_dir / "shared" / "hooks"
    shared_hooks.mkdir(parents=True)
    (shared_hooks / "validate_settings.py").write_text("#!/usr/bin/env python3\n")

    hooks_dir = temp_dir / ".gobby" / "hooks"
    hooks_dir.mkdir(parents=True)
    custom_guard = hooks_dir / "ghook_guard.py"
    custom_guard.write_text("# custom guard\n")

    with (
        patch("gobby.cli.installers.shared.get_install_dir", return_value=install_dir),
        patch.dict(os.environ, {"GOBBY_HOOKS_DIR": str(hooks_dir)}),
    ):
        installed = install_global_hooks()

    assert installed == ["validate_settings.py"]
    assert custom_guard.read_text() == "# custom guard\n"


class TestInstallSharedContent:
    """Tests for install_shared_content function."""

    def test_install_shared_content_no_shared_dir(self, temp_dir: Path) -> None:
        """Test when shared directory doesn't exist."""
        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = temp_dir / "install"
            # Don't create shared dir
            result = install_shared_content(cli_path, project_path)

        assert result == {"plugins": [], "docs": []}

    def test_install_shared_workflows_db_managed(self, temp_dir: Path) -> None:
        """Test that workflow files are not installed (DB-managed via sync_bundled_content_to_db)."""
        install_dir = temp_dir / "install"
        shared_dir = install_dir / "shared"
        workflows_dir = shared_dir / "workflows"
        workflows_dir.mkdir(parents=True)

        # Create sample workflow files (should be ignored)
        (workflows_dir / "plan-execute.yaml").write_text("name: plan-execute")
        (workflows_dir / "test-driven.yaml").write_text("name: test-driven")

        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_shared_content(cli_path, project_path)

        # Workflows are DB-managed, not file-based
        assert "workflows" not in result
        assert not (project_path / ".gobby" / "workflows").exists()

    def test_install_shared_workflows_subdirectories_db_managed(self, temp_dir: Path) -> None:
        """Test that workflow subdirectories are not copied (DB-managed)."""
        install_dir = temp_dir / "install"
        shared_dir = install_dir / "shared"
        workflows_dir = shared_dir / "workflows"
        workflows_dir.mkdir(parents=True)

        # Create a file and a directory (should be ignored)
        (workflows_dir / "valid.yaml").write_text("name: valid")
        (workflows_dir / "lifecycle").mkdir()
        (workflows_dir / "lifecycle" / "session.yaml").write_text("name: session")

        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_shared_content(cli_path, project_path)

        # Workflows are DB-managed, not file-based
        assert "workflows" not in result
        assert not (project_path / ".gobby" / "workflows").exists()

    def test_install_shared_plugins(self, temp_dir: Path) -> None:
        """Test installing shared plugins to .gobby/plugins/ (project-scoped)."""
        install_dir = temp_dir / "install"
        shared_dir = install_dir / "shared"
        plugins_dir = shared_dir / "plugins"
        plugins_dir.mkdir(parents=True)

        # Create sample plugin files
        (plugins_dir / "notify.py").write_text("# Notification plugin")
        (plugins_dir / "audit.py").write_text("# Audit plugin")
        (plugins_dir / "README.md").write_text("# Not a plugin")

        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_shared_content(cli_path, project_path)

        # Only .py files should be installed
        assert "notify.py" in result["plugins"]
        assert "audit.py" in result["plugins"]
        assert "README.md" not in result["plugins"]
        # Verify they're installed to project path
        assert (project_path / ".gobby" / "plugins" / "notify.py").exists()
        assert (project_path / ".gobby" / "plugins" / "audit.py").exists()

    def test_install_shared_docs(self, temp_dir: Path) -> None:
        """Test installing shared docs to .gobby/docs/."""
        install_dir = temp_dir / "install"
        shared_dir = install_dir / "shared"
        docs_dir = shared_dir / "docs"
        docs_dir.mkdir(parents=True)

        # Create sample doc files
        (docs_dir / "spec-planning.md").write_text("# Spec Planning Guide")
        (docs_dir / "workflow-guide.md").write_text("# Workflow Guide")

        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_shared_content(cli_path, project_path)

        assert "spec-planning.md" in result["docs"]
        assert "workflow-guide.md" in result["docs"]
        assert (project_path / ".gobby" / "docs" / "spec-planning.md").exists()
        assert (project_path / ".gobby" / "docs" / "workflow-guide.md").exists()

    def test_install_shared_content_all_types(self, temp_dir: Path) -> None:
        """Test installing all content types at once."""
        install_dir = temp_dir / "install"
        shared_dir = install_dir / "shared"

        # Create workflows
        workflows_dir = shared_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "workflow1.yaml").write_text("name: workflow1")

        # Create plugins
        plugins_dir = shared_dir / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "plugin1.py").write_text("# Plugin 1")

        # Create docs
        docs_dir = shared_dir / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "guide.md").write_text("# Guide")

        cli_path = temp_dir / ".claude"
        project_path = temp_dir / "project"
        cli_path.mkdir(parents=True)
        project_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_shared_content(cli_path, project_path)

        # Workflows are DB-managed, not in result
        assert "workflows" not in result
        assert result["plugins"] == ["plugin1.py"]
        assert result["docs"] == ["guide.md"]
        # Verify project-scoped installation
        assert (project_path / ".gobby" / "plugins" / "plugin1.py").exists()


class TestInstallCliContent:
    """Tests for install_cli_content function."""

    def test_install_cli_content_no_cli_dir(self, temp_dir: Path) -> None:
        """Test when CLI-specific directory doesn't exist."""
        target_path = temp_dir / ".claude"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = temp_dir / "install"
            result = install_cli_content("claude", target_path)

        assert result == {"commands": []}

    def test_install_cli_workflows_db_managed(self, temp_dir: Path) -> None:
        """Test that CLI-specific workflows are not installed (DB-managed)."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "gemini"
        workflows_dir = cli_dir / "workflows"
        workflows_dir.mkdir(parents=True)

        (workflows_dir / "gemini-workflow.yaml").write_text("name: gemini-workflow")

        target_path = temp_dir / ".gemini"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("gemini", target_path)

        # Workflows are DB-managed, not file-based
        assert "workflows" not in result

    def test_install_cli_commands_directory(self, temp_dir: Path) -> None:
        """Test installing CLI commands from commands/ directory."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "claude"
        commands_dir = cli_dir / "commands"
        commands_dir.mkdir(parents=True)

        # Create command directory
        memory_dir = commands_dir / "memory"
        memory_dir.mkdir()
        (memory_dir / "remember.md").write_text("Remember something")
        (memory_dir / "recall.md").write_text("Recall something")

        # Create single command file
        (commands_dir / "status.md").write_text("Show status")

        target_path = temp_dir / ".claude"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("claude", target_path)

        assert "memory/" in result["commands"]
        assert "status.md" in result["commands"]
        assert (target_path / "commands" / "memory" / "remember.md").exists()
        assert (target_path / "commands" / "status.md").exists()

    def test_install_cli_prompts_directory(self, temp_dir: Path) -> None:
        """Test installing CLI commands from prompts/ directory (Codex style)."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "codex"
        prompts_dir = cli_dir / "prompts"
        prompts_dir.mkdir(parents=True)

        (prompts_dir / "commit.md").write_text("Create a commit")

        target_path = temp_dir / ".codex"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("codex", target_path)

        assert "commit.md" in result["commands"]
        assert (target_path / "prompts" / "commit.md").exists()

    def test_install_cli_commands_overwrites_existing_directory(self, temp_dir: Path) -> None:
        """Test that command directories are replaced entirely."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "claude"
        commands_dir = cli_dir / "commands"
        memory_dir = commands_dir / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "new-command.md").write_text("New command")

        target_path = temp_dir / ".claude"
        target_path.mkdir(parents=True)

        # Create existing command directory
        existing_memory = target_path / "commands" / "memory"
        existing_memory.mkdir(parents=True)
        (existing_memory / "old-command.md").write_text("Old command")

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("claude", target_path)

        assert "memory/" in result["commands"]
        # New command should exist
        assert (target_path / "commands" / "memory" / "new-command.md").exists()
        # Old command should be removed
        assert not (target_path / "commands" / "memory" / "old-command.md").exists()


class TestConfigureMcpServerJson:
    """Tests for configure_mcp_server_json function."""

    def test_configure_new_settings_file(self, temp_dir: Path) -> None:
        """Test creating new settings file with MCP server."""
        settings_path = temp_dir / ".claude" / "settings.json"

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["added"] is True
        assert result["already_configured"] is False
        assert result["backup_path"] is None  # No backup for new file
        assert result["error"] is None

        # Verify file contents
        settings = json.loads(settings_path.read_text())
        assert "mcpServers" in settings
        assert "gobby" in settings["mcpServers"]
        assert settings["mcpServers"]["gobby"]["command"].endswith("gobby")
        assert settings["mcpServers"]["gobby"]["args"] == ["mcp-server"]

    def test_configure_existing_settings_no_mcp(self, temp_dir: Path) -> None:
        """Test adding MCP server to existing settings without mcpServers."""
        settings_path = temp_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"otherSetting": "value"}))

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is not None

        settings = json.loads(settings_path.read_text())
        assert settings["otherSetting"] == "value"
        assert "gobby" in settings["mcpServers"]

    def test_configure_existing_settings_with_other_mcp(self, temp_dir: Path) -> None:
        """Test adding gobby to existing mcpServers."""
        settings_path = temp_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        existing = {"mcpServers": {"other-server": {"command": "other", "args": ["arg"]}}}
        settings_path.write_text(json.dumps(existing))

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["added"] is True

        settings = json.loads(settings_path.read_text())
        assert "other-server" in settings["mcpServers"]
        assert "gobby" in settings["mcpServers"]

    def test_configure_already_configured(self, temp_dir: Path) -> None:
        """Test when gobby is already configured."""
        settings_path = temp_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        existing = {"mcpServers": {"gobby": {"command": "existing", "args": []}}}
        settings_path.write_text(json.dumps(existing))

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["added"] is False
        assert result["already_configured"] is True
        assert result["backup_path"] is None  # No backup when already configured

    def test_configure_custom_server_name(self, temp_dir: Path) -> None:
        """Test using a custom server name."""
        settings_path = temp_dir / ".claude" / "settings.json"

        result = configure_mcp_server_json(settings_path, server_name="custom-gobby")

        assert result["success"] is True
        assert result["added"] is True

        settings = json.loads(settings_path.read_text())
        assert "custom-gobby" in settings["mcpServers"]
        assert "gobby" not in settings["mcpServers"]

    def test_configure_invalid_json(self, temp_dir: Path) -> None:
        """Test handling invalid JSON in settings file."""
        settings_path = temp_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text("{ invalid json }")

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to parse" in result["error"]

    def test_configure_read_permission_error(self, temp_dir: Path) -> None:
        """Test handling read permission error."""
        settings_path = temp_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text("{}")
        settings_path.chmod(0o000)

        try:
            result = configure_mcp_server_json(settings_path)
            assert result["success"] is False
            assert result["error"] is not None
        finally:
            settings_path.chmod(0o644)

    def test_configure_creates_parent_directory(self, temp_dir: Path) -> None:
        """Test that parent directory is created if it doesn't exist."""
        settings_path = temp_dir / "deep" / "nested" / "settings.json"

        result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert settings_path.exists()

    def test_configure_backup_created(self, temp_dir: Path) -> None:
        """Test that backup file is created for existing settings."""
        settings_path = temp_dir / "settings.json"
        settings_path.write_text('{"existing": true}')

        with patch("gobby.cli.installers.mcp_config.time") as mock_time:
            mock_time.time.return_value = 1234567890
            result = configure_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["backup_path"] is not None
        assert "1234567890" in result["backup_path"]

        backup_path = Path(result["backup_path"])
        assert backup_path.exists()
        backup_content = json.loads(backup_path.read_text())
        assert backup_content["existing"] is True


class TestRemoveMcpServerJson:
    """Tests for remove_mcp_server_json function."""

    def test_remove_nonexistent_file(self, temp_dir: Path) -> None:
        """Test removing from nonexistent file."""
        settings_path = temp_dir / "settings.json"

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["removed"] is False

    def test_remove_no_mcp_servers_section(self, temp_dir: Path) -> None:
        """Test removing when no mcpServers section exists."""
        settings_path = temp_dir / "settings.json"
        settings_path.write_text('{"other": "value"}')

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["removed"] is False

    def test_remove_server_not_present(self, temp_dir: Path) -> None:
        """Test removing when server isn't in mcpServers."""
        settings_path = temp_dir / "settings.json"
        settings_path.write_text('{"mcpServers": {"other": {}}}')

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["removed"] is False

    def test_remove_server_successfully(self, temp_dir: Path) -> None:
        """Test successfully removing MCP server."""
        settings_path = temp_dir / "settings.json"
        existing = {
            "mcpServers": {
                "gobby": {"command": "uv", "args": ["run", "gobby", "mcp-server"]},
                "other": {"command": "other"},
            }
        }
        settings_path.write_text(json.dumps(existing))

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["removed"] is True
        assert result["backup_path"] is not None

        settings = json.loads(settings_path.read_text())
        assert "gobby" not in settings["mcpServers"]
        assert "other" in settings["mcpServers"]

    def test_remove_last_server_cleans_section(self, temp_dir: Path) -> None:
        """Test removing the last server cleans up mcpServers section."""
        settings_path = temp_dir / "settings.json"
        existing = {"mcpServers": {"gobby": {"command": "uv"}}, "otherSetting": "preserved"}
        settings_path.write_text(json.dumps(existing))

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is True
        assert result["removed"] is True

        settings = json.loads(settings_path.read_text())
        assert "mcpServers" not in settings
        assert settings["otherSetting"] == "preserved"

    def test_remove_custom_server_name(self, temp_dir: Path) -> None:
        """Test removing with custom server name."""
        settings_path = temp_dir / "settings.json"
        existing = {
            "mcpServers": {"custom-gobby": {"command": "uv"}, "gobby": {"command": "other"}}
        }
        settings_path.write_text(json.dumps(existing))

        result = remove_mcp_server_json(settings_path, server_name="custom-gobby")

        assert result["success"] is True
        assert result["removed"] is True

        settings = json.loads(settings_path.read_text())
        assert "custom-gobby" not in settings["mcpServers"]
        assert "gobby" in settings["mcpServers"]

    def test_remove_invalid_json(self, temp_dir: Path) -> None:
        """Test handling invalid JSON."""
        settings_path = temp_dir / "settings.json"
        settings_path.write_text("not valid json")

        result = remove_mcp_server_json(settings_path)

        assert result["success"] is False
        assert result["error"] is not None

    def test_remove_creates_backup(self, temp_dir: Path) -> None:
        """Test that backup is created before removal."""
        settings_path = temp_dir / "settings.json"
        existing = {"mcpServers": {"gobby": {"command": "uv"}}}
        settings_path.write_text(json.dumps(existing))

        with patch("gobby.cli.installers.mcp_config.time") as mock_time:
            mock_time.time.return_value = 9876543210
            result = remove_mcp_server_json(settings_path)

        assert result["backup_path"] is not None
        assert "9876543210" in result["backup_path"]
        assert Path(result["backup_path"]).exists()


class TestConfigureMcpServerToml:
    """Tests for configure_mcp_server_toml function."""

    def test_configure_new_toml_file(self, temp_dir: Path) -> None:
        """Test creating new TOML file with MCP server."""
        config_path = temp_dir / ".codex" / "config.toml"

        result = configure_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["added"] is True
        assert result["already_configured"] is False

        content = config_path.read_text()
        assert "[mcp_servers.gobby]" in content
        assert 'command = "gobby"' in content
        assert 'args = ["mcp-server"]' in content

    def test_configure_existing_toml_no_mcp(self, temp_dir: Path) -> None:
        """Test adding MCP server to existing TOML without mcp_servers."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('model = "gpt-4"\n')

        result = configure_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is not None

        content = config_path.read_text()
        assert 'model = "gpt-4"' in content
        assert "[mcp_servers.gobby]" in content

    def test_configure_already_configured_toml(self, temp_dir: Path) -> None:
        """Test when server is already configured in TOML."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('[mcp_servers.gobby]\ncommand = "existing"\n')

        result = configure_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["added"] is False
        assert result["already_configured"] is True
        assert result["backup_path"] is None

    def test_configure_custom_server_name_toml(self, temp_dir: Path) -> None:
        """Test using custom server name in TOML."""
        config_path = temp_dir / "config.toml"

        result = configure_mcp_server_toml(config_path, server_name="my-gobby")

        assert result["success"] is True
        assert result["added"] is True

        content = config_path.read_text()
        assert "[mcp_servers.my-gobby]" in content

    def test_configure_toml_creates_parent_directory(self, temp_dir: Path) -> None:
        """Test that parent directory is created."""
        config_path = temp_dir / "deep" / "path" / "config.toml"

        result = configure_mcp_server_toml(config_path)

        assert result["success"] is True
        assert config_path.exists()

    def test_configure_toml_backup_created(self, temp_dir: Path) -> None:
        """Test that backup is created for existing TOML."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('existing = "value"\n')

        with patch("gobby.cli.installers.mcp_config.time") as mock_time:
            mock_time.time.return_value = 1111111111
            result = configure_mcp_server_toml(config_path)

        assert result["backup_path"] is not None
        assert "1111111111" in result["backup_path"]
        assert Path(result["backup_path"]).exists()

    def test_configure_toml_preserves_empty_content(self, temp_dir: Path) -> None:
        """Test handling empty TOML file."""
        config_path = temp_dir / "config.toml"
        config_path.write_text("")

        result = configure_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["added"] is True

    def test_configure_toml_regex_escapes_server_name(self, temp_dir: Path) -> None:
        """Test that special characters in server name are handled."""
        config_path = temp_dir / "config.toml"
        config_path.write_text("")

        # This tests that the regex properly escapes the server name
        result = configure_mcp_server_toml(config_path, server_name="gobby.test")

        assert result["success"] is True
        content = config_path.read_text()
        assert "[mcp_servers.gobby.test]" in content


class TestRemoveMcpServerToml:
    """Tests for remove_mcp_server_toml function."""

    def test_remove_nonexistent_toml(self, temp_dir: Path) -> None:
        """Test removing from nonexistent file."""
        config_path = temp_dir / "config.toml"

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["removed"] is False

    def test_remove_server_not_in_toml(self, temp_dir: Path) -> None:
        """Test removing when server isn't present."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('[mcp_servers.other]\ncommand = "other"\n')

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["removed"] is False

    def test_remove_server_successfully_toml(self, temp_dir: Path) -> None:
        """Test successfully removing MCP server from TOML."""
        config_path = temp_dir / "config.toml"
        content = """[mcp_servers.gobby]
command = "uv"
args = ["run", "gobby", "mcp-server"]

[mcp_servers.other]
command = "other"
"""
        config_path.write_text(content)

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["removed"] is True
        assert result["backup_path"] is not None

        # Re-read the file - tomli_w reformats so check semantically
        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        assert "gobby" not in config.get("mcp_servers", {})
        assert "other" in config.get("mcp_servers", {})

    def test_remove_last_server_cleans_section_toml(self, temp_dir: Path) -> None:
        """Test removing the last server removes mcp_servers section."""
        config_path = temp_dir / "config.toml"
        content = """model = "gpt-4"

[mcp_servers.gobby]
command = "uv"
"""
        config_path.write_text(content)

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["removed"] is True

        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        assert "mcp_servers" not in config
        assert config.get("model") == "gpt-4"

    def test_remove_custom_server_name_toml(self, temp_dir: Path) -> None:
        """Test removing with custom server name."""
        config_path = temp_dir / "config.toml"
        content = """[mcp_servers.custom-gobby]
command = "uv"

[mcp_servers.gobby]
command = "default"
"""
        config_path.write_text(content)

        result = remove_mcp_server_toml(config_path, server_name="custom-gobby")

        assert result["success"] is True
        assert result["removed"] is True

        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        assert "custom-gobby" not in config["mcp_servers"]
        assert "gobby" in config["mcp_servers"]

    def test_remove_invalid_toml(self, temp_dir: Path) -> None:
        """Test handling invalid TOML."""
        config_path = temp_dir / "config.toml"
        config_path.write_text("[ invalid toml ]]")

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to parse TOML" in result["error"]

    def test_remove_toml_creates_backup(self, temp_dir: Path) -> None:
        """Test that backup is created before removal."""
        config_path = temp_dir / "config.toml"
        content = """[mcp_servers.gobby]
command = "uv"
"""
        config_path.write_text(content)

        with patch("gobby.cli.installers.mcp_config.time") as mock_time:
            mock_time.time.return_value = 2222222222
            result = remove_mcp_server_toml(config_path)

        assert result["backup_path"] is not None
        assert "2222222222" in result["backup_path"]
        assert Path(result["backup_path"]).exists()

    def test_remove_no_mcp_servers_section_toml(self, temp_dir: Path) -> None:
        """Test removing when no mcp_servers section exists."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('model = "gpt-4"\n')

        result = remove_mcp_server_toml(config_path)

        assert result["success"] is True
        assert result["removed"] is False


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_configure_json_write_error(self, temp_dir: Path) -> None:
        """Test handling write permission error for JSON."""
        settings_path = temp_dir / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        with patch("builtins.open") as mock_open:
            # First call succeeds (read attempt on non-existent file - handled)
            # Second call fails (write)
            mock_open.side_effect = [
                FileNotFoundError(),  # File doesn't exist (OK)
                OSError("Permission denied"),  # Write fails
            ]

            # Need to also ensure parent exists check passes
            result = configure_mcp_server_json(settings_path)

        assert result["success"] is False
        assert "Failed to write" in result["error"]

    def test_remove_json_write_error(self, temp_dir: Path) -> None:
        """Test handling write permission error when removing from JSON."""
        settings_path = temp_dir / "settings.json"
        existing = {"mcpServers": {"gobby": {"command": "uv"}}}
        settings_path.write_text(json.dumps(existing))

        # Track call count to differentiate read vs write calls
        original_open = open
        call_count = [0]

        def mock_open_fn(path, mode="r", *args, **kwargs):
            call_count[0] += 1
            # Fail on the write call (mode "w")
            if "w" in str(mode):
                raise OSError("Permission denied")
            return original_open(path, mode, *args, **kwargs)

        with patch("gobby.cli.installers.mcp_config.copy2"):  # Skip actual backup
            with patch("builtins.open", mock_open_fn):
                result = remove_mcp_server_json(settings_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to write" in result["error"]

    def test_install_cli_content_multiple_command_dirs(self, temp_dir: Path) -> None:
        """Test that both commands/ and prompts/ directories are processed."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "test-cli"

        # Create both commands/ and prompts/
        commands_dir = cli_dir / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "cmd1.md").write_text("Command 1")

        prompts_dir = cli_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "prompt1.md").write_text("Prompt 1")

        target_path = temp_dir / ".test-cli"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("test-cli", target_path)

        assert "cmd1.md" in result["commands"]
        assert "prompt1.md" in result["commands"]
        assert (target_path / "commands" / "cmd1.md").exists()
        assert (target_path / "prompts" / "prompt1.md").exists()

    def test_configure_json_backup_error(self, temp_dir: Path) -> None:
        """Test handling backup creation failure for JSON."""
        settings_path = temp_dir / "settings.json"
        settings_path.write_text('{"existing": true}')

        with patch("gobby.cli.installers.mcp_config.copy2") as mock_copy:
            mock_copy.side_effect = OSError("Disk full")
            result = configure_mcp_server_json(settings_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to create backup" in result["error"]

    def test_remove_json_backup_error(self, temp_dir: Path) -> None:
        """Test handling backup creation failure when removing JSON server."""
        settings_path = temp_dir / "settings.json"
        existing = {"mcpServers": {"gobby": {"command": "uv"}}}
        settings_path.write_text(json.dumps(existing))

        with patch("gobby.cli.installers.mcp_config.copy2") as mock_copy:
            mock_copy.side_effect = OSError("Disk full")
            result = remove_mcp_server_json(settings_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to create backup" in result["error"]

    def test_configure_toml_read_error(self, temp_dir: Path) -> None:
        """Test handling read error for TOML file."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('valid = "content"')
        config_path.chmod(0o000)

        try:
            result = configure_mcp_server_toml(config_path)
            assert result["success"] is False
            assert result["error"] is not None
            assert "Failed to read" in result["error"]
        finally:
            config_path.chmod(0o644)

    def test_configure_toml_backup_error(self, temp_dir: Path) -> None:
        """Test handling backup creation failure for TOML."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('existing = "value"')

        with patch.object(Path, "write_text") as mock_write:
            # First call is for backup, make it fail
            mock_write.side_effect = OSError("Disk full")
            result = configure_mcp_server_toml(config_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to create backup" in result["error"]

    def test_configure_toml_write_error(self, temp_dir: Path) -> None:
        """Test handling write error for TOML file."""
        config_path = temp_dir / "config.toml"
        # Create a new file (no backup needed)

        with patch.object(Path, "write_text") as mock_write:
            mock_write.side_effect = OSError("Permission denied")
            result = configure_mcp_server_toml(config_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to write" in result["error"]

    def test_remove_toml_read_error(self, temp_dir: Path) -> None:
        """Test handling read error when removing TOML server."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('[mcp_servers.gobby]\ncommand = "uv"')
        config_path.chmod(0o000)

        try:
            result = remove_mcp_server_toml(config_path)
            assert result["success"] is False
            assert result["error"] is not None
            assert "Failed to read" in result["error"]
        finally:
            config_path.chmod(0o644)

    def test_remove_toml_backup_error(self, temp_dir: Path) -> None:
        """Test handling backup creation failure when removing TOML server."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('[mcp_servers.gobby]\ncommand = "uv"')

        with patch.object(Path, "write_text") as mock_write:
            mock_write.side_effect = OSError("Disk full")
            result = remove_mcp_server_toml(config_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to create backup" in result["error"]

    def test_remove_toml_write_error(self, temp_dir: Path) -> None:
        """Test handling write error when removing TOML server."""
        config_path = temp_dir / "config.toml"
        config_path.write_text('[mcp_servers.gobby]\ncommand = "uv"')

        with patch.object(Path, "write_text", side_effect=[None, OSError("Permission denied")]):
            result = remove_mcp_server_toml(config_path)

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to write" in result["error"]

    def test_install_cli_workflows_subdirectories_db_managed(self, temp_dir: Path) -> None:
        """Test that CLI workflow subdirectories are not copied (DB-managed)."""
        install_dir = temp_dir / "install"
        cli_dir = install_dir / "claude"
        workflows_dir = cli_dir / "workflows"
        workflows_dir.mkdir(parents=True)

        # Create a file and a directory (should be ignored)
        (workflows_dir / "valid.yaml").write_text("name: valid")
        (workflows_dir / "lifecycle").mkdir()
        (workflows_dir / "lifecycle" / "session.yaml").write_text("name: session")

        target_path = temp_dir / ".claude"
        target_path.mkdir(parents=True)

        with patch("gobby.cli.installers.shared.get_install_dir") as mock_install_dir:
            mock_install_dir.return_value = install_dir
            result = install_cli_content("claude", target_path)

        # Workflows are DB-managed, not file-based
        assert "workflows" not in result


class TestGetIdeConfigDir:
    """Tests for _get_ide_config_dir cross-platform resolution."""

    def test_macos_path(self) -> None:
        with patch("gobby.cli.installers.ide_config.sys") as mock_sys:
            mock_sys.platform = "darwin"
            path = _get_ide_config_dir("TestIDE")
        assert path == Path.home() / "Library" / "Application Support" / "TestIDE"

    def test_linux_path(self) -> None:
        with patch("gobby.cli.installers.ide_config.sys") as mock_sys:
            mock_sys.platform = "linux"
            path = _get_ide_config_dir("TestIDE")
        assert path == Path.home() / ".config" / "TestIDE"

    def test_windows_path(self) -> None:
        with (
            patch("gobby.cli.installers.ide_config.sys") as mock_sys,
            patch.dict(os.environ, {"APPDATA": "C:\\Users\\test\\AppData\\Roaming"}),
        ):
            mock_sys.platform = "win32"
            path = _get_ide_config_dir("TestIDE")
        assert path == Path("C:\\Users\\test\\AppData\\Roaming") / "TestIDE"


class TestConfigureIdeTerminalTitle:
    """Tests for configure_ide_terminal_title function."""

    def test_skip_when_ide_not_installed(self, temp_dir: Path) -> None:
        """IDE config dir doesn't exist — skip silently."""
        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = temp_dir / "NonExistent"
            result = configure_ide_terminal_title("NonExistent")

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["added"] is False

    def test_create_settings_from_scratch(self, temp_dir: Path) -> None:
        """Config dir exists but no settings.json — creates it."""
        config_dir = temp_dir / "TestIDE"
        config_dir.mkdir()

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("TestIDE")

        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is None  # No backup for new file

        settings_path = config_dir / "User" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert settings["terminal.integrated.tabs.title"] == "${sequence}"

    def test_add_to_existing_settings(self, temp_dir: Path) -> None:
        """Existing settings.json without the setting — adds it with backup."""
        config_dir = temp_dir / "TestIDE2"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        settings_path = user_dir / "settings.json"
        settings_path.write_text(json.dumps({"editor.fontSize": 14}))

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("TestIDE2")

        assert result["success"] is True
        assert result["added"] is True
        assert result["backup_path"] is not None

        settings = json.loads(settings_path.read_text())
        assert settings["editor.fontSize"] == 14
        assert settings["terminal.integrated.tabs.title"] == "${sequence}"
        assert "terminal.integrated.tabs.hideCondition" not in settings

        # Verify backup exists and has original content
        backup = json.loads(Path(result["backup_path"]).read_text())
        assert "terminal.integrated.tabs.title" not in backup

    def test_noop_when_already_configured(self, temp_dir: Path) -> None:
        """Setting already present — no-op, no backup."""
        config_dir = temp_dir / "TestIDE3"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        settings_path = user_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"terminal.integrated.tabs.title": "${process} - ${sequence}"})
        )

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("TestIDE3")

        assert result["success"] is True
        assert result["already_configured"] is True
        assert result["added"] is False
        assert result["backup_path"] is None

    def test_updates_existing_title_without_sequence(self, temp_dir: Path) -> None:
        """Existing title setting without ${sequence} is repaired."""
        config_dir = temp_dir / "TestIDE6"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        settings_path = user_dir / "settings.json"
        settings_path.write_text(json.dumps({"terminal.integrated.tabs.title": "${process}"}))

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("TestIDE6")

        assert result["success"] is True
        assert result["added"] is False
        assert result["updated"] is True
        assert result["already_configured"] is False
        assert result["backup_path"] is not None

        settings = json.loads(settings_path.read_text())
        assert settings["terminal.integrated.tabs.title"] == "${sequence}${separator}${process}"

    def test_antigravity_keeps_terminal_tabs_visible(self, temp_dir: Path) -> None:
        """Antigravity keeps the tab list visible so ${sequence} can be seen."""
        config_dir = temp_dir / "Antigravity"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        settings_path = user_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "terminal.integrated.tabs.title": "${sequence}",
                    "terminal.integrated.tabs.hideCondition": "singleTerminal",
                }
            )
        )

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("Antigravity")

        assert result["success"] is True
        assert result["added"] is False
        assert result["updated"] is True
        assert result["already_configured"] is False

        settings = json.loads(settings_path.read_text())
        assert settings["terminal.integrated.tabs.title"] == "${sequence}"
        assert settings["terminal.integrated.tabs.hideCondition"] == "never"

    def test_invalid_json_in_settings(self, temp_dir: Path) -> None:
        """Existing settings.json with invalid JSON — returns error."""
        config_dir = temp_dir / "TestIDE4"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text("{ broken json }")

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("TestIDE4")

        assert result["success"] is False
        assert result["error"] is not None
        assert "Failed to parse" in result["error"]

    def test_backup_failure(self, temp_dir: Path) -> None:
        """Backup creation fails — returns error without modifying file."""
        config_dir = temp_dir / "TestIDE5"
        user_dir = config_dir / "User"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text("{}")

        with (
            patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir,
            patch("gobby.cli.installers.ide_config.copy2") as mock_copy,
        ):
            mock_dir.return_value = config_dir
            mock_copy.side_effect = OSError("Disk full")
            result = configure_ide_terminal_title("TestIDE5")

        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_cursor_settings_created(self, temp_dir: Path) -> None:
        """Cursor uses the same VS Code-family settings layout."""
        config_dir = temp_dir / "Cursor"
        config_dir.mkdir()

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("Cursor")

        assert result["success"] is True
        assert result["added"] is True
        settings = json.loads((config_dir / "User" / "settings.json").read_text())
        assert settings["terminal.integrated.tabs.title"] == "${sequence}"
        assert "terminal.integrated.tabs.hideCondition" not in settings

    def test_antigravity_ide_settings_created(self, temp_dir: Path) -> None:
        """Antigravity IDE uses the same VS Code-family settings layout."""
        config_dir = temp_dir / "Antigravity IDE"
        config_dir.mkdir()

        with patch("gobby.cli.installers.ide_config._get_ide_config_dir") as mock_dir:
            mock_dir.return_value = config_dir
            result = configure_ide_terminal_title("Antigravity IDE")

        assert result["success"] is True
        assert result["added"] is True
        settings = json.loads((config_dir / "User" / "settings.json").read_text())
        assert settings["terminal.integrated.tabs.title"] == "${sequence}"
        assert settings["terminal.integrated.tabs.hideCondition"] == "never"


class TestConfigureVsCodeFamilyTerminalTitles:
    """Tests for configuring known VS Code-family roots."""

    def test_attempts_all_known_ide_roots(self) -> None:
        with patch("gobby.cli.installers.ide_config.configure_ide_terminal_title") as mock_config:
            mock_config.return_value = {"success": True, "skipped": True}

            results = configure_vscode_family_terminal_titles()

        assert tuple(results) == VSCODE_FAMILY_IDE_NAMES
        assert [call.args[0] for call in mock_config.call_args_list] == list(
            VSCODE_FAMILY_IDE_NAMES
        )

    def test_absent_roots_are_skipped(self, temp_dir: Path) -> None:
        existing_root = temp_dir / "Cursor"
        existing_root.mkdir()

        def fake_config_dir(ide_name: str) -> Path:
            return existing_root if ide_name == "Cursor" else temp_dir / ide_name

        with patch(
            "gobby.cli.installers.ide_config._get_ide_config_dir", side_effect=fake_config_dir
        ):
            results = configure_vscode_family_terminal_titles(("Code", "Cursor"))

        assert results["Code"]["success"] is True
        assert results["Code"]["skipped"] is True
        assert not (temp_dir / "Code").exists()
        assert results["Cursor"]["success"] is True
        assert results["Cursor"]["added"] is True
        assert (existing_root / "User" / "settings.json").exists()
