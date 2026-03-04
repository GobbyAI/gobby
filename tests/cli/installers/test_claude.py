"""Tests for the Claude Code installer module.

This module tests install_claude() and uninstall_claude() functions
which handle installing and uninstalling Gobby hooks for Claude Code CLI.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestInstallClaude:
    """Tests for the install_claude function."""

    @pytest.fixture
    def temp_project(self, temp_dir: Path) -> Path:
        """Create a temporary project directory."""
        project = temp_dir / "test-project"
        project.mkdir()
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory with required files."""
        install_dir = temp_dir / "install"
        claude_dir = install_dir / "claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create mock hook files (legacy location)
        (hooks_dir / "hook_dispatcher.py").write_text("# mock hook dispatcher")
        (hooks_dir / "validate_settings.py").write_text("# mock validate settings")

        # Create shared hooks (used by install_shared_hooks)
        shared_hooks_dir = install_dir / "shared" / "hooks"
        shared_hooks_dir.mkdir(parents=True)
        (shared_hooks_dir / "hook_dispatcher.py").write_text("# mock hook dispatcher")
        (shared_hooks_dir / "validate_settings.py").write_text("# mock validate settings")

        # Create hooks-template.json
        hooks_template = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "test"}]}],
                "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "test"}]}],
            }
        }
        (claude_dir / "hooks-template.json").write_text(json.dumps(hooks_template))

        return install_dir

    @pytest.fixture
    def mock_home_dir(self, temp_dir: Path) -> Path:
        """Create a mock home directory."""
        home = temp_dir / "home"
        home.mkdir()
        return home

    @patch("gobby.cli.installers.claude.install_global_hooks")
    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_success(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        mock_global_hooks: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test successful Claude Code installation."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_global_hooks.return_value = ["hook_dispatcher.py", "validate_settings.py"]
        mock_shared_content.return_value = {
            "plugins": [],
            "docs": [],
        }
        mock_cli_content.return_value = {
            "commands": ["memory/"],
        }
        mock_mcp_config.return_value = {"success": True, "added": True}

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True
        assert result["error"] is None
        assert "SessionStart" in result["hooks_installed"]
        assert "PreToolUse" in result["hooks_installed"]
        assert result["workflows_installed"] == []  # DB-managed
        assert "memory/" in result["commands_installed"]
        assert result["mcp_configured"] is True

        # Verify .claude directory structure was created
        assert (temp_project / ".claude").exists()
        assert (temp_project / ".claude" / "settings.json").exists()

        # Verify global hooks were installed
        mock_global_hooks.assert_called_once()

    @patch("gobby.cli.installers.claude.get_install_dir")
    def test_install_claude_missing_source_files(
        self,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        temp_dir: Path,
    ) -> None:
        """Test installation fails when source files are missing."""
        from gobby.cli.installers.claude import install_claude

        # Create empty install dir without required files
        install_dir = temp_dir / "empty_install"
        (install_dir / "claude" / "hooks").mkdir(parents=True)
        mock_get_install_dir.return_value = install_dir

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Missing source files" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    def test_install_claude_missing_hooks_template(
        self,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        temp_dir: Path,
    ) -> None:
        """Test installation fails when hooks-template.json is missing."""
        from gobby.cli.installers.claude import install_claude

        install_dir = temp_dir / "partial_install"
        hooks_dir = install_dir / "claude" / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create hook files but not template
        (hooks_dir / "hook_dispatcher.py").write_text("# mock")
        (hooks_dir / "validate_settings.py").write_text("# mock")

        mock_get_install_dir.return_value = install_dir

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Missing source files" in result["error"]
        assert "hooks-template.json" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_merges_existing_settings(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test installation merges with existing settings.json."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        # Create existing settings.json with custom config
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        existing_settings = {
            "allowedTools": ["tool1", "tool2"],
            "hooks": {"CustomHook": [{"hooks": [{"type": "command", "command": "custom"}]}]},
        }
        (claude_path / "settings.json").write_text(json.dumps(existing_settings))

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True

        # Load merged settings
        with open(claude_path / "settings.json") as f:
            merged = json.load(f)

        # Verify existing content is preserved
        assert merged["allowedTools"] == ["tool1", "tool2"]
        # Verify gobby hooks were added
        assert "SessionStart" in merged["hooks"]
        assert "PreToolUse" in merged["hooks"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_creates_backup(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test installation creates backup of existing settings.json."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        # Create existing settings.json
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        original_content = {"original": "content"}
        (claude_path / "settings.json").write_text(json.dumps(original_content))

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True

        # Verify backup was created
        backup_files = list(claude_path.glob("settings.json.*.backup"))
        assert len(backup_files) == 1

        # Verify backup content matches original
        with open(backup_files[0]) as f:
            backup_content = json.load(f)
        assert backup_content == original_content

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_invalid_existing_json(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test installation fails gracefully with invalid existing JSON."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}

        # Create invalid JSON settings file
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text("{ invalid json }")

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Failed to parse settings.json" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    def test_install_claude_shared_content_error(
        self,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test installation handles shared content installation errors."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.side_effect = Exception("Shared content error")

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Failed to install shared content" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    def test_install_claude_cli_content_error(
        self,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test installation handles CLI content installation errors."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.side_effect = Exception("CLI content error")

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Failed to install CLI content" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_mcp_config_failure_non_fatal(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test MCP configuration failure is non-fatal."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": False, "error": "MCP config failed"}

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        # Installation should still succeed
        assert result["success"] is True
        assert result["mcp_configured"] is False

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_mcp_already_configured(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test handling when MCP is already configured."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {
            "success": True,
            "added": False,
            "already_configured": True,
        }

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True
        assert result["mcp_configured"] is False
        assert result["mcp_already_configured"] is True

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.shared.copy2")
    def test_install_claude_copy_error(
        self,
        mock_copy2: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test installation handles file copy errors."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_copy2.side_effect = OSError("Permission denied")

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Failed to install hook files" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_hooks_dir_replacement(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_home_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test that $HOOKS_DIR is replaced in hooks template."""
        from gobby.cli.installers.claude import install_claude

        # Create install dir with $HOOKS_DIR in template
        install_dir = temp_dir / "install"
        claude_dir = install_dir / "claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        (hooks_dir / "hook_dispatcher.py").write_text("# mock")
        (hooks_dir / "validate_settings.py").write_text("# mock")

        hooks_template = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": 'python "$HOOKS_DIR/hook.py"'}]}
                ]
            }
        }
        (claude_dir / "hooks-template.json").write_text(json.dumps(hooks_template))

        mock_get_install_dir.return_value = install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True

        # Verify $HOOKS_DIR was replaced
        with open(temp_project / ".claude" / "settings.json") as f:
            settings = json.load(f)

        command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert str((mock_home_dir / ".gobby" / "hooks").resolve()) in command
        assert "$HOOKS_DIR" not in command

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    def test_install_claude_invalid_hooks_template_json(
        self,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        temp_dir: Path,
    ) -> None:
        """Test installation handles invalid hooks template JSON."""
        from gobby.cli.installers.claude import install_claude

        install_dir = temp_dir / "install"
        claude_dir = install_dir / "claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        (hooks_dir / "hook_dispatcher.py").write_text("# mock")
        (hooks_dir / "validate_settings.py").write_text("# mock")
        (claude_dir / "hooks-template.json").write_text("{ invalid json }")

        mock_get_install_dir.return_value = install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}

        result = install_claude(temp_project, mode="project")

        assert result["success"] is False
        assert "Failed to parse hooks template" in result["error"]

    @patch("gobby.cli.installers.claude.install_global_hooks")
    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_hook_file_overwrite(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        mock_global_hooks: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test that install succeeds when existing project hook files exist."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_global_hooks.return_value = ["hook_dispatcher.py", "validate_settings.py"]
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        # Create existing hook file (legacy per-project hooks)
        hooks_dir = temp_project / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        existing_hook = hooks_dir / "hook_dispatcher.py"
        existing_hook.write_text("# old content")

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True

        # Global hooks were installed
        mock_global_hooks.assert_called_once()

    @patch("gobby.cli.installers.claude.install_global_hooks")
    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_hook_file_permissions(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        mock_global_hooks: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test that global hook installation is called during install."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_global_hooks.return_value = ["hook_dispatcher.py", "validate_settings.py"]
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True
        mock_global_hooks.assert_called_once()


class TestUninstallClaude:
    """Tests for the uninstall_claude function."""

    @pytest.fixture
    def temp_project(self, temp_dir: Path) -> Path:
        """Create a temporary project directory."""
        project = temp_dir / "test-project"
        project.mkdir()
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory."""
        install_dir = temp_dir / "install"
        (install_dir / "claude").mkdir(parents=True)
        return install_dir

    @pytest.fixture
    def installed_claude_project(self, temp_project: Path) -> Path:
        """Create a project with Claude hooks installed."""
        claude_path = temp_project / ".claude"
        hooks_dir = claude_path / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create settings.json with hooks
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "test"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "test"}]}],
                "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "test"}]}],
                "PostToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "test"}]}
                ],
                "PreCompact": [{"hooks": [{"type": "command", "command": "test"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "test"}]}],
                "CustomUserHook": [{"hooks": [{"type": "command", "command": "user"}]}],
            },
            "allowedTools": ["tool1"],
        }
        (claude_path / "settings.json").write_text(json.dumps(settings))

        # Create hook files
        (hooks_dir / "hook_dispatcher.py").write_text("# hook")
        (hooks_dir / "validate_settings.py").write_text("# validate")
        (hooks_dir / "README.md").write_text("# readme")
        (hooks_dir / "HOOK_SCHEMAS.md").write_text("# schemas")

        return temp_project

    @pytest.fixture
    def mock_home_dir(self, temp_dir: Path) -> Path:
        """Create a mock home directory."""
        home = temp_dir / "home"
        home.mkdir()
        return home

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_success(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        installed_claude_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test successful Claude Code uninstallation."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(installed_claude_project)

        assert result["success"] is True
        assert result["error"] is None

        # Verify Gobby hooks were removed
        assert "SessionStart" in result["hooks_removed"]
        assert "SessionEnd" in result["hooks_removed"]
        assert "PreToolUse" in result["hooks_removed"]
        assert "PostToolUse" in result["hooks_removed"]

        # Verify files were removed
        assert "hook_dispatcher.py" in result["files_removed"]
        assert "validate_settings.py" in result["files_removed"]
        assert "README.md" in result["files_removed"]
        assert "HOOK_SCHEMAS.md" in result["files_removed"]

        assert result["mcp_removed"] is True

        # Verify settings.json still exists but without Gobby hooks
        settings_file = installed_claude_project / ".claude" / "settings.json"
        assert settings_file.exists()

        with open(settings_file) as f:
            settings = json.load(f)

        # Gobby hooks should be removed
        assert "SessionStart" not in settings.get("hooks", {})
        assert "PreToolUse" not in settings.get("hooks", {})

        # User's custom content should be preserved
        assert settings["allowedTools"] == ["tool1"]

    def test_uninstall_claude_no_settings_file(self, temp_project: Path) -> None:
        """Test uninstallation when no settings.json exists."""
        from gobby.cli.installers.claude import uninstall_claude

        result = uninstall_claude(temp_project)

        assert result["success"] is False
        assert "Settings file not found" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_invalid_json(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test uninstallation handles invalid settings.json."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir

        # Create invalid JSON settings file
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text("{ invalid json }")

        result = uninstall_claude(temp_project)

        assert result["success"] is False
        assert "Failed to parse settings.json" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_creates_backup(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        installed_claude_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test uninstallation creates backup of settings.json."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        # Store original content
        settings_file = installed_claude_project / ".claude" / "settings.json"
        with open(settings_file) as f:
            original_content = json.load(f)

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(installed_claude_project)

        assert result["success"] is True

        # Verify backup was created
        claude_path = installed_claude_project / ".claude"
        backup_files = list(claude_path.glob("settings.json.*.backup"))
        assert len(backup_files) == 1

        # Verify backup content matches original
        with open(backup_files[0]) as f:
            backup_content = json.load(f)
        assert backup_content == original_content

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_no_hooks_section(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test uninstallation when settings has no hooks section."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": False}

        # Create settings.json without hooks
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"allowedTools": []}))

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(temp_project)

        assert result["success"] is True
        assert result["hooks_removed"] == []

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_removes_all_hook_types(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test that all supported hook types are removed."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        # Create settings with all hook types
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)

        all_hook_types = [
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "Notification",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
        ]

        settings = {"hooks": {hook: [{}] for hook in all_hook_types}}
        (claude_path / "settings.json").write_text(json.dumps(settings))

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(temp_project)

        assert result["success"] is True

        # Verify all hook types were removed
        for hook_type in all_hook_types:
            assert hook_type in result["hooks_removed"]

        # Verify settings file is updated
        with open(claude_path / "settings.json") as f:
            updated_settings = json.load(f)
        assert updated_settings["hooks"] == {}

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_partial_hook_files(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test uninstallation handles partial hook file presence."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        # Create minimal installation (only some hook files)
        claude_path = temp_project / ".claude"
        hooks_dir = claude_path / "hooks"
        hooks_dir.mkdir(parents=True)

        (claude_path / "settings.json").write_text(json.dumps({"hooks": {}}))
        (hooks_dir / "hook_dispatcher.py").write_text("# hook")
        # validate_settings.py is missing

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(temp_project)

        assert result["success"] is True
        assert "hook_dispatcher.py" in result["files_removed"]
        # Should not fail even though validate_settings.py is missing
        assert "validate_settings.py" not in result["files_removed"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_mcp_removal_failure(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test that MCP removal failure is handled gracefully."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": False, "error": "MCP removal failed"}

        # Create minimal installation
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"hooks": {}}))

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(temp_project)

        # Uninstallation should still succeed
        assert result["success"] is True
        assert result["mcp_removed"] is False

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.copy2")
    def test_uninstall_claude_backup_failure(
        self,
        mock_copy2: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test uninstallation handles backup creation failure."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_copy2.side_effect = OSError("Permission denied")

        # Create minimal installation
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"hooks": {}}))

        result = uninstall_claude(temp_project)

        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    @patch("gobby.cli.installers.claude.os.fdopen")
    @patch("gobby.cli.installers.claude.tempfile.mkstemp")
    def test_uninstall_claude_atomic_write_failure(
        self,
        mock_mkstemp: MagicMock,
        mock_fdopen: MagicMock,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        installed_claude_project: Path,
        mock_install_dir: Path,
        mock_home_dir: Path,
    ) -> None:
        """Test that write failures trigger backup restoration."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        # Create a temp file path (doesn't need to exist)
        temp_path = str(installed_claude_project / ".claude" / "temp_settings.tmp")

        # Make mkstemp return a fake fd and path
        mock_mkstemp.return_value = (999, temp_path)

        # Make fdopen raise OSError
        mock_fdopen.side_effect = OSError("Failed to open file")

        with patch.object(Path, "home", return_value=mock_home_dir):
            result = uninstall_claude(installed_claude_project)

        assert result["success"] is False
        assert "Failed to write settings.json" in result["error"]


class TestInstallClaudeEdgeCases:
    """Edge case tests for install_claude."""

    @pytest.fixture
    def temp_project(self, temp_dir: Path) -> Path:
        """Create a temporary project directory."""
        project = temp_dir / "test-project"
        project.mkdir()
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory with required files."""
        install_dir = temp_dir / "install"
        claude_dir = install_dir / "claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        (hooks_dir / "hook_dispatcher.py").write_text("# mock hook dispatcher")
        (hooks_dir / "validate_settings.py").write_text("# mock validate settings")

        hooks_template = {"hooks": {"SessionStart": [{"hooks": []}]}}
        (claude_dir / "hooks-template.json").write_text(json.dumps(hooks_template))

        return install_dir

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_empty_hooks_section_in_existing(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test installation with existing empty hooks section."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        # Create existing settings with empty hooks
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"hooks": {}}))

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = install_claude(temp_project, mode="project")

        assert result["success"] is True

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_with_unicode_path(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        mock_install_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test installation with unicode characters in project path."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        # Create project with unicode path
        unicode_project = temp_dir / "test-project-unicode"
        unicode_project.mkdir()

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = install_claude(unicode_project, mode="project")

        assert result["success"] is True

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    def test_install_claude_result_structure(
        self,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test that result dictionary has expected structure."""
        from gobby.cli.installers.claude import install_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_shared_content.return_value = {"workflows": [], "plugins": []}
        mock_cli_content.return_value = {"workflows": [], "commands": []}
        mock_mcp_config.return_value = {"success": True, "added": False}

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = install_claude(temp_project, mode="project")

        # Verify all expected keys are present
        expected_keys = {
            "success",
            "hooks_installed",
            "workflows_installed",
            "commands_installed",
            "mcp_configured",
            "mcp_already_configured",
            "error",
            "plugins_installed",
            "agents_installed",
        }
        assert set(result.keys()) == expected_keys

        # Verify types
        assert isinstance(result["success"], bool)
        assert isinstance(result["hooks_installed"], list)
        assert isinstance(result["workflows_installed"], list)
        assert isinstance(result["commands_installed"], list)
        assert isinstance(result["agents_installed"], list)
        assert isinstance(result["mcp_configured"], bool)
        assert isinstance(result["mcp_already_configured"], bool)


class TestCleanProjectHooks:
    """Tests for _clean_project_hooks (global mode cleanup)."""

    @pytest.fixture
    def temp_project(self, temp_dir: Path) -> Path:
        """Create a temporary project directory."""
        project = temp_dir / "test-project"
        project.mkdir()
        return project

    def test_clean_project_hooks_removes_gobby_hooks(self, temp_project: Path) -> None:
        """Test that gobby hooks are removed from project settings."""
        from gobby.cli.installers.shared import clean_project_hooks

        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python hook_dispatcher.py --type=session-start",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python hook_dispatcher.py --type=pre-tool-use",
                            }
                        ],
                    }
                ],
            },
            "allowedTools": ["tool1"],
        }
        (claude_path / "settings.json").write_text(json.dumps(settings))

        removed = clean_project_hooks(temp_project / ".claude" / "settings.json")

        assert "SessionStart" in removed
        assert "PreToolUse" in removed

        with open(claude_path / "settings.json") as f:
            updated = json.load(f)
        assert "hooks" not in updated  # empty hooks dict removed
        assert updated["allowedTools"] == ["tool1"]

    def test_clean_project_hooks_preserves_non_gobby_hooks(self, temp_project: Path) -> None:
        """Test that non-gobby hooks are preserved."""
        from gobby.cli.installers.shared import clean_project_hooks

        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python hook_dispatcher.py --type=session-start",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "my-custom-linter"}]}
                ],
            }
        }
        (claude_path / "settings.json").write_text(json.dumps(settings))

        removed = clean_project_hooks(temp_project / ".claude" / "settings.json")

        assert "SessionStart" in removed
        assert "PreToolUse" not in removed  # not a gobby hook

        with open(claude_path / "settings.json") as f:
            updated = json.load(f)
        assert "PreToolUse" in updated["hooks"]

    def test_clean_project_hooks_no_settings_file(self, temp_project: Path) -> None:
        """Test cleanup when no project settings.json exists."""
        from gobby.cli.installers.shared import clean_project_hooks

        removed = clean_project_hooks(temp_project / ".claude" / "settings.json")
        assert removed == []

    def test_clean_project_hooks_no_hooks_section(self, temp_project: Path) -> None:
        """Test cleanup when settings has no hooks."""
        from gobby.cli.installers.shared import clean_project_hooks

        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"allowedTools": []}))

        removed = clean_project_hooks(temp_project / ".claude" / "settings.json")
        assert removed == []

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.install_shared_content")
    @patch("gobby.cli.installers.claude.install_cli_content")
    @patch("gobby.cli.installers.claude.configure_mcp_server_json")
    @patch("gobby.cli.installers.claude.install_global_hooks")
    def test_global_install_cleans_project_hooks(
        self,
        mock_global_hooks: MagicMock,
        mock_mcp_config: MagicMock,
        mock_cli_content: MagicMock,
        mock_shared_content: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        temp_dir: Path,
    ) -> None:
        """Test that global install cleans up project-level hooks."""
        from gobby.cli.installers.claude import install_claude

        install_dir = temp_dir / "install"
        claude_dir = install_dir / "claude"
        claude_dir.mkdir(parents=True)
        hooks_template = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "test"}]}],
            }
        }
        (claude_dir / "hooks-template.json").write_text(json.dumps(hooks_template))

        mock_get_install_dir.return_value = install_dir
        mock_shared_content.return_value = {"plugins": []}
        mock_cli_content.return_value = {"commands": []}
        mock_mcp_config.return_value = {"success": True, "added": True}
        mock_global_hooks.return_value = ["hook_dispatcher.py"]

        # Create project-level hooks that should be cleaned
        project_claude = temp_project / ".claude"
        project_claude.mkdir(parents=True)
        project_settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python hook_dispatcher.py --type=session-start",
                            }
                        ]
                    }
                ],
            }
        }
        (project_claude / "settings.json").write_text(json.dumps(project_settings))

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = install_claude(temp_project, mode="global")

        assert result["success"] is True
        assert "project_hooks_cleaned" in result
        assert "SessionStart" in result["project_hooks_cleaned"]

        # Verify project settings were cleaned
        with open(project_claude / "settings.json") as f:
            updated = json.load(f)
        assert "hooks" not in updated or "SessionStart" not in updated.get("hooks", {})


