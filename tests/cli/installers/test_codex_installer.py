"""Comprehensive tests for the Codex CLI installer module."""

import json
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# The hooks template events that install_codex should write
EXPECTED_HOOK_EVENTS: Final[tuple[str, ...]] = (
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SubagentStart",
    "SessionEnd",
    "UserPromptSubmit",
    "SubagentStop",
    "Stop",
)
EXPECTED_HOOK_EVENT_SET: Final[set[str]] = set(EXPECTED_HOOK_EVENTS)
HOOKS_WITH_MATCHERS: Final[set[str]] = {"PreToolUse", "PermissionRequest", "PostToolUse"}
EVENT_KEY_LABELS: Final[dict[str, str]] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SubagentStart": "subagent_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
}


def _load_toml_file(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _make_hooks_template(events: tuple[str, ...] = EXPECTED_HOOK_EVENTS) -> dict[str, Any]:
    hooks: dict[str, Any] = {}
    for event in events:
        command = f"ghook --gobby-owned --cli=codex --type={event}"
        handler: dict[str, Any] = {
            "type": "command",
            "command": command,
        }
        if event == "SessionEnd":
            handler["command"] = f"{command} --enqueue-only"
            handler["timeout"] = 3
        group: dict[str, Any] = {"hooks": [handler]}
        if event in HOOKS_WITH_MATCHERS:
            group["matcher"] = ".*"
        hooks[event] = [group]
    return {"hooks": hooks}


def _assert_stable_hooks_feature(config_data: dict[str, Any]) -> None:
    features = config_data["features"]
    assert isinstance(features, dict)
    assert features["hooks"] is True
    assert "codex_hooks" not in features


def _assert_gobby_trust_state(config_data: dict[str, Any], hooks_path: Path) -> None:
    hooks = config_data["hooks"]
    assert isinstance(hooks, dict)
    state = hooks["state"]
    assert isinstance(state, dict)

    hooks_prefix = f"{hooks_path.resolve()}:"
    gobby_entries = {
        key: value
        for key, value in state.items()
        if isinstance(key, str) and key.startswith(hooks_prefix)
    }
    assert len(gobby_entries) == len(EXPECTED_HOOK_EVENTS)
    for event in EXPECTED_HOOK_EVENTS:
        suffix = f":{EVENT_KEY_LABELS[event]}:0:0"
        matching = [entry for key, entry in gobby_entries.items() if key.endswith(suffix)]
        assert len(matching) == 1
        assert isinstance(matching[0], dict)
        assert matching[0]["trusted_hash"].startswith("sha256:")


def test_set_toml_value_raises_when_descending_through_scalar() -> None:
    from gobby.cli.installers.codex import _load_toml_config, _set_toml_value

    config = _load_toml_config('foo = "bar"\n')

    with pytest.raises(ValueError, match="foo"):
        _set_toml_value(config, "foo.bar", True)


def test_codex_hook_trust_hash_matches_codex_discovery() -> None:
    from gobby.cli.installers.codex import _normalized_codex_command_hook_hash

    # Codex canonicalizes the event name plus command-hook payload before SHA-256 hashing.
    hook = {"type": "command", "command": "python3 /tmp/user.py"}
    spaced_hook = {"type": "command", "command": "  python3   /tmp/user.py\n"}
    argv_hook = {"type": "command", "command": ["python3", "/tmp/user.py"]}
    expected = "sha256:775a1a39423c99333a34296e0b7c23c35bd26a3f709d4df4fbb3d15304ae8adc"

    assert _normalized_codex_command_hook_hash("SessionStart", {"hooks": [hook]}, hook) == expected
    assert (
        _normalized_codex_command_hook_hash("SessionStart", {"hooks": [spaced_hook]}, spaced_hook)
        == expected
    )
    assert (
        _normalized_codex_command_hook_hash("SessionStart", {"hooks": [argv_hook]}, argv_hook)
        == expected
    )


@pytest.mark.parametrize("event", ["SubagentStart", "SubagentStop"])
def test_codex_subagent_trust_hash_includes_native_matcher(event: str) -> None:
    """Codex lifecycle trust identities include matchers when present."""
    from gobby.cli.installers.codex import _normalized_codex_command_hook_hash

    hook = {"type": "command", "command": f"ghook --cli=codex --type={event}"}

    matcherless_hash = _normalized_codex_command_hook_hash(event, {"hooks": [hook]}, hook)
    matcher_hash = _normalized_codex_command_hook_hash(
        event, {"matcher": ".*", "hooks": [hook]}, hook
    )

    assert matcher_hash != matcherless_hash


def test_codex_hook_trust_state_prunes_stale_gobby_positions(tmp_path: Path) -> None:
    from gobby.cli.installers.codex import (
        _ensure_codex_hook_trust_state,
        _load_toml_config,
    )

    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": "python3 /tmp/user.py"}],
                        },
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "ghook --gobby-owned --cli=codex --type=PreToolUse",
                                }
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hooks_prefix = str(hooks_path.resolve())
    config = _load_toml_config(
        f"""
[hooks.state."{hooks_prefix}:pre_tool_use:0:0"]
trusted_hash = "sha256:old-position-without-gobby-marker"

[hooks.state."{hooks_prefix}:pre_tool_use:9:0"]
trusted_hash = "sha256:user-tool"
"""
    )

    trusted_keys = _ensure_codex_hook_trust_state(
        config,
        hooks_path,
        previous_entries=[
            (
                f"{hooks_prefix}:pre_tool_use:0:0",
                "sha256:old-position-without-gobby-marker",
            )
        ],
    )

    state = cast(Any, config["hooks"])["state"]
    assert f"{hooks_prefix}:pre_tool_use:1:0" in trusted_keys
    assert f"{hooks_prefix}:pre_tool_use:0:0" not in state
    assert state[f"{hooks_prefix}:pre_tool_use:9:0"]["trusted_hash"] == "sha256:user-tool"


