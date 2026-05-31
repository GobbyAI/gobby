"""
Tests for Sandbox Configuration Models.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents.sandbox import (
    ClaudeSandboxResolver,
    CodexSandboxResolver,
    GeminiSandboxResolver,
    QwenSandboxResolver,
    ResolvedSandboxPaths,
    SandboxConfig,
    SandboxResolver,
    _normalize_sandbox_path,
    _resolve_git_metadata_path,
    coerce_sandbox_config,
    compute_sandbox_paths,
    get_sandbox_resolver,
    materialize_claude_settings,
    materialize_claude_settings_async,
)

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
            SandboxResolver()

    def test_subclass_must_implement_cli_name(self) -> None:
        """Test that subclass must implement cli_name property."""

        class IncompleteResolver(SandboxResolver):
            def resolve(
                self, config: SandboxConfig, paths: ResolvedSandboxPaths
            ) -> tuple[list[str], dict[str, str]]:
                return ([], {})

        with pytest.raises(TypeError):
            IncompleteResolver()

    def test_subclass_must_implement_resolve(self) -> None:
        """Test that subclass must implement resolve method."""

        class IncompleteResolver(SandboxResolver):
            @property
            def cli_name(self) -> str:
                return "test"

        with pytest.raises(TypeError):
            IncompleteResolver()

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

    def test_no_external_paths_omits_filesystem_key(self) -> None:
        """Workspace-only write paths should not emit a filesystem block."""
        import json

        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, _env = ClaudeSandboxResolver().resolve(SandboxConfig(enabled=True), paths)
        settings = json.loads(args[1])

        assert "filesystem" not in settings["sandbox"]

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

    def test_materialize_claude_settings_merges_base_hooks(self, tmp_path) -> None:
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
    async def test_materialize_claude_settings_async_matches_sync_output(self, tmp_path) -> None:
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
        self, tmp_path, caplog: pytest.LogCaptureFixture
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
class TestGeminiSandboxResolver:
    """Tests for GeminiSandboxResolver."""

    def test_cli_name(self) -> None:
        """Test that cli_name returns 'gemini'."""
        resolver = GeminiSandboxResolver()
        assert resolver.cli_name == "gemini"

    def test_disabled_returns_empty(self) -> None:
        """Test that disabled sandbox returns empty args and env."""
        resolver = GeminiSandboxResolver()
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

    def test_enabled_returns_sandbox_flag(self) -> None:
        """Test that enabled sandbox returns exactly -s flag."""
        resolver = GeminiSandboxResolver()
        config = SandboxConfig(enabled=True)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert args == ["-s"]

    def test_permissive_sets_exact_seatbelt_profile(self) -> None:
        """Test permissive mode sets SEATBELT_PROFILE to permissive-open."""
        resolver = GeminiSandboxResolver()
        config = SandboxConfig(enabled=True, mode="permissive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert env["SEATBELT_PROFILE"] == "permissive-open"

    def test_restrictive_sets_exact_seatbelt_profile(self) -> None:
        """Test restrictive mode sets SEATBELT_PROFILE to restrictive-open."""
        resolver = GeminiSandboxResolver()
        config = SandboxConfig(enabled=True, mode="restrictive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert env["SEATBELT_PROFILE"] == "restrictive-open"

    def test_network_disabled_uses_proxied_profile(self) -> None:
        """Test disabled network maps to the documented proxied Seatbelt profile."""
        resolver = GeminiSandboxResolver()
        config = SandboxConfig(enabled=True, mode="permissive", allow_network=False)
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=False,
        )

        _args, env = resolver.resolve(config, paths)
        assert env["SEATBELT_PROFILE"] == "permissive-proxied"

    def test_permissive_returns_both_args_and_env(self) -> None:
        """Test permissive mode returns both -s flag and SEATBELT_PROFILE."""
        resolver = GeminiSandboxResolver()
        config = SandboxConfig(enabled=True, mode="permissive")
        paths = ResolvedSandboxPaths(
            workspace_path="/project",
            read_paths=[],
            write_paths=["/project"],
            allow_external_network=True,
        )

        args, env = resolver.resolve(config, paths)
        assert args == ["-s"]
        assert "SEATBELT_PROFILE" in env
        assert len(env) == 1  # Only SEATBELT_PROFILE

    def test_external_worktree_git_metadata_paths_emit_include_dirs(self, tmp_path: Path) -> None:
        """Linked worktree Git metadata outside the workspace is included."""
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

        args, env = GeminiSandboxResolver().resolve(SandboxConfig(enabled=True), paths)

        git_dir = _resolve_from(worktree, _git_stdout(worktree, "rev-parse", "--git-dir"))
        common_dir = _resolve_from(
            worktree,
            _git_stdout(worktree, "rev-parse", "--git-common-dir"),
        )
        assert args[0] == "-s"
        assert _include_dirs(args) == [git_dir, common_dir]
        assert env["SEATBELT_PROFILE"] == "permissive-open"

    def test_workspace_and_workspace_internal_write_paths_are_not_include_dirs(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        internal = workspace / ".git"
        nested = workspace / "nested" / "path"
        workspace.mkdir()

        paths = ResolvedSandboxPaths(
            workspace_path=str(workspace),
            read_paths=[],
            write_paths=[str(workspace), str(internal), str(nested), str(workspace)],
            allow_external_network=True,
        )

        args, _env = GeminiSandboxResolver().resolve(SandboxConfig(enabled=True), paths)

        assert args == ["-s"]

    def test_in_place_repo_git_metadata_emits_no_include_dirs(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")

        paths = compute_sandbox_paths(
            config=SandboxConfig(enabled=True),
            workspace_path=str(repo),
        )

        args, _env = GeminiSandboxResolver().resolve(SandboxConfig(enabled=True), paths)

        assert args == ["-s"]

    def test_external_write_paths_are_deduped(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        external = tmp_path / "repo" / ".git"
        workspace.mkdir()

        paths = ResolvedSandboxPaths(
            workspace_path=str(workspace),
            read_paths=[],
            write_paths=[str(workspace), str(external), str(external)],
            allow_external_network=True,
        )

        args, _env = GeminiSandboxResolver().resolve(SandboxConfig(enabled=True), paths)

        assert _include_dirs(args) == [str(external.resolve(strict=False))]

    def test_more_than_five_external_write_paths_raise_clear_error(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external_paths = [tmp_path / f"external-{index}" for index in range(6)]
        paths = ResolvedSandboxPaths(
            workspace_path=str(workspace),
            read_paths=[],
            write_paths=[str(workspace), *[str(path) for path in external_paths]],
            allow_external_network=True,
        )

        with pytest.raises(ValueError, match="at most 5 external --include-directories paths"):
            GeminiSandboxResolver().resolve(SandboxConfig(enabled=True), paths)


@pytest.mark.unit
class TestQwenSandboxResolver:
    """Tests for QwenSandboxResolver."""

    def test_cli_name(self) -> None:
        resolver = QwenSandboxResolver()
        assert resolver.cli_name == "qwen"

    def test_qwen_matches_gemini_seatbelt_profiles(self) -> None:
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

    def test_qwen_matches_gemini_include_dir_behavior(self, tmp_path: Path) -> None:
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

    def test_returns_gemini_resolver(self) -> None:
        """Test that 'gemini' returns GeminiSandboxResolver."""
        resolver = get_sandbox_resolver("gemini")
        assert isinstance(resolver, GeminiSandboxResolver)

    def test_returns_qwen_resolver(self) -> None:
        """Test that 'qwen' returns QwenSandboxResolver."""
        resolver = get_sandbox_resolver("qwen")
        assert isinstance(resolver, QwenSandboxResolver)

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
        assert "/tmp/output" in paths.write_paths
        assert "/opt/data" in paths.read_paths

    def test_workspace_always_in_write_paths(self) -> None:
        """Test that workspace is always included in write_paths."""
        config = SandboxConfig(enabled=True)

        paths = compute_sandbox_paths(
            config=config,
            workspace_path="/my/workspace",
        )

        assert "/my/workspace" in paths.write_paths

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