class TestUninstallClaudeEdgeCases:
    """Edge case tests for uninstall_claude."""

    @pytest.fixture
    def temp_project(self, temp_dir: Path) -> Path:
        """Create a temporary project directory."""
        project = temp_dir / "test-project"
        project.mkdir()
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory."""
        install_dir = temp_dir / "install"
        (install_dir / "claude").mkdir(parents=True)
        return install_dir

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_result_structure(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test that uninstall result dictionary has expected structure."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": False}

        # Create minimal installation
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        (claude_path / "settings.json").write_text(json.dumps({"hooks": {}}))

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = uninstall_claude(temp_project)

        # Verify all expected keys are present
        expected_keys = {
            "success",
            "hooks_removed",
            "files_removed",
            "mcp_removed",
            "error",
        }
        assert set(result.keys()) == expected_keys

        # Verify types
        assert isinstance(result["success"], bool)
        assert isinstance(result["hooks_removed"], list)
        assert isinstance(result["files_removed"], list)
        assert isinstance(result["mcp_removed"], bool)

    @patch("gobby.cli.installers.claude.get_install_dir")
    @patch("gobby.cli.installers.claude.remove_mcp_server_json")
    def test_uninstall_claude_preserves_custom_hooks(
        self,
        mock_remove_mcp: MagicMock,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
        temp_dir: Path,
    ) -> None:
        """Test that custom user hooks are preserved during uninstall."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir
        mock_remove_mcp.return_value = {"success": True, "removed": True}

        # Create settings with both Gobby and custom hooks
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": []}],  # Gobby hook
                "CustomHook": [{"hooks": []}],  # User's custom hook
                "AnotherCustom": [{"hooks": []}],  # Another user hook
            }
        }
        (claude_path / "settings.json").write_text(json.dumps(settings))

        mock_home = temp_dir / "home"
        mock_home.mkdir()

        with patch.object(Path, "home", return_value=mock_home):
            result = uninstall_claude(temp_project)

        assert result["success"] is True

        # Verify custom hooks are preserved
        with open(claude_path / "settings.json") as f:
            updated = json.load(f)

        # Gobby hook should be removed
        assert "SessionStart" not in updated["hooks"]
        # Custom hooks should remain
        assert "CustomHook" in updated["hooks"]
        assert "AnotherCustom" in updated["hooks"]

    @patch("gobby.cli.installers.claude.get_install_dir")
    def test_uninstall_claude_read_error(
        self,
        mock_get_install_dir: MagicMock,
        temp_project: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test uninstallation handles file read errors."""
        from gobby.cli.installers.claude import uninstall_claude

        mock_get_install_dir.return_value = mock_install_dir

        # Create settings file but make it unreadable
        claude_path = temp_project / ".claude"
        claude_path.mkdir(parents=True)
        settings_file = claude_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {}}))
        settings_file.chmod(0o000)

        try:
            result = uninstall_claude(temp_project)
            assert result["success"] is False
            # The error can be either "Failed to read" or "Failed to create backup"
            # depending on where the permission error is caught
            assert (
                "Failed to read settings.json" in result["error"]
                or "Failed to create backup" in result["error"]
            )
        finally:
            settings_file.chmod(0o644)