def test_codex_hook_trust_state_reenables_disabled_gobby_hooks(tmp_path: Path) -> None:
    from gobby.cli.installers.codex import (
        _ensure_codex_hook_trust_state,
        _load_toml_config,
        _normalized_codex_command_hook_hash,
    )

    # Codex disables a hook by writing enabled=false into its trust entry; a
    # disabled Gobby hook deadlocks enforcement gates and must be re-enabled.
    gobby_hook = {
        "type": "command",
        "command": "ghook --gobby-owned --cli=codex --type=PostToolUse",
    }
    gobby_group = {"matcher": ".*", "hooks": [gobby_hook]}
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        gobby_group,
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": "python3 /tmp/user.py"}],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hooks_prefix = str(hooks_path.resolve())
    config = _load_toml_config(
        f"""
[hooks.state."{hooks_prefix}:post_tool_use:0:0"]
enabled = false
trusted_hash = "sha256:stale"

[hooks.state."{hooks_prefix}:post_tool_use:1:0"]
enabled = false
trusted_hash = "sha256:user-tool"
"""
    )

    trusted_keys = _ensure_codex_hook_trust_state(config, hooks_path)

    hooks_table = cast(dict[str, Any], config["hooks"])
    state = cast(dict[str, Any], hooks_table["state"])
    gobby_key = f"{hooks_prefix}:post_tool_use:0:0"
    gobby_entry = cast(dict[str, Any], state[gobby_key])
    assert gobby_key in trusted_keys
    assert "enabled" not in gobby_entry
    assert gobby_entry["trusted_hash"] == _normalized_codex_command_hook_hash(
        "PostToolUse", gobby_group, gobby_hook
    )
    user_entry = cast(dict[str, Any], state[f"{hooks_prefix}:post_tool_use:1:0"])
    assert user_entry["enabled"] is False
    assert user_entry["trusted_hash"] == "sha256:user-tool"


