"""
Tests for Sandbox Configuration Models.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents import sandbox_policy
from gobby.agents.constants import GOBBY_SESSION_ID, get_agent_session_cache_dir
from gobby.agents.provider_capabilities import provider_capabilities, provider_supports_sandbox
from gobby.agents.sandbox import (
    _DAEMON_SANDBOX_POLICY_VERSION,
    ResolvedSandboxPaths,
    SandboxConfig,
    _resolve_git_metadata_path,
    coerce_sandbox_config,
    compute_sandbox_paths,
    daemon_owned_sandbox_policy_hash,
    web_chat_sandbox_policy_hash,
)
from gobby.agents.sandbox_policy import default_write_paths
from gobby.agents.sandbox_resolvers import (
    ClaudeSandboxResolver,
    CodexSandboxResolver,
    QwenSandboxResolver,
    SandboxResolver,
    _normalize_sandbox_path,
    get_sandbox_resolver,
    materialize_claude_settings,
    materialize_claude_settings_async,
)
from gobby.agents.spawn_cache_policy import (
    SANDBOX_CACHE_ENV_VARS,
    build_spawn_cache_env,
    sandbox_config_for_spawn,
)
from gobby.config.app import DaemonConfig
from gobby.integrations.rtk import platform_paths
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_stdout(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _resolve_from(workspace: Path, raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        path = workspace / path
    return str(path.resolve(strict=False))


def _include_dirs(args: list[str]) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args) if arg == "--include-directories"]


@pytest.mark.unit
class TestSandboxConfig:
    """Tests for SandboxConfig Pydantic model."""

    def test_default_values(self) -> None:
        """Test that SandboxConfig has correct default values."""
        config = SandboxConfig()

        assert config.enabled is False
        assert config.mode == "permissive"
        assert config.allow_network is True
        assert config.extra_read_paths == []
        assert config.extra_write_paths == []

    def test_custom_values(self) -> None:
        """Test creating SandboxConfig with custom values."""
        config = SandboxConfig(
            enabled=True,
            mode="restrictive",
            allow_network=False,
            extra_read_paths=["/usr/share", "/opt/data"],
            extra_write_paths=["/tmp/output"],
        )

        assert config.enabled is True
        assert config.mode == "restrictive"
        assert config.allow_network is False
        assert config.extra_read_paths == ["/usr/share", "/opt/data"]
        assert config.extra_write_paths == ["/tmp/output"]

    def test_mode_literal_validation(self) -> None:
        """Test that mode only accepts valid literal values."""
        # Valid modes
        for mode in ["permissive", "restrictive"]:
            config = SandboxConfig(mode=mode)
            assert config.mode == mode

        # Invalid mode should raise validation error
        with pytest.raises(ValueError):
            SandboxConfig(mode="invalid_mode")

    def test_serialization_to_dict(self) -> None:
        """Test that SandboxConfig can be serialized to dict."""
        config = SandboxConfig(
            enabled=True,
            mode="restrictive",
            allow_network=False,
            extra_read_paths=["/path/one"],
            extra_write_paths=["/path/two"],
        )

        data = config.model_dump()

        assert isinstance(data, dict)
        assert data["enabled"] is True
        assert data["mode"] == "restrictive"
        assert data["allow_network"] is False
        assert data["extra_read_paths"] == ["/path/one"]
        assert data["extra_write_paths"] == ["/path/two"]

    def test_serialization_from_dict(self) -> None:
        """Test that SandboxConfig can be created from dict."""
        data: dict[str, Any] = {
            "enabled": True,
            "mode": "permissive",
            "allow_network": True,
            "extra_read_paths": ["/data"],
            "extra_write_paths": [],
        }

        config = SandboxConfig(**data)

        assert config.enabled is True
        assert config.mode == "permissive"
        assert config.extra_read_paths == ["/data"]

    def test_json_serialization(self) -> None:
        """Test JSON serialization round-trip."""
        config = SandboxConfig(
            enabled=True,
            mode="restrictive",
            extra_read_paths=["/opt"],
        )

        json_str = config.model_dump_json()
        restored = SandboxConfig.model_validate_json(json_str)

        assert restored.enabled == config.enabled
        assert restored.mode == config.mode
        assert restored.extra_read_paths == config.extra_read_paths

    def test_partial_dict_uses_defaults(self) -> None:
        """Test that partial dict uses defaults for missing fields."""
        data: dict[str, Any] = {
            "enabled": True,
        }

        config = SandboxConfig(**data)

        assert config.enabled is True
        # Other fields should have defaults
        assert config.mode == "permissive"
        assert config.allow_network is True
        assert config.extra_read_paths == []
        assert config.extra_write_paths == []

    def test_enabled_field_accepts_bool(self) -> None:
        """Test that enabled field only accepts boolean values."""
        # True
        config_true = SandboxConfig(enabled=True)
        assert config_true.enabled is True

        # False
        config_false = SandboxConfig(enabled=False)
        assert config_false.enabled is False

    def test_allow_network_field_accepts_bool(self) -> None:
        """Test that allow_network field only accepts boolean values."""
        # True
        config_true = SandboxConfig(allow_network=True)
        assert config_true.allow_network is True

        # False
        config_false = SandboxConfig(allow_network=False)
        assert config_false.allow_network is False

    def test_empty_path_lists_are_valid(self) -> None:
        """Test that empty path lists are valid configuration."""
        config = SandboxConfig(
            enabled=True,
            extra_read_paths=[],
            extra_write_paths=[],
        )

        assert config.extra_read_paths == []
        assert config.extra_write_paths == []

    def test_paths_preserve_order(self) -> None:
        """Test that path lists preserve insertion order."""
        paths = ["/first", "/second", "/third"]
        config = SandboxConfig(
            extra_read_paths=paths,
            extra_write_paths=list(reversed(paths)),
        )

        assert config.extra_read_paths == ["/first", "/second", "/third"]
        assert config.extra_write_paths == ["/third", "/second", "/first"]

    def test_model_copy_deep_creates_independent_instance(self) -> None:
        """Test that model_copy(deep=True) creates an independent copy."""
        original = SandboxConfig(
            enabled=True,
            mode="restrictive",
            extra_read_paths=["/data"],
        )

        copy = original.model_copy(deep=True)
        copy.extra_read_paths.append("/new")

        # Original should be unchanged (deep copy)
        assert "/new" not in original.extra_read_paths
        assert "/new" in copy.extra_read_paths

    def test_coerce_sandbox_config_invalid_object_mode_defaults_to_permissive(self) -> None:
        """Invalid object-like sandbox modes should be normalized before model construction."""
        config = coerce_sandbox_config(SimpleNamespace(enabled=True, mode="broken"))

        assert config is not None
        assert config.enabled is True
        assert config.mode == "permissive"


@pytest.mark.unit
class TestResolvedSandboxPaths:
    """Tests for ResolvedSandboxPaths Pydantic model."""

    def test_creation_with_required_fields(self) -> None:
        """Test creating ResolvedSandboxPaths with required fields."""
        paths = ResolvedSandboxPaths(
            workspace_path="/home/user/project",
            read_paths=["/usr/share"],
            write_paths=["/home/user/project"],
            allow_external_network=False,
        )

        assert paths.workspace_path == "/home/user/project"
        assert paths.gobby_daemon_port == 60887  # Default
        assert paths.read_paths == ["/usr/share"]
        assert paths.write_paths == ["/home/user/project"]
        assert paths.allow_external_network is False

    def test_default_daemon_port(self) -> None:
        """Test that gobby_daemon_port defaults to 60887."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=[],
            allow_external_network=True,
        )

        assert paths.gobby_daemon_port == 60887

    def test_custom_daemon_port(self) -> None:
        """Test setting custom daemon port."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            gobby_daemon_port=9999,
            read_paths=[],
            write_paths=[],
            allow_external_network=True,
        )

        assert paths.gobby_daemon_port == 9999

    def test_path_list_handling(self) -> None:
        """Test that path lists are handled correctly."""
        paths = ResolvedSandboxPaths(
            workspace_path="/workspace",
            read_paths=["/opt", "/usr/local", "/etc/config"],
            write_paths=["/workspace", "/tmp"],
            allow_external_network=False,
        )

        assert len(paths.read_paths) == 3
        assert "/opt" in paths.read_paths
        assert len(paths.write_paths) == 2
        assert "/tmp" in paths.write_paths

    def test_empty_path_lists(self) -> None:
        """Test with empty path lists."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=[],
            allow_external_network=True,
        )

        assert paths.read_paths == []
        assert paths.write_paths == []

    def test_serialization_to_dict(self) -> None:
        """Test that ResolvedSandboxPaths can be serialized to dict."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            gobby_daemon_port=60887,
            read_paths=["/data"],
            write_paths=["/project"],
            allow_external_network=False,
        )

        data = paths.model_dump()

        assert isinstance(data, dict)
        assert data["workspace_path"] == "/project"
        assert data["gobby_daemon_port"] == 60887
        assert data["read_paths"] == ["/data"]
        assert data["write_paths"] == ["/project"]
        assert data["allow_external_network"] is False


@pytest.mark.unit
class TestSandboxResolver:
    """Tests for SandboxResolver abstract base class."""

    def test_cannot_instantiate_directly(self) -> None:
        """Test that SandboxResolver cannot be instantiated directly."""
        with pytest.raises(TypeError):
            cast(Any, SandboxResolver)()

    def test_subclass_must_implement_cli_name(self) -> None:
        """Test that subclass must implement cli_name property."""

        class IncompleteResolver(SandboxResolver):
            def resolve(
                self, config: SandboxConfig, paths: ResolvedSandboxPaths
            ) -> tuple[list[str], dict[str, str]]:
                return ([], {})

        with pytest.raises(TypeError):
            cast(Any, IncompleteResolver)()

    def test_subclass_must_implement_resolve(self) -> None:
        """Test that subclass must implement resolve method."""

        class IncompleteResolver(SandboxResolver):
            @property
            def cli_name(self) -> str:
                return "test"

        with pytest.raises(TypeError):
            cast(Any, IncompleteResolver)()

    def test_complete_subclass_can_be_instantiated(self) -> None:
        """Test that a complete subclass can be instantiated."""

        class CompleteResolver(SandboxResolver):
            @property
            def cli_name(self) -> str:
                return "test-cli"

            def resolve(
                self, config: SandboxConfig, paths: ResolvedSandboxPaths
            ) -> tuple[list[str], dict[str, str]]:
                return (["--sandbox"], {"TEST_VAR": "value"})

        resolver = CompleteResolver()
        assert resolver.cli_name == "test-cli"

        config = SandboxConfig(enabled=True)
        resolved_paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        args, env = resolver.resolve(config, resolved_paths)
        assert args == ["--sandbox"]
        assert env == {"TEST_VAR": "value"}


@pytest.mark.unit
class TestClaudeSandboxResolver:
    """Tests for ClaudeSandboxResolver."""

    def test_cli_name(self) -> None:
        """Test that cli_name returns 'claude'."""
        resolver = ClaudeSandboxResolver()
        assert resolver.cli_name == "claude"

    def test_disabled_returns_empty(self) -> None:
        """Test that disabled sandbox returns empty args and env."""
        resolver = ClaudeSandboxResolver()
        config = SandboxConfig(enabled=False)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert args == []
        assert env == {}

    def test_enabled_returns_settings_flag(self) -> None:
        """Test that enabled sandbox returns --settings with JSON config."""
        resolver = ClaudeSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        # Claude Code uses --settings with JSON
        assert len(args) == 2
        assert args[0] == "--settings"
        # Second arg should be valid JSON containing sandbox.enabled: true
        import json

        settings = json.loads(args[1])
        assert settings["sandbox"]["enabled"] is True

    def test_settings_json_structure(self) -> None:
        """Test that JSON settings has correct structure."""
        resolver = ClaudeSandboxResolver()
        config = SandboxConfig(enabled=True, mode="restrictive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        import json

        settings = json.loads(args[1])
        # Verify full sandbox structure
        assert settings["allowManagedPermissionRulesOnly"] is True
        assert "sandbox" in settings
        assert settings["sandbox"]["enabled"] is True
        assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False
        assert settings["sandbox"]["allowUnsandboxedCommands"] is False
        assert "network" in settings["sandbox"]
        assert settings["sandbox"]["network"]["allowLocalBinding"] is True
        assert settings["sandbox"]["network"]["allowedDomains"] == [
            "localhost",
            "127.0.0.1",
            "::1",
        ]
        assert "httpProxyPort" not in settings["sandbox"]["network"]
        assert "socksProxyPort" not in settings["sandbox"]["network"]

    def test_network_enabled_allows_loopback_domains(self) -> None:
        """Enabled network grants spawned agents access to local Gobby services."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        settings = ClaudeSandboxResolver().build_settings(SandboxConfig(enabled=True), paths)

        assert settings["sandbox"]["network"]["allowLocalBinding"] is True
        assert settings["sandbox"]["network"]["allowedDomains"] == [
            "localhost",
            "127.0.0.1",
            "::1",
        ]

    def test_network_disabled_omits_loopback_domains(self) -> None:
        """Explicit network disable keeps Claude's domain allowlist empty."""
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        settings = ClaudeSandboxResolver().build_settings(
            SandboxConfig(enabled=True, allow_network=False),
            paths,
        )

        assert settings["sandbox"]["network"]["allowLocalBinding"] is False
        assert settings["sandbox"]["network"]["allowedDomains"] == []

    def test_external_write_paths_grant_filesystem_allow_write(self, tmp_path: Path) -> None:
        """Worktree git-metadata dirs (outside the workspace) must be granted
        sandbox.filesystem.allowWrite so sandboxed commits don't EPERM."""
        import json

        workspace = tmp_path / "worktree"
        workspace.mkdir()
        git_meta = tmp_path / "repo" / ".git" / "worktrees" / "wt"

        paths = ResolvedSandboxPaths(
            workspace_path=str(workspace),
            read_paths=[],
            write_paths=[str(workspace), str(git_meta)],
            allow_external_network=True,
        )

        args, _env = ClaudeSandboxResolver().resolve(SandboxConfig(enabled=True), paths)
        settings = json.loads(args[1])

        assert settings["sandbox"]["filesystem"]["allowWrite"] == [
            str(git_meta.resolve(strict=False))
        ]

    def test_no_external_paths_still_emits_sensitive_denies(self) -> None:
        import json

        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, _env = ClaudeSandboxResolver().resolve(SandboxConfig(enabled=True), paths)
        settings = json.loads(args[1])

        assert settings["sandbox"]["filesystem"] == {"denyRead": [], "denyWrite": []}

    def test_returns_empty_env(self) -> None:
        """Test that Claude resolver always returns empty env dict."""
        resolver = ClaudeSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        args, env = resolver.resolve(config, paths)
        assert env == {}

    def test_materialize_claude_settings_merges_base_hooks(self, tmp_path: Path) -> None:
        """Generated runtime settings should preserve base settings and add sandbox config."""
        base_settings = tmp_path / "headless.json"
        base_settings.write_text('{"hooks":{"SessionStart":[]}}', encoding="utf-8")

        settings_path = materialize_claude_settings(
            base_settings_path=base_settings,
            config=SandboxConfig(enabled=True),
            workspace_path="/project",
            name="test",
        )

        assert settings_path is not None
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        assert payload["hooks"]["SessionStart"] == []
        assert payload["sandbox"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_materialize_claude_settings_async_matches_sync_output(
        self, tmp_path: Path
    ) -> None:
        """Async wrapper should preserve sync helper behavior while moving work off-loop."""
        base_settings = tmp_path / "headless.json"
        base_settings.write_text('{"hooks":{"SessionStart":[]}}', encoding="utf-8")

        settings_path = await materialize_claude_settings_async(
            base_settings_path=base_settings,
            config=SandboxConfig(enabled=True),
            workspace_path="/project",
            name="test-async",
        )

        assert settings_path is not None
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        assert payload["hooks"]["SessionStart"] == []
        assert payload["sandbox"]["enabled"] is True

    def test_materialize_claude_settings_logs_invalid_base_settings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid base settings should warn and still fall back to an empty payload."""
        base_settings = tmp_path / "headless.json"
        base_settings.write_text("{invalid", encoding="utf-8")

        with caplog.at_level("WARNING"):
            settings_path = materialize_claude_settings(
                base_settings_path=base_settings,
                config=SandboxConfig(enabled=True),
                workspace_path="/project",
                name="test-invalid",
            )

        assert settings_path is not None
        assert "Failed to read Claude base settings for runtime sandbox overlay" in caplog.text


@pytest.mark.unit
class TestCodexSandboxResolver:
    """Tests for CodexSandboxResolver."""

    def test_cli_name(self) -> None:
        """Test that cli_name returns 'codex'."""
        resolver = CodexSandboxResolver()
        assert resolver.cli_name == "codex"

    def test_disabled_returns_empty(self) -> None:
        """Test that disabled sandbox returns empty args and env."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=False)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert args == []
        assert env == {}

    def test_enabled_permissive_mode(self) -> None:
        """Test permissive mode returns workspace-write."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True, mode="permissive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, _env = resolver.resolve(config, paths)
        assert args == [
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
        ]

    def test_enabled_permissive_mode_disables_network_when_configured(self) -> None:
        """Explicit network disable is passed through to Codex workspace-write."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True, mode="permissive", allow_network=False)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        args, _env = resolver.resolve(config, paths)
        assert args == [
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=false",
        ]

    def test_enabled_restrictive_mode(self) -> None:
        """Test restrictive mode returns read-only."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True, mode="restrictive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert "--sandbox" in args
        assert "read-only" in args

    def test_extra_write_paths_added(self) -> None:
        """Test that extra write paths are added via --add-dir."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project", "/extra/path"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert "--add-dir" in args
        # Extra path should be added (workspace is implicit)
        add_dir_idx = args.index("--add-dir")
        assert args[add_dir_idx + 1] == "/extra/path"

    def test_multiple_extra_write_paths(self) -> None:
        """Test that multiple extra write paths are all added."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project", "/path/one", "/path/two"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        # Count --add-dir occurrences (should be 2 for the extra paths)
        add_dir_count = args.count("--add-dir")
        assert add_dir_count == 2

    def test_no_extra_paths_no_add_dir(self) -> None:
        """Test that no --add-dir is added when only workspace path exists."""
        resolver = CodexSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],  # Only workspace
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert "--add-dir" not in args


@pytest.mark.unit
class TestQwenSandboxResolver:
    """Tests for QwenSandboxResolver."""

    def test_cli_name(self) -> None:
        resolver = QwenSandboxResolver()
        assert resolver.cli_name == "qwen"

    def test_qwen_sets_seatbelt_profiles(self) -> None:
        resolver = QwenSandboxResolver()
        config = SandboxConfig(enabled=True, mode="restrictive", allow_network=False)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        args, env = resolver.resolve(config, paths)
        assert args == ["-s"]
        assert env["SEATBELT_PROFILE"] == "restrictive-proxied"

    def test_qwen_includes_external_write_dirs(self, tmp_path: Path) -> None:
        resolver = QwenSandboxResolver()
        workspace = tmp_path / "workspace"
        external = tmp_path / "repo" / ".git" / "worktrees" / "task"
        workspace.mkdir()
        paths = ResolvedSandboxPaths(
            workspace_path=str(workspace),
            read_paths=[],
            write_paths=[str(workspace), str(external)],
            allow_external_network=True,
        )

        args, env = resolver.resolve(SandboxConfig(enabled=True), paths)

        assert args == [
            "-s",
            "--include-directories",
            str(external.resolve(strict=False)),
        ]
        assert env["SEATBELT_PROFILE"] == "permissive-open"


@pytest.mark.unit
class TestGetSandboxResolver:
    """Tests for get_sandbox_resolver factory function."""

    def test_returns_claude_resolver(self) -> None:
        """Test that 'claude' returns ClaudeSandboxResolver."""
        resolver = get_sandbox_resolver("claude")
        assert isinstance(resolver, ClaudeSandboxResolver)

    def test_returns_codex_resolver(self) -> None:
        """Test that 'codex' returns CodexSandboxResolver."""
        resolver = get_sandbox_resolver("codex")
        assert isinstance(resolver, CodexSandboxResolver)

    def test_returns_qwen_resolver(self) -> None:
        """Test that 'qwen' returns QwenSandboxResolver."""
        resolver = get_sandbox_resolver("qwen")
        assert isinstance(resolver, QwenSandboxResolver)

    def test_droid_raises_value_error(self) -> None:
        """Test that providers without sandbox support raise ValueError."""
        with pytest.raises(ValueError, match="Unknown CLI"):
            get_sandbox_resolver("droid")

    def test_unknown_cli_raises_value_error(self) -> None:
        """Test that unknown CLI raises ValueError."""
        with pytest.raises(ValueError, match="Unknown CLI"):
            get_sandbox_resolver("unknown-cli")


@pytest.mark.unit
class TestComputeSandboxPaths:
    """Tests for compute_sandbox_paths helper function."""

    def test_computes_paths_from_config(self) -> None:
        """Test computing paths from SandboxConfig."""
        config = SandboxConfig(
            enabled=True,
            allow_network=False,
            extra_read_paths=["/opt/data"],
            extra_write_paths=["/tmp/output"],
        )

        paths = compute_sandbox_paths(
            config=config,
            workspace_path="/project",
            gobby_daemon_port=60887,
        )

        assert paths.workspace_path == "/project"
        assert paths.gobby_daemon_port == 60887
        assert paths.allow_external_network is False
        assert "/project" in paths.write_paths
        assert str(Path("/tmp/output").resolve()) in paths.write_paths
        assert str(Path("/opt/data").resolve()) in paths.read_paths

    def test_workspace_always_in_write_paths(self) -> None:
        """Test that workspace is always included in write_paths."""
        config = SandboxConfig(enabled=True)

        paths = compute_sandbox_paths(
            config=config,
            workspace_path="/my/workspace",
        )

        assert "/my/workspace" in paths.write_paths

    def test_canonicalizes_workspace_symlink_before_granting_access(self, tmp_path: Path) -> None:
        real_workspace = tmp_path / "real-workspace"
        real_workspace.mkdir()
        linked_workspace = tmp_path / "linked-workspace"
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(linked_workspace),
        )

        assert paths.workspace_path == str(real_workspace)
        assert str(real_workspace) in paths.write_paths
        assert str(linked_workspace) not in paths.write_paths

    def test_sensitive_gobby_roots_are_effectively_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        real_gobby_home = tmp_path / "real-gobby-home"
        real_gobby_home.mkdir()
        gobby_home = tmp_path / "gobby-home"
        gobby_home.symlink_to(real_gobby_home, target_is_directory=True)
        runtime_home = gobby_home / "gcode-runtime" / "current"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": "", "GOBBY_CODE_INDEX_RUNTIME_HOME": str(runtime_home)},
        )

        literal_protected = {
            str(gobby_home / name)
            for name in (
                "bootstrap.yaml",
                ".secret_kek",
                "local_cli_token",
                "gcode-runtime",
                "tools/srt",
            )
        }
        resolved_protected = {
            str((gobby_home / name).resolve())
            for name in (
                "bootstrap.yaml",
                ".secret_kek",
                "local_cli_token",
                "gcode-runtime",
                "tools/srt",
            )
        }
        assert literal_protected <= set(paths.deny_read_paths)
        assert resolved_protected <= set(paths.deny_read_paths)
        assert literal_protected <= set(paths.deny_write_paths)
        assert resolved_protected <= set(paths.deny_write_paths)
        for allowed in (*paths.read_paths, *paths.write_paths):
            allowed_path = Path(allowed)
            assert all(
                sensitive != allowed_path and not sensitive.is_relative_to(allowed_path)
                for sensitive in map(Path, resolved_protected)
            )
        assert str(runtime_home.resolve()) in paths.read_paths
        assert str(workspace.resolve()) in paths.write_paths

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("claude", True),
            ("codex", False),
            ("droid", False),
            ("qwen", False),
            ("grok", False),
        ],
    )
    def test_provider_declares_sensitive_path_enforcement(
        self, provider: str, expected: bool
    ) -> None:
        assert provider_capabilities(provider).sensitive_path_enforcement is expected

    def test_normalize_sandbox_path_tolerates_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path resolution can raise ValueError for malformed local paths."""

        def fail_resolve(self: Path, strict: bool = False) -> Path:
            _ = strict
            raise ValueError("bad path")

        monkeypatch.setattr(Path, "resolve", fail_resolve)

        assert _normalize_sandbox_path("/bad/path") == Path("/bad/path")

    def test_resolve_git_metadata_path_tolerates_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git metadata path resolution can fail on malformed or platform-specific paths."""

        def fail_resolve(self: Path, strict: bool = False) -> Path:
            _ = strict
            raise RuntimeError("bad path")

        monkeypatch.setattr(Path, "resolve", fail_resolve)

        assert _resolve_git_metadata_path(Path("/workspace"), ".git") == "/workspace/.git"

    def test_custom_daemon_port(self) -> None:
        """Test custom daemon port is set."""
        config = SandboxConfig(enabled=True)

        paths = compute_sandbox_paths(
            config=config,
            workspace_path="/project",
            gobby_daemon_port=9999,
        )

        assert paths.gobby_daemon_port == 9999

    def test_linked_worktree_git_metadata_dirs_are_writable(self, tmp_path: Path) -> None:
        """Linked worktree commits need write access to Git metadata outside the worktree."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.name", "Gobby Test")
        _git(repo, "config", "user.email", "test@gobby.local")
        (repo / "README.md").write_text("root\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "initial")

        worktree = tmp_path / "task-worktree"
        _git(repo, "worktree", "add", "-b", "task-worktree", str(worktree))

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True),
            workspace_path=str(worktree),
        )

        git_dir = _git_stdout(worktree, "rev-parse", "--git-dir")
        common_dir = _git_stdout(worktree, "rev-parse", "--git-common-dir")

        assert _resolve_from(worktree, git_dir) in paths.write_paths
        assert _resolve_from(worktree, common_dir) in paths.write_paths
        assert len(paths.write_paths) == len(set(paths.write_paths))

    def test_isolated_mcp_project_root_is_readable_but_not_writable(self, tmp_path: Path) -> None:
        """The MCP server's `uv run --project <main repo>` target must be readable.

        Without it the proxy subprocess dies on the main repo's pyproject.toml
        and the agent starts with no gobby tools at all (#19097).
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        workspace = tmp_path / "worktree"
        workspace.mkdir()
        (workspace / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "--project", str(main_repo), "gobby", "mcp-server"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
        )

        assert str(main_repo.resolve()) in paths.read_paths
        assert str(main_repo.resolve()) not in paths.write_paths

    def test_missing_or_malformed_mcp_config_grants_nothing_extra(self, tmp_path: Path) -> None:
        workspace = tmp_path / "worktree"
        workspace.mkdir()
        baseline = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
        )

        (workspace / ".mcp.json").write_text("{not json", encoding="utf-8")
        malformed = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
        )

        assert malformed.read_paths == baseline.read_paths

    def test_mcp_config_args_that_are_not_directories_are_ignored(self, tmp_path: Path) -> None:
        workspace = tmp_path / "worktree"
        workspace.mkdir()
        missing_dir = tmp_path / "gone"
        regular_file = tmp_path / "note.txt"
        regular_file.write_text("x\n", encoding="utf-8")
        (workspace / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": [
                                "run",
                                "--project",
                                str(missing_dir),
                                str(regular_file),
                                "relative/path",
                                None,
                            ],
                        },
                        "broken": "not-a-mapping",
                    }
                }
            ),
            encoding="utf-8",
        )

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
        )

        assert str(missing_dir.resolve()) not in paths.read_paths
        assert str(regular_file.resolve()) not in paths.read_paths


class TestSharedTempRootsAreNotGranted:
    """Agent scratchpads are the per-run sandbox tmp, never a shared temp root."""

    def test_shared_temp_roots_absent_from_computed_paths(self, tmp_path: Path) -> None:
        import tempfile as _tempfile

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
        )

        shared_roots = {
            _normalize_sandbox_path("/tmp"),
            _normalize_sandbox_path("/var/tmp"),
            _normalize_sandbox_path(_tempfile.gettempdir()),
        }
        assert shared_roots.isdisjoint(paths.write_paths)
        assert shared_roots.isdisjoint(paths.read_paths)


class TestToolchainGrants:
    """Toolchains stay reachable without mutable shared installation state."""

    @staticmethod
    def _fake_home(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        relative_roots: tuple[str, ...],
    ) -> Path:
        home = tmp_path / "home"
        for relative in relative_roots:
            (home / relative).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
        return home

    @staticmethod
    def _srt_can_read(path: Path, paths: ResolvedSandboxPaths) -> bool:
        """Model SRT v0.0.66: an allowRead ancestor wins over denyRead."""
        resolved = path.resolve()
        if any(resolved.is_relative_to(Path(root)) for root in paths.read_paths):
            return True
        return not any(resolved.is_relative_to(Path(root)) for root in paths.deny_read_paths)

    @staticmethod
    def _srt_can_write(path: Path, paths: ResolvedSandboxPaths) -> bool:
        """Model SRT write access, where denyWrite takes precedence."""
        resolved = path.resolve()
        if any(resolved.is_relative_to(Path(root)) for root in paths.deny_write_paths):
            return False
        return any(resolved.is_relative_to(Path(root)) for root in paths.write_paths)

    def test_default_write_paths_exclude_toolchain_executables_and_auto_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        unsafe_locations = (
            ".cargo/bin",
            ".cargo/config.toml",
            ".gradle/init.d",
            "go/bin",
            ".npm/_npx",
            ".bundle/plugin",
            ".nuget/plugins",
            ".pub-cache/bin",
            ".mix/escripts",
            ".local/bin",
        )
        home = self._fake_home(
            monkeypatch,
            tmp_path,
            tuple(str(Path(relative).parent) for relative in unsafe_locations),
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        write_paths = default_write_paths(SandboxConfig(enabled=True, backend="srt"), workspace)

        for write_path in map(Path, write_paths):
            for relative in unsafe_locations:
                unsafe_path = (home / relative).resolve()
                assert not unsafe_path.is_relative_to(write_path), (
                    f"{write_path} grants write access to {unsafe_path}"
                )

    def test_toolchain_credentials_are_effectively_denied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        credentials = (
            ".cargo/credentials",
            ".cargo/credentials.toml",
            ".npm/_auth",
            ".gradle/gradle.properties",
            ".m2/settings.xml",
            ".m2/settings-security.xml",
            ".gem/credentials",
            ".bundle/config",
            ".sbt/.credentials",
            ".ivy2/.credentials",
            ".nuget/NuGet/NuGet.Config",
            ".composer/auth.json",
            ".config/composer/auth.json",
            ".pub-cache/credentials.json",
            ".hex/hex.config",
        )
        home = self._fake_home(
            monkeypatch,
            tmp_path,
            tuple(str(Path(relative).parent) for relative in credentials),
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        for relative in credentials:
            credential = home / relative
            resolved = str(credential.resolve())
            assert resolved in paths.deny_read_paths, relative
            assert resolved in paths.deny_write_paths, relative
            assert not self._srt_can_read(credential, paths), relative
            assert not self._srt_can_write(credential, paths), relative

    def test_installed_toolchains_and_shared_caches_are_read_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Executables, SDKs, installed packages, and shared caches remain usable."""
        installed = (
            ".rustup",
            ".cargo/bin",
            "go",
            ".gradle/caches",
            ".m2/repository",
            ".nvm",
            ".rbenv",
            ".nuget/packages",
            ".pub-cache/hosted",
            ".sdkman",
        )
        home = self._fake_home(monkeypatch, tmp_path, installed)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        for relative in installed:
            assert str((home / relative).resolve()) in paths.read_paths, relative
            assert str((home / relative).resolve()) not in paths.write_paths, relative

    def test_absent_toolchain_roots_are_not_emitted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Existence filtering keeps the emitted policy tight per machine."""
        home = self._fake_home(monkeypatch, tmp_path, (".cargo/bin",))
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        assert str((home / ".cargo/bin").resolve()) in paths.read_paths
        for absent in (".rustup", "go", ".gradle", ".m2", ".pub-cache"):
            assert str((home / absent).resolve()) not in paths.read_paths, absent
            assert str((home / absent).resolve()) not in paths.write_paths, absent

    def test_package_installs_use_explicit_per_run_cache_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shared_caches = (
            ".cargo/registry",
            ".cache/uv",
            ".cache/pip",
            ".npm/_cacache",
            "go/pkg/mod",
            ".cache/go-build",
            ".gradle/caches",
            ".m2/repository",
            ".ivy2/cache",
            ".sbt/boot",
            ".gem/cache",
            ".bundle/cache",
            ".nuget/packages",
            ".composer/cache",
            ".pub-cache/hosted",
            ".hex/packages",
            ".luarocks",
            ".swiftpm",
        )
        home = self._fake_home(monkeypatch, tmp_path, shared_caches)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        per_run_caches = tuple(
            tmp_path / "run" / "tool-cache" / name
            for name in (
                "cargo",
                "uv",
                "pip",
                "npm",
                "go-build",
                "go-mod",
                "gradle",
                "maven",
                "ivy",
                "sbt",
                "gem",
                "bundle",
                "nuget",
                "composer",
                "pub",
                "hex",
                "mix",
                "luarocks",
                "swiftpm",
            )
        )

        paths = compute_sandbox_paths(
            config=SandboxConfig(
                enabled=True,
                backend="srt",
                allow_network=False,
                allow_package_registries=False,
                extra_write_paths=[str(path) for path in per_run_caches],
            ),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        for relative in shared_caches:
            shared_cache = home / relative
            assert self._srt_can_read(shared_cache, paths), relative
            assert not self._srt_can_write(shared_cache, paths), relative
        for per_run_cache in per_run_caches:
            resolved = str(per_run_cache.resolve())
            assert resolved in paths.read_paths, resolved
            assert resolved in paths.write_paths, resolved

    def test_registry_flag_gates_network_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """allow_package_registries controls egress; filesystem grants stay fixed."""
        self._fake_home(monkeypatch, tmp_path, (".cargo", ".npm"))
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        offline = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )
        networked = compute_sandbox_paths(
            config=SandboxConfig(
                enabled=True,
                backend="srt",
                allow_network=False,
                allow_package_registries=True,
            ),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        assert "registry.npmjs.org" not in offline.allowed_domains
        assert "registry.npmjs.org" in networked.allowed_domains
        assert set(offline.write_paths) == set(networked.write_paths)


class TestRtkSandboxGrants:
    """RTK keeps its developer-wide state while sandboxed agents run."""

    def test_rtk_binary_config_and_state_paths_are_granted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        binary = home / ".gobby" / "bin" / "rtk"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        workspace.mkdir()
        binary.parent.mkdir(parents=True)
        binary.touch()
        defaults = platform_paths(home=home)
        defaults.config_dir.mkdir(parents=True)
        custom_database = tmp_path / "rtk-state" / "history.db"
        custom_tee = tmp_path / "rtk-state" / "tee"
        (defaults.config_dir / "config.toml").write_text(
            f'[tracking]\ndatabase_path = "{custom_database}"\n[tee]\ndirectory = "{custom_tee}"\n',
            encoding="utf-8",
        )
        expected = platform_paths(home=home)
        expected.data_dir.mkdir(parents=True, exist_ok=True)

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        assert str(binary.resolve()) in paths.read_paths
        assert str(expected.config_dir.resolve()) in paths.read_paths
        assert str(expected.data_dir.resolve()) in paths.read_paths
        assert str(expected.data_dir.resolve()) in paths.write_paths
        assert str(custom_database.parent.resolve()) in paths.write_paths
        assert str(custom_tee.resolve()) in paths.write_paths

    def test_rtk_grants_do_not_include_unrelated_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        workspace.mkdir()
        expected = platform_paths(home=home)
        expected.data_dir.mkdir(parents=True)
        unrelated = expected.data_dir.parent / "other-tool"
        unrelated.mkdir()

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )

        assert str(unrelated.resolve()) not in paths.read_paths
        assert str(unrelated.resolve()) not in paths.write_paths
        assert not TestToolchainGrants._srt_can_write(unrelated / "state.db", paths)


class TestSandboxCacheProvisioning:
    """Sandboxed spawns get per-run caches for every env-redirectable toolchain."""

    def test_sandbox_spawn_provisions_per_run_toolchain_caches(self) -> None:
        env_vars = {GOBBY_SESSION_ID: "provision-test-session", "PATH": ""}

        config = sandbox_config_for_spawn(SandboxConfig(enabled=True, backend="srt"), env_vars)

        assert config is not None
        session_root = get_agent_session_cache_dir("provision-test-session", "gobby")
        for env_var in SANDBOX_CACHE_ENV_VARS:
            cache_path = Path(env_vars[env_var])
            assert cache_path.is_dir(), env_var
            assert cache_path.is_relative_to(session_root.parent), env_var
            assert str(cache_path) in config.extra_write_paths, env_var

    def test_sandbox_spawn_enables_scoped_package_registry_egress(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        env_vars = {GOBBY_SESSION_ID: "registry-test-session", "PATH": ""}

        config = sandbox_config_for_spawn(
            SandboxConfig(enabled=True, backend="srt", allow_network=False), env_vars
        )

        assert config is not None
        paths = compute_sandbox_paths(
            config=config,
            workspace_path=str(workspace),
            provider="codex",
            env=env_vars,
        )
        assert config.allow_package_registries is True
        assert {"crates.io", "index.crates.io", "static.crates.io"} <= set(paths.allowed_domains)
        assert "github.com" not in paths.allowed_domains

    def test_disabled_sandbox_skips_toolchain_cache_redirects(self) -> None:
        env_vars = {GOBBY_SESSION_ID: "provision-test-session", "PATH": ""}

        config = sandbox_config_for_spawn(SandboxConfig(enabled=False), env_vars)

        assert config is not None
        assert config.extra_write_paths == []
        assert config.allow_package_registries is False
        for env_var in SANDBOX_CACHE_ENV_VARS:
            assert env_var not in env_vars

    def test_non_sandbox_spawn_env_keeps_shared_toolchain_caches(self) -> None:
        env = build_spawn_cache_env("provision-test-session")

        for env_var in SANDBOX_CACHE_ENV_VARS:
            assert env_var not in env


def _daemon_sandbox_ns(**overrides: Any) -> SimpleNamespace:
    payload: dict[str, Any] = {
        "enabled": True,
        "backend": "srt",
        "mode": "permissive",
        "allow_network": False,
        "extra_read_paths": [],
        "extra_write_paths": [],
        "extra_deny_read_paths": [],
        "extra_deny_write_paths": [],
        "allowed_domains": [],
        "denied_domains": [],
        "allow_git_network": False,
        "allow_package_registries": False,
        "allow_unix_sockets": [],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


@pytest.mark.unit
class TestDaemonOwnedSandboxPolicyHash:
    def test_hash_version_is_explicit(self) -> None:
        assert _DAEMON_SANDBOX_POLICY_VERSION >= 2

    @pytest.mark.parametrize(
        "override",
        [
            {"backend": "provider-native"},
            {"allowed_domains": ["api.example.com"]},
            {"denied_domains": ["evil.test"]},
            {"allow_git_network": True},
            {"allow_package_registries": True},
            {"allow_unix_sockets": ["/tmp/gobby.sock"]},
            {"extra_deny_read_paths": ["/secret"]},
            {"extra_deny_write_paths": ["/secret-w"]},
        ],
        ids=[
            "backend",
            "allowed_domains",
            "denied_domains",
            "allow_git_network",
            "allow_package_registries",
            "allow_unix_sockets",
            "extra_deny_read_paths",
            "extra_deny_write_paths",
        ],
    )
    def test_complete_policy_field_changes_hash(self, override: dict[str, Any]) -> None:
        base = daemon_owned_sandbox_policy_hash(_daemon_sandbox_ns(), scope="web_chat")
        changed = daemon_owned_sandbox_policy_hash(_daemon_sandbox_ns(**override), scope="web_chat")
        assert changed != base

    def test_web_chat_hash_tracks_daemon_owned_hash(self) -> None:
        raw = _daemon_sandbox_ns(allowed_domains=["api.example.com"])
        daemon = SimpleNamespace(web_chat_sandbox=raw)
        assert web_chat_sandbox_policy_hash(daemon) == daemon_owned_sandbox_policy_hash(
            raw, scope="web_chat"
        )


_AGY_PATHS = ResolvedSandboxPaths(
    workspace_path="/project",
    read_paths=[],
    write_paths=["/project"],
    allow_external_network=False,
)

_AGY_PROBE_DOMAINS = (
    "daily-cloudcode-pa.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    "play.googleapis.com",
    "playwright*.azureedge.net",
    "googleusercontent.com",
)


def _agy_resolver() -> SandboxResolver:
    from gobby.agents import sandbox_resolvers as module

    resolver_cls = getattr(module, "AgySandboxResolver", None)
    assert resolver_cls is not None
    resolver = resolver_cls()
    if not isinstance(resolver, SandboxResolver):
        raise AssertionError("AgySandboxResolver did not return a SandboxResolver")
    assert resolver.cli_name == "agy"
    return resolver


@pytest.mark.unit
class TestAgySandboxResolver:
    """AGY sandbox resolver, policy maps, and capability-gate reachability (plan 3.2)."""

    # TDD recapture window for TestAgySandboxResolver.test_* named criteria.

    def test_agy_sandbox_resolver_emits_boolean_sandbox_for_provider_native(self) -> None:
        resolver = _agy_resolver()
        assert resolver.__doc__ is not None
        assert ".git" in resolver.__doc__
        assert "read-only" in resolver.__doc__.lower()

        args, env = resolver.resolve(
            SandboxConfig(enabled=True, backend="provider-native"),
            _AGY_PATHS,
        )
        assert args == ["--sandbox"]
        assert env == {}

    def test_agy_sandbox_resolver_emits_sandbox_false_when_srt_enforces(self) -> None:
        resolver = _agy_resolver()
        args, env = resolver.resolve(
            SandboxConfig(enabled=True, backend="srt", allow_network=False),
            _AGY_PATHS,
        )
        assert args == ["--sandbox=false"]
        assert env == {}

    def test_get_sandbox_resolver_returns_agy_through_capability_gate(self) -> None:
        assert provider_supports_sandbox("agy") is True
        try:
            resolver = get_sandbox_resolver("agy")
        except ValueError as exc:
            raise AssertionError("get_sandbox_resolver('agy') refused the provider") from exc
        assert type(resolver).__name__ == "AgySandboxResolver"
        assert resolver.cli_name == "agy"

    def test_agy_provider_capabilities_admit_sandbox_without_sensitive_path_proof(self) -> None:
        caps = provider_capabilities("agy")
        assert caps.sandbox is True
        assert caps.sensitive_path_enforcement is False
        assert caps.reasoning_flag == "claude-effort"
        assert provider_supports_sandbox("agy") is True

    def test_agy_sandbox_policy_uses_probe_recorded_domains_roots_and_empty_credential_env(
        self,
    ) -> None:
        domains = sandbox_policy._PROVIDER_DOMAINS.get("agy")
        assert domains is not None
        assert domains == _AGY_PROBE_DOMAINS

        auth_paths = sandbox_policy._PROVIDER_AUTH_PATHS.get("agy")
        assert auth_paths is not None
        assert "~/.gemini/antigravity-cli" in auth_paths
        assert "~/Library/Caches/ms-playwright-go" in auth_paths

        read_only = sandbox_policy._PROVIDER_AUTH_READ_ONLY_PATHS.get("agy")
        assert read_only is not None
        assert "~/.gemini/config/projects" in read_only
        assert "~/Library/Keychains/login.keychain-db" in read_only

        assert "agy" in sandbox_policy._PROVIDER_CREDENTIAL_ENV
        assert sandbox_policy._PROVIDER_CREDENTIAL_ENV["agy"] == ()

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path="/project",
            provider="agy",
            env={"PATH": ""},
        )
        for domain in _AGY_PROBE_DOMAINS:
            assert domain in paths.allowed_domains
        write_roots = sandbox_policy.provider_write_exceptions("agy")
        read_roots = sandbox_policy.provider_read_exceptions("agy", {"PATH": ""})
        for root in write_roots:
            assert root in paths.write_paths
            assert root in paths.read_paths
        for root in read_roots:
            assert root in paths.read_paths
        assert paths.credential_env_vars == []

    async def test_create_session_admits_agy_under_srt_and_rejects_provider_native(self) -> None:
        caps = provider_capabilities("agy")
        assert caps.sandbox is True
        assert caps.sensitive_path_enforcement is False

        native = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=DaemonConfig(
                web_chat_sandbox={"enabled": True, "backend": "provider-native"}
            ),
        )
        with pytest.raises(
            RuntimeError,
            match="agy cannot prove the sensitive-root contract under provider-native sandbox",
        ):
            await native.create_session(provider="agy", conversation_id="agy-native")

        srt = WebChatRuntimeManager(codex_client=None)
        record = SimpleNamespace(supported=True, reason="supported")
        with (
            patch(
                "gobby.providers.version_gate.ensure_agy_support",
                AsyncMock(return_value=record),
            ),
            pytest.raises(RuntimeError) as exc,
        ):
            await srt.create_session(provider="agy", conversation_id="agy-srt")
        assert "sensitive-root" not in str(exc.value)
