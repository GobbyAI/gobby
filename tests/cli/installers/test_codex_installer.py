"""Comprehensive tests for the Codex CLI installer module."""

import json
import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# The hooks template events that install_codex should write
EXPECTED_HOOK_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}


def _load_toml_file(path: Path) -> dict[str, object]:
    with open(path, "rb") as f:
        return tomllib.load(f)


class TestInstallCodex:
    """Tests for install_codex function."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path):
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path):
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)

        # Create the hooks-template.json
        hooks_template = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=SessionStart',
                            }
                        ]
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=UserPromptSubmit',
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=PreToolUse',
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=PostToolUse',
                            }
                        ],
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=Stop',
                            }
                        ]
                    }
                ],
            }
        }
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template, indent=2))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    @pytest.fixture
    def mock_shared_content(self):
        """Mock the shared content installation functions."""
        with (
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks") as mock_clean,
        ):
            mock_shared.return_value = {
                "plugins": ["plugin1.py"],
            }
            mock_cli.return_value = {
                "commands": ["cmd1"],
            }
            mock_global.return_value = ["hook_dispatcher.py", "validate_settings.py"]
            mock_clean.return_value = []
            yield mock_shared, mock_cli, mock_global

    @pytest.fixture
    def mock_mcp_configure(self):
        """Mock the MCP server configuration."""
        with patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock:
            mock.return_value = {"success": True, "added": True, "already_configured": False}
            yield mock

    def test_install_success_new_config(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test successful installation with a new config file."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)

        assert result["success"] is True
        assert result["error"] is None
        assert result["mcp_configured"] is True

        # Verify hooks.json was created
        hooks_path = mock_home / ".codex" / "hooks.json"
        assert hooks_path.exists()
        hooks_config = json.loads(hooks_path.read_text())
        assert set(hooks_config["hooks"].keys()) == EXPECTED_HOOK_EVENTS

        # Verify $HOOKS_DIR was substituted
        hooks_str = hooks_path.read_text()
        assert "$HOOKS_DIR" not in hooks_str
        assert "hook_dispatcher.py" in hooks_str

        # Verify config.toml has feature flag
        config_path = mock_home / ".codex" / "config.toml"
        assert config_path.exists()
        config_data = _load_toml_file(config_path)
        assert config_data["features"]["codex_hooks"] is True

    def test_install_hooks_installed_list(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that hooks_installed lists all 5 event types."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)

        assert result["success"] is True
        assert set(result["hooks_installed"]) == EXPECTED_HOOK_EVENTS

    def test_install_migrates_from_notify(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that legacy notify line is removed during install."""
        from gobby.cli.installers.codex import install_codex

        # Create existing config with notify
        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text('model = "gpt-4"\nnotify = ["python3", "/old/path"]\n')

        result = install_codex(mock_home)

        assert result["success"] is True
        assert result["config_updated"] is True

        # Verify notify removed, feature flag added, model preserved
        config_data = _load_toml_file(config_path)
        assert "notify" not in config_data
        assert config_data["features"]["codex_hooks"] is True
        assert config_data["model"] == "gpt-4"

        # Verify backup created
        backup_path = config_path.with_suffix(".toml.bak")
        assert backup_path.exists()

    def test_install_cleans_old_notify_script(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that old notify script at ~/.gobby/hooks/codex/ is cleaned up."""
        from gobby.cli.installers.codex import install_codex

        # Create legacy notify script
        old_dir = mock_home / ".gobby" / "hooks" / "codex"
        old_dir.mkdir(parents=True)
        old_script = old_dir / "hook_dispatcher.py"
        old_script.write_text("# old notify script")

        result = install_codex(mock_home)

        assert result["success"] is True
        assert not old_script.exists()
        assert not old_dir.exists()  # Empty dir removed

    def test_install_existing_config_with_feature_flag(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test installation when feature flag already exists."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text("features.codex_hooks = false\n")

        result = install_codex(mock_home)

        assert result["success"] is True
        config_data = _load_toml_file(config_path)
        assert config_data["features"]["codex_hooks"] is True

    def test_install_feature_flag_before_table_headers(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that feature flag is placed before [table] headers, not inside them."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n\n'
            '[projects."/some/path"]\ntrust_level = "trusted"\n'
        )

        result = install_codex(mock_home)

        assert result["success"] is True
        config_content = config_path.read_text()
        assert "[features]" in config_content

        # Feature flag must appear BEFORE the first [table] header
        flag_pos = config_content.index("[features]")
        table_pos = config_content.index("[mcp_servers")
        assert flag_pos < table_pos, (
            f"Feature flag at {flag_pos} should be before [table] at {table_pos}"
        )

    def test_install_feature_flag_into_existing_features_section(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that feature flag is placed inside existing [features] section."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            '[features]\nfast_mode = true\n\n[mcp_servers.gobby]\ncommand = "uv"\n'
        )

        result = install_codex(mock_home)

        assert result["success"] is True
        config_content = config_path.read_text()
        # Should be placed as bare key inside [features], not as dotted key
        assert "codex_hooks = true" in config_content
        # [features] section should still exist
        assert "[features]" in config_content
        # fast_mode preserved
        assert "fast_mode = true" in config_content
        # codex_hooks must be between [features] and [mcp_servers]
        features_pos = config_content.index("[features]")
        codex_hooks_pos = config_content.index("codex_hooks = true")
        mcp_pos = config_content.index("[mcp_servers")
        assert features_pos < codex_hooks_pos < mcp_pos
        parsed = tomllib.loads(config_content)
        assert parsed["features"]["fast_mode"] is True
        assert parsed["features"]["codex_hooks"] is True

    def test_install_replaces_flag_in_existing_features_section(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that existing codex_hooks in [features] section is updated."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text("[features]\ncodex_hooks = false\nfast_mode = true\n")

        result = install_codex(mock_home)

        assert result["success"] is True
        config_content = config_path.read_text()
        parsed = tomllib.loads(config_content)
        assert parsed["features"]["codex_hooks"] is True
        assert parsed["features"]["fast_mode"] is True

    def test_install_merges_into_existing_hooks_json(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that existing hooks.json entries are preserved during merge."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        existing_hooks = {
            "hooks": {
                "CustomEvent": [{"hooks": [{"type": "command", "command": "echo custom"}]}],
            }
        }
        hooks_path.write_text(json.dumps(existing_hooks))

        result = install_codex(mock_home)

        assert result["success"] is True
        merged = json.loads(hooks_path.read_text())
        # Custom event preserved
        assert "CustomEvent" in merged["hooks"]
        # Gobby events added
        assert set(merged["hooks"].keys()) >= EXPECTED_HOOK_EVENTS

    def test_install_missing_template(self, mock_home: Path, temp_dir: Path) -> None:
        """Test installation fails when hooks-template.json is missing."""
        from gobby.cli.installers.codex import install_codex

        install_dir = temp_dir / "empty_install"
        install_dir.mkdir(parents=True)

        with (
            patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir),
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
        ):
            mock_global.return_value = ["hook_dispatcher.py"]
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}

            result = install_codex(mock_home)

        assert result["success"] is False
        assert "hooks.json" in result["error"]

    def test_install_mcp_config_failure_non_fatal(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
    ) -> None:
        """Test that MCP config failure is non-fatal."""
        from gobby.cli.installers.codex import install_codex

        with patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp:
            mock_mcp.return_value = {"success": False, "error": "MCP config error"}

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["mcp_configured"] is False

    def test_install_mcp_already_configured(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
    ) -> None:
        """Test detection of already configured MCP server."""
        from gobby.cli.installers.codex import install_codex

        with patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp:
            mock_mcp.return_value = {
                "success": True,
                "added": False,
                "already_configured": True,
            }

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["mcp_configured"] is False
        assert result["mcp_already_configured"] is True

    def test_install_workflows_db_managed(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_mcp_configure,
    ) -> None:
        """Test that workflows are DB-managed (not merged from file installs)."""
        from gobby.cli.installers.codex import install_codex

        with (
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": ["plugin.py"]}
            mock_cli.return_value = {"commands": ["command1"]}
            mock_global.return_value = ["hook_dispatcher.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["workflows_installed"] == []  # DB-managed
        assert result["commands_installed"] == ["command1"]
        assert result["plugins_installed"] == ["plugin.py"]

    def test_install_config_write_exception(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test handling of config write exception."""
        from gobby.cli.installers.codex import install_codex

        # Create config directory first, then make config.toml a directory
        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.mkdir()

        result = install_codex(mock_home)

        assert result["success"] is False
        assert "Failed to update Codex config" in result["error"]

    def test_install_global_hooks_failure(self, mock_home: Path, mock_install_dir: Path) -> None:
        """Test that global hooks installation failure stops install."""
        from gobby.cli.installers.codex import install_codex

        with (
            patch("gobby.cli.installers.codex.install_global_hooks", side_effect=OSError("fail")),
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            result = install_codex(mock_home)

        assert result["success"] is False
        assert "global hooks" in result["error"]

    def test_install_strips_tool_overrides(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content,
        mock_mcp_configure,
    ) -> None:
        """Test that install strips per-tool approval overrides from config.toml."""
        from gobby.cli.installers.codex import install_codex

        # Pre-seed config.toml with per-tool approval overrides
        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            "features.codex_hooks = true\n\n"
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            'args = ["run", "gobby", "mcp-server"]\n\n'
            "[mcp_servers.gobby.tools.call_tool]\n"
            'approval_mode = "approve"\n\n'
            "[mcp_servers.gobby.tools.get_tool_schema]\n"
            'approval_mode = "approve"\n'
        )

        mock_mcp_configure.return_value = {
            "success": True,
            "added": False,
            "already_configured": True,
        }

        result = install_codex(mock_home)

        assert result["success"] is True
        assert result["mcp_tools_stripped"] is True

        # Verify tools overrides are gone but server config preserved
        content = config_path.read_text()
        assert "approval_mode" not in content
        assert "uv" in content

    def test_backward_compat_alias(self) -> None:
        """Test that install_codex_notify is an alias for install_codex."""
        from gobby.cli.installers.codex import install_codex, install_codex_notify

        assert install_codex_notify is install_codex


class TestUninstallCodex:
    """Tests for uninstall_codex function."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path):
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_mcp_remove(self):
        """Mock the MCP server removal function."""
        with patch("gobby.cli.installers.codex.remove_mcp_server_toml") as mock:
            mock.return_value = {"success": True, "removed": True}
            yield mock

    def test_uninstall_success_full(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test successful uninstallation with all components present."""
        from gobby.cli.installers.codex import uninstall_codex

        # Set up hooks.json with gobby hooks
        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_config = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "uv run hook_dispatcher.py --cli=codex"}
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {"type": "command", "command": "uv run hook_dispatcher.py --cli=codex"}
                        ],
                    }
                ],
            }
        }
        hooks_path.write_text(json.dumps(hooks_config))

        # Set up config.toml with feature flag
        config_path = codex_dir / "config.toml"
        config_path.write_text('model = "gpt-4"\nfeatures.codex_hooks = true\n')

        result = uninstall_codex()

        assert result["success"] is True
        assert result["error"] is None
        assert set(result["hooks_removed"]) == {"SessionStart", "PreToolUse"}
        assert result["config_updated"] is True
        assert result["mcp_removed"] is True

        # Verify hooks.json cleaned (empty, so deleted)
        assert not hooks_path.exists()

        # Verify feature flag removed, model preserved
        config_data = _load_toml_file(config_path)
        features = config_data.get("features")
        assert not isinstance(features, dict) or "codex_hooks" not in features
        assert config_data["model"] == "gpt-4"

    def test_uninstall_preserves_non_gobby_hooks(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test that non-gobby hooks are preserved in hooks.json."""
        from gobby.cli.installers.codex import uninstall_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_config = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "hook_dispatcher.py --cli=codex"}]}
                ],
                "CustomEvent": [{"hooks": [{"type": "command", "command": "echo custom"}]}],
            }
        }
        hooks_path.write_text(json.dumps(hooks_config))

        result = uninstall_codex()

        assert result["success"] is True
        assert "SessionStart" in result["hooks_removed"]
        assert "CustomEvent" not in result["hooks_removed"]

        # hooks.json still exists with custom event
        remaining = json.loads(hooks_path.read_text())
        assert "CustomEvent" in remaining["hooks"]
        assert "SessionStart" not in remaining["hooks"]

    def test_uninstall_cleans_legacy_notify_script(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test that legacy notify script is cleaned up during uninstall."""
        from gobby.cli.installers.codex import uninstall_codex

        # Set up legacy notify script
        old_dir = mock_home / ".gobby" / "hooks" / "codex"
        old_dir.mkdir(parents=True)
        old_script = old_dir / "hook_dispatcher.py"
        old_script.write_text("# old notify")

        result = uninstall_codex()

        assert result["success"] is True
        assert not old_script.exists()
        assert str(old_script) in result["files_removed"]

    def test_uninstall_removes_legacy_notify_from_config(
        self, mock_home: Path, mock_mcp_remove
    ) -> None:
        """Test that legacy notify line is also removed from config.toml."""
        from gobby.cli.installers.codex import uninstall_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text('notify = ["python3", "/path"]\nfeatures.codex_hooks = true\n')

        result = uninstall_codex()

        assert result["success"] is True
        config_data = _load_toml_file(config_path)
        assert "notify" not in config_data
        features = config_data.get("features")
        assert not isinstance(features, dict) or "codex_hooks" not in features

    def test_uninstall_no_hooks_json(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test uninstallation when hooks.json doesn't exist."""
        from gobby.cli.installers.codex import uninstall_codex

        result = uninstall_codex()

        assert result["success"] is True
        assert len(result["hooks_removed"]) == 0

    def test_uninstall_no_config_file(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test uninstallation when config file doesn't exist."""
        from gobby.cli.installers.codex import uninstall_codex

        result = uninstall_codex()

        assert result["success"] is True
        assert result["config_updated"] is False

    def test_uninstall_creates_backup(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test that config backup is created before modification."""
        from gobby.cli.installers.codex import uninstall_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        original = 'features.codex_hooks = true\nmodel = "gpt-4"\n'
        config_path.write_text(original)

        result = uninstall_codex()

        assert result["success"] is True
        backup_path = config_path.with_suffix(".toml.bak")
        assert backup_path.exists()
        assert backup_path.read_text() == original

    def test_uninstall_nothing_installed(self, mock_home: Path, mock_mcp_remove) -> None:
        """Test uninstallation when nothing is installed."""
        from gobby.cli.installers.codex import uninstall_codex

        result = uninstall_codex()

        assert result["success"] is True
        assert len(result["hooks_removed"]) == 0
        assert len(result["files_removed"]) == 0
        assert result["config_updated"] is False

    def test_uninstall_mcp_removal_failure_non_fatal(self, mock_home: Path) -> None:
        """Test that MCP removal failure is non-fatal."""
        from gobby.cli.installers.codex import uninstall_codex

        with patch("gobby.cli.installers.codex.remove_mcp_server_toml") as mock_mcp:
            mock_mcp.return_value = {"success": False, "error": "MCP removal error"}

            result = uninstall_codex()

        assert result["success"] is True
        assert result["mcp_removed"] is False

    def test_backward_compat_alias(self) -> None:
        """Test that uninstall_codex_notify is an alias for uninstall_codex."""
        from gobby.cli.installers.codex import uninstall_codex, uninstall_codex_notify

        assert uninstall_codex_notify is uninstall_codex


class TestHooksTemplateFormat:
    """Tests for hooks.json format and content."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path):
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path):
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)

        hooks_template = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=SessionStart',
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=PreToolUse',
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=PostToolUse',
                            }
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=UserPromptSubmit',
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=Stop',
                            }
                        ]
                    }
                ],
            }
        }
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template, indent=2))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    @pytest.fixture
    def mock_deps(self):
        """Mock shared content and MCP configuration."""
        with (
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}
            mock_global.return_value = ["hook_dispatcher.py"]
            yield

    def test_hooks_use_regex_matcher(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps,
    ) -> None:
        """Test that PreToolUse/PostToolUse use regex matchers (not glob)."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_path = mock_home / ".codex" / "hooks.json"
        hooks_config = json.loads(hooks_path.read_text())

        # PreToolUse and PostToolUse should use ".*" regex matcher
        for event in ["PreToolUse", "PostToolUse"]:
            assert hooks_config["hooks"][event][0]["matcher"] == ".*"

    def test_hooks_dir_substituted_with_absolute_path(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps,
    ) -> None:
        """Test that $HOOKS_DIR is replaced with absolute path."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_path = mock_home / ".codex" / "hooks.json"
        hooks_content = hooks_path.read_text()

        assert "$HOOKS_DIR" not in hooks_content
        assert str(mock_home) in hooks_content

    def test_hooks_use_codex_cli_flag(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps,
    ) -> None:
        """Test that all hooks use --cli=codex flag."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_path = mock_home / ".codex" / "hooks.json"
        hooks_config = json.loads(hooks_path.read_text())

        for event_name, entries in hooks_config["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    assert "--cli=codex" in hook["command"], f"{event_name} missing --cli=codex"


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path):
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    def _make_install_dir(self, temp_dir: Path) -> Path:
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)
        hooks_template = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=SessionStart',
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'uv run "$HOOKS_DIR/hook_dispatcher.py" --cli=codex --type=Stop',
                            }
                        ]
                    }
                ],
            }
        }
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template))
        return install_dir

    def test_install_with_empty_existing_config(self, mock_home: Path, temp_dir: Path) -> None:
        """Test installation with an empty existing config file."""
        from gobby.cli.installers.codex import install_codex

        install_dir = self._make_install_dir(temp_dir)

        # Create empty config
        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text("")

        with (
            patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir),
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}
            mock_global.return_value = ["hook_dispatcher.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["config_updated"] is True
        config_data = _load_toml_file(config_path)
        assert config_data["features"]["codex_hooks"] is True

    def test_install_preserves_other_config_content(self, mock_home: Path, temp_dir: Path) -> None:
        """Test that updating config preserves other content."""
        from gobby.cli.installers.codex import install_codex

        install_dir = self._make_install_dir(temp_dir)

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        original_config = """# Comment at top
model = "gpt-4"
notify = ["old", "command"]
temperature = 0.7