class TestInstallCodex:
    """Tests for install_codex function."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
            patch(
                "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
                return_value="/Users/test/.gobby/bin/ghook",
            ),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Iterator[Path]:
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)

        hooks_template = _make_hooks_template()
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template, indent=2))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    @pytest.fixture
    def mock_shared_content(self) -> Iterator[tuple[Any, Any, Any]]:
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
            mock_global.return_value = ["validate_settings.py"]
            mock_clean.return_value = []
            yield mock_shared, mock_cli, mock_global

    @pytest.fixture
    def mock_mcp_configure(self) -> Iterator[Any]:
        """Mock the MCP server configuration."""
        with patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock:
            mock.return_value = {"success": True, "added": True, "already_configured": False}
            yield mock

    def test_install_success_new_config(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
    ) -> None:
        """Test successful installation with a new config file."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)

        assert result["success"] is True
        assert result["error"] is None
        assert result["mcp_configured"] is True
        assert result["trust"]["success"] is True

        # Verify hooks.json was created
        hooks_path = mock_home / ".codex" / "hooks.json"
        assert hooks_path.exists()
        hooks_config = json.loads(hooks_path.read_text())
        assert set(hooks_config["hooks"].keys()) == EXPECTED_HOOK_EVENT_SET

        # Verify $HOOKS_DIR was substituted
        hooks_str = hooks_path.read_text()
        assert "$HOOKS_DIR" not in hooks_str
        assert "ghook --gobby-owned" in hooks_str
        assert "--cli=codex" in hooks_str

        # Verify config.toml has feature flag
        config_path = mock_home / ".codex" / "config.toml"
        assert config_path.exists()
        config_data = _load_toml_file(config_path)
        _assert_stable_hooks_feature(config_data)
        _assert_gobby_trust_state(config_data, hooks_path)
        assert config_data["projects"][os.environ["GOBBY_HOME"]]["trust_level"] == "trusted"

    def test_install_hooks_installed_list(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
    ) -> None:
        """Test that hooks_installed lists all 8 event types."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)

        assert result["success"] is True
        assert set(result["hooks_installed"]) == EXPECTED_HOOK_EVENT_SET

    def test_install_existing_config_with_feature_flag(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
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
        _assert_stable_hooks_feature(config_data)

    def test_install_feature_flag_before_table_headers(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
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
        mock_shared_content: Any,
        mock_mcp_configure: Any,
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
        assert "hooks = true" in config_content
        assert "codex_hooks" not in config_content
        # [features] section should still exist
        assert "[features]" in config_content
        # fast_mode preserved
        assert "fast_mode = true" in config_content
        # hooks must be between [features] and [mcp_servers]
        features_pos = config_content.index("[features]")
        hooks_pos = config_content.index("hooks = true")
        mcp_pos = config_content.index("[mcp_servers")
        assert features_pos < hooks_pos < mcp_pos
        parsed = tomllib.loads(config_content)
        assert parsed["features"]["fast_mode"] is True
        _assert_stable_hooks_feature(parsed)

    def test_install_replaces_legacy_flag_in_existing_features_section(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
    ) -> None:
        """Test that existing codex_hooks in [features] section is removed."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text("[features]\ncodex_hooks = false\nfast_mode = true\n")

        result = install_codex(mock_home)

        assert result["success"] is True
        config_content = config_path.read_text()
        parsed = tomllib.loads(config_content)
        _assert_stable_hooks_feature(parsed)
        assert parsed["features"]["fast_mode"] is True

    def test_install_merges_into_existing_hooks_json(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
    ) -> None:
        """Test that existing hooks.json entries are preserved during merge."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        existing_hooks = {
            "hooks": {
                "CustomEvent": [{"hooks": [{"type": "command", "command": "echo custom"}]}],
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {"type": "command", "command": "echo user pre-tool hook"},
                            {
                                "type": "command",
                                "command": (
                                    "ghook --gobby-owned --cli=codex --type=PreToolUse --old"
                                ),
                            },
                        ],
                    }
                ],
            }
        }
        hooks_path.write_text(json.dumps(existing_hooks))

        result = install_codex(mock_home)

        assert result["success"] is True
        merged = json.loads(hooks_path.read_text())
        # Custom event preserved
        assert "CustomEvent" in merged["hooks"]
        # Gobby events added
        assert set(merged["hooks"].keys()) >= EXPECTED_HOOK_EVENT_SET
        pre_tool_commands = [
            hook["command"] for group in merged["hooks"]["PreToolUse"] for hook in group["hooks"]
        ]
        assert "echo user pre-tool hook" in pre_tool_commands
        assert not any(command.endswith("--old") for command in pre_tool_commands)

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
            mock_global.return_value = ["validate_settings.py"]
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
        mock_shared_content: Any,
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
        mock_shared_content: Any,
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
        mock_mcp_configure: Any,
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
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["workflows_installed"] == []  # DB-managed
        assert result["commands_installed"] == ["command1"]
        assert result["plugins_installed"] == ["plugin.py"]

    def test_install_config_write_exception(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
        mock_mcp_configure: Any,
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
        mock_shared_content: Any,
        mock_mcp_configure: Any,
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
        _assert_stable_hooks_feature(_load_toml_file(config_path))

    def test_install_repairs_stale_uv_directory_mcp_entry(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_shared_content: Any,
    ) -> None:
        """Install repairs old Codex MCP entries that pin Gobby's repo as the CWD."""
        from gobby.cli.installers.codex import install_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            '[mcp_servers.gobby]\ncommand = "uv"\n'
            'args = ["run", "--directory", "/Users/test/gobby", "gobby", "mcp-server"]\n'
        )

        result = install_codex(mock_home)

        assert result["success"] is True
        config = _load_toml_file(config_path)
        assert config["mcp_servers"]["gobby"]["command"] == "gobby"
        assert list(config["mcp_servers"]["gobby"]["args"]) == ["mcp-server"]


