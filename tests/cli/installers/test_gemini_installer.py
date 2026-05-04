"""Tests for the Gemini CLI installer module."""

import json
import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.gemini import (
    _install_gemini_policy,
    _uninstall_gemini_policy,
    install_gemini,
    uninstall_gemini,
)

pytestmark = pytest.mark.unit


class TestInstallGemini:
    """Tests for install_gemini function."""

    @pytest.fixture
    def project_path(self, temp_dir: Path) -> Path:
        """Create a project directory for testing."""
        project = temp_dir / "test-project"
        project.mkdir(parents=True)
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory with required files."""
        install_dir = temp_dir / "install"
        gemini_dir = install_dir / "gemini"
        hooks_dir = gemini_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create hook dispatcher
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text('#!/usr/bin/env python3\nprint("dispatcher")\n')

        # Create hooks template
        template = gemini_dir / "hooks-template.json"
        template_content = {
            "hooks": {
                "SessionStart": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
                "SessionEnd": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
            }
        }
        template.write_text(json.dumps(template_content))

        with patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/usr/local/bin/ghook",
        ):
            yield install_dir

    @pytest.fixture
    def mock_shared_content(self) -> dict:
        """Mock return value for install_shared_content."""
        return {"plugins": ["plugin1.py"], "docs": []}

    @pytest.fixture
    def mock_cli_content(self) -> dict:
        """Mock return value for install_cli_content."""
        return {
            "commands": ["command1.md"],
        }

    def test_install_gemini_success(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test successful Gemini installation."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.install_router_skills_as_gemini_skills",
                return_value=["gobby/", "g/"],
            ),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert result["error"] is None
            assert "SessionStart" in result["hooks_installed"]
            assert "SessionEnd" in result["hooks_installed"]
            assert result["workflows_installed"] == []  # DB-managed
            assert result["commands_installed"] == ["command1.md", "gobby/", "g/"]
            assert result["plugins_installed"] == ["plugin1.py"]
            assert result["mcp_configured"] is True

            # Verify settings file was created
            settings_file = project_path / ".gemini" / "settings.json"
            assert settings_file.exists()

            # Verify settings content
            with open(settings_file) as f:
                settings = json.load(f)
            assert settings["general"]["enableHooks"] is True
            assert "hooks" in settings

    def test_install_gemini_missing_dispatcher(self, project_path: Path, temp_dir: Path) -> None:
        """Test installation fails when dispatcher is missing."""
        install_dir = temp_dir / "install"
        gemini_dir = install_dir / "gemini"
        gemini_dir.mkdir(parents=True)

        with patch("gobby.cli.installers.gemini.get_install_dir", return_value=install_dir):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is False
            assert "Missing hooks template" in result["error"]

    def test_install_gemini_missing_template(self, project_path: Path, temp_dir: Path) -> None:
        """Test installation fails when hooks template is missing."""
        install_dir = temp_dir / "install"
        gemini_dir = install_dir / "gemini"
        hooks_dir = gemini_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create dispatcher but not template
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("print('dispatcher')")

        with patch("gobby.cli.installers.gemini.get_install_dir", return_value=install_dir):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is False
            assert "Missing hooks template" in result["error"]

    def test_install_gemini_existing_settings_backup(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that existing settings.json is backed up."""
        # Create existing settings
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)
        settings_file = gemini_path / "settings.json"
        existing_settings = {"existing": "setting", "general": {"customValue": True}}
        settings_file.write_text(json.dumps(existing_settings))

        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            # Verify backup was created
            backup_file = gemini_path / "settings.json.1234567890.backup"
            assert backup_file.exists()

            # Verify existing settings were preserved and merged
            with open(settings_file) as f:
                settings = json.load(f)
            assert settings["existing"] == "setting"
            assert settings["general"]["enableHooks"] is True

    def test_install_gemini_invalid_json_settings(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test installation handles invalid JSON in existing settings."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)
        settings_file = gemini_path / "settings.json"
        settings_file.write_text("not valid json {{{")

        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = install_gemini(project_path, mode="project")

            # Should still succeed, treating invalid JSON as empty
            assert result["success"] is True

    def test_install_gemini_ghook_path_substitution(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that the resolved ghook path is used in hook commands."""
        template = mock_install_dir / "gemini" / "hooks-template.json"
        template_content = {
            "hooks": {
                "SessionStart": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
            }
        }
        template.write_text(json.dumps(template_content))

        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch(
                "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
                return_value="/custom/path/to/ghook",
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            # Verify the resolved ghook path was used
            settings_file = project_path / ".gemini" / "settings.json"
            with open(settings_file) as f:
                settings = json.load(f)

            hook_command = settings["hooks"]["SessionStart"]["command"]
            assert (
                hook_command
                == "/custom/path/to/ghook --gobby-owned --cli=gemini --type=SessionStart"
            )

    def test_install_gemini_ghook_fallback_when_not_found(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test fallback to the bare ghook command when no absolute path is resolved."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch(
                "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
                return_value="ghook",
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            settings = json.loads((project_path / ".gemini" / "settings.json").read_text())
            assert settings["hooks"]["SessionStart"]["command"].startswith("ghook --gobby-owned")

    def test_install_gemini_mcp_already_configured(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test when MCP server is already configured."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "already_configured": True, "added": False},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert result["mcp_configured"] is False
            assert result["mcp_already_configured"] is True

    def test_install_gemini_mcp_config_failure(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test installation continues when MCP config fails."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": False, "error": "Permission denied"},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            # Installation should still succeed (MCP config is non-fatal)
            assert result["success"] is True
            assert result["mcp_configured"] is False

    def test_install_gemini_creates_directories(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that .gemini and global hooks directories are created."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert (project_path / ".gemini").exists()
            # Hooks are installed globally, not per-project
            assert Path(hooks_dir).exists()

    def test_install_gemini_validate_settings_is_executable(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that copied shared hook helpers are made executable."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            # Hooks are installed globally via install_global_hooks()
            helper = Path(hooks_dir) / "validate_settings.py"
            assert helper.exists()
            # Check executable bit (0o755 means rwxr-xr-x)
            mode = helper.stat().st_mode
            assert mode & 0o111 != 0  # At least one execute bit set

    def test_install_gemini_replaces_existing_validate_settings(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that existing global hook helpers are replaced."""
        hooks_dir_path = temp_dir / ".gobby" / "hooks"
        hooks_dir_path.mkdir(parents=True)
        existing_helper = hooks_dir_path / "validate_settings.py"
        existing_helper.write_text("# old validate settings")

        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": str(hooks_dir_path)}),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            content = existing_helper.read_text()
            assert "old validate settings" not in content

    def test_install_gemini_project_path_substitution(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that $HOOKS_DIR is substituted with absolute path."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            settings_file = project_path / ".gemini" / "settings.json"
            with open(settings_file) as f:
                settings = json.load(f)

            hook_command = settings["hooks"]["SessionStart"]["command"]
            assert "$HOOKS_DIR" not in hook_command
            assert "/usr/local/bin/ghook" in hook_command

    def test_install_gemini_preserves_existing_hooks(
        self,
        project_path: Path,
        mock_install_dir: Path,
        mock_shared_content: dict,
        mock_cli_content: dict,
        temp_dir: Path,
    ) -> None:
        """Test that existing hooks are preserved (overwritten by type)."""
        # Create existing settings with hooks
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)
        settings_file = gemini_path / "settings.json"
        existing_settings = {
            "hooks": {
                "CustomHook": {"command": "custom_command"},
            }
        }
        settings_file.write_text(json.dumps(existing_settings))

        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value=mock_shared_content,
            ),
            patch("gobby.cli.installers.gemini.install_cli_content", return_value=mock_cli_content),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = install_gemini(project_path, mode="project")

            assert result["success"] is True

            # Verify custom hook was NOT removed (merge behavior)
            with open(settings_file) as f:
                settings = json.load(f)
            assert "CustomHook" in settings["hooks"]
            assert "SessionStart" in settings["hooks"]


class TestUninstallGemini:
    """Tests for uninstall_gemini function."""

    @pytest.fixture
    def project_path(self, temp_dir: Path) -> Path:
        """Create a project directory for testing."""
        project = temp_dir / "test-project"
        project.mkdir(parents=True)
        return project

    def test_uninstall_gemini_no_settings_file(self, project_path: Path, temp_dir: Path) -> None:
        """Test uninstall when no settings file exists."""
        with patch.object(Path, "home", return_value=temp_dir):
            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert result["error"] is None
            assert result["hooks_removed"] == []
            assert result["files_removed"] == []

    def test_uninstall_gemini_success(self, project_path: Path, temp_dir: Path) -> None:
        """Test successful uninstallation."""
        # Create Gemini installation
        gemini_path = project_path / ".gemini"
        hooks_dir = gemini_path / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create settings with hooks
        settings_file = gemini_path / "settings.json"
        settings = {
            "hooks": {
                "SessionStart": {"command": "dispatcher"},
                "SessionEnd": {"command": "dispatcher"},
                "BeforeTool": {"command": "dispatcher"},
            },
            "general": {"enableHooks": True},
        }
        settings_file.write_text(json.dumps(settings))

        # Create dispatcher
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("print('dispatcher')")

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert "SessionStart" in result["hooks_removed"]
            assert "SessionEnd" in result["hooks_removed"]
            assert "BeforeTool" in result["hooks_removed"]
            assert "hook_dispatcher.py" in result["files_removed"]
            assert result["mcp_removed"] is True

            # Verify dispatcher was removed
            assert not dispatcher.exists()

            # Verify backup was created
            backup_file = gemini_path / "settings.json.1234567890.backup"
            assert backup_file.exists()

    def test_uninstall_gemini_removes_all_hook_types(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that all Gobby hook types are removed."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        # Create settings with all hook types
        settings = {
            "hooks": {
                "SessionStart": {"command": "cmd"},
                "SessionEnd": {"command": "cmd"},
                "BeforeAgent": {"command": "cmd"},
                "AfterAgent": {"command": "cmd"},
                "BeforeTool": {"command": "cmd"},
                "AfterTool": {"command": "cmd"},
                "BeforeToolSelection": {"command": "cmd"},
                "BeforeModel": {"command": "cmd"},
                "AfterModel": {"command": "cmd"},
                "PreCompress": {"command": "cmd"},
                "Notification": {"command": "cmd"},
            },
            "general": {"enableHooks": True},
        }
        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            expected_hooks = [
                "SessionStart",
                "SessionEnd",
                "BeforeAgent",
                "AfterAgent",
                "BeforeTool",
                "AfterTool",
                "BeforeToolSelection",
                "BeforeModel",
                "AfterModel",
                "PreCompress",
                "Notification",
            ]
            for hook in expected_hooks:
                assert hook in result["hooks_removed"]

    def test_uninstall_gemini_preserves_other_settings(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that non-Gobby settings are preserved."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings = {
            "hooks": {
                "SessionStart": {"command": "gobby"},
                "CustomHook": {"command": "my_custom_hook"},
            },
            "general": {"enableHooks": True, "otherSetting": "value"},
            "customSection": {"key": "value"},
        }
        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": False},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True

            # Verify non-Gobby settings are preserved
            with open(settings_file) as f:
                updated = json.load(f)

            assert "CustomHook" in updated["hooks"]
            assert updated["customSection"]["key"] == "value"
            assert updated["general"]["otherSetting"] == "value"
            # enableHooks should be removed if it was the only Gobby setting
            assert "enableHooks" not in updated["general"]

    def test_uninstall_gemini_removes_general_when_only_enable_hooks(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that 'general' section is removed if only enableHooks was present."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings = {
            "hooks": {"SessionStart": {"command": "cmd"}},
            "general": {"enableHooks": True},
        }
        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True

            with open(settings_file) as f:
                updated = json.load(f)

            assert "general" not in updated

    def test_uninstall_gemini_preserves_general_with_other_entries(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that 'general' section is preserved if it has other entries."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings = {
            "hooks": {"SessionStart": {"command": "cmd"}},
            "general": {"enableHooks": True, "theme": "dark"},
        }
        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True

            with open(settings_file) as f:
                updated = json.load(f)

            assert "general" in updated
            assert updated["general"]["theme"] == "dark"
            assert "enableHooks" not in updated["general"]

    def test_uninstall_gemini_removes_empty_hooks_directory(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that empty hooks directory is removed."""
        gemini_path = project_path / ".gemini"
        hooks_dir = gemini_path / "hooks"
        hooks_dir.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {}}))

        # Create only the dispatcher (no other files)
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("print('dispatcher')")

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert not hooks_dir.exists()

    def test_uninstall_gemini_keeps_nonempty_hooks_directory(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test that hooks directory with other files is preserved."""
        gemini_path = project_path / ".gemini"
        hooks_dir = gemini_path / "hooks"
        hooks_dir.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {}}))

        # Create dispatcher and another file
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("print('dispatcher')")
        other_file = hooks_dir / "custom_hook.py"
        other_file.write_text("print('custom')")

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert hooks_dir.exists()
            assert other_file.exists()

    def test_uninstall_gemini_mcp_remove_failure(self, project_path: Path, temp_dir: Path) -> None:
        """Test uninstall continues when MCP removal fails."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"SessionStart": {"command": "cmd"}}}))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": False, "error": "Permission denied"},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            # Should still succeed (MCP removal is non-fatal)
            assert result["success"] is True
            assert result["mcp_removed"] is False

    def test_uninstall_gemini_no_hooks_section(self, project_path: Path, temp_dir: Path) -> None:
        """Test uninstall when settings has no hooks section."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"general": {"theme": "dark"}}))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": False},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert result["hooks_removed"] == []

    def test_uninstall_gemini_no_dispatcher(self, project_path: Path, temp_dir: Path) -> None:
        """Test uninstall when dispatcher doesn't exist."""
        gemini_path = project_path / ".gemini"
        hooks_dir = gemini_path / "hooks"
        hooks_dir.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"SessionStart": {"command": "cmd"}}}))

        # Don't create dispatcher

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True
            assert "hook_dispatcher.py" not in result["files_removed"]