[advanced]
debug = true
"""
        config_path.write_text(original_config)

        with (
            patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir),
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}
            mock_global.return_value = ["hook_dispatcher.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        new_config = _load_toml_file(config_path)
        assert new_config["model"] == "gpt-4"
        assert new_config["temperature"] == 0.7
        assert new_config["advanced"]["debug"] is True
        assert "notify" not in new_config
        assert new_config["features"]["codex_hooks"] is True
        assert "# Comment at top" in config_path.read_text()

    def test_install_corrupt_hooks_json_is_overwritten(
        self, mock_home: Path, temp_dir: Path
    ) -> None:
        """Test that corrupt hooks.json is handled gracefully."""
        from gobby.cli.installers.codex import install_codex

        install_dir = self._make_install_dir(temp_dir)

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text("{invalid json")

        with (
            patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir),
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}
            mock_global.return_value = ["hook_dispatcher.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        # Should have overwritten corrupt file
        hooks_config = json.loads(hooks_path.read_text())
        assert "hooks" in hooks_config


class TestResultStructure:
    """Tests for the result dictionary structure."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path):
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path):
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)
        hooks_template = {
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo test"}]}]}
        }
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    def test_install_result_has_all_keys(self, mock_home: Path, mock_install_dir: Path) -> None:
        """Test that install result contains all expected keys."""
        from gobby.cli.installers.codex import install_codex

        with (
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.install_global_hooks") as mock_global,
            patch("gobby.cli.installers.codex.clean_project_hooks"),
        ):
            mock_shared.return_value = {"plugins": []}
            mock_cli.return_value = {"commands": []}
            mock_mcp.return_value = {"success": True, "added": True}
            mock_global.return_value = ["hook_dispatcher.py"]

            result = install_codex(mock_home)

        expected_keys = {
            "success",
            "hooks_installed",
            "files_installed",
            "workflows_installed",
            "commands_installed",
            "plugins_installed",
            "agents_installed",
            "config_updated",
            "mcp_configured",
            "mcp_already_configured",
            "error",
        }
        assert set(result.keys()) >= expected_keys

    def test_uninstall_result_has_all_keys(self, mock_home: Path) -> None:
        """Test that uninstall result contains all expected keys."""
        from gobby.cli.installers.codex import uninstall_codex

        with patch("gobby.cli.installers.codex.remove_mcp_server_toml") as mock_mcp:
            mock_mcp.return_value = {"success": True, "removed": True}

            result = uninstall_codex()

        expected_keys = {
            "success",
            "hooks_removed",
            "files_removed",
            "config_updated",
            "mcp_removed",
            "error",
        }
        assert set(result.keys()) == expected_keys