class TestInstallCodexProjectHooks:
    """Tests for project-local Codex hook installation."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
            patch(
                "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
                return_value="/Users/test/.gobby/bin/ghook",
            ),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Iterator[Path]:
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "hooks-template.json").write_text(json.dumps(_make_hooks_template(), indent=2))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    def test_project_install_creates_hooks_and_trust(
        self,
        mock_home: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test project hook install writes project hooks and Codex trust state."""
        from gobby.cli.installers.codex import install_codex_project_hooks

        project_path = mock_home / "worktree"
        project_path.mkdir()

        with patch("gobby.cli.installers.codex.install_global_hooks") as mock_global:
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex_project_hooks(
                project_path,
                hook_timeout_seconds=150,
            )

        assert result["success"] is True
        assert result["error"] is None
        assert set(result["hooks_installed"]) == EXPECTED_HOOK_EVENT_SET
        assert result["files_installed"] == ["validate_settings.py"]
        assert result["config_updated"] is True

        project_hooks_path = project_path / ".codex" / "hooks.json"
        assert project_hooks_path.exists()
        assert not (mock_home / ".codex" / "hooks.json").exists()

        hooks_config = json.loads(project_hooks_path.read_text())
        assert set(hooks_config["hooks"].keys()) == EXPECTED_HOOK_EVENT_SET
        assert hooks_config["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 150
        session_end = hooks_config["hooks"]["SessionEnd"][0]["hooks"][0]
        assert session_end["timeout"] == 3
        assert "--enqueue-only" in session_end["command"]
        hooks_content = project_hooks_path.read_text()
        assert "$HOOKS_DIR" not in hooks_content
        assert "ghook --gobby-owned" in hooks_content
        assert "--cli=codex" in hooks_content

        config_data = _load_toml_file(mock_home / ".codex" / "config.toml")
        _assert_stable_hooks_feature(config_data)
        _assert_gobby_trust_state(config_data, project_hooks_path)
        assert config_data["projects"][str(project_path)]["trust_level"] == "trusted"
        assert result["trust"]["success"] is True

    def test_project_install_preserves_user_hooks_and_replaces_gobby_hooks(
        self,
        mock_home: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test reinstall keeps user hooks while replacing stale Gobby-owned hooks."""
        from gobby.cli.installers.codex import install_codex_project_hooks

        project_path = mock_home / "worktree"
        project_path.mkdir()
        project_hooks_path = project_path / ".codex" / "hooks.json"

        with patch("gobby.cli.installers.codex.install_global_hooks", return_value=[]):
            first_result = install_codex_project_hooks(project_path)

        assert first_result["success"] is True

        hooks_config = json.loads(project_hooks_path.read_text())
        hooks_config["hooks"]["PreToolUse"].insert(
            0,
            {
                "matcher": ".*",
                "hooks": [
                    {"type": "command", "command": "echo user pre-tool hook"},
                    {
                        "type": "command",
                        "command": "ghook --gobby-owned --cli=codex --type=PreToolUse --old",
                    },
                ],
            },
        )
        hooks_config["hooks"]["SessionEnd"].insert(
            0,
            {
                "matcher": "custom-session-end",
                "hooks": [
                    {"type": "command", "command": "echo user session end hook"},
                    {
                        "type": "command",
                        "command": ("ghook --gobby-owned --cli=codex --type=SessionEnd --old"),
                    },
                ],
            },
        )
        hooks_config["hooks"]["CustomEvent"] = [
            {"hooks": [{"type": "command", "command": "echo custom hook"}]}
        ]
        project_hooks_path.write_text(json.dumps(hooks_config))

        with patch("gobby.cli.installers.codex.install_global_hooks", return_value=[]):
            second_result = install_codex_project_hooks(project_path)

        assert second_result["success"] is True
        merged = json.loads(project_hooks_path.read_text())
        assert "CustomEvent" in merged["hooks"]
        pre_tool_commands = [
            hook["command"] for group in merged["hooks"]["PreToolUse"] for hook in group["hooks"]
        ]
        assert "echo user pre-tool hook" in pre_tool_commands
        assert "echo custom hook" in json.dumps(merged["hooks"]["CustomEvent"])
        assert not any(command.endswith("--old") for command in pre_tool_commands)
        assert sum("--gobby-owned" in command for command in pre_tool_commands) == 1
        session_end_groups = merged["hooks"]["SessionEnd"]
        custom_group = next(
            group for group in session_end_groups if group.get("matcher") == "custom-session-end"
        )
        assert custom_group["hooks"] == [
            {"type": "command", "command": "echo user session end hook"}
        ]
        gobby_session_end_groups = [
            group
            for group in session_end_groups
            if any("--gobby-owned" in hook["command"] for hook in group["hooks"])
        ]
        assert len(gobby_session_end_groups) == 1
        gobby_group = gobby_session_end_groups[0]
        assert "matcher" not in gobby_group
        assert len(gobby_group["hooks"]) == 1
        gobby_handler = gobby_group["hooks"][0]
        assert gobby_handler["type"] == "command"
        assert gobby_handler["command"].endswith(
            " --gobby-owned --cli=codex --type=SessionEnd --enqueue-only"
        )
        assert gobby_handler["timeout"] == 3

    def test_project_install_skips_shared_cli_and_cleanup(
        self,
        mock_home: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test project hook install avoids shared content, CLI content, and cleanup."""
        from gobby.cli.installers.codex import install_codex_project_hooks

        project_path = mock_home / "worktree"
        project_path.mkdir()

        with (
            patch("gobby.cli.installers.codex.install_global_hooks", return_value=[]),
            patch("gobby.cli.installers.codex.install_shared_content") as mock_shared,
            patch("gobby.cli.installers.codex.install_cli_content") as mock_cli,
            patch("gobby.cli.installers.codex.clean_project_hooks") as mock_clean,
        ):
            result = install_codex_project_hooks(project_path)

        assert result["success"] is True
        assert (project_path / ".codex" / "hooks.json").exists()
        assert not (mock_home / ".codex" / "hooks.json").exists()
        mock_shared.assert_not_called()
        mock_cli.assert_not_called()
        mock_clean.assert_not_called()

    def test_project_install_skips_mcp_config(
        self,
        mock_home: Path,
        mock_install_dir: Path,
    ) -> None:
        """Test project hook install avoids MCP configuration changes."""
        from gobby.cli.installers.codex import install_codex_project_hooks

        project_path = mock_home / "worktree"
        project_path.mkdir()

        with (
            patch("gobby.cli.installers.codex.install_global_hooks", return_value=[]),
            patch("gobby.cli.installers.codex.configure_mcp_server_toml") as mock_mcp,
            patch("gobby.cli.installers.codex.strip_mcp_tool_overrides_toml") as mock_strip,
        ):
            result = install_codex_project_hooks(project_path)

        assert result["success"] is True
        config_data = _load_toml_file(mock_home / ".codex" / "config.toml")
        assert "mcp_servers" not in config_data
        mock_mcp.assert_not_called()
        mock_strip.assert_not_called()


class TestUninstallCodex:
    """Tests for uninstall_codex function."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_mcp_remove(self) -> Iterator[Any]:
        """Mock the MCP server removal function."""
        with patch("gobby.cli.installers.codex.remove_mcp_server_toml") as mock:
            mock.return_value = {"success": True, "removed": True}
            yield mock

    def test_uninstall_success_full(self, mock_home: Path, mock_mcp_remove: Any) -> None:
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
                            {
                                "type": "command",
                                "command": "ghook --gobby-owned --cli=codex --type=SessionStart",
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
                                "command": "ghook --gobby-owned --cli=codex --type=PreToolUse",
                            }
                        ],
                    }
                ],
            }
        }
        hooks_path.write_text(json.dumps(hooks_config))

        # Set up config.toml with feature flags and hook trust state
        config_path = codex_dir / "config.toml"
        hooks_prefix = str(hooks_path.resolve())
        config_path.write_text(
            f"""model = "gpt-4"

[features]
hooks = true
codex_hooks = true

[hooks.state."{hooks_prefix}:session_start:0:0"]
trusted_hash = "sha256:gobby-session"

[hooks.state."{hooks_prefix}:pre_tool_use:0:0"]
trusted_hash = "sha256:gobby-tool"

[hooks.state."{hooks_prefix}:pre_tool_use:9:0"]
trusted_hash = "sha256:user-tool"
"""
        )

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
        assert not isinstance(features, dict) or "hooks" not in features
        assert not isinstance(features, dict) or "codex_hooks" not in features
        state = config_data["hooks"]["state"]
        assert f"{hooks_prefix}:session_start:0:0" not in state
        assert f"{hooks_prefix}:pre_tool_use:0:0" not in state
        assert state[f"{hooks_prefix}:pre_tool_use:9:0"]["trusted_hash"] == "sha256:user-tool"
        assert config_data["model"] == "gpt-4"

    def test_uninstall_preserves_non_gobby_hooks(
        self, mock_home: Path, mock_mcp_remove: Any
    ) -> None:
        """Test that non-gobby hooks are preserved in hooks.json."""
        from gobby.cli.installers.codex import uninstall_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_config = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "ghook --gobby-owned --cli=codex --type=SessionStart",
                            },
                            {"type": "command", "command": "echo user session hook"},
                        ]
                    }
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
        assert remaining["hooks"]["SessionStart"][0]["hooks"] == [
            {"type": "command", "command": "echo user session hook"}
        ]

    def test_uninstall_no_hooks_json(self, mock_home: Path, mock_mcp_remove: Any) -> None:
        """Test uninstallation when hooks.json doesn't exist."""
        from gobby.cli.installers.codex import uninstall_codex

        result = uninstall_codex()

        assert result["success"] is True
        assert len(result["hooks_removed"]) == 0

    def test_uninstall_no_config_file(self, mock_home: Path, mock_mcp_remove: Any) -> None:
        """Test uninstallation when config file doesn't exist."""
        from gobby.cli.installers.codex import uninstall_codex

        result = uninstall_codex()

        assert result["success"] is True
        assert result["config_updated"] is False

    def test_uninstall_creates_backup(self, mock_home: Path, mock_mcp_remove: Any) -> None:
        """Test that config backup is created before modification."""
        from gobby.cli.installers.codex import uninstall_codex

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        config_path = codex_dir / "config.toml"
        original = 'features.hooks = true\nfeatures.codex_hooks = true\nmodel = "gpt-4"\n'
        config_path.write_text(original)

        result = uninstall_codex()

        assert result["success"] is True
        backup_path = config_path.with_suffix(".toml.bak")
        assert backup_path.exists()
        assert backup_path.read_text() == original

    def test_uninstall_nothing_installed(self, mock_home: Path, mock_mcp_remove: Any) -> None:
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


class TestHooksTemplateFormat:
    """Tests for hooks.json format and content."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Iterator[Path]:
        """Create a mock install directory with hooks-template.json."""
        install_dir = temp_dir / "install"
        codex_dir = install_dir / "codex"
        codex_dir.mkdir(parents=True)

        hooks_template = _make_hooks_template()
        (codex_dir / "hooks-template.json").write_text(json.dumps(hooks_template, indent=2))

        with patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir):
            yield install_dir

    @pytest.fixture
    def mock_deps(self) -> Iterator[None]:
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
            mock_global.return_value = ["validate_settings.py"]
            yield

    def test_hooks_use_regex_matcher(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps: None,
    ) -> None:
        """Test that PreToolUse/PostToolUse use regex matchers (not glob)."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_path = mock_home / ".codex" / "hooks.json"
        hooks_config = json.loads(hooks_path.read_text())

        # Tool and permission events should use ".*" regex matchers
        for event in ["PreToolUse", "PermissionRequest", "PostToolUse"]:
            assert hooks_config["hooks"][event][0]["matcher"] == ".*"

    def test_subagent_hooks_are_matcherless_with_standard_timeout(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps: None,
    ) -> None:
        """Subagent lifecycle handlers use the standard matcherless hook shape."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_config = json.loads((mock_home / ".codex" / "hooks.json").read_text())
        for event in ["SubagentStart", "SubagentStop"]:
            group = hooks_config["hooks"][event][0]
            assert "matcher" not in group
            assert group["hooks"][0]["timeout"] == 120

    def test_hooks_dir_substituted_with_absolute_path(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps: None,
    ) -> None:
        """Test that $HOOKS_DIR is replaced with absolute path."""
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)
        assert result["success"] is True

        hooks_path = mock_home / ".codex" / "hooks.json"
        hooks_content = hooks_path.read_text()

        assert "$HOOKS_DIR" not in hooks_content
        assert "ghook --gobby-owned" in hooks_content
        assert "--cli=codex" in hooks_content

    def test_hooks_use_codex_cli_flag(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps: None,
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

    def test_session_end_is_matcherless_enqueue_only_with_three_second_timeout(
        self,
        mock_home: Path,
        mock_install_dir: Path,
        mock_deps: Any,
    ) -> None:
        from gobby.cli.installers.codex import install_codex

        result = install_codex(mock_home)

        assert result["success"] is True
        hooks_config = json.loads((mock_home / ".codex" / "hooks.json").read_text())
        session_end_groups = hooks_config["hooks"]["SessionEnd"]
        assert len(session_end_groups) == 1
        assert "matcher" not in session_end_groups[0]
        handler = session_end_groups[0]["hooks"][0]
        assert handler["command"].endswith(
            " --gobby-owned --cli=codex --type=SessionEnd --enqueue-only"
        )
        assert handler["timeout"] == 3


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
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
        hooks_template = _make_hooks_template()
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
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        assert result["config_updated"] is True
        config_data = _load_toml_file(config_path)
        _assert_stable_hooks_feature(config_data)

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
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        new_config = _load_toml_file(config_path)
        assert new_config["model"] == "gpt-4"
        assert list(new_config["notify"]) == ["old", "command"]
        assert new_config["temperature"] == 0.7
        assert new_config["advanced"]["debug"] is True
        _assert_stable_hooks_feature(new_config)
        assert "# Comment at top" in config_path.read_text()

    def test_install_corrupt_hooks_json_is_quarantined(
        self, mock_home: Path, temp_dir: Path
    ) -> None:
        """A corrupt hooks.json is preserved as a .corrupt sibling, never silently lost."""
        from gobby.cli.installers.codex import install_codex

        install_dir = self._make_install_dir(temp_dir)

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        corrupt_content = '{invalid json with a foreign hook: "node third-party.mjs"'
        hooks_path.write_text(corrupt_content)

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
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        # Fresh Gobby hooks installed
        hooks_config = json.loads(hooks_path.read_text())
        assert "hooks" in hooks_config
        # Original content preserved beside it
        quarantined = list(codex_dir.glob("hooks.json.*.corrupt"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text() == corrupt_content

    def test_corrupt_hook_quarantines_are_unique_within_same_second(self, tmp_path: Path) -> None:
        from gobby.cli.installers.codex import _quarantine_corrupt_hooks_file

        hooks_path = tmp_path / "hooks.json"
        first_bytes = b'{"first": invalid}'
        second_bytes = b'{"second": invalid}'

        with patch("gobby.cli.installers.codex.datetime") as mock_datetime:
            mock_datetime.now.return_value.strftime.return_value = "20260808123456"
            hooks_path.write_bytes(first_bytes)
            _quarantine_corrupt_hooks_file(hooks_path, "first")
            hooks_path.write_bytes(second_bytes)
            _quarantine_corrupt_hooks_file(hooks_path, "second")

        quarantined = list(tmp_path.glob("hooks.json.20260808123456.*.corrupt"))
        assert len(quarantined) == 2
        assert {path.read_bytes() for path in quarantined} == {first_bytes, second_bytes}

    def test_corrupt_hook_quarantine_replace_failure_preserves_source(self, tmp_path: Path) -> None:
        from gobby.cli.installers.codex import _quarantine_corrupt_hooks_file

        hooks_path = tmp_path / "hooks.json"
        original_bytes = b'{"foreign": invalid}'
        hooks_path.write_bytes(original_bytes)

        with (
            patch("gobby.cli.installers.codex.os.replace", side_effect=OSError("denied")),
            pytest.raises(OSError, match="denied"),
        ):
            _quarantine_corrupt_hooks_file(hooks_path, "invalid JSON")

        assert hooks_path.read_bytes() == original_bytes
        assert list(tmp_path.glob("hooks.json.*.corrupt")) == []

    def test_corrupt_hook_replacement_write_failure_leaves_recoverable_quarantine(
        self, tmp_path: Path
    ) -> None:
        from gobby.cli.installers.codex import _install_hooks_file

        install_dir = self._make_install_dir(tmp_path)
        hooks_dir = tmp_path / "installed-hooks"
        hooks_dir.mkdir()
        hooks_path = tmp_path / "hooks.json"
        original_bytes = b'{"foreign": invalid}'
        hooks_path.write_bytes(original_bytes)

        with (
            patch("gobby.cli.installers.codex.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.codex._atomic_write_json",
                side_effect=OSError("write failed"),
            ),
            pytest.raises(OSError, match="write failed"),
        ):
            _install_hooks_file(hooks_path, hooks_dir)

        quarantined = list(tmp_path.glob("hooks.json.*.corrupt"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes
        assert not hooks_path.exists()

    def test_install_non_object_hooks_json_is_quarantined(
        self, mock_home: Path, temp_dir: Path
    ) -> None:
        """A hooks.json holding a non-object JSON value is quarantined, not merged."""
        from gobby.cli.installers.codex import install_codex

        install_dir = self._make_install_dir(temp_dir)

        codex_dir = mock_home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text('["not", "an", "object"]')

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
            mock_global.return_value = ["validate_settings.py"]

            result = install_codex(mock_home)

        assert result["success"] is True
        hooks_config = json.loads(hooks_path.read_text())
        assert "hooks" in hooks_config
        quarantined = list(codex_dir.glob("hooks.json.*.corrupt"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text() == '["not", "an", "object"]'


class TestResultStructure:
    """Tests for the result dictionary structure."""

    @pytest.fixture
    def mock_home(self, temp_dir: Path) -> Iterator[Path]:
        """Mock Path.home() and GOBBY_HOOKS_DIR to return temp directory."""
        hooks_dir = str(temp_dir / ".gobby" / "hooks")
        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.dict(os.environ, {"GOBBY_HOOKS_DIR": hooks_dir}),
        ):
            yield temp_dir

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Iterator[Path]:
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
            mock_global.return_value = ["validate_settings.py"]

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
        assert result["success"] is True

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