class TestInstallGeminiEdgeCases:
    """Edge case tests for install_gemini."""

    @pytest.fixture
    def project_path(self, temp_dir: Path) -> Path:
        """Create a project directory for testing."""
        project = temp_dir / "test-project"
        project.mkdir(parents=True)
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory with required files."""
        install_dir = temp_dir / "install"
        gemini_dir = install_dir / "gemini"
        hooks_dir = gemini_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text('print("dispatcher")\n')

        template = gemini_dir / "hooks-template.json"
        template_content = {
            "hooks": {
                "SessionStart": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
            }
        }
        template.write_text(json.dumps(template_content))

        return install_dir

    def test_install_gemini_empty_shared_content(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        """Test installation with no shared content."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value={"workflows": []},
            ),
            patch(
                "gobby.cli.installers.gemini.install_cli_content",
                return_value={"workflows": [], "commands": []},
            ),
            patch(
                "gobby.cli.installers.gemini.install_router_skills_as_gemini_skills",
                return_value=[],
            ),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert result["workflows_installed"] == []
            assert result["commands_installed"] == []

    def test_install_gemini_shared_content_without_plugins(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        """Test installation when shared content doesn't include plugins key."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value={"workflows": []},  # No plugins key
            ),
            patch(
                "gobby.cli.installers.gemini.install_cli_content",
                return_value={"workflows": [], "commands": []},
            ),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert result["plugins_installed"] == []

    def test_install_gemini_cli_content_without_commands(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        """Test installation when CLI content doesn't include commands key."""
        with (
            patch("gobby.cli.installers.gemini.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.gemini.install_shared_content",
                return_value={"workflows": [], "plugins": []},
            ),
            patch(
                "gobby.cli.installers.gemini.install_cli_content",
                return_value={"workflows": []},  # No commands key
            ),
            patch(
                "gobby.cli.installers.gemini.install_router_skills_as_gemini_skills",
                return_value=[],
            ),
            patch(
                "gobby.cli.installers.gemini.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_gemini(project_path, mode="project")

            assert result["success"] is True
            assert result["commands_installed"] == []


class TestUninstallGeminiEdgeCases:
    """Edge case tests for uninstall_gemini."""

    @pytest.fixture
    def project_path(self, temp_dir: Path) -> Path:
        """Create a project directory for testing."""
        project = temp_dir / "test-project"
        project.mkdir(parents=True)
        return project

    def test_uninstall_gemini_hooks_dir_rmdir_error(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test uninstall handles error when removing hooks directory."""
        gemini_path = project_path / ".gemini"
        hooks_dir = gemini_path / "hooks"
        hooks_dir.mkdir(parents=True)

        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {}}))

        # Create dispatcher
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("print('dispatcher')")

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            # Mock rmdir to raise an exception
            original_rmdir = Path.rmdir

            def mock_rmdir(self):
                if "hooks" in str(self):
                    raise OSError("Permission denied")
                return original_rmdir(self)

            with patch.object(Path, "rmdir", mock_rmdir):
                result = uninstall_gemini(project_path)

            # Should still succeed (rmdir error is caught)
            assert result["success"] is True

    def test_uninstall_gemini_with_enable_hooks_false(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        """Test uninstall when enableHooks is False."""
        gemini_path = project_path / ".gemini"
        gemini_path.mkdir(parents=True)

        settings = {
            "hooks": {"SessionStart": {"command": "cmd"}},
            "general": {"enableHooks": False, "otherSetting": True},
        }
        settings_file = gemini_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        with (
            patch(
                "gobby.cli.installers.gemini.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
            patch("gobby.cli.installers.gemini.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890

            result = uninstall_gemini(project_path)

            assert result["success"] is True

            # enableHooks: False should not trigger the removal logic
            with open(settings_file) as f:
                updated = json.load(f)
            # general section should still have enableHooks (it was False)
            assert updated["general"]["enableHooks"] is False


class TestGeminiPolicy:
    """Tests for ~/.gemini/policies/gobby.toml management."""

    def test_install_gemini_policy_writes_expected_rules(self, temp_dir: Path) -> None:
        """Policy file contains the two rules with the resolved bin path."""
        gemini_home = temp_dir / ".gemini"
        gobby_bin = temp_dir / ".gobby" / "bin"
        gobby_bin.mkdir(parents=True)

        result = _install_gemini_policy(gemini_home, gobby_bin)

        assert result["written"] is True
        assert result["backup_path"] is None

        policy_file = gemini_home / "policies" / "gobby.toml"
        assert policy_file.exists()

        parsed = tomllib.loads(policy_file.read_text())
        rules = parsed["rule"]
        assert len(rules) == 2

        mcp_rule, bin_rule = rules
        assert mcp_rule == {
            "mcpName": "gobby",
            "toolName": "*",
            "decision": "allow",
            "priority": 500,
            "modes": ["plan"],
        }
        assert bin_rule == {
            "toolName": "run_shell_command",
            "commandPrefix": f"{gobby_bin}/",
            "decision": "allow",
            "priority": 500,
        }

    def test_install_gemini_policy_backs_up_existing(self, temp_dir: Path) -> None:
        """Existing gobby.toml is preserved as a timestamped backup."""
        gemini_home = temp_dir / ".gemini"
        policies_dir = gemini_home / "policies"
        policies_dir.mkdir(parents=True)
        existing = policies_dir / "gobby.toml"
        existing.write_text("# user customization\n")

        gobby_bin = temp_dir / ".gobby" / "bin"
        gobby_bin.mkdir(parents=True)

        with patch("gobby.cli.installers.gemini.time") as mock_time:
            mock_time.time.return_value = 1234567890
            result = _install_gemini_policy(gemini_home, gobby_bin)

        backup_file = policies_dir / "gobby.toml.1234567890.backup"
        assert result["backup_path"] == str(backup_file)
        assert backup_file.exists()
        assert backup_file.read_text() == "# user customization\n"

    def test_install_gemini_policy_regeneration_is_idempotent(self, temp_dir: Path) -> None:
        """Running install twice yields one current file with no rule duplication."""
        gemini_home = temp_dir / ".gemini"
        gobby_bin = temp_dir / ".gobby" / "bin"
        gobby_bin.mkdir(parents=True)

        with patch("gobby.cli.installers.gemini.time") as mock_time:
            mock_time.time.return_value = 1
            _install_gemini_policy(gemini_home, gobby_bin)
            mock_time.time.return_value = 2
            _install_gemini_policy(gemini_home, gobby_bin)

        policy_file = gemini_home / "policies" / "gobby.toml"
        parsed = tomllib.loads(policy_file.read_text())
        assert len(parsed["rule"]) == 2

    def test_uninstall_gemini_policy_removes_file(self, temp_dir: Path) -> None:
        """Uninstall deletes the policy file."""
        gemini_home = temp_dir / ".gemini"
        gobby_bin = temp_dir / ".gobby" / "bin"
        gobby_bin.mkdir(parents=True)
        _install_gemini_policy(gemini_home, gobby_bin)
        policy_file = gemini_home / "policies" / "gobby.toml"
        assert policy_file.exists()

        removed = _uninstall_gemini_policy(gemini_home)

        assert removed is True
        assert not policy_file.exists()

    def test_uninstall_gemini_policy_idempotent_when_absent(self, temp_dir: Path) -> None:
        """Uninstall with no policy file present returns False without error."""
        gemini_home = temp_dir / ".gemini"
        removed = _uninstall_gemini_policy(gemini_home)
        assert removed is False
